# Publishing plivo-mirror to PyPI

## What's ready

```
v2/dist/
├── plivo_mirror-0.1.0a1-py3-none-any.whl    52 KB
└── plivo_mirror-0.1.0a1.tar.gz              39 KB
```

Both artifacts pass `twine check`. Wheel contains 35 modules + LICENSE + METADATA + py.typed marker.

## Prerequisites

1. **A PyPI account** — sign up at https://pypi.org/account/register/
2. **A project API token** with upload scope:
   - Go to https://pypi.org/manage/account/token/
   - Create a new token, scope: "Entire account" (first publish) or "plivo-mirror" project (after first upload)
   - Token format: `pypi-AgEIcHlwa...`
3. **`twine` installed** — already in the venv.

## Strongly recommended: test on TestPyPI first

```bash
# 1. Get a TestPyPI token from https://test.pypi.org/manage/account/token/
# 2. Upload to TestPyPI:
cd /Users/vijay.krishna/Desktop/vijay-echo-codec/v2
TWINE_USERNAME=__token__ TWINE_PASSWORD="$TESTPYPI_TOKEN" \
    python -m twine upload --repository testpypi dist/*

# 3. Test-install from TestPyPI into a fresh venv:
python -m venv /tmp/test-venv
/tmp/test-venv/bin/pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ \
    "plivo-mirror[openai,plivo]"

# 4. Verify import + version:
/tmp/test-venv/bin/python -c "import plivo_mirror; print(plivo_mirror.__version__)"
# Should print: 0.1.0a1
```

## Real PyPI publish

```bash
cd /Users/vijay.krishna/Desktop/vijay-echo-codec/v2
TWINE_USERNAME=__token__ TWINE_PASSWORD="$PYPI_TOKEN" \
    python -m twine upload dist/*
```

Once it succeeds you'll see something like:

```
View at:
https://pypi.org/project/plivo-mirror/0.1.0a1/
```

Anyone can then:

```bash
pip install plivo-mirror[openai,plivo]
```

## After publishing

1. **Tag the release in git:**
   ```bash
   git tag -a v0.1.0a1 -m "plivo-mirror 0.1.0a1 — first PyPI release"
   git push origin v0.1.0a1
   ```

2. **Bump the version for next development:**
   - Edit `v2/pyproject.toml` line 5: `version = "0.1.0a2"` (or `0.1.0` for first stable)
   - Edit `v2/plivo_mirror/__init__.py` line `__version__ = "0.1.0a2"`

3. **Verify the install on a clean machine** so the README quickstart actually works as documented.

## To re-build after code changes

```bash
cd /Users/vijay.krishna/Desktop/vijay-echo-codec/v2
rm -rf dist/
python -m build
python -m twine check dist/*
```

Then upload again. PyPI does NOT allow re-uploading the same version — bump the version in `pyproject.toml` first.

## Troubleshooting

| Error | Cause |
|---|---|
| `400 Bad Request: Filename or contents have already been used` | You're trying to re-upload the same version. Bump version, rebuild, retry. |
| `403 Forbidden` | Token scope is wrong or token is for a different project. |
| `Invalid distribution` | Run `python -m twine check dist/*` to see the validation error. |
| `Project name already taken` | Someone else owns `plivo-mirror` on PyPI. Either pick a different name (e.g. `plivo-mirror-sdk`) or reach out to the existing owner. |

## What's in the wheel

```
plivo_mirror/
├── __init__.py             zero side effects re-exports
├── config.py               MirrorConfig (pydantic)
├── context.py              Verdict, TurnPayload, TurnOutcome, etc.
├── supervisor.py           Supervisor, CallSupervisor
├── replay.py               offline replay CLI (also installed as `plivo-mirror-replay`)
├── agents/openai_loop.py   supervised OpenAI tool-use loop
├── scorer/                 llm, pregate, streaming, tool_gate
├── policy/compiler.py      policies → judging prompt
├── intervention/           orchestrator, generator, templates
├── voice/tts/              ws_inject (primary), plivo_speak (REST fallback)
├── plivo/                  stream_sdk binding, raw_ws adapter
├── llm/                    LLMClient protocol + OpenAI (Azure auto-detect)
├── state/                  StateStore protocol + in-memory default
└── py.typed                type-hints marker for downstream type checkers
```

Examples (`v2/examples/`, `v2/voice_agents/`) and tests (`v2/tests/`) are deliberately excluded from the wheel.
