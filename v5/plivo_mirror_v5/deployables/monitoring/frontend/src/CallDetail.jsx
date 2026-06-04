import React, { useEffect, useState } from 'react'
import { fetchCall } from './api.js'

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

function Turn({ turn }) {
  const flagged = turn.verdicts.filter((v) => v.fired)
  const [open, setOpen] = useState(flagged.length > 0)
  const offset = fmtOffset(turn.audio_offset_ms)
  return (
    <div className={`turn role-${turn.role} ${flagged.length ? 'flagged' : ''}`}>
      <div className="turn-head" onClick={() => setOpen(!open)}>
        <span className="role">{turn.role}</span>
        <span className="transcript">{turn.transcript}</span>
        {flagged.length > 0 && <span className="badge flag">⚑ {flagged.length}</span>}
        {turn.actions?.map((a, i) =>
          a.taken !== 'none' && (
            <span key={i} className="badge action">{a.taken}{a.hook ? ` (hook ${a.hook})` : ''}</span>
          ))}
        {offset && (
          /* TODO: wire to the LiveKit room recording once audio storage lands */
          <a className="replay" href={`#replay-${turn.turn_id}`}
             title={`replay at ${offset}`}>▶ {offset}</a>
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

  useEffect(() => {
    fetchCall(callId).then(setCall).catch((err) => setError(err.message))
  }, [callId])

  if (error) return <p className="error">{error}</p>
  if (call === null) return <p>loading…</p>

  return (
    <div>
      <button className="back" onClick={onBack}>← all calls</button>
      <h2 className="mono">{call.call_id}</h2>
      <p className="dim">
        {call.agent_id} v{call.agent_version} · {call.channel} · {call.outcome}
      </p>
      <div className="timeline">
        {call.turns.map((t) => <Turn key={t.turn_id} turn={t} />)}
      </div>
    </div>
  )
}
