// The console.
//
// What this replaces: a 560-line simulation that invented mandate ids with
// Math.random(), paid hardcoded sellers on setTimeout, and typed out agent
// dialogue. It demonstrated an idea. This shows a system — every row below is a
// real Biscuit token, every rupee is the gateway's ledger, and the run stops
// when the bounds say so rather than when the script ends.

import * as gw from './gateway.js'
import { build, summarise, layers, money, escapeHtml, BUCKET } from './funnel.js'
import * as intake from './intake.js'
import './tips.js'

const $ = (id) => document.getElementById(id)

const BOUNDS = [
  { key: 'depth', name: 'Depth', limit: 8, fault: false,
    note: 'derived from the token chain, never claimed' },
  { key: 'floor', name: 'Budget floor', limit: '₹5,000', fault: false,
    note: 'below this a task acts instead of splitting — this is what ends the recursion' },
  { key: 'widest', name: 'Fan-out', limit: 6, fault: true, note: 'children per node' },
  { key: 'nodes', name: 'Node budget', limit: 128, fault: true, note: 'per run; exhaustion refuses' },
  { key: 'cycles', name: 'Cycles', limit: 0, fault: true, note: 'a sub-task restating an ancestor' },
  { key: 'conservation', name: 'Conservation', limit: '100%', fault: true,
    note: 'children may not be allocated more than their parent holds' },
  { key: 'railcap', name: 'Rail cap', limit: '₹5,00,000', fault: true,
    note: 'the mirror of the floor — the most one order may be' },
  { key: 'critic', name: 'Critic', limit: 'advisory', fault: true, judged: true,
    note: 'a second model reads each plan before any token is minted. It can refuse; it can never authorise' },
]

const seen = new Map()
const state = {
  live: false, running: false, filter: 'all',
  mandateId: null, task: '', floor: null, mandate: null, approvals: [], stop: null,
  audit: [], chain: null, inspecting: null, replays: {}, parties: [],
  fleet: null, policies: [], tab: 'run', auditFilter: 'all',
  // null, not true. The console only learns whether a critic ran from a run it
  // started itself; asserting "watching" over a run seeded elsewhere would claim
  // a safety layer was active when it may not have been.
  criticOn: null, can: null,
}

// Node ids are tree-local: every run has a "root", a "root.1", a "root.1.2".
// Two runs on one gateway therefore emit colliding ids, and a console keying by
// node id alone draws ONE tree out of TWO — silently mixing somebody else's
// spending into yours. The run id (the mandate the run draws on) is part of the
// key, and the tree is filtered to the run being watched.
const record = (event) =>
  seen.set(`${event.run_id ?? '-'}|${event.node_id}|${event.kind}|${event.at}`, event)

// Which run this tab started, remembered across a reload and nowhere else.
//
// sessionStorage rather than localStorage on purpose: a demo that is reloaded
// mid-run should come back, and a browser opened tomorrow should not resurrect
// a mandate that has long since expired. Every accessor is wrapped because a
// private window, a thumbnail capture or blocked site data makes these throw
// rather than return empty.
const OWN_RUN = 'pc.run'

const rememberRun = (id, task) => {
  try { sessionStorage.setItem(OWN_RUN, JSON.stringify({ id, task })) } catch { /* fine */ }
}

const ownRun = () => {
  try { return JSON.parse(sessionStorage.getItem(OWN_RUN) || 'null') } catch { return null }
}

const forgetRun = () => {
  try { sessionStorage.removeItem(OWN_RUN) } catch { /* fine */ }
}

const allEvents = () => {
  const all = [...seen.values()]
  if (!state.mandateId) return all
  // Events predating run scoping carry no run_id. Keeping them rather than
  // dropping them means an older gateway still renders; they simply cannot be
  // separated, which is the honest outcome for data that was never tagged.
  return all.filter((e) => !e.run_id || e.run_id === state.mandateId)
}

// --- connection ------------------------------------------------------------

function connection(mode, label) {
  const el = $('conn')
  el.className = `sb-conn ${mode}`
  el.textContent = label
}

