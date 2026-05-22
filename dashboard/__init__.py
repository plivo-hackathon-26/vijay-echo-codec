"""Phase 5 dashboard. Non-invasive overlay on the Phase 1-3 backend.

All hooks here are additive monkey-patches applied at import time. No
existing logic in agent/, voice/, mirror/ is modified. Specifically:

  - db.create_call → wrapped to also stamp agent_name + mirror_enabled
    onto the calls row at call start, and freeze the per-call mirror
    state in dashboard.mirror_toggle._call_states.
  - db.end_call → wrapped to also compute final_outcome and forget the
    call's frozen state.
  - mirror.state.get_intervention_pending → wrapped to return None for
    calls whose mirror_enabled was False at start, suppressing the
    pattern-driven intervention path.
  - mirror.semantic.review_response → wrapped to coerce its verdict to
    no-intervention for calls whose mirror_enabled was False, leaving
    detection (LLM call + log line) intact.

Import order matters: this module installs hooks the moment it's
imported, which is before uvicorn accepts any traffic.
"""

from dashboard import mirror_toggle as _mirror_toggle

_mirror_toggle.install_hooks()
