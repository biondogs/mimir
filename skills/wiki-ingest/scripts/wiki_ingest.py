#!/usr/bin/env python3
"""wiki-ingest — helpers déterministes de compilation `raw/` → `wiki/` (SPEC §5, §7).

La décomposition en notions et la reformulation Karpathy sont **sémantiques** : c'est
l'agent (guidé par `SKILL.md`) qui les fait. Ce CLI ne fournit que les opérations qui
doivent être **exactes et idempotentes**, que l'agent enchaîne librement :

    wiki_ingest.py --config <wiki.config.json> inventory   [SOURCE] [--skip-sync] [--dry-run]
    wiki_ingest.py --config <wiki.config.json> status      [--sync] [--dry-run]
    wiki_ingest.py --config <wiki.config.json> write-article --subject S --notion N --body-file F.md
                       [--title T] [--tags a,b] [--source raw/...#ch3 ...] [--asset /chemin ...] [--dry-run]
    wiki_ingest.py --config <wiki.config.json> finalize --source raw/<type>/<nom>.<ext> --sha <sha>
                       --articles s1/n1,s2/n2 [--status compilé|partiellement-compilé] [--skip-sync] [--dry-run]

- `inventory` : worklist JSON des fichiers de contenu à (re)compiler (diff ledger) + notions existantes.
- `status` : digest machine-lisible du statut de compilation **par source**
  (`extrait` | `partiellement compilé` | `compilé`) + `article_count`.
  Lit uniquement (`--skip-sync` par défaut : pas d'appel réseau).
- `write-article` : écrit/écrase un article notion (antidoublon = replace-body, validation NFD/NFC).
- `finalize` : MAJ `_status.md` (statut + « Articles wiki ») + ledger atomique (idempotence).

Garanties : confinement à `work_root`, ledger atomique hors sync, pas de doublon (antidoublon
NFD/NFC), pas de suffixage côté wiki. Synchro stubbée en P2 (`--skip-sync` la court-circuite).
"""

from __future__ import annotations

import argparse
import json
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
import ledger as ledger_mod  # noqa: E402
import status_table  # noqa: E402

import inventory  # noqa: E402
import sync  # noqa: E402  (moteur de synchro partagé : pull/push)
from article_writer import ArticleError, ArticleSpec, write_article  # noqa: E402


@dataclass
class Outcome:
    action: str
    status: str  # ok | écrit | enrichi | finalisé | error
    detail: str = ""


def _today() -> date:
    return date.today()


# --- inventory -------------------------------------------------------------
def cmd_inventory(cfg, source: str | None, *, skip_sync: bool, dry_run: bool) -> dict:
    """Worklist des sources à compiler (diff ledger) + notions existantes par sujet."""
    if not skip_sync and sync.pull(cfg) != 0:
        raise RuntimeError("pré-sync (pull) en échec — compilation interrompue")
    led = ledger_mod.load_ledger(cfg.LEDGER)
    return inventory.select(cfg, led, source)


# --- status ----------------------------------------------------------------
def cmd_status(cfg, *, skip_sync: bool, dry_run: bool) -> dict:
    """Digest machine-lisible du statut de compilation **par source** (spec
    `compile-source` — Per-source compilation status digest).

    Lit `raw/`, le ledger et `_status.md` ; ne mute rien. `--skip-sync` est la
    valeur par défaut : un digest est une lecture, pas une synchro.
    """
    if not skip_sync and sync.pull(cfg) != 0:
        raise RuntimeError("pré-sync (pull) en échec — digest statut indisponible")
    _ = dry_run  # lecture seule : le digest n'a pas d'effet de bord
    return inventory.status_digest(cfg)


# --- write-article ---------------------------------------------------------
def cmd_write_article(
    cfg, spec: ArticleSpec, *, assets: list[str] | None = None, dry_run: bool = False
):
    """Écrit/écrase un article notion. Renvoie l'`ArticleResult`."""
    return write_article(cfg, spec, today=_today(), assets=assets, dry_run=dry_run)


# --- finalize --------------------------------------------------------------
def _doc_type_of(content_rel: str, cfg) -> tuple[str, str]:
    """(`doc_type`, basename) depuis un chemin de contenu relatif/absolu sous raw/."""
    p = Path(content_rel)
    rel = p if not p.is_absolute() else p.resolve().relative_to(cfg.work_root)
    parts = rel.as_posix().split("/")
    # attendu : raw/<doc_type>/<fichier>
    if len(parts) < 3 or parts[0] != cfg.layout["raw"]:
        raise ValueError(f"Chemin de contenu inattendu : {content_rel!r} (attendu raw/<type>/<fichier>)")
    return parts[1], parts[-1]