async function connect() {
  try {
    // What this deployment can ACTUALLY do, not what the checkboxes offer.
    // Every layer here degrades quietly with no key configured — that is the
    // right failure — but the console used to render a degraded run and a full
    // one identically, which is the wrong report.
    const health = await gw.health()
    state.can = health.capabilities || null
    showCapabilities()
  } catch {
    state.live = false
    connection('is-off', 'no gateway')
    $('note').innerHTML =
      `No gateway at <b>${escapeHtml(gw.url())}</b>. Start one with ` +
      `<b>pocketchange serve</b> — this console shows real runs only.`
    $('send').disabled = true
    return
  }
  state.live = true
  connection('is-live', state.can?.degraded ? 'live · degraded' : 'live')
  attach()
  await refresh()
  gate()
}

function attach() {
  state.stop?.()
  state.stop = gw.stream({
    onEvent: (event) => {
      record(event)
      // Someone else's run arriving on the shared stream must not redraw over
      // the tree being watched, nor make this console look busy.
      if (state.mandateId && event.run_id && event.run_id !== state.mandateId) return
      draw()
      if (['settled', 'denied', 'escalated', 'bound_hit'].includes(event.kind)) refresh()
    },
    onOpen: () => connection('is-live', 'live'),
    onError: () => connection('', 'reconnecting'),
  })
}

async function refresh() {
  const [approvals, auditBody, parties, fleet, policies] = await Promise.all([
    gw.approvals().catch(() => ({ pending: [] })),
    gw.audit().catch(() => ({ entries: [] })),
    gw.counterparties().catch(() => ({ counterparties: [] })),
    gw.agents().catch(() => null),
    gw.standing().catch(() => ({ orders: [] })),
  ])
  state.parties = parties.counterparties ?? []
  state.fleet = fleet
  state.policies = policies.orders ?? []
  state.approvals = approvals.pending ?? []
  state.audit = auditBody.entries ?? []

  // Adopt only a run this browser actually started.
  //
  // This used to take the first `mandate` entry out of the audit log, which is
  // shared by every visitor to the deployment - so opening the console attached
  // you to a stranger's run and drew it as though it were yours. `.find()` also
  // returns the OLDEST such entry, so it was not even the most recent one.
  //
  // The audit stays global and hash-linked, because a log anyone can verify is
  // the point of it. What is per-visitor is which run the console is looking
  // at, and that belongs in the browser rather than in the server's memory.
  if (!state.mandateId) {
    const mine = ownRun()
    if (mine && (auditBody.entries ?? []).some((e) => e.mandate_id === mine.id)) {
      state.mandateId = mine.id
      state.task = mine.task || ''
    }
  }
  if (state.mandateId) {
    state.mandate = await gw.mandate(state.mandateId).catch(() => state.mandate)
  }
  draw()
}

// --- drawing ---------------------------------------------------------------

function draw() {
  const tree = build(allEvents())
  const s = summarise(tree)
  drawLedger(s)
  drawProfile(tree)
  drawBounds(s, tree)
  drawLatency()
  drawParties()
  drawChain()
  drawAudit()
  drawFleet()
  drawPolicies()
  drawApprovals()
  drawTranscript(tree, s)
}

function drawLedger(s) {
  const m = state.mandate
  $('cap').textContent = m ? money(m.cap_paise) : '—'
  $('task-line').textContent = state.task || 'No run yet'
  $('mandate-id').textContent = state.mandateId ? state.mandateId.slice(0, 20) + '…' : ''
  $('committed').textContent = m ? money(m.committed_paise) : money(s.committed)
  $('held').textContent = m ? money(m.reserved_paise) : '—'
  $('available').textContent = m ? money(m.available_paise) : '—'

  const share = m && m.cap_paise ? m.committed_paise / m.cap_paise : 0
  const fill = $('meter')
  fill.style.width = `${Math.min(100, share * 100).toFixed(1)}%`
  fill.dataset.level = share > 0.9 ? 'high' : share > 0.7 ? 'mid' : 'low'
}

