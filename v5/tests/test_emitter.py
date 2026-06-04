from plivo_mirror_v5.engine.verdict import Action, Evidence, TurnResult, Verdict, new_verdict_id
from plivo_mirror_v5.telemetry import InMemorySink, TelemetryEmitter
from plivo_mirror_v5.telemetry import schema as S


def make_result(fired=True, action=None):
    verdict = Verdict(
        verdict_id=new_verdict_id(), detector="L2", fired=fired,
        severity="high" if fired else "info", latency_ms=0.02,
        evidence=Evidence(claim_type="price", spoken_value="$59.99",
                          truth_value="79.99",
                          source="reference.plan.turbo.price_per_month",
                          extra={"claim_id": "c1"}),
    )
    return TurnResult(
        turn_id="room-1-t1", call_id="room-1", turn_index=1, role="agent",
        transcript="The Turbo plan is $59.99 a month.", asr_confidence=None,
        state_snapshot_id="snap-room-1-1", verdicts=[verdict], action=action,
    )


def test_turn_span_emits_turn_verdict_action_and_metrics():
    sink = InMemorySink()
    emitter = TelemetryEmitter(sink)
    emitter.start_call("room-1", agent_id="aurora", agent_version="1.0.0")
    emitter.turn_span(make_result(action=Action(taken="would_have")),
                      audio_offset_ms=4000)
    emitter.end_call("room-1")

    assert len(sink.of_type(S.REC_CALL_START)) == 1
    [turn] = sink.of_type(S.REC_TURN)
    assert turn[S.ATTR_CALL_ID] == "room-1"
    assert turn[S.ATTR_AUDIO_OFFSET_MS] == 4000
    assert turn[S.ATTR_STATE_SNAPSHOT_ID] == "snap-room-1-1"

    [verdict] = sink.of_type(S.REC_VERDICT)
    assert verdict[S.ATTR_DETECTOR] == "L2"
    assert verdict[S.ATTR_EVIDENCE]["spoken_value"] == "$59.99"
    assert verdict[S.ATTR_ARBITRATION] == {"won": True, "suppressed": []}

    [action] = sink.of_type(S.REC_ACTION)
    assert action[S.ATTR_ACTION_TAKEN] == "would_have"

    metrics = {(m["name"], m["kind"]) for m in sink.of_type(S.REC_METRIC)}
    assert (S.METRIC_FLAGS_TOTAL, "counter") in metrics
    assert (S.METRIC_DETECTOR_LATENCY_MS, "histogram") in metrics
    assert (S.METRIC_INTERVENTION_TOTAL, "counter") in metrics

    [flags] = [m for m in sink.of_type(S.REC_METRIC) if m["name"] == S.METRIC_FLAGS_TOTAL]
    assert flags["labels"] == {"layer": "L2", "severity": "high", "agent_id": "aurora"}


def test_non_firing_verdict_emits_latency_but_no_flag_counter():
    sink = InMemorySink()
    emitter = TelemetryEmitter(sink)
    emitter.start_call("room-1")
    emitter.turn_span(make_result(fired=False))
    names = [m["name"] for m in sink.of_type(S.REC_METRIC)]
    assert S.METRIC_FLAGS_TOTAL not in names
    assert S.METRIC_DETECTOR_LATENCY_MS in names


def test_redaction():
    sink = InMemorySink()
    emitter = TelemetryEmitter(
        sink, redact_fields={S.ATTR_TRANSCRIPT, "evidence.spoken_value"})
    emitter.start_call("room-1")
    emitter.turn_span(make_result())
    [turn] = sink.of_type(S.REC_TURN)
    assert turn[S.ATTR_TRANSCRIPT] == "[REDACTED]"
    [verdict] = sink.of_type(S.REC_VERDICT)
    assert verdict[S.ATTR_EVIDENCE]["spoken_value"] == "[REDACTED]"
    assert verdict[S.ATTR_EVIDENCE]["truth_value"] == "79.99"  # untouched
