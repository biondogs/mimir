---
type: Concept
title: Mimir Shared Core Library
description: The skills/_shared-references Python socle used by all 8 wiki-* skills — bootstrap, config loading, confinement guard, slug/wikilink helpers, ledger, status table, pluggable sync backends, and the vault migration engine with paliers.
tags: [mimir, shared-library, sync, migration, config, conventions]
resource: /skills/_shared-references
---

# Architecture: Shared Core Library (`skills/_shared-references`)

The "socle" is the single source of truth for everything the [8 wiki-* skills](../architecture/overview.md) share: configuration, safety invariants, file mechanics, sync, and migrations. It also hosts the three normative digest docs (`CONVENTIONS.md`, `FRONTMATTERS.md`, `TOOLING.md`) and the config JSON schema.

## Entry point: `scripts/bootstrap.py`

Idempotently prepends `$MIMIR_SHARED` (if a valid dir) then the bundled `scripts/` to `sys.path` (paths resolved first to defeat macOS `/var`↔`/private/var` duplicates), then re-exports `config_loader`, `frontmatter`, `iohelpers`, `ledger`, `migration`, `slug`, `vault_meta` and the `guard` symbols. Missing symbols therefore raise `ImportError` at startup, never `NameError` mid-run. The motivation (recorded in `wiki-batch-produce`): a real "Wharram run" crash caused by `safe_path not imported`.

## Config & schema

- `config_loader.py` — `resolve_config_path()` order: explicit → `$MIMIR_CONFIG` (defined-but-missing is tolerated) → profile dir → XDG → cwd. `load_config()` validates via `jsonschema` against `schema/wiki.config.schema.json` when installed, else a manual fallback (non-empty `work_root`, backend ∈ rclone/git/obsidian/auto). `work_root` is `~`-expanded, resolved relative to the config file, `realpath`'d. Exposes `DEFAULT_LAYOUT`, `ASSETS_DIRNAME="_assets"`, `EXPECTED_SCHEMA = 4`, `VAULT_META_REL = ".mimir/vault.json"`.
- `schema/wiki.config.schema.json` — draft 2020-12; requires `work_root`; `sync.backend` enum `rclone|git|obsidian|auto`; `sync.presync_gate` (opt-in audit-before-push, consumed by the sync skill); per-backend blocks. Secret references are always **env var names** (`pass_env`, `api_key_env`), never values.

## Safety & file mechanics

