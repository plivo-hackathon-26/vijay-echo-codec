import React, { useEffect, useState } from 'react'
import { fetchAgents, registerAgent, setAgentMode } from './api.js'

const POLL_MS = 5000

function snippet(agentId) {
  return `from plivo_mirror_v5.integrations import attach_mirror

# inside your LiveKit entrypoint, after ctx.connect():
attach_mirror(
    session,
    room_id=ctx.room.name,            # call_id == LiveKit room id
    backend_url="${window.location.origin.replace(/:\d+$/, ':8500')}",
    agent_id="${agentId}",            # ← must match this registration
    agent=my_agent,                   # enables dashboard-toggled intervene
)
await session.start(agent=my_agent, room=ctx.room)`
}

function ago(t) {
  if (!t) return 'never'
  const s = Math.max(0, Date.now() / 1000 - t)
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  return `${Math.round(s / 86400)}d ago`
}

function RegisterForm({ onDone }) {
  const [form, setForm] = useState({ agent_id: '', name: '', system_prompt: '',
                                     facts: '{\n}', policies: '' })
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value })

  const submit = async (e) => {
    e.preventDefault()
    setError(null)
    let facts
    try { facts = form.facts.trim() ? JSON.parse(form.facts) : {} }
    catch { setError('facts must be valid JSON (e.g. {"plan": {"turbo": {"price_per_month": 79.99}}})'); return }
    setBusy(true)
    try {
      await registerAgent({ agent_id: form.agent_id.trim(), name: form.name.trim(),
                            system_prompt: form.system_prompt, facts,
                            policies: form.policies })
      setForm({ agent_id: '', name: '', system_prompt: '', facts: '{\n}', policies: '' })
      onDone()
    } catch (err) { setError(err.message) }
    finally { setBusy(false) }
  }

  return (
    <form className="panel agent-form" onSubmit={submit}>
      <div className="panel-head"><span className="panel-title">register an agent</span>
        <span className="dim">one window — monitoring starts in shadow, flip intervene when ready</span></div>

      <label className="mlabel">agent id</label>
      <input className="inp mono" required value={form.agent_id} onChange={set('agent_id')}
             placeholder="support-bot-prod" pattern="[A-Za-z0-9._-]+" />
      <div className="form-hint dim">Any stable name YOU choose (letters, digits, dot, dash,
        underscore). It just has to match the <code>agent_id=</code> you pass to
        <code> attach_mirror()</code> in your LiveKit worker — the snippet below fills it in.
        The LiveKit room id becomes the call id automatically.</div>

      <label className="mlabel">display name</label>
      <input className="inp" value={form.name} onChange={set('name')} placeholder="Support Bot (prod)" />

      <label className="mlabel">agent system prompt — judge context (recommended)</label>
      <textarea className="inp mono" rows="5" value={form.system_prompt} onChange={set('system_prompt')}
                placeholder="Paste the agent's own system prompt. The judge uses it to know the agent's intended role, scope and rules — so it can tell a violation from intended behaviour." />

      <label className="mlabel">facts — ground truth JSON (judge + deterministic diff)</label>
      <textarea className="inp mono" rows="6" value={form.facts} onChange={set('facts')} />
      <div className="form-hint dim">{'e.g. {"plan": {"turbo": {"price_per_month": 79.99}}, "policy": {"refund_window_days": 30}}'}</div>

      <label className="mlabel">policies — one rule per line</label>
      <textarea className="inp mono" rows="4" value={form.policies} onChange={set('policies')}
                placeholder={'Never promise refunds over $50 without a supervisor.\nAlways state the recording disclosure at call start.'} />

      {error && <div className="error">{error}</div>}
      <div><button className="btn" disabled={busy}>{busy ? 'saving…' : 'register agent'}</button></div>
    </form>
  )
}

function AgentCard({ agent, onToggle }) {
  const [showSnippet, setShowSnippet] = useState(false)
  const [copied, setCopied] = useState(false)
  const intervene = agent.mode === 'intervene'

  const copy = () => {
    navigator.clipboard.writeText(snippet(agent.agent_id))
    setCopied(true); setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="panel agent-card">
      <div className="panel-head">
        <span className="panel-title mono">{agent.name || agent.agent_id}</span>
        <span className="chip muted">{agent.agent_id}</span>
        {!agent.registered && <span className="chip flag">seen, not registered</span>}
        <span className="dim mono" style={{ marginLeft: 'auto' }}>
          {agent.calls || 0} calls · {agent.flagged || 0} flagged · last {ago(agent.last_seen)}
        </span>
      </div>
      {agent.registered && (
        <div className="agent-actions">
          <label className={`mode-toggle ${intervene ? 'on' : ''}`}
                 title="shadow: watch + flag only · intervene: Hook A corrections on the next call">
            <input type="checkbox" checked={intervene}
                   onChange={() => onToggle(agent.agent_id, intervene ? 'shadow' : 'intervene')} />
            <span className="mode-label mono">{intervene ? 'INTERVENE ON' : 'shadow only'}</span>
          </label>
          <a className="replay" onClick={() => setShowSnippet(!showSnippet)}>
            {showSnippet ? 'hide snippet ▴' : 'integration snippet ▾'}
          </a>
        </div>
      )}
      {showSnippet && (
        <div className="snippet-box">
          <pre className="mono">{snippet(agent.agent_id)}</pre>
          <button className="btn" onClick={copy}>{copied ? 'copied ✓' : 'copy'}</button>
        </div>
      )}
    </div>
  )
}

export default function AgentsView() {
  const [agents, setAgents] = useState(null)
  const [error, setError] = useState(null)

  const load = () => fetchAgents().then((a) => { setAgents(a); setError(null) })
    .catch((err) => setError(err.message))
  useEffect(() => {
    load()
    const t = setInterval(load, POLL_MS)
    return () => clearInterval(t)
  }, [])

  const toggle = async (id, mode) => { await setAgentMode(id, mode); load() }

  return (
    <div className="detail">
      <div className="detail-head"><h2>agents</h2>
        <span className="mlabel">register once · connect from livekit · toggle intervene here</span></div>
      {error && <div className="error pad">{error}</div>}
      <RegisterForm onDone={load} />
      <div className="section-head"><span className="sec-no">01</span>
        <span className="sec-title">registered & connected agents</span><span className="sec-rule" /></div>
      {agents === null ? <div className="dim">loading…</div>
        : agents.length === 0 ? <div className="panel dim">No agents yet — register one above,
            drop the snippet into your LiveKit worker, and calls appear in the sidebar the moment it connects.</div>
        : agents.map((a) => <AgentCard key={a.agent_id} agent={a} onToggle={toggle} />)}
    </div>
  )
}
