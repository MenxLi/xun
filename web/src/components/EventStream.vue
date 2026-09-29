<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowUp, Check, ChevronRight, CircleAlert, Clock3, Copy, Info, Puzzle, Terminal, TriangleAlert, Wrench } from 'lucide-vue-next'
import MarkdownText from './MarkdownText.vue'
import HtmlText from './HtmlText.vue'
import ToolCalls from './ToolCalls.vue'
import ConfirmPill from './ConfirmPill.vue'
import { eventTime, formatTokens, fullEventTime } from '../api'
import { copyText } from '../clipboard'
import type { AgentInfo, ConfirmDisplayEvent, DisplayEvent, ModelMessageDisplayEvent, ToolCallDisplayEvent, ToolItem } from '../types'

const props = defineProps<{ events: DisplayEvent[]; markdown: boolean; runningAgents: Set<string> }>()
const { t } = useI18n()

type TurnStep =
  | { kind: 'reason'; key: string; event: ModelMessageDisplayEvent }
  | { kind: 'tools'; key: string; tools: ToolItem[] }
  | { kind: 'confirm'; key: string; event: ConfirmDisplayEvent }

type AgentActivity = { key: string; agent: AgentInfo; steps: TurnStep[]; tokens: number | null; autoApprovals: number; last: DisplayEvent; lastTool: string | null; working: boolean }
type BatchItem = { kind: 'batch'; key: string; agents: AgentActivity[]; last: DisplayEvent }
type ToolOwner = { activity: AgentActivity; batch: BatchItem; name: string }

type StreamItem =
  | { kind: 'event'; key: string; data: DisplayEvent }
  | BatchItem

const items = computed<StreamItem[]>(() => {
  const output: StreamItem[] = []
  const toolItems = new Map<string, ToolItem>()
  const toolOwners = new Map<string, ToolOwner>()
  const latestActivities = new Map<string, AgentActivity>()
  let batch: BatchItem | null = null
  let rootAgent = ''

  function ensureActivity(data: DisplayEvent, index: number): AgentActivity {
    if (!batch) {
      batch = { kind: 'batch', key: `batch-${index}`, agents: [], last: data }
      output.push(batch)
    }
    batch.last = data
    let activity = batch.agents.find(item => item.agent.identifier === data.agent.identifier)
    if (!activity) {
      activity = { key: `${batch.key}-${data.agent.identifier}`, agent: data.agent, steps: [], tokens: null, autoApprovals: 0, last: data, lastTool: null, working: false }
      batch.agents.push(activity)
    }
    activity.lastTool = null
    latestActivities.set(data.agent.identifier, activity)
    return activity
  }

  props.events.forEach((data, index) => {
    if (data.name === 'UserMessageEvent' || data.name === 'UserCommandEvent') {
      batch = null
      rootAgent = data.agent.identifier
      latestActivities.clear()
      output.push({ kind: 'event', key: `${data.name}-${index}`, data })
    } else if (data.name === 'ToolResultEvent') {
      // Results attach to their call in place; they never appear as their own item.
      const tool = toolItems.get(data.payload.tool_call_id)
      if (!tool) return
      tool.result = data
      const owner = toolOwners.get(data.payload.tool_call_id)
      if (owner) {
        owner.activity.last = data
        owner.activity.lastTool = owner.name
        owner.batch.last = data
      }
    } else if (data.name === 'ModelWorkingEvent') {
      ensureActivity(data, index).last = data
    } else if (data.name === 'ModelMessageEvent' && (data.agent.identifier !== rootAgent || !data.payload.content.trim())) {
      const hasContent = !!data.payload.content.trim()
      if (data.payload.reasoning?.trim()) {
        const current = ensureActivity(data, index)
        current.tokens = hasContent ? null : data.payload.total_tokens
        current.last = data
        current.steps.push({ kind: 'reason', key: `reason-${index}`, event: data })
      }
      if (hasContent) {
        batch = null
        latestActivities.delete(data.agent.identifier)
        output.push({ kind: 'event', key: `${data.name}-${index}`, data })
      }
    } else if (data.name === 'ToolCallEvent') {
      const item: ToolItem = { key: data.payload.tool_call_id || `tool-${index}`, call: data }
      if (data.payload.tool_call_id) toolItems.set(data.payload.tool_call_id, item)
      const current = ensureActivity(data, index)
      current.lastTool = toolSummary(data)
      if (data.payload.tool_call_id) toolOwners.set(data.payload.tool_call_id, { activity: current, batch: batch!, name: current.lastTool })
      const last = current.steps.at(-1)
      if (last?.kind === 'tools') last.tools.push(item)
      else current.steps.push({ kind: 'tools', key: `steps-${item.key}`, tools: [item] })
      current.last = data
    } else if (data.name === 'ConfirmEvent' && data.payload.source === 'auto') {
      const current = ensureActivity(data, index)
      current.autoApprovals += 1
      current.last = data
    } else if (data.name === 'ConfirmEvent') {
      const current = ensureActivity(data, index)
      current.steps.push({ kind: 'confirm', key: `confirm-${index}`, event: data })
      current.last = data
    } else if (data.name === 'AgentBindEvent' || data.name === 'AgentUnbindEvent') {
      return
    } else {
      batch = null
      latestActivities.delete(data.agent.identifier)
      output.push({ kind: 'event', key: `${data.name}-${index}`, data })
    }
  })

  latestActivities.forEach(activity => { activity.working = props.runningAgents.has(activity.agent.identifier) })
  output.forEach(item => {
    if (item.kind === 'batch') {
      item.agents = item.agents.filter(activity => activity.steps.length || activity.autoApprovals || activity.working)
    }
  })
  return output.filter(item => item.kind !== 'batch' || item.agents.length)
})

