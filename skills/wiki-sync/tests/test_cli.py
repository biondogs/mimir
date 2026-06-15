"""Tests du CLI wiki-sync : dispatch des sous-commandes, ordre du cycle, verrou tenu."""

import json
from types import SimpleNamespace

import pytest

import wiki_sync
from sync import locking as sync_lock


@pytest.fixture(autouse=True)
def _lock_in_tmp(tmp_path, monkeypatch):
    """Verrou hors ~/.cache réel et hors work_root."""
    monkeypatch.setattr(sync_lock, "_DEFAULT_LOCK", tmp_path / "lock" / "wiki-sync.lock")


@pytest.fixture
def config_noop(tmp_path):
    """Config sans clé `sync` -> backend noop (toutes les ops réussissent)."""
    work = tmp_path / "work"
    work.mkdir()
    cfgp = tmp_path / "wiki.config.json"
    cfgp.write_text(json.dumps({"work_root": str(work)}), encoding="utf-8")
    return str(cfgp)


def test_sync_noop_reussit(config_noop, capsys):
    assert wiki_sync.main(["--config", config_noop, "sync"]) == 0
    out = capsys.readouterr().out
    assert "pull" in out and "push" in out and "validate" in out


@pytest.mark.parametrize("cmd", ["lock", "pull", "push", "validate"])
def test_sous_commandes_noop(config_noop, cmd):
    assert wiki_sync.main(["--config", config_noop, cmd]) == 0


def test_cycle_sync_ordre(config_noop, monkeypatch):
    """`sync` enchaîne pull -> push -> validate dans cet ordre."""
    order = []
    backend = SimpleNamespace(
        warnings=[],
        pull=lambda: order.append("pull") or 0,
        push=lambda: order.append("push") or 0,
        validate=lambda: order.append("validate") or 0,
    )
    monkeypatch.setattr(wiki_sync.sync, "get_backend", lambda cfg, runner=None: backend)
    assert wiki_sync.main(["--config", config_noop, "sync"]) == 0
    assert order == ["pull", "push", "validate"]


def test_sync_dry_run_saute_push(config_noop, monkeypatch):
    order = []
    backend = SimpleNamespace(
        warnings=[],
        pull=lambda: order.append("pull") or 0,
        push=lambda: order.append("push") or 0,
        validate=lambda: order.append("validate") or 0,
    )
    monkeypatch.setattr(wiki_sync.sync, "get_backend", lambda cfg, runner=None: backend)
    assert wiki_sync.main(["--config", config_noop, "sync", "--dry-run"]) == 0
    assert order == ["pull", "validate"]  # push sauté


def test_pull_echec_stoppe_le_cycle(config_noop, monkeypatch):
    order = []
    backend = SimpleNamespace(
        warnings=[],
        pull=lambda: order.append("pull") or 3,
        push=lambda: order.append("push") or 0,
        validate=lambda: order.append("validate") or 0,
    )
    monkeypatch.setattr(wiki_sync.sync, "get_backend", lambda cfg, runner=None: backend)
    assert wiki_sync.main(["--config", config_noop, "sync"]) == 3
    assert order == ["pull"]  # ni push ni validate


def test_verrou_tenu_skip_propre(config_noop, capsys):
    """Si le verrou est déjà tenu, le CLI sort proprement en code 0."""
    held = sync_lock.acquire()
    try:
        assert wiki_sync.main(["--config", config_noop, "sync"]) == 0
        assert "🔒" in capsys.readouterr().out
    finally:
        held.release()


# ---------- Garde-fou pré-push (opt-in : sync.presync_gate) ----------------


def _config_with_gate(config_noop, *, gate: bool) -> str:
    """Réécrit `config_noop` avec `sync.presync_gate = gate` (+ backend). Renvoie le chemin."""
    from pathlib import Path

    data = json.loads(Path(config_noop).read_text(encoding="utf-8"))
    data["sync"] = {"backend": "rclone", "presync_gate": gate}
    Path(config_noop).write_text(json.dumps(data), encoding="utf-8")
    return config_noop


