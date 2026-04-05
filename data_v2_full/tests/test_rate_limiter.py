from __future__ import annotations

import asyncio
import time

from bsky_collector_v2.http_client import AsyncRateLimiter


def test_rate_limiter_respects_rps() -> None:
    async def run() -> float:
        limiter = AsyncRateLimiter(rps=20.0)  # 50ms interval
        start = time.monotonic()
        for _ in range(10):
            await limiter.wait()
        return time.monotonic() - start

    elapsed = asyncio.run(run())
    # First acquire is immediate; remaining 9 should enforce pacing.
    assert elapsed >= 0.35

