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


def _decision_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "decisions": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "decision": {"type": "string", "enum": ["include", "exclude"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "decision", "reason"],
                },
            }
        },
        "required": ["decisions"],
    }


def _priority_schema() -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "shortlist": {
                "type": "array", "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "reason"],
                },
            }
        },
        "required": ["shortlist"],
    }


def llm_rerank(
    papers: list[Paper], preferences: dict, backend, *, batch_size: int = 40,
    abstract_chars: int = 1600, max_output_tokens: int = 4000,
    thinking_mode: str = "disabled", decision_policy: str = "", budget=None,
) -> list[Paper]:
    """Make one explicit include/exclude decision for every non-excluded paper."""
    eligible = [(index, paper) for index, paper in enumerate(papers) if paper.selection_decision != "hard_exclude"]
    results: dict[int, tuple[str, str]] = {}
    profile = {
        "research_interests": preferences.get("interests") or [],
        "positive_terms": preferences.get("include_keywords") or [],
        "target_categories": preferences.get("categories") or [],
        "decision_policy": decision_policy,
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
        prompt = f"""Decide whether to include or exclude every paper for this user's research feed. This is semantic relevance filtering, not keyword counting.

Follow the configured decision policy as the authoritative inclusion rule. Return `include` only when the title and abstract provide concrete evidence that the policy is satisfied. Positive terms are soft hints, never mandatory matches. Return `exclude` for incidental mentions, generic AI relevance, weakly related applications, or insufficient evidence. Resolve borderline cases by asking whether the user would reasonably want to read the full paper under that policy.

Use the same decision boundary across every batch. Do not assign scores, probabilities, confidence values, ranks, or per-batch quotas. Return one item for every supplied id in strict JSON and keep each reason under 20 words.

Required JSON example:
{{"decisions":[{{"id":"p000000","decision":"include","reason":"Primary method directly advances multimodal agent reasoning."}}]}}

User profile:
{json.dumps(profile, ensure_ascii=False)}

Papers:
{json.dumps(compact, ensure_ascii=False)}"""
        if budget is not None:
            budget.reserve(prompt, max_output_tokens)
        response, _usage = backend.generate_json(
            "You are a strict academic-paper relevance gate. Output JSON only.",
            prompt, max_output_tokens=max_output_tokens, schema=_decision_schema(), thinking_mode=thinking_mode,
        )
        expected = {f"p{index:06d}": index for index, _paper in batch}
        seen: set[str] = set()
        for item in response.get("decisions", []):
            record_id = str(item.get("id") or "")
            if record_id not in expected or record_id in seen:
                continue
            decision = str(item.get("decision") or "").strip().lower()
            if decision not in {"include", "exclude"}:
                continue
            reason = " ".join(str(item.get("reason") or "LLM binary relevance decision").split())
            results[expected[record_id]] = (decision, reason)
            seen.add(record_id)
        missing = sorted(set(expected) - seen)
        if missing:
            raise RuntimeError(f"LLM ranking response omitted {len(missing)} of {len(expected)} papers in a batch")

    reranked: list[Paper] = []
    for index, paper in enumerate(papers):
        if paper.selection_decision == "hard_exclude":
            reranked.append(paper)
            continue
        decision, reason = results[index]
        reranked.append(replace(
            paper, score=0.0, score_reasons=[f"LLM {decision}: {reason}"],
            selection_decision=decision, selection_scores={},
        ))
    return reranked


def _priority_request(
    papers: list[tuple[str, Paper]], backend, *, quota: int, abstract_chars: int,
    max_output_tokens: int, thinking_mode: str, priority_policy: str,
    stage: str, budget=None,
) -> list[tuple[str, str]]:
    compact = [
        {
            "id": record_id, "title": paper.title,
            "abstract": paper.abstract[:abstract_chars], "categories": paper.categories,
        }
        for record_id, paper in papers
    ]
    prompt = f"""Select the most valuable papers for this user's daily research reading list from the supplied candidates.

This is a comparative shortlist, not numeric scoring. Apply the priority policy to the paper's primary
contribution using title and abstract evidence. Prefer strong research fit, substantive and novel methods,
credible experiments or benchmarks, and ideas likely to inform the user's own research. Preserve diversity
across the user's stated interests instead of filling the list with near-duplicate papers.

Return exactly {quota} unique papers, ordered from most to least valuable within this {stage} selection.
Do not output scores, probabilities, confidence values, or ranks. Return strict JSON and keep each reason
under 20 words.

Required JSON example:
{{"shortlist":[{{"id":"p000000","reason":"Directly improves grounded visual evidence acquisition with strong evaluation."}}]}}

Priority policy:
{priority_policy}

Papers:
{json.dumps(compact, ensure_ascii=False)}"""
    if budget is not None:
        budget.reserve(prompt, max_output_tokens)
    response, _usage = backend.generate_json(
        "You are a strict academic-paper shortlist editor. Output JSON only.",
        prompt, max_output_tokens=max_output_tokens, schema=_priority_schema(), thinking_mode=thinking_mode,
    )
    expected = {record_id for record_id, _paper in papers}
    chosen: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in response.get("shortlist", []):
        record_id = str(item.get("id") or "")
        if record_id not in expected or record_id in seen:
            continue
        reason = " ".join(str(item.get("reason") or "Selected by LLM priority shortlist").split())
        chosen.append((record_id, reason))
        seen.add(record_id)
    if len(chosen) != quota:
        raise RuntimeError(f"LLM priority response returned {len(chosen)} valid papers; expected exactly {quota}")
    return chosen


def llm_prioritize(
    papers: list[Paper], backend, *, max_papers: int, batch_size: int = 50,
    local_buffer_ratio: float = 1.5,
    abstract_chars: int = 1200, max_output_tokens: int = 5000,
    thinking_mode: str = "disabled", priority_policy: str = "", budget=None,
) -> list[Paper]:
    """Choose an ordered top-N shortlist without assigning numeric relevance scores."""
    if max_papers < 1:
        raise ValueError("max_papers must be at least 1")
    if len(papers) <= max_papers:
        return papers

    indexed = [(f"p{index:06d}", paper) for index, paper in enumerate(papers)]
    paper_by_id = dict(indexed)
    pool: list[tuple[str, Paper]] = []
    total = len(indexed)
    for start in range(0, total, batch_size):
        batch = indexed[start : start + batch_size]
        proportional_quota = math.ceil(max_papers * len(batch) / total)
        # A configurable buffer reduces early batch-allocation bias before the global pass.
        local_quota = min(len(batch), max(1, math.ceil(proportional_quota * local_buffer_ratio)))
        local = _priority_request(
            batch, backend, quota=local_quota, abstract_chars=abstract_chars,
            max_output_tokens=max_output_tokens, thinking_mode=thinking_mode,
            priority_policy=priority_policy, stage="local", budget=budget,
        )
        pool.extend((record_id, paper_by_id[record_id]) for record_id, _reason in local)

    if len(pool) > max_papers:
        final = _priority_request(
            pool, backend, quota=max_papers, abstract_chars=abstract_chars,
            max_output_tokens=max_output_tokens, thinking_mode=thinking_mode,
            priority_policy=priority_policy, stage="global", budget=budget,
        )
    else:
        final = [(record_id, "Selected by local priority shortlist") for record_id, _paper in pool]

    result: list[Paper] = []
    for rank, (record_id, reason) in enumerate(final, start=1):
        paper = paper_by_id[record_id]
        result.append(replace(
            paper,
            score_reasons=[*paper.score_reasons, f"LLM priority #{rank}: {reason}"],
        ))
    return result
