<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Flame, X } from 'lucide-vue-next'
import type { DisplayEvent } from '../types'

const props = defineProps<{ events: DisplayEvent[]; rootId: string; running: Set<string> }>()
defineEmits<{ close: [] }>()
const { t } = useI18n()

type ExecInfo = { agentId: string; agentName: string; parent: string | null; tool: string | null; start: number; end: number }
type Segment = { kind: 'model' | 'tool' | 'live'; label: string; start: number; end: number; error: boolean; callId: string }
type Bar = { kind: 'model' | 'tool' | 'live'; left: number; width: number; label: string; tip: string; error: boolean; spawns: boolean; flatLeft: boolean; flatRight: boolean }
type Row = { key: string; label: string; depth: number; total: string; bars: Bar[] }
type Tick = { at: number; label: string }

const LABEL_W = 182
const DURATION_W = 44
const GAPS = 16
// idle compression only zeroes gaps where nothing ran, so real sub-second gaps
// survive on the axis: they count as touching (bars flatten, no break marker)
const SUB_SECOND = 1
const RESOLUTIONS = [
  { value: 0, label: 'off' },
  { value: 60, label: '1 min' },
  { value: 300, label: '5 min' },
  { value: 900, label: '15 min' },
  { value: 1800, label: '30 min' },
  { value: 3600, label: '1 h' },
]
const RULER_STEPS = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]

function durationText(seconds: number): string {
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  if (seconds >= 10) return `${seconds.toFixed(0)}s`
  if (seconds >= 1) return `${seconds.toFixed(1)}s`
  return `${Math.round(seconds * 1000)}ms`
}

const resolution = ref(0)

const hover = ref<{ text: string; x: number; y: number } | null>(null)
function setHover(event: PointerEvent, text: string) {
  hover.value = {
    text,
    x: Math.min(event.clientX + 12, window.innerWidth - 170),
    y: Math.min(event.clientY + 16, window.innerHeight - 32),
  }
}

