<script setup lang="ts">
withDefaults(defineProps<{ name: 'new' | 'history' | 'task' | 'star' | 'upload' }>(), { name: 'task' })
const paths = {
  new: 'M12 5v14M5 12h14',
  history: 'M3 11a9 9 0 1 1 2.6 7M3 5v6h6M12 7v5l3 2',
  task: 'M9 3h6l1 2h4v16H4V5h4l1-2ZM8 11l2 2 5-5M8 17h8',
  star: 'm12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9L12 3Z',
  upload: 'M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5',
}
</script>
<template><svg class="app-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path :d="paths[name]" /></svg></template>
