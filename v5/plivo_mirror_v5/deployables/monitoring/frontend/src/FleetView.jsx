import React, { useEffect, useState } from 'react'
import { fetchStatsOverview, fetchPatterns, fetchPrecision } from './api.js'

const POLL_MS = 5000

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

function pct(x) { return `${(x * 100).toFixed(1)}%` }

// Per-day calls (grey) with flagged overlay (red) — pure SVG, no deps.
function TrendChart({ daily }) {
  const W = 660; const H = 96; const PAD = 2
  const max = Math.max(1, ...daily.map((d) => d.calls))
  const bw = Math.min(38, (W - PAD * 2) / Math.max(daily.length, 1) - 6)
  return (
    <svg className="trend" viewBox={`0 0 ${W} ${H + 18}`} preserveAspectRatio="none">
      {daily.map((d, i) => {
        const x = PAD + i * ((W - PAD * 2) / daily.length)
        const ch = (d.calls / max) * H
        const fh = ((d.flagged || 0) / max) * H
        return (
          <g key={d.day}>
            <rect x={x} y={H - ch} width={bw} height={ch} fill="var(--line)" rx="1.5">
              <title>{d.day}: {d.calls} calls</title>
            </rect>
            <rect x={x} y={H - fh} width={bw} height={fh} fill="var(--high)" rx="1.5" opacity=".85">
              <title>{d.day}: {d.flagged} flagged</title>
            </rect>
            <text x={x + bw / 2} y={H + 13} textAnchor="middle" className="trend-x">
              {d.day.slice(5)}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

function CategoryBars({ categories }) {
  const max = Math.max(1, ...categories.map((c) => c.hits))
  return (
    <div className="cat-bars">
      {categories.map((c) => (
        <div className="cat-row" key={`${c.category}-${c.detector}`}>
          <span className="cat-name mono">{c.category || 'uncategorised'}</span>
          <span className={`chip layer ${c.detector === 'JUDGE' ? 'action' : ''}`}>{c.detector}</span>
          <div className="cat-track">
            <div className="cat-fill" style={{ width: `${(c.hits / max) * 100}%` }} />
          </div>
          <span className="cat-n mono">{c.hits} hits · {c.calls} calls</span>
        </div>
      ))}
      {categories.length === 0 && <div className="dim">no flagged turns in window</div>}
    </div>
  )
}

function ago(t) {
  if (!t) return '—'
  const s = Math.max(0, Date.now() / 1000 - t)
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

export default function FleetView({ onSelectCall }) {
  const [stats, setStats] = useState(null)
  const [patterns, setPatterns] = useState(null)
  const [precision, setPrecision] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      Promise.all([fetchStatsOverview(), fetchPatterns(), fetchPrecision()])
        .then(([s, p, pr]) => alive && (
          setStats(s), setPatterns(p), setPrecision(pr), setError(null)))
        .catch((err) => alive && setError(err.message))
    load()
    const timer = setInterval(load, POLL_MS)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  if (error) return <div className="pad error">backend unreachable: {error}</div>
  if (!stats) return <div className="pad dim">loading fleet…</div>

  const sysCount = (patterns?.fact_patterns?.length || 0) + (patterns?.judge_clusters?.length || 0)

  return (
    <div className="detail fleet">
      <div className="detail-head">
        <h2>fleet</h2>
        <span className="mlabel">last {stats.window_days} days</span>
      </div>

      <div className="stats-row">
        <div className="stat"><div className="stat-label">calls</div>
          <div className="stat-value">{stats.calls}</div></div>
        <div className="stat"><div className="stat-label">flagged calls</div>
          <div className="stat-value" style={{ color: stats.flagged_calls ? 'var(--high)' : 'var(--ok)' }}>
            {stats.flagged_calls}</div></div>
        <div className="stat"><div className="stat-label">flag rate</div>
          <div className="stat-value">{pct(stats.flag_rate)}</div></div>
        <div className="stat"><div className="stat-label">interventions</div>
          <div className="stat-value">{stats.interventions}</div></div>
        <div className="stat"><div className="stat-label">audited</div>
          <div className="stat-value">{stats.audited_calls}
            <span className="dim stat-small">/ {stats.judge_flagged_calls} judge-flagged</span></div></div>
        <div className="stat"><div className="stat-label">systemic patterns</div>
          <div className="stat-value" style={{ color: sysCount ? 'var(--high)' : 'var(--ok)' }}>{sysCount}</div></div>
        <div className="stat"
             title="MEASURED on your traffic from reviewer ✓/✗ labels on flags — not a benchmark claim. Review flags in any call to feed it.">
          <div className="stat-label">measured precision</div>
          <div className="stat-value">
            {precision?.precision != null
              ? <span style={{ color: precision.precision >= 0.9 ? 'var(--ok)' : 'var(--med)' }}>
                  {(precision.precision * 100).toFixed(0)}%</span>
              : <span className="dim stat-small">review flags →</span>}
            {precision?.reviewed > 0 && (
              <span className="dim stat-small">{precision.confirmed}✓ {precision.rejected}✗</span>
            )}
          </div>
        </div>
      </div>

      <SectionHead no="01" title="systemic failures" side="same wrong fact, many calls" />
      {patterns && patterns.fact_patterns.length > 0 ? patterns.fact_patterns.map((p) => (
        <div className="panel pattern" key={`${p.source}|${p.spoken_value}`}>
          <div className="panel-head">
            <span className="panel-title" style={{ color: 'var(--high)' }}>
              {p.claim_type || 'fact'} · {p.calls} calls · {p.hits} occurrences
            </span>
            <span className="dim mono">first {ago(p.first_seen)} · last {ago(p.last_seen)}</span>
          </div>
          <table className="evidence-table"><tbody>
            <tr><th>agent says</th><td className="spoken">{p.spoken_value}</td></tr>
            <tr><th>truth</th><td className="truth">{p.truth_value}</td></tr>
            <tr><th>source</th><td className="dim">{p.source}</td></tr>
          </tbody></table>
          <div className="receipts">
            <span className="mlabel">receipts</span>
            {p.call_ids.map((id) => (
              <button key={id} className="chip receipt" onClick={() => onSelectCall(id)}>{id}</button>
            ))}
          </div>
          <div className="pattern-hint dim">
            Same wrong value against the same source across {p.calls} calls —
            this is a prompt/config bug, not a one-off.
          </div>
        </div>
      )) : <div className="panel dim">No repeated wrong-fact patterns. One-off flags appear under categories below.</div>}

      {patterns && patterns.judge_clusters.length > 0 && patterns.judge_clusters.map((c) => (
        <div className="panel pattern" key={c.category}>
          <div className="panel-head">
            <span className="panel-title" style={{ color: 'var(--med)' }}>
              {c.category} · {c.calls} calls (post-call judge)
            </span>
            <span className="dim mono">first {ago(c.first_seen)} · last {ago(c.last_seen)}</span>
          </div>
          <div className="receipts">
            <span className="mlabel">receipts</span>
            {c.call_ids.map((id) => (
              <button key={id} className="chip receipt" onClick={() => onSelectCall(id)}>{id}</button>
            ))}
          </div>
        </div>
      ))}

      <SectionHead no="02" title="violation trend" side="grey calls · red flagged" />
      <div className="panel">
        {stats.daily.length > 0
          ? <TrendChart daily={stats.daily} />
          : <span className="dim">no calls in window</span>}
      </div>

      <SectionHead no="03" title="failure categories" side="engine + judge" />
      <div className="panel"><CategoryBars categories={stats.categories} /></div>

      <SectionHead no="04" title="agent versions" side="did the new prompt regress?" />
      <div className="panel">
        <table className="ver-table">
          <thead><tr>
            <th>agent</th><th>version</th><th>calls</th><th>flagged</th><th>flag rate</th>
          </tr></thead>
          <tbody>
            {stats.versions.map((v) => (
              <tr key={`${v.agent_id}-${v.agent_version}`}>
                <td className="mono">{v.agent_id}</td>
                <td className="mono">{v.agent_version}</td>
                <td className="mono">{v.calls}</td>
                <td className="mono" style={{ color: v.flagged ? 'var(--high)' : 'var(--ok)' }}>{v.flagged}</td>
                <td className="mono">{pct(v.flag_rate)}</td>
              </tr>
            ))}
            {stats.versions.length === 0 && (
              <tr><td colSpan="5" className="dim">no calls yet</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
