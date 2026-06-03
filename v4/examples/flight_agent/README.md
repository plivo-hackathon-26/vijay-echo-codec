# SkyLine Airways — interactive LiveKit voice agent

A real, talk-to-it voice agent that **books** and **cancels** flights. Plain
LiveKit (Deepgram STT · OpenAI/Azure LLM · ElevenLabs TTS) over an in-memory
booking store — **no plivo-mirror yet**. This is the clean baseline we'll
later make slip, then guard with the firewall.

## Run

```bash
cd v4/examples/flight_agent
source ../../../venv/bin/activate
python agent.py dev
```

Needs the repo-root `.env`: `LIVEKIT_URL/API_KEY/API_SECRET`,
`DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` (or `ELEVEN_API_KEY`), and either
`AZURE_OPENAI_*` or `OPENAI_API_KEY` + `OPENAI_BASE_URL`/`OPENAI_API_URL`.

## Talk to it

`python agent.py dev` starts a **worker that waits for a call**. Connect a
room and just talk:

- **LiveKit Agents Playground** — https://agents-playground.livekit.io →
  connect with your project (`LIVEKIT_URL`) → it routes to your worker.
- or the LiveKit CLI / your own frontend.

The agent greets you, then handles a full conversation.

## What it can do

| Tool | What it does |
|---|---|
| `search_flights(origin, destination, date)` | 3 deterministic options for the route/date (cheapest first). Accepts cities ("New York") or codes ("JFK"). |
| `book_flight(flight_number, passenger_name)` | Books a searched flight, returns a 6-char **PNR**. |
| `get_booking(pnr)` | Looks a booking up. |
| `cancel_booking(pnr)` | Cancels, refunds 80% (20% fee). |

Served airports: JFK/EWR/LGA (New York), LAX, SFO, ORD, MIA, BOS, SEA, DEN,
ATL, DFW, LAS, AUS.

## Try these

**Book:**
> "I'd like to fly from New York to Los Angeles on June 20th."
> → it offers options → "Book the SkyLine one, name's Jordan Lee." → confirm
> → you get a PNR.

**Cancel (works immediately — seeded booking):**
> "I need to cancel a flight. My reference is J-T-4-R-9-X."
> → it reads back the JFK→LAX flight → confirm → refund quoted.

The store is in-memory: bookings live for the duration of the worker
process. Restart `agent.py` to reset (the seeded `JT4R9X` comes back).