// How wide the tree is at each depth, and what happened there. Genuinely
// different information from the transcript, which reads one node at a time.
function drawProfile(tree) {
  const byDepth = new Map()
  for (const node of tree.ordered) {
    const bucket = BUCKET[node.state] || 'open'
    const row = byDepth.get(node.depth) || { done: 0, held: 0, stop: 0, open: 0, n: 0 }
    row[bucket] += 1
    row.n += 1
    byDepth.set(node.depth, row)
  }
  if (!byDepth.size) {
    $('profile').innerHTML = '<p class="sb-empty">Nothing running.</p>'
    return
  }
  const widest = Math.max(...[...byDepth.values()].map((r) => r.n))
  $('profile').innerHTML = [...byDepth.entries()].sort((a, b) => a[0] - b[0])
    .map(([depth, r]) => {
      const seg = (k, cls) => r[k]
        ? `<span class="${cls}" style="flex:${r[k]}"></span>` : ''
      return `
        <div class="profile-row">
          <span>d${depth}</span>
          <span class="profile-bar" style="width:${Math.max(12, (r.n / widest) * 100)}%">
            ${seg('done', 'p-done')}${seg('held', 'p-held')}${seg('stop', 'p-stop')}${seg('open', 'p-open')}
          </span>
          <span class="profile-n">${r.n}</span>
        </div>`
    }).join('')
}

function drawBounds(s, tree) {
  const critiques = tree.ordered.filter((n) => /second model refused/i.test(n.note || '')).length
  const reached = {
    depth: s.depth, floor: '—', widest: s.widest, nodes: s.nodes,
    cycles: s.faults.filter((f) => /cycle/i.test(f)).length,
    conservation: s.faults.some((f) => /conservation/i.test(f)) ? 'over' : 'ok',
    railcap: 'ok',
    critic: critiques
      ? `${critiques} refused`
      : state.can?.critic === 'unconfigured' ? 'none'
      : state.criticOn === null ? '—' : (state.criticOn ? 'watching' : 'off'),
  }
  $('bounds').innerHTML = BOUNDS.map((b) => {
    const hit = s.faults.some((f) => new RegExp(b.name.split(' ')[0], 'i').test(f))
    return `
      <div class="bound${b.fault ? ' bound-fault' : ''}${b.judged ? ' bound-judged' : ''}${hit ? ' is-hit' : ''}">
        <span class="bound-name">${b.name}</span>
        <span class="bound-val">${reached[b.key]} <em>/ ${b.limit}</em></span>
        <span class="bound-note">${b.note}</span>
      </div>`
  }).join('')
}

function drawApprovals() {
  const block = $('approvals-block')
  if (!state.approvals.length) { block.hidden = true; return }
  block.hidden = false
  $('approvals').innerHTML = state.approvals.map((p) => `
    <article class="approval">
      <p class="approval-amt num">${money(p.amount_paise)}<span class="approval-kind">${
        p.kind === 'policy' ? 'per period, recurring' : 'one payment, held'}</span></p>
      <p class="approval-why">${escapeHtml(p.reason)}</p>
      <p class="approval-claim">
        <span>${p.kind === 'policy' ? 'your instruction' : 'the agent says'}</span>
        ${escapeHtml(p.agent_claim || '—')}
      </p>
      <div class="approval-btns">
        <button class="yes" data-yes="${escapeHtml(p.approval_id)}">Release</button>
        <button class="no"  data-no="${escapeHtml(p.approval_id)}">Refuse</button>
      </div>
    </article>`).join('')
}

// --- two layers, measured -------------------------------------------------
//
// enforcement_ms and monitor_ms have been written into every payment entry since
// the gateway was built and read by nothing. They are the project's headline
// claim, so showing the real figures matters more than showing a flattering one.

