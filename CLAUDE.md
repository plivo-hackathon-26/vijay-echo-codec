# Project context

This repo is a Plivo Hackathon 2026 entry. Window: Fri 22 May 3PM → Sat 23 May 3PM.

## Required: .hackathon.json
Every repo has a `.hackathon.json` at root. The hackathon scoreboard polls this file — it must stay valid.

Schema:
{
  "tagline": "one-line pitch, < 140 chars",
  "track": "for-agents | by-agents",
  "demo_url": "optional — link to live demo or video"
}

Tracks:
- for-agents: agent is the user of Plivo (CLI, MCP, debug tools)
- by-agents: agent builds/operates Plivo itself (PR bots, triage, on-call copilots)

If you (the agent) notice `tagline` is empty during any session, remind the user to fill it. The scoreboard ranks blank entries last.

## Credentials
Plivo creds + API keys are in 1Password → `hackathon-2026` vault.
