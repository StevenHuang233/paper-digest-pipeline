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
        "ranker": "llm", "min_score": 0.0, "max_selected_papers": 500,
        "rules_preview_min_score": 0.0,
        "llm_batch_size": 40, "llm_abstract_chars": 1600,
        "llm_max_output_tokens": 4000, "llm_thinking_mode": "disabled",
        "llm_prioritize": False, "priority_batch_size": 50, "priority_local_buffer_ratio": 1.5,
        "priority_abstract_chars": 1200, "priority_max_output_tokens": 5000,
        "priority_policy": "Prefer the papers that are most useful for the configured research interests.",
        "decision_policy": (
            "Include only papers whose primary problem, method, or contribution directly matches at least one "
            "research interest. Exclude incidental mentions, generic AI relevance, and insufficient evidence."
        ),
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
    "email": {
        "enabled": False, "provider": "auto", "smtp_host": "", "smtp_port": 0, "security": "auto",
        "subject_prefix": "[Paper Digest]", "username_env": "PAPER_DIGEST_SMTP_USERNAME",
        "password_env": "PAPER_DIGEST_SMTP_PASSWORD", "to_env": "PAPER_DIGEST_EMAIL_TO",
        "from_env": "PAPER_DIGEST_EMAIL_FROM", "attach_pdf": True, "attach_markdown": False,
        "require_pdf_attachment": True, "attach_log_on_failure": True,
        "max_attachment_mb": 20, "timeout_seconds": 60,
    },
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


def validate_email_config(config: dict[str, Any]) -> None:
    email = config["email"]
    if bool(email["enabled"]):
        provider = str(email["provider"]).strip().lower()
        if provider not in {"auto", "qq", "gmail", "netease", "custom"}:
            raise ValueError("email.provider must be auto, qq, gmail, netease, or custom")
        host = str(email["smtp_host"]).strip()
        port = int(email["smtp_port"])
        security = str(email["security"]).strip().lower()
        if port != 0 and not 1 <= port <= 65535:
            raise ValueError("email.smtp_port must be 0 (preset) or between 1 and 65535")
        if security not in {"auto", "ssl", "starttls"}:
            raise ValueError("email.security must be auto, ssl, or starttls")
        if provider == "custom" and (not host or port == 0 or security == "auto"):
            raise ValueError("custom email provider requires smtp_host, smtp_port, and explicit security")
        for field in ("username_env", "password_env", "to_env"):
            if not str(email[field]).strip():
                raise ValueError(f"email.{field} is required when email.enabled is true")
        if float(email["max_attachment_mb"]) <= 0:
            raise ValueError("email.max_attachment_mb must be greater than zero")
        if bool(email["require_pdf_attachment"]) and not bool(email["attach_pdf"]):
            raise ValueError("email.attach_pdf must be true when require_pdf_attachment is enabled")


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
    if int(config["selection"]["priority_batch_size"]) < 2:
        raise ValueError("selection.priority_batch_size must be >= 2")
    if not 1.0 <= float(config["selection"]["priority_local_buffer_ratio"]) <= 5.0:
        raise ValueError("selection.priority_local_buffer_ratio must be between 1.0 and 5.0")
    if int(config["selection"]["priority_abstract_chars"]) < 200:
        raise ValueError("selection.priority_abstract_chars must be >= 200")
    if int(config["selection"]["priority_max_output_tokens"]) < 100:
        raise ValueError("selection.priority_max_output_tokens must be >= 100")
    if config["selection"]["ranker"] not in {"rules", "llm"}:
        raise ValueError("selection.ranker must be rules or llm")
    if config["selection"]["ranker"] == "llm" and not str(config["selection"]["decision_policy"]).strip():
        raise ValueError("selection.decision_policy is required for llm selection")
    if bool(config["selection"]["llm_prioritize"]) and not str(config["selection"]["priority_policy"]).strip():
        raise ValueError("selection.priority_policy is required when llm_prioritize is enabled")
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
    validate_email_config(config)
