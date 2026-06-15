#!/usr/bin/env python3
"""Bootstrap unifié du socle partagé Mimir (spec `mimir-invariants`).

Chaque orchestrateur `wiki-*` dupliquait ~10 lignes de `sys.path.insert` vers
`_shared-references/scripts/`, avec ses propres bugs silencieux : un symbole
du socle oublié (ex. `guard.safe_path`) plantait **en cours de run** par
`NameError` au lieu de planter **au chargement** par `ImportError`.

Ce module centralise :

1. **L'insertion idempotente** du dossier `scripts/` du socle en tête de
   `sys.path` — en honorant l'override `MIMIR_SHARED` (priorité sur le chemin
   relatif bundlé). Idempotent : ré-appel = no-op.
2. **La ré-exportation** des modules et symboles du socle (cf. spec). Tout
   `from bootstrap import <symbole>` qui rate lève `ImportError` **au moment
   de l'import** de ce module, donc **avant** le moindre travail — fail fast.

Les orchestrateurs remplacent leur ancien bloc dupliqué par :

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared-references" / "scripts"))
    import bootstrap  # noqa: E402

L'insertion de la ligne `sys.path` reste nécessaire pour que Python trouve
`bootstrap` lui-même (le bootstrap n'est pas dans un répertoire standard).
Une fois `bootstrap` importé, c'est lui qui prend la main sur la politique
`MIMIR_SHARED` + ré-exports.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Dossier du socle (`.../_shared-references/scripts/`). Auto-localisé via
#: `__file__` : pas de calcul de `parents[2]` fragile à déplacer.
_SCRIPTS = Path(__file__).resolve().parent


def _ensure_socle_path() -> None:
    """Insère `MIMIR_SHARED` (si défini et valide) en tête de `sys.path`,
    puis le dossier bundlé du socle. **Idempotent** : un chemin déjà présent
    n'est pas ré-inséré (sinon `sys.path` enfle et l'ordre devient instable).

    Tous les chemins sont normalisés via `Path.resolve()` AVANT comparaison,
    sinon macOS (`/var` ↔ `/private/var` symlink) peut laisser coexister
    la forme « resolved » et la forme « unresolved » d'un même répertoire —
    la seconde prendrait alors la position 0 en silence, invalidant
    l'override `MIMIR_SHARED`.

    Ordre de priorité (premier match gagne) :
      1. `os.environ['MIMIR_SHARED']` (override explicite, ex. déploiement).
      2. `_SCRIPTS` (chemin relatif bundlé — cas nominal des tests/dev).
    """
    candidates: list[str] = []
    env = os.environ.get("MIMIR_SHARED")
    if env:
        env_resolved = Path(env).resolve()
        if env_resolved.is_dir():
            candidates.append(str(env_resolved))
    candidates.append(str(_SCRIPTS))
    seen: set[str] = {Path(p).resolve().as_posix() for p in sys.path if p}
    for cand in candidates:
        key = Path(cand).resolve().as_posix()
        if key not in seen:
            sys.path.insert(0, str(cand))
            seen.add(key)


_ensure_socle_path()


# --- ré-exports du socle (spec `mimir-invariants` §Unified socle bootstrap) --
# Modules
import config_loader  # noqa: E402
import frontmatter  # noqa: E402
import iohelpers  # noqa: E402
import ledger  # noqa: E402
import migration  # noqa: E402
import slug  # noqa: E402
import vault_meta  # noqa: E402

# Symboles de `guard` (re-exportés en flat pour `from bootstrap import safe_path`).
# Tout `from X import Y` raté = ImportError immédiat, pas de NameError en cours.
from guard import (  # noqa: E402,F401
    ConfinementError,
    assert_within,
    is_within,
    safe_path,
)


__all__ = [
    # modules
    "config_loader",
    "frontmatter",
    "iohelpers",
    "ledger",
    "migration",
    "slug",
    "vault_meta",
    # symboles guard
    "ConfinementError",
    "assert_within",
    "is_within",
    "safe_path",
]
