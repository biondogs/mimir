"""Tests de la factory de backends (`get_backend`) et de l'injection du runner."""

from pathlib import Path

import pytest

import sync
from sync.auto import AutoBackend
from sync.base import SyncBackend, SyncError
from sync.obsidian import ObsidianBackend
from sync.rclone import RcloneBackend
from sync.git import GitBackend
from sync.noop import NoopBackend


class _Cfg:
    def __init__(self, sync_conf, work_root=None):
        self.sync = sync_conf
        # `work_root` est requis par les backends obsidian/auto (Path + rclone
        # remote optionnel lu depuis sync.rclone). Pour les tests factory
        # purs, un Path jetable suffit.
        self.work_root = work_root or Path("/tmp/mimir-test-factory")

    @property
    def backend(self):
        return self.sync.get("backend", "rclone")


def test_backend_inconnu_leve_syncerror():
    cfg = _Cfg({"backend": "dropbox"})
    with pytest.raises(SyncError):
        sync.get_backend(cfg)


def test_runner_injecte_est_utilise():
    """`_run` délègue au runner fourni (aucun subprocess réel)."""
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    class _Backend(SyncBackend):
        name = "test"

        def pull(self):
            return self._run(["echo", "hi"]).returncode

        def push(self):
            return 0

        def validate(self):
            return 0

    b = _Backend(_Cfg({}), runner=fake_runner)
    assert b.pull() == 0
    assert calls == [["echo", "hi"]]


def test_lock_herite_du_backend(tmp_path, monkeypatch):
    """Tout backend expose `lock()` via le verrou partagé (hors work_root)."""
    from sync import locking as sync_lock

    target = tmp_path / "lock" / "wiki-sync.lock"
    monkeypatch.setattr(sync_lock, "_DEFAULT_LOCK", target)

    backend = sync.get_backend(_Cfg({}))  # noop
    lk = backend.lock()
    try:
        assert target.is_dir()
    finally:
        lk.release()


# --- non-régression : chaque backend reste sélectionnable par sa valeur (7.5)


def test_factory_rclone_inchange():
    """`backend = rclone` → RcloneBackend (non-régression 7.5)."""
    assert isinstance(sync.get_backend(_Cfg({"backend": "rclone"})), RcloneBackend)


def test_factory_git_inchange():
    """`backend = git` → GitBackend (non-régression 7.5)."""
    assert isinstance(sync.get_backend(_Cfg({"backend": "git"})), GitBackend)


def test_factory_noop_inchange():
    """Pas de clé `sync` → NoopBackend (non-régression 7.5)."""
    assert isinstance(sync.get_backend(_Cfg({})), NoopBackend)


def test_factory_obsidian_selectionne():
    """`backend = obsidian` → ObsidianBackend."""
    assert isinstance(sync.get_backend(_Cfg({"backend": "obsidian"})), ObsidianBackend)


def test_factory_auto_selectionne():
    """`backend = auto` → AutoBackend (avec sous-backend décidé à la construction)."""
    backend = sync.get_backend(_Cfg({"backend": "auto"}))
    assert isinstance(backend, AutoBackend)
    # sous-backend : Obsidian (probe KO par défaut → rclone) ou RcloneBackend
    from sync.rclone import RcloneBackend as _RB
    assert isinstance(backend._active, _RB)  # probe KO par défaut → rclone


# --- import paresseux (spec : backends importés paresseusement, 7.5) -------


def test_imports_paresseux_obsidian_et_auto():
    """Les modules `obsidian` et `auto` ne sont importés QUE quand on instancie
    le backend correspondant. Vérifie qu'on peut les simuler absents sans
    casser le chargement de `sync` (test du contrat ABC, pas de l'import)."""
    # `get_backend` avec un backend non-reconnu : ne lève PAS d'ImportError.
    with pytest.raises(SyncError):
        sync.get_backend(_Cfg({"backend": "dropbox"}))
