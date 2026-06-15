"""Tests de `wiki-research` (spec `wiki-research`) : orchestration d'un dossier de recherche.

Couvre : clip loop + dédup URL, scaffold (frontmatter `research-report`),
audit des renvois, idempotence de `update` (préserve `created:`), intégration
`wiki-index` (reconnaissance `type: research-report`).

Tous les tests sont **hermétiques** : on monkeypatche `wiki_extract.run` pour
faking le clip (zéro réseau). Les `runner` du socle sont stubbés
(`sync.pull/push`, `wiki_extract.run`) pour isoler le test.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

import config_loader
import frontmatter
import index_builder
import link_audit
import wiki_extract
import wiki_research as wr


# --- helpers de fixtures ---------------------------------------------------
@pytest.fixture
def cfg(tmp_path):
    """Vault minimal : `work_root` avec `raw/web/`, `reading-grids/`, `wiki/`.

    On **ne** crée pas `_inbox/` : `wiki-research` ne dépose pas dans l'inbox,
    il clippe directement des URLs via `wiki-extract` (qui les range sous
    `raw/web/`). On crée `wiki/<sujet>/` quand un test veut poser un article
    à lier en renvoi.
    """
    work = tmp_path / "work"
    (work / "raw" / "web").mkdir(parents=True)
    (work / "reading-grids").mkdir(parents=True)
    cfgp = tmp_path / "wiki.config.json"
    cfgp.write_text(json.dumps({"work_root": str(work)}), encoding="utf-8")
    return config_loader.load_config(cfgp)


def _stub_sync(monkeypatch, target=None):
    """Neutralise `sync.pull/push` pour le module ciblé (`wiki_extract` par défaut)."""
    if target is None:
        target = wiki_extract.sync
    monkeypatch.setattr(target, "pull", lambda c: 0)
    monkeypatch.setattr(target, "push", lambda c: 0)


def _fake_clip_factory(monkeypatch, *, fail_urls: set[str] | None = None):
    """Stub `wiki_extract.run` : écrit un faux clip `.md` sous `raw/web/`.

    Le contenu du clip est déterministe (titre = slug de l'URL, source = URL
    complète). Si `fail_urls` contient l'URL, on renvoie un Outcome `error`
    sans rien écrire.

    Le `_inbox/` n'est pas utilisé : on n'appelle pas `process_source` (qui
    déplace depuis l'inbox), on **écrit directement** un `.md` dans
    `raw/web/<slug>-<date>.md` pour simuler ce que `wiki-extract` aurait
    produit.
    """
    fail_urls = fail_urls or set()
    calls: list[str] = []

    def _stub_run(c, source, *, lang, dry_run, skip_sync=False):
        calls.append(source)
        if source in fail_urls:
            return [wiki_extract.Outcome(source, "web", "error", message="fetch KO (stub)")]
        # slug basique depuis l'URL
        slug = source.rsplit("/", 1)[-1].split("?")[0] or "clip"
        from datetime import date
        today = date.today().strftime("%Y%m%d")
        base = f"{slug}-{today}"
        dest = c.RAW / "web" / f"{base}.md"
        # contenu : frontmatter `source:` + corps (le slug est <base>, le sujet
        # sera tagué après par wiki_research)
        text = (
            "---\n"
            f"title: {slug}\n"
            f"source: {source}\n"
            f"type: web\n"
            f"created: {date.today().isoformat()}\n"
            "tags: [source]\n"
            "---\n\n"
            f"Contenu factice du clip sur {source}.\n"
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        return [wiki_extract.Outcome(source, "web", "extrait", content_path=dest)]

    monkeypatch.setattr(wr.wiki_extract, "run", _stub_run)
    return calls


# --- 6.1 : sujet → boucle de clip → raw/web/<sujet>/ -----------------------
def test_clip_loop_pose_clips_et_les_annote(cfg, monkeypatch):
    """`clip <sujet> <url1> <url2>` produit 2 fichiers `raw/web/*.md` tagués du sujet."""
    _stub_sync(monkeypatch)
    calls = _fake_clip_factory(monkeypatch)

    urls = ["https://exemple.org/article-1", "https://exemple.org/article-2"]
    records = wr.cmd_clip(
        cfg, "expertise-judiciaire", urls, lang="fra", dry_run=False, skip_sync=True,
    )

    assert len(records) == 2
    assert all(r.status == "extrait" for r in records)
    assert calls == urls

    # les clips existent, sont tagués du sujet
    for r in records:
        p = Path(r.content_path)
        assert p.exists()
        front, _ = frontmatter.parse_frontmatter(p.read_text(encoding="utf-8"))
        assert front.get("subject") == "expertise-judiciaire"
        assert "expertise-judiciaire" in (front.get("tags") or [])


def test_clip_renvoie_err_pour_url_inaccessible(cfg, monkeypatch, capsys):
    """Un clip en échec est remonté `error`, **rien d'inventé** (pas de placeholder)."""
    _stub_sync(monkeypatch)
    _fake_clip_factory(monkeypatch, fail_urls={"https://exemple.org/ko"})

    records = wr.cmd_clip(
        cfg, "expertise-judiciaire",
        ["https://exemple.org/ok", "https://exemple.org/ko"],
        lang="fra", dry_run=False, skip_sync=True,
    )
    statuses = [r.status for r in records]
    assert "extrait" in statuses
    assert "error" in statuses
    # le clip OK a bien été écrit, le KO n'a pas créé de fichier
    web_dir = cfg.RAW / "web"
    mds = list(web_dir.glob("*.md"))
    assert len(mds) == 1


# --- 6.2 : dédup URL --------------------------------------------------------
def test_dedup_url_pas_de_reclip(cfg, monkeypatch):
    """Re-clipper une URL déjà présente : `skipped_duplicate`, pas de nouvel appel `wiki_extract.run`."""
    _stub_sync(monkeypatch)
    calls = _fake_clip_factory(monkeypatch)

    urls = ["https://exemple.org/article-1"]
    wr.cmd_clip(cfg, "expertise-judiciaire", urls, lang="fra", dry_run=False, skip_sync=True)
    assert len(calls) == 1

    # 2e appel : même URL, dédup doit court-circuiter AVANT `wiki_extract.run`.
    records2 = wr.cmd_clip(
        cfg, "expertise-judiciaire", urls, lang="fra", dry_run=False, skip_sync=True,
    )
    assert len(records2) == 1
    assert records2[0].status == "skipped_duplicate"
    # on n'a **pas** re-clippé : `wiki_extract.run` n'a pas été rappelé
    assert len(calls) == 1


# --- 6.3 : scaffold ---------------------------------------------------------
def test_scaffold_produit_frontmatter_research_report(cfg):
    """`scaffold <sujet>` écrit `reading-grids/<sujet>.md` avec frontmatter conforme."""
    dest = wr.cmd_scaffold(cfg, "expertise-judiciaire", title="Expertise judiciaire en France")

    assert dest.exists()
    front, body = frontmatter.parse_frontmatter(dest.read_text(encoding="utf-8"))
    assert front.get("type") == "research-report"
    assert front.get("topic") == "Expertise judiciaire en France"
    assert front.get("subject") == "expertise-judiciaire"
    assert front.get("created") == date.today().isoformat()
    assert front.get("updated") == date.today().isoformat()
    assert front.get("sources") == []
    assert "report" in front.get("tags", [])
    assert "expertise-judiciaire" in front.get("tags", [])
    # corps = plan (sections indicatives)
    assert "## Angle" in body
    assert "## Synthèse" in body
    assert "## Sources" in body


def test_sandbox_slug_kebab_case(cfg):
    """Le sujet texte est slugifié (kebab-case ASCII).

    `slug.slugify` retire les apostrophes et la ponctuation **sans** insérer de
    séparateur (`L'Expertise` → `lexpertise`). Le test vérifie ce comportement
    réel (pas un cas idéalisé).
    """
    wr.cmd_scaffold(cfg, "L'Expertise Judiciaire en Appel !")
    dest = cfg.READING_GRIDS / "lexpertise-judiciaire-en-appel.md"
    assert dest.exists(), "le slug ASCII kebab-case doit être produit"


def test_scaffold_idempotent_pas_d_ecrasement(cfg):
    """Un rapport déjà existant n'est PAS écrasé par `scaffold`."""
    dest = wr.cmd_scaffold(cfg, "expertise-judiciaire")
    original = dest.read_text(encoding="utf-8")
    # 2e scaffold → ne change rien
    wr.cmd_scaffold(cfg, "expertise-judiciaire")
    assert dest.read_text(encoding="utf-8") == original


# --- 6.4 : renvois invalides -----------------------------------------------
def test_update_audite_les_renvois_casses(cfg, monkeypatch):
    """Un `[[sujet/notion-inexistante]]` dans le rapport est listé comme renvoi invalide."""
    _stub_sync(monkeypatch)
    _fake_clip_factory(monkeypatch)

    # 1) on clip une URL
    wr.cmd_clip(
        cfg, "expertise-judiciaire", ["https://exemple.org/article-1"],
        lang="fra", dry_run=False, skip_sync=True,
    )
    # 2) on scaffolde le rapport
    wr.cmd_scaffold(cfg, "expertise-judiciaire")
    # 3) on injecte un wikilink cassé dans le rapport (article inexistant)
    rapport = cfg.READING_GRIDS / "expertise-judiciaire.md"
    text = rapport.read_text(encoding="utf-8")
    text += "\n\nRenvoi cassé : [[expertise-judiciaire/notion-inexistante]]\n"
    rapport.write_text(text, encoding="utf-8")

    # 4) update doit le signaler
    st = wr.cmd_update(cfg, "expertise-judiciaire", dry_run=False)
    invalid_targets = [r.target for r in st.renvois_invalid]
    assert "expertise-judiciaire/notion-inexistante" in invalid_targets


def test_update_renvois_valides_ne_signalent_rien(cfg, monkeypatch):
    """Un `[[sujet/notion-reelle]]` (article wiki existant) ne génère aucun renvoi invalide."""
    # on crée un article réel dans le wiki
    (cfg.WIKI / "expertise-judiciaire").mkdir(parents=True)
    (cfg.WIKI / "expertise-judiciaire" / "mission.md").write_text(
        "---\ntitle: Mission\nsubject: expertise-judiciaire\n---\n\n# Mission\n", encoding="utf-8"
    )
    _stub_sync(monkeypatch)
    _fake_clip_factory(monkeypatch)
    wr.cmd_clip(
        cfg, "expertise-judiciaire", ["https://exemple.org/article-1"],
        lang="fra", dry_run=False, skip_sync=True,
    )
    # on scaffolde puis on **remplace le corps** par un contenu qui ne référence
    # QUE des cibles valides (le plan par défaut contient un placeholder `[[<sujet>/<notion>]]`
    # qui, par construction, ne résout pas).
    wr.cmd_scaffold(cfg, "expertise-judiciaire")
    rapport = cfg.READING_GRIDS / "expertise-judiciaire.md"
    front, _ = frontmatter.parse_frontmatter(rapport.read_text(encoding="utf-8"))
    body = "\n# Expertise\n\nMission : [[expertise-judiciaire/mission]]\n"
    rapport.write_text(wr._render_frontmatter(front) + body, encoding="utf-8")

    st = wr.cmd_update(cfg, "expertise-judiciaire", dry_run=False)
    assert st.renvois_invalid == [], (
        f"un renvoi vers un article réel ne doit pas être invalide, "
        f"renvois_invalides={st.renvois_invalid}"
    )


# --- 6.5 : idempotence update ----------------------------------------------
def test_update_preserve_created_et_corps(cfg, monkeypatch):
    """`update` préserve `created:` ET le corps du rapport (ne réécrit pas la prose)."""
    _stub_sync(monkeypatch)
    _fake_clip_factory(monkeypatch)
    wr.cmd_clip(
        cfg, "expertise-judiciaire", ["https://exemple.org/article-1"],
        lang="fra", dry_run=False, skip_sync=True,
    )
    wr.cmd_scaffold(cfg, "expertise-judiciaire")
    rapport = cfg.READING_GRIDS / "expertise-judiciaire.md"
    text = rapport.read_text(encoding="utf-8")
    # on fige la date de création dans le passé
    text = text.replace(
        f"created: {date.today().isoformat()}", "created: 2020-01-01"
    )
    # on ajoute un paragraphe de corps (semantic content)
    text += "\n## Note de l'agent\n\nParagraphe rédigé à la main — NE PAS ÉCRASER.\n"
    rapport.write_text(text, encoding="utf-8")

    # 1er update
    wr.cmd_update(cfg, "expertise-judiciaire", dry_run=False)
    after_first = rapport.read_text(encoding="utf-8")
    assert "created: 2020-01-01" in after_first, "created: doit être préservé"
    assert "Paragraphe rédigé à la main" in after_first, "corps préservé"
    assert "updated: " + date.today().isoformat() in after_first

    # 2e update : aucune duplication, pas de régression
    wr.cmd_update(cfg, "expertise-judiciaire", dry_run=False)
    after_second = rapport.read_text(encoding="utf-8")
    # le bloc Note de l'agent n'apparaît qu'UNE fois
    assert after_second.count("Note de l'agent") == 1
    assert after_second.count("Paragraphe rédigé à la main") == 1
    assert "created: 2020-01-01" in after_second


def test_update_synchronise_sources_avec_etat_reel(cfg, monkeypatch):
    """`update` met `sources:` à la liste **effective** des URLs clippées."""
    _stub_sync(monkeypatch)
    _fake_clip_factory(monkeypatch)
    urls = [
        "https://exemple.org/article-1",
        "https://exemple.org/article-2",
        "https://exemple.org/article-3",
    ]
    wr.cmd_clip(
        cfg, "expertise-judiciaire", urls, lang="fra", dry_run=False, skip_sync=True,
    )
    wr.cmd_scaffold(cfg, "expertise-judiciaire")
    st = wr.cmd_update(cfg, "expertise-judiciaire", dry_run=False)
    # sources = URLs clippées (triées)
    assert sorted(st.sources_actual) == sorted(urls)
    # le frontmatter du rapport est aligné
    front, _ = frontmatter.parse_frontmatter(
        (cfg.READING_GRIDS / "expertise-judiciaire.md").read_text(encoding="utf-8")
    )
    assert sorted(front.get("sources") or []) == sorted(urls)


# --- 6.6 : intégration wiki-index ------------------------------------------
def _add_research_report_to_vault(cfg):
    """Pose un rapport `research-report` factice dans `reading-grids/`.

    Crée aussi un article `wiki/<sujet>/<notion>.md` minimal pour que
    l'audit ait un référentiel cohérent.
    """
    sujet = "expertise-judiciaire"
    (cfg.WIKI / sujet).mkdir(parents=True, exist_ok=True)
    (cfg.WIKI / sujet / "_INDEX.md").write_text(
        f"# Index — {sujet}\n\n- [[{sujet}/appel]]\n", encoding="utf-8"
    )
    (cfg.WIKI / sujet / "appel.md").write_text(
        f"---\ntitle: Appel\nsubject: {sujet}\n---\n\n# Appel\n", encoding="utf-8"
    )
    # le rapport
    text = (
        "---\n"
        "title: Expertise judiciaire en France\n"
        "type: research-report\n"
        f"topic: Expertise judiciaire en France\n"
        f"subject: {sujet}\n"
        "created: 2026-06-14\n"
        "updated: 2026-06-14\n"
        "sources: []\n"
        f"tags: [report, {sujet}]\n"
        "---\n\n"
        f"# Expertise judiciaire en France\n\n"
        f"Voir aussi [[{sujet}/appel]].\n"
    )
    (cfg.READING_GRIDS / f"{sujet}.md").write_text(text, encoding="utf-8")


def test_wiki_index_reconnait_research_report_comme_grille(cfg):
    """`grids_by_subject` retourne le rapport sous `kind: research-report`."""
    _add_research_report_to_vault(cfg)
    grids = index_builder.grids_by_subject(cfg)
    assert "expertise-judiciaire" in grids
    refs = grids["expertise-judiciaire"]
    research = [r for r in refs if r.kind == "research-report"]
    assert len(research) == 1
    assert research[0].slug == "expertise-judiciaire"
    assert research[0].topic == "Expertise judiciaire en France"
    assert research[0].chapters == 0


def test_wiki_index_indexe_le_rapport_dans_sujet_index(cfg):
    """`regenerate` liste le rapport dans `## Rapports de recherche` du sujet index."""
    _add_research_report_to_vault(cfg)
    index_builder.regenerate(cfg, dry_run=False)
    doc = (cfg.WIKI / "expertise-judiciaire" / "_INDEX.md").read_text(encoding="utf-8")
    assert "## Rapports de recherche" in doc
    assert "[[reading-grids/expertise-judiciaire]] — Expertise judiciaire en France" in doc


def test_wiki_index_audite_les_liens_du_rapport(cfg):
    """`audit` signale les wikilinks cassés présents dans le rapport."""
    _add_research_report_to_vault(cfg)
    # on injecte un lien cassé dans le rapport
    rapport = cfg.READING_GRIDS / "expertise-judiciaire.md"
    text = rapport.read_text(encoding="utf-8")
    text += "\n\n[[expertise-judiciaire/notion-nexiste-pas]]\n"
    rapport.write_text(text, encoding="utf-8")

    report = link_audit.audit(cfg)
    # le rapport figure dans les fichiers audités, son lien cassé remonte
    broken_targets = [tgt for _src, tgt in report.broken]
    assert "expertise-judiciaire/notion-nexiste-pas" in broken_targets


# --- helpers de status ------------------------------------------------------
def test_status_diagnostique_rapport(cfg, monkeypatch):
    """`status` retourne l'état lecture seule (created, updated, count, renvois)."""
    _stub_sync(monkeypatch)
    _fake_clip_factory(monkeypatch)
    wr.cmd_clip(
        cfg, "expertise-judiciaire", ["https://exemple.org/article-1"],
        lang="fra", dry_run=False, skip_sync=True,
    )
    wr.cmd_scaffold(cfg, "expertise-judiciaire")
    st = wr.cmd_status(cfg, "expertise-judiciaire")
    assert st.exists
    assert st.clips_count == 1
    assert "https://exemple.org/article-1" in st.sources_actual


def test_status_sans_rapport_existe_false(cfg):
    st = wr.cmd_status(cfg, "sujet-nexiste-pas")
    assert not st.exists


# --- CLI smoke test --------------------------------------------------------
def test_cli_status_sans_rapport_exit_1(cfg, monkeypatch):
    """`status` retourne exit 1 si le rapport n'existe pas (signal consultatif)."""
    import subprocess
    import sys as _sys
    # on propage sys.path (conftest) au sous-processus, sinon bootstrap casse
    # sur l'import de `grid_builder` (palier v3→v4 du socle).
    env_add = {"PYTHONPATH": ":".join(sys.path)}
    import os
    env = {**os.environ, **env_add}
    cfg_path = cfg.config_path
    result = subprocess.run(
        [_sys.executable, str(Path(wr.__file__).resolve()),
         "--config", str(cfg_path), "status", "sujet-nexiste-pas"],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 1
    assert "aucun rapport" in result.stderr
