from plivo_mirror_v5.engine.arbitration import arbitrate
from plivo_mirror_v5.engine.verdict import Evidence, Verdict, new_verdict_id


def _verdict(detector, claim_id, fired=True, severity="med"):
    return Verdict(
        verdict_id=new_verdict_id(),
        detector=detector,
        fired=fired,
        severity=severity,
        latency_ms=0.1,
        evidence=Evidence(
            claim_type="fact", spoken_value="x", truth_value="y",
            source="test", extra={"claim_id": claim_id},
        ),
    )


def test_l2_suppresses_l3_on_same_claim():
    l2 = _verdict("L2", "c1", severity="high")
    l3 = _verdict("L3", "c1")
    arbitrate([l2, l3])
    assert l2.fired and not l2.suppressed_by
    assert not l3.fired
    assert l3.suppressed_by == ["L2"]
    assert l3.evidence.extra["fired_before_suppression"] is True


def test_l2_jurisdiction_suppresses_even_when_l2_passes():
    l2 = _verdict("L2", "c1", fired=False, severity="info")  # checked, clean
    l3 = _verdict("L3", "c1")
    arbitrate([l2, l3])
    assert not l3.fired
    assert l3.suppressed_by == ["L2"]


def test_distinct_claims_unaffected():
    l2 = _verdict("L2", "c1")
    l3 = _verdict("L3", "c2")
    arbitrate([l2, l3])
    assert l2.fired and l3.fired
    assert not l2.suppressed_by and not l3.suppressed_by


def test_never_two_firing_verdicts_per_claim():
    verdicts = [_verdict("L2", "c1"), _verdict("L3", "c1"), _verdict("L3", "c2")]
    arbitrate(verdicts)
    firing_by_claim: dict[str, int] = {}
    for v in verdicts:
        if v.fired:
            firing_by_claim[v.claim_id] = firing_by_claim.get(v.claim_id, 0) + 1
    assert all(count == 1 for count in firing_by_claim.values())


def test_l1_turn_level_verdicts_pass_through():
    l1 = Verdict(verdict_id=new_verdict_id(), detector="L1", fired=True,
                 severity="info", latency_ms=0.0, evidence=None)
    arbitrate([l1, _verdict("L2", "c1")])
    assert l1.fired and not l1.suppressed_by
