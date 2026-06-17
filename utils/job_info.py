import asyncio
from typing import Optional

from arq.jobs import Job

from models import JobStatusResponse


async def process_job_info(job: Job, ctx: Optional[dict] = None) -> None | JobStatusResponse:
    start_time = None

    # Get job info and result_info
    job_info = await job.info()

    if job_info is None:
        return None

    # Get job status
    status = await job.status()

    # Set enqueue_time as start_time if available
    start_time = getattr(job_info, "enqueue_time", None)

    # Use ctx for attempts if available
    attempts = None
    if ctx and "job_try" in ctx:
        attempts = ctx["job_try"]
    else:
        attempts = getattr(job_info, "job_try", None)

    print(f"Processing job info for job_id {job.job_id}: {job_info=}, {status=}")

    # Prepare data for response model
    data = {
        "job_id": job.job_id,
        "status": status.value,
        "success": getattr(job_info, "success", False),  # Fix: default to False if not present
        "result": {},
        "start_time": None,
        "finish_time": None,
        "username": None,
        "function": getattr(job_info, "function", None),
        "args": str(getattr(job_info, "args", "")),
        "error": None,
        "attempts": attempts,
    }

    # Extract timestamps
    if status.value == "in_progress":
        # Use enqueue_time as start_time if available
        data["start_time"] = start_time.isoformat() if start_time else None

    else:
        start_time = getattr(job_info, "start_time", None)
        data["start_time"] = start_time.isoformat() if start_time else None

    finish_time = getattr(job_info, "finish_time", None)
    data["finish_time"] = finish_time.isoformat() if finish_time else None

    # if a start_time is present, and the status is not_found, set status to queued
    # this is to handle a rare case where the job has been queued but arq is not running
    if start_time and status.value == "not_found":
        data["status"] = "queued"

    # Extract username from kwargs if present
    username = None
    if job_info.kwargs and isinstance(job_info.kwargs, dict):
        username = job_info.kwargs.get("username")
    data["username"] = username

    # If job is complete, fetch result or error
    if status.value == "complete":
        try:
            result = await job.result(timeout=5)
            data["result"] = result if isinstance(result, dict) else {"value": result}
        except asyncio.TimeoutError:
            data["error"] = "TimeoutError: Job took longer than 5 seconds to fetch result"
        except Exception as e:
            data["error"] = str(e)

    return JobStatusResponse(**data)
