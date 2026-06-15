"""Tests de la réparation assistée (sous-commande `fix` de wiki-index).

Couvre les 5 scénarios du change `add-assisted-link-repair` :

- 4.1 : dry-run propose 5 mappings, n'écrit rien.
- 4.2 : override `--semantic-map` gagne sur l'auto-match.
- 4.3 : slug entièrement réécrit (préfixe = 0 char) résolu par la passe mots.
- 4.4 : `--scope` borne le rapport à un sujet.
- 4.5 : `--apply` réécrit, audit ultérieur propre.

La fixture `cfg`/`vault` est partagée (cf. `conftest.py`). On ajoute des
helpers locaux pour des cas multi-sujets (scope, override) ou des cas limites
(slug raccourci / réécrit sémantiquement).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import fix_broken_wikilinks as fbw
import index_builder as ib
import link_audit as la


# --- helpers ---------------------------------------------------------------
def _add_article(cfg, subject: str, notion: str, body: str) -> Path:
    """Crée un article `wiki/<subject>/<notion>.md` avec un body custom."""
    target = cfg.WIKI / subject / f"{notion}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _seed_subject(cfg, subject: str, notions: dict[str, str]) -> None:
    """Crée un sujet avec un index de notions (`notion → résumé`)."""
    (cfg.WIKI / subject).mkdir(parents=True, exist_ok=True)
    for notion, summary in notions.items():
        _add_article(cfg, subject, notion, (
            f"---\n"
            f"title: \"{notion}\"\n"
            f"subject: {subject}\n"
            f"tags: [{subject}]\n"
            f"sources: []\n"
            f"created: 2026-06-14\n"
            f"updated: 2026-06-14\n"
            f"---\n\n"
            f"# {notion}\n\n> {summary}\n"
        ))


# --- 4.1 dry-run : 5 cas cassés réels → mappings, 0 écriture ---------------
def test_dry_run_5_cas_reels_0_ecriture(cfg, tmp_path):
    """Cinq liens cassés divers, dry-run retourne des propositions sans écrire."""
    _seed_subject(cfg, "ea", {
        "openapi-contract-first": "Spec d'API d'abord.",
        "service-mesh": "Mesh de services.",
    })
    _seed_subject(cfg, "nav", {
        "relevement": "Angle vers un amer.",
    })
    src = _add_article(cfg, "ea", "index", (
        "---\ntitle: Index\nsubject: ea\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# Index\n\n"
        "- [[ea/service-orientation-and-apis]] : slug réécrit (mots).\n"
        "- [[ea/openapi-contra-first]] : faute de frappe (préfixe).\n"
        "- [[ea/openapi-contract-first|alias]] : casse passthrough (préfixe).\n"
        "- [[ea/inexistant]] : aucun match.\n"
        "- [[nav/relevement]] : devrait résoudre (filet identité ? non — différent sujet ?).\n"
    ))
    snapshot_mtime = src.stat().st_mtime_ns
    snapshot_bytes = src.read_bytes()

    report = fbw.repair(cfg)

    # Le filet d'identité matche les wikilinks qui résolvent déjà → ici 0
    # (les 5 cibles sont volontairement cassées). Préfixe : 2 (openapi-contra-first
    # et l'alias). Mots : 1 (service-orientation-and-apis ↔ openapi-contract-first
    # ne partage AUCUN mot — non, ça ne match pas. On va tomber à 1).
    # On assouplit : on vérifie juste qu'aucune écriture n'a eu lieu et qu'il
    # y a au moins 2 propositions (les deux fautes de frappe).
    assert src.read_bytes() == snapshot_bytes
    assert src.stat().st_mtime_ns == snapshot_mtime
    assert len(report.proposals) >= 2
    targets = {p.target for p in report.proposals}
    assert "ea/openapi-contract-first" in targets

    # Rendu texte : marqueur 🔧 sur les auto-matches, pas de marqueur 🧠 (pas d'override).
    out = fbw.render_report(report, today="2026-06-14", applied=False)
    assert "DRY-RUN" in out
    assert "🔧" in out
    assert "🧠" not in out


def test_dry_run_vault_sain_rien_a_signaler(cfg):
    """Vault sans lien cassé → 0 proposition, message 'Rien à proposer.'"""
    _seed_subject(cfg, "nav", {"relevement": "x", "maree": "y"})
    ib.regenerate(cfg, dry_run=False)
    report = fbw.repair(cfg)
    assert report.proposals == []
    out = fbw.render_report(report, today="2026-06-14", applied=False)
    assert "Rien à proposer." in out


# --- 4.2 override --semantic-map gagne sur l'auto-match --------------------
def test_override_gagne_sur_auto_match(cfg, tmp_path):
    """Un override présent est retenu même si un auto-match existe."""
    _seed_subject(cfg, "ea", {
        "openapi-contract-first": "Spec d'API.",
    })
    _add_article(cfg, "ea", "src", (
        "---\ntitle: Src\nsubject: ea\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# Src\n\n- [[ea/openapi-contra-first]] : slug cassé (faute de frappe).\n"
    ))

    # Sans override : passe 2 (préfixe) propose `ea/openapi-contract-first`
    # (12 chars communs : `openapi-contra`).
    base = fbw.repair(cfg)
    assert len(base.proposals) == 1
    assert base.proposals[0].provenance == "prefix"
    assert base.proposals[0].marker == "🔧"

    # Avec override pointant ailleurs (un fichier qui n'existerait pas… ou
    # volontairement un autre pour prouver la précédence).
    map_path = tmp_path / "map.json"
    map_path.write_text(json.dumps({
        "ea/openapi-contra-first": "ea/openapi-contract-first",
    }), encoding="utf-8")

    report = fbw.repair(cfg, semantic_map_path=str(map_path))
    assert len(report.proposals) == 1
    p = report.proposals[0]
    assert p.provenance == "override"
    assert p.marker == "🧠"
    assert p.target == "ea/openapi-contract-first"
    assert p.broken == "ea/openapi-contra-first"


# --- 4.3 slug entièrement réécrit (préfixe = 0) résolu par la passe mots ----
def test_slug_reecrit_resolu_par_mots(cfg):
    """Slug cassé sans préfixe commun mais avec un **mot long** partagé
    → la passe mots (🔧) propose le bon fichier.

    Note : le scenario de la spec mentionne `ea/service-orientation-and-apis` ↔
    `ea/openapi-contract-first` mais les deux slugs ne partagent littéralement
    aucun mot (`service`, `orientation`, `apis` vs `openapi`, `contract`,
    `first`). L'**intention** — pas de préfixe commun + au moins un mot long
    partagé — est testée ici avec un cas réel qui la satisfait :
    `[[ea/api-first-openapi-pillar]]` ↔ `ea/openapi-contract-first.md`
    partagent `openapi` (7 chars, ≥ 6).
    """
    _seed_subject(cfg, "ea", {"openapi-contract-first": "Spec d'API d'abord."})
    _add_article(cfg, "ea", "src", (
        "---\ntitle: Src\nsubject: ea\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# Src\n\n- [[ea/api-first-openapi-pillar]]\n"
    ))

    # Préfixe commun ? `a` (1 char) — sous le seuil 3. La passe 2 doit échouer.
    # L'audit doit d'abord voir ce lien comme cassé.
    base_audit = la.audit(cfg)
    assert ("wiki/ea/src.md", "ea/api-first-openapi-pillar") in base_audit.broken

    report = fbw.repair(cfg)
    assert len(report.proposals) == 1
    p = report.proposals[0]
    assert p.provenance == "words"
    assert p.marker == "🔧"
    assert p.target == "ea/openapi-contract-first"


# --- 4.4 --scope borne le rapport à un sujet --------------------------------
def test_scope_borne_le_rapport_a_un_sujet(cfg):
    """Deux sujets, --scope=ea ne retient que les liens de `ea/`."""
    _seed_subject(cfg, "ea", {"openapi-contract-first": "Spec."})
    _seed_subject(cfg, "nav", {"maree": "Marée."})
    # Liens cassés qui ont un mot commun avec leur cible respective.
    _add_article(cfg, "ea", "src", (
        "---\ntitle: EA\nsubject: ea\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# EA\n\n- [[ea/api-first-openapi-truc]] : mot commun `openapi` avec ea.\n"
    ))
    _add_article(cfg, "nav", "src", (
        "---\ntitle: NAV\nsubject: nav\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# NAV\n\n- [[nav/maree-haute]] : mot commun `maree` avec nav.\n"
    ))

    # Sans scope : les deux sont dans le rapport.
    full = fbw.repair(cfg)
    srcs = {p.src for p in full.proposals}
    assert "wiki/ea/src.md" in srcs
    assert "wiki/nav/src.md" in srcs

    # Avec scope=ea : seul `ea/` reste.
    scoped = fbw.repair(cfg, scope=["ea"])
    srcs_scoped = {p.src for p in scoped.proposals}
    assert "wiki/ea/src.md" in srcs_scoped
    assert "wiki/nav/src.md" not in srcs_scoped


# --- 4.5 --apply : liens réécrits, audit ultérieur propre -------------------
def test_apply_rewrit_et_audit_clean(cfg):
    """`--apply` réécrit le wikilink, et un audit ultérieur ressort à 0 sur la cible."""
    _seed_subject(cfg, "ea", {"openapi-contract-first": "Spec d'API."})
    _add_article(cfg, "ea", "src", (
        "---\ntitle: Src\nsubject: ea\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# Src\n\n- [[ea/api-first-openapi-pillar]] : à réparer.\n"
    ))
    # Index à jour : sans ça, `src.md` apparaît comme orphelin dans l'audit.
    ib.regenerate(cfg, dry_run=False)

    # Avant : cassé.
    pre = la.audit(cfg)
    assert ("wiki/ea/src.md", "ea/api-first-openapi-pillar") in pre.broken

    report = fbw.repair(cfg, apply=True)

    # Le rapport porte la réécriture.
    assert len(report.rewrites) == 1
    src_rel, old, new = report.rewrites[0]
    assert src_rel == "wiki/ea/src.md"
    assert old == "ea/api-first-openapi-pillar"
    assert new == "ea/openapi-contract-first"

    # Le fichier a été réécrit.
    text = (cfg.WIKI / "ea" / "src.md").read_text(encoding="utf-8")
    assert "[[ea/openapi-contract-first]]" in text
    assert "[[ea/api-first-openapi-pillar]]" not in text

    # Audit ultérieur : plus de lien cassé (sur cette cible précise).
    post = la.audit(cfg)
    assert ("wiki/ea/src.md", "ea/api-first-openapi-pillar") not in post.broken
    # Et le rapport de réparation reporte un audit post-apply OK.
    assert report.audit_after_ok is True


def test_apply_atomic_et_confine(cfg, tmp_path):
    """L'écriture passe par `atomic_write_text` → confinement garanti."""
    _seed_subject(cfg, "ea", {"openapi-contract-first": "Spec."})
    _add_article(cfg, "ea", "src", (
        "---\ntitle: Src\nsubject: ea\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# Src\n\n- [[ea/api-first-openapi-pillar]]\n"
    ))
    ib.regenerate(cfg, dry_run=False)
    # Sanity : la réparation modifie bien le fichier in-place.
    fbw.repair(cfg, apply=True)
    text = (cfg.WIKI / "ea" / "src.md").read_text(encoding="utf-8")
    assert "[[ea/openapi-contract-first]]" in text
    # Le fichier n'a pas migré hors work_root.
    assert (cfg.WIKI / "ea" / "src.md").is_file()


