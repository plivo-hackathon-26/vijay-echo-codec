"""Shared OpenAI-compatible chat client for the LLM-backed components
(LLM claim extractor, post-call judge). The ENGINE core never imports
this — those components are opt-in and the engine stays offline-capable.

Azure-deployment quirks (paid for in real time on this project — keep):
- ❌ ``max_tokens``        → omit entirely (or use max_completion_tokens)
- ❌ ``tool_choice="none"`` → never send ``tools`` at all
- ⚠️ ``temperature``       → silently ignored on some deployments; omit
- ✅ ``response_format={"type": "json_object"}`` is supported
"""

from __future__ import annotations

import json
import os


class ChatClient:
    """Thin sync wrapper: one system+user exchange → parsed JSON object."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        from openai import OpenAI  # lazy: only LLM-backed components pay this

        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self._client = OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY"),
            base_url=base_url
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_URL")
            or None,
        )

    def complete_json(self, system: str, user: str) -> dict:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {}
