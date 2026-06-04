import React, { useState } from 'react'
import CallList from './CallList.jsx'
import CallDetail from './CallDetail.jsx'
import FleetView from './FleetView.jsx'
import AgentsView from './AgentsView.jsx'

// TODO: auth + PII redaction — out of scope for v5.
export default function App() {
  // deep link: ?call=<call_id> (PII rule: call_id is the only id in URLs)
  const [selectedCallId, setSelectedCallId_] = useState(
    () => new URLSearchParams(window.location.search).get('call'))
  const [view, setView] = useState('fleet')           // 'fleet' | 'agents'
  const setSelectedCallId = (id) => {
    setSelectedCallId_(id)
    const url = id ? `?call=${encodeURIComponent(id)}` : window.location.pathname
    window.history.replaceState(null, '', url)
  }
  const goHome = (v) => { setSelectedCallId(null); setView(v) }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand" onClick={() => goHome('fleet')}
             style={{ cursor: 'pointer' }} title="fleet overview">
          <span className="brand-mark">◈</span>
          <div>
            <div className="brand-name">plivo-mirror</div>
            <div className="brand-sub">agent output verification</div>
          </div>
        </div>
        <button className={`fleet-link ${selectedCallId === null && view === 'fleet' ? 'active' : ''}`}
                onClick={() => goHome('fleet')}>
          ⌂ fleet overview
        </button>
        <button className={`fleet-link ${selectedCallId === null && view === 'agents' ? 'active' : ''}`}
                onClick={() => goHome('agents')}>
          ⚙ agents & intervene
        </button>
        <CallList selected={selectedCallId} onSelect={setSelectedCallId} />
      </aside>
      <main className="main">
        {selectedCallId !== null ? (
          <CallDetail callId={selectedCallId} />
        ) : view === 'agents' ? (
          <AgentsView />
        ) : (
          <FleetView onSelectCall={setSelectedCallId} />
        )}
      </main>
    </div>
  )
}
