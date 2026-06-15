#!/usr/bin/env python3
"""wiki-batch-produce — production déterministe d'articles Karpathy en batch (spec `batch-produce`).

Voie **rapide** (vs `wiki-ingest` qui est sémantique) : transforme N sources web
homogènes (`raw/web/<sujet>/*.md`) en N articles `wiki/<sujet>/<slug>.md` **sans
appel LLM**. Le contenu de l'article vient **intégralement** de la source — pas
d'invention. La décomposition en notions est **bijective** : 1 source = 1 article.

Garanties (spec `batch-produce`) :
- **Web uniquement** : refuse `.pdf` / `.epub` / sources chapitrées (aiguillage
  vers `wiki-ingest` qui honore le chapitrage `#chK`).
- **Idempotent** : `wiki/<sujet>/<slug>.md` existant → `↩️`, rien n'est réécrit.
- **Validation post-écriture + rollback** : ≥ 4 sections `##`, bloc ` ```mermaid `
  ouvert **et** fermé, taille ≥ 500 octets. Échec → `os.remove(dest)` + `❌`.
- **Frontmatter flow-list** : `sources: [...]`, `tags: [...]` — parsable par le
  lecteur de frontmatter partagé.
- **Sériel** : pas de parallélisme (évite races, rollbacks concurrents, ledger
  corrompu).
- **Per-source finalize** : un `wiki-ingest finalize` par source réussie → ledger
  + `_status.md` à jour (cohérent avec `wiki-reading-grid`).
- **Audit best-effort** : `wiki-index audit` est appelé une fois à la fin, son
  résultat est loggé **sans abort** (un wiki neuf a des liens non encore résolus).

CLI :
    batch_produce.py [--config <wiki.config.json>] --inbox-dir raw/web/<sujet> --subject <sujet> [--skip-sync] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# --- bootstrap : importer le socle via le module unifié `bootstrap` ---------
# (L'insertion de sys.path est centralisée dans `bootstrap._ensure_socle_path` :
#  MIMIR_SHARED honoré, idempotent, fail fast sur symbole manquant.)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared-references" / "scripts"))
import bootstrap  # noqa: E402,F401  (socle mutualisé ; voir skills/_shared-references/scripts/bootstrap.py)

import config_loader  # noqa: E402
import frontmatter as fm  # noqa: E402
import iohelpers  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import slug as slug_mod  # noqa: E402
from guard import assert_within  # noqa: E402

# `wiki-ingest` est un sibling : on importe par chemin relatif pour réutiliser
# `cmd_finalize` (ledger + _status.md) et garder la source de vérité unique.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wiki-ingest" / "scripts"))
import wiki_ingest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "wiki-index" / "scripts"))
import link_audit  # noqa: E402
import wiki_index  # noqa: E402

import sync  # noqa: E402  (moteur de synchro partagé : pull/push)


# --- constantes contractuelles ---------------------------------------------
SLUG_MAX_TITLE = 60  # spec `batch-produce::idempotent-per-article-production`
MIN_ARTICLE_BYTES = 500  # validation post-écriture
MIN_HEADING_COUNT = 4  # au moins Définition / Points clés / Mermaid / Relations
SKIP_NAMES = {"INDEX.md", "_status.md", ".DS_Store"}  # sidecars de service
WEB_EXT = ".md"

#: Préfixes de fichiers qui signalent une source non-web — refus immédiat.
NON_WEB_EXTENSIONS = (".pdf", ".epub")


# --- exceptions -------------------------------------------------------------
class BatchError(ValueError):
    """Erreur de configuration / de contrat (refus source, sujet vide, etc.)."""


# --- résultat par source ----------------------------------------------------
@dataclass
class SourceOutcome:
    """Bilan d'une source : traitée, skippée, refusée ou en erreur."""

    source: str           # chemin relatif à work_root
    status: str           # "written" | "skipped" | "refused" | "rolled_back" | "error"
    slug: str = ""
    detail: str = ""


# --- helpers frontmatter ----------------------------------------------------
def _render_list(values: list[str], *, quote: bool) -> str:
    """Flow-list YAML `[a, b]` (sources entre guillemets, tags nus). Cohérent
    avec `article_writer._render_list` (même forme, source de vérité unique)."""
    if not values:
        return "[]"
    if quote:
        items = ", ".join('"' + v.replace('"', "'") + '"' for v in values)
    else:
        items = ", ".join(values)
    return f"[{items}]"


