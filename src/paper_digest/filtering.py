from __future__ import annotations

import json
import math
import re
from dataclasses import replace

from .models import Paper


def _terms(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = " ".join(value.lower().split())
        if normalized:
            result.append(normalized)
            result.extend(token for token in re.findall(r"[a-z][a-z0-9+.-]{2,}", normalized) if token not in {"and", "the", "with", "for"})
    return list(dict.fromkeys(result))


def rule_rank(papers: list[Paper], preferences: dict) -> list[Paper]:
    interests = _terms(list(preferences.get("interests") or []))
    includes = _terms(list(preferences.get("include_keywords") or []))
    excludes = _terms(list(preferences.get("exclude_keywords") or []))
    category_preferences = {str(item).lower() for item in preferences.get("categories") or []}
    ranked: list[Paper] = []
    for paper in papers:
        title = paper.title.lower()
        abstract = paper.abstract.lower()
        haystack = f"{title}\n{abstract}"
        blocked = [term for term in excludes if term in haystack]
        if blocked:
            ranked.append(replace(paper, score=-1.0, score_reasons=[f"excluded: {', '.join(blocked[:3])}"]))
            continue
        title_hits = [term for term in interests + includes if term in title]
        abstract_hits = [term for term in interests + includes if term in abstract and term not in title_hits]
        category_hits = [category for category in paper.categories if category.lower() in category_preferences]
        # Saturating components prevent repeated generic words from dominating.
        title_score = 0.55 * (1 - math.exp(-len(title_hits) / 2))
        abstract_score = 0.30 * (1 - math.exp(-len(abstract_hits) / 4))
        category_score = 0.15 if category_hits else 0.0
        score = min(1.0, title_score + abstract_score + category_score)
        reasons = []
        if title_hits:
            reasons.append(f"title: {', '.join(title_hits[:4])}")
        if abstract_hits:
            reasons.append(f"abstract: {', '.join(abstract_hits[:4])}")
        if category_hits:
            reasons.append(f"category: {', '.join(category_hits[:4])}")
        ranked.append(replace(paper, score=round(score, 4), score_reasons=reasons or ["no configured preference matched"]))
    return sorted(ranked, key=lambda item: (-item.score, item.published, item.title.lower()))


def llm_rerank(papers: list[Paper], preferences: dict, backend, *, batch_size: int = 20, budget=None) -> list[Paper]:
    interests = preferences.get("interests") or []
    results: dict[str, tuple[float, str]] = {}
    for start in range(0, len(papers), batch_size):
        batch = papers[start : start + batch_size]
        compact = [{"id": p.id, "title": p.title, "abstract": p.abstract[:3500], "categories": p.categories} for p in batch]
        prompt = (
            "Score each paper's relevance to the user's research interests from 0 to 1. "
            "Judge research-task and method alignment, not keyword frequency. Return strict JSON: "
            '{"scores":[{"id":"...","score":0.0,"reason":"one short reason"}]}.\n\n'
            f"Interests:\n{json.dumps(interests, ensure_ascii=False)}\n\nPapers:\n{json.dumps(compact, ensure_ascii=False)}"
        )
        if budget is not None:
            budget.reserve(prompt, 1800)
        response, _usage = backend.generate_json("You are a precise academic-paper relevance ranker.", prompt, max_output_tokens=1800)
        for item in response.get("scores", []):
            try:
                results[str(item["id"])] = (max(0.0, min(1.0, float(item["score"]))), str(item.get("reason", "LLM relevance score")))
            except (KeyError, TypeError, ValueError):
                continue
    reranked = [replace(p, score=round(results.get(p.id, (p.score, "fallback rule score"))[0], 4), score_reasons=[results.get(p.id, (p.score, p.score_reasons[0] if p.score_reasons else "fallback rule score"))[1]]) for p in papers]
    return sorted(reranked, key=lambda item: (-item.score, item.title.lower()))
