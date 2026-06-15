"""Tests du CLI wiki-init : création idempotente, confinement, diagnostic, amorçage sync."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import wiki_init
import vault_meta
from sync import locking as sync_lock
from config_loader import EXPECTED_SCHEMA


@pytest.fixture(autouse=True)
def _lock_in_tmp(tmp_path, monkeypatch):
    """Verrou hors ~/.cache réel et hors work_root (mêmes garanties que wiki-sync)."""
    monkeypatch.setattr(sync_lock, "_DEFAULT_LOCK", tmp_path / "lock" / "wiki-sync.lock")


def _make_config(tmp_path, *, sync_cfg=None, layout=None, work_exists=False):
    """Écrit un wiki.config.json de test ; work_root vide par défaut (cas premier run)."""
    work = tmp_path / "vault" / "second-cerveau"
    if work_exists:
        work.mkdir(parents=True)
    data = {"work_root": str(work)}
    if layout is not None:
        data["layout"] = layout
    if sync_cfg is not None:
        data["sync"] = sync_cfg
    cfgp = tmp_path / "wiki.config.json"
    cfgp.write_text(json.dumps(data), encoding="utf-8")
    return str(cfgp), work


# --- apply : création de l'arborescence ------------------------------------
def test_apply_cree_la_structure_sur_vault_vide(tmp_path):
    """work_root inexistant -> racine + 4 dossiers + accueil + INDEX créés."""
    cfgp, work = _make_config(tmp_path)  # work_root n'existe pas encore
    assert wiki_init.main(["--config", cfgp, "apply", "--skip-sync"]) == 0

    assert work.is_dir()
    for sub in ("_inbox", "raw", "wiki", "reading-grids"):
        assert (work / sub).is_dir(), f"{sub} manquant"
    assert (work / "_inbox" / "LISEZ-MOI.md").is_file()
    assert (work / "wiki" / "INDEX.md").is_file()
    # `.wiki/` (ledger) n'est PAS créé par l'init.
    assert not (work / ".wiki").exists()


def test_apply_respecte_le_layout_personnalise(tmp_path):
    cfgp, work = _make_config(tmp_path, layout={"inbox": "boite", "wiki": "savoir"})
    assert wiki_init.main(["--config", cfgp, "apply", "--skip-sync"]) == 0
    assert (work / "boite").is_dir()
    assert (work / "savoir").is_dir()
    assert (work / "boite" / "LISEZ-MOI.md").is_file()
    assert (work / "savoir" / "INDEX.md").is_file()


def test_apply_idempotent_ne_reecrit_pas_les_fichiers(tmp_path):
    """Relancer apply ne doit jamais écraser l'accueil/INDEX modifiés par l'utilisateur."""
    cfgp, work = _make_config(tmp_path)
    assert wiki_init.main(["--config", cfgp, "apply", "--skip-sync"]) == 0

    readme = work / "_inbox" / "LISEZ-MOI.md"
    index = work / "wiki" / "INDEX.md"
    readme.write_text("PERSO inbox", encoding="utf-8")
    index.write_text("PERSO index", encoding="utf-8")

    assert wiki_init.main(["--config", cfgp, "apply", "--skip-sync"]) == 0
    assert readme.read_text(encoding="utf-8") == "PERSO inbox"
    assert index.read_text(encoding="utf-8") == "PERSO index"


def test_apply_refuse_layout_evasion(tmp_path):
    """Un layout qui sort de work_root est refusé (confinement, §2)."""
    from guard import ConfinementError

    cfgp, _ = _make_config(tmp_path, layout={"inbox": "../../evasion"})
    with pytest.raises(ConfinementError):
        wiki_init.main(["--config", cfgp, "apply", "--skip-sync"])


# --- status (lecture seule) ------------------------------------------------
def test_status_affiche_chemins_et_ne_cree_rien(tmp_path, capsys):
    cfgp, work = _make_config(tmp_path)
    assert wiki_init.main(["--config", cfgp, "status"]) == 0
    out = capsys.readouterr().out
    assert "CONFIG_PATH=" in out
    assert "WORK_ROOT=" in out and str(work) in out
    assert "INBOX=" in out and "WIKI=" in out
    # status est en lecture seule : rien n'est créé.
    assert not work.exists()


# --- amorçage de la synchro ------------------------------------------------
def test_apply_amorce_sync_pull_puis_push(tmp_path, monkeypatch):
    """Sans --skip-sync, apply enchaîne pull -> push via le backend."""
    cfgp, _ = _make_config(tmp_path, sync_cfg={"backend": "git", "git": {"repo_root": str(tmp_path)}})
    order = []
    backend = SimpleNamespace(
        warnings=["note"],
        pull=lambda: order.append("pull") or 0,
        push=lambda: order.append("push") or 0,
        validate=lambda: order.append("validate") or 0,
    )
    monkeypatch.setattr(wiki_init.sync, "get_backend", lambda cfg: backend)
    assert wiki_init.main(["--config", cfgp, "apply"]) == 0
    assert order == ["pull", "push"]  # pas de validate à l'amorçage


def test_apply_pull_echec_n_appelle_pas_push(tmp_path, monkeypatch):
    cfgp, _ = _make_config(tmp_path, sync_cfg={"backend": "git", "git": {"repo_root": str(tmp_path)}})
    order = []
    backend = SimpleNamespace(
        warnings=[],
        pull=lambda: order.append("pull") or 2,
        push=lambda: order.append("push") or 0,
        validate=lambda: 0,
    )
    monkeypatch.setattr(wiki_init.sync, "get_backend", lambda cfg: backend)
    assert wiki_init.main(["--config", cfgp, "apply"]) == 2
    assert order == ["pull"]  # push non appelé


def test_apply_noop_sans_cle_sync_reussit(tmp_path):
    """Config sans clé `sync` -> backend noop : amorçage réel réussit (code 0)."""
    cfgp, work = _make_config(tmp_path)
    assert wiki_init.main(["--config", cfgp, "apply"]) == 0
    assert (work / "_inbox").is_dir()


# --- 5.1 : marqueur de version (.mimir/vault.json) ------------------------

def test_apply_pose_marqueur_a_expected_sur_vault_vierge(tmp_path):
    """Sur un vault vierge (aucun raw, aucun wiki) : .mimir/vault.json est posé
    à `EXPECTED_SCHEMA`."""
    cfgp, work = _make_config(tmp_path)
    assert wiki_init.main(["--config", cfgp, "apply", "--skip-sync"]) == 0
    meta = work / ".mimir" / "vault.json"
    assert meta.is_file()
    assert vault_meta.read_vault_schema(_reload_cfg(cfgp)) == EXPECTED_SCHEMA


def test_apply_pose_baseline_1_sur_vault_peuple_sans_marqueur(tmp_path):
    """6.6 : un vault peuplé (raw/ contient des fichiers, pas de .mimir/)
    reçoit la baseline 1, AUCUNE migration n'est exécutée."""
    cfgp, work = _make_config(tmp_path)
    # Crée un raw/ avec du contenu (vault déjà peuplé)
    (work / "raw" / "pdfs").mkdir(parents=True)
    (work / "raw" / "pdfs" / "foo.pdf.txt").write_text("contenu", encoding="utf-8")
    # Crée un article wiki
    (work / "wiki" / "nav").mkdir(parents=True)
    (work / "wiki" / "nav" / "releve.md").write_text(
        "---\ntitle: r\n---\n\n# r\n", encoding="utf-8"
    )
    # Pas de .mimir/vault.json
    assert not (work / ".mimir" / "vault.json").exists()
    assert wiki_init.main(["--config", cfgp, "apply", "--skip-sync"]) == 0
    # Le marqueur est posé à 1 (baseline), pas à EXPECTED_SCHEMA
    meta = work / ".mimir" / "vault.json"
    assert meta.is_file()
    cfg = _reload_cfg(cfgp)
    assert vault_meta.read_vault_schema(cfg) == 1
    # Les articles et le raw sont INTACTS (pas de migration)
    assert (work / "raw" / "pdfs" / "foo.pdf.txt").is_file()
    assert (work / "wiki" / "nav" / "releve.md").is_file()


