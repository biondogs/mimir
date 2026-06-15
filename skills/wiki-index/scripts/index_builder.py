#!/usr/bin/env python3
"""Génération des index du wiki à deux niveaux (SPEC §9).

- **INDEX principal** `wiki/INDEX.md` : la liste des **sujets**, chacun pointant vers
  son index de sujet. La **description éditoriale** (texte après le `—`) est
  **préservée** d'une régénération à l'autre ; à défaut, on retombe sur les trois
  premières notions.
- **INDEX par sujet** `wiki/<sujet>/_INDEX.md` : les **notions** (avec leur résumé
  d'une ligne extrait du callout `>`) + les **grilles de lecture** rattachées (une
  grille est rattachée à **chaque** sujet qu'elle cite).

Tout est **déterministe** : on scanne le disque, on n'invente aucun contenu. Les
wikilinks sont émis dans la **forme majoritaire** du vault (préfixée vs relative).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import article_index
import iohelpers
import wikilinks
from frontmatter import parse_frontmatter

#: Extrait `sujet` + description d'une ligne `- [[<sujet>/_INDEX]] — desc` de l'INDEX.
_MAIN_ENTRY_RE = re.compile(r"-\s*\[\[(?:wiki/)?([^\]/|#^]+)/_INDEX\]\][^—]*—\s*(.*\S)")


@dataclass
class GridRef:
    """Une entrée de `reading-grids/` rattachée à un sujet.

    Deux `kind` reconnus (cf. spec `wiki-research` §5) :
    - `"reading-grid"` (palier v3→v4) : ouvrage chapitré ; `work` + `chapters` posés.
    - `"research-report"` (skill `wiki-research`) : synthèse sur un sujet texte ;
      `topic` posé, `work` et `chapters` à `""`/`0`.
    """

    slug: str
    work: str
    chapters: int
    kind: str = "reading-grid"
    topic: str = ""


@dataclass
class IndexResult:
    subjects: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)   # chemins relatifs écrits
    form: str = "relative"


def list_subjects(cfg) -> list[str]:
    """Sujets = sous-dossiers de `wiki/` ne commençant pas par `_`."""
    if not cfg.WIKI.is_dir():
        return []
    return sorted(
        d.name for d in cfg.WIKI.iterdir() if d.is_dir() and not d.name.startswith("_")
    )


def list_notions(cfg, subject: str) -> list[str]:
    """Notions d'un sujet = `*.md` hors `_*`, triées (stems)."""
    subj_dir = cfg.WIKI / subject
    if not subj_dir.is_dir():
        return []
    return sorted(
        p.stem for p in subj_dir.iterdir()
        if p.is_file() and p.suffix == ".md" and not p.name.startswith("_")
    )


def detect_form(cfg) -> str:
    """Forme majoritaire des wikilinks du vault (`prefixed`/`relative`)."""
    targets: list[str] = []
    if cfg.WIKI.is_dir():
        for p in cfg.WIKI.rglob("*.md"):
            targets.extend(wikilinks.extract_wikilinks(p.read_text(encoding="utf-8")))
    return wikilinks.majority_form(targets)


def grids_by_subject(cfg) -> dict[str, list[GridRef]]:
    """Rattache chaque entrée de `reading-grids/` aux sujets qu'elle cite.

    Reconnait deux `type:` (cf. spec `wiki-research` §5) :
    - `reading-grid` : grille de lecture d'un ouvrage chapitré ;
    - `research-report` : rapport de recherche (synthèse sur un sujet texte).

    Pour un `research-report`, le sujet principal est son `subject:` de
    frontmatter (mandatoire, posé par `wiki-research scaffold`). Les wikilinks
    du corps ajoutent des rattachements secondaires (le rapport peut évoquer
    d'autres sujets que le sien).
    """
    out: dict[str, list[GridRef]] = {}
    if not cfg.READING_GRIDS.is_dir():
        return out
    for p in sorted(cfg.READING_GRIDS.glob("*.md")):
        front, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        kind = str(front.get("type") or "")
        if kind == "reading-grid":
            work = str(front.get("work") or p.stem)
            chapters = int(front["chapters"]) if str(front.get("chapters", "")).isdigit() else 0
            ref = GridRef(slug=p.stem, work=work, chapters=chapters, kind=kind)
        elif kind == "research-report":
            topic = str(front.get("topic") or front.get("title") or p.stem)
            ref = GridRef(slug=p.stem, work="", chapters=0, kind=kind, topic=topic)
        else:
            continue  # ni grille, ni rapport → ignoré (permet d'autres types future-proof)

        # rattachements : sujet du frontmatter + sujets cités par wikilinks
        subjects: set[str] = set()
        primary = str(front.get("subject") or "").strip()
        if primary:
            subjects.add(primary)
        for t in wikilinks.extract_wikilinks(body):
            if "/" in t and not t.startswith(("raw/", "wiki/", "reading-grids/")):
                subjects.add(t.split("/")[0])
            elif t.startswith("wiki/") and "/" in t[5:]:
                subjects.add(t.split("/")[1])
        for subject in sorted(subjects):
            out.setdefault(subject, []).append(ref)
    return out


