"""Session middleware: transparent retry on Telegram flood-wait (429).

aiogram 3 raises TelegramRetryAfter and does NOT retry by itself; this
middleware sleeps `retry_after` and retries a bounded number of times so
admin reports and approvals survive burst load.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramRetryAfter

log = logging.getLogger(__name__)


class RetryAfterMiddleware(BaseRequestMiddleware):
    def __init__(self, max_retries: int = 3, max_wait_s: float = 60.0):
        self.max_retries = max_retries
        self.max_wait_s = max_wait_s

    async def __call__(self, make_request, bot, method):
        for attempt in range(self.max_retries + 1):
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as e:
                if attempt >= self.max_retries or e.retry_after > self.max_wait_s:
                    raise
                log.warning("flood-wait %ss on %s (attempt %d/%d)",
                            e.retry_after, type(method).__name__,
                            attempt + 1, self.max_retries)
                await asyncio.sleep(e.retry_after + 0.5)
        raise RuntimeError("unreachable")
