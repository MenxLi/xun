<script setup lang="ts">
import { ref, watch } from 'vue'
import { LoaderCircle, MessageSquare, Plus, Trash2, X } from 'lucide-vue-next'
import type { SessionInfo } from '../types'

const props = defineProps<{
  sessions: SessionInfo[]
  currentPath: string
  canManage: boolean
  busy: boolean
  error: string
}>()
const emit = defineEmits<{
  close: []
  create: [name: string]
  remove: [session: SessionInfo]
  select: [path: string]
}>()

const creating = ref(false)
const name = ref('')

function submit() {
  if (props.busy) return
  emit('create', name.value.trim())
}

function cancelCreate() {
  creating.value = false
  name.value = ''
}

function confirmRemove(session: SessionInfo) {
  if (props.busy || !window.confirm(`Remove ${session.name}?`)) return
  emit('remove', session)
}

watch(() => props.busy, (busy, wasBusy) => {
  if (wasBusy && !busy && !props.error) cancelCreate()
})
</script>

<template>
  <aside class="session-sidebar">
    <header class="session-header">
      <div>
        <span class="eyebrow">Xun</span>
        <strong>Sessions</strong>
      </div>
      <div class="session-header-actions">
        <button v-if="canManage" class="icon-button" title="New session" :disabled="busy" @click="creating = true"><Plus :size="17" /></button>
        <button class="icon-button mobile-close" title="Close sessions" @click="emit('close')"><X :size="18" /></button>
      </div>
    </header>

    <form v-if="creating" class="session-create" @submit.prevent="submit">
      <input v-model="name" autofocus maxlength="80" placeholder="Session name" aria-label="Session name">
      <button class="icon-button" type="submit" title="Create session" :disabled="busy"><LoaderCircle v-if="busy" :size="15" class="spinning" /><Plus v-else :size="16" /></button>
      <button class="icon-button" type="button" title="Cancel" :disabled="busy" @click="cancelCreate"><X :size="16" /></button>
    </form>

    <div v-if="error" class="session-error">{{ error }}</div>
    <nav class="session-list" aria-label="Sessions">
      <span v-if="!sessions.length" class="session-empty">No sessions</span>
      <div
        v-for="session in sessions"
        :key="session.path"
        class="session-row"
        :class="{ active: session.path === currentPath }"
      >
        <button class="session-select" type="button" @click="emit('select', session.path)">
          <MessageSquare :size="15" />
          <span class="session-copy">
            <strong>{{ session.name }}</strong>
            <small><i class="session-status" :class="session.status" />{{ session.status === 'waiting' ? 'Waiting for input' : session.status }}</small>
          </span>
        </button>
        <button
          v-if="canManage"
          class="icon-button danger session-remove"
          type="button"
          :title="sessions.length === 1 ? 'The last session cannot be removed' : 'Remove session'"
          :disabled="busy || sessions.length === 1"
          @click="confirmRemove(session)"
        ><Trash2 :size="14" /></button>
      </div>
    </nav>
  </aside>
</template>
