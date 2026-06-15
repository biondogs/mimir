#!/usr/bin/env python3
"""Réparation **assistée** des wikilinks cassés (SPEC `audit-index` — opt-in, écrit sur demande).

Distincte de l'audit (`link_audit.audit`) qui reste **strictement lecture seule** :
cette commande **propose** toujours un mapping, et ne réécrit les liens que lorsque
`--apply` est passé. Sans `--apply` : aperçu — aucune écriture sous `wiki/` ni
`reading-grids/`.

Passes de matching, **dans l'ordre** (chacune ne s'applique que si la précédente
n'a rien proposé) :

0. **Override** — `--semantic-map <json>` charge un mapping manuel
   `{cible_cassée: cible_proposée}`. Priorité absolue, marqueur 🧠.
1. **Identité** — la cible résout telle quelle (cas dégénéré : un lien qu'on
   croyait cassé mais qui existe). Filet de sécurité ; marqueur 🔧.
2. **Préfixe commun char-par-char** — la **notion** de la cible cassée partage
   ≥ 3 caractères consécutifs avec la **notion** d'un fichier existant du
   même sujet (ou de tout le wiki si pas de sujet). On garde le plus long
   préfixe commun. Score = `|préfixe|`. Marqueur 🔧.
3. **Matching par mots** — tokenisation après retrait des stop-words ; un match
   est retenu si **au moins un mot long (≥ 6 chars) est commun** OU **au moins
   deux mots courts sont communs**. Score `0.5 + 0.1·|mots communs| + 0.2·|mots
   longs communs|`. Ce score est **toujours** inférieur au score préfixe : on
   n'applique la passe 3 qu'aux liens qui n'ont pas trouvé de préfixe.
   Marqueur 🔧.

Filtrage par `--scope <sujet>` (répétable) : on ne garde que les liens dont
**le fichier source** est sous `wiki/<scope>/` (ou `reading-grids/<scope>/` pour
les grilles). Sans `--scope` : tous les liens cassés.

L'écriture passe par `iohelpers.atomic_write_text` + `guard.assert_within` :
tout reste confiné à `work_root` (SPEC §12.8).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import guard
import iohelpers
import link_audit
import wikilinks


# --- bornes des passes ------------------------------------------------------
_PREFIX_MIN = 3            # passe 2 : ≥ 3 chars consécutifs communs
_LONG_WORD_MIN = 6         # passe 3 : un mot « long » fait au moins 6 chars
_SHORT_WORDS_MIN = 2       # passe 3 : sinon, ≥ 2 mots courts communs

#: Stop-words français + anglais (passe 3). En minuscules, ASCII.
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "au", "aux", "avec", "by", "ce", "ces", "dans", "de", "des", "du",
    "elle", "en", "et", "for", "from", "il", "ils", "in", "la", "le", "les",
    "leur", "lui", "ma", "mais", "me", "mes", "mon", "ne", "nos", "not", "of",
    "on", "or", "ou", "our", "par", "pas", "pour", "que", "qui", "sa", "se",
    "ses", "si", "son", "sur", "ta", "te", "tes", "the", "to", "ton", "tu",
    "un", "une", "vos", "votre", "vous", "with", "y",
})

#: Marqueurs de provenance (visibles dans la sortie dry-run).
_MARK_OVERRIDE = "🧠"
_MARK_AUTO = "🔧"


# --- modèles ----------------------------------------------------------------
@dataclass
class Proposal:
    """Une proposition de réparation pour un lien cassé."""

    src: str                 # chemin relatif (work_root) du fichier source
    broken: str              # cible cassée, telle qu'écrite dans le wikilink
    target: str              # cible proposée (forme canonique du wiki)
    target_path: str         # chemin relatif du fichier cible résolu
    provenance: str          # "override" | "identity" | "prefix" | "words"
    marker: str              # "🧠" (override) | "🔧" (auto)
    score: float             # score de la passe (debug / tri)


@dataclass
class RepairReport:
    """Résultat d'un run de réparation (dry-run ou apply)."""

    proposals: list[Proposal] = field(default_factory=list)
    rewrites: list[tuple[str, str, str]] = field(
        default_factory=list
    )                                       # (src, old, new) — uniquement en --apply
    audit_after_ok: bool | None = None     # None si pas re-audité, sinon résultat

    @property
    def total(self) -> int:
        return len(self.proposals)


# --- helpers de normalisation des cibles ------------------------------------
def _notion_of(target: str) -> str:
    """Extrait la **notion** (dernier segment) d'une cible de wikilink.

    `ea/service-orientation-and-apis` → `service-orientation-and-apis`.
    `service-orientation` (sans `/`) → `service-orientation`.
    """
    return target.rsplit("/", 1)[-1] if "/" in target else target


