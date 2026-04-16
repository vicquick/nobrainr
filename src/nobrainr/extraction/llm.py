"""LLM inference helper — supports llama-server (primary) and Ollama (fallback).

Retry + timeout policy (April 2026 industry standard, documented in Appendix H
of OVERNIGHT_REPORT_2026-04-11.md + nobrainr memory
``ollama-chat-retry-policy-2026-04-11``).

Why the defaults below look the way they do:

* **DEFAULT_LLM_TIMEOUT = 600s** matches OpenAI Python SDK default (10 min) and
  llama.cpp server's own ``LLAMA_ARG_TIMEOUT`` default (600s). Qwen3.5-35B-A3B
  is pinned at ``n_parallel=1`` because its hybrid SWA architecture invalidates
  recurrent state between requests when slots > 1 (see nobrainr memory
  ``id:019d3f0e``). With one slot, requests queue inside llama-server and wait
  their turn. The client's job is to WAIT, not to retry.

* **RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 529}** — transient server
  errors worth retrying. Matches OpenAI SDK (408/409/429/5xx) modulo Anthropic's
  529 (overloaded_error). ``404`` is explicitly excluded because it almost
  always signals a config error, not a transient condition.

* **RETRYABLE_EXCEPTIONS** — ``ConnectError``, ``PoolTimeout``,
  ``RemoteProtocolError`` only. Critically, ``httpx.ReadTimeout`` and
  ``httpx.WriteTimeout`` are NOT retried: when the single-slot llama-server
  queues a request, the client's read-timeout fires before the server returns.
  Retrying pushes a DUPLICATE request onto the queue behind the still-running
  first one, doubling load and strictly making things worse. This is the exact
  cascade that caused quality_scoring / contextual_prefix_backfill /
  entity_enrichment to time out 5+ times in 24h on 2026-04-10/11. Instead,
  raise ReadTimeout so the caller lengthens its own timeout next time.

* **Decorrelated jitter** (AWS Builders' Library canonical pattern since 2015):
  ``sleep = min(cap, uniform(base, prev * 3))``. This beats raw exponential
  backoff when multiple clients contend — which is exactly our pattern when
  scheduler jobs and MCP user calls fire concurrently.

* **DEFAULT_LLM_MAX_RETRIES = 2** (3 total attempts) matches OpenAI SDK default.
  Before this change the retry count was 5 (unconfigurable) which amplified
  the ReadTimeout cascade by ~3×.
"""

import asyncio
import json
import logging
import random
import re

import httpx

from nobrainr.config import settings

logger = logging.getLogger("nobrainr")

# ──────────────────────────────────────────────
# Retry + timeout policy (see module docstring for rationale)
# ──────────────────────────────────────────────

# Default per-request HTTP timeout. Matches OpenAI Python SDK + llama.cpp
# server ``LLAMA_ARG_TIMEOUT`` default. The server-side queue for our single
# slot can legitimately hold requests for minutes when multiple callers
# contend; 10 minutes gives enough headroom for realistic bursts while the
# job-level ``asyncio.wait_for(..., LLM_JOB_TIMEOUT=30*60)`` remains the
# ultimate safety net in scheduler.py.
DEFAULT_LLM_TIMEOUT = 600.0

# Max retry attempts per call (total attempts = 1 + this). 2 matches OpenAI
# Python SDK's default and is small enough to avoid amplifying load during
# contention.
DEFAULT_LLM_MAX_RETRIES = 2

# HTTP statuses that indicate a transient server condition. 404 is NOT here
# — it means the endpoint/model is wrong, not that we should retry. 529 is
# Anthropic's overloaded_error, harmless to include for forward-compat.
RETRYABLE_STATUSES = {429, 500, 502, 503, 504, 529}

# httpx exceptions worth retrying. Importantly excludes ReadTimeout and
# WriteTimeout — those mean "the client gave up waiting on a server that's
# probably still working" and retrying makes the queue worse, not better.
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

# Decorrelated jitter parameters (AWS standard mode).
_JITTER_BASE = 0.2   # min sleep
_JITTER_CAP = 30.0   # max sleep per retry


def _next_jitter_sleep(prev: float) -> float:
    """Decorrelated-jitter backoff: ``min(cap, uniform(base, prev * 3))``."""
    return min(_JITTER_CAP, random.uniform(_JITTER_BASE, max(_JITTER_BASE, prev) * 3))


