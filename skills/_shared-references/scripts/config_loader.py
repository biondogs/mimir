#!/usr/bin/env python3
"""Chargeur de `wiki.config.json` — résout `work_root` et dérive les chemins.

`work_root` est la **racine unique** ; toutes les autres variables en dérivent
(SPEC §2). Tous les chemins exposés sont **absolus** : les sessions cron Hermes
n'ont pas de `cwd` garanti (vigilance §12.7).

Deux usages de `work_root`, le **mode étant déduit du chemin** (pas de flag) :

- **Mode A — vault complet** : `work_root` = racine du vault Obsidian
  (détectée par la présence d'un dossier `.obsidian/`).
- **Mode B — répertoire dédié** : `work_root` = un sous-répertoire `[base_repertoire]`.

La résolution des chemins est **identique** dans les deux modes (tout est ancré
sur `work_root`) ; le mode n'est qu'informatif et sert au périmètre de synchro (§10).

Usage CLI (diagnostic) :

    python config_loader.py /chemin/vers/wiki.config.json
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # validation par schéma si la dépendance est présente (sinon fallback manuel)
    import jsonschema  # type: ignore
except Exception:  # pragma: no cover - dépendance optionnelle
    jsonschema = None  # type: ignore

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema" / "wiki.config.schema.json"

#: Layout par défaut (SPEC §2/§3) — surchargé par la clé `layout` du config.
DEFAULT_LAYOUT = {
    "inbox": "_inbox",
    "raw": "raw",
    "wiki": "wiki",
    "reading_grids": "reading-grids",
    "ledger": ".wiki/ingest-ledger.json",
}

#: Nom de dossier des assets (un par sujet wiki ET un par type raw — vigilance §12.14).
ASSETS_DIRNAME = "_assets"

VALID_BACKENDS = {"rclone", "git", "obsidian", "auto"}

#: Version de structure **attendue par le code courant** (capability
#: `vault-migration`). C'est la valeur à laquelle un vault fraîchement amorcé
#: doit arriver. Bumpée par chaque change de palier (v2, v3, v4…) — voir
#: `openspec/changes/add-vault-schema-migration/proposal.md` pour la table.
#:
#: v1 (baseline, `add-vault-schema-migration`)   : pose de `.mimir/vault.json`,
#:                                                aucun changement de structure.
#: v2 (`migrate-pdf-assets-per-source`)         : assets PDF/EPUB regroupés
#:                                                sous `_assets/<base>/`.
#: v3 (`refit-web-extraction-fidelity`)         : web clipping fidèle
#:                                                (`.html` immuable + .md
#:                                                re-dérivable + assets par
#:                                                slug).
#: v4 (`rework-reading-grid-as-guide`)          : grilles de lecture au
#:                                                format guide (charpente +
#:                                                zone blurb éditoriale).
EXPECTED_SCHEMA = 4

#: Chemin du marqueur de version **dans la zone synchronisée** (vs le ledger
#: `.wiki/` qui est machine-local, hors sync). Fixé (pas configurable via
#: `layout`) car c'est l'**adresse canonique** que toute machine ouvrant un
#: vault doit pouvoir lire.
VAULT_META_REL = Path(".mimir") / "vault.json"

#: Variable d'environnement pointant le `wiki.config.json` actif (cf. MIMIR_SHARED).
ENV_CONFIG = "MIMIR_CONFIG"

#: Emplacement par défaut (standard XDG). Calculé à la volée dans le resolver
#: (jamais figé à l'import) pour rester sensible à un HOME surchargé (tests).
_XDG_DEFAULT = "~/.config/mimir/wiki.config.json"

#: Nom recherché dans le répertoire courant (dernier recours ; peu fiable en cron
#: sans cwd garanti — §12.7 — d'où la priorité donnée à ENV_CONFIG puis XDG).
_CWD_CONFIG_NAME = "wiki.config.json"


def _self_root() -> Path:
    """Racine du profil Hermes installé (`~/.hermes/profiles/<nom>/`) ou du repo (dev).

    Déduite de l'emplacement de CE fichier, dont le chemin est stable :
    ``<racine>/skills/_shared-references/scripts/config_loader.py`` → ``parents[3]``.
    Permet de déposer un `wiki.config.json` **dans le dossier du profil courant**
    et de le découvrir sans variable d'environnement (fonction isolée pour rester
    monkeypatchable dans les tests).
    """
    return Path(__file__).resolve().parents[3]


class ConfigError(ValueError):
    """Config absente, illisible, ou non conforme."""


@dataclass(frozen=True)
class Config:
    """Vue résolue d'un `wiki.config.json` (tous chemins absolus)."""

    config_path: Path
    work_root: Path
    layout: dict[str, str]
    sync: dict[str, Any] = field(default_factory=dict)
    mode: str = "B"  # "A" (vault complet) | "B" (répertoire dédié)

    # --- chemins dérivés (absolus), ancrés sur work_root --------------------
    @property
    def INBOX(self) -> Path:
        """Dropzone des sources à traiter (synchronisée, vidée par wiki-extract)."""
        return self.work_root / self.layout["inbox"]

    @property
    def RAW(self) -> Path:
        return self.work_root / self.layout["raw"]

    @property
    def WIKI(self) -> Path:
        return self.work_root / self.layout["wiki"]

    @property
    def READING_GRIDS(self) -> Path:
        return self.work_root / self.layout["reading_grids"]

    @property
    def LEDGER(self) -> Path:
        return self.work_root / self.layout["ledger"]

    @property
    def ASSETS_DIRNAME(self) -> str:
        """Nom (pas chemin) du dossier d'assets : il y en a un par conteneur."""
        return ASSETS_DIRNAME

    @property
    def VAULT_META(self) -> Path:
        """Chemin absolu du marqueur de version de structure (`.mimir/vault.json`).

        **Synchronisé** : la version voyage avec le contenu, contrairement au
        ledger `.wiki/` qui reste machine-local. Voir capability `vault-migration`.
        """
        return self.work_root / VAULT_META_REL

    @property
    def VAULT_META_DIR(self) -> Path:
        """Dossier parent du marqueur (`.mimir/`) — utile pour la pose atomique."""
        return self.VAULT_META.parent

    @property
    def SNAPSHOTS_DIR(self) -> Path:
        """Dossier des snapshots de migration : **hors sync**, sous `.mimir/`.

        Volumineux (copies de deltas) et local-only : exclu du périmètre rclone
        et de `sync.git.scope` (cf. spec `vault-migration` §1.3).
        """
        return self.VAULT_META_DIR / "snapshots"

    @property
    def backend(self) -> str:
        return str(self.sync.get("backend", "rclone"))

    def as_dict(self) -> dict[str, str]:
        """Variables dérivées, pratique pour le diagnostic / l'export shell."""
        return {
            "WORK_ROOT": str(self.work_root),
            "INBOX": str(self.INBOX),
            "RAW": str(self.RAW),
            "WIKI": str(self.WIKI),
            "READING_GRIDS": str(self.READING_GRIDS),
            "LEDGER": str(self.LEDGER),
            "VAULT_META": str(self.VAULT_META),
            "MODE": self.mode,
            "BACKEND": self.backend,
        }


