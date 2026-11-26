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


def resolve_llm(llm: "LLMConfig | None") -> "LLMConfig | None":
    """Use a request LLM config, filling a missing key from the server environment."""

    from browser_platform.llm.openrouter import env_api_key

    model = llm.model if llm is not None else None
    max_tokens = llm.max_tokens if llm is not None else 800
    key = ""
    if llm is not None:
        try:
            key = (llm.key() or "").strip()
        except Exception:
            key = ""
    if not key:
        key = (env_api_key() or "").strip()
    if not key:
        return llm
    return LLMConfig(api_key=key, model=model or DEFAULT_MODEL, max_tokens=max_tokens)
