---
type: Concept
title: Mimir Pipeline — Knowledge Skills
description: The back of the Mimir pipeline — wiki-reading-grid chapter ordering, wiki-index regeneration and link audit with assisted repair, wiki-sync pluggable vault synchronization, and wiki-research topic dossiers.
tags: [mimir, pipeline, reading-grid, index, sync, research, obsidian]
resource: /skills
---

# Pipeline: Knowledge Skills

The four skills that turn compiled articles into a navigable, synchronized knowledge base. Mechanics come from the [shared core](../architecture/shared-core.md); invariants from [Domain: Conventions](../domain/conventions-and-method.md).

## wiki-reading-grid — restore reading order per book

`skills/wiki-reading-grid/SKILL.md` · `scripts/wiki_reading_grid.py` · `scripts/grid_builder.py` (500 lines)

Articles are per-notion; a book's reading order is lost after `wiki-ingest`. The grid rebuilds it as a link-only page.

- `generate --source raw/<type>/<name>.<ext> [--skip-sync] [--dry-run]` · `generate-all` (iterates every `*.toc.json` in `raw/pdfs` + `raw/epubs`; web/notes ignored). JSON output includes `grid_path`, `linked_articles`, `unresolved_chapters`, `orphan_articles`, `chapters_missing_blurb`.
- **Skeleton is 100% deterministic**: cross-join of `toc.json` chapter `order` × [ledger](../architecture/shared-core.md) article order × each article's `#chK` anchors. The toc order is canonical — the grid **never re-sorts** (sorting `R1…G3` book codes alphabetically previously broke canonical order; fixed in v0.5.0 with the `_chapter_key` normalizer, `"03"→"3"`, `"g3"→"G3"`).
- **One semantic step for the agent**: write one-line editorial blurbs into `<!-- blurb:start --> … <!-- blurb:end -->` zones for chapters listed in `chapters_missing_blurb`. Missing blurbs render as `_à rédiger_` (placeholder, not an error); regeneration extracts existing blurbs and re-injects them by toc `order`, so renames don't lose prose. This hybrid skeleton+blurb format is what the v3→v4 migration palier produces ([shared-core](../architecture/shared-core.md)).
- Writes `reading-grids/<title-slug>.md` (`type: reading-grid`) with Prev/Next `[[#Ch. …]]` links resolved by exact title text; updates the "Grid" column of `raw/<type>/_status.md`.

## wiki-index — indexes, audit, assisted link repair

`skills/wiki-index/SKILL.md` · `scripts/{wiki_index.py, index_builder.py, link_audit.py, fix_broken_wikilinks.py}`

