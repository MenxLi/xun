<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'

const props = defineProps<{ content: string }>()
const html = computed(() => DOMPurify.sanitize(props.content))
</script>

<template>
  <div class="markdown html-content" v-html="html" />
</template>
