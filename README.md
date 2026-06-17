# Async-llm-job-queue

An async job-queue service for running **bulk LLM workloads** without blocking
HTTP requests. Submit a batch of texts, get a job ID back instantly, and let a
background worker process the items concurrently — with per-item retries,
rate-limit-aware concurrency control, partial-failure reporting, and idempotent
submission.

Built with **FastAPI + arq + Redis + Groq**, with job history persisted to SQL
(SQLite locally; drop in a Postgres URL for production).

![Demo](demo.gif)

---

## Why this exists

Synchronous LLM calls don't scale to bulk work. A request to classify 500
documents can't hold an HTTP connection open for minutes, and a single failed
item shouldn't sink the whole batch. This service decouples *submission* from
*processing* and treats reliability as a first-class concern.

## Architecture

```
Client --POST /batch/classify--> FastAPI --enqueue--> Redis (arq queue)
   |                                                       |
   |                                                  arq worker
   |                                          (bounded concurrency, per-item
   |                                           retry/backoff, token tracking)
   |                                                       |
   +--GET /batch/{id}<-- Redis (live) / SQL (history) <----+
```

- **API stays responsive** — submission returns immediately with a job ID.
- **Worker is a separate process** — reads from the same Redis, processes jobs.
- **Job history persists to SQL** via arq's `after_job_end` hook, so results
  survive worker restarts and Redis result expiry.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/batch/classify` | Submit a batch of texts + labels. Accepts an `Idempotency-Key` header. |
| `GET`  | `/jobs/{job_id}` | Poll job status + results. Checks Redis first, falls back to SQL. |
| `GET`  | `/metrics` | Aggregate, real metrics across completed batches (items, tokens, success rate). |

## Reliability features (the interesting part)

- **Bounded concurrency** — a semaphore caps in-flight LLM calls per batch, so
  no batch size can blow past Groq's rate limit.
- **Per-item retry with exponential backoff + jitter** — honors `Retry-After`
  on 429s; retries 5xx/network errors; fails fast on non-retryable 4xx.
- **Partial-failure reporting** — 48/50 succeed and 2 fail? The job completes
  and reports exactly which items failed and why. No all-or-nothing.
- **Idempotent submission** — an `Idempotency-Key` header makes client retries
  safe; a replay returns the original job ID instead of re-running the batch.
- **Honest metrics** — `/metrics` sums real persisted token usage and outcomes.
  No fabricated numbers; if `GROQ_API_KEY` is missing, jobs fail loudly.

### Verified under real load

Running a 50-item batch against live Groq rate limits — the worker retries
rate-limited items with backoff and isolates the ones that can't complete,
without blocking the rest of the batch:

![Metrics from a 50-item batch run](Screenshot 2026-06-17 170451)(Screenshot 2026-06-17 170502)

## Run locally

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add your GROQ_API_KEY

# terminal 1 — API
uvicorn main:app --port 5000

# terminal 2 — worker (needs a running Redis on localhost:6379)
python -m arq worker.WorkerSettings
```

Then run the automated end-to-end demo (submit → idempotent replay → poll →
metrics) in a third terminal:

```bash
python demo.py
```

> Tip: to try the full flow without a Groq key, start the API and worker with
> `LLM_DEMO_MODE=1` set — classification runs locally with deterministic rules
> (no network call, no fabricated API responses). Remove it for real Groq calls.

Example submission:

```bash
curl -X POST http://localhost:5000/batch/classify \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-001" \
  -d '{"items":["Win a free prize now!","Lunch at noon?"],"labels":["spam","ham"]}'
```

## Attribution

The base job-queue plumbing (arq + FastAPI enqueue/poll, SQL job-history
persistence via `after_job_end`) started from the open-source template
[`davidmuraya/fastapi-arq`](https://github.com/davidmuraya/fastapi-arq).

Built on top of it for this project: the entire **LLM batch-processing layer** —
`llm_client.py` (Groq client with retry/backoff), `llm_tasks.py` (concurrent
batch classification with per-item failure handling and token aggregation),
`utils/idempotency.py`, and the `/batch/classify` + `/metrics` endpoints. A bug
in the template's worker (import of a non-existent `always_fail` task) was also
fixed.
