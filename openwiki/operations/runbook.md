---
type: Playbook
title: Mimir Operations Runbook and Testing
description: How to install and operate a Mimir Hermes profile, configure wiki.config.json and sync backends, run vault migrations, troubleshoot sync/extraction, and run the hermetic pytest suite.
tags: [mimir, runbook, operations, testing, pytest, sync, migration, install]
resource: /wiki.config.example.json
---

# Operations: Runbook & Testing

## Install / upgrade on Hermes

Prerequisite: [Hermes Agent](https://github.com/NousResearch/hermes-agent). Commands from `README.md`:

```bash
hermes profile install https://github.com/vivian-maes/mimir.git
hermes update            # ⚠️ seeds bundled base skills — without it the profile has only the 8 wiki-*
hermes -p mimir chat     # first launch → hermes setup wizard (provider, terminal, optional Telegram gateway)
```

Gotchas (all recorded in `README.md` / `config.yaml` comments):

- Invocation is always `hermes -p mimir <cmd>`; `hermes mimir …` is invalid unless installed with `--alias`.
- Update uses the **profile name**: `hermes profile update mimir` (the git URL fails with "not a distribution"). Re-`install` without `--force` fails on an existing profile; `--force` overwrites only the `distribution_owned` files (`SOUL.md`, `config.yaml`, `skills/` per `distribution.yaml`) — other copied files survive.
- `hermes profile install` copies the **whole repo** into `~/.hermes/profiles/mimir/` (except `.env`, `auth.json`, `memories/`, `sessions/`); there is no `files:` key. This is why cross-skill imports in [overview](../architecture/overview.md) are safe.
- Model/LLM is intentionally *not* pinned in `config.yaml` — inherited from the default profile or set via `hermes -p mimir setup`.

## Configure the vault

Copy `wiki.config.example.json` → `wiki.config.json` in the profile dir (auto-discovered since v0.3.3) and validate with `hermes profile info mimir`. Resolution order and schema are documented in [overview](../architecture/overview.md#configuration-configure-once-call-without---config). Minimum: `work_root` + `sync.backend`. Secrets are referenced by **env var name only** (`MIMIR_KDRIVE_PASS`, `MIMIR_OBSIDIAN_API_KEY`) — never stored in JSON. `MIMIR_CONFIG` overrides config location; `MIMIR_SHARED` overrides the shared-core path (dev/testing).

First use on an empty vault: ask Mimir to "initialize the wiki" ([wiki-init](../pipeline/ingestion-skills.md#wiki-init--vault-bootstrap)), or check location with "where is my wiki?".

## System dependencies (`skills/_shared-references/TOOLING.md`)

Python ≥ 3.11, git; `rclone` for the rclone backend. Extractor libs (PyMuPDF, poppler, ocrmypdf, tesseract, ebooklib, trafilatura, bs4, markdownify, requests) are listed in `skills/wiki-extract/requirements.txt` — every one degrades gracefully to CLI/stdlib, so the suite runs (with lower fidelity) without them. Optional core deps: `jsonschema`, `PyYAML`, `pytest`.

## Sync operations & troubleshooting

Manual control: `python skills/wiki-sync/scripts/wiki_sync.py {status-like subcommands}` — `lock|pull|push|validate|sync [--dry-run]`. Semantics and backend internals: [wiki-sync](../pipeline/knowledge-skills.md#wiki-sync--pluggable-vault-synchronization) and [shared core sync](../architecture/shared-core.md#sync-backends-scriptsync).

| Symptom | Likely cause / action |
|---|---|
| Sync "lost state" / bisync refuses | self-heal `bisync --resync` (lossless union) should fire automatically (`sync/rclone.py`); never substitute one-way `rclone sync` — it destroys one side |
| 401 from kDrive WebDAV | `sync.rclone.remote_setup` auto-recreates the remote; check `MIMIR_KDRIVE_PASS` is exported |
| Pull fails in a pipeline run | by design the skill stops — resolve remote conflict before compiling |
| Validate reports divergence after Obsidian sync | expected risk (stale plugin state); `auto` backend falls back to rclone push+validate in the same run; standalone obsidian backend needs an rclone remote just for validation |
| "lock held" skip | another run owns `~/.cache/mimir/wiki-sync.lock`; stale locks (>30 min) are reclaimed automatically |
| Dead links on macOS | NFD/NFC filename drift — run `wiki-index audit`, then `fix` ([knowledge skills](../pipeline/knowledge-skills.md#wiki-index--indexes-audit-assisted-link-repair)) |
| Push aborted unexpectedly | opt-in `sync.presync_gate=true` aborts push on audit anomalies — fix links or disable the gate |

## Vault schema migrations ("paliers")

`wiki-init status` shows `SCHEMA=<vault>` vs `EXPECTED=4` and pending paliers. Migrations run lock → plan → snapshot → apply → validate → bump `.mimir/vault.json`; only the plan delta is snapshotted to `.mimir/snapshots/` (machine-local, excluded from sync via `filters.txt`). Engine, exit codes (2 validation-fail/restore, 3 lock, 4 missing palier), and what each palier does: [shared core migration engine](../architecture/shared-core.md#migration-engine-scriptsmigration--capability-vault-migration). Interrupted runs are resumable: moves are idempotent and lazy paliers track per-file `schema_version`.

## Testing

No pytest config or packaging files exist; run from anywhere in the repo:

```bash
pytest skills/_shared-references/tests      # socle: config, guard, sync, migration (24 files)
pytest skills/wiki-extract/tests skills/wiki-ingest/tests skills/wiki-batch-produce/tests \
       skills/wiki-reading-grid/tests skills/wiki-index/tests skills/wiki-sync/tests \
       skills/wiki-research/tests skills/wiki-init/tests
```

Suite size at HEAD: **446 tests** (`CHANGELOG.md` v0.6.1). Each skill's `tests/conftest.py` injects the needed `sys.path` dirs (the shared socle; `wiki-extract/scripts` and `wiki-reading-grid/scripts` for cross-importing modules/tests). Hermeticity patterns to preserve when adding tests:

- Inject a fake `runner` (argv recorder) for rclone/git and a fake `http_client` for obsidian/auto — no real binaries or network.
- `tmp_path` vaults + `monkeypatch` of `sync.locking._DEFAULT_LOCK` (never write `~/.cache`).
- `migration.registry.clear()` between migration cases; `NoopBackend` for sync-less fixtures.
- Optional deps must stay optional so the bare suite runs green (`jsonschema`, `PyYAML`, `requests` all have fallbacks).
- `link_audit` exit-code contract (0 iff clean) is relied on by CI-style usage, the pre-push gate, and migration validation — regression-test changes there.
- `slug.py` carries doctests (`python slug.py`).

## Dev-loop notes

- Working tree at HEAD (v0.6.1) carries an uncommitted fr→en translation of docs/SKILL.md — account for it in diffs; generated vault artifacts (status strings, blurbs) remain French.
- To run skills outside Hermes during development, point `MIMIR_CONFIG` at a test config and `MIMIR_SHARED` at `skills/_shared-references/scripts` if relocated.
- Dead code to eventually clean: double `_apply` in `migration/paliers/v1_v2_pdf_assets.py`; `wiki-index/SKILL.md` documents 3 audit passes but the code runs 4.