function toolSummary(event: ToolCallDisplayEvent): string {
  const detail = Object.values(event.payload.args).find(value => typeof value === 'string' && value.trim())
  if (typeof detail !== 'string') return event.payload.tool_name
  return `${event.payload.tool_name} · ${detail.replace(/\s+/g, ' ').trim().slice(0, 120)}`
}

function detailCount(activity: AgentActivity): number {
  return activity.steps.reduce((total, step) => total + (step.kind === 'tools' ? step.tools.length : 1), 0)
}

function batchDetailCount(batch: BatchItem): number {
  return batch.agents.reduce((total, agent) => total + detailCount(agent), 0)
}

const latestBatchKey = computed(() => {
  for (let index = items.value.length - 1; index >= 0; index--) {
    if (items.value[index].kind === 'batch') return items.value[index].key
  }
  return null
})

function batchSummary(batch: BatchItem): string {
  const activity = batch.agents.reduce((latest, item) => item.last.timestamp > latest.last.timestamp ? item : latest)
  const event = activity.last
  let action = ''
  if (event.name === 'ModelWorkingEvent') action = t('stream.generating')
  else if (event.name === 'ModelMessageEvent') action = event.payload.content || event.payload.reasoning || ''
  else if (event.name === 'ToolCallEvent') action = activity.lastTool || event.payload.tool_name
  else if (event.name === 'ToolResultEvent') action = `${activity.lastTool || t('stream.tool')} · ${t('stream.completed')}`
  else if (event.name === 'ConfirmEvent') action = event.payload.prompt
  const detail = action.replace(/\s+/g, ' ').trim().slice(0, 160) || t('stream.generating')
  return `${activity.agent.name} · ${detail}`
}

// Render only a trailing window: mounting thousands of message components at
// once is what made long sessions crawl.
const INITIAL_WINDOW = 80
const EARLIER_BATCH = 120

const rendered = ref(INITIAL_WINDOW)
const totalItems = computed(() => items.value.length)
const startIndex = computed(() => Math.max(0, totalItems.value - rendered.value))
const visibleItems = computed(() => items.value.slice(startIndex.value))
const hasEarlier = computed(() => startIndex.value > 0)

