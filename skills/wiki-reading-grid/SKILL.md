---
name: wiki-reading-grid
description: >
  Génère la grille de lecture d'un ouvrage : restitue l'ordre de lecture (chapitrage)
  perdu par l'éclatement en notions, en pointant dans le bon ordre vers les articles wiki
  avec liens Précédent/Suivant. Aucune duplication de contenu. Déclencher sur « génère la
  grille de lecture de cet ouvrage », « régénère la grille ». Borné à work_root.
license: PolyForm-Noncommercial-1.0.0
metadata:
  version: "0.6.1"
  author: Vivian MAES
  tags: [knowledge-base, obsidian, reading-grid, navigation]
  hermes:
    profile: mimir
    category: knowledge-management
    related_skills: [wiki-extract, wiki-ingest, wiki-index, wiki-sync]
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

# wiki-reading-grid

## Quand l'utiliser

Après compilation d'un ouvrage (`wiki-ingest`), pour restituer son **ordre de lecture** que
l'éclatement par notion a fait perdre. Trigger : « génère la grille de lecture de cet ouvrage »,
« régénère la grille ».

## Principe

Skill **100 % déterministe** : il n'y a **rien à rédiger**, tu enchaînes des sous-commandes.
La grille **ordonne des liens** — elle ne duplique **aucun** contenu. Elle croise trois sources
(SPEC §8) :

- le `<base>.toc.json` (clé `chapters`, **ordre = ordre de lecture**, non retrié) — l'ossature ;
- le **ledger** — les articles produits depuis cet ouvrage, dans l'ordre d'écriture ;
- les `sources` du frontmatter de chaque article — l'ancre `…#chK` rattache l'article au
  chapitre dont `order == K`.

> **`K` = numéro OU code d'ouvrage.** `#ch3` matche `order: 3` ; `#chG3` matche `order: "G3"`
> (codes type R1-R9 / G1-G6 / C1-C2). Dans tous les cas `K` **doit** matcher le champ `order`
> du `toc.json`, et l'**ordre des chapitres dans le `toc.json` fait foi** (la grille ne réordonne
> pas — trier des codes alphabétiquement casserait l'ordre canonique).

```sh
python scripts/wiki_reading_grid.py <generate|generate-all> …
```

> `--config <wiki.config.json>` est **optionnel** : sans lui, le config est auto-découvert
> (`$MIMIR_CONFIG` → `dossier du profil` → `~/.config/mimir/wiki.config.json` → `./wiki.config.json`). Les exemples
> ci-dessous montrent `--config CFG` à titre indicatif ; il peut être omis.

## Procédure

1. **Pré-sync** — implicite (stub en P3). `--skip-sync` dans une chaîne pour ne pas synchroniser
   plusieurs fois.
2. **Génération d'un ouvrage** — après `wiki-ingest finalize`, lance :
   ```sh
   python scripts/wiki_reading_grid.py --config CFG generate \
       --source raw/pdfs/navigation-cotiere.pdf.txt --skip-sync
   ```
   Sortie JSON : `grid_path`, `linked_articles`, `unresolved_chapters` (chapitres sans article),
   `orphan_articles` (articles sans ancre `#chK`), `chapters_missing_blurb` (chapitres
   au placeholder `_à rédiger_`). Le script écrit `reading-grids/<slug-du-titre>.md`
   et met à jour la colonne « Grille » de `raw/<type>/_status.md`.
3. **Régénération incrémentale** — relance `generate` après chaque vague de compilation : la grille
   est reconstruite **en entier** (idempotent ; seul `created` est préservé). À la
   régénération, les phrases éditoriales déjà présentes (zones `<!-- blurb:… -->`)
   sont **extraites** puis **réinjectées** par appariement sur l'`order` du
   `toc.json` (tolérant à un léger renommage de titre) — la charpente est
   recalculée, jamais la prose. Un chapitre sans article affiche `_à rédiger_`
   entre les marqueurs, pas une erreur. Un lien vers un article non encore compilé
   reste tel quel — l'**audit** `wiki-index` le remontera.
4. **Tout régénérer** — pour un rebuild complet (pdfs + epubs ; le web sans chapitrage est ignoré) :
   ```sh
   python scripts/wiki_reading_grid.py --config CFG generate-all --skip-sync
   ```
5. **Rédaction des phrases éditoriales (étape agent)** — la charpente est
   régénérée par le skill ; les **phrases** sont sémantiques. Après un
   `generate` (ou une campagne `wiki-migration v3→v4`), la sortie JSON
   liste `chapters_missing_blurb`. L'agent **rédige une phrase courte
   (une ligne) par chapitre** listé, en éditant directement la zone
   `<!-- blurb:début --> … <!-- blurb:fin -->` du fichier généré, puis
   relance `generate` (idempotent : les phrases sont préservées, la
   charpente est recalculée). **Aucun script ne juge la qualité des
   phrases** (revue humaine, par construction).
6. **Post-sync** — implicite (stub en P3), sauf `--skip-sync`.

## Format produit

```markdown
---
type: reading-grid
work: "Navigation côtière"
source: raw/pdfs/navigation-cotiere.pdf.txt
toc: raw/pdfs/navigation-cotiere.toc.json
chapters: 12
created: 2026-06-04
---

# Grille de lecture — Navigation côtière

## Ch. 3 — Se positionner

<!-- blurb:début -->
Comment fixer sa position en mer par recoupement de relèvements.
<!-- blurb:fin -->

Lire dans l'ordre :

1. [[navigation/relevement]]
2. [[navigation/triangulation]]

⬅ [[#Ch. 2 — Instruments]] | ➡ [[#Ch. 4 — Marées]]
```

> **Format hybride** : la **charpente** (ordre des chapitres, liens, navigation) est
> déterministe (ne dépend que du `toc.json` et du ledger). La **phrase**
> entre marqueurs `<!-- blurb:… -->` est éditoriale, écrite par l'agent,
> **préservée** d'une régénération à l'autre. Sans phrase, un chapitre
> sans article porte `_à rédiger_` entre les marqueurs. La phrase n'est
> **jamais** une recopie du corps d'un article (la règle d'or « aucun
> dump brut » reste entière) — c'est une **cadrage court** (une ligne).
>
> Les liens Précédent/Suivant pointent vers le **texte exact** du titre de chapitre (Obsidian
> résout `[[#…]]` par le texte du titre) : titre et lien dérivent de la même fonction, donc aucun
> lien interne cassé.

## Références

- Gabarit de grille : [`../_shared-references/FRONTMATTERS.md`](../_shared-references/FRONTMATTERS.md) §2.
- Croisement toc/ledger/ancres, ancrage chapitre : [`../_shared-references/scripts/`](../_shared-references/scripts/) (`article_index.py`, `wikilinks.py`, `ledger.py`).
- Liens/Mermaid + slug/NFD-NFC : spec `reading-grid` (ordre canonique, ancres alphanumériques) + spec `mimir-invariants::mermaid-mandatory` + spec `mimir-invariants::nfd-nfc-normalization`.
- Spécification : `specs/reading-grid` (toc-order-is-canonical, chapter-anchor-flexible).
