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

## Sources

- `arxiv`: queries the official Atom API by submitted date and category.
- `crossref`: searches proceedings metadata by conference/container name and publication date without requiring sign-up.
- `openreview`: accepts an OpenReview venue ID. `accepted` queries the final venue ID; `all` uses the venue submission invitation.
- `json`: reads a local list of normalized records, which covers proceedings sites or exports without a stable public API.

OpenReview may require interactive challenge verification for anonymous clients in some environments. For unattended conference jobs, prefer Crossref when its proceedings metadata is sufficient, or import an official conference export through `json`.

JSON records accept: `id`, `title`, `abstract`, `authors`, `published`, `venue`, `categories`, `url`, `pdf_url`, and `source`.

## Cost controls

Rules run before any model call. Optional LLM ranking sees only titles and abstracts, and only the selected Top-K papers proceed to full-text review. `budget.max_total_tokens` and `budget.max_estimated_usd` are hard preflight gates. Keys are read from the configured environment variable and are never stored in outputs.

## Scheduling

The included GitHub Actions workflow supports a daily cron and a manual date override. Copy `config.example.toml` to `config.toml` in a private deployment or adjust the workflow to point to a safe committed config, then add `PAPER_DIGEST_API_KEY` as a repository secret. Local Task Scheduler or cron can run the same `paper-digest run` command.
