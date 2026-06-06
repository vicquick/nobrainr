<template>
  <v-navigation-drawer
    v-model="chatStore.isOpen"
    location="right"
    :width="mobile ? '100%' : 420"
    temporary
    :scrim="mobile ? 'rgba(0,0,0,0.6)' : 'rgba(0,0,0,0.3)'"
    class="chat-panel"
    :class="{ 'chat-panel-mobile': mobile }"
    :touchless="false"
  >
    <div class="d-flex flex-column fill-height">
      <!-- Header -->
      <div class="chat-header d-flex align-center pa-3">
        <v-icon icon="mdi-chat-outline" size="18" class="mr-2" color="primary" />
        <span class="text-subtitle-2 font-weight-bold">Knowledge Chat</span>
        <v-spacer />
        <v-btn
          :icon="chatStore.voiceMode ? 'mdi-volume-high' : 'mdi-volume-off'"
          variant="text"
          size="x-small"
          :color="chatStore.voiceMode ? 'primary' : undefined"
          @click="chatStore.toggleVoiceMode()"
          title="Voice responses"
        />
        <v-btn
          v-if="chatStore.isSpeaking"
          icon="mdi-stop-circle"
          variant="text"
          size="x-small"
          color="error"
          @click="chatStore.stopSpeaking()"
          title="Stop speaking"
        />
        <v-btn
          icon="mdi-delete-outline"
          variant="text"
          size="x-small"
          :disabled="chatStore.messages.length === 0"
          @click="chatStore.clearHistory()"
        />
        <v-btn icon="mdi-close" variant="text" size="x-small" @click="chatStore.close()" />
      </div>

      <!-- Messages -->
      <div ref="messagesContainer" class="flex-grow-1 messages-area pa-3">
        <div v-if="chatStore.messages.length === 0" class="empty-state">
          <v-icon icon="mdi-brain" size="40" color="primary" class="mb-3" style="opacity: 0.3;" />
          <div class="text-caption text-medium-emphasis">
            Ask questions about the knowledge base
          </div>
        </div>

        <div
          v-for="msg in chatStore.messages"
          :key="msg.id"
          class="message-bubble"
          :class="msg.role"
        >
          <!-- Inline image preview for user messages.
               Images are stored as full data URLs (data:image/xxx;base64,...) preserving
               the original MIME type from the uploaded file. -->
          <div v-if="msg.images?.length" class="message-images mb-1">
            <img
              v-for="(img, idx) in msg.images"
              :key="idx"
              :src="img"
              class="message-image-thumb"
              @click="openImagePreview(img)"
            />
          </div>

          <div v-if="chatStore.isThinking && msg === lastAssistantMsg && !msg.content" class="thinking-state">
            <v-progress-circular indeterminate size="14" width="1.5" color="primary" class="mr-2" />
            <span class="text-caption text-medium-emphasis">{{ chatStore.thinkingStatus || 'Thinking...' }}</span>
          </div>
          <div v-else class="message-content">
            {{ msg.content }}<span v-if="chatStore.isStreaming && msg === lastAssistantMsg && !msg.content" class="typing-dot" />
            <v-btn
              v-if="msg.role === 'assistant' && msg.content && !chatStore.isStreaming"
              icon="mdi-volume-medium"
              variant="text"
              size="x-small"
              class="speak-btn ml-1"
              :loading="chatStore.isSpeaking"
              @click.stop="chatStore.speakText(msg.content)"
              title="Read aloud"
            />
          </div>

          <!-- Sources -->
          <div v-if="msg.sources && (msg.sources.entities.length || msg.sources.memories.length)" class="sources-section mt-2">
            <div
              class="sources-toggle d-flex align-center"
              @click="toggleSources(msg.id)"
            >
              <v-icon icon="mdi-link-variant" size="12" class="mr-1" />
              <span>{{ msg.sources.entities.length }} entities · {{ msg.sources.memories.length }} memories</span>
              <v-icon
                :icon="expandedSources.has(msg.id) ? 'mdi-chevron-up' : 'mdi-chevron-down'"
                size="14"
                class="ml-1"
              />
            </div>
            <div v-if="expandedSources.has(msg.id)" class="sources-list mt-1">
              <div
                v-for="entity in msg.sources.entities"
                :key="entity.id"
                class="source-entity"
                @click="focusSingleEntity(entity.id)"
              >
                <span class="entity-type-dot" :style="{ background: typeColor(entity.entity_type) }" />
                {{ entity.name }}
              </div>
              <div
                v-for="mem in msg.sources.memories.slice(0, 5)"
                :key="mem.id"
                class="source-memory"
              >
                {{ mem.summary || mem.content }}
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Input -->
      <div class="chat-input pa-3">
        <!-- Image preview -->
        <div v-if="pendingImages.length" class="image-preview-row d-flex ga-2 mb-2">
          <div v-for="(img, idx) in pendingImages" :key="idx" class="image-preview-item">
            <img :src="img.dataUrl" class="preview-thumb" />
            <v-btn
              icon="mdi-close-circle"
              size="x-small"
              variant="text"
              class="preview-remove"
              @click="removeImage(idx)"
            />
          </div>
        </div>
        <!-- Image size error -->
        <div v-if="imageError" class="text-caption text-error mb-1">{{ imageError }}</div>
        <div class="d-flex align-center ga-2">
          <v-btn
            icon="mdi-image-plus"
            size="small"
            variant="text"
            color="grey"
            :disabled="chatStore.isStreaming"
            @click="triggerFileInput"
          />
          <input
            ref="fileInput"
            type="file"
            accept="image/jpeg,image/png,image/gif,image/webp"
            multiple
            class="d-none"
            @change="onFileSelected"
          />
          <v-textarea
            ref="textareaRef"
            v-model="input"
            placeholder="Ask about the knowledge base..."
            variant="outlined"
            density="compact"
            rows="1"
            max-rows="4"
            auto-grow
            hide-details
            class="flex-grow-1"
            @keydown.enter.exact.prevent="send"
            @paste="onPaste"
          />
          <v-btn
            :icon="isRecording ? 'mdi-stop' : 'mdi-microphone'"
            :color="isRecording ? 'error' : 'default'"
            size="small"
            variant="tonal"
            :disabled="chatStore.isStreaming || isTranscribing"
            :loading="isTranscribing"
            :class="{ 'mic-recording': isRecording }"
            @click="toggleRecording"
          />
          <v-btn
            icon="mdi-send"
            color="primary"
            size="small"
            variant="tonal"
            :disabled="(!input.trim() && !pendingImages.length) || chatStore.isStreaming"
            :loading="chatStore.isStreaming"
            @click="send"
          />
        </div>
        <div v-if="micError" class="text-caption text-error mt-1">{{ micError }}</div>
      </div>
    </div>
    <!-- Full-size image preview dialog -->
    <v-dialog v-model="showImagePreview" max-width="90vw">
      <v-card color="#1a1a2e" class="pa-2">
        <v-btn icon="mdi-close" variant="text" size="small" class="float-right" @click="showImagePreview = false" />
        <v-img :src="previewImageSrc" max-height="80vh" contain />
      </v-card>
    </v-dialog>
  </v-navigation-drawer>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { useDisplay } from 'vuetify'
