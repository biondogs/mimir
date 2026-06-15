#!/usr/bin/env python3
"""wiki-sync — synchronisation pluggable du vault Mimir (SPEC §10).

Usage :
    wiki_sync.py --config <wiki.config.json> {lock|pull|push|validate|sync} [--dry-run]

- `lock`     : teste l'acquisition du verrou (mutex hors work_root) puis le relâche.
- `pull`     : récupère l'état distant → local (pré-sync).
- `push`     : publie local → distant (post-sync).
- `validate` : vérifie la cohérence local↔distant.
- `sync`     : cycle complet `pull → push → validate` (trigger « synchronise le vault »).

Le backend (`rclone` | `git`, ou `noop` si le config n'a pas de clé `sync`) est
choisi par `sync.backend`. Le périmètre est **borné à `work_root`** (confinement,
SPEC §2) ; le verrou vit **hors** zone synchronisée (vigilance §12.9).

**Garde-fou pré-push (opt-in)** : si `sync.presync_gate = true`, `push` et `sync`
lancent `wiki-index audit` via le **runner injectable** avant le push ; si l'audit
signale une anomalie, le push est **avorté** (exit non-zéro) et l'état local est
préservé (SPEC sync-vault::opt-in-pre-push-validation-gate).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# --- bootstrap : importer le socle via le module unifié `bootstrap` ---------
# (L'insertion de sys.path est centralisée dans `bootstrap._ensure_socle_path` :
#  MIMIR_SHARED honoré, idempotent, fail fast sur symbole manquant.)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared-references" / "scripts"))
import bootstrap  # noqa: E402,F401  (socle mutualisé ; voir skills/_shared-references/scripts/bootstrap.py)

import config_loader  # noqa: E402
import sync  # noqa: E402


# Chemin du CLI `wiki_index.py` (résolu une fois pour le gate `presync_gate`).
_WIKI_INDEX_CLI = (
    Path(__file__).resolve().parents[2] / "wiki-index" / "scripts" / "wiki_index.py"
)


def _line(op: str, rc: int) -> str:
    return f"{'✅' if rc == 0 else '❌'} {op} (code {rc})"


def _flush_warnings(backend) -> None:
    for w in getattr(backend, "warnings", []):
        print(f"⚠️  {w}")


def _with_lock(cfg, fn) -> int:
    """Exécute `fn` sous verrou ; verrou déjà tenu = skip propre (code 0, SPEC §10)."""
    try:
        lk = sync.lock(cfg)
    except sync.LockHeld as exc:
        print(f"🔒 {exc} — une autre synchro tourne, skip.")
        return 0
    try:
        return fn()
    finally:
        lk.release()


def _run_presync_audit(cfg, runner) -> int:
    """Si `sync.presync_gate=true`, lance `wiki_index audit` via `runner`.

    - **Désactivé** (clé absente / `false`) : renvoie 0 sans rien spawner
      (non-régression, SPEC §10 scenario « Gate disabled is the default »).
    - **Activé** : spawn l'audit via le runner injectable et renvoie son exit
      code. stdout/stderr de l'audit sont relayés sur les flux du CLI pour que
      l'utilisateur voie le rapport (sinon le push est avorté sans contexte).
    """
    if not cfg.sync.get("presync_gate", False):
        return 0
    argv = [sys.executable, str(_WIKI_INDEX_CLI), "--config", str(cfg.config_path), "audit"]
    result = runner(argv, capture_output=True, text=True)
    if getattr(result, "stdout", None):
        sys.stdout.write(result.stdout)
    if getattr(result, "stderr", None):
        sys.stderr.write(result.stderr)
    return int(getattr(result, "returncode", 0))


def cmd_lock(cfg, **_) -> int:
    return _with_lock(cfg, lambda: (print("✅ verrou disponible"), 0)[1])


def cmd_pull(cfg, **_) -> int:
    def run():
        backend = sync.get_backend(cfg)
        rc = backend.pull()
        print(_line("pull", rc))
        _flush_warnings(backend)
        return rc

    return _with_lock(cfg, run)


def cmd_push(cfg, *, runner=None, **_) -> int:
    runner = runner or subprocess.run

    def run():
        gate_rc = _run_presync_audit(cfg, runner)
        if gate_rc != 0:
            print(_line("presync audit", gate_rc))
            print("🛑 push avorté — état local préservé.")
            return gate_rc
        backend = sync.get_backend(cfg, runner)
        rc = backend.push()
        print(_line("push", rc))
        _flush_warnings(backend)
        return rc

    return _with_lock(cfg, run)


def cmd_validate(cfg, **_) -> int:
    def run():
        rc = sync.get_backend(cfg).validate()
        print(_line("validate", rc))
        return rc

    return _with_lock(cfg, run)


def cmd_sync(cfg, *, dry_run: bool = False, runner=None) -> int:
    """Cycle complet `pull → push → validate` sous un unique verrou."""
    runner = runner or subprocess.run

    def cycle():
        backend = sync.get_backend(cfg, runner)
        rc = backend.pull()
        print(_line("pull", rc))
        if rc != 0:
            return rc
        if not dry_run:
            gate_rc = _run_presync_audit(cfg, runner)
            if gate_rc != 0:
                print(_line("presync audit", gate_rc))
                print("🛑 push avorté — état local préservé.")
                return gate_rc
            rc = backend.push()
            print(_line("push", rc))
            if rc != 0:
                _flush_warnings(backend)
                return rc
        rc = backend.validate()
        print(_line("validate", rc))
        _flush_warnings(backend)
        return rc

    return _with_lock(cfg, cycle)


_COMMANDS = {
    "lock": cmd_lock,
    "pull": cmd_pull,
    "push": cmd_push,
    "validate": cmd_validate,
    "sync": cmd_sync,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronisation pluggable du vault (Mimir).")
    parser.add_argument(
        "--config", default=None,
        help="Chemin du wiki.config.json (optionnel ; auto-découverte si omis : "
             "$MIMIR_CONFIG, dossier du profil, ~/.config/mimir/wiki.config.json, ./wiki.config.json)",
    )
    parser.add_argument("command", choices=sorted(_COMMANDS), help="Opération de synchro")
    parser.add_argument("--dry-run", action="store_true", help="`sync` : pull+validate sans push")
    args = parser.parse_args(argv)

    cfg = config_loader.load_resolved_config(args.config)
    if args.command == "sync":
        return cmd_sync(cfg, dry_run=args.dry_run)
    return _COMMANDS[args.command](cfg)


if __name__ == "__main__":
    raise SystemExit(main())
