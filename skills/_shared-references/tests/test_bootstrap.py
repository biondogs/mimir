"""Tests du bootstrap unifié (`spec mimir-invariants` — Unified socle bootstrap).

Couvre :
- 3.1 `from bootstrap import safe_path` fonctionne **hors** d'un skill
       (charge le socle via son chemin absolu, pas via une manip relative
       propre à un orchestrateur `wiki-*`).
- 3.2 Un orchestrateur qui référence un symbole du socle **non exporté** par
       `bootstrap` lève `ImportError` au chargement, jamais `NameError` en
       cours de run.
- 3.3 L'override `MIMIR_SHARED` est honoré : quand la variable pointe un
       répertoire valide, c'est ce chemin qui prend la tête de `sys.path`
       (priorité sur le chemin bundlé), même sur macOS où `/var` et
       `/private/var` se résolvent vers le même inode.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest


SHARED_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _ensure_socle_on_path():
    """Garantit que `SHARED_SCRIPTS` est sur `sys.path` (idempotent). Ne touche
    **pas** `sys.modules` : on veut que les modules du socle (notamment
    `guard`) restent **la même instance** pour tous les tests, sinon un
    orchestrateur (`wiki_init` etc.) qui les a déjà chargés en globals
    verrait un `ConfinementError` d'une classe ≠ de celle attendue."""
    if str(SHARED_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SHARED_SCRIPTS))


def _reload_bootstrap():
    """Force un re-exec du top-level de `bootstrap` (et lui seul). Les
    modules `guard`/`config_loader`/... restent **la même instance** :
    on rejoue juste `_ensure_socle_path` pour relire `MIMIR_SHARED`."""
    sys.modules.pop("bootstrap", None)


# --- 3.1 : `from bootstrap import safe_path` hors d'un skill ----------------
def test_safe_path_importable_hors_skill():
    """`safe_path` (et tout le socle) résout depuis n'importe quel cwd, dès
    lors que `bootstrap.py` est sur `sys.path`. C'est précisément le scénario
    que ne pouvaient pas garantir les blocs `sys.path` dupliqués par skill."""
    _ensure_socle_on_path()
    sys.path.insert(0, str(SHARED_SCRIPTS))
    from bootstrap import safe_path, assert_within, ConfinementError  # noqa: F401

    # `safe_path` utilise `os.path.realpath` (résout les symlinks, ex. /tmp
    # -> /private/tmp sur macOS) — d'où `.resolve()` des deux côtés.
    root = Path("/tmp").resolve()
    p = safe_path(root, "raw", "pdfs", "wharram.pdf")
    assert p == root / "raw" / "pdfs" / "wharram.pdf"
    # `assert_within` doit bien sûr valider un chemin sous work_root.
    assert assert_within(root, root / "raw") == (root / "raw")
    # Et lever ConfinementError sur une évasion.
    with pytest.raises(ConfinementError):
        assert_within(root, "/etc/passwd")


def test_bootstrap_reexporte_les_modules_socle():
    """Tous les modules du socle (cf. spec) sont accessibles via `bootstrap.<mod>`."""
    _ensure_socle_on_path()
    sys.path.insert(0, str(SHARED_SCRIPTS))
    import bootstrap
    for mod_name in ("config_loader", "frontmatter", "iohelpers", "ledger", "slug"):
        mod = getattr(bootstrap, mod_name)
        assert hasattr(mod, "__file__"), f"{mod_name} non ré-exporté par bootstrap"
        assert mod.__name__ == mod_name