def _article_link(form: str, subject: str, notion: str) -> str:
    prefix = "wiki/" if form == "prefixed" else ""
    return f"{prefix}{subject}/{notion}"


def _existing_descriptions(cfg) -> dict[str, str]:
    """Descriptions éditoriales du `wiki/INDEX.md` existant, par sujet (préservation)."""
    index = cfg.WIKI / "INDEX.md"
    if not index.is_file():
        return {}
    out: dict[str, str] = {}
    for line in index.read_text(encoding="utf-8").splitlines():
        m = _MAIN_ENTRY_RE.search(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def build_subject_index(cfg, subject: str, form: str, grids: list[GridRef]) -> str:
    """Markdown de `wiki/<sujet>/_INDEX.md` : notions (+ résumé) et grilles rattachées.

    Les rapports de recherche (`kind: research-report`) sont listés dans une
    section dédiée `## Rapports de recherche` pour rester visuellement
    séparés des grilles de lecture chapitrées.
    """
    lines = [f"# Index — {subject}", "", "## Notions", ""]
    for notion in list_notions(cfg, subject):
        info = article_index.load_article(cfg, f"{subject}/{notion}")
        link = f"[[{_article_link(form, subject, notion)}]]"
        lines.append(f"- {link} — {info.summary}" if info.summary else f"- {link}")

    grids_only = [g for g in grids if g.kind == "reading-grid"]
    reports = [g for g in grids if g.kind == "research-report"]

    if grids_only:
        lines += ["", "## Grilles de lecture", ""]
        for g in sorted(grids_only, key=lambda r: r.slug):
            suffix = f" ({g.chapters} ch.)" if g.chapters else ""
            lines.append(f"- [[{cfg.layout['reading_grids']}/{g.slug}]] — {g.work}{suffix}")
    if reports:
        lines += ["", "## Rapports de recherche", ""]
        for g in sorted(reports, key=lambda r: r.slug):
            lines.append(f"- [[{cfg.layout['reading_grids']}/{g.slug}]] — {g.topic}")
    return "\n".join(lines).rstrip("\n") + "\n"


def build_main_index(cfg, subjects: list[str], form: str) -> str:
    """Markdown de `wiki/INDEX.md` : liste des sujets, descriptions préservées."""
    descriptions = _existing_descriptions(cfg)
    prefix = "wiki/" if form == "prefixed" else ""
    lines = ["# Index du wiki", "", "> Carte par sujet.", ""]
    for subject in subjects:
        desc = descriptions.get(subject)
        if not desc:
            notions = list_notions(cfg, subject)[:3]
            desc = ", ".join(notions) if notions else "—"
        lines.append(f"- [[{prefix}{subject}/_INDEX]] — {desc}")
    return "\n".join(lines).rstrip("\n") + "\n"


def regenerate(cfg, *, dry_run: bool = False) -> IndexResult:
    """Reconstruit `wiki/INDEX.md` + tous les `wiki/<sujet>/_INDEX.md`."""
    subjects = list_subjects(cfg)
    form = detect_form(cfg)
    grids = grids_by_subject(cfg)
    res = IndexResult(subjects=subjects, form=form)

    for subject in subjects:
        doc = build_subject_index(cfg, subject, form, grids.get(subject, []))
        target = cfg.WIKI / subject / "_INDEX.md"
        if not dry_run:
            iohelpers.atomic_write_text(target, doc, work_root=cfg.work_root)
        res.written.append(target.relative_to(cfg.work_root).as_posix())

    main_doc = build_main_index(cfg, subjects, form)
    main_target = cfg.WIKI / "INDEX.md"
    if not dry_run:
        iohelpers.atomic_write_text(main_target, main_doc, work_root=cfg.work_root)
    res.written.append(main_target.relative_to(cfg.work_root).as_posix())
    return res
