<script setup lang="ts">
defineProps<{
  previews: readonly { key: string; agent: string; text: string }[]
  single: boolean
}>()
</script>

<template>
  <TransitionGroup
    name="activity-preview"
    tag="span"
    class="batch-preview"
    :class="{ single }"
    :style="{ '--preview-lines': Math.min(previews.length, 3) }"
  >
    <span v-for="preview in previews" :key="preview.key" class="batch-preview-line">
      <span class="batch-preview-agent">{{ preview.agent }}</span>
      <span class="batch-preview-text">{{ preview.text }}</span>
    </span>
  </TransitionGroup>
</template>

<style scoped>
.batch-preview {
  position: relative;
  /* Height follows the line count (15px lines + 1px gaps) so short previews sit
     tight under the header; the transition keeps growth smooth, and the steady
     3-line case never changes height at all. */
  height: calc(var(--preview-lines, 1) * 16px - 1px);
  transition: height 180ms cubic-bezier(.22, 1, .36, 1);
  margin: 0 19px 6px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  gap: 1px;
  color: var(--muted);
  text-transform: none;
  contain: layout paint;
}
.batch-preview-line {
  flex: 0 0 15px;
  min-width: 0;
  width: 100%;
  height: 15px;
  display: grid;
  grid-template-columns: minmax(64px, 112px) minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  font-weight: 400;
  line-height: 15px;
}
.batch-preview.single .batch-preview-line { grid-template-columns: minmax(0, 1fr); }
.batch-preview-agent, .batch-preview-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.batch-preview-agent { color: var(--muted); font-size: 9px; }
.batch-preview.single .batch-preview-agent { display: none; }
.activity-preview-enter-active,
.activity-preview-leave-active,
.activity-preview-move {
  transition: opacity 180ms ease, transform 180ms cubic-bezier(.22, 1, .36, 1);
}
.activity-preview-enter-from { opacity: 0; transform: translateY(16px); }
.activity-preview-leave-to { opacity: 0; transform: translateY(-16px); }
.activity-preview-leave-active {
  position: absolute;
  left: 0;
  right: 0;
  bottom: calc((var(--preview-lines, 1) - 1) * 16px);
}

@media (max-width: 520px) {
  .batch-preview { height: calc(min(var(--preview-lines, 1), 2) * 16px - 1px); }
}

@media (prefers-reduced-motion: reduce) {
  .batch-preview,
  .activity-preview-enter-active,
  .activity-preview-leave-active,
  .activity-preview-move { transition: none; }
}
</style>