function median(values) {
  if (!values.length) return null
  const s = [...values].sort((a, b) => a - b)
  const m = Math.floor(s.length / 2)
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

function drawLatency() {
  const pays = state.audit.filter((e) => e.tool === 'pay' && e.detail)
  const enf = pays.map((e) => e.detail.enforcement_ms).filter((n) => typeof n === 'number')
  const jud = pays.map((e) => e.detail.monitor_ms)
    .filter((n, i) => typeof n === 'number' && pays[i].detail.monitor_ran !== false)

  if (!enf.length) {
    $('latency').innerHTML = '<p class="sb-empty">No payments yet.</p>'
    return
  }
  const me = median(enf), mj = median(jud)
  const worst = Math.max(me || 0, mj || 0) || 1
  const ms = (n) => n === null ? '—' : (n < 10 ? n.toFixed(3) : Math.round(n).toLocaleString()) + ' ms'

  const row = (name, value, note) => `
    <div class="lat-row${value !== null && value > 50 ? ' is-slow' : ''}">
      <span class="lat-name">${name}</span>
      <span class="lat-val">${ms(value)}</span>
      <span class="lat-bar"><span style="width:${value === null ? 0 : (value / worst) * 100}%"></span></span>
      ${note ? `<span class="lat-note">${note}</span>` : ''}
    </div>`

  // Say why enforcement is slower than the quoted figure rather than let the
  // number quietly contradict the README.
  const overNetwork = me !== null && me > 50
  $('latency').innerHTML =
    row('Enforcement', me, overNetwork
      ? 'includes a DynamoDB round trip; 0.164 ms with the in-memory ledger'
      : 'signature, expiry, depth, scope, cumulative spend, idempotency') +
    row('Judgement', mj, jud.length ? `${jud.length} model call${jud.length === 1 ? '' : 's'}`
                                    : 'monitor not run') +
    `<p class="lat-note">median of ${enf.length} payment${enf.length === 1 ? '' : 's'}</p>`
}

// --- who we have paid -----------------------------------------------------
//
// The contrast with seller_reputation(), which reports what a supplier says
// about itself. Nothing here came from outside the gateway.

function concerns(p) {
  const bits = []
  if (p.vetoed)     bits.push(`${p.vetoed} refused by a person`)
  if (p.injections) bits.push(`${p.injections} injected page${p.injections === 1 ? '' : 's'}`)
  if (p.escalated)  bits.push(`${p.escalated} held for review`)
  if (p.refused)    bits.push(`${p.refused} refused by the gateway`)
  return bits
}

function drawParties() {
  const rows = [...state.parties].sort(
    (a, b) => (b.trouble || 0) - (a.trouble || 0) || b.orders - a.orders)

  const table = $('parties-table')
  if (!rows.length) {
    table.innerHTML = `<tbody><tr><td class="wrap">Nobody has been paid yet.</td></tr></tbody>`
    return
  }
  table.innerHTML = `
    <thead><tr>
      <th>counterparty</th><th>orders</th><th>settled</th>
      <th>mandates</th><th>first paid</th><th>on the record</th>
    </tr></thead>
    <tbody>${rows.map((p) => {
      const bad = concerns(p)
      return `
        <tr class="${bad.length ? 'row-trouble' : ''}">
          <td class="m">${escapeHtml(p.id)}</td>
          <td class="n">${p.orders || '—'}</td>
          <td class="n">${p.orders ? money(p.total_paise) : '—'}</td>
          <td class="n">${p.mandates || '—'}</td>
          <td class="m">${p.orders ? String(p.first_paid).slice(0, 10) : '—'}</td>
          <td class="wrap">${bad.length
            ? `<span class="verdict bad">concerns</span> ${escapeHtml(bad.join(' · '))}`
            : 'nothing against them'}</td>
        </tr>`
    }).join('')}</tbody>`
}

// --- the trail -------------------------------------------------------------

const VERDICT = { allowed: 'ok', escalated: 'held', denied: 'bad' }

function drawAudit() {
  const rows = state.audit
    .filter((e) => state.auditFilter === 'all' || e.decision === state.auditFilter)
    .slice().reverse()

  const table = $('audit-table')
  if (!rows.length) {
    table.innerHTML = `<tbody><tr><td class="wrap">Nothing recorded under this filter.</td></tr></tbody>`
    return
  }
  table.innerHTML = `
    <thead><tr>
      <th>#</th><th>time</th><th>tool</th><th>decision</th>
      <th>amount</th><th>enforce</th><th>judge</th><th>reason</th>
    </tr></thead>
    <tbody>${rows.map((e) => {
      const d = e.detail || {}
      const kind = VERDICT[e.decision] || 'ok'
      // A payment that settled without judgement is not one that was judged
      // and approved, and the trail has to say which it was.
      const skipped = d.monitor_ran === false
        ? '<span class="unjudged">not judged</span>' : ''
      const ms = (n) => typeof n === 'number'
        ? (n < 10 ? n.toFixed(2) : Math.round(n).toLocaleString()) : '—'
      return `
        <tr class="${kind === 'bad' ? 'row-trouble' : kind === 'held' ? 'row-held' : ''}">
          <td class="n">${e.seq}</td>
          <td class="m">${String(e.at || '').slice(11, 19)}</td>
          <td class="m">${escapeHtml(e.tool)}</td>
          <td><span class="verdict ${kind}">${escapeHtml(e.decision)}</span>${skipped}</td>
          <td class="n">${e.amount_paise ? money(e.amount_paise) : '—'}</td>
          <td class="n">${ms(d.enforcement_ms)}</td>
          <td class="n">${d.monitor_ran === false ? '—' : ms(d.monitor_ms)}</td>
          <td class="wrap">${escapeHtml(e.reason)}</td>
        </tr>`
    }).join('')}</tbody>`
}

// --- the fleet -------------------------------------------------------------
//
// The registry is a SECOND bound on what an agent may hold, and it fails for a
// different reason than the token chain does: attenuation stops a token being
// widened, this stops one being minted wider than the agent was ever approved
// for. Two independent checks, so a bug in one does not disable the other.

function drawFleet() {
  const table = $('fleet-table')
  const fleet = state.fleet
  if (!fleet || !(fleet.agents || []).length) {
    table.innerHTML = `<tbody><tr><td class="wrap">No agents published.</td></tr></tbody>`
    return
  }
  table.innerHTML = `
    <thead><tr>
      <th>agent</th><th>department</th><th>owner</th>
      <th>may ever hold</th><th>ceiling</th><th>approved</th>
    </tr></thead>
    <tbody>${fleet.agents.map((a) => `
      <tr class="${a.approved ? '' : 'row-trouble'}">
        <td class="m">${escapeHtml(a.ref)}</td>
        <td>${escapeHtml(a.department)}</td>
        <td>${escapeHtml(a.owner)}</td>
        <td>${(a.capabilities || []).map((c) =>
          `<span class="cap-chip">${escapeHtml(c)}</span>`).join('')}</td>
        <td class="n">${money(a.max_budget_paise)}</td>
        <td>${a.approved
          ? '<span class="verdict ok">yes</span>'
          : '<span class="verdict bad">no</span>'}</td>
      </tr>`).join('')}</tbody>`
}

// --- standing policies -----------------------------------------------------

function drawPolicies() {
  const el = $('policies')
  if (!state.policies.length) {
    el.innerHTML = `<p class="sb-empty">No standing policies yet. A recurring
      instruction lives here once one is written, and does nothing until a
      person approves it.</p>`
    return
  }
  // Unapproved first: a policy waiting on a human is the one worth reading.
  const rows = [...state.policies].sort((a, b) => (a.approved ? 1 : 0) - (b.approved ? 1 : 0))
  el.innerHTML = `<div class="table-wrap"><table class="grid">
    <thead><tr>
      <th>instruction</th><th>dept</th><th>per period</th>
      <th>spent</th><th>reorder rules</th><th>state</th>
    </tr></thead>
    <tbody>${rows.map((o) => `
      <tr class="${o.approved ? '' : 'row-held'}">
        <td class="wrap">${escapeHtml(o.instruction || o.id || '')}</td>
        <td>${escapeHtml(o.department || '—')}</td>
        <td class="n">${money(o.period_budget_paise)} / ${escapeHtml(o.period || '')}</td>
        <td class="n">${money(o.spent_this_period_paise)}</td>
        <td class="m">${(o.rules || []).map((r) =>
          `<span class="cap-chip">${escapeHtml(r.sku)} &lt;${r.reorder_point} &rarr; ${r.target_level}</span>`
        ).join('')}</td>
        <td>${o.approved
          ? '<span class="verdict ok">running</span>'
          : '<span class="verdict held">inert until approved</span>'}</td>
      </tr>`).join('')}</tbody></table></div>`
}

// --- audit chain ----------------------------------------------------------

function drawChain() {
  const c = state.chain
  if (!c) {
    $('chain').innerHTML = `<p class="sb-empty">Not checked yet.</p>`
    return
  }
  $('chain').innerHTML = `
    <p class="chain-state ${c.ok ? 'ok' : 'bad'}">
      ${c.ok ? 'Intact' : 'BROKEN'} · ${c.entries} entr${c.entries === 1 ? 'y' : 'ies'}
    </p>
    <p class="chain-head">${escapeHtml(c.head)}</p>
    <p class="chain-proves">${escapeHtml(c.proves)}</p>`
}

function keep(node) {
  switch (state.filter) {
    case 'paid':    return node.state === 'settled' && !node.helper
    case 'web':     return node.sourcing === 'best' || node.helper
    case 'trouble': return node.state === 'refused' || node.state === 'escalated'
    default:        return true
  }
}

// --- the token inspector --------------------------------------------------
//
// What a token IS, without handing anyone the token. The Datalog of each block
// states the constraints this holder carries; the signed base64 stays in the
// gateway, because a live `pay` token is spendable by whoever holds it.

function blockKind(source, index) {
  if (index === 0) return 'the authority you signed'
  const to = /delegate\("([^"]+)"\)/.exec(source)
  if (!to) return 'delegation'
  return to[1].includes('/broker/') ? 'delegation to a branch' : 'delegation to a leaf'
}

