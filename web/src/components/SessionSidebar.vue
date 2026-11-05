<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { LoaderCircle, LogOut, MoreHorizontal, Pencil, Plus, Trash2, X } from 'lucide-vue-next'
import AppDialog from './AppDialog.vue'
import ResizeHandle from './ResizeHandle.vue'
import { logout } from '../api'
import { useSettingsStore } from '../stores/settings'
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
  create: []
  rename: [session: SessionInfo, name: string]
  remove: [session: SessionInfo]
  select: [path: string]
}>()

const settings = useSettingsStore()
const { t } = useI18n()
const renamingPath = ref<string | null>(null)
const renameName = ref('')
const renameInput = ref<HTMLInputElement>()

// v-for string refs bind as arrays, so the inline-edit input uses a function ref
function setRenameInput(el: unknown, path: string) {
  if (el && path === renamingPath.value) renameInput.value = el as HTMLInputElement
}

const removing = ref<SessionInfo | null>(null)
const logoutOpen = ref(false)
const activeSession = ref<SessionInfo | null>(null)
const menuPosition = ref({ top: '0px', left: '0px' })

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

function beginRename(session: SessionInfo) {
  closeMenu()
  if (!props.canManage || props.busy) return
  renamingPath.value = session.path
  renameName.value = session.name
  void nextTick(() => {
    renameInput.value?.focus()
    renameInput.value?.select()
  })
}

function cancelRename() {
  renamingPath.value = null
  renameName.value = ''
}

function submitRename(session: SessionInfo) {
  if (renamingPath.value !== session.path) return
  const nextName = renameName.value.trim()
  cancelRename()
  if (nextName && nextName !== session.name) {
    emit('rename', session, nextName)
  }
}

function confirmRemove(session: SessionInfo) {
  closeMenu()
  if (!props.busy && props.sessions.length > 1) removing.value = session
}

function remove() {
  if (!removing.value || props.busy) return
  emit('remove', removing.value)
  removing.value = null
}

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

function resizeWidth(delta: number) {
  // keep the chat pane usable
  const max = Math.min(420, Math.max(180, window.innerWidth * 0.45))
  settings.sessionsWidth = clamp(settings.sessionsWidth + delta, 180, max)
}

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
  <aside class="session-sidebar" :style="{ width: `${settings.sessionsWidth}px` }" @keydown.esc="closeMenu">
    <ResizeHandle class="edge-right" orientation="horizontal" @drag="resizeWidth" @reset="settings.sessionsWidth = 216" />
    <header class="session-header">
      <div>
        <span class="eyebrow">Xun</span>
        <strong>{{ t('sessions.title') }}</strong>
      </div>
      <div class="session-header-actions">
        <button v-if="canManage" class="icon-button" :title="t('sessions.newSession')" :disabled="busy" @click="emit('create')">
          <LoaderCircle v-if="busy && !removing" :size="15" class="spinning" />
          <Plus v-else :size="16" />
        </button>
        <button class="icon-button mobile-close" :title="t('sessions.closeSessions')" @click="emit('close')"><X :size="17" /></button>
      </div>
    </header>

    <div v-if="error" class="session-error">{{ error }}</div>
    <nav class="session-list" :aria-label="t('sessions.title')" @scroll="closeMenu">
      <div
        v-for="session in sessions"
        :key="session.path"
        class="session-row"
        :class="{ active: session.path === currentPath }"
      >
        <div v-if="renamingPath === session.path" class="session-select session-inline-edit">
          <i class="session-status" :class="session.status" />
          <input
            :ref="(el) => setRenameInput(el, session.path)"
            v-model="renameName"
            maxlength="80"
            :aria-label="t('sessions.sessionName')"
            @keydown.enter.prevent="submitRename(session)"
            @keydown.esc.prevent.stop="cancelRename"
            @blur="submitRename(session)"
          >
        </div>
        <template v-else>
          <button
            class="session-select"
            type="button"
            :title="`${session.name} · ${t(`sessions.${session.status}`)}`"
            :aria-current="session.path === currentPath ? 'page' : undefined"
            @click="emit('select', session.path)"
            @dblclick="beginRename(session)"
          >
            <i class="session-status" :class="session.status" :aria-label="t(`sessions.${session.status}`)" />
            <span class="session-name">{{ session.name }}</span>
          </button>
          <div v-if="canManage" class="session-actions" :class="{ open: activeSession?.path === session.path }" @click.stop>
            <button class="icon-button" type="button" :title="t('sessions.sessionActions')" :disabled="busy" :aria-expanded="activeSession?.path === session.path" @click="toggleMenu(session, $event)"><MoreHorizontal :size="14" /></button>
          </div>
        </template>
      </div>
    </nav>

    <footer class="session-footer">
      <button class="session-logout" type="button" :title="t('sessions.logout')" @click="logoutOpen = true">
        <LogOut :size="14" /><span>{{ t('sessions.logout') }}</span>
      </button>
    </footer>

    <Teleport to="body">
      <div v-if="activeSession" class="action-menu" :style="menuPosition" @click.stop>
        <button @click="beginRename(activeSession)"><Pencil :size="14" /><span>{{ t('sessions.renameSession') }}</span></button>
        <button class="danger" :title="sessions.length === 1 ? t('sessions.lastCannotRemove') : undefined" :disabled="busy || sessions.length === 1" @click="confirmRemove(activeSession)"><Trash2 :size="14" /><span>{{ t('sessions.removeSession') }}</span></button>
      </div>
    </Teleport>

    <AppDialog :open="removing !== null" :title="t('sessions.removeSession')" :confirm-label="t('common.remove')" danger :busy="busy" @close="removing = null" @confirm="remove">
      <i18n-t keypath="sessions.removeConfirm" scope="global" tag="p">
        <template #name><strong>{{ removing?.name }}</strong></template>
      </i18n-t>
    </AppDialog>

    <AppDialog :open="logoutOpen" :title="t('sessions.logout')" :confirm-label="t('sessions.logout')" danger @close="logoutOpen = false" @confirm="void logout()">
      <p>{{ t('sessions.logoutConfirm') }}</p>
    </AppDialog>
  </aside>
</template>
