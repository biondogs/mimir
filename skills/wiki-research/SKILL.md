---
name: wiki-research
description: >
  Recherche en profondeur sur un sujet texte : l'agent rassemble des sources
  web, les fait clipper automatiquement (réutilise wiki-extract), puis rédige
  un rapport de synthèse dans reading-grids/<sujet>.md. Déclencher sur
  « fais une recherche sur X », « monte un dossier sur X », « synthèse sur X »,
  « écris-moi un rapport sur X ». Strictement séparé de wiki-extract (URL
  unique, déterministe) et de wiki-reading-grid (PDF chapitré, déterministe) :
  ici le travail sémantique (sélection des sources + rédaction) est piloté par
  l'agent, le skill ne fait que clipper et scaffolder.
license: PolyForm-Noncommercial-1.0.0
metadata:
  version: "0.6.1"
  author: Vivian MAES
  tags: [knowledge-base, obsidian, research, report, web]
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
      - key: mimir.research.lang
        description: Codes langues acceptés par les extracteurs (web = fallback si pas d'extraction)
        default: "fra+eng"
        prompt: Langues pour l'extraction
---

# wiki-research

> Recherche en profondeur sur un sujet texte : **sélection de sources** (par
> l'agent) → **clip web automatisé** (via `wiki-extract`) → **rédaction d'un
> rapport de synthèse** dans `reading-grids/<sujet>.md`.

## Quand l'utiliser

Pour produire un **dossier de recherche** (état de l'art, vue d'ensemble, panorama)
sur un sujet texte — par opposition à :

- une **URL unique** → `wiki-extract <URL>` (clip sec, déterministe, sans synthèse) ;
- un **PDF chapitré** → `wiki-extract` puis `wiki-reading-grid` (grille ordonnée
  d'articles, déterministe).

❌ **Pas pour** : un PDF, un EPUB, un fichier local (→ `wiki-extract`).
❌ **Pas pour** : un seul article web (→ `wiki-extract` + `wiki-ingest`).
❌ **Pas pour** : un travail 100 % déterministe (la sélection des sources et la
rédaction sont sémantiques — c'est l'agent qui les fait).

## Différences avec les autres skills

| Skill               | Entrée            | Travail sémantique | Sortie                       |
| ------------------- | ----------------- | ------------------ | ---------------------------- |
| `wiki-extract`      | URL / PDF / EPUB  | Aucun (clip sec)   | `raw/<type>/<slug>.md`       |
| `wiki-ingest`       | source extraite   | Agent (Karpathy)   | `wiki/<sujet>/<notion>.md`   |
| `wiki-reading-grid` | `.toc.json`       | Aucun              | `reading-grids/<slug>.md`    |
| **`wiki-research`** | **sujet texte**   | **Agent (sources + rapport)** | **`reading-grids/<sujet>.md` (rapport)** |

`wiki-research` réutilise le **clip web** de `wiki-extract` (pipeline durci v3 :
`.html` immuable + `.md` re-dérivable, dédup par URL + SHA, assets par slug). La
valeur ajoutée est **uniquement** : (1) la **sélection** de sources, (2) la
**rédaction** du rapport.

## Procédure (ce que fait l'agent)

Le skill expose deux sous-commandes :

```sh
# 1) Boucle de clip : rassemble les sources web sur le sujet.
python scripts/wiki_research.py --config wiki.config.json clip <sujet> <url1> <url2> …

# 2) Squelette du rapport (frontmatter + plan), sans corps rédigé.
python scripts/wiki_research.py --config wiki.config.json scaffold <sujet>

# 3) Mise à jour idempotente du rapport (renvois recalculés, `created` préservé).
python scripts/wiki_research.py --config wiki.config.json update <sujet>
```

L'agent **orchestre** les étapes dans cet ordre :

1. **Définir le sujet** : texte court (ex. *« juridiction de l'expert judiciaire
   en appel »*) + **slug** ASCII (kebab-case, slugifié par `slug.slugify`).
2. **Réunir des URLs** : 5 à 15 sources web de qualité (officiels > blogs > forums).
   Hiérarchie implicite : jurisprudence, doctrine, articles de synthèse.
3. **Clipper en lot** : `clip <sujet> <url…>` — chaque URL est passée à
   `wiki-extract` avec sortie forcée sous `raw/web/<sujet>/`. Dédup automatique
   (URL déjà clippée → `↩️`, pas de re-fetch). Clip en échec (`❌`) → signalé
   dans le rapport d'exécution mais **jamais inventé** (zéro `Placeholder`
   fourré-tout).
4. **Rédiger** le rapport en lisant **intégralement** chaque clip : pour chaque
   URL clippée, ouvrir `raw/web/<sujet>/<slug>-<date>.md` (et son `.html` si
   besoin), en extraire la substantifique moelle.
5. **Produire** `reading-grids/<sujet>.md` via `scaffold <sujet>` (frontmatter
   + plan) puis **éditer manuellement** le corps (sections `## Angle`,
   `## Synthèse`, `## Points de friction`, `## Mermaid`, `## Sources`).
6. **Recalculer les renvois** : `update <sujet>` rebascule le frontmatter
   (`sources:` synchronisé avec ce qui existe vraiment) et audite les wikilinks
   du rapport (les `[[raw/web/<sujet>/<slug>]]` doivent pointer vers un fichier
   réel ; les `[[<sujet>/<notion>]]` doivent pointer vers un article wiki
   existant — sinon signalés en marge pour correction manuelle).

## Frontmatter du rapport (gabarit)

```yaml
---
title: "<sujet>"
type: research-report
topic: "<sujet>"
subject: <sujet-slug>
created: 2026-06-14
updated: 2026-06-14
sources:
  - https://exemple.org/article-1
  - https://exemple.org/article-2
tags: [report, <sujet-slug>]
---
```

> `type: research-report` est reconnu par `wiki-index` comme entrée de
> `reading-grids/` (cf. §5 du change `add-wiki-research-skill`).

## Renvois (wikilinks)

Le rapport référence ses sources de deux manières complémentaires :

| Forme                                | Cible                                 | Vérification |
| ------------------------------------ | ------------------------------------- | ------------ |
| `[[raw/web/<sujet>/<slug>-<date>]]`  | Le clip brut (markdown dérivé)        | Doit exister |
| `[[<sujet>/<notion>]]`               | Un article wiki du sujet (s'il existe) | Doit exister |

L'agent **ne forge pas** un `[[…]]` vers un clip qui n'a pas été extrait :
`update <sujet>` lève un avertissement `🔗 invalide` pour chaque cible qui ne
résout pas vers un fichier réel (cf. `wikilinks.resolve`).

## Déterminisme et garde-fous

1. **Dédup URL** : la dédup est héritée de `wiki-extract._find_ingested_url`
   (match frontmatter `source:` + SHA contenu) — relancer `clip` sur les mêmes
   URLs est un no-op (`↩️`).
2. **Idempotence du rapport** : `update <sujet>` **préserve** `created` et
   **n'écrase** que `updated` + `sources` (alignés sur l'état réel de
   `raw/web/<sujet>/`). Le **corps** du rapport (Karpathy de l'agent) n'est
   **jamais** réécrit automatiquement — c'est une édition manuelle.
3. **Confinement** : toutes les écritures passent par `iohelpers.atomic_write_text`
   et `guard.assert_within` → `work_root` (cf. bootstrap).
4. **Pas d'invention** : un clip en échec reste listé dans la sortie console
   `❌ <url> — <raison>` ; il n'est jamais remplacé par un placeholder.
5. **Frontmatter flow-list** : `sources: [...]`, `tags: [...]` — garantit
   `parse_frontmatter`.

## Liens avec les autres skills

- **Hérite de** : `wiki-extract` (clip web, dédup), `_shared-references` (config,
  guard, slug, iohelpers, frontmatter, wikilinks).
- **Alimente** : `reading-grids/<sujet>.md` (lu par `wiki-index` comme une
  grille de lecture classique — `type: research-report` est un sous-type).
- **Différent de** : `wiki-reading-grid` (grille ordonnée d'un PDF chapitré,
  100 % déterministe) ; `wiki-batch-produce` (production en série d'articles
  depuis `raw/web/<sujet>/`, 100 % déterministe).

## Références

- Format de rapport + flow-list frontmatter : `FRONTMATTERS.md §1` (gabarit
  grille de lecture étendu).
- Clip web : spec `extract-source` §4.4 (pipeline v3, `.html` immuable).
- Indexation : spec `index` §9 (lecture des grilles de `reading-grids/`).
