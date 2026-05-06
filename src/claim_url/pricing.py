"""Token usage tracking and per-model cost estimation.

Pricing here is a snapshot of published list prices (USD per 1M tokens).
Prices change — treat the totals as estimates, not invoices. Update the
:data:`PRICING` table when providers change rates. Unknown models still
get token totals; only the cost line shows ``n/a``.

Some providers (Gemini 2.5 Pro, 3.1 Pro, GPT-5.5/5.4 long-context) charge
a higher rate once the prompt exceeds a threshold (typically 200k tokens).
:class:`ModelRates` captures both tiers; :meth:`ModelRates.rates_for`
picks the right pair for a given prompt size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelRates:
    """USD-per-1M-token rates with optional long-context tier.

    ``input_long`` / ``output_long`` apply when the prompt exceeds
    ``long_context_threshold`` tokens. If either is ``None`` the short
    tier is used at all sizes.
    """

    input_short: float
    output_short: float
    input_long: Optional[float] = None
    output_long: Optional[float] = None
    long_context_threshold: int = 200_000

    def rates_for(self, prompt_tokens: int) -> tuple[float, float]:
        if (
            self.input_long is not None
            and self.output_long is not None
            and prompt_tokens > self.long_context_threshold
        ):
            return self.input_long, self.output_long
        return self.input_short, self.output_short


def _r(in_short: float, out_short: float,
       in_long: Optional[float] = None,
       out_long: Optional[float] = None) -> ModelRates:
    return ModelRates(in_short, out_short, in_long, out_long)


# Prefix-matched against the model id, longest match wins.
PRICING: dict[str, ModelRates] = {
    # OpenAI — long tier from public pricing page (>200k prompt).
    "gpt-5.5-pro":       _r(30.00, 180.00, 60.00, 270.00),
    "gpt-5.5":           _r(5.00,  30.00,  10.00, 45.00),
    "gpt-5.4-pro":       _r(30.00, 180.00, 60.00, 270.00),
    "gpt-5.4-nano":      _r(0.20,  1.25),
    "gpt-5.4-mini":      _r(0.75,  4.50),
    "gpt-5.4":           _r(2.50,  15.00,  5.00,  22.50),
    "gpt-5.2-pro":       _r(21.00, 168.00),
    "gpt-5.2":           _r(1.75,  14.00),
    "gpt-5.1":           _r(1.25,  10.00),
    "gpt-5-pro":         _r(15.00, 120.00),
    "gpt-5-mini":        _r(0.25,  2.00),
    "gpt-5-nano":        _r(0.05,  0.40),
    "gpt-5":             _r(1.25,  10.00),
    "gpt-4.1-nano":      _r(0.10,  0.40),
    "gpt-4.1-mini":      _r(0.40,  1.60),
    "gpt-4.1":           _r(2.00,  8.00),
    "gpt-4o-mini":       _r(0.15,  0.60),
    "gpt-4o":            _r(2.50,  10.00),
    "gpt-4-turbo":       _r(10.00, 30.00),
    "gpt-4":             _r(30.00, 60.00),
    "gpt-3.5-turbo":     _r(0.50,  1.50),
    "o1-mini":           _r(3.00,  12.00),
    "o1":                _r(15.00, 60.00),
    "o3-mini":           _r(1.10,  4.40),
    "o3":                _r(2.00,  8.00),
    "o4-mini":           _r(1.10,  4.40),
    # Anthropic — single tier.
    "claude-opus-4-7":   _r(5.00,  25.00),
    "claude-opus-4-6":   _r(5.00,  25.00),
    "claude-opus-4-5":   _r(5.00,  25.00),
    "claude-opus-4-1":   _r(15.00, 75.00),
    "claude-opus-4":     _r(15.00, 75.00),
    "claude-sonnet-4-6": _r(3.00,  15.00),
    "claude-sonnet-4-5": _r(3.00,  15.00),
    "claude-sonnet-4":   _r(3.00,  15.00),
    "claude-3-7-sonnet": _r(3.00,  15.00),
    "claude-haiku-4-5":  _r(1.00,  5.00),
    "claude-3-5-sonnet": _r(3.00,  15.00),
    "claude-3-5-haiku":  _r(0.80,  4.00),
    "claude-3-opus":     _r(15.00, 75.00),
    "claude-3-sonnet":   _r(3.00,  15.00),
    "claude-3-haiku":    _r(0.25,  1.25),
    # Google — long tier (>200k prompt) where Google publishes one.
    "gemini-3.1-pro":         _r(2.00,  12.00, 4.00, 18.00),
    "gemini-3.1-flash-lite":  _r(0.25,  1.50),
    "gemini-3.1-flash-live":  _r(0.75,  4.50),
    "gemini-3.1-flash":       _r(0.75,  4.50),
    "gemini-3-pro-image":     _r(2.00,  12.00),
    "gemini-3-flash":         _r(0.50,  3.00),
    "gemini-2.5-pro":         _r(1.25,  10.00, 2.50, 15.00),
    "gemini-2.5-flash-lite":  _r(0.10,  0.40),
    "gemini-2.5-flash":       _r(0.30,  2.50),
    "gemini-2.0-flash":       _r(0.10,  0.40),
    "gemini-1.5-pro":         _r(1.25,  5.00,  2.50, 10.00),
    "gemini-1.5-flash":       _r(0.075, 0.30,  0.15, 0.60),
}


def lookup_pricing(model: str) -> Optional[ModelRates]:
    """Return :class:`ModelRates` for ``model``, or ``None`` if unknown.

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
    """Accumulator for token + cost stats across all LLM calls in a run.

    Cache hits are tracked separately: ``cache_hits`` /
    ``cached_prompt_tokens`` / ``cached_completion_tokens`` /
    ``cached_cost_usd`` reflect what the LLM cache *saved* — i.e. tokens
    and dollars that would have been billed if the cache were absent.
    The non-cached counters reflect actual API usage in this run.
    """

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Optional[float] = 0.0
    pricing_known: bool = True

    cache_hits: int = 0
    cached_prompt_tokens: int = 0
    cached_completion_tokens: int = 0
    cached_cost_usd: Optional[float] = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cached_total_tokens(self) -> int:
        return self.cached_prompt_tokens + self.cached_completion_tokens

    def record(
        self,
        *,
        prompt: int,
        completion: int,
        pricing: Optional[ModelRates],
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
        in_rate, out_rate = pricing.rates_for(prompt)
        self.cost_usd += (prompt / 1_000_000.0) * in_rate
        self.cost_usd += (completion / 1_000_000.0) * out_rate

    def record_cache_hit(
        self,
        *,
        prompt: int,
        completion: int,
        pricing: Optional[ModelRates],
    ) -> None:
        """Record what the cache saved on a hit (no API call was made)."""
        self.cache_hits += 1
        self.cached_prompt_tokens += prompt
        self.cached_completion_tokens += completion
        if pricing is None or self.cached_cost_usd is None:
            self.cached_cost_usd = None
            return
        in_rate, out_rate = pricing.rates_for(prompt)
        self.cached_cost_usd += (prompt / 1_000_000.0) * in_rate
        self.cached_cost_usd += (completion / 1_000_000.0) * out_rate


__all__ = ["PRICING", "ModelRates", "UsageStats", "lookup_pricing"]