function highlight(source) {
  return escapeHtml(source)
    .split('\n')
    .map((line) => /^\s*check if/.test(line) ? `<span class="chk">${line}</span>` : line)
    .join('\n')
}

function openInspector(nodeId) {
  const tree = build(allEvents())
  const node = tree.nodes.get(nodeId)
  if (!node) return

  state.inspecting = nodeId
  $('drawer').hidden = false
  $('drawer-title').textContent = node.task || nodeId
  $('drawer-sub').textContent =
    `${nodeId} · depth ${node.depth} · ${node.tokenBytes ?? '?'} chars` +
    (node.expires ? ` · expires ${String(node.expires).slice(11, 19)}` : '')

  const blocks = node.blocks || []
  $('drawer-body').innerHTML = blocks.length
    ? blocks.map((src, i) => `
        <div class="blk">
          <div class="blk-head">
            <span class="blk-n">block ${i}</span>
            <span class="blk-kind">${escapeHtml(blockKind(src, i))}</span>
          </div>
          <pre class="blk-src">${highlight(src)}</pre>
        </div>`).join('') +
      `<p class="chain-proves">Verification requires every check in every block to
        pass, so each block can only narrow the one above it. ${blocks.length}
        block${blocks.length === 1 ? '' : 's'} means ${blocks.length - 1}
        delegation${blocks.length === 2 ? '' : 's'} from the mandate.</p>`
    : `<p class="sb-empty">The root mandate is minted, not attenuated, so it
        carries no delegation block yet.</p>`
}

