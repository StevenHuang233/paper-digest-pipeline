from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path


SECTIONS = [
    ("background", "问题背景 / Problem Background"),
    ("motivation", "研究动机 / Motivation"),
    ("idea", "核心思想 / Core Idea"),
    ("method", "具体方法 / Method"),
    ("experiments", "实验与说明 / Experiments and Interpretation"),
    ("conclusion", "结论 / Conclusion"),
]


def render_markdown(records: list[dict]) -> str:
    lines = ["# Paper Digest", ""]
    for index, record in enumerate(records, 1):
        paper, review = record["paper"], record["review"]
        link = paper.get("url") or paper.get("pdf_url") or ""
        title = paper["title"]
        lines.extend([f"## {index}. [{title}]({link})" if link else f"## {index}. {title}", ""])
        meta = " · ".join(filter(None, [", ".join(paper.get("authors") or []), paper.get("published", ""), paper.get("venue", "")]))
        if meta:
            lines.extend([meta, ""])
        lines.extend([f"Evidence: `{review.get('evidence_level', 'unknown')}`", ""])
        for key, label in SECTIONS:
            lines.extend([f"### {label}", "", review.get(key, "").strip(), ""])
        if review.get("limitations"):
            lines.extend(["### 证据与局限 / Evidence Notes", "", review["limitations"].strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def _latex_plain(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_\allowbreak{}", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
        "α": r"\ensuremath{\alpha}", "β": r"\ensuremath{\beta}", "γ": r"\ensuremath{\gamma}",
        "η": r"\ensuremath{\eta}", "θ": r"\ensuremath{\theta}", "λ": r"\ensuremath{\lambda}",
        "π": r"\ensuremath{\pi}", "ρ": r"\ensuremath{\rho}", "τ": r"\ensuremath{\tau}",
        "φ": r"\ensuremath{\phi}", "ψ": r"\ensuremath{\psi}", "ω": r"\ensuremath{\omega}",
        "ζ": r"\ensuremath{\zeta}", "≥": r"\ensuremath{\geq}", "≤": r"\ensuremath{\leq}",
        "∝": r"\ensuremath{\propto}", "⟨": r"\ensuremath{\langle}", "⟩": r"\ensuremath{\rangle}",
        "−": "-",
    }
    return "".join(replacements.get(char, char) for char in text)


def _latex_math(text: str) -> str:
    replacements = {
        "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "η": r"\eta", "θ": r"\theta",
        "λ": r"\lambda", "π": r"\pi", "ρ": r"\rho", "τ": r"\tau", "φ": r"\phi",
        "ψ": r"\psi", "ω": r"\omega", "ζ": r"\zeta", "≥": r"\geq", "≤": r"\leq",
        "∝": r"\propto", "⟨": r"\langle", "⟩": r"\rangle", "−": "-",
    }
    return "".join(replacements.get(char, char) for char in text)


def latex_escape(text: str) -> str:
    # Preserve genuine math spans, but treat currency pairs containing prose as
    # literal dollars rather than opening and closing math mode.
    parts = re.split(r"(\$[^$]+\$|\\\(.+?\\\)|\\\[.+?\\\])", text, flags=re.S)
    rendered: list[str] = []
    for part in parts:
        if part.startswith("$") and part.endswith("$"):
            inner = part[1:-1]
            if re.search(r"[\u3400-\u9fff]", inner):
                rendered.append(_latex_plain(part))
            else:
                rendered.append(f"${_latex_math(inner)}$")
        elif part.startswith(r"\(") or part.startswith(r"\["):
            rendered.append(_latex_math(part))
        else:
            rendered.append(_latex_plain(part))
    return "".join(rendered)


def render_latex(records: list[dict]) -> str:
    body: list[str] = []
    for record in records:
        paper, review = record["paper"], record["review"]
        title = latex_escape(paper["title"])
        title = title.replace(r"$\beta$", r"\texorpdfstring{$\beta$}{beta}")
        url = paper.get("url") or paper.get("pdf_url") or ""
        body.append(f"\\section{{{title}}}")
        if url:
            body.append(f"\\noindent\\url{{{url}}}\\par")
        meta = " · ".join(filter(None, [", ".join(paper.get("authors") or []), paper.get("published", ""), paper.get("venue", "")]))
        if meta:
            body.append(f"\\noindent {latex_escape(meta)}\\par")
        body.append(f"\\noindent\\textbf{{Evidence:}} \\texttt{{{latex_escape(review.get('evidence_level', 'unknown'))}}}\\par")
        for key, label in SECTIONS:
            body.append(f"\\subsection*{{{latex_escape(label)}}}")
            body.append(latex_escape(review.get(key, "").strip()))
        if review.get("limitations"):
            body.append("\\subsection*{证据与局限 / Evidence Notes}")
            body.append(latex_escape(review["limitations"].strip()))
    return r"""\documentclass[11pt,a4paper]{ctexart}
\usepackage[margin=2.25cm]{geometry}
\usepackage{amsmath,amssymb,booktabs,longtable,microtype,xurl}
\usepackage[colorlinks=true,linkcolor=blue,urlcolor=blue]{hyperref}
\sloppy
\setlength{\emergencystretch}{3em}
\Urlmuskip=0mu plus 1mu
\setlength{\parindent}{2em}
\setlength{\parskip}{0.45em}
\begin{document}
\title{Paper Digest}
\author{}
\date{\today}
\maketitle
\pagestyle{plain}
\tableofcontents
\newpage
""" + "\n\n".join(body) + "\n\\end{document}\n"


def write_outputs(run_dir: Path, records: list[dict], formats: list[str], compile_pdf: bool) -> dict[str, str]:
    outputs: dict[str, str] = {}
    run_dir.mkdir(parents=True, exist_ok=True)
    if "json" in formats:
        path = run_dir / "digest.json"
        path.write_text(json.dumps({"papers": records}, ensure_ascii=False, indent=2), encoding="utf-8")
        outputs["json"] = str(path)
    if "markdown" in formats:
        path = run_dir / "digest.md"
        path.write_text(render_markdown(records), encoding="utf-8")
        outputs["markdown"] = str(path)
    if "latex" in formats or compile_pdf:
        tex_path = run_dir / "digest.tex"
        tex_path.write_text(render_latex(records), encoding="utf-8")
        outputs["latex"] = str(tex_path)
        if compile_pdf:
            engine = shutil.which("xelatex") or shutil.which("lualatex")
            if engine:
                for _ in range(2):
                    completed = subprocess.run(
                        [engine, "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
                        cwd=run_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
                    )
                    if completed.returncode != 0:
                        (run_dir / "latex-error.log").write_text(completed.stdout + "\n" + completed.stderr, encoding="utf-8")
                        break
                pdf_path = run_dir / "digest.pdf"
                if pdf_path.exists():
                    outputs["pdf"] = str(pdf_path)
            else:
                (run_dir / "latex-warning.txt").write_text("XeLaTeX or LuaLaTeX was not found; digest.tex was generated but not compiled.\n", encoding="utf-8")
    return outputs
