import json
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .models import StrictModel


class SearchConfig(StrictModel):
    provider: str = Field(default="none", pattern="^(none|brave)$")
    api_key_env: str = "BRAVE_API_KEY"
    queries: list[str] = Field(default_factory=list)
    request_queries: list[str] = Field(
        default_factory=lambda: [
            '"ищем подрядчика" "автоматизация продаж"',
            '"нужна автоматизация" "обработка заявок"',
            '"требуется внедрение" "CRM"',
            '"ищем исполнителя" "автоматизация документов"',
        ]
    )
    limit: int = Field(default=5, ge=1, le=20)


class LLMConfig(StrictModel):
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "B2B_LLM_API_KEY"
    model: str = "gpt-4o-mini"


class OutreachConfig(StrictModel):
    enabled: bool = False
    provider: Literal["gmail", "smtp"] = "gmail"
    from_email: str = ""
    reply_to: str = ""
    signature: str = ""
    gmail_client_id_env: str = "GMAIL_CLIENT_ID"
    gmail_client_secret_env: str = "GMAIL_CLIENT_SECRET"
    gmail_refresh_token_env: str = "GMAIL_REFRESH_TOKEN"
    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_security: Literal["ssl", "starttls"] = "ssl"
    smtp_user_env: str = "OUTREACH_SMTP_USER"
    smtp_password_env: str = "OUTREACH_SMTP_PASSWORD"
    max_per_run: int = Field(default=5, ge=1, le=50)
    max_per_day: int = Field(default=10, ge=1, le=100)
    recipient_cooldown_days: int = Field(default=30, ge=1, le=365)
    dialogue_max_per_hour: int = Field(default=5, ge=1, le=100)
    dialogue_min_interval_seconds: int = Field(default=0, ge=0, le=86400)
    max_ad_age_days: int = Field(default=30, ge=1, le=90)
    timezone: str = "Europe/Moscow"
    workflows: list[str] = Field(
        default_factory=lambda: [
            "sales_intake",
            "sales_quotes",
            "sales_followup",
            "documents",
        ]
    )

    @field_validator("from_email", "reply_to")
    @classmethod
    def validate_mailbox(cls, value: str) -> str:
        import re

        if value and not re.fullmatch(
            r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value
        ):
            raise ValueError("Use a literal mailbox without a display name or header characters")
        return value

    @field_validator("smtp_host")
    @classmethod
    def validate_smtp_host(cls, value: str) -> str:
        import re

        if value and not re.fullmatch(r"[A-Za-z0-9.-]+", value):
            raise ValueError("SMTP host must be a hostname")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
        return value


class InboxConfig(StrictModel):
    enabled: bool = False
    provider: Literal["gmail", "import"] = "import"
    mailbox_id: str = "default"
    freshness_minutes: int = Field(default=15, ge=1)
    query: str = "label:b2b-agent"
    max_messages: int = Field(default=100, ge=1, le=500)
    max_pages: int = Field(default=10, ge=1, le=100)


class ContactPolicyConfig(StrictModel):
    version: str = "1"
    cold_contact_allowed: bool = False
    require_explicit_basis: bool = True
    market: str = "unknown"
    provider_policy: str = "unknown"
    contact_hours_start: int | None = Field(default=None, ge=0, le=23)
    contact_hours_end: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = None


class BudgetConfig(StrictModel):
    currency: str = "USD"
    max_run_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_campaign_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_company_cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_llm_calls: int = Field(default=10, ge=0)
    max_research_steps: int = Field(default=20, ge=0)
    max_drafts_per_conversation: int = Field(default=10, ge=0)
    max_tokens: int = Field(default=20000, ge=0)
    tool_prices: dict[str, dict] = Field(default_factory=dict)


class MemoryConfig(StrictModel):
    input_tokens: int = Field(default=4000, ge=100)
    response_reserve_tokens: int = Field(default=1000, ge=0)
    context_window: int = Field(default=16000, ge=100)
    capsule_tokens: int = Field(default=700, ge=50)
    stats_tokens: int = Field(default=150, ge=20)
    offer_tokens: int = Field(default=350, ge=20)
    new_text_tokens: int = Field(default=600, ge=20)


class MeetingConfig(StrictModel):
    duration_minutes: int = Field(default=25, ge=5, le=180)
    timezone: str = "Europe/Moscow"
    slots: list[dict] = Field(default_factory=list)


class CapacityConfig(StrictModel):
    availability: Literal["available", "unavailable", "unknown"] = "unknown"
    max_active_opportunities: int | None = Field(default=None, ge=0)
    meeting_slots: list[dict] = Field(default_factory=list)
    owner_minutes_available: int | None = Field(default=None, ge=0)


class UnsubscribeConfig(StrictModel):
    mode: Literal["reply", "mailto", "https"] = "reply"
    mailbox: str = ""
    public_base_url: str = ""
    public_endpoint_verified: bool = False
    dkim_headers_verified: bool = False
    sync_required: bool = False
    sync_url: str = ""
    signing_secret_env: str = "B2B_UNSUBSCRIBE_SECRET"
    sync_secret_env: str = "B2B_UNSUBSCRIBE_SYNC_SECRET"


class Config(StrictModel):
    schema_version: Literal[1, 2] = 2
    input_path: str = "INPUT"
    approval_ttl_hours: int = Field(default=24, ge=1, le=720)
    inbox: InboxConfig = Field(default_factory=InboxConfig)
    policy: ContactPolicyConfig = Field(default_factory=ContactPolicyConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    meetings: MeetingConfig = Field(default_factory=MeetingConfig)
    capacity: CapacityConfig = Field(default_factory=CapacityConfig)
    unsubscribe: UnsubscribeConfig = Field(default_factory=UnsubscribeConfig)
    dns_mode: Literal["local", "proxy"] = "local"
    user_agent: str = "B2BResearchAgent/0.1"
    max_pages: int = Field(default=5, ge=1, le=20)
    max_request_pages: int = Field(default=2, ge=1, le=5)
    max_companies: int = Field(default=10, ge=1, le=100)
    delay_seconds: float = Field(default=2, ge=1)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    search: SearchConfig = Field(default_factory=SearchConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    outreach: OutreachConfig = Field(default_factory=OutreachConfig)
    niche_keywords: list[str] = Field(
        default_factory=lambda: ["упаков", "расходные материалы", "промышленная комплектация"]
    )
    sender: str = "Команда внедрения ИИ-автоматизации"


def load_config(path: str) -> Config:
    return Config.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
