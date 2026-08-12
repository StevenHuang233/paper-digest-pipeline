from __future__ import annotations

import hashlib
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
                        "match_area": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "decision", "match_area", "reason"],
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


def _stable_rotation(items: list, seed: str, round_number: int) -> list:
    """Return a deterministic shuffle that is stable across Python processes."""
    def key(item) -> str:
        record_id, paper = item
        material = f"{seed}\0{round_number}\0{record_id}\0{paper.id}\0{paper.title}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    return sorted(items, key=key)


def _decision_pass(
    eligible: list[tuple[int, Paper]], preferences: dict, backend, *, batch_size: int,
    abstract_chars: int, max_output_tokens: int, thinking_mode: str,
    decision_policy: str, stage: str, response_attempts: int = 3, budget=None,
) -> dict[int, tuple[str, str, str]]:
    results: dict[int, tuple[str, str, str]] = {}
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

This is {stage}. Follow the configured decision policy as the authoritative boundary. An `include`
requires the paper's PRIMARY problem, method, dataset, or evaluation contribution to directly satisfy
one explicit policy area. For every include, return that area's configured label in `match_area` and cite
concrete title/abstract evidence. If no explicit area is directly satisfied, return `exclude` with
`match_area` set to `none`. Possible future usefulness, incidental terminology, an off-the-shelf model,
or a routine domain application is not enough.

Apply hard exclusions before positive rules. Treat generic AI, generic reinforcement learning, generic
language-agent work, and single-modality applications as excluded unless the configured policy explicitly
includes them. Read every supplied paper before answering and use the same boundary regardless of batch
composition or item position.

Do not assign scores, probabilities, confidence values, ranks, or per-batch quotas. Return one item for
every supplied id in strict JSON and keep each reason under 24 words.

Required JSON example:
{{"decisions":[{{"id":"p000000","decision":"include","match_area":"B","reason":"Primary method diagnoses missing visual evidence in an MLLM."}},{{"id":"p000001","decision":"exclude","match_area":"none","reason":"Generic control RL without a foundation-model contribution."}}]}}

User profile:
{json.dumps(profile, ensure_ascii=False)}

