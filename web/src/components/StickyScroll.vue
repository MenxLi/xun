<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowDown } from 'lucide-vue-next'

// Auto-scrolls to the bottom while the user is following the stream. Scrolling
// up detaches; scrolling back down re-attaches; `size` growth while detached
// surfaces a "jump to latest" pill instead of moving the view.
const props = defineProps<{ size: number }>()

const { t } = useI18n()
const container = ref<HTMLElement>()
const follow = ref(true)
const unseen = ref(0)
const showJump = computed(() => unseen.value > 0 && !follow.value)

const stickThreshold = 64
let previousSize = props.size
let ignoreScrollUntil = 0

function scrollToEnd(behavior: ScrollBehavior = 'instant') {
  const el = container.value
  if (!el) return
  // Ignore scroll events fired by our own scroll calls so they are not
  // mistaken for the user detaching from the bottom.
  ignoreScrollUntil = performance.now() + (behavior === 'smooth' ? 500 : 100)
  el.scrollTo({ top: el.scrollHeight, behavior })
  follow.value = true
  unseen.value = 0
}

function anchor() {
  nextTick(() => scrollToEnd())
}

function onScroll() {
  if (performance.now() < ignoreScrollUntil) return
  const el = container.value
  const nearBottom = !el || el.scrollHeight - el.scrollTop - el.clientHeight <= stickThreshold
  follow.value = nearBottom
  if (nearBottom) unseen.value = 0
}

watch(() => props.size, size => {
  const added = Math.max(0, size - previousSize)
  previousSize = size
  if (follow.value) nextTick(() => scrollToEnd())
  else unseen.value += added
})

defineExpose({ anchor })
</script>

<template>
  <div class="stream-area">
    <section ref="container" class="conversation" aria-live="polite" @scroll.passive="onScroll"><slot /></section>
    <Transition name="jump-fade">
      <button v-if="showJump" type="button" class="jump-to-latest" @click="scrollToEnd('smooth')">
        <ArrowDown :size="13" />
        <span>{{ t('stream.jumpToLatest') }}</span>
        <em v-if="unseen">{{ t('stream.newCount', { n: unseen }) }}</em>
      </button>
    </Transition>
  </div>
</template>
