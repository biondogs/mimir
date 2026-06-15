#!/usr/bin/env python3
"""Palier de migration **v3→v4** — grilles de lecture au format guide.

Change `rework-reading-grid-as-guide` (palier versionné). Pour chaque
grille PDF/EPUB existante (i.e. liée à un `<base>.toc.json`), régénère la
grille au format **hybride guide** (zones blurb `<!-- blurb:… -->` +
charpente déterministe), en **préservant** les phrases éditoriales déjà
rédigées (extraction/réinjection par appariement sur l'`order` du
`toc.json`).

**Best-effort sur les phrases** : la rédaction des phrases `_à rédiger_`
reste à la charge de l'agent (par lots supervisés), via la sortie
`chapters_missing_blurb` du générateur. Le palier ne fabrique aucune
prose (l'audit est sémantique, pas scriptable).

**Reprise** : chaque grille migrée reçoit `schema_version: 4`. Une
campagne interrompue peut reprendre sur les seules grilles restées en
deçà. La validation porte sur la **structure** (0 lien cassé, ancres
cohérentes, confinement) — **PAS** la qualité des phrases (revue humaine).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import frontmatter
import grid_builder
import iohelpers
import vault_meta

from migration import plan, registry


#: Status des grilles éligibles à la migration v3→v4 (PDF/EPUB chapitrés).
_TYPES = ("pdfs", "epubs")


def _grids_with_toc(cfg) -> list[Path]:
    """Chemins des grilles `reading-grids/*.md` liées à un `toc.json` voisin.

    Filtre : on ne s'attaque qu'aux grilles dont la `source:` est un chemin
    `raw/<type>/<base>.<ext>` avec un `<base>.toc.json` voisin (= ouvrage
    chapitré). Les grilles web (`skipped: no-toc`) sont laissées intactes
    (la branche web best-effort n'est pas enrichie par ce palier).
    """
    out: list[Path] = []
    grids_dir = cfg.READING_GRIDS
    if not grids_dir.is_dir():
        return out
    for p in sorted(grids_dir.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            front, _ = frontmatter.parse_frontmatter(text)
        except Exception:
            continue
        source = str(front.get("source") or "")
        if not source:
            continue
        # `source: raw/<type>/<base>.<content_ext>` → on cherche `<base>.toc.json`
        src_path = cfg.work_root / source
        base = grid_builder._base_of(src_path.name)
        doc_type = src_path.parts[-2] if len(src_path.parts) >= 2 else ""
        if doc_type not in _TYPES:
            continue
        toc = cfg.RAW / doc_type / f"{base}.toc.json"
        if not toc.is_file():
            continue
        out.append(p)
    return out


def _grid_already_at_schema_4(grid_path: Path) -> bool:
    """Vrai si la grille porte `schema_version: 4` (déjà migrée, on skip)."""
    try:
        text = grid_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    try:
        front, _ = frontmatter.parse_frontmatter(text)
    except Exception:
        return False
    v = frontmatter.read_schema_version(front)
    return v is not None and v >= 4


def _build_plan(cfg) -> plan.Plan:
    """Plan dry-run : un `WRITE` par grille à régénérer (intent only)."""
    p = plan.Plan(description="régénère les grilles PDF/EPUB au format guide (v3→v4)")
    for grid_path in _grids_with_toc(cfg):
        if _grid_already_at_schema_4(grid_path):
            continue
        # Le plan n'a pas vocation à « effectuer » la régénération : on
        # marque juste l'intention. L'apply custom fait le vrai travail
        # (régénération via grid_builder + écriture + bump schema_version).
        p.add(plan.WRITE(grid_path, content="<regenerate-pending>"))
    return p


def _apply(cfg, p: plan.Plan) -> None:
    """Régénère chaque grille au format guide + bump `schema_version: 4`.

    Réutilise `wiki_reading_grid.cmd_generate` (sans pull/push puisque le
    palier tourne sous le verrou de synchro du moteur). Chaque grille
    migrée reçoit un champ `schema_version: 4` au frontmatter (cf.
    `migration.lazy.mark_migrated`).
    """
    from migration import lazy

    grids_to_process = [
        op.dst for op in p.operations
        if op.action == "write"
    ]
    if not grids_to_process:
        return

    for grid_path in grids_to_process:
        # Reconstruit la `content_rel` à partir du frontmatter de la grille
        # (chemin `raw/<type>/<base>.<ext>`).
        try:
            text = grid_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            front, _ = frontmatter.parse_frontmatter(text)
        except Exception:
            continue
        source = str(front.get("source") or "")
        if not source:
            continue
        # Appelle `grid_builder.build_grid(..., existing_grid_path=grid_path)`
        # — c'est lui qui fait l'extraction/réinjection des phrases (cf. le
        # changement `rework-reading-grid-as-guide`, tasks 2.x).
        result = grid_builder.build_grid(cfg, source, existing_grid_path=grid_path)
        if result.skipped:
            continue
        # Construit le document (frontmatter + corps), en POSANT
        # `schema_version: 4` à la place de l'éventuel ancien champ.
        today = date.today().isoformat()
        new_text = grid_builder.document(result, today)
        new_text = _set_schema_version_in_text(new_text, 4)
        iohelpers.atomic_write_text(grid_path, new_text, work_root=cfg.work_root)


def _set_schema_version_in_text(text: str, target: int) -> str:
    """Pose/met à jour `schema_version: <target>` dans le frontmatter YAML.

    Variante locale de `migration.lazy.mark_migrated` qui ne touche QUE
    `schema_version` (sans ajouter d'autres champs), pour rester
    minimaliste sur les grilles régénérées.
    """
    from migration.lazy import _set_schema_version_in_text as _lazy_set
    return _lazy_set(text, target)


#: Le palier de migration v3→v4 — exporté pour les tests et l'enregistrement.
#: Note : `preserves_counts=False` parce que la régénération peut modifier
#: le **contenu** des grilles (ajout de zones blurb, recalcul de charpente).
#: C'est un palier « transformateur » — la validation ne s'applique pas à
#: l'intégrité byte-pour-byte, seulement à la structure (0 lien cassé,
#: ancres cohérentes, confinement).
PALIER = registry.Migration(
    from_schema=3, to_schema=4,
    plan=_build_plan, apply=_apply,
    description="grilles PDF/EPUB au format guide (charpente + zones blurb préservées)",
    preserves_counts=False,
)


def register() -> registry.Migration:
    """Enregistre le palier v3→v4 dans le registre."""
    return registry.register(PALIER)
