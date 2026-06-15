#!/usr/bin/env python3
"""Backend de synchro **git** (SPEC §10).

Versionne la zone de travail dans un dépôt git et se synchronise via `origin` :

- `pull`     = `git pull --rebase origin <branch>` (avant compilation).
- `push`     = `git add <scope existant> && git commit && git push` (après compilation).
- `validate` = `git fetch` puis comparaison `HEAD` local vs `origin/<branch>`.

Le **scope** (`sync.git.scope`) borne ce qui est versionné aux sous-arbres Mimir
(`_inbox/`, `raw/`, `wiki/`, `reading-grids/`) : c'est la frontière de confinement
côté git (le ledger `.wiki/` et le verrou en sont exclus de fait, n'étant pas dans
le scope). Toutes les commandes passent par le runner injectable de `SyncBackend`.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from .base import SyncBackend

_DEFAULT_SCOPE = ["_inbox", "raw", "wiki", "reading-grids", ".mimir"]
_NOTHING_TO_COMMIT = ("nothing to commit", "nothing added to commit", "no changes added")


class GitBackend(SyncBackend):
    """Synchro via un dépôt git (`origin` distant), scope limité aux sous-arbres Mimir."""

    name = "git"

    def __init__(self, cfg, runner=None):
        super().__init__(cfg, runner)
        conf = (getattr(cfg, "sync", {}) or {}).get("git", {}) or {}
        self.repo_root = Path(os.path.expanduser(conf.get("repo_root") or str(cfg.work_root)))
        self.branch = conf.get("branch", "main")
        self.scope = conf.get("scope") or list(_DEFAULT_SCOPE)
        self.bin = conf.get("bin", "git")

    # --- exécution git ------------------------------------------------------
    def _git(self, *args: str):
        return self._run([self.bin, "-C", str(self.repo_root), *args])

    # --- interface ----------------------------------------------------------
    def pull(self) -> int:
        """Rebase le local sur l'état distant avant compilation."""
        return self._git("pull", "--rebase", "origin", self.branch).returncode

    def push(self) -> int:
        """Stage le scope, commit (si nouveauté) et publie sur `origin`."""
        existing = [p for p in self.scope if (self.repo_root / p).exists()]
        if existing:
            added = self._git("add", "--", *existing)
            if added.returncode != 0:
                return added.returncode

        commit = self._git("commit", "-m", f"auto(wiki): sync {date.today().isoformat()}")
        if commit.returncode != 0:
            out = (commit.stdout or "") + (commit.stderr or "")
            if not any(token in out for token in _NOTHING_TO_COMMIT):
                return commit.returncode  # échec réel (conflit d'index, hook, etc.)
            # « rien à committer » : pas une erreur ; on tente quand même un push
            # (des commits antérieurs peuvent rester à publier).

        return self._git("push", "origin", self.branch).returncode

    def validate(self) -> int:
        """0 si `HEAD` local == `origin/<branch>` après fetch, sinon non-zéro."""
        if self._git("fetch", "origin", self.branch).returncode != 0:
            return 1
        local = self._git("rev-parse", "HEAD")
        remote = self._git("rev-parse", f"origin/{self.branch}")
        if local.returncode != 0 or remote.returncode != 0:
            return 1
        return 0 if local.stdout.strip() == remote.stdout.strip() else 1
