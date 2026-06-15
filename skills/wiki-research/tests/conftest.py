"""Rend importables les scripts de `wiki-research/` ET du socle partagé.

Cinq dossiers sur `sys.path` (imports plats, cohérent avec le socle Phase 0) :
- `skills/wiki-research/scripts/`         -> wiki_research (orchestrateur + helpers)
- `skills/wiki-extract/scripts/`          -> wiki_extract (clip web délégué)
- `skills/wiki-index/scripts/`            -> index_builder, link_audit (intégration §5)
- `skills/wiki-reading-grid/scripts/`     -> grid_builder (palier v3→v4 référencé par
                                             bootstrap→migration)
- `skills/_shared-references/scripts/`    -> config_loader, guard, slug, iohelpers,
                                             frontmatter, wikilinks, migration
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_RESEARCH_SCRIPTS = _HERE.parent.parent / "scripts"
_EXTRACT_SCRIPTS = _HERE.parents[2] / "wiki-extract" / "scripts"
_INDEX_SCRIPTS = _HERE.parents[2] / "wiki-index" / "scripts"
_READING_GRID_SCRIPTS = _HERE.parents[2] / "wiki-reading-grid" / "scripts"
_SHARED_SCRIPTS = _HERE.parents[2] / "_shared-references" / "scripts"

for p in (_RESEARCH_SCRIPTS, _EXTRACT_SCRIPTS, _INDEX_SCRIPTS, _READING_GRID_SCRIPTS, _SHARED_SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
