"""The token cost model and count formatting — the only place pricing lives."""
from __future__ import annotations

from dataclasses import dataclass

UNITS = (("B", 1_000_000_000.0), ("M", 1_000_000.0), ("K", 1_000.0))
"""Suffixes for numbers >= 1000, largest first."""


@dataclass(frozen=True)
class Pricing:
    cached_prompt: float = 0.05
    prompt: float = 0.2
    completion: float = 1.0

    def cost(self, *, prompt_tokens: int, cached_prompt_tokens: int, completion_tokens: int) -> float:
        uncached = max(prompt_tokens - cached_prompt_tokens, 0)
        return (self.cached_prompt * cached_prompt_tokens
                + self.prompt * uncached
                + self.completion * completion_tokens)

    def cost_sql(self, table: str = "token") -> str:
        """The same formula as SQL, so aggregates reprice history when `PRICING` changes."""
        cached = f"COALESCE({table}.prompt_tokens_cached, 0)"
        uncached = f"MAX({table}.prompt_tokens - {cached}, 0)"
        return (f"{self.cached_prompt} * {cached}"
                f" + {self.prompt} * {uncached}"
                f" + {self.completion} * {table}.completion_tokens")


PRICING = Pricing()
"""The weights in effect; tweak them here to re-estimate cost everywhere."""


def format_count(value: float | None, digits: int = 2) -> str:
    """Render a count with a K/M/B unit above 1000; None renders as '-'."""
    if value is None:
        return "-"
    for suffix, size in UNITS:
        if abs(value) >= size:
            return f"{value / size:.{digits}f}{suffix}"
    return f"{value:,.0f}"


def parse_count(text: str) -> float:
    """Inverse of `format_count`: '1234', '12.5K', '0.25M', '1B' -> float."""
    stripped = text.strip().upper()
    for suffix, size in UNITS:
        if stripped.endswith(suffix):
            return float(stripped[:-1]) * size
    return float(stripped)