- `regenerate [--skip-sync] [--dry-run]` — rebuilds `wiki/INDEX.md` (subjects list; editorial `— description` lines are **preserved** across regenerations, fallback = first 3 notions) and `wiki/<subject>/_INDEX.md` (notions with their `>` one-line summaries, plus attached grids and research reports). Emits wikilinks in the vault's **majority form** (`wikilinks.majority_form`).
- `audit [--json]` — read-only, exit 0 iff clean (usable in CI). Four passes per the docstring: broken links, phantom files, index→empty entries, and stray CJK characters (a known sub-agent artifact, e.g. `充当`). Scope: `wiki/` + `reading-grids/`; links into `raw/` are out of scope. ⚠️ `SKILL.md` still documents only 3 passes — the docstring is current.
- `fix [--apply] [--scope S]… [--semantic-map map.json] [--json]` — **v0.6.0** assisted repair (`fix_broken_wikilinks.py`): match passes 0 explicit agent override 🧠 → 1 identity → 2 common-character prefix ≥3 → 3 stop-word word matching (scored deliberately below prefix matches). Rewrites preserve `#anchor` and `|alias`; writes go through atomic+guard; `--apply` needed to touch files; best-effort re-audit afterwards. The `--semantic-map` JSON (`{broken: proposed}`) is the agent's semantic input.
- v0.6.0: `type: research-report` grids are indexed under a `## Research reports` section, so [wiki-research](#wiki-research--topic-dossier-agent-driven) output flows into indexes and the audit for free. v0.5.0: audit suggests ASCII-slugified forms for broken accented links without legitimizing non-ASCII slugs.
- The audit is also consumed by the migration engine's link validation and by `wiki-batch-produce` (best-effort) — treat its exit-code contract as load-bearing.

## wiki-sync — pluggable vault synchronization

`skills/wiki-sync/SKILL.md` · `scripts/wiki_sync.py` (thin CLI) · engine in [`sync/` package](../architecture/shared-core.md)

- `wiki_sync.py [--config CFG] {lock|pull|push|validate|sync} [--dry-run]` (`sync` = pull→push→validate; `--dry-run` = pull+validate, no push).
- Failure semantics wired into every skill: **pull failure ⇒ stop** (don't compile on stale data); push failure ⇒ reported, local work preserved; validate discrepancy ⇒ remediation. A remote listing can be **stale** — post-sync validation is mandatory.
- Opt-in `sync.presync_gate=true`: runs `wiki-index audit` before push; anomalies abort the push.
- Backends (`sync.backend`): `rclone` (hardened kDrive WebDAV bisync, self-heal `--resync`, `remote_setup` auto-repair of 401s — `auto_resync` key obsolete), `git` (pull --rebase, scoped add/commit/push), `obsidian` (triggers Remotely Save via Local REST API; validate still needs an rclone remote — `references/OBSIDIAN_REMOTELY_SAVE.md`), `auto` (probe/launch Obsidian, fall back to rclone within the same run, never two synchronizers in parallel), or none ⇒ `noop`.
- Lock: mutex **outside** the synced zone (`~/.cache/mimir/wiki-sync.lock`); held lock = clean skip exit 0.
- Backend references: `skills/wiki-sync/references/GIT_BACKEND.md`, `RCLONE_KDRIVE.md`, `OBSIDIAN_REMOTELY_SAVE.md`.

## wiki-research — topic dossier (agent-driven)

`skills/wiki-research/SKILL.md` · `scripts/wiki_research.py` (575 lines) · `references/research-report.md` · new in **v0.6.0**

For "research X / build a dossier on X" — distinct from `wiki-extract` (single URL) and `wiki-reading-grid` (chaptered book). The most semantic-heavy skill: the **agent** selects 5–15 web sources and writes the synthesis; the script only clips, scaffolds, and recalculates references.

- `clip <topic> <url…> [--lang] [--dry-run] [--skip-sync]` — delegates each URL to `wiki_extract.run` (sibling import), so clips land in `raw/web/` with `subject:`/`tags:` frontmatter; URL dedup scans existing clip frontmatter `source:`. Failed clips are reported `❌`, never replaced by placeholders.
- `scaffold <topic> [--title] [--tag …]` — writes `reading-grids/<topic>.md` with `type: research-report` (sections Angle / Synthesis / Points of friction / Mermaid / Sources); never overwrites an existing report.
- `update <topic>` — aligns `sources:` with actual clips, bumps `updated`, preserves `created` and the agent's body; flags unresolvable `[[…]]` as `🔗 invalide`.
- `status <topic>` — read-only diagnostic.
- ⚠️ The changelog claims recursive dedup scanning of `raw/web/`; the implementation's `_find_existing_clip` globs top-level `raw/web/*.md` only — verify before relying on dedup across subject subdirs.

## Changing this area

- Grid format changes: update `grid_builder.py` **and** register a new palier (the v3→v4 palier reinjects old blurbs via `build_grid(existing_grid_path=…)` — the pattern to copy). Tests: `skills/wiki-reading-grid/tests/` incl. `test_web_grid.py`, `test_chapter_codes.py`.
- Audit passes: keep `link_audit.py` docstring, `wiki-index/SKILL.md`, and CI exit-code expectations in sync (the 3-vs-4-pass doc drift above is a live example).
- Sync: every backend change must stay testable with an injected fake `runner`/`http_client` — see `skills/_shared-references/tests/test_sync_*.py`.
