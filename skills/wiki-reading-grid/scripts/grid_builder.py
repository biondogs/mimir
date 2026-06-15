#!/usr/bin/env python3
"""Cœur déterministe de `wiki-reading-grid` : construit la grille d'un ouvrage.

La grille **restitue l'ordre de lecture** (chapitrage) perdu par l'éclatement en
notions. Elle ne duplique aucun contenu : elle **ordonne des liens**. Trois sources
sont croisées (SPEC §8) :

1. le `<base>.toc.json` (clé disque `chapters`, ordonnée) — l'ossature de lecture ;
2. le **ledger** — la liste des articles produits depuis cet ouvrage, dans l'ordre
   d'écriture par l'agent (proxy de l'ordre de lecture, conservé dans un chapitre) ;
3. les `sources` du frontmatter de chaque article — l'ancre `…#chK` rattache
   l'article au chapitre `order=K` de cet ouvrage.

**Format hybride (palier v3→v4)** : la grille combine une **charpente
déterministe** (ordre, liens, nav — invariante) et une **zone éditoriale**
facultative (une phrase courte par chapitre, sémantique, écrite par
l'agent). La charpente reste 100 % déterministe (ne dépend que du
`toc.json` et du ledger) ; seules les phrases sont sémantiques, et leur
absence n'empêche pas la génération.

```
## Ch. 2 — Se positionner
<!-- blurb:début -->
Comment fixer sa position en mer par recoupement de relèvements.
<!-- blurb:fin -->
Lire dans l'ordre :
1. [[navigation/relevement]]
2. [[navigation/triangulation]]
⬅ [[#Ch. 1 — …]] | ➡ [[#Ch. 3 — …]]
```

Un chapitre sans phrase porte `_à rédiger_` entre les marqueurs. À la
régénération, les phrases existantes sont **extraites puis réinjectées**
dans leur chapitre (appariement par `order`, tolérant à un léger
renommage de titre) — exactement le mécanisme de préservation
éditoriale de `wiki-index regenerate`.

Cas limites assumus (pas des bugs) : un chapitre sans article reçoit un marqueur
« _à rédiger_ » dans la zone blurb ; un article du ledger sans ancre résolvable
pour cet ouvrage est listé « Hors chapitrage » ; un wikilink sans fichier réel
est laissé tel quel (l'audit `wiki-index` le remontera).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import article_index
import frontmatter
import ledger as ledger_mod
import slug
import status_table
import wikilinks

#: Extensions de contenu reconnues (pour retrouver le `base` d'un chemin raw).
_CONTENT_EXTS = (".pdf.txt", ".epub.txt", ".md")
#: Ancre de chapitre `chK` dans une source. `K` est soit un **numéro** (`#ch3`),
#: soit un **code d'ouvrage** (`#chR2`, `#chG3`, `#chC1`). Dans les deux cas, `K`
#: doit matcher le champ `order` du `toc.json` (cf. `_chapter_key`).
_CH_ANCHOR_RE = re.compile(r"^ch([A-Za-z]{0,2}\d{1,3})$", re.IGNORECASE)
#: Marqueurs de la zone blurb (HTML-comment, invisibles en rendu Obsidian,
#: robustes au re-parsing, n'interfèrent pas avec les wikilinks). Tout
#: texte entre ces deux marqueurs est une « phrase éditoriale » préservée
#: d'une régénération à l'autre.
_BLURB_START = "<!-- blurb:début -->"
_BLURB_END = "<!-- blurb:fin -->"
#: Placeholder sémantique pour un chapitre sans phrase (l'agent le rédigera
#: par lots supervisés, cf. `chapters_missing_blurb` dans le diagnostic).
_BLURB_PLACEHOLDER = "_à rédiger_"


def _chapter_key(raw) -> str:
    """Clé de chapitre normalisée, pour matcher une ancre `#chK` et un `order` du toc.

    Numérique → sans zéros de tête (`"03"` → `"3"`) ; code d'ouvrage → majuscule
    (`"g3"` → `"G3"`). Toute autre valeur est renvoyée trimée/majuscule telle quelle.
    """
    s = str(raw).strip().upper()
    m = re.fullmatch(r"([A-Z]{0,2})0*(\d+)", s)
    return f"{m.group(1)}{m.group(2)}" if m else s


@dataclass
class GridResult:
    """Résultat de construction d'une grille (markdown + diagnostic)."""

    content_rel: str
    doc_type: str = ""
    work: str = ""
    toc_rel: str = ""
    n_chapters: int = 0
    markdown: str = ""
    linked_articles: list[str] = field(default_factory=list)     # wikilinks placés ≥1 chapitre
    unresolved_chapters: list[int] = field(default_factory=list)  # chapitres sans article
    orphan_articles: list[str] = field(default_factory=list)      # ledger sans ancre résolvable
    #: Chapitres au placeholder `_à rédiger_` (sans phrase éditoriale). Sert
    #: à l'agent pour savoir **quoi rédiger** par lots supervisés. Clé =
    #: `order` (tel qu'il apparaît dans le toc.json), valeur = titre courant.
    chapters_missing_blurb: dict[str, str] = field(default_factory=dict)
    skipped: str | None = None                                    # ex. "no-toc" (web)


