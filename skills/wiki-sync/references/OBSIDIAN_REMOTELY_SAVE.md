# Backend `obsidian` — délégation à Remotely Save (Local REST API)

> Le backend `obsidian` de Mimir ne **réimplémente pas** un synchroniseur : il
> déclenche le plugin **Remotely Save** d'Obsidian via le plugin **Local REST
> API**. Remotely Save est per-fichier, à base d'état (DB), **propage les
> suppressions** dans les deux sens — battle-tested et bien plus tolérant que
> `rclone bisync` sur kDrive WebDAV. Le mode `auto` (cf.
> [`SKILL.md`](../SKILL.md)) préfère Obsidian quand il est disponible et
> bascule sur rclone sinon (jamais les deux en parallèle — stand-down).

## Plugins Obsidian requis

1. **[Local REST API](https://github.com/coddingtonbear/obsidian-local-rest-api)**
   — expose l'API HTTPS sur `https://127.0.0.1:27124` (port par défaut),
   certificat **auto-signé**, auth `Authorization: Bearer <clé>`. La clé se
   génère dans les paramètres du plugin : **ne la mettez jamais dans
   `wiki.config.json`** (cf. `api_key_env` ci-dessous).
2. **[Remotely Save](https://github.com/remotely-save/remotely-save)** —
   synchronisation bidirectionnelle vers kDrive / S3 / Dropbox / WebDAV / etc.
   Configurez-le une fois (remote + dossier) ; Mimir n'agit que comme
   **déclencheur** (commande `remotely-save:start-sync`).

> **Auto-sync Remotely Save : DÉSACTIVÉ.** Un seul synchroniseur par machine
> (cf. règle kDrive). Mimir est le seul déclencheur — l'intervalle automatique
> de Remotely Save doit être **off** ou **coordonné** avec Mimir.

## Configuration

```jsonc
"sync": {
  "backend": "obsidian",            // ou "auto" (cf. SKILL.md)
  "obsidian": {
    "base_url": "https://127.0.0.1:27124",       // HTTPS Local REST API (défaut 27124)
    "api_key_env": "MIMIR_OBSIDIAN_API_KEY",     // NOM de var d'env, jamais le secret
    "vault_name": "kb",                          // optionnel : URI macOS
    "command_id": "remotely-save:start-sync",    // défaut ; dry-run : …-dry-run
    "auto_launch": true,                         // opt-in : lance Obsidian si KO
    "launch_cmd": null,                          // override par plateforme
    "ready_timeout_s": 30,                       // poll readiness après launch
    "post_sync_wait_s": 20,                      // settle après trigger
    "verify_tls": false                          // cert auto-signé toléré
  }
}
```

> **Secret hors JSON** : la clé du Local REST API est référencée par **nom de
> variable d'environnement** (`api_key_env`). Convention : poser le secret
> dans le `.env` du profil Hermes (exclu des fichiers « possédés » par
> `distribution.yaml`, donc jamais écrasé par `hermes update`).

## Cycle

`pull` / `push` / `sync` côté CLI délèguent au **même** trigger (Remotely
Save est bidirectionnel — il n'y a pas de « pull » et « push » sémantiquement
distincts) :

1. **Sonde `GET {base_url}/`** — readiness (200 = Obsidian joignable).
2. **`POST {base_url}/commands/{command_id}/`** avec `Authorization: Bearer
   <clé lue depuis `api_key_env`>` — `204 No Content` = commande
   *dispatchée* (fire-and-forget : pas la fin de sync).
3. **Attente `post_sync_wait_s`** — laisse Remotely Save terminer (settle).
4. **`validate`** (à l'étape suivante du cycle CLI) : compte local ↔
   `rclone lsf <remote> --recursive --files-only`. L'écart dans la tolérance
   = succès. **Hors tolérance = échec** (le mode `auto` peut alors basculer
   sur rclone dans le même run).

## Mode `auto` : Obsidian d'abord, rclone en repli

`sync.backend = "auto"` instancie un wrapper (`AutoBackend`) qui :

1. Sonde Obsidian (`GET /`).
2. Si KO + `auto_launch = true` + affichage disponible → lance Obsidian
   (commande par plateforme, cf. plus bas) puis poll readiness.
3. Si Obsidian utilisable → backend `obsidian`. Sinon → backend `rclone`
   (repli durci : warm-up, retry, self-heal `--resync` union).
4. `validate` post-push : si Obsidian est KO, **bascule sur rclone dans le
   même run** (push + re-validate) — l'erreur est résolue sans intervention.

> **Stand-down** : rclone n'est **jamais** appelé en parallèle d'Obsidian.
> Le verrou global de Mimir (`~/.cache/mimir/wiki-sync.lock`, hors zone
> synchronisée) couvre Mimir-vs-Mimir ; ici, en complément, Obsidian-vs-rclone
> est sérialisé au sein du run. **Un seul synchroniseur par run.**

## Auto-lancement Obsidian (best-effort, opt-in)

Garde-fous (cf. spec `sync-vault` requirement « auto-launch best-effort ») :

- **Opt-in** : `auto_launch = true` (défaut `false`).
- **Linux** : `$DISPLAY` ou `$WAYLAND_DISPLAY` doit être défini (sinon
  headless → pas de tentative de lancement, repli rclone immédiat).
- **macOS** : toujours autorisé (affichage implicite).
- **Commande** :
  - macOS : `open -a Obsidian` par défaut ; ou `open
    "obsidian://open?vault=<vault_name>"` si `vault_name` est défini.
  - Linux : `launch_cmd` **requis** (chemin du binaire, AppImage, ou
    `flatpak run md.obsidian.Obsidian`).
- **Readiness** : poll `GET {base_url}/` jusqu'à 200 dans la limite
  `ready_timeout_s` (cold start + chargement des plugins = quelques
  secondes).
- **Échec** (binaire absent, pas d'affichage, timeout) → **repli rclone**,
  **jamais d'exception** (cf. `launch()`/`wait_until_ready()` qui ne
  lèvent jamais).

## Un seul synchroniseur par machine

C'est une règle d'or kDrive, déjà documentée pour rclone. Avec `obsidian` /
`auto`, elle se double : **ne pas avoir deux synchroniseurs concurrents sur
le même vault** (sinon suppressions ping-pong, conflits). Mimir et Remotely
Save ne doivent **pas** synchroniser en parallèle :

- **Auto-sync Remotely Save** : intervalle **off** (ou long, hors fenêtre
  d'exécution de Mimir).
- Mimir est le seul déclencheur de Remotely Save.
- Le verrou Mimir (`~/.cache/mimir/wiki-sync.lock`) couvre les runs
  concurrents de Mimir — pas Remotely Save.

## Diagnostic

```bash
python wiki-sync/scripts/wiki_sync.py --config wiki.config.json lock
# → "✅ verrou disponible"

python wiki-sync/scripts/wiki_sync.py --config wiki.config.json pull
# → POST /commands/remotely-save:start-sync/ → 204, attente 20s
# → ✅ pull (code 0)

python wiki-sync/scripts/wiki_sync.py --config wiki.config.json validate
# → rclone lsf <remote> --recursive --files-only vs compte local
# → ✅ validate (code 0)
```

## Sources vérifiées

- **Remotely Save** — `manifest.id = "remotely-save"`, commande
  `start-sync` ⇒ id complet `remotely-save:start-sync` ; dry-run :
  `remotely-save:start-sync-dry-run` :
  <https://github.com/remotely-save/remotely-save>
- **Obsidian Local REST API** — `POST /commands/{commandId}/` (→ 204), lister
  `GET /commands/`, HTTPS port 27124 (cert auto-signé), auth
  `Authorization: Bearer` :
  <https://coddingtonbear.github.io/obsidian-local-rest-api/> /
  <https://github.com/coddingtonbear/obsidian-local-rest-api>
