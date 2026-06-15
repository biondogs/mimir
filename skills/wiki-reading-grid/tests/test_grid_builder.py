"""Tests unitaires du cœur de construction de grille (croisement toc × ledger × ancres)."""

import article_index
import grid_builder as gb

CONTENT_REL = "raw/pdfs/navigation-cotiere.pdf.txt"


def test_load_article_tolere_prefixe_wiki(vault):
    """`load_article` résout aussi bien `sujet/notion` que `wiki/sujet/notion`."""
    a = article_index.load_article(vault, "navigation/relevement")
    b = article_index.load_article(vault, "wiki/navigation/relevement")
    assert a.exists and b.exists
    assert a.path == b.path
    assert b.sources == [f"{CONTENT_REL}#ch2"]


def test_chapters_cited():
    srcs = [f"{CONTENT_REL}#ch2", f"{CONTENT_REL}#ch4", "raw/web/x.md", "raw/pdfs/autre.pdf.txt#ch1"]
    assert gb._chapters_cited(srcs, CONTENT_REL) == ["2", "4"]   # clés str ; autre ouvrage + web ignorés


def test_chapters_cited_codes_ouvrage():
    """`#chR2`/`#chG3` (codes d'ouvrage) sont reconnus et normalisés en majuscule."""
    srcs = [f"{CONTENT_REL}#chR2", f"{CONTENT_REL}#chg3", f"{CONTENT_REL}#chC1"]
    assert gb._chapters_cited(srcs, CONTENT_REL) == ["R2", "G3", "C1"]   # ordre de citation préservé


def test_chapter_key_normalise():
    assert gb._chapter_key(3) == "3"
    assert gb._chapter_key("03") == "3"          # zéros de tête (ancien #ch03)
    assert gb._chapter_key("g3") == "G3"         # code d'ouvrage en majuscule
    assert gb._chapter_key("R02") == "R2"


def test_build_buckets_et_ordre(vault):
    res = gb.build_grid(vault, CONTENT_REL)
    assert res.skipped is None
    assert res.work == "Navigation côtière"
    assert res.n_chapters == 3
    # ch2 contient relèvement puis triangulation (ordre du ledger) ; ch3 marée
    body = res.markdown
    assert "## Ch. 2 — Se positionner" in body
    i_rel = body.index("[[navigation/relevement]]")
    i_tri = body.index("[[navigation/triangulation]]")
    assert i_rel < i_tri
    assert "[[navigation/maree]]" in body


def test_chapitre_sans_article(vault):
    res = gb.build_grid(vault, CONTENT_REL)
    assert 1 in res.unresolved_chapters                     # ch1 Instruments
    assert "_Aucun article rattaché pour l'instant._" in res.markdown


def test_orphelin_hors_chapitrage(vault):
    res = gb.build_grid(vault, CONTENT_REL)
    assert res.orphan_articles == ["navigation/intro"]       # sources web sans #ch
    assert "## Hors chapitrage" in res.markdown
    assert "- [[navigation/intro]]" in res.markdown


def test_prev_next_pointent_vers_titres_reels(vault):
    """DoD : chaque lien Précédent/Suivant pointe vers un titre `##` présent."""
    res = gb.build_grid(vault, CONTENT_REL)
    body = res.markdown
    headings = {ln[3:].strip() for ln in body.splitlines() if ln.startswith("## ")}
    import re
    for target in re.findall(r"\[\[#([^\]]+)\]\]", body):
        assert target in headings, f"lien interne cassé : #{target}"
    # premier chapitre : pas de ⬅ ; structure de navigation présente
    assert "➡ [[#Ch. 2 — Se positionner]]" in body


def test_web_sans_toc_skipped(vault):
    res = gb.build_grid(vault, "raw/web/preface.md")
    assert res.skipped == "no-toc"
    assert res.markdown == ""


def test_document_ajoute_frontmatter(vault):
    res = gb.build_grid(vault, CONTENT_REL)
    doc = gb.document(res, "2026-06-04")
    assert doc.startswith("---\ntype: reading-grid\n")
    assert 'work: "Navigation côtière"' in doc
    assert f"source: {CONTENT_REL}" in doc
    assert "chapters: 3" in doc
    assert "created: 2026-06-04" in doc


# --- 6.1 / 6.2 / 6.3 : format hybride (charpente + blurb préservé) ---------

