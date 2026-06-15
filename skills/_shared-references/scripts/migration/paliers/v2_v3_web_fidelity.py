#!/usr/bin/env python3
"""Palier de migration **v2→v3** — clipping web fidèle (refetch + .html + assets).

Change `refit-web-extraction-fidelity` (palier versionné). Pour chaque
`raw/web/*.md` **dépourvu** de `.html` source voisin, refetch la page depuis
le `source:` du frontmatter, puis écrit :
- `<slug>-<date>.html` (immuable, analogue du `.pdf` binaire) ;
- un `.md` propre (re-dérivé du HTML, strip `<script>`/`<style>`) ;
- les images sous `_assets/<slug>-<date>/` (regroupées, cohérent v1→v2).

Le palier est **best-effort** : un refetch en échec (URL morte, fetch
refusé, contenu vide) **laisse l'original intact** et inscrit l'URL au
rapport. Jamais d'écrasement par du vide.

**Reprise** : un fichier migré reçoit `schema_version: 3` (et
`refetched: <date>`) au frontmatter. Une campagne interrompue peut
recommencer sur les seuls fichiers dont `schema_version < 3` (sélection
effectuée par le `plan`).

**Doublons** : les re-clips (`foo`, `foo-2`, `foo-3`) sont **listés** dans
le rapport du plan, **pas fusionnés** (cf. design — l'append-only de
`raw/` garde la première copie).
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import frontmatter
import iohelpers

from migration import plan, registry
from migration.lazy import _set_schema_version_in_text


#: Header HTTP User-Agent (le même que `extractors/web._UA`).
_UA = {"User-Agent": "Mozilla/5.0 (compatible; MimirWikiExtract/1.0)"}


# --- rapport ---------------------------------------------------------------
@dataclass
class MigrationReport:
    """Rapport best-effort du palier v2→v3.

    Collecté pendant le `plan` (lecture seule, dry-run-friendly) et
    affiché/mis à jour pendant l'`apply`. Les entrées de doublons sont
    **détectées au plan** (par signature du frontmatter `source:`), les
    échecs de refetch sont **détectés à l'apply**.
    """

    refetched: list[str] = field(default_factory=list)      # URLs (ou slugs) refetchés OK
    skipped_no_html: list[str] = field(default_factory=list)  # .md qui n'ont rien à faire
    skipped_in_schema: list[str] = field(default_factory=list)  # déjà à schema >= 3
    refetch_failed: list[str] = field(default_factory=list)  # URLs en échec
    duplicates: list[list[str]] = field(default_factory=list)  # groupes de .md doublons

    def render(self) -> str:
        lines = ["Rapport palier v2→v3 (clipping web fidèle) :"]
        lines.append(f"  • refetch réussi : {len(self.refetched)}")
        lines.append(f"  • déjà à schema >= 3 : {len(self.skipped_in_schema)}")
        lines.append(f"  • rien à faire (pas de source: à refetch) : {len(self.skipped_no_html)}")
        lines.append(f"  • refetch ÉCHOUÉ (original intact) : {len(self.refetch_failed)}")
        if self.duplicates:
            lines.append(f"  • doublons probables (groupes) : {len(self.duplicates)}")
            for grp in self.duplicates:
                lines.append(f"    - {', '.join(grp)}")
        if self.refetch_failed:
            lines.append("  URLs en échec (à traiter manuellement) :")
            for u in self.refetch_failed:
                lines.append(f"    - {u}")
        return "\n".join(lines)


# --- détection des candidats ------------------------------------------------
def _read_source_url(md_path: Path) -> str | None:
    """Lit le `source:` du frontmatter d'un `.md` web. `None` si absent / vide."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.lstrip().startswith("---"):
        return None
    try:
        front, _ = frontmatter.parse_frontmatter(text)
    except Exception:
        return None
    src = front.get("source")
    if not isinstance(src, str) or not src.strip():
        return None
    return src.strip()


def _has_html_neighbor(md_path: Path) -> bool:
    """Vrai si `<md_path>.html` (ou avec suffixe `-N`) existe à côté du `.md`."""
    candidates = [md_path.with_suffix(".html"), md_path.with_suffix(md_path.suffix + ".html")]
    # Le plus simple : `md_path` est `foo-N.md`, le `.html` est `foo-N.html`.
    # `Path.with_suffix` n'ajoute pas le suffixe correctement quand l'extension
    # est `.md` — passons par `with_name` pour reconstruire.
    candidates.append(md_path.with_name(md_path.name.removesuffix(".md") + ".html"))
    return any(c.is_file() for c in candidates)


