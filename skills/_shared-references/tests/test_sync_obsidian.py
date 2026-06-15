"""Tests du backend obsidian — 100 % hermétiques (HTTP client factice, aucun réseau)."""

from types import SimpleNamespace

import pytest

import sync
from sync.obsidian import ObsidianBackend, _default_http_client


class FakeHTTP:
    """Client HTTP injectable : `responses[key] = (status, body)` ou une exception.

    `key` est un tuple `(method, url)`. Les clés absentes renvoient `(404, "")`
    par défaut (404 = pas joignable). Les `Exception` levées simulent des
    erreurs réseau (à capturer par `obsidian.probe()`). Une **liste** de
    réponses est consommée dans l'ordre (chaque élément est lui-même un tuple
    ou une exception).
    """

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


class _Cfg:
    """Config minimale ciblant le backend obsidian (rclone remote requis pour validate)."""

    def __init__(self, work_root, *, obsidian_conf=None, rclone_conf=None, backend="obsidian"):
        self.work_root = work_root
        if rclone_conf is None:
            rclone_conf = {"remote": "mimir:Vault/kb"}
        self.sync = {
            "backend": backend,
            "obsidian": obsidian_conf or {},
            "rclone": rclone_conf,
        }

    @property
    def backend(self) -> str:
        return self.sync["backend"]


@pytest.fixture
def cfg(tmp_path):
    return _Cfg(tmp_path, obsidian_conf={
        "base_url": "https://127.0.0.1:27124",
        "api_key_env": "MIMIR_OBSIDIAN_API_KEY",
    })


# --- factory ----------------------------------------------------------------


def test_factory_selectionne_obsidian(cfg):
    assert isinstance(sync.get_backend(cfg), ObsidianBackend)


# --- probe readiness --------------------------------------------------------


def test_probe_ok_si_200(cfg):
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): (200, "Ready")})
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.probe() is True
    assert http.calls[0][0] == "GET"
    assert http.calls[0][1].endswith("/")


def test_probe_ko_si_non_200(cfg):
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): (503, "")})
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.probe() is False


def test_probe_ko_si_erreur_reseau(cfg):
    """`ConnectionError` simulée → probe renvoie False, ne lève pas."""
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): ConnectionError("refused")})
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.probe() is False


# --- trigger Remotely Save --------------------------------------------------


def test_trigger_envoie_post_avec_bearer(cfg, monkeypatch):
    """Le trigger envoie POST /commands/<id>/ avec Authorization Bearer."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "secret-key-XYZ")
    http = FakeHTTP({("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"): (204, "")})
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.trigger() == 0
    method, url, kwargs = http.calls[0]
    assert method == "POST"
    assert url == "https://127.0.0.1:27124/commands/remotely-save:start-sync/"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-key-XYZ"
    assert kwargs["verify_tls"] is False  # cert auto-signé toléré


def test_trigger_echoue_si_cle_absente(cfg, monkeypatch):
    """Clé API absente : warning, return 1, aucun appel HTTP."""
    monkeypatch.delenv("MIMIR_OBSIDIAN_API_KEY", raising=False)
    http = FakeHTTP()
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.trigger() == 1
    assert http.calls == []                          # pas d'appel HTTP
    assert any("MIMIR_OBSIDIAN_API_KEY" in w for w in backend.warnings)
    # Le warning ne contient PAS le secret (juste le nom de la variable)
    assert not any("secret" in w for w in backend.warnings)


def test_trigger_echoue_si_erreur_reseau(cfg, monkeypatch):
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    http = FakeHTTP({
        ("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"):
        ConnectionError("connection refused"),
    })
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.trigger() == 1
    assert any("inaccessible" in w for w in backend.warnings)


def test_trigger_echoue_si_500(cfg, monkeypatch):
    """5xx → return 1, message générique (pas le body de la réponse)."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    http = FakeHTTP({
        ("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"):
        (500, "internal error"),
    })
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.trigger() == 1
    assert any("500" in w for w in backend.warnings)


# --- secret jamais sérialisé (scenario « secret jamais sérialisé ») --------


