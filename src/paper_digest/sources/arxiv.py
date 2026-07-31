from __future__ import annotations

import datetime as dt
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Callable

from ..models import Paper
from .common import get_bytes


ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def resolve_date(value: str, today: dt.date | None = None) -> dt.date:
    base = today or dt.datetime.now().astimezone().date()
    if value == "today":
        return base
    if value == "yesterday":
        return base - dt.timedelta(days=1)
    return dt.date.fromisoformat(value)


def build_query(date: dt.date, categories: list[str]) -> str:
    stamp = date.strftime("%Y%m%d")
    date_query = f"submittedDate:[{stamp}0000 TO {stamp}2359]"
    if not categories:
        return date_query
    category_query = " OR ".join(f"cat:{category}" for category in categories)
    return f"{date_query} AND ({category_query})"


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


def fetch_arxiv(config: dict, *, get: Callable[[str], bytes] | None = None) -> list[Paper]:
    getter = get or (lambda url: get_bytes(url, accept="application/atom+xml"))
    discovery = config["discovery"]
    date = resolve_date(str(discovery["date"]))
    query = build_query(date, list(config["preferences"].get("categories") or []))
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
        batch, total = parse_feed(getter(f"https://export.arxiv.org/api/query?{params}"))
        papers.extend(batch)
        if not batch:
            break
        start += len(batch)
        if len(papers) < limit and start < total and get is None:
            time.sleep(delay)
    return papers[:limit]