Papers:
{json.dumps(compact, ensure_ascii=False)}"""
        expected = {f"p{index:06d}": index for index, _paper in batch}
        for attempt in range(1, response_attempts + 1):
            if budget is not None:
                budget.reserve(prompt, max_output_tokens)
            response, _usage = backend.generate_json(
                "You are a strict and position-invariant academic-paper relevance gate. Output JSON only.",
                prompt, max_output_tokens=max_output_tokens, schema=_decision_schema(), thinking_mode=thinking_mode,
            )
            batch_results: dict[int, tuple[str, str, str]] = {}
            seen: set[str] = set()
            for item in response.get("decisions", []):
                record_id = str(item.get("id") or "")
                if record_id not in expected or record_id in seen:
                    continue
                decision = str(item.get("decision") or "").strip().lower()
                if decision not in {"include", "exclude"}:
                    continue
                match_area = " ".join(str(item.get("match_area") or "none").split())
                reason = " ".join(str(item.get("reason") or "LLM binary relevance decision").split())
                if decision == "include" and match_area.lower() in {"", "none", "n/a", "na", "unmatched"}:
                    decision = "exclude"
                    match_area = "none"
                    reason = "No explicit configured policy area was identified."
                elif decision == "exclude":
                    match_area = "none"
                batch_results[expected[record_id]] = (decision, match_area, reason)
                seen.add(record_id)
            missing = sorted(set(expected) - seen)
            if not missing:
                results.update(batch_results)
                break
            if attempt >= response_attempts:
                raise RuntimeError(f"LLM ranking response omitted {len(missing)} of {len(expected)} papers in a batch")
            prompt += (
                f"\n\nRetry notice: the previous response omitted {len(missing)} required ids. "
                "Return exactly one valid decision for every supplied id."
            )
    return results


def llm_rerank(
    papers: list[Paper], preferences: dict, backend, *, batch_size: int = 40,
    abstract_chars: int = 1600, max_output_tokens: int = 4000,
    thinking_mode: str = "disabled", decision_policy: str = "",
    decision_rounds: int = 1, decision_shuffle_seed: str = "paper-digest-decision-v2",
    response_attempts: int = 3, budget=None,
) -> list[Paper]:
    """Make explicit decisions, rotating batches and adjudicating disagreements."""
    if decision_rounds < 1:
        raise ValueError("decision_rounds must be at least 1")
    eligible = [(index, paper) for index, paper in enumerate(papers) if paper.selection_decision != "hard_exclude"]
    round_results: list[dict[int, tuple[str, str, str]]] = []
    indexed = [(f"p{index:06d}", paper) for index, paper in eligible]
    for round_number in range(decision_rounds):
        rotated = _stable_rotation(indexed, decision_shuffle_seed, round_number)
        rotated_eligible = [(int(record_id[1:]), paper) for record_id, paper in rotated]
        round_results.append(_decision_pass(
            rotated_eligible, preferences, backend, batch_size=batch_size,
            abstract_chars=abstract_chars, max_output_tokens=max_output_tokens,
            thinking_mode=thinking_mode, decision_policy=decision_policy,
            stage=f"independent decision pass {round_number + 1}/{decision_rounds}",
            response_attempts=response_attempts, budget=budget,
        ))

    disagreements: list[tuple[int, Paper]] = []
    for index, paper in eligible:
        votes = [result[index][0] for result in round_results]
        if len(set(votes)) > 1:
            disagreements.append((index, paper))
    adjudicated: dict[int, tuple[str, str, str]] = {}
    if disagreements:
        rotated_disagreements = _stable_rotation(
            [(f"p{index:06d}", paper) for index, paper in disagreements],
            f"{decision_shuffle_seed}-adjudication", 0,
        )
        adjudicated = _decision_pass(
            [(int(record_id[1:]), paper) for record_id, paper in rotated_disagreements],
            preferences, backend, batch_size=batch_size, abstract_chars=abstract_chars,
            max_output_tokens=max_output_tokens, thinking_mode=thinking_mode,
            decision_policy=decision_policy,
            stage="final adjudication for papers with conflicting independent decisions",
            response_attempts=response_attempts, budget=budget,
        )

    reranked: list[Paper] = []
    for index, paper in enumerate(papers):
        if paper.selection_decision == "hard_exclude":
            reranked.append(paper)
            continue
        votes = [result[index][0] for result in round_results]
        if index in adjudicated:
            decision, match_area, reason = adjudicated[index]
            audit = f"adjudicated after {votes.count('include')}/{decision_rounds} include votes"
        else:
            decision, match_area, reason = round_results[0][index]
            audit = f"{decision_rounds}/{decision_rounds} consistent"
        reranked.append(replace(
            paper, score=0.0, score_reasons=[f"LLM {decision} [{match_area}] ({audit}): {reason}"],
            selection_decision=decision,
            selection_scores={"include_votes": votes.count("include"), "decision_rounds": decision_rounds},
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

Return at most {quota} unique papers, ordered from most to least valuable within this {stage} selection.
It is acceptable to return fewer when fewer candidates deserve to advance. Do not output scores,
probabilities, confidence values, or ranks. Return strict JSON and keep each reason under 20 words.

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
    # Some OpenAI-compatible providers do not enforce array length from the
    # prompt and may return the whole panel. Keep the model's order, cap the
    # response locally, and accept a shorter shortlist instead of failing the
    # entire daily job.
    return chosen[:quota]


def llm_prioritize(
    papers: list[Paper], backend, *, max_papers: int, batch_size: int = 50,
    local_buffer_ratio: float = 1.0, rounds: int = 3,
    shuffle_seed: str = "paper-digest-priority-v2",
    abstract_chars: int = 1200, max_output_tokens: int = 5000,
    thinking_mode: str = "disabled", priority_policy: str = "", budget=None,
) -> list[Paper]:
    """Choose an ordered top-N shortlist without assigning numeric relevance scores."""
    if max_papers < 1:
        raise ValueError("max_papers must be at least 1")
    if rounds < 1:
        raise ValueError("rounds must be at least 1")
    if len(papers) <= max_papers:
        return papers

    indexed = [(f"p{index:06d}", paper) for index, paper in enumerate(papers)]
    paper_by_id = dict(indexed)
    total = len(indexed)
    wins = {record_id: 0 for record_id, _paper in indexed}
    rank_cost = {record_id: 0.0 for record_id, _paper in indexed}
    best_reason: dict[str, str] = {}
    for round_number in range(rounds):
        rotated = _stable_rotation(indexed, shuffle_seed, round_number)
        for start in range(0, total, batch_size):
            batch = rotated[start : start + batch_size]
            proportional_quota = math.ceil(max_papers * len(batch) / total)
            local_quota = min(len(batch), max(1, math.ceil(proportional_quota * local_buffer_ratio)))
            local = _priority_request(
                batch, backend, quota=local_quota, abstract_chars=abstract_chars,
                max_output_tokens=max_output_tokens, thinking_mode=thinking_mode,
                priority_policy=priority_policy,
                stage=f"rotating panel {round_number + 1}/{rounds}", budget=budget,
            )
            positions = {record_id: position for position, (record_id, _reason) in enumerate(local, start=1)}
            reasons = dict(local)
            for record_id, _paper in batch:
                if record_id in positions:
                    wins[record_id] += 1
                    rank_cost[record_id] += positions[record_id] / local_quota
                    best_reason.setdefault(record_id, reasons[record_id])
                else:
                    # A fixed loss penalty is comparable across differently sized panels.
                    rank_cost[record_id] += 2.0

    # A paper must advance through at least one comparison panel. This keeps
    # max_papers as a ceiling rather than silently filling the digest with
    # candidates that the model never shortlisted.
    final_ids = sorted(
        (record_id for record_id in paper_by_id if wins[record_id] > 0),
        key=lambda record_id: (
            -wins[record_id], rank_cost[record_id],
            hashlib.sha256(f"{shuffle_seed}\0final\0{record_id}".encode("utf-8")).hexdigest(),
        ),
    )[:max_papers]

    result: list[Paper] = []
    for rank, record_id in enumerate(final_ids, start=1):
        paper = paper_by_id[record_id]
        reason = best_reason.get(record_id, "Advanced through the rotating comparison panels.")
        result.append(replace(
            paper,
            score_reasons=[
                *paper.score_reasons,
                f"LLM priority #{rank} ({wins[record_id]}/{rounds} panels): {reason}",
            ],
        ))
    return result
