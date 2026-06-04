// Thin client for the monitoring backend. PII rule: call_id is the only
// identifier that ever appears in a URL.
const BASE = '/api'

export async function fetchCalls() {
  const res = await fetch(`${BASE}/calls`)
  if (!res.ok) throw new Error(`GET /calls -> ${res.status}`)
  return res.json()
}

export async function fetchCall(callId) {
  const res = await fetch(`${BASE}/calls/${encodeURIComponent(callId)}`)
  if (!res.ok) throw new Error(`GET /calls/${callId} -> ${res.status}`)
  return res.json()
}
