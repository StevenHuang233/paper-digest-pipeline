from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path


SECTIONS = [
    ("background", "问题背景 / Problem Background"),
    ("motivation", "研究动机 / Motivation"),
    ("idea", "核心思想 / Core Idea"),
    ("method", "具体方法 / Method"),
    ("experiments", "实验与说明 / Experiments and Interpretation"),
    ("conclusion", "结论 / Conclusion"),
]


def sanitize_text(text: str) -> str:
    """Remove characters that cannot safely cross JSON/Markdown/LaTeX boundaries.

    A common model-output failure is an unescaped TeX command inside JSON: for
    example ``\bar`` is decoded as a backspace followed by ``ar``. Reconstruct
    the consumed command letter for backspace/form-feed, preserve ordinary
    line breaks, and discard the remaining invisible control/format codepoints.
    """
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x08", r"\b").replace("\x0c", r"\f")
    return "".join(
        char for char in text
        if char in {"\n", "\t"} or not unicodedata.category(char).startswith("C")
    )


def _sanitize_value(value):
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_value(item) for key, item in value.items()}
    return value


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
        "δ": r"\ensuremath{\delta}", "ε": r"\ensuremath{\epsilon}", "ζ": r"\ensuremath{\zeta}",
        "η": r"\ensuremath{\eta}", "θ": r"\ensuremath{\theta}", "κ": r"\ensuremath{\kappa}",
        "λ": r"\ensuremath{\lambda}", "μ": r"\ensuremath{\mu}", "ν": r"\ensuremath{\nu}",
        "ξ": r"\ensuremath{\xi}", "π": r"\ensuremath{\pi}", "ρ": r"\ensuremath{\rho}",
        "σ": r"\ensuremath{\sigma}", "τ": r"\ensuremath{\tau}", "φ": r"\ensuremath{\phi}",
        "χ": r"\ensuremath{\chi}", "ψ": r"\ensuremath{\psi}", "ω": r"\ensuremath{\omega}",
        "Δ": r"\ensuremath{\Delta}", "Σ": r"\ensuremath{\Sigma}", "Ω": r"\ensuremath{\Omega}",
        "≥": r"\ensuremath{\geq}", "≤": r"\ensuremath{\leq}",
        "∝": r"\ensuremath{\propto}", "⟨": r"\ensuremath{\langle}", "⟩": r"\ensuremath{\rangle}",
        "−": "-",
    }
    return "".join(replacements.get(char, char) for char in text)


def _latex_math(text: str) -> str:
    replacements = {
        "α": r"\alpha", "β": r"\beta", "γ": r"\gamma", "δ": r"\delta", "ε": r"\epsilon",
        "ζ": r"\zeta", "η": r"\eta", "θ": r"\theta", "κ": r"\kappa", "λ": r"\lambda",
        "μ": r"\mu", "ν": r"\nu", "ξ": r"\xi", "π": r"\pi", "ρ": r"\rho",
        "σ": r"\sigma", "τ": r"\tau", "φ": r"\phi", "χ": r"\chi", "ψ": r"\psi",
        "ω": r"\omega", "Δ": r"\Delta", "Σ": r"\Sigma", "Ω": r"\Omega",
        "≥": r"\geq", "≤": r"\leq",
        "∝": r"\propto", "⟨": r"\langle", "⟩": r"\rangle", "−": "-",
    }
    return "".join(replacements.get(char, char) for char in text)


def latex_escape(text: str, preserve_math: bool = True) -> str:
    if not preserve_math:
        return _latex_plain(text)
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


