"""
FR-MQ-01, FR-MQ-03  Publish settlement messages to Redis/RQ.

A message carries the transaction's idempotency_key (the worker claims on it) and
the transaction id in job meta, so every job traces back to exactly one row.
The job is referenced by import path, so the web app never imports the worker.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from redis import Redis
from rq import Queue

from app.config import settings

QUEUE_NAME = "settlement"
SETTLE_JOB = "app.workers.settlement_worker.settle"
JOB_TIMEOUT = 300  # seconds; a Testnet payment validates in ~4-10 s

_queue: Optional[Queue] = None


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue(QUEUE_NAME, connection=Redis.from_url(settings.redis_url))
    return _queue


def _job_kwargs(transaction_id: str) -> dict:
    return {
        "job_timeout": JOB_TIMEOUT,
        "description": f"settle transaction {transaction_id}",
        "meta": {"transaction_id": transaction_id},
    }


def enqueue_settlement(idempotency_key: str, transaction_id: str) -> str:
    """Publish one settlement message now. Returns the RQ job id."""
    job = get_queue().enqueue(SETTLE_JOB, idempotency_key, **_job_kwargs(transaction_id))
    return job.id


def enqueue_settlement_in(seconds: int, idempotency_key: str, transaction_id: str) -> str:
    """Publish a delayed retry. Needs a worker started with --with-scheduler."""
    job = get_queue().enqueue_in(
        timedelta(seconds=seconds), SETTLE_JOB, idempotency_key, **_job_kwargs(transaction_id)
    )
    return job.id
