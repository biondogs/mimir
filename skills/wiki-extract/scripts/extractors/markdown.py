#!/usr/bin/env python3
"""Extracteur Markdown déposé (pass-through, SPEC §6 + palier `route-md-inbox-notes`).

Un `.md` déposé dans `_inbox/` (clip web fait à la main, curl + markdownify
maison, test d'un nouveau scraper) **est** sa propre matière source : il n'a pas
de `.html` parent re-dérivable. On ne le fait **pas** transiter par la branche
web (sinon : double frontmatter, dédup URL abusive, sidecar `.html` absent).

Stratégie : pass-through. On lit le fichier, on sépare frontmatter/corps, on
dérive `title` (frontmatter `title` sinon basename) et `created` (frontmatter
`created` sinon `mtime`). Le corps est **inchangé**. L'octet brut est exposé via
`source_content` + `source_ext="src.md"` pour que l'orchestrateur écrive la
matière immuable à côté de la dérivation normalisée (modèle du triplet web —
`.html` immuable + `.md` dérivé).

Frontmatter YAML malformé : pas un échec — on dérive `title`/`created` par
défaut et le corps = fichier entier (cf. spec `extract-source` « Routage des
notes markdown »).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .base import ExtractorError, ExtractResult

SUPPORTED_EXTS = {".md"}


def _try_import_parse_frontmatter():
    """Import paresseux du parseur de frontmatter partagé.

    L'orchestrateur l'a déjà mis sur `sys.path` (bootstrap du socle mutualisé) ;
    en test isolé, c'est `conftest.py` qui s'en charge. On tolère l'absence
    pour ne pas dupliquer la logique de parsing : sans le helper, on dégrade
    vers « pas de frontmatter » (le `.md` entier devient le corps).
    """
    try:
        from frontmatter import parse_frontmatter  # type: ignore
    except Exception:
        return None
    return parse_frontmatter


def _parse_frontmatter_safe(text: str) -> tuple[dict, str]:
    """Frontmatter tolérant : si le helper est dispo, on l'utilise ; sinon on
    détecte un éventuel bloc `--- … ---` à la main, en attrapant toute exception
    (frontmatter YAML malformé). On ne fait JAMAIS échouer le lot : on revient à
    « pas de frontmatter » (front vide, corps = texte entier)."""
    parse = _try_import_parse_frontmatter()
    if parse is None:
        return _naive_frontmatter(text)
    try:
        return parse(text)
    except Exception:
        return _naive_frontmatter(text)


def _naive_frontmatter(text: str) -> tuple[dict, str]:
    """Fallback stdlib : repère un bloc `--- … ---` initial, splitte naïvement.

    On ne parse PAS le YAML (pas de dépendance). On récupère uniquement les
    `key: value` simples et la première valeur scalaire ; les listes YAML ne
    sont pas décodées. C'est un **repli** : le helper partagé fait mieux.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    front: dict[str, str] = {}
    body_start = len(lines)
    for i in range(1, len(lines)):
        line = lines[i].rstrip("\n").strip()
        if line == "---":
            body_start = i + 1
            break
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                front[key] = value
    body = "".join(lines[body_start:])
    return front, body.lstrip("\n")


def _mtime_iso(path: Path) -> str:
    """Date de modification du fichier (ISO court) — fallback `created` quand
    le frontmatter n'en porte pas."""
    return date.fromtimestamp(path.stat().st_mtime).isoformat()


def extract(source, *, lang: str = "fra+eng") -> ExtractResult:
    """Lit un `.md` déposé et le rend sous le contrat `ExtractResult`.

    - `raw_content` = corps markdown (frontmatter strippé) ; l'orchestrateur le
      ré-encapsulera avec un frontmatter normalisé (`type: note`, `title`, …).
    - `source_content` = octets du fichier tel quel (matière immuable, analogue
      du `.html` web) ; `source_ext="src.md"`.
    - `metadata.title` = `frontmatter.title` sinon basename sans `.md`.
    - `metadata.created` = `frontmatter.created` sinon `mtime` du fichier.
    - `metadata.source` = valeur du frontmatter `source:` si présent (jamais
      inventé).
    """
    path = Path(str(source))
    if not path.is_file():
        raise ExtractorError(f"Fichier Markdown introuvable : {path}")

    raw_bytes = path.read_bytes()
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    front, body = _parse_frontmatter_safe(raw_text)

    title = (front.get("title") or path.stem) if isinstance(front, dict) else path.stem
    title = str(title).strip() or path.stem

    created = None
    if isinstance(front, dict):
        c = front.get("created")
        if isinstance(c, str) and c.strip():
            created = c.strip()
    if not created:
        created = _mtime_iso(path)

    metadata: dict = {
        "title": title,
        "type": "note",
        "created": created,
    }
    if isinstance(front, dict):
        src = front.get("source")
        if isinstance(src, str) and src.strip():
            metadata["source"] = src.strip()

    return ExtractResult(
        raw_content=body,
        content_ext="md",
        source_content=raw_bytes,
        source_ext="src.md",
        metadata=metadata,
        structure=[],
        assets=[],
        doc_type="notes",
    )
