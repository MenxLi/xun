<script setup lang="ts">
defineProps<{
  previews: readonly { key: string; agent: string; text: string }[]
  single: boolean
}>()
</script>

<template>
  <TransitionGroup name="activity-preview" tag="span" class="batch-preview" :class="{ single }">
    <span v-for="preview in previews" :key="preview.key" class="batch-preview-line">
      <span class="batch-preview-agent">{{ preview.agent }}</span>
      <span class="batch-preview-text">{{ preview.text }}</span>
    </span>
  </TransitionGroup>
</template>

<style scoped>
.batch-preview {
  position: relative;
  height: 47px;
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
.activity-preview-leave-active { position: absolute; left: 0; right: 0; top: 0; }

@media (max-width: 520px) {
  .batch-preview { height: 31px; }
}

@media (prefers-reduced-motion: reduce) {
  .activity-preview-enter-active,
  .activity-preview-leave-active,
  .activity-preview-move { transition: none; }
}
</style>