def _render_frontmatter(
    *,
    title: str,
    subject: str,
    tags: list[str],
    sources: list[str],
    created: str,
    updated: str,
) -> str:
    """Frontmatter d'un article notion (gabarit FRONTMATTERS.md §1) en flow-list.

    Le titre est entre guillemets pour tolérer les deux-points et apostrophes ;
    `subject` est un slug nu. Cohérent avec `article_writer.render_frontmatter`
    pour que la lecture (parseur partagé) ne distingue pas les deux producteurs.
    """
    safe_title = (title or "").replace('"', "'")
    return (
        "---\n"
        f'title: "{safe_title}"\n'
        f"subject: {subject}\n"
        f"tags: {_render_list(tags, quote=False)}\n"
        f"sources: {_render_list(sources, quote=True)}\n"
        f"created: {created}\n"
        f"updated: {updated}\n"
        "---\n\n"
    )


# --- helpers de construction du corps Karpathy ------------------------------
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_HEADING_RE = re.compile(r"^#{1,6}\s+")
_LIST_RE = re.compile(r"^\s*[-*]\s+")


def _first_sentence(text: str, *, max_len: int = 200) -> str:
    """Première phrase non vide du texte, plafonnée à `max_len` caractères.

    Si la première phrase est plus longue que `max_len`, on coupe au mot le plus
    proche et on ajoute `…`. C'est ce qui peuple le callout `>` (résumé
    d'article), repris par l'`INDEX` par sujet.
    """
    text = text.strip()
    if not text:
        return ""
    parts = _SENTENCE_END.split(text, maxsplit=1)
    sentence = parts[0].strip()
    if len(sentence) > max_len:
        cut = sentence[:max_len].rsplit(" ", 1)[0]
        return (cut or sentence[:max_len]) + "…"
    return sentence


def _first_paragraph(text: str, *, max_len: int = 500) -> str:
    """Premier paragraphe de `text` (avant le 1er titre `##` ou ligne vide double).

    Sert de base à `## Définition`. Plafonné à `max_len` caractères.
    """
    paragraphs: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if buf:
                paragraphs.append(" ".join(buf).strip())
                buf = []
            if paragraphs:
                break  # on a notre premier paragraphe
            continue
        if _HEADING_RE.match(stripped):
            if buf:
                paragraphs.append(" ".join(buf).strip())
                buf = []
            break  # un titre coupe le paragraphe
        buf.append(stripped)
    if buf and not paragraphs:
        paragraphs.append(" ".join(buf).strip())
    if not paragraphs:
        return ""
    para = paragraphs[0]
    if len(para) > max_len:
        cut = para[:max_len].rsplit(" ", 1)[0]
        return (cut or para[:max_len]) + "…"
    return para


def _extract_key_points(text: str, *, max_points: int = 5, min_points: int = 3) -> list[str]:
    """Points clés extraits déterministiquement du corps de la source.

    Priorité : (1) puces Markdown déjà présentes, (2) premières phrases
    porteuses (≥ 30 caractères, ≠ titre). Déduplique en conservant l'ordre.
    Garantit `min_points` si la source a assez de matière ; sinon renvoie ce
    qu'on a (l'article sera probablement trop court → rollback).
    """
    points: list[str] = []
    seen: set[str] = set()

    def _add(point: str) -> None:
        cleaned = point.strip().strip("-* ").strip()
        if not cleaned or len(cleaned) < 20:
            return
        if cleaned in seen:
            return
        seen.add(cleaned)
        points.append(cleaned)

    bullets: list[str] = []
    sentences: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _LIST_RE.match(line):
            bullets.append(_LIST_RE.sub("", stripped))
        elif not _HEADING_RE.match(stripped):
            for s in _SENTENCE_END.split(stripped):
                if s.strip():
                    sentences.append(s.strip())

    for b in bullets:
        if len(points) >= max_points:
            break
        _add(b)
    for s in sentences:
        if len(points) >= max_points:
            break
        _add(s)
    return points[:max_points]