import { useChatStore } from '@/stores/chat'
import type { ChatSources } from '@/types'

// Canonical manuscript pigments — keep in sync with GraphView TYPE_COLORS
const TYPE_COLORS: Record<string, string> = {
  person: '#c98a6d', project: '#c9a96e', technology: '#7fa3c2',
  concept: '#a98bc0', file: '#8a8f98', config: '#a89a62',
  error: '#bd5a52', location: '#74a48d', organization: '#6f81ab',
}

const MAX_IMAGE_SIZE = 10 * 1024 * 1024 // 10 MB
const MAX_IMAGES = 5
const ACCEPTED_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp']

interface PendingImage {
  dataUrl: string   // for preview display
  base64: string    // raw base64 without data: prefix (for API)
}

const chatStore = useChatStore()
const { mobile } = useDisplay()

const input = ref('')
const messagesContainer = ref<HTMLElement | null>(null)
const expandedSources = ref(new Set<string>())
const fileInput = ref<HTMLInputElement | null>(null)
const textareaRef = ref<InstanceType<any> | null>(null)
const pendingImages = ref<PendingImage[]>([])
const imageError = ref('')
const showImagePreview = ref(false)
const previewImageSrc = ref('')

// Voice recording state
const isRecording = ref(false)
const isTranscribing = ref(false)
const micError = ref('')
let mediaRecorder: MediaRecorder | null = null
let micStream: MediaStream | null = null
let audioChunks: Blob[] = []

