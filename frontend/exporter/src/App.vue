<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { createExport, downloadBundle, establishLocalSession, getExport, type Ecosystem, type Environment, type ExportTask } from './api'

const ecosystem = ref<Ecosystem>('pypi')
const packages = ref('')
const projectPath = ref('')
const reuse = ref(false)
const ignoreLock = ref(false)
const productionOnly = ref(true)
const selected = ref<string[]>(['py39-win', 'py39-linux', 'py312-win', 'py312-linux'])
const customTargets = ref<Environment[]>([])
const custom = ref<Environment>({ environment_id: '', runtime: 'cpython', version: '', platform: '', abi: '', libc: '' })
const busy = ref(false)
const booting = ref(true)
const error = ref('')
const notice = ref('')
const task = ref<ExportTask | null>(null)
const lookupId = ref('')
let poller: number | undefined

const pythonTargets: Environment[] = [
  { environment_id: 'py39-win', runtime: 'cpython', version: '3.9', platform: 'win_amd64', abi: 'cp39' },
  { environment_id: 'py39-linux', runtime: 'cpython', version: '3.9', platform: 'manylinux_2_31_x86_64', abi: 'cp39', libc: 'glibc' },
  { environment_id: 'py312-win', runtime: 'cpython', version: '3.12', platform: 'win_amd64', abi: 'cp312' },
  { environment_id: 'py312-linux', runtime: 'cpython', version: '3.12', platform: 'manylinux_2_31_x86_64', abi: 'cp312', libc: 'glibc' },
]
const npmTargets: Environment[] = [
  { environment_id: 'node-win', runtime: 'node', version: '26', platform: 'win32-x64', abi: 'none' },
  { environment_id: 'node-linux', runtime: 'node', version: '26', platform: 'linux-x64', abi: 'none', libc: 'glibc' },
]
const targets = computed(() => [...(ecosystem.value === 'pypi' ? pythonTargets : npmTargets), ...customTargets.value.filter(item => item.runtime === (ecosystem.value === 'pypi' ? 'cpython' : 'node'))])
const isDone = computed(() => !!task.value && ['completed', 'complete', 'succeeded', 'partial', 'failed', 'error', 'interrupted'].includes(task.value.status.toLowerCase()))
const canDownload = computed(() => !!task.value && ['completed', 'complete', 'succeeded', 'partial'].includes(task.value.status.toLowerCase()))
const manifest = computed(() => task.value?.manifest || null)
const verification = computed(() => manifest.value?.verification as Record<string, string> | undefined)
const warnings = computed(() => Array.isArray(manifest.value?.warnings) ? manifest.value.warnings as Array<Record<string, unknown>> : [])
const hasGaps = computed(() => !!verification.value && (verification.value.resolution_status !== 'COMPLETE' || verification.value.collection_status !== 'COMPLETE'))
const manifestStats = computed(() => {
  if (!manifest.value) return []
  const m = manifest.value
  const files = Array.isArray(m.artifacts) ? m.artifacts.length : Array.isArray(m.files) ? m.files.length : null
  const environments = Array.isArray(m.environments) ? m.environments.length : null
  const risks = Array.isArray(m.warnings) ? m.warnings.length : Array.isArray(m.risks) ? m.risks.length : null
  return [
    { label: '原始制品', value: files ?? '—' },
    { label: '目标环境', value: environments ?? '—' },
    { label: '风险提示', value: risks ?? '—' },
  ]
})

function resetTargets() {
  selected.value = targets.value.map(item => item.environment_id)
}

function addTarget() {
  error.value = ''
  const value = { ...custom.value, environment_id: custom.value.environment_id.trim(), version: custom.value.version.trim(), platform: custom.value.platform.trim(), abi: custom.value.abi.trim(), libc: custom.value.libc?.trim() || undefined }
  if (!value.environment_id || !value.version || !value.platform || !value.abi) { error.value = '自定义环境需要填写 ID、版本、平台和 ABI。'; return }
  if (targets.value.some(item => item.environment_id === value.environment_id)) { error.value = '环境 ID 已存在。'; return }
  customTargets.value.push(value)
  selected.value.push(value.environment_id)
  custom.value = { environment_id: '', runtime: ecosystem.value === 'pypi' ? 'cpython' : 'node', version: '', platform: '', abi: '', libc: '' }
}

