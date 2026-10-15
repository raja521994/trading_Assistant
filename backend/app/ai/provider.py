"""Provider-agnostic LLM interface. LLMs only rephrase/interpret supplied facts; they never produce
numbers, never place orders and never override risk controls."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Explanation:
    facts: list[str] = field(default_factory=list)
    calculations: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    interpretation: list[str] = field(default_factory=list)
    provider: str = "template"
    warnings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fact_text(self) -> str:
        return "\n".join(self.facts + self.calculations + self.assumptions + self.interpretation)


class LLMProvider(ABC):
    name = "base"

    @abstractmethod
    def interpret(self, question: str, base: Explanation) -> Explanation:
        """Return an explanation whose facts/calculations/assumptions are copied from `base` unchanged."""


_NUM = re.compile(r"(?<![A-Za-z])-?\d+(?:,\d{3})*(?:\.\d+)?")


def numbers_in(text: str) -> set[float]:
    out = set()
    for m in _NUM.findall(text):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            continue
    return out


def unsupported_numbers(generated: str, source: str) -> list[float]:
    """Numbers in LLM output that do not appear in the supplied facts (tolerance 0.01, small ints allowed)."""
    src = numbers_in(source)
    bad = []
    for n in numbers_in(generated):
        if abs(n) <= 10 and float(n).is_integer():
            continue  # list ordinals / small counts
        if not any(abs(n - s) <= 0.011 for s in src):
            bad.append(n)
    return bad