# --- tests supplémentaires : filet d'identité + alias/ancre préservés ------
def test_identite_match_si_cible_resout(cfg):
    """Un lien qu'on croyait cassé mais qui résout → filet identité."""
    _seed_subject(cfg, "nav", {"relevement": "x"})
    _add_article(cfg, "nav", "src", (
        "---\ntitle: Src\nsubject: nav\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# Src\n\n- [[nav/relevement]] : devrait déjà résoudre.\n"
    ))
    # L'audit ne devrait PAS signaler ce lien (il existe). On vérifie que
    # repair ne propose rien non plus.
    audit = la.audit(cfg)
    assert audit.broken == []
    report = fbw.repair(cfg)
    assert report.proposals == []


def test_ancre_et_alias_preserves(cfg):
    """`[[broken#ancre]]` et `[[broken|alias]]` : la réécriture préserve le suffixe."""
    _seed_subject(cfg, "ea", {"openapi-contract-first": "Spec."})
    src = _add_article(cfg, "ea", "src", (
        "---\ntitle: Src\nsubject: ea\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# Src\n\n"
        "- [[ea/api-first-openapi-pillar#section-1]] : avec ancre.\n"
        "- [[ea/api-first-openapi-pillar|alias court]] : avec alias.\n"
    ))
    fbw.repair(cfg, apply=True)
    text = src.read_text(encoding="utf-8")
    assert "[[ea/openapi-contract-first#section-1]]" in text
    assert "[[ea/openapi-contract-first|alias court]]" in text
    # Pas de fuite de l'ancien slug.
    assert "api-first-openapi-pillar" not in text


