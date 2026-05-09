/**
 * Search-result highlighting — escape, then wrap query terms in <mark>.
 *
 * Always HTML-escapes the source text BEFORE inserting any markup, so
 * memory content can carry literal `<script>` etc. without becoming
 * executable. The query string is also HTML-escaped before the regex
 * pattern is built, so a user typing `<` won't accidentally match the
 * synthetic `&lt;` entity in the escaped text — they get no highlight,
 * which is the correct degradation.
 *
 * Match semantics: case-insensitive substring on each whitespace-split
 * token of the query. Mirrors what readers expect from a search box —
 * loose, not full-text-tokenized.
 */

const HTML_ENTITIES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}

export function escapeHtml(s: string): string {
  if (!s) return ''
  return s.replace(/[&<>"']/g, (ch) => HTML_ENTITIES[ch])
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * Returns an HTML string with query tokens wrapped in
 * `<mark class="cp-mark">…</mark>`. Caller should v-html this output;
 * since both source text and query are escaped first, this is safe
 * against memory content that contains tag-like substrings.
 *
 * If query is empty / whitespace-only, returns just the escaped text.
 */
export function highlightMatches(text: string, query: string | null | undefined): string {
  const safeText = escapeHtml(text ?? '')
  if (!query || !query.trim()) return safeText

  const tokens = escapeHtml(query)
    .trim()
    .split(/\s+/)
    .filter((t) => t.length >= 2)        // skip 1-char noise (would mark every "a")
    .map(escapeRegex)
  if (!tokens.length) return safeText

  // Single regex for all tokens — one pass, alphabet ordered by length
  // descending so longer matches win when tokens overlap (e.g. user
  // types "scheduler sched" — the longer token's match takes the slot).
  tokens.sort((a, b) => b.length - a.length)
  const pattern = new RegExp(`(${tokens.join('|')})`, 'gi')
  return safeText.replace(pattern, '<mark class="cp-mark">$1</mark>')
}
