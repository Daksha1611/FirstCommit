// The front door, as a conversation rather than a form.
//
// The old composer asked for two things before it would do anything: a task
// string and a ceiling, typed into an unlabelled numeric box. The second is the
// number a first-time reader is least able to supply and the one the whole
// system hangs off, so the interface demanded exactly the wrong thing first.
//
// Here you write one sentence. The gateway reads it, says what it understood,
// and asks only for what is genuinely missing — each question carrying the
// reason it cannot be inferred. Nothing is minted until a proposal is confirmed.
//
// One rule is worth reading in the code as well as the prose: a budget the model
// found in your request text arrives as a SUGGESTION on a question, never as an
// answer. Request text is untrusted. A prompt that could set its own cap would
// make the cap meaningless.

import * as gw from './gateway.js'

const $ = (id) => document.getElementById(id)

export const state = {
  text: '',
  answers: {},
  reading: null,
  busy: false,
}

const money = (rupees) => '₹' + Number(rupees).toLocaleString('en-IN')

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))
}

export function reset() {
  state.text = ''
  state.answers = {}
  state.reading = null
  state.busy = false
  const box = $('intake')
  box.hidden = true
  box.innerHTML = ''
}

// --- rendering --------------------------------------------------------------

const KIND_NOTE = {
  errand: 'a bounded purchase — one obvious cart',
  target: 'a goal, not a list — it has to work out what to buy',
  standing: 'recurring — this would keep spending, so it needs approving first',
}

function understanding(u) {
  const bits = []
  if (u.task_kind) {
    bits.push(`<span class="in-tag" data-tip="${escapeHtml(KIND_NOTE[u.task_kind] || '')}"
      >${escapeHtml(u.task_kind)}</span>`)
  }
  for (const item of (u.items || []).slice(0, 6)) {
    const q = item.quantity ? ` ${escapeHtml(item.quantity)}` : ''
    bits.push(`<span class="in-tag is-item">${escapeHtml(item.name)}${q}</span>`)
  }
  if (u.supplier) {
    bits.push(`<span class="in-tag is-item">from ${escapeHtml(u.supplier)}</span>`)
  }
  const read = u.extracted_by === 'model'
    ? 'read by the intake model'
    : 'no model reachable — asking everything'
  return `
    <div class="in-card in-read">
      <p class="in-label" data-tip-title="What it understood"
         data-tip="Extracted from your sentence by a model that holds no authority. It can be wrong; that is why anything it changes about the money is asked rather than applied.">Here is what I understood</p>
      <p class="in-goal">${escapeHtml(u.goal || '')}</p>
      ${bits.length ? `<div class="in-tags">${bits.join('')}</div>` : ''}
      <p class="in-foot">${escapeHtml(read)}</p>
    </div>`
}

function control(q) {
  if (q.kind === 'money') {
    const value = q.suggestion ? escapeHtml(q.suggestion) : ''
    return `
      <div class="in-money">
        <label class="in-rupee">₹<input class="in-input" inputmode="numeric"
          data-answer="${q.id}" value="${value}"
          aria-label="Ceiling in rupees" placeholder="0" /></label>
        <button class="in-go" data-submit="${q.id}">
          ${q.confirming ? 'Use this ceiling' : 'Set ceiling'}
        </button>
      </div>`
  }
  if (q.kind === 'choice') {
    return `<div class="in-choices">${q.options.map((o) => `
      <button class="in-choice" data-pick="${q.id}" data-value="${escapeHtml(o.value)}"
              ${o.note ? `data-tip="${escapeHtml(o.note)}"` : ''}>
        <b>${escapeHtml(o.label)}</b>
        ${o.note ? `<span>${escapeHtml(o.note)}</span>` : ''}
      </button>`).join('')}</div>`
  }
  return `
    <div class="in-money">
      <input class="in-input is-wide" data-answer="${q.id}"
             placeholder="Type it here" aria-label="${escapeHtml(q.ask)}" />
      <button class="in-go" data-submit="${q.id}">Use this</button>
    </div>`
}

function question(q) {
  return `
    <div class="in-card in-q${q.confirming ? ' is-confirming' : ''}" data-q="${q.id}">
      <p class="in-ask">${escapeHtml(q.ask)}</p>
      <p class="in-why">${escapeHtml(q.why)}</p>
      ${control(q)}
    </div>`
}

