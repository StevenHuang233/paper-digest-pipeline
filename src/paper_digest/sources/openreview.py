from __future__ import annotations

import urllib.parse
from typing import Any, Callable

from ..models import Paper
from .common import get_json


def _value(content: dict[str, Any], key: str, default: Any = "") -> Any:
    value = content.get(key, default)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def parse_notes(payload: dict[str, Any], venue_id: str) -> list[Paper]:
    papers: list[Paper] = []
    for note in payload.get("notes", []):
        content = note.get("content") or {}
        title = str(_value(content, "title", "")).strip()
        if not title:
            continue
        note_id = str(note.get("id") or note.get("forum") or "")
        forum_id = str(note.get("forum") or note_id)
        authors = _value(content, "authors", []) or []
        keywords = _value(content, "keywords", []) or []
        cdate = note.get("cdate") or note.get("pdate") or ""
        papers.append(Paper(
            id=note_id,
            title=" ".join(title.split()),
            abstract=" ".join(str(_value(content, "abstract", "")).split()),
            authors=[str(item) for item in authors],
            published=str(cdate),
            venue=venue_id,
            categories=[str(item) for item in keywords],
            url=f"https://openreview.net/forum?id={forum_id}",
            pdf_url=f"https://openreview.net/pdf?id={note_id}",
            source="openreview",
        ))
    return papers


def _submission_invitation(base_url: str, venue_id: str, getter: Callable[[str], dict]) -> str:
    url = f"{base_url}/groups?{urllib.parse.urlencode({'id': venue_id})}"
    payload = getter(url)
    groups = payload.get("groups") or []
    if not groups:
        raise RuntimeError(f"OpenReview venue group not found: {venue_id}")
    submission_name = _value(groups[0].get("content") or {}, "submission_name", "Submission")
    return f"{venue_id}/-/{submission_name}"


def fetch_openreview(config: dict, *, get: Callable[[str], dict] | None = None) -> list[Paper]:
    raw_getter = get or get_json

    def getter(url: str) -> dict:
        try:
            return raw_getter(url)
        except RuntimeError as exc:
            if "ChallengeRequiredError" in str(exc) or "challenge verification required" in str(exc).lower():
                raise RuntimeError(
                    "OpenReview requires interactive challenge verification in this environment. "
                    "Use the Crossref conference source or import an official OpenReview export through the JSON source."
                ) from exc
            raise
    discovery = config["discovery"]
    settings = discovery["openreview"]
    venue_id = settings["venue_id"]
    base_url = settings["base_url"].rstrip("/")
    limit = int(discovery["max_candidates"])
    page_size = min(int(discovery["page_size"]), 1000)
    status = settings["status"]
    if status == "accepted":
        filter_key, filter_value = "content.venueid", venue_id
    elif status == "all":
        invitation = settings.get("submission_invitation") or _submission_invitation(base_url, venue_id, getter)
        filter_key, filter_value = "invitation", invitation
    else:
        raise ValueError("OpenReview status must be accepted or all")

    papers: list[Paper] = []
    offset = 0
    while len(papers) < limit:
        params = {filter_key: filter_value, "limit": min(page_size, limit - len(papers)), "offset": offset}
        payload = getter(f"{base_url}/notes?{urllib.parse.urlencode(params)}")
        batch = parse_notes(payload, venue_id)
        papers.extend(batch)
        raw_count = len(payload.get("notes", []))
        if raw_count == 0 or raw_count < params["limit"]:
            break
        offset += raw_count
    return papers[:limit]
