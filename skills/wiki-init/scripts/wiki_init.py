#!/usr/bin/env python3
"""wiki-init — amorçage du vault Mimir au premier usage (SPEC §2, §3, §10).

Usage :
    wiki_init.py [--config <wiki.config.json>] {apply|status} [--skip-sync]

- `status` : diagnostic en **lecture seule** — imprime le `wiki.config.json` résolu et
  tous les chemins dérivés (work_root, _inbox, raw, wiki, reading-grids), en signalant
  ce qui existe (✓) ou manque (✗). Répond à « où est mon wiki ».
- `apply`  : (défaut) crée la racine + l'arborescence, écrit un accueil `_inbox/` et un
  `wiki/INDEX.md` initial **s'ils sont absents** (jamais d'écrasement), puis amorce la
  synchro (`pull` → `push` sous verrou).

Sur un `work_root` vide, rien n'existe encore (ni `_inbox/` où déposer, ni état de
synchro) : `config_loader` ne crée rien et les autres skills ne créent leurs dossiers
qu'implicitement, à la première écriture. Ce skill pose donc la structure une fois,
de façon **idempotente** et **bornée à work_root** (confinement, §2).

**Version de structure** (capability `vault-migration`) : `apply` pose
`.mimir/vault.json` à `EXPECTED_SCHEMA` sur un vault vierge, et à la **baseline
`1`** (sans migrer) sur un vault déjà peuplé qui n'a pas le fichier. `status`
affiche `SCHEMA=<vault>` vs `EXPECTED=<code>` + la liste des paliers en attente.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# --- bootstrap : importer le socle via le module unifié `bootstrap` ---------
# (L'insertion de sys.path est centralisée dans `bootstrap._ensure_socle_path` :
#  MIMIR_SHARED honoré, idempotent, fail fast sur symbole manquant.)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_shared-references" / "scripts"))
import bootstrap  # noqa: E402,F401  (socle mutualisé ; voir skills/_shared-references/scripts/bootstrap.py)

import config_loader  # noqa: E402
import guard  # noqa: E402
import iohelpers  # noqa: E402
import sync  # noqa: E402
import vault_meta  # noqa: E402

#: Fichier d'accueil déposé dans `_inbox/` (mode d'emploi pour l'utilisateur).
INBOX_README_NAME = "LISEZ-MOI.md"
#: Index initial déposé dans `wiki/` (point de départ ; régénéré par wiki-index).
INDEX_NAME = "INDEX.md"

_INBOX_README = """# _inbox — dépose ici tes sources

C'est la **boîte d'entrée** de ton second cerveau. Tout ce que tu déposes ici est
digéré automatiquement par Mimir :

- **PDF** et **EPUB** : glisse le fichier dans ce dossier.
- **Page web** : crée un fichier `.url` (ou donne le lien à Mimir) — il sera clippé.

Mimir extrait la matière brute vers `raw/` (immuable), puis la compile en articles
par notion dans `wiki/`. Le dossier `_inbox/` est **vidé** au fil du traitement
(le binaire est déplacé vers `raw/`, jamais copié).

> Ce fichier est informatif : tu peux le laisser, il n'est pas traité comme une source.
"""

_INDEX_INIT = """# Index du wiki

> Index initial posé par `wiki-init`. Il sera **régénéré automatiquement** par
> `wiki-index` dès que des articles existeront (`wiki-index regenerate`).

