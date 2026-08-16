# Configuration reference

Use the repository-root `config.toml` as the only non-secret configuration source. Do not maintain a second example config that can drift from production. Its Chinese comments are user-facing guidance; preserve them when changing fields.

## Sources

`discovery.source` accepts:

- `arxiv`: normally reads `discovery.window`, which defines a recurring half-open local-time range `[start, end)` using an IANA timezone, day offsets, and `HH:MM` clocks. It converts both boundaries to GMT; because arXiv's minute-resolution range is inclusive, the query ends one minute before the configured exclusive endpoint. When the window is disabled, it reads `discovery.date`. An explicit `--date` disables the window for that run. Keep consecutive requests at least three seconds apart.
- `crossref`: reads a conference/container title and optional publication-date range. No sign-up is required. Set `mailto` to enter Crossref's polite pool. Crossref metadata can be sparse, so missing abstracts or PDF links must remain explicitly missing until later source resolution.
- `openreview`: reads `discovery.openreview.venue_id`. `status = "accepted"` queries notes whose final `venueid` matches the venue. `status = "all"` queries the submission invitation; set `submission_invitation` explicitly when venue metadata is unusual.
- `json`: reads `discovery.json_path`, relative to the config file unless absolute.

JSON may be a top-level list or `{ "papers": [...] }`. Fields are `id`, `title`, `abstract`, `authors`, `published`, `venue`, `categories`, `url`, `pdf_url`, and `source`. `title` and `id` are required in practice. Use stable DOI, arXiv, or proceedings IDs for resumability.

## Selection

`rules` uses title, abstract, category, positive terms, and hard negative terms. `llm` applies hard negatives first, then sends compact metadata and truncated abstracts to the selected backend. Write the exact inclusion boundary in `decision_policy`; the model must return `include` or `exclude` plus a short reason for every paper. LLM selection does not use `min_score`; that threshold remains available only for `rules` and dry-run previews. `max_selected_papers` is a safety cap on included papers, while `review.max_papers` independently controls expensive full-text reviews.

For a 2,000-paper scan, use batches around 40, abstract excerpts around 1,600 characters, and a maximum retained shortlist of 500. Do not impose a hidden per-batch quota. If binary includes exceed the cap, preserve source order and truncate only at the global safety limit. Inspect `selection-decisions.json` to audit all accepted and rejected papers. A dry run intentionally uses rules and `rules_preview_min_score`; its scores are not comparable to the paid binary LLM decisions.

Treat the preference fields differently:

- Put concise natural-language directions in `interests`, including the target domain, research problem, and preferred method when each matters. This is the primary semantic profile.
- Put acronyms, aliases, task names, model families, and distinctive method terms in `include_keywords`. They are soft positive evidence, not requirements.
- Put only unambiguous unwanted phrases in `exclude_keywords`. Matching is case-insensitive substring matching and is a non-reversible hard exclusion before the LLM call.
- Use `categories` carefully. arXiv applies them during remote discovery, so overly narrow categories permanently reduce recall before ranking. Other sources pass available category metadata to the LLM but may not use it for retrieval.

The candidate cap applies after the configured source query. If `candidate_limit_reached` is true, results beyond the cap were not considered; split dates, categories, tracks, or venues when exhaustive coverage is required. The LLM must return every synthetic paper ID in every batch. Missing IDs cause the run to fail rather than silently shrinking the shortlist.

## Backends

`openai_compatible` calls `<base_url>/chat/completions`, reads the key only from `api_key_env`, and works with providers that implement the common chat-completions response shape. Configure the provider's exact model and per-million-token prices. For DeepSeek V4 Flash, use `https://api.deepseek.com`, `deepseek-v4-flash`, JSON mode, and `supports_thinking_toggle = true`; disable thinking for filtering.

`codex` runs `codex exec` non-interactively with an explicit read-only sandbox, ephemeral sessions, and a JSON output schema. It uses the local Codex login; `codex_model` may remain blank to use the local default.

## Budgets

`max_total_tokens` and `max_estimated_usd` are preflight ceilings. Estimates are conservative and reserve the configured maximum output for every selected paper. Provider-reported usage is recorded when available. A zero price is valid when only a token ceiling matters.

## Output and state

Each job gets a stable directory under `project.output_dir` with `candidates.json`, `selected.json`, `state.json`, per-paper checkpoints, downloaded PDFs, `manifest.json`, and configured digest formats. Install the optional `.[pdf]` dependency for local PDF extraction; otherwise the pipeline honestly falls back to abstract evidence. XeLaTeX or LuaLaTeX is required to compile PDF; LaTeX source is still retained when no engine is installed.

## GitHub Actions and email

Use `.github/workflows/daily-digest.yml` for unattended daily runs. GitHub cron expressions use UTC and must remain in workflow YAML because Actions cannot load a TOML file before scheduling a job. The cron controls when processing starts; `discovery.window` independently controls which submission interval is queried. Keep all application defaults in `config.toml`; use workflow-dispatch inputs only as explicit one-run overrides. Add concurrency control so slow runs do not overlap, always upload outputs and logs, and mark partial generation or failed notification as a failed workflow.

Store these values as repository Actions Secrets, never TOML values:

- `PAPER_DIGEST_API_KEY`: model provider key.
- `PAPER_DIGEST_SMTP_USERNAME`: full sender mailbox address.
- `PAPER_DIGEST_SMTP_PASSWORD`: SMTP authorization code or app password, not a normal account password.
- `PAPER_DIGEST_EMAIL_TO`: one or more comma-separated recipient addresses.
- `PAPER_DIGEST_EMAIL_FROM`: optional envelope/header sender; fall back to the SMTP username.

Configure only the environment-variable names in the `backend` and `email` sections. Set `email.provider` to `qq`, `gmail`, or `netease` and leave host empty, port zero, and security `auto` to use the built-in SSL/465 preset. For NetEase, derive the host from sender domains `163.com`, `126.com`, `yeah.net`, `vip.163.com`, or `vip.126.com`. Use `auto` to infer these supported providers from the sender address. Use `custom` only with an explicit host, port, and `ssl` or `starttls`. Require an SMTP authorization code for QQ/NetEase or an App Password for Gmail. Never print the password while diagnosing authentication failures.

Invoke `paper-digest email --config config.toml --result run-result.json --status success` to send a result. Attach the generated PDF and Markdown only when present and under `email.max_attachment_mb`; attach the run log for partial or failed runs. Include counts, limit warnings, and the Actions run URL in the message body.
