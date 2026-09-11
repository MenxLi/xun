<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Bot, Check, Monitor, Moon, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, Settings, Sun, Wifi, WifiOff } from 'lucide-vue-next'
import { api, appUrl, basePath, configureSessionsApi, formatTokens } from './api'
import InputComposer from './components/InputComposer.vue'
import EventStream from './components/EventStream.vue'
import FileBrowser from './components/FileBrowser.vue'
import PromptCard from './components/PromptCard.vue'
import SessionSidebar from './components/SessionSidebar.vue'
import StickyScroll from './components/StickyScroll.vue'
import type { AgentInfo, ClientMessage, CommandInfo, DisplayEvent, ImageDescriptor, PendingPrompt, ServerMessage, SessionInfo } from './types'
import { useSettingsStore, type Theme } from './stores/settings'
import { useInputHistoryStore } from './stores/inputHistory'

const settings = useSettingsStore()
const inputHistory = useInputHistoryStore()

const events = ref<DisplayEvent[]>([])
const agents = ref<AgentInfo[]>([])
const selectedAgentId = ref('')
const selectedOnly = ref(false)
const commands = ref<CommandInfo[]>([])
const input = ref('')
const connected = ref(false)
const exposeFiles = ref(false)
const settingsOpen = ref(false)
const sessions = ref<SessionInfo[]>([])
const canManageSessions = ref(false)
const sessionBusy = ref(false)
const sessionError = ref('')
const narrowLayout = ref(window.innerWidth < 900)
const pendingPrompts = ref<PendingPrompt[]>([])
const supportsVision = ref(false)
const images = ref<Array<{ file: File; url: string }>>([])
const sending = ref(false)
const runningAgents = ref(new Set<string>())
const cancellingAgents = ref(new Set<string>())
const sendError = ref('')
const promptErrors = ref(new Map<string, string>())
const resolvingPrompts = ref(new Set<string>())
const stream = ref<InstanceType<typeof StickyScroll> | null>(null)
let socket: WebSocket | null = null
let reconnectTimer: number | undefined
let sessionRefreshTimer: number | undefined
let agentDataRequest = 0
let syncing = false
let queuedMessages: ServerMessage[] = []

const currentSessionPath = computed(() => basePath || '/')
const showSessions = computed(() => settings.sessionsOpen)
const showFiles = computed(() => exposeFiles.value && settings.filesOpen && (!narrowLayout.value || !showSessions.value))
const selectedAgent = computed(() => agents.value.find(agent => agent.identifier === selectedAgentId.value))
const selectedAgentRunning = computed(() => runningAgents.value.has(selectedAgentId.value))
const selectedAgentCancelling = computed(() => cancellingAgents.value.has(selectedAgentId.value))
const visibleEvents = computed(() => selectedOnly.value && selectedAgentId.value
  ? events.value.filter(event => event.agent.identifier === selectedAgentId.value)
  : events.value,
)
const visiblePrompts = computed(() => selectedOnly.value && selectedAgentId.value
  ? pendingPrompts.value.filter(prompt => prompt.agent_id === selectedAgentId.value)
  : pendingPrompts.value,
)
const selectedAgentTokens = computed(() => {
  for (let i = events.value.length - 1; i >= 0; i--) {
    const event = events.value[i]
    if (event.name !== 'ModelMessageEvent' || event.agent.identifier !== selectedAgentId.value) continue
    return event.payload.total_tokens
  }
  return null
})

watch(() => settings.theme, theme => {
  if (theme === 'system') delete document.documentElement.dataset.theme
  else document.documentElement.dataset.theme = theme
}, { immediate: true })
watch([selectedAgentId, selectedOnly], () => stream.value?.anchor())
watch(selectedAgentId, async agentId => {
  const requestId = ++agentDataRequest
  commands.value = []
  supportsVision.value = false
  clearImages()
  if (!agentId) return
  try {
    const [commandData, capabilityData] = await Promise.all([api.commands(agentId), api.capabilities(agentId)])
    if (requestId !== agentDataRequest) return
    commands.value = commandData
    supportsVision.value = capabilityData.capabilities.includes('vision')
  } catch {
    if (requestId === agentDataRequest) commands.value = []
  }
})

