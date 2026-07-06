"""Production ingestion + maintenance worker.

Run this as a separate process when A2A_INGESTION_MODE=worker:

    python worker.py

The API process only creates queued jobs. This worker atomically claims queued
rows from Postgres (FOR UPDATE SKIP LOCKED) and processes OCR/embedding/
summarization work away from live request handling.

It also owns the daily data-retention run (RETENTION_HOUR_UTC, default 02:00
UTC), guarded by a Postgres advisory lock so multiple worker replicas never
run it concurrently.
"""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import date, datetime, timezone

from dotenv import load_dotenv

from services.a2a_ingestor import (
    claim_next_queued_job,
    claim_next_received_change,
    process_ingestion_job,
    process_received_change,
)

load_dotenv()

_stop = asyncio.Event()

# Stable advisory-lock key for the retention singleton (shares the Postgres
# advisory-lock keyspace with main.py's startup/maintenance keys).
RETENTION_ADVISORY_LOCK_KEY = 0x4E50_4349_0000_0003

_last_retention_date: date | None = None


def _poll_interval() -> float:
    try:
        return float(os.getenv("A2A_WORKER_POLL_SECONDS", "2"))
    except (TypeError, ValueError):
        return 2.0


def _retention_hour_utc() -> int:
    try:
        return int(os.getenv("RETENTION_HOUR_UTC", "2"))
    except (TypeError, ValueError):
        return 2


def _run_retention_locked() -> dict | None:
    """Run retention under an advisory lock; None when another replica holds it."""
    from database import SessionLocal, pg_try_advisory_lock
    from services.retention import run_retention

    with pg_try_advisory_lock(RETENTION_ADVISORY_LOCK_KEY) as acquired:
        if not acquired:
            return None
        db = SessionLocal()
        try:
            return run_retention(db)
        finally:
            db.close()


async def _maybe_run_retention() -> None:
    """Run the daily retention purge once per UTC day, after RETENTION_HOUR_UTC."""
    global _last_retention_date
    now = datetime.now(timezone.utc)
    if now.hour < _retention_hour_utc():
        return
    if _last_retention_date == now.date():
        return
    try:
        result = await asyncio.to_thread(_run_retention_locked)
    except Exception as exc:
        # Leave _last_retention_date unset so the next loop iteration retries.
        print(f"[worker] Retention run failed: {exc!r}")
        return
    _last_retention_date = now.date()
    if result is not None:
        print(f"[worker] Daily retention complete: {result}")


def _install_signal_handlers() -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: _stop.set())


async def run_worker() -> None:
    _install_signal_handlers()
    print("[worker] A2A ingestion worker started.")
    while not _stop.is_set():
        await _maybe_run_retention()

        change_id = await asyncio.to_thread(claim_next_received_change)
        if change_id is not None:
            print(f"[worker] Claimed received change {change_id}.")
            await process_received_change(change_id)
            continue

        job_id = await asyncio.to_thread(claim_next_queued_job)
        if job_id is None:
            try:
                await asyncio.wait_for(_stop.wait(), timeout=_poll_interval())
            except asyncio.TimeoutError:
                pass
            continue

        print(f"[worker] Claimed ingestion job {job_id}.")
        await process_ingestion_job(job_id)

    print("[worker] A2A ingestion worker stopped.")


if __name__ == "__main__":
    asyncio.run(run_worker())
