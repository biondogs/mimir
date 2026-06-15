#!/usr/bin/env python3
"""Moteur de synchro pluggable de Mimir (SPEC §10).

Façade unique que tous les orchestrateurs importent (`import sync`) :

    sync.pull(cfg)     # pré-sync : récupère le distant avant compilation
    sync.push(cfg)     # post-sync : publie le local après compilation
    sync.validate(cfg) # vérifie la cohérence local↔distant
    with sync.lock(cfg): ...   # mutex hors work_root

Le backend est **sélectionné par `sync.backend`** du `wiki.config.json`
(`rclone` | `git` | `obsidian` | `auto`), ou `noop` si le config n'a pas de
clé `sync`. Le mode `auto` sonde Obsidian et bascule dynamiquement (cf.
`AutoBackend`). Les backends réels sont importés **paresseusement** (un
environnement git n'a pas besoin de `rclone` et vice-versa). Le `runner`
(= `subprocess.run`) et le `http_client` (pour le backend `obsidian`) sont
injectables pour des tests hermétiques.
"""

from __future__ import annotations

from typing import Any, Callable

from . import locking as _lockmod
from .base import SyncBackend, SyncError
from .locking import InboxLock, LockHeld, SyncLock

__all__ = [
    "get_backend",
    "pull",
    "push",
    "validate",
    "lock",
    "SyncBackend",
    "SyncError",
    "SyncLock",
    "InboxLock",
    "LockHeld",
]


def get_backend(
    cfg,
    runner: Callable[..., Any] | None = None,
    *,
    http_client: Callable[..., tuple[int, str]] | None = None,
) -> SyncBackend:
    """Instancie le backend désigné par `cfg` (`noop` si aucune clé `sync`).

    `http_client` n'est utilisé que par les backends `obsidian` et `auto`
    (pour leurs appels Local REST API) ; les autres l'ignorent.
    """
    sync = getattr(cfg, "sync", None) or {}
    if not sync:
        from .noop import NoopBackend

        return NoopBackend(cfg, runner)

    backend = cfg.backend
    if backend == "rclone":
        from .rclone import RcloneBackend

        return RcloneBackend(cfg, runner)
    if backend == "git":
        from .git import GitBackend

        return GitBackend(cfg, runner)
    if backend == "obsidian":
        from .obsidian import ObsidianBackend

        return ObsidianBackend(cfg, runner, http_client=http_client)
    if backend == "auto":
        from .auto import AutoBackend

        return AutoBackend(cfg, runner, http_client=http_client)
    raise SyncError(f"Backend de synchro inconnu : {backend!r}")


def pull(cfg, *, runner: Callable[..., Any] | None = None) -> int:
    return get_backend(cfg, runner).pull()


def push(cfg, *, runner: Callable[..., Any] | None = None) -> int:
    return get_backend(cfg, runner).push()


def validate(cfg, *, runner: Callable[..., Any] | None = None) -> int:
    return get_backend(cfg, runner).validate()


def lock(cfg=None, **kwargs: Any) -> SyncLock:
    """Acquiert le verrou mutex (hors `work_root`). Lève `LockHeld` si déjà tenu."""
    return _lockmod.acquire(cfg, **kwargs)
