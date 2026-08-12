from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .backends import REVIEW_SCHEMA, make_backend
from .budget import from_config
from .filtering import llm_prioritize, llm_rerank, rule_rank
from .fulltext import download_and_extract, safe_name
from .models import Paper, SixPartReview
from .outputs import write_outputs
from .review_prompt import SYSTEM_PROMPT, build_review_prompt
from .sources import fetch_arxiv, fetch_crossref, fetch_json, fetch_openreview
from .sources.arxiv import resolve_date
from .state import read_json, write_json


def deduplicate(papers: list[Paper]) -> list[Paper]:
    seen: set[str] = set()
    result: list[Paper] = []
    for paper in papers:
        key = " ".join(paper.title.lower().split()) or paper.id.lower()
        if key not in seen:
            seen.add(key)
            result.append(paper)
    return result


def discover(config: dict, config_path: Path) -> list[Paper]:
    source = config["discovery"]["source"]
    if source == "arxiv":
        return deduplicate(fetch_arxiv(config))
    if source == "openreview":
        return deduplicate(fetch_openreview(config))
    if source == "crossref":
        return deduplicate(fetch_crossref(config))
    return deduplicate(fetch_json(config, config_dir=config_path.parent))


def job_label(config: dict) -> str:
    source = config["discovery"]["source"]
    discriminator = config["discovery"].get("date", "")
    if source == "arxiv":
        discriminator = str(resolve_date(str(discriminator)))
    elif source == "openreview":
        discriminator = config["discovery"]["openreview"]["venue_id"]
    elif source == "crossref":
        discriminator = config["discovery"]["crossref"]["conference"]
    elif source == "json":
        discriminator = Path(config["discovery"]["json_path"]).stem
    return f"{safe_name(source)}-{safe_name(str(discriminator))}"


def job_directory(config: dict) -> Path:
    return Path(config["project"]["output_dir"]) / job_label(config)


def rank_and_select(
    config: dict, papers: list[Paper], *, backend=None, allow_llm: bool = True,
    budget=None, min_score_override: float | None = None, return_ranked: bool = False,
) -> list[Paper] | tuple[list[Paper], list[Paper]]:
    ranked = rule_rank(papers, config["preferences"])
    if config["selection"]["ranker"] == "llm" and allow_llm:
        # rule_rank identifies hard exclusions, but binary LLM selection must not
        # inherit its score ordering. Restore source order before batching.
        marked = {(paper.source, paper.id, paper.title): paper for paper in ranked}
        ranked = [marked[(paper.source, paper.id, paper.title)] for paper in papers]
        backend = backend or make_backend(config["backend"])
        ranked = llm_rerank(
            ranked, config["preferences"], backend,
            batch_size=int(config["selection"]["llm_batch_size"]),
            abstract_chars=int(config["selection"]["llm_abstract_chars"]),
            max_output_tokens=int(config["selection"]["llm_max_output_tokens"]),
            thinking_mode=str(config["selection"]["llm_thinking_mode"]),
            decision_policy=str(config["selection"].get("decision_policy") or ""),
            decision_rounds=int(config["selection"].get("decision_rounds", 2)),
            decision_shuffle_seed=str(config["selection"].get("decision_shuffle_seed") or "paper-digest-decision-v2"),
            response_attempts=int(config["selection"].get("llm_response_attempts", 3)),
            budget=budget,
        )
        selected = [paper for paper in ranked if paper.selection_decision == "include"]
        selected_limit = int(config["selection"]["max_selected_papers"])
        if bool(config["selection"].get("llm_prioritize", False)) and len(selected) > selected_limit:
            selected = llm_prioritize(
                selected, backend, max_papers=selected_limit,
                batch_size=int(config["selection"].get("priority_batch_size", 50)),
                local_buffer_ratio=float(config["selection"].get("priority_local_buffer_ratio", 1.0)),
                rounds=int(config["selection"].get("priority_rounds", 3)),
                shuffle_seed=str(config["selection"].get("priority_shuffle_seed") or "paper-digest-priority-v2"),
                abstract_chars=int(config["selection"].get("priority_abstract_chars", 1200)),
                max_output_tokens=int(config["selection"].get("priority_max_output_tokens", 5000)),
                thinking_mode=str(config["selection"].get("llm_thinking_mode", "disabled")),
                priority_policy=str(config["selection"].get("priority_policy") or ""),
                budget=budget,
            )
    else:
        threshold = float(config["selection"]["min_score"] if min_score_override is None else min_score_override)
        selected = [paper for paper in ranked if paper.score >= threshold]
    selected = selected[: int(config["selection"]["max_selected_papers"])]
    return (selected, ranked) if return_ranked else selected


def _validate_review(value: dict, evidence_level: str) -> SixPartReview:
    fields = ["background", "motivation", "idea", "method", "experiments", "conclusion"]
    missing = [key for key in fields if not str(value.get(key, "")).strip()]
    if missing:
        raise ValueError(f"Model review is missing fields: {', '.join(missing)}")
    clean = {key: str(value[key]).strip() for key in fields}
    clean["evidence_level"] = evidence_level
    clean["limitations"] = str(value.get("limitations", "")).strip()
    return SixPartReview(**clean)


