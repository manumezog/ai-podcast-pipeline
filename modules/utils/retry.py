"""
Retry utility with exponential backoff for external API calls.

All modules that hit external APIs (Anthropic, Google, NewsAPI, Reddit, ArXiv)
use this instead of rolling their own retry logic.

Usage:
    from modules.utils.retry import retry_with_backoff

    result = retry_with_backoff(
        lambda: some_api_call(arg1, arg2),
        max_attempts=3,
        base_delay=1.0,
    )
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Tuple, Type, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


def retry_with_backoff(
    fn: Callable[[], T],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    retriable_exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> T:
    """
    Call fn() up to max_attempts times with exponential backoff between retries.

    Args:
        fn: Zero-argument callable. Use a lambda or functools.partial to pass
            arguments: ``lambda: api_call(x, y)``.
        max_attempts: Total number of attempts (not retries). Default 3.
        base_delay: Initial wait in seconds before the first retry. Each
            subsequent retry doubles the delay: 1s, 2s, 4s, ... Default 1.0.
        retriable_exceptions: Exception types that trigger a retry. Defaults
            to (Exception,), which retries on anything. Pass a narrower tuple
            (e.g. ``(httpx.HTTPStatusError,)``) for more targeted retry logic.

    Returns:
        The return value of fn() on success.

    Raises:
        The last exception raised by fn() after all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except retriable_exceptions as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "Attempt %d/%d failed (%s: %s). Retrying in %.1fs.",
                attempt,
                max_attempts,
                type(exc).__name__,
                exc,
                delay,
            )
            time.sleep(delay)
    raise last_exc  # type: ignore[misc]
