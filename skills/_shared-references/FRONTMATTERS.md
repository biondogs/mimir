# Gabarits de frontmatter & formats — Mimir

> Référence des en-têtes YAML et sidecars. Source normative :
> `specs/compile-source::karpathy-article-format`,
> `specs/reading-grid` (gabarit de grille),
> `specs/audit-index` (`_status.md` & INDEX par sujet).

---

## 1. Article notion — `wiki/<sujet>/<notion>.md` `[SPEC §5.2]`

```yaml
---
title: Relèvement
subject: navigation
tags: [navigation, technique]
sources: ["raw/pdfs/navigation-cotiere.pdf.txt#ch3", "raw/web/amers-2024.md"]
created: 2026-06-04
updated: 2026-06-04
---
```

Corps **Karpathy** (jamais de dump brut) :

```markdown
# Relèvement

> Mesure de l'angle entre le nord et la direction d'un amer, pour se positionner.

## Définition
[Reformulation complète, en phrases.]

## Points clés
[Tableaux, listes, schéma Mermaid si pertinent.]

## Relations
- [[navigation/route-fond]] : un relèvement alimente le calcul de route.

## Sources
- [[raw/pdfs/navigation-cotiere.pdf.txt]] — chapitre 3, p. 41-48.
```

---

## 2. Grille de lecture — `reading-grids/<ouvrage-slug>.md` `[SPEC §8]`

```yaml
---
type: reading-grid
work: Navigation côtière
source: raw/pdfs/navigation-cotiere.pdf.txt
toc: raw/pdfs/navigation-cotiere.toc.json
chapters: 12
created: 2026-06-04
---
```

La grille **ordonne des liens** vers les articles (Précédent/Suivant au niveau chapitre) et **ne duplique
aucun contenu**. Un lien non résolu = travail restant (visible à l'audit), pas un bug.

---

## 3. Sidecar chapitrage — `raw/<type>/<nom>.toc.json` `[SPEC §4.3, §6]`

```json
{
  "title": "Navigation côtière",
  "source": "raw/pdfs/navigation-cotiere.pdf",
  "pages": 240,
  "ocr": false,
  "chapters": [
    { "order": 1, "title": "Instruments", "page_start": 1, "page_end": 22 },
    { "order": 2, "title": "Se positionner", "page_start": 23, "page_end": 48 }
  ]
}
```

`metadata` + `structure` (chapitrage ordonné) sont sérialisés **ensemble** ici ; le contenu de travail
va dans `<nom>.pdf.txt` / `<nom>.epub.txt`. `ocr: true` trace un basculement OCR (PDF scanné).

---

## 4. Table de statut — `raw/<type>/_status.md` `[SPEC §4.3]`

```markdown
# Statut — pdfs

| Fichier                    | SHA256  | Statut  | Articles wiki                | Grille                               | MAJ        |
| -------------------------- | ------- | ------- | ---------------------------- | ------------------------------------ | ---------- |
| navigation-cotiere.pdf.txt | a1b2c3… | compilé | [[navigation/route-fond]]    | [[reading-grids/navigation-cotiere]] | 2026-06-04 |
```

- SHA = celui du **fichier de contenu** (`.pdf.txt`/`.epub.txt`/`.md`), pas du binaire.
- Statuts : `déposé` → `extrait` → `partiellement compilé` → `compilé`.

---

## 5. Clipping web — `raw/web/<slug>-<AAAAMMJJ>.md` `[SPEC §4.4]`

```yaml
---
title: Titre de la page
source: https://exemple.org/article
type: web
created: 2026-06-04
tags: [clippings]
---
```

Contenu nettoyé **inline** (anti link-rot) ; images localisées dans `raw/web/_assets/`, liens relatifs.

---

## 6. Frontmatter d'un `SKILL.md` (agentskills.io) `[SPEC §11]`

```yaml
---
name: wiki-ingest                 # == nom du dossier ; [a-z0-9-], pas de "--"
description: >
  <≤ 1024 caractères, inclut le trigger d'activation>
license: PolyForm-Noncommercial-1.0.0
metadata:
  version: "0.1.0"
  author: Vivian MAES
  tags: [knowledge-base, obsidian, karpathy]
  hermes:
    profile: mimir         # indice OPTIONNEL — la skill ne dépend pas d'un profil
---
```

Obligatoires : `name`, `description`. ⚠️ `version`/`platforms`/`title`/`category` vont **sous `metadata`**.
Valider avec [`scripts/validate_skills.py`](scripts/validate_skills.py) (ou `skills-ref validate`).
