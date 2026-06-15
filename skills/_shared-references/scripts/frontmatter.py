#!/usr/bin/env python3
"""Parsing de frontmatter YAML d'un document Markdown (NFD/NFC-tolérant).

Promu depuis `wiki-ingest/scripts/article_writer.py` (Phase 2) : à partir de la
Phase 3, trois consommateurs en ont besoin (`wiki-ingest`, `wiki-reading-grid`,
`wiki-index`). On garde la même sémantique tolérante : pas de dépendance `yaml`
runtime, frontmatter absent → `({}, texte)`.

Le parseur sépare l'en-tête `--- … ---` du corps et renvoie un `dict` où les clés
listées (`tags`, `sources` par défaut) sont décodées en `list[str]`, les autres
en chaînes débarrassées des guillemets. Deux formes YAML sont acceptées pour les
listes :
- **flow-list** : `key: [a, b]` (ou `key: a, b`).
- **block-list** : `key:` suivi de lignes indentées `- item` (forme standard YAML
  produite par défaut par les LLM — piège historique : ignorée silencieusement
  avant ce change, REX ENFSI 2026-06-08 bug #3).

**Champ `schema_version` (capability `vault-migration`)** : un palier de
migration sémantique peut lire le frontmatter pour sélectionner les fichiers
« en retard » (`schema_version < palier`). Le parseur **tolère** ce champ comme
un entier facultatif ; son absence signifie « non migré » (jamais une erreur).
Cf. `read_schema_version()` ci-dessous.
"""

from __future__ import annotations

import re

#: Clés dont la valeur est une liste YAML (flow-list OU block-list, le reste = scalaire).
LIST_KEYS_DEFAULT = ("tags", "sources")

#: Clé du frontmatter indiquant la version de structure **par fichier**
#: (capability `vault-migration` — *schema_version optionnel par fichier*).
#: Lecture seule : aucune écriture automatique ; c'est aux paliers sémantiques
#: de poser cette clé après migration réussie d'un fichier.
SCHEMA_VERSION_KEY = "schema_version"

#: Ligne de block-list indentée : au moins un espace, `-`, puis l'item.
#: On capture l'item complet (sans le `-`) ; le trim des guillemets se fait après.
_BLOCK_ITEM_RE = re.compile(r"^[ \t]+-\s*(.*)$")


def parse_list(raw: str) -> list[str]:
    """Parse une flow-list `[a, "b c"]` → `["a", "b c"]` (tolérant)."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip().strip('"').strip("'").strip()
        if item:
            out.append(item)
    return out


def _parse_block_list(lines: list[str], start: int) -> tuple[list[str], int]:
    """Consomme un block-list YAML à partir de `lines[start]`.

    Renvoie `(items, next_index)`. Un block-list est une séquence de lignes
    indentées (` ` ou `\\t`) qui commencent par `-`. La séquence s'arrête à la
    première ligne non conforme (typiquement la prochaine clé du frontmatter, ou
    `---` fermant). Les lignes vides sont tolérées et skipppées. Chaque item est
    trimmé (guillemets + whitespace) par parité avec `parse_list` ; les items
    vides sont filtrés.

    Le `start` est censé pointer APRÈS la ligne `key:` (qui a déclenché le scan).
    """
    items: list[str] = []
    j = start
    while j < len(lines):
        raw = lines[j]
        # On travaille sur la ligne nue (sans le \n final)
        line = raw.rstrip("\n")
        if not line.strip():
            j += 1                                  # ligne vide au sein d'un block-list
            continue
        m = _BLOCK_ITEM_RE.match(line)
        if not m:
            break                                   # fin du block-list
        item = m.group(1).strip().strip('"').strip("'").strip()
        if item:                                    # parité parse_list : on ignore les items vides
            items.append(item)
        j += 1
    return items, j


def _coerce_int(value: object) -> int | None:
    """Convertit en `int` positif (exclut `bool` qui est un sous-type de `int`)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    # Tolérance : un scalaire YAML type-ché en str (« 3 ») — courant quand le
    # frontmatter est sérialisé via des LLM qui quotent tout.
    if isinstance(value, str):
        s = value.strip().strip('"').strip("'").strip()
        try:
            n = int(s)
        except ValueError:
            return None
        return n if n >= 1 else None
    return None


def parse_frontmatter(
    text: str, *, list_keys: tuple[str, ...] = LIST_KEYS_DEFAULT
) -> tuple[dict[str, object], str]:
    """Sépare frontmatter et corps d'un document Markdown.

    Renvoie `(front, body)` où `front` porte les scalaires (chaînes) et les
    `list_keys` (listes). Tolérant : pas de frontmatter → `({}, text)`. Le corps
    est renvoyé débarrassé de l'éventuelle ligne vide qui suit le `---` fermant.

    Listes : la valeur inline (`[a, b]`) prend la voie flow-list ; une valeur
    **vide** (juste `key:`) déclenche la détection d'un block-list YAML sur les
    lignes qui suivent (un item par ligne, indenté, préfixé par `-`).
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    front: dict[str, object] = {}
    body_start = len(lines)
    i = 1
    while i < len(lines):
        line = lines[i].rstrip("\n")
        if line.strip() == "---":
            body_start = i + 1
            break
        if ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key in list_keys and not value:
            # block-list YAML : `key:` nu → scanner les `- item` indentés qui suivent
            front[key], i = _parse_block_list(lines, i + 1)
        elif key in list_keys:
            front[key] = parse_list(value)
            i += 1
        else:
            front[key] = value.strip('"').strip("'")
            i += 1
    body = "".join(lines[body_start:])
    return front, body.lstrip("\n")


def read_schema_version(front: dict[str, object]) -> int | None:
    """Lit la `schema_version` d'un frontmatter parsé. `None` = « non migré ».

    Tolérant : valeur absente, mal formée, ou hors plage → `None`. **Ne lève
    jamais** (cf. spec `vault-migration` — *schema_version optionnel par
    fichier*, scénario « absence tolérée »).
    """
    return _coerce_int(front.get(SCHEMA_VERSION_KEY))
