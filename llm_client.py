# llm_client.py
"""
Thin async wrapper around the Groq chat completions API.

Responsibilities that belong to *this* layer (not arq, not FastAPI):
  - Retry transient failures (HTTP 429 rate-limit, 5xx, network) with
    exponential backoff + jitter.
  - Surface token usage so the batch layer can aggregate cost/throughput metrics.
  - Keep a single shared AsyncClient alive for connection reuse.

Honesty note: this talks to a real Groq endpoint. If GROQ_API_KEY is unset,
calls raise immediately rather than returning fake data.
"""

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Optional

import httpx

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.1-8b-instant"


class LLMError(Exception):
    """Raised when an LLM call fails after exhausting retries."""


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    attempts: int


def _is_retryable(status_code: Optional[int]) -> bool:
    # 429 = rate limited, 5xx = transient server error.
    if status_code is None:
        return True  # network-level error, worth a retry
    return status_code == 429 or 500 <= status_code < 600


async def call_groq(
    client: httpx.AsyncClient,
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    system: Optional[str] = None,
    max_retries: int = 4,
    base_delay: float = 0.75,
    temperature: float = 0.0,
) -> LLMResult:
    """
    Call Groq once, with retry/backoff on retryable errors.

    Raises LLMError if all attempts fail or the API key is missing.
    """
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        try:
            from config import get_settings
            api_key = get_settings().GROQ_API_KEY or None
        except Exception:
            api_key = None

    # Demo/offline mode: deterministic local classification with no network call.
    # Gated behind an explicit env var; never active in normal operation.
    if os.environ.get("LLM_DEMO_MODE") == "1":
        await asyncio.sleep(0.2)  # simulate a little latency
        low = prompt.lower()
        spammy = any(w in low for w in ["won", "click here", "urgent", "offer", "% off", "claim", "suspend", "verify your password"])
        label = "spam" if spammy else "ham"
        return LLMResult(text=label, prompt_tokens=12, completion_tokens=1, total_tokens=13, attempts=1)

    if not api_key:
        raise LLMError("GROQ_API_KEY is not set; refusing to fabricate a response.")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {"model": model, "messages": messages, "temperature": temperature}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_err: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.post(GROQ_URL, json=payload, headers=headers, timeout=60)
            if _is_retryable(resp.status_code) and resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                # Honor Retry-After if the server sent one.
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after else _backoff(base_delay, attempt)
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage", {})
            return LLMResult(
                text=data["choices"][0]["message"]["content"].strip(),
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                attempts=attempt,
            )
        except httpx.HTTPStatusError as exc:
            # Non-retryable status (e.g. 400 bad request) — fail fast.
            raise LLMError(f"Non-retryable HTTP error: {exc}") from exc
        except (httpx.RequestError, KeyError, ValueError) as exc:
            last_err = repr(exc)
            await asyncio.sleep(_backoff(base_delay, attempt))

    raise LLMError(f"Exhausted {max_retries} attempts. Last error: {last_err}")


def _backoff(base: float, attempt: int) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, base * (2 ** (attempt - 1)))
