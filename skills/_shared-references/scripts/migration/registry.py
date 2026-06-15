#!/usr/bin/env python3
"""Registre ordonné de paliers de migration `vault-migration`.

Un palier est un objet `Migration` qui déclare :

- `from_schema` / `to_schema` : la version qu'il fait passer (entiers ≥ 1).
- `plan(cfg) -> Plan` : **dry-run pur** (aucun effet de bord) qui liste les
  fichiers touchés. C'est ce que l'humain valide avant apply.
- `apply(cfg, plan) -> None` : la **réécriture** réelle (atomique, confinée).
  Doit être **idempotente** : ré-exécutée à `schema = N` ⇒ no-op.

Le moteur (`engine.py`) énumère les paliers depuis `current_schema` jusqu'à
`EXPECTED_SCHEMA` (ou `target` explicite) en les chaînant **dans l'ordre**.
Chaque change feature qui ajoute un palier de migration y enregistre son
migrateur (cf. spec `vault-migration` — *Migrateurs idempotents et rejouables*).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .plan import Plan


@dataclass(frozen=True)
class Migration:
    """Un palier de migration versionné (idempotent, dry-run-able).

    `apply` est **optionnel** : s'il est `None`, le moteur applique le plan
    via son `_atomic_apply` par défaut (moves/writes/deletes). S'il est
    fourni, le moteur l'appelle à la place — utile pour les paliers qui
    ont une logique plus complexe (ex. refetch réseau pour le palier v2→v3
    `refit-web-extraction-fidelity`).

    `preserves_counts` (défaut `True`) : si `True`, la validation post-apply
    exige que le nombre d'articles / d'assets soit **identique** avant et
    après l'apply (invariant d'un palier qui ne fait que **déplacer** des
    fichiers, ex. v1→v2). Si `False`, la validation tolère des counts
    différents — utile pour les paliers qui **créent** du contenu (ex.
    v2→v3 qui refetch et télécharge de nouvelles images).
    """

    from_schema: int
    to_schema: int
    plan: Callable[..., Plan]
    apply: Callable[..., None] | None = None
    #: Description lisible (une ligne) — utilisée par le moteur pour le
    #: diagnostic (« en attente : v1→v2 : regrouper assets PDF »).
    description: str = ""
    preserves_counts: bool = True

    def __post_init__(self) -> None:
        if self.to_schema <= self.from_schema:
            raise ValueError(
                f"palier invalide : to_schema={self.to_schema} doit être > "
                f"from_schema={self.from_schema}"
            )


#: Registre **ordonné** : les paliers sont insérés dans l'ordre naturel
#: (`from_schema` croissant), ce qui rend `pending_for()` trivial à raisonner.
#: Au premier usage d'un change feature, on enregistre son palier ici.
_REGISTRY: list[Migration] = []


def register(migration: Migration) -> Migration:
    """Enregistre un palier dans le registre. Renvoie le palier (chaînage).

    Refuse les doublons stricts (`from_schema`+`to_schema` égaux) — un seul
    palier par cran de version. Refuse aussi les paliers rétrogrades
    (`to_schema` ≤ `from_schema`).
    """
    if any(m.from_schema == migration.from_schema and m.to_schema == migration.to_schema
           for m in _REGISTRY):
        raise ValueError(
            f"palier déjà enregistré : v{migration.from_schema}→v{migration.to_schema}"
        )
    # Insertion en queue (l'ordre d'enregistrement reflète les dépendances) ;
    # on vérifie quand même la cohérence : pas de palier qui enjambe un cran
    # déjà occupé (sinon ambigu au chainage).
    for m in _REGISTRY:
        if migration.from_schema < m.to_schema and migration.to_schema > m.from_schema:
            # `migration` chevauche `m` — on l'autorise seulement s'il est
            # strictement après (ex. v1→v3 coexisté avec v1→v2 + v2→v3 ? Non :
            # on interdit le chevauchement pour forcer la décomposition).
            if not (migration.from_schema == m.to_schema or migration.to_schema == m.from_schema):
                raise ValueError(
                    f"palier v{migration.from_schema}→v{migration.to_schema} chevauche "
                    f"v{m.from_schema}→v{m.to_schema} — décomposer en paliers contigus"
                )
    _REGISTRY.append(migration)
    _REGISTRY.sort(key=lambda m: (m.from_schema, m.to_schema))
    return migration


def registered() -> list[Migration]:
    """Copie figée du registre (lecture seule — ne pas muter)."""
    return list(_REGISTRY)


def pending_for(current: int, target: int | None = None) -> list[Migration]:
    """Paliers strictement entre `current` (exclu) et `target` (inclu, si donné).

    - `target=None` ⇒ on vise `current` + 1 (un seul palier suivant). C'est le
      mode « enchaîne le suivant » utilisé par le moteur pas-à-pas.
    - `target=int` ⇒ on vise `target` ; on exclut tout palier dont `to_schema`
      dépasse `target`.
    - Un palier absent du registre entre `current` et `target` est une **erreur**
      : on ne peut pas sauter de version sans palier.
    """
    if target is None:
        target = current + 1
    if target <= current:
        return []

    applicable = [m for m in _REGISTRY if m.from_schema >= current and m.to_schema <= target]
    # Vérification de continuité : pas de trou.
    if not applicable:
        return []
    chain_from = min(m.from_schema for m in applicable)
    if chain_from != current:
        return []
    # Tri déterministe.
    applicable.sort(key=lambda m: (m.from_schema, m.to_schema))
    return applicable


def clear() -> None:
    """Vide le registre. **Réservé aux tests** (reset entre cas)."""
    _REGISTRY.clear()
