<script setup lang="ts">
import { computed, ref, useId, watchPostEffect } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronDown, ChevronUp } from 'lucide-vue-next'

const props = defineProps<{ collapsible: boolean }>()
const { t } = useI18n()
const contentId = useId()
const content = ref<HTMLElement>()
const expanded = ref(false)
const overflowing = ref(false)
const canCollapse = computed(() => props.collapsible && overflowing.value)
const collapsed = computed(() => canCollapse.value && !expanded.value)
const previewHeight = 240
watchPostEffect(onCleanup => {
  overflowing.value = false
  const element = content.value
  if (!element || !props.collapsible) return
  const observer = new ResizeObserver(entries => {
    for (const entry of entries) overflowing.value = entry.contentRect.height > previewHeight
  })
  observer.observe(element)
  onCleanup(() => observer.disconnect())
})
</script>

<template>
  <div class="collapsible-message" :class="{ collapsed }">
    <div class="message-viewport" :style="collapsed ? { maxHeight: `${previewHeight}px` } : undefined">
      <div :id="contentId" ref="content" class="message-content" :inert="collapsed">
        <slot />
      </div>
    </div>
    <button
      v-if="canCollapse"
      type="button"
      class="message-toggle"
      :aria-expanded="!collapsed"
      :aria-controls="contentId"
      @click="expanded = !expanded"
    >
      <template v-if="collapsed">{{ t('stream.expandMessage') }}<ChevronDown :size="14" /></template>
      <template v-else>{{ t('stream.collapseMessage') }}<ChevronUp :size="14" /></template>
    </button>
  </div>
</template>

<style scoped>
.collapsible-message { position: relative; }
.message-content { display: flow-root; }
.collapsed .message-viewport { overflow: hidden; mask-image: linear-gradient(to bottom, #000 calc(100% - 64px), transparent); }
.message-toggle { display: flex; align-items: center; justify-content: center; gap: 4px; width: 100%; padding: 8px; border: 0; border-radius: 4px; background: transparent; color: var(--accent); font-size: 12px; cursor: pointer; }
.message-toggle:hover { color: var(--accent-dark); }
.collapsed .message-toggle { position: absolute; inset: 0; align-items: flex-end; }
</style>