def _subject_of(target: str) -> str | None:
    """Sujet d'une cible préfixée (`ea/foo`) ou relative au wiki (`foo/bar`).

    `ea/service-orientation-and-apis` → `ea`. `service-orientation` → `None`
    (pas de sujet — c'est juste un nom de notion nu).
    """
    if "/" not in target:
        return None
    return target.split("/", 1)[0]


def _candidate_targets(cfg) -> list[tuple[str, str]]:
    """Liste `(cible_canonique, chemin_relatif)` de tous les articles/grilles résolvables.

    « Cible canonique » = la forme `sujet/notion` (sans préfixe `wiki/`) pour les
    articles, et `reading-grids/<slug>` pour les grilles. C'est la forme qu'on
    va **proposer** dans le rapport et utiliser pour la réécriture.
    """
    out: list[tuple[str, str]] = []
    if cfg.WIKI.is_dir():
        for p in sorted(cfg.WIKI.rglob("*.md")):
            if p.name.startswith("_"):
                continue  # _INDEX.md et _assets/_ ne sont pas des cibles de liens
            rel = p.relative_to(cfg.work_root).as_posix()
            subject, _, _ = rel[len("wiki/"):].partition("/")
            canonical = f"{subject}/{p.stem}"
            out.append((canonical, rel))
    if cfg.READING_GRIDS.is_dir():
        for p in sorted(cfg.READING_GRIDS.glob("*.md")):
            rel = p.relative_to(cfg.work_root).as_posix()
            canonical = f"reading-grids/{p.stem}"
            out.append((canonical, rel))
    return out


# --- passes de matching -----------------------------------------------------
def _common_prefix_len(a: str, b: str) -> int:
    """Longueur du préfixe commun caractère par caractère (≥ 0)."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _word_tokens(s: str) -> list[str]:
    """Découpe en **mots ASCII kebab** après normalisation diacritique.

    `service-orientation-and-apis` → `["service", "orientation", "and", "apis"]`
    `openapi-contract-first`       → `["openapi", "contract", "first"]`
    `état-de-l'art`                → `["etat", "de", "l", "art"]` (puis stop-words retirés)
    """
    decomposed = unicodedata.normalize("NFKD", s)
    ascii_only = "".join(
        ch for ch in decomposed if not unicodedata.combining(ch)
    ).lower()
    return [w for w in re.split(r"[^a-z0-9]+", ascii_only) if w]


def _words_for_match(target: str) -> list[str]:
    """Mots d'une cible pour la passe 3, **sans stop-words**."""
    notion = _notion_of(target)
    return [w for w in _word_tokens(notion) if w not in _STOP_WORDS]


def _prefix_match(broken: str, candidates: list[tuple[str, str]]) -> Proposal | None:
    """Passe 2 : meilleur préfixe commun char-par-char ≥ 3 chars, sur la notion.

    On compare la **notion** de `broken` à la **notion** de chaque candidat
    (les préfixes de sujet ne comptent pas). `ea/service-orientation-and-apis`
    et `ea/service-orientation-and-apis-v2` partagent le préfixe
    `service-orientation-and-apis` (longueur 30), bien qu'ils soient dans le
    même sujet.
    """
    broken_notion = _notion_of(broken)
    broken_subj = _subject_of(broken)
    best: tuple[int, tuple[str, str]] | None = None
    for canonical, rel in candidates:
        cand_subj = _subject_of(canonical)
        # Si la cible cassée a un sujet explicite, on n'autorise les candidats
        # du même sujet (ou les grilles `reading-grids/...`) — éviter de
        # proposer une notion d'un autre sujet sous le même préfixe.
        if broken_subj is not None and cand_subj is not None and cand_subj != broken_subj:
            continue
        cand_notion = _notion_of(canonical)
        n = _common_prefix_len(broken_notion, cand_notion)
        if n < _PREFIX_MIN:
            continue
        if best is None or n > best[0]:
            best = (n, (canonical, rel))
    if best is None:
        return None
    canonical, rel = best[1]
    return Proposal(
        src="",                  # rempli par l'appelant (on n'a pas la source ici)
        broken=broken,
        target=canonical,
        target_path=rel,
        provenance="prefix",
        marker=_MARK_AUTO,
        score=float(best[0]),
    )