_llm_client: httpx.AsyncClient | None = None
_ollama_client: httpx.AsyncClient | None = None


def _get_llm_client() -> httpx.AsyncClient:
    """Client for llama-server (GPU LLM inference).

    Base timeout matches ``DEFAULT_LLM_TIMEOUT`` so that calls without an
    explicit per-request timeout still get the April-2026-standard 10-min
    wait window instead of the previous 5-min one.
    """
    global _llm_client
    if _llm_client is None or _llm_client.is_closed:
        _llm_client = httpx.AsyncClient(
            base_url=settings.llm_server_url, timeout=DEFAULT_LLM_TIMEOUT
        )
    return _llm_client


def _get_ollama_client() -> httpx.AsyncClient:
    """Client for Ollama (embeddings only)."""
    global _ollama_client
    if _ollama_client is None or _ollama_client.is_closed:
        _ollama_client = httpx.AsyncClient(
            base_url=settings.ollama_url, timeout=180.0
        )
    return _ollama_client


def _normalize_list(parsed: object) -> dict:
    """Wrap a bare list in the expected extraction result structure."""
    if isinstance(parsed, list):
        return {"entities": parsed, "relationships": []}
    return parsed  # type: ignore[return-value]


# Regex to quote bare JS object keys: {name: "val"} → {"name": "val"}
# Matches word chars at key positions (after { , [ or start-of-line) followed by :
_JS_KEY_RE = re.compile(
    r'([\[{,\n]|^)\s*([a-zA-Z_]\w*)\s*:', re.MULTILINE,
)


def _quote_js_keys(text: str) -> str:
    """Convert JS-like object notation to valid JSON by quoting bare keys.

    Handles LLM output like:
        entities: [{name: "Docker", entity_type: "technology"}]
    Converting to:
        {"entities": [{"name": "Docker", "entity_type": "technology"}]}
    """
    s = text.strip()
    # Wrap in {} if it starts with a bare key (top-level assignments like `entities: [...]`)
    if s and not s.startswith('{') and not s.startswith('['):
        s = '{' + s + '}'
    s = _JS_KEY_RE.sub(r'\1"\2":', s)
    return s