def render_latex(records: list[dict], preserve_math: bool = True) -> str:
    evidence_labels = {
        "fulltext": "全文：已读取正文至附录或参考文献前",
        "abstract": "摘要：仅依据可靠摘要",
        "metadata": "元数据：仅验证书目信息",
    }
    evidence_counts = {
        level: sum(record["review"].get("evidence_level") == level for record in records)
        for level in evidence_labels
    }
    body: list[str] = []
    for record in records:
        paper, review = record["paper"], record["review"]
        title = latex_escape(paper["title"], preserve_math=preserve_math)
        title = title.replace(r"$\beta$", r"\texorpdfstring{$\beta$}{beta}")
        url = paper.get("url") or paper.get("pdf_url") or ""
        authors = ", ".join(paper.get("authors") or [])
        published = str(paper.get("published") or "").split("T", 1)[0]
        venue = str(paper.get("venue") or "")
        categories = "; ".join(paper.get("categories") or [])
        level = str(review.get("evidence_level") or "unknown")
        info: list[str] = []
        if authors:
            info.append(f"\\textbf{{作者：}}{latex_escape(authors)}")
        publication = "；".join(filter(None, [published, venue]))
        if publication:
            info.append(f"\\textbf{{日期/发表：}}{latex_escape(publication)}")
        if categories:
            info.append(f"\\textbf{{分类标签：}}{latex_escape(categories)}")
        info.append(f"\\textbf{{证据等级：}}{latex_escape(evidence_labels.get(level, level))}")
        if url:
            info.append(f"\\textbf{{论文链接：}}\\href{{\\detokenize{{{url}}}}}{{打开论文来源}}")
        body.extend([
            "\\begin{samepage}",
            f"\\subsection{{{title}}}",
            "\\paperbox{" + r"\\".join(info) + "}",
            "\\end{samepage}",
        ])
        display_labels = {
            "background": "问题背景",
            "motivation": "Motivation｜为什么需要解决",
            "idea": "Idea｜核心思想",
            "method": "Method｜实现流程与关键公式",
            "experiments": "实验｜做了什么、说明了什么",
            "conclusion": "结论",
        }
        for key, _ in SECTIONS:
            body.append(f"\\parthead{{{latex_escape(display_labels[key])}}}")
            body.append(latex_escape(review.get(key, "").strip(), preserve_math=preserve_math))
        if review.get("limitations"):
            body.append("\\parthead{证据与局限}")
            body.append(latex_escape(review["limitations"].strip(), preserve_math=preserve_math))
    preamble = rf"""\documentclass[11pt,UTF8,a4paper]{{ctexart}}
\usepackage[margin=24mm,headheight=22pt]{{geometry}}
\usepackage{{amsmath,amssymb,booktabs,longtable,microtype,xurl}}
\usepackage{{xcolor}}
\usepackage{{fancyhdr}}
\usepackage[colorlinks=true,linkcolor=navy,urlcolor=blue]{{hyperref}}
\definecolor{{navy}}{{HTML}}{{16324F}}
\definecolor{{blue}}{{HTML}}{{246B9E}}
\definecolor{{softblue}}{{HTML}}{{EAF3F8}}
\definecolor{{graytext}}{{HTML}}{{58636D}}
\definecolor{{rulegray}}{{HTML}}{{D9E2E8}}
\hypersetup{{
  pdfauthor={{Paper Digest Pipeline}},
  pdftitle={{每日论文六段式深度总结}}
}}
\sloppy
\raggedbottom
\setlength{{\emergencystretch}}{{3em}}
\Urlmuskip=0mu plus 1mu
\setlength{{\parindent}}{{2em}}
\setlength{{\parskip}}{{0.45em}}
\makeatletter
\renewcommand\section{{\@startsection{{section}}{{1}}{{0pt}}{{2.2ex plus .5ex minus .2ex}}{{1.0ex}}{{\Large\bfseries\color{{navy}}}}}}
\renewcommand\subsection{{\@startsection{{subsection}}{{2}}{{0pt}}{{1.8ex plus .4ex minus .2ex}}{{0.7ex}}{{\large\bfseries\color{{navy}}\raggedright}}}}
\makeatother
\pagestyle{{fancy}}
\fancyhf{{}}
\fancyhead[L]{{\small\color{{graytext}}每日论文精读}}
\fancyhead[R]{{\small\color{{graytext}}六段式深度总结}}
\fancyfoot[C]{{\small\color{{graytext}}\thepage}}
\renewcommand{{\headrulewidth}}{{0.4pt}}
\renewcommand{{\headrule}}{{\hbox to\headwidth{{\color{{rulegray}}\leaders\hrule height \headrulewidth\hfill}}}}
\newcommand{{\paperbox}}[1]{{%
  \par\noindent\colorbox{{softblue}}{{%
    \parbox{{\dimexpr\linewidth-2\fboxsep\relax}}{{\small #1}}%
  }}\par
}}
\newcommand{{\parthead}}[1]{{%
  \par\pagebreak[1]\vspace{{1.0em}}%
  {{\large\bfseries\color{{blue}}#1\par}}%
  \nopagebreak[4]\vspace{{0.25em}}%
}}
\begin{{document}}
\pagenumbering{{gobble}}
\begin{{titlepage}}
  \thispagestyle{{empty}}
  \centering
  \vspace*{{2.5cm}}
  {{\Huge\bfseries\color{{navy}}每日论文精读\par}}
  \vspace{{0.45cm}}
  {{\Huge\bfseries\color{{navy}}六段式深度总结\par}}
  \vspace{{0.8cm}}
  {{\Large\color{{blue}}问题背景—Motivation—Idea—Method—实验—结论\par}}
  \vspace{{1.4cm}}
  {{\color{{rulegray}}\rule{{0.72\textwidth}}{{1pt}}}}
  \vspace{{1.4cm}}
  \begin{{minipage}}{{0.82\textwidth}}
    \raggedright\color{{graytext}}
    本册按论文正文重建因果链，不做逐句翻译。Idea 提炼决定性的概念变化；
    Method 从输入出发说明实现阶段、组件接口、训练与推理，并解释必要公式；
    实验同时回答“做了什么”和“说明了什么”。正文阅读默认截止于附录、
    补充材料、致谢或参考文献之前；未取得全文时按摘要或元数据降级。
  \end{{minipage}}
  \vfill
  {{\large 本期收录：{len(records)} 篇\par}}
  \vspace{{0.25cm}}
  {{\normalsize 全文 {evidence_counts['fulltext']}｜摘要 {evidence_counts['abstract']}｜元数据 {evidence_counts['metadata']}\par}}
  \vspace{{0.25cm}}
  {{\large 生成日期：\today\par}}
\end{{titlepage}}
\clearpage
\pagenumbering{{roman}}
\tableofcontents
\thispagestyle{{fancy}}
\clearpage
\pagenumbering{{arabic}}
\section{{论文精读}}
本期收录 {len(records)} 篇，按筛选结果顺序排列；完整分类标签保留在论文信息框中。
"""
    return preamble + "\n\n".join(body) + "\n\\end{document}\n"