def cmd_finalize(
    cfg,
    source: str,
    sha: str,
    articles: list[str],
    *,
    status: str = "compilé",
    skip_sync: bool = False,
    dry_run: bool = False,
) -> Outcome:
    """MAJ `_status.md` (statut + articles) + record ledger atomique. Idempotence."""
    doc_type, fichier = _doc_type_of(source, cfg)
    content_rel = source if not Path(source).is_absolute() else (
        Path(source).resolve().relative_to(cfg.work_root).as_posix()
    )
    # Canonicalisation : le ledger (et la colonne « Articles wiki ») stocke des
    # wikilinks `sujet/notion` SANS préfixe `wiki/`. `article_index.load_article`
    # concatène `cfg.WIKI / wikilink` ; un `wiki/` résiduel donnerait `wiki/wiki/…`
    # (article introuvable → grille à 0 article lié). Re-`finalize` reste idempotent.
    articles_clean = [
        a.strip().removeprefix("wiki/").lstrip("/")
        for a in articles
        if a.strip()
    ]
    articles_cell = ", ".join(f"[[{a}]]" for a in articles_clean) or "—"
    today = _today().isoformat()

    if dry_run:
        return Outcome("finalize", "finalisé", f"(dry-run) {fichier} → {status} ; {len(articles_clean)} article(s)")

    # --- _status.md : préserver la colonne Grille de la ligne existante -----
    status_path = cfg.RAW / doc_type / "_status.md"
    rows = status_table.load_status(status_path)
    existing = next((r for r in rows if r.fichier == fichier), None)
    grille = existing.grille if existing else "—"
    status_table.upsert_row(
        rows,
        status_table.StatusRow(
            fichier=fichier,
            sha256=sha,
            statut=status,
            articles=articles_cell,
            grille=grille,
            maj=today,
        ),
    )
    status_table.save_status(status_path, doc_type, rows, work_root=cfg.work_root)

    # --- ledger atomique (hors sync) ---------------------------------------
    led = ledger_mod.load_ledger(cfg.LEDGER)
    ledger_mod.record(led, content_rel, sha, articles_clean, updated=today)
    ledger_mod.save_ledger(cfg.LEDGER, led, work_root=cfg.work_root)

    if not skip_sync and sync.push(cfg) != 0:
        return Outcome("finalize", "error", "post-sync (push) en échec — travail local conservé")
    return Outcome("finalize", "finalisé", f"{fichier} → {status} ; {len(articles_clean)} article(s)")


# --- CLI -------------------------------------------------------------------
def _split_csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compilation raw/ → wiki/ par notion (Mimir).")
    parser.add_argument(
        "--config", default=None,
        help="Chemin du wiki.config.json (optionnel ; auto-découverte si omis : "
             "$MIMIR_CONFIG, dossier du profil, ~/.config/mimir/wiki.config.json, ./wiki.config.json)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("inventory", help="Worklist des sources à compiler (diff ledger).")
    p_inv.add_argument("source", nargs="?", help="Restreindre à un fichier de contenu (raw/<type>/<nom>).")
    p_inv.add_argument("--skip-sync", action="store_true")
    p_inv.add_argument("--dry-run", action="store_true")

    p_st = sub.add_parser(
        "status",
        help="Digest machine-lisible du statut de compilation par source "
             "(`extrait` | `partiellement compilé` | `compilé`) + `article_count`.",
    )
    p_st.add_argument(
        "--sync", dest="do_sync", action="store_true",
        help="Faire un pull avant le digest (par défaut : aucune I/O réseau).",
    )
    p_st.add_argument("--dry-run", action="store_true")

    p_wr = sub.add_parser("write-article", help="Écrit/écrase un article notion (format Karpathy).")
    p_wr.add_argument("--subject", required=True)
    p_wr.add_argument("--notion", required=True)
    p_wr.add_argument("--body-file", required=True, help="Fichier du corps Karpathy (sans frontmatter).")
    p_wr.add_argument("--title")
    p_wr.add_argument("--tags", help="Tags séparés par des virgules.")
    p_wr.add_argument("--source", action="append", default=[], help="Source raw (répétable).")
    p_wr.add_argument("--asset", action="append", default=[], help="Image à localiser (répétable).")
    p_wr.add_argument("--dry-run", action="store_true")

    p_fin = sub.add_parser("finalize", help="MAJ _status.md + ledger (clôt une source).")
    p_fin.add_argument("--source", required=True, help="Fichier de contenu raw/<type>/<nom>.<ext>")
    p_fin.add_argument("--sha", required=True, help="SHA256 du fichier de contenu.")
    p_fin.add_argument("--articles", default="", help="Wikilinks produits : s1/n1,s2/n2")
    p_fin.add_argument("--status", default="compilé", choices=list(status_table.STATUSES))
    p_fin.add_argument("--skip-sync", action="store_true")
    p_fin.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    cfg = config_loader.load_resolved_config(args.config)

    if args.cmd == "inventory":
        data = cmd_inventory(cfg, args.source, skip_sync=args.skip_sync, dry_run=args.dry_run)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        n = len(data["worklist"])
        print(f"{n} source(s) à compiler.", file=sys.stderr)
        return 0

    if args.cmd == "status":
        data = cmd_status(cfg, skip_sync=not args.do_sync, dry_run=args.dry_run)
        print(json.dumps(data, ensure_ascii=False, indent=2))
        n = len(data["sources"])
        print(f"{n} source(s) inspectée(s).", file=sys.stderr)
        return 0

    if args.cmd == "write-article":
        spec = ArticleSpec(
            subject=args.subject,
            notion=args.notion,
            body=Path(args.body_file).read_text(encoding="utf-8"),
            sources=list(args.source),
            tags=_split_csv(args.tags),
            title=args.title,
        )
        try:
            res = cmd_write_article(cfg, spec, assets=list(args.asset), dry_run=args.dry_run)
        except ArticleError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        print(json.dumps(
            {"wikilink": res.wikilink, "path": str(res.path), "created": res.created},
            ensure_ascii=False,
        ))
        return 0

    if args.cmd == "finalize":
        out = cmd_finalize(
            cfg,
            args.source,
            args.sha,
            _split_csv(args.articles),
            status=args.status,
            skip_sync=args.skip_sync,
            dry_run=args.dry_run,
        )
        print(f"{'✅' if out.status != 'error' else '❌'} {out.detail}")
        return 1 if out.status == "error" else 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
