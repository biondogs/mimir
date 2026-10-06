---
type: Concept
title: Mimir Domain — Conventions and the Karpathy Method
description: The domain rules of the Mimir second brain — two-layer vault (immutable raw vs compiled wiki), Karpathy article method, ASCII slugs, NFD/NFC normalization, Mermaid mandate, frontmatter shapes, ledger and status conventions, license posture.
tags: [mimir, conventions, karpathy-method, frontmatter, invariants, license]
resource: /skills/_shared-references/CONVENTIONS.md
---

# Domain: Conventions & the Karpathy Method

These rules define what a "correct" Mimir vault is. They are enforced in code by the [shared core](../architecture/shared-core.md) and obeyed by every skill in the [ingestion](../pipeline/ingestion-skills.md) and [knowledge](../pipeline/knowledge-skills.md) pipelines. `SOUL.md` (the agent persona) keeps a short "convention memory" of them; `skills/_shared-references/CONVENTIONS.md` is a pointer digest whose normative targets (`openspec/specs/`) are absent from this snapshot — the code below is the effective source of truth.

## The two-layer epistemics

**"Separate what you gather from what you understand of it."** The whole architecture derives from this:

- **`raw/` is sacred** — faithful, immutable, never rewritten or invented. Extraction failure writes nothing. The only permitted mutation is the `_status.md` tracking table. Dedup collisions on the raw side get `-2`/`-3` suffixes rather than overwrites.
- **`wiki/` is understanding** — rephrased, per-notion, interlinked. Raw dumps must never enter it (enforced conceptually by the Karpathy shape, mechanically by `lint_docs.has_dumps`: >5000 chars of prose without a heading fails).
- Machine-local bookkeeping splits the same way: `.wiki/ingest-ledger.json` (per-machine, synced **out**) vs `.mimir/vault.json` (vault structure version, synced **with** the vault).

## The Karpathy article method

One notion = one article (`wiki-ingest`), or one URL = one notion in batch mode (`wiki-batch-produce`). Canonical shape (`FRONTMATTERS.md`, `references/karpathy-template.md`):

```
# Title
> one-line summary          ← extracted into _INDEX.md by article_index.py
## Definition
## Key points
## Relations                ← [[wikilinks]] to other notions
## Sources                  ← raw/ references with #chK chapter anchors
```

Plus a **mandatory Mermaid diagram**; ASCII art is banned (`lint_docs.has_ascii_art_schemes`). Wikilinks inside diagram nodes use the verbatim form `A["[[subject/notion]]"]`. Chapter anchors `#chK` must match the `.toc.json` `order` field — numeric (`#ch3`) or book code (`#chG3`) since v0.5.0.

## Filename and link rules

- **ASCII kebab-case slugs** for all filenames (`slug.py`: NFKD accent stripping); the displayable title lives in the frontmatter. Accented broken links get slugified suggestions from `wiki-index` audit, never legitimization.
- **NFD/NFC**: macOS/APFS stores filenames NFD while Obsidian wikilinks are NFC. Compare filenames only via `slug.same_file()` (NFC-normalized), never `str.__eq__`; unverified renames silently create dead links.
- **Wikilink resolution order** (`wikilinks.py`): prefixed → `WIKI/`-relative → file-relative, each checked for [confinement](../architecture/shared-core.md#safety--file-mechanics) and NFD/NFC-tolerantly. Index regeneration emits the vault's majority link form for consistency.

## Frontmatter shapes (from `FRONTMATTERS.md`)

- **Article**: `title`, `subject`, `tags[]`, `sources[]` (with `#chK` anchors), `created`, `updated`; on dedup, `created` is kept and `sources`/`tags` are unioned (`article_writer.py`).
- **Reading grid**: `type: reading-grid`, `source:` pointing at the `raw/` file.
- **Research report**: `type: research-report`, `subject:`/`tags:` — a reading-grids subtype recognized by `wiki-index` since v0.6.0.
- **Note clip** (v0.6.1): `type: note`, `tags: [clippings]`; flow-lists (`[a, b]`) and block lists both parse (`frontmatter.py` fixed a historical flow-list bug).
- `schema_version:` in frontmatter drives lazy migration ([shared-core](../architecture/shared-core.md)); absence means un-migrated, never an error.

## Tracking conventions

- **Ledger** (`ledger.py`): keys are the SHA256 of the raw *content* file (never the binary); entries canonicalized as `subject/notion`. Corrupt ledger ⇒ treated as empty `{}` — safe because `_status.md` + file scans allow rebuild.
- **`_status.md`** (`status_table.py`): columns Fichier | SHA256 | Statut | Articles wiki | Grille | MAJ; status lifecycle `déposé → extrait → partiellement compilé → compilé` (French strings in data; docs partially translated).
- **Sync confinement**: read/write/sync only under `work_root`; the sync lock lives outside the synced zone; post-sync validation is mandatory because remote listings can be stale.

## Language and licensing posture

- The runtime vocabulary (status strings, welcome files like `_inbox/LISEZ-MOI.md`, blurbs placeholder `_à rédiger_`, code comments) is **French**; v0.6.1 working tree adds an English translation of README/SOUL/SKILL.md prose (`git status`). Expect mixed-language artifacts in generated vaults.
- **PolyForm Noncommercial 1.0.0** (since v0.5.1): source-available, non-commercial use only (personal, research, education, associations), commercial use requires written agreement. Not OSI open source — relevant when reusing code or docs. `LICENSE`, `README.md` §License, `distribution.yaml`.

## Changing this area

New conventions here ripple everywhere: update `CONVENTIONS.md` digest, `FRONTMATTERS.md`, the enforcing module in the shared core, `SOUL.md` convention memory, and per-skill SKILL.md text — past releases (v0.5.0 chapter codes, v0.6.1 notes) touched exactly this set.
