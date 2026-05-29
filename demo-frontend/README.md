# plivo-mirror — live demo frontend

A single self-contained HTML page that visualises a running Plivo voice agent + plivo-mirror in real time. Built for recording demos, screenshots, and "show, don't tell" pitches.

## What it shows

- **Live call timeline** — every customer utterance, agent response, tool call, scorer verdict, and intervention as the call happens. Per-step latencies as visual bars.
- **Mirror ON / OFF toggle** — flip Mirror in real time. When OFF, the scorer still runs (so you see the verdict the UI would have used), but the agent's text is spoken as-is. Side-by-side comparison without running two agents.
- **Pending failure reports** — when a call ends with one or more interventions, the auto-generated report appears in the right column. Click to review.
- **Diff preview + Approve** — the modal generates the LLM rewrite, shows you a unified diff, then commits + opens a GitHub PR when you click Approve.
- **Light + Dark theme** — Plivo-style design tokens, sun/moon toggle in the header.

## How to run it

The frontend talks to two backends — pick from the dropdown in the header:

| Dropdown option | Backend | What it shows |
|---|---|---|
| **sandwich-mirror (supervised) · :8002** | `~/Desktop/sandwich-mirror/` | Full demo: mirror toggle, live verdicts, interventions, reports, Fix-PR approve. |
| **sandwich-plain (control / no Mirror) · :8001** | `~/Desktop/sandwich-plain/` | Calls + transcript stream, but Mirror toggle is greyed out ("Mirror N/A") and the reports column says "Plain agent — no reports". Use this side-by-side with the mirror backend to prove the before/after story. |

### 1. Start both backends

In two separate terminals:

```bash
# Terminal A — mirror-supervised agent on :8002
cd ~/Desktop/sandwich-mirror
source venv/bin/activate
uvicorn main:app --port 8002 --reload

# Terminal B — plain control agent on :8001
cd ~/Desktop/sandwich-plain
source venv/bin/activate
uvicorn main:app --port 8001 --reload
```

In a third terminal, start ngrok for whichever backend you want to receive a real Plivo call:

```bash
ngrok http 8002        # or 8001 for the plain agent
```

Set `PUBLIC_HOST` in that backend's `.env` to your ngrok host, then restart that uvicorn.

### 2. Open the demo frontend

```bash
cd ~/Desktop/vijay-echo-codec/demo-frontend
python3 -m http.server 9000
```

Then visit **http://localhost:9000/** in your browser.

The frontend's static — no build step, no node_modules, no backend. It calls each agent's `/admin/*` endpoints via fetch + EventSource directly. CORS is wide-open on both backends so this just works.

### 3. Make a call

Dial whichever Plivo number is pointed at your ngrok tunnel. Watch the left column populate with the new call, the middle column light up with the live turn-by-turn timeline, and the toggle in the header reflect the current Mirror state.

### 4. Three ways to compare with/without Mirror

The dropdown gives you two backends. The Mirror toggle gives you a third dimension:

| You want to demo... | Pick this |
|---|---|
| **Same backend, Mirror flips in real time** (recommended cinematic) | Stay on `sandwich-mirror (:8002)`, click the Mirror ON pill to flip OFF. With Mirror OFF, the scorer still runs and the UI annotates each agent response with **WOULD have intervened (score=0.98)** so you can see exactly what Mirror would have caught. |
| **Two separate processes, mirror vs no-mirror** | Switch the dropdown to `sandwich-plain (:8001)`. The Mirror toggle goes grey ("Mirror N/A") because the plain backend physically has no Mirror in its WebSocket path. Make a call here, then switch back to `:8002` and make the same call to compare. |
| **Quick A/B without re-dialling** | Make one call on `:8002` with Mirror ON, then click toggle → OFF, make another call. Three calls side-by-side in the left column give you the demo cinematic. |

### 5. Review the report

After a call where Mirror intervened, a pending report appears in the right column. Click it → modal opens with:
- Pattern + severity + confidence
- Summary
- Root cause
- Proposed fix text
- Target file
- Suggested diff (advisory)

Click **Generate diff preview** → the LLM rewrites the agent's source file and the UI renders a unified diff.

Click **Approve & open PR** → commits + pushes the branch + opens a real GitHub PR via `gh`. The PR URL opens in a new tab.

## Requirements

- `sandwich-mirror` running on `http://localhost:8002` and/or `sandwich-plain` running on `http://localhost:8001`. The frontend hard-codes these URLs in the dropdown.
- `gh auth login` must be done once if you want to use the **Approve & open PR** path from the reports panel.
- Whichever backend folder you want Fix-PR to target (typically `sandwich-mirror/`) must be a real GitHub repo with an `origin` remote — see `sandwich-mirror/README.md` for the `gh repo create` line.

## What lives where

```
demo-frontend/
├── index.html       single-file UI (Plivo-style, light+dark, ~1000 lines incl. CSS+JS)
└── README.md        you are here
```

Open `index.html` directly in a browser. No build, no server (well, just `python -m http.server` to avoid CORS-from-file://).
