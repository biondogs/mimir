#!/usr/bin/env python3
"""Snapshot ciblé d'un palier de migration (delta only) — capability `vault-migration`.

**Rôle** : avant tout `apply`, on copie **uniquement** les fichiers listés dans
le `Plan` sous `.mimir/snapshots/pre-v<N>-<date>/` (hors-sync, jamais
synchronisé vers le distant). En cas d'échec de validation post-apply, on
restaure depuis ce snapshot.

**Pourquoi un delta, pas le vault entier** : un vault peut peser plusieurs Go
d'images (PDF, EPUB, captures web). Copier 2000+ assets pour déplacer 40
fichiers serait prohibitif. Le delta isole exactement ce que l'apply va toucher
— c'est suffisant pour un rollback local et bien plus léger.

**Forme du dossier snapshot** :

    .mimir/snapshots/pre-v<N>-<date>/
        plan.json         # Plan sérialisé (preuve, audit)
        <rel_path_1>      # copie de l'état **avant** apply
        <rel_path_2>
        ...

`restore(snapshot_dir)` recopie chaque fichier du snapshot **à sa position
d'origine** (le `src` du Plan), puis supprime le dossier snapshot. Il NE
touche qu'aux fichiers du delta — pas au reste du vault.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import guard
import iohelpers

from .plan import Operation, Plan


@dataclass
class Snapshot:
    """Référence à un dossier de snapshot créé. `restore()` ramène le delta."""

    dir: Path
    plan: Plan
    schema_from: int
    schema_to: int


def _snapshot_name(schema_to: int, day: str | None = None) -> str:
    """Nom canonique d'un dossier snapshot. `day` = ISO date (défaut : aujourd'hui)."""
    d = day if isinstance(day, str) and day else date.today().isoformat()
    return f"pre-v{schema_to}-{d}"


def create(cfg, plan: Plan, schema_from: int, schema_to: int, *, day: str | None = None) -> Snapshot:
    """Copie le delta du `Plan` sous `.mimir/snapshots/pre-v<N>-<date>/`.

    - Crée le dossier (confiné `work_root`).
    - Sérialise le Plan dans `plan.json` (preuve / audit).
    - Pour chaque `move` : copie `src` à son `src` (l'**état avant** apply).
    - Pour chaque `write` : copie `dst` (l'**état avant** apply, le contenu
      actuel du fichier qui sera écrasé).
    - `delete` n'a rien à copier (le snapshot de l'absence est l'absence) — on
      journalise l'intention dans `plan.json`, c'est tout.

    Renvoie un `Snapshot` réutilisable par `restore()`. Lève `FileNotFoundError`
    si un `src` n'existe pas (le plan a tort — l'apply n'aurait pas pu être
    idempotent non plus).
    """
    guard.assert_within(cfg.work_root, cfg.SNAPSHOTS_DIR)
    snap_dir = cfg.SNAPSHOTS_DIR / _snapshot_name(schema_to, day)
    guard.assert_within(cfg.work_root, snap_dir)
    iohelpers.ensure_dir(snap_dir)

    # Sérialise le plan (preuve d'intention)
    plan_doc = {
        "schema_from": schema_from,
        "schema_to": schema_to,
        "description": plan.description,
        "operations": [
            {
                "action": op.action,
                "src": _rel(cfg, op.src),
                "dst": _rel(cfg, op.dst) if op.dst else None,
            }
            for op in plan.operations
        ],
    }
    iohelpers.write_json(snap_dir / "plan.json", plan_doc, work_root=cfg.work_root)

    # Copie le delta
    for op in plan.operations:
        if op.action in ("move", "write"):
            src_abs = guard.assert_within(cfg.work_root, op.src)
            if not src_abs.is_file():
                # `move`/`write` sur un fichier inexistant : le plan est inapplicable
                # → on **nettoie** le snapshot partiel (sinon état trompeur) puis lève.
                shutil.rmtree(snap_dir, ignore_errors=True)
                raise FileNotFoundError(
                    f"plan référence un fichier absent : {src_abs} (palier v{schema_from}→v{schema_to})"
                )
            dst_abs = snap_dir / _rel(cfg, op.src)
            iohelpers.ensure_dir(dst_abs.parent)
            shutil.copy2(src_abs, dst_abs)
        # `delete` : pas de copie (le snapshot de l'absence est l'absence).

    return Snapshot(dir=snap_dir, plan=plan, schema_from=schema_from, schema_to=schema_to)


def restore(snapshot: Snapshot, cfg) -> None:
    """Restaure le delta d'un `Snapshot` à sa position d'origine, puis nettoie.

    Sémantique par opération :

    - `move(src, dst)` : l'apply a déplacé `src` → `dst`. On **restaure `src`**
      (snapshot) **et on supprime `dst`** (le nouveau fichier produit par
      l'apply) — sinon le rollback laissait des fichiers « fantômes ».
    - `write(dst, ...)` : l'apply a écrasé `dst`. On **restaure `dst`** à
      son contenu d'avant (snapshot du fichier pré-écriture).
    - `delete(src)` : l'apply a supprimé `src`. On **restaure `src`** depuis
      le snapshot (la copie faite par `create()` avant l'apply).

    En cas d'échec en milieu de restore, le dossier snapshot est **conservé**
    (l'opérateur peut retenter). On **n'avale pas** l'exception — l'appelant
    décide du code de sortie.
    """
    if not snapshot.dir.is_dir():
        # Déjà restauré (ou n'a jamais existé) → no-op.
        return
    for op in snapshot.plan.operations:
        if op.action == "move":
            snap_file = snapshot.dir / _rel(cfg, op.src)
            src_abs = guard.assert_within(cfg.work_root, op.src)
            dst_abs = guard.assert_within(cfg.work_root, op.dst) if op.dst else src_abs
            if not snap_file.is_file():
                print(f"⚠️  snapshot incomplet : {snap_file} absent (skip move)")
                continue
            # 1) Restaurer le src (atomique, comme l'apply).
            iohelpers.ensure_dir(src_abs.parent)
            iohelpers.atomic_write_bytes(src_abs, snap_file.read_bytes(), work_root=cfg.work_root)
            # 2) Supprimer le dst (le nouveau fichier produit par l'apply) —
            # sinon le rollback laissait un fichier « orphelin » à `dst`.
            if dst_abs.exists() and dst_abs.resolve() != src_abs.resolve():
                if dst_abs.is_dir():
                    shutil.rmtree(dst_abs, ignore_errors=True)
                else:
                    dst_abs.unlink()
        elif op.action == "write":
            snap_file = snapshot.dir / _rel(cfg, op.src)
            if not snap_file.is_file():
                print(f"⚠️  snapshot incomplet : {snap_file} absent (skip write)")
                continue
            dst_abs = guard.assert_within(cfg.work_root, op.dst) if op.dst else op.src
            iohelpers.ensure_dir(dst_abs.parent)
            iohelpers.atomic_write_bytes(dst_abs, snap_file.read_bytes(), work_root=cfg.work_root)
        elif op.action == "delete":
            snap_file = snapshot.dir / _rel(cfg, op.src)
            src_abs = guard.assert_within(cfg.work_root, op.src)
            if not snap_file.is_file():
                print(f"⚠️  snapshot incomplet : {snap_file} absent (skip delete)")
                continue
            iohelpers.ensure_dir(src_abs.parent)
            iohelpers.atomic_write_bytes(src_abs, snap_file.read_bytes(), work_root=cfg.work_root)
    # Nettoyage du dossier snapshot (delta restauré → plus besoin).
    shutil.rmtree(snapshot.dir, ignore_errors=True)


def _rel(cfg, path: Path) -> str:
    """Chemin relatif à `work_root` (POSIX). Lève si le chemin est hors racine.

    Robuste aux symlinks (`/var` ↔ `/private/var` sur macOS) : on compare les
    chemins **résolus** (côté `work_root` aussi) avant `relative_to`, sinon le
    `ValueError` nous fait retomber sur le chemin absolu — ce qui corrompt
    ensuite `snap_dir / _rel(...)` (un `Path / str_absolu` **remplace** le
    préfixe, d'où le `SameFileError` sur `shutil.copy2`).
    """
    abs_p = guard.assert_within(cfg.work_root, path)
    root_resolved = Path(os.path.realpath(cfg.work_root))
    try:
        return os.path.relpath(str(abs_p), str(root_resolved))
    except ValueError:  # lecteurs différents (Windows)
        return str(abs_p)


def latest_snapshot(cfg, schema_to: int) -> Snapshot | None:
    """Trouve le dernier snapshot pour un palier donné (ou `None`).

    Utilisé par le moteur en cas d'**interruption** : on peut reprendre en
    réutilisant le snapshot existant plutôt que d'en créer un nouveau.
    """
    pattern = f"pre-v{schema_to}-"
    if not cfg.SNAPSHOTS_DIR.is_dir():
        return None
    candidates = [d for d in cfg.SNAPSHOTS_DIR.iterdir() if d.is_dir() and d.name.startswith(pattern)]
    if not candidates:
        return None
    # Plus récent d'abord (nom = `pre-v<N>-<date>` → tri lexicographique = chronologique)
    latest = sorted(candidates, key=lambda d: d.name, reverse=True)[0]
    plan_path = latest / "plan.json"
    if not plan_path.is_file():
        return None
    try:
        doc = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(doc, dict):
        return None
    plan = Plan(description=doc.get("description", ""))
    for op_doc in doc.get("operations", []):
        if not isinstance(op_doc, dict):
            continue
        action = op_doc.get("action")
        src_rel = op_doc.get("src")
        dst_rel = op_doc.get("dst")
        if not isinstance(src_rel, str) or action not in ("move", "write", "delete"):
            continue
        src_abs = cfg.work_root / src_rel
        if action == "delete":
            plan.add(Operation(action="delete", src=src_abs))
        else:
            dst_abs = cfg.work_root / dst_rel if dst_rel else src_abs
            plan.add(Operation(action=action, src=src_abs, dst=dst_abs, content=None))
    return Snapshot(dir=latest, plan=plan, schema_from=doc.get("schema_from", 0), schema_to=schema_to)
