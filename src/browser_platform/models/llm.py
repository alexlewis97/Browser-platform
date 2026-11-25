"""Per-request LLM configuration. The key must never be stored."""

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from browser_platform import DEFAULT_MODEL


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: SecretStr
    model: str = DEFAULT_MODEL
    max_tokens: int = Field(default=800, ge=32, le=4000)
    max_steps: int = Field(default=5, ge=1, le=20)

    def key(self) -> str:
        return self.api_key.get_secret_value()