async function loadInitialData() {
  const [config, eventData, agentData, runningData, promptData] = await Promise.all([
    api.config(), api.events(), api.agents(), api.running(), api.prompts(),
  ])
  exposeFiles.value = config.expose_files
  configureSessionsApi(config.sessions_api)
  events.value = eventData
  agents.value = agentData
  runningAgents.value = new Set(runningData)
  pendingPrompts.value = promptData
  ensureAgentSelection()
  await loadSessions()
}

async function loadSessions() {
  try {
    const listing = await api.sessions()
    sessions.value = listing.sessions
    canManageSessions.value = listing.can_manage
    sessionError.value = ''
  } catch (error) {
    sessionError.value = error instanceof Error ? error.message : 'Could not load sessions'
  }
}

async function createSession(name: string) {
  sessionBusy.value = true
  sessionError.value = ''
  try {
    const session = await api.createSession(name)
    window.location.assign(`${session.path.replace(/\/$/, '')}/`)
  } catch (error) {
    sessionError.value = error instanceof Error ? error.message : 'Could not create session'
    sessionBusy.value = false
  }
}

async function removeSession(session: SessionInfo) {
  sessionBusy.value = true
  sessionError.value = ''
  try {
    await api.removeSession(session.path)
    const remaining = sessions.value.filter(item => item.path !== session.path)
    sessions.value = remaining
    if (session.path === currentSessionPath.value) {
      const next = remaining[0]?.path || '/'
      window.location.assign(`${next.replace(/\/$/, '')}/`)
      return
    }
  } catch (error) {
    sessionError.value = error instanceof Error ? error.message : 'Could not remove session'
  } finally {
    sessionBusy.value = false
  }
}

function toggleSessions() {
  if (narrowLayout.value && !showSessions.value) {
    settings.sessionsOpen = true
    settings.filesOpen = false
  } else {
    settings.sessionsOpen = !settings.sessionsOpen
  }
}

function toggleFiles() {
  if (narrowLayout.value && !showFiles.value) {
    settings.filesOpen = true
    settings.sessionsOpen = false
  } else {
    settings.filesOpen = !settings.filesOpen
  }
}

function updateLayout() {
  narrowLayout.value = window.innerWidth < 900
}

function ensureAgentSelection() {
  if (!agents.value.some(agent => agent.identifier === selectedAgentId.value)) {
    selectedAgentId.value = agents.value[0]?.identifier || ''
  }
}

function applyAgentEvent(event: DisplayEvent) {
  const agent = event.agent
  if (event.name === 'AgentBindEvent') {
    const index = agents.value.findIndex(item => item.identifier === agent.identifier)
    if (index === -1) agents.value.push(agent)
    else agents.value[index] = agent
  } else if (event.name === 'AgentUnbindEvent') {
    agents.value = agents.value.filter(item => item.identifier !== agent.identifier)
  }
  ensureAgentSelection()
}

function connect() {
  const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
  socket = new WebSocket(`${protocol}://${location.host}${appUrl('/ws')}`)
  socket.addEventListener('open', async () => {
    connected.value = true
    syncing = true
    await loadInitialData().catch(() => undefined)
    syncing = false
    queuedMessages.forEach(handleServerMessage)
    queuedMessages = []
  })
  socket.addEventListener('message', message => {
    const payload = JSON.parse(message.data) as ServerMessage
    if (syncing) queuedMessages.push(payload)
    else handleServerMessage(payload)
  })
  socket.addEventListener('close', () => {
    connected.value = false
    reconnectTimer = window.setTimeout(connect, 2500)
  })
}

function handleServerMessage(payload: ServerMessage) {
  if (isPendingPrompt(payload)) {
    pendingPrompts.value = [...pendingPrompts.value.filter(prompt => prompt.id !== payload.data.id), payload.data]
  } else if (isPromptResolved(payload)) {
    pendingPrompts.value = pendingPrompts.value.filter(prompt => prompt.id !== payload.prompt_id)
  } else if (isExecutionState(payload)) {
    const running = new Set(runningAgents.value)
    const cancelling = new Set(cancellingAgents.value)
    if (payload.running) {
      running.add(payload.agent_id)
      cancelling.delete(payload.agent_id)
    } else {
      running.delete(payload.agent_id)
      cancelling.delete(payload.agent_id)
    }
    runningAgents.value = running
    cancellingAgents.value = cancelling
  } else {
    applyAgentEvent(payload)
    events.value.push(payload)
  }
}

