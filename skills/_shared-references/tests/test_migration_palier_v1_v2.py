"""Tests du palier de migration v1→v2 — regrouper les assets PDF/EPUB par source.

Couvre :
- 3.2 Palier v1→v2 : plan liste les déplacements attendus ; apply regroupe ;
      2e passage = no-op.
- 3.3 Validation : count d'assets identique avant/après ; aucun fichier
      hors `work_root`.
- 3.4 Slug avec tirets (`navigation-cotiere`) : découpage correct, pas de
      faux split sur le premier `-`.
- 3.5 Non-régression : les liens des articles `wiki/` (copies propres
      sous `wiki/<sujet>/_assets/`) restent valides après le déplacement
      de `raw/.../_assets/`.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import config_loader
import iohelpers
import vault_meta
from migration import engine, paliers
from migration.engine import RC_OK


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


def _lock_dir(monkeypatch, tmp_path: Path) -> Path:
    from sync import locking
    target = tmp_path / "lock" / "wiki-sync.lock"
    monkeypatch.setattr(locking, "_DEFAULT_LOCK", target)
    return target


def _plant_pdf_source(cfg, base: str) -> None:
    """Plante une source PDF complète : binaire, contenu, toc, sidecar."""
    pdfs = cfg.RAW / "pdfs"
    pdfs.mkdir(parents=True, exist_ok=True)
    (pdfs / f"{base}.pdf").write_bytes(b"%PDF-fake")
    (pdfs / f"{base}.pdf.txt").write_text("Texte.\n", encoding="utf-8")
    toc = {
        "title": base,
        "source": f"raw/pdfs/{base}.pdf",
        "pages": 1,
        "ocr": False,
        "chapters": [],
    }
    iohelpers.write_json(pdfs / f"{base}.toc.json", toc, work_root=cfg.work_root)


def _plant_asset(cfg, base_reste: str) -> Path:
    """Plante un asset non-web à plat (`_assets/<base>-<reste>`).

    Retourne le chemin absolu du fichier créé.
    """
    assets_dir = cfg.RAW / "pdfs" / "_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    p = assets_dir / base_reste
    p.write_bytes(b"PNG")
    return p


@pytest.fixture(autouse=True)
def _lock_in_tmp(tmp_path, monkeypatch):
    _lock_dir(monkeypatch, tmp_path)


# --- 3.2 : palier v1→v2 : plan + apply + idempotence -----------------------

def test_palier_v1_v2_plan_liste_deplacements_attendus(tmp_path):
    """Le palier émet un MOVE par asset à plat qui matche une base connue."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    _plant_pdf_source(cfg, "cncej-livret-accueil-2026")
    _plant_asset(cfg, "cncej-livret-accueil-2026-img-001-000.jpg")
    _plant_asset(cfg, "cncej-livret-accueil-2026-img-002-000.jpg")
    _plant_asset(cfg, "cncej-livret-accueil-2026-img-003-000.png")

    plan = paliers.v1_v2_pdf_assets.PALIER.plan(cfg)
    assert len(plan) == 3
    # Tous les moves pointent vers le sous-répertoire
    for op in plan.operations:
        assert op.action == "move"
        assert op.src.name.startswith("cncej-livret-accueil-2026-")
        assert op.dst.parent.name == "cncej-livret-accueil-2026"


def test_palier_v1_v2_apply_regroupe_les_assets(tmp_path):
    """apply déplace les fichiers dans le sous-répertoire de la source."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    _plant_pdf_source(cfg, "book-a")
    _plant_pdf_source(cfg, "book-b")
    a1 = _plant_asset(cfg, "book-a-img-001.png")
    a2 = _plant_asset(cfg, "book-a-img-002.png")
    b1 = _plant_asset(cfg, "book-b-fig-001.jpg")

    palier = paliers.v1_v2_pdf_assets.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK
    assert res.bumped

    # Sous-répertoires créés
    pdfs_assets = cfg.RAW / "pdfs" / "_assets"
    assert (pdfs_assets / "book-a").is_dir()
    assert (pdfs_assets / "book-b").is_dir()

    # Fichiers déplacés
    assert not a1.exists() and not a2.exists()
    assert (pdfs_assets / "book-a" / "img-001.png").is_file()
    assert (pdfs_assets / "book-a" / "img-002.png").is_file()
    assert (pdfs_assets / "book-b" / "fig-001.jpg").is_file()

    # Schema bumpé à 2
    assert vault_meta.read_vault_schema(cfg) == 2


def test_palier_v1_v2_2e_passage_no_op(tmp_path):
    """Un 2e passage sur un vault déjà à schema=2 : plan vide, no bump."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    _plant_pdf_source(cfg, "book")
    _plant_asset(cfg, "book-img-001.png")

    palier = paliers.v1_v2_pdf_assets.PALIER
    res1 = engine.run_one(cfg, palier)
    assert res1.rc == RC_OK and res1.bumped

    res2 = engine.run_one(cfg, palier)
    assert res2.rc == RC_OK
    assert not res2.applied
    assert not res2.bumped  # schema déjà à 2
    # Le fichier est resté en place (pas re-déplacé)
    assert (cfg.RAW / "pdfs" / "_assets" / "book" / "img-001.png").is_file()


