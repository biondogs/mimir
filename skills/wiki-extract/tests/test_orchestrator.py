"""Tests du cœur de l'orchestrateur (DoD Phase 1) via un extracteur factice.

Couvre, sans aucune lib lourde : routage, écriture raw/<type>/ complète, move
qui vide `_inbox/`, déduplication par SHA (suppression du re-dépôt), suffixage
`-2`, confinement (rien hors raw/+_inbox/), reprise sur crash (rejeu sans doublon).
"""

import json
from pathlib import Path

import pytest

import config_loader
import wiki_extract
from sync import locking as inbox_lock
from extractors.base import Asset, Chapter, ExtractResult, ExtractorError


@pytest.fixture(autouse=True)
def _lock_in_tmp(tmp_path, monkeypatch):
    """Verrou hors ~/.cache réel et hors work_root."""
    monkeypatch.setattr(inbox_lock, "_DEFAULT_LOCK", tmp_path / "lock" / "wiki-sync.lock")


@pytest.fixture
def cfg(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "_inbox").mkdir()
    cfgp = tmp_path / "wiki.config.json"
    cfgp.write_text(json.dumps({"work_root": str(work)}), encoding="utf-8")
    return config_loader.load_config(cfgp)


def _install_fake(monkeypatch, *, content, content_ext="pdf.txt", doc_type="pdfs",
                  assets=None, structure=None, error=None, title="Titre"):
    class Fake:
        SUPPORTED_EXTS = set()

        @staticmethod
        def extract(source, *, lang="fra+eng"):
            if error:
                raise error
            return ExtractResult(
                raw_content=content,
                content_ext=content_ext,
                metadata={"title": title, "pages": 3, "ocr": False, "source": str(source)},
                structure=structure if structure is not None else [Chapter(1, "Ch1", 1, 3)],
                assets=assets or [],
                doc_type=doc_type,
            )

    monkeypatch.setattr(wiki_extract.extractors, "get_extractor", lambda src: Fake)


def _drop(cfg, name, data=b"%PDF-binaire"):
    p = cfg.INBOX / name
    p.write_bytes(data)
    return p


# --- robustesse _inbox/ : les intrus n'interrompent pas le scan -----------
def test_scan_inbox_trie_supportes_et_intrus(cfg):
    """`.pdf`/`.epub`/`.md` → à extraire ; `.txt` → ignorés ; `.`/.DS_Store écartés.

    Note (palier `route-md-inbox-notes`) : un `README.md` est désormais une
    extension **supportée** — il devient une note (la discipline `_inbox/`
    reste une responsabilité utilisateur, cf. spec `extract-source`).
    """
    _drop(cfg, "guide.pdf")
    _drop(cfg, "livre.epub")
    _drop(cfg, "README.md", data=b"# procedure")
    _drop(cfg, "notes.txt", data=b"texte")
    _drop(cfg, ".DS_Store", data=b"\x00")
    to_extract, skipped = wiki_extract.scan_inbox(cfg)
    assert sorted(p.name for p in to_extract) == ["README.md", "guide.pdf", "livre.epub"]
    assert sorted(p.name for p in skipped) == ["notes.txt"]  # .DS_Store ni l'un ni l'autre