| Module | Guarantees |
|---|---|
| `guard.py` | `is_within` / `assert_within` / `safe_path` on `realpath` + `commonpath` — defeats `..` and symlinks; the confinement invariant of the whole repo |
| `iohelpers.py` | `atomic_write_text`, `write_json` (tmp + `os.replace`), `unique_suffixed_path` (`-2`/`-3` on the *base slug*, shared across a source's file family); every atomic write calls `guard.assert_within` first |
| `slug.py` | ASCII kebab-case slugify (NFKD accent stripping, 80 chars); `same_file()` compares NFC-normalized — never `str.__eq__` (macOS/APFS stores NFD, Obsidian links are NFC) |
| `wikilinks.py` | `[[...]]` parse (alias `|`, anchor `#`/`^` stripped); resolution prefixed → `WIKI/`-relative → file-relative, each candidate `normpath`'d, confinement-checked, NFD/NFC-tolerant; `majority_form()` keeps index writes consistent; `chapter_anchor()` derives heading + Prev/Next from one string |
| `frontmatter.py` | dependency-free tolerant parser; `tags`/`sources` accept flow **and** block lists; `read_schema_version()` never raises (absence = un-migrated) |
| `ledger.py` | `.wiki/ingest-ledger.json` — SHA256 of the *content file* → compiled state; atomic; corrupt ⇒ `{}`; deliberately machine-local and **out of sync** |
| `status_table.py` | `raw/<type>/_status.md` — the only tolerated mutation inside `raw/`; columns Fichier|SHA256|Statut|Articles wiki|Grille|MAJ |
| `article_index.py` | extracts the one-line `>` summary used in subject indexes |
| `vault_meta.py` | `.mimir/vault.json` = `{schema, migrated, history[]}`; absent/corrupt ⇒ baseline `1`, never auto-rewritten; `bump_vault_schema` is atomic + confined + appends history |
| `lint_docs.py` | detects ASCII-art schemes (Mermaid-mandatory) and >5000-char heading-less dumps |
| `validate_skills.py` | stands in for `skills-ref`: SKILL.md frontmatter contract (name regex, description ≤1024, metadata placement) |

## Sync backends (`scripts/sync/`)

`base.py` defines the `SyncBackend` ABC (`pull` / `push` / `validate`) with an injectable `runner` (and `http_client` for HTTP backends) so tests never need real binaries. `__init__.py` `get_backend()` is a lazy factory: no `sync` key ⇒ `NoopBackend`; unknown ⇒ `SyncError`.

- **`locking.py`** — mutex via atomic `mkdir` **outside** `work_root` (default `~/.cache/mimir/wiki-sync.lock`; §12.9 rule: the lock must never sync itself). `owner.json` metadata; stale >30 min reclaimed; held ⇒ `LockHeld`.
- **`rclone.py`** (default) — `rclone bisync` used identically for pull and push. `--size-only` is **mandatory** (kDrive WebDAV has no reliable modtime/hash; without it bisync ping-pongs deletions). Hardening: warm-up `lsf`, bounded retry (3×, exponential backoff) only on transient patterns, and **unconditional self-heal** `bisync --resync` (lossless union) on lost state — the `auto_resync` config key is obsolete but kept for compat. It never falls back to destructive one-way `rclone sync`. `remote_setup` can (re)create the WebDAV remote from `pass_env`. `validate()` is anti-stale: local count vs `rclone lsf`, tolerance 5, one re-reconcile.
- **`git.py`** — pull `--rebase`; add/commit/push restricted to `sync.git.scope` (default `_inbox|raw|wiki|reading-grids|.mimir` — `.mimir` so the schema marker travels; `.wiki/` excluded by omission). Validate = local HEAD vs `origin/<branch>`.
- **`obsidian.py`** — does not reimplement sync; triggers the **Remotely Save** plugin through Obsidian's Local REST API (`POST /commands/remotely-save:start-sync`, Bearer key from `api_key_env`), fire-and-forget + settle wait, then validates via `rclone lsf` counts (an rclone remote is still required for validation). Optional `auto_launch`. References: `skills/wiki-sync/references/OBSIDIAN_REMOTELY_SAVE.md`.
- **`auto.py`** — probes (and optionally launches) Obsidian at construction; picks one active sub-backend. **Intra-run fallback**: if Obsidian `validate()` fails, performs rclone push+validate in the same run. Never runs two synchronizers in parallel.
- **`noop.py`** — all ops return 0 ("content first, sync later"); used by test fixtures and unsynced setups.

## Migration engine (`scripts/migration/`) — capability `vault-migration`

The vault structure evolves; paliers migrate old vaults safely. Added in v0.6.0 (see `CHANGELOG.md`).

- `registry.py` — `Migration` dataclass (`from_schema`/`to_schema`, pure `plan(cfg)`, optional custom `apply`, `preserves_counts`). Rejects duplicates, retrograde steps, gaps; `pending_for()` never skips versions. `migration/__init__.py` auto-registers paliers at import so `wiki-init status` can list them.
- `engine.py` `run_one` — sequence **lock → plan → snapshot delta → apply → validate → bump `.mimir/vault.json` → unlock**. Idempotence rules for interrupted runs (dst exists & src gone ⇒ skip; both exist ⇒ raise). Exit codes: validation fail ⇒ restore + 2; lock held ⇒ 3; missing palier ⇒ 4. `run_pending` chains paliers.
- `snapshot.py` — copies **only the plan delta** to `.mimir/snapshots/pre-v<N>-<date>/` (machine-local, excluded from sync) + `plan.json`; restore recreates moved sources *and deletes produced destinations* (no ghosts).
- `lazy.py` — per-file supervision for "creator" paliers: `select_lagging()` lists `wiki/`+`reading-grids/` files whose frontmatter `schema_version < target`; `mark_migrated()` refuses downgrades.
- `validate.py` — post-apply invariants: 0 broken links (reuses wiki-index's `link_audit` if present), article/asset count preservation for `preserves_counts` paliers, no symlink escapes from `work_root`.
- Paliers (`paliers/`): **v1→v2** per-source PDF/EPUB asset regrouping (longest-prefix match against known source slugs; unknown files ignored, never invented); **v2→v3** web fidelity refetch rebuild of the `.html`+`.md`+`_assets/` triplet (custom apply, best-effort, duplicate clips listed not merged); **v3→v4** reading-grid "guide" format (deterministic skeleton + blurb zones, reinjects existing prose).

Known dead code: `v1_v2_pdf_assets.py` defines `_apply` twice (both no-ops); `v3_v4` has a redundant schema-version wrapper.

## Docs & tests

- `CONVENTIONS.md` is an explicit **pointer digest** (≤50 lines): normative rules live in `openspec/specs/<cap>/spec.md` — a directory **not present in this snapshot** (see quickstart backlog). Code comments still cite the old centralized pitfall numbers (§12.x).
- `FRONTMATTERS.md` defines the canonical frontmatter shapes (Karpathy article, reading grid, `.toc.json`, `_status.md`, clipping, SKILL.md) — summarized on the [conventions page](../domain/conventions-and-method.md).
- `TOOLING.md` lists system deps and the graceful-degradation policy (rich lib → CLI → stdlib) observed on the dev machine.
- Tests: 24 files in `tests/`, one per module; `conftest.py` only prepends three `sys.path` dirs (shared scripts, `wiki-extract/scripts`, `wiki-reading-grid/scripts`). No pytest config anywhere — run `pytest skills/<area>/tests`. Hermeticity: fake `runner`/`http_client`, `tmp_path`, monkeypatched lock, `registry.clear()`. See [Runbook & Testing](../operations/runbook.md).
