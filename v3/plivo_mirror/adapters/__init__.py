"""Adapters for plugging plivo-mirror into specific voice-agent stacks.

Each adapter ships a minimal integration surface so customers don't
have to hand-roll the same llm_node / chat_ctx / cooldown / intent-note
glue. Currently:

    plivo_mirror.adapters.livekit  — ``SupervisedAgent`` mixin for the
                                     livekit-agents framework.

Adapters are optional imports; importing this package itself never
imports the underlying framework.
"""