def test_run_ne_crashe_pas_sur_readme(cfg, monkeypatch):
    """Un README.md dans `_inbox/` ne fait plus échouer le batch ; le .pdf est traité.

    Changement de comportement (palier `route-md-inbox-notes`) : un `README.md`
    est désormais extrait comme note (status `extrait`), pas ignoré. Le test
    installe un fake `pdfs` ET un fake `notes` pour couvrir les deux branches.
    """
    fake_pdf_called = {"n": 0}
    fake_notes_called = {"n": 0}

    class FakePdf:
        SUPPORTED_EXTS = set()

        @staticmethod
        def extract(source, *, lang="fra+eng"):
            fake_pdf_called["n"] += 1
            return ExtractResult(
                raw_content="Texte du PDF.\n",
                content_ext="pdf.txt",
                metadata={"title": "book", "pages": 3, "ocr": False, "source": str(source)},
                structure=[Chapter(1, "Ch1", 1, 3)],
                assets=[],
                doc_type="pdfs",
            )

    class FakeNotes:
        SUPPORTED_EXTS = set()

        @staticmethod
        def extract(source, *, lang="fra+eng"):
            fake_notes_called["n"] += 1
            return ExtractResult(
                raw_content="procedure body\n",
                content_ext="md",
                source_content=b"---\ntitle: proc\n---\n\nprocedure body\n",
                source_ext="src.md",
                metadata={"title": "proc", "type": "note", "created": "2026-06-16"},
                structure=[],
                assets=[],
                doc_type="notes",
            )

    def get_extractor(src):
        if wiki_extract.extractors.doc_type_for(src) == "notes":
            return FakeNotes
        return FakePdf

    monkeypatch.setattr(wiki_extract.extractors, "get_extractor", get_extractor)
    _drop(cfg, "book.pdf")
    _drop(cfg, "README.md", data=b"# procedure")
    outcomes = wiki_extract.run(cfg, None, lang="fra+eng", dry_run=False, skip_sync=True)
    statuses = sorted(o.status for o in outcomes)
    assert statuses == ["extrait", "extrait"]
    assert (cfg.RAW / "pdfs" / "book.pdf.txt").is_file()
    # README.md est désormais routé vers notes (Décision 3 — assumé)
    assert any("README" in o.source for o in outcomes if o.status == "extrait")
    # les deux extracteurs ont été appelés
    assert fake_pdf_called["n"] == 1 and fake_notes_called["n"] == 1
    # code retour non-erreur
    assert not any(o.status == "error" for o in outcomes)


# --- écriture complète + move vide l'inbox --------------------------------
def test_pdf_complet_et_inbox_vide(cfg, monkeypatch):
    _install_fake(monkeypatch, content="Texte du PDF.\n")
    _drop(cfg, "book.pdf")
    outcomes = wiki_extract.run(cfg, None, lang="fra+eng", dry_run=False)

    assert [o.status for o in outcomes] == ["extrait"]
    pdfs = cfg.RAW / "pdfs"
    assert (pdfs / "book.pdf.txt").read_text(encoding="utf-8") == "Texte du PDF.\n"
    assert (pdfs / "book.pdf").is_file()  # binaire déplacé dans raw/
    toc = json.loads((pdfs / "book.toc.json").read_text(encoding="utf-8"))
    assert toc["ocr"] is False and toc["chapters"][0]["title"] == "Ch1"
    # _status.md contient une ligne avec le SHA du CONTENU
    status = (pdfs / "_status.md").read_text(encoding="utf-8")
    assert "book.pdf.txt" in status
    # _inbox/ vidé
    assert list(cfg.INBOX.iterdir()) == []