const view = computed(() => {
  const events = props.events
  const execs = new Map<string, ExecInfo>()
  const openModel = new Map<string, { callId: string; start: number }>()
  const openTools = new Map<string, { exec: string; start: number; label: string }>()
  const segments: { exec: string; segment: Segment }[] = []

  // a model turn closes at its message, first tool call, or next retry attempt
  function closeModel(exec: string, end: number) {
    const open = openModel.get(exec)
    if (!open) return
    openModel.delete(exec)
    if (end > open.start) {
      segments.push({ exec, segment: { kind: 'model', label: t('flame.model'), start: open.start, end, error: false, callId: '' } })
    }
  }

  for (const ev of events) {
    const exec = ev.span.execution
    if (exec) {
      const info = execs.get(exec)
      if (info) {
        info.start = Math.min(info.start, ev.timestamp)
        info.end = Math.max(info.end, ev.timestamp)
      } else {
        execs.set(exec, {
          agentId: ev.agent.identifier,
          agentName: ev.agent.name,
          parent: ev.span.parent?.execution ?? null,
          tool: ev.span.parent?.tool_call ?? null,
          start: ev.timestamp,
          end: ev.timestamp,
        })
      }
    }
    if (ev.name === 'ModelWorkingEvent' && exec) {
      closeModel(exec, ev.timestamp)
      openModel.set(exec, { callId: ev.payload.model_call_id, start: ev.timestamp })
    } else if (ev.name === 'ModelMessageEvent' && exec) {
      if (openModel.get(exec)?.callId === ev.payload.model_call_id) closeModel(exec, ev.timestamp)
    } else if (ev.name === 'ToolCallEvent' && exec) {
      closeModel(exec, ev.timestamp)
      openTools.set(ev.payload.tool_call_id, { exec, start: ev.timestamp, label: ev.payload.tool_name })
    } else if (ev.name === 'ToolResultEvent' && exec) {
      const open = openTools.get(ev.payload.tool_call_id)
      if (open) {
        openTools.delete(ev.payload.tool_call_id)
        segments.push({ exec: open.exec, segment: { kind: 'tool', label: open.label, start: open.start, end: ev.timestamp, error: ev.payload.is_error, callId: ev.payload.tool_call_id } })
      }
    }
  }
  // keep the selected agent's subtree, following span parent links
  const childExecs = new Map<string, string[]>()
  for (const [id, info] of execs) {
    if (!info.parent) continue
    const siblings = childExecs.get(info.parent)
    if (siblings) siblings.push(id)
    else childExecs.set(info.parent, [id])
  }
  const kept = new Map<string, ExecInfo>()
  const queue = [...execs.entries()].filter(([, info]) => info.agentId === props.rootId).map(([id]) => id)
  while (queue.length) {
    const id = queue.pop()!
    if (kept.has(id)) continue
    const info = execs.get(id)
    if (!info) continue
    kept.set(id, info)
    queue.push(...(childExecs.get(id) ?? []))
  }
  if (!kept.size) return null

  // a tool call that owns a child execution is a spawn point
  const spawned = new Set<string>()
  for (const info of kept.values()) {
    if (info.parent && info.tool) spawned.add(`${info.parent}:${info.tool}`)
  }

  // live heads come from the running set, not from unclosed calls — cancellation abandons those too
  const latest = new Map<string, string>()
  for (const [id, info] of kept) {
    const current = latest.get(info.agentId)
    if (!current || info.start >= kept.get(current)!.start) latest.set(info.agentId, id)
  }
  const now = Date.now() / 1000
  for (const [agentId, execId] of latest) {
    const info = kept.get(execId)!
    if (props.running.has(agentId) && info.end < now) {
      segments.push({ exec: execId, segment: { kind: 'live', label: '', start: info.end, end: now, error: false, callId: '' } })
    }
  }

  const mine = segments.filter(entry => kept.has(entry.exec))

  // compress the axis: union of busy intervals, idle gaps collapse to zero
  const busy: { start: number; end: number; offset: number }[] = []
  for (const { segment } of [...mine].sort((a, b) => a.segment.start - b.segment.start)) {
    const last = busy.at(-1)
    if (last && segment.start <= last.end) last.end = Math.max(last.end, segment.end)
    else busy.push({ start: segment.start, end: segment.end, offset: 0 })
  }
  let cursor = 0
  for (const interval of busy) {
    interval.offset = cursor
    cursor += interval.end - interval.start
  }
  const total = cursor
  if (total <= 0) return null
  function position(time: number): number {
    for (const interval of busy) {
      if (time <= interval.start) return interval.offset
      if (time <= interval.end) return interval.offset + (time - interval.start)
    }
    return total
  }

  // zoom: show the most recent window; older bars fall off the left edge
  const windowed = resolution.value > 0 ? Math.min(total, resolution.value) : total
  const windowStart = total - windowed
  const step = RULER_STEPS.find(value => windowed / value <= 6) ?? 3600
  const ticks: Tick[] = []
  for (let at = Math.ceil(windowStart / step) * step; at <= total; at += step) {
    ticks.push({ at: ((at - windowStart) / windowed) * 100, label: at === 0 ? '0' : durationText(at) })
  }
  const gutter = LABEL_W + DURATION_W + GAPS

  // seam where an idle gap was compressed away
  const breaks: { left: string; tip: string }[] = []
  for (let index = 1; index < busy.length; index += 1) {
    const skipped = busy[index].start - busy[index - 1].end
    if (skipped < SUB_SECOND) continue
    const x = busy[index].offset - windowStart
    if (x < 0) continue
    breaks.push({
      left: `calc(${LABEL_W + GAPS / 2}px + (100% - ${gutter}px) * ${(x / windowed).toFixed(6)})`,
      tip: t('flame.skipped', { time: durationText(skipped) }),
    })
  }

  const depthOf = (info: ExecInfo): number => {
    let depth = 0
    let parent = info.parent
    while (parent && kept.has(parent)) {
      depth += 1
      parent = kept.get(parent)!.parent
    }
    return depth
  }

  const rows: Row[] = [...kept.entries()]
    .sort(([, a], [, b]) => a.start - b.start)
    .map(([id, info]) => {
      const own = mine
        .filter(entry => entry.exec === id)
        .map(entry => entry.segment)
        .sort((a, b) => a.start - b.start)
      return {
        key: id,
        label: info.agentName,
        depth: depthOf(info),
        total: durationText(own.reduce((sum, segment) => sum + (segment.end - segment.start), 0)),
        bars: own.map((segment, index) => {
          const start = position(segment.start) - windowStart
          const width = Math.max(0.25, position(segment.end) - position(segment.start))
          const prev = own[index - 1]
          const next = own[index + 1]
          return {
            kind: segment.kind,
            left: (start / windowed) * 100,
            width: (width / windowed) * 100,
            label: segment.label,
            error: segment.error,
            spawns: segment.kind === 'tool' && spawned.has(`${id}:${segment.callId}`),
            flatLeft: !!prev && position(segment.start) - position(prev.end) < SUB_SECOND,
            flatRight: !!next && position(next.start) - position(segment.end) < SUB_SECOND,
            tip: segment.kind === 'live'
              ? t('flame.running')
              : `${segment.label} · ${durationText(segment.end - segment.start)}`,
          }
        }),
      }
    })
    // rows with no bar inside the visible window carry no information
    .filter(row => row.bars.some(bar => bar.left + bar.width > 0))

  return {
    rows,
    ticks,
    breaks,
    total: durationText(total),
    grid: `${((step / windowed) * 100).toFixed(4)}%`,
  }
})
</script>

