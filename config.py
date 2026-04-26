from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider: "openai" (default, OpenAI-compatible) or "anthropic" (native Messages API)
    llm_provider: str = "openai"
    openai_base_url: str = "https://openrouter.ai/api/v1"
    openai_api_key: str
    ta_model: str = "qwen/qwen3.5-397b-a17b"
    # Enable extended thinking / chain-of-thought for deeper reasoning.
    # - OpenAI-compat (Qwen3): passes enable_thinking=true in extra_body
    # - Anthropic native: enables Claude 3.7+ extended thinking mode
    enable_thinking: bool = False
    # Number of parallel LLM scoring threads
    ta_threads: int = 4

    # Confidence threshold below which a result is flagged for human review
    review_threshold: float = 0.75

    # PKU credentials for IAAA SSO
    pku_username: str = ""
    pku_password: str = ""

    # course.pku.edu.cn Blackboard course ID (e.g. "_12345_1")
    course_id: str = ""

    # Comma-separated student ID whitelist; empty = all students
    student_whitelist: str = ""

    @property
    def whitelist_ids(self) -> set[str]:
        if not self.student_whitelist.strip():
            return set()
        return {s.strip() for s in self.student_whitelist.split(",") if s.strip()}


settings = Settings()
