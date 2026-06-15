#!/usr/bin/env python3
"""Audit de cohérence des liens du wiki — 4 passes, **lecture seule** (SPEC §9).

Aucune écriture (pas même `_status.md`) : l'audit doit pouvoir tourner en CI sans
effet de bord. Périmètre (décision P3) : les wikilinks **wiki/ + reading-grids/**
seulement ; les liens vers `raw/` (champ `sources`) sont hors scope.

Quatre passes :
1. **liens cassés** — un `[[…]]` d'un article/grille sans fichier cible résolu ;
2. **fichiers fantômes** — un article réel non listé dans le `_INDEX.md` de son sujet ;
3. **index → vide** — un `[[…]]` d'un fichier d'index (`INDEX.md`/`_INDEX.md`) sans cible ;
4. **caractères CJK parasites** — présence de Han/Hiragana/Katakana suspects dans
   les corps d'articles (artefacts de sous-agents, ex. `充当`, `弥补`).

Chemins normalisés avec `os.path.normpath` avant test d'existence (sinon faux
positifs sur les `..`), comparaison NFD/NFC, via `wikilinks.resolve`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import slug
import wikilinks

_INDEX_NAMES = {"INDEX.md", "_INDEX.md"}


@dataclass
class AuditReport:
    broken: list[tuple[str, str]] = field(default_factory=list)    # (fichier, cible)
    orphans: list[str] = field(default_factory=list)              # article non indexé
    dangling: list[tuple[str, str]] = field(default_factory=list)  # (index, cible)
    cjk: list[tuple[str, list[str]]] = field(default_factory=list) # (fichier, [caractères suspects])
    suggestions: dict[str, str] = field(default_factory=dict)      # cible cassée → slug ASCII

    @property
    def total(self) -> int:
        return (
            len(self.broken)
            + len(self.orphans)
            + len(self.dangling)
            + len(self.cjk)
        )

    @property
    def ok(self) -> bool:
        return self.total == 0


def _is_index(path: Path) -> bool:
    return path.name in _INDEX_NAMES


def _audit_targets(md_text: str) -> list[str]:
    """Cibles à auditer : wikilinks hors ancres pures et hors liens vers `raw/`."""
    return [t for t in wikilinks.extract_wikilinks(md_text) if not t.startswith("raw/")]


def _markdown_files(cfg) -> list[Path]:
    """Tous les `.md` du périmètre audité : `wiki/**` + `reading-grids/*`."""
    files: list[Path] = []
    if cfg.WIKI.is_dir():
        files.extend(sorted(cfg.WIKI.rglob("*.md")))
    if cfg.READING_GRIDS.is_dir():
        files.extend(sorted(cfg.READING_GRIDS.glob("*.md")))
    return files


def _pass_broken_and_dangling(cfg, files: list[Path]) -> tuple[list, list]:
    """Passes 1 & 3 : liens cassés (hors index) et entrées d'index mortes."""
    broken: list[tuple[str, str]] = []
    dangling: list[tuple[str, str]] = []
    for f in files:
        rel = f.relative_to(cfg.work_root).as_posix()
        for target in _audit_targets(f.read_text(encoding="utf-8")):
            if wikilinks.resolve(cfg, f, target) is None:
                (dangling if _is_index(f) else broken).append((rel, target))
    return broken, dangling


def _pass_orphans(cfg) -> list[str]:
    """Passe 2 : articles réels non listés dans le `_INDEX.md` de leur sujet."""
    orphans: list[str] = []
    if not cfg.WIKI.is_dir():
        return orphans
    for subj_dir in sorted(cfg.WIKI.iterdir()):
        if not subj_dir.is_dir() or subj_dir.name.startswith("_"):
            continue
        index = subj_dir / "_INDEX.md"
        indexed: set[Path] = set()
        if index.is_file():
            for target in _audit_targets(index.read_text(encoding="utf-8")):
                resolved = wikilinks.resolve(cfg, index, target)
                if resolved is not None:
                    indexed.add(resolved)
        for article in sorted(subj_dir.iterdir()):
            if not article.is_file() or article.suffix != ".md" or article.name.startswith("_"):
                continue
            if article.resolve() not in {p.resolve() for p in indexed}:
                orphans.append(article.relative_to(cfg.work_root).as_posix())
    return orphans


