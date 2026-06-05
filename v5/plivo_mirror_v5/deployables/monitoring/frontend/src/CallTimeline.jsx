import React from 'react'

// The call-signal view: every turn is a block on a shared time axis
// (x = audio_offset_ms), user above the axis, agent below.
//
// Waveform bars come from REAL RMS levels when the source provided them
// (room audio taps / wav analysis / simulator). When a turn has no levels
// (e.g. LiveKit console mode exposes no room tracks) we render an
// ESTIMATED signal derived deterministically from the transcript — the
// legend says which one you're looking at. Flagged turns glow.
// Plivo palette on the light blueprint strip: indigo agent, neutral caller,
// warm flags.
const SEV_COLOR = { high: '#e5484d', med: '#d97706', low: '#b48b07' }
const ROLE_COLOR = { agent: '#323dfe', user: '#aab2bd' }

function turnSeverity(turn) {
  const fired = turn.verdicts.filter((v) => v.fired && v.severity !== 'info')
  if (!fired.length) return null
  const order = ['low', 'med', 'high']
  return fired.map((v) => v.severity).sort((a, b) => order.indexOf(b) - order.indexOf(a))[0]
}

// Deterministic pseudo-waveform from the transcript (ESTIMATED signal).
function estimatedLevels(text, n = 22) {
  let h = 2166136261
  const out = []
  for (let i = 0; i < n; i++) {
    const ch = text.charCodeAt(i % Math.max(1, text.length)) || 32
    h = Math.imul(h ^ (ch + i * 31), 16777619) >>> 0
    out.push(0.2 + (h % 1000) / 1250)
  }
  return out
}

function Bars({ levels, color }) {
  const n = levels.length
  return levels.map((lv, i) => (
    <div key={i} className="tl-bar" style={{
      left: `${(i / n) * 100}%`,
      width: `${Math.max(0.5, 100 / n - 1.2)}%`,
      height: `${Math.max(10, Math.min(100, lv * 100))}%`,
      background: color,
    }} />
  ))
}

export default function CallTimeline({ turns, onJump }) {
  const placed = turns.filter((t) => t.audio_offset_ms != null)
  if (placed.length < 2) return null

  const DEFAULT_MS = 3000
  const total = Math.max(...placed.map(
    (t) => t.audio_offset_ms + (t.audio_duration_ms || DEFAULT_MS)))
  const anyReal = placed.some((t) => t.audio_levels?.length)

  return (
    <div className="timeline-card">
      <div className="timeline-legend">
        <span><i className="lg-swatch" style={{ background: ROLE_COLOR.user }} /> caller</span>
        <span><i className="lg-swatch" style={{ background: ROLE_COLOR.agent }} /> agent</span>
        <span><i className="lg-swatch" style={{ background: SEV_COLOR.high }} /> flagged</span>
        <span className="dim lg-note">
          signal: {anyReal ? 'live audio RMS' : 'estimated from transcript'}
          {' '}· click a block to jump
        </span>
      </div>
      <div className="timeline-strip">
        <div className="tl-axis" />
        {placed.map((t) => {
          const sev = turnSeverity(t)
          const left = (t.audio_offset_ms / total) * 100
          const width = Math.max(2, ((t.audio_duration_ms || DEFAULT_MS) / total) * 100)
          const color = sev ? SEV_COLOR[sev] : ROLE_COLOR[t.role] || ROLE_COLOR.user
          const levels = t.audio_levels?.length
            ? t.audio_levels
            : estimatedLevels(t.transcript || '')
          return (
            <div
              key={t.turn_id}
              className={`tl-turn role-${t.role} ${sev ? 'tl-flagged' : ''}`}
              style={{ left: `${left}%`, width: `${width}%`, '--tl-color': color }}
              title={`${t.role} @ ${(t.audio_offset_ms / 1000).toFixed(1)}s — ${t.transcript}`}
              onClick={() => onJump && onJump(t)}
            >
              <Bars levels={levels} color={color} />
              {sev && <div className="tl-flag-dot" style={{ background: SEV_COLOR[sev] }} />}
            </div>
          )
        })}
        <div className="tl-times">
          <span>0:00</span>
          <span>{Math.floor(total / 60000)}:{String(Math.floor((total % 60000) / 1000)).padStart(2, '0')}</span>
        </div>
      </div>
    </div>
  )
}
