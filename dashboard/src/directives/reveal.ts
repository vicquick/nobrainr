import type { Directive } from 'vue'

/**
 * v-reveal — IntersectionObserver-driven, fire-once scroll reveal.
 *
 * Usage:
 *   <section v-reveal>...</section>
 *
 * The element starts with `data-reveal="off"` (CSS handles the
 * initial hidden state via [data-reveal="off"]). When it enters the
 * viewport, the attribute flips to `"on"`, the CSS transitions
 * opacity + translateY, and the observer disconnects. No re-hiding
 * on scroll-up — those waterfall every-time effects exhaust quickly.
 *
 * Honors prefers-reduced-motion: skips observation, attribute starts
 * "on" so the element is fully visible immediately.
 *
 * Style hookups live in /styles/motion.css (.cp-reveal-* rules).
 */
const REVEAL_ATTR = 'data-reveal'

export const reveal: Directive<HTMLElement> = {
  mounted(el) {
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduced || typeof IntersectionObserver === 'undefined') {
      el.setAttribute(REVEAL_ATTR, 'on')
      return
    }
    el.setAttribute(REVEAL_ATTR, 'off')
    const obs = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            el.setAttribute(REVEAL_ATTR, 'on')
            obs.disconnect()
            break
          }
        }
      },
      // Start the reveal a bit before the element fully enters — a
      // 60px bottom margin means it animates as the reader scrolls
      // toward it, not after it's already on screen.
      { rootMargin: '0px 0px -60px 0px', threshold: 0.05 },
    )
    obs.observe(el)
  },
}
