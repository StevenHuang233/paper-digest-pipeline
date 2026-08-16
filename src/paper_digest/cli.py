from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from .config import load_config, validate_config, validate_email_config
from .emailer import read_result, send_digest_email
from .orchestrator import discover, job_directory, run_pipeline
from .state import write_json


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper-digest", description="Discover, filter, and synthesize academic papers.")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Copy the repository config to a new location")
    init.add_argument("--output", default="config.toml")
    for name in ("discover", "run", "summarize"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--config", default="config.toml")
        cmd.add_argument("--date")
        cmd.add_argument("--source", choices=["arxiv", "crossref", "openreview", "json"])
        cmd.add_argument("--max-selected", type=int, help="Maximum papers retained after relevance filtering")
        cmd.add_argument("--max-papers", type=int, help="Maximum retained papers to summarize deeply")
        cmd.add_argument("--backend", choices=["openai_compatible", "codex"])
        if name == "run":
            cmd.add_argument("--dry-run", action="store_true")
            cmd.add_argument("--force", action="store_true")
        if name == "summarize":
            cmd.add_argument("--papers", required=True, help="Path to a JSON list or {papers:[...]} file")
            cmd.add_argument("--force", action="store_true")
    email = sub.add_parser("email", help="Send a digest result through the configured SMTP server")
    email.add_argument("--config", default="config.toml")
    email.add_argument("--result", help="Path to the JSON result printed by paper-digest run")
    email.add_argument("--status", choices=["success", "partial", "failure"], default="success")
    email.add_argument("--log", help="Failure log to attach when configured")
    email.add_argument("--run-url", default="", help="GitHub Actions or other run URL")
    return parser


def _overrides(config: dict, args: argparse.Namespace) -> None:
    if getattr(args, "date", None):
        config["discovery"]["date"] = args.date
        config["discovery"]["window"]["enabled"] = False
    if getattr(args, "source", None):
        config["discovery"]["source"] = args.source
    if getattr(args, "max_papers", None):
        config["review"]["max_papers"] = args.max_papers
    if getattr(args, "max_selected", None):
        config["selection"]["max_selected_papers"] = args.max_selected
    if getattr(args, "backend", None):
        config["backend"]["type"] = args.backend


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "init":
        source = Path(__file__).resolve().parents[2] / "config.toml"
        target = Path(args.output).expanduser().resolve()
        if target.exists():
            raise SystemExit(f"Refusing to overwrite existing file: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        print(target)
        return
    config, config_path = load_config(args.config)
    if args.command == "email":
        validate_email_config(config)
        result = send_digest_email(
            config, read_result(args.result), status=args.status,
            run_url=args.run_url, log_path=args.log,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    _overrides(config, args)
    validate_config(config, require_backend=args.command != "discover")
    if args.command == "discover":
        papers = discover(config, config_path)
        run_dir = job_directory(config)
        path = run_dir / "candidates.json"
        limit = int(config["discovery"]["max_candidates"])
        write_json(path, {
            "count": len(papers), "limit": limit, "limit_reached": len(papers) >= limit,
            "papers": [paper.to_dict() for paper in papers],
        })
        result = {"candidate_count": len(papers), "candidate_limit_reached": len(papers) >= limit, "path": str(path)}
    elif args.command == "summarize":
        result = run_pipeline(config, config_path, force=args.force, papers_path=Path(args.papers).resolve())
    else:
        result = run_pipeline(config, config_path, dry_run=args.dry_run, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
