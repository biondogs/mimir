"""Tests de l'orchestrateur wiki-ingest (DoD Phase 2, sans LLM).

Couvre : inventaire (exclusions), idempotence du ledger, re-ingestion sur SHA changé,
MAJ `_status.md` + ledger, pipeline complet (inventory → write-article → finalize),
`--dry-run` et `--skip-sync`.
"""

import json
from datetime import date
from pathlib import Path

import pytest

import config_loader
import ledger as L
import status_table
import wiki_ingest
from article_writer import ArticleSpec


@pytest.fixture
def cfg(tmp_path):
    work = tmp_path / "work"
    (work / "raw" / "pdfs").mkdir(parents=True)
    cfgp = tmp_path / "wiki.config.json"
    cfgp.write_text(json.dumps({"work_root": str(work)}), encoding="utf-8")
    return config_loader.load_config(cfgp)


def _seed_pdf(cfg, base="navigation-cotiere", content="Le relèvement permet de se positionner.\n"):
    pdfs = cfg.RAW / "pdfs"
    (pdfs / f"{base}.pdf.txt").write_text(content, encoding="utf-8")
    (pdfs / f"{base}.toc.json").write_text(json.dumps({"chapters": []}), encoding="utf-8")
    # sidecars de service qui NE doivent PAS apparaître dans la worklist
    (pdfs / "_status.md").write_text("# Statut — pdfs\n", encoding="utf-8")
    (pdfs / "INDEX.md").write_text("# index\n", encoding="utf-8")
    return f"raw/pdfs/{base}.pdf.txt"


def _write(cfg, subject, notion, body, tmp_path, **kw):
    return wiki_ingest.cmd_write_article(
        cfg, ArticleSpec(subject=subject, notion=notion, body=body, **kw)
    )


# --- inventaire : worklist exclut les fichiers de service ------------------
def test_inventory_exclut_service(cfg):
    rel = _seed_pdf(cfg)
    data = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)
    paths = [w["content_path"] for w in data["worklist"]]
    assert paths == [rel]                                # ni _status.md, ni INDEX.md, ni .toc.json
    assert data["worklist"][0]["toc_path"] == "raw/pdfs/navigation-cotiere.toc.json"


# --- idempotence : après finalize, la worklist est vide --------------------
def test_idempotence_apres_finalize(cfg):
    _seed_pdf(cfg)
    data = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)
    entry = data["worklist"][0]
    wiki_ingest.cmd_finalize(cfg, entry["content_path"], entry["sha256"],
                             ["navigation/relevement"], skip_sync=True)

    again = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)
    assert again["worklist"] == []                       # rien à recompiler


# --- re-ingestion : SHA changé -> la source ressort ------------------------
def test_reingestion_sur_sha_change(cfg):
    rel = _seed_pdf(cfg)
    data = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)
    e = data["worklist"][0]
    wiki_ingest.cmd_finalize(cfg, e["content_path"], e["sha256"], ["navigation/relevement"], skip_sync=True)

    # le contenu change -> nouveau SHA
    (cfg.RAW / "pdfs" / "navigation-cotiere.pdf.txt").write_text("Contenu révisé.\n", encoding="utf-8")
    again = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)
    assert [w["content_path"] for w in again["worklist"]] == [rel]


# --- MAJ _status.md + ledger -----------------------------------------------
def test_finalize_met_a_jour_statut_et_ledger(cfg):
    _seed_pdf(cfg)
    e = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)["worklist"][0]
    wiki_ingest.cmd_finalize(
        cfg, e["content_path"], e["sha256"],
        ["navigation/relevement", "navigation/triangulation"], skip_sync=True,
    )

    rows = status_table.load_status(cfg.RAW / "pdfs" / "_status.md")
    row = next(r for r in rows if r.fichier == "navigation-cotiere.pdf.txt")
    assert row.statut == "compilé"
    assert row.sha256 == e["sha256"]
    assert "[[navigation/relevement]]" in row.articles and "[[navigation/triangulation]]" in row.articles

    led = L.load_ledger(cfg.LEDGER)
    assert led["raw/pdfs/navigation-cotiere.pdf.txt"].articles == [
        "navigation/relevement", "navigation/triangulation"
    ]


# --- normalisation : le ledger stocke sujet/notion SANS préfixe wiki/ ------
def test_finalize_strippe_prefixe_wiki(cfg):
    """Un `--articles wiki/sujet/notion` est canonicalisé en `sujet/notion`.

    Sinon `load_article` résoudrait `cfg.WIKI / "wiki/sujet/notion"` (introuvable),
    d'où la grille à « 0 article lié » du guide brevet. Re-finalize reste idempotent.
    """
    _seed_pdf(cfg)
    e = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)["worklist"][0]
    wiki_ingest.cmd_finalize(
        cfg, e["content_path"], e["sha256"],
        ["wiki/navigation/relevement", "navigation/triangulation"], skip_sync=True,
    )
    led = L.load_ledger(cfg.LEDGER)
    assert led["raw/pdfs/navigation-cotiere.pdf.txt"].articles == [
        "navigation/relevement", "navigation/triangulation"
    ]
    rows = status_table.load_status(cfg.RAW / "pdfs" / "_status.md")
    row = next(r for r in rows if r.fichier == "navigation-cotiere.pdf.txt")
    assert "[[wiki/navigation/relevement]]" not in row.articles
    assert "[[navigation/relevement]]" in row.articles


