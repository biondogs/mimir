#!/usr/bin/env python3
"""wiki-index — régénère les index du wiki et audite les liens (SPEC §9).

Skill **déterministe** : aucune prose à inventer. Trois sous-commandes :

    wiki_index.py --config CFG regenerate [--skip-sync] [--dry-run]
    wiki_index.py --config CFG audit [--json]
    wiki_index.py --config CFG fix [--apply] [--scope S]… [--semantic-map map.json] [--json]

- `regenerate` : reconstruit `wiki/INDEX.md` (sujets) + tous les `wiki/<sujet>/_INDEX.md`
  (notions + grilles). Écriture atomique confinée. Préserve la description éditoriale
  des sujets dans l'INDEX principal.
- `audit` : **lecture seule**, 4 passes (liens cassés / fichiers fantômes / index → vide
  / CJK parasites). Code retour **0 ssi 0 anomalie** — exploitable en CI / pour le DoD.
- `fix` : **réparation assistée** des wikilinks cassés, opt-in (n'écrit jamais sans
  `--apply`). Détection réutilise `audit` ; passes de matching ordonnées (override →
  identité → préfixe → mots) ; réécriture atomique confinée.
"""

from __future__ import annotations

import argparse
import json
import sys
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

import fix_broken_wikilinks  # noqa: E402
import index_builder  # noqa: E402
import link_audit  # noqa: E402
import sync  # noqa: E402  (moteur de synchro partagé : pull/push)


def _today() -> str:
    return date.today().isoformat()


def cmd_regenerate(cfg, *, skip_sync: bool = False, dry_run: bool = False):
    """Reconstruit les deux niveaux d'index. Renvoie l'`IndexResult`."""
    if not skip_sync and sync.pull(cfg) != 0:
        raise RuntimeError("pré-sync (pull) en échec — régénération interrompue")
    res = index_builder.regenerate(cfg, dry_run=dry_run)
    if not dry_run and not skip_sync:
        sync.push(cfg)
    return res


def cmd_audit(cfg):
    """Audit lecture seule (3 passes). Aucune synchro, aucune écriture."""
    return link_audit.audit(cfg)


def cmd_fix(cfg, *, scope, semantic_map, apply, json_out):
    """Réparation assistée des wikilinks cassés (opt-in, écrit sur demande)."""
    return fix_broken_wikilinks.repair(
        cfg, scope=scope, semantic_map_path=semantic_map, apply=apply,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Index + audit liens du wiki (Mimir).")
    parser.add_argument(
        "--config", default=None,
        help="Chemin du wiki.config.json (optionnel ; auto-découverte si omis : "
             "$MIMIR_CONFIG, dossier du profil, ~/.config/mimir/wiki.config.json, ./wiki.config.json)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reg = sub.add_parser("regenerate", help="Reconstruit INDEX.md + <sujet>/_INDEX.md.")
    p_reg.add_argument("--skip-sync", action="store_true")
    p_reg.add_argument("--dry-run", action="store_true")

    p_aud = sub.add_parser("audit", help="Audit liens (3 passes, lecture seule).")
    p_aud.add_argument("--json", action="store_true", help="Rapport machine (JSON).")

    p_fix = sub.add_parser(
        "fix", help="Réparation assistée des wikilinks cassés (dry-run par défaut).",
    )
    p_fix.add_argument(
        "--scope", action="append", default=None,
        help="Sujet à borner (répétable) ; défaut = tout le vault.",
    )
    p_fix.add_argument(
        "--semantic-map", default=None,
        help="Chemin d'un JSON {cible_cassée: cible_proposée} (passe 0, marqueur 🧠).",
    )
    p_fix.add_argument(
        "--apply", action="store_true",
        help="Réécrit les wikilinks (défaut : dry-run, n'écrit rien).",
    )
    p_fix.add_argument(
        "--json", action="store_true", help="Rapport machine (JSON).",
    )

    args = parser.parse_args(argv)
    cfg = config_loader.load_resolved_config(args.config)

    if args.cmd == "regenerate":
        res = cmd_regenerate(cfg, skip_sync=args.skip_sync, dry_run=args.dry_run)
        print(json.dumps({
            "subjects": res.subjects, "written": res.written, "form": res.form,
        }, ensure_ascii=False, indent=2))
        tag = "(dry-run) " if args.dry_run else ""
        print(f"✅ {tag}{len(res.subjects)} sujet(s), {len(res.written)} index écrit(s).", file=sys.stderr)
        return 0

    if args.cmd == "audit":
        report = cmd_audit(cfg)
        if args.json:
            print(json.dumps({
                "ok": report.ok,
                "broken": report.broken,
                "orphans": report.orphans,
                "dangling": report.dangling,
                "cjk": report.cjk,
            }, ensure_ascii=False, indent=2))
        else:
            print(link_audit.render_report(report, today=_today()))
        return 0 if report.ok else 1

    if args.cmd == "fix":
        report = cmd_fix(
            cfg, scope=args.scope, semantic_map=args.semantic_map,
            apply=args.apply, json_out=args.json,
        )
        if args.json:
            print(json.dumps({
                "proposals": [
                    {
                        "src": p.src, "broken": p.broken, "target": p.target,
                        "target_path": p.target_path, "provenance": p.provenance,
                        "marker": p.marker, "score": (
                            None if p.score == float("inf") else p.score
                        ),
                    }
                    for p in report.proposals
                ],
                "rewrites": [
                    {"src": s, "old": o, "new": n}
                    for s, o, n in report.rewrites
                ],
                "audit_after_ok": report.audit_after_ok,
            }, ensure_ascii=False, indent=2))
        else:
            print(fix_broken_wikilinks.render_report(
                report, today=_today(), applied=args.apply,
            ))
        # Code retour : 0 si rien à signaler (rien cassé OU tout résolu), 1 sinon.
        return 0 if not report.proposals else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
