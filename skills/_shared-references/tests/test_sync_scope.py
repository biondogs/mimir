"""Tests 6.7 : `.mimir/vault.json` est dans le scope de sync,
`.mimir/snapshots/` est exclu (rclone filters + git scope)."""

from __future__ import annotations

import json
from pathlib import Path

import config_loader
import pytest


# --- config_loader : VAULT_META dans as_dict -------------------------------

def test_config_expose_vaul_meta_dans_as_dict(tmp_path: Path):
    """`Config.as_dict()` expose `VAULT_META=<work_root>/.mimir/vault.json`."""
    base = tmp_path / "vault"
    base.mkdir()
    cfgp = base / "wiki.config.json"
    cfgp.write_text(json.dumps({"work_root": str(base), "sync": {"backend": "rclone"}}), encoding="utf-8")
    cfg = config_loader.load_config(cfgp)
    assert "VAULT_META" in cfg.as_dict()
    assert cfg.as_dict()["VAULT_META"] == str((base / ".mimir" / "vault.json").resolve())


def test_config_vault_meta_chemin_absolu(tmp_path: Path):
    base = tmp_path / "vault"
    base.mkdir()
    cfgp = base / "wiki.config.json"
    cfgp.write_text(json.dumps({"work_root": str(base), "sync": {"backend": "rclone"}}), encoding="utf-8")
    cfg = config_loader.load_config(cfgp)
    assert cfg.VAULT_META.is_absolute()
    assert cfg.VAULT_META == (base.resolve() / ".mimir" / "vault.json")


def test_config_expected_schema_est_un_entier_positif():
    """`EXPECTED_SCHEMA` est un entier ≥ 1 (cf. spec `vault-migration`)."""
    assert isinstance(config_loader.EXPECTED_SCHEMA, int)
    assert config_loader.EXPECTED_SCHEMA >= 1


def test_config_vault_meta_rel_constant():
    """`VAULT_META_REL` est `.mimir/vault.json` (constante du socle, pas du layout)."""
    assert config_loader.VAULT_META_REL == Path(".mimir") / "vault.json"


# --- rclone filters : `.mimir/snapshots/` exclu -----------------------------

def test_filters_txt_exclut_mimir_snapshots():
    """Le template `filters.txt` (rclone) exclut `.mimir/snapshots/**`."""
    from pathlib import Path
    filters = Path(__file__).resolve().parents[3] / "filters.txt"
    content = filters.read_text(encoding="utf-8")
    assert "- .mimir/snapshots/**" in content


def test_filters_txt_inclut_vault_json_avec_le_whitelist_final():
    """Le `+ **` final whitelist tout le reste, donc `.mimir/vault.json` est
    implicitement INCLUS (seul `.mimir/snapshots/` est exclu)."""
    from pathlib import Path
    filters = Path(__file__).resolve().parents[3] / "filters.txt"
    content = filters.read_text(encoding="utf-8")
    # L'exclusion ciblée
    assert "- .mimir/snapshots/**" in content
    # Whitelist finale (couvre tout ce qui n'est pas exclu ci-dessus)
    assert "+ **" in content


# --- git scope : `.mimir/` inclus par défaut -------------------------------

def test_git_scope_par_defaut_inclut_mimir():
    """Le scope par défaut du backend git inclut `.mimir/` (pour que
    `vault.json` voyage avec le contenu)."""
    from sync.git import _DEFAULT_SCOPE
    assert ".mimir" in _DEFAULT_SCOPE


# --- git scope custom : opt-in exclure snapshots via filtre manuel ---------

def test_git_scope_personnalisable(tmp_path: Path):
    """Un config peut override le scope pour exclure `.mimir/snapshots/`
    (le scope git est un filtre grossier, c'est surtout le filtre rclone
    qui fait le travail fin)."""
    from types import SimpleNamespace
    from sync.git import GitBackend

    class FakeRunner:
        def __call__(self, argv, **kwargs):
            from types import SimpleNamespace as NS
            return NS(returncode=0, stdout="", stderr="")

    cfg = SimpleNamespace(
        work_root=tmp_path,
        sync={
            "backend": "git",
            "git": {
                "repo_root": str(tmp_path),
                "scope": ["_inbox", "raw", "wiki", "reading-grids"],  # pas de .mimir
            },
        },
        backend="git",
    )
    backend = GitBackend(cfg, runner=FakeRunner())
    assert ".mimir" not in backend.scope  # override respecté