def _build_body(
    *, title: str, slug: str, subject: str, body_source: str
) -> str:
    """Assemble le corps Karpathy (5 sections obligatoires) à partir de la source.

    Le contenu vient **intégralement** de `body_source` : pas d'invention. Si la
    source est trop maigre, la validation post-écriture déclenchera un rollback.
    """
    summary = _first_sentence(body_source) or title
    definition = _first_paragraph(body_source) or title
    key_points = _extract_key_points(body_source)
    points_block = (
        "\n".join(f"- {p}" for p in key_points) if key_points else "- (À compléter.)"
    )

    wikilink = f"{subject}/{slug}"
    return (
        f"# {title}\n\n"
        f"> {summary}\n\n"
        f"## Définition\n{definition}\n\n"
        f"## Points clés\n{points_block}\n\n"
        f"## Mermaid\n"
        f"```mermaid\n"
        f"flowchart LR\n"
        f'  A["[[{wikilink}]]"]\n'
        f"```\n\n"
        f"## Relations\n"
        f"- _Aucune relation automatique._ À enrichir par `wiki-ingest` ou manuellement.\n\n"
        f"## Sources\n"
        f"- Page d'origine : voir `sources:` du frontmatter.\n"
    )


# --- validation post-écriture ----------------------------------------------
@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


def _validate_article(text: str) -> ValidationResult:
    """3 critères (spec `batch-produce::karpathy-format-with-post-write-validation`) :
    (1) ≥ `MIN_HEADING_COUNT` sections `## `, (2) bloc ` ```mermaid ` ouvert et
    fermé, (3) taille ≥ `MIN_ARTICLE_BYTES` octets.
    """
    if len(text.encode("utf-8")) < MIN_ARTICLE_BYTES:
        return ValidationResult(False, f"article trop court (< {MIN_ARTICLE_BYTES} octets)")
    headings = re.findall(r"^## .+$", text, flags=re.MULTILINE)
    if len(headings) < MIN_HEADING_COUNT:
        return ValidationResult(
            False, f"seulement {len(headings)} section(s) `##` (attendu >= {MIN_HEADING_COUNT})"
        )
    open_m = text.find("```mermaid")
    if open_m == -1:
        return ValidationResult(False, "bloc ```mermaid manquant")
    close_m = text.find("```", open_m + len("```mermaid"))
    if close_m == -1:
        return ValidationResult(False, "bloc ```mermaid non fermé")
    return ValidationResult(True)


# --- slug + collision ------------------------------------------------------
def _next_disambiguated_slug(subject_dir: Path, base: str) -> str:
    """Renvoie `<base>-2`, `<base>-3`, … : premier suffixe libre.

    Appelé **après** le check d'idempotence : on sait que `<base>` est libre,
    reste à trouver un suffixe qui ne le soit pas (cas de plusieurs sources
    distinctes avec un titre slugifié identique, au sein du même run).
    """
    n = 2
    while True:
        candidate = f"{base}-{n}"
        if not any(slug_mod.same_file(p.stem, candidate) for p in subject_dir.glob("*.md")):
            return candidate
        n += 1


# --- scan d'un inbox-dir ----------------------------------------------------
def _scan_inbox(
    inbox_dir: Path, *, work_root: Path
) -> tuple[list[Path], list[SourceOutcome]]:
    """Liste triée des `.md` top-level + journaux de refus pour les non-`.md`.

    Refuse **immédiatement** les `.pdf`/`.epub` : ils sont dans la zone inbox
    par erreur, le message d'aiguillage vers `wiki-ingest` doit partir avant
    tout autre traitement.
    """
    paths: list[Path] = []
    refusals: list[SourceOutcome] = []
    for p in sorted(inbox_dir.iterdir()):
        if not p.is_file() or p.name in SKIP_NAMES or p.name.startswith("."):
            continue
        rel = p.relative_to(work_root).as_posix()
        if p.suffix.lower() in NON_WEB_EXTENSIONS:
            refusals.append(
                SourceOutcome(
                    source=rel,
                    status="refused",
                    detail=(
                        f"source non-web ({p.suffix}); utilise `wiki-ingest` (chapitrage `#chK` à respecter)"
                    ),
                )
            )
            continue
        if p.suffix.lower() != WEB_EXT:
            continue  # on ignore silencieusement les autres extensions parasites
        paths.append(p)
    return paths, refusals


