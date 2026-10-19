"""Small retry helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    attempts: int = 2,
    delay: float = 0.5,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return await fn()
        except retry_on as e:  # noqa: PERF203
            last = e
            if i < attempts - 1:
                await asyncio.sleep(delay * (2**i))
    assert last is not None
    raise last
