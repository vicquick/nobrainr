import { ref, onUnmounted } from 'vue'

export type SSEEvent = {
  type: string
  [key: string]: unknown
}

type SSEHandler = (event: SSEEvent) => void

const handlers = new Set<SSEHandler>()
let source: EventSource | null = null
const connected = ref(false)

function connect() {
  if (source) return

  const baseUrl = import.meta.env.VITE_API_BASE || ''
  source = new EventSource(`${baseUrl}/api/events`)

  source.onopen = () => {
    connected.value = true
  }

  source.onmessage = (e) => {
    try {
      const data: SSEEvent = JSON.parse(e.data)
      handlers.forEach((h) => h(data))
    } catch {
      // ignore malformed messages
    }
  }

  source.onerror = () => {
    connected.value = false
    source?.close()
    source = null
    // Reconnect after 3s
    setTimeout(() => {
      if (handlers.size > 0) connect()
    }, 3000)
  }
}

export function useSSE(handler: SSEHandler) {
  handlers.add(handler)
  connect()

  onUnmounted(() => {
    handlers.delete(handler)
    if (handlers.size === 0 && source) {
      source.close()
      source = null
      connected.value = false
    }
  })

  return { connected }
}
