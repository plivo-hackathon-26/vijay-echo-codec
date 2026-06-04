import React, { useEffect, useState } from 'react'
import { fetchCalls } from './api.js'

const POLL_MS = 3000

function SevDot({ severity }) {
  if (!severity) return <span className="sev-dot clean" title="clean" />
  return <span className={`sev-dot ${severity}`} title={`max severity: ${severity}`} />
}

export default function CallList({ selected, onSelect }) {
  const [calls, setCalls] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      fetchCalls()
        .then((data) => alive && (setCalls(data), setError(null)))
        .catch((err) => alive && setError(err.message))
    load()
    const timer = setInterval(load, POLL_MS)
    return () => { alive = false; clearInterval(timer) }
  }, [])

  return (
    <div className="call-list">
      <div className="call-list-head">
        <span>calls</span>
        {calls && <span className="dim">{calls.length}</span>}
      </div>
      {error && <p className="error">backend unreachable: {error}</p>}
      {calls === null && !error && <p className="dim pad">loading…</p>}
      {calls && calls.length === 0 && (
        <p className="dim pad">No calls yet — attach the observer, run the
          simulator, or analyze a recording.</p>
      )}
      {calls?.map((c) => {
        const live = c.outcome === 'in_progress'
        return (
          <div key={c.call_id}
               className={`call-item ${selected === c.call_id ? 'active' : ''}`}
               onClick={() => onSelect(c.call_id)}>
            <div className="call-item-top">
              <SevDot severity={c.max_severity} />
              <span className="mono call-item-id">{c.call_id}</span>
              {live && <span className="live-pill">● live</span>}
            </div>
            <div className="call-item-sub">
              <span className="dim">{c.agent_id} · v{c.agent_version}</span>
            </div>
            <div className="call-item-badges">
              {Object.entries(c.flags_by_layer || {}).map(([layer, n]) => (
                <span key={layer} className="chip layer">{layer}×{n}</span>
              ))}
              {c.flag_count === 0 && <span className="chip clean">clean</span>}
              {c.intervention_count > 0 && (
                <span className="chip action">⚡ {c.intervention_count}</span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
