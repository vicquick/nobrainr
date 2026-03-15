import { ref } from 'vue'
import { defineStore } from 'pinia'
import type { ChatMessage, ChatSources } from '@/types'

// Sentence boundary: period/exclamation/question followed by space, newline, or end
const SENTENCE_RE = /[.!?]\s+|[.!?]$/

export const useChatStore = defineStore('chat', () => {
  const isOpen = ref(false)
  const messages = ref<ChatMessage[]>([])
  const isStreaming = ref(false)
  const isThinking = ref(false)
  const isSpeaking = ref(false)
  const voiceMode = ref(true)  // voice responses on by default
  const currentSources = ref<ChatSources | null>(null)
  const focusEntityId = ref<string | null>(null)

  // Audio queue for streaming TTS
  let audioQueue: string[] = []  // blob URLs
  let isPlayingQueue = false
  let currentAudio: HTMLAudioElement | null = null
  let ttsAborted = false

  function toggle() { isOpen.value = !isOpen.value }
  function open() { isOpen.value = true }
  function close() { isOpen.value = false }

  function clearSources() { currentSources.value = null }
  function focusEntity(id: string) { focusEntityId.value = id }
  function clearFocus() { focusEntityId.value = null }
  function toggleVoiceMode() { voiceMode.value = !voiceMode.value }

  function stopSpeaking() {
    ttsAborted = true
    if (currentAudio) {
      currentAudio.pause()
      currentAudio = null
    }
    audioQueue = []
    isPlayingQueue = false
    isSpeaking.value = false
  }

  async function playNextInQueue() {
    if (ttsAborted || audioQueue.length === 0) {
      isPlayingQueue = false
      if (audioQueue.length === 0) isSpeaking.value = false
      return
    }
    isPlayingQueue = true
    isSpeaking.value = true
    const url = audioQueue.shift()!
    currentAudio = new Audio(url)
    currentAudio.onended = () => {
      URL.revokeObjectURL(url)
      currentAudio = null
      playNextInQueue()
    }
    currentAudio.onerror = () => {
      URL.revokeObjectURL(url)
      currentAudio = null
      playNextInQueue()
    }
    try { await currentAudio.play() } catch { playNextInQueue() }
  }

  async function enqueueTTS(text: string) {
    if (!text.trim() || ttsAborted) return
    try {
      const baseUrl = import.meta.env.VITE_API_BASE || ''
      const res = await fetch(`${baseUrl}/api/tts`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }),
      })
      if (!res.ok || ttsAborted) return
      const blob = await res.blob()
      if (ttsAborted) return
      const url = URL.createObjectURL(blob)
      audioQueue.push(url)
      // Start playing if not already
      if (!isPlayingQueue) playNextInQueue()
    } catch {
      // TTS failed for this chunk, skip
    }
  }

  // Speak full text (for replay button)
  async function speakText(text: string) {
    if (!text.trim()) return
    stopSpeaking()
    ttsAborted = false
    // Split into sentences for chunked playback
    const sentences = splitIntoSentences(text)
    isSpeaking.value = true
    for (const s of sentences) {
      if (ttsAborted) break
      await enqueueTTS(s)
    }
  }

  function splitIntoSentences(text: string): string[] {
    const sentences: string[] = []
    let remaining = text.trim()
    while (remaining.length > 0) {
      const match = SENTENCE_RE.exec(remaining)
      if (match && match.index < 200) {
        // Found sentence boundary within reasonable length
        const end = match.index + match[0].length
        sentences.push(remaining.slice(0, end).trim())
        remaining = remaining.slice(end).trim()
      } else if (remaining.length > 200) {
        // No boundary found, break at last space before 200 chars
        const spaceIdx = remaining.lastIndexOf(' ', 200)
        const breakAt = spaceIdx > 50 ? spaceIdx : 200
        sentences.push(remaining.slice(0, breakAt).trim())
        remaining = remaining.slice(breakAt).trim()
      } else {
        sentences.push(remaining)
        remaining = ''
      }
    }
    return sentences.filter(s => s.length > 0)
  }

  async function sendMessage(text: string, images?: string[], displayImages?: string[]) {
    if (!text.trim() || isStreaming.value) return

    messages.value.push({
      id: crypto.randomUUID(),
      role: 'user',
      content: text.trim(),
      images: displayImages?.length ? displayImages : undefined,
      timestamp: Date.now(),
    })

    const assistantMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
    }
    messages.value.push(assistantMsg)
    isStreaming.value = true
    isThinking.value = true
    currentSources.value = null

    // Reset TTS state for streaming
    ttsAborted = false
    audioQueue = []
    isPlayingQueue = false
    let sentenceBuffer = ''

    const baseUrl = import.meta.env.VITE_API_BASE || ''
    const history = messages.value
      .slice(0, -2)
      .slice(-20)
      .map(m => ({ role: m.role, content: m.content }))

    try {
      const res = await fetch(`${baseUrl}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text.trim(), history, ...(images?.length ? { images } : {}) }),
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
            if (event.type === 'thinking') {
              isThinking.value = true
            } else if (event.type === 'token') {
              isThinking.value = false
              assistantMsg.content += event.content

              // Streaming TTS: accumulate tokens, send at sentence boundaries
              if (voiceMode.value && !ttsAborted) {
                sentenceBuffer += event.content
                // Check for sentence boundary in buffer
                const match = SENTENCE_RE.exec(sentenceBuffer)
                if (match && sentenceBuffer.length > 15) {
                  const end = match.index + match[0].length
                  const sentence = sentenceBuffer.slice(0, end).trim()
                  sentenceBuffer = sentenceBuffer.slice(end)
                  if (sentence.length > 5) {
                    enqueueTTS(sentence)  // fire-and-forget, don't await
                  }
                }
              }
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
      isThinking.value = false

      // Flush remaining sentence buffer to TTS
      if (voiceMode.value && sentenceBuffer.trim().length > 5 && !ttsAborted) {
        enqueueTTS(sentenceBuffer.trim())
      }
    }
  }

  function clearHistory() {
    messages.value = []
    currentSources.value = null
    stopSpeaking()
  }

  return {
    isOpen, messages, isStreaming, isThinking, isSpeaking, voiceMode,
    currentSources, focusEntityId,
    toggle, open, close, clearSources, focusEntity, clearFocus,
    toggleVoiceMode, stopSpeaking, speakText,
    sendMessage, clearHistory,
  }
})
