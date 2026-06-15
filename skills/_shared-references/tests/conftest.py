"""Rend les modules de `scripts/` importables depuis les tests.

On ajoute aussi `wiki-extract/scripts/` et `wiki-reading-grid/scripts/`
sur le path : certains tests ont besoin d'importer `extractors.web` (pour
la chaîne d'extraction HTML→markdown du palier v2→v3) ou `grid_builder`
(pour le palier v3→v4 sur les grilles de lecture au format guide).
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# `_HERE = skills/_shared-references/tests/` ; `_HERE.parent = skills/_shared-references/`
# ; `_HERE.parent.parent = skills/`.
_SKILLS_ROOT = _HERE.parent.parent
_SHARED_SCRIPTS = _SKILLS_ROOT / "_shared-references" / "scripts"
_WIKI_EXTRACT_SCRIPTS = _SKILLS_ROOT / "wiki-extract" / "scripts"
_WIKI_READING_GRID_SCRIPTS = _SKILLS_ROOT / "wiki-reading-grid" / "scripts"

for p in (_SHARED_SCRIPTS, _WIKI_EXTRACT_SCRIPTS, _WIKI_READING_GRID_SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
