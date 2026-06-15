"""Linters statiques pour invariants documentaires.

Deux invariants du méta-spec `mimir-invariants` n'étaient testés que
partiellement (voir `openspec/changes/setup-openspec-as-design-tool/
design.md` § 4 — audit de testabilité). Ce module pose les détecteurs
pour qu'un test puisse les brancher.

- `has_ascii_art_schemes(md)` : `mimir-invariants::mermaid-mandatory`
  (jamais d'ASCII art dans les références de skill).

- `has_dumps(md)` : `mimir-invariants::no-dump-in-wiki` (jamais de bloc
  de texte > 5000 chars d'affilée sans heading intermédiaire).
"""

from __future__ import annotations

import re
from pathlib import Path


_ASCII_ART_CHARS = set("+-|")
_HEADING_RE = re.compile(r"^#{1,6}\s")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _strip_fences(lines: list[str]) -> list[tuple[int, str]]:
    """Filtre les lignes à l'intérieur de blocs ``` / ~~~ (fenced code)."""
    out: list[tuple[int, str]] = []
    in_fence = False
    fence_marker: str | None = None
    for i, ln in enumerate(lines, start=1):
        stripped = ln.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = "```" if stripped.startswith("```") else "~~~"
            if not in_fence:
                in_fence = True
                fence_marker = marker
                continue
            if fence_marker == marker:
                in_fence = False
                fence_marker = None
                continue
        if not in_fence:
            out.append((i, ln))
    return out


def has_ascii_art_schemes(md_path: Path) -> list[tuple[int, str]]:
    """Détecte les lignes *hors fenced* qui ressemblent à un schéma ASCII.

    Heuristique : ligne ≥ 20 chars, ≥ 3 caractères de l'ensemble
    `+|-|/\\`, ET au moins 2 *caractères distincts* parmi ceux-là
    (évite les faux positifs sur les séparateurs `------`).

    Exclusions (faux positifs connus) :
    - Lignes de **table Markdown** : commencent par `|` après strip.
    - **Séparateurs** de table : uniquement `|`, `-`, `:`, espace.

    Renvoie la liste des `(numéro_de_ligne, contenu)` litigieuses.
    """
    hits: list[tuple[int, str]] = []
    table_separator_chars = set("|-: \t")
    list_bullet_prefixes = ("- ", "* ", "+ ")
    for lineno, ln in _strip_fences(md_path.read_text(encoding="utf-8").splitlines()):
        stripped = ln.lstrip()
        if not stripped:
            continue
        if stripped.startswith("|"):
            continue                                # ligne de table
        if stripped and all(c in table_separator_chars for c in stripped):
            continue                                # séparateur de table
        if stripped.startswith(list_bullet_prefixes):
            continue                                # puce de liste
        if len(ln) < 20:
            continue
        art_chars = [c for c in ln if c in _ASCII_ART_CHARS]
        if len(art_chars) < 3:
            continue
        if len(set(art_chars)) < 2:
            continue
        hits.append((lineno, ln))
    return hits


def has_dumps(md_path: Path, threshold: int = 5000) -> list[tuple[int, int]]:
    """Détecte les blocs de prose > `threshold` chars sans heading intermédiaire.

    Renvoie la liste des `(numéro_de_ligne_début, taille_en_chars)`
    des blocs suspects. Un article wiki bien formé a des headings `##`
    régulièrement espacés (Karpathy : résumé / Définition / Points
    clés / Relations / Sources) ; un *dump* d'une source n'en a pas.
    """
    text = md_path.read_text(encoding="utf-8")
    in_fence = False
    fence_marker: str | None = None
    block_start: int | None = None
    block_len: int = 0
    hits: list[tuple[int, int]] = []

    for i, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = "```" if stripped.startswith("```") else "~~~"
            if not in_fence:
                in_fence = True
                fence_marker = marker
                if block_start is not None and block_len > threshold:
                    hits.append((block_start, block_len))
                block_start = None
                block_len = 0
                continue
            if fence_marker == marker:
                in_fence = False
                fence_marker = None
                continue
        if in_fence:
            continue
        if _HEADING_RE.match(stripped):
            if block_start is not None and block_len > threshold:
                hits.append((block_start, block_len))
            block_start = i
            block_len = 0
            continue
        block_len += len(raw) + 1  # +1 pour le \n

    if block_start is not None and block_len > threshold:
        hits.append((block_start, block_len))
    return hits
