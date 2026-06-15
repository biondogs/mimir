#!/usr/bin/env python3
"""Façade des paliers concrets de `vault-migration`.

Chaque palier vit dans son propre module et expose un objet `Migration`
déjà construit (PALIER). Le palier **n'est pas enregistré automatiquement**
au chargement de ce package : l'appelant (orchestrateur, CLI, test) doit
invoquer `register()` explicitement. C'est plus testable (chaque test peut
choisir quels paliers charger) et ça évite les effets de bord à l'import.
"""

from __future__ import annotations

from . import v1_v2_pdf_assets, v2_v3_web_fidelity, v3_v4_reading_grid_guide

__all__ = ["v1_v2_pdf_assets", "v2_v3_web_fidelity", "v3_v4_reading_grid_guide", "register_all"]


def register_all() -> list:
    """Enregistre tous les paliers connus. Renvoie la liste des paliers
    enregistrés (pour le diagnostic / les tests)."""
    return [
        v1_v2_pdf_assets.register(),
        v2_v3_web_fidelity.register(),
        v3_v4_reading_grid_guide.register(),
    ]
