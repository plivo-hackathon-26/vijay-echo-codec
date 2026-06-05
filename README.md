# plivo-mirror

**Catch your LLM voice agent lying — before the caller hears it, before the
money moves.**

Mirror is not just a library: it's a **monitoring + live-intervention
product** for LLM voice agents. Every sentence the agent speaks is diffed
against *your own ground truth* with an auditable receipt
(`spoken: $59.99 | truth: $79.99 | source: plan.turbo.price_per_month`);
flagged replies are corrected **before TTS**; unauthorized tool calls are
**blocked before execution**; everything lands on a live dashboard with a
reviewer loop that measures precision on *your* traffic.

`pip install plivo-mirror-v5` · the engine + LiveKit adapter + dashboard,
one package.

---

## ⭐ v5 — the current product (start here)

| | |
|---|---|
| **What it is** | A grounded verification layer + dashboard: shadow-monitor any LiveKit voice agent in ~5 minutes, flip one toggle to live intervention. |
| **The wedge** | Deterministic grounded **receipts** (a diff, not a judge's opinion) · **live in-call self-correction** (pre-TTS gate) · **pre-execution tool blocking** (the vishing defense) · **measured-on-your-traffic precision** (reviewer ✓/✗ loop). Verified absent in Hamming / Coval / Langfuse / Arize / Vapi / Retell. |
| **Measured** | 180 labeled cases, fresh reproducible run: **80.2% of violations blocked before speech · 86.4% combined catch · ~0–6% false alarms · 0.1 ms deterministic layer**. (v4 on the same sets: 35.4% catch.) |
| **Status** | Feature-complete (wrapped 2026-06-05) · 185 tests, all offline · `plivo-mirror-v5` on PyPI · production pickup planned. |

**Read in this order:**

1. **[`v5/DOCUMENTATION.md`](v5/DOCUMENTATION.md)** — the full product doc: idea, architecture, every layer, config, eval.
2. **[`v5/QUICKSTART.md`](v5/QUICKSTART.md)** — connect your agent in 5 minutes.
3. [`v5/README.md`](v5/README.md) — engine architecture (L1/L2/judge, two deployables).
4. [`v5/PRODUCTION.md`](v5/PRODUCTION.md) — measured numbers + env-var reference.
5. [`v5/docs/HANDOFF.md`](v5/docs/HANDOFF.md) — resume-here page for the production pickup.
6. [`v5/docs/ROADMAP.md`](v5/docs/ROADMAP.md) — honest limitations + the phased plan.

**Try it in 2 minutes, zero keys:**

```bash
pip install -e 'v5[monitoring]'
python -m uvicorn plivo_mirror_v5.deployables.monitoring.backend.app:app --port 8500 &
python v5/plivo_mirror_v5/deployables/monitoring/replay_fixture.py   # demo call → dashboard
```

**Wire a real agent (the whole integration):**

```python
from plivo_mirror_v5.integrations import attach_mirror

attach_mirror(session, room_id=ctx.room.name,
              backend_url=os.environ["MIRROR_BACKEND_URL"],
              agent_id="support-bot-prod",      # registered in the dashboard
              agent=my_agent)                   # dashboard-toggled intervene
```

In intervene mode the pre-TTS gate and the tool blocker **auto-wire** — zero
agent code changes. Four demo agents (ISP · bank · clinic · airline) in
[`v5/examples/`](v5/examples/) prove the engine is domain-generic.

---

## Where things are

| Path | What it is | Status |
|---|---|---|
| **[`v5/`](v5/)** | **THE PRODUCT** — grounded verification engine + monitoring dashboard + live intervention (pre-TTS gate, tool blocking) + post-call judge + eval harness. | **`plivo-mirror-v5` 0.5.x on PyPI** |
| [`v4/`](v4/) | The dual-boundary firewall — proved the deterministic action-boundary defenses (ported into v5), but its risk-lexicon gating capped catch at 35%. | `0.4.0rc1` (superseded) |
| [`v3/`](v3/) | The three-tier scorer (regex → NLI → LLM judge) + LiveKit `SupervisedAgent`. Its eval datasets are still v5's benchmark. | `plivo-mirror 0.3.x` (legacy stable) |
| [`v1/`](v1/), [`v2/`](v2/) | Original hackathon demo + first library iteration. | Archaeology |
| [`demo-frontend/`](demo-frontend/) | Early demo UI (pre-v5; v5 ships its own React dashboard). | — |

> Lineage in one line each: v1 proved the demo → v2 showed judge-every-turn
> kills voice latency → v3 shipped the tiered scorer to PyPI → v4 built the
> right deterministic defenses but starved its verifier → **v5 kept v4's
> deterministic floor, added the assertiveness-gated grounded judge, the
> receipts dashboard, pre-TTS gating and pre-execution tool blocking — and
> doubled catch while cutting false alarms.**

---

## v4 — the dual-boundary firewall (superseded by v5)

Two boundaries over a validated `SessionState` kept outside the model's
context: a **speech guard** (deterministic checks → risk-span tagger → NLI
tier → grounded verifier) and an **action guard** (false-completion,
arg↔state, authorization separation, zero-argument tools). Interventions
spoke a filler, regenerated from facts, re-verified, never restated the
wrong value.

What survived into v5: the action-boundary defenses (now L2 policy checks +
`ToolGate`), authorization separation, the session-state-as-truth principle,
and the pink-elephant correction discipline. What didn't: the risk-lexicon
router (35% catch) and the NLI tier. Details: [`v4/`](v4/) ·
[`v4/v4_overview.html`](v4/v4_overview.html) (interactive explainer).

---

## License

MIT (see `v5/pyproject.toml` / `v4/pyproject.toml`). Earlier lines retain
their own license files.