<template>
  <section class="flame-panel" :aria-label="t('flame.title')">
    <header class="flame-head">
      <Flame :size="13" />
      <span class="flame-title">{{ t('flame.title') }}</span>
      <span v-if="view" class="flame-total">{{ view.total }} {{ t('flame.active') }}</span>
      <span class="flame-legend">
        <i class="flame-chip model" />{{ t('flame.model') }}
        <i class="flame-chip tool" />{{ t('flame.tool') }}
        <i class="flame-chip error" />{{ t('flame.failed') }}
        <i class="flame-chip live" />{{ t('flame.running') }}
      </span>
      <select v-model.number="resolution" class="flame-resolution" :aria-label="t('flame.resolution')">
        <option v-for="option in RESOLUTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
      </select>
      <button class="icon-button flame-close" :title="t('flame.close')" :aria-label="t('flame.close')" @click="$emit('close')"><X :size="14" /></button>
    </header>
    <div v-if="!view" class="flame-empty">{{ t('flame.empty') }}</div>
    <div v-else class="flame-body" @scroll="hover = null">
      <div class="flame-canvas" :style="{ '--grid': view.grid }">
        <div class="flame-row flame-ruler">
          <span class="flame-label" />
          <span class="flame-track">
            <span v-for="tick in view.ticks" :key="tick.at" class="flame-tick" :style="{ left: `${tick.at}%` }">
              <i /><em>{{ tick.label }}</em>
            </span>
          </span>
          <span class="flame-duration" />
        </div>
        <div v-for="row in view.rows" :key="row.key" class="flame-row">
          <span class="flame-label" :style="{ paddingLeft: `${row.depth * 12}px` }">
            <i v-if="row.depth" class="flame-elbow" />{{ row.label }}
          </span>
          <span class="flame-track">
            <span
              v-for="(bar, index) in row.bars"
              :key="index"
              class="flame-seg"
              :class="[bar.kind, { error: bar.error, spawns: bar.spawns, 'flat-left': bar.flatLeft, 'flat-right': bar.flatRight }]"
              :style="{ left: `${bar.left}%`, width: `${bar.width}%` }"
              @pointermove="setHover($event, bar.tip)"
              @pointerleave="hover = null"
            >{{ bar.label }}</span>
          </span>
          <span class="flame-duration">{{ row.total }}</span>
        </div>
        <span v-for="brk in view.breaks" :key="brk.left" class="flame-break" :style="{ left: brk.left }" @pointermove="setHover($event, brk.tip)" @pointerleave="hover = null" />
      </div>
    </div>
    <span v-if="hover" class="flame-hover" :style="{ left: `${hover.x}px`, top: `${hover.y}px` }">{{ hover.text }}</span>
  </section>
</template>

