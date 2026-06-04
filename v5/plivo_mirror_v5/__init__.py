"""plivo-mirror v5 — a real-time firewall that verifies a voice agent's
OUTPUT against ground truth.

One detection engine (``plivo_mirror_v5.engine``), two deployables:

- monitoring (shadow mode): verdicts become telemetry, rendered in a
  call-ID-keyed dashboard (``deployables/monitoring``);
- live intervention (inline mode): verdicts trigger a correction / hold /
  handoff via a LiveKit hook (``deployables/intervention``).

Both are powered by the single LiveKit observer in
``integrations/livekit_observer.py``; a ``mode`` flag selects routing.
"""

__version__ = "0.5.0"
