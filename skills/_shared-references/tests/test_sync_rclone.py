"""Tests du backend rclone — 100 % hermétiques (runner factice, aucun binaire rclone)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import sync
from sync.rclone import RcloneBackend


class FakeRunner:
    """Runner injectable : réponses par sous-commande rclone (`bisync`/`sync`/`lsf`)."""

    def __init__(self, responses=None):
        # responses[sub] = (rc, stdout, stderr) ou une liste consommée dans l'ordre
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        sub = argv[1] if len(argv) > 1 else ""
        r = self.responses.get(sub, (0, "", ""))
        if isinstance(r, list):
            r = r.pop(0) if r else (0, "", "")
        rc, out, err = r
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def subcommands(self) -> list[str]:
        return [c[1] for c in self.calls if len(c) > 1]


@pytest.fixture
def cfg(tmp_path):
    class Cfg:
        work_root = tmp_path
        sync = {
            "backend": "rclone",
            "rclone": {"remote": "mimir:Vault/kb", "filters": "filters.txt"},
        }
        backend = "rclone"

    (tmp_path / "filters.txt").write_text("+ **\n", encoding="utf-8")
    return Cfg()


def test_factory_selectionne_rclone(cfg):
    assert isinstance(sync.get_backend(cfg), RcloneBackend)


def test_pull_construit_bisync_size_only(cfg):
    runner = FakeRunner({"bisync": (0, "", ""), "lsf": (0, "", "")})
    assert sync.pull(cfg, runner=runner) == 0
    # 1er appel = warm-up lsf, 2e = bisync
    assert runner.subcommands()[0] == "lsf"
    bisync_argv = runner.calls[1]
    assert bisync_argv[:2] == ["rclone", "bisync"]
    assert "mimir:Vault/kb" in bisync_argv
    assert "--size-only" in bisync_argv
    assert "--filter-from" in bisync_argv
    assert bisync_argv[bisync_argv.index("--filter-from") + 1].endswith("filters.txt")


def test_resync_requis_self_heal_via_resync_union(cfg):
    """Marqueur resync → self-heal `bisync --resync` (union sans perte, **PAS**
    de repli `rclone sync` unidirectionnel destructif silencieux)."""
    runner = FakeRunner({
        "bisync": [(1, "", "Bisync critical error: Must run --resync to recover."), (0, "", "")],
    })
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    # 1) warm-up (lsf) 2) bisync échoue 3) bisync --resync réussit
    assert runner.subcommands() == ["lsf", "bisync", "bisync"]
    assert "--resync" in runner.calls[-1]
    assert "sync" not in runner.subcommands()  # PAS de rclone sync unidirectionnel
    assert backend.warnings and "union" in backend.warnings[0].lower()


def test_echec_bisync_non_resync_propage(cfg):
    """Échec permanent (non-transitoire, non-resync) : pas de retry, pas de
    repli — code retour propagé. (Le test historique : le message n'est pas
    transitoire ET pas un marqueur resync → propagé directement.)"""
    runner = FakeRunner({"bisync": (7, "", "network unreachable")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 7                       # pas un cas resync -> propagé
    assert runner.subcommands() == ["lsf", "bisync"]  # warm-up + bisync KO, rien d'autre


# --- self-heal `bisync --resync` (spec : inconditionnel) --------------------
_RESYNC_ERR = "Bisync critical error: Must run --resync to recover."


def _enable(cfg, **opts):
    cfg.sync["rclone"].update(opts)
    return cfg


def test_self_heal_resync_inconditionnel(cfg):
    """Même sans `auto_resync=true` (clé désormais obsolète), le self-heal
    `bisync --resync` (union) est inconditionnel (spec `sync-vault` — modification
    du backend rclone)."""
    runner = FakeRunner({"bisync": [(1, "", _RESYNC_ERR), (0, "", "")]})
    backend = RcloneBackend(cfg, runner=runner)  # pas de auto_resync
    assert backend.pull() == 0
    assert runner.subcommands() == ["lsf", "bisync", "bisync"]
    assert "--resync" in runner.calls[-1]
    assert "sync" not in runner.subcommands()  # pas de repli destructif
    assert backend.warnings and "union" in backend.warnings[0].lower()


def test_auto_resync_cle_obsolete_mais_inoffensive(cfg):
    """La clé `auto_resync: true` est conservée pour ne pas casser les configs
    existants, mais n'a plus d'effet (self-heal inconditionnel)."""
    _enable(cfg, auto_resync=True)
    runner = FakeRunner({"bisync": [(1, "", _RESYNC_ERR), (0, "", "")]})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    assert runner.subcommands() == ["lsf", "bisync", "bisync"]
    assert "sync" not in runner.subcommands()


def test_resync_echoue_propage_sans_sync_silencieux(cfg):
    """Si le `bisync --resync` (union) échoue : on **propage** l'échec, on ne
    tente PAS de `rclone sync` unidirectionnel destructif en repli silencieux
    (spec `sync-vault` — modification du backend rclone)."""
    runner = FakeRunner({
        "bisync": [(1, "", _RESYNC_ERR), (5, "", "resync boom")],
    })
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 1                      # échec propagé (1, pas 0)
    assert runner.subcommands() == ["lsf", "bisync", "bisync"]
    assert "sync" not in runner.subcommands()       # AUCUN sync destructif
    assert any("sync" in w and "destructif" in w for w in backend.warnings)


# --- warm-up listing avant bisync (spec : warm-up de listing) --------------


def test_warmup_listing_avant_bisync(cfg):
    """`rclone lsf <remote> --recursive` est émis AVANT le bisync (résultat
    jetable, sert juste à forcer kDrive à rafraîchir son cache)."""
    runner = FakeRunner({"bisync": (0, "", "")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    assert runner.subcommands()[0] == "lsf"          # warm-up d'abord
    assert runner.calls[0][-1] == "--recursive"     # lsf --recursive
    assert "bisync" in runner.subcommands()


# --- retry borné + backoff sur erreurs transitoires (spec) -----------------


def test_retry_avec_backoff_sur_5xx_transitoire(cfg, monkeypatch):
    """5xx transitoire : retry borné + backoff exponentiel (3 tentatives).
    Au sein des tentatives, un succès renvoie 0."""
    sleeps: list[float] = []
    monkeypatch.setattr("sync.rclone.time.sleep", lambda s: sleeps.append(s))
    # 1er bisync : 503 (transient), 2e : 200
    runner = FakeRunner({
        "bisync": [(503, "", "ERROR : 503 Service Unavailable"),
                   (0, "", "all good")],
    })
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    # 2 bisync seulement (pas de 3e — succès au 2e)
    assert runner.subcommands() == ["lsf", "bisync", "bisync"]
    # Backoff : 1s, 2s, 4s… entre les tentatives
    assert sleeps == [1.0]
    # Warning de retry
    assert any("transient" in w and "1/3" in w for w in backend.warnings)


def test_retry_abandonne_apres_max_tentatives(cfg, monkeypatch):
    """3 tentatives max, puis abandon : propage l'échec (mais PAS de
    `rclone sync` destructif)."""
    monkeypatch.setattr("sync.rclone.time.sleep", lambda s: None)
    runner = FakeRunner({
        "bisync": [
            (503, "", "ERROR : 503 Service Unavailable"),
            (503, "", "ERROR : 503 Service Unavailable"),
            (503, "", "ERROR : 503 Service Unavailable"),
        ],
    })
    # le FakeRunner consomme les réponses dans l'ordre
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 1
    # 1 lsf (warm-up) + 3 bisync (max_retries), puis rien d'autre
    assert runner.subcommands() == ["lsf", "bisync", "bisync", "bisync"]
    assert "sync" not in runner.subcommands()


def test_erreur_non_transitoire_pas_de_retry(cfg, monkeypatch):
    """Erreur permanente (ni resync ni transitoire) : pas de retry, pas de
    fallback — propagation immédiate du code retour."""
    monkeypatch.setattr("sync.rclone.time.sleep", lambda s: None)
    runner = FakeRunner({"bisync": (7, "", "permission denied")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 7
    assert runner.subcommands() == ["lsf", "bisync"]  # 1 seul bisync
    # Pas de sleep (pas de retry)
    # Pas de repli sync


def test_transient_resync_marker_passe_par_retry_resync(cfg, monkeypatch):
    """Marqueur resync + sortie transitoire sur bisync : on retente avec
    `--resync` (et le `--resync` est lui aussi retry sur transient)."""
    sleeps: list[float] = []
    monkeypatch.setattr("sync.rclone.time.sleep", lambda s: sleeps.append(s))
    runner = FakeRunner({
        "bisync": [
            (1, "", "Bisync critical error: Must run --resync to recover."),
            # 1er --resync : transitoire (503)
            (503, "", "ERROR : 503 Service Unavailable"),
            # 2e --resync : OK
            (0, "", "all good"),
        ],
    })
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    # warm-up lsf + 1er bisync (resync marker) + 1er --resync (503) + 2e --resync (OK)
    assert runner.subcommands() == ["lsf", "bisync", "bisync", "bisync"]
    # Le 1er bisync (calls[1]) est SANS --resync (le bisync initial) ;
    # les 2 suivants (calls[2..3]) sont avec --resync.
    assert "--resync" not in runner.calls[1]
    assert "--resync" in runner.calls[2]
    assert "--resync" in runner.calls[3]
    assert sleeps == [1.0]


# --- auth : (re)configuration du remote depuis le JSON+env -----------------
_SETUP = {"url": "https://dav/x", "vendor": "other", "user": "u", "pass_env": "MIMIR_KDRIVE_PASS"}


def test_ensure_remote_cree_si_absent(cfg, monkeypatch):
    monkeypatch.setenv("MIMIR_KDRIVE_PASS", "s3cret")
    _enable(cfg, remote_setup=_SETUP)
    runner = FakeRunner({"listremotes": (0, "autre:\n", ""), "bisync": (0, "", "")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    assert runner.subcommands()[:2] == ["listremotes", "config"]
    cfg_call = runner.calls[1]
    assert cfg_call[1:3] == ["config", "create"] and "mimir" in cfg_call
    assert "--obscure" in cfg_call
    # le secret transite par argv mais n'est jamais relayé dans un warning/log
    assert not any("s3cret" in w for w in backend.warnings)


def test_ensure_remote_update_si_present(cfg, monkeypatch):
    monkeypatch.setenv("MIMIR_KDRIVE_PASS", "s3cret")
    _enable(cfg, remote_setup=_SETUP)
    runner = FakeRunner({"listremotes": (0, "mimir:\n", ""), "bisync": (0, "", "")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    assert runner.calls[1][1:3] == ["config", "update"]   # remote présent => répare le 401


def test_ensure_remote_sans_pass_env_warn_et_continue(cfg, monkeypatch):
    monkeypatch.delenv("MIMIR_KDRIVE_PASS", raising=False)
    _enable(cfg, remote_setup=_SETUP)
    runner = FakeRunner({"bisync": (0, "", "")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    assert "config" not in runner.subcommands()           # aucune mutation rclone
    assert backend.warnings and "401" in backend.warnings[0]


def test_ensure_remote_absent_skip(cfg):
    # pas de remote_setup => aucune commande config/listremotes (comportement historique)
    runner = FakeRunner({"bisync": (0, "", ""), "lsf": (0, "", "")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    # warm-up lsf + bisync (pas de listremotes ni config)
    assert runner.subcommands() == ["lsf", "bisync"]
    assert "listremotes" not in runner.subcommands()


def test_validate_ok_quand_comptage_proche(cfg, tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("y", encoding="utf-8")
    runner = FakeRunner({"lsf": (0, "a.md\nb.md\n", "")})
    assert sync.validate(cfg, runner=runner) == 0


def test_validate_repousse_si_ecart(cfg, tmp_path):
    """Listing stale : 10 fichiers locaux, mais le 1er lsf n'en voit aucun.
    Recovery = re-reconcile (warm-up + bisync + self-heal), PAS un `rclone sync`
    unidirectionnel destructif (spec `sync-vault` — modification du backend
    rclone)."""
    for i in range(10):
        (tmp_path / f"f{i}.md").write_text("x", encoding="utf-8")
    # 1er lsf (validate init) : 0 ; 2e lsf (warm-up du reconcile) : 0 ;
    # 3e lsf (validate final) : 10. Le warm-up consomme la 2e réponse.
    runner = FakeRunner({
        "lsf": [
            (0, "", ""),  # validate init
            (0, "", ""),  # warm-up du reconcile
            (0, "\n".join(f"f{i}.md" for i in range(10)), ""),  # validate final
        ],
        "bisync": (0, "", ""),
    })
    assert sync.validate(cfg, runner=runner) == 0
    # a tenté un reconcile (warm-up lsf + bisync) entre les deux lsf « validate »
    assert "bisync" in runner.subcommands()
    # PAS de repli `rclone sync` unidirectionnel destructif
    assert "sync" not in runner.subcommands()


def test_validate_echoue_si_lsf_ko(cfg):
    runner = FakeRunner({"lsf": (1, "", "directory not found")})
    assert sync.validate(cfg, runner=runner) == 1


# --- bruit rclone cosmétique (spec `sync-vault`) ----------------------------
def test_bruit_hash_missing_agregé_et_exit_0(cfg):
    """Un `bisync` qui émet plusieurs lignes `failed to check as hash is missing`
    tout en retournant 0 doit être rapporté comme un succès (rc=0), ET le
    bruit est **agrégé** en une seule entrée de `warnings` (jamais compté
    comme erreur par-fichier dans le digest opérateur)."""
    cosmetic = (
        "2026/06/11 09:00:00 ERROR : foo.md: failed to check as hash is missing\n"
        "2026/06/11 09:00:00 ERROR : bar.md: failed to check as hash is missing\n"
        "2026/06/11 09:00:00 ERROR : baz.md: failed to check as hash is missing\n"
    )
    runner = FakeRunner({"bisync": (0, cosmetic, "")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.push() == 0                          # exit 0 NON basculé en échec
    cosmetic_warnings = [w for w in backend.warnings if "cosmetic" in w]
    assert len(cosmetic_warnings) == 1                  # une seule entrée agrégée
    assert "3" in cosmetic_warnings[0]                  # compte exact
    # la ligne brute « failed to check as hash is missing » n'apparaît pas telle
    # quelle dans les warnings (sinon un digest la compterait comme erreur).
    assert not any("failed to check as hash is missing" in w for w in backend.warnings)


def test_pull_sans_bruit_cosmétique_pas_de_warning(cfg):
    """Sortie rclone propre → pas d'entrée cosmétique dans `warnings`."""
    runner = FakeRunner({"bisync": (0, "all good\n", "")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 0
    assert not any("cosmetic" in w for w in backend.warnings)


def test_erreur_reelle_non_masquee_par_filtre_cosmétique(cfg):
    """Une ligne d'erreur rclone qui n'est PAS dans la liste cosmétique
    propage le code retour ≠ 0 (le filtre cosmétique ne masque rien)."""
    runner = FakeRunner({"bisync": (7, "", "network unreachable")})
    backend = RcloneBackend(cfg, runner=runner)
    assert backend.pull() == 7                          # vraie erreur, pas un cosmétique
    assert not any("cosmetic" in w for w in backend.warnings)