<style scoped>
.flame-panel { flex: none; display: flex; flex-direction: column; max-height: 40vh; border-top: 1px solid var(--line); background: var(--panel); }
.flame-head { flex: none; display: flex; align-items: center; gap: 8px; height: 34px; padding: 0 14px; color: var(--accent-dark); font-size: 11px; }
.flame-title { font-weight: 600; }
.flame-total { color: var(--muted); font: 10px/1 'Fira Code', monospace; }
.flame-legend { display: flex; align-items: center; gap: 5px; margin-left: auto; color: var(--muted); font-size: 10px; }
.flame-chip { width: 8px; height: 8px; }
.flame-chip.model { background: var(--accent); }
.flame-chip.tool { background: var(--warm); }
.flame-chip.error { background: var(--danger); }
.flame-chip.live { background: repeating-linear-gradient(45deg, var(--muted) 0 2px, transparent 2px 4px); opacity: .6; }
.flame-resolution { height: 20px; padding: 0 4px; border: 1px solid var(--line-strong); border-radius: 4px; background: var(--input); color: var(--ink); font-size: 10px; }
.flame-close { width: 24px; height: 24px; }
.flame-empty { padding: 14px; color: var(--muted); font-size: 12px; }
.flame-body { overflow-y: auto; padding: 0 14px 10px; scrollbar-width: none; }
.flame-body::-webkit-scrollbar { display: none; }
.flame-canvas { position: relative; }
.flame-break { position: absolute; top: 20px; bottom: 0; width: 9px; margin-left: -4px; z-index: 0; cursor: help; }
.flame-break::before { content: ''; position: absolute; top: 0; bottom: 0; left: 4px; border-left: 1px dashed var(--line-strong); }
.flame-row { display: flex; align-items: center; gap: 8px; height: 26px; }
.flame-ruler { position: sticky; top: 0; z-index: 2; height: 20px; background: var(--panel); will-change: transform; }
.flame-label { flex: none; width: 182px; display: flex; align-items: center; gap: 4px; overflow: hidden; color: var(--ink); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; user-select: none; }
.flame-ruler .flame-label, .flame-ruler .flame-duration { box-shadow: 0 1px 0 var(--line); }
.flame-elbow { flex: none; width: 7px; height: 7px; border-left: 1px solid var(--line-strong); border-bottom: 1px solid var(--line-strong); }
.flame-track { position: relative; flex: 1; height: 100%; overflow: hidden; background: repeating-linear-gradient(to right, transparent, transparent calc(var(--grid, 20%) - 1px), color-mix(in srgb, var(--line) 60%, transparent) calc(var(--grid, 20%) - 1px), color-mix(in srgb, var(--line) 60%, transparent) var(--grid, 20%)); }
.flame-ruler .flame-track { background: none; }
.flame-tick { position: absolute; top: 0; bottom: 0; user-select: none; }
.flame-tick i { position: absolute; top: 4px; width: 1px; height: 4px; background: var(--line-strong); }
.flame-tick em { position: absolute; top: 9px; left: 3px; color: var(--muted); font: 9px/1 'Fira Code', monospace; font-style: normal; }
.flame-seg { position: absolute; top: 5px; bottom: 5px; display: block; overflow: hidden; min-width: 3px; border-radius: 3px; padding: 0 5px; color: #fff; font-size: 9px; line-height: 16px; text-overflow: ellipsis; white-space: nowrap; }
.flame-seg.flat-left { border-radius: 0 3px 3px 0; }
.flame-seg.flat-right { border-radius: 3px 0 0 3px; }
.flame-seg.flat-left.flat-right { border-radius: 0; }
.flame-seg.model { background-color: var(--accent); }
.flame-seg.tool { background-color: var(--warm); }
.flame-seg.error { background-color: var(--danger); }
.flame-seg.live { opacity: .55; background-color: var(--muted); background-image: repeating-linear-gradient(45deg, rgba(255, 255, 255, .38) 0 3px, transparent 3px 6px); }
.flame-seg.spawns { box-shadow: inset 0 0 0 1.5px color-mix(in srgb, #fff 70%, transparent); }

.flame-hover { position: fixed; z-index: 40; max-width: 340px; padding: 3px 7px; border: 1px solid var(--line-strong); border-radius: 4px; background: var(--panel); color: var(--ink); font: 10px/1.5 'Fira Code', monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; pointer-events: none; box-shadow: 0 3px 10px var(--shadow); }
.flame-duration { flex: none; width: 44px; color: var(--muted); font: 9px/1 'Fira Code', monospace; text-align: right; user-select: none; }
</style>
