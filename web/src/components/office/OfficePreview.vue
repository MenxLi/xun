<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'
import type { DocxViewer } from '@silurus/ooxml/docx'
import type { XlsxViewer } from '@silurus/ooxml/xlsx'
import type { PptxViewer } from '@silurus/ooxml/pptx'
import { errorMessage, fetchBinary } from '../../api'
import type { OfficeFormat } from './formats'

type Viewer = DocxViewer | XlsxViewer | PptxViewer

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
    } else {
      const canvas = document.createElement('canvas')
      mount.append(canvas)
      instance = 'DocxViewer' in module
        ? new module.DocxViewer(canvas, { ...options, container: mount, onPageChange })
        : new module.PptxViewer(canvas, { ...options, enableMediaPlayback: false, onSlideChange: onPageChange })
    }
    await instance.load(bytes)
    if (controller.signal.aborted) return
    viewer = instance
    if (format === 'xlsx') await instance.setScale(zoom.value / 100)
    else await instance.fitWidth()
  } catch (reason) {
    onError(reason)
  } finally {
    if (!controller.signal.aborted) {
      loading.value = false
      if (error.value) dispose()
    }
  }
}, { immediate: true })

async function act(action: (active: Viewer) => void | Promise<void>) {
  const active = viewer
  if (!active || loading.value || busy.value || error.value) return
  busy.value = true
  try {
    await action(active)
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
    await active.setScale(value / 100)
  } catch (reason) {
    if (viewer === active) error.value = errorMessage(reason, t('preview.officeError'))
  }
})

function navigate(delta: number) {
  void act(active => {
    if ('goToPage' in active) return active.goToPage(page.value + delta)
    if ('goToSlide' in active) return active.goToSlide(page.value + delta)
  })
}
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
      <template v-if="format !== 'xlsx'">
        <button class="icon-button" :title="t('preview.previousPage')" :disabled="busy || page <= 0" @click="navigate(-1)">
          <ChevronLeft :size="14" />
        </button>
        <span>{{ page + 1 }} / {{ total }}</span>
        <button class="icon-button" :title="t('preview.nextPage')" :disabled="busy || page + 1 >= total" @click="navigate(1)">
          <ChevronRight :size="14" />
        </button>
      </template>
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
