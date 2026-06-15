"""Grilles de lecture best-effort pour les sources web (sans `toc.json`).

Couvre l'OpenSpec `add-web-reading-grids` :
- 3.1 Web avec `_status.md` + articles → grille listant les articles.
- 3.2 Web sans `_status.md` → `skipped: no-toc`.
- 3.3 `generate-all` n'énumère toujours pas le web.
- 3.4 Colonne « Grille » mise à jour pour une row web préfixée.
- 3.5 Non-régression PDF/EPUB (chemin `toc.json` prioritaire).
"""

import json

import grid_builder as gb
import ledger as ledger_mod
import status_table
import wiki_reading_grid as wrg
from frontmatter import parse_frontmatter


CONTENT_REL = "raw/web/fooclipping-20260608.md"
PREFIXED = "raw/web/fooclipping-20260608.md"           # forme stockée dans _status.md (préfixée)


def _article(subject, notion, summary, sources):
    src = ", ".join(f'"{s}"' for s in sources)
    return (
        "---\n"
        f'title: "{notion}"\n'
        f"subject: {subject}\n"
        "tags: [web]\n"
        f"sources: [{src}]\n"
        "created: 2026-06-08\n"
        "updated: 2026-06-08\n"
        "---\n\n"
        f"# {notion}\n\n> {summary}\n\n## Définition\nTexte.\n"
    )


def _source_md(title: str) -> str:
    return (
        "---\n"
        f'title: "{title}"\n'
        "type: web\n"
        "source: https://example.com/fooclipping\n"
        "created: 2026-06-08\n"
        "---\n\n"
        "# Corps de la source web\n"
    )


def _web_vault(cfg, *, with_status: bool = True, with_articles: bool = True, prefixed_row: bool = True):
    """Vault web : source + _status.md (row préfixée ou basename) + articles dans le ledger."""
    web = cfg.RAW / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "fooclipping-20260608.md").write_text(_source_md("Clipping Foo"), encoding="utf-8")

    if with_status:
        fichier = PREFIXED if prefixed_row else "fooclipping-20260608.md"
        rows = [status_table.StatusRow(
            fichier=fichier, sha256="def456", statut="compilé",
            articles="[[navigation/a]]", maj="2026-06-08")]
        status_table.save_status(web / "_status.md", "web", rows, work_root=cfg.work_root)

    if with_articles:
        nav = cfg.WIKI / "navigation"
        nav.mkdir(parents=True, exist_ok=True)
        nav.joinpath("a.md").write_text(
            _article("navigation", "Article A", "Résumé A.", [CONTENT_REL]), encoding="utf-8")
        nav.joinpath("b.md").write_text(
            _article("navigation", "Article B", "Résumé B.", [CONTENT_REL]), encoding="utf-8")
        led = {}
        ledger_mod.record(
            led, CONTENT_REL, "def456",
            ["navigation/a", "navigation/b"], updated="2026-06-08")
        ledger_mod.save_ledger(cfg.LEDGER, led, work_root=cfg.work_root)
    return cfg


# --- 3.1 : Web avec _status.md + articles → grille listant les articles --------

def test_web_avec_status_et_articles_produit_grille(cfg):
    """Best-effort : source web + statut + ledger → grille non vide, articles listés."""
    vault = _web_vault(cfg, with_status=True, with_articles=True, prefixed_row=True)
    res = gb.build_grid(vault, CONTENT_REL)
    assert res.skipped is None
    assert res.work == "Clipping Foo"
    assert res.n_chapters == 0
    assert res.linked_articles == ["navigation/a", "navigation/b"]   # ordre du ledger préservé
    # Pas de toc : omise du frontmatter
    doc = gb.document(res, "2026-06-08")
    front, body = parse_frontmatter(doc)
    assert front["type"] == "reading-grid"
    assert front["source"] == CONTENT_REL
    assert str(front["chapters"]) == "0"
    assert "toc" not in front
    # Corps : titre, section « Lectures » (sujet inconnu), liens dans l'ordre
    assert "## Lectures" in body
    assert "1. [[navigation/a]]" in body
    assert "2. [[navigation/b]]" in body
    assert body.index("[[navigation/a]]") < body.index("[[navigation/b]]")


