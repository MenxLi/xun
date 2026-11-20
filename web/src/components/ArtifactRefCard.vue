<script setup lang="ts">
import { computed, ref } from 'vue'
import { FileText, Folder } from 'lucide-vue-next'
import { api } from '../api'
import { asFileInfo, canThumb, previewKind } from '../preview'
import type { ArtifactRef } from '../types'

const props = defineProps<{ artifact: ArtifactRef; agentId: string; previewEnabled: boolean; big: boolean }>()
const emit = defineEmits<{ activate: [] }>()

const thumbFailed = ref(false)

const isDir = computed(() => props.artifact.dir === true)
const name = computed(() => props.artifact.path.split('/').pop() || props.artifact.path)
// folders leave the browser as zip archives
const href = computed(() => isDir.value
  ? api.archiveUrl(props.agentId, props.artifact.path)
  : api.downloadUrl(props.agentId, props.artifact.path))
const downloadName = computed(() => isDir.value ? `${name.value}.zip` : name.value)
const thumbUrl = computed(() => api.thumbUrl(props.agentId, props.artifact.path))
const showThumb = computed(() => props.previewEnabled && !isDir.value && canThumb(props.artifact.path) && !thumbFailed.value)
const sub = computed(() => {
  if (props.artifact.note) return props.artifact.note
  const parent = props.artifact.path.split('/').slice(0, -1).join('/')
  return parent ? `${parent}/` : ''
})

// Plain left click: folders open in the file browser, previewable files open the
// preview, anything else downloads; modifier and middle clicks keep native href.
async function onClick(event: MouseEvent) {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  if (!props.previewEnabled) return
  event.preventDefault()
  if (isDir.value) {
    emit('activate')
    return
  }
  try {
    const file = asFileInfo(await api.fileInfo(props.agentId, props.artifact.path))
    if (file && previewKind(file) !== 'unsupported') {
      emit('activate')
      return
    }
  } catch {
    // inspection failed: fall through to the plain download
  }
  const link = document.createElement('a')
  link.href = href.value
  link.download = downloadName.value
  link.click()
}
</script>

<template>
  <component
    :is="previewEnabled ? 'a' : 'div'"
    class="artifact-card"
    :class="{ static: !previewEnabled, image: big && showThumb }"
    :href="previewEnabled ? href : undefined"
    :download="previewEnabled ? downloadName : undefined"
    :title="artifact.note || artifact.path"
    @click="onClick"
  >
    <span class="artifact-thumb">
      <img v-if="showThumb" :src="thumbUrl" loading="lazy" alt="" @error="thumbFailed = true" />
      <Folder v-else-if="isDir" :size="24" />
      <FileText v-else :size="24" />
    </span>
    <span class="artifact-meta">
      <code class="artifact-name">{{ name }}<template v-if="isDir">/</template></code>
      <span v-if="sub" class="artifact-sub">{{ sub }}</span>
    </span>
  </component>
</template>
