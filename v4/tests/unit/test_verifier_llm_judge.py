"""Phase 2 — default LLM-judge verifier (OpenAI-compatible client mocked).

Asserts JSON parsing, fail-open on bad responses, and that the call obeys
the Azure quirks (max_completion_tokens, json_object, no max_tokens/
tool_choice)."""

from __future__ import annotations

import json

from plivo_mirror.verifier.base import GroundingEvidence
from plivo_mirror.verifier.llm_judge import LLMJudgeVerifier


class _Msg:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})


class _Resp:
    def __init__(self, content):
        self.choices = [_Msg(content)]


class FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        if isinstance(self._content, Exception):
            raise self._content
        return _Resp(self._content)


class FakeClient:
    def __init__(self, content):
        self.chat = type("C", (), {"completions": FakeCompletions(content)})()


def _ev():
    return GroundingEvidence(reply="That'll be $12.50.", flagged_spans=["$12.50"])


async def test_parses_unsupported():
    client = FakeClient(json.dumps({"supported": False, "policy_id": "no_price", "reason": "x"}))
    vf = LLMJudgeVerifier(client, model="gpt-5-mini")
    r = await vf.verify("That'll be $12.50.", _ev())
    assert r.supported is False
    assert r.policy_id == "no_price"


async def test_parses_supported():
    client = FakeClient(json.dumps({"supported": True, "policy_id": None, "reason": ""}))
    vf = LLMJudgeVerifier(client, model="gpt-5-mini")
    r = await vf.verify("ok", _ev())
    assert r.supported is True
    assert r.policy_id is None


async def test_obeys_azure_quirks_in_call():
    client = FakeClient(json.dumps({"supported": True}))
    vf = LLMJudgeVerifier(client, model="gpt-5-mini", max_completion_tokens=222)
    await vf.verify("ok", _ev())
    kw = client.chat.completions.last_kwargs
    assert kw["max_completion_tokens"] == 222
    assert kw["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in kw
    assert "tool_choice" not in kw


async def test_bad_json_fails_open():
    client = FakeClient("not json at all")
    vf = LLMJudgeVerifier(client, model="gpt-5-mini")
    r = await vf.verify("ok", _ev())
    assert r.supported is True
    assert r.reason == "verifier_error"


async def test_client_exception_fails_open():
    client = FakeClient(RuntimeError("500"))
    vf = LLMJudgeVerifier(client, model="gpt-5-mini")
    r = await vf.verify("ok", _ev())
    assert r.supported is True