def _schema_version_at_least(md_path: Path, target: int) -> bool:
    """Vrai si le `.md` porte `schema_version >= target` (ou ≥ 3, ici)."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    try:
        front, _ = frontmatter.parse_frontmatter(text)
    except Exception:
        return False
    v = frontmatter.read_schema_version(front)
    return v is not None and v >= target


# --- groupement par signature source (doublons) ----------------------------
def _detect_duplicate_groups(web_dir: Path) -> list[list[str]]:
    """Groupe les `.md` web par `source:` du frontmatter (URL identique).

    Renvoie une liste de groupes (≥ 2 entrées). Ne signale que les
    collisions, pas les `.md` uniques. Le palier NE supprime ni ne
    fusionne : il liste (cf. design — l'append-only `raw/` garde la
    première copie).
    """
    by_source: dict[str, list[str]] = {}
    for md in sorted(web_dir.glob("*.md")):
        src = _read_source_url(md)
        if not src:
            continue
        by_source.setdefault(src, []).append(md.name)
    return [grp for grp in by_source.values() if len(grp) >= 2]


# --- plan ------------------------------------------------------------------
def _build_plan(cfg) -> plan.Plan:
    """Plan dry-run : liste les `.md` candidats au refetch (et signale les doublons).

    Le `plan.Plan` reste un objet « sec » : il n'effectue **aucun** refetch.
    Les opérations émises sont des `WRITE` à venir, avec `content=""` comme
    placeholder (sera rempli par `_apply` lors de l'apply réel). Le moteur
    n'exécute pas `_atomic_apply` pour ce palier (on a un `apply` custom
    qui fait le refetch + les écritures atomiques lui-même, après le
    snapshot).
    """
    p = plan.Plan(description="refetch des clippings web sans .html source (v2→v3)")
    web_dir = cfg.RAW / "web"
    if not web_dir.is_dir():
        return p
    for md in sorted(web_dir.glob("*.md")):
        if _has_html_neighbor(md):
            continue  # déjà migré (le .html existe à côté)
        if _schema_version_at_least(md, target=3):
            continue  # déjà à schema >= 3 (marqué par un apply précédent)
        src = _read_source_url(md)
        if not src:
            continue  # pas de source: à refetch — skip (signalé au rapport)
        # Le `plan` n'a pas vocation à effectuer le refetch : on marque juste
        # l'intention via un WRITE avec content="" (placeholder, jamais écrit
        # tel quel — l'apply custom fait le vrai write après refetch).
        p.add(plan.WRITE(md, content="<refetch-pending>"))
    return p


# --- refetch (helper isolé pour faciliter le test sans réseau) -------------
def _refetch_html(url: str, opener: Callable | None = None) -> str:
    """Fetch une URL en HTTP(S). `opener` est injectable pour les tests."""
    try:
        import requests
    except ImportError:
        requests = None  # type: ignore
    if requests is not None:
        r = requests.get(url, headers=_UA, timeout=20)
        r.raise_for_status()
        return r.text
    if opener is None:
        opener = urllib.request.build_opener()
        opener.addheaders = list(_UA.items())
    with opener.open(url, timeout=20) as resp:  # noqa: S310 (URL validée par le caller)
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, "replace")


# --- apply -----------------------------------------------------------------
def _apply(cfg, p: plan.Plan) -> None:
    """Effectue le refetch + écrit le triplet (.html, .md, assets) + bump.

    Stratégie : on refetch **directement** dans le filesystem (le `plan`
    ne contient qu'une intention — l'`apply` fait le travail réel). On
    réutilise la même chaîne d'extraction que `wiki-extract` via
    `extractors.web.extract` (sans réécrire le pipeline). En cas d'échec,
    l'original reste intact et l'URL est ajoutée au rapport (jamais
    d'écrasement par du vide, cf. design).
    """
    from extractors import web as web_extractor
    from extractors.base import ExtractorError

    web_dir = cfg.RAW / "web"
    if not web_dir.is_dir():
        return

    report = _build_report(cfg)

    # On itère les `WRITE` (qui sont des marqueurs d'intention) et on refetch
    # pour chacun. L'astuce : pour récupérer l'URL, on relit le frontmatter
    # du `.md` à la volée (le `plan` n'a pas stocké l'URL — laissons-le
    # dry-run).
    for op in p.operations:
        if op.action != "write":
            continue
        md_path = op.dst  # le plan a utilisé le .md comme dst
        src_url = _read_source_url(md_path)
        if not src_url:
            report.skipped_no_html.append(md_path.name)
            continue
        try:
            html = _refetch_html(src_url)
        except Exception as exc:
            report.refetch_failed.append(f"{src_url} ({exc!r})")
            continue  # original INTACT (cf. design)
        # Chaîne d'extraction HTML→markdown (mêmes garanties que wiki-extract).
        try:
            result = web_extractor.extract(src_url, lang="fra+eng")
            # `extract` refetch en interne, mais on a déjà le HTML — on
            # pourrait économiser l'appel. Le double-fetch est assumé (le
            # réseau est non-déterministe de toute façon) ; en échange, on
            # hérite de la chaîne complète (frontmatter, title, etc.).
            html = result.source_content.decode("utf-8") if result.source_content else html
            markdown = result.raw_content
            assets = result.assets
        except ExtractorError as exc:
            report.refetch_failed.append(f"{src_url} ({exc})")
            continue

        # 1) Écrire le .html immuable à côté du .md
        html_path = md_path.with_name(md_path.name.removesuffix(".md") + ".html")
        iohelpers.atomic_write_bytes(
            html_path, html.encode("utf-8"), work_root=cfg.work_root
        )

        # 2) Réécrire le .md avec le nouveau contenu + frontmatter à jour
        # (title, refetched, schema_version=3).
        # On garde le `created` d'origine (s'il existe) et on ajoute
        # `refetched` et `schema_version`.
        existing_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        existing_front: dict = {}
        if existing_text.lstrip().startswith("---"):
            try:
                existing_front, _ = frontmatter.parse_frontmatter(existing_text)
            except Exception:
                pass
        from datetime import date as _date
        new_front = {
            "title": result.metadata.get("title") or existing_front.get("title") or src_url,
            "source": src_url,
            "type": "web",
            "created": existing_front.get("created") or result.metadata.get("created") or _date.today().isoformat(),
            "tags": existing_front.get("tags", ["clippings"]),
            "schema_version": 3,
            "refetched": _date.today().isoformat(),
        }
        # Reconstruit le frontmatter à la main (cohérent avec `_web_frontmatter`
        # de `wiki_extract.py`).
        title = str(new_front["title"]).replace('"', "'")
        created = new_front["created"]
        tags = new_front["tags"]
        new_text = (
            "---\n"
            f'title: "{title}"\n'
            f"source: {new_front['source']}\n"
            "type: web\n"
            f"created: {created}\n"
            f"refetched: {new_front['refetched']}\n"
            f"schema_version: {new_front['schema_version']}\n"
            f"tags: {tags if isinstance(tags, list) else '[' + tags + ']'}\n"
            "---\n\n"
            + markdown
        )

        # 3) Écrire les assets sous _assets/<slug>/
        # `<slug>` = nom de fichier `.md` sans extension, qui matche
        # `<slug>-<date>.md` (le `_slug_base` ajoute le suffixe date).
        slug = md_path.name.removesuffix(".md")
        assets_dir = web_dir / cfg.ASSETS_DIRNAME / slug
        for asset in assets:
            asset_path = assets_dir / asset.filename
            iohelpers.atomic_write_bytes(asset_path, asset.data, work_root=cfg.work_root)
            # Réécriture des liens d'image dans le markdown.
            if asset.original_ref:
                rel = f"{cfg.ASSETS_DIRNAME}/{slug}/{asset.filename}"
                new_text = new_text.replace(asset.original_ref, rel)

        iohelpers.atomic_write_text(md_path, new_text, work_root=cfg.work_root)
        report.refetched.append(src_url)

    # Le rapport est imprimé sur stdout — l'opérateur le voit dans le
    # digest du run. Le moteur continue ensuite vers validate + bump.
    print(report.render())


# --- rapport détaillé (pour l'apply) ----------------------------------------
def _build_report(cfg) -> MigrationReport:
    """Construit un rapport à partir de l'état du dossier `raw/web/`.

    Capture les doublons (lecture seule) et les fichiers déjà à schema
    >= 3. Les refetch OK/KO sont remplis pendant l'`apply`.
    """
    rep = MigrationReport()
    web_dir = cfg.RAW / "web"
    if not web_dir.is_dir():
        return rep
    rep.duplicates = _detect_duplicate_groups(web_dir)
    for md in sorted(web_dir.glob("*.md")):
        if _schema_version_at_least(md, target=3):
            rep.skipped_in_schema.append(md.name)
    return rep


#: Le palier de migration v2→v3 — exporté pour les tests et l'enregistrement.
#: Note : `preserves_counts=False` parce que le refetch **crée** des
#: nouveaux fichiers (`.html`, `_assets/<slug>/*`) qui n'existaient pas
#: avant — l'invariant « counts préservés » du socle ne s'applique
#: pas aux paliers « créateurs » (cf. `Migration.preserves_counts`).
PALIER = registry.Migration(
    from_schema=2, to_schema=3,
    plan=_build_plan, apply=_apply,
    description="refetch des clippings web (v2→v3) : .html immuable + .md propre + assets par slug",
    preserves_counts=False,
)


def register() -> registry.Migration:
    """Enregistre le palier v2→v3 dans le registre."""
    return registry.register(PALIER)
