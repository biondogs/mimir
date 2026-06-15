"""Tests du parseur de frontmatter partagé (promu depuis article_writer, P3)."""

import frontmatter as fm


_ARTICLE = """---
title: "Relèvement"
subject: navigation
tags: [navigation, technique]
sources: ["raw/pdfs/nav.pdf.txt#ch3", "raw/web/amers.md"]
created: 2026-06-04
updated: 2026-06-04
---

# Relèvement

> Mesure de l'angle vers un amer.

## Définition
Texte.
"""


def test_parse_scalaires_et_listes():
    front, body = fm.parse_frontmatter(_ARTICLE)
    assert front["title"] == "Relèvement"          # guillemets retirés
    assert front["subject"] == "navigation"
    assert front["tags"] == ["navigation", "technique"]
    assert front["sources"] == ["raw/pdfs/nav.pdf.txt#ch3", "raw/web/amers.md"]
    assert front["created"] == "2026-06-04"
    assert body.startswith("# Relèvement")          # ligne vide initiale consommée


def test_sans_frontmatter():
    front, body = fm.parse_frontmatter("# Titre\n\ntexte\n")
    assert front == {}
    assert body == "# Titre\n\ntexte\n"


def test_parse_list_tolerant():
    assert fm.parse_list('[a, "b c", \'d\']') == ["a", "b c", "d"]
    assert fm.parse_list("[]") == []
    assert fm.parse_list("a, b") == ["a", "b"]       # crochets optionnels


def test_list_keys_personnalisables():
    text = "---\nfoo: [x, y]\n---\nbody\n"
    front, _ = fm.parse_frontmatter(text, list_keys=("foo",))
    assert front["foo"] == ["x", "y"]
    front2, _ = fm.parse_frontmatter(text)            # foo non listé -> scalaire brut
    assert front2["foo"] == "[x, y]"


# --- block-list YAML : forme standard produite par les LLM, ignorée
#     silencieusement avant ce change (REX ENFSI 2026-06-08 bug #3) ---------

def test_block_list_sources_est_parse():
    """`sources:` block-list multi-lignes → liste non vide (cœur du fix)."""
    text = (
        "---\n"
        "title: Article\n"
        "sources:\n"
        "  - raw/pdfs/foo.pdf.txt#chE1\n"
        "  - raw/pdfs/foo.pdf.txt#chE2\n"
        "---\n\n"
        "Corps.\n"
    )
    front, _ = fm.parse_frontmatter(text)
    assert front["sources"] == [
        "raw/pdfs/foo.pdf.txt#chE1",
        "raw/pdfs/foo.pdf.txt#chE2",
    ]


def test_block_list_tags_avec_indentation_variable():
    """Tolère 2 espaces, 4 espaces, tabulations."""
    text = (
        "---\n"
        "tags:\n"
        "    - alpha\n"
        "  - beta\n"
        "\t- gamma\n"
        "---\n"
    )
    front, _ = fm.parse_frontmatter(text)
    assert front["tags"] == ["alpha", "beta", "gamma"]


def test_block_list_items_avec_guillemets_et_espaces():
    """Chaque item est trimmé (guillemets + whitespace), parité avec `parse_list`."""
    text = (
        "---\n"
        "sources:\n"
        "  - 'raw/pdfs/foo.pdf.txt#ch1'\n"
        '  - "raw/pdfs/foo.pdf.txt#ch2"\n'
        "  -   raw/pdfs/foo.pdf.txt#ch3   \n"
        "---\n"
    )
    front, _ = fm.parse_frontmatter(text)
    assert front["sources"] == [
        "raw/pdfs/foo.pdf.txt#ch1",
        "raw/pdfs/foo.pdf.txt#ch2",
        "raw/pdfs/foo.pdf.txt#ch3",
    ]


def test_block_list_se_termine_a_la_prochaine_cle():
    """Le block-list s'arrête à la première ligne non indentée (clé suivante)."""
    text = (
        "---\n"
        "sources:\n"
        "  - a\n"
        "  - b\n"
        "type: web\n"
        "created: 2026-06-08\n"
        "---\n"
    )
    front, _ = fm.parse_frontmatter(text)
    assert front["sources"] == ["a", "b"]            # pas de fuite de `type`
    assert front["type"] == "web"                    # scalaire correctement lu
    assert front["created"] == "2026-06-08"


def test_block_list_avec_ligne_vide_au_milieu():
    """Ligne(s) vide(s) tolérée(s) au sein d'un block-list."""
    text = (
        "---\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "  - b\n"
        "\n"
        "type: web\n"
        "---\n"
    )
    front, _ = fm.parse_frontmatter(text)
    assert front["sources"] == ["a", "b"]
    assert front["type"] == "web"


def test_block_list_vide_devient_liste_vide():
    """`sources:` sans item et sans valeur inline → `[]` (jamais `[""]` ni crash)."""
    text = "---\nsources:\ntype: web\n---\n"
    front, _ = fm.parse_frontmatter(text)
    assert front["sources"] == []
    assert front["type"] == "web"


def test_block_list_vide_suivi_de_close():
    """`sources:` immédiatement suivi de `---` → `[]`."""
    text = "---\nsources:\n---\nbody\n"
    front, body = fm.parse_frontmatter(text)
    assert front["sources"] == []
    assert body.startswith("body")


# --- non-régression : flow-list et scalaire inchangés -----------------------

def test_non_regression_flow_list_avec_guillemets():
    """Le flow-list est toujours parsé comme avant (scénario du spec)."""
    text = '---\nsources: ["raw/pdfs/foo.pdf.txt#ch1", \'b\']\n---\n'
    front, _ = fm.parse_frontmatter(text)
    assert front["sources"] == ["raw/pdfs/foo.pdf.txt#ch1", "b"]


def test_non_regression_flow_list_sans_crochets():
    """`key: a, b` (flow-list sans crochets) reste parsé comme avant."""
    text = "---\ntags: a, b, c\n---\n"
    front, _ = fm.parse_frontmatter(text)
    assert front["tags"] == ["a", "b", "c"]


def test_non_regression_scalaire_apres_block_list():
    """Les scalaires (`title:`, `created:`...) restent lus normalement."""
    text = (
        "---\n"
        'title: "Mon Titre"\n'
        "sources:\n"
        "  - a\n"
        "created: 2026-06-08\n"
        "---\n"
    )
    front, _ = fm.parse_frontmatter(text)
    assert front["title"] == "Mon Titre"             # guillemets retirés
    assert front["sources"] == ["a"]
    assert front["created"] == "2026-06-08"