# --- déduplication : re-dépôt identique supprimé, pas de doublon ----------
def test_dedup_supprime_redepot(cfg, monkeypatch):
    _install_fake(monkeypatch, content="même contenu")
    _drop(cfg, "a.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False)

    # re-dépôt d'un binaire au contenu identique (autre nom)
    redo = _drop(cfg, "copie.pdf")
    out = wiki_extract.run(cfg, None, lang="x", dry_run=False)
    assert out[0].status == "skipped_duplicate"
    assert not redo.exists()  # supprimé de _inbox/
    # aucun fichier -2 créé
    assert not list((cfg.RAW / "pdfs").glob("*-2*"))
    assert list(cfg.INBOX.iterdir()) == []


# --- suffixage -2 : même nom, contenu différent ---------------------------
def test_suffixage_nom_identique_contenu_different(cfg, monkeypatch):
    _install_fake(monkeypatch, content="contenu A")
    _drop(cfg, "book.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False)

    _install_fake(monkeypatch, content="contenu B different")
    _drop(cfg, "book.pdf")  # même nom, mais l'ancien a été déplacé dans raw/
    out = wiki_extract.run(cfg, None, lang="x", dry_run=False)

    pdfs = cfg.RAW / "pdfs"
    assert out[0].suffixed is True
    assert (pdfs / "book-2.pdf.txt").read_text(encoding="utf-8") == "contenu B different"
    assert (pdfs / "book-2.pdf").is_file()
    assert (pdfs / "book-2.toc.json").is_file()
    # immutabilité : l'original intact
    assert (pdfs / "book.pdf.txt").read_text(encoding="utf-8") == "contenu A"


# --- erreur d'extraction : binaire conservé dans _inbox/ ------------------
def test_erreur_conserve_binaire(cfg, monkeypatch):
    _install_fake(monkeypatch, content="x", error=ExtractorError("couche texte vide, OCR indispo"))
    kept = _drop(cfg, "scan.pdf")
    out = wiki_extract.run(cfg, None, lang="x", dry_run=False)
    assert out[0].status == "error"
    assert kept.exists()  # NON déplacé : reprise possible
    assert not (cfg.RAW / "pdfs").exists()  # rien écrit dans raw/


# --- reprise sur crash : contenu+statut déjà écrits, binaire resté en inbox
def test_reprise_sans_doublon(cfg, monkeypatch):
    _install_fake(monkeypatch, content="déjà extrait")
    _drop(cfg, "first.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False)  # premier run complet (SHA enregistré)
    # simule l'état post-crash : raw+statut déjà là, binaire au MÊME contenu re-déposé
    leftover = _drop(cfg, "again.pdf")

    out = wiki_extract.run(cfg, None, lang="x", dry_run=False)
    assert out[0].status == "skipped_duplicate"
    assert not leftover.exists()
    assert not list((cfg.RAW / "pdfs").glob("*-2*"))


# --- confinement : rien hors raw/ + _inbox/ -------------------------------
def test_confinement_perimetre(cfg, monkeypatch):
    _install_fake(monkeypatch, content="texte")
    _drop(cfg, "book.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False)
    enfants = {p.name for p in cfg.work_root.iterdir()}
    assert enfants <= {"_inbox", "raw"}  # ni wiki/ ni reading-grids/ ni autre


# --- web : markdown inline + frontmatter + image localisée ----------------
def test_web_clipping(cfg, monkeypatch):
    asset = Asset(filename="img.png", data=b"PNG", original_ref="https://ex.org/i.png")
    _install_fake(
        monkeypatch,
        content="# Titre\n\n![x](https://ex.org/i.png)\n",
        content_ext="md",
        doc_type="web",
        assets=[asset],
        structure=[],
        title="Ma Page",
    )
    out = wiki_extract.run(cfg, "https://ex.org/article", lang="x", dry_run=False)
    assert out[0].status == "extrait"
    web = cfg.RAW / "web"
    md = next(web.glob("ma-page-*.md"))
    text = md.read_text(encoding="utf-8")
    assert text.startswith("---\n")  # frontmatter
    assert "type: web" in text
    assert "https://ex.org/i.png" not in text  # lien réécrit
    assert "_assets/" in text
    assert (web / "_assets").is_dir() and any((web / "_assets").iterdir())


# --- 3.1 : PDF à 3 images -> _assets/<base>/img-*.png ----------------------

def test_pdf_assets_regroupes_par_source(cfg, monkeypatch):
    """Un PDF produisant 3 images écrit les assets dans
    `raw/pdfs/_assets/<base>/<nom-image>` (palier v1→v2), pas à plat
    avec un préfixe `<base>-`."""
    assets = [
        Asset(filename="p001-img01.png", data=b"PNG1"),
        Asset(filename="p002-img02.jpg", data=b"JPG2"),
        Asset(filename="p003-img03.gif", data=b"GIF3"),
    ]
    _install_fake(
        monkeypatch,
        content="Texte du PDF.\n",
        content_ext="pdf.txt",
        doc_type="pdfs",
        assets=assets,
    )
    _drop(cfg, "book.pdf")
    out = wiki_extract.run(cfg, None, lang="x", dry_run=False)
    assert out[0].status == "extrait"

    pdfs = cfg.RAW / "pdfs"
    assets_dir = pdfs / "_assets"
    # Le sous-répertoire par source existe
    assert (assets_dir / "book").is_dir()
    # Les 3 images sont dans le sous-répertoire, pas à plat
    assert (assets_dir / "book" / "p001-img01.png").is_file()
    assert (assets_dir / "book" / "p002-img02.jpg").is_file()
    assert (assets_dir / "book" / "p003-img03.gif").is_file()
    # Pas d'images à plat avec préfixe `<base>-`
    flat_assets = [p.name for p in assets_dir.iterdir() if p.is_file()]
    assert not any(name.startswith("book-") for name in flat_assets)
    # Le contenu de chaque image est préservé
    assert (assets_dir / "book" / "p001-img01.png").read_bytes() == b"PNG1"


def test_pdf_deux_sources_ont_leurs_propres_sous_repertoires(cfg, monkeypatch):
    """Deux PDF distincts = deux sous-répertoires disjoints sous `_assets/`."""
    # Premier PDF
    _install_fake(
        monkeypatch,
        content="PDF A",
        content_ext="pdf.txt",
        doc_type="pdfs",
        assets=[Asset(filename="p001.png", data=b"A1")],
    )
    _drop(cfg, "alpha.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False)

    # Deuxième PDF (reconfiguré)
    _install_fake(
        monkeypatch,
        content="PDF B",
        content_ext="pdf.txt",
        doc_type="pdfs",
        assets=[Asset(filename="p001.png", data=b"B1")],
    )
    _drop(cfg, "beta.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False)

    pdfs = cfg.RAW / "pdfs"
    assert (pdfs / "_assets" / "alpha" / "p001.png").read_bytes() == b"A1"
    assert (pdfs / "_assets" / "beta" / "p001.png").read_bytes() == b"B1"


def test_pdf_assets_dans_sous_repertoire_confine(cfg, monkeypatch):
    """L'écriture des assets dans `_assets/<base>/` reste confinée à work_root.

    Un `<base>` qui tenterait une évasion (`../foo`) serait rejeté par
    `guard.assert_within` au moment du `atomic_write_bytes`. On vérifie
    qu'un slug légitime (et non une évasion) passe — confinement **positif**
    (chemin sous work_root) et non que l'évasion est levée (cas testé en
    aval dans `test_guard.py`)."""
    _install_fake(
        monkeypatch,
        content="x",
        content_ext="pdf.txt",
        doc_type="pdfs",
        assets=[Asset(filename="p001.png", data=b"PNG")],
    )
    _drop(cfg, "book.pdf")
    wiki_extract.run(cfg, None, lang="x", dry_run=False)

    # Tout ce qui a été créé est sous work_root
    enfants = {p.name for p in cfg.work_root.iterdir()}
    assert enfants <= {"_inbox", "raw"}  # pas d'évasion


# --- 5.2 / 5.3 : web clipping avec HTML source + assets regroupés ----------

def test_web_clipping_ecrit_html_a_cote_du_md(cfg, monkeypatch):
    """Le clipping web écrit `raw/web/<slug>-<date>.html` (matière immuable)
    à côté du `.md` dérivé. Le `.html` est la réponse fetchée brute, pas une
    transformation (palier v2→v3)."""
    _HTML = "<html><body><article><h1>Le Titre</h1><p>Le contenu.</p></article></body></html>"

    class FakeWeb:
        SUPPORTED_EXTS = set()
        @staticmethod
        def extract(source, *, lang="fra+eng"):
            return ExtractResult(
                raw_content="# Le Titre\n\nLe contenu.\n",
                content_ext="md",
                source_content=_HTML.encode("utf-8"),
                source_ext="html",
                metadata={
                    "title": "Le Titre",
                    "source": str(source),
                    "type": "web",
                    "created": "2026-06-15",
                },
                structure=[],
                assets=[],
                doc_type="web",
            )

    monkeypatch.setattr(wiki_extract.extractors, "get_extractor", lambda src: FakeWeb)

    out = wiki_extract.run(cfg, "https://exemple.org/titre", lang="x", dry_run=False)
    assert out[0].status == "extrait"

    web = cfg.RAW / "web"
    md_files = [p for p in web.glob("*.md") if p.name != "_status.md"]
    html_files = list(web.glob("*.html"))
    assert len(md_files) == 1
    assert len(html_files) == 1
    # Le .html et le .md partagent le même préfixe (suffixage -N partagé).
    assert md_files[0].name.removesuffix(".md") == html_files[0].name.removesuffix(".html")
    # Le .html est la réponse brute (pas une transformation).
    assert html_files[0].read_text(encoding="utf-8") == _HTML


def test_web_clipping_assets_regroupes_par_slug(cfg, monkeypatch):
    """Les images web sont rangées sous `raw/web/_assets/<slug>/` (regroupées
    par clipping), pas à plat. Le slug = nom de fichier du .md sans extension."""
    asset = Asset(filename="img.png", data=b"PNG", original_ref="https://ex.org/i.png")
    _HTML_SRC = "<html><body><article><h1>Ma Page</h1><p>x</p></article></body></html>"

    # Custom installer : on doit aussi poser source_content (la fixture
    # `_install_fake` ne le fait pas — le test vérifie l'intégration
    # d'orchestrateur avec un ExtractResult complet).
    class FakeWeb:
        SUPPORTED_EXTS = set()
        @staticmethod
        def extract(source, *, lang="fra+eng"):
            return ExtractResult(
                raw_content="# Ma Page\n\n![x](https://ex.org/i.png)\n",
                content_ext="md",
                source_content=_HTML_SRC.encode("utf-8"),
                source_ext="html",
                metadata={"title": "Ma Page", "source": str(source), "type": "web", "created": "2026-06-15"},
                structure=[],
                assets=[asset],
                doc_type="web",
            )

    monkeypatch.setattr(wiki_extract.extractors, "get_extractor", lambda src: FakeWeb)

    out = wiki_extract.run(cfg, "https://ex.org/article", lang="x", dry_run=False)
    assert out[0].status == "extrait"

    web = cfg.RAW / "web"
    md_files = [p for p in web.glob("ma-page-*.md")]
    assert len(md_files) == 1
    md_file = md_files[0]
    slug = md_file.name.removesuffix(".md")
    # L'asset est sous `<slug>/`
    asset_path = web / "_assets" / slug / "img.png"
    assert asset_path.is_file()
    # Le lien dans le .md est réécrit en relatif
    text = md_file.read_text(encoding="utf-8")
    assert f"_assets/{slug}/img.png" in text
    # Le HTML source est aussi écrit
    html_files = list(web.glob("*.html"))
    assert len(html_files) == 1
    assert html_files[0].name == md_file.name.removesuffix(".md") + ".html"


# --- verrou tenu : skip propre --------------------------------------------
def test_verrou_tenu(cfg, monkeypatch):
    _install_fake(monkeypatch, content="x")
    held = inbox_lock.acquire()  # un autre process tient le verrou
    try:
        _drop(cfg, "book.pdf")
        out = wiki_extract.run(cfg, None, lang="x", dry_run=False)
        assert out[0].status == "locked"
        assert (cfg.INBOX / "book.pdf").exists()  # rien traité
    finally:
        held.release()


# --- dry-run : aucune écriture --------------------------------------------
def test_dry_run(cfg, monkeypatch):
    _install_fake(monkeypatch, content="x")
    kept = _drop(cfg, "book.pdf")
    out = wiki_extract.run(cfg, None, lang="x", dry_run=True)
    assert out[0].status == "extrait" and "dry-run" in out[0].message
    assert kept.exists()
    assert not (cfg.RAW / "pdfs").exists()


# --- pré-sync (pull) en échec : on n'extrait pas (SPEC §10) ----------------
def test_pull_echec_interrompt(cfg, monkeypatch):
    _install_fake(monkeypatch, content="x")
    kept = _drop(cfg, "book.pdf")
    monkeypatch.setattr(wiki_extract.sync, "pull", lambda c: 1)
    out = wiki_extract.run(cfg, None, lang="x", dry_run=False)
    assert out[0].status == "error" and "pull" in out[0].message
    assert kept.exists()  # rien consommé
    assert not (cfg.RAW / "pdfs").exists()


# --- post-sync (push) en échec : signalé, travail local conservé ----------
def test_push_echec_signale_mais_conserve(cfg, monkeypatch):
    _install_fake(monkeypatch, content="Texte.\n")
    _drop(cfg, "book.pdf")
    monkeypatch.setattr(wiki_extract.sync, "push", lambda c: 1)
    out = wiki_extract.run(cfg, None, lang="x", dry_run=False)
    statuses = [o.status for o in out]
    assert "extrait" in statuses and "error" in statuses
    assert (cfg.RAW / "pdfs" / "book.pdf.txt").is_file()  # écriture bien conservée


# --- --skip-sync : pull/push jamais appelés -------------------------------
def test_skip_sync_court_circuite(cfg, monkeypatch):
    _install_fake(monkeypatch, content="x")
    _drop(cfg, "book.pdf")
    monkeypatch.setattr(wiki_extract.sync, "pull", lambda c: (_ for _ in ()).throw(AssertionError("pull")))
    monkeypatch.setattr(wiki_extract.sync, "push", lambda c: (_ for _ in ()).throw(AssertionError("push")))
    out = wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)
    assert out[0].status == "extrait"


# --- palier `route-md-inbox-notes` : routage des `.md` de `_inbox/` --------
# Un `.md` déposé est sa propre matière source (pas de `.html` parent
# re-dérivable). On introduit un `doc_type = "notes"` distinct du web :
#   1. `raw/notes/<base>.src.md` = original verbatim (immuable)
#   2. `raw/notes/<base>.md`      = dérivation (frontmatter normalisé)
# Pas de double frontmatter, pas de sidecar `.html`, pas de dédup URL.


def _install_fake_notes(monkeypatch, *, source_bytes, title, created, source=None):
    """Installe un faux extracteur `notes` qui produit un `ExtractResult`
    cohérent avec ce que renvoie `markdown.extract()` en pratique. Le frontmatter
    de l'original est déjà strippé dans `raw_content` (le corps seul)."""

    class FakeNotes:
        SUPPORTED_EXTS = set()

        @staticmethod
        def extract(src, *, lang="fra+eng"):
            metadata: dict = {"title": title, "type": "note", "created": created}
            if source:
                metadata["source"] = source
            return ExtractResult(
                raw_content="Corps de la note.\n",
                content_ext="md",
                source_content=source_bytes,
                source_ext="src.md",
                metadata=metadata,
                structure=[],
                assets=[],
                doc_type="notes",
            )

    def get_extractor(src):
        return FakeNotes

    monkeypatch.setattr(wiki_extract.extractors, "get_extractor", get_extractor)


def test_note_avec_frontmatter_routee_vers_notes(cfg, monkeypatch):
    """5.1 — `.md` avec frontmatter déposé en `_inbox/` → `raw/notes/<slug>.md`
    (frontmatter normalisé) + `<slug>.src.md` (verbatim), `_inbox/` vidé."""
    original = (
        b"---\n"
        b"title: Ma Note\n"
        b"source: https://exemple.org/clip\n"
        b"author: moi\n"
        b"---\n\n"
        b"# Ma Note\n\nCorps de la note avec un auteur perso.\n"
    )
    _install_fake_notes(
        monkeypatch,
        source_bytes=original,
        title="Ma Note",
        created="2026-06-16",
        source="https://exemple.org/clip",
    )
    _drop(cfg, "ma-note.md", data=original)
    outcomes = wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)

    assert [o.status for o in outcomes] == ["extrait"]
    notes = cfg.RAW / "notes"
    # Le `.src.md` est l'original **verbatim** (octet pour octet)
    src_path = notes / "ma-note.src.md"
    assert src_path.is_file()
    assert src_path.read_bytes() == original
    # Le `.md` dérivé porte un frontmatter normalisé
    md_path = notes / "ma-note.md"
    assert md_path.is_file()
    md_text = md_path.read_text(encoding="utf-8")
    assert md_text.startswith("---\n")
    assert "type: note" in md_text
    assert "title: \"Ma Note\"" in md_text
    assert "source: https://exemple.org/clip" in md_text
    assert "tags: [clippings]" in md_text
    # `_inbox/` est vidé
    assert list(cfg.INBOX.iterdir()) == []
    # _status.md contient la ligne (SHA du contenu dérivé)
    status = (notes / "_status.md").read_text(encoding="utf-8")
    assert "ma-note.md" in status


def test_note_sans_frontmatter_devient_note(cfg, monkeypatch, tmp_path):
    """5.2 — `.md` sans frontmatter → title=basename, created=mtime, pas de crash."""
    # Pas de frontmatter : on laisse l'extracteur `markdown` réel faire son
    # travail (pas de mock) — on passe par `process_source` directement.
    raw_text = b"# procedure\n\n- etape 1\n- etape 2\n"
    p = cfg.INBOX / "readme.md"
    p.write_bytes(raw_text)
    # date de mtime déterministe (pour le `created` dérivé)
    import os
    import time

    fixed_mtime = time.mktime(time.strptime("2026-06-10", "%Y-%m-%d"))
    os.utime(p, (fixed_mtime, fixed_mtime))

    out = wiki_extract.process_source(
        cfg, str(p), from_inbox=True, lang="x", dry_run=False
    )
    assert out.status == "extrait"
    notes = cfg.RAW / "notes"
    assert (notes / "readme.src.md").read_bytes() == raw_text
    md_text = (notes / "readme.md").read_text(encoding="utf-8")
    assert "type: note" in md_text
    assert "title: \"readme\"" in md_text
    assert "created: 2026-06-10" in md_text
    # Pas de `source:` inventé
    assert "source:" not in md_text.split("tags:")[0]
    # _inbox/ vidé
    assert not p.exists()


def test_redepot_note_dedup_sha(cfg, monkeypatch):
    """5.3 — re-dépôt du même `.md` → `skipped_duplicate` (SHA de contenu)."""
    original = b"---\ntitle: A\n---\n\nCorps identique.\n"
    _install_fake_notes(
        monkeypatch,
        source_bytes=original,
        title="A",
        created="2026-06-16",
    )
    _drop(cfg, "a.md", data=original)
    wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)

    # Re-dépôt (autre nom) au MÊME contenu
    redo = _drop(cfg, "copie.md", data=original)
    outcomes = wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)
    skipped = [o for o in outcomes if o.status == "skipped_duplicate"]
    assert len(skipped) == 1
    assert not redo.exists()  # supprimé de _inbox/
    notes = cfg.RAW / "notes"
    # Aucun `-2` créé
    assert not list(notes.glob("*-2*.md"))


def test_note_pas_de_double_frontmatter_ni_html(cfg, monkeypatch):
    """Le `.md` dérivé a un frontmatter normalisé (pas empilé) et **aucun**
    sidecar `.html` n'est écrit (la branche web n'est pas empruntée)."""
    original = b"---\ntitle: B\nauthor: x\n---\n\nCorps B\n"
    _install_fake_notes(monkeypatch, source_bytes=original, title="B", created="2026-06-16")
    _drop(cfg, "b.md", data=original)
    wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)

    notes = cfg.RAW / "notes"
    md_text = (notes / "b.md").read_text(encoding="utf-8")
    # Un seul bloc frontmatter, pas de wrap web
    assert md_text.count("---") == 2
    assert "type: web" not in md_text
    assert "type: note" in md_text
    # Pas de sidecar .html
    assert not list(notes.glob("*.html"))


def test_note_src_md_et_md_partagent_suffixe_collision(cfg, monkeypatch):
    """Si `<base>.md` et `<base>.src.md` sont déjà pris, le suffixage `-N` est
    partagé entre les deux fichiers du triplet (cohérent avec le triplet web)."""
    original1 = b"---\ntitle: C\n---\n\nCorps C v1\n"
    original2 = b"---\ntitle: C\n---\n\nCorps C v2 different\n"

    # Fake qui varie le `raw_content` selon l'entrée, pour que la dédup SHA
    # voie bien un contenu différent au 2e dépôt.
    class FakeNotesVar:
        SUPPORTED_EXTS = set()

        @staticmethod
        def extract(src, *, lang="fra+eng"):
            data = Path(str(src)).read_bytes()
            return ExtractResult(
                raw_content=data.decode("utf-8", errors="replace"),
                content_ext="md",
                source_content=data,
                source_ext="src.md",
                metadata={"title": "C", "type": "note", "created": "2026-06-16"},
                structure=[],
                assets=[],
                doc_type="notes",
            )

    def get_extractor(src):
        return FakeNotesVar

    monkeypatch.setattr(wiki_extract.extractors, "get_extractor", get_extractor)

    # Premier dépôt : crée `c.src.md` et `c.md`
    _drop(cfg, "c.md", data=original1)
    wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)

    # Deuxième dépôt au même nom (le 1er a été bougé dans raw/) — contenu
    # différent → nouvelle entrée avec suffixage -2 partagé.
    _drop(cfg, "c.md", data=original2)
    outcomes = wiki_extract.run(cfg, None, lang="x", dry_run=False, skip_sync=True)
    # Le contenu a changé → nouvelle entrée (suffixage -2 partagé)
    assert outcomes[0].status == "extrait"
    assert outcomes[0].suffixed is True
    notes = cfg.RAW / "notes"
    assert (notes / "c-2.md").is_file()
    assert (notes / "c-2.src.md").is_file()
    # Pas de `-3`
    assert not (notes / "c-3.md").exists()


