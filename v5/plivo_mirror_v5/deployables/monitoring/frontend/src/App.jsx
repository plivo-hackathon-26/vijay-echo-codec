import React, { useState } from 'react'
import CallList from './CallList.jsx'
import CallDetail from './CallDetail.jsx'

// TODO: auth + PII redaction — out of scope for v5.
export default function App() {
  const [selectedCallId, setSelectedCallId] = useState(null)

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">◈</span>
          <div>
            <div className="brand-name">plivo-mirror</div>
            <div className="brand-sub">agent output verification</div>
          </div>
        </div>
        <CallList selected={selectedCallId} onSelect={setSelectedCallId} />
      </aside>
      <main className="main">
        {selectedCallId === null ? (
          <div className="empty-state">
            <div className="empty-mark">◈</div>
            <h2>Select a call</h2>
            <p className="dim">
              Live calls stream in on the left with a pulsing dot.<br />
              Flagged turns show spoken-vs-truth evidence; ended calls can be<br />
              replayed and audited with the post-call AI analysis.
            </p>
          </div>
        ) : (
          <CallDetail callId={selectedCallId} />
        )}
      </main>
    </div>
  )
}
