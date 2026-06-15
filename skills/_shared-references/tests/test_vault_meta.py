"""Tests de `vault_meta` — version de structure persistée (capability `vault-migration`).

Couvre :
- 6.1 `read_vault_schema` : absent → 1 ; présent → valeur ; corrompu → 1.
- 6.2 `bump` atomique : une écriture interrompue laisse l'ancienne version intacte.
- (réutilisé) Idempotence, confinement, format du journal `history`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import vault_meta


# --- 6.1 : read_vault_schema : absent / présent / corrompu -------------------

class _Cfg:
    """Mock minimal de `Config` (n'a besoin que de `work_root` et `VAULT_META`)."""

    def __init__(self, work_root: Path):
        self.work_root = work_root
        self.VAULT_META = work_root / ".mimir" / "vault.json"


def test_read_vault_schema_absent_vaut_baseline(tmp_path: Path):
    """`.mimir/vault.json` absent → 1 (baseline), pas d'écriture."""
    cfg = _Cfg(tmp_path)
    assert vault_meta.read_vault_schema(cfg) == vault_meta.BASELINE_SCHEMA
    # Aucun fichier créé.
    assert not (tmp_path / ".mimir").exists()


def test_read_vault_schema_present_renvoie_valeur(tmp_path: Path):
    cfg = _Cfg(tmp_path)
    cfg.VAULT_META.parent.mkdir(parents=True)
    cfg.VAULT_META.write_text(
        json.dumps({"schema": 3, "migrated": "2026-06-15", "history": []}),
        encoding="utf-8",
    )
    assert vault_meta.read_vault_schema(cfg) == 3


def test_read_vault_schema_corrompu_retombe_baseline(tmp_path: Path):
    """JSON invalide, forme inattendue, schema non-entier → 1, pas d'erreur."""
    cfg = _Cfg(tmp_path)
    cfg.VAULT_META.parent.mkdir(parents=True)
    # JSON invalide
    cfg.VAULT_META.write_text("{ pas du json", encoding="utf-8")
    assert vault_meta.read_vault_schema(cfg) == vault_meta.BASELINE_SCHEMA
    # Forme inattendue
    cfg.VAULT_META.write_text('"juste une chaîne"', encoding="utf-8")
    assert vault_meta.read_vault_schema(cfg) == vault_meta.BASELINE_SCHEMA
    # schema non-entier
    cfg.VAULT_META.write_text('{"schema": "trois"}', encoding="utf-8")
    assert vault_meta.read_vault_schema(cfg) == vault_meta.BASELINE_SCHEMA
    # schema = 0 (invalide : < 1)
    cfg.VAULT_META.write_text('{"schema": 0}', encoding="utf-8")
    assert vault_meta.read_vault_schema(cfg) == vault_meta.BASELINE_SCHEMA
    # schema négatif
    cfg.VAULT_META.write_text('{"schema": -1}', encoding="utf-8")
    assert vault_meta.read_vault_schema(cfg) == vault_meta.BASELINE_SCHEMA
    # schema = bool (sous-type de int en Python : on l'exclut)
    cfg.VAULT_META.write_text('{"schema": true}', encoding="utf-8")
    assert vault_meta.read_vault_schema(cfg) == vault_meta.BASELINE_SCHEMA


# --- 6.2 : bump atomique ----------------------------------------------------

def test_bump_vault_schema_ecrit_atomiquement(tmp_path: Path):
    """bump écrit `.mimir/vault.json` (créé si besoin) avec schema + journal."""
    cfg = _Cfg(tmp_path)
    p = vault_meta.bump_vault_schema(cfg, 3, at="2026-06-15")
    assert p == cfg.VAULT_META
    assert p.is_file()
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["schema"] == 3
    assert doc["migrated"] == "2026-06-15"
    assert doc["history"] == [{"to": 3, "at": "2026-06-15"}]


def test_bump_atomic_interruption_laisse_ancienne_version_intacte(tmp_path: Path):
    """Simulation : un crash en plein écriture (tmp laissé derrière) n'écrase
    pas la version précédente. Le `os.replace` (dans iohelpers.write_json) est
    atomique : tant que le replace n'a pas eu lieu, l'ancien fichier persiste."""
    cfg = _Cfg(tmp_path)
    cfg.VAULT_META.parent.mkdir(parents=True)
    cfg.VAULT_META.write_text(
        json.dumps({"schema": 2, "migrated": "2026-01-01", "history": [{"to": 2, "at": "2026-01-01"}]}),
        encoding="utf-8",
    )
    # On simule une interruption en **ne faisant pas** le replace final : on
    # patche `iohelpers.write_json` pour qu'il lève APRÈS avoir écrit le tmp.
    import iohelpers

    original_atomic = iohelpers.atomic_write_text

    def crash_after_tmp(path, text, *, work_root):
        # écrit dans le tmp comme le vrai, mais lève avant le replace
        import os
        import tempfile
        target = path
        os.fdopen  # noqa: F401 (import placeholder)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=target.suffix)
        with open(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        # Laisse le tmp derrière, lève.
        raise RuntimeError("simulated crash")

    import iohelpers as _ih
    orig = _ih.atomic_write_text
    _ih.atomic_write_text = crash_after_tmp
    try:
        with pytest.raises(RuntimeError):
            vault_meta.bump_vault_schema(cfg, 3)
    finally:
        _ih.atomic_write_text = orig

    # L'ancien fichier est INTACT (schema = 2, journal inchangé).
    doc = json.loads(cfg.VAULT_META.read_text(encoding="utf-8"))
    assert doc["schema"] == 2
    assert doc["history"] == [{"to": 2, "at": "2026-01-01"}]


def test_bump_preserve_historique_et_ajoute_entree(tmp_path: Path):
    """Un bump sur un fichier déjà bumpé **préserve** l'historique."""
    cfg = _Cfg(tmp_path)
    cfg.VAULT_META.parent.mkdir(parents=True)
    cfg.VAULT_META.write_text(
        json.dumps({
            "schema": 2,
            "migrated": "2026-01-01",
            "history": [{"to": 1, "at": "2025-12-31"}, {"to": 2, "at": "2026-01-01"}],
        }),
        encoding="utf-8",
    )
    vault_meta.bump_vault_schema(cfg, 3, at="2026-06-15")
    doc = json.loads(cfg.VAULT_META.read_text(encoding="utf-8"))
    assert doc["schema"] == 3
    assert len(doc["history"]) == 3
    assert doc["history"][-1] == {"to": 3, "at": "2026-06-15"}


def test_bump_date_par_defaut_aujourd_hui(tmp_path: Path):
    """Si `at` n'est pas fourni, on prend `date.today().isoformat()`."""
    cfg = _Cfg(tmp_path)
    vault_meta.bump_vault_schema(cfg, 1)
    doc = json.loads(cfg.VAULT_META.read_text(encoding="utf-8"))
    assert doc["migrated"] == date.today().isoformat()


def test_bump_refuse_version_invalide(tmp_path: Path):
    """`to` doit être un entier ≥ 1 (sinon `ValueError`)."""
    cfg = _Cfg(tmp_path)
    for bad in (0, -1, "1", True, False, None, 1.5):
        with pytest.raises(ValueError):
            vault_meta.bump_vault_schema(cfg, bad)


def test_bump_refuse_chemin_hors_work_root(tmp_path: Path):
    """`bump_vault_schema` confine : un `work_root` minimal refuse un path
    qui en sort (sanity check de la garde, pas une régression)."""
    cfg = _Cfg(tmp_path)
    # On force un VAULT_META hors work_root (manip défensive) :
    cfg.VAULT_META = tmp_path.parent / "evil" / "vault.json"
    from guard import ConfinementError
    with pytest.raises(ConfinementError):
        vault_meta.bump_vault_schema(cfg, 1)


def test_bump_idempotent_au_meme_niveau(tmp_path: Path):
    """Bumper deux fois au même niveau = deux entrées d'historique (log), mais
    même `schema`. On ne déduplique pas : c'est un journal d'événements."""
    cfg = _Cfg(tmp_path)
    vault_meta.bump_vault_schema(cfg, 2, at="2026-06-15")
    vault_meta.bump_vault_schema(cfg, 2, at="2026-06-16")
    doc = json.loads(cfg.VAULT_META.read_text(encoding="utf-8"))
    assert doc["schema"] == 2
    assert len(doc["history"]) == 2
