"""Tests du palier de migration v3→v4 — grilles de lecture au format guide.

Couvre :
- 6.6 Palier v3→v4 : charpente régénérée, phrases préexistantes
      réinjectées, validation structure + 0 lien cassé.
- Reprise (5.4) : grilles en `schema_version < 4` uniquement.
- (bonus) Caractère « best-effort » du palier : il NE FABRIQUE PAS de prose
  (les phrases manquantes restent `_à rédiger_`).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import grid_builder
import iohelpers
import vault_meta
from migration import engine, paliers
from migration.engine import RC_OK, RC_VALIDATION_FAILED


# --- helpers --------------------------------------------------------------

class _Cfg:
    """Mock minimal de `Config` — n'expose que ce que le palier consomme."""

    def __init__(self, work_root: Path):
        self.work_root = work_root
        self.INBOX = work_root / "_inbox"
        self.RAW = work_root / "raw"
        self.WIKI = work_root / "wiki"
        self.READING_GRIDS = work_root / "reading-grids"
        self.LEDGER = work_root / ".wiki" / "ingest-ledger.json"
        self.VAULT_META = work_root / ".mimir" / "vault.json"
        self.VAULT_META_DIR = work_root / ".mimir"
        self.SNAPSHOTS_DIR = work_root / ".mimir" / "snapshots"
        self.ASSETS_DIRNAME = "_assets"
        self.sync = {"backend": "rclone"}
        self.config_path = work_root / "wiki.config.json"
        self.mode = "B"
        self.backend = "rclone"
        self.layout = {
            "raw": "raw",
            "wiki": "wiki",
            "reading_grids": "reading-grids",
            "ledger": ".wiki/ingest-ledger.json",
        }


def _lock_dir(monkeypatch, tmp_path: Path) -> Path:
    from sync import locking
    target = tmp_path / "lock" / "wiki-sync.lock"
    monkeypatch.setattr(locking, "_DEFAULT_LOCK", target)
    return target


def _plant_pdf_source(cfg, base: str) -> None:
    """Plante un PDF complet : binaire, contenu, toc, sidecar."""
    pdfs = cfg.RAW / "pdfs"
    pdfs.mkdir(parents=True, exist_ok=True)
    (pdfs / f"{base}.pdf").write_bytes(b"%PDF-fake")
    (pdfs / f"{base}.pdf.txt").write_text("Contenu.\n", encoding="utf-8")
    toc = {
        "title": f"Test {base}",
        "source": f"raw/pdfs/{base}.pdf",
        "pages": 1,
        "ocr": False,
        "chapters": [
            {"order": 1, "title": "Intro", "page_start": 1, "page_end": 10},
            {"order": 2, "title": "Suite", "page_start": 11, "page_end": 20},
        ],
    }
    iohelpers.write_json(pdfs / f"{base}.toc.json", toc, work_root=cfg.work_root)


def _plant_existing_grid(cfg, base: str, *, with_blurb: bool = False, blurb_text: str = "") -> Path:
    """Plante une grille « ancienne » (v3 : sans zone blurb, ou avec une
    phrase préservée si `with_blurb=True`)."""
    grid_path = cfg.READING_GRIDS / f"{base}.md"
    grid_path.parent.mkdir(parents=True, exist_ok=True)
    body_lines = [
        f"---\n",
        f"type: reading-grid\n",
        f'work: "Test {base}"\n',
        f"source: raw/pdfs/{base}.pdf.txt\n",
        f"toc: raw/pdfs/{base}.toc.json\n",
        f"chapters: 2\n",
        f"created: 2026-06-04\n",
        f"---\n\n",
        f"# Grille de lecture — Test {base}\n",
        f"\n",
    ]
    if with_blurb:
        body_lines += [
            "## Ch. 2 — Suite\n",
            "\n",
            f"<!-- blurb:début -->\n{blurb_text}\n<!-- blurb:fin -->\n",
            "\n",
            "Lire dans l'ordre :\n",
            "\n",
            "1. [[test/notion1]]\n",
        ]
    else:
        body_lines += [
            "## Ch. 1 — Intro\n",
            "\n",
            "Lire dans l'ordre :\n",
            "\n",
            "1. [[test/notion1]]\n",
        ]
    grid_path.write_text("".join(body_lines), encoding="utf-8")
    return grid_path


@pytest.fixture(autouse=True)
def _lock_in_tmp(tmp_path, monkeypatch):
    _lock_dir(monkeypatch, tmp_path)


# --- 6.6 : palier v3→v4 ------------------------------------------------------

