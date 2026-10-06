---
type: Concept
title: Mimir Pipeline — Ingestion Skills
description: The front of the Mimir pipeline — wiki-init vault bootstrap, wiki-extract source-to-raw extraction (PDF/EPUB/web/markdown), wiki-ingest Karpathy compilation, and wiki-batch-produce deterministic batch articles from web clips.
tags: [mimir, pipeline, extract, ingest, batch-produce, init, karpathy]
resource: /skills
---

# Pipeline: Ingestion Skills

The four skills that move material from `_inbox/` to compiled articles. They share the [shared core](../architecture/shared-core.md) (config, guard, sync, ledger) and follow the two-layer rule from [Domain: Conventions](../domain/conventions-and-method.md).

## wiki-init — vault bootstrap

`skills/wiki-init/SKILL.md` · `scripts/wiki_init.py`

Invoked on first use ("initialize the wiki") or for orientation ("where is my wiki?"). Fully deterministic, idempotent, confined to `work_root`.

- `wiki_init.py [--config CFG] {apply|status} [--skip-sync]`
- **`apply`** (default): creates `_inbox/`, `raw/`, `wiki/`, `reading-grids/`; writes `_inbox/LISEZ-MOI.md` (welcome) and `wiki/INDEX.md` **only if absent** (never overwrites); then bootstraps sync pull→push under lock (already-held lock = clean skip, exit 0). Deliberately does *not* create `.wiki/` — the ledger appears on first `wiki-ingest`.
- **`status`**: read-only diagnostic printing the resolved `wiki.config.json` and every derived path with ✓/✗, plus vault schema (`SCHEMA=<vault>` vs `EXPECTED=4`) and **pending migration paliers** (lazily imported from `migration.registry`) — see [shared-core migration engine](../architecture/shared-core.md).
- On a virgin vault it stamps `.mimir/vault.json` at `EXPECTED_SCHEMA`; on a populated vault lacking the file it baselines to `1` (so paliers will run).

## wiki-extract — source → immutable raw

`skills/wiki-extract/SKILL.md` · `scripts/wiki_extract.py` (431 lines) · `scripts/extractors/{base,pdf,epub,web,markdown}.py`

`wiki_extract.py [--config CFG] [SOURCE] [--lang fra+eng] [--dry-run] [--skip-sync]`. No `SOURCE` = scan `_inbox/` (cron/curator mode). Exit 1 if any outcome is `error`.

**Extractor contract** (`extractors/base.py`): `extract(source) → ExtractResult{raw_content, content_ext, metadata, structure, assets[], doc_type}`. Each extractor tries a rich library then degrades to CLI/stdlib (PyMuPDF→`pdftotext`; `ocrmypdf`→`pdftoppm`+tesseract; `ebooklib`→`zipfile`; `trafilatura`→`bs4`+`markdownify`→stdlib; see `TOOLING.md` and `skills/wiki-extract/requirements.txt`). Golden rule: **never invent** — on failure raise `ExtractorError`/`ExtractorUnavailable` and write nothing.

**Orchestrator flow**: lock → route by extension (`doc_type_for`: pdfs/epubs/web/notes) → dedup (SHA content; URL dedup first for web) → write to `raw/<type>/` → **move** the `_inbox/` file only after success (crash-safe resumption). Outputs per doc type:

- PDF/EPUB: `<base>.pdf.txt` (content) + `<base>.toc.json` + `_assets/`; PDFs with no native TOC get a `_TOC_REBUILD_HINT` machine signal (human/agent rebuilds it).
- Web clip: `<slug>-<YYYYMMDD>.md` + immutable `.html` + assets (the "fidelity triplet").
- Notes (**v0.6.1 fix**): `.md` dropped in `_inbox/` was previously orphaned ("format not supported"). New `extractors/markdown.py` (pass-through) writes the triplet `<base>.src.md` (verbatim, immutable) + `<base>.md` (normalized frontmatter `type: note`, `tags: [clippings]`); slug comes from the *filename*, not frontmatter; malformed YAML frontmatter is tolerated, not fatal.

