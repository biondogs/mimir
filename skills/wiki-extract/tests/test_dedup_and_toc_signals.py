"""Dédup URL des clippings web + signal TOC-rebuild des PDF sans chapitrage.

Couvre l'OpenSpec `add-extract-dedup-and-toc-signals` :
- 1.3 2e clipping même URL → skip ; URL différente → pas skip ; `source:` quotée gérée.
- 2.4 PDF sans TOC → marqueur présent ; PDF born-digital avec TOC → marqueur absent.
- 3.2 Non-régression dédup SHA (PDF/EPUB inchangés).
"""

import json
from pathlib import Path

import pytest

import config_loader
import wiki_extract
from extractors.base import Chapter, ExtractResult
from sync import locking as inbox_lock


@pytest.fixture(autouse=True)
def _lock_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_lock, "_DEFAULT_LOCK", tmp_path / "lock" / "wiki-sync.lock")


@pytest.fixture
def cfg(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "_inbox").mkdir()
    cfgp = tmp_path / "wiki.config.json"
    cfgp.write_text(json.dumps({"work_root": str(work)}), encoding="utf-8")
    return config_loader.load_config(cfgp)


def _install_web_fake(monkeypatch, *, content, url, assets=None, title="Titre Web"):
    """Stub l'extracteur web : renvoie un contenu + l'URL passée en metadata."""
    class Fake:
        SUPPORTED_EXTS = set()

        @staticmethod
        def extract(source, *, lang="fra+eng"):
            return ExtractResult(
                raw_content=content,
                content_ext="md",
                metadata={"title": title, "source": url, "type": "web",
                          "created": "2026-06-08"},
                structure=[],
                assets=assets or [],
                doc_type="web",
            )

    monkeypatch.setattr(wiki_extract.extractors, "get_extractor", lambda src: Fake)


def _install_pdf_fake(monkeypatch, *, content="Texte.\n", structure=None):
    """Stub l'extracteur PDF avec un chapitrage paramétrable (None = pas de TOC)."""
    struct = structure if structure is not None else [Chapter(1, "Ch1", 1, 3)]

    class Fake:
        SUPPORTED_EXTS = set()

        @staticmethod
        def extract(source, *, lang="fra+eng"):
            return ExtractResult(
                raw_content=content,
                content_ext="pdf.txt",
                metadata={"title": "Doc", "pages": 3, "ocr": False, "source": str(source)},
                structure=struct,
                assets=[],
                doc_type="pdfs",
            )

    monkeypatch.setattr(wiki_extract.extractors, "get_extractor", lambda src: Fake)


def _drop(cfg, name, data=b"%PDF-binaire"):
    p = cfg.INBOX / name
    p.write_bytes(data)
    return p


def _web_clippings(cfg) -> list[Path]:
    """Liste les **clippings** web (`.md` de contenu) sous `raw/web/`, hors `_status.md`."""
    web = cfg.RAW / "web"
    if not web.is_dir():
        return []
    return [p for p in web.glob("*.md") if p.name != "_status.md"]


# --- 1.3a : 2e clipping MÊME URL → skipped_duplicate -------------------------

def test_url_dedup_skip_si_meme_url(cfg, monkeypatch):
    """Premier clip ingère ; second clip même URL → skipped_duplicate, rien n'est ré-écrit."""
    url = "https://example.org/article"
    _install_web_fake(monkeypatch, content="A", url=url)
    wiki_extract.run(cfg, url, lang="x", dry_run=False, skip_sync=True)
    assert len(_web_clippings(cfg)) == 1

    # 2e appel, contenu différent (SHA différent) mais même URL
    _install_web_fake(monkeypatch, content="B — variation Obsidian Web Clipper", url=url)
    out = wiki_extract.run(cfg, url, lang="x", dry_run=False, skip_sync=True)
    assert out[0].status == "skipped_duplicate"
    assert "URL déjà ingérée" in out[0].message
    # Pas de second .md écrit
    assert len(_web_clippings(cfg)) == 1


