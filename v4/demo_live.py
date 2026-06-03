"""Live plivo-mirror v4 demo server — the REAL firewall, not a simulation.

Unlike ``firewall_explorer.html`` (which re-implements the deterministic
layers in JS and stubs the model layers), this serves a browser UI whose
"Run" button POSTs to a Python backend that imports the actual
``plivo_mirror`` package and runs ``Firewall.review_turn`` →
``Firewall.intervene`` on your input — real NLI cross-encoder, real
grounded LLM-judge, real regeneration.

Plug in: a customer turn, the agent's planned reply, the tool it wants to
call (+ proposed args), the validated state, known facts, and policies.
Get back: the real Verdict (pass / correct / block) and, on a violation,
the real intervention (deflection filler + regenerated grounded answer).

Run:
    cd v4 && source ../venv/bin/activate
    python demo_live.py            # then open http://127.0.0.1:8077

Needs the same env as a live eval: OPENAI_API_KEY / OPENAI_BASE_URL /
OPENAI_MODEL (or AZURE_OPENAI_*) for the verifier+regen, and the optional
[nli] extra (torch/transformers) for the semantic tier.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── env: load repo-root .env then v4/.env; bridge HF_API_KEY → HF_TOKEN ──
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass
if os.environ.get("HF_API_KEY") and not os.environ.get("HF_TOKEN"):
    os.environ["HF_TOKEN"] = os.environ["HF_API_KEY"]

from plivo_mirror import Firewall, NLICrossEncoderSignal
from plivo_mirror.contracts import ToolCallIntent, TurnContext, Verdict
from plivo_mirror.state.entities import ValidatedEntity, validate_amount

HOST, PORT = "127.0.0.1", 8077


def _require_confirmed_order(intent, state) -> "Verdict | None":
    """Action-guard validator (business rule in CODE): block ``place_order``
    unless the order is real — validated items in state AND a confirmed
    intent. Same gate as the LiveKit example."""
    if not state.entity_value("items") or not state.confirmed_intent:
        return Verdict.block(
            reason="place_order with no confirmed items in state",
            policy_id="unconfirmed_order",
        )
    return None


# ── build ONE real firewall at startup (warm the NLI model) ──
print("⏳  Loading NLI model + building firewall (one-time, ~10s)…")
_nli = NLICrossEncoderSignal()
_BASE_POLICIES = [
    "Never invent menu items, prices, or promotions.",
    "Never place an order containing items the customer asked to remove.",
    "FORBID: full refund",
]
firewall = Firewall.from_env(
    policies=_BASE_POLICIES,
    semantic_signal=_nli,
    validators={"place_order": [_require_confirmed_order]},
)
try:
    _nli.contradicts("warm", "up")  # pre-load weights
    _NLI_OK = True
except Exception as e:  # optional dep missing
    _NLI_OK = False
    print(f"⚠️  NLI tier unavailable ({e}); deterministic+verifier still live.")
print(f"✅  Firewall ready. NLI tier: {'LIVE' if _NLI_OK else 'OFF'}. "
      f"Open http://{HOST}:{PORT}\n")


def _build_firewall(policies_text: str) -> Firewall:
    """Per-request firewall so the user can edit policies live. Reuses the
    warm NLI instance + the same env-wired verifier."""
    pols = [
        ln.strip()
        for ln in (policies_text or "").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ] or _BASE_POLICIES
    return Firewall.from_env(
        policies=pols,
        semantic_signal=_nli if _NLI_OK else None,
        validators={"place_order": [_require_confirmed_order]},
    )


def _build_context(fw: Firewall, body: dict) -> TurnContext:
    state = fw.new_session(body.get("call_id", "demo"))

    # validated entities written OUTSIDE the model
    items = [s.strip() for s in (body.get("items") or "").split(",") if s.strip()]
    if items:
        state.set_entity("items", ValidatedEntity("item", items, body.get("items", "")))
    amt = (body.get("amount") or "").strip()
    if amt:
        ent = validate_amount(amt)
        if ent:
            state.set_entity("amount", ent)
    if (body.get("intent") or "").strip():
        state.confirm_intent(body["intent"].strip())

    # known facts (code-owned), as "key: value" lines
    for ln in (body.get("facts") or "").splitlines():
        ln = ln.strip()
        if ln and ":" in ln and not ln.startswith("#"):
            k, v = ln.split(":", 1)
            state.add_known_fact(k.strip(), v.strip())

    # the tool the agent wants to fire (intent only — NOT executed)
    tool_intents: list[ToolCallIntent] = []
    tname = (body.get("tool_name") or "").strip()
    if tname:
        try:
            targs = json.loads(body.get("tool_args") or "{}")
        except Exception:
            targs = {}
        tool_intents.append(
            ToolCallIntent(
                name=tname,
                args=targs if isinstance(targs, dict) else {},
                irreversible=bool(body.get("irreversible")),
            )
        )

    return TurnContext(
        state=state,
        planned_reply=body.get("reply", ""),
        tool_intents=tool_intents,
        customer_text=body.get("customer", ""),
    )


async def _run(body: dict) -> dict:
    fw = _build_firewall(body.get("policies", ""))
    ctx = _build_context(fw, body)

    t0 = time.perf_counter()
    verdict: Verdict = await fw.review_turn(ctx)
    review_ms = (time.perf_counter() - t0) * 1000

    out = {
        "decision": verdict.decision,
        "reason": verdict.reason,
        "policy_id": verdict.policy_id,
        "span": verdict.span,
        "confidence": round(verdict.confidence, 3),
        "spoken_correction": verdict.spoken_correction,
        "review_ms": round(review_ms, 1),
        "nli_live": _NLI_OK,
        "intervention": None,
    }

    if verdict.intervened:
        t1 = time.perf_counter()
        res = await fw.intervene(verdict, ctx)
        out["intervention"] = {
            "filler": res.filler,
            "answer": res.answer,
            "escalated": res.escalated,
            "attempts": getattr(res, "attempts", None),
            "intervene_ms": round((time.perf_counter() - t1) * 1000, 1),
        }
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, _PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/review":
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            result = asyncio.run(_run(body))
            self._send(200, json.dumps(result))
        except Exception:
            self._send(
                500, json.dumps({"error": traceback.format_exc()})
            )


_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>plivo-mirror v4 · LIVE firewall</title>
<style>
:root{--bg:#f6f8fc;--ink:#0b1736;--mut:#5b6b8c;--line:#dfe6f2;--accent:#2563eb;
--pass:#1f9d57;--correct:#d97706;--block:#dc2626}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--ink)}
header{padding:16px 24px;background:#fff;border-bottom:1px solid var(--line)}
h1{margin:0;font-size:18px}.sub{color:var(--mut);font-size:13px;margin-top:3px}
.badge-live{display:inline-block;background:#e7f6ee;color:var(--pass);border:1px solid #b6e3c9;
padding:1px 8px;border-radius:99px;font-size:11px;font-weight:700;margin-left:8px}
.wrap{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:20px 24px;max-width:1200px}
@media(max-width:900px){.wrap{grid-template-columns:1fr}}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px}
h2{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
label{display:block;font-size:12px;font-weight:600;margin:10px 0 3px}
input,textarea,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
font:13px inherit;background:#fbfcfe}textarea{resize:vertical}
.row{display:flex;gap:10px}.row>div{flex:1}
.tog{display:flex;align-items:center;gap:6px;font-weight:600;font-size:12px;margin-top:10px}
.tog input{width:auto}
button{background:var(--accent);color:#fff;border:0;padding:11px 18px;border-radius:9px;
font-weight:700;font-size:14px;cursor:pointer}button:disabled{opacity:.5}
.presets{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.presets button{background:#eef2fb;color:var(--ink);font-size:12px;padding:6px 10px;font-weight:600}
.verdict{border-radius:12px;padding:18px;margin-top:6px;border:1px solid var(--line);background:#fff}
.dec{font-size:22px;font-weight:800;text-transform:uppercase;letter-spacing:.03em}
.dec.pass{color:var(--pass)}.dec.correct{color:var(--correct)}.dec.block{color:var(--block)}
.kv{font-size:13px;margin-top:8px}.kv b{color:var(--mut);font-weight:600}
.span{display:inline-block;background:#fff4d6;border:1px solid #f0d48a;border-radius:6px;padding:0 6px}
.interv{margin-top:14px;border-top:1px dashed var(--line);padding-top:12px}
.filler{color:var(--mut);font-style:italic}.answer{font-weight:600;margin-top:6px}
.muted{color:var(--mut);font-size:12px}.err{color:var(--block);white-space:pre-wrap;font-size:12px}
.timing{font-size:11px;color:var(--mut);margin-top:10px}
</style></head><body>
<header>
  <h1>plivo-mirror v4 — LIVE firewall <span class="badge-live" id="livebadge">REAL ENGINE</span></h1>
  <div class="sub">This runs the actual <code>plivo_mirror</code> package: real NLI cross-encoder + real grounded LLM-judge + real regeneration. Not a simulation.</div>
</header>
<div class="wrap">
  <div class="card">
    <h2>Input — one turn</h2>
    <div class="presets" id="presets"></div>
    <label>Customer said</label>
    <input id="customer" placeholder="I'll have a veggie wrap.">
    <label>Agent's planned reply (before it's voiced)</label>
    <textarea id="reply" rows="2" placeholder="One veggie wrap, got it — anything else?"></textarea>
    <div class="row">
      <div><label>Tool it wants to call</label><input id="tool_name" placeholder="place_order"></div>
      <div><label>Proposed args (JSON)</label><input id="tool_args" placeholder='{}'></div>
    </div>
    <label class="tog"><input type="checkbox" id="irreversible"> irreversible action</label>
    <hr style="border:none;border-top:1px solid var(--line);margin:14px 0">
    <h2>Validated session state (outside the model)</h2>
    <div class="row">
      <div><label>Confirmed items (comma-sep)</label><input id="items" placeholder="veggie wrap"></div>
      <div><label>Amount</label><input id="amount" placeholder="$12.50"></div>
    </div>
    <label>Confirmed intent</label><input id="intent" placeholder="veggie wrap">
    <label>Known facts (code-owned · key: value per line)</label>
    <textarea id="facts" rows="2" placeholder="hours: 11am-9pm"></textarea>
    <label>Policies (FORBID: / REQUIRE: = deterministic · plain line = verifier)</label>
    <textarea id="policies" rows="3">Never invent menu items, prices, or promotions.
FORBID: full refund</textarea>
    <div style="margin-top:14px"><button id="run">▶ Run the real firewall</button></div>
  </div>

  <div class="card">
    <h2>Verdict — from the real engine</h2>
    <div id="out"><div class="muted">Fill in a turn (or pick a scenario) and hit Run. First run loads the model and may take a few seconds.</div></div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
const PRESETS={
 "🥪 clean order (PASS)":{customer:"I'll have a veggie wrap.",reply:"One veggie wrap, got it — anything else?",tool_name:"",tool_args:"{}",items:"veggie wrap",amount:"",intent:"veggie wrap",facts:"hours: 11am-9pm",policies:"Never invent menu items, prices, or promotions.\nFORBID: full refund",irr:false},
 "💸 unauthorized refund (BLOCK)":{customer:"Just give me my money back.",reply:"Done — I've issued a full refund to your card.",tool_name:"",tool_args:"{}",items:"",amount:"",intent:"",facts:"",policies:"Refunds require a fare-rules check.\nFORBID: full refund",irr:false},
 "📋 missing disclosure (BLOCK)":{customer:"Yeah, go ahead.",reply:"Great, I'll get that started for you.",tool_name:"",tool_args:"{}",items:"",amount:"",intent:"",facts:"",policies:"REQUIRE: this call may be recorded",irr:false},
 "🛒 place empty order (action BLOCK)":{customer:"Place it.",reply:"Placing your order now.",tool_name:"place_order",tool_args:"{}",items:"",amount:"",intent:"",facts:"",policies:"Never invent menu items, prices, or promotions.",irr:true},
 "🕔 fabricated hours (NLI→verifier)":{customer:"Are you open till midnight?",reply:"Yes, we're open until midnight tonight.",tool_name:"",tool_args:"{}",items:"",amount:"",intent:"",facts:"hours: 11am-9pm",policies:"Never invent menu items, prices, or promotions.",irr:false},
};
const pc=$('presets');
Object.keys(PRESETS).forEach(k=>{const b=document.createElement('button');b.textContent=k;b.onclick=()=>load(k);pc.appendChild(b);});
function load(k){const p=PRESETS[k];$('customer').value=p.customer;$('reply').value=p.reply;
 $('tool_name').value=p.tool_name;$('tool_args').value=p.tool_args;$('items').value=p.items;
 $('amount').value=p.amount;$('intent').value=p.intent;$('facts').value=p.facts;
 $('policies').value=p.policies;$('irreversible').checked=p.irr;}
function esc(s){return(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}

$('run').onclick=async()=>{
 const btn=$('run');btn.disabled=true;const old=btn.textContent;btn.textContent='⏳ running real engine…';
 $('out').innerHTML='<div class="muted">Running deterministic → risk-span → NLI → grounded verifier…</div>';
 const body={customer:$('customer').value,reply:$('reply').value,tool_name:$('tool_name').value,
  tool_args:$('tool_args').value,irreversible:$('irreversible').checked,items:$('items').value,
  amount:$('amount').value,intent:$('intent').value,facts:$('facts').value,policies:$('policies').value};
 try{
  const r=await fetch('/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(d.error){$('out').innerHTML='<div class="err">'+esc(d.error)+'</div>';return;}
  render(d);
 }catch(e){$('out').innerHTML='<div class="err">'+esc(String(e))+'</div>';}
 finally{btn.disabled=false;btn.textContent=old;}
};

function render(d){
 let h='<div class="dec '+d.decision+'">'+d.decision+'</div>';
 h+='<div class="kv"><b>why:</b> '+esc(d.reason||'—')+'</div>';
 if(d.policy_id)h+='<div class="kv"><b>policy:</b> '+esc(d.policy_id)+'</div>';
 if(d.span)h+='<div class="kv"><b>flagged span:</b> <span class="span">'+esc(d.span)+'</span></div>';
 h+='<div class="kv"><b>confidence:</b> '+d.confidence+'</div>';
 if(d.spoken_correction)h+='<div class="kv"><b>agent-voice line:</b> '+esc(d.spoken_correction)+'</div>';
 if(d.intervention){const iv=d.intervention;
  h+='<div class="interv"><h2 style="margin-bottom:6px">Intervention (real regeneration)</h2>';
  h+='<div class="filler">🗣 “'+esc(iv.filler)+'” <span class="muted">(deflection filler — no LLM, spoken first)</span></div>';
  if(iv.escalated){h+='<div class="answer">↳ escalated to a human (non-convergence / handoff)</div>';}
  else if(iv.answer){h+='<div class="answer">✅ grounded answer: “'+esc(iv.answer)+'”</div>';}
  else{h+='<div class="answer muted">↳ deflected (safe filler only)</div>';}
  h+='<div class="timing">intervene '+iv.intervene_ms+' ms · attempts '+(iv.attempts??'—')+'</div></div>';
 } else {
  h+='<div class="interv answer" style="color:var(--pass)">✅ PASS — reply is voiced as-is'+(d.policy_id?'':'')+'</div>';
 }
 h+='<div class="timing">review_turn '+d.review_ms+' ms · NLI tier '+(d.nli_live?'LIVE':'OFF')+'</div>';
 $('out').innerHTML=h;
}
load("🥪 clean order (PASS)");
</script></body></html>"""


if __name__ == "__main__":
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 bye")