def _selection_fingerprint(config: dict, papers: list[Paper], effective_ranker: str) -> str:
    value = {
        "preferences": config["preferences"], "selection": config["selection"],
        "effective_ranker": effective_ranker, "selection_logic_version": "llm-consensus-rotating-panels-v3",
        "papers": [(paper.source, paper.id, paper.title, paper.abstract) for paper in papers],
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _completion_status(completed: int, target: int, *, fail_on_incomplete: bool) -> str:
    if completed == target:
        return "complete"
    if completed > 0 and not fail_on_incomplete:
        return "complete"
    return "partial"


def run_pipeline(config: dict, config_path: Path, *, dry_run: bool = False, force: bool = False, papers_path: Path | None = None) -> dict:
    run_dir = job_directory(config)
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    progress_manifest = {
        "status": "failure", "job_label": job_label(config),
        "failure_stage": "discovery", "run_dir": str(run_dir),
    }
    write_json(manifest_path, progress_manifest)
    if papers_path:
        loaded = read_json(papers_path, [])
        records = loaded.get("papers", []) if isinstance(loaded, dict) else loaded
        candidates = [Paper.from_dict(item) for item in records]
    else:
        candidates = discover(config, config_path)
    candidate_limit = int(config["discovery"]["max_candidates"])
    progress_manifest.update({
        "candidate_count": len(candidates),
        "candidate_limit_reached": len(candidates) >= candidate_limit,
        "failure_stage": "selection",
    })
    write_json(manifest_path, progress_manifest)
    write_json(run_dir / "candidates.json", {
        "count": len(candidates), "limit": candidate_limit,
        "limit_reached": len(candidates) >= candidate_limit,
        "papers": [paper.to_dict() for paper in candidates],
    })

    state_path = run_dir / "state.json"
    state = read_json(state_path, {"completed": {}, "failed": {}, "budget": {}})
    budget = from_config(config["budget"])
    budget.reserved_tokens = int(state.get("budget", {}).get("reserved_tokens", 0))
    budget.reserved_usd = float(state.get("budget", {}).get("reserved_usd", 0.0))
    backend = None
    effective_ranker = "rules_preview" if dry_run and config["selection"]["ranker"] == "llm" else config["selection"]["ranker"]
    fingerprint = _selection_fingerprint(config, candidates, effective_ranker)
    selected_path = run_dir / "selected.json"
    decisions_path = run_dir / "selection-decisions.json"
    cached_selection = read_json(selected_path, {})
    if not force and cached_selection.get("fingerprint") == fingerprint:
        selected = [Paper.from_dict(item) for item in cached_selection.get("papers", [])]
        cached_decisions = read_json(decisions_path, {})
        evaluated = [Paper.from_dict(item) for item in cached_decisions.get("papers", [])] or selected
    else:
        if not dry_run and config["selection"]["ranker"] == "llm":
            backend = make_backend(config["backend"])
        try:
            preview_threshold = None
            if dry_run and config["selection"]["ranker"] == "llm":
                preview_threshold = float(config["selection"]["rules_preview_min_score"])
            selected, evaluated = rank_and_select(
                config, candidates, backend=backend, allow_llm=not dry_run,
                budget=budget, min_score_override=preview_threshold, return_ranked=True,
            )
        except Exception:
            state["budget"] = {"reserved_tokens": budget.reserved_tokens, "reserved_usd": round(budget.reserved_usd, 6)}
            write_json(state_path, state)
            raise
        selected_limit = int(config["selection"]["max_selected_papers"])
        decision_counts = {
            decision: sum(paper.selection_decision == decision for paper in evaluated)
            for decision in ("include", "exclude", "hard_exclude", "rule_score")
        }
        write_json(decisions_path, {
            "fingerprint": fingerprint, "ranker_used": effective_ranker,
            "count": len(evaluated), "decision_counts": decision_counts,
            "papers": [paper.to_dict() for paper in evaluated],
        })
        write_json(selected_path, {
            "fingerprint": fingerprint, "ranker_used": effective_ranker,
            "count": len(selected), "limit": selected_limit,
            "limit_reached": len(selected) >= selected_limit,
            "papers": [paper.to_dict() for paper in selected],
        })
    decision_counts = {
        decision: sum(paper.selection_decision == decision for paper in evaluated)
        for decision in ("include", "exclude", "hard_exclude", "rule_score")
    }
    if dry_run:
        review_target_count = min(len(selected), int(config["review"]["max_papers"]))
        manifest = {
            "status": "dry-run", "candidate_count": len(candidates), "selected_count": len(selected),
            "job_label": job_label(config),
            "candidate_limit_reached": len(candidates) >= candidate_limit,
            "selected_limit_reached": len(selected) >= int(config["selection"]["max_selected_papers"]),
            "selection_decisions": decision_counts,
            "review_limit": int(config["review"]["max_papers"]),
            "review_target_count": review_target_count, "run_dir": str(run_dir),
        }
        write_json(manifest_path, manifest)
        return manifest

    review_targets = selected[: int(config["review"]["max_papers"])]
    progress_manifest.update({
        "selected_count": len(selected),
        "selected_limit_reached": len(selected) >= int(config["selection"]["max_selected_papers"]),
        "selection_decisions": decision_counts,
        "review_target_count": len(review_targets), "completed_count": 0,
        "failed_count": 0, "failure_stage": "review",
    })
    write_json(manifest_path, progress_manifest)
    if review_targets and backend is None:
        backend = make_backend(config["backend"])
    summaries_dir = run_dir / "summaries"
    summaries_dir.mkdir(exist_ok=True)
    records: list[dict] = []
    for paper in review_targets:
        key = hashlib.sha256(f"{paper.source}\0{paper.id}\0{paper.title}".encode("utf-8")).hexdigest()[:16]
        summary_path = summaries_dir / f"{key}.json"
        if summary_path.exists() and not force:
            records.append(read_json(summary_path, {}))
            progress_manifest["completed_count"] = len(records)
            write_json(manifest_path, progress_manifest)
            continue
        if config["fulltext"]["download_pdf"]:
            text, evidence_level, evidence_note = download_and_extract(
                paper, run_dir / "pdfs", int(config["fulltext"]["max_main_text_chars"])
            )
        else:
            text, evidence_level, evidence_note = paper.abstract, "abstract", "PDF download was disabled by configuration."
        prompt = build_review_prompt(paper, text, evidence_level, config["project"]["language"], evidence_note)
        try:
            max_attempts = int(config["review"].get("max_attempts", 3))
            attempt_prompt = prompt
            for attempt in range(1, max_attempts + 1):
                try:
                    tokens, cost = budget.reserve(attempt_prompt, int(config["backend"]["max_output_tokens"]))
                    value, usage = backend.generate_json(
                        SYSTEM_PROMPT, attempt_prompt,
                        max_output_tokens=int(config["backend"]["max_output_tokens"]), schema=REVIEW_SCHEMA,
                    )
                    review = _validate_review(value, evidence_level)
                    break
                except Exception as exc:
                    if attempt >= max_attempts or "budget exceeded" in str(exc).lower():
                        raise
                    attempt_prompt = (
                        f"{prompt}\n\nThe previous generation attempt failed with: {type(exc).__name__}: {exc}. "
                        "Retry from the supplied paper evidence and return one complete JSON object containing "
                        "all required six-part review fields."
                    )
            record = {
                "paper": paper.to_dict(), "review": review.to_dict(),
                "generation": {
                    "backend": config["backend"]["type"], "model": config["backend"].get("model", ""),
                    "attempts": attempt, "reserved_tokens": tokens, "reserved_usd": round(cost, 6),
                    "reported_usage": usage,
                },
            }
            write_json(summary_path, record)
            state["completed"][key] = str(summary_path)
            state["failed"].pop(key, None)
            records.append(record)
            progress_manifest.update({"completed_count": len(records), "failed_count": len(state["failed"])})
            write_json(manifest_path, progress_manifest)
        except Exception as exc:
            state["failed"][key] = f"{type(exc).__name__}: {exc}"
            write_json(state_path, state)
            progress_manifest.update({"completed_count": len(records), "failed_count": len(state["failed"])})
            write_json(manifest_path, progress_manifest)
            if "budget exceeded" in str(exc).lower():
                break
    state["budget"] = {"reserved_tokens": budget.reserved_tokens, "reserved_usd": round(budget.reserved_usd, 6)}
    write_json(state_path, state)
    progress_manifest.update({
        "completed_count": len(records), "failed_count": len(state["failed"]),
        "budget": state["budget"], "failure_stage": "output",
    })
    write_json(manifest_path, progress_manifest)
    try:
        outputs = write_outputs(run_dir, records, list(config["output"]["formats"]), bool(config["output"]["compile_pdf"]))
    except Exception as exc:
        progress_manifest["error"] = f"{type(exc).__name__}: {exc}"
        write_json(manifest_path, progress_manifest)
        raise
    manifest = {
        "status": _completion_status(
            len(records), len(review_targets),
            fail_on_incomplete=bool(config["review"].get("fail_on_incomplete", False)),
        ),
        "job_label": job_label(config),
        "candidate_count": len(candidates), "selected_count": len(selected),
        "candidate_limit_reached": len(candidates) >= candidate_limit,
        "selected_limit_reached": len(selected) >= int(config["selection"]["max_selected_papers"]),
        "selection_decisions": decision_counts,
        "review_target_count": len(review_targets), "completed_count": len(records),
        "failed_count": len(state["failed"]), "budget": state["budget"], "outputs": outputs, "run_dir": str(run_dir),
    }
    write_json(manifest_path, manifest)
    return manifest
