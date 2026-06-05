import React, { useEffect, useRef, useState } from 'react'
import { API_BASE, analyzeCall, fetchCall, fetchReceipts, saveLabel } from './api.js'
import CallTimeline from './CallTimeline.jsx'

const LIVE_POLL_MS = 1500

function fmtOffset(ms) {
  if (ms == null) return null
  const s = Math.floor(ms / 1000)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

function fmtDuration(call) {
  if (!call.started_at) return '—'
  const end = call.ended_at || Date.now() / 1000
  const s = Math.max(0, Math.round(end - call.started_at))
  return `${Math.floor(s / 60)}m ${s % 60}s`
}

// ── stats row ────────────────────────────────────────────────────────────

function Stats({ call }) {
  const turns = call.turns || []
  const fired = turns.flatMap((t) => t.verdicts.filter(
    (v) => v.fired && v.severity !== 'info'))
  const byLayer = {}
  fired.forEach((v) => { byLayer[v.detector] = (byLayer[v.detector] || 0) + 1 })
  const order = ['low', 'med', 'high']
  const maxSev = fired.map((v) => v.severity)
    .sort((a, b) => order.indexOf(b) - order.indexOf(a))[0] || null
  const interventions = turns.flatMap((t) => t.actions || [])
    .filter((a) => a.taken !== 'none').length

  return (
    <div className="stats-row">
      <div className="stat">
        <div className="stat-label">flags</div>
        <div className="stat-value">{fired.length}
          <span className="stat-extra">
            {Object.entries(byLayer).map(([l, n]) => (
              <span key={l} className="chip layer">{l}×{n}</span>
            ))}
          </span>
        </div>
      </div>
      <div className="stat">
        <div className="stat-label">max severity</div>
        <div className="stat-value">
          {maxSev ? <span className={`chip sev-${maxSev}`}>{maxSev}</span>
            : <span className="chip clean">clean</span>}
        </div>
      </div>
      <div className="stat">
        <div className="stat-label">interventions</div>
        <div className="stat-value">{interventions}</div>
      </div>
      <div className="stat">
        <div className="stat-label">turns</div>
        <div className="stat-value">{turns.length}</div>
      </div>
      <div className="stat">
        <div className="stat-label">duration</div>
        <div className="stat-value">{fmtDuration(call)}</div>
      </div>
      <div className="stat">
        <div className="stat-label">agent</div>
        <div className="stat-value stat-small">{call.agent_id}
          <span className="dim"> v{call.agent_version}</span></div>
      </div>
    </div>
  )
}

// ── evidence (rendered VERBATIM — the product differentiator) ─────────────

// Review buttons: every flag gets a human ✓/✗ — the loop that feeds the
// MEASURED production-precision metric on the fleet page. No competitor
// exposes a live precision number; this is where ours comes from.
function ReviewButtons({ current, onLabel }) {
  return (
    <span className="review-btns">
      <button className={`review-btn ok ${current === 'confirmed' ? 'active' : ''}`}
              title="confirm: this flag is a real violation"
              onClick={(e) => { e.stopPropagation(); onLabel('confirmed') }}>
        ✓ real
      </button>
      <button className={`review-btn bad ${current === 'rejected' ? 'active' : ''}`}
              title="reject: this flag is a false alarm"
              onClick={(e) => { e.stopPropagation(); onLabel('rejected') }}>
        ✗ false alarm
      </button>
    </span>
  )
}

function EvidenceCard({ verdict, review, onLabel }) {
  const ev = verdict.evidence
  if (!ev) return null
  return (
    <div className={`evidence sev-border-${verdict.severity}`}>
      <div className="evidence-head">
        <span className="chip layer">{verdict.detector}</span>
        <span className={`chip sev-${verdict.severity}`}>{verdict.severity}</span>
        <span className="dim">{ev.claim_type}</span>
        <span className="dim">{verdict.latency_ms?.toFixed(3)} ms</span>
        {verdict.arbitration?.suppressed?.length > 0 && (
          <span className="chip muted">suppressed by {verdict.arbitration.suppressed.join(', ')}</span>
        )}
        {onLabel && <ReviewButtons current={review} onLabel={onLabel} />}
      </div>
      <table className="evidence-table">
        <tbody>
          <tr><th>spoken</th><td className="spoken">{String(ev.spoken_value)}</td></tr>
          <tr><th>truth</th><td className="truth">{String(ev.truth_value)}</td></tr>
          <tr><th>source</th><td className="mono dim">{ev.source}</td></tr>
        </tbody>
      </table>
    </div>
  )
}

// ── one conversation turn ─────────────────────────────────────────────────

function Turn({ turn, onReplay, hasAudio, labels, onLabel }) {
  const flagged = turn.verdicts.filter((v) => v.fired)
  const [open, setOpen] = useState(flagged.length > 0)
  const offset = fmtOffset(turn.audio_offset_ms)
  return (
    <div id={`turn-${turn.turn_id}`}
         className={`turn role-${turn.role} ${flagged.length ? 'flagged' : ''}`}>
      <div className="turn-head" onClick={() => setOpen(!open)}>
        <span className={`role-chip role-${turn.role}`}>{turn.role}</span>
        <span className="transcript">{turn.transcript}</span>
        {flagged.length > 0 && <span className="chip flag">⚑ {flagged.length}</span>}
        {turn.actions?.map((a, i) =>
          a.taken !== 'none' && (
            <span key={i} className="chip action">
              {a.taken}{a.hook ? ` · hook ${a.hook}` : ''}
            </span>
          ))}
        {offset && (
          <a className={`replay ${hasAudio ? '' : 'replay-disabled'}`}
             title={hasAudio ? `replay recording at ${offset}` : 'no recording for this call'}
             onClick={(e) => { e.stopPropagation(); hasAudio && onReplay(turn.audio_offset_ms) }}
          >▶ {offset}</a>
        )}
      </div>
      {open && flagged.map((v) => (
        <EvidenceCard key={v.verdict_id} verdict={v}
          review={labels?.[`verdict:${v.verdict_id}`]}
          onLabel={onLabel && ((label) => onLabel('verdict', v.verdict_id, label))} />
      ))}
      {open && (
        <div className="turn-meta dim">
          snapshot {turn.state_snapshot_id}
          {turn.asr_confidence != null && ` · asr ${turn.asr_confidence}`}
        </div>
      )}
    </div>
  )
}

// ── post-call LLM analysis (optional; OUTSIDE the engine; offline only) ──

function PostCallAnalysis({ call, onDone, labels, onLabel }) {
  const [running, setRunning] = useState(false)
  const [error, setError] = useState(null)
  const audit = call.audit || { analyzed: false, findings: [] }
  const live = call.outcome === 'in_progress'

  const run = async () => {
    setRunning(true); setError(null)
    try {
      await analyzeCall(call.call_id)
      onDone()
    } catch (err) { setError(String(err.message || err)) }
    setRunning(false)
  }

  return (
    <div className="panel audit-panel">
      <div className="panel-head">
        <span className="dim">LLM judge over the stored transcript — never in the live path</span>
        <button className="btn" onClick={run} disabled={running || live}>
          {running ? 'analyzing…' : audit.analyzed ? 're-run analysis' : 'run analysis →'}
        </button>
      </div>
      {live && <p className="dim">available once the call ends.</p>}
      {error && <p className="error">{error}</p>}
      {audit.analyzed && audit.findings.length === 0 && (
        <p className="audit-clean">✓ judge agrees with the inline layers — no missed
          failures, no false alarms.</p>
      )}
      {audit.findings.map((f) => (
        <div key={f.id} className={`audit-finding kind-${f.kind}`}>
          <span className={`chip ${f.kind === 'missed_failure' ? 'flag' : 'muted'}`}>
            {f.kind === 'missed_failure' ? 'missed by inline' : 'inline false alarm'}
          </span>
          {f.category && <span className="chip layer">{f.category}</span>}
          <a className="mono dim turn-link" onClick={() =>
            document.getElementById(`turn-${f.turn_id}`)?.scrollIntoView(
              { behavior: 'smooth', block: 'center' })}>{f.turn_id}</a>
          {onLabel && <ReviewButtons current={labels?.[`finding:${f.id}`]}
            onLabel={(label) => onLabel('finding', String(f.id), label)} />}
          <div className="audit-rationale">{f.rationale}</div>
        </div>
      ))}
    </div>
  )
}

// plivo.com-style numbered section header: "02 ── SIGNAL ──────"
function SectionHead({ no, title, side }) {
  return (
    <div className="section-head">
      <span className="sec-no">{no}</span>
      <span className="sec-title">{title}</span>
      <span className="sec-rule" />
      {side && <span className="sec-side">{side}</span>}
    </div>
  )
}

// ── the page ─────────────────────────────────────────────────────────────

export default function CallDetail({ callId }) {
  const [call, setCall] = useState(null)
  const [error, setError] = useState(null)
  const bottomRef = useRef(null)
  const audioRef = useRef(null)
  const turnCount = useRef(0)

  useEffect(() => {
    setCall(null); turnCount.current = 0
    let alive = true
    let timer = null
    const load = async () => {
      try {
        const data = await fetchCall(callId)
        if (!alive) return
        setCall(data); setError(null)
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

  useEffect(() => {
    if (call && call.turns.length > turnCount.current) {
      turnCount.current = call.turns.length
      if (call.outcome === 'in_progress')
        bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [call])

  if (error && !call) return <p className="error pad">{error}</p>
  if (call === null) return <p className="dim pad">loading…</p>

  const live = call.outcome === 'in_progress'
  const replayAt = (offsetMs) => {
    const audio = audioRef.current
    if (!audio) return
    audio.currentTime = (offsetMs || 0) / 1000
    audio.play()
  }
  // SIGNAL-strip click: seek the recording (when present) AND scroll to the
  // turn. Accepts a turn object (from the strip) or a turn_id string.
  const jump = (turn) => {
    const turnId = typeof turn === 'string' ? turn : turn.turn_id
    if (typeof turn === 'object' && call.has_audio && turn.audio_offset_ms != null)
      replayAt(turn.audio_offset_ms)
    document.getElementById(`turn-${turnId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
  // ✓/✗ review → label saved → optimistic refresh (feeds /stats/precision)
  const labelFlag = (kind, targetId, label) =>
    saveLabel(call.call_id, kind, targetId, label)
      .then(() => fetchCall(callId).then(setCall))
      .catch((e) => setError(e.message))
  // audit-grade evidence packet download (compliance receipt)
  const downloadReceipts = () =>
    fetchReceipts(call.call_id).then((data) => {
      const blob = new Blob([JSON.stringify(data, null, 2)],
                            { type: 'application/json' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `mirror-receipts-${call.call_id}.json`
      a.click()
      URL.revokeObjectURL(a.href)
    }).catch((e) => setError(e.message))

  return (
    <div className="detail">
      <div className="detail-head">
        <h2 className="mono">{call.call_id}</h2>
        {live ? <span className="live-pill big">● LIVE</span>
          : <span className="chip muted">{call.outcome}</span>}
        <span className="dim">{call.channel}</span>
        {!live && (
          <button className="btn btn-receipts" onClick={downloadReceipts}
                  title="audit-grade evidence packet: every violation with its spoken/truth/source receipt, reviews, and interventions">
            ⬇ receipts
          </button>
        )}
      </div>

      <Stats call={call} />

      {call.has_audio && (
        <>
          <SectionHead no="01" title="recording" side="▶ links seek the audio" />
          <div className="panel audio-panel">
            <audio ref={audioRef} controls preload="metadata" className="call-audio"
                   src={`${API_BASE}/calls/${encodeURIComponent(call.call_id)}/audio`} />
          </div>
        </>
      )}

      <SectionHead no={call.has_audio ? '02' : '01'} title="signal" />
      <CallTimeline turns={call.turns} onJump={jump} />

      <SectionHead no={call.has_audio ? '03' : '02'} title="transcript"
                   side={`${call.turns.length} turns`} />
      <div className="conversation">
        {call.turns.map((t) => (
          <Turn key={t.turn_id} turn={t} onReplay={replayAt} hasAudio={!!call.has_audio}
                labels={call.labels} onLabel={labelFlag} />
        ))}
        <div ref={bottomRef} />
      </div>

      <SectionHead no={call.has_audio ? '04' : '03'} title="post-call analysis"
                   side="offline llm judge" />
      <PostCallAnalysis call={call} labels={call.labels} onLabel={labelFlag}
        onDone={() => fetchCall(callId).then(setCall).catch((e) => setError(e.message))} />
    </div>
  )
}
