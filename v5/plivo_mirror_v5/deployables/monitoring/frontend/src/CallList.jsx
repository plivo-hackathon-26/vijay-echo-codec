import React, { useEffect, useState } from 'react'
import { fetchCalls } from './api.js'

const POLL_MS = 3000

function SeverityBadge({ severity }) {
  if (!severity) return <span className="badge clean">clean</span>
  return <span className={`badge sev-${severity}`}>{severity}</span>
}

export default function CallList({ onSelect }) {
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

  if (error) return <p className="error">backend unreachable: {error}</p>
  if (calls === null) return <p>loading…</p>
  if (calls.length === 0) return <p>No calls yet — replay a fixture or attach the observer.</p>

  return (
    <table className="call-list">
      <thead>
        <tr>
          <th>call</th><th>agent</th><th>status</th>
          <th>flags</th><th>max severity</th><th>interventions</th>
        </tr>
      </thead>
      <tbody>
        {calls.map((c) => (
          <tr key={c.call_id} onClick={() => onSelect(c.call_id)}>
            <td className="mono">{c.call_id}</td>
            <td>{c.agent_id} <span className="dim">v{c.agent_version}</span></td>
            <td>{c.outcome}</td>
            <td>
              {Object.entries(c.flags_by_layer || {}).map(([layer, n]) => (
                <span key={layer} className="badge layer">{layer}×{n}</span>
              ))}
              {c.flag_count === 0 && <span className="dim">0</span>}
            </td>
            <td><SeverityBadge severity={c.max_severity} /></td>
            <td>{c.intervention_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
