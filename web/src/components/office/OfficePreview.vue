<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { DocxScrollViewer } from '@silurus/ooxml/docx'
import type { XlsxViewer } from '@silurus/ooxml/xlsx'
import type { PptxScrollViewer } from '@silurus/ooxml/pptx'
import { errorMessage, fetchBinary } from '../../api'
import { preserveViewportCenter } from '../../previewViewport'
import type { OfficeFormat } from './formats'

type Viewer = DocxScrollViewer | XlsxViewer | PptxScrollViewer

const props = defineProps<{ src: string; format: OfficeFormat; downloadUrl: string }>()
const zoom = defineModel<number>('zoom', { required: true })
const { t } = useI18n()
const host = ref<HTMLDivElement>()
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const page = ref(0)
const total = ref(0)
let viewer: Viewer | undefined

watch([() => props.src, () => props.format], async ([src, format], _, onCleanup) => {
  const controller = new AbortController()
  const mount = document.createElement('div')
  mount.className = 'office-surface'
  let instance: Viewer | undefined
  const dispose = () => {
    controller.abort()
    viewer = undefined
    instance?.destroy()
    mount.remove()
  }
  onCleanup(dispose)
  loading.value = true
  busy.value = false
  error.value = ''
  page.value = 0
  total.value = 0

  const onError = (reason: unknown) => {
    if (!controller.signal.aborted) error.value = errorMessage(reason, t('preview.officeError'))
  }
  const onPageChange = (index: number, count: number) => {
    if (controller.signal.aborted) return
    page.value = index
    total.value = count
  }
  const options = {
    useGoogleFonts: false,
    enableHyperlinks: false,
    zoomMin: 0.25,
    zoomMax: 4,
    onError,
    onScaleChange: (scale: number) => {
      if (!controller.signal.aborted && instance?.getScale() === scale) zoom.value = Math.round(scale * 100)
    },
  }

  try {
    const [bytes, module] = await Promise.all([
      fetchBinary(src, controller.signal),
      format === 'docx' ? import('@silurus/ooxml/docx')
        : format === 'xlsx' ? import('@silurus/ooxml/xlsx')
          : import('@silurus/ooxml/pptx'),
    ])
    await nextTick()
    if (controller.signal.aborted) return
    if (!host.value) throw new Error(t('preview.officeError'))
    host.value.append(mount)
    if ('XlsxViewer' in module) {
      instance = new module.XlsxViewer(mount, { ...options, showZoomSlider: false })
    } else if ('DocxScrollViewer' in module) {
      instance = new module.DocxScrollViewer(mount, { ...options, enableTextSelection: true, onVisiblePageChange: onPageChange })
    } else {
      instance = new module.PptxScrollViewer(mount, { ...options, enableTextSelection: true, enableMediaPlayback: false, onVisibleSlideChange: onPageChange })
    }
    await instance.load(bytes)
    if (controller.signal.aborted) return
    viewer = instance
    if (format === 'xlsx') await instance.setScale(zoom.value / 100)
    else await instance.fitWidth()
    if (!controller.signal.aborted) zoom.value = Math.round(instance.getScale() * 100)
  } catch (reason) {
    onError(reason)
  } finally {
    if (!controller.signal.aborted) {
      loading.value = false
      if (error.value) dispose()
    }
  }
}, { immediate: true })

function preserveOfficeCenter() {
  if (props.format === 'xlsx') return
  const canvases = [...(host.value?.querySelectorAll('canvas') ?? [])]
  const first = canvases[0]
  let viewport = first?.parentElement
  while (viewport && viewport !== host.value && !/(auto|scroll)/.test(getComputedStyle(viewport).overflow)) {
    viewport = viewport.parentElement
  }
  if (!viewport?.clientWidth || !first?.getBoundingClientRect().width) return
  const center = viewport.getBoundingClientRect().top + viewport.clientHeight / 2
  const content = canvases.find(canvas => {
    const bounds = canvas.getBoundingClientRect()
    return bounds.top <= center && bounds.bottom >= center
  }) ?? first
  return preserveViewportCenter(viewport, content)
}

async function act(action: (active: Viewer) => void | Promise<void>) {
  const active = viewer
  if (!active || loading.value || busy.value || error.value) return
  busy.value = true
  try {
    const scale = active.getScale()
    const restore = preserveOfficeCenter()
    await action(active)
    if (viewer === active && active.getScale() !== scale) restore?.()
  } catch (reason) {
    if (viewer === active) error.value = errorMessage(reason, t('preview.officeError'))
  } finally {
    if (viewer === active) busy.value = false
  }
}

watch(zoom, async value => {
  const active = viewer
  if (!active || loading.value || Math.abs(active.getScale() - value / 100) < 0.005) return
  try {
    const restore = preserveOfficeCenter()
    await active.setScale(value / 100)
    if (viewer === active && active.getScale() === value / 100) restore?.()
  } catch (reason) {
    if (viewer === active) error.value = errorMessage(reason, t('preview.officeError'))
  }
})
</script>

<template>
  <div class="office-preview">
    <div v-if="loading" class="office-status" role="status">{{ t('preview.loading') }}</div>
    <div v-if="error" class="office-status" role="alert">
      <span>{{ error }}</span>
      <a :href="downloadUrl" download>{{ t('files.download') }}</a>
    </div>
    <div ref="host" v-show="!error" class="office-host" :class="{ 'is-loading': loading }" />
    <div v-if="!loading && !error" class="office-controls">
      <span v-if="format !== 'xlsx'">{{ page + 1 }} / {{ total }}</span>
      <button class="fit-width" :disabled="busy" @click="act(active => active.fitWidth())">{{ t('preview.fitWidth') }}</button>
    </div>
  </div>
</template>

<style scoped>
.office-preview { min-height: 0; min-width: 0; flex: 1; display: flex; flex-direction: column; overflow: hidden; background: var(--panel); }
.office-host { min-height: 0; min-width: 0; flex: 1; overflow: auto; }
.office-host.is-loading { visibility: hidden; }
.office-host :deep(.office-surface) { width: 100%; height: 100%; overflow: auto; }
.office-status { padding: 16px; display: flex; gap: 12px; align-items: center; justify-content: center; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.office-controls { flex: none; display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 30px; border-top: 1px solid var(--line); font-size: 11px; }
.fit-width { border: 0; background: transparent; color: var(--muted); cursor: pointer; font-size: 11px; }
.fit-width:disabled { cursor: default; opacity: .5; }
</style>
