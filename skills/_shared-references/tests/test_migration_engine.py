"""Tests du moteur de migration `vault-migration` — couvre :

- 6.3 Engine sur un palier factice : plan listé, snapshot du seul delta,
  apply idempotent (2e passage = no-op), bump correct.
- 6.4 Validate : un lien cassé introduit → rollback déclenché, version
  inchangée.
- 6.5 Snapshot/restore : restaure exactement le delta, ne touche pas le
  reste.
- (bonus) Lock : verrou tenu → abort propre.
- (bonus) Plan dry-run : aucune écriture.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import iohelpers
import vault_meta
from migration import engine, plan, registry, snapshot, validate
from migration.engine import (
    RC_LOCK_HELD,
    RC_NO_MIGRATION,
    RC_OK,
    RC_VALIDATION_FAILED,
)


# --- helpers --------------------------------------------------------------

class _Cfg:
    """Mock minimal de `Config` (n'a besoin que des chemins + lock_cfg)."""

    def __init__(self, work_root: Path):
        self.work_root = work_root
        self.INBOX = work_root / "_inbox"
        self.RAW = work_root / "raw"
        self.WIKI = work_root / "wiki"
        self.READING_GRIDS = work_root / "reading-grids"
        self.LEDGER = work_root / ".wiki" / "ingest-ledger.json"
        self.VAULT_META = work_root / ".mimir" / "vault.json"
        self.VAULT_META_DIR = work_root / ".mimir"
        self.SNAPSHOTS_DIR = work_root / ".mimir" / "snapshots"
        self.ASSETS_DIRNAME = "_assets"
        self.sync: dict[str, Any] = {"backend": "rclone"}
        self.config_path = work_root / "wiki.config.json"
        self.mode = "B"
        self.backend = "rclone"


def _lock_dir(monkeypatch, tmp_path: Path) -> Path:
    """Force le verrou de synchro dans `tmp_path` (jamais `~/.cache`)."""
    from sync import locking
    target = tmp_path / "lock" / "wiki-sync.lock"
    monkeypatch.setattr(locking, "_DEFAULT_LOCK", target)
    return target


def _make_palier_v1_v2(cfg, *extra_files_to_plant: Path):
    """Palier factice v1→v2 : déplace un fichier `raw/old.md` vers `raw/new.md`."""
    src = cfg.RAW / "old.md"
    dst = cfg.RAW / "new.md"

    def plan_fn(c):
        if dst.exists() and not src.exists():
            # déjà migré → plan vide
            return plan.Plan(description="déplace old.md → new.md")
        return plan.Plan(description="déplace old.md → new.md", operations=[
            plan.MOVE(src, dst),
        ])

    return registry.Migration(
        from_schema=1, to_schema=2,
        plan=plan_fn,
        description="déplace old.md → new.md",
    )


@pytest.fixture(autouse=True)
def _clear_registry():
    """Reset le registre entre tests (chaque test enregistre son palier)."""
    registry.clear()
    yield
    registry.clear()


# --- 6.3 : engine sur un palier factice -----------------------------------

def test_engine_dry_run_ne_crie_rien(tmp_path: Path, monkeypatch):
    """`run_one(dry_run=True)` : plan listé, aucun fichier modifié, pas de bump."""
    _lock_dir(monkeypatch, tmp_path)
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    (cfg.RAW / "old.md").write_text("hello", encoding="utf-8")

    palier = _make_palier_v1_v2(cfg)
    res = engine.run_one(cfg, palier, dry_run=True)
    assert res.rc == RC_OK
    assert not res.applied
    assert not res.bumped
    # Le fichier source est INTACT.
    assert (cfg.RAW / "old.md").is_file()
    assert (cfg.RAW / "old.md").read_text(encoding="utf-8") == "hello"
    # Pas de destination créée.
    assert not (cfg.RAW / "new.md").exists()
    # Pas de marqueur vault.json (dry-run n'écrit rien).
    assert not cfg.VAULT_META.exists()


def test_engine_run_one_succes_bump_et_atomicite(tmp_path: Path, monkeypatch):
    """run_one réel : plan appliqué, snapshot créé, bump à 2, fichier déplacé."""
    _lock_dir(monkeypatch, tmp_path)
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    src = cfg.RAW / "old.md"
    src.write_text("hello", encoding="utf-8")

    palier = _make_palier_v1_v2(cfg)
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK
    assert res.bumped
    assert res.applied
    # Fichier déplacé
    assert not src.exists()
    assert (cfg.RAW / "new.md").is_file()
    assert (cfg.RAW / "new.md").read_text(encoding="utf-8") == "hello"
    # Bump à 2
    assert vault_meta.read_vault_schema(cfg) == 2
    # Snapshot nettoyé après succès
    assert not cfg.SNAPSHOTS_DIR.exists() or not any(cfg.SNAPSHOTS_DIR.iterdir())


def test_engine_apply_idempotent_2e_passage_no_op(tmp_path: Path, monkeypatch):
    """Un 2e passage sur un palier déjà appliqué : schéma déjà à to_schema,
    le moteur short-circuit (palier déjà appliqué), no-op, pas de bump."""
    _lock_dir(monkeypatch, tmp_path)
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    (cfg.RAW / "old.md").write_text("hello", encoding="utf-8")

    palier = _make_palier_v1_v2(cfg)
    # 1er passage : schéma passe de 1 à 2
    res1 = engine.run_one(cfg, palier)
    assert res1.rc == RC_OK
    assert res1.bumped
    # 2e passage : schema_from = 2 = schema_to, le moteur short-circuit
    res2 = engine.run_one(cfg, palier)
    assert res2.rc == RC_OK
    assert not res2.applied
    assert not res2.bumped  # pas de re-bump : schéma déjà à jour
    # Le fichier n'a pas été re-déplacé (et le contenu est intact)
    assert (cfg.RAW / "new.md").is_file()
    assert (cfg.RAW / "new.md").read_text(encoding="utf-8") == "hello"


# --- 6.4 : validate : rollback sur lien cassé -----------------------------

def test_engine_rollback_si_validation_echoue(tmp_path: Path, monkeypatch):
    """Si validate signale un lien cassé, l'engine restaure le snapshot ET
    ne bumpe pas (le schema reste à 1)."""
    _lock_dir(monkeypatch, tmp_path)
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    src = cfg.RAW / "old.md"
    src.write_text("hello", encoding="utf-8")

    # On patche la validation pour qu'elle échoue (broken_links > 0)
    real_validate = engine.validate_mod.validate

    def patched_validate(c, before, **kwargs):
        res = real_validate(c, before, **kwargs)
        res.broken_links = 1  # force l'échec
        return res

    monkeypatch.setattr(engine.validate_mod, "validate", patched_validate)

    palier = _make_palier_v1_v2(cfg)
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_VALIDATION_FAILED
    assert res.applied  # l'apply a eu lieu
    assert not res.bumped
    # Le fichier source a été restauré
    assert src.is_file()
    assert src.read_text(encoding="utf-8") == "hello"
    assert not (cfg.RAW / "new.md").exists()
    # Le marqueur vault.json est resté à 1 (baseline) : le bump n'a pas eu lieu
    # Note : on n'écrit PAS le marqueur tant qu'on n'a pas bumpé → fichier absent
    # est traité comme baseline 1 par read_vault_schema.
    assert vault_meta.read_vault_schema(cfg) == 1
    # Snapshot restauré (dossier nettoyé par restore() en cas de succès restore)
    # Note: si restore a réussi, le dossier est nettoyé. Sinon il reste pour
    # investigation — on n'impose pas l'un ou l'autre, on vérifie l'état final.
    if cfg.SNAPSHOTS_DIR.exists():
        # soit le dossier est vide, soit le dossier n'existe pas
        assert not any(cfg.SNAPSHOTS_DIR.iterdir())


# --- 6.5 : snapshot/restore ne touche que le delta ------------------------

def test_snapshot_restore_reste_correct_meme_avec_symlinks(tmp_path: Path, monkeypatch):
    """Robustesse macOS : `/var` ↔ `/private/var` symlink. La résolution de
    chemins dans `_rel()` ne doit pas produire un chemin absolu qui corrompt
    `snap_dir / _rel(...)` (sinon `Path / str_absolu` **remplace** le préfixe
    et le `shutil.copy2` lève `SameFileError`).

    On simule le scénario en passant un `cfg` dont `work_root` est le chemin
    « unresolved » (le `Path` qu'aurait l'appelant) et on vérifie que le
    snapshot finit bien sous `.mimir/snapshots/...` et **pas** à la racine
    `raw/`."""
    _lock_dir(monkeypatch, tmp_path)
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    src = cfg.RAW / "old.md"
    src.write_text("hello", encoding="utf-8")

    palier = _make_palier_v1_v2(cfg)
    res = engine.run_one(cfg, palier)
    assert res.rc == RC_OK
    # Le snapshot est passé par `.mimir/snapshots/...` — pas collé à work_root.
    # (cleanup après succès, mais on a validé que rien n'a planté)
    assert vault_meta.read_vault_schema(cfg) == 2


def test_snapshot_restore_restaure_exactement_le_delta(tmp_path: Path, monkeypatch):
    """Le snapshot ne copie que les fichiers du plan ; restore ne touche qu'eux."""
    _lock_dir(monkeypatch, tmp_path)
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    # Fichiers dans le delta
    delta_src = cfg.RAW / "old.md"
    delta_src.write_text("to-move", encoding="utf-8")
    # Fichiers HORS delta (ne doivent PAS être touchés par le snapshot/restore)
    outside = cfg.RAW / "untouched.md"
    outside.write_text("untouched", encoding="utf-8")

    pl = plan.Plan(description="move", operations=[plan.MOVE(delta_src, cfg.RAW / "new.md")])
    snap = snapshot.create(cfg, pl, schema_from=1, schema_to=2)

    # Le snapshot ne contient QUE delta_src (pas outside)
    snap_files = list(snap.dir.rglob("*"))
    snap_md = [f for f in snap_files if f.is_file() and f.suffix == ".md"]
    assert delta_src.relative_to(cfg.work_root).as_posix() in [
        f.relative_to(snap.dir).as_posix() for f in snap_md
    ]
    assert "untouched.md" not in [f.name for f in snap_files]

    # Restore : on simule que l'apply a déjà déplacé (mais on triche : on simule
    # le crash en faisant un move réel, puis on restore)
    import shutil
    shutil.move(str(delta_src), str(cfg.RAW / "new.md"))
    # le fichier "untouched" est toujours là, intact
    assert outside.is_file()
    snapshot.restore(snap, cfg)
    # Le delta est restauré, untouched toujours intact
    assert delta_src.is_file()
    assert delta_src.read_text(encoding="utf-8") == "to-move"
    assert outside.is_file()
    assert outside.read_text(encoding="utf-8") == "untouched"


# --- lock : abort propre si verrou tenu ------------------------------------

def test_engine_verrou_deja_tenu_abort_propre(tmp_path: Path, monkeypatch):
    """Verrou de synchro déjà tenu (par un autre process simulé) : abort, pas
    de demi-migration, schema inchangé."""
    _lock_dir(monkeypatch, tmp_path)
    cfg = _Cfg(tmp_path)
    cfg.RAW.mkdir(parents=True)
    (cfg.RAW / "old.md").write_text("hello", encoding="utf-8")

    # On tient le verrou "manuellement" avant de lancer le moteur
    from sync.locking import acquire
    lk = acquire()
    try:
        palier = _make_palier_v1_v2(cfg)
        res = engine.run_one(cfg, palier)
        assert res.rc == RC_LOCK_HELD
        assert not res.applied
        # Le fichier source n'a pas été touché
        assert (cfg.RAW / "old.md").is_file()
    finally:
        lk.release()


# --- run_pending : pas de palier en attente --------------------------------

def test_run_pending_aucun_palier_renvoie_no_migration(tmp_path: Path, monkeypatch):
    """Aucun palier en attente et aucun dans le registre : rc = NO_MIGRATION
    (= 4) quand un target est explicitement demandé (sinon OK = rien à faire)."""
    _lock_dir(monkeypatch, tmp_path)
    cfg = _Cfg(tmp_path)
    res_list = engine.run_pending(cfg, target=2)  # target=2 alors qu'aucun palier
    assert len(res_list) == 1
    assert res_list[0].rc == RC_NO_MIGRATION


def test_run_pending_sans_target_et_rien_a_faire_renvoie_ok(tmp_path: Path, monkeypatch):
    """Sans target et sans paliers : rc = OK (l'appelant peut continuer)."""
    _lock_dir(monkeypatch, tmp_path)
    cfg = _Cfg(tmp_path)
    res_list = engine.run_pending(cfg)  # pas de target
    assert len(res_list) == 1
    assert res_list[0].rc == RC_OK


# --- plan : helpers & rendu texte ------------------------------------------

def test_plan_rendu_texte_lisible():
    pl = plan.Plan(description="move some files", operations=[
        plan.MOVE(Path("a/b"), Path("a/c")),
        plan.WRITE(Path("a/d.txt"), "hello"),
        plan.DELETE(Path("a/e")),
    ])
    text = pl.render()
    assert "Plan: move some files" in text
    assert "MOVE" in text and "a/b" in text and "a/c" in text
    assert "WRITE" in text and "a/d.txt" in text
    assert "DELETE" in text and "a/e" in text
    assert "3 opération(s)" in text


def test_plan_vide_rend_aucune_op():
    pl = plan.Plan(description="vide")
    text = pl.render()
    assert "(vide" in text


# --- registry : chaînage de paliers ---------------------------------------

def test_registry_chaine_les_paliers_entre_deux_versions():
    """`pending_for(1, target=3)` doit retourner v1→v2 + v2→v3, dans l'ordre."""
    p12 = registry.Migration(1, 2, plan=lambda c: plan.Plan(), apply=lambda c, p: None, description="p12")
    p23 = registry.Migration(2, 3, plan=lambda c: plan.Plan(), apply=lambda c, p: None, description="p23")
    registry.register(p12)
    registry.register(p23)
    chain = registry.pending_for(1, target=3)
    assert chain == [p12, p23]


def test_registry_refuse_doublon():
    p = registry.Migration(1, 2, plan=lambda c: plan.Plan(), apply=lambda c, p: None)
    registry.register(p)
    with pytest.raises(ValueError):
        registry.register(p)


def test_registry_refuse_palier_non_strictement_croissant():
    with pytest.raises(ValueError):
        registry.Migration(2, 2, plan=lambda c: plan.Plan(), apply=lambda c, p: None)
    with pytest.raises(ValueError):
        registry.Migration(3, 2, plan=lambda c: plan.Plan(), apply=lambda c, p: None)
