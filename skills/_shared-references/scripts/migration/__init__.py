#!/usr/bin/env python3
"""Façade du sous-module `migration` (capability `vault-migration`).

Réexporte les briques pour qu'un orchestrateur puisse écrire :

    from migration import engine, lazy, plan, registry, snapshot, validate

Plutôt que d'aller chercher `migration.engine.run_one` dans un sous-module
profond (les imports plats sont la convention du socle, cf. spec
`mimir-invariants` §Unified socle bootstrap).

**Enregistrement automatique des paliers** : à l'import du package, on
charge `migration.paliers` et on enregistre les paliers concrets (v1→v2,
etc.). C'est ce qui rend `wiki-init status` capable de lister les paliers
en attente sans orchestration explicite.

Les tests qui veulent un registre vide utilisent le fixture
`migration.registry.clear()` (déjà standard dans `test_migration_engine.py`).
"""

from __future__ import annotations

from . import engine, lazy, paliers, plan, registry, snapshot, validate

# Auto-enregistrement : on importe les paliers concrets et on les enregistre.
# Idempotent (registry.register refuse les doublons stricts).
_ = paliers.register_all()

__all__ = ["engine", "lazy", "paliers", "plan", "registry", "snapshot", "validate"]