_Aucun sujet pour l'instant — dépose une première source dans `_inbox/` pour démarrer._
"""


def _line(op: str, rc: int) -> str:
    return f"{'✅' if rc == 0 else '❌'} {op} (code {rc})"


def _structure(cfg) -> list[tuple[str, Path]]:
    """Dossiers de base à garantir (ordre logique du pipeline), ancrés sur work_root."""
    return [
        ("_inbox", cfg.INBOX),
        ("raw", cfg.RAW),
        ("wiki", cfg.WIKI),
        ("reading-grids", cfg.READING_GRIDS),
    ]


def _with_lock(cfg, fn) -> int:
    """Exécute `fn` sous verrou ; verrou déjà tenu = skip propre (code 0, SPEC §10)."""
    try:
        lk = sync.lock(cfg)
    except sync.LockHeld as exc:
        print(f"🔒 {exc} — une autre synchro tourne, skip.")
        return 0
    try:
        return fn()
    finally:
        lk.release()


# --- status (lecture seule) ------------------------------------------------
def _pending_paliers(cfg) -> list[str]:
    """Liste lisible des paliers en attente (`v1→v2: regrouper assets PDF`).

    Lecture seule : on interroge le registre du moteur de migration. Import
    paresseux pour ne pas coupler `wiki-init` à la machinerie de migration au
    top-level (peut être absente dans un environnement allégé).
    """
    try:
        from migration.registry import registered, pending_for
    except ImportError:
        return []
    current = vault_meta.read_vault_schema(cfg)
    expected = getattr(cfg, "_schema_target", None) or _expected_schema()
    chain = pending_for(current, target=expected)
    if not chain:
        # Vérifier qu'il n'y a pas un **trou** dans le registre (paliers
        # attendus mais absents) — on le signale pour ne pas masquer une
        # dépendance manquante.
        all_paliers = registered()
        missing = [
            f"v{current}→v{current + 1}"
            for m in all_paliers
            if m.from_schema == current
        ]
        if not all_paliers and current < expected:
            return [f"v{current}→v{expected}: (aucun palier enregistré)"]
        if missing:
            return missing
        return []
    return [f"v{m.from_schema}→v{m.to_schema}: {m.description}" for m in chain]


def _expected_schema() -> int:
    """Lit la constante `EXPECTED_SCHEMA` du socle (sans coupler à `wiki-init`)."""
    import config_loader as cl
    return cl.EXPECTED_SCHEMA


def cmd_status(cfg, **_) -> int:
    """Imprime la config résolue + l'état de la structure (existe / manquant).

    Inclut le diagnostic de version de structure (`SCHEMA` vs `EXPECTED`) +
    la liste des paliers en attente (capability `vault-migration`).
    """
    print(f"CONFIG_PATH={cfg.config_path}")
    for key, value in cfg.as_dict().items():
        print(f"{key}={value}")

    # Version de structure (capability `vault-migration`)
    current = vault_meta.read_vault_schema(cfg)
    expected = _expected_schema()
    pending = _pending_paliers(cfg)
    print(f"\nSCHEMA={current} (vault) vs EXPECTED={expected} (code)")
    if current >= expected:
        print("  ✓ à jour")
    else:
        print(f"  ⚠️  en retard de {expected - current} palier(s)")
        for p in pending:
            print(f"    - {p}")

    print("\nÉtat de la structure :")
    root = Path(cfg.work_root)
    print(f"  {'✓' if root.is_dir() else '✗'} work_root        {root}")
    for label, path in _structure(cfg):
        print(f"  {'✓' if Path(path).is_dir() else '✗'} {label:<15} {path}")
    files = [
        ("_inbox/" + INBOX_README_NAME, cfg.INBOX / INBOX_README_NAME),
        ("wiki/" + INDEX_NAME, cfg.WIKI / INDEX_NAME),
        (".mimir/vault.json", cfg.VAULT_META),
    ]
    for label, path in files:
        print(f"  {'✓' if Path(path).is_file() else '✗'} {label:<22} {path}")
    return 0


# --- apply -----------------------------------------------------------------
def _write_if_absent(cfg, path: Path, content: str) -> None:
    """Écrit `content` atomiquement (confiné) seulement si `path` n'existe pas."""
    guard.assert_within(cfg.work_root, path)
    if Path(path).exists():
        print(f"  · {path.name} déjà présent (inchangé)")
        return
    iohelpers.atomic_write_text(path, content, work_root=cfg.work_root)
    print(f"  + {path.name} créé")


def _pose_vault_marker(cfg, expected: int) -> None:
    """Pose `.mimir/vault.json` selon les règles (capability `vault-migration`).

    - Vault **vierge** (aucun article, aucun raw) → `expected` (à jour d'emblée).
    - Vault **peuplé** sans marqueur → **baseline 1** (jamais de migration rétroactive).
    - Marqueur déjà présent → no-op (jamais d'écrasement).

    Cette pose NE déclenche **aucune** migration : c'est un simple marqueur initial.

    **Détection « peuplé »** : on considère qu'un vault est peuplé s'il a
    - du contenu réel sous `raw/<type>/` (pas juste `_status.md` qui est
      créé par `wiki-extract` au premier passage) ;
    - OU au moins un sujet réel sous `wiki/<sujet>/` (pas juste le
      `wiki/INDEX.md` placeholder que `wiki-init` lui-même vient d'écrire).
    Sans cette discrimination, on classerait à tort un vault fraîchement
    amorcé par `apply` comme « peuplé » (à cause du placeholder INDEX.md) et
    on le poserait à baseline 1 au lieu d'`expected` — régression introduite
    par le bump d'`EXPECTED_SCHEMA` à 2.
    """
    guard.assert_within(cfg.work_root, cfg.VAULT_META)
    if cfg.VAULT_META.is_file():
        print(f"  · {cfg.VAULT_META.relative_to(cfg.work_root)} déjà présent (inchangé)")
        return
    is_populated = _is_vault_populated(cfg)
    target_schema = expected if not is_populated else vault_meta.BASELINE_SCHEMA
    iohelpers.ensure_dir(cfg.VAULT_META_DIR)
    vault_meta.bump_vault_schema(cfg, target_schema)
    label = (
        f"{target_schema} (à jour d'emblée)" if not is_populated
        else f"{target_schema} (baseline, vault déjà peuplé)"
    )
    print(f"  + {cfg.VAULT_META.relative_to(cfg.work_root)} créé — schema = {label}")


