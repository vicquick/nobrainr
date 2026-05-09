import { marked } from 'marked'
import DOMPurify from 'dompurify'

/**
 * Render a memory's text content as sanitised HTML.
 *
 * Pipeline: marked → DOMPurify. Memory content comes from a wide
 * range of untrusted sources (chatgpt exports, claude conversations,
 * web crawls, manual stores), so the sanitiser is non-negotiable —
 * marked alone will faithfully reproduce a `<script>` block written
 * inside a code fence.
 *
 * marked is configured for GFM (tables, strikethrough, task lists,
 * fenced code) with breaks=true so a single newline becomes <br/>
 * — most memory content is paragraph-style notes, not strict
 * markdown documents.
 *
 * DOMPurify is configured to allow only the tags marked actually
 * emits. No <iframe>, no <object>, no <form> — the kind of payload
 * an attacker would smuggle through a markdown body.
 *
 * If the input doesn't look like markdown (no #, *, _, `, [, >),
 * fast-path to escapeHtml + <br/> wraps so we don't pay the parser
 * cost on plain text and don't add stray <p> wrappers.
 */

marked.setOptions({
  gfm: true,
  breaks: true,
})

const ALLOWED_TAGS = [
  'p', 'br', 'hr',
  'strong', 'em', 'b', 'i', 'u', 's', 'del', 'ins',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'a',
  'ul', 'ol', 'li',
  'blockquote',
  'pre', 'code',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'span', 'div',
  'mark',
  'input', // task-list checkboxes
]

const ALLOWED_ATTR = ['href', 'title', 'target', 'rel', 'class', 'type', 'checked', 'disabled']

const HAS_MARKDOWN_RE = /[#*_`[>]|^\s*-\s|^\s*\d+\.\s/m

const HTML_ENTITIES: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}
function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (ch) => HTML_ENTITIES[ch])
}

export function looksLikeMarkdown(text: string): boolean {
  if (!text) return false
  return HAS_MARKDOWN_RE.test(text)
}

export function renderMemoryMarkdown(text: string | null | undefined): string {
  const src = (text ?? '').trim()
  if (!src) return ''

  if (!looksLikeMarkdown(src)) {
    // Plain text — preserve newlines without invoking marked's
    // paragraph-wrapping so single-line summaries don't get an
    // unnecessary <p> shell.
    return escapeHtml(src).replace(/\n/g, '<br/>')
  }

  // marked.parse can be sync (string) or async (Promise) depending on
  // version + options. We never set options that trigger async, but
  // narrow the type defensively.
  const raw = marked.parse(src, { async: false }) as string

  return DOMPurify.sanitize(raw, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ADD_ATTR: ['target'],
    ALLOW_DATA_ATTR: false,
    USE_PROFILES: { html: true },
  })
}
