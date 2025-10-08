import logging
import random
from typing import Any, Dict, Optional

import tenacity
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut


logger = logging.getLogger(__name__)

_TRANSIENT_KEYWORDS = (
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "too many requests",
)


def _is_transient_telegram_error(exc: BaseException) -> bool:
    if isinstance(exc, (RetryAfter, TimedOut, NetworkError)):
        return True
    if isinstance(exc, TelegramError):
        message = str(exc).lower()
        return any(keyword in message for keyword in _TRANSIENT_KEYWORDS)
    return False


def _wait_strategy(retry_state: tenacity.RetryCallState) -> float:
    exception = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exception, RetryAfter):
        return max(float(getattr(exception, "retry_after", 1.0)), 0.5)
    base_delay = min(10.0, 2 ** (retry_state.attempt_number - 1))
    return base_delay + random.uniform(0, 0.5)


async def send_message_with_retry(
    bot: Any,
    chat_id: int,
    *,
    retry_attempts: int = 5,
    log_context: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> Any:
    log_context = log_context or {}

    async for attempt in tenacity.AsyncRetrying(
        retry=tenacity.retry_if_exception(_is_transient_telegram_error),
        wait=_wait_strategy,
        stop=tenacity.stop_after_attempt(retry_attempts),
        reraise=True,
        before_sleep=tenacity.before_sleep_log(logger, logging.WARNING),
    ):
        with attempt:
            try:
                return await bot.send_message(chat_id=chat_id, **kwargs)
            except RetryAfter as exc:
                logger.warning(
                    "Telegram rate-limited send; retrying after %.1fs | context=%s",
                    getattr(exc, "retry_after", 0),
                    log_context,
                )
                raise
            except (TimedOut, NetworkError) as exc:
                logger.warning(
                    "Transient Telegram network error (%s); retrying | context=%s",
                    exc.__class__.__name__,
                    log_context,
                )
                raise

