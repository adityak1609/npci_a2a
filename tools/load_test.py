"""Small async load harness for pre-production smoke tests.

Set LOAD_TEST_BASE_URL, LOAD_TEST_TOKEN, LOAD_TEST_REQUESTS, and
LOAD_TEST_CONCURRENCY to exercise a deployed environment.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx


BASE_URL = os.getenv("LOAD_TEST_BASE_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("LOAD_TEST_TOKEN", "")
REQUESTS = int(os.getenv("LOAD_TEST_REQUESTS", "100"))
CONCURRENCY = int(os.getenv("LOAD_TEST_CONCURRENCY", "10"))


async def _one(client: httpx.AsyncClient, index: int) -> float:
    started = time.perf_counter()
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    response = await client.get(f"{BASE_URL}/health", headers=headers)
    response.raise_for_status()
    return time.perf_counter() - started


async def main() -> None:
    limits = httpx.Limits(max_connections=CONCURRENCY)
    timeout = httpx.Timeout(30.0)
    latencies: list[float] = []
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        async def run_one(index: int) -> None:
            async with semaphore:
                latencies.append(await _one(client, index))

        await asyncio.gather(*(run_one(i) for i in range(REQUESTS)))

    latencies.sort()
    p95_index = max(0, int(len(latencies) * 0.95) - 1)
    print(
        f"requests={REQUESTS} concurrency={CONCURRENCY} "
        f"avg={sum(latencies) / len(latencies):.3f}s "
        f"p95={latencies[p95_index]:.3f}s max={latencies[-1]:.3f}s"
    )


if __name__ == "__main__":
    asyncio.run(main())
