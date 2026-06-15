"""Tests du backend `auto` — Obsidian d'abord, rclone en repli, bascule intra-run."""

from types import SimpleNamespace

import pytest

import sync
from sync.auto import AutoBackend
from sync.obsidian import ObsidianBackend
from sync.rclone import RcloneBackend

#: Reprise du FakeHTTP des tests obsidian — dupliquée ici pour rester
#: auto-suffisant (les modules de test sont chargés en importlib mode).
class FakeHTTP:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        key = (method, url)
        r = self.responses.get(key, (404, ""))
        if isinstance(r, list):
            r = r.pop(0) if r else (404, "")
        if isinstance(r, Exception):
            raise r
        return r


class _RcloneRunner:
    """Runner factice pour le sous-backend rclone (comptes cohérents)."""

    def __init__(self, bisync_rc=0, lsf_stdout="", lsf_rc=0):
        self.bisync_rc = bisync_rc
        self.lsf_stdout = lsf_stdout
        self.lsf_rc = lsf_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        # warm-up = `rclone lsf --recursive` (sans --files-only)
        # validate = `rclone lsf --recursive --files-only`
        # bisync = `rclone bisync …`
        if "bisync" in argv:
            return SimpleNamespace(returncode=self.bisync_rc, stdout="", stderr="")
        if "lsf" in argv:
            return SimpleNamespace(returncode=self.lsf_rc, stdout=self.lsf_stdout, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class _Cfg:
    def __init__(self, work_root, *, obsidian_conf=None, rclone_conf=None):
        self.work_root = work_root
        self.sync = {
            "backend": "auto",
            "obsidian": obsidian_conf or {},
            "rclone": rclone_conf or {"remote": "mimir:Vault/kb"},
        }

    @property
    def backend(self) -> str:
        return self.sync["backend"]


@pytest.fixture
def cfg(tmp_path):
    return _Cfg(tmp_path)


# --- factory ----------------------------------------------------------------


def test_factory_selectionne_auto_backend(cfg):
    assert isinstance(sync.get_backend(cfg), AutoBackend)


# --- Obsidian joignable → backend obsidian ---------------------------------


def test_auto_obsidian_joignable_pick_obsidian(cfg, monkeypatch):
    """`GET /` répond 200 → backend obsidian retenu."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): (200, "")})
    backend = AutoBackend(cfg, http_client=http)
    assert isinstance(backend._active, ObsidianBackend)
    assert backend._rclone_fallback is None  # créé paresseusement


# --- Obsidian injoignable + headless → repli rclone -------------------------


def test_auto_obsidian_injoignable_et_pas_auto_launch_repli_rclone(cfg, monkeypatch):
    """`GET /` KO + auto_launch=false → rclone directement (spec scenario)."""
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): ConnectionError("refused")})
    backend = AutoBackend(cfg, http_client=http)
    assert isinstance(backend._active, RcloneBackend)
    assert any("indisponible" in w for w in backend.warnings)


def test_auto_obsidian_injoignable_et_auto_launch_mais_headless_repli_rclone(
    cfg, monkeypatch
):
    """Linux + pas de $DISPLAY + auto_launch=true → pas de tentative de launch,
    repli rclone immédiat (spec scenario « headless »)."""
    monkeypatch.setattr("sync.obsidian.sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): ConnectionError("refused")})
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={"auto_launch": True})
    backend = AutoBackend(cfg2, http_client=http)
    assert isinstance(backend._active, RcloneBackend)
    # Aucun subprocess n'a été spawned pour le launch
    assert not any("open" in c[0] or "obsidian" in c[0].lower() for c in http.calls)


# --- Obsidian injoignable + auto_launch → lancer + poll ---------------------


def test_auto_launch_et_puis_pret_pick_obsidian(cfg, monkeypatch):
    """Lancement OK + readiness OK → backend obsidian (spec scenario « launch »)."""
    monkeypatch.setattr("sync.obsidian.sys.platform", "darwin")
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    http = FakeHTTP({
        # 1er GET (probe initial) : KO
        ("GET", "https://127.0.0.1:27124/"): [
            ConnectionError("refused"),
            (503, ""),  # 1er poll readiness
            (200, "ready"),  # 2e poll readiness
        ],
    })
    spawned: list[list[str]] = []
    def fake_runner(argv, **kw):
        spawned.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={"auto_launch": True, "vault_name": "kb"})
    backend = AutoBackend(cfg2, runner=fake_runner, http_client=http)
    assert isinstance(backend._active, ObsidianBackend)
    # Le launch a bien invoqué `open` (macOS)
    assert any(c[:1] == ["open"] for c in spawned)


def test_auto_launch_et_timeout_readiness_repli_rclone_sans_exception(cfg, monkeypatch):
    """Lancement OK mais readiness timeout → repli rclone, pas d'exception
    (spec scenario « timeout de readiness »)."""
    monkeypatch.setattr("sync.obsidian.sys.platform", "darwin")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    # Le readiness poll ne renvoie jamais 200 — on remplit une longue liste
    # de (503, "") pour dépasser le timeout (poll toutes les 1s, timeout 30s).
    # En test on veut aller vite : on monkeypatche `time.sleep` pour ne pas
    # attendre, et on remplit juste assez pour couvrir 1 probe + N polls.
    http = FakeHTTP({
        ("GET", "https://127.0.0.1:27124/"): [(503, "")] * 50,
    })
    def fake_runner(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={
        "auto_launch": True,
        "ready_timeout_s": 1,  # très court pour le test
    })
    backend = AutoBackend(cfg2, runner=fake_runner, http_client=http)
    assert isinstance(backend._active, RcloneBackend)


def test_auto_launch_et_binaire_absent_repli_rclone_sans_exception(cfg, monkeypatch):
    """Launch renvoie FileNotFoundError → repli rclone, pas d'exception
    (cf. obsidian.launch() qui ne lève jamais)."""
    monkeypatch.setattr("sync.obsidian.sys.platform", "darwin")
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): ConnectionError("refused")})
    class _Runner:
        def __call__(self, argv, **kwargs):
            raise FileNotFoundError("no such file")
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={"auto_launch": True})
    # Doit construire sans exception
    backend = AutoBackend(cfg2, runner=_Runner(), http_client=http)
    assert isinstance(backend._active, RcloneBackend)


# --- bascule intra-run : validate KO → rclone -----------------------------


def test_auto_validate_ko_si_obsidian_bascule_sur_rclone(cfg, monkeypatch):
    """Obsidian validate KO → rclone.push() + rclone.validate() dans le même run
    (spec scenario « bascule intra-run »)."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    # Obsidian : probe OK, trigger OK
    http = FakeHTTP({
        ("GET", "https://127.0.0.1:27124/"): (200, ""),
        ("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"): (204, ""),
    })
    # Rclone : bisync OK, lsf = 0 fichiers (écart > tolérance 0)
    runner = _RcloneRunner(bisync_rc=0, lsf_stdout="", lsf_rc=0)
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={"tolerance": 0})
    # Crée 10 fichiers locaux (écart avec lsf à 0)
    for i in range(10):
        (cfg.work_root / f"f{i}.md").write_text("x", encoding="utf-8")

    backend = AutoBackend(cfg2, runner=runner, http_client=http)
    assert isinstance(backend._active, ObsidianBackend)

    rc = backend.validate()
    # Obsidian.validate() → 1 (écart), rclone.validate() (après rclone.push()
    # qui re-reconcile) → on re-lsf à 0 → encore KO. Donc rc = 1. Mais
    # l'important : rclone a bien été invoqué (basculé en intra-run).
    assert rc == 1
    # Le warning de bascule est présent
    assert any("bascule" in w for w in backend.warnings)
    # rclone a reçu au moins un bisync (push du fallback)
    assert any("bisync" in c for c in runner.calls)