def _is_vault_populated(cfg) -> bool:
    """Vrai si le vault porte du contenu réel (au-delà des artefacts
    d'amorçage écrits par `wiki-init apply` lui-même).

    Heuristique (volontairement conservatrice — un faux positif « peuplé »
    pose la baseline 1 alors qu'on aurait pu poser `expected` ; c'est
    récupérable en relançant `apply` plus tard, mais c'est mieux de
    l'éviter) :

    - **Raw réel** : `raw/<type>/` contient au moins un fichier autre que
      `_status.md` (la table de statut est le 1er fichier créé par
      `wiki-extract`, pas un contenu utilisateur).
    - **Wiki réel** : `wiki/<sujet>/` existe (un sous-dossier de sujet ;
      le `wiki/INDEX.md` placeholder ne compte pas).
    """
    if cfg.RAW.is_dir():
        for type_dir in cfg.RAW.iterdir():
            if not type_dir.is_dir():
                continue
            for f in type_dir.iterdir():
                if f.is_file() and f.name != "_status.md":
                    return True
    if cfg.WIKI.is_dir():
        for entry in cfg.WIKI.iterdir():
            if entry.is_dir():  # un sous-dossier de sujet = wiki peuplé
                return True
    return False


def _amorcer_sync(cfg) -> int:
    """Premier cycle `pull` → `push` sous verrou (rclone amorce seul ; git premier commit)."""
    print("\nAmorçage de la synchro :")

    def run() -> int:
        backend = sync.get_backend(cfg)
        rc = backend.pull()
        print(_line("pull", rc))
        if rc == 0:
            rc = backend.push()
            print(_line("push", rc))
        for w in getattr(backend, "warnings", []):
            print(f"⚠️  {w}")
        return rc

    return _with_lock(cfg, run)


def cmd_apply(cfg, *, skip_sync: bool = False, **_) -> int:
    """Crée l'arborescence + l'accueil + l'INDEX + le marqueur de version
    (idempotent), puis amorce la synchro."""
    print("Création de l'arborescence (sous work_root) :")
    for label, path in [("work_root", Path(cfg.work_root))] + _structure(cfg):
        guard.assert_within(cfg.work_root, path)  # refuse un layout d'évasion (../)
        existed = Path(path).is_dir()
        iohelpers.ensure_dir(path)
        print(f"  {'·' if existed else '+'} {label:<15} {path}")

    print("\nFichiers d'amorçage :")
    _write_if_absent(cfg, cfg.INBOX / INBOX_README_NAME, _INBOX_README)
    _write_if_absent(cfg, cfg.WIKI / INDEX_NAME, _INDEX_INIT)

    print("\nMarqueur de version de structure :")
    _pose_vault_marker(cfg, expected=_expected_schema())

    rc = 0
    if skip_sync:
        print("\n↪ Synchro sautée (--skip-sync).")
    else:
        rc = _amorcer_sync(cfg)

    print("\nRécapitulatif :")
    cmd_status(cfg)
    return rc


_COMMANDS = {"apply": cmd_apply, "status": cmd_status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Initialise le wiki Mimir (structure + amorçage synchro)."
    )
    parser.add_argument(
        "--config", default=None,
        help="Chemin du wiki.config.json (optionnel ; auto-découverte si omis : "
             "$MIMIR_CONFIG, dossier du profil, ~/.config/mimir/wiki.config.json, ./wiki.config.json)",
    )
    parser.add_argument(
        "command", nargs="?", choices=sorted(_COMMANDS), default="apply",
        help="apply (défaut) : crée la structure + amorce la synchro ; status : diagnostic lecture seule.",
    )
    parser.add_argument(
        "--skip-sync", action="store_true",
        help="apply : crée la structure sans amorcer la synchro (hors-ligne / backend noop).",
    )
    args = parser.parse_args(argv)

    cfg = config_loader.load_resolved_config(args.config)
    if args.command == "status":
        return cmd_status(cfg)
    return cmd_apply(cfg, skip_sync=args.skip_sync)


if __name__ == "__main__":
    raise SystemExit(main())