def test_extracteur_markdown_passthrough():
    """5.x — test direct de l'extracteur `markdown.py` (sans orchestrateur)."""
    import tempfile

    from extractors.markdown import extract as md_extract

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.md"
        p.write_bytes(
            b"---\n"
            b"title: Le Titre\n"
            b"source: https://exemple.org/clip\n"
            b"---\n\n"
            b"# Le Titre\n\nContenu.\n"
        )
        r = md_extract(str(p))
        assert r.doc_type == "notes"
        assert r.content_ext == "md"
        assert r.source_ext == "src.md"
        assert r.source_content.startswith(b"---\n")
        # Le body est strippé du frontmatter
        assert r.raw_content.startswith("# Le Titre")
        assert "title:" not in r.raw_content
        assert r.metadata["title"] == "Le Titre"
        assert r.metadata["source"] == "https://exemple.org/clip"
        assert r.metadata["type"] == "note"


def test_extracteur_markdown_sans_frontmatter(tmp_path):
    """Un `.md` sans frontmatter : title=basename, created=mtime, pas de crash."""
    from datetime import date
    from pathlib import Path

    from extractors.markdown import extract as md_extract

    p = tmp_path / "notes-perso.md"
    p.write_bytes(b"juste du texte, pas de frontmatter\n")
    r = md_extract(str(p))
    assert r.metadata["title"] == "notes-perso"
    assert r.metadata["type"] == "note"
    assert r.metadata["created"] == date.today().isoformat()
    assert "source" not in r.metadata
    assert r.raw_content == "juste du texte, pas de frontmatter\n"


def test_extracteur_markdown_frontmatter_malforme(tmp_path):
    """Un frontmatter YAML cassé ne fait PAS échouer le lot : on dérive
    title/created par défaut, corps = fichier entier (Décision 2 / spec)."""
    from extractors.markdown import extract as md_extract

    p = tmp_path / "broken.md"
    p.write_bytes(
        b"---\n"
        b"title: ok\n"
        b"  bad indent: : ::\n"
        b"unclosed: [\n"
        b"---\n\n"
        b"corps qui suit\n"
    )
    r = md_extract(str(p))
    # Pas d'exception, et la dérive par défaut prend le relais
    assert r.doc_type == "notes"
    # En cas de fallback stdlib, on garde au moins `title` (parsing naïf)
    # OU on retombe sur le basename — l'un ou l'autre est acceptable tant
    # qu'on n'invente rien.
    assert r.metadata.get("title") in ("ok", "broken")
