# Configuration reference

## Sources

`discovery.source` accepts:

- `arxiv`: reads `discovery.date`, preference categories, pagination, and the request delay. The official API uses GMT submission dates. Keep consecutive requests at least three seconds apart.
- `crossref`: reads a conference/container title and optional publication-date range. No sign-up is required. Set `mailto` to enter Crossref's polite pool. Crossref metadata can be sparse, so missing abstracts or PDF links must remain explicitly missing until later source resolution.
- `openreview`: reads `discovery.openreview.venue_id`. `status = "accepted"` queries notes whose final `venueid` matches the venue. `status = "all"` queries the submission invitation; set `submission_invitation` explicitly when venue metadata is unusual.
- `json`: reads `discovery.json_path`, relative to the config file unless absolute.

JSON may be a top-level list or `{ "papers": [...] }`. Fields are `id`, `title`, `abstract`, `authors`, `published`, `venue`, `categories`, `url`, `pdf_url`, and `source`. `title` and `id` are required in practice. Use stable DOI, arXiv, or proceedings IDs for resumability.

## Selection

`rules` uses title, abstract, category, positive terms, and hard negative terms. `llm` first runs the same rule pass, then sends only compact metadata and abstracts to the selected backend. `min_score` is applied before `max_papers`.

## Backends

`openai_compatible` calls `<base_url>/chat/completions`, reads the key only from `api_key_env`, and works with providers that implement the common chat-completions response shape. Configure the provider's exact model and per-million-token prices.

`codex` runs `codex exec` non-interactively with an explicit read-only sandbox, ephemeral sessions, and a JSON output schema. It uses the local Codex login; `codex_model` may remain blank to use the local default.

## Budgets

`max_total_tokens` and `max_estimated_usd` are preflight ceilings. Estimates are conservative and reserve the configured maximum output for every selected paper. Provider-reported usage is recorded when available. A zero price is valid when only a token ceiling matters.

## Output and state

Each job gets a stable directory under `project.output_dir` with `candidates.json`, `selected.json`, `state.json`, per-paper checkpoints, downloaded PDFs, `manifest.json`, and configured digest formats. Install the optional `.[pdf]` dependency for local PDF extraction; otherwise the pipeline honestly falls back to abstract evidence. XeLaTeX or LuaLaTeX is required to compile PDF; LaTeX source is still retained when no engine is installed.
