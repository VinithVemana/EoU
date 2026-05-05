# tests/ — pytest suite

Mocked LLM + Serp; no network, no API keys needed.

## Files

```
conftest.py           # puts src/ on sys.path so `pytest tests/` works pre-install
test_models.py        # dataclass round-trips
test_utils.py         # normalize_domain, domain_matches, parse_json_object, dedupe, chunked
test_extractor.py     # ClaimElementExtractor (mocked LLM)
test_search.py        # OfficialDomainSearch filter + (query, domain) cache
test_relevance.py     # RelevanceCheckingAgent batching + dedupe semantics
test_finder.py        # ClaimURLFinder.run end-to-end with mocks
```

## Conventions

- **No real network calls.** Mock `LLMClient.complete` and `SerpApiClient.search` at the boundary. Page fetch tests should mock `requests.Session.get`.
- **No real API keys.** Tests must pass with `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `SERPAPI_API_KEY` unset.
- **`conftest.py` puts `src/` on `sys.path`** so `pytest tests/` works without installing the package. `pyproject.toml`'s `[tool.pytest.ini_options]` does this too — keep both in sync if you change the layout.
- **Adding a new provider in `llm/`** → mirror existing mock patterns in `conftest.py` so tests don't need that provider's SDK installed.
- **Adding a new agent stage** → add a corresponding `test_<stage>.py` and a path through `test_finder.py`.

## Running

```bash
PY=/Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python
$PY -m pytest                                          # all
$PY -m pytest tests/test_finder.py -k end_to_end -v    # one test
$PY -m pytest --cov=claim_url --cov-report=term-missing
```
