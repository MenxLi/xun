<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { preserveViewportCenter } from '../previewViewport'

const props = defineProps<{ zoom: number }>()
const viewport = ref<HTMLDivElement>()
const media = ref<HTMLDivElement>()
let observer: ResizeObserver | undefined
let restoreOnResize: (() => void) | undefined
const size = { width: 0, height: 0 }

function rememberCenter() {
  if (viewport.value?.clientWidth === size.width && viewport.value?.clientHeight === size.height && media.value?.offsetWidth) {
    restoreOnResize = preserveViewportCenter(viewport.value, media.value)
  }
}

onMounted(() => {
  const frame = viewport.value!
  size.width = frame.clientWidth
  size.height = frame.clientHeight
  rememberCenter()
  observer = new ResizeObserver(() => {
    if (frame.clientWidth === size.width && frame.clientHeight === size.height) return
    restoreOnResize?.()
    size.width = frame.clientWidth
    size.height = frame.clientHeight
    rememberCenter()
  })
  observer.observe(frame)
})
onBeforeUnmount(() => observer?.disconnect())

watch(() => props.zoom, async (_, __, onCleanup) => {
  const frame = viewport.value
  const content = media.value
  if (!frame?.clientWidth || !content?.offsetWidth) return
  const restore = preserveViewportCenter(frame, content)
  restoreOnResize = restore
  let cancelled = false
  onCleanup(() => { cancelled = true })
  await nextTick()
  if (!cancelled) {
    restore()
    rememberCenter()
  }
})
</script>

<template>
  <div ref="viewport" class="preview-viewport media-viewport" :style="{ '--media-zoom': zoom / 100 }" @scroll="rememberCenter">
    <div ref="media" class="preview-media">
      <div class="preview-surface"><slot /></div>
    </div>
  </div>
</template>

<style scoped>
.media-viewport { display: flex; overflow: scroll; overflow-anchor: none; container-type: size; }
.preview-media { flex: none; width: calc(100cqw * var(--media-zoom)); height: calc(100cqh * var(--media-zoom)); margin: auto; }
.preview-surface { width: 100cqw; height: 100cqh; transform: scale(var(--media-zoom)); transform-origin: top left; }
.preview-surface > :deep(img), .preview-surface > :deep(iframe) { display: block; width: 100%; height: 100%; }
</style>