def test_secret_jamais_dans_repr_ou_warnings(cfg, monkeypatch):
    """La clé API ne doit JAMAIS apparaître dans `repr()`, `warnings`, ou quoi
    que ce soit de sérialisable. Seul le NOM de la variable d'env apparaît."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "ULTRA-SECRET-KEY-12345")
    http = FakeHTTP()
    backend = ObsidianBackend(cfg, http_client=http)

    # 1. `__repr__` ne contient pas la clé
    assert "ULTRA-SECRET-KEY-12345" not in repr(backend)
    # 2. `vars(backend)` ne contient pas la clé
    assert "ULTRA-SECRET-KEY-12345" not in str(vars(backend))
    # 3. le nom de la variable d'env apparaît dans le `repr`
    assert "MIMIR_OBSIDIAN_API_KEY" in repr(backend)

    # 4. trigger puis inspecter les warnings accumulés
    http2 = FakeHTTP({
        ("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"):
        (401, "Unauthorized"),
    })
    backend2 = ObsidianBackend(cfg, http_client=http2)
    backend2.trigger()
    for w in backend2.warnings:
        assert "ULTRA-SECRET-KEY-12345" not in w
    # ...mais le nom de la variable est référencé quand la clé manque
    http3 = FakeHTTP()
    monkeypatch.delenv("MIMIR_OBSIDIAN_API_KEY", raising=False)
    backend3 = ObsidianBackend(cfg, http_client=http3)
    backend3.trigger()
    assert any("MIMIR_OBSIDIAN_API_KEY" in w for w in backend3.warnings)


# --- pull / push / cycle (settle) ------------------------------------------


def test_pull_trigger_et_wait(cfg, monkeypatch):
    """`pull` : trigger + post_sync_wait_s (settle). Aucun `time.sleep` réel
    nécessaire en test — on monkeypatche `time.sleep`."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    sleeps: list[float] = []
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: sleeps.append(s))
    http = FakeHTTP({
        ("GET", "https://127.0.0.1:27124/"): (200, ""),  # readiness
        ("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"): (204, ""),
    })
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.pull() == 0
    # Le settle (post_sync_wait_s = 20 par défaut) est appelé
    assert any(s == 20 for s in sleeps)
    # Trigger bien envoyé
    assert any(c[0] == "POST" and "/commands/" in c[1] for c in http.calls)


def test_pull_court_circuite_si_trigger_ko(cfg, monkeypatch):
    """trigger KO → return immédiat, pas de wait (sinon cycle ralenti pour rien)."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    http = FakeHTTP({
        ("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"):
        (500, "boom"),
    })
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.pull() == 1
    # Warning présent
    assert any("500" in w for w in backend.warnings)


def test_push_equivaut_pull(cfg, monkeypatch):
    """`push` et `pull` déclenchent la même commande (Remotely Save bidirectionnel)."""
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    http = FakeHTTP({
        ("POST", "https://127.0.0.1:27124/commands/remotely-save:start-sync/"): (204, ""),
    })
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.push() == 0
    assert any(c[0] == "POST" and "/commands/" in c[1] for c in http.calls)


# --- validate (réutilise rclone lsf) ---------------------------------------


def test_validate_ok_si_comptage_proche(cfg, monkeypatch, tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("y", encoding="utf-8")
    # rclone runner (subprocess injectable) — voir test_sync_rclone
    fake_run = SimpleNamespace(
        returncode=0, stdout="a.md\nb.md\n", stderr="",
    )
    class _Runner:
        def __call__(self, argv, **kwargs):
            return fake_run
    backend = ObsidianBackend(cfg, runner=_Runner(), http_client=FakeHTTP())
    assert backend.validate() == 0


def test_validate_ko_si_ecart_comptage(cfg, monkeypatch, tmp_path):
    for i in range(10):
        (tmp_path / f"f{i}.md").write_text("x", encoding="utf-8")
    # rclone lsf : 0 fichier (stale) → écart > tolérance
    fake_run = SimpleNamespace(returncode=0, stdout="", stderr="")
    class _Runner:
        def __call__(self, argv, **kwargs):
            return fake_run
    backend = ObsidianBackend(cfg, runner=_Runner(), http_client=FakeHTTP())
    assert backend.validate() == 1


def test_validate_sans_rclone_remote_retourne_1_avec_warning(cfg, tmp_path):
    """Si `sync.rclone.remote` est absent, validate ne peut pas vérifier."""
    cfg2 = _Cfg(tmp_path, obsidian_conf={}, rclone_conf={})  # pas de remote
    backend = ObsidianBackend(cfg2, http_client=FakeHTTP())
    assert backend.validate() == 1
    assert any("remote" in w for w in backend.warnings)


def test_validate_echoue_si_lsf_ko(cfg, tmp_path):
    fake_run = SimpleNamespace(returncode=1, stdout="", stderr="directory not found")
    class _Runner:
        def __call__(self, argv, **kwargs):
            return fake_run
    backend = ObsidianBackend(cfg, runner=_Runner(), http_client=FakeHTTP())
    assert backend.validate() == 1


# --- auto-launch (best-effort) ---------------------------------------------


def test_launch_skip_si_auto_launch_off(cfg, monkeypatch):
    """`auto_launch = false` (défaut) → launch() retourne != 0 sans rien faire."""
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    backend = ObsidianBackend(cfg, http_client=FakeHTTP())
    assert backend.launch() != 0  # pas de commande spawned


def test_launch_skip_si_pas_d_affichage_sous_linux(cfg, monkeypatch):
    """Linux + pas de $DISPLAY → skip launch, pas d'exception."""
    monkeypatch.setattr("sync.obsidian.sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={"auto_launch": True})
    backend = ObsidianBackend(cfg2, http_client=FakeHTTP())
    assert backend.launch() == 2  # code dédié « pas d'affichage »


def test_launch_macos_utilise_open_a(cfg, monkeypatch):
    """macOS + vault_name → URI obsidian://open?vault=…"""
    monkeypatch.setattr("sync.obsidian.sys.platform", "darwin")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={"auto_launch": True, "vault_name": "kb"})
    spawned: list[list[str]] = []
    class _Runner:
        def __call__(self, argv, **kwargs):
            spawned.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
    backend = ObsidianBackend(cfg2, runner=_Runner(), http_client=FakeHTTP())
    assert backend.launch() == 0
    assert spawned and spawned[0][0] == "open"
    assert "obsidian://open?vault=kb" in spawned[0]


