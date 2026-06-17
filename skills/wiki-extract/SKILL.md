---
name: wiki-extract
description: >
  Extrait une source (PDF, EPUB, URL) en matière brute immuable dans raw/<type>/.
  Extracteurs pluggables exposant un contrat commun ; PDF avec OCR auto si pas de
  couche texte. Déclencher sur « extrais ce PDF/EPUB/URL en raw », « ajoute cette
  source », « clippe cette page ». N'écrit que dans raw/ (confinement).
license: PolyForm-Noncommercial-1.0.0
metadata:
  version: "0.6.1"
  author: Vivian MAES
  tags: [knowledge-base, obsidian, extraction, pdf, epub, ocr, scraping]
  hermes:
    profile: mimir
    category: knowledge-management
    related_skills: [wiki-ingest, wiki-reading-grid, wiki-index, wiki-sync]
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

# wiki-extract

## Quand l'utiliser

Pour déposer une nouvelle source dans le second cerveau. Trigger : « extrais ce PDF/EPUB/URL »,
« ajoute cette source », « clippe cette page ». Une source peut aussi être **déposée dans `_inbox/`**
(dropzone synchronisée surveillée — voie privilégiée du cron Hermes) ; les fichiers y transitent avant
d'être déplacés vers `raw/<type>/`.

## Usage

```sh
python scripts/wiki_extract.py [SOURCE] [--lang fra+eng] [--dry-run]
```

> `--config <wiki.config.json>` est **optionnel** : sans lui, le config est auto-découvert
> (`$MIMIR_CONFIG` → `dossier du profil` → `~/.config/mimir/wiki.config.json` → `./wiki.config.json`).

- **SOURCE absent** → scan de `_inbox/` (mode cron/curator) : tous les fichiers supportés sont traités.
- **SOURCE = chemin `.pdf`/`.epub`/`.md`** → extraction de cette source.
- **SOURCE = URL `http(s)`** → clipping web.

## Procédure

Contrat d'extracteur commun, un module par format dans [`scripts/extractors/`](scripts/extractors/) :

```
extract(source) → ExtractResult{ raw_content, content_ext, metadata, structure, assets[], doc_type }
```

Chaque extracteur **tente la lib riche puis dégrade** vers un fallback CLI/stdlib (la skill se charge
même si une lib manque). **Jamais d'invention de contenu** (§12.5) : si l'extraction est impossible,
`ExtractorError`/`ExtractorUnavailable` est levée et **rien** n'est écrit dans `raw/`.

- **PDF** : couche texte (`fitz` → `pdftotext`) ; si quasi vide → **OCR auto** (`ocrmypdf` →
  `pdftoppm`+`tesseract`) → `ocr:true`, sinon `ExtractorUnavailable`. Chapitrage `get_toc()` →
  `.toc.json` ; images (`fitz`/`pdfimages`) → `_assets/`. Sortie : `<base>.pdf.txt` + `<base>.toc.json`.
- **EPUB** : spine = chapitrage (`ebooklib` → `zipfile` stdlib) → `<base>.epub.txt` + `.toc.json` ;
  images du manifest → `_assets/`.
- **Web/URL** : nettoyage (`trafilatura` → `bs4`+`markdownify` → stdlib) → markdown **inline**
  `<slug>-<AAAAMMJJ>.md` (frontmatter YAML) ; images localisées → `_assets/`, liens réécrits en relatif.
- **Notes / `.md` déposé** (palier `route-md-inbox-notes`) : pass-through. Le `.md` est sa propre
  matière source — il n'a pas de `.html` parent re-dérivable. L'orchestrateur écrit
  `<base>.src.md` (original verbatim, immuable) + `<base>.md` (dérivation à frontmatter
  normalisé `type: note`, `title`, `source?`, `created`, `tags: [clippings]`). Pas de sidecar
  `.html`, pas de dédup URL. Le slug est dérivé du nom de fichier, pas du frontmatter.

### Flux `_inbox/` (orchestrateur)

1. **Verrou** `wiki-sync` (mkdir hors `work_root`, §12.9 ; stub P1, vrai backend P4).
2. Routage **par extension** (URL → web, `.md` → notes).
3. `extract()` → calcul du **SHA du contenu**.
4. **Dédup** : SHA déjà présent dans `_status.md` → fichier re-déposé **supprimé**, rien réécrit.
   Dédup URL web avant la dédup SHA ; pas de dédup URL pour les notes.
5. Écritures (ordre : `_assets/` → contenu → source immuable → `.toc.json` → `_status.md`), toutes
   **confinées**.
6. **Move** du fichier `_inbox/` → `raw/<type>/` **après** écriture réussie (reprise sûre sur
   crash). Notes : move du `.md` (la matière immuable est copiée dans `<base>.src.md`).

> **Immutabilité** : raw append-only, suffixage cohérent `-2` (binaire + `.txt` + `.toc.json`
> pour les PDF/EPUB ; `.src.md` + `.md` pour les notes) ; seule mutation tolérée = `_status.md`
> (SHA du **contenu**, pas du fichier source).

> **Import du socle partagé** : résolu par chemin relatif (`../../_shared-references/scripts`) avec
> repli sur la variable `MIMIR_SHARED`. Durcissement packaging Hermes prévu en Phase 5.

## Références

- Formats raw + immutabilité + sidecars : spec `extract-source::extraction-immutable` + spec
  `extract-source::routage-des-notes-markdown` + spec `mimir-invariants::append-only-raw` ;
  gabarits frontmatter : [`../_shared-references/FRONTMATTERS.md`](../_shared-references/FRONTMATTERS.md).
- Slug ASCII, NFD/NFC : [`../_shared-references/scripts/slug.py`](../_shared-references/scripts/slug.py).
- Outillage (PyMuPDF, ocrmypdf, tesseract) : [`../_shared-references/TOOLING.md`](../_shared-references/TOOLING.md).
- Spécification : `specs/extract-source` (dropzone-routing, extractor-pluggable-contract,
  extraction-immutable, routage-des-notes-markdown) + `specs/compile-source::karpathy-article-format`
  + `specs/mimir-invariants::append-only-raw`.
