import type { AxiosError, AxiosInstance } from 'axios'
import { useConnectionStore } from '@/stores/connection'

/** No answer at all, or the server (or a proxy in front of it) failed. */
function isOutage(status: number | undefined): boolean {
  return status === undefined || status >= 500
}

function isApiUrl(input: RequestInfo | URL): boolean {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  try {
    const u = new URL(url, window.location.origin)
    return u.origin === window.location.origin && u.pathname.startsWith('/api/')
  } catch {
    return false
  }
}

/**
 * Report failed /api calls to the connection store — from the axios client
 * and from plain fetch(), which about half the views use. Callers still get
 * their rejection or response exactly as before.
 */
export function watchConnection(api: AxiosInstance) {
  const connection = useConnectionStore()

  api.interceptors.response.use(undefined, (error: AxiosError) => {
    const cancelled = error.code === 'ERR_CANCELED'
    if (!cancelled && isOutage(error.response?.status)) connection.reportFailure()
    return Promise.reject(error)
  })

  const rawFetch = window.fetch.bind(window)
  connection.useRawFetch(rawFetch)
  window.fetch = async (input, init) => {
    try {
      const res = await rawFetch(input, init)
      if (res.status >= 500 && isApiUrl(input)) connection.reportFailure()
      return res
    } catch (error) {
      const aborted = error instanceof DOMException && error.name === 'AbortError'
      if (!aborted && isApiUrl(input)) connection.reportFailure()
      throw error
    }
  }
}
