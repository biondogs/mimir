---
type: Reference
title: Mimir Quickstart
description: Entry point for the Mimir repository — an 8-skill Hermes agent suite that maintains an Obsidian second-brain wiki. Repo map, pipeline summary, page index, and backlog.
tags: [mimir, quickstart, hermes, obsidian, wiki-pipeline]
resource: /README.md
---

# Mimir — Quickstart

Mimir is a **self-maintaining second brain**: a suite of **8 `wiki-*` skills** ([agentskills.io](https://agentskills.io) format) orchestrated by the **Hermes agent** ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)). A source goes in (PDF, EPUB, URL, Markdown note); a linked Obsidian wiki comes out. Version 0.6.1, Python ≥ 3.11, license **PolyForm Noncommercial 1.0.0** (source-available, *not* OSI open source — commercial use needs written agreement; see `LICENSE`, `README.md` §License).

## The golden rule

> **Separate what you gather from what you understand of it.** (`SOUL.md`)

The vault has two strict layers:

- `raw/` — **raw material**, kept intact and immutable. Never rewritten; only its status/SHA evolves.
- `wiki/` — **constructed knowledge**: one notion per article, rephrased with the Karpathy method (summary → definition → key points → relations → sources), woven with `[[wikilinks]]`. See [Domain: Conventions & the Karpathy method](domain/conventions-and-method.md).

The complementary discipline throughout the codebase: **deterministic scripts do the mechanics, the LLM agent does the semantics** (notion decomposition, editorial blurbs, source selection, report writing). Each skill documents exactly where that line sits — see [Architecture: Overview](architecture/overview.md).

## The pipeline at a glance

```
_inbox/ drop → wiki-extract → raw/<type>/ → wiki-ingest (or wiki-batch-produce) → wiki/<subject>/
                                                        ↓
                     wiki-reading-grid → reading-grids/ · wiki-index → INDEX + link audit · wiki-sync → Obsidian vault
```

| Skill | Role | Nature |
|---|---|---|
| `wiki-init` | bootstrap empty vault ("where is my wiki?") | deterministic |
| `wiki-extract` | `_inbox/` source → immutable `raw/` triplet | deterministic |
| `wiki-ingest` | `raw/` → notion articles (Karpathy) | agent writes, scripts place |
| `wiki-batch-produce` | `raw/web/` → articles in one batch, no LLM | deterministic |
| `wiki-reading-grid` | restore chapter reading order per book | skeleton deterministic + agent blurbs |
| `wiki-index` | rebuild INDEXes, audit links, assisted fix | deterministic (+ agent `--semantic-map`) |
| `wiki-sync` | pluggable vault sync (rclone / git / obsidian / auto) | deterministic |
| `wiki-research` | topic dossier: agent selects sources, clips, writes report | agent selects & writes, script clips |

Full per-skill detail: [Pipeline: Ingestion Skills](pipeline/ingestion-skills.md) and [Pipeline: Knowledge Skills](pipeline/knowledge-skills.md).

## Repository map

```
README.md / SOUL.md          product vision · agent persona + vault convention memory
config.yaml                  Hermes profile config (deliberately minimal)
distribution.yaml            Hermes profile distribution manifest (distribution_owned files)
wiki.config.example.json     template for the user's wiki.config.json (work_root + sync)
filters.txt                  rclone sync exclusion template
skills.sh.json               skill catalog listing the 8 wiki-* skills
skills/_shared-references/   shared Python "socle": config, guard, sync, migration, tests
skills/wiki-*/               one directory per skill: SKILL.md + scripts/ + tests/ + references/
assets/                      README images
```

There is no application entrypoint: each `skills/wiki-*/scripts/*.py` is an independently invoked CLI, run by the Hermes agent per its `SKILL.md`. Everything they share lives in the [shared core library](architecture/shared-core.md).

## Where to go next

- [Architecture: Overview](architecture/overview.md) — vault layout, config discovery, skill coupling, deterministic/semantic split.
- [Architecture: Shared Core Library](architecture/shared-core.md) — `skills/_shared-references/scripts/`: bootstrap, config_loader, guard, slug, frontmatter, ledger, sync backends, migration engine.
- [Pipeline: Ingestion Skills](pipeline/ingestion-skills.md) — init, extract, ingest, batch-produce.
- [Pipeline: Knowledge Skills](pipeline/knowledge-skills.md) — reading-grid, index, sync, research.
- [Domain: Conventions & Karpathy Method](domain/conventions-and-method.md) — the rules every page must obey: slugs, NFD/NFC, raw immutability, frontmatter shapes.
- [Operations: Runbook & Testing](operations/runbook.md) — install, config, sync backends, migrations, troubleshooting, test suite.

## Project history in one paragraph

v0.2.0 shipped the 5-skill core (extract, ingest, reading-grid, index, sync) plus the shared socle; v0.3.x made `wiki.config.json` auto-discovered and moved profile files to the repo root; v0.4.0 added rclone `remote_setup` auto-repair; v0.5.0 added `wiki-init` and alphanumeric chapter codes; v0.5.1 switched to PolyForm Noncommercial; v0.6.0 added `wiki-batch-produce` and `wiki-research` plus the migration "paliers" engine, auto/obsidian sync backends and `fix_broken_wikilinks`; v0.6.1 (HEAD) added the Markdown/notes extractor. `CHANGELOG.md` is the authoritative narrative. Note: the working tree currently carries a large uncommitted **French → English translation** of docs and SKILL.md files (code comments are still in French).

## Backlog

- **`openspec/` specs** — `CONVENTIONS.md` and `TOOLING.md` cite `openspec/specs/<cap>/spec.md` as the normative rule source, but no `openspec/` directory exists in this snapshot; rules are documented here from code evidence instead. Source anchor: `skills/_shared-references/CONVENTIONS.md`.
- **Hermes profile distribution mechanics** — `hermes profile install/update`, `distribution_owned` semantics documented only briefly in the runbook; full behavior lives in the external Hermes repo. Source anchor: `distribution.yaml`.
- **`www/` site** — CHANGELOG references `www/index.html` (EN/FR landing pages) that is not in this snapshot.
