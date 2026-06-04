"""Telemetry schema constants — span names, event names, attribute keys,
metric names. Both deployables and the backend speak exactly this schema.

The model: **call = trace, turn = span, verdict/action = span events.**
``call_id`` == the LiveKit room/session id (or SIP call id) — we never mint
our own, so telemetry joins LiveKit's traces and the audio recording.
"""

# -- spans -------------------------------------------------------------------
SPAN_CALL = "mirror.call"
SPAN_TURN = "mirror.turn"

# -- span events --------------------------------------------------------------
EVENT_VERDICT = "mirror.verdict"
EVENT_ACTION = "mirror.action"

# -- record types (the local-sink wire format; one dict per record) -----------
REC_CALL_START = "call_start"
REC_CALL_END = "call_end"
REC_TURN = "turn"
REC_VERDICT = "verdict"
REC_ACTION = "action"
REC_METRIC = "metric"

# -- attribute keys ------------------------------------------------------------
ATTR_CALL_ID = "mirror.call_id"
ATTR_AGENT_ID = "mirror.agent_id"
ATTR_AGENT_VERSION = "mirror.agent_version"
ATTR_CHANNEL = "mirror.channel"
ATTR_OUTCOME = "mirror.outcome"
ATTR_TURN_ID = "mirror.turn_id"
ATTR_TURN_INDEX = "mirror.turn_index"
ATTR_ROLE = "mirror.role"
ATTR_TRANSCRIPT = "mirror.transcript"
ATTR_ASR_CONFIDENCE = "mirror.asr_confidence"
ATTR_AUDIO_OFFSET_MS = "mirror.audio_offset_ms"
ATTR_TOOL_CALLS = "mirror.tool_calls"
ATTR_STATE_SNAPSHOT_ID = "mirror.state_snapshot_id"
ATTR_VERDICT_ID = "mirror.verdict_id"
ATTR_DETECTOR = "mirror.detector"
ATTR_FIRED = "mirror.fired"
ATTR_SEVERITY = "mirror.severity"
ATTR_LATENCY_MS = "mirror.latency_ms"
ATTR_EVIDENCE = "mirror.evidence"
ATTR_ARBITRATION = "mirror.arbitration"
ATTR_ACTION_TAKEN = "mirror.action.taken"
ATTR_ACTION_HOOK = "mirror.action.hook"
ATTR_ACTION_CORRECTION = "mirror.action.correction_text"

# -- metrics (separate from traces; for trend dashboards) ----------------------
METRIC_FLAGS_TOTAL = "mirror.flags_total"                 # counter{layer,severity,agent_id}
METRIC_DETECTOR_LATENCY_MS = "mirror.detector_latency_ms"  # histogram{layer}
METRIC_INTERVENTION_TOTAL = "mirror.intervention_total"    # counter{hook,action}
