#!/usr/bin/env python3
# demo.py
"""
End-to-end demo of the LLM Batch Processor.

Runs the full lifecycle against a running API:
    1. POST /batch/classify        -> get a job_id
    2. (replay with same key)      -> proves idempotency returns the SAME job_id
    3. GET /jobs/{id} (poll)       -> watch queued -> in_progress -> complete
    4. GET /metrics                -> show real aggregate token/success numbers

Prereqs (all must be live):
    - Redis running on localhost:6379
    - API:    uvicorn main:app --port 5000
    - Worker: arq worker.WorkerSettings
    - GROQ_API_KEY set in the worker's environment

Usage:
    python demo.py
    python demo.py --base-url http://localhost:5000
"""

import argparse
import sys
import time

import httpx

SAMPLE_ITEMS = [
    "CONGRATULATIONS! You've won a $1000 gift card. Click here to claim now!!!",
    "Hey, are we still on for lunch tomorrow at noon?",
    "URGENT: Your account will be suspended. Verify your password immediately.",
    "Thanks for the report — I'll review it and send feedback by Friday.",
    "Limited time offer! Buy now and get 90% off luxury watches.",
    "Can you forward me the slides from this morning's standup?",
]
LABELS = ["spam", "ham"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:5000")
    parser.add_argument("--idempotency-key", default="demo-run-001")
    parser.add_argument("--timeout", type=int, default=120, help="max seconds to wait for completion")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    client = httpx.Client(timeout=30)

    print(f"\n  LLM Batch Processor — end-to-end demo")
    print(f"  Target: {base}\n")

    # 1. Submit
    print(f"  [1] Submitting batch of {len(SAMPLE_ITEMS)} items, labels={LABELS} ...")
    try:
        resp = client.post(
            f"{base}/batch/classify",
            json={"items": SAMPLE_ITEMS, "labels": LABELS, "username": "demo"},
            headers={"Idempotency-Key": args.idempotency_key},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"\n  ERROR: could not reach the API ({exc}).")
        print("  Is uvicorn running on the target URL?\n")
        return 1

    job_id = resp.json()["job_id"]
    print(f"      -> job_id: {job_id}")

    # 2. Idempotency replay
    print(f"  [2] Re-submitting with the SAME Idempotency-Key (proving safe retries) ...")
    replay = client.post(
        f"{base}/batch/classify",
        json={"items": SAMPLE_ITEMS, "labels": LABELS, "username": "demo"},
        headers={"Idempotency-Key": args.idempotency_key},
    )
    replay_id = replay.json()["job_id"]
    same = "SAME job_id (no duplicate work)" if replay_id == job_id else "DIFFERENT — idempotency NOT working!"
    print(f"      -> {replay_id}  [{same}]")

    # 3. Poll
    print(f"  [3] Polling /jobs/{job_id[:8]}... until complete (timeout {args.timeout}s) ...")
    deadline = time.time() + args.timeout
    last_status = None
    while time.time() < deadline:
        s = client.get(f"{base}/jobs/{job_id}")
        if s.status_code == 404:
            print("      -> job not found yet, waiting...")
            time.sleep(1)
            continue
        status = s.json().get("status")
        if status != last_status:
            print(f"      -> status: {status}")
            last_status = status
        if status == "complete":
            result = s.json().get("result", {})
            _print_results(result)
            break
        time.sleep(1.5)
    else:
        print("      -> TIMED OUT. Is the arq worker running?")
        return 1

    # 4. Metrics
    print(f"  [4] Fetching /metrics (real aggregate numbers) ...")
    m = client.get(f"{base}/metrics").json()
    print(f"      batch_jobs_completed : {m.get('batch_jobs_completed')}")
    print(f"      items_processed      : {m.get('items_processed')}")
    print(f"      items_succeeded      : {m.get('items_succeeded')}")
    print(f"      items_failed         : {m.get('items_failed')}")
    print(f"      total_tokens         : {m.get('total_tokens')}")
    print(f"      success_rate         : {m.get('success_rate')}")
    print("\n  Demo complete.\n")
    return 0


def _print_results(result: dict) -> None:
    print(f"\n      Batch summary: {result.get('succeeded')}/{result.get('total_items')} succeeded, "
          f"{result.get('failed')} failed, {result.get('total_tokens')} tokens")
    for r in result.get("results", []):
        idx = r["index"]
        if r["status"] == "succeeded":
            print(f"        [{idx}] {r['label']:6}  <- {SAMPLE_ITEMS[idx][:50]!r}")
        else:
            print(f"        [{idx}] FAILED  ({r['error']})")
    print()


if __name__ == "__main__":
    sys.exit(main())
