---
name: daily-paper-digest
description: "Discover arXiv papers for today or a specified date, retrieve conference papers through Crossref, OpenReview, or a normalized JSON proceedings export, filter them by a user's research preferences, and create budget-controlled six-part paper reviews. Use when Codex is asked for a daily paper feed, conference-paper shortlist, research-interest alert, cheap-API paper digest, scheduled literature scan, or one-to-many review run with either an OpenAI-compatible provider or Codex."
---

# Daily Paper Digest

Run the project pipeline without changing the six-part review semantics. Separate discovery/ranking from full-text synthesis so broad candidate scans remain inexpensive.

## Workflow

1. Locate the project root containing `pyproject.toml` and `config.example.toml`.
2. Create a user config with `python .agents/skills/daily-paper-digest/scripts/run_digest.py init --output config.toml` when none exists.
3. Edit only non-secret preferences and provider settings in `config.toml`. Keep API keys in the environment variable named by `backend.api_key_env`.
4. Run a zero-model preview first for a new preference profile:

```powershell
python .agents/skills/daily-paper-digest/scripts/run_digest.py run --config config.toml --dry-run
```

5. Inspect `selected.json`. Adjust interests, include/exclude keywords, categories, threshold, or Top-K when selection is visibly off.
6. Generate with an inexpensive OpenAI-compatible provider or the current Codex login:

```powershell
python .agents/skills/daily-paper-digest/scripts/run_digest.py run --config config.toml --backend openai_compatible
python .agents/skills/daily-paper-digest/scripts/run_digest.py run --config config.toml --backend codex
```

7. Report the manifest status, selected/completed counts, reserved budget, failures, and output paths. Do not claim completion when the manifest says `partial`.

## Source selection

- Use `arxiv` for a daily or specified submission date. Accept `today`, `yesterday`, or `YYYY-MM-DD`.
- Use `crossref` for unattended proceedings discovery by conference name and publication-date range.
- Use `openreview` for a venue ID such as `ICLR.cc/2026/Conference`. Prefer `accepted`; use `all` only when under-review submissions are intended.
- Use `json` for non-OpenReview proceedings or a curated list. Read [references/configuration.md](references/configuration.md) before mapping unfamiliar metadata.

## Cost and evidence rules

- Use deterministic ranking for zero-cost previews. Use `selection.ranker = "llm"` when semantic relevance is the primary selection signal and the configured budget covers the candidate set.
- For large LLM scans, keep `discovery.max_candidates`, `selection.max_selected_papers`, and `review.max_papers` distinct. Retaining 500 papers must not silently trigger 500 deep reviews.
- Apply hard negative-keyword exclusions before LLM ranking. Score every remaining paper with the fixed topic/problem/method/confidence rubric and reject incomplete batch responses instead of silently dropping papers.
- When using DeepSeek V4 Flash for filtering, use model `deepseek-v4-flash`, enable JSON Output, and disable thinking for the selection calls.
- Apply the configured token and estimated-dollar limits as hard gates. Never raise them silently.
- Download and read full main text only for selected papers. Stop before appendices/references by default.
- Preserve the fixed narrative sections: Background, Motivation, Core Idea, implementation-level Method, Experiments and what they demonstrate, and Conclusion.
- Explain Idea conceptually and Method as an input-to-output implementation trace with essential interpreted equations. Do not substitute translation or abstract paraphrase for synthesis.
- Label abstract-only fallbacks honestly and do not invent missing experimental or method details.
- Preserve clickable arXiv, OpenReview, proceedings, or DOI links in outputs.

## Resume and scheduling

Reuse existing per-paper summary checkpoints unless the user explicitly requests regeneration with `--force`. For schedules, configure the included GitHub workflow or call the same command from cron/Task Scheduler. Never commit `.env`, API keys, or generated private-paper content.