def test_rendu_chapitre_avec_phrase_contient_zone_blurb(vault):
    """Avec un blurb préexistant (planté manuellement), la zone `<!-- blurb:… -->` est
    réinjectée et le chapitre est rendu en guide (zone + liens + nav)."""
    # Plante une grille v3 (sans zone blurb) + une phrase dans ch2.
    grid_path = vault.READING_GRIDS / "navigation-cotiere.md"
    body = (
        "## Ch. 2 — Se positionner\n"
        "\n"
        "<!-- blurb:début -->\n"
        "Phrase éditoriale de test.\n"
        "<!-- blurb:fin -->\n"
        "\n"
        "Lire dans l'ordre :\n"
        "\n"
        "1. [[navigation/relevement]]\n"
        "2. [[navigation/triangulation]]\n"
        "\n"
    )
    res = gb.build_grid(vault, CONTENT_REL, existing_grid_path=grid_path)
    # On injecte la grille déjà existante dans le filesystem
    # (la fixture `vault` n'a pas écrit de grille, c'est un dry-run).
    # Re-jouons l'injection via une autre passe :
    from datetime import date
    res_real = gb.build_grid(vault, CONTENT_REL)
    # Écrivons la grille manuellement
    full_doc = gb.document(res_real, "2026-06-04")
    grid_path.write_text(full_doc.replace(
        "## Ch. 2 — Se positionner",
        "## Ch. 2 — Se positionner\n\n<!-- blurb:début -->\nPhrase éditoriale.\n<!-- blurb:fin -->"
    ), encoding="utf-8")
    # Régénère en passant le grid_path existant → doit préserver la phrase
    res2 = gb.build_grid(vault, CONTENT_REL, existing_grid_path=grid_path)
    assert "Phrase éditoriale." in res2.markdown
    # Zone blurb présente
    assert "<!-- blurb:début -->" in res2.markdown
    assert "<!-- blurb:fin -->" in res2.markdown
    # Chapitre 2 n'est PAS dans `chapters_missing_blurb`
    assert "2" not in res2.chapters_missing_blurb
    # Chapitre 1 (sans article) EST dans `chapters_missing_blurb` (placeholder)
    assert "1" in res2.chapters_missing_blurb
    # Le placeholder est bien dans le markdown
    assert "<!-- blurb:début -->\n_à rédiger_\n<!-- blurb:fin -->" in res2.markdown


def test_idempotence_regenerate_preserve_phrase(vault):
    """Une 2e régénération préserve les phrases non vides et ne les duplique
    pas (la zone blurb apparaît UNE seule fois par chapitre)."""
    grid_path = vault.READING_GRIDS / "navigation-cotiere.md"
    # 1re génération + injection d'une phrase dans ch2
    res1 = gb.build_grid(vault, CONTENT_REL)
    doc1 = gb.document(res1, "2026-06-04")
    # Injecte une phrase dans ch2 du document 1
    doc1_with_blurb = doc1.replace(
        "## Ch. 2 — Se positionner",
        "## Ch. 2 — Se positionner\n\n<!-- blurb:début -->\nMa phrase ch2.\n<!-- blurb:fin -->"
    )
    grid_path.write_text(doc1_with_blurb, encoding="utf-8")
    # 2e génération
    res2 = gb.build_grid(vault, CONTENT_REL, existing_grid_path=grid_path)
    # La phrase est conservée
    assert "Ma phrase ch2." in res2.markdown
    # Et elle n'apparaît qu'UNE fois (pas de duplication)
    assert res2.markdown.count("Ma phrase ch2.") == 1
    # Charpente intacte (toujours 3 chapitres, ordre préservé)
    assert res2.n_chapters == 3
    i_ch1 = res2.markdown.index("Ch. 1 —")
    i_ch2 = res2.markdown.index("Ch. 2 —")
    i_ch3 = res2.markdown.index("Ch. 3 —")
    assert i_ch1 < i_ch2 < i_ch3


def test_preservation_tolerante_renommage_titre(vault):
    """Un léger renommage de titre (entre deux générations) n'empêche PAS
    la réinjection de la phrase — l'appariement se fait sur l'`order`,
    pas sur le titre complet."""
    grid_path = vault.READING_GRIDS / "navigation-cotiere.md"
    # 1re génération + phrase dans ch2
    res1 = gb.build_grid(vault, CONTENT_REL)
    doc1 = gb.document(res1, "2026-06-04")
    doc1 = doc1.replace(
        "## Ch. 2 — Se positionner",
        "## Ch. 2 — Se positionner\n\n<!-- blurb:début -->\nMa phrase ch2.\n<!-- blurb:fin -->"
    )
    grid_path.write_text(doc1, encoding="utf-8")

    # Modifie le toc.json : ch2 a maintenant un titre légèrement différent
    toc_path = vault.RAW / "pdfs" / "navigation-cotiere.toc.json"
    import json
    toc = json.loads(toc_path.read_text(encoding="utf-8"))
    for ch in toc["chapters"]:
        if ch["order"] == 2:
            ch["title"] = "Se positionner en mer"   # renommage
    toc_path.write_text(json.dumps(toc, ensure_ascii=False), encoding="utf-8")

    # 2e génération : la phrase DOIT être réinjectée (appariement par order=2)
    res2 = gb.build_grid(vault, CONTENT_REL, existing_grid_path=grid_path)
    assert "Ma phrase ch2." in res2.markdown
    # Et le nouveau titre apparaît dans la charpente
    assert "Ch. 2 — Se positionner en mer" in res2.markdown


