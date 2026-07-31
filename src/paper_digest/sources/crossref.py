from __future__ import annotations

import html
import re
import time
import urllib.parse
from typing import Any, Callable

from ..models import Paper
from .common import get_json


STOPWORDS = {"the", "of", "on", "and", "in", "for", "proceedings", "conference", "international", "annual"}


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if token not in STOPWORDS and not token.isdigit()}


def venue_similarity(query: str, containers: list[str]) -> float:
    expected = _tokens(query)
    if not expected:
        return 1.0
    best = 0.0
    for container in containers:
        actual = _tokens(container)
        if actual:
            best = max(best, len(expected & actual) / len(expected))
    return best


def _date(item: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "issued"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts:
            values = [int(value) for value in parts]
            return "-".join(f"{value:02d}" if index else f"{value:04d}" for index, value in enumerate(values))
    return ""


def _strip_jats(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def parse_items(items: list[dict[str, Any]], conference: str, min_similarity: float) -> list[Paper]:
    papers: list[Paper] = []
    for item in items:
        titles = item.get("title") or []
        if not titles:
            continue
        containers = [str(value) for value in item.get("container-title") or []]
        if venue_similarity(conference, containers) < min_similarity:
            continue
        doi = str(item.get("DOI") or "")
        links = item.get("link") or []
        pdf_url = next((str(link.get("URL")) for link in links if "pdf" in str(link.get("content-type", "")).lower()), "")
        authors = []
        for author in item.get("author") or []:
            name = " ".join(filter(None, [str(author.get("given") or ""), str(author.get("family") or "")])).strip()
            if name:
                authors.append(name)
        url = str(item.get("URL") or (f"https://doi.org/{doi}" if doi else ""))
        papers.append(Paper(
            id=doi or url or str(titles[0]), title=" ".join(str(titles[0]).split()),
            abstract=_strip_jats(str(item.get("abstract") or "")), authors=authors,
            published=_date(item), venue=containers[0] if containers else conference,
            categories=[str(value) for value in item.get("subject") or []],
            url=url, pdf_url=pdf_url, source="crossref",
        ))
    return papers


def fetch_crossref(config: dict, *, get: Callable[[str], dict] | None = None) -> list[Paper]:
    getter = get or get_json
    discovery = config["discovery"]
    settings = discovery["crossref"]
    limit = int(discovery["max_candidates"])
    page_size = min(int(discovery["page_size"]), 1000, limit)
    filters = ["type:proceedings-article"]
    if settings.get("from_date"):
        filters.append(f"from-pub-date:{settings['from_date']}")
    if settings.get("until_date"):
        filters.append(f"until-pub-date:{settings['until_date']}")
    cursor = "*"
    papers: list[Paper] = []
    while len(papers) < limit:
        params = {
            "query.container-title": settings["conference"], "filter": ",".join(filters),
            "rows": min(page_size, limit - len(papers)), "cursor": cursor,
            "select": "DOI,title,author,published,published-print,published-online,issued,container-title,URL,abstract,link,subject,type",
        }
        if settings.get("mailto"):
            params["mailto"] = settings["mailto"]
        payload = getter(f"{settings['base_url'].rstrip('/')}/works?{urllib.parse.urlencode(params)}")
        message = payload.get("message") or {}
        items = message.get("items") or []
        papers.extend(parse_items(items, settings["conference"], float(settings["min_venue_similarity"])))
        next_cursor = str(message.get("next-cursor") or "")
        if not items or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        if get is None and len(papers) < limit:
            time.sleep(1.0)
    return papers[:limit]
