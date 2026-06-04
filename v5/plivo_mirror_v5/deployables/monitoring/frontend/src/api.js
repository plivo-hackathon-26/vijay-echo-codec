// Thin client for the monitoring backend. PII rule: call_id is the only
// identifier that ever appears in a URL.
// Dev: vite proxies /api → the backend. Prod build: the BACKEND serves the
// built frontend, so the API is same-origin at the root.
const BASE = import.meta.env.DEV ? '/api' : ''

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

export async function fetchStatsOverview(days = 14) {
  const res = await fetch(`${BASE}/stats/overview?days=${days}`)
  if (!res.ok) throw new Error(`GET /stats/overview -> ${res.status}`)
  return res.json()
}

export async function fetchPatterns(minCalls = 2) {
  const res = await fetch(`${BASE}/stats/patterns?min_calls=${minCalls}`)
  if (!res.ok) throw new Error(`GET /stats/patterns -> ${res.status}`)
  return res.json()
}

export async function fetchAgents() {
  const res = await fetch(`${BASE}/agents`)
  if (!res.ok) throw new Error(`GET /agents -> ${res.status}`)
  return res.json()
}

export async function registerAgent(agent) {
  const res = await fetch(`${BASE}/agents`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(agent),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `POST /agents -> ${res.status}`)
  }
  return res.json()
}

export async function setAgentMode(agentId, mode) {
  const res = await fetch(`${BASE}/agents/${encodeURIComponent(agentId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode }),
  })
  if (!res.ok) throw new Error(`PATCH /agents/${agentId} -> ${res.status}`)
  return res.json()
}
