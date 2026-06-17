---
name: wiki-init
description: >
  Initialise le second cerveau Mimir au premier usage : crée l'arborescence du vault
  (_inbox, raw, wiki, reading-grids), un accueil dans _inbox, un INDEX initial, puis
  amorce la première synchro. Affiche aussi l'emplacement résolu du wiki. Déclencher sur
  « initialise le wiki », « prépare le second cerveau », « où est mon wiki », « premier
  démarrage ». Idempotent ; strictement borné à work_root (confinement).
license: PolyForm-Noncommercial-1.0.0
metadata:
  version: "0.6.1"
  author: Vivian MAES
  tags: [knowledge-base, obsidian, init, bootstrap, setup]
  hermes:
    profile: mimir
    category: knowledge-management
    related_skills: [wiki-extract, wiki-ingest, wiki-reading-grid, wiki-index, wiki-sync]
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
        description: Backend de synchronisation du vault (rclone | git)
        default: rclone
        prompt: Backend de synchro
---

# wiki-init

> Amorçage du vault au **premier usage**. Sur un `work_root` vide, rien n'existe encore
> (pas de `_inbox/` où déposer les sources, pas d'`INDEX.md`, pas d'état de synchro) :
> ce skill pose la structure, l'explique, et lance la première synchro.

## Quand l'utiliser

- **Premier démarrage** : « initialise le wiki », « prépare le second cerveau ».
- **Doute sur l'emplacement** : « où est mon wiki ? » → `status` affiche le
  `wiki.config.json` résolu et tous les chemins dérivés (work_root, _inbox, raw, wiki…).
- **Après-coup** : rejouable sans risque (idempotent — ne réécrit jamais un fichier existant).

## CLI

```
wiki_init.py [--config <wiki.config.json>] {apply|status} [--skip-sync]
```

> `--config` est **optionnel** : sans lui, le config est auto-découvert dans l'ordre
> `$MIMIR_CONFIG` → `dossier du profil` → `~/.config/mimir/wiki.config.json` → `./wiki.config.json`.

| Commande | Effet |
| -------- | ----- |
| `status` | **Lecture seule.** Imprime `CONFIG_PATH` + chemins dérivés et indique, pour chaque dossier/fichier de base, s'il existe (✓) ou manque (✗). Répond à « où est mon wiki ». |
| `apply`  | **(défaut)** Crée la racine + l'arborescence (`_inbox/`, `raw/`, `wiki/`, `reading-grids/`), écrit un accueil `_inbox/LISEZ-MOI.md` et un `wiki/INDEX.md` initial **s'ils sont absents**, puis amorce la synchro (`pull` → `push` sous verrou). |

`--skip-sync` : pose la structure sans toucher au distant (hors-ligne / backend `noop`).

## Ce qui est créé

```
work_root/
├─ _inbox/           ← dropzone : on y dépose PDF / EPUB / URL à digérer
│  └─ LISEZ-MOI.md   ← mode d'emploi (créé si absent)
├─ raw/              ← matière brute immuable (peuplée par wiki-extract)
├─ wiki/             ← savoir construit par notion (peuplé par wiki-ingest)
│  └─ INDEX.md       ← index initial (créé si absent ; régénéré par wiki-index)
└─ reading-grids/    ← grilles de lecture par ouvrage (peuplées par wiki-reading-grid)
```

`.wiki/` (ledger d'ingestion) n'est **pas** créé ici : il apparaît au premier `wiki-ingest`.

## Après l'init

1. Déposer une première source dans `_inbox/`, puis `wiki-extract`.
2. Compiler : `wiki-ingest`.
3. **Régénérer l'index complet** une fois du contenu présent : `wiki-index regenerate`
   (l'`INDEX.md` posé ici n'est qu'un point de départ).

## Références

- Confinement : spec `mimir-invariants::confinement`. Amorce & idempotence : spec `bootstrap-vault` (apply / status / initial sync).
- Chargeur de config & garde : [`../_shared-references/scripts/`](../_shared-references/scripts/).
- Moteur de synchro : [`../_shared-references/scripts/sync/`](../_shared-references/scripts/sync/).
- Spécification : `specs/mimir-invariants::confinement` + `specs/bootstrap-vault` + `specs/sync-vault`.
