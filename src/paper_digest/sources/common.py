from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


USER_AGENT = "paper-digest-pipeline/0.1 (research-paper discovery; contact: local-user)"


def get_bytes(url: str, *, timeout: int = 60, accept: str = "*/*") -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc


def get_json(url: str, *, timeout: int = 60) -> dict[str, Any]:
    return json.loads(get_bytes(url, timeout=timeout, accept="application/json").decode("utf-8"))