def _has_chaptered_frontmatter(front: dict) -> bool:
    """Vrai si le frontmatter déclare un chapitrage (bascule vers `wiki-ingest`).

    Toute présence de la clé `chapters` — non vide (liste / int > 0) **ou** chaîne
    non triviale (forme JSON sérialisée par un extracteur) — déclenche le refus :
    on ne traite **que** du web 1-URL-1-notion.
    """
    if "chapters" not in front:
        return False
    ch = front.get("chapters")
    if isinstance(ch, list) and ch:
        return True
    if isinstance(ch, int) and ch > 0:
        return True
    if isinstance(ch, str) and ch.strip() and ch.strip() not in ("[]", "0", "null", "None"):
        return True
    return False


# --- production d'une source -----------------------------------------------
def _produce_one(
    cfg,
    src_path: Path,
    *,
    subject: str,
    today: str,
    work_root: Path,
    warnings: list[str],
    reserved: set[str],
    initial_slugs: set[str],
) -> SourceOutcome:
    """Traite une source `.md` → `wiki/<subject>/<slug>.md`. Renvoie le bilan.

    Étapes (spec) : lecture frontmatter → garde-fous (sujet/titre/source) →
    slug + idempotence → écriture atomique → validation post-écriture →
    rollback ou succès. **Aucune** étape n'est sautée en silence.

    `reserved` est l'ensemble des slugs déjà **consommés par d'autres sources
    du même run** (cumul intra-batch pour la désambiguïsation `-2`/`-3`).
    `initial_slugs` est le snapshot des slugs **déjà sur disque au début du
    run** : c'est l'ancre de l'idempotence (spec).
    """
    rel = src_path.relative_to(work_root).as_posix()
    text = src_path.read_text(encoding="utf-8")
    front, body = fm.parse_frontmatter(text)
    title = str(front.get("title", "")).strip()
    source_url = str(front.get("source", "")).strip()
    raw_tags = front.get("tags") if isinstance(front.get("tags"), list) else []
    tags = [str(t) for t in raw_tags]

    if not title or not source_url:
        return SourceOutcome(
            source=rel,
            status="refused",
            detail="frontmatter incomplet (`title` et `source` requis)",
        )
    if _has_chaptered_frontmatter(front):
        return SourceOutcome(
            source=rel,
            status="refused",
            detail="frontmatter chapitré (`chapters:`) → utiliser `wiki-ingest`",
        )
    if subject not in tags:
        warnings.append(
            f"{rel}: `tags` ne contient pas `{subject}` (mismatch sujet/source, on continue)"
        )

    subject_slug = slug_mod.slugify(subject)
    if not subject_slug:
        raise BatchError(f"Sujet vide après slugification : {subject!r}")
    subject_dir = assert_within(work_root, cfg.WIKI / subject_slug)
    if not subject_dir.is_dir():
        iohelpers.ensure_dir(subject_dir)

    # --- idempotence (slug déterministe depuis le titre) -------------------
    # L'idempotence compare à l'**état initial** du wiki (snapshot pris en
    # début de run dans `cmd_batch_produce`), pas à l'état **après** les
    # écritures intra-batch : sans ça, deux sources homonymes dans le même
    # run seraient prises pour des doublons et la 2e serait skippée au lieu
    # d'obtenir un suffixe `-2`.
    base_slug = slug_mod.slugify(title, max_len=SLUG_MAX_TITLE)
    if not base_slug:
        return SourceOutcome(
            source=rel, status="error",
            detail=f"slug vide pour le titre {title!r}",
        )
    if base_slug in initial_slugs or base_slug in reserved:
        # `initial_slugs` : la dest existait AVANT le run → idempotence (↩️)
        # `reserved` : la dest a été écrite PAR ce run → désambiguïsation `-2`
        if base_slug in initial_slugs:
            return SourceOutcome(
                source=rel,
                status="skipped",
                slug=base_slug,
                detail="destination existante (↩️)",
            )
        slug = _next_disambiguated_slug(subject_dir, base_slug)
    else:
        slug = base_slug
    target = assert_within(work_root, subject_dir / f"{slug}.md")
    reserved.add(slug)

    # --- corps Karpathy + frontmatter flow-list ----------------------------
    sources = [rel, source_url]
    body_md = _build_body(title=title, slug=slug, subject=subject_slug, body_source=body)
    document = (
        _render_frontmatter(
            title=title,
            subject=subject_slug,
            tags=_merge_tags(tags, subject_slug),
            sources=sources,
            created=today,
            updated=today,
        )
        + body_md.lstrip("\n")
    )

    # --- écriture atomique + validation + rollback -------------------------
    iohelpers.atomic_write_text(target, document, work_root=work_root)
    written = target.read_text(encoding="utf-8")
    check = _validate_article(written)
    if not check.ok:
        target.unlink(missing_ok=True)
        return SourceOutcome(
            source=rel, status="rolled_back", slug=slug, detail=check.reason
        )
    return SourceOutcome(source=rel, status="written", slug=slug)


