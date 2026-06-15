"""Tests de `wiki-batch-produce` (spec `batch-produce`) : orchestration batch web.

Couvre : refus des sources non-web (PDF/EPUB), idempotence, validation
post-écriture + rollback, frontmatter flow-list, sérialisation + finalize
par source + audit best-effort.

Tous les tests sont **hermétiques** : on monkeypatche les sous-appels
(`wiki_ingest.cmd_finalize`, `link_audit.audit`, `sync.pull/push`) pour ne
déclencher aucune I/O hors `tmp_path`. Les `runner` du socle acceptent
l'injection — c'est précisément le point du bootstrap mutualisé.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import batch_produce
import config_loader
import ledger as L
import status_table
import wiki_ingest
from article_writer import ArticleSpec


# --- helpers de fixtures ---------------------------------------------------
@pytest.fixture
def cfg(tmp_path):
    work = tmp_path / "work"
    (work / "raw" / "web" / "expertise-judiciaire").mkdir(parents=True)
    cfgp = tmp_path / "wiki.config.json"
    cfgp.write_text(json.dumps({"work_root": str(work)}), encoding="utf-8")
    return config_loader.load_config(cfgp)


def _render_front_line(k: str, v) -> str:
    """Sérialise une ligne de frontmatter (scalaire | liste[str] | liste[dict])."""
    if isinstance(v, list):
        if all(isinstance(x, str) for x in v):
            return f"{k}: [{', '.join(v)}]"
        # liste de dicts (chapitrage) : on émet en JSON compact pour ce test
        return f"{k}: {json.dumps(v, ensure_ascii=False)}"
    return f"{k}: {json.dumps(v, ensure_ascii=False)}"


def _seed_web_source(
    cfg,
    base: str = "expertise-source",
    *,
    title: str = "L'expertise judiciaire en France",
    body: str | None = None,
    tags: list[str] | None = None,
    extra_front: dict | None = None,
) -> Path:
    """Écrit une source `.md` valide (frontmatter + corps) sous raw/web/<sujet>/."""
    default_body = body or (
        "L'expertise judiciaire est une mesure d'instruction réalisée par un technicien. "
        "L'expert est désigné par le juge. Il dispose d'une mission précise. "
        "Son rapport est versé au dossier. La déontologie encadre sa pratique.\n\n"
        "## Cadre légal\n\n"
        "Les articles 263 et suivants du Code de procédure civile organisent la mesure. "
        "Le juge peut ordonner toute expertise utile. Les parties formulent des observations.\n\n"
        "## Déroulement\n\n"
        "- Le juge rend une ordonnance de désignation.\n"
        "- L'expert accepte la mission et convoque les parties.\n"
        "- Les opérations se déroulent sur pièces ou sur place.\n"
        "- Le rapport est déposé dans le délai imparti.\n"
        "- Les parties disposent d'un délai pour observations.\n\n"
        "L'expert doit respecter le principe du contradictoire. Il peut être récusé pour cause de partialité. "
        "Sa mission peut être étendue par le juge. Le rapport est un document technique opposable."
    )
    front = {
        "title": title,
        "source": f"https://exemple.org/articles/{base}",
        "type": "web",
        "created": "2026-06-14",
        "tags": tags if tags is not None else ["expertise-judiciaire", "source"],
    }
    if extra_front:
        front.update(extra_front)
    text = (
        "---\n"
        + "\n".join(_render_front_line(k, v) for k, v in front.items())
        + "\n---\n\n"
        + default_body
    )
    src = cfg.RAW / "web" / "expertise-judiciaire" / f"{base}.md"
    src.write_text(text, encoding="utf-8")
    return src


def _stub_sync(monkeypatch):
    """Neutralise sync.pull / sync.push — batch est par défaut en skip-sync."""
    monkeypatch.setattr(batch_produce.sync, "pull", lambda c: 0)
    monkeypatch.setattr(batch_produce.sync, "push", lambda c: 0)


def _stub_finalize(monkeypatch):
    """Stub `wiki_ingest.cmd_finalize` pour ne pas écrire de ledger dans ces tests
    (les tests dédiés `test_finalize_par_source_*` réactivent la vraie fonction)."""
    calls: list[dict] = []

    def _fake(cfg, *, source, sha, articles, status="compilé", skip_sync=False, dry_run=False):
        calls.append({"source": source, "sha": sha, "articles": articles, "status": status})
        return wiki_ingest.Outcome("finalize", "finalisé", f"(stub) {len(articles)} article(s)")

    monkeypatch.setattr(batch_produce.wiki_ingest, "cmd_finalize", _fake)
    return calls


# --- 4.4 : refus des sources non-web (.pdf / .epub) -----------------------
def test_refus_pdf_et_epub(cfg, tmp_path, monkeypatch, capsys):
    """Une source `.pdf` ou `.epub` dans l'inbox-dir est refusée avec
    message d'aiguillage vers `wiki-ingest` (spec : Web-only batch scope)."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)

    inbox = cfg.RAW / "web" / "expertise-judiciaire"
    (inbox / "document.pdf").write_bytes(b"%PDF-1.4\n")
    (inbox / "livre.epub").write_bytes(b"PK\x03\x04 EPUB")

    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    outcomes = report["outcomes"]
    refused = [o for o in outcomes if o["status"] == "refused"]
    assert len(refused) == 2
    for o in refused:
        assert "wiki-ingest" in o["detail"]  # aiguillage explicite
    # rien d'écrit côté wiki
    assert not (cfg.WIKI / "expertise-judiciaire").exists() or not list(
        (cfg.WIKI / "expertise-judiciaire").glob("*.md")
    )


