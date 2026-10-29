from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(StrictModel):
    id: str
    kind: Literal["niche", "b2b", "catalog", "order", "contact"]
    quote: str
    source_url: str


class Contact(StrictModel):
    kind: Literal["email", "phone"]
    value: str
    source_url: str


class Hypothesis(StrictModel):
    statement: str
    evidence_ids: list[str] = Field(min_length=1)
    validation_question: str
    confidence: Literal["low", "medium"] = "low"


class Analysis(StrictModel):
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=5)


class Score(StrictModel):
    icp: int = Field(default=0, ge=0, le=100)
    automation: int = Field(default=0, ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(
        default_factory=lambda: ["Размер компании неизвестен", "Объём ручной работы не подтверждён"]
    )


class Research(StrictModel):
    domain: str
    website: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Literal["ok", "partial", "failed"] = "failed"
    pages: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    contacts: list[Contact] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    score: Score = Field(default_factory=Score)
    draft: str | None = None
    analysis_mode: str = "rules"
    errors: list[str] = Field(default_factory=list)
