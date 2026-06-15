"""Tests de l'orchestrateur wiki-reading-grid (écriture, _status.md, idempotence)."""

import wiki_reading_grid as wrg
from frontmatter import parse_frontmatter

CONTENT_REL = "raw/pdfs/navigation-cotiere.pdf.txt"


def test_generate_ecrit_grille_et_statut(vault):
    out = wrg.cmd_generate(vault, CONTENT_REL, skip_sync=True)
    assert out.status == "généré"
    grid = vault.READING_GRIDS / "navigation-cotiere.md"
    assert grid.is_file()
    front, _ = parse_frontmatter(grid.read_text(encoding="utf-8"))
    assert front["type"] == "reading-grid"
    assert front["source"] == CONTENT_REL

    # colonne Grille de _status.md mise à jour
    import status_table
    rows = status_table.load_status(vault.RAW / "pdfs" / "_status.md")
    row = next(r for r in rows if r.fichier == "navigation-cotiere.pdf.txt")
    assert row.grille == "[[reading-grids/navigation-cotiere]]"
    assert row.statut == "compilé"                          # autres colonnes préservées


def test_slug_depuis_titre(vault):
    out = wrg.cmd_generate(vault, CONTENT_REL, skip_sync=True)
    # "Navigation côtière" -> navigation-cotiere
    assert out.grid_path == "reading-grids/navigation-cotiere.md"


def test_idempotence_created_preserve(vault, monkeypatch):
    wrg.cmd_generate(vault, CONTENT_REL, skip_sync=True)
    grid = vault.READING_GRIDS / "navigation-cotiere.md"
    front1, _ = parse_frontmatter(grid.read_text(encoding="utf-8"))

    # 2e génération un autre jour : created conservé, pas de doublon de fichier
    import datetime
    monkeypatch.setattr(wrg, "_today", lambda: datetime.date(2027, 1, 1))
    wrg.cmd_generate(vault, CONTENT_REL, skip_sync=True)
    front2, _ = parse_frontmatter(grid.read_text(encoding="utf-8"))
    assert front2["created"] == front1["created"]
    assert len(list(vault.READING_GRIDS.glob("*.md"))) == 1


def test_dry_run_n_ecrit_rien(vault):
    out = wrg.cmd_generate(vault, CONTENT_REL, skip_sync=True, dry_run=True)
    assert out.status == "généré" and "dry-run" in out.detail
    assert not (vault.READING_GRIDS / "navigation-cotiere.md").exists()
    import status_table
    rows = status_table.load_status(vault.RAW / "pdfs" / "_status.md")
    assert next(r for r in rows if r.fichier == "navigation-cotiere.pdf.txt").grille == "—"


def test_web_ignore(vault):
    (vault.RAW / "web").mkdir(parents=True, exist_ok=True)
    (vault.RAW / "web" / "preface.md").write_text("# Préface\n", encoding="utf-8")
    out = wrg.cmd_generate(vault, "raw/web/preface.md", skip_sync=True)
    assert out.status == "ignoré"
    assert not list(vault.READING_GRIDS.glob("*.md"))


def test_generate_all(vault):
    outs = wrg.cmd_generate_all(vault, skip_sync=True)
    assert [o.status for o in outs] == ["généré"]
    assert (vault.READING_GRIDS / "navigation-cotiere.md").is_file()


def test_collision_de_slug(vault):
    # un 2e ouvrage de même titre -> slug suffixé -2
    pdfs = vault.RAW / "pdfs"
    (pdfs / "nav2.pdf.txt").write_text("Autre.\n", encoding="utf-8")
    import json
    (pdfs / "nav2.toc.json").write_text(json.dumps(
        {"title": "Navigation côtière", "chapters": [{"order": 1, "title": "X"}]}, ensure_ascii=False), encoding="utf-8")

    wrg.cmd_generate(vault, CONTENT_REL, skip_sync=True)
    out2 = wrg.cmd_generate(vault, "raw/pdfs/nav2.pdf.txt", skip_sync=True)
    assert out2.grid_path == "reading-grids/navigation-cotiere-2.md"


