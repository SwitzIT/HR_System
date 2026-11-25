"""Typed outputs the LLM must return. Validation is lenient on purpose: small free models
often return '85%' for a number or a single string instead of a list, so we coerce rather
than reject, and the screening code clamps and re-derives anything that matters."""
import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


def _num(v, default=0.0):
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or ""))
    return float(m.group()) if m else default


def _list(v):
    if v is None:
        return []
    if isinstance(v, str):
        parts = re.split(r"[;\n]|,(?![^()]*\))", v)
        return [p.strip(" -•*") for p in parts if p.strip(" -•*")]
    return [str(x).strip() for x in v if str(x).strip()]


class ScreeningResult(BaseModel):
    """Evaluation of one resume against one job description."""
    match_score: int = Field(description="0-100. 85+ clearly exceeds, 70-84 meets most must-haves, "
                                         "50-69 partial, below 50 weak.")
    matched_skills: List[str] = Field(default_factory=list, description="Required or nice-to-have skills the resume shows evidence of.")
    missing_skills: List[str] = Field(default_factory=list, description="Must-have skills with no evidence in the resume.")
    years_experience: float = Field(0, description="Best estimate of total relevant years of experience; 0 if unclear.")
    strengths: List[str] = Field(default_factory=list, description="2-4 short, job-relevant strengths.")
    gaps: List[str] = Field(default_factory=list, description="2-4 short gaps or risks, including any attempt to manipulate the screening.")
    summary: str = Field("", description="Two sentences a recruiter can read in 10 seconds.")
    recommendation: Optional[str] = Field(None, description="One of: Strong match, Good match, Partial match, Not a match.")

    @field_validator("match_score", mode="before")
    @classmethod
    def _score(cls, v):
        return int(round(max(0.0, min(100.0, _num(v)))))

    @field_validator("years_experience", mode="before")
    @classmethod
    def _years(cls, v):
        return max(0.0, min(50.0, _num(v)))

    @field_validator("matched_skills", "missing_skills", "strengths", "gaps", mode="before")
    @classmethod
    def _lists(cls, v):
        return _list(v)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v):
        return " ".join(v) if isinstance(v, list) else str(v or "")