def test_launch_macos_sans_vault_utilise_open_a_obsidian(cfg, monkeypatch):
    """macOS + pas de vault_name → `open -a Obsidian`."""
    monkeypatch.setattr("sync.obsidian.sys.platform", "darwin")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={"auto_launch": True})
    spawned: list[list[str]] = []
    class _Runner:
        def __call__(self, argv, **kwargs):
            spawned.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
    backend = ObsidianBackend(cfg2, runner=_Runner(), http_client=FakeHTTP())
    assert backend.launch() == 0
    assert spawned[0] == ["open", "-a", "Obsidian"]


def test_launch_utilise_launch_cmd_si_fourmi(cfg, monkeypatch):
    """`launch_cmd` override la commande par plateforme."""
    monkeypatch.setattr("sync.obsidian.sys.platform", "darwin")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={"auto_launch": True, "launch_cmd": "my-obsidian --foo"})
    spawned: list[list[str]] = []
    class _Runner:
        def __call__(self, argv, **kwargs):
            spawned.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
    backend = ObsidianBackend(cfg2, runner=_Runner(), http_client=FakeHTTP())
    assert backend.launch() == 0
    assert spawned[0] == ["my-obsidian", "--foo"]


def test_launch_binaire_absent_ne_leve_pas(cfg, monkeypatch):
    """Binaire absent (FileNotFoundError du runner) → launch() renvoie 4 sans lever."""
    monkeypatch.setattr("sync.obsidian.sys.platform", "darwin")
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: None)
    cfg2 = _Cfg(cfg.work_root, obsidian_conf={"auto_launch": True})
    class _Runner:
        def __call__(self, argv, **kwargs):
            raise FileNotFoundError("no such file")
    backend = ObsidianBackend(cfg2, runner=_Runner(), http_client=FakeHTTP())
    assert backend.launch() == 4  # pas d'exception propagée


def test_wait_until_ready_poll_jusqua_200(cfg, monkeypatch):
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "k")
    # Premiers GET → 503, puis 200
    http = FakeHTTP({
        ("GET", "https://127.0.0.1:27124/"): [
            (503, ""), (503, ""), (200, "ready"),
        ]
    })
    sleeps: list[float] = []
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: sleeps.append(s))
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.wait_until_ready(timeout=10) is True
    assert len(sleeps) == 2  # poll interval 1s entre les essais


def test_wait_until_ready_timeout(cfg, monkeypatch):
    """Aucun GET ne répond 200 dans le délai → False (et dégrade rclone)."""
    http = FakeHTTP({("GET", "https://127.0.0.1:27124/"): (503, "")})
    sleeps: list[float] = []
    monkeypatch.setattr("sync.obsidian.time.sleep", lambda s: sleeps.append(s))
    backend = ObsidianBackend(cfg, http_client=http)
    assert backend.wait_until_ready(timeout=2) is False


# --- introspection ----------------------------------------------------------


def test_repr_Contient_pas_le_secret(cfg, monkeypatch):
    monkeypatch.setenv("MIMIR_OBSIDIAN_API_KEY", "DO-NOT-LEAK")
    backend = ObsidianBackend(cfg, http_client=FakeHTTP())
    text = repr(backend)
    assert "DO-NOT-LEAK" not in text
    # ...mais contient le nom de la variable (info non-sensible)
    assert "MIMIR_OBSIDIAN_API_KEY" in text


# --- client HTTP par défaut (smoke test non-réseau) -------------------------


def test_default_http_client_signature():
    """`_default_http_client` est importable et a la signature attendue."""
    assert callable(_default_http_client)
    import inspect
    sig = inspect.signature(_default_http_client)
    params = list(sig.parameters)
    assert params[:2] == ["method", "url"]
    assert "headers" in sig.parameters
    assert "body" in sig.parameters
    assert "verify_tls" in sig.parameters
    assert "timeout" in sig.parameters
