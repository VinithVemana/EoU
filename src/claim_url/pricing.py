"""Token usage tracking and per-model cost estimation.

Pricing here is a snapshot of published list prices (USD per 1M tokens).
Prices change — treat the totals as estimates, not invoices. Update the
:data:`PRICING` table when providers change rates. Unknown models still
get token totals; only the cost line shows ``n/a``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# (input_per_mtok_usd, output_per_mtok_usd). Prefix-matched against the
# model id, longest match wins.
PRICING: dict[str, tuple[float, float]] = {
    # OpenAI
    "gpt-5.4-mini":      (0.25, 2.00),
    "gpt-5.4":           (1.25, 10.00),
    "gpt-5-mini":        (0.25, 2.00),
    "gpt-5-nano":        (0.05, 0.40),
    "gpt-5":             (1.25, 10.00),
    "gpt-4o-mini":       (0.15, 0.60),
    "gpt-4o":            (2.50, 10.00),
    "gpt-4-turbo":       (10.00, 30.00),
    "gpt-4":             (30.00, 60.00),
    "gpt-3.5-turbo":     (0.50, 1.50),
    "o1-mini":           (3.00, 12.00),
    "o1":                (15.00, 60.00),
    "o3-mini":           (1.10, 4.40),
    "o3":                (2.00, 8.00),
    "o4-mini":           (1.10, 4.40),
    # Anthropic
    "claude-opus-4":     (15.00, 75.00),
    "claude-sonnet-4":   (3.00, 15.00),
    "claude-haiku-4":    (1.00, 5.00),
    "claude-3-5-sonnet": (3.00, 15.00),
    "claude-3-5-haiku":  (0.80, 4.00),
    "claude-3-opus":     (15.00, 75.00),
    "claude-3-sonnet":   (3.00, 15.00),
    "claude-3-haiku":    (0.25, 1.25),
    # Google
    "gemini-2.5-pro":    (1.25, 10.00),
    "gemini-2.5-flash":  (0.30, 2.50),
    "gemini-2.0-flash":  (0.10, 0.40),
    "gemini-1.5-pro":    (1.25, 5.00),
    "gemini-1.5-flash":  (0.075, 0.30),
}


def lookup_pricing(model: str) -> Optional[tuple[float, float]]:
    """Return ``(input_rate, output_rate)`` per 1M tokens, or ``None`` if unknown.

    Longest-prefix match — ``"gpt-4o-mini-2024-07-18"`` matches ``"gpt-4o-mini"``
    rather than the more general ``"gpt-4o"``.
    """
    if not model:
        return None
    name = model.lower()
    best_key: Optional[str] = None
    for key in PRICING:
        if name.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return PRICING[best_key] if best_key else None


@dataclass
class UsageStats:
    """Accumulator for token + cost stats across all LLM calls in a run."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Optional[float] = 0.0
    pricing_known: bool = True

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def record(
        self,
        *,
        prompt: int,
        completion: int,
        pricing: Optional[tuple[float, float]],
    ) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        if pricing is None:
            self.pricing_known = False
            self.cost_usd = None
            return
        if not self.pricing_known or self.cost_usd is None:
            return
        in_rate, out_rate = pricing
        self.cost_usd += (prompt / 1_000_000.0) * in_rate
        self.cost_usd += (completion / 1_000_000.0) * out_rate


__all__ = ["PRICING", "UsageStats", "lookup_pricing"]
