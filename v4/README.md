# plivo-mirror v4 — dual-boundary policy firewall

Real-time policy firewall for LLM voice agents. It guards two boundaries
before anything reaches the caller or the world:

- **Speech boundary** (LLM tokens → TTS): false facts, unauthorized
  verbal commitments, missing disclosures.
- **Action boundary** (tool call → execution): wrong/unauthorized
  actions, prompt-injection-driven tool calls, policy violations.

v4 is a major version of the same `plivo-mirror` package. It does **not**
reuse v3's three-tier scorer; it is a clean rebuild around a single
grounded verifier and a `SessionState`-as-source-of-truth design. See the
repo-root `CLAUDE.md` for the full architecture and build plan.

This package is built in phases. **Phase 1 (current):** core contracts +
session state store + policy compiler.

```bash
cd v4 && pip install -e ".[dev]" && pytest tests/ -q
```