class _FakeResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_presync_gate_bloque_push_si_anomalie(config_noop, monkeypatch):
    """presync_gate=true + audit en anomalie → push avorté, runner enregistré, aucun push réseau."""
    _config_with_gate(config_noop, gate=True)

    calls: list[list[str]] = []

    def fake_runner(argv, **kwargs):
        calls.append(list(argv))
        return _FakeResult(returncode=1, stdout="KO (1 anomalie)\n")

    pushed: list[bool] = []
    backend = SimpleNamespace(
        warnings=[],
        pull=lambda: 0,
        push=lambda: pushed.append(True) or 0,
        validate=lambda: 0,
    )
    monkeypatch.setattr(wiki_sync, "subprocess", SimpleNamespace(run=fake_runner))
    monkeypatch.setattr(wiki_sync.sync, "get_backend", lambda cfg, runner=None: backend)

    assert wiki_sync.main(["--config", config_noop, "push"]) == 1
    assert pushed == []                         # push jamais appelé → état préservé
    assert len(calls) == 1                      # audit invoqué une fois via le runner
    assert calls[0][-1] == "audit"              # sous-commande wiki_index correcte


def test_presync_gate_laisse_passer_si_vault_propre(config_noop, monkeypatch):
    """presync_gate=true + audit OK → push normal."""
    _config_with_gate(config_noop, gate=True)

    def fake_runner(argv, **kwargs):
        return _FakeResult(returncode=0, stdout="OK\n")

    pushed: list[bool] = []
    backend = SimpleNamespace(
        warnings=[],
        pull=lambda: 0,
        push=lambda: pushed.append(True) or 0,
        validate=lambda: 0,
    )
    monkeypatch.setattr(wiki_sync, "subprocess", SimpleNamespace(run=fake_runner))
    monkeypatch.setattr(wiki_sync.sync, "get_backend", lambda cfg, runner=None: backend)

    assert wiki_sync.main(["--config", config_noop, "push"]) == 0
    assert pushed == [True]                     # push a bien eu lieu


def test_presync_gate_bloque_sync_si_anomalie(config_noop, monkeypatch):
    """Le cycle `sync` partage le même garde-fou (proposal : `push` ET `sync`)."""
    _config_with_gate(config_noop, gate=True)

    def fake_runner(argv, **kwargs):
        return _FakeResult(returncode=1, stdout="KO\n")

    pushed: list[bool] = []
    order: list[str] = []
    backend = SimpleNamespace(
        warnings=[],
        pull=lambda: (order.append("pull"), 0)[1],
        push=lambda: (pushed.append(True), order.append("push"), 0)[2],
        validate=lambda: (order.append("validate"), 0)[1],
    )
    monkeypatch.setattr(wiki_sync, "subprocess", SimpleNamespace(run=fake_runner))
    monkeypatch.setattr(wiki_sync.sync, "get_backend", lambda cfg, runner=None: backend)

    assert wiki_sync.main(["--config", config_noop, "sync"]) == 1
    assert order == ["pull"]                    # pull OK, push et validate avortés
    assert pushed == []


def test_presync_gate_absent_non_regression(config_noop, monkeypatch):
    """`presync_gate` absent → push normal, audit jamais invoqué (3.4 / non-régression)."""
    runner_calls: list[list[str]] = []

    def fake_runner(argv, **kwargs):
        runner_calls.append(list(argv))
        return _FakeResult(returncode=0)

    pushed: list[bool] = []
    backend = SimpleNamespace(
        warnings=[],
        pull=lambda: 0,
        push=lambda: pushed.append(True) or 0,
        validate=lambda: 0,
    )
    monkeypatch.setattr(wiki_sync, "subprocess", SimpleNamespace(run=fake_runner))
    monkeypatch.setattr(wiki_sync.sync, "get_backend", lambda cfg, runner=None: backend)

    assert wiki_sync.main(["--config", config_noop, "push"]) == 0
    assert pushed == [True]
    assert runner_calls == []                    # gate désactivé, audit non lancé


def test_presync_gate_false_non_regression(config_noop, monkeypatch):
    """`presync_gate=false` explicite → même non-régression que clé absente."""
    _config_with_gate(config_noop, gate=False)

    runner_calls: list[list[str]] = []

    def fake_runner(argv, **kwargs):
        runner_calls.append(list(argv))
        return _FakeResult(returncode=0)

    pushed: list[bool] = []
    backend = SimpleNamespace(
        warnings=[],
        pull=lambda: 0,
        push=lambda: pushed.append(True) or 0,
        validate=lambda: 0,
    )
    monkeypatch.setattr(wiki_sync, "subprocess", SimpleNamespace(run=fake_runner))
    monkeypatch.setattr(wiki_sync.sync, "get_backend", lambda cfg, runner=None: backend)

    assert wiki_sync.main(["--config", config_noop, "push"]) == 0
    assert pushed == [True]
    assert runner_calls == []
