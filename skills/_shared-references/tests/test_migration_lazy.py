"""Tests de `migration.lazy` — sélection « fichiers en retard » + marquage.

Couvre :
- 4.1 `schema_version` toléré comme entier optionnel (déjà couvert par
  `test_frontmatter.py` + nouveaux tests ciblés sur `read_schema_version`).
- 4.2 Un palier peut sélectionner « les fichiers en retard » via ce champ
  pour reprendre une migration supervisée interrompue.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

import iohelpers
from migration import lazy


def _make_article(path: Path, *, body: str = "corps", schema_version=None) -> None:
    """Helper : écrit un article avec (ou sans) `schema_version` dans le frontmatter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---\n", "title: x\n"]
    if schema_version is not None:
        fm_lines.append(f"schema_version: {schema_version}\n")
    fm_lines.append("---\n\n")
    path.write_text("".join(fm_lines) + body, encoding="utf-8")


def test_read_schema_version_absence_vaut_none():
    """Frontmatter sans `schema_version` → `None` (« non migré »)."""
    front, _ = frontmatter.parse_frontmatter("---\ntitle: x\n---\nbody")
    assert frontmatter.read_schema_version(front) is None


def test_read_schema_version_entier_valide():
    """Frontmatter avec `schema_version: 3` → 3."""
    front, _ = frontmatter.parse_frontmatter("---\nschema_version: 3\n---\nbody")
    assert frontmatter.read_schema_version(front) == 3


def test_read_schema_version_invalide_vaut_none():
    """Frontmatter avec `schema_version: pas-un-int` → `None` (jamais d'erreur)."""
    front, _ = frontmatter.parse_frontmatter("---\nschema_version: foo\n---\nbody")
    assert frontmatter.read_schema_version(front) is None


def test_read_schema_version_zero_ou_negatif_vaut_none():
    """Frontmatter avec `schema_version: 0` (invalide) → `None`."""
    front, _ = frontmatter.parse_frontmatter("---\nschema_version: 0\n---\nbody")
    assert frontmatter.read_schema_version(front) is None
    front, _ = frontmatter.parse_frontmatter("---\nschema_version: -1\n---\nbody")
    assert frontmatter.read_schema_version(front) is None


# --- 4.2 : select_lagging ----------------------------------------------------

def test_select_lagging_inclut_les_fichiers_sans_schema_version(tmp_path: Path):
    """Un article sans `schema_version` est considéré comme « non migré »."""
    wiki = tmp_path / "wiki" / "nav"
    _make_article(wiki / "releve.md", schema_version=None)
    _make_article(wiki / "amer.md", schema_version=2)

    class Cfg:
        work_root = tmp_path
        WIKI = tmp_path / "wiki"
        READING_GRIDS = tmp_path / "reading-grids"
        ASSETS_DIRNAME = "_assets"

    selected = lazy.select_lagging(Cfg(), target=3)
    # `releve.md` n'a pas de schema_version → inclus
    # `amer.md` est à 2 < 3 → inclus
    assert wiki / "releve.md" in selected
    assert wiki / "amer.md" in selected


def test_select_lagging_exclut_les_fichiers_a_jour(tmp_path: Path):
    """Un article à `schema_version = target` est exclu (déjà migré)."""
    wiki = tmp_path / "wiki"
    _make_article(wiki / "old.md", schema_version=1)
    _make_article(wiki / "fresh.md", schema_version=3)

    class Cfg:
        work_root = tmp_path
        WIKI = tmp_path / "wiki"
        READING_GRIDS = tmp_path / "reading-grids"
        ASSETS_DIRNAME = "_assets"

    selected = lazy.select_lagging(Cfg(), target=3)
    assert wiki / "old.md" in selected
    assert wiki / "fresh.md" not in selected


def test_select_lagging_ordre_deterministe(tmp_path: Path):
    """L'ordre est alphabétique (reproductibilité des runs supervisés)."""
    wiki = tmp_path / "wiki"
    for name in ("z.md", "a.md", "m.md"):
        _make_article(wiki / name, schema_version=None)

    class Cfg:
        work_root = tmp_path
        WIKI = tmp_path / "wiki"
        READING_GRIDS = tmp_path / "reading-grids"
        ASSETS_DIRNAME = "_assets"

    selected = lazy.select_lagging(Cfg(), target=2)
    assert [p.name for p in selected] == ["a.md", "m.md", "z.md"]


