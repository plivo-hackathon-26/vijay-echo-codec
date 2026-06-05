import threading

from plivo_mirror_v5.engine.verdict import Action, Evidence, TurnResult, Verdict, new_verdict_id
from plivo_mirror_v5.telemetry import InMemorySink, TelemetryEmitter, ThreadedSink
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


class FlakySink:
    """Inner sink that fails the first ``fail_first`` emits, then succeeds.
    ``gate`` (when set) blocks every emit until released — lets a test fill
    the queue deterministically without sleeps."""

    def __init__(self, fail_first: int = 0, gate: threading.Event | None = None):
        self.fail_first = fail_first
        self.gate = gate
        self.records: list[dict] = []

    def emit(self, record: dict) -> None:
        if self.gate is not None:
            self.gate.wait(timeout=5)
        if self.fail_first > 0:
            self.fail_first -= 1
            raise ConnectionError("backend down")
        self.records.append(record)


def test_threaded_sink_delivers_in_order():
    inner = FlakySink()
    sink = ThreadedSink(inner, maxsize=100)
    for i in range(10):
        sink.emit({"i": i})
    sink.close()
    assert [r["i"] for r in inner.records] == list(range(10))
    assert sink.dropped == 0


def test_threaded_sink_bounded_queue_drops_oldest():
    gate = threading.Event()
    inner = FlakySink(gate=gate)
    sink = ThreadedSink(inner, maxsize=3)
    # The drain thread blocks on the gate holding record 0; fill past the cap.
    for i in range(8):
        sink.emit({"i": i})
    assert sink.dropped > 0
    gate.set()
    sink.close()
    # Newest records survive (drop-oldest); nothing duplicated.
    delivered = [r["i"] for r in inner.records]
    assert delivered[-1] == 7
    assert delivered == sorted(delivered)
    assert len(delivered) + sink.dropped == 8


def test_threaded_sink_spool_recovers_failed_records(tmp_path):
    spool = tmp_path / "spool.jsonl"
    inner = FlakySink(fail_first=2)
    sink = ThreadedSink(inner, maxsize=100, spool_path=str(spool))
    for i in range(5):
        sink.emit({"i": i})
    sink.close()
    # The 2 failed records were spooled, then replayed after recovery:
    # nothing lost, spool truncated.
    assert sorted(r["i"] for r in inner.records) == list(range(5))
    assert sink.spooled == 0
    assert not spool.exists()


def test_threaded_sink_without_spool_drops_failed_records():
    inner = FlakySink(fail_first=2)
    sink = ThreadedSink(inner, maxsize=100)  # pre-existing behavior
    for i in range(5):
        sink.emit({"i": i})
    sink.close()
    assert [r["i"] for r in inner.records] == [2, 3, 4]


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
