from __future__ import annotations

import re
from pathlib import Path

from .models import Paper
from .sources.common import get_bytes


CUTOFF = re.compile(
    r"^\s*(?:(?:\d+|[A-Z])\s*[.)]?\s+)?"
    r"(?:appendix|appendices|supplementary\s+(?:material|information)|"
    r"supplemental\s+(?:material|information)|references|bibliography|"
    r"acknowledg(?:e)?ments?)\b[^\n]{0,120}$",
    re.I | re.M,
)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "paper"


def download_and_extract(paper: Paper, pdf_dir: Path, max_chars: int) -> tuple[str, str, str]:
    if not paper.pdf_url:
        return paper.abstract, "abstract", "No PDF URL was available."
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / f"{safe_name(paper.id)}.pdf"
    try:
        from pypdf import PdfReader

        if not pdf_path.exists():
            pdf_path.write_bytes(get_bytes(paper.pdf_url, timeout=120, accept="application/pdf"))
        reader = PdfReader(str(pdf_path))
        chunks: list[str] = []
        total = 0
        for index, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if index >= 2:
                match = CUTOFF.search(text)
                if match:
                    text = text[: match.start()]
                    if text.strip():
                        chunks.append(text)
                    break
            chunks.append(text)
            total += len(text)
            if total >= max_chars:
                break
        fulltext = "\n\n".join(chunks).strip()[:max_chars]
        if len(fulltext) < 1500:
            return paper.abstract, "abstract", "PDF extraction returned too little reliable main text."
        return fulltext, "fulltext", ""
    except Exception as exc:
        return paper.abstract, "abstract", f"PDF retrieval/extraction failed: {type(exc).__name__}: {exc}"