// Cleanup mic resources on unmount
onBeforeUnmount(() => {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop()
  }
  if (micStream) {
    micStream.getTracks().forEach(t => t.stop())
    micStream = null
  }
  mediaRecorder = null
})

async function toggleRecording() {
  if (isRecording.value) {
    stopRecording()
  } else {
    await startRecording()
  }
}

async function startRecording() {
  micError.value = ''
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    micStream = stream

    // Pick a supported mimeType (Safari doesn't support audio/webm)
    let mimeType = 'audio/webm'
    if (!MediaRecorder.isTypeSupported('audio/webm')) {
      if (MediaRecorder.isTypeSupported('audio/mp4')) {
        mimeType = 'audio/mp4'
      } else {
        stream.getTracks().forEach(t => t.stop())
        micStream = null
        micError.value = 'Audio recording not supported in this browser'
        return
      }
    }

    mediaRecorder = new MediaRecorder(stream, { mimeType })
    audioChunks = []

    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) audioChunks.push(event.data)
    }

    mediaRecorder.onstop = async () => {
      // Stop all tracks to release the microphone
      stream.getTracks().forEach(t => t.stop())
      micStream = null

      if (audioChunks.length === 0) return
      const ext = mimeType === 'audio/mp4' ? 'mp4' : 'webm'
      const blob = new Blob(audioChunks, { type: mimeType })
      await transcribe(blob, `recording.${ext}`)
    }

    mediaRecorder.start()
    isRecording.value = true
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === 'NotAllowedError') {
      micError.value = 'Microphone permission denied'
    } else if (err instanceof DOMException && err.name === 'NotFoundError') {
      micError.value = 'No microphone found'
    } else {
      micError.value = 'Could not access microphone'
    }
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop()
  }
  isRecording.value = false
}

async function transcribe(blob: Blob, filename = 'recording.webm') {
  isTranscribing.value = true
  micError.value = ''
  try {
    const formData = new FormData()
    formData.append('file', blob, filename)

    const baseUrl = import.meta.env.VITE_API_BASE || ''
    const res = await fetch(`${baseUrl}/api/transcribe`, {
      method: 'POST',
      body: formData,
    })

    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: 'Transcription failed' }))
      micError.value = err.error || 'Transcription failed'
      return
    }

    const data = await res.json()
    if (data.text) {
      input.value = input.value ? `${input.value} ${data.text}` : data.text
    }
  } catch {
    micError.value = 'Could not reach transcription service'
  } finally {
    isTranscribing.value = false
  }
}

const lastAssistantMsg = computed(() => {
  const msgs = chatStore.messages
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') return msgs[i]
  }
  return null
})

function typeColor(type: string) {
  return TYPE_COLORS[type] || '#8a8f98'
}

function toggleSources(msgId: string) {
  const s = new Set(expandedSources.value)
  if (s.has(msgId)) s.delete(msgId)
  else s.add(msgId)
  expandedSources.value = s
}

function highlightEntity(sources: ChatSources) {
  chatStore.currentSources = sources
}

