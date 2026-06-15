#!/usr/bin/env python3
"""Moteur de migration `vault-migration` — orchestration sûre sans git.

Séquence (cf. spec `vault-migration` §3 — *Protocole de migration sûr sans git*) :

    1. LOCK sync        verrou hors-zone (sync.lock, déjà partagé avec wiki-sync)
    2. PLAN (dry-run)   un palier à la fois, listing (avant → après), aucun effet
    3. SNAPSHOT ciblé   copie du delta sous .mimir/snapshots/pre-v<N>-<date>/
    4. APPLY            atomique (iohelpers), confiné work_root, idempotent
    5. VALIDATE         invariants (validate.py) ; échec → restore(snapshot) + exit ≠ 0
    6. BUMP             .mimir/vault.json : schema = N, history += {to: N, at}
    7. PUSH             sync.push (publication de la réorganisation)
    8. UNLOCK

Le moteur avance **un palier à la fois** (`run_one(cfg, palier)`) — le chainage
de N paliers d'affilée est laissé à l'appelant (ou à un futur `run_all`).

**Aucune migration silencieuse** : si `current_schema == EXPECTED_SCHEMA`, le
moteur ne fait rien et renvoie `0` proprement. Une ré-exécution sur un palier
déjà appliqué détecte un plan vide et skip sans erreur.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

import guard
import iohelpers
import sync as sync_mod
import vault_meta

from .plan import Plan
from .registry import Migration, pending_for
from . import snapshot as snap_mod
from . import validate as validate_mod

#: Code retour « succès, rien à faire » (= 0 par convention Unix).
RC_OK = 0
#: Code retour « validation post-apply échouée → rollback effectué ».
RC_VALIDATION_FAILED = 2
#: Code retour « verrou tenu par un autre process → migration annulée ».
RC_LOCK_HELD = 3
#: Code retour « un palier référencé n'est pas dans le registre ».
RC_NO_MIGRATION = 4


@dataclass
class RunResult:
    """Résultat agrégé d'un `run_one`. `bumped = True` si la version a avancé."""

    rc: int
    schema_from: int
    schema_to: int
    plan: Plan
    applied: bool
    bumped: bool
    validation: validate_mod.ValidationResult | None = None
    message: str = ""


def _resolve_lock(cfg):
    """Acquiert le verrou de synchro (mutex hors-zone, partagé avec wiki-sync)."""
    return sync_mod.lock(cfg)


def _atomic_apply(cfg, plan: Plan) -> None:
    """Exécute le plan : `move` / `write` / `delete`, atomique et confiné.

    Idempotence : si un `move` détecte que `dst` existe déjà ET que `src` est
    absent (= déjà migré), c'est un no-op (pas d'écrasement silencieux). Si
    `src` ET `dst` existent tous les deux, on lève : le palier n'est pas
    idempotent (à corriger dans le palier).
    """
    work_root = cfg.work_root
    for op in plan.operations:
        if op.action == "move":
            src_abs = guard.assert_within(work_root, op.src)
            dst_abs = guard.assert_within(work_root, op.dst) if op.dst else src_abs
            if not src_abs.exists() and dst_abs.exists():
                # Déjà migré : no-op silencieux (idempotence).
                continue
            if src_abs.exists() and dst_abs.exists():
                raise RuntimeError(
                    f"ambiguïté : {src_abs} ET {dst_abs} existent — palier non idempotent"
                )
            if not src_abs.exists():
                # Source absente : on saute (cas reprise après interruption).
                continue
            iohelpers.ensure_dir(dst_abs.parent)
            # Déplacement atomique via tmp + rename (le cas échantillon est petit,
            # le coût d'un `os.replace` est négligeable).
            tmp = dst_abs.with_suffix(dst_abs.suffix + ".tmp-mv")
            shutil.move(str(src_abs), str(tmp))
            tmp.replace(dst_abs)
        elif op.action == "write":
            assert op.content is not None  # validé à la construction
            dst_abs = guard.assert_within(work_root, op.dst) if op.dst else None
            if dst_abs is None:
                continue
            iohelpers.atomic_write_text(dst_abs, op.content, work_root=work_root)
        elif op.action == "delete":
            src_abs = guard.assert_within(work_root, op.src)
            # Idempotence : src déjà absent = no-op.
            if src_abs.exists():
                if src_abs.is_dir():
                    shutil.rmtree(src_abs)
                else:
                    src_abs.unlink()


