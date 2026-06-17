---
name: wiki-index
description: >
  Régénère les index du wiki à deux niveaux (INDEX principal des sujets + INDEX par sujet :
  notions et grilles), lance l'audit liens en lecture seule (cassés, fantômes, entrées
  vides) et propose une **réparation assistée** des wikilinks cassés (opt-in, n'écrit
  que sur `--apply`). Déclencher sur « régénère les index », « audite les liens du wiki »,
  « répare les liens cassés », « mets à jour la carte des sujets ». Audit lecture seule ;
  écritures bornées à work_root et atomiques.
license: PolyForm-Noncommercial-1.0.0
metadata:
  version: "0.6.1"
  author: Vivian MAES
  tags: [knowledge-base, obsidian, index, audit]
  hermes:
    profile: mimir
    category: knowledge-management
    related_skills: [wiki-extract, wiki-ingest, wiki-reading-grid, wiki-sync]
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

# wiki-index

## Quand l'utiliser

Après compilation (`wiki-ingest`) et/ou génération de grilles (`wiki-reading-grid`), pour
**maintenir la navigation** et **vérifier l'intégrité** des liens. Trigger : « régénère les
index », « audite les liens », « mets à jour la carte des sujets ».

## Principe

Skill **100 % déterministe** : il n'y a **rien à rédiger**, tu enchaînes des sous-commandes.

```sh
python scripts/wiki_index.py <regenerate|audit> …
```

> `--config <wiki.config.json>` est **optionnel** : sans lui, le config est auto-découvert
> (`$MIMIR_CONFIG` → `dossier du profil` → `~/.config/mimir/wiki.config.json` → `./wiki.config.json`). Les exemples
> ci-dessous montrent `--config CFG` à titre indicatif ; il peut être omis.

## Procédure

1. **Régénération des index** — reconstruit `wiki/INDEX.md` + tous les `wiki/<sujet>/_INDEX.md` :
   ```sh
   python scripts/wiki_index.py --config CFG regenerate --skip-sync
   ```
   - **INDEX principal** : la liste des **sujets** → leur index de sujet. La **description**
     éditoriale d'un sujet (texte après le `—`) est **préservée** si tu l'as écrite à la main ;
     sinon, fallback automatique sur les 3 premières notions.
   - **INDEX par sujet** : les **notions** (résumé d'une ligne tiré du callout `>` de l'article)
     + les **grilles de lecture** rattachées (une grille est rattachée à chaque sujet qu'elle cite).
   - Les wikilinks sont émis dans la **forme majoritaire** du vault (préfixée `[[wiki/x]]` vs
     relative `[[x]]`). `--dry-run` calcule sans écrire.

2. **Audit liens** — **lecture seule**, 3 passes, **aucune écriture** :
   ```sh
   python scripts/wiki_index.py --config CFG audit          # rapport lisible
   python scripts/wiki_index.py --config CFG audit --json   # rapport machine
   ```
   - **Passe 1 — liens cassés** : un `[[…]]` d'un article/grille sans fichier cible.
   - **Passe 2 — fichiers fantômes** : un article réel non listé dans le `_INDEX.md` de son sujet.
   - **Passe 3 — index → vide** : un `[[…]]` d'un fichier d'index sans cible.

   Code retour **0 ssi 0 anomalie** (exploitable en CI). Un lien cassé = **travail restant**
   (notion à compiler, grille à régénérer), pas forcément un bug du pipeline. Périmètre :
   wikilinks `wiki/` + `reading-grids/` ; les liens vers `raw/` sont hors scope.

3. **Réparation assistée** des liens cassés — **opt-in, écrit sur demande** (sous-commande
   `fix`). Distincte de l'audit : elle **propose** un mapping pour chaque lien cassé et ne
   réécrit **que** si `--apply` est passé. Sans `--apply` : aperçu, 0 écriture.
   ```sh
   python scripts/wiki_index.py --config CFG fix                          # dry-run (défaut)
   python scripts/wiki_index.py --config CFG fix --apply                  # réécrit
   python scripts/wiki_index.py --config CFG fix --scope ea               # borne au sujet ea
   python scripts/wiki_index.py --config CFG fix --semantic-map map.json  # override manuel
   python scripts/wiki_index.py --config CFG fix --json                   # rapport machine
   ```
   - **Passes de matching** (chacune ne s'applique que si la précédente échoue) :
     0. **Override** `--semantic-map <json>` (mapping `{cible_cassée: cible_proposée}`), marqueur 🧠 ;
     1. **Identité** (la cible résout telle quelle), marqueur 🔧 ;
     2. **Préfixe commun char-par-char** ≥ 3 chars sur la notion, marqueur 🔧 ;
     3. **Matching par mots** (≥ 1 mot long ≥ 6 chars OU ≥ 2 mots courts communs,
        stop-words retirés), score `0.5 + 0.1·|mots communs| + 0.2·|mots longs communs|`,
        marqueur 🔧. Le score mots reste < score préfixe : on n'écrase jamais une évidence.
   - `--scope <sujet>` est **répétable** : ne retient que les liens dont la source est
     sous `wiki/<scope>/`.
   - La réécriture préserve les ancres `#…` et alias `|…` éventuels, et passe par
     `atomic_write_text` (confinement `work_root`). Un re-audit best-effort est
     lancé après `--apply` ; son résultat est dans le rapport.

## Format produit

```markdown
# Index du wiki

> Carte par sujet.

- [[navigation/_INDEX]] — pilotage, position, marées.
```

```markdown
# Index — navigation

## Notions

- [[navigation/relevement]] — Mesure de l'angle vers un amer.

## Grilles de lecture

- [[reading-grids/navigation-cotiere]] — Navigation côtière (12 ch.)
```

## Références

- Normalisation de chemins (`os.path.normpath`), forme majoritaire, NFD/NFC : [`../_shared-references/scripts/`](../_shared-references/scripts/) (`wikilinks.py`, `slug.py`) ; spec normative : `mimir-invariants::nfd-nfc-normalization`.
- Résumé d'article (callout `>`) : [`../_shared-references/scripts/article_index.py`](../_shared-references/scripts/article_index.py).
- Spécification : `specs/audit-index` (index-regenerable, link-audit-three-pass-readonly, ascii-slug-suggestion).