function focusSingleEntity(entityId: string) {
  chatStore.focusEntity(entityId)
}

function openImagePreview(src: string) {
  previewImageSrc.value = src
  showImagePreview.value = true
}

function triggerFileInput() {
  fileInput.value?.click()
}

function processFile(file: File): Promise<PendingImage | null> {
  return new Promise((resolve) => {
    if (!ACCEPTED_TYPES.includes(file.type)) {
      imageError.value = `Unsupported format: ${file.type}. Use JPEG, PNG, GIF, or WebP.`
      resolve(null)
      return
    }
    if (file.size > MAX_IMAGE_SIZE) {
      imageError.value = `Image too large (${(file.size / 1024 / 1024).toFixed(1)} MB). Max 10 MB.`
      resolve(null)
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = reader.result as string
      // Strip data:image/...;base64, prefix for API
      const base64 = dataUrl.replace(/^data:image\/[^;]+;base64,/, '')
      resolve({ dataUrl, base64 })
    }
    reader.onerror = () => resolve(null)
    reader.readAsDataURL(file)
  })
}

async function onFileSelected(event: Event) {
  imageError.value = ''
  const target = event.target as HTMLInputElement
  if (!target.files?.length) return
  for (const file of Array.from(target.files)) {
    if (pendingImages.value.length >= MAX_IMAGES) {
      imageError.value = `Maximum ${MAX_IMAGES} images allowed.`
      break
    }
    const img = await processFile(file)
    if (img) pendingImages.value.push(img)
  }
  // Reset input so same file can be re-selected
  target.value = ''
}

async function onPaste(event: ClipboardEvent) {
  const items = event.clipboardData?.items
  if (!items) return
  for (const item of Array.from(items)) {
    if (item.type.startsWith('image/')) {
      event.preventDefault()
      imageError.value = ''
      if (pendingImages.value.length >= MAX_IMAGES) {
        imageError.value = `Maximum ${MAX_IMAGES} images allowed.`
        break
      }
      const file = item.getAsFile()
      if (!file) continue
      const img = await processFile(file)
      if (img) pendingImages.value.push(img)
    }
  }
}

function removeImage(idx: number) {
  pendingImages.value.splice(idx, 1)
  imageError.value = ''
}

function send() {
  if ((!input.value.trim() && !pendingImages.value.length) || chatStore.isStreaming) return
  // Raw base64 for the API, full data URLs (with correct MIME type) for display
  const apiImages = pendingImages.value.length
    ? pendingImages.value.map(img => img.base64)
    : undefined
  const displayImages = pendingImages.value.length
    ? pendingImages.value.map(img => img.dataUrl)
    : undefined
  const text = input.value.trim() || (apiImages ? 'What is in this image?' : '')
  chatStore.sendMessage(text, apiImages, displayImages)
  input.value = ''
  pendingImages.value = []
  imageError.value = ''
}

// Auto-scroll on new messages
watch(
  () => chatStore.messages[chatStore.messages.length - 1]?.content,
  async () => {
    await nextTick()
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  },
)
</script>

