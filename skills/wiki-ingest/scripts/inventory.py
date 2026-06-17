#!/usr/bin/env python3
"""Inventaire de `raw/` et sélection des sources à (re)compiler (SPEC §7.2–§7.3).

On liste les **fichiers de contenu** (`.pdf.txt` / `.epub.txt` / web `.md` /
notes `.md`) — jamais les binaires, ni les sidecars de service (`INDEX.md`,
`_status.md`, `*.toc.json`, `_assets/`, **et `*.src.md`** pour les notes) — et
on calcule leur SHA256. La sélection compare au ledger : une source absente
ou au SHA différent est « à compiler » ; sinon elle est déjà à jour
(idempotence).

> ⚠️ **Piège principal `notes`** : `_CONTENT_EXT["notes"] = ".md"` matche
> `<base>.src.md` ET `<base>.md` (les deux finissent par `.md`). L'original
> immuable `.src.md` est un sidecar — il NE DOIT PAS apparaître dans la
> worklist, sinon double compilation d'une même note. Le filtre `_SKIP_NAMES`
> est étendu dynamiquement pour exclure `*.src.md` du scan. Cf. design
> `route-md-inbox-notes::Décision 2`.

Le digest de statut (`status_digest`) expose, **par source** et de manière
machine-lisible, le statut de compilation dérivé du ledger et de `_status.md` :

- `extrait` (sidecars présents, **0 article** compilé) — `article_count = 0`
- `partiellement compilé` (≥1 article mais statut `_status.md` ≠ `compilé`)
- `compilé` (`_status.md` = `compilé`, count = N articles du ledger)

Une source à 0 article n'est **jamais** présentée comme `compilé` (cf. spec
`compile-source` « Extracted-but-not-compiled is reported honestly »).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import iohelpers
import ledger as ledger_mod
import status_table

#: Extension du fichier de contenu par type de document.
_CONTENT_EXT = {"pdfs": ".pdf.txt", "epubs": ".epub.txt", "web": ".md", "notes": ".md"}
_SKIP_NAMES = {"INDEX.md", "_status.md", ".DS_Store"}

#: Statut final d'une source dans le digest (cf. spec `compile-source`).
STATUSES = ("extrait", "partiellement compilé", "compilé")


@dataclass
class ContentFile:
    """Un fichier de contenu raw prêt à être ingéré."""

    content_path: str   # relatif à work_root (posix), ex. "raw/pdfs/x.pdf.txt"
    doc_type: str       # pdfs | epubs | web
    sha256: str
    toc_path: str | None  # relatif à work_root, ou None (web)

    def as_dict(self) -> dict:
        return {
            "content_path": self.content_path,
            "doc_type": self.doc_type,
            "sha256": self.sha256,
            "toc_path": self.toc_path,
        }


def _rel(path: Path, work_root: Path) -> str:
    return path.relative_to(work_root).as_posix()


def scan_content(cfg) -> list[ContentFile]:
    """Liste tous les fichiers de contenu de `raw/{pdfs,epubs,web,notes}` avec leur SHA.

    Exclut explicitement les sidecars `*.src.md` (notes) : seul le `.md` dérivé
    est compilable. Le `.src.md` est la matière immuable — l'original verbatim
    jamais compilé.
    """
    out: list[ContentFile] = []
    work_root = cfg.work_root
    for doc_type, ext in _CONTENT_EXT.items():
        type_dir = cfg.RAW / doc_type
        if not type_dir.is_dir():
            continue
        for p in sorted(type_dir.iterdir()):
            if not p.is_file() or p.name in _SKIP_NAMES or p.name.startswith("."):
                continue
            if not p.name.endswith(ext):
                continue
            # ⚠️ notes : filtrer `*.src.md` (matière immuable, jamais compilée)
            if p.name.endswith(".src.md"):
                continue
            base = p.name[: -len(ext)]
            toc = type_dir / f"{base}.toc.json"
            out.append(
                ContentFile(
                    content_path=_rel(p, work_root),
                    doc_type=doc_type,
                    sha256=iohelpers.sha256_file(p),
                    toc_path=_rel(toc, work_root) if toc.is_file() else None,
                )
            )
    return out


def existing_notions(cfg) -> dict[str, list[str]]:
    """Notions déjà présentes par sujet (`{sujet: [notion-slug, …]}`) — informe l'antidoublon."""
    out: dict[str, list[str]] = {}
    if not cfg.WIKI.is_dir():
        return out
    for subject_dir in sorted(cfg.WIKI.iterdir()):
        if not subject_dir.is_dir() or subject_dir.name.startswith("_"):
            continue
        notions = sorted(
            p.stem
            for p in subject_dir.iterdir()
            if p.is_file() and p.suffix == ".md" and not p.name.startswith("_")
        )
        if notions:
            out[subject_dir.name] = notions
    return out


