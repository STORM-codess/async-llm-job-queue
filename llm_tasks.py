# llm_tasks.py
"""
The LLM batch-processing layer — the part of this project that is genuinely
mine, built on top of the arq + FastAPI job-queue plumbing.

Design decisions worth explaining in an interview:

  - Concurrency is bounded by a semaphore so we never exceed Groq's
    rate limit, regardless of batch size. One slow/huge batch can't
    starve the worker either, because max_jobs caps concurrent jobs too.

  - Failure is per-item, not all-or-nothing. If 48/50 items succeed and
    2 fail after retries, the job still completes and reports exactly
    which items failed and why. Bulk work that aborts on the first bad
    row is useless in production.

  - Token usage is aggregated across the batch so /metrics can report
    real throughput and cost — no fabricated numbers.
"""

import asyncio
from typing import Any, Dict, List, Optional

from llm_client import LLMError, call_groq

# Bound concurrent in-flight LLM calls *within a single batch*.
# Groq free tier rate limits are modest; 5 is a safe default and is the
# knob you'd tune per account.
DEFAULT_CONCURRENCY = 5

CLASSIFY_SYSTEM = (
    "You are a precise text classifier. You will be given a piece of text and a "
    "fixed list of allowed labels. Respond with EXACTLY ONE label from the list "
    "and nothing else — no punctuation, no explanation. If none fit, respond with "
    "the single word: other."
)


def _build_prompt(text: str, labels: List[str]) -> str:
    label_str = ", ".join(labels)
    return f"Allowed labels: {label_str}\n\nText:\n{text}\n\nLabel:"


async def _classify_one(
    sem: asyncio.Semaphore,
    client,
    idx: int,
    text: str,
    labels: List[str],
    model: str,
) -> Dict[str, Any]:
    """Classify a single item. Never raises — failures are captured per-item."""
    async with sem:
        try:
            result = await call_groq(
                client,
                _build_prompt(text, labels),
                model=model,
                system=CLASSIFY_SYSTEM,
            )
            raw = result.text.strip().lower()
            # Snap the model's answer to an allowed label when possible.
            label = next((l for l in labels if l.lower() == raw), None) or (
                "other" if raw == "other" else raw
            )
            return {
                "index": idx,
                "status": "succeeded",
                "label": label,
                "raw": result.text,
                "valid_label": label in labels or label == "other",
                "total_tokens": result.total_tokens,
                "attempts": result.attempts,
                "error": None,
            }
        except LLMError as exc:
            return {
                "index": idx,
                "status": "failed",
                "label": None,
                "raw": None,
                "valid_label": False,
                "total_tokens": 0,
                "attempts": None,
                "error": str(exc),
            }


async def batch_classify(
    ctx: dict,
    items: List[str],
    labels: List[str],
    model: str = "llama-3.1-8b-instant",
    concurrency: int = DEFAULT_CONCURRENCY,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """
    arq task: classify a batch of texts into one of `labels` each.

    Returns a structured summary with per-item results, a succeeded/failed
    breakdown, and aggregate token usage. This dict is what the worker's
    after_job_end hook persists to the DB and what GET /batch/{id} returns.
    """
    client = ctx["session"]  # shared httpx.AsyncClient from worker startup
    sem = asyncio.Semaphore(concurrency)

    tasks = [
        _classify_one(sem, client, idx, text, labels, model)
        for idx, text in enumerate(items)
    ]
    results = await asyncio.gather(*tasks)

    succeeded = [r for r in results if r["status"] == "succeeded"]
    failed = [r for r in results if r["status"] == "failed"]
    total_tokens = sum(r["total_tokens"] for r in results)

    return {
        "username": username,
        "model": model,
        "total_items": len(items),
        "succeeded": len(succeeded),
        "failed": len(failed),
        "total_tokens": total_tokens,
        "results": results,
    }
