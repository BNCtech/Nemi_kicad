import os
from dataclasses import dataclass
from pathlib import Path

try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


@dataclass(frozen=True)
class Config:
    api_key: str = os.getenv("CLAUDE_API_KEY", "")
    model_deep: str = os.getenv("CLAUDE_MODEL_DEEP", "claude-sonnet-4-6")
    model_fast: str = os.getenv("CLAUDE_MODEL_FAST", "claude-haiku-4-5-20251001")
    max_tokens: int = int(os.getenv("CLAUDE_MAX_TOKENS", "16000"))
    enable_cache: bool = os.getenv("CLAUDE_PROMPT_CACHE", "1") == "1"


def require_api_key() -> str:
    cfg = Config()
    if not cfg.api_key:
        raise RuntimeError(
            "CLAUDE_API_KEY missing — copy .env.example to .env and set the key"
        )
    return cfg.api_key
