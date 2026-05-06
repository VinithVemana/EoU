# src/claim_url/llm/ — LLM provider adapters

Single facade (`LLMClient`) over three provider SDKs.

## Files

```
base.py              # LLMClient facade: complete(system, prompt, ..., json_mode), retries, jitter
openai_provider.py   # OpenAIProvider
claude_provider.py   # ClaudeProvider (Anthropic)
google_provider.py   # GoogleProvider (Gemini, google-genai)
```

## Facade contract (`base.py::LLMClient.complete`)

```
complete(system: str, prompt: str, *, json_mode: bool = False, ...) -> str
```

- Jittered exponential backoff on transient errors.
- Returns the raw text payload. Callers parse JSON via `utils.parse_json_object` (handles markdown fences and prose-wrapped JSON) — do NOT `json.loads` directly.
- **Thread-safe.** `LLMClient.complete` may be called concurrently from multiple worker threads (Agent 2 batch scoring, future stages). `usage` accumulation is guarded by an internal `threading.Lock`; provider implementations return `(text, prompt_tokens, completion_tokens)` so concurrent calls don't race on a shared `last_usage` attribute.

## Provider quirks

- **OpenAIProvider** picks `max_completion_tokens` for `gpt-5.x` / `o1` / `o3` / `o4` reasoning models and `max_tokens` for legacy chat models. One-shot retry if the API rejects the chosen kwarg — keep that retry in place when adding new model families.
- **GoogleProvider** uses `response_mime_type="application/json"` for JSON mode.
- **ClaudeProvider** has no JSON mode here — relies on prompt instructions + `parse_json_object` fallback. If you add native JSON support, keep the parser fallback for older Anthropic SDK versions.

## Lazy SDK imports

Each provider imports its SDK only on construction. Installing only one of `openai` / `anthropic` / `google-genai` is sufficient — the unused providers must not import their SDK at module load time. Don't move imports to module top.

## Adding a new provider

1. New file `<name>_provider.py` with a class exposing `complete(...) -> tuple[str, int, int]` (text, prompt_tokens, completion_tokens).
2. Lazy-import the SDK inside `__init__`.
3. Add an `LLMProvider` enum entry in `config.py` and required env-var name.
4. Wire it into `LLMClient` dispatch in `base.py`.
5. Add a fixture/mock in `tests/conftest.py` mirroring the existing patterns. The mock's `complete` must return the 3-tuple — see `tests/test_cache.py::_FakeProvider`.
