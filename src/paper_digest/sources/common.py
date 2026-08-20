from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Any


USER_AGENT = (
    "paper-digest-pipeline/0.1 "
    "(+https://github.com/StevenHuang233/paper-digest-pipeline)"
)


def _retry_after_seconds(headers: Any) -> float | None:
    """Parse Retry-After as either seconds or an HTTP date."""
    if headers is None:
        return None
    raw = str(headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds())


def _bounded_delay(value: float, maximum: float) -> float:
    return min(max(0.0, value), max(0.0, maximum))


def _log_retry(reason: str, delay: float, attempt: int, attempts: int) -> None:
    print(
        f"Transient {reason}; retrying in {delay:.1f}s "
        f"(attempt {attempt + 1}/{attempts})",
        file=sys.stderr,
        flush=True,
    )


def get_bytes(
    url: str, *, timeout: int = 60, accept: str = "*/*",
    attempts: int = 3, backoff_seconds: float = 2.0,
    rate_limit_backoff_seconds: float = 60.0,
    max_backoff_seconds: float = 300.0,
) -> bytes:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if backoff_seconds < 0 or rate_limit_backoff_seconds < 0 or max_backoff_seconds < 0:
        raise ValueError("retry delays must be non-negative")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    retryable_statuses = {408, 429, 500, 502, 503, 504}
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in retryable_statuses or attempt >= attempts:
                detail = exc.read(1000).decode("utf-8", errors="replace")
                raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc
            ordinary_delay = backoff_seconds * (2 ** (attempt - 1))
            retry_after = _retry_after_seconds(exc.headers)
            if exc.code == 429:
                rate_limit_delay = rate_limit_backoff_seconds * (2 ** (attempt - 1))
                delay = max(ordinary_delay, rate_limit_delay, retry_after or 0.0)
            else:
                delay = max(ordinary_delay, retry_after or 0.0)
            delay = _bounded_delay(delay, max_backoff_seconds)
            _log_retry(f"HTTP {exc.code}", delay, attempt, attempts)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt >= attempts:
                raise RuntimeError(
                    f"Network request failed after {attempts} attempts for {url}: {type(exc).__name__}: {exc}"
                ) from exc
            delay = _bounded_delay(backoff_seconds * (2 ** (attempt - 1)), max_backoff_seconds)
            _log_retry(type(exc).__name__, delay, attempt, attempts)
            time.sleep(delay)


def get_json(url: str, *, timeout: int = 60) -> dict[str, Any]:
    return json.loads(get_bytes(url, timeout=timeout, accept="application/json").decode("utf-8"))
