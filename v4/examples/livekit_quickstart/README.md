# LiveKit quickstart (plivo-mirror v4)

A supervised sandwich agent. The v4 firewall guards both boundaries and
keeps the model grounded in validated `SessionState` every turn.

```bash
pip install "plivo-mirror[livekit,openai]"
# env: LIVEKIT_URL/API_KEY/API_SECRET, DEEPGRAM_API_KEY, ELEVENLABS_API_KEY,
#      and AZURE_OPENAI_* (or OPENAI_API_KEY) for the grounded verifier
python agent.py dev
```

The integration is ~5 lines: build a `Firewall`, subclass
`SupervisedAgent`, add `@function_tool` methods that read from state.

**Configuring the "last model" (grounded verifier):**

```python
# auto from env:
firewall = Firewall.from_env(policies=POLICIES)
# explicit model:
firewall = Firewall.from_env(policies=POLICIES, model="gpt-4o-mini")
# fully custom verifier (any object implementing the Verifier protocol):
firewall = Firewall(policies=POLICIES, verifier=MyVerifier())
```

**Zero-argument tools:** `place_order(self)` takes no model-supplied args
— it reads the validated items from `SessionState`. The action guard also
blocks any tool call whose proposed args disagree with confirmed state.
