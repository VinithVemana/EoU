"""DiskCache + cache-aware client tests (no network)."""

from __future__ import annotations

from typing import Any

import pytest

from claim_url.cache import DiskCache
from claim_url.config import LLMProvider
from claim_url.llm.base import LLMClient
from claim_url.models import SearchResult
from claim_url.serp import SerpApiClient


# ---------- DiskCache ----------------------------------------------------


def test_diskcache_roundtrip(tmp_path):
    cache = DiskCache(tmp_path, "ns")
    key = {"a": 1, "b": "x"}

    assert cache.get(key) is None
    assert cache.misses == 1

    cache.set(key, {"value": 42})
    assert cache.writes == 1

    assert cache.get(key) == {"value": 42}
    assert cache.hits == 1


def test_diskcache_disabled_is_noop(tmp_path):
    cache = DiskCache(tmp_path, "ns", enabled=False)
    cache.set({"k": 1}, "v")
    assert cache.get({"k": 1}) is None
    assert cache.hits == 0
    assert cache.writes == 0


def test_diskcache_root_none_disabled():
    cache = DiskCache(None, "ns")
    assert cache.enabled is False
    cache.set({"k": 1}, "v")
    assert cache.get({"k": 1}) is None


def test_diskcache_keys_canonical_independent_of_dict_order(tmp_path):
    cache = DiskCache(tmp_path, "ns")
    cache.set({"a": 1, "b": 2}, "value")
    assert cache.get({"b": 2, "a": 1}) == "value"


# ---------- SerpApiClient cache --------------------------------------------


class _FakeGoogleSearch:
    """Stub that mirrors serpapi.GoogleSearch(params).get_dict()."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params
        _FakeGoogleSearch.calls += 1

    calls = 0

    def get_dict(self) -> dict[str, Any]:
        return {
            "organic_results": [
                {"link": "https://example.com/a", "title": "A", "snippet": "snip"}
            ]
        }


@pytest.fixture
def fake_serp(monkeypatch, tmp_path):
    _FakeGoogleSearch.calls = 0

    cache = DiskCache(tmp_path, "serp")
    client = SerpApiClient(api_key="dummy", cache=cache)
    monkeypatch.setattr(client, "_google_search_cls", _FakeGoogleSearch)
    return client, cache


def test_serpapi_cache_hit_skips_network(fake_serp):
    client, cache = fake_serp

    first = client.search("foo bar", num=5)
    second = client.search("foo bar", num=5)

    assert first == second
    assert _FakeGoogleSearch.calls == 1  # second call satisfied from cache
    assert cache.hits == 1
    assert cache.writes == 1
    assert isinstance(second[0], SearchResult)


def test_serpapi_cache_distinguishes_num(fake_serp):
    client, _ = fake_serp
    client.search("q", num=5)
    client.search("q", num=10)
    assert _FakeGoogleSearch.calls == 2  # different num = different key


# ---------- LLMClient cache ------------------------------------------------


class _FakeProvider:
    model = "claude-haiku-4-5"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, **kwargs: Any) -> tuple[str, int, int]:
        self.calls += 1
        return ('{"ok": true}', 10, 5)


@pytest.fixture
def fake_llm(monkeypatch, tmp_path):
    cache = DiskCache(tmp_path, "llm")
    fake = _FakeProvider()
    monkeypatch.setattr(LLMClient, "_make_provider", staticmethod(lambda *a, **k: fake))
    client = LLMClient(provider=LLMProvider.CLAUDE, cache=cache)
    return client, fake, cache


def test_llmclient_cache_hit_records_savings(fake_llm):
    client, fake, cache = fake_llm

    first = client.complete(system="s", prompt="p", max_tokens=100, temperature=0.0)
    second = client.complete(system="s", prompt="p", max_tokens=100, temperature=0.0)

    assert first == second == '{"ok": true}'
    assert fake.calls == 1  # second served from cache
    assert client.usage.calls == 1
    assert client.usage.cache_hits == 1
    assert client.usage.cached_prompt_tokens == 10
    assert client.usage.cached_completion_tokens == 5
    assert cache.hits == 1


def test_llmclient_skips_cache_when_temperature_nonzero(fake_llm):
    client, fake, cache = fake_llm

    client.complete(system="s", prompt="p", max_tokens=100, temperature=0.7)
    client.complete(system="s", prompt="p", max_tokens=100, temperature=0.7)

    assert fake.calls == 2  # nondeterministic = no caching
    assert cache.hits == 0
    assert client.usage.cache_hits == 0
