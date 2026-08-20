from __future__ import annotations

import datetime as dt
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import Paper
from .common import get_bytes


ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
_RESOLVED_WINDOW = "_resolved_window_utc"


def resolve_date(value: str, today: dt.date | None = None) -> dt.date:
    base = today or dt.datetime.now().astimezone().date()
    if value == "today":
        return base
    if value == "yesterday":
        return base - dt.timedelta(days=1)
    return dt.date.fromisoformat(value)


def _minute_clock(value: str, field: str) -> dt.time:
    try:
        clock = dt.time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must use HH:MM 24-hour format") from exc
    if clock.tzinfo is not None or clock.second or clock.microsecond:
        raise ValueError(f"{field} must use HH:MM 24-hour format")
    return clock


def resolve_relative_window(
    discovery: dict, *, now: dt.datetime | None = None,
) -> tuple[dt.datetime, dt.datetime]:
    if now is None:
        frozen = discovery.get(_RESOLVED_WINDOW)
        if isinstance(frozen, list) and len(frozen) == 2:
            start = dt.datetime.fromisoformat(str(frozen[0]))
            end = dt.datetime.fromisoformat(str(frozen[1]))
            if start.tzinfo is None or end.tzinfo is None or end <= start:
                raise ValueError("Stored arXiv discovery window is invalid")
            return start.astimezone(dt.timezone.utc), end.astimezone(dt.timezone.utc)

    window = discovery.get("window") or {}
    timezone_name = str(window.get("timezone") or "").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown discovery.window.timezone: {timezone_name}") from exc

    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_today = current.astimezone(timezone).date()
    start_days_ago = int(window["start_days_ago"])
    end_days_ago = int(window["end_days_ago"])
    start_clock = _minute_clock(str(window["start_time"]), "discovery.window.start_time")
    end_clock = _minute_clock(str(window["end_time"]), "discovery.window.end_time")
    start_local = dt.datetime.combine(local_today - dt.timedelta(days=start_days_ago), start_clock).replace(tzinfo=timezone)
    end_local = dt.datetime.combine(local_today - dt.timedelta(days=end_days_ago), end_clock).replace(tzinfo=timezone)
    if end_local <= start_local:
        raise ValueError("discovery.window end must be later than its start")
    return start_local.astimezone(dt.timezone.utc), end_local.astimezone(dt.timezone.utc)


def freeze_relative_window(
    discovery: dict, *, now: dt.datetime | None = None,
) -> tuple[dt.datetime, dt.datetime]:
    """Resolve a relative window once so labels and queries cannot cross a day boundary."""
    if now is None and discovery.get(_RESOLVED_WINDOW):
        return resolve_relative_window(discovery)
    current = now or dt.datetime.now(dt.timezone.utc)
    start, end = resolve_relative_window(discovery, now=current)
    discovery[_RESOLVED_WINDOW] = [start.isoformat(), end.isoformat()]
    return start, end


def relative_window_label(
    discovery: dict, *, now: dt.datetime | None = None,
) -> str:
    start_utc, end_utc = resolve_relative_window(discovery, now=now)
    timezone = ZoneInfo(str(discovery["window"]["timezone"]).strip())
    start_local = start_utc.astimezone(timezone)
    end_local = end_utc.astimezone(timezone)
    return f"{start_local:%Y-%m-%d-%H%M}_to_{end_local:%Y-%m-%d-%H%M}"


def build_range_query(start: dt.datetime, end: dt.datetime, categories: list[str]) -> str:
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("arXiv date range must be timezone-aware")
    start_utc = start.astimezone(dt.timezone.utc)
    end_utc = end.astimezone(dt.timezone.utc)
    if end_utc <= start_utc:
        raise ValueError("arXiv date range end must be later than start")
    if any((start_utc.second, start_utc.microsecond, end_utc.second, end_utc.microsecond)):
        raise ValueError("arXiv date range must use minute precision")
    inclusive_end = end_utc - dt.timedelta(minutes=1)
    date_query = f"submittedDate:[{start_utc:%Y%m%d%H%M} TO {inclusive_end:%Y%m%d%H%M}]"
    if not categories:
        return date_query
    category_query = " OR ".join(f"cat:{category}" for category in categories)
    return f"{date_query} AND ({category_query})"


