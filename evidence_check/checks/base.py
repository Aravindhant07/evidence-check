"""Check plugins. Every check returns Findings with a pointer to the evidence behind them."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from ..models import Case, Evidence, Finding
from ..policy import DomainPolicy


@dataclass
class Context:
    case: Case
    evidence: list[Evidence]
    policy: DomainPolicy
    work_dir: Path  # heatmaps and other artefacts
    history: Any = None  # HistoryIndex, when available
    models: Any = None  # model registry, when available
    skipped: list[str] = field(default_factory=list)

    def of_role(self, *roles: str) -> list[Evidence]:
        return [e for e in self.evidence if e.role in roles]


class Check(Protocol):
    id: str
    layer: Literal["item", "case", "history"]

    def run(self, ctx: Context) -> list[Finding]: ...


REGISTRY: dict[str, Check] = {}


def register(cls: Callable[[], Check]) -> Callable[[], Check]:
    inst = cls()
    REGISTRY[inst.id] = inst
    return cls
