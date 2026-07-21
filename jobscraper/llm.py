"""Multi-provider LLM abstraction with rate-limit prevention."""

import json
import re
import time

from jobscraper.config import get_env

MAX_RETRIES = 5
RETRY_DELAY = 10  # seconds

# Circuit breaker: after this many LLM calls fail (each already exhausting its
# own retries), stop hammering the API and give up on the CURRENT company so the
# run can move on to the next one instead of grinding through backoff forever.
# Reset per-company via reset_failure_state().
MAX_CONSECUTIVE_FAILURES = int(get_env("MAX_CONSECUTIVE_LLM_FAILURES", "2"))

_client = None
_last_call_time = 0.0
_consecutive_failures = 0


class LLMUnavailableError(Exception):
    """The LLM is repeatedly unavailable/rate-limited.

    Raised once the per-company circuit breaker trips. It is NOT swallowed by
    the extract/enrich/plan helpers — it propagates up so the current company
    is abandoned (and reported as an error) and the run continues elsewhere.
    """


def reset_failure_state() -> None:
    """Reset the circuit breaker. Call at the start of each company so one
    unreachable company doesn't poison the next."""
    global _consecutive_failures
    _consecutive_failures = 0


def _get_provider() -> str:
    return get_env("LLM_PROVIDER", "groq").lower()


def _get_model() -> str:
    # gemini-flash-latest: free tier, 1M-token context, 250K tokens/minute — the
    #   auto-updating alias for the current Gemini Flash (won't deprecate).
    # gpt-oss-120b: free on Groq, 131K context, but only ~8K tokens/minute.
    # NOTE: gemini-2.5-flash was retired (404 on new keys) — do not default to it.
    defaults = {
        "groq": "openai/gpt-oss-120b",
        "gemini": "gemini-flash-latest",
    }
    return get_env("LLM_MODEL", defaults.get(_get_provider(), "gemini-flash-latest"))


def _get_client():
    global _client
    if _client is not None:
        return _client

    provider = _get_provider()

    if provider == "groq":
        from groq import Groq
        api_key = get_env("GROQ_API_KEY") or get_env("LLM_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY not set in .env file")
        _client = Groq(api_key=api_key)

    elif provider == "gemini":
        from google import genai
        api_key = get_env("GEMINI_API_KEY") or get_env("LLM_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not set in .env file")
        _client = genai.Client(api_key=api_key)

    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}. Use 'groq' or 'gemini'.")

    return _client


def _call_provider(client, prompt: str) -> str:
    """Dispatch to the appropriate provider SDK."""
    provider = _get_provider()

    if provider == "groq":
        response = client.chat.completions.create(
            model=_get_model(),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        return response.choices[0].message.content

    elif provider == "gemini":
        response = client.models.generate_content(
            model=_get_model(),
            contents=prompt,
            config={"temperature": 0.1},  # low temp for consistent JSON output
        )
        return response.text

    raise ValueError(f"Unknown provider: {provider}")


def call_llm(prompt: str) -> str:
    """Call the configured LLM with retry, proactive rate-limit delay, and a
    per-company circuit breaker.

    Raises LLMUnavailableError once too many calls have failed for the current
    company, so the caller can abandon it quickly instead of getting stuck.
    """
    global _last_call_time, _consecutive_failures

    # Circuit breaker already tripped for this company — fail fast, no waiting.
    if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        raise LLMUnavailableError(
            f"LLM unavailable after {_consecutive_failures} failed calls; skipping."
        )

    # Proactive delay to avoid rate limits. The binding free-tier constraint:
    #   gemini-flash-latest = 10 RPM  -> ~6s between calls
    #   groq gpt-oss-120b   = 30 RPM but 8K TPM -> pace on tokens instead
    default_delay = {"gemini": "6.5", "groq": "5"}.get(_get_provider(), "2")
    delay = float(get_env("LLM_CALL_DELAY", default_delay))
    elapsed = time.time() - _last_call_time
    if elapsed < delay:
        time.sleep(delay - elapsed)

    client = _get_client()

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            result = _call_provider(client, prompt)
            _last_call_time = time.time()
            _consecutive_failures = 0  # a success clears the breaker
            return result
        except Exception as e:
            last_exc = e
            # Record the attempt time so the proactive delay still applies to
            # the request that follows a failure.
            _last_call_time = time.time()
            err_str = str(e).lower()

            # A 413 "request too large" is permanent for this prompt size —
            # waiting never helps, so fail it fast (don't burn retries on it).
            is_too_large = "413" in err_str or "request too large" in err_str

            # Rate-limit / server-overload signals — back off and retry.
            is_rate_limited = not is_too_large and any(code in err_str for code in [
                "429", "resource_exhausted", "503", "500", "502", "504",
                "unavailable", "rate_limit", "rate limit",
                "overloaded", "try again",
            ])
            # Transient network faults — worth a retry rather than failing the
            # whole company on a single dropped connection.
            is_transient = any(code in err_str for code in [
                "timeout", "timed out", "connection", "temporarily",
                "reset by peer", "eof occurred",
            ])

            if (is_rate_limited or is_transient) and attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY * (attempt + 1)
                kind = "Rate limited/unavailable" if is_rate_limited else "Transient error"
                print(f"    {kind}, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})...")
                time.sleep(wait)
                continue
            break  # retries exhausted (or a non-retryable error) — handle below

    # This call failed for good. Count it toward the circuit breaker.
    _consecutive_failures += 1
    if _consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        # Too many failures in a row — trip the breaker so the current company
        # is abandoned rather than retried endlessly.
        raise LLMUnavailableError(
            f"LLM failed {_consecutive_failures} times in a row for this company "
            f"(last error: {last_exc})"
        ) from last_exc

    # Below the threshold: surface the original error. Callers that swallow it
    # (extractor/enricher) degrade gracefully for this one call and continue.
    if last_exc is not None:
        raise last_exc
    return ""


def parse_json_from_response(text: str) -> list | dict:
    """Extract JSON from LLM response, handling markdown code blocks and extra text."""
    # Try code blocks first
    match = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', text)
    if match:
        text = match.group(1)
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the first JSON object or array in the text
    for start_char, end_char in [('[', ']'), ('{', '}')]:
        start = text.find(start_char)
        if start == -1:
            continue
        # Find matching closing bracket by counting depth
        depth = 0
        for i in range(start, len(text)):
            if text[i] == start_char:
                depth += 1
            elif text[i] == end_char:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break

    # Last resort: try to parse first line
    return json.loads(text.split('\n')[0])
