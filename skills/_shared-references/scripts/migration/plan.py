#!/usr/bin/env python3
"""Plan d'un palier de migration — listing (avant → après) sans effet de bord.

Le `Plan` est le **contrat dry-run** d'un palier : c'est ce que la fonction
`Migration.plan(cfg) -> Plan` renvoie, et c'est ce que l'humain valide
visuellement avant l'`apply`. Aucune mutation du vault : un plan est un
objet-valeur, recalculable à l'identique tant que le vault n'a pas changé.

Forme : une suite d'**opérations** typées. Pour ce socle, on supporte :

- `MOVE(src, dst)`   : déplacement atomique (le plus courant).
- `WRITE(path, content)` : écriture atomique (ex. `.mimir/vault.json`).
- `DELETE(path)`     : suppression atomique (rare, justifiée par l'audit).

Chaque opération est **idempotente** : ré-exécutée sur un état déjà migré
= no-op (test par `Plan.is_noop(state)` ou par l'apply lui-même).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

Action = Literal["move", "write", "delete"]


@dataclass(frozen=True)
class Operation:
    """Une opération atomique du plan — déclarative, sans effet de bord."""

    action: Action
    src: Path
    dst: Path | None = None  # seulement pour `move` et `write` (dst = src pour delete)
    content: str | None = None  # seulement pour `write`

    def __post_init__(self) -> None:
        if self.action in ("move", "write") and self.dst is None:
            raise ValueError(f"opération {self.action!r} requiert un `dst`")
        if self.action == "write" and self.content is None:
            raise ValueError("opération `write` requiert un `content`")
        if self.action == "delete" and self.dst is not None:
            raise ValueError("opération `delete` ne prend pas de `dst`")


@dataclass
class Plan:
    """Liste ordonnée d'opérations pour un palier. **Aucune écriture**."""

    operations: list[Operation] = field(default_factory=list)
    description: str = ""

    def add(self, op: Operation) -> None:
        self.operations.append(op)

    @property
    def empty(self) -> bool:
        return not self.operations

    def __len__(self) -> int:
        return len(self.operations)

    def __iter__(self) -> Iterable[Operation]:
        return iter(self.operations)

    def render(self) -> str:
        """Rendu texte pour validation humaine (lecture seule)."""
        lines = [f"Plan: {self.description or '(sans description)'}"]
        if not self.operations:
            lines.append("  (vide — palier déjà appliqué ou sans effet)")
            return "\n".join(lines) + "\n"
        lines.append(f"  {len(self.operations)} opération(s) :")
        for i, op in enumerate(self.operations, 1):
            tag = {"move": "MOVE  ", "write": "WRITE ", "delete": "DELETE"}[op.action]
            if op.action == "move":
                lines.append(f"  {i:>3}. {tag}  {op.src}  →  {op.dst}")
            elif op.action == "write":
                preview = (op.content or "").splitlines()[0][:60] if op.content else ""
                lines.append(f"  {i:>3}. {tag}  {op.dst}  (← {preview!r})")
            else:  # delete
                lines.append(f"  {i:>3}. {tag}  {op.src}")
        return "\n".join(lines) + "\n"


# --- fabriques d'opérations (lisibles) --------------------------------------

def MOVE(src: Path, dst: Path) -> Operation:
    return Operation(action="move", src=Path(src), dst=Path(dst))


def WRITE(path: Path, content: str) -> Operation:
    return Operation(action="write", src=Path(path), dst=Path(path), content=content)


def DELETE(path: Path) -> Operation:
    return Operation(action="delete", src=Path(path))