function closeInspector() {
  state.inspecting = null
  $('drawer').hidden = true
}

function drawTranscript(tree, s) {
  if (!tree.ordered.length) return
  $('empty')?.remove()

  const shown = tree.ordered.filter(keep)
  // `settled` is a property of the tree, not of this flag. Deriving it from
  // state.running made the two mutually dependent, so the tail said "running"
  // forever after a run that had plainly finished.
  const done = s.settled
  if (done) state.running = false
  const tail = done
    ? `${s.paid} paid · ${s.held} held · ${s.stopped} stopped` +
      (s.faults.length ? ` · a bound refused this run` : ' · every bound respected')
    : 'running…'

  $('pane-run').innerHTML = `
    <div class="run-head">
      <p class="run-task">${escapeHtml(state.task || 'Run')}</p>
      <p class="run-meta">
        <span>${s.nodes} nodes</span>
        <span>depth ${s.depth}</span>
        <span>${s.lookers} zero-budget looker${s.lookers === 1 ? '' : 's'}</span>
        <span>${money(s.committed)} committed</span>
        ${state.floor ? `<span>splits above ₹${state.floor.toLocaleString('en-IN')}</span>` : ''}
      </p>
    </div>
    ${layers(shown)}
    <p class="tail${done ? ' is-done' : ''}">${escapeHtml(tail)}</p>`

  addReplayActions()
  if (!done) $('pane-run').scrollTop = $('pane-run').scrollHeight
}

// A settled leaf can be paid again, byte for byte. The gateway derives the
// idempotency key from the request itself, so the second attempt returns the
// first order and the ledger does not move — the gap Razorpay leaves open on
// Orders creation, closed and demonstrable in one click.
function addReplayActions() {
  const paid = new Map()
  for (const e of state.audit) {
    if (e.tool === 'pay' && e.decision === 'allowed' && e.detail && e.detail.order_id) {
      paid.set(String(e.context || '').replace('leaf of the funnel: ', ''), e.seq)
    }
  }
  for (const row of document.querySelectorAll('.row.is-done')) {
    if (row.querySelector('.row-actions')) continue
    const id = row.dataset.node
    const task = row.querySelector('.row-task')?.textContent || ''
    const seq = paid.get(task.replace(/open web|sources for.*$/g, '').trim())
    if (seq === undefined) continue
    const out = state.replays[seq]
    row.insertAdjacentHTML('beforeend', `
      <div class="row-actions">
        <button class="mini-btn" data-replay="${seq}">Replay this payment</button>
        ${out ? `<span class="replay-out ${out.charged_twice ? 'bad' : ''}">${escapeHtml(out.text)}</span>` : ''}
      </div>`)
    void id
  }
}

