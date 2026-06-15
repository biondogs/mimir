---
name: wiki-ingest
description: >
  Compile la base de connaissance (méthode Karpathy) : lit raw/ et produit des articles
  wiki par notion (un article = une notion, jamais un dump brut). Antidoublon, ledger SHA
  idempotent, mise à jour des statuts. Déclencher sur « compile la KB », « ingère les
  sources », « décompose ce PDF en articles ». Borné à work_root.
license: PolyForm-Noncommercial-1.0.0
metadata:
  version: "0.6.0"
  author: Vivian MAES
  tags: [knowledge-base, obsidian, karpathy, ingestion]
  hermes:
    profile: mimir
    category: knowledge-management
    related_skills: [wiki-extract, wiki-reading-grid, wiki-index, wiki-sync]
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

# wiki-ingest

## Quand l'utiliser

Pour transformer la **matière brute** (`raw/`) en **savoir construit par notion** (`wiki/`).
Trigger : « compile la KB », « ingère les sources », « décompose ce PDF en articles ».

## Principe

La décomposition en notions et la reformulation Karpathy sont **ton travail** (sémantique) :
tu lis le contenu, tu le **comprends** et tu le **reformules** notion par notion — **jamais**
de copier-coller brut (SPEC §0, §5). Les scripts ne font que les opérations **déterministes**
(inventaire SHA, écriture fiable, antidoublon, statuts, ledger) que tu enchaînes :

```sh
python scripts/wiki_ingest.py <inventory|write-article|finalize> …
```

> `--config <wiki.config.json>` est **optionnel** : sans lui, le config est auto-découvert
> (`$MIMIR_CONFIG` → `dossier du profil` → `~/.config/mimir/wiki.config.json` → `./wiki.config.json`). Les exemples
> ci-dessous montrent `--config CFG` à titre indicatif ; il peut être omis.

## Procédure

1. **Pré-sync** — implicite (stub en P2). Ajoute `--skip-sync` dans une chaîne pour ne pas
   synchroniser plusieurs fois.
2. **Inventaire** — `inventory [SOURCE]` renvoie un JSON : `worklist` (fichiers de contenu à
   compiler = absents du ledger ou SHA changé) + `existing_notions` (par sujet, pour l'antidoublon).
   ```sh
   python scripts/wiki_ingest.py --config CFG inventory --skip-sync
   ```
3. **Lecture** — pour chaque source de la worklist, lis le **`.txt`** (contenu, économe en tokens)
   et le **`.toc.json`** (chapitrage) ; web = le `.md`. Jamais le binaire.
4. **Décomposition par notion** — produis **N notions atomiques**. Pour chacune, détermine le
   **sujet** et rédige le corps au **format Karpathy** :

   ```markdown
   # <Titre de la notion>

   > Résumé d'une ligne (description fonctionnelle).

   ## Définition
   [Reformulation complète, en phrases.]

   ## Points clés
   [Tableaux, listes, schéma Mermaid si pertinent.]

   ## Relations
   - [[<sujet>/<autre-notion>]] : … en quoi elles se relient.

   ## Sources
   - [[raw/pdfs/<nom>.pdf.txt]] — chapitre N, p. X-Y.
   ```

   - **Ancre de chapitre** dans la source : `…#chK` où `K` = le `order` du chapitre dans le
     `.toc.json`, soit un **numéro** (`#ch3`) soit un **code d'ouvrage** (`#chR2`, `#chG3`).
     C'est cette ancre qui rattache la notion à son chapitre dans la grille de lecture.

   - **Mermaid** obligatoire pour les schémas (jamais d'ASCII art). Wikilink dans un nœud :
     `A["[[<sujet>/<notion>]]"]` — **jamais** `[[[…]]]`.
   - **Slugs ASCII** : le titre accentué reste dans le frontmatter ; le nom de fichier est
     slugifié automatiquement par le script.

5. **Écriture** — écris le corps dans un fichier temporaire, puis :
   ```sh
   python scripts/wiki_ingest.py --config CFG write-article \
       --subject navigation --notion "Relèvement" --body-file /tmp/relevement.md \
       --source "raw/pdfs/navigation-cotiere.pdf.txt#ch3" --tags navigation,technique \
       --asset /tmp/schema.png
   ```
   Le script gère le frontmatter, les slugs, l'antidoublon, `wiki/<sujet>/_assets/` et la
   validation NFD/NFC ; il renvoie `{wikilink, path, created}`.
   - **Antidoublon = remplacer le corps** : si la notion existe déjà, le script **remplace le
     corps** et **fusionne le frontmatter** (`created` conservé, `sources`/`tags` en union,
     `updated` = jour). Aucun fichier `-2` côté wiki.
   - **Double `_assets`** : les images d'un article vont dans `wiki/<sujet>/_assets/` (un par
     sujet) — distinct de `raw/<type>/_assets/`. Référence-les dans le corps en `_assets/<nom>`.

6. **Grosses vagues** — délègue la production via `delegate_task` (toolset `file`), puis
   **vérifie l'existence réelle** des `.md` produits avant de finaliser (les sous-agents
   réussissent parfois partiellement).

7. **Finalisation** — par source, enregistre les articles produits :
   ```sh
   python scripts/wiki_ingest.py --config CFG finalize \
       --source raw/pdfs/navigation-cotiere.pdf.txt --sha <sha256-du-.txt> \
       --articles navigation/relevement,navigation/triangulation --skip-sync
   ```
   MAJ `_status.md` (statut `compilé`, colonne « Articles wiki ») + ledger atomique. Une relance
   `inventory` ne ressortira plus cette source (idempotence). Utilise `--status "partiellement
   compilé"` s'il reste des notions à compiler.

8. **Post-sync** — implicite (stub en P2), sauf `--skip-sync`.

## Références

- Format Karpathy + Mermaid + no-dump : spec `compile-source::karpathy-article-format` + spec `mimir-invariants::mermaid-mandatory` + spec `mimir-invariants::no-dump-in-wiki` ; gabarits frontmatter : [`../_shared-references/FRONTMATTERS.md`](../_shared-references/FRONTMATTERS.md).
- Ledger, slug/NFD-NFC, écriture confinée : [`../_shared-references/scripts/`](../_shared-references/scripts/) (`ledger.py`, `slug.py`, `iohelpers.py`, `guard.py`).
- Spécification : `specs/compile-source::karpathy-article-format` + `specs/compile-source::inventory-diff-ledger` + `specs/compile-source::finalize-status-and-ledger` + `specs/mimir-invariants::idempotence-ledger` + `specs/mimir-invariants::no-dump-in-wiki`.
