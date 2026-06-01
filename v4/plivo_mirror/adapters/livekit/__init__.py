"""LiveKit adapter. Importing this requires ``livekit-agents`` installed
(``pip install "plivo-mirror[livekit]"``). It is intentionally NOT
imported by the top-level ``plivo_mirror`` package, so the core stays
importable without LiveKit."""

from __future__ import annotations

from plivo_mirror.adapters.livekit.supervised_agent import SupervisedAgent

__all__ = ["SupervisedAgent"]
