// Vite plugin: ship only the Material Design Icons this app uses.
//
// `@mdi/font` is 7,400 icons: a 403 KB woff2 (plus eot/ttf/woff copies, 3.2 MB in dist) and ~330 KB of
// CSS classes, loaded on every page for the ~100 icons the dashboard shows. On `vite build` this plugin
// replaces `@mdi/font/css/materialdesignicons.css` with the same stylesheet reduced to the icons found
// in the sources (plus Vuetify's built-in aliases) and a subset font inlined as a data URI — no extra
// request, no flash of missing icons.
//
// Icons are found by scanning for the literal `mdi-<name>`. An icon name assembled at runtime
// (`'mdi-' + x`) would be missed; none exists today, and such a name must be listed in ALWAYS below.
// `vite dev` is untouched (full font), so a new icon shows up without a restart.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import type { Plugin } from 'vite'
import subsetFont from 'subset-font'

const ALWAYS: string[] = []
const NAME = /mdi-[a-z0-9]+(?:-[a-z0-9]+)*/g
const SCANNED = /\.(vue|ts|tsx|js|css|html)$/

function sources(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry)
    if (statSync(path).isDirectory()) sources(path, out)
    else if (SCANNED.test(entry)) out.push(path)
  }
  return out
}

export function mdiSubset(): Plugin {
  let build = false
  let root = process.cwd()
  return {
    name: 'nobrainr:mdi-subset',
    enforce: 'pre',
    configResolved(config) {
      build = config.command === 'build'
      root = config.root
    },
    async load(id) {
      const file = id.split('?')[0]
      if (!build || !file.replace(/\\/g, '/').endsWith('@mdi/font/css/materialdesignicons.css')) return null

      const css = readFileSync(file, 'utf8')
      const rule = /\.(mdi-[a-z0-9-]+)::before\s*\{\s*content:\s*"\\([0-9A-Fa-f]+)";?\s*\}\s*/g
      const codepoint = new Map<string, number>()
      for (const m of css.matchAll(rule)) codepoint.set(m[1], parseInt(m[2], 16))

      const scanned = [
        ...sources(resolve(root, 'src')),
        resolve(root, 'index.html'),
        resolve(root, 'node_modules/vuetify/lib/iconsets/mdi.js'),
      ]
      const used = new Set<string>(ALWAYS)
      for (const path of scanned) {
        for (const name of readFileSync(path, 'utf8').match(NAME) ?? []) {
          if (codepoint.has(name)) used.add(name)
        }
      }
      const unknown = ALWAYS.filter((name) => !codepoint.has(name))
      if (unknown.length) this.error(`mdi-subset: not in @mdi/font: ${unknown.join(', ')}`)
      if (used.size < 20) this.error(`mdi-subset: only ${used.size} icons found — scan is broken, refusing to ship blank icons`)

      const glyphs = [...used].map((name) => String.fromCodePoint(codepoint.get(name)!)).join('')
      const full = readFileSync(resolve(dirname(file), '../fonts/materialdesignicons-webfont.woff2'))
      const font = await subsetFont(full, glyphs, { targetFormat: 'woff2' })
      if (font.length < 1024) this.error(`mdi-subset: subset font is ${font.length} bytes — refusing to ship it`)

      const face = [
        '@font-face {',
        '  font-family: "Material Design Icons";',
        `  src: url("data:font/woff2;base64,${font.toString('base64')}") format("woff2");`,
        '  font-weight: normal;',
        '  font-style: normal;',
        '  font-display: block;',
        '}',
        '',
      ].join('\n')
      const reduced = css
        .replace(/@font-face\s*\{[^}]*\}\s*/, face)
        .replace(rule, (whole, name: string) => (used.has(name) ? whole : ''))
        .replace(/\/\*# sourceMappingURL=.*?\*\//, '')
      if (!reduced.includes('data:font/woff2')) this.error('mdi-subset: @font-face block not found in @mdi/font CSS')

      this.info(`mdi-subset: ${used.size} of ${codepoint.size} icons, font ${(font.length / 1024).toFixed(1)} KB (full: ${(full.length / 1024).toFixed(0)} KB), css ${(reduced.length / 1024).toFixed(1)} KB (full: ${(css.length / 1024).toFixed(0)} KB)`)
      return { code: reduced, map: null }
    },
  }
}
