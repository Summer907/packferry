<script setup lang="ts">
import { computed } from 'vue'
import { statusText, type Progress } from './ui'
const props = defineProps<{ progress?: Progress | null; status: string; kind: 'export' | 'import' }>()
const stages = computed(() => props.kind === 'export'
  ? [['queued', '等待执行'], ['preparing', '准备输入'], ['resolving', '依赖解析与制品收集'], ['checking', '完整性汇总'], ['bundling', '生成 Bundle ZIP']]
  : [['confirmation', '预检与用户确认'], ['queued', '等待执行'], ['publishing', '发布与逐文件核验']])
const failed = computed(() => ['FAILED','PARTIAL','INTERRUPTED','NEEDS_AUTH','EXPIRED'].includes(props.status.toUpperCase()))
const index = computed(() => stages.value.findIndex(s => s[0] === props.progress?.stage))
const countable = computed(() => props.progress?.total != null && props.progress.total > 0 && props.progress.completed != null)
</script>
<template>
  <section class="stages" :data-status="status" aria-label="任务阶段" aria-live="polite">
    <p v-if="!progress" class="muted">此任务没有阶段记录，当前状态：{{ statusText(status) }}</p>
    <template v-else>
      <div v-for="(stage, i) in stages" :key="stage[0]" class="stage" :class="{ active: i === index, finished: !failed && (i < index || status === 'COMPLETED') }">
        <span class="stage-dot" aria-hidden="true">{{ !failed && (i < index || status === 'COMPLETED') ? '✓' : i + 1 }}</span>
        <div><strong>{{ stage[1] }}</strong><small v-if="i === index">{{ progress.operation }}</small></div>
      </div>
      <p v-if="failed" class="alert warning">{{ statusText(status) }}：{{ progress.operation }}</p>
      <div v-if="countable" class="actual-progress"><progress :value="progress.completed!" :max="progress.total!" aria-label="文件处理进度" /><small>已处理 {{ progress.completed }} / {{ progress.total }} 个文件（处理完成不代表全部成功）</small></div>
      <p v-else-if="status === 'RUNNING'" class="muted"><span class="spinner" /> 正在执行，当前阶段暂无可靠总数。</p>
    </template>
  </section>
</template>
