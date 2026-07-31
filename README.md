# Paper Digest Pipeline

An independent, budget-aware pipeline that discovers papers, filters them against a research profile, and then applies the existing six-part review semantics: Background, Motivation, Idea, implementation-level Method, Experiments, and Conclusion.

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[pdf]"
Copy-Item config.example.toml config.toml
$env:PAPER_DIGEST_API_KEY = "your-provider-key"
paper-digest run --config config.toml --date 2026-07-28
```

For discovery and dry runs without PDF extraction, `pip install -e .` is sufficient. The repository skill wrapper also works without installation:

```powershell
python .agents\skills\daily-paper-digest\scripts\run_digest.py run --config config.toml --dry-run
```

Use `--dry-run` to discover and rank without downloading PDFs or calling a model. To use the current Codex login instead of an API key, set `backend.type = "codex"` or pass `--backend codex`.

```powershell
paper-digest run --config config.toml --backend codex --max-papers 3
```

The state and all artifacts are written below `project.output_dir`. Re-running the same date/source job reuses completed summaries unless `--force` is supplied.

## LLM selection at scale

The example configuration discovers at most 2,000 papers and retains at most 500 in `selected.json`. Deep review is a separate limit (`review.max_papers`, default 5), so retaining a large conference shortlist does not accidentally generate 500 full reviews.

With `selection.ranker = "llm"`, every non-excluded paper is scored with one fixed 100-point rubric: topic alignment 0–40, problem/task alignment 0–20, method alignment 0–30, and evidence confidence 0–10. The default threshold is 60. Papers are evaluated in batches of 40 using title, categories, and at most 1,600 abstract characters. Exact negative-keyword matches are removed before the model call.

The information flow is: source query → at most 2,000 normalized candidates → exact hard exclusions → 40-paper LLM batches → one score for every candidate → threshold and global sort → at most 500 retained papers → at most `review.max_papers` full-text reviews. An incomplete LLM batch response fails visibly instead of silently losing papers.

Configure the research profile by meaning, not by building one giant keyword list:

- `interests`: write a few precise research directions that name the domain, problem, and preferred methods. These are the main semantic instructions to the LLM.
- `include_keywords`: add aliases, model families, task names, or technical terms that should increase relevance. These are positive hints, not mandatory matches.
- `exclude_keywords`: use only high-precision unwanted phrases. A case-insensitive substring match is a hard exclusion and the LLM cannot restore that paper.
- `categories`: for arXiv, these restrict the server-side query before the 2,000-paper cap, so keep them broad enough to avoid recall loss. For imported or conference records, categories are metadata supplied to the ranker.

The 2,000 cap is a hard safety ceiling, not a promise to retrieve every matching paper when a source contains more than 2,000 results. `candidates.json` and `manifest.json` record `candidate_limit_reached`; when it is true, narrow by date/category or split the job into multiple queries if exhaustive coverage matters.

`--dry-run` remains free: it writes a rule-ranked preview using `rules_preview_min_score`. Rule-preview scores are not predictions of the later 100-point LLM score.

The bundled DeepSeek profile uses the official OpenAI-format endpoint and `deepseek-v4-flash`. It enables JSON Output and disables thinking during selection for lower cost and more predictable structured output. Keep the API key only in `PAPER_DIGEST_API_KEY`; never place it in TOML or Git.

## Sources

- `arxiv`: queries the official Atom API by submitted date and category.
- `crossref`: searches proceedings metadata by conference/container name and publication date without requiring sign-up.
- `openreview`: accepts an OpenReview venue ID. `accepted` queries the final venue ID; `all` uses the venue submission invitation.
- `json`: reads a local list of normalized records, which covers proceedings sites or exports without a stable public API.

OpenReview may require interactive challenge verification for anonymous clients in some environments. For unattended conference jobs, prefer Crossref when its proceedings metadata is sufficient, or import an official conference export through `json`.

JSON records accept: `id`, `title`, `abstract`, `authors`, `published`, `venue`, `categories`, `url`, `pdf_url`, and `source`.

## Cost controls

Rules and hard exclusions run before any model call. LLM ranking sees only compact metadata and abstracts, and only the selected shortlist can proceed to full-text review. Selection and completed summaries are checkpointed. `budget.max_total_tokens` and `budget.max_estimated_usd` are hard preflight gates. Keys are read from the configured environment variable and are never stored in outputs.

## Scheduling

The included GitHub Actions workflow supports a daily cron and a manual date override. Copy `config.example.toml` to `config.toml` in a private deployment or adjust the workflow to point to a safe committed config, then add `PAPER_DIGEST_API_KEY` as a repository secret. Local Task Scheduler or cron can run the same `paper-digest run` command.