function isPendingPrompt(payload: ServerMessage): payload is Extract<ServerMessage, { type: 'pending_prompt' }> {
  return 'type' in payload && payload.type === 'pending_prompt'
}

function isPromptResolved(payload: ServerMessage): payload is Extract<ServerMessage, { type: 'prompt_resolved' }> {
  return 'type' in payload && payload.type === 'prompt_resolved'
}

function isExecutionState(payload: ServerMessage): payload is Extract<ServerMessage, { type: 'execution_state' }> {
  return 'type' in payload && payload.type === 'execution_state'
}

function send(payload: ClientMessage) {
  if (socket?.readyState !== WebSocket.OPEN) return false
  socket.send(JSON.stringify(payload))
  return true
}

function readImage(file: File): Promise<ImageDescriptor> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.addEventListener('load', () => resolve({ kind: 'base64', value: String(reader.result) }))
    reader.addEventListener('error', () => reject(reader.error || new Error(`Could not read ${file.name}`)))
    reader.readAsDataURL(file)
  })
}

async function submit() {
  const value = input.value.trim()
  if ((!value && !images.value.length) || !connected.value || !selectedAgentId.value || sending.value || selectedAgentRunning.value) return
  sendError.value = ''
  if (value.startsWith('/')) {
    const [name, ...argumentsParts] = value.slice(1).split(/\s+/)
    if (send({ type: 'command', agent_id: selectedAgentId.value, name, arguments: argumentsParts.join(' ') || null })) {
      inputHistory.add(value)
      stream.value?.anchor()
    }
  } else {
    sending.value = true
    try {
      const messageImages = await Promise.all(images.value.map(image => readImage(image.file)))
      if (!send({ type: 'message', agent_id: selectedAgentId.value, content: value, images: messageImages })) return
      inputHistory.add(value)
      clearImages()
      stream.value?.anchor()
    } catch (error) {
      sendError.value = error instanceof Error ? error.message : 'Could not upload images'
      return
    } finally {
      sending.value = false
    }
  }
  input.value = ''
}

function cancelExecution() {
  const agentId = selectedAgentId.value
  if (!agentId || selectedAgentCancelling.value || !send({ type: 'cancel', agent_id: agentId })) return
  cancellingAgents.value = new Set(cancellingAgents.value).add(agentId)
}

function removeImage(index: number) {
  const [image] = images.value.splice(index, 1)
  if (image) URL.revokeObjectURL(image.url)
}

function clearImages() {
  images.value.forEach(image => URL.revokeObjectURL(image.url))
  images.value = []
}

function attachImages(files: File[]) {
  images.value.push(...files.map(file => ({ file, url: URL.createObjectURL(file) })))
}

async function answerPrompt(promptId: string, value: string) {
  resolvingPrompts.value = new Set(resolvingPrompts.value).add(promptId)
  promptErrors.value.delete(promptId)
  try {
    await api.resolvePrompt(promptId, value)
    pendingPrompts.value = pendingPrompts.value.filter(prompt => prompt.id !== promptId)
  } catch (error) {
    promptErrors.value.set(promptId, error instanceof Error ? error.message : 'Could not submit response')
    promptErrors.value = new Map(promptErrors.value)
  } finally {
    const resolving = new Set(resolvingPrompts.value)
    resolving.delete(promptId)
    resolvingPrompts.value = resolving
  }
}

const themeOptions: Array<{ value: Theme; label: string; icon: typeof Monitor }> = [
  { value: 'system', label: 'System', icon: Monitor },
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
]

onMounted(() => {
  connect()
  sessionRefreshTimer = window.setInterval(loadSessions, 2000)
  window.addEventListener('resize', updateLayout)
})
onBeforeUnmount(() => {
  window.clearTimeout(reconnectTimer)
  window.clearInterval(sessionRefreshTimer)
  window.removeEventListener('resize', updateLayout)
  socket?.close()
  clearImages()
})
</script>