def test_web_sujet_via_sous_dossier(cfg):
    """`raw/web/<sujet>/foo.md` → section de grille `## <sujet>` (sous-dossier préféré)."""
    web = cfg.RAW / "web"
    web.mkdir(parents=True, exist_ok=True)
    subj = web / "navigation"
    subj.mkdir(parents=True, exist_ok=True)
    rel = "raw/web/navigation/fooclipping-20260608.md"
    (subj / "fooclipping-20260608.md").write_text(_source_md("Clipping Nav"), encoding="utf-8")
    # statut + ledger pour ce chemin
    rows = [status_table.StatusRow(
        fichier=rel, sha256="def456", statut="compilé",
        articles="[[x]]", maj="2026-06-08")]
    status_table.save_status(web / "_status.md", "web", rows, work_root=cfg.work_root)
    led = {}
    ledger_mod.record(led, rel, "def456", ["navigation/a"], updated="2026-06-08")
    ledger_mod.save_ledger(cfg.LEDGER, led, work_root=cfg.work_root)

    res = gb.build_grid(cfg, rel)
    assert res.skipped is None
    assert "## navigation" in res.markdown
    assert "## Lectures" not in res.markdown


def test_web_sujet_via_prefixe_double_dash(cfg):
    """`raw/web/<sujet>--foo.md` → section `## <sujet>` quand pas de sous-dossier."""
    web = cfg.RAW / "web"
    web.mkdir(parents=True, exist_ok=True)
    rel = "raw/web/navigation--fooclipping-20260608.md"
    (web / "navigation--fooclipping-20260608.md").write_text(
        _source_md("Clipping Pref"), encoding="utf-8")
    rows = [status_table.StatusRow(
        fichier=rel, sha256="def456", statut="compilé",
        articles="[[x]]", maj="2026-06-08")]
    status_table.save_status(web / "_status.md", "web", rows, work_root=cfg.work_root)
    led = {}
    ledger_mod.record(led, rel, "def456", ["navigation/a"], updated="2026-06-08")
    ledger_mod.save_ledger(cfg.LEDGER, led, work_root=cfg.work_root)

    res = gb.build_grid(cfg, rel)
    assert res.skipped is None
    assert "## navigation" in res.markdown
    assert "## Lectures" not in res.markdown


# --- 3.2 : Web sans _status.md OU sans articles → skipped: no-toc ---------------

def test_web_sans_status_md_skipped(cfg):
    """Pas de row dans `_status.md` → `skipped: no-toc` (aucune grille vide)."""
    web = cfg.RAW / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "fooclipping-20260608.md").write_text(_source_md("Clipping Foo"), encoding="utf-8")
    # Articles dans le ledger, mais pas de _status.md pour le web
    led = {}
    ledger_mod.record(led, CONTENT_REL, "def456", ["navigation/a"], updated="2026-06-08")
    ledger_mod.save_ledger(cfg.LEDGER, led, work_root=cfg.work_root)

    res = gb.build_grid(cfg, CONTENT_REL)
    assert res.skipped == "no-toc"
    assert res.markdown == ""


def test_web_sans_articles_dans_ledger_skipped(cfg):
    """_status.md présent mais ledger vide → `skipped: no-toc`."""
    _web_vault(cfg, with_status=True, with_articles=False, prefixed_row=True)
    res = gb.build_grid(cfg, CONTENT_REL)
    assert res.skipped == "no-toc"


def test_web_aucune_donnee_rien_ecrit(cfg):
    """Orchestrateur : web sans statut → `ignoré`, aucun fichier produit."""
    web = cfg.RAW / "web"
    web.mkdir(parents=True, exist_ok=True)
    (web / "preface.md").write_text("# Préface\n", encoding="utf-8")
    out = wrg.cmd_generate(cfg, "raw/web/preface.md", skip_sync=True)
    assert out.status == "ignoré"
    assert not list(cfg.READING_GRIDS.glob("*.md"))


