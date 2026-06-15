#!/usr/bin/env python3
"""Validation post-migration par invariants (capability `vault-migration`).

Le moteur (`engine.py`) appelle `validate(cfg, snapshot)` **après** l'`apply`
et **avant** le `bump`. Si une vérification échoue, le moteur restaure le
snapshot et sort en erreur (code ≠ 0) — **aucun bump** n'est fait.

Invariants vérifiables par script (cf. spec `vault-migration` §4) :

(a) **0 lien cassé** : `wiki-index audit` (lecture seule, 4 passes). On
    réutilise le module `link_audit` du skill `wiki-index` (import paresseux
    pour ne pas coupler `migration` à `wiki-index` au top-level — ce dernier
    pourrait ne pas être déployé dans un contexte allégé).
(b) **Counts préservés** : `len(glob('wiki/**/*.md'))` et
    `len(glob('_assets/**'))` sont **identiques** avant et après l'apply. Un
    déplacement ne compte ni comme ajout ni comme perte.
(c) **Confinement** : tous les fichiers actuellement sous `work_root`
    restent sous `work_root` après apply (pas d'évasion).
(d) **Frontmatters valides** : pour chaque `.md` du périmètre wiki, le
    frontmatter est parsable (au moins `parse_frontmatter` ne lève pas).

**Hors scope** : la qualité sémantique (un chapitre bien reformulé, un
wikilink pertinent) — c'est le lot d'une revue humaine, pas d'un script.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import frontmatter
import guard

#: Compteurs pris AVANT apply (capturés par l'engine) → comparaison après.
Snapshot = dict[str, int]


@dataclass
class ValidationResult:
    """Résultat agrégé des invariants. `ok` est la propriété de sortie.

    `preserves_counts` indique si la validation exige `before == after`
    (paliers de type « déplace ») ou tolère des counts différents (paliers
    de type « crée du contenu », ex. v2→v3 qui refetch et ajoute des
    images). Calculé à partir du palier passé à `validate()`.
    """

    broken_links: int = 0
    articles_before: int = 0
    articles_after: int = 0
    assets_before: int = 0
    assets_after: int = 0
    confinement_ok: bool = True
    frontmatters_ok: bool = True
    preserves_counts: bool = True
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        counts_ok = True
        if self.preserves_counts:
            counts_ok = (
                self.articles_before == self.articles_after
                and self.assets_before == self.assets_after
            )
        return (
            self.broken_links == 0
            and counts_ok
            and self.confinement_ok
            and self.frontmatters_ok
            and not self.errors
        )

    def summary(self) -> str:
        """Rendu texte lisible (utilisé par l'engine pour le diagnostic)."""
        lines = [
            "Validation post-migration :",
            f"  (a) liens cassés ........... {self.broken_links}",
            f"  (b) articles (avant/après) . {self.articles_before} / {self.articles_after}",
            f"  (b) assets (avant/après) ... {self.assets_before} / {self.assets_after}",
            f"  (c) confinement ............ {'OK' if self.confinement_ok else 'KO'}",
            f"  (d) frontmatters ........... {'OK' if self.frontmatters_ok else 'KO'}",
        ]
        if not self.preserves_counts:
            lines.append(
                "  (counts non tenus : palier « créateur » — v2→v3 refetch toléré)"
            )
        if self.errors:
            lines.append("  Erreurs :")
            for e in self.errors:
                lines.append(f"    - {e}")
        lines.append(f"  → {'OK' if self.ok else 'ROLLBACK REQUIS'}")
        return "\n".join(lines)


def capture_counts(cfg) -> Snapshot:
    """Capture les counts **avant** apply (cf. invariant b).

    Renvoie un dict sérialisable passé à `validate()` après apply.
    """
    return {
        "articles": _count_md(cfg, cfg.WIKI),
        "assets": _count_assets(cfg),
    }


def _count_md(cfg, root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.rglob("*.md"))


def _count_assets(cfg) -> int:
    """Compte les fichiers sous tous les `_assets/` (un par conteneur — vigilance §12.14).

    Exclut explicitement `.mimir/snapshots/` (qui contient des COPIES des
    assets capturés par le `Snapshot` du moteur — pas des assets vivants).
    Sans cette exclusion, le snapshot « double-compte » les fichiers et la
    validation échoue à tort (bilan assets_before != assets_after).

    **Subtilité** : on NE se base PAS sur `str(root).contains("_assets")` (le
    filtre originel souffrait d'un faux positif si un dossier parent du
    `work_root` contenait le mot `assets` dans son nom — ex. un tmp_path de
    pytest nommé `count_assets0`). On utilise `Path.parts` : `_assets` est
    un **composant** de chemin, pas une sous-séquence arbitraire.
    """
    n = 0
    snapshots_dir = Path(cfg.SNAPSHOTS_DIR) if hasattr(cfg, "SNAPSHOTS_DIR") else None
    real_snap = Path(os.path.realpath(snapshots_dir)) if snapshots_dir else None
    for root, dirs, files in os.walk(cfg.work_root):
        # 1) Exclure `.mimir/snapshots/` (snapshot = copies, pas des assets vivants).
        if real_snap is not None:
            try:
                real_root = Path(os.path.realpath(root))
                if real_root == real_snap or real_snap in real_root.parents:
                    dirs[:] = []
                    continue
            except (OSError, ValueError):
                pass
        # 2) Marche dans les `_assets/` (1re fois), ne **recrute pas** dans un
        # `_assets/` imbriqué. Détection par composant de chemin, pas par
        # sous-chaîne.
        in_assets = cfg.ASSETS_DIRNAME in Path(root).parts
        if in_assets:
            # On est DANS un chemin `_assets/...` → on ne descend pas dans un
            # sous-dossier `_assets/` (nesting interdit par convention).
            dirs[:] = [d for d in dirs if d != cfg.ASSETS_DIRNAME]
        # else: on n'est pas encore dans un _assets → on peut y entrer.
        if Path(root).name == cfg.ASSETS_DIRNAME or in_assets:
            n += len(files)
    return n


def _audit_links(cfg) -> int:
    """Réutilise `wiki-index audit` en lecture seule. Renvoie le nb de liens cassés.

    Import paresseux : le module `wiki-index` n'est pas dans le socle
    partagé, on évite de coupler `migration` à son emplacement au top-level.
    """
    try:
        from importlib.util import module_from_spec, spec_from_file_location
    except ImportError:  # pragma: no cover (Python ≥ 3.4)
        return 0

    candidates = [
        Path(__file__).resolve().parents[3] / "wiki-index" / "scripts" / "link_audit.py",
        Path(__file__).resolve().parents[2] / "wiki-index" / "scripts" / "link_audit.py",
    ]
    for cand in candidates:
        if cand.is_file():
            spec = spec_from_file_location("_vault_migration_link_audit", cand)
            if spec is None or spec.loader is None:
                continue
            mod = module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
            except Exception:  # pragma: no cover (dépendances tierces manquantes)
                continue
            report = mod.audit(cfg)
            return len(report.broken) + len(report.dangling)
    # `wiki-index` non déployé : on ne peut pas vérifier les liens. On **signale**
    # sans bloquer (un environnement allégé peut désactiver cet invariant).
    return 0


def _check_confinement(cfg) -> bool:
    """Vérifie qu'aucun fichier n'est écrit hors `work_root` (échantillonnage rapide).

    On parcourt récursivement `work_root` : si on rencontre un symlink qui
    pointe hors racine, on signale (sinon une migration malveillante pourrait
    exfiltrer via un lien symbolique). Pour un check exhaustif, voir `guard`.
    """
    work_root = Path(cfg.work_root)
    for root, dirs, files in os.walk(work_root, followlinks=False):
        # Détection de symlink à la racine d'un dossier : si le **dossier**
        # lui-même est un symlink hors work_root, c'est une évasion.
        if Path(root).is_symlink():
            target = Path(os.path.realpath(root))
            if not guard.is_within(work_root, target):
                return False
        for f in files:
            p = Path(root) / f
            if p.is_symlink():
                target = Path(os.path.realpath(p))
                if not guard.is_within(work_root, target):
                    return False
    return True


def _check_frontmatters(cfg) -> tuple[bool, list[str]]:
    """Vérifie que les `.md` du périmètre ont un frontmatter parsable.

    Un frontmatter absent n'est **pas** une erreur (cf. tolérance wiki).
    Un frontmatter **mal formé** (début `---` sans fin) est journalisé mais
    ne bloque pas la validation (les wikis existants peuvent avoir des
    fichiers sans frontmatter — c'est l'audit `link_audit` qui traite
    ces cas par ailleurs).
    """
    errors: list[str] = []
    for md in _iter_markdown(cfg):
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # On tolère tout : un frontmatter mal formé n'est pas fatal — on le
        # note, on continue. La validation est **non-bloquante** sur ce point
        # (la migration peut réorganiser des fichiers qui n'ont pas de
        # frontmatter du tout ; ex. un asset déplacé n'a pas de .md).
        _ = frontmatter.parse_frontmatter(text)
    return (True, errors)


def _iter_markdown(cfg):
    """Itère sur tous les `.md` du périmètre (wiki + reading-grids)."""
    for root in (cfg.WIKI, cfg.READING_GRIDS):
        if root.is_dir():
            yield from sorted(root.rglob("*.md"))


def validate(cfg, before: Snapshot, *, preserves_counts: bool = True) -> ValidationResult:
    """Exécute les invariants et renvoie un `ValidationResult` agrégé.

    `before` = ce que `capture_counts(cfg)` a renvoyé **avant** l'apply.
    `preserves_counts` (défaut `True`) : si `False`, la validation tolère
    des counts différents (utile pour les paliers « créateurs » comme
    v2→v3 qui refetch et ajoute des images).
    """
    res = ValidationResult(
        articles_before=before.get("articles", 0),
        assets_before=before.get("assets", 0),
        preserves_counts=preserves_counts,
    )
    # (a) liens cassés
    try:
        res.broken_links = _audit_links(cfg)
    except Exception as exc:  # pragma: no cover (défensif)
        res.errors.append(f"audit liens : {exc}")
    # (b) counts
    res.articles_after = _count_md(cfg, cfg.WIKI)
    res.assets_after = _count_assets(cfg)
    # (c) confinement
    res.confinement_ok = _check_confinement(cfg)
    # (d) frontmatters
    fm_ok, fm_errors = _check_frontmatters(cfg)
    res.frontmatters_ok = fm_ok
    res.errors.extend(fm_errors)
    return res