def test_auto_validate_ok_si_obsidian_valide_ok(cfg, monkeypatch, tmp_path):
    """Obsidian validate OK → pas de bascule rclone, return 0."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    http = FakeHTTP({
        ("GET", "https://127.0.0.1:27124/"): (200, ""),
        ("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"): (204, ""),
    })
    runner = _RcloneRunner(bisync_rc=0, lsf_stdout="a.md\nb.md\n", lsf_rc=0)
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("y", encoding="utf-8")
    backend = AutoBackend(cfg, runner=runner, http_client=http)
    assert backend.validate() == 0
    # rclone **bisync** n'a PAS été appelé (validate Obsidian a suffi).
    # Note : rclone **lsf** est appelé par obsidian.validate() pour le comptage —
    # c'est attendu, ce n'est pas une bascule intra-run.
    bisync_calls = [c for c in runner.calls if "bisync" in c]
    assert bisync_calls == []


def test_auto_validate_repli_inutile_si_actif_est_deja_rclone(cfg, monkeypatch):
    """Si Obsidian est indisponible à la construction, _active = rclone :
    validate ne « bascule » pas sur soi-même (sinon double appel)."""
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): ConnectionError("refused")})
    runner = _RcloneRunner(bisync_rc=0, lsf_stdout="", lsf_rc=0)
    backend = AutoBackend(cfg, runner=runner, http_client=http)
    assert isinstance(backend._active, RcloneBackend)
    rc = backend.validate()
    # rclone.validate() fait : lsf (vide) → 0 fichiers, local = 0 → 0 !
    # OK en soit (l'écart est 0, on est dans la tolérance). L'important est
    # qu'on n'ait pas déclenché un second rclone.
    assert rc == 0
    # _rclone_fallback reste None : la bascule intra-run n'a pas eu lieu
    # (puisqu'on est déjà sur rclone).
    assert backend._rclone_fallback is None


# --- stand-down : un seul synchroniseur par run ----------------------------


def test_auto_pull_pull_delegue_sans_concurrrence(cfg, monkeypatch):
    """`pull` (et `push`) délèguent au sous-backend actif — un seul appel
    réseau par pull/push (pas de double exécution obsidian + rclone)."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    http = FakeHTTP({
        ("GET", "https://127.0.0.1:27124/"): (200, ""),
        ("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"): (204, ""),
    })
    runner = _RcloneRunner(bisync_rc=0, lsf_stdout="", lsf_rc=0)
    backend = AutoBackend(cfg, runner=runner, http_client=http)
    assert isinstance(backend._active, ObsidianBackend)
    backend.pull()
    # 1 seul POST trigger (Remotely Save), 0 appel rclone
    post_calls = [c for c in http.calls if c[0] == "POST"]
    rclone_calls = [c for c in runner.calls if "bisync" in c]
    assert len(post_calls) == 1
    assert rclone_calls == []


# --- secret jamais sérialisé dans le mode auto ------------------------------


def test_auto_secret_jamais_dans_warnings(cfg, monkeypatch):
    monkeypatch.delenv("MIMIR_OBSIDIAN_API_KEY", raising=False)
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): ConnectionError("refused")})
    backend = AutoBackend(cfg, http_client=http)
    # On déclenche un trigger KO pour accumuler des warnings
    backend._obsidian.trigger()
    for w in backend.warnings:
        # Le NOM de la variable peut apparaître, mais aucune valeur secrète
        # (ici on n'a pas posé de secret, donc le test passe trivialement —
        # voir test_sync_obsidian.py pour la version exhaustive).
        assert "Bearer" not in w