_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x3040, 0x309F),  # Hiragana          (぀-ゟ)
    (0x30A0, 0x30FF),  # Katakana          (゠-ヿ)
    (0x4E00, 0x9FFF),  # CJK Unified       (一-鿿)
)


def _is_suspicious_cjk(ch: str) -> bool:
    """Vrai si `ch` est un Han / Hiragana / Katakana (artefact de génération)."""
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def _pass_cjk(cfg, files: list[Path]) -> list[tuple[str, list[str]]]:
    """Passe 4 : caractères CJK/Hiragana/Katakana dans les corps d'articles.

    Lit chaque `.md` du périmètre, collecte (dans l'ordre d'apparition, sans
    doublon par fichier) tous les caractères suspects. **Aucune écriture.**
    """
    hits: list[tuple[str, list[str]]] = []
    for f in files:
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        seen: set[str] = set()
        offending: list[str] = []
        for ch in text:
            if _is_suspicious_cjk(ch) and ch not in seen:
                seen.add(ch)
                offending.append(ch)
        if offending:
            hits.append((f.relative_to(cfg.work_root).as_posix(), offending))
    return hits


def _suggest_slug(cfg, target: str) -> str | None:
    """Pour une cible cassée accentuée, propose le slug ASCII si le fichier existe.

    Les noms de fichiers sont des slugs ASCII (convention) ; un wikilink accentué
    `[[réglementation/relèvement]]` ne résout pas, alors que le fichier
    `reglementation/relevement.md` existe. On propose la forme slugifiée plutôt que
    de tolérer l'accent (qui resterait un slug cassé). Lecture seule.
    """
    rel = target.strip().removeprefix("wiki/").lstrip("/")
    if "/" not in rel:
        return None
    subject, _, notion = rel.rpartition("/")
    candidate = f"{slug.slugify(subject)}/{slug.slugify(notion)}"
    if candidate == rel:  # déjà en ASCII : pas une suggestion utile
        return None
    return candidate if (cfg.WIKI / f"{candidate}.md").is_file() else None


def audit(cfg) -> AuditReport:
    """Exécute les 4 passes en lecture seule et renvoie le rapport."""
    files = _markdown_files(cfg)
    broken, dangling = _pass_broken_and_dangling(cfg, files)
    orphans = _pass_orphans(cfg)
    cjk = _pass_cjk(cfg, files)
    suggestions: dict[str, str] = {}
    for _, target in (*broken, *dangling):
        if target not in suggestions:
            sugg = _suggest_slug(cfg, target)
            if sugg:
                suggestions[target] = sugg
    return AuditReport(
        broken=broken, orphans=orphans, dangling=dangling, cjk=cjk, suggestions=suggestions,
    )


def render_report(report: AuditReport, *, today: str) -> str:
    """Rapport texte lisible (lecture seule)."""
    lines = [
        f"AUDIT wiki — {today}",
        f"Passe 1 — liens cassés ........... {len(report.broken)}",
        f"Passe 2 — fichiers fantômes ...... {len(report.orphans)}",
        f"Passe 3 — index → vide ........... {len(report.dangling)}",
        f"Passe 4 — CJK parasites .......... {len(report.cjk)}",
        f"RÉSULTAT : {'OK (0 anomalie)' if report.ok else f'KO ({report.total} anomalies)'}",
    ]
    def _line(src: str, tgt: str) -> str:
        sugg = report.suggestions.get(tgt)
        hint = f" (suggéré : [[{sugg}]])" if sugg else ""
        return f"  - {src} → [[{tgt}]]{hint}"

    if report.broken:
        lines.append("")
        lines.append("Liens cassés :")
        lines += [_line(src, tgt) for src, tgt in report.broken]
    if report.orphans:
        lines.append("")
        lines.append("Fichiers fantômes (non indexés) :")
        lines += [f"  - {p}" for p in report.orphans]
    if report.dangling:
        lines.append("")
        lines.append("Entrées d'index pointant dans le vide :")
        lines += [_line(src, tgt) for src, tgt in report.dangling]
    if report.cjk:
        lines.append("")
        lines.append("Caractères CJK/Hiragana/Katakana parasites :")
        for src, chars in report.cjk:
            lines.append(f"  - {src} : {''.join(chars)}")
    return "\n".join(lines) + "\n"
