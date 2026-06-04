#!/usr/bin/env python
"""Analyze an uploaded call RECORDING through the same mirror pipeline.

Pipeline: audio file → ASR with diarization (Deepgram prerecorded; needs
``DEEPGRAM_API_KEY``) → utterances become conversation turns with REAL
offsets, durations, per-utterance ASR confidence and REAL RMS waveform
levels (wav) → the standard observer evaluates every agent turn → results
land in the monitoring backend, and the audio file is copied next to it so
the dashboard's ▶ links seek the actual recording.

    venv/bin/python v5/plivo_mirror_v5/deployables/monitoring/analyze_recording.py \
        path/to/call.wav \
        --reference v5/eval/fixtures/reference_aurora.json \
        --db v5/mirror_monitoring.db          # or --url http://localhost:8500

Role mapping: voice agents speak first, so the first utterance's speaker
is assumed to be the agent — override with ``--agent-speaker N``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from plivo_mirror_v5.deployables.monitoring.backend.store import CallStore  # noqa: E402
from plivo_mirror_v5.engine import ReferenceStore  # noqa: E402
from plivo_mirror_v5.integrations import ConversationItem, attach_mirror  # noqa: E402
from plivo_mirror_v5.integrations.audio_levels import rms_levels  # noqa: E402
from plivo_mirror_v5.telemetry import HTTPSink  # noqa: E402


@dataclass
class Utterance:
    speaker: int
    text: str
    start_s: float
    end_s: float
    confidence: float | None = None


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(self, path: Path) -> list[Utterance]: ...


class DeepgramTranscriber:
    """Deepgram prerecorded API with diarization + utterances. stdlib-only."""

    URL = ("https://api.deepgram.com/v1/listen"
           "?model=nova-2&diarize=true&utterances=true&punctuate=true&smart_format=true")
    MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg", ".m4a": "audio/mp4"}

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("DEEPGRAM_API_KEY") or ""
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set — required to "
                               "transcribe recordings")

    def transcribe(self, path: Path) -> list[Utterance]:
        req = urllib.request.Request(
            self.URL,
            data=path.read_bytes(),
            headers={
                "Authorization": f"Token {self.api_key}",
                "Content-Type": self.MIME.get(path.suffix.lower(), "audio/wav"),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.load(resp)
        return [
            Utterance(
                speaker=int(u.get("speaker", 0)),
                text=u.get("transcript", ""),
                start_s=float(u["start"]),
                end_s=float(u["end"]),
                confidence=u.get("confidence"),
            )
            for u in payload["results"]["utterances"]
            if u.get("transcript", "").strip()
        ]


def wav_levels_for(path: Path, start_s: float, end_s: float) -> list[float] | None:
    """Real RMS bars for one utterance window of a 16-bit PCM wav."""
    if path.suffix.lower() != ".wav":
        return None  # TODO: decode compressed formats (ffmpeg) — post-v5
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return None
            rate, channels = w.getframerate(), w.getnchannels()
            w.setpos(min(w.getnframes(), int(start_s * rate)))
            n = max(0, min(w.getnframes() - w.tell(), int((end_s - start_s) * rate)))
            import array
            pcm = array.array("h")
            pcm.frombytes(w.readframes(n))
        levels = rms_levels(pcm, rate, channels)
        if not levels:
            return None
        # compress to <=24 bars for the dashboard strip
        step = max(1, len(levels) // 24)
        return [max(levels[i:i + step]) for i in range(0, len(levels), step)][:24]
    except Exception:  # noqa: BLE001 — levels are cosmetic
        return None


async def analyze(path: Path, *, reference: ReferenceStore, sink,
                  recordings_dir: Path, agent_speaker: int | None,
                  agent_id: str, action_verbs: dict | None,
                  transcriber: Transcriber) -> str:
    utterances = transcriber.transcribe(path)
    if not utterances:
        raise RuntimeError("transcriber returned no utterances")
    agent_spk = agent_speaker if agent_speaker is not None else utterances[0].speaker

    call_id = re.sub(r"[^A-Za-z0-9._-]", "-", path.stem)
    session = _NullSession()
    observer = attach_mirror(
        session, room_id=call_id, reference=reference, sink=sink,
        agent_id=agent_id, agent_version="recording", mode="shadow",
        action_verbs=action_verbs,
    )

    flagged = 0
    for u in utterances:
        role = "agent" if u.speaker == agent_spk else "user"
        observer._on_item(ConversationItem(
            role=role,
            text=u.text,
            asr_confidence=u.confidence if role == "user" else None,
            audio_offset_ms=u.start_s * 1000.0,
            audio_duration_ms=(u.end_s - u.start_s) * 1000.0,
            audio_levels=wav_levels_for(path, u.start_s, u.end_s),
        ))
        await observer.drain()
        result = observer.results[-1]
        for v in result.fired_verdicts:
            if v.severity != "info":
                flagged += 1
                print(f"  🚩 [{u.start_s:6.1f}s] {v.detector} {v.severity} "
                      f"{v.evidence.claim_type}: said {v.evidence.spoken_value!r} "
                      f"truth {v.evidence.truth_value!r} ({v.evidence.source})")
    observer.close(outcome="analyzed")

    recordings_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, recordings_dir / f"{call_id}{path.suffix.lower()}")
    print(f"analyzed {len(utterances)} utterances, {flagged} flags -> call {call_id}")
    return call_id


class _NullSession:
    def on(self, _event, _handler) -> None:  # adapter events never fire here
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--db", default="v5/mirror_monitoring.db")
    parser.add_argument("--url", default=None, help="POST to a running backend")
    parser.add_argument("--recordings-dir", type=Path,
                        default=Path(os.environ.get("MIRROR_RECORDINGS_DIR",
                                                    "v5/recordings")))
    parser.add_argument("--agent-speaker", type=int, default=None)
    parser.add_argument("--agent-id", default="uploaded-recording")
    parser.add_argument("--action-verbs", type=json.loads, default=None,
                        help='JSON, e.g. {"cancel_service": ["cancelled"]}')
    args = parser.parse_args()

    sink = HTTPSink(args.url) if args.url else CallStore(args.db)
    asyncio.run(analyze(
        args.audio,
        reference=ReferenceStore.from_file(args.reference),
        sink=sink,
        recordings_dir=args.recordings_dir,
        agent_speaker=args.agent_speaker,
        agent_id=args.agent_id,
        action_verbs=args.action_verbs,
        transcriber=DeepgramTranscriber(),
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
