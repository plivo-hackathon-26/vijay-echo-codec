"""Phase 4 — LiveKit adapter smoke test. Skipped unless livekit-agents is
installed; the core package never imports this adapter."""

from __future__ import annotations

import pytest

pytest.importorskip("livekit.agents")


def test_supervised_agent_importable_and_subclassable():
    from plivo_mirror.adapters.livekit import SupervisedAgent

    assert isinstance(SupervisedAgent, type)
    # it really is a livekit Agent subclass
    from livekit.agents import Agent

    assert issubclass(SupervisedAgent, Agent)
