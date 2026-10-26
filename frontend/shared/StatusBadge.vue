<script setup lang="ts">
import { statusText } from './ui'
defineProps<{ status: string }>()
</script>
<template><span class="badge" :data-status="status.toUpperCase()" :title="status">{{ statusText(status) }}</span></template>
