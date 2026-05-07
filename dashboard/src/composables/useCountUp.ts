import { ref, watch, type Ref } from 'vue'

/**
 * rAF-based number ticker. Animates from a starting value to the target
 * over `duration` ms with cubic ease-out. Honors prefers-reduced-motion
 * by snapping immediately to the target.
 *
 * Usage:
 *   const total = computed(() => stats.value?.total_memories ?? 0)
 *   const display = useCountUp(total, { duration: 600 })
 *   // template: {{ display.toLocaleString() }}
 *
 * Re-runs only when the *first* meaningful (>0) value arrives so we
 * don't re-tick on every poll. Subsequent value changes are applied
 * directly without animation — that keeps live data honest.
 */
export function useCountUp(
  target: Ref<number | null | undefined>,
  opts: { duration?: number; easing?: (t: number) => number } = {},
) {
  const duration = opts.duration ?? 600
  const easing = opts.easing ?? ((t: number) => 1 - Math.pow(1 - t, 3))
  const display = ref(0)
  let primed = false
  let raf = 0

  const reduced =
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  watch(
    target,
    (next) => {
      const n = Number(next ?? 0)
      if (!Number.isFinite(n)) return

      if (!primed && n > 0) {
        primed = true
        if (reduced) {
          display.value = n
          return
        }
        const start = performance.now()
        const from = 0
        cancelAnimationFrame(raf)
        const tick = (now: number) => {
          const t = Math.min(1, (now - start) / duration)
          display.value = Math.round(from + (n - from) * easing(t))
          if (t < 1) raf = requestAnimationFrame(tick)
        }
        raf = requestAnimationFrame(tick)
      } else {
        // Subsequent updates: snap. Live data shouldn't re-tick.
        display.value = n
      }
    },
    { immediate: true },
  )

  return display
}