watch(totalItems, (total, previous) => {
  if (previous === undefined) return
  // Keep an explicitly expanded history visible as live placeholders come and go.
  const minimum = Math.min(INITIAL_WINDOW, total)
  rendered.value = Math.min(total, Math.max(minimum, rendered.value + total - previous))
})

const root = ref<HTMLElement>()

async function showEarlier() {
  const container = root.value?.closest<HTMLElement>('.conversation') ?? null
  const previousHeight = container?.scrollHeight ?? 0
  const previousTop = container?.scrollTop ?? 0
  rendered.value = Math.min(totalItems.value, rendered.value + EARLIER_BATCH)
  await nextTick()
  // Compensate scrollTop so prepended items don't shift the view.
  if (container) container.scrollTop = container.scrollHeight - previousHeight + previousTop
}

type NoticeEvent = Extract<DisplayEvent, { name: 'InfoEvent' | 'WarningEvent' | 'ErrorEvent' }>

function isPlainTextEvent(event: DisplayEvent): event is NoticeEvent {
  return event.name === 'InfoEvent' || event.name === 'WarningEvent' || event.name === 'ErrorEvent'
}

function text(event: DisplayEvent): string {
  if (event.name === 'UserMessageEvent') return event.payload.content
  if (event.name === 'ModelMessageEvent') return event.payload.content
  if (event.name === 'HTMLInfoEvent') return event.payload.html
  if (isPlainTextEvent(event)) return event.payload.message
  return JSON.stringify(event.payload, null, 2)
}

function label(event: DisplayEvent): string {
  if (event.name === 'ModelMessageEvent') return event.agent.name
  if (event.name === 'UserCommandEvent') return t('stream.command')
  if (event.name === 'InfoEvent') return t('stream.info')
  if (event.name === 'HTMLInfoEvent') return event.payload.title || t('stream.info')
  if (event.name === 'WarningEvent') return t('stream.warning')
  if (event.name === 'ErrorEvent') return t('stream.error')
  return event.name.replace(/Event$/, '').replace(/([a-z])([A-Z])/g, '$1 $2')
}

function isUser(event: DisplayEvent): boolean {
  return event.name === 'UserMessageEvent' || (event.name === 'InfoEvent' && event.payload.message.startsWith('[user] '))
}

function displayText(event: DisplayEvent): string {
  const value = text(event)
  return event.name === 'InfoEvent' && isUser(event) ? value.slice(7) : value
}

const copiedKey = ref('')
let copyTimer = 0

async function copyMessage(key: string, event: DisplayEvent) {
  await copyText(displayText(event))
  copiedKey.value = key
  window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => { copiedKey.value = '' }, 1600)
}

</script>

