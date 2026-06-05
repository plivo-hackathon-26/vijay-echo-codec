# LiveKit Agents Cloud image for the SkyLine example with plivo-mirror.
#   lk agent create && lk agent deploy   (see v5/docs/CONNECT_CLOUD.md)
#   docker build -f v5/examples/skyline_flight_agent/Dockerfile v5/examples/skyline_flight_agent
FROM python:3.11-slim

WORKDIR /app

# Mirror (from PyPI) + the LiveKit stack this example uses.
RUN pip install --no-cache-dir "plivo-mirror-v5[agent,livekit]" \
    "livekit-plugins-deepgram" "livekit-plugins-elevenlabs" \
    "livekit-plugins-openai" "livekit-plugins-silero" \
    "python-dotenv"

COPY agent.py flight_store.py ./

# Runtime env (set in LiveKit Cloud agent settings, not baked in):
#   MIRROR_BACKEND_URL, MIRROR_API_KEY (optional),
#   LIVEKIT_URL/API_KEY/API_SECRET, OPENAI_*, DEEPGRAM_*, ELEVEN_*
CMD ["python", "agent.py", "start"]
