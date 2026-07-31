from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any


DEFAULTS: dict[str, Any] = {
    "project": {"name": "paper-digest", "language": "zh-CN", "output_dir": "outputs"},
    "preferences": {"interests": [], "include_keywords": [], "exclude_keywords": [], "categories": []},
    "discovery": {
        "source": "arxiv", "date": "today", "max_candidates": 2000,
        "page_size": 200, "request_delay_seconds": 3.1, "json_path": "",
        "openreview": {"venue_id": "", "status": "accepted", "submission_invitation": "", "base_url": "https://api2.openreview.net"},
        "crossref": {
            "conference": "", "from_date": "", "until_date": "", "mailto": "",
            "min_venue_similarity": 0.15, "base_url": "https://api.crossref.org",
        },
    },
    "selection": {
        "ranker": "llm", "min_score": 0.60, "max_selected_papers": 500,
        "rules_preview_min_score": 0.0,
        "llm_batch_size": 40, "llm_abstract_chars": 1600,
        "llm_max_output_tokens": 4000, "llm_thinking_mode": "disabled",
    },
    "review": {"max_papers": 5},
    "backend": {
        "type": "openai_compatible", "base_url": "", "model": "", "api_key_env": "PAPER_DIGEST_API_KEY",
        "max_output_tokens": 5500, "temperature": 0.2, "timeout_seconds": 300, "codex_model": "",
        "json_mode": True, "thinking_mode": "", "supports_thinking_toggle": False,
    },
    "fulltext": {"download_pdf": True, "max_main_text_chars": 180000},
    "budget": {
        "max_total_tokens": 3000000, "max_estimated_usd": 2.0,
        "input_usd_per_million": 0.0, "output_usd_per_million": 0.0,
    },
    "output": {"formats": ["json", "markdown", "latex"], "compile_pdf": True},
}


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        loaded = tomllib.load(handle)
    config = _merge(DEFAULTS, loaded)
    # Backward compatibility: the original selection.max_papers controlled both
    # shortlist size and review count.
    loaded_selection = loaded.get("selection") or {}
    if "max_papers" in loaded_selection and "max_selected_papers" not in loaded_selection:
        legacy_limit = int(loaded_selection["max_papers"])
        config["selection"]["max_selected_papers"] = legacy_limit
        if "max_papers" not in (loaded.get("review") or {}):
            config["review"]["max_papers"] = legacy_limit
    if os.getenv("PAPER_DIGEST_BASE_URL"):
        config["backend"]["base_url"] = os.environ["PAPER_DIGEST_BASE_URL"]
    if os.getenv("PAPER_DIGEST_MODEL"):
        config["backend"]["model"] = os.environ["PAPER_DIGEST_MODEL"]
    output_dir = Path(config["project"]["output_dir"])
    if not output_dir.is_absolute():
        output_dir = config_path.parent / output_dir
    config["project"]["output_dir"] = str(output_dir.resolve())
    return config, config_path


def validate_config(config: dict[str, Any], *, require_backend: bool = True) -> None:
    source = config["discovery"]["source"]
    if source not in {"arxiv", "crossref", "openreview", "json"}:
        raise ValueError(f"Unsupported discovery.source: {source}")
    if source == "openreview" and not config["discovery"]["openreview"]["venue_id"]:
        raise ValueError("discovery.openreview.venue_id is required")
    if source == "crossref" and not config["discovery"]["crossref"]["conference"]:
        raise ValueError("discovery.crossref.conference is required")
    if source == "json" and not config["discovery"]["json_path"]:
        raise ValueError("discovery.json_path is required")
    if int(config["discovery"]["max_candidates"]) < 1:
        raise ValueError("discovery.max_candidates must be >= 1")
    if int(config["selection"]["max_selected_papers"]) < 1:
        raise ValueError("selection.max_selected_papers must be >= 1")
    if int(config["review"]["max_papers"]) < 1:
        raise ValueError("review.max_papers must be >= 1")
    if int(config["selection"]["llm_batch_size"]) < 1:
        raise ValueError("selection.llm_batch_size must be >= 1")
    if int(config["selection"]["llm_abstract_chars"]) < 200:
        raise ValueError("selection.llm_abstract_chars must be >= 200")
    if config["selection"]["ranker"] not in {"rules", "llm"}:
        raise ValueError("selection.ranker must be rules or llm")
    if not 0.0 <= float(config["selection"]["min_score"]) <= 1.0:
        raise ValueError("selection.min_score must be between 0 and 1")
    if int(config["selection"]["max_selected_papers"]) > int(config["discovery"]["max_candidates"]):
        raise ValueError("selection.max_selected_papers cannot exceed discovery.max_candidates")
    backend = config["backend"]["type"]
    if backend not in {"openai_compatible", "codex"}:
        raise ValueError(f"Unsupported backend.type: {backend}")
    if require_backend and backend == "openai_compatible":
        if not config["backend"]["base_url"] or not config["backend"]["model"]:
            raise ValueError("backend.base_url and backend.model are required")