def test_skip_sync(vault, monkeypatch):
    calls = {"pull": 0, "push": 0}
    monkeypatch.setattr(wrg.sync, "pull", lambda c: calls.__setitem__("pull", calls["pull"] + 1) or 0)
    monkeypatch.setattr(wrg.sync, "push", lambda c: calls.__setitem__("push", calls["push"] + 1) or 0)
    wrg.cmd_generate(vault, CONTENT_REL, skip_sync=True)
    assert calls == {"pull": 0, "push": 0}
    wrg.cmd_generate(vault, CONTENT_REL, skip_sync=False)
    assert calls == {"pull": 1, "push": 1}


# --- 3.2 : Smoke test du fix ENFSI — block-list YAML → articles liés --------
# Reproduction du bug #3 (REX ENFSI 2026-06-08) : 16 articles avec un
# `sources:` au format block-list (forme YAML standard produite par les LLM)
# produisaient une grille vide. Le fix du parseur de frontmatter permet
# désormais de les rattacher à leurs chapitres.

def test_grille_lie_les_articles_avec_sources_block_list(cfg):
    """Frontmatter `sources:` au format block-list → les articles sont liés."""
    import json

    import grid_builder as gb
    import ledger as ledger_mod
    import status_table
    from frontmatter import parse_frontmatter

    pdfs = cfg.RAW / "pdfs"
    pdfs.mkdir(parents=True, exist_ok=True)
    (pdfs / "nav.pdf.txt").write_text("Contenu.\n", encoding="utf-8")
    toc = {
        "title": "Navigation",
        "source": "raw/pdfs/nav.pdf",
        "pages": 100,
        "ocr": False,
        "chapters": [
            {"order": 1, "title": "Instruments", "page_start": 1, "page_end": 20},
            {"order": 2, "title": "Positionnement", "page_start": 21, "page_end": 50},
        ],
    }
    (pdfs / "nav.toc.json").write_text(json.dumps(toc, ensure_ascii=False), encoding="utf-8")

    nav = cfg.WIKI / "navigation"
    nav.mkdir(parents=True, exist_ok=True)
    # Forme BLOCK-LIST dans le frontmatter (c'est précisément le bug d'origine)
    block_article = (
        "---\n"
        'title: "Relèvement"\n'
        "subject: navigation\n"
        "tags: [navigation]\n"
        "sources:\n"
        "  - raw/pdfs/nav.pdf.txt#ch2\n"
        "created: 2026-06-08\n"
        "updated: 2026-06-08\n"
        "---\n\n"
        "# Relèvement\n\n> Mesure.\n\n## Définition\nTexte.\n"
    )
    nav.joinpath("relevement.md").write_text(block_article, encoding="utf-8")

    rows = [status_table.StatusRow(
        fichier="nav.pdf.txt", sha256="sha-block", statut="compilé",
        articles="[[navigation/relevement]]", maj="2026-06-08")]
    status_table.save_status(pdfs / "_status.md", "pdfs", rows, work_root=cfg.work_root)

    led = {}
    ledger_mod.record(led, "raw/pdfs/nav.pdf.txt", "sha-block",
                      ["navigation/relevement"], updated="2026-06-08")
    ledger_mod.save_ledger(cfg.LEDGER, led, work_root=cfg.work_root)

    # Sanity : parse_frontmatter AVEC ce change sait lire le block-list
    front, _ = parse_frontmatter(block_article)
    assert front["sources"] == ["raw/pdfs/nav.pdf.txt#ch2"]

    # Et la grille générée lie bien l'article à son chapitre
    res = gb.build_grid(cfg, "raw/pdfs/nav.pdf.txt")
    assert res.skipped is None
    assert res.linked_articles == ["navigation/relevement"]
    body = res.markdown
    assert "## Ch. 2 — Positionnement" in body
    assert "[[navigation/relevement]]" in body

    # Round-trip orchestrateur
    out = wrg.cmd_generate(cfg, "raw/pdfs/nav.pdf.txt", skip_sync=True)
    assert out.status == "généré"
    assert out.result is not None
    assert out.result.linked_articles == ["navigation/relevement"]
    assert out.grid_path.startswith("reading-grids/")
