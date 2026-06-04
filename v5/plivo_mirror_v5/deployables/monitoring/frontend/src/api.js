// Thin client for the monitoring backend. PII rule: call_id is the only
// identifier that ever appears in a URL.
// Dev: vite proxies /api → the backend. Prod build: the BACKEND serves the
// built frontend, so the API is same-origin at the root.
const BASE = import.meta.env.DEV ? '/api' : ''
export const API_BASE = BASE

// Opt-in write auth: when the backend sets MIRROR_API_KEY, reviewers paste
// the key once (localStorage) and every mutating request carries it.
function writeHeaders() {
  const h = { 'Content-Type': 'application/json' }
  const key = localStorage.getItem('mirror_api_key')
  if (key) h['X-API-Key'] = key
  return h
}

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

export async function analyzeCall(callId) {
  const res = await fetch(`${BASE}/calls/${encodeURIComponent(callId)}/analyze`, {
    method: 'POST',
    headers: writeHeaders(),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `POST /calls/${callId}/analyze -> ${res.status}`)
  }
  return res.json()
}

export async function saveLabel(callId, targetKind, targetId, label) {
  const res = await fetch(`${BASE}/calls/${encodeURIComponent(callId)}/labels`, {
    method: 'POST',
    headers: writeHeaders(),
    body: JSON.stringify({ target_kind: targetKind, target_id: targetId, label }),
  })
  if (!res.ok) throw new Error(`POST label -> ${res.status}`)
  return res.json()
}

export async function fetchPrecision() {
  const res = await fetch(`${BASE}/stats/precision`)
  if (!res.ok) throw new Error(`GET /stats/precision -> ${res.status}`)
  return res.json()
}

export async function fetchReceipts(callId) {
  const res = await fetch(`${BASE}/calls/${encodeURIComponent(callId)}/receipts`)
  if (!res.ok) throw new Error(`GET receipts -> ${res.status}`)
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
    headers: writeHeaders(),
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
    headers: writeHeaders(),
    body: JSON.stringify({ mode }),
  })
  if (!res.ok) throw new Error(`PATCH /agents/${agentId} -> ${res.status}`)
  return res.json()
}
