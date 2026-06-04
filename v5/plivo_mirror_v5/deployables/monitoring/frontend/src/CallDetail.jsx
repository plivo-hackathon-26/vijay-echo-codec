import React, { useEffect, useRef, useState } from 'react'
import { fetchCall } from './api.js'
import CallTimeline from './CallTimeline.jsx'

const LIVE_POLL_MS = 1500

function fmtOffset(ms) {
  if (ms == null) return null
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

// The evidence payload is the product differentiator — render it VERBATIM.
function EvidenceCard({ verdict }) {
  const ev = verdict.evidence
  if (!ev) return null
  return (
    <div className={`evidence sev-${verdict.severity}`}>
      <div className="evidence-head">
        <span className="badge layer">{verdict.detector}</span>
        <span className={`badge sev-${verdict.severity}`}>{verdict.severity}</span>
        <span className="dim">{ev.claim_type}</span>
        <span className="dim">{verdict.latency_ms?.toFixed(3)} ms</span>
        {verdict.arbitration?.suppressed?.length > 0 && (
          <span className="badge suppressed">
            suppressed by {verdict.arbitration.suppressed.join(', ')}
          </span>
        )}
      </div>
      <table className="evidence-table">
        <tbody>
          <tr><th>spoken</th><td className="spoken">{String(ev.spoken_value)}</td></tr>
          <tr><th>truth</th><td className="truth">{String(ev.truth_value)}</td></tr>
          <tr><th>source</th><td className="mono">{ev.source}</td></tr>
        </tbody>
      </table>
    </div>
  )
}

function Turn({ turn, onReplay, hasAudio }) {
  const flagged = turn.verdicts.filter((v) => v.fired)
  const [open, setOpen] = useState(flagged.length > 0)
  const offset = fmtOffset(turn.audio_offset_ms)
  return (
    <div id={`turn-${turn.turn_id}`}
         className={`turn role-${turn.role} ${flagged.length ? 'flagged' : ''}`}>
      <div className="turn-head" onClick={() => setOpen(!open)}>
        <span className="role">{turn.role}</span>
        <span className="transcript">{turn.transcript}</span>
        {flagged.length > 0 && <span className="badge flag">⚑ {flagged.length}</span>}
        {turn.actions?.map((a, i) =>
          a.taken !== 'none' && (
            <span key={i} className="badge action">{a.taken}{a.hook ? ` (hook ${a.hook})` : ''}</span>
          ))}
        {offset && (
          <a className={`replay ${hasAudio ? '' : 'replay-disabled'}`}
             title={hasAudio ? `replay at ${offset}` : 'no recording for this call'}
             onClick={(e) => { e.stopPropagation(); hasAudio && onReplay(turn.audio_offset_ms) }}
          >▶ {offset}</a>
        )}
      </div>
      {open && flagged.map((v) => <EvidenceCard key={v.verdict_id} verdict={v} />)}
      {open && (
        <div className="turn-meta dim">
          snapshot {turn.state_snapshot_id}
          {turn.asr_confidence != null && ` · asr ${turn.asr_confidence}`}
        </div>
      )}
    </div>
  )
}

export default function CallDetail({ callId, onBack }) {
  const [call, setCall] = useState(null)
  const [error, setError] = useState(null)
  const bottomRef = useRef(null)
  const audioRef = useRef(null)
  const turnCount = useRef(0)

  // Live view: poll while the call is in progress (and once after it ends).
  useEffect(() => {
    let alive = true
    let timer = null
    const load = async () => {
      try {
        const data = await fetchCall(callId)
        if (!alive) return
        setCall(data)
        setError(null)
        if (data.outcome === 'in_progress') timer = setTimeout(load, LIVE_POLL_MS)
      } catch (err) {
        if (!alive) return
        setError(err.message)
        timer = setTimeout(load, LIVE_POLL_MS * 2)
      }
    }
    load()
    return () => { alive = false; clearTimeout(timer) }
  }, [callId])

  // Follow the live transcript as new turns stream in.
  useEffect(() => {
    if (call && call.turns.length > turnCount.current) {
      turnCount.current = call.turns.length
      if (call.outcome === 'in_progress')
        bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [call])

  if (error && !call) return <p className="error">{error}</p>
  if (call === null) return <p>loading…</p>

  const live = call.outcome === 'in_progress'
  const jump = (turnId) =>
    document.getElementById(`turn-${turnId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  const replayAt = (offsetMs) => {
    const audio = audioRef.current
    if (!audio) return
    audio.currentTime = (offsetMs || 0) / 1000
    audio.play()
  }

  return (
    <div>
      <button className="back" onClick={onBack}>← all calls</button>
      <h2 className="mono">
        {call.call_id}
        {live && <span className="badge live">● LIVE</span>}
      </h2>
      <p className="dim">
        {call.agent_id} v{call.agent_version} · {call.channel} · {call.outcome}
      </p>
      {call.has_audio && (
        <audio ref={audioRef} controls preload="metadata" className="call-audio"
               src={`/api/calls/${encodeURIComponent(call.call_id)}/audio`} />
      )}
      <CallTimeline turns={call.turns} onJump={jump} />
      <div className="timeline">
        {call.turns.map((t) => (
          <Turn key={t.turn_id} turn={t} onReplay={replayAt} hasAudio={!!call.has_audio} />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