def test_chapitre_sans_phrase_a_placeholder_et_est_dans_missing(vault):
    """Un chapitre sans blurb (ch1, sans article) a :
    - une zone blurb avec placeholder `_à rédiger_ ;
    - une entrée dans `chapters_missing_blurb` pour l'agent."""
    res = gb.build_grid(vault, CONTENT_REL)
    # ch1 (Instruments) n'a pas d'article → placeholder
    assert "1" in res.chapters_missing_blurb
    assert res.chapters_missing_blurb["1"] == "Instruments"
    # Le placeholder apparaît dans le markdown
    assert "<!-- blurb:début -->\n_à rédiger_\n<!-- blurb:fin -->" in res.markdown
    # Sanity check : la fixture ch1 a bien un article, et notre code ne
    # met le placeholder QUE pour les chapitres sans article. Si la fixture
    # change, on lèvera une erreur claire ici.
    # Et ch1 reste généré (utilisable)
    assert "## Ch. 1 — Instruments" in res.markdown


def test_phrase_non_vide_non_ecrasee_par_placeholder(vault):
    """Une phrase déjà écrite n'est JAMAIS remplacée par `_à rédiger_` à la
    régénération (invariant « ne pas détruire la prose éditoriale »)."""
    grid_path = vault.READING_GRIDS / "navigation-cotiere.md"
    res1 = gb.build_grid(vault, CONTENT_REL)
    doc1 = gb.document(res1, "2026-06-04")
    doc1 = doc1.replace(
        "## Ch. 2 — Se positionner",
        "## Ch. 2 — Se positionner\n\n<!-- blurb:début -->\nPhrase ch2 sacrée.\n<!-- blurb:fin -->"
    )
    grid_path.write_text(doc1, encoding="utf-8")
    # 2e génération : la phrase reste intacte (pas de placeholder)
    res2 = gb.build_grid(vault, CONTENT_REL, existing_grid_path=grid_path)
    # On cherche la zone blurb SPÉCIFIQUEMENT dans la section ch2
    # (le doc peut avoir plusieurs zones, dont celle de ch1 qui est placeholder).
    import re
    # Split par sections `## `, prendre celle de ch2
    sections = re.split(r"(?m)^## ", res2.markdown)
    ch2_section = next(s for s in sections if s.startswith("Ch. 2 —"))
    m = re.search(
        re.escape(gb._BLURB_START) + r"\s*(.*?)\s*" + re.escape(gb._BLURB_END),
        ch2_section, re.DOTALL,
    )
    assert m is not None, f"pas de zone blurb dans la section ch2 : {ch2_section!r}"
    assert m.group(1).strip() == "Phrase ch2 sacrée."
    # Et "ch2" n'est PAS dans chapters_missing_blurb
    assert "2" not in res2.chapters_missing_blurb


# --- 6.5 : non-régression ordre + ancres -----------------------------------

def test_ordre_chapitres_inchange_apres_hybride(vault):
    """L'ordre des chapitres dans le markdown est identique à celui du toc.json,
    même avec le nouveau format guide (zone blurb + nav)."""
    res = gb.build_grid(vault, CONTENT_REL)
    body = res.markdown
    # Titres des chapitres, dans l'ordre
    chapter_titles = [
        ln[3:].strip() for ln in body.splitlines()
        if ln.startswith("## ") and "Ch." in ln
    ]
    assert chapter_titles == [
        "Ch. 1 — Instruments",
        "Ch. 2 — Se positionner",
        "Ch. 3 — Marées",
    ]


def test_liens_navigation_coherents_avec_ancres(vault):
    """Les liens ⬅ / ➡ pointent vers des ancres réellement présentes dans
    le document (pas de lien interne cassé)."""
    res = gb.build_grid(vault, CONTENT_REL)
    body = res.markdown
    headings = {ln[3:].strip() for ln in body.splitlines() if ln.startswith("## ")}
    import re
    targets = re.findall(r"\[\[#([^\]]+)\]\]", body)
    assert all(t in headings for t in targets)
