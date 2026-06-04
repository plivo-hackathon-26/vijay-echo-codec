"""Pre-TTS gate runner: the flagged draft never reaches TTS.

All offline — fake chunks, fake chat context, scriptable judge, no network.
"""

from types import SimpleNamespace

from plivo_mirror_v5.deployables.intervention import JudgedPreTTSGate
from plivo_mirror_v5.engine import Engine, EngineConfig, PolicyPack, SessionState
from plivo_mirror_v5.engine.claims import LexiconClaimExtractor
from plivo_mirror_v5.integrations import PreTTSGateRunner

from helpers import REFERENCE
from test_hook_b_inline_judge import FakeJudge


def chunk(text=None, tool_calls=None):
    return SimpleNamespace(delta=SimpleNamespace(content=text, tool_calls=tool_calls))


class FakeChatCtx:
    """Duck-typed livekit ChatContext: items + copy + add_message."""

    def __init__(self, items=None):
        self.items = list(items or [])

    def copy(self):
        return FakeChatCtx(self.items)

    def add_message(self, *, role, content):
        self.items.append(SimpleNamespace(role=role, text_content=content))


def make_runner(judge=None, *, regenerated="The Turbo plan is $79.99 a month."):
    pack = PolicyPack.from_dict({"commitments": [{
        "id": "no_waiver",
        "pattern": r"\bwaiv\w+\b",
        "allowed_if": "session.auth.fee_waiver_authorized"}]})
    engine = Engine(EngineConfig(mode="intervene", policy=pack),
                    reference=REFERENCE)
    gate = JudgedPreTTSGate(engine, judge or FakeJudge())
    runner = PreTTSGateRunner(gate, SessionState("call-pt"),
                              LexiconClaimExtractor(REFERENCE))
    calls = {"n": 0}

    def default_node(ctx):
        async def stream():
            calls["n"] += 1
            if calls["n"] == 1:   # the draft
                yield chunk("DRAFT-SHOULD-NOT-LEAK")
            else:                 # regeneration
                yield chunk(regenerated)
        return stream()

    return runner, default_node, calls


async def collect(agen):
    return [x async for x in agen]


async def test_clean_draft_passes_through_original_chunks():
    runner, _, _ = make_runner()
    original = [chunk("Sure, "), chunk("happy to help with that.")]

    def default_node(ctx):
        async def stream():
            for c in original:
                yield c
        return stream()

    out = await collect(runner.gate_stream(FakeChatCtx(), default_node))
    assert out == original          # untouched objects, ~0 ms path


async def test_tool_call_stream_passes_through():
    runner, _, _ = make_runner()
    tool_chunks = [chunk(None, tool_calls=[{"name": "cancel_booking"}])]

    def default_node(ctx):
        async def stream():
            for c in tool_chunks:
                yield c
        return stream()

    out = await collect(runner.gate_stream(FakeChatCtx(), default_node))
    assert out == tool_chunks       # actions are not the speech gate's job


async def test_flagged_draft_never_reaches_tts():
    """The whole point: the violating draft is replaced by filler +
    corrected text — the original words are never yielded."""
    pack_judge = FakeJudge()        # judge clean; the L2 commitment flags
    runner, _, calls = make_runner(pack_judge)

    def default_node(ctx):
        async def stream():
            calls["n"] += 1
            if calls["n"] == 1:
                yield chunk("Done — I've waived the fee for you!")
            else:
                yield chunk("I can process the standard refund, "
                            "but I'm not able to remove the fee.")
        return stream()

    out = await collect(runner.gate_stream(FakeChatCtx(), default_node))
    text = " ".join(str(o) for o in out)
    assert "waived the fee for you" not in text          # never spoken
    assert "one moment" in text.lower()                  # filler first
    assert "standard refund" in text                     # corrected reply
    assert all(isinstance(o, str) for o in out)


async def test_judge_violation_holds_prose_draft():
    judge = FakeJudge([{"violation": True, "category": "fabricated_fact",
                        "reason": "no such promo"},
                       {"violation": False, "category": None, "reason": ""}])
    runner, default_node, _ = make_runner(
        judge, regenerated="We don't have any promotions right now.")

    def node(ctx):
        async def stream():
            if not judge.calls:      # first pass: the bad draft
                yield chunk("We have a buy-one-get-one-free promo today!")
            else:
                yield chunk("We don't have any promotions right now.")
        return stream()

    ctx = FakeChatCtx([SimpleNamespace(role="user", text_content="Any deals?")])
    out = await collect(runner.gate_stream(ctx, node))
    text = " ".join(str(o) for o in out)
    assert "buy-one-get-one" not in text
    assert "promotions right now" in text


async def test_gate_crash_releases_draft_unchanged():
    """A broken gate must never mute the agent — fail-open."""
    runner, _, _ = make_runner()
    runner.claim_extractor = None    # force an exception inside the gate
    original = [chunk("The Turbo plan is $59.99 a month.")]

    def default_node(ctx):
        async def stream():
            for c in original:
                yield c
        return stream()

    out = await collect(runner.gate_stream(FakeChatCtx(), default_node))
    assert out == original
