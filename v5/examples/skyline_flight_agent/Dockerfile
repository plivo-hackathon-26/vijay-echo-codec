# LiveKit Agents Cloud image for the SkyLine example with plivo-mirror.
#   lk agent create && lk agent deploy   (see v5/docs/CONNECT_CLOUD.md)
# Build context: the REPO ROOT (the image needs the v5 package):
#   docker build -f v5/examples/skyline_flight_agent/Dockerfile .
FROM python:3.11-slim

WORKDIR /app

# Mirror core + the LiveKit stack this example uses.
COPY v5/pyproject.toml v5/pyproject.toml
COPY v5/plivo_mirror_v5 v5/plivo_mirror_v5
RUN pip install --no-cache-dir -e 'v5[livekit]' \
    "livekit-plugins-deepgram" "livekit-plugins-elevenlabs" \
    "livekit-plugins-openai" "livekit-plugins-silero" \
    "python-dotenv" "openai"

COPY v5/examples/skyline_flight_agent/agent.py \
     v5/examples/skyline_flight_agent/flight_store.py ./

# Runtime env (set in LiveKit Cloud agent settings, not baked in):
#   MIRROR_BACKEND_URL, MIRROR_API_KEY (optional),
#   LIVEKIT_URL/API_KEY/API_SECRET, OPENAI_*, DEEPGRAM_*, ELEVEN_*
CMD ["python", "agent.py", "start"]
