#!/usr/bin/env python3
"""Backend de synchro **auto** — Obsidian d'abord, repli rclone durci (SPEC §10).

`get_backend(cfg)` (quand `sync.backend == "auto"`) renvoie une instance de
cette classe. À la **construction**, le backend :

1. **Sonde Obsidian** (`GET {base_url}/`) via le client HTTP injectable.
2. Si KO et `auto_launch = true` et un **affichage** est disponible (macOS
   toujours, Linux : `$DISPLAY`/`$WAYLAND_DISPLAY`) : **lance** Obsidian puis
   **poll readiness** jusqu'à `ready_timeout_s`.
3. **Décide** du sous-backend actif :
   - Obsidian joignable (ou lancé avec succès) → `ObsidianBackend`.
   - Sinon → `RcloneBackend` (repli durci, warm-up + retry + self-heal union).
4. **Bascule intra-run** : si la sous-backend actif est Obsidian et que
   `validate` échoue (écart local/distant), **exécute rclone dans le même
   run** (push + re-validate) — résout l'erreur sans intervention.

**Stand-down** : quand `auto` délègue à Obsidian, rclone n'est jamais appelé
**en parallèle** (le verrou global couvre déjà Mimir-vs-Mimir ; ici, en
complément, Obsidian-vs-rclone est sérialisé au sein du run). Le mode
`auto` **n'ouvre pas deux synchroniseurs** — le fallback est séquentiel.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import SyncBackend


class AutoBackend(SyncBackend):
    """Mode `auto` : Obsidian d'abord, rclone en repli, bascule intra-run."""

    name = "auto"

    def __init__(
        self,
        cfg,
        runner: Callable[..., Any] | None = None,
        *,
        http_client: Callable[..., tuple[int, str]] | None = None,
    ):
        super().__init__(cfg, runner)
        # Import paresseux : évite tout import circulaire, et garde `auto`
        # importable même si `obsidian` ou `rclone` a un bug (les autres
        # backends restent sélectionnables).
        from .obsidian import ObsidianBackend
        from .rclone import RcloneBackend

        self._http = http_client
        # Sous-backend Obsidian (toujours créé : sert aussi à la sonde/au launch).
        self._obsidian = ObsidianBackend(cfg, runner, http_client=self._http)

        # Décision à la construction — *un seul* sous-backend par run.
        ready = self._obsidian.probe()
        if not ready and self._obsidian.auto_launch:
            launched = self._obsidian.launch()
            if launched == 0:
                ready = self._obsidian.wait_until_ready()
            # lancement KO / timeout readiness : on dégrade silencieusement
            # vers rclone (cf. requirement `auto-launch`, scénario « timeout »).
        self.warnings: list[str] = []
        if ready:
            self._active: SyncBackend = self._obsidian
        else:
            self._active = RcloneBackend(cfg, runner)
            self.warnings.append(
                "Obsidian indisponible (et non lançable) — repli sur rclone durci."
            )
        # rclone de repli (création paresseuse : on n'instancie le second
        # backend que si on en a vraiment besoin, c-à-d si `_active` est
        # Obsidian et que `validate` échoue).
        self._rclone_fallback: SyncBackend | None = None

    # --- helpers internes ---------------------------------------------------
    def _rclone(self) -> SyncBackend:
        """Rclone de repli : instance paresseuse, sauf si `_active` EST déjà
        rclone (Obsidian indisponible) — alors on le réutilise directement.
        """
        from .rclone import RcloneBackend

        if isinstance(self._active, RcloneBackend):
            return self._active
        if self._rclone_fallback is None:
            self._rclone_fallback = RcloneBackend(self.cfg, self._runner)
        return self._rclone_fallback

    # --- interface SyncBackend ----------------------------------------------
    def pull(self) -> int:
        return self._active.pull()

    def push(self) -> int:
        return self._active.push()

    def validate(self) -> int:
        """Bascule intra-run : KO d'Obsidian ⇒ rclone.push() + re-validate."""
        from .obsidian import ObsidianBackend

        rc = self._active.validate()
        if rc == 0:
            return 0
        if not isinstance(self._active, ObsidianBackend):
            # On est déjà sur rclone, on ne bascule pas sur soi-même.
            return rc
        # Obsidian a échoué son validate → on tente rclone dans le même run.
        rclone = self._rclone()
        prc = rclone.push()
        vrc = rclone.validate()
        self.warnings.append(
            f"Obsidian validate KO (rc={rc}) — bascule rclone intra-run "
            f"(push rc={prc}, validate rc={vrc})."
        )
        if prc != 0:
            return prc
        return vrc