def select(cfg, led: dict[str, ledger_mod.LedgerEntry], source: str | None = None) -> dict:
    """Worklist des sources à compiler + notions existantes par sujet.

    `source` (optionnel) restreint l'inventaire à un seul fichier de contenu
    (chemin relatif à work_root ou absolu sous raw/).
    """
    files = scan_content(cfg)
    if source:
        wanted = Path(source)
        wanted_rel = (
            wanted.as_posix()
            if not wanted.is_absolute()
            else wanted.resolve().relative_to(cfg.work_root).as_posix()
        )
        files = [f for f in files if f.content_path == wanted_rel]
    worklist = [
        f.as_dict()
        for f in files
        if ledger_mod.needs_compile(led, f.content_path, f.sha256)
    ]
    return {"worklist": worklist, "existing_notions": existing_notions(cfg)}


# --- digest de statut par source (spec `compile-source`) -------------------
@dataclass
class StatusEntry:
    """Statut de compilation d'une source + compte d'articles (ledger)."""

    content_path: str   # chemin relatif à work_root (posix)
    doc_type: str       # pdfs | epubs | web
    status: str         # extrait | partiellement compilé | compilé
    article_count: int  # taille de ledger[content_path].articles (0 si absent)

    def as_dict(self) -> dict:
        return {
            "content_path": self.content_path,
            "doc_type": self.doc_type,
            "status": self.status,
            "article_count": self.article_count,
        }


def _derive_status(article_count: int, status_row: status_table.StatusRow | None) -> str:
    """Dérive le statut final à partir du compte d'articles et de `_status.md`.

    Règle d'or (spec `compile-source` « Extracted-but-not-compiled is reported
    honestly ») : **0 article ne peut jamais devenir `compilé`**. La seule
    branche qui produit `compilé` exige `article_count ≥ 1` ET
    `status_row.statut == "compilé"`.
    """
    if article_count <= 0:
        return "extrait"
    if status_row is not None and status_row.statut == "compilé":
        return "compilé"
    return "partiellement compilé"


def _load_status_rows_cached(cfg, doc_type: str, _cache: dict[str, list[status_table.StatusRow]]) -> list[status_table.StatusRow]:
    """Charge `_status.md` une seule fois par `doc_type` (évite les I/O répétés)."""
    if doc_type not in _cache:
        _cache[doc_type] = status_table.load_status(cfg.RAW / doc_type / "_status.md")
    return _cache[doc_type]


def status_digest(cfg) -> dict:
    """Digest machine-lisible du statut de compilation **par source**.

    Combine `scan_content` (énumération des sources), le ledger (compte
    d'articles) et `_status.md` (statut posé par `finalize`). Une source
    `extrait` (sidecars présents, 0 article) n'est **jamais** étiquetée
    `compilé` — voir `_derive_status` pour la garantie.
    """
    led = ledger_mod.load_ledger(cfg.LEDGER)
    cache: dict[str, list[status_table.StatusRow]] = {}
    out: list[StatusEntry] = []
    for f in scan_content(cfg):
        rows = _load_status_rows_cached(cfg, f.doc_type, cache)
        row = next((r for r in rows if r.fichier == Path(f.content_path).name), None)
        entry = led.get(f.content_path)
        count = len(entry.articles) if entry is not None else 0
        out.append(
            StatusEntry(
                content_path=f.content_path,
                doc_type=f.doc_type,
                status=_derive_status(count, row),
                article_count=count,
            )
        )
    return {"sources": [e.as_dict() for e in out]}