# --- pipeline complet sans LLM ---------------------------------------------
def test_pipeline_complet(cfg, tmp_path):
    _seed_pdf(cfg)
    e = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)["worklist"][0]

    r1 = _write(cfg, "navigation", "Relèvement", "# Relèvement\n\n> r\n\n## Définition\nx\n", tmp_path,
                sources=[e["content_path"] + "#ch1"])
    r2 = _write(cfg, "navigation", "Triangulation", "# Triangulation\n\n> t\n\n## Définition\ny\n", tmp_path,
                sources=[e["content_path"] + "#ch1"])
    assert r1.created and r2.created

    wiki_ingest.cmd_finalize(cfg, e["content_path"], e["sha256"],
                             [r1.wikilink, r2.wikilink], skip_sync=True)

    nav = cfg.WIKI / "navigation"
    assert {p.name for p in nav.glob("*.md")} == {"relevement.md", "triangulation.md"}
    rows = status_table.load_status(cfg.RAW / "pdfs" / "_status.md")
    assert next(r for r in rows if r.fichier == "navigation-cotiere.pdf.txt").statut == "compilé"

    # relance complète : rien à recompiler, pas de doublon
    assert wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)["worklist"] == []
    _write(cfg, "navigation", "Relèvement", "# Relèvement\n\n> r2\n", tmp_path)  # re-write idempotent
    assert len(list(nav.glob("*.md"))) == 2


# --- dry-run : aucune écriture ---------------------------------------------
def test_finalize_dry_run(cfg):
    _seed_pdf(cfg)
    e = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)["worklist"][0]
    out = wiki_ingest.cmd_finalize(cfg, e["content_path"], e["sha256"],
                                   ["navigation/relevement"], skip_sync=True, dry_run=True)
    assert out.status == "finalisé" and "dry-run" in out.detail
    assert not cfg.LEDGER.exists()                       # ledger non écrit
    status = (cfg.RAW / "pdfs" / "_status.md").read_text(encoding="utf-8")
    assert "compilé" not in status


# --- skip-sync : le stub n'est appelé que si on ne skippe pas --------------
def test_skip_sync(cfg, monkeypatch):
    _seed_pdf(cfg)
    calls = {"pull": 0, "push": 0}
    monkeypatch.setattr(wiki_ingest.sync, "pull", lambda c: calls.__setitem__("pull", calls["pull"] + 1) or 0)
    monkeypatch.setattr(wiki_ingest.sync, "push", lambda c: calls.__setitem__("push", calls["push"] + 1) or 0)

    wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)
    e = wiki_ingest.cmd_inventory(cfg, None, skip_sync=False, dry_run=False)["worklist"][0]
    assert calls["pull"] == 1                             # 1er appel skippé, 2e non

    wiki_ingest.cmd_finalize(cfg, e["content_path"], e["sha256"], ["navigation/relevement"], skip_sync=True)
    assert calls["push"] == 0                             # finalize skippé
    wiki_ingest.cmd_finalize(cfg, e["content_path"], e["sha256"], ["navigation/relevement"], skip_sync=False)
    assert calls["push"] == 1


# --- digest de statut par source (spec `compile-source`) -------------------
def test_status_extrait_quand_sidecars_et_zero_article(cfg):
    """Sidecars (.txt + .toc.json) présents, 0 article dans le ledger
    → statut `extrait`, compte 0. **Jamais** `compilé`."""
    rel = _seed_pdf(cfg)
    digest = wiki_ingest.cmd_status(cfg, skip_sync=True, dry_run=False)
    assert "sources" in digest
    row = next(s for s in digest["sources"] if s["content_path"] == rel)
    assert row["doc_type"] == "pdfs"
    assert row["status"] == "extrait"                    # pas compilé !
    assert row["article_count"] == 0


def test_status_compile_apres_finalize(cfg, tmp_path):
    """Source compilée via finalize → statut `compilé`, compte = N articles."""
    _seed_pdf(cfg)
    e = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)["worklist"][0]
    r1 = _write(cfg, "navigation", "Relèvement", "# Relèvement\n\nx\n", tmp_path,
                sources=[e["content_path"] + "#ch1"])
    r2 = _write(cfg, "navigation", "Triangulation", "# Triangulation\n\ny\n", tmp_path,
                sources=[e["content_path"] + "#ch1"])
    wiki_ingest.cmd_finalize(
        cfg, e["content_path"], e["sha256"],
        [r1.wikilink, r2.wikilink], skip_sync=True,
    )
    digest = wiki_ingest.cmd_status(cfg, skip_sync=True, dry_run=False)
    row = next(s for s in digest["sources"] if s["content_path"] == e["content_path"])
    assert row["status"] == "compilé"
    assert row["article_count"] == 2


