<template>
  <div class="thread-page">
    <div class="thread-shell">
      <header class="thread-header">
        <button class="back-btn" @click="$router.back()">← back</button>
        <div class="header-rule" />
        <div class="header-meta">
          <span v-if="conv?.source_type" class="source-tag">{{ conv.source_type }}</span>
        </div>
      </header>

      <div v-if="loading" class="thread-loading">
        <span class="dotty">·  ·  ·</span>
        <p class="loading-text">opening the thread</p>
      </div>
      <div v-else-if="!conv" class="thread-empty">
        <span class="ornament">❦</span>
        <p>— this thread is not in the codex —</p>
      </div>
      <article v-else class="thread-content">
        <div class="masthead-rule" />
        <div class="title-block">
          <p class="thread-eyebrow">A conversation, kept</p>
          <h1 class="thread-title">{{ conv.title || '(untitled)' }}</h1>
          <p class="thread-imported">
            <em>{{ messages.length }}</em> messages · imported {{ formatDate(conv.imported_at) }}
          </p>
        </div>
        <div class="masthead-rule" />

        <div class="messages">
          <article
            v-for="(m, idx) in messages"
            :key="idx"
            class="msg"
            :class="`role-${messageRole(m) || 'unknown'}`"
          >
            <div class="msg-margin">
              <span class="msg-numeral">{{ idx + 1 }}</span>
              <span class="msg-role">{{ messageRole(m) || 'message' }}</span>
            </div>
            <p class="msg-content">{{ messageText(m) }}</p>
          </article>
        </div>
      </article>

    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const conv = ref<any>(null)
const loading = ref(true)

const messages = computed(() => {
  if (!conv.value?.messages) return []
  if (Array.isArray(conv.value.messages)) return conv.value.messages
  return []
})

function messageRole(m: any): string {
  if (typeof m !== 'object' || m === null) return ''
  return m.role || m.author?.role || ''
}

function messageText(m: any): string {
  if (typeof m !== 'object' || m === null) return String(m || '')
  if (typeof m.content === 'string') return m.content
  if (Array.isArray(m.content)) {
    return m.content.map((p: any) => typeof p === 'string' ? p : (p?.text || '')).join('\n')
  }
  if (m.parts && Array.isArray(m.parts)) {
    return m.parts.map((p: any) => typeof p === 'string' ? p : (p?.text || '')).join('\n')
  }
  return JSON.stringify(m).slice(0, 1000)
}

function formatDate(s?: string) {
  if (!s) return ''
  return new Date(s).toLocaleDateString('en-GB', {
    year: 'numeric', month: 'long', day: 'numeric',
  })
}

async function load() {
  loading.value = true
  try {
    const r = await fetch(`/api/conversations/${route.params.id}`)
    if (r.ok) conv.value = await r.json()
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.thread-page {
  --cp-gold: #c8a96e;
  --cp-gold-soft: rgba(200, 169, 110, 0.45);
  --cp-gold-faint: rgba(200, 169, 110, 0.18);
  --cp-ink: rgba(238, 224, 196, 0.94);
  --cp-ink-mute: rgba(238, 224, 196, 0.55);
  font-family: Georgia, 'Palatino Linotype', Palatino, serif;
  color: var(--cp-ink);
  padding: 24px 24px 80px;
  min-height: 100vh;
}

.thread-shell { max-width: 720px; margin: 0 auto; }

/* Header */
.thread-header {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 16px;
}
.back-btn {
  background: transparent;
  border: none;
  color: var(--cp-gold);
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 13px;
  letter-spacing: 0.05em;
  cursor: pointer;
  padding: 4px 0;
  transition: color 150ms;
}
.back-btn:hover { color: rgba(238, 224, 196, 0.95); }
.header-rule {
  flex-grow: 1;
  height: 1px;
  background: linear-gradient(90deg, var(--cp-gold-soft), transparent);
}
.source-tag {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--cp-gold);
}

/* Title block */
.masthead-rule {
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--cp-gold-soft) 30%, var(--cp-gold) 50%, var(--cp-gold-soft) 70%, transparent);
}
.title-block {
  text-align: center;
  padding: 24px 0 16px;
}
.thread-eyebrow {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 11px;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--cp-gold);
  margin: 0 0 8px;
}
.thread-title {
  font-family: Georgia, serif;
  font-size: clamp(24px, 3.5vw, 36px);
  font-weight: 400;
  letter-spacing: 0.02em;
  color: var(--cp-ink);
  margin: 0 0 8px;
  line-height: 1.3;
}
.thread-imported {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 12px;
  color: var(--cp-ink-mute);
  margin: 0;
}
.thread-imported em {
  color: var(--cp-ink);
  font-style: normal;
  font-variant-numeric: tabular-nums;
}

/* Messages */
.messages {
  margin-top: 32px;
  display: flex;
  flex-direction: column;
}
.msg {
  display: grid;
  grid-template-columns: 80px 1fr;
  gap: 16px;
  padding: 18px 0;
  border-bottom: 1px dotted var(--cp-gold-faint);
}
.msg-margin {
  text-align: right;
  border-right: 1px solid var(--cp-gold-faint);
  padding-right: 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-end;
}
.msg-numeral {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 18px;
  color: var(--cp-gold-soft);
  font-variant-numeric: tabular-nums;
  line-height: 1;
}
.msg-role {
  font-family: Georgia, serif;
  font-style: italic;
  font-size: 10px;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--cp-ink-mute);
}
.role-user .msg-numeral { color: var(--cp-gold); }
.role-user .msg-role { color: var(--cp-gold); }

.msg-content {
  font-family: Georgia, serif;
  font-size: 14px;
  line-height: 1.75;
  color: rgba(238, 224, 196, 0.96);
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
.role-assistant .msg-content {
  color: rgba(238, 224, 196, 0.88);
}

/* States */
.thread-loading, .thread-empty {
  text-align: center;
  padding: 80px 0;
  color: var(--cp-ink-mute);
  font-style: italic;
  font-family: Georgia, serif;
}
.ornament {
  display: block;
  font-size: 24px;
  color: var(--cp-gold-soft);
  margin-bottom: 12px;
}
.dotty { letter-spacing: 0.5em; color: var(--cp-gold-soft); }
.loading-text { font-size: 13px; margin: 8px 0 0; letter-spacing: 0.05em; }

@media (max-width: 720px) {
  .thread-page { padding: 16px 14px 64px; }
  .thread-title { font-size: 22px; }
  .msg {
    grid-template-columns: 48px 1fr;
    gap: 10px;
    padding: 14px 0;
  }
  .msg-margin { padding-right: 8px; }
  .msg-numeral { font-size: 14px; }
  .msg-content { font-size: 13px; }
}
</style>
