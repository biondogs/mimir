# Conventions partagées — Mimir (digest pointeur)

> **Ce fichier est un digest pointeur** (≤ 50 lignes), **pas** la source
> de vérité normative. Toute règle prescriptive vit dans
> `openspec/specs/<cap>/spec.md`.
>
> On lit ce fichier pour savoir *où chercher*. On lit les specs pour
> savoir *ce qui est garanti*.
>
> Migration opérée lors du change `setup-openspec-as-design-tool`
> (cf. `openspec/changes/.../design.md` § 3).

---

## 1. Racine de travail & confinement

> Norme : `specs/mimir-invariants/spec.md` § `confinement` — toute
> opération (lecture, écriture, synchro) bornée à `work_root`.
> Implémenté par `skills/_shared-references/scripts/guard.py`
> (`assert_within`, `safe_path`, normalisation `realpath` + `commonpath`).
> Test : `test_guard.py` (4 cas) + 4 tests croisés dans les skills.

## 2. Slugs ASCII kebab-case

> Norme : `specs/mimir-invariants/spec.md` § `nfd-nfc-normalization` —
> noms de fichiers ASCII kebab-case, titre affichable dans le frontmatter.
> Helper : `skills/_shared-references/scripts/slug.py`.

## 3. NFD / NFC

> Norme : `specs/mimir-invariants/spec.md` § `nfd-nfc-normalization` —
> comparer via `slug.same_file(a, b)`, jamais en `str.__eq__`.
> Test : `test_article_writer::test_nfd_nfc_slug_ascii`,
> `test_wikilinks.py` (résolution).

## 4. Mermaid obligatoire

> Norme : `specs/mimir-invariants/spec.md` § `mermaid-mandatory` —
> tout schéma en Mermaid, jamais en ASCII art. Wikilink dans un nœud :
> `A["[[navigation/relevement]]"]` (verbatim).
> Tests : `test_article_writer::test_mermaid_wikilink_verbatim`
> (écriture) + 1 test squelette à venir (présence).

## 5. Immutabilité de `raw/`

> Norme : `specs/mimir-invariants/spec.md` § `append-only-raw` —
> une source n'est jamais écrasée, re-dépôt ⇒ suffixe `-2` (partagé
> entre binaire et sidecars). Seule mutation tolérée : colonne statut
> de `_status.md`. Web : staging réversible avant commit.
> Tests : `test_orchestrator_extract::test_suffixage_*`,
> `test_iohelpers::test_unique_suffixed_*`.

## 6. Frontmatters & formats

> Norme : gabarits dans `skills/_shared-references/FRONTMATTERS.md`
> (article notion / grille / `.toc.json` / `_status.md` / clipping web).
> Format d'article = **Karpathy** (H1 / résumé / Définition / Points
> clés / Relations / Sources). Pas de dump brut.
> Specs : `compile-source::karpathy-article-format`,
> `mimir-invariants::no-dump-in-wiki`.

## 7. Synchro

> Norme : `specs/sync-vault/spec.md` — backends `rclone | git | noop`,
> verrou **hors** `work_root` (default `~/.cache/mimir/…`), validation
> post-sync obligatoire, scope `raw/ + wiki/ + reading-grids/`.
> Filtres : `filters.txt`. Cycle : `lock → pull → push → validate → unlock`.
> Tests : `test_sync_lock.py` (5 cas), `test_sync_factory.py`,
> `test_sync_rclone.py::test_validate_repousse_si_ecart`,
> `test_sync_git.py::test_push_exclut_le_ledger_hors_scope`.

## 8. Pièges transverses & Open Issues

> Les anciens « 14 pièges » (ancienne section `§12` d'une spec
> d'architecture **supprimée** lors du passage à OpenSpec) sont
> maintenant **dispersés** entre les 7 specs (notamment
> `mimir-invariants` + Open Issues par capability). Si un piège connu
> n'est couvert par *aucun* spec, c'est un **Open Issue** à formaliser
> dans la capability correspondante — la liste n'est plus centralisée.