async function selectProject() {
  const picker = window.pywebview?.api?.select_project
  if (!picker) {
    notice.value = '浏览器模式请填写本机项目路径；路径由运行 Exporter 的机器读取。'
    return
  }
  try {
    const value = await picker()
    if (value) projectPath.value = value
  } catch (e) {
    error.value = String(e)
  }
}

async function start() {
  error.value = ''
  notice.value = ''
  if (poller) window.clearInterval(poller)
  const constraints = packages.value.split(/\r?\n/).map(item => item.trim()).filter(Boolean)
  if (!constraints.length && !projectPath.value.trim()) {
    error.value = '请填写包与版本约束，或选择项目文件。'
    return
  }
  if (constraints.length && projectPath.value.trim()) { error.value = '包约束与项目路径只能选一种输入方式。'; return }
  const environments = targets.value.filter(item => selected.value.includes(item.environment_id))
  if (!environments.length) {
    error.value = '请至少选择一个目标环境。'
    return
  }
  busy.value = true
  try {
    task.value = await createExport({
      ecosystem: ecosystem.value,
      constraints,
      environments,
      ...(projectPath.value.trim() ? { project_path: projectPath.value.trim() } : {}),
      resolution_policy: reuse.value ? 'reuse' : 'fresh',
      lock_policy: ignoreLock.value ? 'ignore' : 'prefer',
      production_only: productionOnly.value,
    })
    localStorage.setItem('pf_last_export_id', task.value.id)
    await refresh()
    poller = window.setInterval(refresh, 2000)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    busy.value = false
  }
}

async function refresh() {
  if (!task.value) return
  try {
    task.value = await getExport(task.value.id)
    if (task.value.error) error.value = task.value.error.message
    if (isDone.value && poller) window.clearInterval(poller)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    if (poller) window.clearInterval(poller)
  }
}

async function restoreTask() {
  if (!lookupId.value.trim()) return
  error.value = ''
  if (poller) window.clearInterval(poller)
  try {
    task.value = await getExport(lookupId.value.trim())
    if (!isDone.value) poller = window.setInterval(refresh, 2000)
  } catch (e) { error.value = e instanceof Error ? e.message : String(e) }
}