// --- interaction -----------------------------------------------------------

const taskField = $('task')

// One field, and it only has to be non-empty. Everything the run actually needs
// — the ceiling above all — is asked for afterwards, by something that can
// explain why it needs it. A numeric box labelled nothing could not.
function gate() {
  $('send').disabled = !state.live || state.running || !taskField.value.trim()
}

taskField.addEventListener('input', () => {
  taskField.style.height = 'auto'
  taskField.style.height = Math.min(taskField.scrollHeight, 128) + 'px'
  gate()
})
$('opt-monitor').addEventListener('change', () => {
  // The monitor changes what a run costs, so re-reading is cheaper than letting
  // the figure on the ready card go stale.
  if (intake.state.reading?.ready) intake.begin(intake.state.text, runOptions())
})
taskField.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); readIt() }
})

const runOptions = () => ({ monitor: $('opt-monitor').checked })

// Say plainly which layers are real. A ticked "semantic monitor" box above a
// monitor that cannot judge is the kind of thing this project exists to refuse
// to do, and it was doing it.
function showCapabilities() {
  const can = state.can
  const banner = $('degraded')
  const box = $('opt-monitor')
  if (!can) { banner.hidden = true; return }

  const judging = can.monitor === 'bedrock'
  box.disabled = !judging
  box.checked = box.checked && judging
  const label = box.closest('.opt')
  label.classList.toggle('is-unavailable', !judging)
  label.querySelector('span').textContent =
    judging ? 'Semantic monitor' : 'Semantic monitor — no model configured'
  label.dataset.tip = judging
    ? label.dataset.tipFull
    : 'There is no model this gateway can reach, so nothing can judge a payment. '
      + 'Enforcement still runs in full and refuses on its own — that is the point '
      + 'of the two layers being separable — but no second opinion is being formed, '
      + 'and the audit trail records every payment as “not judged” rather than approved.'

  banner.hidden = !can.degraded
  if (can.degraded) {
    banner.innerHTML =
      `<b>Running degraded.</b> ${escapeHtml(can.degraded)} `
      + `Enforcement is unaffected — bounds, attenuation and the audit chain are `
      + `deterministic and need no model.`
  }
}

// Step one: read the request. This spends at most one model call and mints
// nothing — the send button starts a conversation, not a mandate.
async function readIt() {
  if ($('send').disabled) return
  const text = taskField.value.trim()
  showTab('run')
  $('head-note').textContent = 'Reading your request. Nothing is signed yet.'
  taskField.value = ''
  taskField.style.height = 'auto'
  gate()
  await intake.begin(text, runOptions())
}

// Step two, and only once a person has confirmed the ceiling: mint and run.
async function startRun(proposal) {
  const rupees = proposal.budget_paise / 100
  state.running = true
  seen.clear()
  state.mandateId = null
  state.mandate = null
  forgetRun()
  state.task = proposal.task
  state.floor = proposal.floor_paise / 100
  intake.reset()
  showTab('run')
  gate()
  $('head-note').textContent = 'The tree is drawing itself as the gateway builds it.'

  try {
    const started = await gw.startProposal(proposal)
    state.mandateId = started.mandate_id
    rememberRun(started.mandate_id, proposal.task)
    // Three states, not two: a layer that is watching, one someone switched
    // off, and one this deployment does not have. Collapsing the last two into
    // "off" was the smaller half of the same dishonesty as the monitor's.
    state.criticOn = started.critic === 'bedrock' ? true
      : started.critic === 'unconfigured' ? null : false
    const layer = (name, value) => value === 'bedrock' ? `${name} on`
      : value === 'off' ? `${name} off` : `no ${name} configured`
    $('head-note').textContent =
      `${started.decomposer} · ${layer('critic', started.critic)} · ` +
      `${layer('monitor', started.monitor)} · ` +
      `${started.model_calls_exact ? '~' : 'at least '}` +
      `${started.expected_model_calls} model call` +
      `${started.expected_model_calls === 1 ? '' : 's'} for this run`
    $('opt-cost').textContent =
      `ceiling ${money(proposal.budget_paise)} · floor ${money(proposal.floor_paise)}`
  } catch (error) {
    state.running = false
    $('head-note').textContent = error.status === 401
      ? 'This deployment gates runs behind a demo token. Everything else on this '
        + 'page is live and readable — the ledger, the audit trail, the tree from '
        + 'the last run.'
      : `The gateway refused the run: ${escapeHtml(String(error.detail ?? error.message))}`
  }
  void rupees
  gate()
  await refresh()
}

