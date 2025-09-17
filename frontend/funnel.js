// Rebuilding the tree from the event stream.
//
// The gateway emits one flat sequence; the tree is implicit in the node ids
// (root.2.1) and each event's parent_id. Reassembling it here means a live run
// and a replayed one go through exactly the same code — there is no second path
// that only works for the recording.

export const money = (paise) =>
  paise === null || paise === undefined
    ? '—'
    : '₹' + Math.round(paise / 100).toLocaleString('en-IN')

export function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch])
}

// done / held / stopped / open — the four states the palette is built for.
export const BUCKET = {
  settled: 'done', decomposed: 'open', granted: 'open', pending: 'open',
  searching: 'open', paying: 'open', escalated: 'held', refused: 'stop',
}

const LABEL = {
  pending: 'queued', granted: 'granted', decomposed: 'split',
  searching: 'reading', paying: 'paying', settled: 'paid',
  escalated: 'held', refused: 'stopped',
}

export function build(events) {
  const nodes = new Map()

  const ensure = (id, parentId, depth) => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id, parentId: parentId ?? null, depth: depth ?? 0,
        task: '', budget: null, tools: [], sourcing: 'catalogue', supplier: null,
        state: 'pending', note: '', helper: false, fault: false, broker: false,
        blocks: null, tokenBytes: null, expires: null,
        children: [], order: nodes.size,
      })
    }
    const node = nodes.get(id)
    if (parentId && !node.parentId) node.parentId = parentId
    return node
  }

  for (const event of events) {
    const node = ensure(event.node_id, event.parent_id, event.depth)
    const d = event.detail || {}

    switch (event.kind) {
      case 'spawned':
        node.task = d.description || node.task
        node.sourcing = d.sourcing || node.sourcing
        node.supplier = d.supplier ?? node.supplier
        // The gateway says so; do not infer it from the id.
        if (d.helper) node.helper = true
        if (d.sources_for) node.sourcesFor = d.sources_for
        // The root is minted rather than attenuated, so it never receives a
        // `granted` event — its ceiling arrives here or nowhere.
        if (node.budget === null && d.budget_paise !== undefined) node.budget = d.budget_paise
        break
      case 'granted':
        node.budget = d.budget_paise ?? node.budget
        node.tools = d.tools || node.tools
        node.broker = Boolean(d.broker)
        // The Datalog of every block in this node's chain. Derived facts only:
        // the gateway never sends the signed token, because a `pay` token in a
        // browser is spendable by anyone who opens devtools.
        node.blocks = d.blocks || node.blocks
        node.tokenBytes = d.token_bytes ?? node.tokenBytes
        node.expires = d.expires || node.expires
        node.state = 'granted'
        break
      case 'decomposed':
        node.state = 'decomposed'
        node.note = `split ${d.children} ways · ${money(d.allocated)} allocated`
        break
      case 'searching':
        node.state = 'searching'
        node.note = 'reading supplier pages it did not write'
        break
      case 'paying':
        node.state = 'paying'
        break
      case 'settled':
        node.state = 'settled'
        node.note = typeof d.result === 'string' ? d.result : node.note
        break
      case 'escalated':
        node.state = 'escalated'
        node.note = d.reason || 'waiting on a person; the money is still held'
        break
      case 'denied':
        node.state = 'refused'
        node.note = d.reason || 'refused'
        break
      case 'bound_hit':
        // A bound that ends the recursion normally is not a fault, and colouring
        // it like one teaches the reader to ignore the ones that are.
        if (d.fault) { node.state = 'refused'; node.fault = true }
        // A critic refusal carries its reasoning, and the reasoning is the whole
        // value — "critic" alone tells a reader nothing.
        node.note = d.critique
          ? `a second model refused this plan: ${d.critique}`
          : (d.reason || node.note)
        break
    }
  }

  const roots = []
  for (const node of nodes.values()) {
    const parent = node.parentId ? nodes.get(node.parentId) : null
    if (parent) parent.children.push(node)
    else roots.push(node)
  }
  for (const node of nodes.values()) node.children.sort((a, b) => a.order - b.order)

  const ordered = []
  const walk = (node) => { ordered.push(node); node.children.forEach(walk) }
  roots.sort((a, b) => a.order - b.order).forEach(walk)

  return { nodes, ordered, roots }
}

