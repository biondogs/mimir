#!/usr/bin/env python3
"""Sélection « fichiers en retard » pour migration **supervisée par lots**.

Un palier sémantique (ex. v4 — grilles « guide de lecture ») peut traiter des
centaines de fichiers un par un, avec intervention humaine entre les lots. En
cas d'interruption, on doit pouvoir **reprendre** sur les fichiers restants
sans double traitement.

Le contrat : un palier appelle `select_lagging(cfg, target, *, roots)` qui
renvoie la liste des fichiers dont `schema_version < target` (ou **absent**,
considéré comme « non migré »). À la fin de son traitement par lot, le palier
peut appeler `mark_migrated(cfg, path, target)` pour **poser** le champ
`schema_version = target` dans le frontmatter (atomique, confiné).

**Hors scope** : la sélection elle-même est **par fichier** — c'est au palier
de décider ce qu'il fait d'un fichier (le transformer, le réorganiser, etc.).
Ce module ne fait **que** la sélection et le marquage.

**Lecture seule par défaut** : `select_lagging` ne modifie rien.
`mark_migrated` est l'**unique** mutation, et elle est idempotente.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import frontmatter
import guard
import iohelpers


#: Racines par défaut où chercher les fichiers candidats (wiki + reading-grids).
DEFAULT_ROOTS_ATTRS: tuple[str, ...] = ("WIKI", "READING_GRIDS")


def _roots(cfg) -> list[Path]:
    """Racines de la sélection (issues du `Config`, ordre stable)."""
    out: list[Path] = []
    for attr in DEFAULT_ROOTS_ATTRS:
        p = getattr(cfg, attr, None)
        if p is not None:
            out.append(Path(p))
    return out


def _iter_markdown(roots: Iterable[Path]) -> Iterable[Path]:
    """Itère récursivement sur les `.md` des racines (ordre déterministe)."""
    for root in roots:
        if not root.is_dir():
            continue
        yield from sorted(root.rglob("*.md"))


def select_lagging(cfg, target: int, *, roots: Iterable[Path] | None = None) -> list[Path]:
    """Fichiers dont `schema_version < target` (ou absent = « non migré »).

    **Lecture seule**. Renvoie un ordre déterministe (alphabétique sur le
    chemin complet) — important pour la reproductibilité des runs supervisés.

    Un fichier sans frontmatter du tout est considéré comme « non migré »
    (équivalent à `schema_version = 0`) et est donc inclus dans la sélection.
    Un fichier avec un frontmatter malformé est **ignoré** (l'audit liens le
    signalera par ailleurs — on ne le traite pas deux fois ici).
    """
    if not isinstance(target, int) or isinstance(target, bool) or target < 1:
        raise ValueError(f"target invalide : {target!r} (attendu : int ≥ 1)")
    rts = list(roots) if roots is not None else _roots(cfg)
    out: list[Path] = []
    for p in _iter_markdown(rts):
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Frontmatter absent → considéré comme « non migré ».
        if not text.lstrip().startswith("---"):
            out.append(p)
            continue
        try:
            front, _ = frontmatter.parse_frontmatter(text)
        except Exception:
            # Malformé : on l'ignore (l'audit liens le traite ; on ne le
            # touche pas depuis la migration).
            continue
        v = frontmatter.read_schema_version(front)
        if v is None or v < target:
            out.append(p)
    out.sort()
    return out


def mark_migrated(
    cfg, path: Path, target: int, *, work_root: Path | None = None
) -> Path:
    """Pose `schema_version = target` dans le frontmatter de `path` (atomique).

    - Si le frontmatter est absent, on l'ajoute (en tête, suivi du corps).
    - Si `schema_version` est déjà `target`, c'est un **no-op** (idempotent).
    - Si `schema_version` est **supérieur** à `target` (palier rétrograde ?),
      on **refuse** de l'écraser — on ne dégrade jamais une version acquise.
    - **Confiné** : `guard.assert_within(work_root, path)`.
    - **Atomique** : `iohelpers.atomic_write_text` (tmp + `os.replace`).
    """
    if not isinstance(target, int) or isinstance(target, bool) or target < 1:
        raise ValueError(f"target invalide : {target!r} (attendu : int ≥ 1)")
    root = Path(work_root) if work_root is not None else Path(cfg.work_root)
    abs_p = guard.assert_within(root, path)
    try:
        text = abs_p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FileNotFoundError(f"impossible de lire {abs_p} : {exc}") from exc

    new_text = _set_schema_version_in_text(text, target)
    if new_text == text:
        return abs_p  # no-op (déjà à `target`, ou frontmatter absent)
    return iohelpers.atomic_write_text(abs_p, new_text, work_root=root)


def _set_schema_version_in_text(text: str, target: int) -> str:
    """Mutateur local (pur) : insère/remplace `schema_version: <target>`.

    - Frontmatter **absent** → on l'ajoute (en-tête) avec un seul champ.
    - Frontmatter **présent** → on remplace/ajoute le champ, on conserve tout
      le reste (ordre des clés, formatage). Le `---` fermant est conservé tel quel.
    - Si `schema_version: <target>` est déjà présent → on renvoie le texte tel quel.
    - Si `schema_version: <supérieur>` est présent → on renvoie le texte tel quel
      (refus d'écraser une version acquise).
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        # Pas de frontmatter : on en crée un minimal.
        new_front = f"---\n{frontmatter.SCHEMA_VERSION_KEY}: {target}\n---\n"
        return new_front + text

    # Frontmatter présent : on localise la clé, on l'insère/remplace.
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        # Frontmatter non terminé : on ne touche pas (malformé).
        return text

    # Cherche la clé dans l'en-tête (entre 0 et `end`).
    key_re = __import__("re").compile(r"^(\s*)" + frontmatter.SCHEMA_VERSION_KEY + r"\s*:\s*(.+?)\s*$")
    for i in range(1, end):
        m = key_re.match(lines[i].rstrip("\n"))
        if m:
            current = m.group(2).strip().strip('"').strip("'")
            try:
                current_n = int(current)
            except ValueError:
                current_n = None
            if current_n == target:
                return text  # déjà à target
            if current_n is not None and current_n > target:
                return text  # refus d'écraser une version supérieure
            # Sinon, on remplace
            indent = m.group(1)
            lines[i] = f"{indent}{frontmatter.SCHEMA_VERSION_KEY}: {target}\n"
            return "".join(lines)

    # Pas trouvé : on insère juste avant le `---` fermant.
    # On préserve l'indentation si le frontmatter utilise des espaces en tête
    # (très rare ; par défaut, on insère à la colonne 0).
    lines.insert(end, f"{frontmatter.SCHEMA_VERSION_KEY}: {target}\n")
    return "".join(lines)