def run_one(cfg, migration: Migration, *, dry_run: bool = False) -> RunResult:
    """Exécute un palier. Renvoie un `RunResult` détaillé (sans crash).

    - `dry_run=True` : calcule le `plan`, le rend texte, **n'écrit rien**.
    - `dry_run=False` : séquence complète (snapshot → apply → validate → bump).
    """
    schema_from = vault_meta.read_vault_schema(cfg)
    schema_to = migration.to_schema

    # Si on est déjà à schema_to, le palier est déjà appliqué → no-op.
    if schema_from >= schema_to:
        return RunResult(
            rc=RC_OK, schema_from=schema_from, schema_to=schema_to,
            plan=Plan(), applied=False, bumped=False,
            message=f"palier v{schema_to} déjà appliqué (schema actuel = {schema_from})",
        )

    # 1) Plan (dry-run pur) — l'engine le calcule sans lock pour permettre
    # l'inspection préalable par un humain.
    plan = migration.plan(cfg)
    if plan.empty:
        # Idempotence : le plan est vide alors que schema_from < schema_to.
        # C'est un cas où le palier a déjà tourné partiellement (snapshot d'un
        # précédent run) : on bump et on sort proprement.
        vault_meta.bump_vault_schema(cfg, schema_to)
        return RunResult(
            rc=RC_OK, schema_from=schema_from, schema_to=schema_to,
            plan=plan, applied=False, bumped=True,
            message="plan vide — bump effectué (idempotence)",
        )

    if dry_run:
        sys.stdout.write(plan.render())
        return RunResult(
            rc=RC_OK, schema_from=schema_from, schema_to=schema_to,
            plan=plan, applied=False, bumped=False,
            message="dry-run : aucun fichier modifié",
        )

    # 2) Verrou + 3) Snapshot + 4) Apply + 5) Validate + 6) Bump
    try:
        lk = _resolve_lock(cfg)
    except sync_mod.LockHeld as exc:
        print(f"🔒 {exc} — migration annulée (autre synchro en cours).")
        return RunResult(
            rc=RC_LOCK_HELD, schema_from=schema_from, schema_to=schema_to,
            plan=plan, applied=False, bumped=False,
            message="verrou tenu",
        )
    try:
        before = validate_mod.capture_counts(cfg)
        snapshot = snap_mod.create(cfg, plan, schema_from, schema_to)
        # Si le palier fournit un `apply` custom (ex. refetch réseau, post-
        # traitement complexe), c'est lui qui prend la main. Sinon, on
        # retombe sur l'apply atomique par défaut (moves/writes/deletes).
        if migration.apply is not None:
            migration.apply(cfg, plan)
        else:
            _atomic_apply(cfg, plan)
        validation = validate_mod.validate(
            cfg, before, preserves_counts=migration.preserves_counts
        )
        if not validation.ok:
            # 5') Échec → restore + exit ≠ 0
            print(validation.summary())
            snap_mod.restore(snapshot, cfg)
            return RunResult(
                rc=RC_VALIDATION_FAILED, schema_from=schema_from, schema_to=schema_to,
                plan=plan, applied=True, bumped=False, validation=validation,
                message="validation post-apply échouée — rollback effectué",
            )
        # 6) Bump + cleanup du snapshot (succès = plus besoin)
        vault_meta.bump_vault_schema(cfg, schema_to)
        shutil.rmtree(snapshot.dir, ignore_errors=True)
        return RunResult(
            rc=RC_OK, schema_from=schema_from, schema_to=schema_to,
            plan=plan, applied=True, bumped=True, validation=validation,
            message="migration réussie",
        )
    finally:
        lk.release()


def run_pending(cfg, *, target: int | None = None, dry_run: bool = False) -> list[RunResult]:
    """Enchaîne les paliers en attente depuis le schéma courant.

    - `target=None` : on vise le **palier suivant** uniquement (`current + 1`).
      C'est le mode « un pas à la fois » utilisé par les tests et le CLI.
    - `target=int`  : on chausse jusqu'à `target` (ex. `EXPECTED_SCHEMA`).
    """
    current = vault_meta.read_vault_schema(cfg)
    paliers = pending_for(current, target=target)
    if not paliers:
        return [RunResult(
            rc=RC_NO_MIGRATION if target is not None else RC_OK,
            schema_from=current, schema_to=current,
            plan=Plan(), applied=False, bumped=False,
            message="aucun palier en attente",
        )]
    results: list[RunResult] = []
    for p in paliers:
        res = run_one(cfg, p, dry_run=dry_run)
        results.append(res)
        if res.rc != RC_OK:
            break  # stop au premier échec
    return results