def test_apply_ne_reecrase_pas_le_marqueur_existant(tmp_path):
    """Si `.mimir/vault.json` existe déjà, apply ne l'écrase pas (jamais)."""
    cfgp, work = _make_config(tmp_path)
    # On pose manuellement un marqueur à 5 (par exemple)
    work.mkdir(parents=True, exist_ok=True)
    (work / ".mimir").mkdir(parents=True, exist_ok=True)
    (work / ".mimir" / "vault.json").write_text(
        json.dumps({"schema": 5, "migrated": "2026-01-01", "history": []}),
        encoding="utf-8",
    )
    assert wiki_init.main(["--config", cfgp, "apply", "--skip-sync"]) == 0
    # Le marqueur est INTACT (schema = 5)
    cfg = _reload_cfg(cfgp)
    assert vault_meta.read_vault_schema(cfg) == 5


# --- 5.2 : status affiche SCHEMA vs EXPECTED ------------------------------

def test_status_affiche_schema_et_pending(tmp_path, capsys):
    """`status` affiche `SCHEMA=<vault>`, `EXPECTED=<code>` et la liste
    des paliers en attente (lecture seule)."""
    cfgp, work = _make_config(tmp_path)
    # Cas 1 : vault vierge (baseline 1 par défaut, EXPECTED=1 → à jour)
    assert wiki_init.main(["--config", cfgp, "status"]) == 0
    out = capsys.readouterr().out
    assert f"SCHEMA=1" in out
    assert f"EXPECTED={EXPECTED_SCHEMA}" in out
    if EXPECTED_SCHEMA == 1:
        assert "à jour" in out


def test_status_affiche_retard_si_vault_en_retard(tmp_path, monkeypatch, capsys):
    """Si on force EXPECTED_SCHEMA > 1, status doit signaler le retard."""
    monkeypatch.setattr(wiki_init, "_expected_schema", lambda: 3)
    cfgp, work = _make_config(tmp_path)
    assert wiki_init.main(["--config", cfgp, "status"]) == 0
    out = capsys.readouterr().out
    # vault.schema = 1 (baseline) ; EXPECTED = 3
    assert "SCHEMA=1" in out
    assert "EXPECTED=3" in out
    assert "retard" in out
    # Pas d'écriture (lecture seule)
    assert not (work / ".mimir").exists()


# --- helpers ---------------------------------------------------------------

def _reload_cfg(cfgp: str):
    """Recharge un `Config` depuis son chemin (post-apply)."""
    import config_loader
    return config_loader.load_config(cfgp)