def _detect_mode(work_root: Path) -> str:
    """Mode A si `work_root` ressemble à une racine de vault (`.obsidian/`), sinon B."""
    return "A" if (work_root / ".obsidian").is_dir() else "B"


def _validate(raw: dict[str, Any]) -> None:
    """Valide la structure du config (schéma JSON si dispo, sinon contrôles manuels)."""
    if jsonschema is not None and _SCHEMA_PATH.exists():
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        try:
            jsonschema.validate(raw, schema)
        except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
            raise ConfigError(f"wiki.config.json non conforme : {exc.message}") from exc
        return

    # Fallback manuel (jsonschema absent) — couvre l'essentiel du DoD.
    if not isinstance(raw.get("work_root"), str) or not raw["work_root"].strip():
        raise ConfigError("`work_root` manquant ou vide.")
    backend = raw.get("sync", {}).get("backend", "rclone")
    if backend not in VALID_BACKENDS:
        raise ConfigError(f"`sync.backend` invalide : {backend!r} (attendu : {VALID_BACKENDS}).")


def resolve_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Localise le `wiki.config.json` à utiliser, sans le charger.

    « Configurer une fois, appeler sans `--config` » : si aucun chemin explicite
    n'est fourni, on cherche le premier emplacement **existant** dans l'ordre :

    1. ``$MIMIR_CONFIG`` (variable d'environnement) ;
    2. ``<racine profil/repo>/wiki.config.json`` (dossier du profil Hermes courant,
       ``~/.hermes/profiles/<nom>/`` — cf. :func:`_self_root`) ;
    3. ``~/.config/mimir/wiki.config.json`` (standard XDG) ;
    4. ``./wiki.config.json`` (répertoire courant — peu fiable en cron, §12.7).

    Un ``explicit`` fourni court-circuite la recherche et est renvoyé tel quel
    (résolu en absolu) **sans vérifier son existence** : c'est `load_config` qui
    lèvera l'erreur « Config introuvable » habituelle, comportement déjà testé.

    Un ``$MIMIR_CONFIG`` défini mais pointant un fichier absent est **toléré** :
    on passe au candidat suivant plutôt que d'échouer sec.
    """
    if explicit is not None:
        return Path(os.path.expanduser(os.fspath(explicit))).resolve()

    env_value = os.environ.get(ENV_CONFIG)
    profile_cfg = _self_root() / _CWD_CONFIG_NAME
    candidates: list[Path] = []
    if env_value:
        candidates.append(Path(os.path.expanduser(env_value)))
    candidates.append(profile_cfg)
    candidates.append(Path(os.path.expanduser(_XDG_DEFAULT)))
    candidates.append(Path.cwd() / _CWD_CONFIG_NAME)

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()

    raise ConfigError(
        "Aucun wiki.config.json trouvé. Emplacements cherchés (dans l'ordre) :\n"
        f"  1. ${ENV_CONFIG} ({env_value or 'non défini'})\n"
        f"  2. {profile_cfg}  (dossier du profil/repo)\n"
        f"  3. {os.path.expanduser(_XDG_DEFAULT)}\n"
        f"  4. ./{_CWD_CONFIG_NAME}  (cwd actuel : {Path.cwd()})\n"
        f"Configurez l'un de ces emplacements, ou passez --config <chemin>.\n"
        f"Exemple : export {ENV_CONFIG}=/chemin/vers/wiki.config.json"
    )


def load_resolved_config(explicit: str | os.PathLike[str] | None = None) -> Config:
    """Résout l'emplacement (auto-découverte si `explicit` absent) puis charge.

    Point d'entrée des skills : ``load_resolved_config(args.config)`` où
    ``args.config`` vaut ``None`` quand l'utilisateur n'a pas passé ``--config``.
    """
    return load_config(resolve_config_path(explicit))


def load_config(config_path: str | os.PathLike[str]) -> Config:
    """Charge et valide un `wiki.config.json`, renvoie une `Config` résolue."""
    path = Path(os.path.expanduser(os.fspath(config_path))).resolve()
    if not path.is_file():
        raise ConfigError(f"Config introuvable : {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"JSON invalide dans {path} : {exc}") from exc

    _validate(raw)

    work_root = Path(os.path.expanduser(raw["work_root"]))
    if not work_root.is_absolute():
        # Un work_root relatif est ancré sur le dossier du config (cohérence cron).
        work_root = (path.parent / work_root)
    work_root = Path(os.path.realpath(work_root))

    layout = {**DEFAULT_LAYOUT, **(raw.get("layout") or {})}
    mode = raw.get("mode") or _detect_mode(work_root)

    return Config(
        config_path=path,
        work_root=work_root,
        layout=layout,
        sync=raw.get("sync") or {},
        mode=mode,
    )


if __name__ == "__main__":  # diagnostic
    import sys

    # Usage : config_loader.py [<wiki.config.json>]
    # Sans argument → auto-découverte ($MIMIR_CONFIG → ~/.config/mimir → ./).
    if len(sys.argv) > 2:
        print("Usage: config_loader.py [<wiki.config.json>]", file=sys.stderr)
        raise SystemExit(2)
    explicit = sys.argv[1] if len(sys.argv) == 2 else None
    try:
        path = resolve_config_path(explicit)
        cfg = load_config(path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    print(f"CONFIG_PATH={path}")
    for key, value in cfg.as_dict().items():
        print(f"{key}={value}")
