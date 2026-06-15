#!/usr/bin/env python3
"""Palier de migration **v1→v2** — regrouper les assets non-web par source.

Change `migrate-pdf-assets-per-source` (palier versionné). Déplace les
assets PDF/EPUB existants de `raw/<type>/_assets/<base>-<reste>` vers
`raw/<type>/_assets/<base>/<reste>`, sous le protocole `vault-migration`
(plan → snapshot → apply → validate → bump).

**Résolution de `<base>`** : jamais devinée en coupant au premier tiret. On
s'ancre sur les **slugs de source réellement présents** dans le vault :

1. Les `<base>.toc.json` voisins (dans `raw/<type>/`) → ce sont les bases
   des sources PDF/EPUB ingérées. Le `<base>` peut être suffixé `-2`, `-3`
   (collisions antérieures) ; on les inclut toutes.
2. Les lignes de `_status.md` (`Fichier` colonne) → on retire l'extension
   `.pdf.txt`/`.epub.txt` pour récupérer le slug.

Le `<base>` retenu est le **plus long préfixe matching `<X>-`** (longest
match) parmi les bases connues. Sans base connue, on **ignore** le fichier
(le palier n'invente rien) — il sera re-découpé par un palier ultérieur
si nécessaire, ou laissé en l'état si la source a été supprimée entre temps.

**Idempotence** : un asset déjà sous `_assets/<base>/` (palier déjà appliqué)
n'est pas re-déplacé. Le `plan` le détecte (`src` absent, `dst` présent)
et l'omet.

**Hors scope** : les assets web (préfixe plat `_assets/<base>-<date>-<file>`)
sont traités par `refit-web-extraction-fidelity` (palier v2→v3).
"""

from __future__ import annotations

from pathlib import Path

from migration import plan, registry


#: Types source concernés par ce palier (PDF + EPUB). Le web est exclu.
_TYPES = ("pdfs", "epubs")

#: Extensions sidecar qui identifient une source (utilisé pour extraire
#: les slugs de `<base>.toc.json`).
_SIDECAR_EXTS = {".toc.json"}


def _known_bases(type_dir: Path) -> set[str]:
    """Ensemble des slugs de source connus pour un type donné.

    Combine :
    - Les `<slug>.toc.json` directs (PDF, EPUB) → le plus fiable.
    - Les `Fichier` du `_status.md` (enlève `.pdf.txt`/`.epub.txt`).

    Renvoie un `set` (pas une liste) car l'ordre n'importe pas pour la
    résolution longest-match (cf. `_resolve_base`).
    """
    bases: set[str] = set()
    if not type_dir.is_dir():
        return bases
    # 1) Slugs via les sidecars `<base>.toc.json` voisins
    for sidecar in type_dir.glob("*.toc.json"):
        # `stem` retire la DERNIÈRE extension, donc `foo.toc.json` → `foo.toc`.
        # On retire le `.toc` pour récupérer le slug réel.
        name = sidecar.name
        if name.endswith(".toc.json"):
            bases.add(name[: -len(".toc.json")])
    # 2) Slugs via le `_status.md` (au cas où un sidecar manque)
    status = type_dir / "_status.md"
    if status.is_file():
        for line in status.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not cells or cells[0] in ("Fichier", "---", ""):
                continue
            fichier = cells[0]
            for ext in (".pdf.txt", ".epub.txt", ".md"):
                if fichier.endswith(ext):
                    bases.add(fichier[: -len(ext)])
                    break
    return bases


def _resolve_base(filename: str, known: set[str]) -> tuple[str, str] | None:
    """Résout `<filename>` (sans extension) en `(<base>, <reste>)` via longest-match.

    - Renvoie `(base, reste)` si un préfixe `<base>-` est trouvé dans `known`
      (préférant le **plus long** pour éviter le faux split `navigation-cotiere`
      → `navigation-`).
    - Renvoie `None` si aucune base connue ne préfixe `<filename>` (le
      palier ignore ces fichiers — pas d'invention).
    """
    if "-" not in filename:
        return None
    candidates = sorted(
        (b for b in known if filename.startswith(b + "-") and filename != b + "-"),
        key=len, reverse=True,
    )
    if not candidates:
        return None
    base = candidates[0]
    reste = filename[len(base) + 1:]
    return base, reste


