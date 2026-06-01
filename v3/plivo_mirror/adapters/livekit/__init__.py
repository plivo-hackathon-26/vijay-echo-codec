"""LiveKit Agents adapter for plivo-mirror.

The ``SupervisedAgent`` class is what customers integrating with the
``livekit-agents`` framework should inherit from. It handles
everything the v0.2.0 hand-rolled mirror_supervisor.py did:

  • Customer text extraction from ``chat_ctx`` (across LiveKit's
    v1.x ChatContext API shapes)
  • LLM-stream buffering for the supervisor to inspect tool_calls
  • Intervention dispatch via MirrorJudge
  • Sticky intent-note injection on the post-correction turn so the
    LLM doesn't ask the customer to repeat themselves
  • Cooldown to prevent duplicate corrections when LiveKit's
    preemptive generation re-invokes llm_node mid-turn
  • Skip-on-empty greeting turns
  • Agent-voice / meta-description filtering for spoken corrections

Customer code becomes:

    from plivo_mirror import Supervisor
    from plivo_mirror.adapters.livekit import SupervisedAgent

    supervisor = Supervisor.from_env(policies=[...])

    class MyAgent(SupervisedAgent):
        def __init__(self):
            super().__init__(supervisor=supervisor, instructions=SYSTEM_PROMPT)

        @function_tool
        async def place_order(self, items: list[str]) -> dict:
            ...

That's it. No manual llm_node override, no chat_ctx mutation, no
intent-note bookkeeping.
"""

try:
    from plivo_mirror.adapters.livekit.supervised_agent import SupervisedAgent
except ImportError as exc:  # pragma: no cover - depends on optional install
    if "livekit" not in str(exc):
        raise
    raise ImportError(
        "plivo_mirror.adapters.livekit requires the `livekit-agents` package. "
        "Install with `pip install \"plivo-mirror[livekit]\"`."
    ) from exc

__all__ = ["SupervisedAgent"]
