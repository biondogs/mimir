#!/usr/bin/env python3
"""wiki-research — orchestrateur d'un dossier de recherche (SPEC `wiki-research`).

Sous-commandes :

    wiki_research.py --config CFG clip <sujet> <url> [<url> …]
    wiki_research.py --config CFG scaffold <sujet> [--title "…"] [--tag t …]
    wiki_research.py --config CFG update <sujet>
    wiki_research.py --config CFG status <sujet>

- `clip` : pour chaque URL, délègue à `wiki-extract` (clip web déterministe).
  Les clips sont déposés sous `raw/web/` (layout `extract-source` v3 :
  `<slug>-<AAAAMMJJ>.md` + `<slug>-<AAAAMMJJ>.html` immuable + assets par slug).
  Le sujet est encodé dans le **frontmatter** (`subject: <slug>` + `tags: [<slug>]`)
  — convention partagée avec `wiki-batch-produce` (cf. spec `batch-produce`).
  Dédup : on **re-scan `raw/web/` récursivement** pour matcher `source:` dans le
  frontmatter ; un hit → `↩️` (no-op). Pas d'appel réseau, pas d'écrasement
  (immutabilité append-only de `raw/`).
- `scaffold` : produit le squelette `reading-grids/<sujet>.md` (frontmatter
  `type: research-report` + plan de sections). Le **corps** est rédigé
  ultérieurement par l'agent (Karpathy sémantique). Idempotent : un rapport
  existant n'est **pas** écrasé (signal `↩️ existant`).
- `update` : aligne `sources:` du frontmatter sur l'état réel de `raw/web/`,
  bascule `updated:` à aujourd'hui, **préserve** `created:` et le corps.
  Audite les wikilinks du rapport (`[[…]]` cassés → `🔗 invalide`).
- `status` : diagnostic lecture seule (combien de clips, sources alignées,
  wikilinks cassés, `created`/`updated`).

Toutes les écritures sont **confinées** à `work_root` (cf. `guard.assert_within`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# --- bootstrap : importer le socle via le module unifié `bootstrap` ---------
# (L'insertion de sys.path est centralisée dans `bootstrap._ensure_socle_path` :
#  MIMIR_SHARED honoré, idempotent, fail fast sur symbole manquant.)
#
# NOTE : `bootstrap` importe `migration` qui charge le palier v3→v4, lequel a
# besoin de `grid_builder` (issu de `wiki-reading-grid/scripts/`). On insère
# donc le chemin AVANT l'import de `bootstrap`, sans quoi le CLI plante en
# standalone (les tests passent via leur conftest ; bug pré-existant partagé
# avec `wiki-index`, mais on traite notre propre chemin d'import).
sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent / "wiki-reading-grid" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared-references" / "scripts"))
import bootstrap  # noqa: E402,F401  (socle mutualisé ; voir skills/_shared-references/scripts/bootstrap.py)

import config_loader  # noqa: E402
import frontmatter  # noqa: E402
import guard  # noqa: E402
import iohelpers  # noqa: E402
import slug as slug_mod  # noqa: E402
import wikilinks  # noqa: E402

# wiki-extract : import paresseux (pas de réseau requis pour `status`/`update`).
# On injecte `wiki_extract.scripts` sur sys.path pour rendre `import wiki_extract`
# résolvable. `wiki-extract/scripts/` est sibling de notre dossier.
sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent / "wiki-extract" / "scripts"))
import wiki_extract  # noqa: E402  (orchestrateur réutilisé pour le clip web)


# --- structures ------------------------------------------------------------
@dataclass
class ClipRecord:
    """Résultat d'un clip web : où il a été déposé (ou pas)."""

    url: str
    status: str  # "extrait" | "skipped_duplicate" | "error" | "locked" | "ignored"
    content_path: str | None = None
    message: str = ""


@dataclass
class RenvoiCheck:
    """Issue d'un wikilink audité dans le rapport."""

    target: str
    resolved: bool
    reason: str = ""


@dataclass
class ResearchStatus:
    """Diagnostic lecture seule d'un dossier de recherche."""

    sujet: str
    report_path: str
    exists: bool
    created: str | None = None
    updated: str | None = None
    sources_declared: list[str] = field(default_factory=list)
    sources_actual: list[str] = field(default_factory=list)
    clips_count: int = 0
    renvois_invalid: list[RenvoiCheck] = field(default_factory=list)


# --- helpers ---------------------------------------------------------------
def _slugify_sujet(sujet: str) -> str:
    """Slug ASCII du sujet (kebab-case, bornes par slugify du socle)."""
    return slug_mod.slugify(sujet)


