"""Rend importables les scripts de `wiki-batch-produce/` ET du socle partagé.

Trois dossiers sur `sys.path` (imports plats, cohérent avec le socle Phase 0) :
- `skills/wiki-batch-produce/scripts/`  -> batch_produce
- `skills/wiki-ingest/scripts/`         -> wiki_ingest (cmd_finalize importé par batch_produce)
- `skills/wiki-index/scripts/`          -> link_audit, wiki_index (audit best-effort)
- `skills/_shared-references/scripts/`  -> config_loader, guard, slug, iohelpers, status_table, ledger, frontmatter, sync
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_BATCH_PRODUCE_SCRIPTS = _HERE.parent.parent / "scripts"
_INGEST_SCRIPTS = _HERE.parents[2] / "wiki-ingest" / "scripts"
_INDEX_SCRIPTS = _HERE.parents[2] / "wiki-index" / "scripts"
_SHARED_SCRIPTS = _HERE.parents[2] / "_shared-references" / "scripts"

for p in (_BATCH_PRODUCE_SCRIPTS, _INGEST_SCRIPTS, _INDEX_SCRIPTS, _SHARED_SCRIPTS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
