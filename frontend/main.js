// ── CTA buttons — navigate to app ─────────────────────────────
document.getElementById('hero-cta')?.addEventListener('click', () => {
  window.location.href = '/app.html';
});

// ── Cover stats, read from the running system ────────────────────
//
// These used to be typed into the HTML, and they went three releases stale: 258
// tests against 372, 9 of 12 SoK vectors against 10, and an enforcement figure
// that only held for the in-memory ledger while the console beside it showed
// something twenty times larger. Every one drifted because a person had to
// remember to change them.
//
// /facts derives them from the same objects the runtime uses, so adding a bound
// to funnel.Bounds updates this page without anyone editing this page. The
// numbers in the markup are the last known good ones and stay put when nothing
// answers — a page opened with no gateway should still say something true,
// just not claim to be live.

const el = (id) => document.getElementById(id);

async function loadFacts() {
  const live = el('cover-live');
  let facts;
  try {
    facts = await fetch(gatewayUrl() + '/facts', { signal: AbortSignal.timeout(3000) })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))));
  } catch {
    if (live) {
      live.textContent = 'figures as built — no gateway reachable from this page';
      live.classList.add('is-static');
    }
    return;
  }

  if (facts.bounds) el('stat-bounds').textContent = facts.bounds;
  if (facts.sok) el('stat-sok').textContent = `${facts.sok.defended}/${facts.sok.total}`;

  // Say what is actually running, not what was true when this was written.
  const l = facts.live || {};
  const parts = [
    facts.rail === 'razorpay-test' ? 'razorpay test rail' : 'simulated rail',
    `${facts.models?.primary === 'bedrock' ? `bedrock ${facts.models?.region ?? ''}`.trim() : 'no model'}` +
      (facts.models?.fallbacks?.length ? ` + ${facts.models.fallbacks.length} fallbacks` : ''),
    facts.verdicts?.length === 3 ? 'all three verdicts' : `${facts.verdicts?.length ?? 0} verdicts`,
  ];
  if (l.payments) parts.push(`${l.payments} payments settled`);
  if (l.counterparties) {
    parts.push(`${l.counterparties} counterpart${l.counterparties === 1 ? 'y' : 'ies'} on record` +
      (l.with_concerns ? `, ${l.with_concerns} with concerns` : ''));
  }
  if (l.chain_intact === false) parts.push('AUDIT CHAIN BROKEN');

  if (live) {
    live.classList.remove('is-static');
    live.innerHTML = `<span class="live-dot"></span>${parts.join(' · ')}`;
  }
}

loadFacts();

// ── The monitor panel ─────────────────────────────────────────
//
// This used to be twelve hardcoded strings looping under a "● LIVE" badge,
// which on a page whose whole argument is "we tell you what does not work" was
// the single most expensive detail to be caught on. It now reads the gateway's
// real audit trail. When there is no gateway it says so and shows a recorded
// trace, labelled as one — never LIVE over invented data.

import { url as gatewayUrl } from './gateway.js';

const terminal = document.getElementById('terminal-output');
const liveBadge = document.getElementById('t-live-indicator');

// A real run, frozen: the decisions a ₹2,00,000 office fit-out actually
// produced, including the leaf that was refused for asking beyond its token.
const RECORDED = [
  { at: '02:41:09', tool: 'mandate',  decision: 'allowed',   amount: '₹2,00,000', reason: 'mandate opened' },
  { at: '02:41:09', tool: 'delegate', decision: 'allowed',   amount: '₹66,667',   reason: 'kit out engineering — confers, cannot spend' },
  { at: '02:41:09', tool: 'delegate', decision: 'allowed',   amount: '₹0',        reason: 'looker — may look, may not spend' },
  { at: '02:41:10', tool: 'pay',      decision: 'allowed',   amount: '₹22,222',   reason: 'paid — remaining ₹1,77,778' },
  { at: '02:41:10', tool: 'pay',      decision: 'allowed',   amount: '₹22,222',   reason: 'paid — remaining ₹1,55,556' },
  { at: '02:41:11', tool: 'pay',      decision: 'escalated', amount: '₹22,222',   reason: 'monitor: supplier not one we have bought from before' },
  { at: '02:41:12', tool: 'pay',      decision: 'denied',    amount: '₹1,77,776', reason: 'budget: this token is capped at ₹22,222' },
  { at: '02:41:12', tool: 'delegate', decision: 'denied',    amount: '—',         reason: 'scope: a leaf holds pay, not delegate' },
];

const ROW = (e) => `
  <div class="log-line">
    <span class="log-prompt">›</span>
    <span class="log-time">[${e.at}]</span>
    <span class="log-action">${e.tool.toUpperCase()}</span>
    <span class="log-${e.decision === 'allowed' ? 'ok' : e.decision === 'escalated' ? 'warn' : 'denied'}">${e.decision.toUpperCase()}</span>
    <span class="log-amt">${e.amount}</span>
    <span class="log-msg">${e.reason}</span>
  </div>`;

const clock = (iso) => (iso || '').slice(11, 19) || '--:--:--';
const rupees = (paise) =>
  paise ? '₹' + Math.round(paise / 100).toLocaleString('en-IN') : '—';

function paint(rows) {
  if (!terminal) return;
  terminal.innerHTML = rows.map(ROW).join('');
  terminal.scrollTop = terminal.scrollHeight;
}

function showRecorded(note) {
  if (liveBadge) {
    liveBadge.textContent = '● RECORDED';
    liveBadge.classList.add('is-recorded');
    liveBadge.title = note;
  }
  paint(RECORDED);
}

async function showLive() {
  const body = await fetch(gatewayUrl() + '/audit', { signal: AbortSignal.timeout(3000) })
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))));

  const entries = (body.entries || []).slice(-14);
  if (!entries.length) throw new Error('no entries yet');

  if (liveBadge) {
    liveBadge.textContent = '● LIVE';
    liveBadge.classList.remove('is-recorded');
    liveBadge.title = `reading ${gatewayUrl()}/audit`;
  }
  paint(entries.map((e) => ({
    at: clock(e.at),
    tool: e.tool,
    decision: e.decision,
    amount: rupees(e.amount_paise),
    reason: e.reason,
  })));
}

if (terminal) {
  showLive().catch((error) =>
    showRecorded(`No gateway at ${gatewayUrl()} (${error.message}) — showing a recorded run.`));
  // Re-read while the page is open, so a run started in the console shows up
  // here. Cheap, and it stops only when the tab is hidden.
  setInterval(() => {
    if (document.visibilityState === 'visible') showLive().catch(() => {});
  }, 5000);
}
