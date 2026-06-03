export const meta = {
  name: 'v4-autotune',
  description: 'Hill-climb the v4 firewall on its eval metrics: understand the code, baseline live eval, diagnose failures, research fixes, then trial candidate changes one at a time on a sandbox branch (live eval each, keep iff catch-rate rises and false-intervention does not get worse, else revert). Nothing touches main.',
  phases: [
    { title: 'Setup', detail: 'fresh sandbox branch off main; abort if tree is dirty' },
    { title: 'Understand', detail: 'parallel readers map every v4 subsystem + the harness' },
    { title: 'Baseline', detail: 'one live eval -> baseline scorecard' },
    { title: 'Diagnose', detail: 'failure-analyst buckets the misses & false alarms' },
    { title: 'Research', detail: 'web research on methods for the top failure buckets' },
    { title: 'Plan', detail: 'synthesize a ranked queue of single-lever candidate fixes' },
    { title: 'Hill-climb', detail: 'sequential: apply -> tests -> live eval -> keep/revert' },
    { title: 'Report', detail: 'summary of kept changes + branch state for your approval' },
  ],
}

// ───────────────────────── config (override via Workflow args) ─────────────
const REPO = '/Users/vijay.krishna/Desktop/vijay-echo-codec'
const cfg = Object.assign({
  branch: 'v4-autotune',
  induced: '../v3/datasets/eval_v2.jsonl',     // matches the latest baseline scorecard
  golden: 'datasets/golden_v1.jsonl',
  policies: '../v3/datasets/policies_v1.txt',
  facts: 'datasets/facts_v1.json',
  nli: true,
  limit: null,            // set e.g. 30 for a fast/cheap smoke run (changes denominators)
  maxCandidates: 6,       // upper bound on trials (each trial = one paid live eval)
}, (args && typeof args === 'object') ? args : {})

// Build the canonical v4 LIVE eval command. Writes the scorecard JSON to `out`.
// `;` (not `&&`) so a missing venv/activate never short-circuits the chain.
const evalCmd = (out) =>
  `cd ${REPO}; source venv/bin/activate 2>/dev/null; set -a; . ./.env 2>/dev/null; set +a; cd v4; ` +
  `PYTHONPATH=. python -m plivo_mirror.eval ` +
  `--induced ${cfg.induced} --golden ${cfg.golden} --policies ${cfg.policies} --facts ${cfg.facts} ` +
  `${cfg.nli ? '--nli ' : ''}${cfg.limit ? `--limit ${cfg.limit} ` : ''}` +
  `--mode live --model "$OPENAI_MODEL" --json > ${out}`

// Restore the working tree to the branch's last commit (drops a losing trial).
// Scoped clean so it never deletes our scorecard_autotune_*.json artifacts.
const REVERT = `cd ${REPO}; git checkout -- .; git clean -fd v4/plivo_mirror v4/datasets`

// ───────────────────────── schemas ─────────────────────────────────────────
const SETUP = { type: 'object', required: ['ok', 'branch'], properties: {
  ok: { type: 'boolean' }, branch: { type: 'string' },
  head: { type: 'string' }, note: { type: 'string' } } }

const UNDERSTAND = { type: 'object', required: ['component', 'role', 'tunables', 'weaknesses'], properties: {
  component: { type: 'string' }, role: { type: 'string' },
  current_approach: { type: 'string' },
  tunables: { type: 'array', items: { type: 'object', properties: {
    name: { type: 'string' }, location: { type: 'string' }, current_value: { type: 'string' } } } },
  weaknesses: { type: 'array', items: { type: 'string' } } } }

const SCORECARD = { type: 'object', required: ['ok', 'catch_rate', 'golden_fi_rate'], properties: {
  ok: { type: 'boolean' },
  catch_rate: { type: 'number' }, caught: { type: 'number' }, missed: { type: 'number' },
  n_induced: { type: 'number' }, missed_at_gate: { type: 'number' }, missed_at_verifier: { type: 'number' },
  golden_fi_rate: { type: 'number' }, golden_fired: { type: 'number' },
  near_miss_fi_rate: { type: 'number' },
  verifier_hit_rate: { type: 'number' }, lexicon_fire_rate: { type: 'number' },
  latency_p95_ms: { type: ['number', 'null'] }, latency_note: { type: 'string' },
  raw_path: { type: 'string' }, problem: { type: 'string' } } }