# --- 3.3 : validation : count préservé + confinement -----------------------

def test_palier_v1_v2_count_assets_preserve(tmp_path):
    """Après apply, le nombre total d'assets est identique à avant (déplacement)."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    _plant_pdf_source(cfg, "book-a")
    _plant_pdf_source(cfg, "book-b")
    for i in range(5):
        _plant_asset(cfg, f"book-a-img-{i:03d}.png")
    for i in range(3):
        _plant_asset(cfg, f"book-b-fig-{i:03d}.jpg")

    palier = paliers.v1_v2_pdf_assets.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK

    # Count préservé
    assets_dir = cfg.RAW / "pdfs" / "_assets"
    flat = sum(1 for p in assets_dir.iterdir() if p.is_file())
    in_subs = sum(
        1 for sub in assets_dir.iterdir() if sub.is_dir()
        for p in sub.iterdir() if p.is_file()
    )
    assert flat == 0  # tout a été déplacé
    assert in_subs == 8  # 5 + 3
    # Validation report : assets_before == assets_after
    assert res.validation is not None
    assert res.validation.assets_before == res.validation.assets_after == 8


def test_palier_v1_v2_aucun_fichier_hors_work_root(tmp_path):
    """Aucun fichier ne sort de work_root (confinement préservé)."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    _plant_pdf_source(cfg, "book")
    _plant_asset(cfg, "book-img-001.png")

    palier = paliers.v1_v2_pdf_assets.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK
    assert res.validation.confinement_ok


# --- 3.4 : slug avec tirets : pas de faux split ----------------------------

def test_palier_v1_v2_slug_avec_tirets_decoupage_correct(tmp_path):
    """Le slug `navigation-cotiere` (qui contient un tiret) est reconnu
    comme une base, pas découpé en `navigation-`."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    _plant_pdf_source(cfg, "navigation-cotiere")
    a = _plant_asset(cfg, "navigation-cotiere-p001-img01.png")
    b = _plant_asset(cfg, "navigation-cotiere-p002-img02.jpg")

    palier = paliers.v1_v2_pdf_assets.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK

    assets_dir = cfg.RAW / "pdfs" / "_assets"
    # La base reconnue est `navigation-cotiere`, pas `navigation`
    assert (assets_dir / "navigation-cotiere" / "p001-img01.png").is_file()
    assert (assets_dir / "navigation-cotiere" / "p002-img02.jpg").is_file()
    # Pas de faux sous-répertoire `navigation`
    assert not (assets_dir / "navigation").exists()


def test_palier_v1_v2_longest_match_si_collision(tmp_path):
    """Si on a deux sources `nav` et `nav-pro` (collision sur préfixe),
    le LONGEST match doit gagner : `nav-pro` ne doit pas être rangé sous
    `nav/`."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    _plant_pdf_source(cfg, "nav")
    _plant_pdf_source(cfg, "nav-pro")
    a = _plant_asset(cfg, "nav-img-001.png")
    b = _plant_asset(cfg, "nav-pro-img-001.png")

    palier = paliers.v1_v2_pdf_assets.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK

    assets_dir = cfg.RAW / "pdfs" / "_assets"
    assert (assets_dir / "nav" / "img-001.png").is_file()
    assert (assets_dir / "nav-pro" / "img-001.png").is_file()
    # Pas de collision : `nav-pro-img-001.png` n'est PAS dans `nav/`
    assert not (assets_dir / "nav" / "pro-img-001.png").exists()


# --- 3.5 : non-régression : liens wiki/<sujet>/_assets/ --------------------

