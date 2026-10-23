"""Notifier process: delivers the durable outbox every second (brief §5 `notifier`).

Instant incident alerts are queued by the worker; this loop sends them within about a
second. The detector also delivers the outbox once a minute as a fallback; rows are
claimed with FOR UPDATE SKIP LOCKED, so both can run without double-sending.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

from app.db import close_db_pool, init_db_pool
from app.outbox import deliver_outbox_batch
from app.queue import close_redis, get_redis, init_redis

logger = logging.getLogger("changewatch.notifier")

POLL_INTERVAL_SEC = 1.0
HEARTBEAT_KEY = "heartbeat:notifier"


async def run_notifier() -> None:
    logger.info("ChangeWatch notifier started (PID: %d)", os.getpid())
    await init_db_pool()
    await init_redis()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        while not stop_event.is_set():
            try:
                delivered = await deliver_outbox_batch()
                if delivered:
                    logger.info("Delivered %d outbox notification(s)", delivered)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Outbox delivery cycle failed: %s", exc, exc_info=True)

            try:
                redis = get_redis()
                if redis:
                    await redis.set(HEARTBEAT_KEY, str(time.time()), ex=60)
            except Exception as exc:
                logger.debug("Failed writing notifier heartbeat: %s", exc)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("Notifier shutting down")
        await close_redis()
        await close_db_pool()


if __name__ == "__main__":
    from app.main import setup_structured_logging

    setup_structured_logging()
    asyncio.run(run_notifier())
