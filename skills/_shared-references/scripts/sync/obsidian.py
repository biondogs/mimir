#!/usr/bin/env python3
"""Backend de synchro **obsidian** — délègue à Remotely Save (SPEC §10).

Remotely Save est un plugin Obsidian **per-fichier**, à base d'état (DB), qui
**propage les suppressions** dans les deux sens. On ne le **réimplémente pas**
: on le déclenche via le plugin **Local REST API** d'Obsidian.

Cycle (`pull` / `push` / `sync` côté CLI) :

1. **Sonde `GET {base_url}/`** — readiness (200 = Obsidian joignable).
2. **`POST {base_url}/commands/{command_id}/`** — déclenche la commande
   `remotely-save:start-sync` (Bearer lu depuis `api_key_env`). `204` =
   commande dispatchée (fire-and-forget : pas la fin de sync).
3. **Attente `post_sync_wait_s`** — settle pour laisser Remotely Save finir.
4. **`validate`** : compte local vs `rclone lsf <remote> --recursive
   --files-only`. Si l'écart dépasse la tolérance, **échec signalé** (le mode
   `auto` peut alors basculer sur rclone dans le même run).

**Secret hors JSON** : la clé d'API est lue depuis la variable d'environnement
nommée par `sync.obsidian.api_key_env`. Le backend n'inclut **jamais** la clé
dans un `repr()`/`str()`/`warnings` (juste le **nom** de la variable).

Toutes les requêtes HTTP passent par un **client injectable**
(`http_client(method, url, **kwargs) -> (status, body)`) : les tests
fournissent un fake, aucun réseau réel n'est requis.
"""

from __future__ import annotations

import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .base import SyncBackend

#: URL par défaut du Local REST API Obsidian (HTTPS, port 27124, cert auto-signé).
DEFAULT_BASE_URL = "https://127.0.0.1:27124"

#: Identifiant complet de la commande Remotely Save (vérifié, cf. `design.md`).
DEFAULT_COMMAND_ID = "remotely-save:start-sync"

#: Bornes par défaut — exposées comme constantes pour les tests.
DEFAULT_API_KEY_ENV = "MIMIR_OBSIDIAN_API_KEY"
DEFAULT_READY_TIMEOUT_S = 30
DEFAULT_POST_SYNC_WAIT_S = 20
DEFAULT_VERIFY_TLS = False
DEFAULT_AUTO_LAUNCH = False
DEFAULT_TOLERANCE = 5

#: Dossiers de service jamais comptés (cohérence rclone).
_IGNORED_DIRS = {".git", ".obsidian", ".wiki", ".rclone-bisync"}


def _default_http_client(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: str | bytes | None = None,
    verify_tls: bool = True,
    timeout: float = 10.0,
) -> tuple[int, str]:
    """Client HTTP par défaut (urllib). Renvoie `(status, body_str)`.

    Lève `ConnectionError` sur erreur réseau (timeout, connection refused, …)
    et `OSError` sur résolution DNS. Les `HTTPError` (4xx/5xx) sont **convertis
    en tuple `(status, body)`** : le caller décide si le code est OK ou non.
    """
    if isinstance(body, str):
        body = body.encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    ctx = ssl._create_unverified_context() if not verify_tls else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ConnectionError(str(exc)) from exc