def _base_of(filename: str) -> str:
    """Retire l'extension de contenu (`nav.pdf.txt` → `nav`)."""
    for ext in _CONTENT_EXTS:
        if filename.endswith(ext):
            return filename[: -len(ext)]
    return Path(filename).stem


def _same_path(a: str, b: str) -> bool:
    """Égalité de chemins relatifs (normpath + NFD/NFC)."""
    return slug.same_file(os.path.normpath(a), os.path.normpath(b))


def _chapters_cited(sources: list[str], content_rel: str) -> list[str]:
    """Clés de chapitres (de CET ouvrage) citées par les `sources` d'un article.

    L'ordre de citation est préservé (pas de tri : les clés peuvent être des codes
    `R2`/`G3` non triables numériquement ; l'ordre de lecture est porté par le toc).
    """
    found: list[str] = []
    for src in sources:
        path, sep, anchor = src.partition("#")
        if not sep or not _same_path(path, content_rel):
            continue
        m = _CH_ANCHOR_RE.match(anchor.strip())
        if m:
            k = _chapter_key(m.group(1))
            if k not in found:
                found.append(k)
    return found


def _extract_blurbs(existing_markdown: str, chapters: list[dict]) -> dict[str, str]:
    """Extrait les phrases éditoriales d'une grille déjà générée.

    Appariement par **`order` du toc.json** (clé normalisée `_chapter_key`) —
    tolérant à un léger renommage de titre (le `order` est stable même
    quand le `title` change entre deux générations). Renvoie
    `{order_key: blurb_text}` pour les chapitres qui ont une phrase **non vide**
    (un placeholder `_à rédiger_` n'est PAS préservé — c'est précisément
    l'invariant « ne jamais écraser une phrase non vide par le placeholder »).
    """
    if not existing_markdown:
        return {}
    # Index des anchors existantes par `order` (clé normalisée) → pour
    # tolérer un léger renommage de titre, on n'appariele PAS sur l'ancre
    # exacte (qui change avec le titre), mais sur l'`order` extrait de
    # l'ancre via regex.
    out: dict[str, str] = {}
    sections = re.split(r"(?m)^## ", existing_markdown)
    for sec in sections[1:]:  # la 1re est le frontmatter (ou vide)
        head, _, body = sec.partition("\n")
        anchor = head.strip()
        order = _order_from_anchor(anchor)
        if order is None:
            continue
        # Vérifier que l'`order` existe dans le `toc.json` actuel
        # (sinon, le chapitre a été supprimé entre deux générations — on
        # drop la phrase, on ne fait pas d'erreur).
        if not any(_chapter_key(c.get("order")) == _chapter_key(order) for c in chapters):
            continue
        m = re.search(
            re.escape(_BLURB_START) + r"\s*(.*?)\s*" + re.escape(_BLURB_END),
            body, re.DOTALL,
        )
        if not m:
            continue
        blurb = m.group(1).strip()
        if blurb and blurb != _BLURB_PLACEHOLDER:
            out[_chapter_key(order)] = blurb
    return out