const DIAGNOSIS = { type: 'object', required: ['top_buckets'], properties: {
  top_buckets: { type: 'array', items: { type: 'object', required: ['category', 'root_cause'], properties: {
    category: { type: 'string' }, miss_count: { type: 'number' },
    where: { type: 'string', enum: ['gate', 'verifier', 'false_intervention', 'mixed'] },
    root_cause: { type: 'string' }, lever: { type: 'string' } } } },
  summary: { type: 'string' } } }

const RESEARCH = { type: 'object', required: ['category', 'methods'], properties: {
  category: { type: 'string' },
  methods: { type: 'array', items: { type: 'object', required: ['name', 'how_to_apply'], properties: {
    name: { type: 'string' }, summary: { type: 'string' },
    how_to_apply: { type: 'string' }, source_url: { type: 'string' } } } } } }

const PLAN = { type: 'object', required: ['candidates'], properties: {
  candidates: { type: 'array', items: { type: 'object', required: ['id', 'title', 'hypothesis', 'files', 'change'], properties: {
    id: { type: 'string' }, title: { type: 'string' }, hypothesis: { type: 'string' },
    files: { type: 'array', items: { type: 'string' } }, change: { type: 'string' },
    metric_targeted: { type: 'string' }, fi_risk: { type: 'string', enum: ['low', 'medium', 'high'] } } } } } }

const IMPL = { type: 'object', required: ['applied', 'tests_pass'], properties: {
  applied: { type: 'boolean' }, tests_pass: { type: 'boolean' },
  files_changed: { type: 'array', items: { type: 'string' } },
  diff_summary: { type: 'string' }, test_output_tail: { type: 'string' } } }

const GUARD = { type: 'object', required: ['verdict'], properties: {
  verdict: { type: 'string', enum: ['clean', 'coupled'] }, findings: { type: 'string' } } }

const COMMIT = { type: 'object', required: ['sha'], properties: {
  sha: { type: 'string' }, message: { type: 'string' } } }

const DONE = { type: 'object', properties: { ok: { type: 'boolean' }, note: { type: 'string' } } }

// ───────────────────────── 0. Setup: sandbox branch ────────────────────────
phase('Setup')
const setup = await agent(
  `Create a clean sandbox branch for an automated tuning run. DO NOT touch main beyond reading it; DO NOT pull.\n` +
  `1. cd ${REPO}\n` +
  `2. Run 'git status --porcelain'. If it shows ANY changes, STOP: return {ok:false, note:"working tree dirty"} and do nothing else.\n` +
  `3. git checkout main\n` +
  `4. Create branch '${cfg.branch}' off main and switch to it. If a branch named '${cfg.branch}' already exists, create '${cfg.branch}-2' (or -3...) instead and use that.\n` +
  `Return {ok:true, branch:<the name you actually created>, head:<short sha>}.`,
  { phase: 'Setup', schema: SETUP })

if (!setup || !setup.ok) {
  log(`Setup aborted: ${setup ? setup.note : 'agent failed'}. Commit/stash your work and re-run.`)
  return { aborted: true, reason: setup ? setup.note : 'setup agent failed' }
}
const BRANCH = setup.branch
log(`Sandbox branch: ${BRANCH} (off main @ ${setup.head}). main stays untouched.`)

// ───────────────────────── 1. Understand the codebase ──────────────────────
phase('Understand')
const AREAS = [
  { k: 'guards',           p: 'v4/plivo_mirror/guards (risk-span tagger, router, speech guard, action guard)' },
  { k: 'verifier',         p: 'v4/plivo_mirror/verifier (grounded LLM judge + the NLI semantic tier)' },
  { k: 'policy',           p: 'v4/plivo_mirror/policy (policy compiler) and the policy txt files used by eval' },
  { k: 'intervention',     p: 'v4/plivo_mirror/intervention (correction packet, regenerate, templating)' },
  { k: 'state+authz',      p: 'v4/plivo_mirror/state and v4/plivo_mirror/authz' },
  { k: 'runtime+firewall', p: 'v4/plivo_mirror/runtime, v4/plivo_mirror/adapters, and v4/plivo_mirror/firewall.py' },
  { k: 'harness+data',     p: 'v4/plivo_mirror/eval.py and the datasets (v4/datasets + ../v3/datasets it consumes)' },
]
const understanding = (await parallel(AREAS.map(a => () =>
  agent(
    `Read and explain ${a.p}.\n` +
    `Return: its role, the current approach/algorithm, the concrete TUNABLES it exposes (thresholds, lexicons/word-lists, prompt text, routing config) with file:line and current value, and SUSPECTED weaknesses that could cause MISSED violations (low catch rate) or FALSE interventions. Be specific.`,
    { label: `understand:${a.k}`, phase: 'Understand', schema: UNDERSTAND, agentType: 'Explore' })
))).filter(Boolean)

