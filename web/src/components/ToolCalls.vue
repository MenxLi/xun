<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronRight, Clock3 } from 'lucide-vue-next'
import { eventTime, fullEventTime } from '../api'
import type { ToolItem } from '../types'

const props = defineProps<{ tools: ToolItem[]; standalone?: boolean }>()
const { t } = useI18n()

const running = computed(() => props.tools.some(tool => !tool.result))
</script>

<template>
  <details v-if="tools.length === 1" class="tool-row" :class="{ 'tool-single': standalone }">
    <summary>
      <ChevronRight :size="standalone ? 14 : 13" class="chevron" />
      <span>{{ tools[0].call.payload.tool_name || t('stream.tool') }}</span>
      <span v-if="!tools[0].result" class="tool-state">
        <Clock3 :size="12" />
        {{ t('stream.running') }}
      </span>
      <time :title="fullEventTime(tools[0].call)">{{ eventTime(tools[0].call) }}</time>
    </summary>
    <div class="tool-detail">
      <span>{{ t('stream.input') }}</span><pre>{{ JSON.stringify(tools[0].call.payload.args, null, 2) }}</pre>
      <template v-if="tools[0].result"><span>{{ t('stream.output') }}</span><pre>{{ JSON.stringify(tools[0].result.payload.result, null, 2) }}</pre></template>
    </div>
  </details>

  <details v-else class="activity-group">
    <summary>
      <ChevronRight :size="standalone ? 14 : 13" class="chevron" />
      <span>{{ standalone ? t('stream.activity') + ' · ' : '' }}{{ t('stream.steps', { n: tools.length }) }}</span>
      <span v-if="running" class="tool-state">
        <Clock3 :size="12" />
        {{ t('stream.running') }}
      </span>
    </summary>
    <div class="activity-list">
      <details v-for="tool in tools" :key="tool.key" class="tool-row">
        <summary>
          <ChevronRight :size="13" class="chevron" />
          <span>{{ tool.call.payload.tool_name || t('stream.tool') }}</span>
          <span v-if="!tool.result" class="tool-state">
            <Clock3 :size="12" />
            {{ t('stream.running') }}
          </span>
          <time :title="fullEventTime(tool.call)">{{ eventTime(tool.call) }}</time>
        </summary>
        <div class="tool-detail">
          <span>{{ t('stream.input') }}</span><pre>{{ JSON.stringify(tool.call.payload.args, null, 2) }}</pre>
          <template v-if="tool.result"><span>{{ t('stream.output') }}</span><pre>{{ JSON.stringify(tool.result.payload.result, null, 2) }}</pre></template>
        </div>
      </details>
    </div>
  </details>
</template>