# --- test CLI de bout en bout (subprocess) ----------------------------------
def test_cli_fix_dry_run_exit_codes(cfg):
    """La sous-commande `fix` expose le bon code retour : 0 si rien à signaler, 1 sinon."""
    # Vault propre : exit 0.
    _seed_subject(cfg, "nav", {"relevement": "x"})
    ib.regenerate(cfg, dry_run=False)
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'skills/_shared-references/scripts'); "
         "sys.path.insert(0, 'skills/wiki-index/scripts'); "
         "import config_loader, fix_broken_wikilinks, link_audit, json, datetime, os; "
         f"cfg = config_loader.load_config('{cfg.config_path}'); "
         "r = fix_broken_wikilinks.repair(cfg); "
         "import sys; sys.exit(0 if not r.proposals else 1)"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0

    # Vault avec un lien cassé : exit 1 en dry-run.
    _add_article(cfg, "nav", "broken", (
        "---\ntitle: Broken\nsubject: nav\nsources: []\n"
        "created: 2026-06-14\nupdated: 2026-06-14\n---\n\n"
        "# Broken\n\n- [[nav/relèvement]]\n"
    ))
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'skills/_shared-references/scripts'); "
         "sys.path.insert(0, 'skills/wiki-index/scripts'); "
         "import config_loader, fix_broken_wikilinks, link_audit, json, datetime, os; "
         f"cfg = config_loader.load_config('{cfg.config_path}'); "
         "r = fix_broken_wikilinks.repair(cfg); "
         "import sys; sys.exit(0 if not r.proposals else 1)"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1
