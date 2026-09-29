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
ATOM_ACCEPT = "application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"


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


def set_explicit_window(
    discovery: dict, start_value: str, end_value: str,
) -> tuple[dt.datetime, dt.datetime]:
    """Freeze an explicit interval, interpreting naive values in the configured timezone."""
    timezone_name = str((discovery.get("window") or {}).get("timezone") or "").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown discovery.window.timezone: {timezone_name}") from exc

    def parse(value: str, field: str) -> dt.datetime:
        try:
            parsed = dt.datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ValueError(
                f"{field} must use YYYY-MM-DD HH:MM or an ISO 8601 datetime"
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        if parsed.second or parsed.microsecond:
            raise ValueError(f"{field} must use minute precision")
        return parsed.astimezone(dt.timezone.utc)

    start = parse(start_value, "window start")
    end = parse(end_value, "window end")
    if end <= start:
        raise ValueError("window end must be later than window start")
    discovery.setdefault("window", {})["enabled"] = True
    discovery[_RESOLVED_WINDOW] = [start.isoformat(), end.isoformat()]
    return start, end


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
    if root.tag != f"{ATOM}feed":
        raise ValueError("arXiv response is not an Atom feed")
    entries = root.findall(f"{ATOM}entry")
    for entry in entries:
        raw_id = (entry.findtext(f"{ATOM}id") or "").strip()
        if urllib.parse.urlsplit(raw_id).path == "/api/errors":
            detail = " ".join((entry.findtext(f"{ATOM}summary") or "Unknown API error").split())
            raise ValueError(f"arXiv API error: {detail[:500]}")
    total_node = root.find("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    if total_node is None or not (total_node.text or "").strip():
        raise ValueError("arXiv Atom response is missing totalResults")
    total = int(total_node.text)
    if total < 0 or len(entries) > total:
        raise ValueError("arXiv Atom response has inconsistent totalResults")
    papers: list[Paper] = []
    for entry in entries:
        raw_id = (entry.findtext(f"{ATOM}id") or "").strip()
        if not raw_id or not (entry.findtext(f"{ATOM}title") or "").strip():
            raise ValueError("arXiv Atom entry is missing its id or title")
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


def _query_urls(query: str, start: int, max_results: int) -> list[str]:
    """Return equivalent API URLs to work around transient edge-cache 406s."""
    values = {
        "search_query": query,
        "start": start,
        "max_results": max_results,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    variants: list[tuple[dict[str, object], str]] = [
        (values, "plus"),
        (values.copy(), "percent"),
    ]
    if max_results > 100:
        reduced = values.copy()
        reduced["max_results"] = min(max_results, 100)
        variants.append((reduced, "percent"))
    urls: list[str] = []
    for variant, encoding in variants:
        if encoding == "percent":
            encoded = urllib.parse.urlencode(variant, quote_via=urllib.parse.quote)
        else:
            encoded = urllib.parse.urlencode(variant)
        url = f"https://export.arxiv.org/api/query?{encoded}"
        if url not in urls:
            urls.append(url)
    return urls


def _fetch_page_with_fallbacks(
    urls: list[str], getter: Callable[[str], bytes], *, attempts: int,
    backoff_seconds: float, max_backoff_seconds: float,
) -> tuple[list[Paper], int]:
    for index, url in enumerate(urls):
        try:
            return _fetch_page(
                url, getter, attempts=attempts,
                backoff_seconds=backoff_seconds,
                max_backoff_seconds=max_backoff_seconds,
            )
        except RuntimeError as exc:
            if "HTTP 406" not in str(exc) or index == len(urls) - 1:
                raise
            print(
                "arXiv returned HTTP 406; retrying with an alternate query form",
                file=sys.stderr,
                flush=True,
            )
    raise RuntimeError("No arXiv query URL was available")


def fetch_arxiv(config: dict, *, get: Callable[[str], bytes] | None = None) -> list[Paper]:
    discovery = config["discovery"]
    request_attempts = int(discovery.get("request_attempts", 5))
    request_backoff = float(discovery.get("request_backoff_seconds", 5.0))
    rate_limit_backoff = float(discovery.get("request_rate_limit_seconds", 60.0))
    max_backoff = float(discovery.get("request_max_backoff_seconds", 300.0))
    getter = get or (lambda url: get_bytes(
        url, accept=ATOM_ACCEPT,
        timeout=int(discovery.get("request_timeout_seconds", 120)),
        attempts=request_attempts,
        backoff_seconds=request_backoff,
        rate_limit_backoff_seconds=rate_limit_backoff,
        max_backoff_seconds=max_backoff,
        extra_retryable_statuses=(406,),
        extra_rate_limit_statuses=(406,),
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
        batch, total = _fetch_page_with_fallbacks(
            _query_urls(query, start, min(page_size, limit - len(papers))), getter,
            attempts=request_attempts,
            backoff_seconds=request_backoff,
            max_backoff_seconds=max_backoff,
        )
        if not batch:
            if start < total:
                raise RuntimeError(f"Incomplete arXiv response: empty page at {start} of {total}")
            break
        papers.extend(batch)
        start += len(batch)
        page_size = min(page_size, len(batch))
        if len(papers) < limit and start < total and get is None:
            time.sleep(delay)
    return papers[:limit]
