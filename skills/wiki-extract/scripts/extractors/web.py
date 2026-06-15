#!/usr/bin/env python3
"""Extracteur Web/URL (SPEC §4.4, §6 + palier v2→v3).

Aligné sur le modèle PDF/EPUB : on **conserve la réponse HTML brute** comme
matière fidèle immuable (`<slug>-<date>.html`, l'analogue du `.pdf` binaire)
et on produit **à côté** un markdown lisible dérivé (`<slug>-<date>.md`).
Le markdown peut être **re-dérivé** à partir du `.html` local, sans nouveau
fetch réseau (cf. spec `extract-source` — palier v2→v3).

Chaîne d'extraction (qualité décroissante, **stdlib garanti en dernier
recours**) :

    1. trafilatura   (markdown, include_images, include_links)  ← qualité
    2. readability   + bs4 + markdownify                       ← repli riche
    3. bs4 : strip script/style/nav/footer/aside/form,
              <article>/<main> prioritaire
    4. DERNIER RECOURS stdlib (regex) : strip <script> ET <style>
       AVANT le strip des balises (régression panocrim — jamais
       de JS/CSS dans le corps)

Images : téléchargées sous `raw/web/_assets/<slug>/` (regroupées par
clipping, cohérent avec le palier v1→v2 pour les PDF), liens d'image
réécrits en relatif par l'orchestrateur.

Échec de fetch/extraction → `ExtractorError` (jamais de contenu inventé).
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin, urlparse

from .base import Asset, ExtractorError, ExtractResult

SUPPORTED_EXTS: set[str] = set()  # routage par schéma URL, pas par extension

_UA = {"User-Agent": "Mozilla/5.0 (compatible; MimirWikiExtract/1.0)"}
_MD_IMG = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# Scripts/styles à stripper **avant** le strip des balises (panocrim).
# On capture `<script…>…</script>` (multi-ligne, attributs) et idem pour `<style>`.
_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_BLOCK = re.compile(r"<style\b[^>]*>.*?</style>", re.IGNORECASE | re.DOTALL)


# --- réseau (isolé pour faciliter le test sans réseau) --------------------
def _fetch_html(url: str) -> str:
    try:
        import requests

        r = requests.get(url, headers=_UA, timeout=20)
        r.raise_for_status()
        return r.text
    except ImportError:
        from urllib.request import Request, urlopen

        with urlopen(Request(url, headers=_UA), timeout=20) as resp:  # noqa: S310
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, "replace")
    except Exception as exc:  # erreur réseau/HTTP -> explicite
        raise ExtractorError(f"Échec du téléchargement de {url} : {exc}") from exc


def _fetch_bytes(url: str) -> bytes | None:
    try:
        import requests

        r = requests.get(url, headers=_UA, timeout=20)
        r.raise_for_status()
        return r.content
    except ImportError:
        from urllib.request import Request, urlopen

        try:
            with urlopen(Request(url, headers=_UA), timeout=20) as resp:  # noqa: S310
                return resp.read()
        except Exception:
            return None
    except Exception:
        return None  # image manquante : on n'échoue pas tout le clipping


# --- HTML -> markdown ------------------------------------------------------
def _title(html: str) -> str | None:
    m = _TITLE_RE.search(html)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else None


def _to_markdown_via_trafilatura(html: str, url: str) -> str | None:
    """Voie 1 : trafilatura (qualité maximale). `None` si absent / résultat vide."""
    try:
        import trafilatura

        md = trafilatura.extract(
            html, output_format="markdown", include_images=True,
            include_links=True, url=url,
        )
        if md and md.strip():
            return md
    except Exception:
        return None
    return None


def _to_markdown_via_readability(html: str, url: str) -> str | None:
    """Voie 2 : readability-lxml + markdownify. `None` si libs absentes."""
    try:
        from readability import Document
        from bs4 import BeautifulSoup
        from markdownify import markdownify
    except Exception:
        return None
    try:
        doc = Document(html, url=url)
        summary_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(summary_html, "html.parser")
        md = markdownify(str(soup), heading_style="ATX")
        if md and md.strip():
            return re.sub(r"\n{3,}", "\n\n", md).strip()
    except Exception:
        return None
    return None


def _to_markdown_via_bs4(html: str, url: str) -> str | None:
    """Voie 3 : bs4 strip (script/style/nav/footer/aside/form) + markdownify."""
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify
    except Exception:
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside", "form"]):
            tag.decompose()
        main = soup.find("article") or soup.find("main") or soup.body or soup
        md = markdownify(str(main), heading_style="ATX")
        if md and md.strip():
            return re.sub(r"\n{3,}", "\n\n", md).strip()
    except Exception:
        return None
    return None


def _to_markdown_via_stdlib(html: str, url: str) -> str:
    """Voie 4 (DERNIER RECOURS, stdlib garanti) : strip `<script>`/`<style>`
    **AVANT** le strip des balises, puis texte brut (régression panocrim).

    Garantit qu'aucun JavaScript ni CSS ne se retrouve dans le corps,
    même si toutes les libs riches sont absentes.
    """
    cleaned = _SCRIPT_BLOCK.sub(" ", html)
    cleaned = _STYLE_BLOCK.sub(" ", cleaned)
    text = re.sub(r"<[^>]+>", " ", cleaned)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ExtractorError(f"Aucun contenu exploitable extrait de {url}.")
    return text


def _to_markdown(html: str, url: str) -> str:
    """Chaîne d'extraction complète. Garantit **toujours** un résultat :
    `ExtractorError` (jamais de contenu vide) en dernier recours.
    """
    for fn in (
        _to_markdown_via_trafilatura,
        _to_markdown_via_readability,
        _to_markdown_via_bs4,
    ):
        md = fn(html, url)
        if md is not None:
            return md
    return _to_markdown_via_stdlib(html, url)


def _localize_images(markdown: str, url: str, slug: str):
    """Télécharge les images référencées.

    Renvoie `(assets, markdown)`. L'orchestrateur range chaque asset sous
    `raw/web/_assets/<slug>/<image>` (cf. palier v1→v2 pour PDF) et réécrit
    les liens via le `original_ref` (cf. `wiki_extract.process_source`).

    Le `slug` est exposé pour permettre à l'orchestrateur de re-grouper les
    assets par clipping (le sous-répertoire est créé par l'orchestrateur, pas
    par l'extracteur — l'extracteur reste agnostique au filesystem).
    """
    assets: list[Asset] = []
    seen: set[str] = set()
    for src in _MD_IMG.findall(markdown):
        if src in seen or src.startswith("data:"):
            continue
        seen.add(src)
        absolute = urljoin(url, src)
        data = _fetch_bytes(absolute)
        if data is None:
            continue  # image inaccessible → omise (tolérée, pas de crash)
        name = urlparse(absolute).path.rsplit("/", 1)[-1] or "image"
        assets.append(Asset(filename=name, data=data, original_ref=src))
    return assets, markdown


def extract(source, *, lang: str = "fra+eng") -> ExtractResult:
    """Extrait une URL : fetch HTML brut, dérive en markdown, télécharge images.

    Le HTML brut est exposé via `source_content` (bytes) + `source_ext="html"`
    pour que l'orchestrateur l'écrive comme matière immuable à côté du
    `.md` dérivé (cf. spec `extract-source`).
    """
    url = str(source)
    html = _fetch_html(url)
    markdown = _to_markdown(html, url)
    # Le slug est posé par l'orchestrateur (`_slug_base`) — l'extracteur ne
    # l'a pas à ce stade. On peut déduire une clé de regroupement à partir
    # du `metadata["title"]` (ou URL par défaut), réutilisée pour nommer
    # les assets via leur nom de fichier d'origine. L'orchestrateur
    # gère le regroupement dans le sous-répertoire.
    assets, markdown = _localize_images(markdown, url, slug="")
    return ExtractResult(
        raw_content=markdown,
        content_ext="md",
        source_content=html.encode("utf-8"),
        source_ext="html",
        metadata={
            "title": _title(html) or url,
            "source": url,
            "type": "web",
            "created": date.today().isoformat(),
        },
        structure=[],  # headings de page : non exploités en P1
        assets=assets,
        doc_type="web",
    )