# --- 1.3b : URL différente → pas de skip ------------------------------------

def test_url_dedup_pas_de_skip_si_url_differente(cfg, monkeypatch):
    """Deux URLs distinctes → deux clippings distincts (la dédup SHA ne s'y oppose pas)."""
    _install_web_fake(monkeypatch, content="A", url="https://a.org/x")
    wiki_extract.run(cfg, "https://a.org/x", lang="x", dry_run=False, skip_sync=True)
    _install_web_fake(monkeypatch, content="B", url="https://a.org/y")
    out = wiki_extract.run(cfg, "https://a.org/y", lang="x", dry_run=False, skip_sync=True)
    assert out[0].status == "extrait"
    assert len(_web_clippings(cfg)) == 2


# --- 1.3c : `source:` quotée (forme 'url') gérée ----------------------------

def test_url_dedup_matche_source_quotee(cfg, monkeypatch):
    """Un clipping déjà ingéré avec `source: 'https://...'` (entre quotes simples)
    doit toujours être matché : `parse_frontmatter` strip les guillemets."""
    url = "https://example.be/faq"
    # Simule manuellement un .md déjà présent avec la forme QUOTÉE
    web = cfg.RAW / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "faq-20260101.md").write_text(
        f'---\ntitle: "FAQ"\nsource: \'{url}\'\ntype: web\ncreated: 2026-01-01\n---\n\n'
        f"Contenu antérieur.\n",
        encoding="utf-8",
    )
    # 2e tentative de clip sur la même URL
    _install_web_fake(monkeypatch, content="Nouveau contenu", url=url)
    out = wiki_extract.run(cfg, url, lang="x", dry_run=False, skip_sync=True)
    assert out[0].status == "skipped_duplicate"
    # Un seul .md sous raw/web/ — pas de doublon créé
    assert len(list(web.glob("*.md"))) == 1


# --- 1.3d (bonus) : URL dedup + re-dépôt inbox supprimé ---------------------

def test_url_dedup_purge_le_binaire_redepose_inbox(cfg, monkeypatch):
    """Quand le 2e clip arrive via un binaire re-déposé en `_inbox/` (PDF — la
    seule source qui peut transiter par l'inbox en pratique), avec une URL
    d'origine déjà ingérée, le binaire est purgé après le `skipped_duplicate`."""
    # Premier clip web, via URL directe (un .md est créé sous raw/web/)
    _install_web_fake(monkeypatch, content="A", url="https://a.org/x")
    wiki_extract.run(cfg, "https://a.org/x", lang="x", dry_run=False, skip_sync=True)

    # 2e clip : on dépose un PDF placeholder dans l'inbox, et on simule qu'un
    # futur extracteur « web-via-inbox » l'aurait ingéré comme re-clip de la
    # même URL. On stubbe doc_type_for pour forcer le routage web SANS
    # désactiver l'extracteur : c'est la branche de dédup URL qui nous intéresse.
    kept = _drop(cfg, "x-clone.pdf", data=b"%PDF-placeholder")
    # Stub : ce PDF est en fait un re-clip web (le vrai extracteur le rejetterait,
    # mais on teste la branche de purge du binaire lors d'un dedup URL matché).
    monkeypatch.setattr(wiki_extract.extractors, "is_url", lambda src: True)
    _install_web_fake(monkeypatch, content="B", url="https://a.org/x")
    # Le `run` ne traite qu'une seule source si SOURCE est fourni ; on
    # court-circuite la résolution inbox de `run()` en appelant directement
    # process_source avec from_inbox=True.
    from pathlib import Path
    out = wiki_extract.process_source(
        cfg, str(kept), from_inbox=True, lang="x", dry_run=False,
    )
    assert out.status == "skipped_duplicate"
    assert "URL déjà ingérée" in out.message
    assert not Path(kept).exists()  # binaire purgé de l'inbox


