# Backend rclone — kDrive WebDAV (pièges & procédure)

> Savoir battle-tested (repris de l'ancien `kb-sync`). Le WebDAV kDrive est un remote
> **sans modtime ni hash fiable** : la synchro doit s'y adapter au mot près.

## Règles d'or

- **`--size-only` est obligatoire.** `--checksum` force le téléchargement des fichiers
  pour comparer (catastrophique en bande passante) ; `--compare modtime` provoque des
  listings incohérents et des **suppressions ping-pong** (des fichiers disparaissent
  localement parce que le remote les a « perdus » entre deux runs). `--size-only` est la
  seule comparaison viable.
- **Verrou hors zone synchronisée** (`~/.cache/mimir/wiki-sync.lock`) : sinon il se
  synchronise et le verrou d'une machine bloque les autres.
- **Warm-up listing avant `bisync`** : `rclone lsf <remote> --recursive` (résultat
  jetable) force kDrive à rafraîchir son cache de listing, sans quoi les fichiers
  entrants peuvent être manqués au premier passage.
- **Retry borné + backoff exponentiel** sur erreurs transitoires (5xx, timeout,
  `connection refused`, etc.) — au plus 3 tentatives (1s, 2s, 4s).
- **Self-heal `bisync --resync` (union sans perte)** quand bisync signale « must run
  --resync ». **Aucun** `rclone sync` unidirectionnel destructif n'est tenté en repli
  silencieux (il pouvait effacer côté distant des fichiers entrants au lieu de les
  récupérer).

## Commande bisync (pull/push)

```
rclone bisync <local> <remote> \
  --size-only --conflict-loser pathname --conflict-suffix sync-conflict \
  --max-delete 25 --resilient --recover --copy-links \
  --filter-from filters.txt
```

`--check-access` et `--track-renames` sont **retirés** (incompatibles WebDAV kDrive :
pas de hash commun, pas de modtime fiable).

## Premier run / état bisync perdu

`bisync` exige un `--resync` initial ; sans état antérieur il refuse (« Must run
--resync »). Le backend **s'auto-répare** en relançant la même commande avec
`--resync` ajouté (mêmes flags, **plus** `--resync`, **sans** `--resync-mode`) :

```
rclone bisync <local> <remote> … --resync --filter-from filters.txt
```

L'absence de `--resync-mode` préserve `--conflict-loser pathname --conflict-suffix
sync-conflict` : un fichier divergent des deux côtés produit **deux copies**
(union sans perte) au lieu d'un écrasement silencieux. `--resync` est **bidirectionnel**,
donc les suppressions restent propagées — pas le comportement destructif du
`rclone sync` unidirectionnel.

> **Replis successifs (en cas d'échec du `--resync`) :**
> 1. Retry avec backoff sur erreur transitoire (jusqu'à 3 tentatives).
> 2. Si l'échec persiste, le backend **renvoie un code non nul** et **ne tente
>    PAS** de `rclone sync` unidirectionnel destructif en repli silencieux.
>    L'opérateur tranche, l'état local est préservé.

> **`auto_resync` (clé de config) est obsolète.** Le self-heal `bisync --resync`
> (union) est désormais **inconditionnel** (spec `sync-vault` — modification du
> backend rclone). La clé est **conservée** dans le config pour ne pas casser
> les configs existants, mais n'a plus d'effet.

### Auth pilotée par le JSON (opt-in)

Avec `sync.rclone.remote_setup`, le backend crée/répare le remote rclone **avant** la synchro,
sans `rclone config` manuel :

```jsonc
"remote_setup": {
  "url": "https://connect.drive.infomaniak.com/<id>/<dossier>",
  "vendor": "other",
  "user": "<login kDrive>",
  "pass_env": "MIMIR_KDRIVE_PASS"   // NOM de la variable d'env, pas le mot de passe
}
```

- Le **mot de passe d'application kDrive n'est jamais dans le JSON** : `pass_env` nomme la
  variable d'environnement qui le porte (à poser dans le `.env` du profil Hermes, exclu des
  fichiers « possédés » par `distribution.yaml`, donc jamais écrasé par `hermes update`).
- Remote absent ⇒ `rclone config create … webdav … --obscure` ; remote présent ⇒ `rclone
  config update … pass … --obscure` → **rafraîchit le secret et répare un 401** au run suivant.
- Si la variable d'env est absente, le backend n'écrit rien et émet un warning (la synchro
  peut alors échouer en 401) — il ne plante pas pour autant.
- **Caveat secret** : le mot de passe transite en clair par `argv` de `rclone config` (bref
  passage dans la table des process). Le backend ne le journalise jamais. Variante « zéro
  écriture » possible hors backend : définir le remote par variables `RCLONE_CONFIG_<NOM>_*`
  plutôt que muter `rclone.conf`.

> **Piège — cohérence du chemin.** L'état bisync est indexé par rclone sur le **chemin
> absolu** des deux côtés. Le backend utilise `cfg.work_root`, que `config_loader` **résout
> via `realpath()`** (symlinks et `..` résolus — ex. sur macOS `/tmp` → `/private/tmp`). Le
> `--resync` manuel doit donc viser **exactement ce chemin résolu**, sinon bisync ne
> retrouve pas l'état (« cannot find prior listings ») et le backend abandonne (sans
> `rclone sync` destructif en repli silencieux). En cas de doute :
> `python config_loader.py wiki.config.json` affiche le `WORK_ROOT` résolu à utiliser.

## Validation anti-listing-stale (le piège majeur)

`bisync` renvoie **0 même quand le listing kDrive est périmé** et qu'aucun fichier n'a
transféré (symptôme : `rclone lsf <remote>/wiki/` répond `directory not found` alors que
le log dit « Bisync successful »). `validate` ne fait donc **pas** confiance au code 0 :

1. compter les fichiers locaux (hors `.git/`, `.obsidian/`, `.wiki/`, `.DS_Store`) ;
2. compter les fichiers distants via `rclone lsf <remote> --recursive --files-only` ;
3. si l'écart dépasse la tolérance (~5, marge pour les exclusions), **re-réconcilier**
   (warm-up, puis `bisync` avec self-heal `--resync` si besoin) puis revérifier `lsf`.
   Pas de `rclone sync` unidirectionnel destructif comme recovery (cf. spec
   `sync-vault`).

## Warm-up listing (avant chaque `bisync`)

```
rclone lsf <remote> --recursive
```

Émis systématiquement avant chaque `bisync` (pull et push) — le résultat est **ignoré**
(on ne compte pas, on ne fait rien avec) ; seul effet de bord : forcer kDrive à
rafraîchir son cache. Corrige le symptôme « fichiers entrants invisibles au premier
run ».

## Retry borné + backoff (erreurs transitoires)

Patterns détectés (sous-chaînes minuscules dans stdout/stderr cumulés) :

```
5xx, timeout, timed out, temporarily unavailable, try again,
connection reset, connection refused, i/o timeout, deadline exceeded,
service unavailable, bad gateway, gateway timeout, network is unreachable
```

Backoff exponentiel : 1s, 2s, 4s (3 tentatives max). Erreur non transitoire (config
invalide, fichier introuvable, etc.) → pas de retry, propagation immédiate.

> Les warnings du backend agrègent **une** entrée par classe d'événement :
> « `bisync` tentative 1/3 échouée (transient) — retry dans 1s. » — jamais une par
> ligne de log brute.

## Filtres (`filters.txt`)

Whitelist inversée (`+ **` final) excluant `.obsidian/`, `.git/`, `.wiki/` (ledger), le
lock, `**/.DS_Store`, `**/*.sync-conflict.*`, et les dossiers de quarantaine/corbeille.
**Après création** d'un dossier `_trash-*/` ou `.quarantine/`, ajouter immédiatement la
ligne d'exclusion **avant** le prochain sync, sinon rclone propage la corbeille vers kDrive.

## Pré-requis (setup machine, une fois)

- Remote `rclone config` (type `webdav`, vendor `other`, URL + mot de passe d'application
  kDrive) — **ou** `sync.rclone.remote_setup` + le secret en variable d'env, et l'agent le
  configure seul (cf. « Auth pilotée par le JSON »).
- **Un seul synchroniseur par machine** : auto-sync Remotely Save **désactivé** (cf.
  [`OBSIDIAN_REMOTELY_SAVE.md`](OBSIDIAN_REMOTELY_SAVE.md)).
- En mode `auto` (cf. SKILL.md) : aucun setup rclone manuel requis si Obsidian gère la
  synchro ; rclone n'est invoqué qu'en repli (Obsidian indisponible ou validate KO).
