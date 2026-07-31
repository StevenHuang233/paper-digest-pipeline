from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Paper:
    id: str
    title: str
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    published: str = ""
    venue: str = ""
    categories: list[str] = field(default_factory=list)
    url: str = ""
    pdf_url: str = ""
    source: str = ""
    score: float = 0.0
    score_reasons: list[str] = field(default_factory=list)
    selection_decision: str = ""
    selection_scores: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Paper":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        clean = {k: v for k, v in value.items() if k in allowed}
        clean["authors"] = list(clean.get("authors") or [])
        clean["categories"] = list(clean.get("categories") or [])
        clean["score_reasons"] = list(clean.get("score_reasons") or [])
        clean["selection_scores"] = dict(clean.get("selection_scores") or {})
        return cls(**clean)


@dataclass(slots=True)
class SixPartReview:
    background: str
    motivation: str
    idea: str
    method: str
    experiments: str
    conclusion: str
    evidence_level: str = "fulltext"
    limitations: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)
