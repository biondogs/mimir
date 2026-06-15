"""Tests squelettes pour 2 invariants documentaires non testés
(voir `openspec/changes/setup-openspec-as-design-tool/design.md` § 4).

Ces tests *prouvent* que la boucle spec → test fonctionne :
un fixture « violant » est détecté par le helper, et la base de code
actuelle passe au vert.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lint_docs import has_ascii_art_schemes, has_dumps


SKILLS_REF_DIR = Path(__file__).resolve().parent.parent
REFS_GLOB = list(SKILLS_REF_DIR.glob("../*/references/*.md"))


# --- Mermaid obligatoire : détection d'ASCII art -----------------------


def test_detect_ascii_art_schemes_trouve_une_violation(tmp_path: Path):
    """Un schéma en ASCII art (en dehors d'un bloc fenced) est détecté."""
    md = tmp_path / "bad.md"
    md.write_text(
        "# Ref\n\n"
        "## Schéma\n\n"
        "Pas de mermaid ici, juste un truc ASCII :\n"
        "\n"
        "+-----+      +-----+\n"
        "|  A  |----->|  B  |\n"
        "+-----+      +-----+\n",
        encoding="utf-8",
    )
    hits = has_ascii_art_schemes(md)
    assert hits, "le helper devrait flagger l'ASCII art"


def test_detect_ascii_art_schemes_ignore_les_blocs_fenced(tmp_path: Path):
    """Un schéma en ASCII art *dans* un bloc ``` n'est pas flaggé."""
    md = tmp_path / "ok.md"
    md.write_text(
        "# Ref\n\n"
        "```\n"
        "+-----+      +-----+\n"
        "|  A  |----->|  B  |\n"
        "+-----+      +-----+\n"
        "```\n",
        encoding="utf-8",
    )
    assert has_ascii_art_schemes(md) == []


def test_detect_ascii_art_schemes_base_de_code_propre():
    """Aucune référence de skill ne contient d'ASCII art non-fencé."""
    offenders: list[tuple[Path, int, str]] = []
    for ref in REFS_GLOB:
        for lineno, line in has_ascii_art_schemes(ref):
            offenders.append((ref, lineno, line))
    assert not offenders, (
        f"violation mermaid-mandatory : {offenders[:3]} "
        f"(total {len(offenders)})"
    )


# --- Pas de dump brut dans wiki/ --------------------------------------


def test_detect_dumps_trouve_un_gros_bloc(tmp_path: Path):
    """Un article wiki avec un bloc de prose > 5000 chars sans heading
    intermédiaire est détecté."""
    md = tmp_path / "wiki_article.md"
    big_block = "Lorem ipsum. " * 500  # ~6500 chars
    md.write_text(
        f"# Article\n\n## Résumé\n\n{big_block}\n",
        encoding="utf-8",
    )
    hits = has_dumps(md, threshold=5000)
    assert hits, "le helper devrait flagger le bloc de prose"
    assert hits[0][1] > 5000


def test_detect_dumps_karpathy_est_propre(tmp_path: Path):
    """Un article au format Karpathy (headings régulièrement espacés)
    n'est pas flaggé."""
    md = tmp_path / "karpathy.md"
    sections = [
        "## Résumé\n\nUne phrase.\n",
        "## Définition\n\n" + ("Lac. " * 100) + "\n",
        "## Points clés\n\n" + ("Repère. " * 100) + "\n",
        "## Relations\n\n- [[autre/notion]] : lien.\n",
        "## Sources\n\n- Source X.\n",
    ]
    md.write_text("# Article\n\n" + "\n".join(sections), encoding="utf-8")
    assert has_dumps(md, threshold=5000) == []