def _compile_latex(engine: str, tex_path: Path, run_dir: Path) -> tuple[bool, str]:
    logs: list[str] = []
    for pass_number in range(1, 3):
        completed = subprocess.run(
            [engine, "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
            cwd=run_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )
        logs.append(f"===== LaTeX pass {pass_number} =====\n{completed.stdout}\n{completed.stderr}")
        if completed.returncode != 0:
            return False, "\n".join(logs)
    return True, "\n".join(logs)


def _clear_latex_build_files(run_dir: Path) -> None:
    for suffix in (".aux", ".out", ".toc", ".pdf"):
        (run_dir / f"digest{suffix}").unlink(missing_ok=True)


def write_outputs(run_dir: Path, records: list[dict], formats: list[str], compile_pdf: bool) -> dict[str, str]:
    outputs: dict[str, str] = {}
    run_dir.mkdir(parents=True, exist_ok=True)
    records = _sanitize_value(records)
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
            if not engine:
                warning_path = run_dir / "latex-warning.txt"
                warning_path.write_text(
                    "XeLaTeX or LuaLaTeX was not found; digest.tex could not be compiled.\n", encoding="utf-8"
                )
                raise RuntimeError(f"PDF compilation was requested but no LaTeX engine was found; see {warning_path}")

            _clear_latex_build_files(run_dir)
            success, build_log = _compile_latex(engine, tex_path, run_dir)
            (run_dir / "latex-build.log").write_text(build_log, encoding="utf-8")
            if not success:
                (run_dir / "latex-error-first-pass.log").write_text(build_log, encoding="utf-8")
                _clear_latex_build_files(run_dir)
                tex_path.write_text(render_latex(records, preserve_math=False), encoding="utf-8")
                success, recovery_log = _compile_latex(engine, tex_path, run_dir)
                (run_dir / "latex-recovery.log").write_text(recovery_log, encoding="utf-8")
                build_log = recovery_log

            pdf_path = run_dir / "digest.pdf"
            if not success or not pdf_path.is_file() or pdf_path.stat().st_size == 0:
                error_path = run_dir / "latex-error.log"
                error_path.write_text(build_log, encoding="utf-8")
                tail = " | ".join(line.strip() for line in build_log.splitlines()[-8:] if line.strip())
                raise RuntimeError(f"PDF compilation failed; see {error_path}. Last LaTeX output: {tail}")
            outputs["pdf"] = str(pdf_path)
    return outputs