class ObsidianBackend(SyncBackend):
    """Délègue la synchro au plugin Remotely Save d'Obsidian (Local REST API)."""

    name = "obsidian"

    def __init__(
        self,
        cfg,
        runner: Callable[..., Any] | None = None,
        *,
        http_client: Callable[..., tuple[int, str]] | None = None,
    ):
        super().__init__(cfg, runner)
        conf = (getattr(cfg, "sync", {}) or {}).get("obsidian", {}) or {}
        self.base_url = conf.get("base_url", DEFAULT_BASE_URL).rstrip("/")
        self.api_key_env = conf.get("api_key_env", DEFAULT_API_KEY_ENV)
        self.command_id = conf.get("command_id", DEFAULT_COMMAND_ID)
        self.vault_name = conf.get("vault_name")
        self.auto_launch = bool(conf.get("auto_launch", DEFAULT_AUTO_LAUNCH))
        self.launch_cmd = conf.get("launch_cmd")
        self.ready_timeout_s = int(conf.get("ready_timeout_s", DEFAULT_READY_TIMEOUT_S))
        self.post_sync_wait_s = int(conf.get("post_sync_wait_s", DEFAULT_POST_SYNC_WAIT_S))
        self.verify_tls = bool(conf.get("verify_tls", DEFAULT_VERIFY_TLS))
        self.tolerance = int(conf.get("tolerance", DEFAULT_TOLERANCE))
        # rclone (remote + bin) **requis** pour `validate` (comptage distant).
        # Sans cette config, validate renvoie 1 + warning.
        rclone_conf = (getattr(cfg, "sync", {}) or {}).get("rclone", {}) or {}
        self.rclone_bin = rclone_conf.get("bin", "rclone")
        self.rclone_remote = rclone_conf.get("remote")
        self.local = Path(cfg.work_root)
        self._http = http_client or _default_http_client
        # Agrégat unique de warnings — jamais le secret en clair.
        self.warnings: list[str] = []

    # --- introspection / représentation sûre --------------------------------
    def __repr__(self) -> str:
        return (
            f"ObsidianBackend(base_url={self.base_url!r}, "
            f"command_id={self.command_id!r}, api_key_env={self.api_key_env!r})"
        )

    # --- auth ---------------------------------------------------------------
    def _api_key(self) -> str | None:
        return os.environ.get(self.api_key_env)

    # --- sonde readiness ----------------------------------------------------
    def probe(self) -> bool:
        """`GET {base_url}/` → `True` si 200. Lève `ConnectionError` attrapée localement."""
        try:
            status, _ = self._http(
                "GET", f"{self.base_url}/", verify_tls=self.verify_tls, timeout=5.0
            )
        except (ConnectionError, OSError):
            return False
        return status == 200

    # --- trigger Remotely Save ----------------------------------------------
    def trigger(self) -> int:
        """`POST {base_url}/commands/{command_id}/` (fire-and-forget, Bearer).

        Renvoie 0 si 200/204, 1 sinon. **N'inclut jamais la clé dans un
        warning** (juste le nom de la variable d'env).
        """
        key = self._api_key()
        if not key:
            self.warnings.append(
                f"clé API Obsidian absente (env {self.api_key_env!r}) — trigger non envoyé."
            )
            return 1
        url = f"{self.base_url}/commands/{self.command_id}/"
        headers = {"Authorization": f"Bearer {key}"}
        try:
            status, _ = self._http("POST", url, headers=headers, body="",
                                    verify_tls=self.verify_tls, timeout=10.0)
        except (ConnectionError, OSError) as exc:
            self.warnings.append(f"Obsidian trigger POST inaccessible : {exc}")
            return 1
        if status in (200, 204):
            return 0
        self.warnings.append(f"Obsidian trigger POST → code HTTP {status}")
        return 1

    # --- attente settle post-trigger ----------------------------------------
    def wait_post_sync(self) -> None:
        if self.post_sync_wait_s > 0:
            time.sleep(self.post_sync_wait_s)

    # --- comptages local / distant (réutilisés par `validate`) ---------------
    def _local_count(self) -> int:
        n = 0
        for root, dirs, files in os.walk(self.local):
            dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
            n += sum(1 for f in files if f != ".DS_Store")
        return n

    def _remote_count(self) -> int | None:
        """`rclone lsf <remote> --recursive --files-only`. `None` si KO."""
        if not self.rclone_remote:
            return None
        res = self._run(
            [self.rclone_bin, "lsf", self.rclone_remote, "--recursive", "--files-only"]
        )
        if res.returncode != 0:
            return None
        return sum(1 for line in (res.stdout or "").splitlines() if line.strip())

    # --- interface SyncBackend ----------------------------------------------
    def pull(self) -> int:
        """Pull = trigger Remotely Save + wait (Remotely Save est bidirectionnel)."""
        rc = self.trigger()
        if rc != 0:
            return rc
        self.wait_post_sync()
        return 0

    def push(self) -> int:
        """Push = pull (mêmes sémantiques : Remotely Save est bidirectionnel)."""
        return self.pull()

    def validate(self, *, tolerance: int | None = None) -> int:
        """Compare `local` ↔ `rclone lsf <remote>` ; écart > tolérance = échec.

        - `rclone.remote` absent : warning + return 1 (impossible de vérifier).
        - Écart dans la tolérance : 0.
        - Écart hors tolérance : 1 (le mode `auto` peut alors basculer rclone).
        """
        tol = tolerance if tolerance is not None else self.tolerance
        if not self.rclone_remote:
            self.warnings.append(
                "Obsidian validate : `sync.rclone.remote` absent — comptage distant "
                "impossible (la bascule `auto` ne peut pas non plus s'appuyer dessus)."
            )
            return 1
        local = self._local_count()
        remote = self._remote_count()
        if remote is None:
            return 1
        return 0 if abs(local - remote) <= tol else 1

    # --- auto-launch Obsidian (best-effort, opt-in) -------------------------
    def _has_display(self) -> bool:
        """macOS : toujours. Linux : `$DISPLAY` ou `$WAYLAND_DISPLAY` requis."""
        if sys.platform == "darwin":
            return True
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def _default_launch_argv(self) -> list[str]:
        if sys.platform == "darwin":
            if self.vault_name:
                return ["open", f"obsidian://open?vault={self.vault_name}"]
            return ["open", "-a", "Obsidian"]
        # Linux : pas de défaut générique — `launch_cmd` doit être fourni.
        return ["obsidian"]

    def _launch_argv(self) -> list[str] | None:
        if self.launch_cmd:
            return self.launch_cmd.split()
        return self._default_launch_argv()

    def launch(self) -> int:
        """Lance Obsidian (best-effort). Renvoie 0 si la commande a été lancée,
        non-zéro sinon. **Ne lève jamais** (OSError, FileNotFoundError attrapés).
        """
        if not self.auto_launch:
            return 1
        if not self._has_display():
            return 2
        argv = self._launch_argv()
        if not argv:
            return 3
        try:
            self._run(argv)
        except (OSError, FileNotFoundError):
            return 4
        return 0

    def wait_until_ready(self, *, timeout: int | None = None) -> bool:
        """Poll `GET /` jusqu'à 200 dans la limite `timeout` (ou `ready_timeout_s`)."""
        deadline = time.time() + (timeout if timeout is not None else self.ready_timeout_s)
        while time.time() < deadline:
            if self.probe():
                return True
            time.sleep(1.0)
        return False
