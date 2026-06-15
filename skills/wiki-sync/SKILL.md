---
name: wiki-sync
description: >
  Synchronise le vault Mimir avec son stockage distant, en pré et post-compilation.
  Backend pluggable (rclone, git, obsidian, auto) sélectionné par wiki.config.json. Le
  mode `auto` préfère Obsidian (Remotely Save via Local REST API) et bascule sur
  rclone durci (warm-up + retry + self-heal union) si Obsidian est indisponible.
  Déclencher sur « synchronise le vault », « pull/push le second cerveau »,
  « lock/validate la synchro ». Périmètre strictement borné à work_root
  (confinement) ; verrou hors zone synchronisée.
license: PolyForm-Noncommercial-1.0.0
metadata:
  version: "0.6.0"
  author: Vivian MAES
  tags: [knowledge-base, obsidian, sync, rclone, git]
  hermes:
    profile: mimir
    category: knowledge-management
    related_skills: [wiki-extract, wiki-ingest, wiki-reading-grid, wiki-index]
    config:
      - key: mimir.config_path
        description: "Chemin du wiki.config.json (sinon auto-découverte : MIMIR_CONFIG → ~/.config/mimir → ./)"
        default: "~/.config/mimir/wiki.config.json"
        prompt: Emplacement du wiki.config.json
      - key: mimir.work_root
        description: Racine unique du second cerveau (vault complet ou sous-répertoire dédié)
        default: ""
        prompt: Racine de travail (work_root)
      - key: mimir.sync.backend
        description: Backend de synchronisation du vault (rclone | git | obsidian | auto)
        default: rclone
        prompt: Backend de synchro
---

# wiki-sync

> Synchronisation pluggable du vault (Phase 4). Interface `lock` / `pull` / `push` /
> `validate`, quatre backends (`rclone`, `git`, `obsidian`, `auto`), bornée à
> `work_root`.

## Quand l'utiliser

- **Avant** toute lecture de `raw/` (pré-sync `pull`) et **après** toute écriture wiki
  (post-sync `push`) — déjà câblé dans `wiki-extract` et `wiki-ingest`/`wiki-reading-grid`/`wiki-index`.
- **À la demande** : « synchronise le vault » → `sync` (cycle `pull → push → validate`).

## Architecture

Le moteur vit dans le socle partagé `_shared-references/scripts/sync/` (package importé
par tous les skills via `import sync`). Ce skill n'expose qu'un **CLI mince** par-dessus :

```
wiki_sync.py [--config <wiki.config.json>] {lock|pull|push|validate|sync} [--dry-run]
```

> `--config` est **optionnel** : sans lui, le config est auto-découvert dans l'ordre
> `$MIMIR_CONFIG` → `dossier du profil` → `~/.config/mimir/wiki.config.json` → `./wiki.config.json`.

Backend choisi par `sync.backend` du config (`rclone` | `git` | `obsidian` | `auto`).
**Sans clé `sync`**, le backend `noop` est utilisé (travail local, « contenu d'abord,
synchro ensuite »).

| Op           | Sémantique                                             | Échec                                      |
| ------------ | ------------------------------------------------------ | ------------------------------------------ |
| `lock`       | Verrou mutex inter-process, **hors zone synchronisée** | déjà tenu ⇒ skip propre (code 0)           |
| `pull`       | Récupère l'état distant → local **avant** compilation  | non-zéro ⇒ stop, ne pas compiler           |
| `push`       | Publie local → distant **après** compilation           | non-zéro ⇒ signalé, travail local conservé |
| `validate`   | Vérifie la cohérence local↔distant après push          | écart ⇒ remédiation / échec contrôlé       |

> **Périmètre = `work_root`** (confinement, §2). En **mode B** (répertoire dédié), seul
> ce répertoire est synchronisé. Le **verrou vit hors** `work_root` (`~/.cache/mimir/`),
> sinon il se synchroniserait et bloquerait les autres machines (§12.9).

## Backends

- **rclone** (kDrive WebDAV) : `bisync --size-only` (avec `--conflict-loser pathname`,
  `--max-delete 25`, `--resilient --recover`, `--filter-from filters.txt`) **durci** :
  **warm-up listing** (`rclone lsf --recursive`, résultat jetable) avant bisync ;
  **retry borné avec backoff** (3 tentatives : 1s, 2s, 4s) sur erreurs transitoires
  (5xx, timeout, etc.) ; **self-heal `bisync --resync` (union sans perte)** en cas
  d'état perdu — **PAS** de repli `rclone sync` unidirectionnel destructif
  silencieux ; `validate` anti-listing-stale (comptage local vs `rclone lsf`).
  Détails et pièges : [`references/RCLONE_KDRIVE.md`](references/RCLONE_KDRIVE.md).
- **git** : `pull --rebase` ; `add`/`commit`/`push` scopé (`_inbox/`, `raw/`, `wiki/`,
  `reading-grids/`) ; `validate` = `HEAD` local vs `origin/<branch>`. Détails :
  [`references/GIT_BACKEND.md`](references/GIT_BACKEND.md).
- **obsidian** : délègue la synchro à **Remotely Save** (plugin Obsidian) via le
  plugin **Local REST API** — `POST {base_url}/commands/{command_id}/` avec Bearer ;
  secret via `api_key_env` (jamais dans le JSON). `pull`/`push`/`sync` déclenchent la
  même commande (Remotely Save est bidirectionnel, propage les suppressions) ;
  `validate` réutilise `rclone lsf` après `post_sync_wait_s`. Détails et pré-requis :
  [`references/OBSIDIAN_REMOTELY_SAVE.md`](references/OBSIDIAN_REMOTELY_SAVE.md).
- **auto** : sonde Obsidian (`GET /`) ; si KO et `auto_launch = true` et affichage
  disponible → lance Obsidian et poll readiness ; si Obsidian utilisable → `obsidian`,
  sinon → `rclone` durci. `validate` post-push : si Obsidian KO, **bascule sur
  rclone dans le même run** (push + re-validate). **Un seul** synchroniseur par run
  (jamais en parallèle).

## Configuration (`wiki.config.json`)

```json
{
  "sync": {
    "backend": "auto",
    "obsidian": {
      "base_url": "https://127.0.0.1:27124",
      "api_key_env": "MIMIR_OBSIDIAN_API_KEY",
      "vault_name": "kb",
      "auto_launch": true,
      "ready_timeout_s": 30,
      "post_sync_wait_s": 20,
      "verify_tls": false
    },
    "rclone": { "remote": "mimir:Vault/kb", "filters": "filters.txt", "lock_dir": "~/.cache/mimir/wiki-sync.lock" },
    "git":    { "repo_root": "/chemin/vault", "branch": "main", "scope": ["_inbox", "raw", "wiki", "reading-grids"] }
  }
}
```

## Références

- Confinement + synchro : spec `mimir-invariants::confinement` + spec `sync-vault` (lock hors work_root, validation post-sync, cycle `lock → pull → push → validate → unlock`).
- Chargeur de config & garde : [`../_shared-references/scripts/`](../_shared-references/scripts/).
- Moteur de synchro : [`../_shared-references/scripts/sync/`](../_shared-references/scripts/sync/).
- Filtres rclone : [`../../filters.txt`](../../filters.txt).
- Spécification : `specs/sync-vault` (pluggable-backends, post-sync-validation, cycle-order).
