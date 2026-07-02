"""Production ingestion worker.

Run this as a separate process when A2A_INGESTION_MODE=worker:

    python worker.py

The API process only creates queued jobs. This worker atomically claims queued
rows from Postgres and processes OCR/embedding/summarization work away from
live request handling.
"""

from __future__ import annotations

import asyncio
import os
import signal

from dotenv import load_dotenv

from services.a2a_ingestor import (
    claim_next_queued_job,
    claim_next_received_change,
    process_ingestion_job,
    process_received_change,
)

load_dotenv()

_stop = asyncio.Event()


def _poll_interval() -> float:
    try:
        return float(os.getenv("A2A_WORKER_POLL_SECONDS", "2"))
    except (TypeError, ValueError):
        return 2.0


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
