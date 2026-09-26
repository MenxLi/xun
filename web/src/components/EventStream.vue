<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowUp, Check, ChevronRight, CircleAlert, Clock3, Copy, Link2, Link2Off, Terminal, Wrench } from 'lucide-vue-next'
import MarkdownText from './MarkdownText.vue'
import HtmlText from './HtmlText.vue'
import ToolCalls from './ToolCalls.vue'
import ConfirmPill from './ConfirmPill.vue'
import { eventTime, formatTokens, fullEventTime } from '../api'
import { copyText } from '../clipboard'
import type { AgentInfo, ConfirmDisplayEvent, DisplayEvent, ModelMessageDisplayEvent, ToolItem } from '../types'

const props = defineProps<{ events: DisplayEvent[]; markdown: boolean }>()
const { t } = useI18n()

type TurnStep =
  | { kind: 'reason'; key: string; event: ModelMessageDisplayEvent }
  | { kind: 'tools'; key: string; tools: ToolItem[] }
  | { kind: 'confirm'; key: string; event: ConfirmDisplayEvent }

// One working stretch of a single agent: consecutive reasoning-only turns, tool calls,
// and confirmations share one header (agent · tokens · time) and a thread line.
type TurnItem = { kind: 'turn'; key: string; agent: AgentInfo; steps: TurnStep[]; tokens: number; last: DisplayEvent; working: boolean }

type StreamItem =
  | { kind: 'event'; key: string; data: DisplayEvent }
  | { kind: 'tool'; key: string; tool: ToolItem }
  | { kind: 'activity'; key: string; tools: ToolItem[] }
  | TurnItem

const items = computed<StreamItem[]>(() => {
  const output: StreamItem[] = []
  const toolItems = new Map<string, ToolItem>()
  let turn: TurnItem | null = null

  props.events.forEach((data, index) => {
    if (data.name === 'ToolResultEvent') {
      // Results attach to their call in place; they never appear as their own item.
      const tool = toolItems.get(data.payload.tool_call_id)
      if (!tool) return
      tool.result = data
      if (turn?.agent.identifier === data.agent.identifier) turn.last = data
    } else if (data.name === 'ModelWorkingEvent') {
      // Only the trailing one matters; it becomes the live indicator on the last turn below.
    } else if (data.name === 'ModelMessageEvent' && !data.payload.content.trim()) {
      // Tool-only iteration: fold its reasoning (and token count) into a turn, not a full message block.
      if (!turn || turn.agent.identifier !== data.agent.identifier) {
        turn = { kind: 'turn', key: `turn-${index}`, agent: data.agent, steps: [], tokens: data.payload.total_tokens, last: data, working: false }
        output.push(turn)
      }
      turn.tokens = data.payload.total_tokens
      turn.last = data
      if (data.payload.reasoning?.trim()) turn.steps.push({ kind: 'reason', key: `reason-${index}`, event: data })
    } else if (data.name === 'ToolCallEvent') {
      const item: ToolItem = { key: data.payload.tool_call_id || `tool-${index}`, call: data }
      if (data.payload.tool_call_id) toolItems.set(data.payload.tool_call_id, item)
      if (turn?.agent.identifier === data.agent.identifier) {
        const last = turn.steps.at(-1)
        if (last?.kind === 'tools') last.tools.push(item)
        else turn.steps.push({ kind: 'tools', key: `steps-${item.key}`, tools: [item] })
        turn.last = data
      } else {
        turn = null
        // Tool calls outside a turn: a lone call renders on its own, consecutive 2+ merge into an activity group.
        const previous = output.at(-1)
        if (previous?.kind === 'tool') output[output.length - 1] = { kind: 'activity', key: `activity-${previous.key}`, tools: [previous.tool, item] }
        else if (previous?.kind === 'activity') previous.tools.push(item)
        else output.push({ kind: 'tool', key: `tool-${item.key}`, tool: item })
      }
    } else if (data.name === 'ConfirmEvent' && turn?.agent.identifier === data.agent.identifier) {
      turn.steps.push({ kind: 'confirm', key: `confirm-${index}`, event: data })
      turn.last = data
    } else {
      turn = null
      output.push({ kind: 'event', key: `${data.name}-${index}`, data })
    }
  })

  const live = props.events.at(-1)
  if (live?.name === 'ModelWorkingEvent') {
    const tail = output.at(-1)
    if (tail?.kind === 'turn' && tail.agent.identifier === live.agent.identifier) tail.working = true
    else output.push({ kind: 'event', key: `event-${props.events.length - 1}`, data: live })
  }
  return output.filter(item => item.kind !== 'turn' || item.steps.length > 0 || item.working)
})

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
  // Grow in place for live events; a shrink means the session was replaced.
  rendered.value = total > previous ? rendered.value + (total - previous) : INITIAL_WINDOW
})

const root = ref<HTMLElement>()

async function showEarlier() {
  const container = root.value?.closest<HTMLElement>('.conversation') ?? null
  const previousHeight = container?.scrollHeight ?? 0
  const previousTop = container?.scrollTop ?? 0
  rendered.value = Math.min(totalItems.value, rendered.value + EARLIER_BATCH)
  await nextTick()
  // Compensate scrollTop so prepended items don't shift the view.
  if (container) container.scrollTo({ top: container.scrollHeight - previousHeight + previousTop, behavior: 'instant' })
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
      <section v-if="item.kind === 'turn'" class="turn">
        <div class="turn-header">
          <span class="turn-agent">{{ item.agent.name }}</span>
          <span class="token-usage" :title="t('stream.tokensTitle')">· {{ formatTokens(item.tokens) }} {{ t('app.tokens') }}</span>
          <span v-if="item.working" class="tool-state">
            <Clock3 :size="12" />
            {{ t('stream.running') }}
          </span>
          <time :title="fullEventTime(item.last)">{{ eventTime(item.last) }}</time>
        </div>
        <div class="turn-steps">
          <template v-for="step in item.steps" :key="step.key">
            <details v-if="step.kind === 'reason'" class="reasoning">
              <summary><ChevronRight :size="11" class="chevron" />{{ t('stream.reasoning') }}</summary>
              <MarkdownText :content="step.event.payload.reasoning!" :enabled="markdown" />
            </details>
            <ToolCalls v-else-if="step.kind === 'tools'" :tools="step.tools" />
            <ConfirmPill v-else :event="step.event" />
          </template>
        </div>
      </section>

      <ToolCalls v-else-if="item.kind === 'tool'" :tools="[item.tool]" standalone />

      <ToolCalls v-else-if="item.kind === 'activity'" :tools="item.tools" standalone />

      <template v-else>
        <div v-if="item.data.name === 'AgentBindEvent' || item.data.name === 'AgentUnbindEvent'" class="agent-lifecycle">
          <Link2 v-if="item.data.name === 'AgentBindEvent'" :size="12" />
          <Link2Off v-else :size="12" />
          <span>{{ item.data.agent.name }}</span>
          {{ item.data.name === 'AgentBindEvent' ? t('stream.joined') : t('stream.left') }}
          <time :title="fullEventTime(item.data)">{{ eventTime(item.data) }}</time>
        </div>

        <div v-else-if="item.data.name === 'ModelWorkingEvent'" class="working">
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

        <article v-else class="message" :class="{
          user: isUser(item.data),
          error: item.data.name === 'ErrorEvent',
          warning: item.data.name === 'WarningEvent',
          notice: item.data.name === 'InfoEvent' && !isUser(item.data),
        }">
          <div class="message-label">
            <CircleAlert v-if="item.data.name === 'ErrorEvent'" :size="13" />
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
