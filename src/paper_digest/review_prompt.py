from __future__ import annotations

import json

from .models import Paper


SYSTEM_PROMPT = """You synthesize academic papers for comprehension. Reconstruct the paper's causal story from evidence; do not translate sentence by sentence, preserve source paragraph order, or stitch lightly paraphrased excerpts. Never invent details. Every field must be a connected, readable narrative paragraph in the requested language. Method may contain compact ordered stages or LaTeX equations when necessary, but must remain explanatory prose."""


def build_review_prompt(paper: Paper, text: str, evidence_level: str, language: str, evidence_note: str = "") -> str:
    metadata = {
        "title": paper.title, "authors": paper.authors, "published": paper.published,
        "venue": paper.venue, "categories": paper.categories, "source_url": paper.url,
        "pdf_url": paper.pdf_url, "evidence_level": evidence_level, "evidence_note": evidence_note,
    }
    language_rule = (
        "Write background, motivation, idea, method, experiments, conclusion, and limitations entirely in fluent "
        "Simplified Chinese. Keep only proper names, model/benchmark/metric names, symbols, and equations in English. "
        "Do not return an English narrative paragraph. 所有叙述字段必须使用简体中文，禁止整段英文输出。"
        if language.lower() in {"zh", "zh-cn", "zh-hans"}
        else f"Write every narrative field in {language}."
    )
    return f"""Write a six-part review in {language}. Keep the original English paper title outside the section text.

Mandatory language rule: {language_rule}

The six fields have fixed semantics:
1. background: explain the task, setting, and why the broad problem matters; do not introduce this paper's solution.
2. motivation: identify the concrete limitation, missing capability, or contradiction in prior work and connect it explicitly to the research need.
3. idea: explain in fresh plain language the single decisive conceptual mechanism that closes the motivation gap. State the concept before module names; do not use a contribution list or abstract paraphrase.
4. method: begin with inputs and outputs, then trace the implementation/training/inference flow in causal order. For each important stage say what enters, what transformation occurs, what comes out, and where it goes next. Explain data construction, component interfaces, objectives, optimization, and inference when present. Include only essential equations, define every symbol, and explain what each equation accomplishes.
5. experiments: identify datasets/scenarios, baselines, metrics, key quantitative or qualitative results, ablations/robustness/efficiency evidence, and explain what each result supports. Do not list numbers without interpretation.
6. conclusion: state what is established, why it matters, and material limitations supported by the paper.

Evidence rules:
- fulltext: use the supplied main text from abstract through conclusion, stopping before appendices/references.
- abstract: restrict claims to the abstract and explicitly disclose unavailable method/experiment detail.
- metadata: do not infer the six parts from a title.
- Preserve exact model, benchmark, and metric names when translation reduces precision.
- Output strict JSON with keys background, motivation, idea, method, experiments, conclusion, evidence_level, limitations.

Metadata:
{json.dumps(metadata, ensure_ascii=False, indent=2)}

Paper evidence:
--- BEGIN EVIDENCE ---
{text}
--- END EVIDENCE ---"""