// ───────────────────────── 2. Baseline live eval ───────────────────────────
phase('Baseline')
const baseline = await agent(
  `Run the v4 LIVE eval and return the scorecard. This calls the real Azure judge and takes several minutes — use a generous Bash timeout (up to 10 min); if it risks timing out, run it with run_in_background and poll until the output file is written.\n` +
  `Command (run exactly):\n\`\`\`\n${evalCmd('scorecard_autotune_baseline.json')}\n\`\`\`\n` +
  `Then read ${REPO}/v4/scorecard_autotune_baseline.json and map it to the schema: catch_rate & caught/missed/missed_at_gate/missed_at_verifier come from the "induced" block; golden_fi_rate/golden_fired from "golden"; near_miss_fi_rate from "induced_near_miss"; verifier_hit_rate/lexicon_fire_rate top-level; latency from the "latency" block (extract a p95 ms number from time_to_corrected_answer_flagged if present, else null). Set raw_path to the file. If live numbers are ABSENT, set ok:false and explain in 'problem'.`,
  { phase: 'Baseline', schema: SCORECARD })

if (!baseline || !baseline.ok) {
  log(`Baseline live eval failed: ${baseline ? baseline.problem : 'agent failed'}. Cannot hill-climb without a baseline.`)
  return { aborted: true, reason: 'baseline eval failed', branch: BRANCH, detail: baseline }
}
log(`Baseline — catch ${(baseline.catch_rate * 100).toFixed(0)}% | FI(golden) ${(baseline.golden_fi_rate * 100).toFixed(0)}% | near-miss FI ${((baseline.near_miss_fi_rate ?? 0) * 100).toFixed(0)}% | verifier-hit ${((baseline.verifier_hit_rate ?? 0) * 100).toFixed(0)}%`)

// ───────────────────────── 3. Diagnose where it fails ──────────────────────
phase('Diagnose')
const diagnosis = await agent(
  `The baseline LIVE scorecard is at v4/scorecard_autotune_baseline.json. Pinpoint WHERE the firewall fails.\n` +
  `- Bucket the missed induced violations by category AND by stage: missed_at_gate (no risk span tagged / no deterministic hit → verifier never consulted; this is the routing/lexicon ceiling) vs missed_at_verifier (flagged but the judge ruled it supported).\n` +
  `- Bucket the golden false-interventions by category.\n` +
  `For each top bucket give the root cause and the single most promising lever. Use the component map for grounding:\n${JSON.stringify(understanding).slice(0, 6000)}`,
  { phase: 'Diagnose', schema: DIAGNOSIS, agentType: 'eval-failure-analyst' })
log(`Diagnosed ${diagnosis.top_buckets.length} failure buckets. Top: ${diagnosis.top_buckets.slice(0, 3).map(b => b.category).join(', ')}`)

// ───────────────────────── 4. Research fixes (web) ─────────────────────────
phase('Research')
const research = (await parallel(diagnosis.top_buckets.slice(0, 3).map(b => () =>
  agent(
    `Use WebSearch/WebFetch. Research concrete, implementable techniques to improve a real-time policy firewall for LLM voice agents on this failure mode:\n` +
    `  category: "${b.category}"\n  root cause: "${b.root_cause}"\n` +
    `The firewall pipeline is: risk-span/lexicon tagger + deterministic checks → router → grounded LLM-as-judge (entailment over FACTS+POLICIES), with an optional cross-encoder NLI tier. Favor methods that drop into THAT shape (better risk-span detection, NLI thresholding/calibration, grounded entailment prompting, retrieval grounding, policy compilation). Avoid anything requiring a fine-tune.\n` +
    `Return 2-4 methods, each with a one-line how-to-apply for this codebase and a source URL.`,
    { label: `research:${b.category}`.slice(0, 40), phase: 'Research', schema: RESEARCH })
))).filter(Boolean)

