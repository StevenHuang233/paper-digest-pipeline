from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any


USER_AGENT = "paper-digest-pipeline/0.1 (research-paper discovery; contact: local-user)"


def get_bytes(
    url: str, *, timeout: int = 60, accept: str = "*/*",
    attempts: int = 3, backoff_seconds: float = 2.0,
) -> bytes:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
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
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt >= attempts:
                raise RuntimeError(
                    f"Network request failed after {attempts} attempts for {url}: {type(exc).__name__}: {exc}"
                ) from exc
        time.sleep(max(0.0, backoff_seconds) * (2 ** (attempt - 1)))


def get_json(url: str, *, timeout: int = 60) -> dict[str, Any]:
    return json.loads(get_bytes(url, timeout=timeout, accept="application/json").decode("utf-8"))