def _words_match(broken: str, candidates: list[tuple[str, str]]) -> Proposal | None:
    """Passe 3 : matching par mots (stop-words retirés).

    Règle de sélection : **un mot long (≥ 6 chars) commun** OU **≥ 2 mots
    courts communs**. Si plusieurs candidats passent, on garde celui qui a le
    meilleur score (`0.5 + 0.1·|mots communs| + 0.2·|mots longs communs|`), puis
    la **notion la plus courte** (le candidat le plus spécifique, pas un fourre-tout).
    """
    broken_tokens = _words_for_match(broken)
    if not broken_tokens:
        return None
    broken_subj = _subject_of(broken)
    broken_long = {w for w in broken_tokens if len(w) >= _LONG_WORD_MIN}

    best: tuple[float, int, tuple[str, str]] | None = None
    for canonical, rel in candidates:
        cand_subj = _subject_of(canonical)
        if broken_subj is not None and cand_subj is not None and cand_subj != broken_subj:
            continue
        cand_tokens = _word_tokens(_notion_of(canonical))
        if not cand_tokens:
            continue
        common = set(broken_tokens) & set(cand_tokens)
        if not common:
            continue
        long_common = common & broken_long
        if not (long_common or len(common) >= _SHORT_WORDS_MIN):
            continue
        long_count = len(long_common)
        score = 0.5 + 0.1 * len(common) + 0.2 * long_count
        # tie-break : notion la plus courte d'abord (plus spécifique), puis alpha
        cand_notion_len = len(_notion_of(canonical))
        key = (score, -cand_notion_len, canonical)
        if best is None or key > (best[0], -best[2][0].count("/") and -len(_notion_of(best[1][0])),
                                  best[1][0]):
            best = (score, cand_notion_len, (canonical, rel))
    if best is None:
        return None
    canonical, rel = best[2]
    return Proposal(
        src="",
        broken=broken,
        target=canonical,
        target_path=rel,
        provenance="words",
        marker=_MARK_AUTO,
        score=best[0],
    )


def _identity_match(cfg, src_file: Path, broken: str) -> Proposal | None:
    """Passe 1 : la cible résout *telle quelle* — filet de sécurité."""
    if wikilinks.resolve(cfg, src_file, broken) is not None:
        return Proposal(
            src="",
            broken=broken,
            target=broken,
            target_path=wikilinks.resolve(cfg, src_file, broken)
                .relative_to(cfg.work_root).as_posix(),
            provenance="identity",
            marker=_MARK_AUTO,
            score=float("inf"),
        )
    return None


# --- filtrage par scope -----------------------------------------------------
def _in_scope(src: str, scopes: list[str] | None) -> bool:
    """Vrai si la source est sous l'un des `scopes`, ou si aucun scope n'est donné.

    `src` est un chemin relatif à `work_root` (`wiki/<sujet>/...` ou
    `reading-grids/<slug>.md`). Un scope `ea` matche `wiki/ea/...` (préfixe
    `wiki/<scope>/`) et `reading-grids/<scope>` n'a pas de notion de sujet — on
    borne donc strictement aux fichiers sous `wiki/`.
    """
    if not scopes:
        return True
    for scope in scopes:
        if src.startswith(f"wiki/{scope}/"):
            return True
    return False


# --- pipeline principal -----------------------------------------------------
def _resolve_override(
    cfg, broken: str, semantic_map: dict[str, str]
) -> Proposal | None:
    """Passe 0 : override manuel, si présent et résout."""
    if broken not in semantic_map:
        return None
    target = semantic_map[broken]
    # Le target doit résoudre vers un fichier existant.
    candidates = [
        cfg.work_root / f"{target}.md",
        cfg.WIKI / f"{target}.md",
    ]
    for cand in candidates:
        if cand.is_file() and guard.is_within(cfg.work_root, cand):
            return Proposal(
                src="",
                broken=broken,
                target=target,
                target_path=cand.relative_to(cfg.work_root).as_posix(),
                provenance="override",
                marker=_MARK_OVERRIDE,
                score=float("inf"),
            )
    return None  # override pointe dans le vide : on laisse les passes auto tenter


def _propose_for_link(
    cfg,
    src_file: Path,
    src_rel: str,
    broken: str,
    candidates: list[tuple[str, str]],
    semantic_map: dict[str, str],
) -> Proposal | None:
    """Enchaîne les passes 0 → 1 → 2 → 3 sur **un** lien cassé."""
    p = _resolve_override(cfg, broken, semantic_map)
    if p is not None:
        p.src = src_rel
        return p
    p = _identity_match(cfg, src_file, broken)
    if p is not None:
        p.src = src_rel
        return p
    p = _prefix_match(broken, candidates)
    if p is not None:
        p.src = src_rel
        return p
    p = _words_match(broken, candidates)
    if p is not None:
        p.src = src_rel
        return p
    return None


def _load_semantic_map(path: str | None) -> dict[str, str]:
    """Charge `--semantic-map <path>` (JSON `{broken: target}`). Vide si non fourni."""
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))