# --- 2.4a : PDF sans TOC → marqueur présent ---------------------------------

def test_pdf_sans_toc_a_le_marqueur_rebuild(cfg, monkeypatch):
    """`chapters == []` → `_needs_toc_rebuild: true` + `_hint` non-vide dans le toc.json."""
    _install_pdf_fake(monkeypatch, content="Texte scanné sans signets.", structure=[])
    _drop(cfg, "scan.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)

    toc = json.loads((cfg.RAW / "pdfs" / "scan.toc.json").read_text(encoding="utf-8"))
    assert toc["chapters"] == []                       # 2.3 : pas d'invention
    assert toc["_needs_toc_rebuild"] is True
    assert isinstance(toc["_hint"], str) and toc["_hint"]


# --- 2.4b : PDF born-digital avec TOC → marqueur ABSENT --------------------

def test_pdf_avec_toc_napas_le_marqueur(cfg, monkeypatch):
    """Un PDF qui expose un chapitrage natif ne reçoit PAS le marqueur."""
    _install_pdf_fake(
        monkeypatch, content="Texte avec signets.",
        structure=[Chapter(1, "Ch1", 1, 3), Chapter(2, "Ch2", 4, 6)],
    )
    _drop(cfg, "native.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)

    toc = json.loads((cfg.RAW / "pdfs" / "native.toc.json").read_text(encoding="utf-8"))
    assert len(toc["chapters"]) == 2                   # 2.2 : pas de marqueur
    assert "_needs_toc_rebuild" not in toc
    assert "_hint" not in toc


# --- 2.4c : Web sans TOC ne reçoit PAS le marqueur (PDF-only) --------------

def test_web_sans_chapitres_n_a_pas_le_marqueur(cfg, monkeypatch):
    """Le marqueur est strictement PDF (signal de reconstruction TOC native)."""
    _install_web_fake(monkeypatch, content="# Page\n", url="https://a.org/x")
    out = wiki_extract.run(cfg, "https://a.org/x", lang="x", dry_run=False, skip_sync=True)
    assert out[0].status == "extrait"
    web = cfg.RAW / "web"
    # Pas de toc.json du tout pour le web (il n'y en a jamais eu)
    assert list(web.glob("*.toc.json")) == []


# --- 3.2 : Non-régression dédup SHA (PDF/EPUB inchangés) --------------------

def test_non_regression_dedup_sha_pdf(cfg, monkeypatch):
    """Le chemin de dédup SHA pour PDF n'est pas court-circuité par le pré-filtre URL."""
    _install_pdf_fake(monkeypatch, content="même contenu PDF")
    _drop(cfg, "a.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)

    # Re-dépôt d'un PDF au contenu identique (autre nom de fichier) — la dédup
    # SHA s'active, le re-dépôt est supprimé, pas de -2 créé.
    redo = _drop(cfg, "copie.pdf")
    out = wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)
    assert out[0].status == "skipped_duplicate"
    assert "contenu déjà ingéré" in out[0].message
    assert not redo.exists()
    assert not list((cfg.RAW / "pdfs").glob("*-2*"))


def test_non_regression_dedup_sha_web(cfg, monkeypatch):
    """La dédup SHA reste le filet de sécurité pour le web : un même contenu
    (même URL + même markdown) ingéré deux fois est toujours dédupliqué."""
    url = "https://a.org/same"
    _install_web_fake(monkeypatch, content="Contenu strictement identique", url=url)
    wiki_extract.run(cfg, url, lang="x", dry_run=False, skip_sync=True)
    # 2e appel, contenu strictement identique → soit URL dedup, soit SHA dedup
    # (le 1er match l'emporte : on s'attend à URL dedup qui se déclenche avant SHA).
    out = wiki_extract.run(cfg, url, lang="x", dry_run=False, skip_sync=True)
    assert out[0].status == "skipped_duplicate"
    assert len(_web_clippings(cfg)) == 1