def test_refus_frontmatter_chapitre(cfg, monkeypatch):
    """Un `.md` qui déclare `chapters:` est chapitré → `wiki-ingest`, pas batch."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    _seed_web_source(
        cfg, base="guide", title="Guide chapitré",
        extra_front={"chapters": [{"order": 1, "title": "Intro"}]},
    )
    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    refused = [o for o in report["outcomes"] if o["status"] == "refused"]
    assert any("wiki-ingest" in o["detail"] for o in refused)


def test_refus_frontmatter_incomplet(cfg, monkeypatch):
    """Frontmatter sans `title` ou sans `source` → refus (`↩️`), pas d'invention."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    inbox = cfg.RAW / "web" / "expertise-judiciaire"
    (inbox / "incomplet.md").write_text(
        "---\ntype: web\n---\n\nDu texte sans titre ni source.\n", encoding="utf-8"
    )
    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    refused = [o for o in report["outcomes"] if o["status"] == "refused"]
    assert any("title" in o["detail"] and "source" in o["detail"] for o in refused)


# --- 4.1 : .md source → article wiki créé ---------------------------------
def test_md_source_cree_article_wiki(cfg, monkeypatch):
    """Une source `.md` valide produit un article Karpathy dans `wiki/<sujet>/`
    avec frontmatter flow-list, ≥ 4 sections, bloc mermaid ouvert ET fermé."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    _seed_web_source(cfg, base="expertise-source", title="L'expertise judiciaire")

    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )

    written = [o for o in report["outcomes"] if o["status"] == "written"]
    assert len(written) == 1
    slug = written[0]["slug"]
    target = cfg.WIKI / "expertise-judiciaire" / f"{slug}.md"
    assert target.is_file()
    text = target.read_text(encoding="utf-8")

    # --- frontmatter flow-list -------------------------------------------
    assert 'title: "L\'expertise judiciaire"' in text
    assert "subject: expertise-judiciaire" in text
    assert "tags: [expertise-judiciaire, source]" in text
    assert re.search(r'sources: \["raw/web/expertise-judiciaire/expertise-source\.md", '
                     r'"https://exemple\.org/articles/expertise-source"\]', text)

    # --- Karpathy : 5 sections attendues ----------------------------------
    headings = re.findall(r"^## .+$", text, flags=re.MULTILINE)
    assert len(headings) >= 4
    for h in ("## Définition", "## Points clés", "## Mermaid", "## Relations", "## Sources"):
        assert h in text

    # --- bloc mermaid ouvert ET fermé ------------------------------------
    open_m = text.find("```mermaid")
    assert open_m != -1
    close_m = text.find("```", open_m + len("```mermaid"))
    assert close_m != -1, "bloc ```mermaid non fermé"

    # --- taille ≥ 500 octets ---------------------------------------------
    assert len(text.encode("utf-8")) >= 500


def test_frontmatter_flow_list_tags_multiples(cfg, monkeypatch):
    """Frontmatter rendu en flow-list même avec plusieurs tags (cohérent
    avec `article_writer._render_list` côté ingest)."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    _seed_web_source(
        cfg, base="multi", title="Cas multi-tags",
        tags=["expertise-judiciaire", "procédure", "source"],
    )
    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    target = cfg.WIKI / "expertise-judiciaire" / "cas-multi-tags.md"
    text = target.read_text(encoding="utf-8")
    assert "tags: [expertise-judiciaire, procédure, source]" in text