intake.mount({ options: runOptions, onRun: (proposal) => startRun(proposal) })

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && state.inspecting) { closeInspector(); return }
  const row = event.target.closest?.('.row')
  if (row && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault()
    openInspector(row.dataset.node)
  }
})

$('send').addEventListener('click', readIt)

const PANES = ['run', 'ledger', 'parties', 'fleet', 'policies']

function showTab(name) {
  state.tab = name
  for (const pane of PANES) $(`pane-${pane}`).hidden = pane !== name
  document.querySelectorAll('.tab').forEach(
    (t) => t.classList.toggle('is-on', t.dataset.tab === name))
  // The composer and the node filters belong to the run, not to a reference
  // table, and leaving them visible over a ledger invites typing into nothing.
  document.querySelector('.composer').hidden = name !== 'run'
  $('run-filters').hidden = name !== 'run'
}

document.addEventListener('click', async (event) => {
  const tab = event.target.closest('[data-tab]')
  if (tab) { showTab(tab.dataset.tab); return }

  const auditChip = event.target.closest('[data-audit]')
  if (auditChip) {
    state.auditFilter = auditChip.dataset.audit
    auditChip.parentElement.querySelectorAll('.chip')
      .forEach((c) => c.classList.toggle('is-on', c === auditChip))
    drawAudit()
    return
  }

  const chip = event.target.closest('[data-filter]')
  if (chip) {
    state.filter = chip.dataset.filter
    chip.parentElement.querySelectorAll('.chip')
      .forEach((c) => c.classList.toggle('is-on', c === chip))
    draw()
    return
  }

  const task = event.target.closest('[data-task]')
  if (task) {
    // The budget goes into the SENTENCE, not into a field. Intake then has to
    // surface it as something to confirm, which is exactly what it does with a
    // figure anyone else puts in a request — including an attacker.
    taskField.value = `${task.dataset.task}, budget around ₹${task.dataset.budget}`
    gate()
    taskField.focus()
    taskField.dispatchEvent(new Event('input'))
    return
  }

  if (event.target.closest('#drawer-close')) { closeInspector(); return }

  const replayBtn = event.target.closest('[data-replay]')
  if (replayBtn) {
    const seq = Number(replayBtn.dataset.replay)
    replayBtn.disabled = true
    replayBtn.textContent = 'Replaying…'
    try {
      const out = await gw.replay(seq)
      state.replays[seq] = {
        charged_twice: out.charged_twice,
        text: out.charged_twice
          ? 'CHARGED TWICE'
          : `same order ${out.second.order_id} · ledger unmoved`,
      }
    } catch (error) {
      state.replays[seq] = { charged_twice: true, text: 'replay failed' }
    }
    await refresh()
    return
  }

  const verify = event.target.closest('#verify-chain')
  if (verify) {
    verify.disabled = true
    verify.textContent = 'Verifying…'
    try { state.chain = await gw.verifyChain() } catch { state.chain = null }
    verify.disabled = false
    verify.textContent = 'Verify chain'
    drawChain()
    return
  }

  // A row opens the inspector — but not when the click was on a button in it.
  const row = event.target.closest('.row')
  if (row && !event.target.closest('button')) { openInspector(row.dataset.node); return }

  const decide = event.target.closest('[data-yes], [data-no]')
  if (decide) {
    const approve = decide.hasAttribute('data-yes')
    decide.disabled = true
    decide.textContent = approve ? 'Releasing…' : 'Refusing…'
    try {
      await gw.decide(approve ? decide.dataset.yes : decide.dataset.no, approve)
    } finally {
      await refresh()
    }
  }
})

showTab('run')
connect()
