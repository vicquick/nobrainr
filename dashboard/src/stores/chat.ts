import { ref } from 'vue'
import { defineStore } from 'pinia'
import type { ChatMessage, ChatSources } from '@/types'

export const useChatStore = defineStore('chat', () => {
  const isOpen = ref(false)
  const messages = ref<ChatMessage[]>([])
  const isStreaming = ref(false)
  const currentSources = ref<ChatSources | null>(null)

  function toggle() { isOpen.value = !isOpen.value }
  function open() { isOpen.value = true }
  function close() { isOpen.value = false }

  function clearSources() { currentSources.value = null }

  async function sendMessage(text: string) {
    if (!text.trim() || isStreaming.value) return

    // Add user message
    messages.value.push({
      id: crypto.randomUUID(),
      role: 'user',
      content: text.trim(),
      timestamp: Date.now(),
    })

    // Add empty assistant message for streaming
    const assistantMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
    }
    messages.value.push(assistantMsg)
    isStreaming.value = true
    currentSources.value = null

    const baseUrl = import.meta.env.VITE_API_BASE || ''
    const history = messages.value
      .slice(0, -2) // exclude current pair
      .slice(-20)
      .map(m => ({ role: m.role, content: m.content }))

    try {
      const res = await fetch(`${baseUrl}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text.trim(), history }),
      })

      if (!res.ok || !res.body) {
        assistantMsg.content = 'Sorry, something went wrong. Please try again.'
        isStreaming.value = false
        return
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          try {
            const event = JSON.parse(line.slice(6))
            if (event.type === 'token') {
              assistantMsg.content += event.content
            } else if (event.type === 'sources') {
              assistantMsg.sources = { memories: event.memories, entities: event.entities }
              currentSources.value = assistantMsg.sources
            } else if (event.type === 'error') {
              assistantMsg.content += event.message || 'An error occurred.'
            }
          } catch {
            // skip malformed SSE
          }
        }
      }
    } catch {
      assistantMsg.content = 'Connection error. Please check your network.'
    } finally {
      isStreaming.value = false
    }
  }

  function clearHistory() {
    messages.value = []
    currentSources.value = null
  }

  return {
    isOpen, messages, isStreaming, currentSources,
    toggle, open, close, clearSources,
    sendMessage, clearHistory,
  }
})
