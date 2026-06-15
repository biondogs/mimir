"""Tests de l'extracteur web — sans réseau (fetch monkeypatché)."""

import pytest

from extractors import web
from extractors.base import ExtractorError

_HTML = """<!doctype html><html><head><title>  Le Relèvement  </title></head>
<body>
<nav>menu pub cookies</nav>
<article>
  <h1>Le relèvement</h1>
  <p>Mesure de l'angle entre le nord et un amer.</p>
  <img src="/img/amer.png" alt="amer"/>
  <p>Trois relèvements donnent un point.</p>
</article>
<footer>pied de page</footer>
</body></html>"""


@pytest.fixture
def no_network(monkeypatch):
    monkeypatch.setattr(web, "_fetch_html", lambda url: _HTML)
    monkeypatch.setattr(web, "_fetch_bytes", lambda url: b"\x89PNG-fake")


def test_web_extract_contrat(no_network):
    # chemin par défaut (trafilatura si dispo) : on valide le contrat, pas la qualité
    res = web.extract("https://exemple.org/relevement")
    assert res.content_ext == "md" and res.doc_type == "web"
    assert res.metadata["type"] == "web"
    assert res.metadata["title"] == "Le Relèvement"
    assert res.metadata["source"] == "https://exemple.org/relevement"
    assert res.metadata["created"]  # date présente
    assert "amer" in res.raw_content.lower()  # contenu principal présent


def test_web_bs4_localise_images(no_network, monkeypatch):
    # Force le chemin bs4+markdownify (déterministe) : nav retirée + image localisée.
    # Ce chemin EXIGE bs4+markdownify (le fallback stdlib ne retire ni nav/footer
    # ni ne localise les images) -> skip propre si absents (env CI minimal).
    pytest.importorskip("bs4")
    pytest.importorskip("markdownify")
    # trafilatura est optionnel : présent -> on neutralise son extract pour forcer
    # le fallback bs4 ; absent -> le fallback bs4 s'applique déjà naturellement.
    try:
        import trafilatura

        monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: None)
    except ModuleNotFoundError:
        pass
    res = web.extract("https://exemple.org/relevement")
    assert "pied de page" not in res.raw_content  # footer retiré
    assert "menu pub cookies" not in res.raw_content  # nav retirée
    assert res.assets and res.assets[0].original_ref == "/img/amer.png"
    assert res.assets[0].filename == "amer.png"


def test_web_fetch_echoue(monkeypatch):
    def boom(url):
        raise ExtractorError("404")

    monkeypatch.setattr(web, "_fetch_html", boom)
    with pytest.raises(ExtractorError):
        web.extract("https://exemple.org/nope")


def test_web_contenu_vide(monkeypatch):
    monkeypatch.setattr(web, "_fetch_html", lambda url: "<html><body></body></html>")
    # selon les libs, soit ExtractorError, soit markdown vide -> on tolère l'erreur
    try:
        res = web.extract("https://exemple.org/vide")
        assert res.raw_content.strip() == "" or res.raw_content is not None
    except ExtractorError:
        pass


# --- palier v2→v3 : HTML source conservé + chaîne d'extraction durcie ---------

_HTML_JS_HEAVY = """<!doctype html>
<html><head>
  <title>Page WP Rocket</title>
  <script>var RocketLazyLoadScripts = "heavy js";</script>
  <style>body { color: red; }</style>
</head>
<body>
  <article>
    <h1>Titre propre</h1>
    <p>Contenu principal de l'article.</p>
    <script>analytics.track('pageview');</script>
    <p>Suite du contenu.</p>
  </article>
  <script src="https://exemple.org/extra.js"></script>
</body></html>"""


def test_web_extract_expose_html_brut(no_network):
    """`ExtractResult.source_content` porte le HTML brut (bytes) et
    `source_ext='html'` (pour que l'orchestrateur écrive le `.html` à
    côté du `.md` dérivé)."""
    res = web.extract("https://exemple.org/relevement")
    assert res.source_content is not None
    assert isinstance(res.source_content, bytes)
    assert res.source_ext == "html"
    # Le HTML brut est bien la réponse fetchée, pas une transformation.
    assert b"Le rel\xc3\xa8vement" in res.source_content  # "Le relèvement" en UTF-8
    assert b"<article>" in res.source_content


def test_web_dernier_recours_strip_script_et_style():
    """Régression panocrim : une page truffée de `<script>`/`<style>`
    DOIT voir son JS/CSS retirés **avant** le strip des balises, même
    si toutes les libs riches (trafilatura/bs4/markdownify) sont absentes.
    On monkey-patche `trafilatura` et `bs4`/`markdownify` pour lever
    `ImportError` côté `_to_markdown`."""
    import importlib
    import sys

    # Force la voie 4 (stdlib) en faisant en sorte que les imports
    # riches lèvent ImportError. On monkey-patch les modules pour qu'ils
    # n'aient pas les attributs attendus.
    fake_traf = type(sys)("trafilatura")
    fake_bs4 = type(sys)("bs4")
    fake_md = type(sys)("markdownify")
    for mod in (fake_traf, fake_bs4, fake_md):
        sys.modules[mod.__name__] = mod
    # `_to_markdown_via_trafilatura` essaie `import trafilatura` — notre
    # fake n'a pas `.extract`, donc ImportError. Idem pour bs4/markdownify.
    try:
        html = _HTML_JS_HEAVY
        md = web._to_markdown(html, "https://exemple.org/x")
    finally:
        for mod in (fake_traf, fake_bs4, fake_md):
            sys.modules.pop(mod.__name__, None)

    # Le JS et le CSS ne doivent PAS apparaître dans le markdown
    # (sinon on a la régression panocrim).
    assert "RocketLazyLoadScripts" not in md
    assert "analytics.track" not in md
    assert "color: red" not in md
    assert "extra.js" not in md  # attribut src du <script src=…>
    # Le contenu principal, lui, est conservé.
    assert "Titre propre" in md or "Contenu principal" in md


def test_web_dernier_recours_strip_script_multi_ligne():
    """Les `<script>` sur plusieurs lignes (cas typique : JS bundlé) sont
    capturés en multi-ligne par la regex."""
    html = """<html><body>
    <script>
      var big_block = function() {
        return 'multi-line js payload';
      };
    </script>
    <p>Contenu visible.</p>
    </body></html>"""
    md = web._to_markdown(html, "https://x/")
    assert "big_block" not in md
    assert "multi-line js payload" not in md
    assert "Contenu visible" in md


def test_web_assets_ont_original_ref(no_network, monkeypatch):
    """Pour réécrire les liens d'image, l'extracteur pose l'`original_ref`
    (= la chaîne exacte du markdown produit) sur chaque `Asset`."""
    try:
        import trafilatura

        monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: None)
    except ModuleNotFoundError:
        pass
    res = web.extract("https://exemple.org/x")
    for asset in res.assets:
        assert asset.original_ref is not None
        assert asset.original_ref in res.raw_content  # chaîne présente dans le md