# --- 3.3 : generate-all n'énumère toujours pas le web --------------------------

def test_generate_all_ignore_le_web(cfg):
    """Même avec une web source « compilée », `generate-all` ne la prend pas."""
    vault = _web_vault(cfg, with_status=True, with_articles=True, prefixed_row=True)
    outs = wrg.cmd_generate_all(vault, skip_sync=True)
    # _all_content_with_toc ne liste que pdfs/epubs : web absent, rien à générer
    assert outs == []
    assert not list(vault.READING_GRIDS.glob("*.md"))


# --- 3.4 : Colonne « Grille » mise à jour pour une row web préfixée -----------

def test_status_grille_pour_row_web_prefixee(cfg):
    """Row `fichier = raw/web/foo.md` (préfixée) → upsert OK, pas d'avertissement."""
    vault = _web_vault(cfg, with_status=True, with_articles=True, prefixed_row=True)
    out = wrg.cmd_generate(vault, CONTENT_REL, skip_sync=True)
    assert out.status == "généré"
    # Pas d'avertissement de type « aucune ligne _status.md »
    assert "⚠" not in out.detail

    # La row préfixée a bien sa colonne « Grille » mise à jour
    rows = status_table.load_status(vault.RAW / "web" / "_status.md")
    row = next(r for r in rows if r.fichier == PREFIXED)
    assert row.grille.startswith("[[reading-grids/")
    assert row.maj                    # rafraîchie


def test_status_grille_pour_row_web_basename(cfg):
    """Row `fichier = foo.md` (basename, forme pdfs/epubs) → upsert OK aussi."""
    vault = _web_vault(cfg, with_status=True, with_articles=True, prefixed_row=False)
    out = wrg.cmd_generate(vault, CONTENT_REL, skip_sync=True)
    assert out.status == "généré"
    rows = status_table.load_status(vault.RAW / "web" / "_status.md")
    row = next(r for r in rows if r.fichier == "fooclipping-20260608.md")
    assert row.grille.startswith("[[reading-grids/")


# --- 3.5 : Non-régression PDF/EPUB (chemin `toc.json` prioritaire) -----------

def test_pdf_avec_toc_reste_prioritaire_sur_branche_web(vault):
    """Un PDF continue d'utiliser son `toc.json` (la branche web n'est pas empruntée)."""
    res = gb.build_grid(vault, "raw/pdfs/navigation-cotiere.pdf.txt")
    assert res.skipped is None
    assert res.n_chapters == 3                       # 3 chapitres depuis le toc.json
    assert "[[navigation/relevement]]" in res.markdown
    # Le toc.json est référencé dans le frontmatter
    doc = gb.document(res, "2026-06-08")
    front, _ = parse_frontmatter(doc)
    assert front["toc"] == "raw/pdfs/navigation-cotiere.toc.json"


def test_epub_avec_toc_reste_prioritaire_sur_branche_web(cfg):
    """Un EPUB suit le même chemin (toc.json prioritaire, branche web jamais empruntée)."""
    epubs = cfg.RAW / "epubs"
    epubs.mkdir(parents=True, exist_ok=True)
    (epubs / "essai.epub.txt").write_text("Contenu.\n", encoding="utf-8")
    toc = {
        "title": "Essai",
        "source": "raw/epubs/essai.epub",
        "chapters": [{"order": 1, "title": "Chapitre unique", "page_start": 1, "page_end": 50}],
    }
    (epubs / "essai.toc.json").write_text(json.dumps(toc, ensure_ascii=False), encoding="utf-8")
    rows = [status_table.StatusRow(
        fichier="essai.epub.txt", sha256="ghi789", statut="compilé",
        articles="", maj="2026-06-08")]
    status_table.save_status(epubs / "_status.md", "epubs", rows, work_root=cfg.work_root)

    res = gb.build_grid(cfg, "raw/epubs/essai.epub.txt")
    assert res.skipped is None
    assert res.n_chapters == 1
    assert "## Ch. 1 — Chapitre unique" in res.markdown