async function saveBundle() {
  if (!task.value) return
  try {
    if (window.pywebview?.api?.save_bundle) {
      const path = await window.pywebview.api.save_bundle(task.value.id)
      if (path) notice.value = `已保存至 ${path}`
    } else downloadBundle(task.value.id)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(async () => {
  try { await establishLocalSession() }
  catch (e) { error.value = `本地会话建立失败：${e instanceof Error ? e.message : String(e)}` }
  finally { booting.value = false }
  lookupId.value = localStorage.getItem('pf_last_export_id') || ''
  if (!error.value && lookupId.value) await restoreTask()
})
onUnmounted(() => { if (poller) window.clearInterval(poller) })
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand"><span class="brand-mark">PF</span><span>PackFerry</span><small>外网准备端</small></div>
      <span class="top-note">解析 · 收集 · 离线交付</span>
    </header>
    <main>
      <section class="hero">
        <div><p class="eyebrow">EXPORT WORKSPACE</p><h1>准备依赖包，<br><em>安全送往内网。</em></h1><p class="lead">选择依赖和目标环境，生成包含原始制品、清单与风险报告的 Bundle。</p></div>
        <div class="hero-graphic" aria-hidden="true"><div class="graphic-node">外网<br><strong>01</strong></div><div class="graphic-path"><span></span><span></span><span></span></div><div class="graphic-node">内网<br><strong>02</strong></div></div>
      </section>
      <div v-if="error" class="alert error" role="alert">{{ error }}</div>
      <div v-if="notice" class="alert notice" role="status">{{ notice }}</div>
      <div class="columns">
        <section class="panel setup">
          <div class="section-heading"><span class="step">01</span><div><h2>定义输入</h2><p>单包、多包或项目文件</p></div></div>
          <div class="segmented" role="group" aria-label="生态"><button :class="{active: ecosystem === 'pypi'}" @click="ecosystem = 'pypi'; resetTargets()">Python / PyPI</button><button :class="{active: ecosystem === 'npm'}" @click="ecosystem = 'npm'; resetTargets()">Node / npm</button></div>
          <label class="field">包与版本约束 <span>每行一个</span><textarea v-model="packages" rows="5" :placeholder="ecosystem === 'pypi' ? 'requests>=2.31\nnumpy==1.26.4' : '@scope/package@^2\nreact@18'" /></label>
          <label class="field">项目文件路径 <span>可选</span><div class="input-action"><input v-model="projectPath" :placeholder="ecosystem === 'pypi' ? 'requirements.txt / pyproject.toml / pylock.toml' : 'package.json / package-lock.json'" /><button class="subtle" type="button" @click="selectProject">选择</button></div></label>
          <div class="section-heading subheading"><span class="step">02</span><div><h2>目标环境</h2><p>同一 Bundle 可覆盖多个目标</p></div></div>
          <div class="target-grid"><label v-for="target in targets" :key="target.environment_id" class="target"><input v-model="selected" type="checkbox" :value="target.environment_id" /><span>{{ target.version }} <strong>{{ target.platform.includes('win') ? 'Windows' : 'Linux' }}</strong><small>{{ target.abi }} · {{ target.platform }}</small></span></label></div>
          <details class="custom-target"><summary>添加自定义目标环境</summary><div class="custom-grid"><input v-model="custom.environment_id" placeholder="环境 ID" aria-label="自定义环境 ID" /><select v-model="custom.runtime" aria-label="运行时"><option value="cpython">CPython</option><option value="node">Node</option></select><input v-model="custom.version" placeholder="版本（例 3.13）" aria-label="自定义版本" /><input v-model="custom.platform" placeholder="平台标签" aria-label="自定义平台" /><input v-model="custom.abi" placeholder="ABI（例 cp313）" aria-label="自定义 ABI" /><input v-model="custom.libc" placeholder="libc（可选）" aria-label="自定义 libc" /></div><button class="subtle" type="button" @click="addTarget">加入目标列表</button></details>
          <details class="options"><summary>解析选项</summary><label><input v-model="reuse" type="checkbox" /> 复用历史解析结果（普通包默认重新解析）</label><label><input v-model="ignoreLock" type="checkbox" /> 忽略项目锁文件并重新求解</label><label><input v-model="productionOnly" type="checkbox" /> 仅生产依赖</label></details>
          <button class="primary" :disabled="busy || booting" @click="start">{{ busy ? '正在提交…' : '开始解析并收集' }} <span>→</span></button>
        </section>
        <section class="panel progress">
          <div class="section-heading"><span class="step">03</span><div><h2>任务与交付</h2><p>状态自动更新，每 2 秒检查</p></div></div>
          <div v-if="!task" class="empty"><div class="empty-icon">◎</div><h3>等待开始</h3><p>提交依赖后，这里显示解析结果、收集状态与 Bundle 下载。</p></div>
          <template v-else>
            <div class="task-head"><div><span class="mini-label">任务 ID</span><code>{{ task.id }}</code></div><span class="badge" :class="{done: isDone}">{{ task.status }}</span></div>
            <div class="progress-line"><i :class="{complete: isDone}" /></div>
            <p class="status-copy">{{ isDone ? '任务已结束，请检查清单和风险后导出。' : '正在处理；关闭浏览器后可通过任务 ID 查询。' }}</p>
            <div v-if="manifest" class="stats"><div v-for="stat in manifestStats" :key="stat.label"><strong>{{ stat.value }}</strong><span>{{ stat.label }}</span></div></div>
            <div v-if="verification" class="verification" :class="{gaps: hasGaps}"><strong>{{ hasGaps ? '有缺口，不能视为完整离线包' : '解析与收集完成' }}</strong><span>解析 {{ verification.resolution_status }} · 收集 {{ verification.collection_status }} · 离线安装 {{ verification.offline_install_status }}</span></div>
            <div v-if="warnings.length" class="risk-list"><strong>风险与缺口</strong><p v-for="(warning, index) in warnings" :key="index">{{ warning.message || warning.code }}</p></div>
            <details v-if="Array.isArray(manifest?.resolutions)" class="dependency-list"><summary>查看各环境依赖和制品</summary><div v-for="resolution in manifest.resolutions as Record<string, unknown>[]" :key="String(resolution.environment_id)"><h4>{{ resolution.environment_id }} · {{ resolution.status }}</h4><ul><li v-for="nodeId in resolution.nodes as string[]" :key="nodeId"><strong>{{ nodeId }}</strong><small>{{ ((manifest.packages as Record<string, unknown>[]) || []).find(item => item.node_id === nodeId)?.dependencies }}</small></li></ul><p>制品：{{ (resolution.selected_artifacts as string[] || []).join('、') || '无' }}</p></div></details>
            <details v-if="manifest" class="manifest"><summary>查看完整 Manifest</summary><pre>{{ JSON.stringify(manifest, null, 2) }}</pre></details>
            <div v-if="canDownload" class="delivery"><div><strong>{{ hasGaps ? '可导出带缺口 Bundle' : 'Bundle 已可导出' }}</strong><p>{{ hasGaps ? '请核对缺口报告后再交付。' : '人工按现有合规流程送入内网。' }}</p></div><button class="primary" @click="saveBundle">保存 ZIP ↓</button></div>
            <button v-if="!isDone" class="subtle refresh" @click="refresh">立即刷新</button>
          </template>
          <div class="lookup"><input v-model="lookupId" placeholder="输入任务 ID 查找已有任务" aria-label="任务 ID" /><button class="subtle" @click="restoreTask">查询</button></div>
        </section>
      </div>
    </main>
    <footer>PackFerry · 原始制品与清单以 SHA-256 校验 · 离线可安装性以实际验证为准</footer>
  </div>
</template>

<style scoped>
.lookup { display: flex; gap: 8px; border-top: 1px solid #edf1f4; padding-top: 18px; margin-top: 20px; }
.lookup input { flex: 1; min-width: 0; border: 1px solid #dce4ed; border-radius: 8px; padding: 10px; color: #27384b; }
.verification { padding: 13px; background: #e8f6ef; color: #287954; border-radius: 8px; margin: 20px 0; font-size: 12px; }
.verification.gaps { background: #fff5e5; color: #986622; }
.verification strong, .verification span { display: block; }
.verification span { margin-top: 5px; }
.risk-list { border-left: 3px solid #e9aa40; padding: 10px 14px; background: #fffaf0; color: #866329; font-size: 12px; margin-bottom: 19px; }
.risk-list p { margin: 7px 0 0; line-height: 1.5; }
.custom-target { margin-top: 13px; color: #51677d; font-size: 12px; }
.custom-target summary, .dependency-list summary { cursor: pointer; font-weight: 700; }
.custom-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 12px 0; }
.custom-grid input, .custom-grid select { min-width: 0; border: 1px solid #dce4ed; border-radius: 7px; padding: 9px; }
.dependency-list { margin: 18px 0; font-size: 12px; color: #53677c; border-top: 1px solid #e8edf1; padding-top: 15px; }
.dependency-list h4 { margin: 14px 0 8px; }
.dependency-list ul { padding-left: 18px; }
.dependency-list li { margin: 4px 0; }
.dependency-list small { display: block; color: #8a98a6; }
</style>
