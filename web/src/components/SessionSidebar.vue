<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { LoaderCircle, MessageSquare, MoreHorizontal, Pencil, Plus, Trash2, X } from 'lucide-vue-next'
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
  rename: [session: SessionInfo, name: string]
  remove: [session: SessionInfo]
  select: [path: string]
}>()

const { t } = useI18n()
const creating = ref(false)
const name = ref('')
const renaming = ref<SessionInfo | null>(null)
const renameName = ref('')
const removing = ref<SessionInfo | null>(null)
const activeSession = ref<SessionInfo | null>(null)
const menuPosition = ref({ top: '0px', left: '0px' })

function submit() {
  if (props.busy) return
  emit('create', name.value.trim())
}

function cancelCreate() {
  creating.value = false
  name.value = ''
}

function confirmRemove(session: SessionInfo) {
  closeMenu()
  if (!props.busy && props.sessions.length > 1) removing.value = session
}

function beginRename(session: SessionInfo) {
  closeMenu()
  if (props.busy) return
  renaming.value = session
  renameName.value = session.name
}

function toggleMenu(session: SessionInfo, event: MouseEvent) {
  if (activeSession.value?.path === session.path) {
    closeMenu()
    return
  }
  const rect = (event.currentTarget as HTMLElement).getBoundingClientRect()
  const margin = 6
  const width = 176
  const height = 72
  const top = rect.bottom + 4 + height <= window.innerHeight - margin
    ? rect.bottom + 4
    : rect.top - height - 4
  menuPosition.value = {
    top: `${Math.max(margin, Math.min(top, window.innerHeight - height - margin))}px`,
    left: `${Math.max(margin, Math.min(rect.right - width, window.innerWidth - width - margin))}px`,
  }
  activeSession.value = session
}

function closeMenu() {
  activeSession.value = null
}

function rename() {
  const nextName = renameName.value.trim()
  if (!renaming.value || props.busy || !nextName) return
  if (nextName === renaming.value.name) {
    renaming.value = null
    return
  }
  emit('rename', renaming.value, nextName)
}

function remove() {
  if (!removing.value || props.busy) return
  emit('remove', removing.value)
  removing.value = null
}

watch(() => props.busy, (busy, wasBusy) => {
  if (wasBusy && !busy && !props.error) {
    cancelCreate()
    renaming.value = null
  }
})

onMounted(() => {
  window.addEventListener('resize', closeMenu)
  document.addEventListener('click', closeMenu)
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', closeMenu)
  document.removeEventListener('click', closeMenu)
})
</script>

<template>
  <aside class="session-sidebar" @keydown.esc="closeMenu">
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

    <div v-if="error && !renaming" class="session-error">{{ error }}</div>
    <nav class="session-list" :aria-label="t('sessions.title')" @scroll="closeMenu">
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
        <div v-if="canManage" class="session-actions" :class="{ open: activeSession?.path === session.path }" @click.stop>
          <button class="icon-button" type="button" :title="t('sessions.sessionActions')" :disabled="busy" :aria-expanded="activeSession?.path === session.path" @click="toggleMenu(session, $event)"><MoreHorizontal :size="15" /></button>
        </div>
      </div>
    </nav>

    <Teleport to="body">
      <div v-if="activeSession" class="action-menu" :style="menuPosition" @click.stop>
        <button @click="beginRename(activeSession)"><Pencil :size="14" /><span>{{ t('sessions.renameSession') }}</span></button>
        <button class="danger" :title="sessions.length === 1 ? t('sessions.lastCannotRemove') : undefined" :disabled="busy || sessions.length === 1" @click="confirmRemove(activeSession)"><Trash2 :size="14" /><span>{{ t('sessions.removeSession') }}</span></button>
      </div>
    </Teleport>

    <AppDialog :open="renaming !== null" :title="t('sessions.renameSession')" :confirm-label="t('common.rename')" :busy="busy" :error="error" @close="renaming = null" @confirm="rename">
      <label class="dialog-field">
        <span>{{ t('sessions.sessionName') }}</span>
        <input v-model="renameName" autofocus required maxlength="80">
      </label>
    </AppDialog>

    <AppDialog :open="removing !== null" :title="t('sessions.removeSession')" :confirm-label="t('common.remove')" danger :busy="busy" @close="removing = null" @confirm="remove">
      <i18n-t keypath="sessions.removeConfirm" scope="global" tag="p">
        <template #name><strong>{{ removing?.name }}</strong></template>
      </i18n-t>
    </AppDialog>
  </aside>
</template>
