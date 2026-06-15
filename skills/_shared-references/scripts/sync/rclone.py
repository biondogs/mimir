#!/usr/bin/env python3
"""Backend de synchro **rclone** — bisync WebDAV (kDrive) durci (SPEC §10).

Battle-tested sur kDrive WebDAV (cf. `wiki-sync/references/RCLONE_KDRIVE.md`) :

- `--size-only` est **obligatoire** : le WebDAV kDrive n'expose ni modtime ni
  hash fiable, donc `--checksum`/`--compare modtime` provoquent des suppressions
  ping-pong. `--size-only` est la seule comparaison viable.
- `pull` et `push` lancent le **même** `bisync` bidirectionnel (réconciliation
  en une passe). Dans une chaîne extract→ingest→grid→index, utiliser
  `--skip-sync` pour éviter de réconcilier plusieurs fois.

**Durcissements (spec `sync-vault` — backend rclone durci) :**

1. **Warm-up listing** avant le bisync (`rclone lsf <remote> --recursive`,
   résultat ignoré) — force kDrive à rafraîchir son cache de listing pour
   qu'aucun fichier entrant ne soit manqué au premier passage.
2. **Retry borné + backoff exponentiel** sur erreurs transitoires (5xx,
   timeout, connection refused, etc.) — aujourd'hui aucun retry, les 5xx
   remontaient tels quels.
3. **Self-heal `bisync --resync` (union sans perte)** quand bisync signale
   « must run --resync » (état perdu). **PAS** de `rclone sync` unidirectionnel
   destructif comme repli silencieux par défaut (il pouvait effacer côté
   distant des fichiers entrants au lieu de les récupérer). `--resync` union
   est bidirectionnel, donc les suppressions restent propagées ; s'il échoue,
   le backend renvoie non-zéro et laisse l'opérateur trancher.

- **Auth depuis le JSON** : si `sync.rclone.remote_setup` est fourni, le
  backend crée/répare le remote rclone (`config create`/`update`) avant la
  synchro, le mot de passe étant lu dans la variable d'environnement nommée
  par `pass_env` (jamais stocké dans le JSON). Un remote présent voit son
  secret rafraîchi (auto-réparation du 401).
- `validate` est **anti-listing-stale** : `bisync` renvoie 0 même quand le
  listing kDrive est périmé et que rien n'a transféré → on compare le nombre
  de fichiers local vs `rclone lsf`, et on **re-réconcilie** (warm-up + bisync
  avec self-heal) si l'écart dépasse la tolérance.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .base import SyncBackend

#: Sous-chaînes (minuscules) signalant qu'un `--resync` est requis.
_RESYNC_MARKERS = ("must run --resync", "bisync aborted", "cannot find prior listing")

#: Sous-chaînes (minuscules) de bruit **cosmétique** (spec `sync-vault` —
#: Cosmetic bisync warnings are not failures). Empirique kDrive : `rclone
#: bisync` émet `failed to check as hash is missing` sur les fichiers
#: fraîchement créés pour lesquels aucun cache de hash n'existe encore. La
#: copie a déjà eu lieu (exit 0, fichiers présents au distant) — ce sont des
#: **logs**, pas des erreurs. On les **agrège** en une seule entrée de
#: `self.warnings` (avec compte).
_COSMETIC_MARKERS = ("failed to check as hash is missing",)

#: Patterns (minuscules) d'erreurs **transitoires** (retry avec backoff).
_TRANSIENT_PATTERNS = (
    "5xx", "timeout", "timed out", "temporarily unavailable", "try again",
    "connection reset", "connection refused", "i/o timeout", "deadline exceeded",
    "service unavailable", "bad gateway", "gateway timeout", "network is unreachable",
)

#: Dossiers de service jamais synchronisés (approximation pour le comptage local).
_IGNORED_DIRS = {".git", ".obsidian", ".wiki", ".rclone-bisync"}

#: Paramètres de retry (borné + backoff exponentiel). Opt-in par défaut — le
#: repli silencieux par défaut est supprimé (cf. spec), donc ces retries sont
#: le seul filet sur erreurs transitoires.
_MAX_RETRIES = 3
_INITIAL_BACKOFF_S = 1.0
_BACKOFF_FACTOR = 2.0


class RcloneBackend(SyncBackend):
    """Synchro bidirectionnelle via `rclone bisync --size-only` + garde-fous kDrive."""

    name = "rclone"

    def __init__(self, cfg, runner=None):
        super().__init__(cfg, runner)
        conf = (getattr(cfg, "sync", {}) or {}).get("rclone", {}) or {}
        self.local = Path(cfg.work_root)
        self.remote = conf.get("remote")
        self.bin = conf.get("bin", "rclone")
        self.max_delete = str(conf.get("max_delete", 25))
        self.filters = self._resolve_filters(conf.get("filters", "filters.txt"))
        #: **Obsolète** : le self-heal `bisync --resync` (union) est désormais
        #: **inconditionnel** (spec `sync-vault` — modification du backend
        #: rclone). La clé est **conservée** dans le config pour ne pas casser
        #: les configs existants, mais n'a plus d'effet (toujours self-heal).
        self.auto_resync = bool(conf.get("auto_resync", False))
        #: Création/réparation du remote rclone depuis le JSON+env (opt-in) ;
        #: secret hors JSON.
        self.remote_setup = conf.get("remote_setup") or None
        #: Garde-fou : ne (re)configure le remote qu'une fois par instance de backend.
        self._remote_ensured = False
        #: Messages non bloquants à remonter à l'appelant (ex. « --resync réussi »).
        #: **Agrégeés** : une entrée par classe d'événement (jamais une par
        #: ligne de log brute).
        self.warnings: list[str] = []

    def _resolve_filters(self, value) -> Path | None:
        if not value:
            return None
        p = Path(os.path.expanduser(value))
        return p if p.is_absolute() else (self.local / p)

    # --- construction des commandes ----------------------------------------
    def _bisync_argv(self) -> list[str]:
        argv = [
            self.bin, "bisync", str(self.local), self.remote,
            "--size-only", "--conflict-loser", "pathname",
            "--conflict-suffix", "sync-conflict", "--max-delete", self.max_delete,
            "--resilient", "--recover", "--copy-links",
        ]
        if self.filters:
            argv += ["--filter-from", str(self.filters)]
        return argv

    def _resync_argv(self) -> list[str]:
        """Amorçage bisync : mêmes flags que `_bisync_argv` **+ `--resync`**.

        Pas de `--resync-mode` (qui désignerait un gagnant) : on conserve
        `--conflict-loser pathname --conflict-suffix sync-conflict`, donc un
        fichier divergent des deux côtés produit **deux copies** au lieu d'un
        écrasement silencieux — union sans perte (SPEC §10).
        """
        return self._bisync_argv() + ["--resync"]

    def _warmup_argv(self) -> list[str]:
        """Warm-up listing : force kDrive à rafraîchir son cache (résultat jeté)."""
        return [self.bin, "lsf", self.remote, "--recursive"]

    # --- auth : (re)configuration du remote depuis le JSON+env --------------
    def _ensure_remote(self) -> None:
        """Crée/répare le remote rclone à partir de `remote_setup` (opt-in, idempotent).

        Le mot de passe ne vit **jamais** dans le JSON : `pass_env` nomme la
        variable d'environnement qui le porte. Un remote absent est créé ; un
        remote présent voit son mot de passe rafraîchi (auto-réparation du
        401). Sans `remote_setup`, le remote est présumé déjà configuré à la
        main (comportement historique).
        """
        if self._remote_ensured or not self.remote_setup or not self.remote:
            return
        self._remote_ensured = True  # une seule tentative par cycle
        name = self.remote.split(":", 1)[0]
        if not name:
            return
        setup = self.remote_setup
        pass_env = setup.get("pass_env")
        password = os.environ.get(pass_env) if pass_env else None
        if not password:
            self.warnings.append(
                f"remote_setup actif mais le mot de passe (env {pass_env!r}) est absent : "
                "remote rclone non (re)configuré — la synchro peut échouer en 401."
            )
            return
        listed = self._run([self.bin, "listremotes"])
        existing = {ln.rstrip(":") for ln in (listed.stdout or "").splitlines() if ln.strip()}
        if name in existing:
            argv = [self.bin, "config", "update", name, "pass", password,
                    "--obscure", "--non-interactive"]
        else:
            argv = [self.bin, "config", "create", name, "webdav",
                    "url", setup.get("url", ""),
                    "vendor", setup.get("vendor", "other"),
                    "user", setup.get("user", ""),
                    "pass", password, "--obscure", "--non-interactive"]
        res = self._run(argv)
        if res.returncode != 0:
            # ne jamais relayer le secret : on remonte seulement le code et le nom du remote.
            self.warnings.append(
                f"(re)configuration du remote rclone {name!r} échouée (rc={res.returncode})."
            )

    # --- bruit cosmétique rclone (spec `sync-vault`) ------------------------
    def _count_cosmetic_lines(self, *results) -> int:
        """Compte les occurrences de marqueurs cosmétiques dans stdout/stderr cumulés."""
        if not results:
            return 0
        blob = "\n".join((r.stdout or "") + (r.stderr or "") for r in results).lower()
        return sum(blob.count(m) for m in _COSMETIC_MARKERS)

    def _record_cosmetic(self, count: int) -> None:
        if count <= 0:
            return
        self.warnings.append(
            f"{count} cosmetic bisync warning(s) ignored (hash missing) — non-blocking."
        )

    # --- détection transitoire + retry/backoff (spec `sync-vault`) ----------
    @staticmethod
    def _is_transient_text(text: str) -> bool:
        return any(p in text for p in _TRANSIENT_PATTERNS)

    def _run_with_retry(self, argv: list[str], label: str) -> tuple[int, Any]:
        """Exécute `argv` avec retry borné + backoff exponentiel **sur erreur transitoire**.

        - `label` est utilisé pour les warnings (« bisync » / « bisync --resync »).
        - Retourne `(rc, last_result)` : `rc = 0` au premier succès, sinon le
          dernier code retour ; `last_result` est l'objet `CompletedProcess`-like
          du dernier essai (pour inspection cosmétique, stdout/stderr).
        - Erreur non transitoire → pas de retry, propagation immédiate.
        """
        delay = _INITIAL_BACKOFF_S
        last_res: Any = None
        for attempt in range(1, _MAX_RETRIES + 1):
            res = self._run(argv)
            last_res = res
            if res.returncode == 0:
                if attempt > 1:
                    self.warnings.append(
                        f"{label} réussi après {attempt} tentative(s) (backoff)."
                    )
                return 0, res
            out = ((res.stdout or "") + (res.stderr or "")).lower()
            if not self._is_transient_text(out):
                # Erreur permanente (config, fichier introuvable, etc.) : pas de retry.
                return res.returncode, res
            if attempt < _MAX_RETRIES:
                self.warnings.append(
                    f"{label} tentative {attempt}/{_MAX_RETRIES} échouée (transient) — "
                    f"retry dans {delay:.0f}s."
                )
                time.sleep(delay)
                delay *= _BACKOFF_FACTOR
        self.warnings.append(f"{label} abandonné après {_MAX_RETRIES} tentatives.")
        return 1, last_res

    # --- réconciliation -----------------------------------------------------
    def _reconcile(self) -> int:
        self._ensure_remote()
        # 1) Warm-up listing : force kDrive à rafraîchir son cache (résultat ignoré).
        self._run(self._warmup_argv())
        # 2) Bisync avec retry borné + backoff sur erreurs transitoires
        #    (`_run_with_retry` retry en interne, ne revient ici qu'avec
        #    `rc = 0` ou un échec **non-transitoire** ou un marqueur resync).
        rc, last_res = self._run_with_retry(self._bisync_argv(), "bisync")
        if rc == 0:
            # exit 0 = succès : les éventuelles lignes cosmétiques restent du
            # **bruit** (spec `sync-vault`). On les agrège pour le digest.
            self._record_cosmetic(self._count_cosmetic_lines(last_res))
            return 0
        # 3) Échec. Distinguer marqueur resync → self-heal via `--resync`
        #    (union, sans perte, bidirectionnel — suppressions propagées) vs
        #    erreur permanente → propagation du code retour.
        out = ((last_res.stdout or "") + (last_res.stderr or "")).lower()
        if any(m in out for m in _RESYNC_MARKERS):
            # Self-heal via `--resync` (lui-même retry/backoff sur transient).
            rs_rc, _ = self._run_with_retry(self._resync_argv(), "bisync --resync")
            if rs_rc == 0:
                self.warnings.append(
                    "bisync sans état antérieur : état amorcé via `bisync --resync` "
                    "(union sans perte, bidirectionnel — suppressions conservées)."
                )
                return 0
            # 4) `--resync` échoué : **PAS** de repli `rclone sync` unidirectionnel
            #    destructif silencieux (spec `sync-vault`). On propage l'échec —
            #    l'opérateur tranche, l'état local est préservé.
            self.warnings.append(
                "`bisync --resync` (union) a échoué — aucun repli `rclone sync` "
                "destructif n'est tenté (l'état local est préservé)."
            )
            return 1
        # Pas de marqueur resync : erreur permanente (config, fichier, etc.)
        # → propagation directe du code retour. Pas de retry supplémentaire
        # (le retry transient a déjà eu lieu dans `_run_with_retry`).
        return rc

    def pull(self) -> int:
        return self._reconcile()

    def push(self) -> int:
        return self._reconcile()

    # --- validation anti-stale ---------------------------------------------
    def _local_count(self) -> int:
        n = 0
        for root, dirs, files in os.walk(self.local):
            dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
            n += sum(1 for f in files if f != ".DS_Store")
        return n

    def _remote_count(self) -> int | None:
        res = self._run([self.bin, "lsf", self.remote, "--recursive", "--files-only"])
        if res.returncode != 0:
            return None
        return sum(1 for line in (res.stdout or "").splitlines() if line.strip())

    def validate(self, *, tolerance: int = 5) -> int:
        """Compare les comptages local/distant ; re-réconcilie si l'écart est grand.

        Recovery : `_reconcile()` (warm-up + bisync + self-heal union). Pas de
        `rclone sync` unidirectionnel destructif (cf. spec `sync-vault`).
        """
        local = self._local_count()
        remote = self._remote_count()
        if remote is None:
            return 1
        if abs(local - remote) <= tolerance:
            return 0
        # listing potentiellement stale : re-réconcilier puis revérifier.
        self._reconcile()
        remote = self._remote_count()
        if remote is None:
            return 1
        return 0 if abs(local - remote) <= tolerance else 1