<template>
  <div ref="root" class="stream">
    <div v-if="hasEarlier" class="stream-earlier">
      <button type="button" @click="showEarlier">
        <ArrowUp :size="13" />
        {{ t('stream.loadEarlier') }}
        <em>{{ t('stream.hiddenCount', { n: startIndex }) }}</em>
      </button>
    </div>
    <template v-for="item in visibleItems" :key="item.key">
      <details v-if="item.kind === 'batch'" class="activity-batch">
        <summary class="batch-header">
          <ChevronRight :size="12" class="chevron" />
          <span class="batch-title">{{ t('stream.agentActivity') }}</span>
          <span class="batch-meta">· {{ t('stream.agents', { n: item.agents.length }) }}</span>
          <span v-if="batchDetailCount(item)" class="batch-meta">· {{ t('stream.details', { n: batchDetailCount(item) }) }}</span>
          <span v-if="item.key === latestBatchKey" class="batch-summary" :title="batchSummary(item)">
            <Transition name="activity-update">
              <span :key="batchSummary(item)" class="batch-summary-text">{{ batchSummary(item) }}</span>
            </Transition>
          </span>
          <span v-if="item.agents.some(agent => agent.working)" class="tool-state">
            <Clock3 :size="12" />
            {{ t('stream.running') }}
          </span>
          <time :title="fullEventTime(item.last)">{{ eventTime(item.last) }}</time>
        </summary>
        <div class="batch-agents">
          <details v-for="agent in item.agents" :key="agent.key" class="turn">
            <summary class="turn-header">
              <ChevronRight :size="12" class="chevron" />
              <span class="turn-agent">{{ agent.agent.name }}</span>
              <span v-if="detailCount(agent)">· {{ t('stream.details', { n: detailCount(agent) }) }}</span>
              <span v-if="agent.autoApprovals" class="turn-approvals" :title="t('confirm.autoConfirmed')">· <Check :size="11" /> {{ agent.autoApprovals }}</span>
              <span v-if="agent.tokens !== null" class="token-usage" :title="t('stream.tokensTitle')">· {{ formatTokens(agent.tokens) }} {{ t('app.tokens') }}</span>
              <span v-if="agent.working" class="tool-state"><Clock3 :size="12" />{{ t('stream.running') }}</span>
              <time :title="fullEventTime(agent.last)">{{ eventTime(agent.last) }}</time>
            </summary>
            <div class="turn-steps">
              <template v-for="step in agent.steps" :key="step.key">
                <details v-if="step.kind === 'reason'" class="reasoning">
                  <summary><ChevronRight :size="11" class="chevron" />{{ t('stream.reasoning') }}</summary>
                  <MarkdownText :content="step.event.payload.reasoning!" :enabled="markdown" />
                </details>
                <ToolCalls v-else-if="step.kind === 'tools'" :tools="step.tools" />
                <ConfirmPill v-else :event="step.event" />
              </template>
            </div>
          </details>
        </div>
      </details>

      <template v-else>
        <div v-if="item.data.name === 'ModelWorkingEvent'" class="working">
          <span class="working-dot" /> {{ t('stream.working', { name: item.data.agent.name }) }}
        </div>

        <ConfirmPill v-else-if="item.data.name === 'ConfirmEvent'" :event="item.data" />

        <section v-else-if="item.data.name === 'ShowHelpEvent'" class="command-result">
          <header><Terminal :size="15" /> {{ t('stream.availableCommands') }}</header>
          <div v-for="command in item.data.payload.commands" :key="command.name" class="command-line">
            <code>/{{ command.name }}</code><span>{{ command.description }}</span>
          </div>
        </section>

        <section v-else-if="item.data.name === 'ShowToolsEvent'" class="tools-result">
          <header><Wrench :size="15" /> {{ t('stream.tools') }} <span>{{ item.data.payload.tools.length }}</span></header>
          <div v-if="!item.data.payload.tools.length" class="tools-empty">{{ t('stream.noTools') }}</div>
          <div v-for="tool in item.data.payload.tools" v-else :key="tool.name" class="tool-listing">
            <div class="tool-listing-name">
              <code>{{ tool.name }}</code>
              <span v-for="capability in tool.required_capabilities" :key="capability" class="capability-chip">{{ capability }}</span>
            </div>
            <p>{{ tool.description || t('stream.noDescription') }}</p>
          </div>
        </section>

        <section v-else-if="item.data.name === 'ShowExtensionsEvent'" class="extensions-result">
          <header><Puzzle :size="15" /> {{ t('stream.extensions') }} <span>{{ item.data.payload.extensions.length }}</span></header>
          <div v-if="!item.data.payload.extensions.length" class="tools-empty">{{ t('stream.noExtensions') }}</div>
          <div v-for="ext in item.data.payload.extensions" v-else :key="ext.name" class="tool-listing">
            <div class="tool-listing-name">
              <code>{{ ext.name }}</code>
              <span class="extension-chip" :class="ext.status">{{ ext.status }}</span>
            </div>
            <div>
              <p>{{ ext.description || t('stream.noDescription') }}</p>
              <p v-if="ext.reason" class="extension-reason" :class="ext.status">{{ ext.reason }}</p>
            </div>
          </div>
        </section>

        <section v-else-if="item.data.name === 'ShowHistoryEvent'" class="history-result">
          <header>{{ t('stream.conversationHistory') }}</header>
          <div v-for="(message, index) in item.data.payload.history" :key="index" class="history-line">
            <span>{{ message.role }}</span>
            <pre>{{ typeof message.content === 'string' ? message.content : JSON.stringify(message.content, null, 2) }}</pre>
          </div>
        </section>

        <div v-else-if="item.data.name === 'UserCommandEvent'" class="command-invocation">
          <Terminal :size="13" /> /{{ item.data.payload.name }}<span v-if="item.data.payload.arguments"> {{ item.data.payload.arguments }}</span>
        </div>

        <section v-else-if="item.data.name === 'HTMLInfoEvent'" class="html-info-result">
          <header v-if="item.data.payload.title">{{ item.data.payload.title }}</header>
          <HtmlText :content="item.data.payload.html" />
        </section>

        <div
          v-else-if="isPlainTextEvent(item.data) && !isUser(item.data)"
          class="stream-notice"
          :class="{ warning: item.data.name === 'WarningEvent', error: item.data.name === 'ErrorEvent' }"
          role="note"
          :aria-label="label(item.data)"
        >
          <Info v-if="item.data.name === 'InfoEvent'" :size="13" />
          <TriangleAlert v-else-if="item.data.name === 'WarningEvent'" :size="13" />
          <CircleAlert v-else :size="13" />
          <div class="notice-content">
            <span class="notice-meta">
              <time :title="fullEventTime(item.data)">{{ eventTime(item.data) }}</time>
              <button
                type="button"
                class="message-copy"
                :title="t('stream.copyMessage')"
                :class="{ copied: copiedKey === item.key }"
                @click="copyMessage(item.key, item.data)"
              >
                <Copy v-if="copiedKey !== item.key" :size="12" />
                <Check v-else :size="12" />
              </button>
            </span>
            <MarkdownText :content="displayText(item.data)" :enabled="markdown" plain />
          </div>
        </div>

        <article v-else class="message" :class="{
          user: isUser(item.data),
        }">
          <div class="message-label">
            {{ isUser(item.data) ? t('stream.you') : label(item.data) }}
            <template v-if="item.data.name === 'UserMessageEvent'">
              <span class="message-recipient">{{ t('stream.to') }}</span> {{ item.data.agent.name }}
            </template>
            <template v-if="item.data.name === 'ModelMessageEvent'">
              <span class="message-recipient">·</span>
              <span class="token-usage" :title="t('stream.tokensTitle')">{{ formatTokens(item.data.payload.total_tokens) }} {{ t('app.tokens') }}</span>
            </template>
            <time :title="fullEventTime(item.data)">{{ eventTime(item.data) }}</time>
            <button
              type="button"
              class="message-copy"
              :title="t('stream.copyMessage')"
              :class="{ copied: copiedKey === item.key }"
              @click="copyMessage(item.key, item.data)"
            >
              <Copy v-if="copiedKey !== item.key" :size="12" />
              <Check v-else :size="12" />
            </button>
          </div>
          <details v-if="item.data.name === 'ModelMessageEvent' && item.data.payload.reasoning" class="reasoning">
            <summary><ChevronRight :size="11" class="chevron" />{{ t('stream.reasoning') }}</summary>
            <MarkdownText :content="item.data.payload.reasoning" :enabled="markdown" />
          </details>
          <MarkdownText v-if="displayText(item.data)" :content="displayText(item.data)" :enabled="markdown" :plain="isPlainTextEvent(item.data)" />
          <div v-if="item.data.name === 'UserMessageEvent' && item.data.payload.images.length" class="message-images">
            <a v-for="image in item.data.payload.images" :key="image.value" :href="image.value" target="_blank" rel="noopener noreferrer">
              <img :src="image.value" :alt="t('stream.attachedImage')">
            </a>
          </div>
        </article>
      </template>
    </template>
  </div>
</template>
