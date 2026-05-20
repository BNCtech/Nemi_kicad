import time
from typing import Optional

import anthropic

from .config import Config, require_api_key


class ClaudeClient:
    def __init__(self, model: Optional[str] = None):
        self.cfg = Config()
        require_api_key()
        self.client = anthropic.Anthropic(api_key=self.cfg.api_key)
        self.model = model or self.cfg.model_deep

    def ask(self, system: str, user: str, max_tokens: Optional[int] = None) -> str:
        max_tokens = max_tokens or self.cfg.max_tokens
        system_arg = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if self.cfg.enable_cache
            else system
        )
        last_err = None
        for attempt in range(3):
            try:
                # Stream required: max_tokens of 16k+ exceeds the SDK's
                # non-streaming 10-minute ceiling. get_final_message() yields
                # the same Message a .create() call would have returned.
                with self.client.messages.stream(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system_arg,
                    messages=[{"role": "user", "content": user}],
                ) as stream:
                    resp = stream.get_final_message()
                return resp.content[0].text
            except anthropic.RateLimitError as e:
                last_err = e
                time.sleep(2 ** attempt)
            except anthropic.APIError as e:
                last_err = e
                if attempt == 2:
                    raise
                time.sleep(2)
        raise RuntimeError(f"Claude API: exhausted retries ({last_err})")