Collisions append `-2`/`-3` on the base slug (shared across a file family). `raw/<type>/_status.md` tracks SHA of the **content** file and lifecycle `déposé → extrait → partiellement compilé → compilé`.

**Gotcha:** a stray `README.md` in `_inbox/` now becomes a note — `_inbox/` hygiene is a user responsibility (acknowledged in `CHANGELOG.md` v0.6.1).

## wiki-ingest — raw → notion articles (Karpathy)

`skills/wiki-ingest/SKILL.md` · `scripts/wiki_ingest.py` · `scripts/inventory.py` · `scripts/article_writer.py`

This is the canonical deterministic/semantic divide ([overview](../architecture/overview.md)): **notion decomposition and Karpathy rephrasing are the agent's job** (article shape: `# title`, `>` one-liner, `## Definition / Key points / Relations / Sources`, mandatory Mermaid, `#chK` chapter anchors matching the `.toc.json` `order` — numeric `#ch3` or book codes `#chG3`). Scripts only perform exact, idempotent file ops:

- `inventory [SOURCE] [--skip-sync] [--dry-run]` — SHA of each raw content file diffed against the [ledger](../architecture/shared-core.md); `status [--sync]` gives machine-readable per-source digest state (read-only, no network by default).
- `write-article --subject S --notion N --body-file F [--title] [--tags a,b] [--source raw/...#ch3]… [--asset …]` — ASCII slug, NFD/NFC-validated writes, atomic + confined. **Dedup policy on the wiki side: replace body + merge frontmatter** (keep `created`, union `sources`/`tags`) — never a `-2` suffix like raw uses.
- `finalize --source … --sha … --articles s1/n1,s2/n2 [--status compilé|partiellement-compilé]` — updates `raw/<type>/_status.md` and the ledger; ledger keys canonicalized to `subject/notion` (v0.5.0 fix for `wiki/wiki/…` orphaning).
- Large waves: the agent may delegate to sub-agents but must verify files exist afterwards.

**Input rule:** reads only content files (`.txt`/`.md`), never binaries; `inventory.py` explicitly filters `*.src.md` — the documented v0.6.1 trap where a note would otherwise be compiled twice (immutable `.src.md` + derived `.md`).

## wiki-batch-produce — deterministic batch articles (web only)

`skills/wiki-batch-produce/SKILL.md` · `scripts/batch_produce.py` (681 lines) · `references/karpathy-template.md` · new in **v0.6.0**

Fast path for homogeneous web sources: 1 URL = 1 notion, **no LLM call** — Karpathy sections are built from source paragraphs/first sentences; Relations sections are left marked for later enrichment by `wiki-ingest` or a human.

- `batch_produce.py [--config CFG] --inbox-dir raw/web/<subject> --subject <subject> [--skip-sync] [--dry-run]` (no subcommands)
- Refuses PDF/EPUB and chaptered sources (`chapters:` frontmatter) — those must go through `wiki-ingest`.
- Writes `wiki/<subject>/<slug>.md` (`slugify(title)[:60]`, `-2`/`-3` collision suffixes allowed here, unlike `wiki-ingest`).
- Safeguards: post-write validation (≥4 `##` sections, closed ```` ```mermaid ```` block, ≥500 bytes) with `os.remove` **rollback**; idempotent skip on second run; serial execution.
- Finishes by calling `wiki-ingest finalize` per source (imports the sibling module) and a best-effort `wiki-index audit` — direct sibling coupling, see [overview](../architecture/overview.md).

## Changing this area

- New doc type: extend `doc_type_for`/`is_supported` in `wiki_extract.py`, add an extractor honoring the base contract, add `_CONTENT_EXT` in `wiki-ingest/scripts/inventory.py`, and (if it changes layout) register a migration palier. The v0.6.1 notes pipeline (`git show f426cae`) is the template commit.
- Tests: `skills/wiki-extract/tests/test_orchestrator.py` (752 lines), `tests/test_dedup_and_toc_signals.py`, `skills/wiki-ingest/tests/`, `skills/wiki-batch-produce/tests/test_batch_produce.py` — all hermetic with fake sync runner.