# --- 3.2 : symbole manquant -> ImportError au chargement --------------------
def test_symbole_manquant_leve_importererror_au_chargement(tmp_path):
    """Si le bootstrap tente de ré-exporter un symbole qui n'existe PAS dans
    le socle, l'`import bootstrap` lui-même doit lever `ImportError`
    (fail fast, pas un `NameError` à retardement).

    C'est précisément la classe de bugs qu'on ferme (REX Wharram §3 :
    `NameError: name 'safe_path' is not defined` parce que `guard.safe_path`
    n'avait pas été importé en haut de `batch_produce.py`).

    Stratégie : on crée un **clone** de `bootstrap.py` dans `tmp_path` qui
    essaie d'importer un symbole absent, on le charge via
    `importlib.util.spec_from_file_location` (sans polluer `sys.modules`
    avec un faux `guard`), et on vérifie que l'`ImportError` est levé au
    chargement du module — pas un `NameError` à l'usage."""
    src = (SHARED_SCRIPTS / "bootstrap.py").read_text(encoding="utf-8")
    # Injecte l'import d'un symbole qui n'existe pas dans `guard`.
    mutated = src.replace(
        "from guard import (",
        "from guard import (\n    this_symbol_does_not_exist,",
        1,
    )
    assert "this_symbol_does_not_exist" in mutated, "patch failed"

    bogus = tmp_path / "bootstrap_broken.py"
    bogus.write_text(mutated, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("bootstrap_broken", bogus)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    with pytest.raises(ImportError):
        spec.loader.exec_module(mod)


# --- 3.3 : MIMIR_SHARED honored --------------------------------------------
def test_mimir_shared_prend_la_tete_de_sys_path(tmp_path):
    """Quand `MIMIR_SHARED` pointe un répertoire valide, ce chemin résolu
    doit être en tête de `sys.path` (priorité sur le chemin bundlé)."""
    # Deux socles distincts : MIMIR_SHARED et le bundled. Chacun est complet
    # (sinon le bootstrap échoue à `import config_loader` dès le chargement).
    alt = tmp_path / "alt-socle"
    shutil.copytree(SHARED_SCRIPTS, alt)
    bundled = tmp_path / "bundled"
    shutil.copytree(SHARED_SCRIPTS, bundled)

    _ensure_socle_on_path()
    # L'orchestrateur pré-insère le bundled (équivalent de la ligne
    # `sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ...))`).
    sys.path.insert(0, str(bundled))
    old = os.environ.get("MIMIR_SHARED")
    os.environ["MIMIR_SHARED"] = str(alt)
    _reload_bootstrap()  # re-run _ensure_socle_path avec MIMIR_SHARED posé
    try:
        importlib.import_module("bootstrap")
        # MIMIR_SHARED résolu doit être en tête de sys.path.
        alt_resolved = str(Path(alt).resolve())
        assert sys.path[0] == alt_resolved, (
            f"MIMIR_SHARED ignoré : sys.path[0]={sys.path[0]!r}, attendu {alt_resolved!r}"
        )
    finally:
        if old is None:
            os.environ.pop("MIMIR_SHARED", None)
        else:
            os.environ["MIMIR_SHARED"] = old


def test_mimir_shared_invalide_est_ignore(tmp_path):
    """Un `MIMIR_SHARED` pointant un répertoire inexistant ne doit pas
    planter : le bootstrap l'ignore silencieusement et retombe sur le
    chemin bundlé. Vérifie aussi qu'aucun chemin invalide n'est inséré
    dans `sys.path` (sinon un attaquant pourrait faire pointer `MIMIR_SHARED`
    vers un répertoire hostile sans qu'on s'en aperçoive)."""
    _ensure_socle_on_path()
    old = os.environ.get("MIMIR_SHARED")
    os.environ["MIMIR_SHARED"] = str(tmp_path / "nope")
    _reload_bootstrap()
    try:
        importlib.import_module("bootstrap")  # ne lève pas
        # Le path invalide n'est PAS dans sys.path.
        assert str(tmp_path / "nope") not in sys.path
        # Et le path résolu du path invalide non plus (sinon `import` pourrait
        # le résoudre par side-effect — ex. symlink créé entre-temps).
        assert str((tmp_path / "nope").resolve()) not in sys.path
    finally:
        if old is None:
            os.environ.pop("MIMIR_SHARED", None)
        else:
            os.environ["MIMIR_SHARED"] = old


# --- 3.4 couvert par `pytest -q` (toute la suite doit rester verte) ---------
def test_bootstrap_idempotent():
    """Réimporter bootstrap (re-run du bloc top-level) n'ajoute pas de doublon
    dans `sys.path` — un chemin déjà présent n'est pas ré-inséré."""
    _ensure_socle_on_path()
    sys.path.insert(0, str(SHARED_SCRIPTS))
    importlib.import_module("bootstrap")
    n0 = len(sys.path)
    # Recharger : on re-déclenche _ensure_socle_path
    import bootstrap as _b
    _b._ensure_socle_path()  # noqa: SLF001 (test interne du contrat)
    assert len(sys.path) == n0, "bootstrap n'est pas idempotent"