def _merge_tags(tags: list[str], subject_slug: str) -> list[str]:
    """Garantit que `subject_slug` figure dans `tags` (sans doublon, ordre stable)."""
    if subject_slug in tags:
        return list(tags)
    return [subject_slug, *tags]


# --- audit best-effort ------------------------------------------------------
def _audit_best_effort(cfg) -> str:
    """Lance `wiki_index.audit` et **loggue** sans abort. Renvoie une ligne lisible."""
    try:
        report = link_audit.audit(cfg)
    except Exception as exc:  # noqa: BLE001 — best-effort, jamais bloquant
        return f"audit: indisponible ({exc.__class__.__name__}: {exc})"
    if report.ok:
        return "audit: ✅ 0 anomalie"
    return (
        f"audit: ⚠️ {report.total} anomalie(s) "
        f"(broken={len(report.broken)} orphans={len(report.orphans)} "
        f"dangling={len(report.dangling)} cjk={len(report.cjk)})"
    )


# --- finalize par source (déterministe) ------------------------------------
def _finalize_source(
    cfg, *, src_path: Path, article_wikilink: str, skip_sync: bool
) -> tuple[bool, str]:
    """Appelle `wiki_ingest.cmd_finalize` pour la source produite. Idempotent."""
    sha = iohelpers.sha256_file(src_path)
    rel = src_path.relative_to(cfg.work_root).as_posix()
    try:
        out = wiki_ingest.cmd_finalize(
            cfg,
            source=rel,
            sha=sha,
            articles=[article_wikilink],
            status="compilé",
            skip_sync=skip_sync,
            dry_run=False,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort, journalise
        return False, f"{exc.__class__.__name__}: {exc}"
    if out.status == "error":
        return False, out.detail
    return True, out.detail


# --- CLI --------------------------------------------------------------------
def cmd_batch_produce(
    cfg,
    *,
    inbox_dir: str,
    subject: str,
    skip_sync: bool = True,
    dry_run: bool = False,
) -> dict:
    """Boucle sérielle sur les sources de `inbox_dir` (relatif à `work_root`).

    Renvoie un rapport JSON-compatible : `subject`, `inbox_dir`, `outcomes` (par
    source), `warnings`, `audit` (best-effort). Le verdict ligne à ligne est
    imprimé sur stderr (`✅ <slug>`, `↩️ <slug>`, `❌ <slug> — <reason>`).
    """
    if not skip_sync and sync.pull(cfg) != 0:
        raise RuntimeError("pré-sync (pull) en échec — batch interrompu")

    work_root = cfg.work_root
    inbox_path = Path(inbox_dir)
    if inbox_path.is_absolute():
        inbox_abs = inbox_path
    else:
        inbox_abs = (work_root / inbox_dir).resolve()
    inbox_abs = assert_within(work_root, inbox_abs)
    if not inbox_abs.is_dir():
        raise BatchError(f"Inbox introuvable : {inbox_abs}")
    if not subject or not subject.strip():
        raise BatchError("`--subject` requis et non vide")

    paths, refusals = _scan_inbox(inbox_abs, work_root=work_root)
    outcomes: list[SourceOutcome] = list(refusals)
    warnings: list[str] = []
    # Snapshot **initial** des slugs du wiki : ancre d'idempotence. Toute
    # collision intra-batch utilise `reserved` (ci-dessous) et reçoit un
    # suffixe `-2`/`-3`, sans re-déclencher le `skipped`.
    subject_slug = slug_mod.slugify(subject)
    subject_dir = (cfg.WIKI / subject_slug) if subject_slug else None
    initial_slugs: set[str] = (
        {p.stem for p in subject_dir.glob("*.md")} if subject_dir and subject_dir.is_dir() else set()
    )
    # Slugs consommés par d'autres sources du même run (intra-batch).
    reserved: set[str] = set()

    today = date.today().isoformat()
    for src in paths:
        if dry_run:
            outcomes.append(
                SourceOutcome(
                    source=src.relative_to(work_root).as_posix(),
                    status="skipped",
                    detail="(dry-run) pas d'écriture",
                )
            )
            continue
        out = _produce_one(
            cfg, src, subject=subject, today=today, work_root=work_root,
            warnings=warnings, reserved=reserved, initial_slugs=initial_slugs,
        )
        outcomes.append(out)
        if out.status == "written":
            # finalize par source — uniquement si on a réellement écrit.
            # `cmd_finalize` hérite de `skip_sync` : pas de push redondant.
            ok, detail = _finalize_source(
                cfg, src_path=src,
                article_wikilink=f"{slug_mod.slugify(subject)}/{out.slug}",
                skip_sync=skip_sync,
            )
            if not ok:
                warnings.append(f"finalize {out.slug}: {detail}")

    audit_line = "audit: (dry-run)" if dry_run else _audit_best_effort(cfg)

    if not skip_sync and not dry_run:
        sync.push(cfg)

    return {
        "subject": subject,
        "inbox_dir": inbox_dir,
        "outcomes": [
            {"source": o.source, "status": o.status, "slug": o.slug, "detail": o.detail}
            for o in outcomes
        ],
        "warnings": warnings,
        "audit": audit_line,
    }


def _print_verdict(outcomes: list[SourceOutcome]) -> None:
    """Imprime le verdict ligne à ligne (cohérent avec les autres skills)."""
    for o in outcomes:
        if o.status == "written":
            print(f"✅ {o.slug}", file=sys.stderr)
        elif o.status == "skipped":
            label = o.slug or o.source
            print(f"↩️ {label} — {o.detail}", file=sys.stderr)
        elif o.status == "refused":
            print(f"🚫 {o.source} — {o.detail}", file=sys.stderr)
        elif o.status in ("rolled_back", "error"):
            print(f"❌ {o.slug or o.source} — {o.detail}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Production batch d'articles Karpathy depuis raw/web/<sujet>/ (Mimir).",
    )
    parser.add_argument(
        "--config", default=None,
        help="Chemin du wiki.config.json (optionnel ; auto-découverte si omis : "
             "$MIMIR_CONFIG, dossier du profil, ~/.config/mimir/wiki.config.json, ./wiki.config.json)",
    )
    parser.add_argument(
        "--inbox-dir", required=True,
        help="Dossier source RELATIF à work_root (ex. raw/web/expertise-judiciaire).",
    )
    parser.add_argument(
        "--subject", required=True,
        help="Sujet destination (dossier wiki/<subject>/ ; gagne sur les tags de la source).",
    )
    parser.add_argument(
        "--skip-sync", action="store_true",
        help="Ne pas appeler wiki-sync en pré/post (par défaut : skip pour les batches).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scanner sans rien écrire (rapport JSON uniquement).",
    )
    args = parser.parse_args(argv)

    cfg = config_loader.load_resolved_config(args.config)
    try:
        report = cmd_batch_produce(
            cfg,
            inbox_dir=args.inbox_dir,
            subject=args.subject,
            skip_sync=args.skip_sync,
            dry_run=args.dry_run,
        )
    except BatchError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    _print_verdict([SourceOutcome(**o) for o in report["outcomes"]])
    n_written = sum(1 for o in report["outcomes"] if o["status"] == "written")
    n_skipped = sum(1 for o in report["outcomes"] if o["status"] == "skipped")
    n_refused = sum(1 for o in report["outcomes"] if o["status"] == "refused")
    n_rollback = sum(1 for o in report["outcomes"] if o["status"] in ("rolled_back", "error"))
    print(
        f"✅ {n_written} écrit(s), ↩️ {n_skipped} skippé(s), "
        f"🚫 {n_refused} refusé(s), ❌ {n_rollback} rollback(s). "
        f"{report['audit']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
