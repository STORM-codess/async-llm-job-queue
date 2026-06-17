# utils/idempotency.py
"""
Idempotency for batch submissions.

Why this exists: clients retry. A network blip after POST /batch/classify
shouldn't cause the same 500-item batch to run twice (double the cost,
double the LLM calls). The client sends an Idempotency-Key header; we map
it to the job_id of the first submission and return that on any replay.

Stored in Redis with a TTL so keys don't accumulate forever.
"""

from typing import Optional

from arq.connections import ArqRedis

_PREFIX = "idem:"
_TTL_SECONDS = 24 * 60 * 60  # 24h


async def get_existing_job(redis: ArqRedis, key: str) -> Optional[str]:
    val = await redis.get(f"{_PREFIX}{key}")
    if val is None:
        return None
    return val.decode() if isinstance(val, (bytes, bytearray)) else str(val)


async def remember_job(redis: ArqRedis, key: str, job_id: str) -> None:
    # NX so the first writer wins under concurrent duplicate submissions.
    await redis.set(f"{_PREFIX}{key}", job_id, ex=_TTL_SECONDS, nx=True)