// ───────────────────────── 5. Plan: ranked candidate queue ─────────────────
phase('Plan')
const plan = await agent(
  `Synthesize a RANKED queue of AT MOST ${cfg.maxCandidates} candidate fixes for the v4 firewall, best-first by (expected catch-rate gain, low false-intervention risk, low effort).\n` +
  `Each candidate is ONE coherent lever: name the exact files to edit, the hypothesis, which metric it should move, and the FI-regression/overfit risk.\n` +
  `HARD RULES: business logic in CODE not prompts; never hardcode or pattern-match the eval cases' answers; never add vertical coupling (pizza/travel/health). A change must generalize.\n\n` +
  `Component map:\n${JSON.stringify(understanding).slice(0, 5000)}\n\nDiagnosis:\n${JSON.stringify(diagnosis)}\n\nResearch:\n${JSON.stringify(research).slice(0, 5000)}`,
  { phase: 'Plan', schema: PLAN })
log(`Planned ${plan.candidates.length} candidate fixes. Beginning sequential trials...`)

// ───────────────────────── 6. Hill-climb (sequential) ──────────────────────
phase('Hill-climb')
let best = baseline                 // current accepted metrics; rises as we keep changes
const trials = []

const improves = (sc, ref) => {
  if (!sc || !sc.ok) return false
  const catchUp = sc.catch_rate > ref.catch_rate
  const fiOk = sc.golden_fi_rate <= ref.golden_fi_rate
  if (catchUp && fiOk) return true                       // primary rule
  // tie-breaks: same catch but fewer false alarms, or same-or-better both + faster
  const catchSame = sc.catch_rate >= ref.catch_rate
  const fiBetter = sc.golden_fi_rate < ref.golden_fi_rate
  if (catchSame && fiBetter) return true
  const faster = sc.latency_p95_ms != null && ref.latency_p95_ms != null && sc.latency_p95_ms < ref.latency_p95_ms
  if (catchSame && fiOk && faster) return true
  return false
}

