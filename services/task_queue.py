"""Redis-backed async task queue using Arq.

Replaces the fragile filesystem watchdog with a robust, distributed queue.
"""

import os
from typing import Any
from arq import create_pool
from arq.connections import RedisSettings

from ingest import run_ingestion
from services.retention import run_retention
from database import SessionLocal

_redis_pool = None

async def get_redis_pool():
    """Return a cached Arq Redis pool."""
    global _redis_pool
    if _redis_pool is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6379")
        _redis_pool = await create_pool(RedisSettings.from_dsn(url))
    return _redis_pool

async def enqueue_ingestion(pdf_path: str) -> None:
    """Enqueue a PDF ingestion job."""
    pool = await get_redis_pool()
    await pool.enqueue_job("task_run_ingestion", pdf_path)

async def enqueue_received_change_processing(change_id: str) -> None:
    """Enqueue durable A2A change processing."""
    pool = await get_redis_pool()
    await pool.enqueue_job("task_process_received_change", change_id)

# --- Arq Task Definitions ---

async def task_run_ingestion(ctx: dict[str, Any], pdf_path: str) -> dict[str, Any]:
    """Background task to run PDF ingestion."""
    import asyncio
    return await asyncio.to_thread(run_ingestion, pdf_path=pdf_path)

async def task_run_retention(ctx: dict[str, Any]) -> dict[str, Any]:
    """Daily cron task: purge expired data per retention policy."""
    import asyncio
    db = SessionLocal()
    try:
        return await asyncio.to_thread(run_retention, db)
    finally:
        db.close()

async def task_process_received_change(ctx: dict[str, Any], change_id: str) -> dict[str, Any]:
    """Background task to process a durable A2A received change."""
    from services.a2a_ingestor import process_received_change
    return await process_received_change(change_id)

class WorkerSettings:
    """Settings for the Arq worker process."""
    functions = [task_run_ingestion, task_run_retention, task_process_received_change]
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://localhost:6379"))
    max_jobs = 4
    # Run retention cleanup once a day at 02:00 UTC
    cron_jobs = [
        {
            "coroutine": task_run_retention,
            "name": "daily_retention",
            "hour": 2,
            "minute": 0,
        }
    ]
