"""TelemetryEmitter — turns engine output into telemetry records.

The emitter is sink-agnostic: it builds plain-dict records (one per
call-start / turn / verdict / action / metric) and hands them to a
``TelemetrySink``. Sinks provided here:

- ``InMemorySink`` — tests.
- ``HTTPSink``     — POSTs records to the monitoring backend's ``/ingest``
  (stdlib urllib; no client dependency).
- The monitoring backend's ``CallStore`` is itself a sink (``emit()``) —
  the "local exporter that writes to the store directly".
- ``OTelSink``     — real OpenTelemetry spans/events/metrics when an OTLP
  exporter is configured (requires the ``otel`` extra).

PII note: transcripts, tool args, and evidence values carry PII. Records
support per-field redaction via ``redact_fields``; keep PII out of URL
params; the store must be access-controlled.
# TODO: extend redaction to nested tool args once a real PII policy lands.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Protocol, runtime_checkable

from plivo_mirror_v5.engine.verdict import Action, TurnResult, Verdict
from plivo_mirror_v5.telemetry import schema as S

_REDACTED = "[REDACTED]"


@runtime_checkable
class TelemetrySink(Protocol):
    def emit(self, record: dict) -> None: ...


class InMemorySink:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def emit(self, record: dict) -> None:
        self.records.append(record)

    def of_type(self, rec_type: str) -> list[dict]:
        return [r for r in self.records if r["type"] == rec_type]


class HTTPSink:
    """POSTs each record to the monitoring backend's ``/ingest``.

    NOTE: blocking I/O — never use it bare on a live call's event loop;
    wrap it in ``ThreadedSink`` (``attach_mirror`` does this for you)."""

    def __init__(self, base_url: str, *, api_key: str | None = None,
                 retries: int = 1) -> None:
        import os  # noqa: PLC0415

        self.ingest_url = base_url.rstrip("/") + "/ingest"
        # Backend write-protection (MIRROR_API_KEY) applies to /ingest too —
        # the sink picks the key up from the same env so a protected backend
        # keeps receiving telemetry without per-agent wiring.
        self.api_key = api_key or os.environ.get("MIRROR_API_KEY")
        self.retries = retries

    def emit(self, record: dict) -> None:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(
            self.ingest_url,
            data=json.dumps(record).encode(),
            headers=headers,
            method="POST",
        )
        last_exc: Exception | None = None
        for _attempt in range(1 + self.retries):
            try:
                urllib.request.urlopen(req, timeout=5).read()
                return
            except Exception as exc:  # noqa: BLE001 — one retry, then surface
                last_exc = exc
        raise last_exc


class ThreadedSink:
    """Decorator sink: hands records to a daemon thread so ``emit`` returns
    in microseconds — the live-call path must never wait on telemetry I/O.
    Records are delivered in order; failures are logged and dropped (the
    call always outranks its telemetry)."""

    def __init__(self, inner: "TelemetrySink") -> None:
        import logging
        import queue

        self.inner = inner
        self._queue: "queue.Queue[dict | None]" = queue.Queue()
        self._log = logging.getLogger("plivo_mirror_v5.telemetry")
        self._thread = threading.Thread(target=self._drain, daemon=True,
                                        name="mirror-telemetry")
        self._thread.start()

    def emit(self, record: dict) -> None:
        self._queue.put(record)

    def _drain(self) -> None:
        while True:
            record = self._queue.get()
            if record is None:
                return
            try:
                self.inner.emit(record)
            except Exception:  # noqa: BLE001 — telemetry must never kill the call
                self._log.exception("telemetry emit failed; record dropped")

    def close(self, timeout: float = 5.0) -> None:
        """Flush and stop the worker (call teardown / tests)."""
        self._queue.put(None)
        self._thread.join(timeout=timeout)


def verdict_to_dict(verdict: Verdict) -> dict:
    ev = verdict.evidence
    return {
        S.ATTR_VERDICT_ID: verdict.verdict_id,
        S.ATTR_DETECTOR: verdict.detector,
        S.ATTR_FIRED: verdict.fired,
        S.ATTR_SEVERITY: verdict.severity,
        S.ATTR_LATENCY_MS: verdict.latency_ms,
        S.ATTR_EVIDENCE: None if ev is None else {
            "claim_type": ev.claim_type,
            "spoken_value": ev.spoken_value,
            "truth_value": ev.truth_value,
            "source": ev.source,
            "extra": ev.extra,
        },
        S.ATTR_ARBITRATION: {
            "won": not verdict.suppressed_by,
            "suppressed": list(verdict.suppressed_by),
        },
    }


def action_to_dict(action: Action) -> dict:
    return {
        S.ATTR_ACTION_TAKEN: action.taken,
        S.ATTR_ACTION_HOOK: action.hook,
        S.ATTR_ACTION_CORRECTION: action.correction_text,
    }


class TelemetryEmitter:
    """API used by the observer: ``start_call`` / ``turn_span`` /
    ``record_verdict`` / ``record_action`` / ``end_call`` + metrics."""

    def __init__(self, sink: TelemetrySink, redact_fields: set[str] | None = None) -> None:
        self.sink = sink
        self.redact_fields = redact_fields or set()
        self._call_meta: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- call lifecycle ------------------------------------------------------

    def start_call(
        self,
        call_id: str,
        *,
        agent_id: str = "unknown",
        agent_version: str = "unknown",
        channel: str = "voice",
    ) -> None:
        with self._lock:
            self._call_meta[call_id] = {"agent_id": agent_id}
        self.sink.emit({
            "type": S.REC_CALL_START,
            "span": S.SPAN_CALL,
            S.ATTR_CALL_ID: call_id,
            S.ATTR_AGENT_ID: agent_id,
            S.ATTR_AGENT_VERSION: agent_version,
            S.ATTR_CHANNEL: channel,
            "t": time.time(),
        })

    def end_call(self, call_id: str, *, outcome: str = "completed") -> None:
        self.sink.emit({
            "type": S.REC_CALL_END,
            S.ATTR_CALL_ID: call_id,
            S.ATTR_OUTCOME: outcome,
            "t": time.time(),
        })
        with self._lock:
            self._call_meta.pop(call_id, None)

    # -- turns ----------------------------------------------------------------

    def turn_span(
        self,
        result: TurnResult,
        *,
        audio_offset_ms: float | None = None,
        audio_duration_ms: float | None = None,
        audio_levels: list[float] | None = None,
    ) -> None:
        """Emit one turn span + its verdict/action events + metrics."""
        self.sink.emit(self._redact({
            "type": S.REC_TURN,
            "span": S.SPAN_TURN,
            S.ATTR_CALL_ID: result.call_id,
            S.ATTR_TURN_ID: result.turn_id,
            S.ATTR_TURN_INDEX: result.turn_index,
            S.ATTR_ROLE: result.role,
            S.ATTR_TRANSCRIPT: result.transcript,
            S.ATTR_ASR_CONFIDENCE: result.asr_confidence,
            S.ATTR_AUDIO_OFFSET_MS: audio_offset_ms,
            S.ATTR_AUDIO_DURATION_MS: audio_duration_ms,
            S.ATTR_AUDIO_LEVELS: audio_levels,
            S.ATTR_STATE_SNAPSHOT_ID: result.state_snapshot_id,
            "t": time.time(),
        }))
        for verdict in result.verdicts:
            self.record_verdict(result.call_id, result.turn_id, verdict)
        if result.action is not None:
            self.record_action(result.call_id, result.turn_id, result.action)

    def record_verdict(self, call_id: str, turn_id: str, verdict: Verdict) -> None:
        self.sink.emit(self._redact({
            "type": S.REC_VERDICT,
            "event": S.EVENT_VERDICT,
            S.ATTR_CALL_ID: call_id,
            S.ATTR_TURN_ID: turn_id,
            **verdict_to_dict(verdict),
            "t": time.time(),
        }))
        # metrics — separate from traces, for trend dashboards
        agent_id = self._call_meta.get(call_id, {}).get("agent_id", "unknown")
        if verdict.fired and not verdict.suppressed_by:
            self._metric(S.METRIC_FLAGS_TOTAL, 1, kind="counter", labels={
                "layer": verdict.detector,
                "severity": verdict.severity,
                "agent_id": agent_id,
            })
        self._metric(S.METRIC_DETECTOR_LATENCY_MS, verdict.latency_ms,
                     kind="histogram", labels={"layer": verdict.detector})

    def record_action(self, call_id: str, turn_id: str, action: Action) -> None:
        self.sink.emit(self._redact({
            "type": S.REC_ACTION,
            "event": S.EVENT_ACTION,
            S.ATTR_CALL_ID: call_id,
            S.ATTR_TURN_ID: turn_id,
            **action_to_dict(action),
            "t": time.time(),
        }))
        if action.taken != "none":
            self._metric(S.METRIC_INTERVENTION_TOTAL, 1, kind="counter", labels={
                "hook": action.hook or "-",
                "action": action.taken,
            })

    # -- internals ---------------------------------------------------------

    def _metric(self, name: str, value: float, *, kind: str, labels: dict) -> None:
        self.sink.emit({
            "type": S.REC_METRIC,
            "name": name,
            "kind": kind,
            "value": value,
            "labels": labels,
            "t": time.time(),
        })

    def _redact(self, record: dict) -> dict:
        """Per-field PII redaction. Field names match attribute keys
        (e.g. ``mirror.transcript``) or evidence sub-keys
        (``evidence.spoken_value``)."""
        if not self.redact_fields:
            return record
        out = dict(record)
        for key in list(out):
            if key in self.redact_fields:
                out[key] = _REDACTED
        ev = out.get(S.ATTR_EVIDENCE)
        if isinstance(ev, dict):
            ev = dict(ev)
            for sub in ("spoken_value", "truth_value"):
                if f"evidence.{sub}" in self.redact_fields:
                    ev[sub] = _REDACTED
            out[S.ATTR_EVIDENCE] = ev
        return out


class OTelSink:
    """Bridges records to real OpenTelemetry. Optional: requires the
    ``otel`` extra and an OTLP exporter configured via the standard
    ``OTEL_EXPORTER_OTLP_*`` environment variables.

    # TODO: map turn records to real child spans of a live call span (the
    # local record model is flat; full span-tree fidelity is post-v5).
    """

    def __init__(self) -> None:
        try:
            from opentelemetry import metrics, trace  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "OTelSink requires the 'otel' extra: pip install 'plivo-mirror-v5[otel]'"
            ) from exc
        self._tracer = trace.get_tracer("plivo_mirror_v5")
        meter = metrics.get_meter("plivo_mirror_v5")
        self._counters = {
            S.METRIC_FLAGS_TOTAL: meter.create_counter(S.METRIC_FLAGS_TOTAL),
            S.METRIC_INTERVENTION_TOTAL: meter.create_counter(S.METRIC_INTERVENTION_TOTAL),
        }
        self._histograms = {
            S.METRIC_DETECTOR_LATENCY_MS: meter.create_histogram(S.METRIC_DETECTOR_LATENCY_MS),
        }

    def emit(self, record: dict) -> None:
        rec_type = record.get("type")
        if rec_type == S.REC_METRIC:
            name, value, labels = record["name"], record["value"], record["labels"]
            if name in self._counters:
                self._counters[name].add(value, labels)
            elif name in self._histograms:
                self._histograms[name].record(value, labels)
            return
        # Spans/events: emit each record as a short span carrying the
        # record's attributes (flat model; see TODO above).
        attrs = {
            k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
            for k, v in record.items()
            if v is not None and k not in ("type", "span", "event")
        }
        name = record.get("span") or record.get("event") or rec_type
        with self._tracer.start_as_current_span(name) as span:
            span.set_attributes(attrs)
