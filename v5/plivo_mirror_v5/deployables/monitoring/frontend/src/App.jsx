import React, { useState } from 'react'
import CallList from './CallList.jsx'
import CallDetail from './CallDetail.jsx'

// TODO: auth + PII redaction — out of scope for v5.
export default function App() {
  const [selectedCallId, setSelectedCallId] = useState(null)

  return (
    <div className="app">
      <header>
        <h1>plivo-mirror</h1>
        <span className="subtitle">agent output verification — live &amp; recent calls</span>
      </header>
      {selectedCallId === null ? (
        <CallList onSelect={setSelectedCallId} />
      ) : (
        <CallDetail callId={selectedCallId} onBack={() => setSelectedCallId(null)} />
      )}
    </div>
  )
}
