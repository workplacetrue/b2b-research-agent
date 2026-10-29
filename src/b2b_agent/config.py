import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import StrictModel


class SearchConfig(StrictModel):
    provider: str = Field(default="none", pattern="^(none|brave)$")
    api_key_env: str = "BRAVE_API_KEY"
    queries: list[str] = Field(default_factory=list)
    limit: int = Field(default=5, ge=1, le=20)


class LLMConfig(StrictModel):
    enabled: bool = False
    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "B2B_LLM_API_KEY"
    model: str = "gpt-4o-mini"


class Config(StrictModel):
    dns_mode: Literal["local", "proxy"] = "local"
    user_agent: str = "B2BResearchAgent/0.1"
    max_pages: int = Field(default=5, ge=1, le=20)
    max_companies: int = Field(default=10, ge=1, le=100)
    delay_seconds: float = Field(default=2, ge=1)
    timeout_seconds: int = Field(default=30, ge=5, le=120)
    search: SearchConfig = Field(default_factory=SearchConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    niche_keywords: list[str] = Field(
        default_factory=lambda: ["упаков", "расходные материалы", "промышленная комплектация"]
    )
    sender: str = "Команда внедрения ИИ-автоматизации"


def load_config(path: str) -> Config:
    return Config.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
