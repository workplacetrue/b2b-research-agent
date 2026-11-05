from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(StrictModel):
    id: str
    kind: Literal[
        "niche",
        "b2b",
        "catalog",
        "order",
        "contact",
        "documents",
        "crm",
        "repeat_sales",
        "buyer_request",
    ]
    quote: str
    source_url: str


class Contact(StrictModel):
    kind: Literal["email", "phone"]
    value: str
    source_url: str


class Hypothesis(StrictModel):
    workflow: Literal["sales_intake", "sales_quotes", "sales_followup", "documents"] = (
        "sales_quotes"
    )
    statement: str
    evidence_ids: list[str] = Field(min_length=1)
    validation_question: str
    confidence: Literal["low", "medium"] = "low"


class Analysis(StrictModel):
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=5)


class Score(StrictModel):
    icp: int = Field(default=0, ge=0, le=100)
    automation: int = Field(default=0, ge=0, le=100)
    sales: int = Field(default=0, ge=0, le=100)
    documents: int = Field(default=0, ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(
        default_factory=lambda: [
            "Размер компании неизвестен",
            "Объём ручной работы не подтверждён",
            "Интерес к покупке, бюджет и сроки не подтверждены",
        ]
    )


class Qualification(StrictModel):
    interest: Literal["unconfirmed", "public_request_to_verify"] = "unconfirmed"
    request_evidence_ids: list[str] = Field(default_factory=list)
    next_step: Literal["skip", "check_relevance", "verify_request"] = "skip"
    reasons: list[str] = Field(default_factory=list)


class RequestCandidate(StrictModel):
    source_url: str
    query: str
    title: str = ""
    snippet: str = ""
    status: Literal["unverified_search_hit"] = "unverified_search_hit"


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
    qualification: Qualification = Field(default_factory=Qualification)
    draft: str | None = None
    analysis_mode: str = "rules"
    errors: list[str] = Field(default_factory=list)