<style scoped>
.chat-panel {
  background: linear-gradient(180deg, #14110a 0%, #0e0b06 100%) !important;
  border-left: 1px solid rgba(200, 169, 110, 0.18) !important;
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: rgba(238, 224, 196, 0.94);
}
.chat-panel-mobile {
  width: 100% !important;
  max-width: 100% !important;
  min-width: 100% !important;
}
.chat-panel-mobile:not(.v-navigation-drawer--active) {
  transform: translateX(100%) !important;
  visibility: hidden !important;
}
@media (min-width: 600px) and (max-width: 960px) {
  .chat-panel:not(.chat-panel-mobile) { width: 380px !important; }
}
.chat-header {
  border-bottom: 1px solid rgba(200, 169, 110, 0.18);
  background: rgba(200, 169, 110, 0.03);
  min-height: 52px;
  font-family: Georgia, serif;
}
.chat-header :deep(.text-subtitle-2) {
  font-family: Georgia, serif !important;
  font-style: italic;
  letter-spacing: 0.08em;
  color: #c8a96e !important;
  font-size: 13px !important;
}
.messages-area {
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  font-family: Georgia, serif;
  font-style: italic;
  color: rgba(238, 224, 196, 0.55);
  opacity: 1;
}
.empty-state :deep(.v-icon) {
  color: #c8a96e !important;
  opacity: 0.4;
}
.message-bubble {
  max-width: 92%;
  padding: 12px 16px;
  font-family: Georgia, serif;
  font-size: 14px;
  line-height: 1.65;
  word-break: break-word;
  border: 1px solid transparent;
}
.message-bubble.user {
  align-self: flex-end;
  background: rgba(200, 169, 110, 0.08);
  border-color: rgba(200, 169, 110, 0.25);
  border-left: 2px solid #c8a96e;
  color: rgba(238, 224, 196, 0.96);
}
.message-bubble.assistant {
  align-self: flex-start;
  background: rgba(200, 169, 110, 0.03);
  border-color: rgba(200, 169, 110, 0.15);
  color: rgba(238, 224, 196, 0.92);
}
.message-content {
  white-space: pre-wrap;
  font-family: Georgia, serif;
}
.speak-btn {
  opacity: 0.3;
  vertical-align: middle;
}
.speak-btn:hover {
  opacity: 0.8;
}
.thinking-state {
  display: flex;
  align-items: center;
  padding: 2px 0;
}
.typing-dot::after {
  content: '...';
  animation: blink 1s steps(3) infinite;
}
@keyframes blink {
  0%, 100% { opacity: 0.2; }
  50% { opacity: 1; }
}
.sources-section {
  border-top: 1px dotted rgba(200, 169, 110, 0.25);
  padding-top: 8px;
  margin-top: 6px;
}
.sources-toggle {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.05em;
  color: rgba(238, 224, 196, 0.55);
  cursor: pointer;
  user-select: none;
}
.sources-toggle:hover { color: #c8a96e; }
.sources-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 6px;
}
.source-entity {
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12px;
  color: rgba(238, 224, 196, 0.7);
  cursor: pointer;
  padding: 3px 6px;
}
.source-entity:hover {
  background: rgba(200, 169, 110, 0.08);
  color: rgba(238, 224, 196, 0.95);
}
.entity-type-dot {
  width: 6px;
  height: 6px;
  flex-shrink: 0;
}
.source-memory {
  font-family: Georgia, serif;
  font-size: 11px;
  color: rgba(238, 224, 196, 0.55);
  padding: 3px 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  border-left: 1px dotted rgba(200, 169, 110, 0.18);
  padding-left: 8px;
  font-style: italic;
}
.chat-input {
  border-top: 1px solid rgba(200, 169, 110, 0.18);
  background: rgba(200, 169, 110, 0.03);
}
.mic-recording {
  animation: pulse-recording 1.2s ease-in-out infinite;
}
@keyframes pulse-recording {
  0%, 100% { box-shadow: 0 0 0 0 rgba(244, 67, 54, 0.4); }
  50% { box-shadow: 0 0 0 8px rgba(244, 67, 54, 0); }
}
/* Image preview in input area */
.image-preview-row {
  flex-wrap: wrap;
}
.image-preview-item {
  position: relative;
  display: inline-block;
}
.preview-thumb {
  width: 56px;
  height: 56px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.12);
}
.preview-remove {
  position: absolute;
  top: -6px;
  right: -6px;
  background: rgba(18, 18, 26, 0.85) !important;
}
/* Image thumbnails inside message bubbles */
.message-images {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.message-image-thumb {
  width: 120px;
  max-height: 120px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  cursor: pointer;
  transition: opacity 0.15s;
}
.message-image-thumb:hover {
  opacity: 0.8;
}
</style>