def test_palier_v1_v2_liens_wiki_inchanges(tmp_path):
    """Les articles wiki ont leurs PROPRES copies sous `wiki/<sujet>/_assets/`.
    Le palier ne touche pas à `wiki/`, donc leurs liens restent valides."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    cfg.WIKI.mkdir(parents=True)
    _plant_pdf_source(cfg, "book")
    _plant_asset(cfg, "book-img-001.png")

    # Article wiki avec une référence à son propre asset (sous wiki/<sujet>/_assets/)
    wiki_assets = cfg.WIKI / "nav" / "_assets"
    wiki_assets.mkdir(parents=True)
    wiki_img = wiki_assets / "book-img-001.png"
    wiki_img.write_bytes(b"COPY")
    article = cfg.WIKI / "nav" / "releve.md"
    article.parent.mkdir(parents=True, exist_ok=True)
    article.write_text(
        "---\ntitle: r\n---\n\n"
        "Voir ![image](_assets/book-img-001.png).\n",
        encoding="utf-8",
    )

    palier = paliers.v1_v2_pdf_assets.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK

    # L'article wiki est INTACT (le palier n'a touché que raw/.../_assets/)
    text = article.read_text(encoding="utf-8")
    assert "_assets/book-img-001.png" in text
    # L'image wiki existe toujours (copie propre, indépendante de raw/)
    assert wiki_img.is_file()
    assert wiki_img.read_bytes() == b"COPY"


# --- edge cases ----------------------------------------------------------

def test_palier_v1_v2_ignore_fichiers_sans_base_connue(tmp_path):
    """Un asset à plat dont le préfixe ne correspond à AUCUNE source connue
    est ignoré (le palier n'invente rien)."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    _plant_pdf_source(cfg, "book-a")
    # Asset orphelin (préfixe `unknown-` ne matche aucune base)
    _plant_asset(cfg, "unknown-img-001.png")
    # Asset d'une source connue
    _plant_asset(cfg, "book-a-img-001.png")

    palier = paliers.v1_v2_pdf_assets.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK

    assets_dir = cfg.RAW / "pdfs" / "_assets"
    # Le fichier `book-a-img-001.png` a été déplacé
    assert (assets_dir / "book-a" / "img-001.png").is_file()
    # Le fichier `unknown-img-001.png` est resté à plat (ignoré)
    assert (assets_dir / "unknown-img-001.png").is_file()


def test_palier_v1_v2_dry_run(tmp_path):
    """`dry_run=True` : plan listé, aucun fichier modifié."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    _plant_pdf_source(cfg, "book")
    a = _plant_asset(cfg, "book-img-001.png")

    palier = paliers.v1_v2_pdf_assets.PALIER
    res = engine.run_one(cfg, palier, dry_run=True)
    assert res.rc == RC_OK
    assert not res.applied
    assert not res.bumped
    assert a.exists()  # INTACT
    assert not (cfg.RAW / "pdfs" / "_assets" / "book").exists()


def test_palier_v1_v2_resolve_base_longest_match():
    """`_resolve_base` est testé en isolation : longest match, pas de split sur
    le premier tiret."""
    from migration.paliers.v1_v2_pdf_assets import _resolve_base
    known = {"navigation", "navigation-cotiere", "book"}

    # Longest match
    assert _resolve_base("navigation-cotiere-p001.png", known) == (
        "navigation-cotiere", "p001.png"
    )
    # Pas de base connue → None
    assert _resolve_base("unknown-img-001.png", known) is None
    # Pas de tiret → None
    assert _resolve_base("book.png", known) is None
    # Cas dégénéré `book-.png` : la base matche et le reste est `.png`
    # (techniquement valide — c'est le job du caller de filtrer).
    assert _resolve_base("book-.png", known) == ("book", ".png")


def test_palier_v1_v2_concernes_pdf_et_epub_pas_web(tmp_path):
    """Le palier ne touche qu'aux types `pdfs` et `epubs`, jamais `web/`
    (qui est le périmètre d'un autre change)."""
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)

    # Source PDF avec asset à plat
    _plant_pdf_source(cfg, "book-pdf")
    pdf_asset = cfg.RAW / "pdfs" / "_assets"
    pdf_asset.mkdir(parents=True, exist_ok=True)
    (pdf_asset / "book-pdf-img-001.png").write_bytes(b"PNG")

    # Source EPUB avec asset à plat
    epubs = cfg.RAW / "epubs"
    epubs.mkdir(parents=True, exist_ok=True)
    (epubs / "book-epub.epub").write_bytes(b"EPUB")
    (epubs / "book-epub.epub.txt").write_text("Texte.\n", encoding="utf-8")
    iohelpers.write_json(
        epubs / "book-epub.toc.json",
        {"title": "book-epub", "pages": 1, "ocr": False, "chapters": []},
        work_root=cfg.work_root,
    )
    epub_assets = epubs / "_assets"
    epub_assets.mkdir(parents=True, exist_ok=True)
    (epub_assets / "book-epub-fig-001.jpg").write_bytes(b"JPG")

    # Web à plat (préfixe `<base>-<date>-`)
    web = cfg.RAW / "web"
    web.mkdir(parents=True, exist_ok=True)
    web_assets = web / "_assets"
    web_assets.mkdir(parents=True, exist_ok=True)
    (web_assets / "ma-page-20260615-img.png").write_bytes(b"WEB-PNG")

    palier = paliers.v1_v2_pdf_assets.PALIER
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK

    # PDF et EPUB : regroupés
    assert (cfg.RAW / "pdfs" / "_assets" / "book-pdf" / "img-001.png").is_file()
    assert (cfg.RAW / "epubs" / "_assets" / "book-epub" / "fig-001.jpg").is_file()
    # Web : INTACT (palier v2→v3 ailleurs)
    assert (web_assets / "ma-page-20260615-img.png").is_file()
    assert not (web_assets / "ma-page-20260615").exists()  # pas de sous-répertoire créé