export function summarise(tree) {
  const all = tree.ordered
  const work = all.filter((n) => !n.helper)
  const done = work.filter((n) => n.state === 'settled')
  return {
    nodes: all.length,
    lookers: all.length - work.length,
    depth: all.reduce((m, n) => Math.max(m, n.depth), 0),
    widest: all.reduce((m, n) => Math.max(m, n.children.length), 0),
    paid: done.length,
    held: all.filter((n) => n.state === 'escalated').length,
    stopped: all.filter((n) => n.state === 'refused').length,
    faults: all.filter((n) => n.fault).map((n) => n.note),
    committed: done.reduce((sum, n) => sum + (n.budget || 0), 0),
    settled: all.length > 0 && all.every(
      (n) => ['settled', 'refused', 'escalated', 'decomposed'].includes(n.state)),
  }
}

// --- rendering -------------------------------------------------------------

function tools(node) {
  if (!node.tools.length) {
    return node.depth === 0 ? '<span class="tool t-mandate">mandate</span>' : ''
  }
  // A branch holds pay and search only so it can confer them; the gateway
  // refuses it either. Showing the same chips as an agent that genuinely does
  // both would be the most misleading thing on the page.
  if (node.broker) return '<span class="tool t-broker">confers · cannot spend</span>'
  return node.tools
    .map((t) => `<span class="tool t-${escapeHtml(t)}">${escapeHtml(t)}</span>`)
    .join('')
}

export function row(node, seq) {
  const bucket = BUCKET[node.state] || 'open'
  const label = node.helper && node.state === 'settled' ? 'read' : (LABEL[node.state] || node.state)
  const serves = node.sourcesFor
    ? `<span class="web-tag">sources for ${escapeHtml(node.sourcesFor)}</span>` : ''
  // A leaf that names a supplier is the only kind that leaves a mark on the
  // counterparty record, so say who it is paying.
  const tag = node.sourcing === 'best'
    ? '<span class="web-tag">open web</span>'
    : node.supplier
      ? `<span class="web-tag">pays ${escapeHtml(node.supplier)}</span>`
      : ''

  return `
    <div class="row is-${bucket}${node.helper ? ' is-helper' : ''}" data-node="${escapeHtml(node.id)}" role="button" tabindex="0">
      <span class="row-seq">${String(seq).padStart(2, '0')}</span>
      <div class="row-body">
        <div class="row-task">${escapeHtml(node.task || '—')}${tag}${serves}</div>
        <div class="row-id">${escapeHtml(node.id)}</div>
        ${node.note ? `<div class="row-note">${escapeHtml(node.note)}</div>` : ''}
      </div>
      <span class="tools">${tools(node)}</span>
      <span class="row-amt">${money(node.budget)}</span>
      <span class="row-state">${label}</span>
    </div>`
}

// Grouped by depth, because depth is the thing the funnel is about: each layer
// is strictly narrower than the one above it.
export function layers(nodes) {
  const byDepth = new Map()
  for (const node of nodes) {
    if (!byDepth.has(node.depth)) byDepth.set(node.depth, [])
    byDepth.get(node.depth).push(node)
  }
  let seq = 0
  return [...byDepth.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([depth, group]) => {
      const total = group.reduce((sum, n) => sum + (n.budget || 0), 0)
      return `
        <section class="layer">
          <div class="layer-head">
            <b>${depth === 0 ? 'Mandate' : `Layer ${depth}`}</b>
            <span>${group.length} node${group.length === 1 ? '' : 's'} · ${money(total)}</span>
            <hr>
          </div>
          ${group.map((n) => row(n, ++seq)).join('')}
        </section>`
    })
    .join('')
}