def test_status_partiellement_compile(cfg, tmp_path):
    """Source finalisée avec statut `partiellement compilé` → son digest
    reste `partiellement compilé` (distinct de `compilé`)."""
    _seed_pdf(cfg, base="brevet-cotiere")
    e = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)["worklist"][0]
    r = _write(cfg, "brevet", "Relèvement", "# Relèvement\n\nx\n", tmp_path,
               sources=[e["content_path"] + "#ch1"])
    wiki_ingest.cmd_finalize(
        cfg, e["content_path"], e["sha256"],
        [r.wikilink], status="partiellement compilé", skip_sync=True,
    )
    digest = wiki_ingest.cmd_status(cfg, skip_sync=True, dry_run=False)
    row = next(s for s in digest["sources"] if s["content_path"] == e["content_path"])
    assert row["status"] == "partiellement compilé"
    assert row["article_count"] == 1


def test_status_skip_sync_par_defaut(cfg, monkeypatch):
    """`cmd_status` ne déclenche AUCUN appel réseau (pull/push) par défaut —
    un digest est une lecture, pas une synchro."""
    _seed_pdf(cfg)
    calls = {"pull": 0, "push": 0}
    monkeypatch.setattr(wiki_ingest.sync, "pull", lambda c: calls.__setitem__("pull", calls["pull"] + 1) or 0)
    monkeypatch.setattr(wiki_ingest.sync, "push", lambda c: calls.__setitem__("push", calls["push"] + 1) or 0)
    wiki_ingest.cmd_status(cfg, skip_sync=True, dry_run=False)
    assert calls["pull"] == 0 and calls["push"] == 0


# --- palier `route-md-inbox-notes` : scan `raw/notes/` ---------------------
# 5.4 : `wiki-ingest` compile `<slug>.md` mais **ignore** `<slug>.src.md`
# (matière immuable, pas de double compilation).


def _seed_note(cfg, base="note-test", content="# Note test\n\nCorps.\n"):
    notes = cfg.RAW / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    # Dérivation compilable (frontmatter normalisé)
    (notes / f"{base}.md").write_text(
        f"---\ntitle: \"{base}\"\ntype: note\ncreated: 2026-06-16\ntags: [clippings]\n---\n\n{content}",
        encoding="utf-8",
    )
    # Matière immuable verbatim (mimicking wiki-extract)
    (notes / f"{base}.src.md").write_bytes(content.encode("utf-8"))
    return f"raw/notes/{base}.md"


def test_inventory_notes_compile_md_pas_src_md(cfg):
    """5.4 — l'inventaire ne liste QUE le `<base>.md` (dérivation compilable).
    Le `<base>.src.md` (matière immuable) est filtré — pas de double notion."""
    rel = _seed_note(cfg)
    data = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)
    paths = [w["content_path"] for w in data["worklist"]]
    assert rel in paths
    # Le `.src.md` n'apparaît PAS
    assert "raw/notes/note-test.src.md" not in paths
    # Le `doc_type` est bien `notes`
    e = next(w for w in data["worklist"] if w["content_path"] == rel)
    assert e["doc_type"] == "notes"
    # Pas de toc.json pour les notes
    assert e["toc_path"] is None


def test_inventory_notes_idempotent_apres_finalize(cfg):
    """Notes : finalize → l'inventaire ressort vide (cohérent avec PDF/EPUB)."""
    rel = _seed_note(cfg)
    e = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)["worklist"][0]
    r = wiki_ingest.cmd_write_article(
        cfg, ArticleSpec(subject="notes", notion="Note test", body="# Note test\n\nx\n",
                          sources=[e["content_path"]])
    )
    wiki_ingest.cmd_finalize(cfg, e["content_path"], e["sha256"], [r.wikilink], skip_sync=True)
    again = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)
    assert again["worklist"] == []


def test_inventory_notes_sha_change_recompile(cfg):
    """Notes : si le contenu dérivé change, l'inventaire le ressort (r2 du SHA)."""
    _seed_note(cfg, content="v1\n")
    e = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)["worklist"][0]
    r = wiki_ingest.cmd_write_article(
        cfg, ArticleSpec(subject="notes", notion="note-test", body="# note-test\n\nx\n",
                          sources=[e["content_path"]])
    )
    wiki_ingest.cmd_finalize(cfg, e["content_path"], e["sha256"], [r.wikilink], skip_sync=True)

    # Le contenu dérivé change
    notes = cfg.RAW / "notes"
    (notes / "note-test.md").write_text(
        "---\ntitle: \"note-test\"\ntype: note\ncreated: 2026-06-16\ntags: [clippings]\n---\n\nv2 modifié\n",
        encoding="utf-8",
    )
    again = wiki_ingest.cmd_inventory(cfg, None, skip_sync=True, dry_run=False)
    paths = [w["content_path"] for w in again["worklist"]]
    assert "raw/notes/note-test.md" in paths
    # Toujours pas de `.src.md`
    assert "raw/notes/note-test.src.md" not in paths
