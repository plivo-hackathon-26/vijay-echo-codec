"""Single-LLM configuration: the verifier defaults to the agent's model,
runs as a SEPARATE stateless entailment call, and can be overridden to a
different model. LLM client fully mocked — no live calls."""

from __future__ import annotations

import json

from plivo_mirror.firewall import Firewall
from plivo_mirror.verifier.base import GroundingEvidence


class _Completions:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return type(
            "R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": self._content})})]}
        )


class FakeClient:
    def __init__(self, content=json.dumps({"supported": True})):
        self.chat = type("Chat", (), {"completions": _Completions(content)})()


def test_verifier_defaults_to_agent_model():
    fw = Firewall.from_env(policies=[], model="gpt-5-mini", client=FakeClient())
    assert fw.verifier is not None
    # single-LLM: the verifier runs on the SAME model configured for the agent
    assert fw.verifier.model == "gpt-5-mini"


def test_verifier_model_can_be_overridden():
    fw = Firewall.from_env(
        policies=[], model="gpt-5-mini", verifier_model="independent-judge", client=FakeClient()
    )
    # escape hatch: agent stays on gpt-5-mini, verifier points elsewhere
    assert fw.verifier.model == "independent-judge"


def test_custom_verifier_instance_wins():
    class Custom:
        model = "custom"

        async def verify(self, claim, evidence):
            ...

    fw = Firewall.from_env(policies=[], model="gpt-5-mini", verifier=Custom())
    assert isinstance(fw.verifier, Custom)


async def test_verifier_is_separate_stateless_entailment_call():
    client = FakeClient(json.dumps({"supported": True}))
    fw = Firewall.from_env(policies=[], model="gpt-5-mini", client=client)
    await fw.verifier.verify(
        "That'll be $5.",
        GroundingEvidence(
            reply="That'll be $5.",
            flagged_spans=["$5"],
            facts={"items": "turkey sub"},
            policies=[{"id": "p1", "text": "Never invent a price."}],
        ),
    )
    msgs = client.chat.completions.last_kwargs["messages"]
    # exactly a fresh 2-message entailment call: system judge + user evidence
    assert len(msgs) == 2
    assert msgs[0]["role"] == "system"
    assert "grounding verifier" in msgs[0]["content"].lower()
    # it judges reply-vs-state, NOT acting as the agent persona / chat history
    assert msgs[1]["role"] == "user"
    assert "FACTS" in msgs[1]["content"]
    assert "turkey sub" in msgs[1]["content"]
    assert "CLAIM" in msgs[1]["content"]


def test_no_client_no_creds_yields_no_verifier(monkeypatch):
    for k in (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_DEPLOYMENT",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)
    fw = Firewall.from_env(policies=[], model="gpt-5-mini")
    assert fw.verifier is None  # speech guard then fails open on risky spans