def _try_parse(text: str) -> dict | None:
    """Try JSON parse, then JS-key-quoting fallback. Returns None on failure."""
    try:
        return _normalize_list(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        return _normalize_list(json.loads(_quote_js_keys(text)))
    except (json.JSONDecodeError, ValueError):
        pass
    return None


# Regex for decision-style natural language answers.
# Matches paragraph-style Qwen3.5 outputs like:
#   "**Yes, these entities refer to the same real-world thing.**"
#   "No, these entities should NOT be merged."
#   "Decision: Do Not Merge"
#   "The answer is UPDATE."
_NL_DECISION_RE = re.compile(
    r'(?:\*\*)?\b'
    r'(yes(?:,?\s+(?:these|they|the))?|'
    r'no(?:,?\s+(?:these|they|the|do\s*not|should\s*not))?|'
    r'(?:decision|answer|verdict)\s*[:—-]\s*(?:do\s*not\s*merge|merge|no|yes|'
    r'add|update|supersede|noop))\b',
    re.IGNORECASE,
)

_NL_ACTION_RE = re.compile(
    r'\*\*\s*(ADD|UPDATE|SUPERSEDE|NOOP|MERGE|DO\s*NOT\s*MERGE)\s*\*\*|'
    r'\b(ADD|UPDATE|SUPERSEDE|NOOP)\b',
)


def _salvage_nl_decision(content: str, schema: dict) -> dict | None:
    """Salvage a decision-style response from natural-language LLM output.

    Qwen3.5-35B-A3B frequently ignores JSON instructions for decision prompts
    and emits paragraph-style answers instead. This function maps those answers
    back to the expected JSON structure when the schema has a known shape.

    Recognized shapes (by required fields):
      - should_merge (bool): entity_merging — yes/no decisions
      - action (enum):       dedup — ADD/UPDATE/SUPERSEDE/NOOP
      - is_valid (bool):     extraction_quality
      - relationship (str):  cooccurrence_linking — relationship type
    """
    props = (schema or {}).get("properties", {}) if isinstance(schema, dict) else {}
    required = set((schema or {}).get("required", []) if isinstance(schema, dict) else [])

    lower = content.lower()

    # Shape 1: should_merge boolean (entity_merging)
    if "should_merge" in props or "should_merge" in required:
        m = _NL_DECISION_RE.search(content)
        if m:
            word = m.group(1).lower()
            if word.startswith("no") or "do not merge" in word or "do_not_merge" in word:
                return {"should_merge": False, "reason": content[:200]}
            if word.startswith("yes") or word.endswith("merge"):
                return {"should_merge": True, "reason": content[:200]}
        # Fallback: look for bare "yes"/"no" at start of line
        stripped = content.lstrip("* \n\t").lower()
        if stripped.startswith("yes"):
            return {"should_merge": True, "reason": content[:200]}
        if stripped.startswith("no"):
            return {"should_merge": False, "reason": content[:200]}
        # Last-chance: scan anywhere for "should not be merged" / "do not merge" /
        # "not be merged" (all NO) vs "should be merged" / "same" + affirmative
        if re.search(r'(?i)(should\s+not|do\s+not|not\s+be|should\s*n\'?t)\s+(?:be\s+)?merge', content):
            return {"should_merge": False, "reason": content[:200]}
        if re.search(r'(?i)(should\s+be\s+merged|are\s+the\s+same|refer\s+to\s+the\s+same)', content):
            return {"should_merge": True, "reason": content[:200]}

    # Shape 2: action enum (dedup write-path)
    if "action" in props or "action" in required:
        m = _NL_ACTION_RE.search(content.upper())
        if m:
            action = (m.group(1) or m.group(2) or "").replace(" ", "").upper()
            if action == "DONOTMERGE":
                action = "NOOP"
            if action in ("ADD", "UPDATE", "SUPERSEDE", "NOOP"):
                return {"action": action, "reason": content[:200]}

    # Shape 3: is_valid boolean (extraction_quality)
    if "is_valid" in props or "is_valid" in required:
        if "yes" in lower[:50] or "valid" in lower[:50] and "not" not in lower[:50]:
            return {"is_valid": True, "confidence": 0.7, "reason": content[:200]}
        if "no" in lower[:50] or "not valid" in lower[:50] or "invalid" in lower[:50]:
            return {"is_valid": False, "confidence": 0.7, "reason": content[:200]}

    return None


def _extract_json(content: str, schema: dict | None = None) -> dict:
    """Extract JSON from LLM response that may contain markdown, preamble,
    JS notation, or paragraph-style natural language."""
    cleaned = content.strip()

    # 1. Direct parse (valid JSON or JS-key-quoted)
    result = _try_parse(cleaned)
    if result is not None:
        return result

    # 2. Strip markdown code blocks
    md_match = re.search(r'```(?:json)?\s*\n(.+?)\n\s*```', cleaned, re.DOTALL)
    if md_match:
        result = _try_parse(md_match.group(1).strip())
        if result is not None:
            return result

    # 3. Find first { to last } (skip preamble text)
    first_brace = cleaned.find('{')
    last_brace = cleaned.rfind('}')
    if first_brace != -1 and last_brace > first_brace:
        result = _try_parse(cleaned[first_brace:last_brace + 1])
        if result is not None:
            return result

    # 4. Find first [ to last ] — bare list
    first_bracket = cleaned.find('[')
    last_bracket = cleaned.rfind(']')
    if first_bracket != -1 and last_bracket > first_bracket:
        result = _try_parse(cleaned[first_bracket:last_bracket + 1])
        if result is not None:
            return result

    # 5. Natural-language salvage for decision-style prompts where the model
    # emits paragraph answers like "**Yes, these entities are the same.**".
    # With response_format: json_schema now on the request path this branch
    # should almost never fire — if it does, something is wrong (schema
    # constraint dropped, old cached response, etc.) so we log at warning.
    if schema is not None:
        nl_result = _salvage_nl_decision(cleaned, schema)
        if nl_result is not None:
            logger.warning(
                "NL salvage fired — grammar constraint bypassed somehow. Keys=%s, "
                "first 120 chars of response: %.120s",
                list(nl_result.keys()), cleaned,
            )
            return nl_result

    # Nothing worked
    logger.warning("Failed to parse LLM response as JSON: %.300s", cleaned)
    return {"entities": [], "relationships": []}


def _wrap_schema_strict(schema: dict) -> dict:
    """Return a copy of the schema with additionalProperties:false injected.

    llama-server's response_format json_schema "strict" mode requires
    additionalProperties:false on every object. We inject it recursively so
    callers don't have to remember.
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    if out.get("type") == "object":
        out.setdefault("additionalProperties", False)
        props = out.get("properties")
        if isinstance(props, dict):
            out["properties"] = {k: _wrap_schema_strict(v) for k, v in props.items()}
    elif out.get("type") == "array":
        items = out.get("items")
        if isinstance(items, dict):
            out["items"] = _wrap_schema_strict(items)
    return out


async def ollama_chat(
    system: str,
    user: str,
    schema: dict,
    *,
    model: str | None = None,
    temperature: float = 0.7,
    num_ctx: int = 8192,
    timeout: float = DEFAULT_LLM_TIMEOUT,
    max_retries: int = DEFAULT_LLM_MAX_RETRIES,
    keep_alive: str = "24h",
    think: bool = False,
) -> dict:
    """Send a schema-constrained request to llama-server (OpenAI-compatible API).

    Uses /v1/chat/completions with ``response_format: json_schema`` for
    grammar-constrained decoding. Confirmed working with
    Qwen3.5-35B-A3B-UD-Q4_K_XL on llama.cpp build b8580 (2026-04-09):
    the ``json_schema`` variant takes a different code path than the broken
    ``json_object`` variant and honours the schema cleanly, including enums.

    Falls back to robust JSON extraction + natural-language salvage on the
    (rare) occasions the server drops the schema constraint.

    Sampling defaults match Qwen3 team's official non-thinking-mode params
    (temp=0.7, top_p=0.8, top_k=20, min_p=0) per the Qwen3 model card.

    Args:
        system: System prompt.
        user: User message.
        schema: JSON schema — passed as ``response_format.json_schema.schema``
            AND summarised in the prompt for double-reinforcement.
        model: Ignored (llama-server serves a single model).
        temperature: LLM temperature (default matches Qwen3 official params).
        num_ctx: Context window size (managed by llama-server config).
        timeout: HTTP timeout in seconds.
        keep_alive: Ignored (llama-server keeps model loaded permanently).
        think: Enable model thinking/reasoning (disable for structured tasks).

    Returns:
        Parsed JSON dict from the LLM response.
    """
    client = _get_llm_client()

    payload: dict = {
        "model": "qwen3-35b",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 4096,
        "temperature": temperature,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "stream": False,
    }

    # Grammar-constrained decoding via OpenAI-compatible json_schema.
    # Works on current llama.cpp build; injects additionalProperties:false
    # recursively so "strict" mode is safe for all in-repo schemas.
    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_output",
                "strict": True,
                "schema": _wrap_schema_strict(schema),
            },
        }

    # Disable thinking for structured output (saves tokens + time)
    if not think:
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    total_attempts = max_retries + 1
    last_exc: Exception | None = None
    prev_sleep = _JITTER_BASE

    for attempt in range(total_attempts):
        try:
            resp = await client.post(
                "/v1/chat/completions", json=payload, timeout=timeout,
            )
            if resp.status_code in RETRYABLE_STATUSES:
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}",
                    request=resp.request, response=resp,
                )
                if attempt < max_retries:
                    prev_sleep = _next_jitter_sleep(prev_sleep)
                    logger.warning(
                        "llama-server HTTP %d (attempt %d/%d), "
                        "sleeping %.1fs with decorrelated jitter before retry",
                        resp.status_code, attempt + 1, total_attempts, prev_sleep,
                    )
                    await asyncio.sleep(prev_sleep)
                    continue
                raise last_exc

            # Non-retryable HTTP error (e.g. 404, 400) — surface immediately.
            resp.raise_for_status()

            raw_text = resp.text
            if not raw_text or not raw_text.strip():
                last_exc = ValueError("Empty response body")
                if attempt < max_retries:
                    prev_sleep = _next_jitter_sleep(prev_sleep)
                    logger.warning(
                        "llama-server empty body (attempt %d/%d), sleeping %.1fs",
                        attempt + 1, total_attempts, prev_sleep,
                    )
                    await asyncio.sleep(prev_sleep)
                    continue
                raise last_exc

            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )

            if not content or not content.strip():
                last_exc = ValueError("Empty LLM content")
                if attempt < max_retries:
                    prev_sleep = _next_jitter_sleep(prev_sleep)
                    logger.warning(
                        "llama-server empty content (attempt %d/%d), "
                        "sleeping %.1fs",
                        attempt + 1, total_attempts, prev_sleep,
                    )
                    await asyncio.sleep(prev_sleep)
                    continue
                raise last_exc

            return _extract_json(content, schema)

        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            # IMPORTANT: do NOT retry on client-side timeouts.
            # llama-server is single-slot (n_parallel=1 is required for
            # Qwen3.5-35B-A3B hybrid SWA — see memory 019d3f0e). Requests
            # queue inside the server; a ReadTimeout here means the client's
            # timeout is shorter than the server's processing time. Retrying
            # sends a NEW request to the queue behind the still-running first
            # one, doubling load instead of freeing the server.
            logger.warning(
                "llama-server %s after %.0fs — NOT retrying "
                "(server is likely still processing; caller should "
                "lengthen timeout or accept the failure)",
                type(exc).__name__, timeout,
            )
            raise

        except RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt < max_retries:
                prev_sleep = _next_jitter_sleep(prev_sleep)
                logger.warning(
                    "llama-server %s (attempt %d/%d), sleeping %.1fs",
                    type(exc).__name__, attempt + 1, total_attempts, prev_sleep,
                )
                await asyncio.sleep(prev_sleep)
                continue
            raise

        except json.JSONDecodeError as exc:
            last_exc = exc
            if attempt < max_retries:
                prev_sleep = _next_jitter_sleep(prev_sleep)
                logger.warning(
                    "llama-server malformed JSON (attempt %d/%d), "
                    "sleeping %.1fs: %.80s",
                    attempt + 1, total_attempts, prev_sleep, str(exc),
                )
                await asyncio.sleep(prev_sleep)
                continue
            raise

    # Unreachable — each branch either returns or raises — but keep a guard
    # for type-checker completeness.
    raise last_exc or RuntimeError("ollama_chat exhausted retries with no error captured")


async def ollama_generate(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    temperature: float = 0.3,
    num_ctx: int = 2048,
    timeout: float = DEFAULT_LLM_TIMEOUT,
    max_retries: int = DEFAULT_LLM_MAX_RETRIES,
    keep_alive: str = "24h",
    max_tokens: int = 512,
) -> str:
    """Generate plain text using llama-server.

    Used for HyDE hypothetical document generation and query decomposition.

    Same retry + timeout policy as :func:`ollama_chat` (see module docstring).
    ``ReadTimeout`` / ``WriteTimeout`` propagate to the caller; HyDE and
    query-decomposition callers are opt-in enhancements that gracefully
    degrade to un-enhanced search, so a raised timeout is fine.
    """
    client = _get_llm_client()
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": "qwen3-35b",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    total_attempts = max_retries + 1
    last_exc: Exception | None = None
    prev_sleep = _JITTER_BASE

    for attempt in range(total_attempts):
        try:
            resp = await client.post(
                "/v1/chat/completions", json=payload, timeout=timeout,
            )
            if resp.status_code in RETRYABLE_STATUSES:
                last_exc = httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}",
                    request=resp.request, response=resp,
                )
                if attempt < max_retries:
                    prev_sleep = _next_jitter_sleep(prev_sleep)
                    logger.warning(
                        "ollama_generate HTTP %d (attempt %d/%d), "
                        "sleeping %.1fs with decorrelated jitter before retry",
                        resp.status_code, attempt + 1, total_attempts, prev_sleep,
                    )
                    await asyncio.sleep(prev_sleep)
                    continue
                raise last_exc
            resp.raise_for_status()
            content = (
                resp.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            return content.strip() if content else ""

        except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            # Do NOT retry — see ollama_chat above for rationale.
            logger.warning(
                "ollama_generate %s after %.0fs — NOT retrying",
                type(exc).__name__, timeout,
            )
            raise

        except RETRYABLE_EXCEPTIONS as exc:
            last_exc = exc
            if attempt < max_retries:
                prev_sleep = _next_jitter_sleep(prev_sleep)
                logger.warning(
                    "ollama_generate %s (attempt %d/%d), sleeping %.1fs",
                    type(exc).__name__, attempt + 1, total_attempts, prev_sleep,
                )
                await asyncio.sleep(prev_sleep)
                continue
            raise

    raise last_exc or RuntimeError("ollama_generate exhausted retries with no error captured")
