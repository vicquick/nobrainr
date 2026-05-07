/**
 * Tiny helper: returns the `style` object a list child needs to
 * participate in the global `.cp-stagger` CSS reveal.
 *
 * Usage:
 *   <ul class="cp-stagger">
 *     <li v-for="(x, i) in xs" :style="staggerStyle(i)"> … </li>
 *
 * Caps the delay so very long lists don't end with multi-second
 * waterfalls (exhausting after revisits). 14 items × 60ms = 840ms
 * total reveal — anything beyond that lands instantly.
 */
const STEP_MS = 60
const CAP = 14

export function staggerStyle(i: number): Record<string, string> {
  const idx = Math.min(i, CAP)
  return { '--i': String(idx) }
}
