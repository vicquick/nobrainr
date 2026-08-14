import { createRouter, createWebHistory } from 'vue-router'

/**
 * Detect "chunk failed to load" / "Failed to fetch dynamically imported
 * module" errors raised when vue-router tries to import a route whose
 * hashed asset filename has changed since the user's tab loaded
 * index.html. Triggered by a redeploy while a tab is still open.
 *
 * The error shape differs across browsers + bundlers:
 *   - Chrome/Edge:  "Failed to fetch dynamically imported module"
 *   - Firefox:      "error loading dynamically imported module"
 *   - Safari:       "Importing a module script failed."
 *   - Vite legacy:  "Failed to load module" / "ChunkLoadError"
 * We match loosely so a future bundler tweak doesn't silently bypass
 * the recovery path.
 */
function isChunkLoadError(err: unknown): boolean {
  if (!err) return false
  const msg = err instanceof Error ? err.message : String(err)
  return /dynamically imported module|chunk(load)?|importing a module script|failed to load module/i.test(msg)
}

/**
 * Single-flight reload guard. Without this, two near-simultaneous
 * route attempts (or a router push triggered while the error is
 * already being handled) would reload twice. sessionStorage carries
 * the flag across the about-to-happen page load so we know if the
 * reload already happened — if so, the next failure surfaces as a
 * real error rather than an infinite reload loop.
 */
const RELOAD_FLAG = 'nobrainr.chunkReloadAttempted'
function attemptReload(): void {
  if (typeof window === 'undefined') return
  try {
    if (sessionStorage.getItem(RELOAD_FLAG)) return
    sessionStorage.setItem(RELOAD_FLAG, '1')
  } catch {
    // sessionStorage unavailable (private mode, etc.) — try anyway.
  }
  window.location.reload()
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: '/galaxy',
    },
    {
      path: '/galaxy',
      name: 'galaxy',
      component: () => import('@/views/GalaxyView.vue'),
    },
    {
      path: '/graph',
      name: 'graph',
      component: () => import('@/views/GraphView.vue'),
    },
    {
      path: '/constellarium',
      name: 'constellarium',
      component: () => import('@/views/ConstellationView.vue'),
    },
    {
      path: '/memories',
      name: 'memories',
      component: () => import('@/views/MemoriesView.vue'),
    },
    {
      path: '/timeline',
      name: 'timeline',
      component: () => import('@/views/TimelineView.vue'),
    },
    {
      path: '/scheduler',
      name: 'scheduler',
      component: () => import('@/views/SchedulerView.vue'),
    },
    {
      path: '/pulse',
      name: 'pulse',
      component: () => import('@/views/PulseView.vue'),
    },
    {
      path: '/library',
      name: 'library',
      component: () => import('@/views/LibraryView.vue'),
    },
    {
      path: '/commonplace',
      name: 'commonplace',
      component: () => import('@/views/CommonplaceView.vue'),
    },
    {
      path: '/insights',
      name: 'insights',
      component: () => import('@/views/InsightsView.vue'),
    },
    {
      path: '/threads',
      name: 'threads',
      component: () => import('@/views/ThreadsView.vue'),
    },
    {
      path: '/threads/:id',
      name: 'thread-detail',
      component: () => import('@/views/ThreadDetailView.vue'),
    },
  ],
})

// Vue Router's onError fires when a navigation guard or a lazy route
// import rejects. We hook only the chunk-load case; everything else is
// re-thrown so dev-tools / Sentry / etc. still see real route bugs.
router.onError((err) => {
  if (isChunkLoadError(err)) {
    attemptReload()
    return
  }
  throw err
})

// Clear the reload flag on a successful navigation — once the new
// build is loaded we want a future stale-tab scenario (next redeploy)
// to be allowed to recover again.
router.afterEach(() => {
  if (typeof window === 'undefined') return
  try { sessionStorage.removeItem(RELOAD_FLAG) } catch { /* ignore */ }
})

// Belt-and-suspenders: when a lazy chunk fails OUTSIDE a route nav
// (e.g. a dynamically imported component inside a view), the browser
// fires an unhandledrejection. Reload on the same condition.
if (typeof window !== 'undefined') {
  window.addEventListener('unhandledrejection', (event) => {
    if (isChunkLoadError(event.reason)) {
      event.preventDefault()
      attemptReload()
    }
  })
}

export default router