def test_select_lagging_refuse_target_invalide(tmp_path: Path):
    wiki = tmp_path / "wiki"
    _make_article(wiki / "x.md")

    class Cfg:
        work_root = tmp_path
        WIKI = tmp_path / "wiki"
        READING_GRIDS = tmp_path / "reading-grids"
        ASSETS_DIRNAME = "_assets"

    with pytest.raises(ValueError):
        lazy.select_lagging(Cfg(), target=0)
    with pytest.raises(ValueError):
        lazy.select_lagging(Cfg(), target=-1)
    with pytest.raises(ValueError):
        lazy.select_lagging(Cfg(), target="3")


# --- mark_migrated ----------------------------------------------------------

def test_mark_migrated_cree_frontmatter_si_absent(tmp_path: Path):
    """Un article sans frontmatter se voit doter d'un frontmatter minimal."""
    article = tmp_path / "wiki" / "no-front.md"
    article.parent.mkdir(parents=True)
    article.write_text("# Titre\n\nCorps.\n", encoding="utf-8")

    class Cfg:
        work_root = tmp_path

    out = lazy.mark_migrated(Cfg(), article, 3)
    text = out.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "schema_version: 3\n" in text
    # Le corps est préservé
    assert "# Titre" in text
    assert "Corps." in text


def test_mark_migrated_met_a_jour_frontmatter_existant(tmp_path: Path):
    article = tmp_path / "wiki" / "with-front.md"
    article.parent.mkdir(parents=True)
    article.write_text(
        "---\ntitle: Test\nschema_version: 1\n---\n\nCorps.\n",
        encoding="utf-8",
    )

    class Cfg:
        work_root = tmp_path

    out = lazy.mark_migrated(Cfg(), article, 3)
    text = out.read_text(encoding="utf-8")
    assert "schema_version: 3" in text
    assert "title: Test" in text  # autre champ préservé
    assert "Corps." in text


def test_mark_migrated_idempotent(tmp_path: Path):
    """Marquer 2x au même target : no-op (le texte reste byte-pour-byte
    identique après le 2e appel)."""
    article = tmp_path / "wiki" / "idem.md"
    article.parent.mkdir(parents=True)
    article.write_text("---\ntitle: x\n---\n\nCorps.\n", encoding="utf-8")

    class Cfg:
        work_root = tmp_path

    out1 = lazy.mark_migrated(Cfg(), article, 3)
    text1 = out1.read_text(encoding="utf-8")
    out2 = lazy.mark_migrated(Cfg(), article, 3)
    text2 = out2.read_text(encoding="utf-8")
    assert text1 == text2


def test_mark_migrated_refuse_d_ecraser_une_version_superieure(tmp_path: Path):
    """On n'écrase jamais une version acquise (pas de régression)."""
    article = tmp_path / "wiki" / "future.md"
    article.parent.mkdir(parents=True)
    article.write_text(
        "---\nschema_version: 5\n---\n\nCorps.\n",
        encoding="utf-8",
    )

    class Cfg:
        work_root = tmp_path

    # Marquer à 3 alors que le fichier est à 5 : no-op (refus)
    out = lazy.mark_migrated(Cfg(), article, 3)
    text = out.read_text(encoding="utf-8")
    assert "schema_version: 5" in text
    assert "schema_version: 3" not in text


def test_mark_migrated_refuse_target_invalide(tmp_path: Path):
    article = tmp_path / "wiki" / "x.md"
    article.parent.mkdir(parents=True)
    article.write_text("# x\n", encoding="utf-8")

    class Cfg:
        work_root = tmp_path

    with pytest.raises(ValueError):
        lazy.mark_migrated(Cfg(), article, 0)
    with pytest.raises(ValueError):
        lazy.mark_migrated(Cfg(), article, -1)
    with pytest.raises(ValueError):
        lazy.mark_migrated(Cfg(), article, "3")
