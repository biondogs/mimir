#!/usr/bin/env python3
"""Routage vers le bon extracteur (par schéma URL ou extension de fichier).

Ajouter un format = ajouter un module exposant `SUPPORTED_EXTS` + `extract()`,
puis l'enregistrer ici. Aucune autre partie du système ne change (SPEC §6).
"""

from __future__ import annotations

from pathlib import Path

from .base import ExtractorError

_URL_SCHEMES = ("http://", "https://")


def is_url(source: str | Path) -> bool:
    return isinstance(source, str) and source.lower().startswith(_URL_SCHEMES)


def doc_type_for(source: str | Path) -> str:
    """Type de document (= sous-dossier raw/<type>/) pour une source donnée."""
    if is_url(source):
        return "web"
    ext = Path(str(source)).suffix.lower()
    if ext == ".pdf":
        return "pdfs"
    if ext == ".epub":
        return "epubs"
    if ext == ".md":
        return "notes"
    raise ExtractorError(
        f"Format non supporté : {source!r} (extensions : .pdf, .epub, .md, ou URL http)."
    )


def is_supported(source: str | Path) -> bool:
    """Vrai si `source` a un extracteur (URL http, `.pdf`, `.epub` ou `.md`). Ne lève jamais.

    Sert au tri de la dropzone `_inbox/` : un fichier non supporté (README, .txt,
    artefact OS…) doit être ignoré, pas faire échouer le scan. **Note** : un `.md`
    est supporté — un `README.md` déposé dans `_inbox/` devient une note (la
    discipline `_inbox/` est une responsabilité utilisateur, cf. spec
    `extract-source` « Routage des notes markdown »).
    """
    try:
        doc_type_for(source)
        return True
    except ExtractorError:
        return False


def get_extractor(source: str | Path):
    """Renvoie le module extracteur adapté (import paresseux pour tolérer les libs absentes)."""
    dt = doc_type_for(source)
    if dt == "web":
        from . import web

        return web
    if dt == "pdfs":
        from . import pdf

        return pdf
    if dt == "epubs":
        from . import epub

        return epub
    if dt == "notes":
        from . import markdown

        return markdown
    raise ExtractorError(f"Aucun extracteur pour le type {dt!r}.")  # pragma: no cover