def test_palier_v3_v4_charpente_regeneree_phrases_preservees(tmp_path):
    """Le palier régénère la charpente au format guide, réinjecte les
    phrases préexistantes, et pose `schema_version: 4`."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    cfg.WIKI.mkdir(parents=True)
    cfg.READING_GRIDS.mkdir(parents=True)

    # Source PDF + toc
    _plant_pdf_source(cfg, "book")
    # Grille v3 avec une phrase éditoriale pour ch2
    _plant_existing_grid(
        cfg, "book", with_blurb=True, blurb_text="Ma phrase ch2."
    )

    palier = paliers.v3_v4_reading_grid_guide.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK
    assert res.bumped

    # Charpente régénérée (format guide)
    grid_path = cfg.READING_GRIDS / "book.md"
    text = grid_path.read_text(encoding="utf-8")
    assert "<!-- blurb:début -->" in text
    assert "<!-- blurb:fin -->" in text
    # Phrase de ch2 préservée
    assert "Ma phrase ch2." in text
    # Schema bumpé à 4
    assert "schema_version: 4" in text
    # Schema fichier cohérent
    assert vault_meta.read_vault_schema(cfg) == 4
    # Validation OK (structure + 0 lien cassé)
    assert res.validation is not None
    assert res.validation.broken_links == 0
    assert res.validation.confinement_ok


def test_palier_v3_v4_ne_fabrique_pas_de_prose(tmp_path):
    """Best-effort : si la grille n'a PAS de phrase pour un chapitre,
    le palier la marque `_à rédiger_` (placeholder) — il NE FABRIQUE PAS
    de prose (audit sémantique, pas scriptable)."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    cfg.WIKI.mkdir(parents=True)
    cfg.READING_GRIDS.mkdir(parents=True)
    _plant_pdf_source(cfg, "book")
    # Grille v3 SANS blurb
    _plant_existing_grid(cfg, "book", with_blurb=False)

    palier = paliers.v3_v4_reading_grid_guide.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK

    text = (cfg.READING_GRIDS / "book.md").read_text(encoding="utf-8")
    # Placeholder présent (l'agent le rédigera par lots supervisés)
    assert "_à rédiger_" in text
    # Pas de fabrication de prose (le palier n'écrit rien d'autre)
    # (on vérifie qu'il n'y a pas de phrase « plausible » inventée)
    assert "Ma phrase" not in text


# --- 5.4 : reprise par schema_version -------------------------------------

def test_palier_v3_v4_reprise_skip_si_deja_a_schema_4(tmp_path):
    """Une grille déjà migrée (schema_version: 4) est IGNORÉE au plan
    (reprise après interruption)."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    cfg.WIKI.mkdir(parents=True)
    cfg.READING_GRIDS.mkdir(parents=True)
    _plant_pdf_source(cfg, "book")
    # Grille v3 avec schema_version: 4 (déjà migrée)
    grid_path = _plant_existing_grid(cfg, "book", with_blurb=False)
    text = grid_path.read_text(encoding="utf-8")
    text = text.replace("created: 2026-06-04\n", "created: 2026-06-04\nschema_version: 4\n")
    grid_path.write_text(text, encoding="utf-8")

    plan = paliers.v3_v4_reading_grid_guide.PALIER.plan(cfg)
    # Pas de candidat (déjà à schema=4)
    assert len(plan) == 0


def test_palier_v3_v4_reprise_traite_grille_sans_schema_version(tmp_path):
    """Une grille sans `schema_version` (= « non migrée ») est candidate
    à la migration v3→v4."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    cfg.WIKI.mkdir(parents=True)
    cfg.READING_GRIDS.mkdir(parents=True)
    _plant_pdf_source(cfg, "book")
    _plant_existing_grid(cfg, "book", with_blurb=False)  # pas de schema_version

    plan = paliers.v3_v4_reading_grid_guide.PALIER.plan(cfg)
    assert len(plan) == 1


# --- 5.2 : plan liste les grilles PDF/EPUB liées à un toc.json -----------

def test_palier_v3_v4_plan_liste_grilles_pdf_et_epub_uniquement(tmp_path):
    """Le palier liste UNIQUEMENT les grilles PDF/EPUB (liées à un toc.json).
    Les grilles web (best-effort, sans toc) sont laissées intactes."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    cfg.WIKI.mkdir(parents=True)
    cfg.READING_GRIDS.mkdir(parents=True)

    # Source PDF avec toc → grille éligible
    _plant_pdf_source(cfg, "pdf-book")
    _plant_existing_grid(cfg, "pdf-book", with_blurb=False)

    # Source web (sans toc) → grille NON éligible
    web = cfg.RAW / "web"
    web.mkdir(parents=True)
    (web / "web-article.md").write_text("# Web\n\nContenu.\n", encoding="utf-8")
    web_grid = cfg.READING_GRIDS / "web-grid.md"
    web_grid.write_text(
        "---\n"
        "type: reading-grid\n"
        'work: "Web Article"\n'
        "source: raw/web/web-article.md\n"
        "chapters: 0\n"
        "created: 2026-06-04\n"
        "---\n\n"
        "# Grille de lecture — Web Article\n"
        "## Lectures\n"
        "1. [[x/y]]\n",
        encoding="utf-8",
    )

    plan = paliers.v3_v4_reading_grid_guide.PALIER.plan(cfg)
    # Une seule candidate (le PDF)
    assert len(plan) == 1
    assert plan.operations[0].dst.name == "pdf-book.md"
