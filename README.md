# Plivo Mirror

Silent AI supervisor for Plivo voice agents — detects, intervenes mid-call, self-corrects, ships failure reports.

This repository contains two things side-by-side:

| Path | What it is | When to look |
|---|---|---|
| **[`v1/`](v1/)** | Original hackathon demo — FastAPI app, dashboard, SQLite, two rigged agents (pizza-plivo + SkyPlivo travel). | You want to run the original 4-scenario phone-call demo. |
| **[`v2/`](v2/)** | The new `pip install plivo-mirror` library — domain-agnostic, single LLM scorer, streaming-aware, pre-tool-call gate, zero import-time side effects. | You're building a production voice agent and want Mirror as a dependency. |

## Quickstart by intent

**I want to run the hackathon demo.**

```bash
cd v1 && source ../venv/bin/activate && pip install -r requirements.txt
uvicorn main:app --port 8000 --reload
# in another shell: ngrok http 8000, then point Plivo XML at the ngrok URL
```

See [`v1/README.md`](v1/README.md) for the full demo guide.

**I want to use the library in my own Plivo voice agent.**

```bash
cd v2 && source ../venv/bin/activate
pip install -e ".[openai,plivo,dev]"
pytest tests/                          # confirm 32/32 pass
python -m plivo_mirror.replay examples/quickstart/sample_transcript.json --policies examples/quickstart/policies.txt --threshold-sweep 0.5,0.7,0.9
```

See [`v2/README.md`](v2/README.md) for the public API and full quickstart.

## Layout

```
.
├── v1/                              # legacy hackathon demo
│   ├── main.py, db.py, prompts.py
│   ├── agent/, agents/, dashboard/
│   ├── mirror/, voice/
│   ├── tests/                       # old tests (pattern engine, etc.)
│   ├── requirements.txt, pytest.ini
│   └── README.md
├── v2/                              # new pip-installable library
│   ├── plivo_mirror/                # the library itself
│   ├── tests/unit/                  # 32 unit tests (3-domain, streaming, tool-gate, no-side-effects)
│   ├── examples/quickstart/         # 20-line runnable demo + sample transcript + policies
│   ├── pyproject.toml
│   ├── LICENSE
│   └── README.md
├── CLAUDE.md                        # repo-wide context for AI assistants
├── .env / .env.example              # shared credentials
├── .hackathon.json                  # hackathon scoreboard metadata
└── venv/                            # shared Python environment
```

## What changed from v1 to v2

- **Detection**: deleted the regex pattern layer entirely; single LLM scorer with a cheap heuristic pre-gate (~80% LLM-cost reduction).
- **Domain coupling**: pizza/travel vocabulary moved out of the library — customer brings their own policies as a plain-English list.
- **Intervention**: replaced REST `speak()` + sleep heuristics with WebSocket-native `send_clear_audio` + `send_media` + `send_checkpoint`.
- **Architecture**: removed every import-time monkey-patch; introduced `LLMClient`, `StateStore`, `TTSSink`, `Scorer` protocols for pluggability.
- **New capabilities**:
  - **Pre-tool-call gate** — blocks irreversible tool calls (`place_order`, `charge_card`, etc.) before they execute, not after.
  - **Streaming-aware scoring** — scores the first-sentence boundary mid-stream for OpenAI Realtime / Gemini Live / Deepgram Voice Agent.
  - **Replay CLI** — `python -m plivo_mirror.replay` lets customers tune threshold + policies offline before going live.

## License

Apache-2.0 (see `v2/LICENSE`).
