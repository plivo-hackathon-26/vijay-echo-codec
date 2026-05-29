# Demo recording runbook

Two sandwich agents side-by-side: **same prompt, same model, same
phone-call infrastructure**. The only difference is whether
`plivo-mirror` is wired in. This is the cleanest demo of what Mirror
adds.

## Folder map

```
/Users/vijay.krishna/Desktop/
├── sandwich-plain/      port 8001   no Mirror anywhere
├── sandwich-mirror/     port 8002   plivo-mirror + admin endpoints
├── burger-agent/        port 8000   the earlier integration (also Mirror-enabled)
└── vijay-echo-codec/
    └── demo-frontend/   the UI you'll record
```

## Prereqs (one-time setup)

```bash
# 1. Each agent needs its own venv + deps.
cd /Users/vijay.krishna/Desktop/sandwich-plain
/opt/homebrew/bin/python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in credentials

cd /Users/vijay.krishna/Desktop/sandwich-mirror
/opt/homebrew/bin/python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "plivo-mirror[openai,plivo]==0.1.0a3"
cp .env.example .env   # fill in credentials

# 2. sandwich-mirror should be a real GitHub repo (so Fix-PR works).
cd /Users/vijay.krishna/Desktop/sandwich-mirror
git init -b main
cat > .gitignore <<'EOF'
venv/
__pycache__/
.env
*.db
EOF
git add . && git commit -m "Initial sandwich-mirror"
gh auth login                              # if not already
gh repo create sandwich-mirror --private --source=. --push
```

## Two Plivo numbers (or two Plivo Applications)

Each agent answers on a different ngrok host, so you need either:

- **Two Plivo numbers**, each pointed at a different application; OR
- **One number that you switch between applications during the recording**.

For a clean recorded demo, two numbers is easier.

## Recording session — 5 terminals

| # | Where | Command |
|---|---|---|
| 1 | `sandwich-plain/` | `source venv/bin/activate && uvicorn main:app --port 8001 --reload` |
| 2 | (anywhere) | `ngrok http 8001`  → copy host → set `PUBLIC_HOST` in `sandwich-plain/.env` → **restart terminal 1** |
| 3 | `sandwich-mirror/` | `source venv/bin/activate && uvicorn main:app --port 8002 --reload` |
| 4 | (anywhere) | `ngrok http 8002`  → copy host → set `PUBLIC_HOST` in `sandwich-mirror/.env` → **restart terminal 3** |
| 5 | `demo-frontend/` | `python3 -m http.server 9000` → open http://localhost:9000 in browser |

In Plivo Console:
- Application **"sandwich-plain"** → Answer URL = `https://<ngrok-8001>/voice/answer`. Bind to your first number.
- Application **"sandwich-mirror"** → Answer URL = `https://<ngrok-8002>/voice/answer`. Bind to your second number.

Verify:

```bash
curl http://localhost:8001/health
# {"status":"ok",...,"mirror":"DISABLED"}
curl http://localhost:8002/health
# {"status":"ok",...,"mirror_enabled":true,"agent":"sandwich-mirror"}
```

## The recording flow (90 seconds, three calls)

### Beat 1 — call sandwich-plain (no Mirror) — show the failure

In the demo UI: select **"Sandwich (NO Mirror) · :8001"** from the dropdown. The middle column shows "This agent has NO plivo-mirror integration." That's intentional — the UI's making the point.

Call your first Plivo number. Say:

> *"My friend wants a club sandwich, but I want a BLT."*

The agent will respond with something like: *"Got it — one club sandwich and one BLT, total $20.00."*

Two failures the customer is paying for:
- Friend's preference was captured into the actual order
- Total is wrong ($20 = club $11 + BLT $9; should be $9 for just BLT)

Hang up.

### Beat 2 — switch to sandwich-mirror — show the catch

In the demo UI: select **"Sandwich (with Mirror) · :8002"** from the dropdown. The middle column now shows the live timeline (empty, waiting for a call).

Call your second Plivo number. Say:

> *"My friend wants a club sandwich, but I want a BLT."*

Watch the UI in real time:
- Customer turn appears
- Agent turn appears with `place_order(["club sandwich","BLT"])` chips
- **Tool-gate verdict 1.00 INTERVENED** — Mirror catches it before tools fire
- Mirror turn: *"Just to confirm — you'd like a BLT only, not the club sandwich?"*
- Caller hears the buffer line + correction
- You say *"yes"*
- Agent re-runs, places `place_order(["BLT"])`, total $9.00

Beat the camera on the side-by-side: same words, two outcomes.

### Beat 3 — review the failure report + open the PR

After Beat 2 ends, a **pending report** appears in the right column. Click it.

Modal shows:
- Pattern: `third_party_preference_in_order`
- Severity: HIGH
- Summary, root cause, proposed fix
- Target file: `agent.py`

Click **"Generate diff preview"** — Mirror's LLM rewrites the rigged
`SYSTEM_PROMPT` in `agent.py`, removes the CRITICAL CAPTURE RULE,
replaces it with "latest-preference-wins" + "third-party-context-only".
You see a unified diff.

Click **"Approve & open PR"** — Mirror:
- Branches off `main` in `sandwich-mirror/`
- Commits the rewrite
- Pushes to `origin`
- `gh pr create` opens a real GitHub PR
- PR URL toasts in the UI; new tab opens to the PR

Click **Merge** on GitHub. Now `main` has the fixed prompt.

### Beat 4 (optional) — call sandwich-mirror AGAIN

```bash
cd /Users/vijay.krishna/Desktop/sandwich-mirror
git pull origin main
# uvicorn auto-reloads when agent.py changes
```

Call the number once more. Same phrase. **Agent gets it right natively** — no intervention needed. Mirror is the silent insurance that catches mistakes when they happen; with the prompt fixed, mistakes stop happening.

That's the entire loop, on camera.

## What to highlight in voiceover

- **Same agent code, same prompt, two outcomes** — Mirror is the only difference.
- **Mirror caught it BEFORE the tool fired** — not after the fact. The kitchen never got the wrong order.
- **The PR is real.** This isn't a mockup — it's a real branch, real commit, real review-ready code change. You merge it and the agent improves permanently.
- **Latency**: agent took ~6-15s including Mirror's scoring + correction. Latency is the trade for getting the order right.

## Common gotchas

- **"Disconnected" pill stays red** → make sure the agent on that port is actually running. `curl http://localhost:8002/health` to confirm.
- **Toggle does nothing on sandwich-plain** → expected. Plain agent has no admin endpoints; toggling doesn't apply.
- **Report-approve says "no preview cached"** → click "Generate diff preview" FIRST, then "Approve". The preview must run before approve so we commit exactly the bytes you saw.
- **"failed apply" on retry** → use `plivo-mirror-fix apply <id> --retry --repo-path ...` from CLI, or click Approve again in the UI which re-runs.
