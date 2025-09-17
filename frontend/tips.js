// Explain-on-hover, for a console full of words that mean something specific.
//
// "Held" is not "pending", a bound is not a setting, and the counterparty tally
// is not a star rating. Every one of those distinctions is the point of the
// project and none of them survive being guessed at from a four-letter label —
// so anything with a `data-tip` explains itself where it sits, rather than in a
// paragraph somewhere else that nobody reads.
//
// One listener, delegated. Tips are declared in the markup next to the thing
// they describe, so a control and its explanation cannot drift apart the way
// they would if this were a lookup table keyed by id.

const bubble = document.getElementById('tip')

let anchor = null
let timer = null

const GAP = 10

function place(target) {
  const box = target.getBoundingClientRect()
  bubble.hidden = false
  bubble.classList.remove('is-on')
  // Measure after unhiding, before positioning: a hidden element has no size.
  const tip = bubble.getBoundingClientRect()

  // Above by default, below when there is no room — a tip that hangs off the
  // top of the viewport is worse than no tip.
  const above = box.top - tip.height - GAP
  const below = box.bottom + GAP
  const flipped = above < 8
  let top = flipped ? below : above

  let left = box.left + box.width / 2 - tip.width / 2
  left = Math.max(8, Math.min(left, window.innerWidth - tip.width - 8))

  bubble.style.top = `${Math.round(top)}px`
  bubble.style.left = `${Math.round(left)}px`
  bubble.style.setProperty('--arrow', `${Math.round(box.left + box.width / 2 - left)}px`)
  bubble.classList.toggle('is-below', flipped)
  requestAnimationFrame(() => bubble.classList.add('is-on'))
}

function show(target) {
  const text = target.dataset.tip
  if (!text) return
  const title = target.dataset.tipTitle
  bubble.innerHTML = ''
  if (title) {
    const h = document.createElement('b')
    h.className = 'tip-title'
    h.textContent = title
    bubble.append(h)
  }
  const p = document.createElement('span')
  p.textContent = text
  bubble.append(p)
  anchor = target
  place(target)
}

function hide() {
  anchor = null
  clearTimeout(timer)
  bubble.classList.remove('is-on')
  // Kept in the DOM through the fade, then taken out of the a11y tree.
  timer = setTimeout(() => { if (!anchor) bubble.hidden = true }, 120)
}

function armed(event) {
  return event.target.closest?.('[data-tip]') ?? null
}

document.addEventListener('pointerover', (event) => {
  const target = armed(event)
  if (!target || target === anchor) return
  clearTimeout(timer)
  // A short delay, so sweeping the cursor across a row of chips does not strobe.
  timer = setTimeout(() => show(target), 160)
})

document.addEventListener('pointerout', (event) => {
  const target = armed(event)
  if (target && target === anchor && !target.contains(event.relatedTarget)) hide()
  else if (target && target !== anchor) clearTimeout(timer)
})

// A field you are typing in is focused to be USED, not to be explained, and a
// bubble that covers the thing you are writing into is worse than no help at
// all. So focus opens a tip on controls; on text fields, hover still does.
const typing = (el) =>
  el.matches?.('input, textarea, [contenteditable]') ||
  Boolean(el.querySelector?.('input, textarea'))

// Keyboard parity. A tip only reachable with a mouse is a tip half the point of
// which — explaining an unfamiliar interface — has been thrown away.
document.addEventListener('focusin', (event) => {
  const target = armed(event)
  if (target && !typing(event.target)) show(target)
})
// And once a key lands, the explanation gets out of the way.
document.addEventListener('input', () => { if (anchor) hide() })
document.addEventListener('focusout', () => { if (anchor) hide() })
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && anchor) hide()
})
window.addEventListener('scroll', () => { if (anchor) hide() }, true)
window.addEventListener('resize', () => { if (anchor) hide() })