function ready(reading) {
  const p = reading.proposal
  const u = reading.understood
  const calls = reading.expected_model_calls
  const where = { catalogue: 'our own catalogue, no web search',
                  specific: `only ${u.supplier}`,
                  best: 'the open web, searched by a zero-budget agent' }[u.sourcing]
  return `
    <div class="in-card in-ready">
      <p class="in-label">Ready. Nothing is signed until you press this.</p>
      <dl class="in-summary">
        <div><dt>Task</dt><dd>${escapeHtml(p.task)}</dd></div>
        <div><dt data-tip="Signed into the root token. No agent below can raise it, and every sub-agent gets a strict share of it.">Ceiling</dt>
             <dd class="num">${money(p.budget_paise / 100)}</dd></div>
        <div><dt data-tip="Below this a task stops splitting and simply buys. It is what ends the recursion — without it the tree would never reach a leaf.">Smallest purchase</dt>
             <dd class="num">${money(p.floor_paise / 100)}</dd></div>
        <div><dt data-tip="Where the goods come from. This is the risk axis, not a preference — reading the open web means reading seller-controlled text.">Sourcing</dt>
             <dd>${escapeHtml(where || '—')}</dd></div>
        ${Number.isFinite(calls) ? `<div><dt data-tip="One call per branch to split it, one per leaf to judge the payment. ${
          reading.model_calls_exact
            ? 'The tree shape is fixed in advance, so this figure is exact.'
            : 'A model chooses how deep each branch goes, so this is a floor — a real tree usually runs larger.'
        }">Model calls</dt>
             <dd class="num">${reading.model_calls_exact ? '' : '≥ '}${calls}</dd></div>` : ''}
      </dl>
      <div class="in-actions">
        <button class="in-start" id="in-start">Mint the mandate &amp; run</button>
        <button class="in-edit" id="in-edit">Start over</button>
      </div>
    </div>`
}

// One centred column, so the said-bubble can sit at its right edge rather than
// at the right edge of the whole window.
const thread = (inner) => `<div class="in-thread">${inner}</div>`

function draw() {
  const box = $('intake')
  const reading = state.reading
  box.hidden = false
  const empty = $('empty')
  if (empty) empty.hidden = true

  const said = `<div class="in-said">${escapeHtml(state.text)}</div>`
  if (!reading) {
    box.innerHTML = thread(said + '<div class="in-card in-wait">Reading that…</div>')
    return
  }
  const notes = (reading.notes || []).length
    ? `<p class="in-note">${reading.notes.map(escapeHtml).join(' ')}</p>` : ''

  box.innerHTML = thread(said + understanding(reading.understood) + notes +
    (reading.ready ? ready(reading) : reading.questions.map(question).join('')))

  const first = box.querySelector('.in-input, .in-choice, #in-start')
  first?.focus()
}

// --- flow -------------------------------------------------------------------

async function ask(options) {
  state.busy = true
  draw()
  try {
    state.reading = await gw.readRequest(state.text, state.answers, options)
  } catch (error) {
    state.reading = null
    $('intake').innerHTML = thread(`
      <div class="in-said">${escapeHtml(state.text)}</div>
      <div class="in-card in-error">
        Could not read that: ${escapeHtml(String(error.detail?.denied ?? error.message))}
      </div>`)
    return
  } finally {
    state.busy = false
  }
  draw()
}

export function begin(text, options) {
  state.text = text
  state.answers = {}
  state.reading = null
  return ask(options)
}

function answer(id, value, options) {
  if (value === '' || value == null) return
  state.answers = { ...state.answers, [id]: value }
  return ask(options)
}

/** Wire the intake pane. `onRun` receives the confirmed proposal. */
export function mount({ options, onRun }) {
  const box = $('intake')

  box.addEventListener('click', (event) => {
    if (state.busy) return

    const pick = event.target.closest('[data-pick]')
    if (pick) { answer(pick.dataset.pick, pick.dataset.value, options()); return }

    const submit = event.target.closest('[data-submit]')
    if (submit) {
      const id = submit.dataset.submit
      const field = box.querySelector(`[data-answer="${id}"]`)
      const raw = field?.value ?? ''
      const value = id.endsWith('_rupees') ? raw.replace(/\D/g, '') : raw.trim()
      if (!value) { field?.focus(); return }
      answer(id, value, options())
      return
    }

    if (event.target.closest('#in-edit')) {
      reset()
      const empty = $('empty')
      if (empty) empty.hidden = false
      return
    }

    if (event.target.closest('#in-start') && state.reading?.proposal) {
      onRun(state.reading.proposal, state.reading.understood)
    }
  })

  box.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return
    const field = event.target.closest('[data-answer]')
    if (!field) return
    event.preventDefault()
    box.querySelector(`[data-submit="${field.dataset.answer}"]`)?.click()
  })
}