def _order_from_anchor(anchor_text: str) -> int | str | None:
    """Extrait l'`order` d'une ancre de chapitre (`Ch. K — Title`).

    Le format est `Ch. <order> — <title>` (cf. `wikilinks.chapter_anchor`).
    On extrait tout ce qui est entre `Ch. ` et ` — ` (le `K` peut être
    numérique OU code d'ouvrage `R2`/`G3`).
    """
    m = re.match(r"^Ch\.\s+(.+?)\s+—\s+", anchor_text)
    if not m:
        return None
    return m.group(1).strip()


def _match_order(anchor_text: str, chapters: list[dict]) -> int | str | None:
    """Résout une ancre de chapitre du corps vers son `order` (cf. toc.json).

    Tolère un léger renommage de titre : on ne matche que le `Ch. K —` initial
    (le `K` est le `order`). Conservé pour compat (utilisé ailleurs si besoin).
    """
    for ch in chapters:
        order = ch.get("order")
        title = str(ch.get("title") or f"Chapitre {order}")
        if wikilinks.chapter_anchor(order, title) == anchor_text:
            return order
    return None


def build_grid(
    cfg,
    content_rel: str,
    *,
    existing_grid_path: Path | None = None,
) -> GridResult:
    """Construit la grille de l'ouvrage de contenu `content_rel` (chemin relatif raw/).

    `markdown` ne contient que le **corps** (sans frontmatter) : le `created` est
    décidé à l'écriture (préservation idempotente), via `document()`.

    Si `existing_grid_path` est fourni et que le fichier existe, ses phrases
    éditoriales (zones `<!-- blurb:… -->`) sont extraites et **réinjectées**
    par appariement sur l'`order` du toc (tolérant à un léger renommage de
    titre). Sinon (première génération), toutes les zones sont au
    placeholder `_à rédiger_`.
    """
    parts = content_rel.split("/")
    doc_type = parts[1] if len(parts) > 1 else ""
    base = _base_of(parts[-1])
    toc_rel = f"{cfg.layout['raw']}/{doc_type}/{base}.toc.json"
    toc_path = cfg.work_root / toc_rel

    # Pas de chapitrage → web : on tente une grille best-effort. Sinon (pdf/epub
    # sans toc) → `skipped: no-toc` (comportement historique).
    if not toc_path.is_file():
        if doc_type == "web":
            return _build_web_grid(cfg, content_rel)
        return GridResult(content_rel=content_rel, doc_type=doc_type, skipped="no-toc")

    toc = json.loads(toc_path.read_text(encoding="utf-8"))
    work = str(toc.get("title") or base)
    # L'ordre des chapitres dans le `toc.json` EST l'ordre de lecture (vrai pour des
    # `order` numériques 1,2,3… comme pour des codes d'ouvrage R1,G3,…). On ne retrie
    # pas : trier des codes alphanumériquement casserait l'ordre canonique.
    chapters = list(toc.get("chapters") or [])

    # Articles de cet ouvrage selon le ledger (ordre d'écriture agent préservé).
    led = ledger_mod.load_ledger(cfg.LEDGER)
    entry = led.get(content_rel)
    article_wikilinks = list(entry.articles) if entry else []

    # Rattachement article → chapitre(s) via les ancres #chK des sources.
    buckets: dict[str, list[str]] = {}
    orphans: list[str] = []
    placed: set[str] = set()
    for wl in article_wikilinks:
        info = article_index.load_article(cfg, wl)
        cited = _chapters_cited(info.sources, content_rel)
        if not cited:
            orphans.append(wl)
            continue
        for k in cited:
            buckets.setdefault(k, []).append(wl)
            placed.add(wl)

    # Préservation des phrases éditoriales (palier v3→v4) : on extrait les
    # blurb d'une grille déjà générée AVANT de ré-écrire. C'est la même
    # politique que `wiki-index regenerate` pour les descriptions de sujet.
    existing_blurbs: dict[str, str] = {}
    if existing_grid_path and existing_grid_path.is_file():
        try:
            existing_markdown = existing_grid_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            existing_markdown = ""
        existing_blurbs = _extract_blurbs(existing_markdown, chapters)

    body, missing_blurb = _render(work, chapters, buckets, orphans, existing_blurbs)
    unresolved = [c.get("order") for c in chapters if not buckets.get(_chapter_key(c.get("order")))]
    return GridResult(
        content_rel=content_rel,
        doc_type=doc_type,
        work=work,
        toc_rel=toc_rel,
        n_chapters=len(chapters),
        markdown=body,
        linked_articles=sorted(placed),
        unresolved_chapters=unresolved,
        orphan_articles=orphans,
        chapters_missing_blurb=missing_blurb,
    )