def _build_plan(cfg) -> plan.Plan:
    """Plan dry-run : pour chaque asset non-web à plat, émet un `MOVE`.

    **Aucun effet de bord**. Le `MOVE` est idempotent côté `engine._atomic_apply`
    (un fichier déjà sous `<base>/` est détecté et ignoré).
    """
    p = plan.Plan(description="regrouper les assets PDF/EPUB par source")
    for doc_type in _TYPES:
        type_dir = cfg.RAW / doc_type
        assets_dir = type_dir / cfg.ASSETS_DIRNAME
        if not assets_dir.is_dir():
            continue
        known = _known_bases(type_dir)
        for asset_file in sorted(assets_dir.iterdir()):
            if not asset_file.is_file():
                # On ne touche pas aux sous-répertoires déjà créés (idempotence) :
                # un dossier `_assets/<base>/` est la **cible** d'un move, pas
                # une source à migrer. S'il n'a pas de fichiers dedans (cas
                # exotique d'un dossier vide laissé par une migration
                # partielle), on le laisse aussi (le prochain apply le peuplera).
                continue
            resolved = _resolve_base(asset_file.name, known)
            if resolved is None:
                # Pas de base connue → on ignore (cf. docstring).
                continue
            base, reste = resolved
            # `reste` inclut déjà l'extension du fichier (cf. `_resolve_base` qui
            # travaille sur `asset_file.name` complet). On utilise `reste` tel
            # quel comme nom de fichier cible — sans concaténer `suffix`.
            target = assets_dir / base / reste
            # Si la cible existe déjà (= palier déjà appliqué pour ce fichier),
            # on omet l'opération (le moteur le détecte aussi, mais l'omettre
            # ici rend le plan plus lisible et évite un faux positif au dry-run).
            if target.exists():
                continue
            p.add(plan.MOVE(asset_file, target))
    return p


def _apply(cfg, p: plan.Plan) -> None:
    """Apply : le moteur (`engine._atomic_apply`) exécute déjà le plan.

    Cette fonction est appelée par le moteur **après** l'apply atomique,
    pour les **post-traitements** éventuels (ici : rien — le MOVE est
    autosuffisant). Conservée pour la symétrie et les évolutions futures
    (ex. mise à jour de `<base>.toc.json` pour référencer le nouvel
    emplacement des images, si on voulait).
    """
    return None


def _apply(cfg, p: plan.Plan) -> None:
    """Apply : le moteur (`engine._atomic_apply`) exécute déjà le plan.

    Cette fonction est appelée par le moteur **après** l'apply atomique,
    pour les **post-traitements** éventuels (ici : rien — le MOVE est
    autosuffisant). Conservée pour la symétrie et les évolutions futures
    (ex. mise à jour de `<base>.toc.json` pour référencer le nouvel
    emplacement des images, si on voulait).
    """
    return None


#: Le palier de migration v1→v2 — exporté pour les tests et l'enregistrement.
#: Note : on ne passe PAS `apply` ici — le moteur retombe sur son `_atomic_apply`
#: par défaut (qui gère les `move`/`write`/`delete` du `plan`). Les paliers
#: qui ont une logique plus complexe (ex. refetch réseau pour v2→v3) passent
#: un `apply` custom que le moteur appelle à la place.
PALIER = registry.Migration(
    from_schema=1, to_schema=2,
    plan=_build_plan,
    description="regrouper les assets PDF/EPUB par source (<base>-<reste> → <base>/<reste>)",
)


def register() -> registry.Migration:
    """Enregistre le palier v1→v2 dans le registre. À appeler une fois
    au chargement du module (idempotent — `registry.register` refuse les
    doublons stricts)."""
    return registry.register(PALIER)
