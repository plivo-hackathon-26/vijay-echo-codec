"""Unit tests for v0.3.0 ``Supervisor.from_env()`` auto-detection.

We exercise priority order and the override knobs (``MIRROR_TIER2``,
``MIRROR_DISABLE_TIER1``) by mutating ``os.environ`` per-test under
a ``clean_env`` fixture that wipes every relevant key before/after.

Key wiring is NOT exercised end-to-end here — the judges themselves
are unit-tested in test_scorer_tier2_*. We only verify that the right
classes get instantiated for a given env profile.
"""

from __future__ import annotations

import os

import pytest

from plivo_mirror import Supervisor


_ENV_KEYS = (
    "HF_API_KEY",
    "ATLA_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
    "OPENAI_API_KEY",
    "MIRROR_TIER2",
    "MIRROR_DISABLE_TIER1",
    "MIRROR_INTERVENTION_THRESHOLD",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield


def _classname(obj) -> str | None:
    return None if obj is None else type(obj).__name__


# ─── tier-2 priority order ──────────────────────────────────────────────


def test_no_creds_yields_none_for_both_tiers():
    sup = Supervisor.from_env(policies=["x"])
    assert sup._scorer.tier1 is None
    assert sup._scorer.tier2 is None


def test_azure_creds_pick_azure_judge(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
    sup = Supervisor.from_env(policies=["x"])
    assert _classname(sup._scorer.tier2) == "AzureOpenAIJudge"


def test_openai_creds_pick_openai_judge(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sup = Supervisor.from_env(policies=["x"])
    assert _classname(sup._scorer.tier2) == "OpenAICompatibleJudge"


def test_hf_only_picks_huggingface_judge(monkeypatch):
    monkeypatch.setenv("HF_API_KEY", "hf_test")
    sup = Supervisor.from_env(policies=["x"])
    # HF is lowest priority for tier 2 but it still gets picked when
    # nothing better is available.
    assert _classname(sup._scorer.tier2) in {
        "HuggingFaceLLMJudge",
        # tolerate future rename
    }


def test_azure_beats_openai(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    sup = Supervisor.from_env(policies=["x"])
    assert _classname(sup._scorer.tier2) == "AzureOpenAIJudge"


def test_azure_beats_hf(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
    monkeypatch.setenv("HF_API_KEY", "hf_test")
    sup = Supervisor.from_env(policies=["x"])
    assert _classname(sup._scorer.tier2) == "AzureOpenAIJudge"


# ─── override knobs ─────────────────────────────────────────────────────


def test_mirror_tier2_forces_openai(monkeypatch):
    # Azure creds are present but operator forces openai.
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("MIRROR_TIER2", "openai")
    sup = Supervisor.from_env(policies=["x"])
    assert _classname(sup._scorer.tier2) == "OpenAICompatibleJudge"


def test_mirror_tier2_forces_hf(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
    monkeypatch.setenv("HF_API_KEY", "hf_test")
    monkeypatch.setenv("MIRROR_TIER2", "hf")
    sup = Supervisor.from_env(policies=["x"])
    assert _classname(sup._scorer.tier2) == "HuggingFaceLLMJudge"


def test_mirror_tier2_none_disables_tier2_entirely(monkeypatch):
    # All creds present, but operator forces tier2 off.
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
    monkeypatch.setenv("MIRROR_TIER2", "none")
    sup = Supervisor.from_env(policies=["x"])
    assert sup._scorer.tier2 is None


# ─── tier-1 ────────────────────────────────────────────────────────────


def test_hf_key_enables_tier1(monkeypatch):
    monkeypatch.setenv("HF_API_KEY", "hf_test")
    sup = Supervisor.from_env(policies=["x"])
    assert _classname(sup._scorer.tier1) == "HuggingFaceClassifier"


def test_mirror_disable_tier1_skips_tier1_even_with_hf_key(monkeypatch):
    monkeypatch.setenv("HF_API_KEY", "hf_test")
    monkeypatch.setenv("MIRROR_DISABLE_TIER1", "1")
    sup = Supervisor.from_env(policies=["x"])
    assert sup._scorer.tier1 is None


def test_no_hf_key_no_tier1():
    sup = Supervisor.from_env(policies=["x"])
    assert sup._scorer.tier1 is None