for (let i = 0; i < Math.min(plan.candidates.length, cfg.maxCandidates); i++) {
  const c = plan.candidates[i]
  log(`Trial ${i + 1}/${plan.candidates.length}: ${c.title}`)

  // 6a. implement + unit tests
  const impl = await agent(
    `On the CURRENT git branch (${BRANCH} — NOT main), implement this candidate fix minimally and exactly. Touch only what's needed.\n` +
    `Candidate: ${JSON.stringify(c)}\n\n` +
    `Constraints: business logic in code not prompts; do NOT hardcode/pattern-match eval-case answers; do NOT add vertical coupling. The change must generalize to any voice agent.\n` +
    `Then run the v4 unit tests and report pass/fail:\n` +
    `\`\`\`\ncd ${REPO}; source venv/bin/activate 2>/dev/null; cd v4; PYTHONPATH=. python -m pytest tests/ -q\n\`\`\`\n` +
    `Return files_changed, a short diff_summary, tests_pass, and the test_output_tail.`,
    { label: `apply:${c.id}`, phase: 'Hill-climb', schema: IMPL })

  if (!impl || !impl.applied || !impl.tests_pass) {
    await agent(`Discard ALL uncommitted changes to restore the branch to its last commit:\n\`\`\`\n${REVERT}\n\`\`\`\nReturn {ok:true} when the tree is clean.`,
      { label: `revert:${c.id}`, phase: 'Hill-climb', schema: DONE })
    trials.push({ candidate: c, decision: 'revert', reason: impl ? (impl.applied ? 'unit tests failed' : 'could not apply') : 'apply agent failed', impl: impl || null })
    log(`  ↳ reverted (${impl && impl.applied ? 'tests failed' : 'apply failed'})`)
    continue
  }

  // 6b. generalization / overfit review (recorded; informs the final report)
  const guard = await agent(
    `Review the current working-tree diff on branch ${BRANCH} for domain-coupling, vertical-specific assumptions, or overfitting to the eval set leaking into the GENERIC firewall core. Verdict 'clean' or 'coupled' with specifics.`,
    { label: `guard:${c.id}`, phase: 'Hill-climb', schema: GUARD, agentType: 'generalization-guard' })

  // 6c. live eval on the changed tree
  const sc = await agent(
    `Run the v4 LIVE eval on the CURRENT working tree (changes applied, uncommitted). Several minutes; generous timeout or run_in_background.\n` +
    `\`\`\`\n${evalCmd(`scorecard_autotune_trial_${i + 1}.json`)}\n\`\`\`\n` +
    `Read ${REPO}/v4/scorecard_autotune_trial_${i + 1}.json and map to the schema (same mapping as the baseline). Set ok:false if live numbers are ABSENT.`,
    { label: `eval:${c.id}`, phase: 'Hill-climb', schema: SCORECARD })

  // 6d. keep or revert
  if (improves(sc, best)) {
    const msg = `v4-autotune: ${c.title} (catch ${(best.catch_rate * 100).toFixed(0)}%->${(sc.catch_rate * 100).toFixed(0)}%, FI ${(best.golden_fi_rate * 100).toFixed(0)}%->${(sc.golden_fi_rate * 100).toFixed(0)}%)`
    const commit = await agent(
      `Commit the current changes to THIS sandbox branch ONLY (you are on ${BRANCH}; never commit to or merge into main). Stage v4/ changes and commit with EXACTLY this message:\n${msg}\nReturn the short sha.`,
      { label: `commit:${c.id}`, phase: 'Hill-climb', schema: COMMIT })
    trials.push({ candidate: c, decision: 'keep', before: best, after: sc, guard, commit })
    log(`  ↳ KEPT — catch ${(best.catch_rate * 100).toFixed(0)}%→${(sc.catch_rate * 100).toFixed(0)}%, FI ${(best.golden_fi_rate * 100).toFixed(0)}%→${(sc.golden_fi_rate * 100).toFixed(0)}% [${commit ? commit.sha : 'committed'}]${guard && guard.verdict === 'coupled' ? '  ⚠ generalization-guard: coupled' : ''}`)
    best = sc                                            // hill-climb: raise the bar
  } else {
    await agent(`Discard ALL uncommitted changes:\n\`\`\`\n${REVERT}\n\`\`\`\nReturn {ok:true} when clean.`,
      { label: `revert:${c.id}`, phase: 'Hill-climb', schema: DONE })
    const after = sc && sc.ok ? `catch ${(sc.catch_rate * 100).toFixed(0)}%, FI ${(sc.golden_fi_rate * 100).toFixed(0)}%` : 'eval absent'
    trials.push({ candidate: c, decision: 'revert', before: best, after: sc, guard, reason: 'did not improve catch-rate without FI regression' })
    log(`  ↳ reverted (no improvement: ${after} vs baseline catch ${(best.catch_rate * 100).toFixed(0)}%, FI ${(best.golden_fi_rate * 100).toFixed(0)}%)`)
  }
}

// ───────────────────────── 7. Report ───────────────────────────────────────
phase('Report')
const kept = trials.filter(t => t.decision === 'keep')
log(`Done. Kept ${kept.length}/${trials.length} change(s) on ${BRANCH}. main is untouched — review before merging.`)
return {
  branch: BRANCH,
  baseline: { catch_rate: baseline.catch_rate, golden_fi_rate: baseline.golden_fi_rate, near_miss_fi_rate: baseline.near_miss_fi_rate, verifier_hit_rate: baseline.verifier_hit_rate, latency_p95_ms: baseline.latency_p95_ms },
  final: { catch_rate: best.catch_rate, golden_fi_rate: best.golden_fi_rate, near_miss_fi_rate: best.near_miss_fi_rate, verifier_hit_rate: best.verifier_hit_rate, latency_p95_ms: best.latency_p95_ms },
  net_catch_delta: +(best.catch_rate - baseline.catch_rate).toFixed(4),
  net_fi_delta: +(best.golden_fi_rate - baseline.golden_fi_rate).toFixed(4),
  kept: kept.map(t => ({ title: t.candidate.title, files: t.candidate.files, sha: t.commit && t.commit.sha, before: { catch: t.before.catch_rate, fi: t.before.golden_fi_rate }, after: { catch: t.after.catch_rate, fi: t.after.golden_fi_rate }, guard: t.guard && t.guard.verdict })),
  reverted: trials.filter(t => t.decision === 'revert').map(t => ({ title: t.candidate.title, reason: t.reason })),
  diagnosis_summary: diagnosis.summary,
  note: 'All kept changes are committed to the sandbox branch ONLY. Review the diffs and approve before merging to main.',
}