def test_mismatch_tags_sujet_procede_avec_warning(cfg, monkeypatch, capsys):
    """Si les `tags` de la source ne contiennent pas `--subject`, on continue
    avec un warning (spec : `--subject` fait foi)."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    _seed_web_source(
        cfg, base="autre-sujet", title="Source d'un autre sujet",
        tags=["autre-sujet", "source"],
    )
    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    written = [o for o in report["outcomes"] if o["status"] == "written"]
    assert len(written) == 1
    assert any("mismatch" in w for w in report["warnings"])


# --- 4.2 : 2e run idempotent (skip) ---------------------------------------
def test_second_run_idempotent_skip(cfg, monkeypatch):
    """Le 2e run sur le même inbox-dir doit reporter `↩️` (skipped) et n'altérer
    ni les fichiers ni le ledger. C'est la garantie qu'on peut relancer un batch."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    _seed_web_source(cfg, base="source-1", title="Source un")

    cfg_path = "raw/web/expertise-judiciaire"
    report1 = batch_produce.cmd_batch_produce(
        cfg, inbox_dir=cfg_path, subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    assert sum(1 for o in report1["outcomes"] if o["status"] == "written") == 1

    target = cfg.WIKI / "expertise-judiciaire" / "source-un.md"
    first_mtime = target.stat().st_mtime_ns
    first_text = target.read_text(encoding="utf-8")

    # 2e run : on n'a pas re-touché la source
    report2 = batch_produce.cmd_batch_produce(
        cfg, inbox_dir=cfg_path, subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    skipped = [o for o in report2["outcomes"] if o["status"] == "skipped"]
    assert len(skipped) == 1
    assert "destination existante" in skipped[0]["detail"]

    # le fichier n'a pas été réécrit
    assert target.read_text(encoding="utf-8") == first_text
    assert target.stat().st_mtime_ns == first_mtime


def test_slug_collision_disambiguation(cfg, monkeypatch):
    """Deux sources avec le même `title` slugifié : la 2e est suffixée `-2`
    (spec : `slug-2`, `slug-3` sur collision intra-sujet)."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    _seed_web_source(cfg, base="a", title="Notion")
    _seed_web_source(cfg, base="b", title="Notion")  # même titre

    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    written_slugs = sorted(o["slug"] for o in report["outcomes"] if o["status"] == "written")
    assert written_slugs == ["notion", "notion-2"]
    assert (cfg.WIKI / "expertise-judiciaire" / "notion.md").is_file()
    assert (cfg.WIKI / "expertise-judiciaire" / "notion-2.md").is_file()


# --- 4.3 : article trop court → rollback (rien sur disque) ---------------
def test_article_trop_court_rollback(cfg, monkeypatch):
    """Une source trop maigre ne passe pas la validation (≥ 500 octets) :
    on rollback (`os.remove(dest)`) et `❌` est reporté. **Rien** ne reste
    sur disque (spec : on ne pollue pas le wiki)."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)

    # Frontmatter MINIMAL (sinon refus) + corps VIDE. Le titre très court
    # et l'URL courte produisent un article squelette sous le seuil de 500
    # octets : rollback effectif. Avec un titre plus long, le fallback
    # `_first_paragraph(body) or title` (cf. `batch_produce._build_body`)
    # répéterait le titre dans la définition, ce qui regonfle l'article
    # au-dessus du seuil — donc on MINIMISE le titre ici.
    src = cfg.RAW / "web" / "expertise-judiciaire" / "trop-court.md"
    src.write_text(
        "---\n"
        'title: "X"\n'
        'source: "https://x.y"\n'
        "tags: [s]\n"
        "---\n\n",
        encoding="utf-8",
    )

    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    rolled = [o for o in report["outcomes"] if o["status"] == "rolled_back"]
    assert len(rolled) == 1
    # la raison doit pointer le critère taille (le 1er à sauter sans body)
    assert "500" in rolled[0]["detail"] or "court" in rolled[0]["detail"]
    # rien d'écrit
    subj_dir = cfg.WIKI / "expertise-judiciaire"
    if subj_dir.exists():
        assert not list(subj_dir.glob("*.md"))


# --- 2.5 : finalize par source + audit best-effort -------------------------
def test_finalize_par_source_et_audit_best_effort(cfg, monkeypatch):
    """Chaque source écrite déclenche un `wiki-ingest finalize` (cohérence
    ledger + _status.md). L'audit final est appelé une fois, en best-effort :
    un échec n'abort pas le run."""
    _stub_sync(monkeypatch)
    finalize_calls = _stub_finalize(monkeypatch)

    # audit best-effort : on le fait renvoyer un rapport "anormal" pour
    # vérifier qu'il est loggé sans abort.
    class _FakeReport:
        ok = False
        total = 7
        broken = ["a"]
        orphans = ["b", "c"]
        dangling = []
        cjk = ["d", "e", "f", "g"]
    monkeypatch.setattr(batch_produce.link_audit, "audit", lambda c: _FakeReport())

    _seed_web_source(cfg, base="source-1", title="Première source")
    _seed_web_source(cfg, base="source-2", title="Deuxième source")

    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    # deux sources écrites
    assert sum(1 for o in report["outcomes"] if o["status"] == "written") == 2
    # un finalize par source écrite (et pas plus)
    assert len(finalize_calls) == 2
    for c in finalize_calls:
        assert c["status"] == "compilé"
        assert c["articles"] == [
            f"expertise-judiciaire/{c['source'].rsplit('/', 1)[-1].removesuffix('.md')}"
        ] or c["articles"][0].startswith("expertise-judiciaire/")
    # audit best-effort : rapport "anormal" loggé sans abort
    assert "anomalie" in report["audit"]


def test_finalize_echec_ne_fait_pas_abort_le_run(cfg, monkeypatch):
    """Un échec de finalize est loggé dans `warnings`, n'abort pas le batch."""
    _stub_sync(monkeypatch)

    def _boom(*a, **kw):
        raise RuntimeError("finalize KO")
    monkeypatch.setattr(batch_produce.wiki_ingest, "cmd_finalize", _boom)

    _seed_web_source(cfg, base="a", title="Source A")
    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    written = [o for o in report["outcomes"] if o["status"] == "written"]
    assert len(written) == 1  # l'article EST écrit malgré l'échec du finalize
    assert any("finalize" in w for w in report["warnings"])


# --- dry-run : aucune écriture ---------------------------------------------
def test_dry_run_necrit_rien(cfg, monkeypatch):
    """`--dry-run` liste sans rien écrire : pas d'article, pas de ledger."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    _seed_web_source(cfg, base="x", title="Source X")

    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=True,
    )
    outcomes = report["outcomes"]
    assert all(o["status"] == "skipped" for o in outcomes)
    assert "dry-run" in report["audit"]
    # aucun article sur disque
    subj = cfg.WIKI / "expertise-judiciaire"
    if subj.exists():
        assert not list(subj.glob("*.md"))
    # finalize non appelé
    # (vérifié par l'absence de stub_calls si on inspecte directement)


# --- serialisation : pas de parallélisme -----------------------------------
def test_sources_traitees_en_serie(monkeypatch, cfg):
    """Les sources sont traitées séquentiellement. On le vérifie en
    observant que l'invariant 'toutes les écritures dupliquées passent par
    `atomic_write_text` une fois à la fois' tient (pas de batch I/O).
    Plus pragmatique : la fonction de batch est une boucle for — si elle
    devient parallèle un jour, ce test saute visiblement."""
    writes: list[Path] = []
    real = batch_produce.iohelpers.atomic_write_text

    def _spy(path, text, *, work_root):
        writes.append(Path(path))
        return real(path, text, work_root=work_root)

    monkeypatch.setattr(batch_produce.iohelpers, "atomic_write_text", _spy)
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    _seed_web_source(cfg, base="a", title="Source A")
    _seed_web_source(cfg, base="b", title="Source B")
    _seed_web_source(cfg, base="c", title="Source C")

    batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    # 3 sources, 3 écritures (1 par article), dans l'ordre alphabétique
    assert len(writes) == 3
    slugs = [p.stem for p in writes]
    assert slugs == ["source-a", "source-b", "source-c"]


# --- slug : titre accentué → ASCII ----------------------------------------
def test_slug_ascii_depuis_titre_accentue(cfg, monkeypatch):
    """`slugify('Notion accentuée')` = 'notion-accentuee' (cf. helpers `slug.py`).

    Sur macOS/APFS, le fichier est stocké en NFD (`é` = `e + ◌́`) : on compare
    via `slug.same_file` (NFD/NFC-safe) plutôt qu'en `Path.__eq__` direct.
    """
    import slug as slug_mod
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    _seed_web_source(cfg, base="acc", title="Notion accentuée")
    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    subject_dir = cfg.WIKI / "expertise-judiciaire"
    md_files = list(subject_dir.glob("*.md"))
    assert any(slug_mod.same_file(p.stem, "notion-accentuee") for p in md_files), (
        f"slug 'notion-accentuee' introuvable dans {[p.name for p in md_files]}"
    )


# --- sidecars de service : jamais traités ---------------------------------
def test_sidecars_service_ignores(cfg, monkeypatch):
    """`INDEX.md`, `_status.md`, fichiers cachés dans inbox-dir : ignorés,
    jamais comptés comme source."""
    _stub_sync(monkeypatch)
    _stub_finalize(monkeypatch)
    inbox = cfg.RAW / "web" / "expertise-judiciaire"
    (inbox / "INDEX.md").write_text("# index\n", encoding="utf-8")
    (inbox / "_status.md").write_text("# statut\n", encoding="utf-8")
    (inbox / ".DS_Store").write_bytes(b"")
    _seed_web_source(cfg, base="seule", title="Seule source")

    report = batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )
    written = [o for o in report["outcomes"] if o["status"] == "written"]
    assert len(written) == 1


# --- finalize : la vraie cmd_finalize est appelée (intégration légère) -----
def test_finalize_integre_ledger_et_status(cfg, monkeypatch):
    """Sanity check : on réactive le VRAI `cmd_finalize` (sans stub) et on
    vérifie que le ledger + `_status.md` sont bien mis à jour. C'est la
    garantie que `batch_produce` est compatible avec le format ledger
    historique."""
    _stub_sync(monkeypatch)

    _seed_web_source(cfg, base="reelle", title="Source réelle")
    batch_produce.cmd_batch_produce(
        cfg, inbox_dir="raw/web/expertise-judiciaire", subject="expertise-judiciaire",
        skip_sync=True, dry_run=False,
    )

    led = L.load_ledger(cfg.LEDGER)
    assert "raw/web/expertise-judiciaire/reelle.md" in led
    status_path = cfg.RAW / "web" / "_status.md"
    if status_path.exists():
        rows = status_table.load_status(status_path)
        row = next((r for r in rows if r.fichier == "reelle.md"), None)
        assert row is not None
        assert row.statut == "compilé"