def _report_path(cfg, sujet: str) -> Path:
    """Chemin absolu du rapport `reading-grids/<sujet>.md`, confiné."""
    p = cfg.READING_GRIDS / f"{sujet}.md"
    guard.assert_within(cfg.work_root, p)
    return p


def _scan_clips_for_subject(cfg, sujet: str) -> list[Path]:
    """Liste les clips `raw/web/**/*.md` qui portent `subject: <sujet>` en frontmatter.

    Récursif : tolère les layouts plat (`raw/web/foo.md`) et groupé
    (`raw/web/<sujet>/foo.md` si un consumer en aval le crée). Le matching
    est fait par **frontmatter** (et non par chemin), pour rester aligné sur
    la convention `subject:` portée par `wiki-batch-produce` (cf. spec
    `batch-produce` §1).
    """
    out: list[Path] = []
    if not cfg.RAW.is_dir():
        return out
    web_dir = cfg.RAW / "web"
    if not web_dir.is_dir():
        return out
    for md in sorted(web_dir.glob("*.md")):
        try:
            front, _ = frontmatter.parse_frontmatter(md.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
        if _front_subject(front) == sujet:
            out.append(md)
    return out


def _front_subject(front: dict[str, object]) -> str | None:
    """Lit `subject:` du frontmatter (scalaire), normalisé en slug."""
    raw = front.get("subject")
    if not isinstance(raw, str):
        return None
    return raw.strip() or None


def _clip_source_url(md_path: Path) -> str | None:
    """Lit `source:` du frontmatter d'un clip (URL d'origine)."""
    try:
        front, _ = frontmatter.parse_frontmatter(md_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    raw = front.get("source")
    if not isinstance(raw, str):
        return None
    return raw.strip().strip('"').strip("'") or None


def _find_existing_clip(cfg, url: str) -> Path | None:
    """Cherche un clip déjà ingéré pour cette URL (récursif sur `raw/web/`).

    Stricte équivalence : on compare l'URL strippée (cf.
    `wiki_extract._find_ingested_url`, qui lui est non récursif et n'opère
    qu'au top-level). On **étend** ce contrat en récursif pour permettre
    un layout `raw/web/<sujet>/<slug>.md` (cf. spec `batch-produce` §1)
    — l'agent peut produire les deux layouts, on dédoublonne dans les deux.
    """
    if not url or not (cfg.RAW / "web").is_dir():
        return None
    for md in (cfg.RAW / "web").glob("*.md"):
        if _clip_source_url(md) == url:
            return md
    return None


# --- frontmatter helpers ---------------------------------------------------
def _render_frontmatter(front: dict[str, object]) -> str:
    """Sérialise un frontmatter dict en YAML flow-list (sources/tags en `[…]`).

    Cohérent avec `parse_frontmatter` (les `LIST_KEYS` sont parsés en
    `list[str]` si la valeur est un `[…]` flow-list ; sinon en block-list
    YAML). On garde le flow-list car `wiki-batch-produce` et les grilles
    de lecture l'utilisent déjà (cf. `FRONTMATTERS.md` §1).
    """
    LIST_KEYS = {"sources", "tags"}
    lines = ["---"]
    for key, val in front.items():
        if key in LIST_KEYS and isinstance(val, list):
            if not val:
                lines.append(f"{key}:")
            else:
                items = ", ".join(_yaml_scalar(v) for v in val)
                lines.append(f"{key}: [{items}]")
        else:
            lines.append(f"{key}: {_yaml_scalar(val)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _yaml_scalar(val: object) -> str:
    """Sérialise une valeur scalaire en YAML safe (string quoted si besoin)."""
    s = str(val)
    # si la chaîne contient des caractères YAML-sensibles, on la quote en double
    if any(c in s for c in ":#{}[]&*?|>!%@`, '\""):
        s_escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s_escaped}"'
    return s


# --- sous-commandes --------------------------------------------------------
def cmd_clip(
    cfg, sujet: str, urls: list[str], *, lang: str, dry_run: bool, skip_sync: bool
) -> list[ClipRecord]:
    """Clippe chaque URL via `wiki-extract` vers `raw/web/`, dédup par URL.

    Le **sujet n'est pas** passé à `wiki-extract` (qui ne le connaît pas) ;
    il est ajouté au frontmatter **après** le clip, par post-traitement du
    fichier résultant. C'est un compromis : on garde le contrat de
    `wiki-extract` intact, on injecte le sujet via une ré-écriture frontmatter
    bornée (`guard.assert_within`, `atomic_write_text`).
    """
    sujet_slug = _slugify_sujet(sujet)
    if sujet_slug != sujet:
        # l'utilisateur a passé un texte libre ; on logge le slug effectif
        print(f"ℹ️  sujet slugifié : {sujet!r} → {sujet_slug!r}", file=sys.stderr)
    records: list[ClipRecord] = []

    for url in urls:
        existing = _find_existing_clip(cfg, url)
        if existing is not None:
            # dédup : on **complète** le frontmatter si `subject:` manque
            # (clip antérieur, fait par `wiki-extract` direct, sans sujet).
            front, body = frontmatter.parse_frontmatter(existing.read_text(encoding="utf-8"))
            if _front_subject(front) != sujet_slug:
                front["subject"] = sujet_slug
                tags = list(front.get("tags") or [])
                if sujet_slug not in tags:
                    tags.append(sujet_slug)
                front["tags"] = tags
                _write_md(cfg, existing, _render_frontmatter(front) + body)
            records.append(ClipRecord(
                url=url, status="skipped_duplicate",
                content_path=str(existing),
                message=f"URL déjà clippée ({existing.name})",
            ))
            continue

        # appel réel à wiki-extract (verrou + pull/push inclus, gérés en interne)
        outcomes = wiki_extract.run(cfg, url, lang=lang, dry_run=dry_run, skip_sync=skip_sync)
        for out in outcomes:
            if out.status == "locked":
                records.append(ClipRecord(url=url, status="locked", message=out.message))
                continue
            if out.status != "extrait" or out.content_path is None:
                records.append(ClipRecord(
                    url=url, status=out.status,
                    message=out.message or "extraction échouée",
                ))
                continue
            # on annote le clip avec le sujet (idempotent : si déjà OK, no-op)
            md_path = Path(out.content_path)
            _tag_clip_with_subject(cfg, md_path, sujet_slug)
            records.append(ClipRecord(
                url=url, status="extrait",
                content_path=str(md_path),
                message=out.message,
            ))
    return records


def _tag_clip_with_subject(cfg, md_path: Path, sujet_slug: str) -> None:
    """Injecte `subject:` + `tags:` dans le frontmatter d'un clip fraichement écrit.

    Pas d'invention de contenu (SPEC §12.5) : on **ne touche pas** au corps, on
    complète seulement les métadonnées. Idempotent : ré-appel avec le même sujet
    est un no-op.
    """
    text = md_path.read_text(encoding="utf-8")
    front, body = frontmatter.parse_frontmatter(text)
    if _front_subject(front) == sujet_slug:
        return  # déjà tagué
    front["subject"] = sujet_slug
    tags = list(front.get("tags") or [])
    if sujet_slug not in tags:
        tags.append(sujet_slug)
    front["tags"] = tags
    _write_md(cfg, md_path, _render_frontmatter(front) + body)


def _write_md(cfg, path: Path, text: str) -> None:
    """Écrit un `.md` de façon atomique, confinée à `work_root`."""
    guard.assert_within(cfg.work_root, path)
    iohelpers.atomic_write_text(path, text, work_root=cfg.work_root)


# --- scaffold --------------------------------------------------------------
_SCAFFOLD_BODY = """\
# {title}

> Synthèse sur **{title}** (rédigée par l'agent ; sections indicatives).

## Angle
_Poser la question de recherche, le périmètre, les angles morts._

## Synthèse
_3 à 6 paragraphes denses, reformulant les clips — pas de copier-coller._

## Points de friction
_Contradictions, désaccords, zones d'ombre entre les sources clippées._

## Mermaid
```mermaid
flowchart LR
  A["{title}"]
```

## Sources
_Liste des clips ingérés (renvois auto-générés par `update`)._

## Renvois wiki
_Liens vers les articles `[[<sujet>/<notion>]]` déjà produits par ailleurs._
"""


def cmd_scaffold(
    cfg, sujet: str, *, title: str | None = None, extra_tags: list[str] | None = None,
    dry_run: bool = False,
) -> Path:
    """Produit le squelette `reading-grids/<sujet>.md` (frontmatter + plan).

    Idempotent : un rapport **existant** n'est pas écrasé (`↩️`). Le **corps**
    n'est jamais réécrit par `update` (cf. SPEC §4.1 du change). Pour forcer
    une régénération, supprimer manuellement le rapport au préalable.
    """
    sujet_slug = _slugify_sujet(sujet)
    dest = _report_path(cfg, sujet_slug)
    if dest.exists():
        print(f"↩️  rapport déjà existant : {dest}", file=sys.stderr)
        return dest

    today = date.today().isoformat()
    title_val = title or sujet
    tags = ["report", sujet_slug]
    if extra_tags:
        tags.extend(extra_tags)

    front = {
        "title": title_val,
        "type": "research-report",
        "topic": title_val,
        "subject": sujet_slug,
        "created": today,
        "updated": today,
        "sources": [],
        "tags": tags,
    }
    body = _SCAFFOLD_BODY.format(title=title_val)
    text = _render_frontmatter(front) + "\n" + body

    if dry_run:
        print(f"(dry-run) scaffold à créer : {dest}", file=sys.stderr)
        return dest

    _write_md(cfg, dest, text)
    print(f"✅ scaffold écrit : {dest}", file=sys.stderr)
    return dest


# --- update ---------------------------------------------------------------
def cmd_update(cfg, sujet: str, *, dry_run: bool = False) -> ResearchStatus:
    """Met à jour le rapport : synchronise `sources:`, préserve `created:`.

    Recalcule les renvois (wikilinks) en auditant chaque `[[…]]` du rapport.
    Renvoie un `ResearchStatus` (toujours, même en dry-run).
    """
    sujet_slug = _slugify_sujet(sujet)
    dest = _report_path(cfg, sujet_slug)
    if not dest.exists():
        raise FileNotFoundError(
            f"rapport introuvable : {dest} — lance d'abord `scaffold {sujet_slug}`"
        )

    text = dest.read_text(encoding="utf-8")
    front, body = frontmatter.parse_frontmatter(text)
    today = date.today().isoformat()

    # 1) sources = URLs effectives des clips de ce sujet
    clips = _scan_clips_for_subject(cfg, sujet_slug)
    actual_urls: list[str] = []
    for clip in clips:
        url = _clip_source_url(clip)
        if url:
            actual_urls.append(url)
    # on conserve l'ordre lexicographique (stable, déterministe)
    actual_urls = sorted(set(actual_urls))

    # 2) renvois = on audite le corps (pas le frontmatter)
    renvois = _audit_renvois(cfg, dest, body)

    # 3) frontmatter : on n'écrase PAS `created`, on met `updated`
    created = front.get("created") or today
    front["created"] = created
    front["updated"] = today
    front["sources"] = actual_urls
    front.setdefault("type", "research-report")
    front.setdefault("topic", sujet_slug)
    front.setdefault("subject", sujet_slug)
    tags = list(front.get("tags") or [])
    if "report" not in tags:
        tags.insert(0, "report")
    if sujet_slug not in tags:
        tags.append(sujet_slug)
    front["tags"] = tags

    new_text = _render_frontmatter(front) + "\n" + body
    if not dry_run:
        _write_md(cfg, dest, new_text)
        print(
            f"✅ rapport mis à jour : {dest} "
            f"(sources={len(actual_urls)}, renvois_invalides={len([r for r in renvois if not r.resolved])})",
            file=sys.stderr,
        )

    return ResearchStatus(
        sujet=sujet_slug,
        report_path=str(dest),
        exists=True,
        created=created,
        updated=today,
        sources_declared=list(front.get("sources") or []),
        sources_actual=actual_urls,
        clips_count=len(clips),
        renvois_invalid=[r for r in renvois if not r.resolved],
    )


# --- audit des renvois -----------------------------------------------------
def _audit_renvois(cfg, report_path: Path, body: str) -> list[RenvoiCheck]:
    """Vérifie que chaque wikilink du rapport résout vers un fichier réel.

    On utilise `wikilinks.resolve` (résout `wiki/x`, `sujet/notion`, relatif au
    fichier). On vérifie en plus les cibles **dans `raw/web/`** (qui ne sont
    pas des articles wiki mais des clips) — `wikilinks.resolve` les couvre
    déjà via son premier candidat (préfixée depuis `work_root`).
    """
    targets = wikilinks.extract_wikilinks(body)
    out: list[RenvoiCheck] = []
    for target in targets:
        resolved = wikilinks.resolve(cfg, report_path, target)
        if resolved is None:
            out.append(RenvoiCheck(target=target, resolved=False, reason="aucun fichier résolu"))
        else:
            out.append(RenvoiCheck(target=target, resolved=True))
    return out


# --- status ---------------------------------------------------------------
def cmd_status(cfg, sujet: str) -> ResearchStatus:
    sujet_slug = _slugify_sujet(sujet)
    dest = _report_path(cfg, sujet_slug)
    if not dest.exists():
        return ResearchStatus(sujet=sujet_slug, report_path=str(dest), exists=False)
    front, body = frontmatter.parse_frontmatter(dest.read_text(encoding="utf-8"))
    clips = _scan_clips_for_subject(cfg, sujet_slug)
    actual_urls = sorted({
        u for u in (_clip_source_url(c) for c in clips) if u
    })
    declared = list(front.get("sources") or [])
    renvois = _audit_renvois(cfg, dest, body)
    return ResearchStatus(
        sujet=sujet_slug,
        report_path=str(dest),
        exists=True,
        created=str(front.get("created") or ""),
        updated=str(front.get("updated") or ""),
        sources_declared=declared,
        sources_actual=actual_urls,
        clips_count=len(clips),
        renvois_invalid=[r for r in renvois if not r.resolved],
    )


# --- main -----------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dossier de recherche (sélection de sources + rapport) — Mimir."
    )
    parser.add_argument("--config", default=None,
                        help="Chemin du wiki.config.json (auto-découverte si omis).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_clip = sub.add_parser("clip", help="Clippe N URLs sur le sujet (dédup par URL).")
    p_clip.add_argument("sujet", help="Texte du sujet (slugifié en kebab-case).")
    p_clip.add_argument("urls", nargs="+", help="URLs à clipper.")
    p_clip.add_argument("--lang", default="fra+eng")
    p_clip.add_argument("--dry-run", action="store_true")
    p_clip.add_argument("--skip-sync", action="store_true")

    p_scaf = sub.add_parser("scaffold", help="Produit le squelette du rapport.")
    p_scaf.add_argument("sujet", help="Texte du sujet (slugifié en kebab-case).")
    p_scaf.add_argument("--title", default=None, help="Titre humain (défaut = sujet).")
    p_scaf.add_argument("--tag", action="append", default=[], help="Tag additionnel (répétable).")
    p_scaf.add_argument("--dry-run", action="store_true")

    p_upd = sub.add_parser("update", help="Met à jour sources:/updated: + audite les renvois.")
    p_upd.add_argument("sujet")
    p_upd.add_argument("--dry-run", action="store_true")

    p_st = sub.add_parser("status", help="Diagnostic lecture seule.")
    p_st.add_argument("sujet")

    args = parser.parse_args(argv)
    cfg = config_loader.load_resolved_config(args.config)

    if args.cmd == "clip":
        records = cmd_clip(
            cfg, args.sujet, args.urls,
            lang=args.lang, dry_run=args.dry_run, skip_sync=args.skip_sync,
        )
        print(json.dumps(
            [{"url": r.url, "status": r.status, "path": r.content_path, "message": r.message}
             for r in records],
            ensure_ascii=False, indent=2,
        ))
        # Code retour : 0 si au moins un succès OU que des dédup, 1 si tout en erreur.
        if not records:
            return 1
        if all(r.status in ("error", "locked") for r in records):
            return 1
        return 0

    if args.cmd == "scaffold":
        dest = cmd_scaffold(
            cfg, args.sujet, title=args.title, extra_tags=args.tag, dry_run=args.dry_run,
        )
        print(json.dumps({"path": str(dest), "created": dest.exists()}, ensure_ascii=False))
        return 0

    if args.cmd == "update":
        st = cmd_update(cfg, args.sujet, dry_run=args.dry_run)
        print(json.dumps({
            "sujet": st.sujet, "report": st.report_path,
            "created": st.created, "updated": st.updated,
            "sources_declared": st.sources_declared,
            "sources_actual": st.sources_actual,
            "clips_count": st.clips_count,
            "renvois_invalid": [{"target": r.target, "reason": r.reason} for r in st.renvois_invalid],
        }, ensure_ascii=False, indent=2))
        return 0 if not st.renvois_invalid else 1

    if args.cmd == "status":
        st = cmd_status(cfg, args.sujet)
        if not st.exists:
            print(f"⚠️  aucun rapport pour {st.sujet!r} (attendu : {st.report_path})",
                  file=sys.stderr)
            return 1
        print(json.dumps({
            "sujet": st.sujet, "report": st.report_path,
            "created": st.created, "updated": st.updated,
            "clips_count": st.clips_count,
            "sources_declared": st.sources_declared,
            "sources_actual": st.sources_actual,
            "sources_diff": sorted(set(st.sources_declared) ^ set(st.sources_actual)),
            "renvois_invalid": [{"target": r.target, "reason": r.reason} for r in st.renvois_invalid],
        }, ensure_ascii=False, indent=2))
        return 0 if not st.renvois_invalid else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