def build_query(date: dt.date, categories: list[str]) -> str:
    start = dt.datetime.combine(date, dt.time.min, tzinfo=dt.timezone.utc)
    return build_range_query(start, start + dt.timedelta(days=1), categories)


def parse_feed(payload: bytes) -> tuple[list[Paper], int]:
    root = ET.fromstring(payload)
    total_node = root.find("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    total = int(total_node.text or 0) if total_node is not None else 0
    papers: list[Paper] = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = (entry.findtext(f"{ATOM}id") or "").strip()
        arxiv_id = raw_id.rsplit("/", 1)[-1]
        links = {node.attrib.get("type", ""): node.attrib.get("href", "") for node in entry.findall(f"{ATOM}link")}
        authors = [(node.findtext(f"{ATOM}name") or "").strip() for node in entry.findall(f"{ATOM}author")]
        categories = [node.attrib.get("term", "") for node in entry.findall(f"{ATOM}category")]
        journal_ref = entry.findtext(f"{ARXIV}journal_ref") or ""
        papers.append(Paper(
            id=arxiv_id,
            title=" ".join((entry.findtext(f"{ATOM}title") or "").split()),
            abstract=" ".join((entry.findtext(f"{ATOM}summary") or "").split()),
            authors=authors,
            published=(entry.findtext(f"{ATOM}published") or "").strip(),
            venue=journal_ref.strip(),
            categories=categories,
            url=f"https://arxiv.org/abs/{arxiv_id}",
            pdf_url=links.get("application/pdf", f"https://arxiv.org/pdf/{arxiv_id}"),
            source="arxiv",
        ))
    return papers, total


def _fetch_page(
    url: str, getter: Callable[[str], bytes], *, attempts: int,
    backoff_seconds: float, max_backoff_seconds: float,
) -> tuple[list[Paper], int]:
    """Retry successful HTTP responses that are not a usable Atom feed."""
    for attempt in range(1, attempts + 1):
        try:
            return parse_feed(getter(url))
        except (ET.ParseError, ValueError) as exc:
            if attempt >= attempts:
                raise RuntimeError(
                    f"Invalid arXiv Atom response after {attempts} attempts: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            delay = min(
                max(0.0, backoff_seconds) * (2 ** (attempt - 1)),
                max(0.0, max_backoff_seconds),
            )
            print(
                f"Invalid arXiv Atom response; retrying in {delay:.1f}s "
                f"(attempt {attempt + 1}/{attempts})",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)


def fetch_arxiv(config: dict, *, get: Callable[[str], bytes] | None = None) -> list[Paper]:
    discovery = config["discovery"]
    request_attempts = int(discovery.get("request_attempts", 5))
    request_backoff = float(discovery.get("request_backoff_seconds", 5.0))
    rate_limit_backoff = float(discovery.get("request_rate_limit_seconds", 60.0))
    max_backoff = float(discovery.get("request_max_backoff_seconds", 300.0))
    getter = get or (lambda url: get_bytes(
        url, accept="application/atom+xml",
        timeout=int(discovery.get("request_timeout_seconds", 120)),
        attempts=request_attempts,
        backoff_seconds=request_backoff,
        rate_limit_backoff_seconds=rate_limit_backoff,
        max_backoff_seconds=max_backoff,
    ))
    categories = list(config["preferences"].get("categories") or [])
    if bool((discovery.get("window") or {}).get("enabled", False)):
        start, end = resolve_relative_window(discovery)
        query = build_range_query(start, end, categories)
    else:
        date = resolve_date(str(discovery["date"]))
        query = build_query(date, categories)
    limit = int(discovery["max_candidates"])
    page_size = min(int(discovery["page_size"]), 2000, limit)
    delay = max(float(discovery["request_delay_seconds"]), 3.0)
    papers: list[Paper] = []
    start = 0
    total = None
    while len(papers) < limit and (total is None or start < total):
        params = urllib.parse.urlencode({
            "search_query": query, "start": start, "max_results": min(page_size, limit - len(papers)),
            "sortBy": "submittedDate", "sortOrder": "descending",
        })
        batch, total = _fetch_page(
            f"https://export.arxiv.org/api/query?{params}", getter,
            attempts=request_attempts,
            backoff_seconds=request_backoff,
            max_backoff_seconds=max_backoff,
        )
        papers.extend(batch)
        if not batch:
            break
        start += len(batch)
        if len(papers) < limit and start < total and get is None:
            time.sleep(delay)
    return papers[:limit]
