from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
        "business_event",
    ]
    quote: str
    source_url: str


class Contact(StrictModel):
    kind: Literal["email", "phone"]
    value: str
    source_url: str
    verified_at: datetime | None = None
    account_id: str | None = None
    author_id: str | None = None
    role: str = "unknown"
    purpose: str = "unknown"
    permission_status: Literal["allowed", "blocked", "unknown"] = "unknown"


class Hypothesis(StrictModel):
    workflow: str = "sales_quotes"
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
    research_id: str | None = None
    version: str = "1"
    evidence: list["Evidence"] = Field(default_factory=list)
    opportunity_id: str | None = None
    assessment: dict = Field(default_factory=dict)
    engagement_plan: dict = Field(default_factory=dict)
    analysis_mode: str = "rules"
    errors: list[str] = Field(default_factory=list)


# v2 models deliberately preserve the legacy research payloads above.
class FrozenModel(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AllowedClaim(FrozenModel):
    text: str = Field(min_length=1)
    source: str = Field(min_length=1)
    kind: Literal["capability_declaration", "verified_result", "team_experience"] = "capability_declaration"
    evidence_ids: tuple[str, ...] = ()


class PriceModel(FrozenModel):
    model: str = "unknown"
    currency: str | None = None
    minimum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    maximum: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    dependencies: tuple[str, ...] = ()
    source: str = "unknown"

    @model_validator(mode="after")
    def validate_range(self):
        if self.maximum is not None and self.minimum is not None and self.maximum < self.minimum:
            raise ValueError("maximum must not be less than minimum")
        if (self.minimum is not None or self.maximum is not None) and not self.currency:
            raise ValueError("currency is required for known prices")
        return self


class ServiceOffer(FrozenModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    name: str = Field(min_length=1)
    enabled: bool = True
    workflow: str = ""
    target_processes: tuple[str, ...] = ()
    target_profiles: tuple[str, ...] = ()
    supported_tasks: tuple[str, ...] = ()
    unsupported_tasks: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    allowed_claims: tuple[AllowedClaim, ...] = ()
    required_inputs: tuple[str, ...] = ()
    client_participation: tuple[str, ...] = ()
    proof_asset_ids: tuple[str, ...] = ()
    price: PriceModel = Field(default_factory=PriceModel)
    timing_constraints: tuple[str, ...] = ()
    team_availability: Literal["available", "unavailable", "unknown"] = "unknown"
    first_steps: tuple[str, ...] = ("проверка применимости",)


class ProofAsset(FrozenModel):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    type: Literal["client_case", "team_experience", "demo", "prototype", "pilot", "reference"]
    service_ids: tuple[str, ...] = ()
    processes: tuple[str, ...] = ()
    industry: str = "unknown"
    scale: str = "unknown"
    company_role: str = "unknown"
    confirmed_results: tuple[AllowedClaim, ...] = ()
    availability: Literal["available", "unavailable", "unknown"] = "unknown"
    publication_allowed: bool = False
    attachment_allowed: bool = False
    nda_restrictions: tuple[str, ...] = ()
    allowed_summary: str = ""
    path: str | None = None
    url: str | None = None
    verified_at: datetime | None = None
    source: str = "unknown"


class SenderProfile(FrozenModel):
    schema_version: Literal[1] = 1
    name: str = ""
    brand: str = ""
    role: str = "unknown"
    from_email: str = ""
    signature: str = ""
    company_facts: tuple[AllowedClaim, ...] = ()
    public_links: tuple[str, ...] = ()
    meeting_owner: str = ""
    source: str = "owner_input"

    @field_validator("from_email")
    @classmethod
    def mailbox(cls, value):
        import re
        if value and not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", value):
            raise ValueError("literal mailbox required")
        return value


class Evidence(StrictModel):
    id: str
    research_version: str
    kind: Literal["observation", "inference", "public_need", "conversation_confirmation", "contradiction"] = "observation"
    quote: str
    source_url: str | None = None
    inbound_message_id: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_at: datetime | None = None
    event_at: datetime | None = None
    account_id: str | None = None
    author_id: str | None = None
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    contradictions: list[str] = Field(default_factory=list)


class NeedAssessment(StrictModel):
    status: Literal["hypothesized", "publicly_stated", "confirmed_in_conversation", "disproved", "unknown"] = "unknown"
    description: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None


class AssessmentDimension(StrictModel):
    value: str = "unknown"
    reasons: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class OpportunityAssessment(StrictModel):
    account_fit: AssessmentDimension = Field(default_factory=AssessmentDimension)
    task_fit: AssessmentDimension = Field(default_factory=AssessmentDimension)
    potential_value: AssessmentDimension = Field(default_factory=AssessmentDimension)
    timing: AssessmentDimension = Field(default_factory=AssessmentDimension)
    evidence_confidence: AssessmentDimension = Field(default_factory=AssessmentDimension)
    contactability: AssessmentDimension = Field(default_factory=AssessmentDimension)


class Opportunity(StrictModel):
    id: str
    campaign_id: str = "default"
    profile_id: str = "default"
    origin: Literal["public_request", "company_research"]
    account_id: str | None = None
    company_name: str | None = None
    request_id: str | None = None
    task: str = "unknown"
    service_id: str | None = None
    status: Literal["candidate", "researched", "ready_for_contact", "deferred", "disqualified"] = "candidate"
    assessment: OpportunityAssessment = Field(default_factory=OpportunityAssessment)
    need: NeedAssessment = Field(default_factory=NeedAssessment)
    unknowns: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ContactPermission(StrictModel):
    status: Literal["allowed", "blocked", "unknown"] = "unknown"
    scope: str = "contact"
    basis: str = "unknown"
    source: str = "unknown"
    policy_version: str = "1"
    expires_at: datetime | None = None
    conditions: list[str] = Field(default_factory=list)


class EngagementPlan(StrictModel):
    opportunity_id: str = ""
    service_id: str = ""
    workflow: str = ""
    occasion: str = ""
    fact: str = ""
    hypothesis: str = ""
    recipient_role: str = "unknown"
    role_reason: str = ""
    customer_benefit: str = ""
    question: str | None = None
    meeting_goal: str | None = None
    recommended_action: str = "research_more"
    reasons: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


ContactPlan = EngagementPlan


class DraftClaim(StrictModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    offer_id: str | None = None
    proof_asset_id: str | None = None
    kind: str = "capability_declaration"


class MessageDraft(StrictModel):
    id: str
    version: int = 1
    context_version: int = 1
    stats_version: int = 0
    catalog_version: str = ""
    dependency_version: str = ""
    opportunity_id: str = ""
    conversation_id: str = ""
    recipient: str
    channel: str = "email"
    sender_account: str = ""
    kind: str = "initial"
    goal: str = ""
    subject: str = ""
    body: str
    attachments: list[str] = Field(default_factory=list)
    claims: list[DraftClaim] = Field(default_factory=list)
    validation_results: list[str] = Field(default_factory=list)
    allowed_delivery_method: str = "pending_approval"
    content_validation: dict = Field(default_factory=dict)


class Conversation(StrictModel):
    id: str
    opportunity_id: str
    mailbox_id: str = "default"
    participants: list[str] = Field(default_factory=list)
    provider_thread_id: str | None = None
    stage: str = "new"
    holds: list[str] = Field(default_factory=list)
    last_inbound_id: str | None = None
    last_outbound_id: str | None = None
    context_version: int = 1
    next_action: str = "no_action"
    is_terminal: bool = False
    closed_at: datetime | None = None
    closed_reason: str | None = None
    closed_by: str | None = None
    closure_event_id: str | None = None


class Decision(StrictModel):
    id: str
    context_version: int = 1
    stats_version: int = 0
    action: str
    reasons: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    rule_version: str = "2.1"
    prompt_version: str = "1"
    config_version: str = ""


class ScheduledAction(StrictModel):
    id: str
    conversation_id: str
    kind: str
    due_at: datetime | None = None
    context_version: int = 1
    reason: str = ""
    approval_id: str | None = None
    status: Literal["pending", "cancelled", "executed"] = "pending"


class MeetingReadiness(StrictModel):
    ready: bool = False
    reasons: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    goal: str = ""
    duration_minutes: int = 25
    participants: list[str] = Field(default_factory=list)


class MeetingBrief(StrictModel):
    conversation_id: str
    opportunity_id: str = ""
    company: str = "unknown"
    participants: list[str] = Field(default_factory=list)
    origin: str = "unknown"
    occasion: str = ""
    customer_task: str = "unknown"
    message_references: list[str] = Field(default_factory=list)
    confirmed: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    service_id: str = ""
    service_boundaries: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    promises: list[str] = Field(default_factory=list)
    useful_outcome: str = ""
    restrictions: list[str] = Field(default_factory=list)


Research.model_rebuild()
