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
            ranked.append(replace(
                paper, score=-1.0, score_reasons=[f"hard excluded: {', '.join(blocked[:3])}"],
                selection_decision="hard_exclude", selection_scores={},
            ))
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
        ranked.append(replace(
            paper, score=round(score, 4), score_reasons=reasons or ["no configured preference matched"],
            selection_decision="rule_score", selection_scores={},
        ))
    return sorted(ranked, key=lambda item: (-item.score, item.published, item.title.lower()))


def _clamp_int(value, lower: int, upper: int) -> int:
    return max(lower, min(upper, int(value)))


def _ranking_schema() -> dict:
    score_fields = {
        "topic": {"type": "integer", "minimum": 0, "maximum": 40},
        "problem": {"type": "integer", "minimum": 0, "maximum": 20},
        "method": {"type": "integer", "minimum": 0, "maximum": 30},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 10},
    }
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "scores": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"id": {"type": "string"}, **score_fields, "reason": {"type": "string"}},
                    "required": ["id", "topic", "problem", "method", "confidence", "reason"],
                },
            }
        },
        "required": ["scores"],
    }


def llm_rerank(
    papers: list[Paper], preferences: dict, backend, *, batch_size: int = 40,
    abstract_chars: int = 1600, max_output_tokens: int = 4000,
    thinking_mode: str = "disabled", budget=None,
) -> list[Paper]:
    """Score every non-excluded paper against one fixed 100-point rubric."""
    eligible = [(index, paper) for index, paper in enumerate(papers) if paper.selection_decision != "hard_exclude"]
    results: dict[int, tuple[float, str, dict[str, int], str]] = {}
    profile = {
        "research_interests": preferences.get("interests") or [],
        "positive_terms": preferences.get("include_keywords") or [],
        "target_categories": preferences.get("categories") or [],
    }
    for start in range(0, len(eligible), batch_size):
        batch = eligible[start : start + batch_size]
        compact = [
            {
                "id": f"p{index:06d}", "title": paper.title,
                "abstract": paper.abstract[:abstract_chars], "categories": paper.categories,
            }
            for index, paper in batch
        ]
        prompt = f"""Evaluate every paper against the same user profile. This is semantic relevance filtering, not keyword counting.

Use this fixed 100-point rubric independently for every paper:
- topic (0-40): direct alignment with the research directions.
- problem (0-20): alignment of the research question, task, or application setting.
- method (0-30): alignment of the technical approach, representations, learning signals, or system design.
- confidence (0-10): how confidently the title and abstract support the judgment; do not award confidence when evidence is vague.

Be strict and cross-batch consistent. A total of 60 means relevant enough to retain, 40-59 means borderline, and below 40 means unrelated. Do not reward generic words such as model, learning, data, or network. Return one item for every supplied id in strict JSON. Keep each reason under 20 words.

Required JSON example:
{{"scores":[{{"id":"p000000","topic":32,"problem":16,"method":24,"confidence":8,"reason":"Direct graph-agent method alignment."}}]}}

User profile:
{json.dumps(profile, ensure_ascii=False)}

Papers:
{json.dumps(compact, ensure_ascii=False)}"""
        if budget is not None:
            budget.reserve(prompt, max_output_tokens)
        response, _usage = backend.generate_json(
            "You are a calibrated academic-paper relevance evaluator. Output JSON only.",
            prompt, max_output_tokens=max_output_tokens, schema=_ranking_schema(), thinking_mode=thinking_mode,
        )
        expected = {f"p{index:06d}": index for index, _paper in batch}
        seen: set[str] = set()
        for item in response.get("scores", []):
            record_id = str(item.get("id") or "")
            if record_id not in expected or record_id in seen:
                continue
            try:
                components = {
                    "topic": _clamp_int(item["topic"], 0, 40),
                    "problem": _clamp_int(item["problem"], 0, 20),
                    "method": _clamp_int(item["method"], 0, 30),
                    "confidence": _clamp_int(item["confidence"], 0, 10),
                }
            except (KeyError, TypeError, ValueError):
                continue
            total = sum(components.values())
            decision = "include" if total >= 60 else "maybe" if total >= 40 else "exclude"
            reason = " ".join(str(item.get("reason") or "LLM rubric score").split())
            results[expected[record_id]] = (total / 100, reason, components, decision)
            seen.add(record_id)
        missing = sorted(set(expected) - seen)
        if missing:
            raise RuntimeError(f"LLM ranking response omitted {len(missing)} of {len(expected)} papers in a batch")

    reranked: list[Paper] = []
    for index, paper in enumerate(papers):
        if paper.selection_decision == "hard_exclude":
            reranked.append(paper)
            continue
        score, reason, components, decision = results[index]
        reranked.append(replace(
            paper, score=round(score, 4), score_reasons=[f"LLM {int(score * 100)}/100: {reason}"],
            selection_decision=decision, selection_scores=components,
        ))
    return sorted(reranked, key=lambda item: (-item.score, item.title.lower()))
