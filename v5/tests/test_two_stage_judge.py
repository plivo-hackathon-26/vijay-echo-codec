"""TwoStageJudge — self-consistency votes on the fast judge, escalate
splits to the strong judge. All offline (scripted fakes, no network)."""

import os
from unittest import mock

from plivo_mirror_v5.auditor import (
    AuditFinding,
    LLMPostCallJudge,
    TwoStageJudge,
    judge_from_env,
)

TURNS = [{"role": "user", "text": "how much is turbo?"},
         {"role": "agent", "text": "The Turbo plan is $59.99 a month."}]


class ScriptedJudge:
    """Returns the next scripted verdict per call; counts invocations."""

    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = 0

    def judge_turn(self, turns, agent_turn_index):
        self.calls += 1
        v = self.verdicts.pop(0) if self.verdicts else self.verdicts_default()
        return dict(v)

    @staticmethod
    def verdicts_default():
        return {"violation": False, "category": None, "reason": "clean"}


VIOLATION = {"violation": True, "category": "fabricated_fact", "reason": "wrong price"}
CLEAN = {"violation": False, "category": None, "reason": "clean"}


def test_unanimous_clean_never_pays_the_strong_model():
    fast, strong = ScriptedJudge([CLEAN] * 3), ScriptedJudge([VIOLATION])
    judge = TwoStageJudge(fast, strong, votes=3)
    verdict = judge.judge_turn(TURNS, 1)
    assert verdict["violation"] is False
    assert verdict["stage"] == "fast"
    assert verdict["votes"] == "0/3"
    assert fast.calls == 3
    assert strong.calls == 0


def test_unanimous_violation_stands_without_escalation():
    fast, strong = ScriptedJudge([VIOLATION] * 3), ScriptedJudge([CLEAN])
    judge = TwoStageJudge(fast, strong, votes=3)
    verdict = judge.judge_turn(TURNS, 1)
    assert verdict["violation"] is True
    assert verdict["category"] == "fabricated_fact"
    assert verdict["stage"] == "fast"
    assert strong.calls == 0


def test_split_vote_escalates_exactly_once_and_strong_wins():
    fast = ScriptedJudge([VIOLATION, CLEAN, CLEAN])
    strong = ScriptedJudge([VIOLATION])
    judge = TwoStageJudge(fast, strong, votes=3)
    verdict = judge.judge_turn(TURNS, 1)
    assert verdict["violation"] is True
    assert verdict["stage"] == "strong"
    assert verdict["votes"] == "1/3"
    assert fast.calls == 3
    assert strong.calls == 1


def test_audit_call_shape_matches_single_judge():
    """TwoStageJudge audits a stored call through the same loop as the
    single judge — a missed violation becomes a missed_failure finding."""
    fast = ScriptedJudge([VIOLATION] * 3)
    judge = TwoStageJudge(fast, ScriptedJudge([]), votes=3)
    call = {"call_id": "c1", "turns": [
        {"role": "agent", "turn_id": "c1-t0",
         "transcript": "The Turbo plan is $59.99.", "verdicts": []},
    ]}
    [finding] = judge.audit_call(call)
    assert isinstance(finding, AuditFinding)
    assert finding.kind == "missed_failure"
    assert finding.extra["category"] == "fabricated_fact"


def test_judge_from_env_defaults_to_single_judge():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MIRROR_JUDGE", None)
        judge = judge_from_env(facts={"k": "v"})
    assert isinstance(judge, LLMPostCallJudge)


def test_judge_from_env_two_stage(monkeypatch):
    monkeypatch.setenv("MIRROR_JUDGE", "two_stage")
    monkeypatch.setenv("MIRROR_JUDGE_VOTES", "5")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # ChatClient constructor only
    judge = judge_from_env(facts={"k": "v"}, policies=["p"])
    assert isinstance(judge, TwoStageJudge)
    assert judge.votes == 5
    assert isinstance(judge.fast, LLMPostCallJudge)
    assert isinstance(judge.strong, LLMPostCallJudge)
    # grounding propagated to both stages
    assert judge.fast.facts == {"k": "v"}
    assert judge.strong.policies == ["p"]
