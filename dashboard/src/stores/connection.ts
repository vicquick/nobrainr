import { ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * Is the backend answering?
 *
 * Measured 2026-11-10 with the API unreachable: none of the twelve views
 * said so. Lists stayed skeletons forever, canvases stayed black, every view
 * threw an unhandled rejection. Views do not have to learn about outages one
 * by one: the axios client and window.fetch report failed /api calls here,
 * the store checks whether the backend as a whole is gone, and the banner
 * (ConnectionBanner.vue) says so.
 *
 *  - backend gone   -> `down`; probed again every few seconds; when it is
 *                      back, `generation` goes up and App.vue remounts the
 *                      current view so it loads its data again.
 *  - backend there, one request failed -> `hiccup` for a few seconds, with
 *                      the option to reload the view.
 */

/** Any answer below 500 proves the backend process is there — a 404 is fine. */
const PROBE_URL = '/api/health'
const PROBE_TIMEOUT_MS = 4000
const RETRY_MS = [2000, 4000, 8000, 15000]
const HICCUP_MS = 8000

async function backendAnswers(rawFetch: typeof fetch): Promise<boolean> {
  const abort = new AbortController()
  const timer = setTimeout(() => abort.abort(), PROBE_TIMEOUT_MS)
  try {
    const res = await rawFetch(PROBE_URL, { cache: 'no-store', signal: abort.signal })
    return res.status < 500
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

export const useConnectionStore = defineStore('connection', () => {
  const down = ref(false)
  const hiccup = ref(false)
  const checking = ref(false)
  /** Bumped when the views should load their data again. */
  const generation = ref(0)

  // The un-wrapped fetch: probes must not report themselves as failures.
  let rawFetch: typeof fetch = (...args) => window.fetch(...args)
  let attempt = 0
  let retryTimer: ReturnType<typeof setTimeout> | undefined
  let hiccupTimer: ReturnType<typeof setTimeout> | undefined
  let probing: Promise<void> | null = null

  function useRawFetch(f: typeof fetch) {
    rawFetch = f
  }

  function scheduleRetry() {
    clearTimeout(retryTimer)
    const wait = RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)]
    attempt += 1
    retryTimer = setTimeout(() => void probe(), wait)
  }

  function probe(): Promise<void> {
    if (probing) return probing
    checking.value = true
    probing = backendAnswers(rawFetch)
      .then((alive) => {
        if (alive) {
          clearTimeout(retryTimer)
          attempt = 0
          if (down.value) {
            down.value = false
            generation.value += 1
          }
        } else {
          down.value = true
          hiccup.value = false
          scheduleRetry()
        }
      })
      .finally(() => {
        checking.value = false
        probing = null
      })
    return probing
  }

  /** A request to /api failed (no answer, or a 5xx). */
  function reportFailure() {
    if (down.value) return
    void probe().then(() => {
      if (down.value) return
      hiccup.value = true
      clearTimeout(hiccupTimer)
      hiccupTimer = setTimeout(() => (hiccup.value = false), HICCUP_MS)
    })
  }

  /** The user asked: check now (when down) or load the view again (after a hiccup). */
  function retryNow() {
    if (down.value) {
      attempt = 0
      void probe()
    } else {
      hiccup.value = false
      generation.value += 1
    }
  }

  // Coming back online or back to the tab is the moment to look again.
  if (typeof window !== 'undefined') {
    window.addEventListener('online', () => down.value && retryNow())
    document.addEventListener('visibilitychange', () => {
      if (down.value && document.visibilityState === 'visible') retryNow()
    })
  }

  return { down, hiccup, checking, generation, reportFailure, retryNow, useRawFetch }
})
