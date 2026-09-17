<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { LoaderCircle, MessageSquare, Plus, Trash2, X } from 'lucide-vue-next'
import AppDialog from './AppDialog.vue'
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

const { t } = useI18n()
const creating = ref(false)
const name = ref('')
const removing = ref<SessionInfo | null>(null)

function submit() {
  if (props.busy) return
  emit('create', name.value.trim())
}

function cancelCreate() {
  creating.value = false
  name.value = ''
}

function confirmRemove(session: SessionInfo) {
  if (!props.busy) removing.value = session
}

function remove() {
  if (!removing.value || props.busy) return
  emit('remove', removing.value)
  removing.value = null
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
        <strong>{{ t('sessions.title') }}</strong>
      </div>
      <div class="session-header-actions">
        <button v-if="canManage" class="icon-button" :title="t('sessions.newSession')" :disabled="busy" @click="creating = true"><Plus :size="17" /></button>
        <button class="icon-button mobile-close" :title="t('sessions.closeSessions')" @click="emit('close')"><X :size="18" /></button>
      </div>
    </header>

    <form v-if="creating" class="session-create" @submit.prevent="submit">
      <input v-model="name" autofocus maxlength="80" :placeholder="t('sessions.sessionName')" :aria-label="t('sessions.sessionName')">
      <button class="icon-button" type="submit" :title="t('sessions.createSession')" :disabled="busy"><LoaderCircle v-if="busy" :size="15" class="spinning" /><Plus v-else :size="16" /></button>
      <button class="icon-button" type="button" :title="t('common.cancel')" :disabled="busy" @click="cancelCreate"><X :size="16" /></button>
    </form>

    <div v-if="error" class="session-error">{{ error }}</div>
    <nav class="session-list" :aria-label="t('sessions.title')">
      <div
        v-for="session in sessions"
        :key="session.path"
        class="session-row"
        :class="{ active: session.path === currentPath }"
      >
        <button class="session-select" type="button" :aria-current="session.path === currentPath ? 'page' : undefined" @click="emit('select', session.path)">
          <MessageSquare :size="14" />
          <span class="session-copy">
            <strong>{{ session.name }}</strong>
            <small><i class="session-status" :class="session.status" />{{ t(`sessions.${session.status}`) }}</small>
          </span>
        </button>
        <button
          v-if="canManage"
          class="icon-button danger session-remove"
          type="button"
          :title="sessions.length === 1 ? t('sessions.lastCannotRemove') : t('sessions.removeSession')"
          :disabled="busy || sessions.length === 1"
          @click="confirmRemove(session)"
        ><Trash2 :size="14" /></button>
      </div>
    </nav>

    <AppDialog :open="removing !== null" :title="t('sessions.removeSession')" :confirm-label="t('common.remove')" danger :busy="busy" @close="removing = null" @confirm="remove">
      <i18n-t keypath="sessions.removeConfirm" scope="global" tag="p">
        <template #name><strong>{{ removing?.name }}</strong></template>
      </i18n-t>
    </AppDialog>
  </aside>
</template>
