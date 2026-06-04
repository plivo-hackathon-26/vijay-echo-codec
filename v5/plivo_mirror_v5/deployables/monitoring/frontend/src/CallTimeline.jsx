import React from 'react'

// The call-signal view: every turn is a block on a shared time axis
// (x = audio_offset_ms), user above the axis, agent below. Bars inside a
// block are the turn's audio_levels when the source provides them
// (the live simulator sends synthetic ones; real RMS taps are a TODO in
// livekit_adapter). Flagged turns glow with their max severity.
const SEV_COLOR = { high: '#d62839', med: '#e8871e', low: '#c9a227' }

function turnSeverity(turn) {
  const fired = turn.verdicts.filter((v) => v.fired && v.severity !== 'info')
  if (!fired.length) return null
  const order = ['low', 'med', 'high']
  return fired.map((v) => v.severity).sort((a, b) => order.indexOf(b) - order.indexOf(a))[0]
}

function Bars({ levels, color }) {
  const n = levels.length
  return levels.map((lv, i) => (
    <div key={i} className="tl-bar" style={{
      left: `${(i / n) * 100}%`,
      width: `${Math.max(0.5, 100 / n - 1)}%`,
      height: `${Math.max(8, lv * 100)}%`,
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

  return (
    <div className="timeline-strip">
      <div className="tl-axis" />
      {placed.map((t) => {
        const sev = turnSeverity(t)
        const left = (t.audio_offset_ms / total) * 100
        const width = Math.max(1.5, ((t.audio_duration_ms || DEFAULT_MS) / total) * 100)
        const color = sev ? SEV_COLOR[sev] : (t.role === 'agent' ? '#3b5bdb' : '#94a3b8')
        return (
          <div
            key={t.turn_id}
            className={`tl-turn role-${t.role} ${sev ? 'tl-flagged' : ''}`}
            style={{ left: `${left}%`, width: `${width}%`, '--tl-color': color }}
            title={`${t.role} @ ${(t.audio_offset_ms / 1000).toFixed(1)}s — ${t.transcript}`}
            onClick={() => onJump && onJump(t.turn_id)}
          >
            {t.audio_levels?.length
              ? <Bars levels={t.audio_levels} color={color} />
              : <div className="tl-solid" style={{ background: color }} />}
            {sev && <div className="tl-flag-dot" style={{ background: SEV_COLOR[sev] }} />}
          </div>
        )
      })}
      <div className="tl-times">
        <span>0:00</span>
        <span>{Math.floor(total / 60000)}:{String(Math.floor((total % 60000) / 1000)).padStart(2, '0')}</span>
      </div>
    </div>
  )
}
