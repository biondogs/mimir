#!/usr/bin/env python3
"""Helpers de **version de structure** du vault (capability `vault-migration`).

Le marqueur vit dans `.mimir/vault.json` **dans la zone synchronisée** (la version
voyage avec le contenu, contrairement au ledger `.wiki/` qui est machine-local).
Forme du fichier :

    {
        "schema": <entier>,
        "migrated": "AAAA-MM-JJ",
        "history": [ {"to": <entier>, "at": "AAAA-MM-JJ"}, ... ]
    }

Règles de lecture (cf. spec `vault-migration` — *Version de structure persistée*) :

- **Absent** → baseline `1` (jamais une erreur fatale, jamais une migration silencieuse).
- **Corrompu** (JSON invalide, forme inattendue, `schema` non entier) → baseline `1`,
  pour ne **pas** bloquer un statut / un `status`. La corruption ne se **réécrit**
  pas automatiquement : seul un `bump` réussi la remplace.

Règle d'écriture : **atomique** (tmp + `os.replace` via `iohelpers.write_json`),
confinée à `work_root` (via `guard.assert_within`). Une interruption en plein
écriture laisse l'**ancienne** version intacte (le `replace` est atomique POSIX/APFS).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import guard
import iohelpers

#: Schéma par défaut (baseline) — un vault qui n'a jamais été migré est implicitement
#: à la version 1. C'est aussi la valeur posée par `wiki-init apply` sur un vault
#: peuplé sans `.mimir/vault.json` (sans migration rétroactive).
BASELINE_SCHEMA = 1


def _coerce_int(value: object) -> int | None:
    """Convertit `value` en `int` positif, ou `None` si la forme est invalide."""
    if isinstance(value, bool):  # `bool` est un sous-type de `int` — on l'exclut
        return None
    if isinstance(value, int) and value >= 1:
        return value
    return None


def _coerce_history(value: object) -> list[dict[str, Any]]:
    """Filtre l'historique pour ne garder que les entrées bien formées (silencieux)."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        to = _coerce_int(item.get("to"))
        at = item.get("at")
        if to is None or not isinstance(at, str) or not at:
            continue
        out.append({"to": to, "at": at})
    return out


def _coerce_migrated(value: object) -> str:
    """Date ISO de la dernière migration (ou chaîne vide si absente)."""
    return value if isinstance(value, str) else ""


def read_vault_schema(cfg) -> int:
    """Lit `.mimir/vault.json` et renvoie la version de structure.

    Comportement (cf. spec) :
    - Fichier **absent** → `1` (baseline) ; aucune écriture.
    - Fichier **corrompu** (JSON invalide, forme inattendue, `schema` non-entier) → `1`,
      pour ne pas bloquer un diagnostic. La corruption n'est **pas** réécrite
      automatiquement : seul un `bump_vault_schema` explicite la remplace.
    - Fichier **valide** → valeur de `schema` (entier ≥ 1).

    Le paramètre `cfg` peut être un objet `Config` du socle (utilise
    `cfg.VAULT_META`) ou tout objet exposant l'attribut — utile pour les tests
    qui passent un mock.
    """
    path = cfg.VAULT_META
    if not Path(path).is_file():
        return BASELINE_SCHEMA
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return BASELINE_SCHEMA
    if not isinstance(raw, dict):
        return BASELINE_SCHEMA
    schema = _coerce_int(raw.get("schema"))
    return schema if schema is not None else BASELINE_SCHEMA


def _read_full(cfg) -> dict[str, Any]:
    """Lit le fichier complet (avec `migrated` + `history`), forme validée.

    Fichier absent OU corrompu → `{"schema": 1, "migrated": "", "history": []}`.
    Utilisé en interne par `bump_vault_schema` pour **préserver** l'historique
    lors d'un bump.
    """
    path = cfg.VAULT_META
    if not Path(path).is_file():
        return {"schema": BASELINE_SCHEMA, "migrated": "", "history": []}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {"schema": BASELINE_SCHEMA, "migrated": "", "history": []}
    if not isinstance(raw, dict):
        return {"schema": BASELINE_SCHEMA, "migrated": "", "history": []}
    schema = _coerce_int(raw.get("schema"))
    return {
        "schema": schema if schema is not None else BASELINE_SCHEMA,
        "migrated": _coerce_migrated(raw.get("migrated")),
        "history": _coerce_history(raw.get("history")),
    }


def bump_vault_schema(cfg, to: int, *, at: str | None = None) -> Path:
    """Bumpe la version de structure vers `to` (atomique, confiné, journalisé).

    - Écriture **atomique** (`iohelpers.write_json` → tmp + `os.replace`) :
      une interruption en plein écriture laisse l'ancienne version intacte.
    - **Confinement** : `guard.assert_within(work_root, path)` avant toute écriture.
    - **Journal** : ajoute `{to, at}` à `history` (sans déduplication — c'est un log).
    - `at` par défaut = date du jour (`date.today().isoformat()`).
    - `to` doit être un entier ≥ 1 (sinon `ValueError`). On **n'impose pas** que
      `to` soit strictement croissant : un palier peut avoir besoin de re-bumper
      (correction d'un crash antérieur) — c'est l'`engine` qui décide.
    """
    if not isinstance(to, int) or isinstance(to, bool) or to < 1:
        raise ValueError(f"version de structure invalide : {to!r} (attendu : int ≥ 1)")

    target = guard.assert_within(cfg.work_root, cfg.VAULT_META)
    current = _read_full(cfg)
    history = current["history"]
    at_iso = at if isinstance(at, str) and at else date.today().isoformat()
    new_doc = {
        "schema": to,
        "migrated": at_iso,
        "history": history + [{"to": to, "at": at_iso}],
    }
    return iohelpers.write_json(target, new_doc, work_root=cfg.work_root)
