/**
 * Cross-surface entity linkification.
 *
 * Walks already-sanitized HTML and wraps occurrences of known entity
 * canonical names in clickable anchors that route to /graph?focus=<id>.
 * Operates only on text nodes; never touches <code>, <pre>, <a>, or
 * existing <mark> wrappers (the highlight composable owns those).
 *
 * Why post-sanitization: the input has already passed through
 * marked → DOMPurify (see useMarkdown.ts), so we know the DOM is
 * trusted. We then only ADD anchors with no user-supplied attributes
 * besides data-entity-id (uuid format) — XSS surface stays nil.
 *
 * The anchor click is delegated by the parent (MemoryDetail listens on
 * .cp-prose) — we don't bind handlers in this util because v-html
 * can't carry Vue event listeners.
 */

interface EntityLike {
  id: string
  canonical_name: string
}

const SKIP_TAGS = new Set(['CODE', 'PRE', 'A', 'SCRIPT', 'STYLE', 'MARK'])

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function isUuid(s: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s)
}

/**
 * Walk the text nodes of `root` (skipping SKIP_TAGS), wrap every entity
 * canonical name occurrence in an <a class="cp-entity-link"> element.
 * Mutates `root` in place.
 */
function walkAndWrap(
  root: Node,
  pattern: RegExp,
  nameToId: Map<string, string>,
): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node: Node): number {
      let p: Node | null = node.parentNode
      while (p && p.nodeType === Node.ELEMENT_NODE) {
        if (SKIP_TAGS.has((p as Element).tagName)) return NodeFilter.FILTER_REJECT
        p = p.parentNode
      }
      return NodeFilter.FILTER_ACCEPT
    },
  })

  const targets: Text[] = []
  let n: Node | null
  while ((n = walker.nextNode())) targets.push(n as Text)

  for (const textNode of targets) {
    const text = textNode.nodeValue || ''
    if (!text || !pattern.test(text)) {
      pattern.lastIndex = 0
      continue
    }
    pattern.lastIndex = 0
    const frag = document.createDocumentFragment()
    let last = 0
    let m: RegExpExecArray | null
    while ((m = pattern.exec(text))) {
      const matchStart = m.index
      const matchEnd = matchStart + m[0].length
      if (matchStart > last) {
        frag.appendChild(document.createTextNode(text.slice(last, matchStart)))
      }
      const id = nameToId.get(m[0].toLowerCase())
      if (id && isUuid(id)) {
        const a = document.createElement('a')
        a.className = 'cp-entity-link'
        a.setAttribute('data-entity-id', id)
        a.setAttribute('href', `/graph?focus=${id}`)
        a.textContent = m[0]
        frag.appendChild(a)
      } else {
        frag.appendChild(document.createTextNode(m[0]))
      }
      last = matchEnd
    }
    if (last < text.length) {
      frag.appendChild(document.createTextNode(text.slice(last)))
    }
    textNode.parentNode?.replaceChild(frag, textNode)
  }
}

/**
 * Wrap entity-name mentions in already-sanitized HTML.
 *
 * Uses DOMParser so the input HTML must already be safe (do not pass
 * raw user content here). Returns the body innerHTML — caller v-html's
 * the result.
 *
 * Behavior:
 *  - Case-insensitive match, preserves original casing in display
 *  - Longest names first (prevents "GPT" eating "GPT-4")
 *  - Word-boundary required on both sides (no infix hits)
 *  - Names shorter than 3 chars are skipped (too noisy)
 *  - Skips text inside <code>, <pre>, <a>, <mark>
 *  - No-op if entities is empty or window/DOMParser unavailable
 */
export function linkifyEntities(html: string, entities?: EntityLike[]): string {
  if (!html || !entities || entities.length === 0) return html
  if (typeof window === 'undefined' || typeof DOMParser === 'undefined') return html

  // Build a name → id map (lowercase keys) and a sorted list for the
  // regex. Longest first so "GPT-4" wins before "GPT".
  const nameToId = new Map<string, string>()
  const candidates: string[] = []
  for (const e of entities) {
    if (!e.canonical_name || !e.id || !isUuid(e.id)) continue
    const name = e.canonical_name.trim()
    if (name.length < 3) continue
    const key = name.toLowerCase()
    if (nameToId.has(key)) continue
    nameToId.set(key, e.id)
    candidates.push(name)
  }
  if (!candidates.length) return html

  candidates.sort((a, b) => b.length - a.length)
  // (?<![\w-]) and (?![\w-]) approximate a word boundary that also
  // refuses to split through hyphens or underscores so "GPT-4" only
  // matches as a whole token, not the embedded "GPT".
  const pattern = new RegExp(
    `(?<![\\w-])(${candidates.map(escapeRegex).join('|')})(?![\\w-])`,
    'gi',
  )

  let parsed: Document
  try {
    parsed = new DOMParser().parseFromString(`<div>${html}</div>`, 'text/html')
  } catch {
    return html
  }

  const root = parsed.body.firstElementChild
  if (!root) return html
  walkAndWrap(root, pattern, nameToId)
  return root.innerHTML
}
