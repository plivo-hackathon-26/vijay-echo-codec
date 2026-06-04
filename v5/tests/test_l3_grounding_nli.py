from plivo_mirror_v5.engine import FakeKBRetriever, KBChunk, SessionState
from plivo_mirror_v5.engine.layers import GroundingNLILayer, KeywordNLI

from helpers import make_ctx, make_turn

ROUTER_CHUNK = KBChunk(
    chunk_id="chunk_02",
    text="The Aurora home router supports wifi 6 and covers up to 2500 square feet of living space.",
    score=0.9,
)


def _prose(text, claim_id="c4"):
    return {"claim_id": claim_id, "claim_type": "fact",
            "spoken_value": None, "ref": None, "text": text}


def test_keyword_nli_labels():
    nli = KeywordNLI()
    premise = ROUTER_CHUNK.text
    assert nli.score(premise, "The Aurora router covers up to 2500 square feet").label == "supported"
    assert nli.score(premise, "The Aurora router covers up to 5000 square feet").label == "contradicted"
    assert nli.score(premise, "Our routers are certified by NASA").label == "unsupported"


def test_contradicted_claim_fires_with_chunk_evidence():
    kb = FakeKBRetriever([ROUTER_CHUNK])
    state, ctx = make_ctx(kb=kb)
    turn = make_turn(claims=[_prose("The Aurora router covers up to 5000 square feet")])
    [v] = GroundingNLILayer().check(turn, state, ctx)
    assert (v.detector, v.fired, v.severity) == ("L3", True, "med")
    assert v.evidence.truth_value == ROUTER_CHUNK.text
    assert v.evidence.source == "kb#chunk_02"
    assert v.evidence.extra["nli_label"] == "contradicted"


def test_supported_claim_does_not_fire():
    kb = FakeKBRetriever([ROUTER_CHUNK])
    state, ctx = make_ctx(kb=kb)
    turn = make_turn(claims=[_prose("The Aurora router covers up to 2500 square feet")])
    [v] = GroundingNLILayer().check(turn, state, ctx)
    assert not v.fired
    assert v.evidence.extra["nli_label"] == "supported"


def test_unsupported_claim_fires_low():
    kb = FakeKBRetriever([ROUTER_CHUNK])
    state, ctx = make_ctx(kb=kb)
    turn = make_turn(claims=[_prose("Our routers are certified by NASA")])
    [v] = GroundingNLILayer().check(turn, state, ctx)
    assert v.fired and v.severity == "low"
    assert v.evidence.extra["nli_label"] == "unsupported"


def test_skips_claims_under_l2_jurisdiction():
    kb = FakeKBRetriever([ROUTER_CHUNK])
    state, ctx = make_ctx(kb=kb)
    ctx.l2_claim_ids.add("c4")
    turn = make_turn(claims=[_prose("The Aurora router covers up to 5000 square feet")])
    assert GroundingNLILayer().check(turn, state, ctx) == []


def test_no_kb_means_no_l3():
    state, ctx = make_ctx(kb=None)
    turn = make_turn(claims=[_prose("anything")])
    assert GroundingNLILayer().check(turn, state, ctx) == []


def test_untrusted_input_downgrades_to_info():
    kb = FakeKBRetriever([ROUTER_CHUNK])
    state = SessionState("call-t")
    state.mark_input_trust(False)
    state, ctx = make_ctx(state=state, kb=kb)
    turn = make_turn(claims=[_prose("The Aurora router covers up to 5000 square feet")])
    [v] = GroundingNLILayer().check(turn, state, ctx)
    assert v.fired and v.severity == "info"
    assert v.evidence.extra["untrusted_input"] is True