<template>
  <div class="app-shell">
    <div v-if="showSessions" class="mobile-scrim session-scrim" @click="settings.sessionsOpen = false" />
    <SessionSidebar
      v-if="showSessions"
      :sessions="sessions"
      :current-path="currentSessionPath"
      :can-manage="canManageSessions"
      :busy="sessionBusy"
      :error="sessionError"
      @close="settings.sessionsOpen = false"
      @create="createSession"
      @remove="removeSession"
    />

    <main class="chat-shell">
      <header class="topbar">
        <div class="brand">
          <button class="icon-button" :title="showSessions ? 'Hide sessions' : 'Show sessions'" @click="toggleSessions">
            <PanelLeftClose v-if="showSessions" :size="18" />
            <PanelLeftOpen v-else :size="18" />
          </button>
        </div>
        <div class="agent-controls">
          <Bot :size="15" />
          <select v-model="selectedAgentId" aria-label="Active agent" :disabled="!agents.length">
            <option v-if="!agents.length" value="">No agents</option>
            <option v-for="agent in agents" :key="agent.identifier" :value="agent.identifier">{{ agent.name }}</option>
          </select>
          <label class="stream-filter" title="Show events from the active agent only">
            <input v-model="selectedOnly" type="checkbox">
            <span>Selected only</span>
          </label>
          <span v-if="selectedAgentTokens != null" class="token-badge" title="Total tokens used by the active agent's conversation">{{ formatTokens(selectedAgentTokens) }} tokens</span>
        </div>
        <div class="topbar-actions">
          <span class="connection" :class="{ connected }"><Wifi v-if="connected" :size="14" /><WifiOff v-else :size="14" />{{ connected ? 'Connected' : 'Reconnecting' }}</span>
          <div class="settings-wrap">
            <button class="icon-button" title="Display settings" aria-label="Display settings" :aria-expanded="settingsOpen" @click="settingsOpen = !settingsOpen"><Settings :size="17" /></button>
            <div v-if="settingsOpen" class="settings-menu">
              <span class="settings-label">Theme</span>
              <div class="theme-options">
                <button v-for="option in themeOptions" :key="option.value" :class="{ selected: settings.theme === option.value }" @click="settings.theme = option.value">
                  <component :is="option.icon" :size="14" />{{ option.label }}<Check v-if="settings.theme === option.value" class="theme-check" :size="13" />
                </button>
              </div>
              <label class="setting-toggle"><span>Render Markdown</span><input v-model="settings.markdown" type="checkbox"></label>
            </div>
          </div>
          <button v-if="exposeFiles" class="icon-button" :title="showFiles ? 'Hide workspace' : 'Show workspace'" @click="toggleFiles">
            <PanelRightClose v-if="showFiles" :size="18" />
            <PanelRightOpen v-else :size="18" />
          </button>
        </div>
      </header>

      <StickyScroll ref="stream" :size="visibleEvents.length + visiblePrompts.length">
        <div v-if="!visibleEvents.length && !visiblePrompts.length" class="empty-chat"><strong>{{ agents.length ? 'No activity here yet' : 'Waiting for an agent' }}</strong><span>{{ agents.length ? 'Send a message or show all agent activity.' : 'Bound agents will appear automatically.' }}</span></div>
        <EventStream v-if="visibleEvents.length" :events="visibleEvents" :markdown="settings.markdown" />
        <div v-if="visiblePrompts.length" class="prompt-stream">
          <PromptCard
            v-for="prompt in visiblePrompts"
            :key="prompt.id"
            :prompt="prompt"
            :agent-name="agents.find(agent => agent.identifier === prompt.agent_id)?.name"
            :submitting="resolvingPrompts.has(prompt.id)"
            :error="promptErrors.get(prompt.id)"
            @submit="value => answerPrompt(prompt.id, value)"
          />
        </div>
      </StickyScroll>

      <footer class="composer-area">
        <InputComposer
          v-model="input"
          :commands="commands"
          :placeholder="selectedAgent ? `Message ${selectedAgent.name}` : 'Select an agent to start'"
          :disabled="!connected || !selectedAgent"
          :supports-vision="supportsVision"
          :images="images"
          :running="selectedAgentRunning"
          :cancelling="selectedAgentCancelling"
          :sending="sending"
          :error="sendError"
          @send="submit"
          @stop="cancelExecution"
          @attach="attachImages"
          @remove-image="removeImage"
        />
      </footer>

    </main>

    <div v-if="showFiles" class="mobile-scrim workspace-scrim" @click="settings.filesOpen = false" />
    <FileBrowser v-if="showFiles" :agents="agents" :agent-id="selectedAgentId" @close="settings.filesOpen = false" />

  </div>
</template>