def repair(
    cfg,
    *,
    scope: list[str] | None = None,
    semantic_map_path: str | None = None,
    apply: bool = False,
) -> RepairReport:
    """Exécute la détection + le matching + (optionnellement) la réécriture.

    `scope`           : liste de sujets (`wiki/<scope>/...`) à borner ; None = tout.
    `semantic_map_path` : chemin d'un JSON `{broken: target}` pour la passe 0.
    `apply`           : si `True`, réécrit via `atomic_write_text`. Sinon, preview.
    """
    semantic_map = _load_semantic_map(semantic_map_path)
    candidates = _candidate_targets(cfg)
    report = link_audit.audit(cfg)
    out = RepairReport()

    # Regroupement par source pour minimiser les lectures/écritures en mode --apply.
    per_src: dict[str, list[tuple[Path, str]]] = {}
    for src_rel, broken in report.broken:
        if not _in_scope(src_rel, scope):
            continue
        per_src.setdefault(src_rel, []).append(
            (cfg.work_root / src_rel, broken)
        )

    rewrites_per_file: dict[str, list[tuple[str, str, str]]] = {}
    for src_rel, items in per_src.items():
        src_file = cfg.work_root / src_rel
        for src_path, broken in items:
            p = _propose_for_link(
                cfg, src_path, src_rel, broken, candidates, semantic_map,
            )
            if p is None:
                continue
            out.proposals.append(p)
            if apply:
                rewrites_per_file.setdefault(src_rel, []).append(
                    (broken, p.target, p.marker)
                )

    if apply:
        for src_rel, edits in rewrites_per_file.items():
            src_file = cfg.work_root / src_rel
            new_text = _rewrite_file(src_file, edits)
            if new_text is not None:
                iohelpers.atomic_write_text(
                    src_file, new_text, work_root=cfg.work_root
                )
                for old, new, _marker in edits:
                    out.rewrites.append((src_rel, old, new))
        # Re-audit best-effort (peut rester cassé pour d'autres raisons : scope, etc.).
        try:
            out.audit_after_ok = link_audit.audit(cfg).ok
        except Exception:  # pragma: no cover - best-effort
            out.audit_after_ok = None

    return out


# --- réécriture de fichier --------------------------------------------------
def _rewrite_file(
    src_file: Path, edits: list[tuple[str, str, str]]
) -> str | None:
    """Applique les éditions `(old, new, marker)` à un fichier.

    On matche `[[<old>` (en respectant l'**absence** d'un `/` ou d'un `]` juste
    après, pour éviter de matcher par hasard `[[old/autre-pertinent]]`), et on
    préserve l'éventuelle ancre `#…` ou alias `|…` qui suit.

    Renvoie `None` si le fichier n'existe pas ou n'a pas pu être lu (no-op).
    """
    try:
        text = src_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for old, new, _marker in edits:
        # `(?<=\[\[)` : on est juste après `[[`, pour ne pas matcher
        # accidentellement au milieu d'un texte (improbable en markdown mais
        # mieux vaut être strict).
        # `(?P<tail>(?:#[^\]|]+)?(?:\|[^\]]+)?)` : ancre + alias optionnels.
        # `(?:\]\]|\|[^\]]+\]\])` : fin = `]]` ou `|alias]]`.
        pattern = re.compile(
            r"(?<=\[\[)"
            + re.escape(old)
            + r"(?P<tail>(?:#[^\]|]+)?(?:\|[^\]]+)?)"
            + r"(?=\]\])"
        )
        text = pattern.sub(new + r"\g<tail>", text)
    return text


# --- rendu texte (dry-run) --------------------------------------------------
def render_report(report: RepairReport, *, today: str, applied: bool) -> str:
    """Rapport lisible pour le dry-run (et pour le résumé post-apply)."""
    mode = "APPLY" if applied else "DRY-RUN"
    lines = [
        f"REPAIR wiki — {today} — {mode}",
        f"Propositions : {len(report.proposals)}",
    ]
    if applied:
        lines.append(f"Réécritures effectives : {len(report.rewrites)}")
        if report.audit_after_ok is not None:
            lines.append(
                "Re-audit : OK (0 anomalie)" if report.audit_after_ok
                else "Re-audit : KO (anomalies restantes)"
            )
    if not report.proposals:
        lines.append("Rien à proposer.")
        return "\n".join(lines) + "\n"
    lines.append("")
    for p in report.proposals:
        lines.append(
            f"  {p.marker} {p.src} : [[{p.broken}]] → [[{p.target}]]  "
            f"({p.provenance}, score={p.score:g})"
        )
    return "\n".join(lines) + "\n"