def _web_source_title(cfg, content_rel: str, base: str) -> str:
    """Titre de la source web, lu du frontmatter du `.md` brut, ou basename en repli."""
    src_path = cfg.work_root / content_rel
    if src_path.is_file():
        try:
            front, _ = frontmatter.parse_frontmatter(src_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            front = {}
        title = str(front.get("title") or "").strip()
        if title:
            return title
    return base


def _web_subject(content_rel: str) -> str:
    """Sujet d'une source web, déduit de son chemin (sous-dossier, puis préfixe `<sujet>--`).

    `raw/web/<sujet>/foo.md` → `<sujet>` ; `raw/web/<sujet>--foo.md` → `<sujet>` ;
    `raw/web/foo.md` (à plat) → `""` (la grille retombe sur une section « Lectures »).
    """
    parts = content_rel.split("/")
    if len(parts) >= 4:                         # sous-dossier raw/web/<sujet>/…
        return parts[2]
    filename = parts[-1]
    if "--" in filename:                        # préfixe <sujet>--…
        return filename.split("--", 1)[0]
    return ""


def _build_web_grid(cfg, content_rel: str) -> GridResult:
    """Grille best-effort pour une source web (pas de `toc.json`).

    Best-effort : si `raw/web/_status.md` n'a pas de ligne pour la source **ou**
    si le ledger n'a pas d'articles pour elle, on retombe sur `skipped: no-toc`
    (aucune grille vide, aucune régression du comportement historique). Sinon on
    restitue les articles en **ordre d'ingestion** (ordre du ledger = ordre
    d'écriture par l'agent), regroupés par sujet (sous-dossier `raw/web/<sujet>/`
    ou préfixe `<sujet>--` dans le nom de fichier, sinon liste à plat).

    Pas de zone blurb pour le web (pas de chapitrage, on reste sur la
    « liste de lectures » historique — l'enrichissement « guide » est
    réservé à la branche chapitrée PDF/EPUB).
    """
    parts = content_rel.split("/")
    doc_type = parts[1] if len(parts) > 1 else ""
    base = _base_of(parts[-1])

    # 1. statut
    status_path = cfg.RAW / doc_type / "_status.md"
    rows = status_table.load_status(status_path)
    fichier = parts[-1]
    status_row = next(
        (r for r in rows if r.fichier == fichier or r.fichier == content_rel), None
    )
    if status_row is None:
        return GridResult(content_rel=content_rel, doc_type=doc_type, skipped="no-toc")

    # 2. ledger
    led = ledger_mod.load_ledger(cfg.LEDGER)
    entry = led.get(content_rel)
    article_wikilinks = list(entry.articles) if entry else []
    if not article_wikilinks:
        return GridResult(content_rel=content_rel, doc_type=doc_type, skipped="no-toc")

    # 3. rendu
    work = _web_source_title(cfg, content_rel, base)
    subject = _web_subject(content_rel)
    body = _render_web(work, subject, article_wikilinks)
    return GridResult(
        content_rel=content_rel,
        doc_type=doc_type,
        work=work,
        toc_rel="",                    # pas de toc.json pour le web
        n_chapters=0,
        markdown=body,
        linked_articles=list(article_wikilinks),
        unresolved_chapters=[],
        orphan_articles=[],
    )


def _render_web(work: str, subject: str, article_wikilinks: list[str]) -> str:
    """Corps Markdown d'une grille web : un titre, une section (sujet ou « Lectures »)."""
    heading = subject if subject else "Lectures"
    lines: list[str] = [f"# Grille de lecture — {work}", ""]
    lines.append(f"## {heading}")
    lines.append("")
    lines.append("Lire dans l'ordre :")
    lines.append("")
    for n, wl in enumerate(article_wikilinks, start=1):
        lines.append(f"{n}. [[{wl}]]")
    lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def document(result: GridResult, created: str) -> str:
    """Document complet = frontmatter (avec `created`) + corps de la grille."""
    return (
        _with_frontmatter(result.work, result.content_rel, result.toc_rel, result.n_chapters, created)
        + result.markdown
    )


def _with_frontmatter(
    work: str, content_rel: str, toc_rel: str, n_chapters: int, created: str
) -> str:
    """Frontmatter de grille (FRONTMATTERS.md §2 / SPEC §8).

    Pour les grilles web (pas de `toc.json`), `toc_rel` est vide : on omet la ligne
    `toc:` plutôt que d'écrire une valeur creuse (le parseur la lirait comme `""`).
    """
    safe_work = work.replace('"', "'")
    lines = [
        "---",
        "type: reading-grid",
        f'work: "{safe_work}"',
        f"source: {content_rel}",
    ]
    if toc_rel:
        lines.append(f"toc: {toc_rel}")
    lines.append(f"chapters: {n_chapters}")
    lines.append(f"created: {created}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + "\n"


def _render(
    work: str,
    chapters: list[dict],
    buckets: dict[str, list[str]],
    orphans: list[str],
    existing_blurbs: dict[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Corps Markdown : un bloc par chapitre + zone blurb + navigation.

    Renvoie `(body, chapters_missing_blurb)` où `chapters_missing_blurb`
    est `{order_key: title}` pour les chapitres au placeholder (sans
    phrase éditoriale à la génération).
    """
    existing_blurbs = existing_blurbs or {}
    lines: list[str] = [f"# Grille de lecture — {work}", ""]
    last = len(chapters) - 1
    missing: dict[str, str] = {}
    for i, ch in enumerate(chapters):
        order = ch.get("order")
        title = str(ch.get("title") or f"Chapitre {order}")
        anchor = wikilinks.chapter_anchor(order, title)
        order_key = _chapter_key(order)
        lines.append(f"## {anchor}")
        lines.append("")

        # Zone blurb (phrase éditoriale) — préservation idempotente
        # (cf. design `rework-reading-grid-as-guide`).
        blurb = existing_blurbs.get(order_key)
        if blurb is None:
            # Pas de phrase préexistante (1re génération, ou nouveau chapitre).
            # Si le chapitre n'a pas non plus d'article, on met le placeholder
            # pour signaler « _à rédiger_ » à l'agent (sinon, on n'affiche
            # pas la zone — c'est un chapitre « _qui marche_ » sans blurb).
            wls = buckets.get(order_key) or []
            if not wls:
                blurb = _BLURB_PLACEHOLDER
                missing[order_key] = title
            else:
                blurb = None
        elif blurb == _BLURB_PLACEHOLDER:
            # Ancien placeholder préservé = à rédiger.
            missing[order_key] = title
        # else: phrase éditoriale existante (non vide, non placeholder) — on l'injecte.
        if blurb is not None:
            lines.append(_BLURB_START)
            lines.append(blurb)
            lines.append(_BLURB_END)
            lines.append("")

        # Liens ordonnés.
        wls = buckets.get(order_key) or []
        if wls:
            lines.append("Lire dans l'ordre :")
            lines.append("")
            for n, wl in enumerate(wls, start=1):
                lines.append(f"{n}. [[{wl}]]")
        else:
            lines.append("_Aucun article rattaché pour l'instant._")
        lines.append("")
        nav = _nav_line(chapters, i, last)
        if nav:
            lines.append(nav)
            lines.append("")

    if orphans:
        lines.append("## Hors chapitrage")
        lines.append("")
        for wl in orphans:
            lines.append(f"- [[{wl}]]")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n", missing


def _nav_line(chapters: list[dict], i: int, last: int) -> str:
    """Ligne `⬅ … | ➡ …` pointant vers les ancres de chapitre Précédent/Suivant."""
    bits: list[str] = []
    if i > 0:
        prev = chapters[i - 1]
        bits.append(f"⬅ [[#{wikilinks.chapter_anchor(prev.get('order'), str(prev.get('title') or ''))}]]")
    if i < last:
        nxt = chapters[i + 1]
        bits.append(f"➡ [[#{wikilinks.chapter_anchor(nxt.get('order'), str(nxt.get('title') or ''))}]]")
    return " | ".join(bits)
