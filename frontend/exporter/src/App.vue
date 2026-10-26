<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { createExport, downloadBundle, establishLocalSession, getExport, listExports, exportEvents, rerunExport, favorites, saveFavorite, deleteFavorite, type Favorite, type Ecosystem, type Environment, type ExportTask } from './api'
import StatusBadge from '../../shared/StatusBadge.vue'
import AppIcon from '../../shared/AppIcon.vue'
import TaskStages from '../../shared/TaskStages.vue'
import { date, errorText, terminal, warningText, type TaskEvent } from '../../shared/ui'

const page = ref<'new' | 'history' | 'detail' | 'favorites'>('new')
const ecosystem = ref<Ecosystem>('pypi')
const packages = ref(''), projectPath = ref('')
const reuse = ref(false), ignoreLock = ref(false), productionOnly = ref(true)
const targets = ref<Environment[]>([])
const blank = (): Environment => ({ environment_id: '', runtime: ecosystem.value === 'pypi' ? 'cpython' : 'node', version: '', platform: '', abi: '', libc: '' })
const custom = ref<Environment>(blank())
const favoriteName = ref(''), editingFavorite = ref(''), editingTarget = ref('')
const saved = ref<Favorite[]>([])
const busy = ref(false), booting = ref(true), error = ref(''), notice = ref('')
const task = ref<ExportTask | null>(null)
const history = ref<ExportTask[]>([]), total = ref(0), offset = ref(0), filter = ref('')
const events = ref<TaskEvent[]>([]), eventTotal = ref(0), logsOpen = ref(false)
const lookupId = ref('')
const drafts: Partial<Record<Ecosystem, { packages: string; projectPath: string; targets: Environment[]; reuse: boolean; ignoreLock: boolean; productionOnly: boolean; custom: Environment }>> = {}
let timer: number | undefined, disposed = false, refreshing = false, failures = 0, selection = 0, historySelection = 0
const isDone = computed(() => !!task.value && terminal(task.value.status))
const canDownload = computed(() => task.value?.status === 'COMPLETED')
const manifest = computed(() => task.value?.manifest)
const verification = computed(() => manifest.value?.verification as Record<string, string> | undefined)
const warnings = computed(() => Array.isArray(manifest.value?.warnings) ? manifest.value!.warnings : [])
const hasGaps = computed(() => verification.value && (verification.value.resolution_status !== 'COMPLETE' || verification.value.collection_status !== 'COMPLETE'))
const visibleFavorites = computed(() => saved.value.filter(x => ecosystem.value === 'npm' ? x.environment.runtime === 'node' : x.environment.runtime !== 'node'))
const resolutions = computed(() => (manifest.value?.resolutions || []) as Array<{ environment_id: string; status: string; nodes: string[]; selected_artifacts: string[] }>)
watch(ecosystem, (next, previous) => {
  drafts[previous] = { packages: packages.value, projectPath: projectPath.value, targets: targets.value, reuse: reuse.value, ignoreLock: ignoreLock.value, productionOnly: productionOnly.value, custom: custom.value }
  const d = drafts[next]
  packages.value = d?.packages || ''; projectPath.value = d?.projectPath || ''; targets.value = d?.targets || []
  reuse.value = d?.reuse || false; ignoreLock.value = d?.ignoreLock || false; productionOnly.value = d?.productionOnly ?? true
  custom.value = d?.custom || blank(); editingTarget.value = ''; editingFavorite.value = ''
})
function environment(): Environment | null {
  const value = { ...custom.value, environment_id: custom.value.environment_id.trim() || `target-${crypto.randomUUID().slice(0, 8)}`, runtime: custom.value.runtime.trim(), version: custom.value.version.trim(), platform: custom.value.platform.trim(), abi: custom.value.abi.trim(), libc: custom.value.libc?.trim() || undefined }
  if (!value.runtime || !value.version || !value.platform || !value.abi) { error.value = '请填写运行时、解释器版本、平台与 ABI；这些参数由你决定。'; return null }
  if (value.environment_id.length > 128) { error.value = '环境 ID 最多 128 个字符。'; return null }
  return value
}
function addTarget() {
  error.value = ''
  const value = environment(); if (!value) return
  if (targets.value.some(x => x.environment_id === value.environment_id && x.environment_id !== editingTarget.value)) { error.value = '环境 ID 已存在，请修改后再添加。'; return }
  const index = targets.value.findIndex(x => x.environment_id === editingTarget.value)
  if (index >= 0) targets.value.splice(index, 1, value); else targets.value.push(value)
  custom.value = blank(); editingTarget.value = ''
}
function editTarget(value: Environment) { custom.value = { ...value }; editingTarget.value = value.environment_id }
function applyFavorite(value: Favorite) {
  if (targets.value.some(x => x.environment_id === value.environment.environment_id)) { notice.value = '目标列表已包含该环境；可先移除再应用更新后的收藏。'; return }
  targets.value.push({ ...value.environment }); notice.value = `已应用 ${value.name}`
}
async function storeFavorite() {
  error.value = ''
  const value = environment(); if (!value) return
  if (!favoriteName.value.trim()) { error.value = '请填写收藏名称。'; return }
  busy.value = true
  try { await saveFavorite(editingFavorite.value || crypto.randomUUID(), favoriteName.value.trim(), value); saved.value = (await favorites()).items; notice.value = '目标环境已保存到本机。'; editingFavorite.value = ''; favoriteName.value = '' }
  catch(e) { error.value = errorText(e) } finally { busy.value = false }
}
async function removeFavorite(id: string) { try { await deleteFavorite(id); saved.value = saved.value.filter(x => x.id !== id) } catch(e) { error.value = errorText(e) } }
function loadFavorite(value: Favorite) { ecosystem.value = value.environment.runtime === 'node' ? 'npm' : 'pypi'; window.setTimeout(() => { custom.value = { ...value.environment }; editingFavorite.value = value.id; favoriteName.value = value.name; page.value = 'new' }, 0) }
async function selectProject() {
  try { const picker = window.pywebview?.api?.select_project; if (!picker) { notice.value = '浏览器模式请填写 Exporter 所在机器的项目路径。'; return }; const value = await picker(); if(value) projectPath.value = value }
  catch(e) { error.value = errorText(e) }
}
async function loadHistory() {
  const ticket = ++historySelection
  try { const result = await listExports(offset.value, filter.value); if(ticket === historySelection) { history.value = result.items; total.value = result.total } } catch(e) { error.value = errorText(e) }
}
async function showHistory() { page.value = 'history'; await loadHistory() }
async function loadLogs(more = false) {
  const id = task.value?.id; if(!id) return
  try { const data = await exportEvents(id, more ? events.value.length : 0); if(task.value?.id === id) { events.value = more ? [...events.value, ...data.items] : data.items; eventTotal.value = data.total } } catch(e) { error.value = errorText(e) }
}
function schedule() { if(timer) clearTimeout(timer); if(!disposed && task.value && !isDone.value) timer = window.setTimeout(refresh, Math.min(30000, 2000 * 2 ** failures)) }
async function refresh() {
  if(!task.value || refreshing) return
  const id = task.value.id, ticket = selection; refreshing = true
  try { const next = await getExport(id); if(ticket === selection && task.value?.id === id) { task.value = next; failures = 0; if(logsOpen.value) await loadLogs() } }
  catch(e) { failures = Math.min(failures + 1, 4); notice.value = '状态更新暂时失败，正在自动重试；已保留最后状态。' }
  finally { refreshing = false; schedule() }
}
async function openTask(id: string) {
  const ticket = ++selection
  if(timer) clearTimeout(timer)
  error.value = ''; logsOpen.value = false; events.value = []
  try { const value = await getExport(id); if(ticket !== selection || disposed) return; task.value = value; page.value = 'detail'; localStorage.setItem('pf_last_export_id', id); schedule() }
  catch(e) { error.value = errorText(e) }
}
async function start() {
  error.value = ''; notice.value = ''
  const constraints = packages.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean)
  if(!constraints.length && !projectPath.value.trim()) { error.value = '请填写包与版本约束，或选择项目文件。'; return }
  if(constraints.length && projectPath.value.trim()) { error.value = '包约束与项目路径只能选择一种输入方式。'; return }
  if(!targets.value.length) { error.value = '请至少添加一个完整目标环境。'; return }
  busy.value = true
  try { const result = await createExport({ ecosystem: ecosystem.value, constraints, environments: targets.value, ...(projectPath.value.trim() ? { project_path: projectPath.value.trim() } : {}), resolution_policy: reuse.value ? 'reuse' : 'fresh', lock_policy: ignoreLock.value ? 'ignore' : 'prefer', production_only: productionOnly.value }); await openTask(result.id) }
  catch(e) { error.value = errorText(e) } finally { busy.value = false }
}
async function rerun() { if(!task.value) return; busy.value = true; try { const result = await rerunExport(task.value.id); await openTask(result.id) } catch(e) { error.value = errorText(e) } finally { busy.value = false } }
async function saveBundle() { if(!task.value) return; try { if(window.pywebview?.api?.save_bundle) { const path = await window.pywebview.api.save_bundle(task.value.id); if(path) notice.value = `已保存至 ${path}` } else downloadBundle(task.value.id) } catch(e) { error.value = errorText(e) } }
onMounted(async() => { try { await establishLocalSession(); saved.value = (await favorites()).items; const id = localStorage.getItem('pf_last_export_id'); if(id) { await openTask(id); page.value = 'new' } } catch(e) { error.value = `本地工作区加载失败：${errorText(e)}` } finally { booting.value = false } })
onUnmounted(() => { disposed = true; selection++; if(timer) clearTimeout(timer) })
</script>

<template>
  <div class="shell">
    <aside class="sidebar" aria-label="主导航">
      <div class="brand"><span class="brand-mark">PF</span>PackFerry</div>
      <button :class="{active: page === 'new'}" @click="page = 'new'"><AppIcon name="new" /> 新建导出</button>
      <button :class="{active: page === 'history'}" @click="showHistory"><AppIcon name="history" /> 最近任务</button>
      <button v-if="task" :class="{active: page === 'detail'}" @click="page = 'detail'"><AppIcon name="task" /> 当前任务 <StatusBadge :status="task.status" /></button>
      <div class="spacer" />
      <button :class="{active: page === 'favorites'}" @click="page = 'favorites'"><AppIcon name="star" /> 环境收藏</button>
      <small>Exporter · 外网准备端<br>本机存储 · 离线交付</small>
    </aside>
    <main class="main" :aria-busy="booting">
      <header class="page-head"><div><h1>{{ page === 'new' ? '新建导出' : page === 'history' ? '最近任务' : page === 'favorites' ? '环境收藏' : '导出任务' }}</h1><p>解析依赖，收集原始制品，准备离线交付。</p></div><small>公网端</small></header>
      <div v-if="booting" class="busy-bar" role="status">正在读取本机工作区…</div>
      <div v-if="error" class="alert error" role="alert">{{ error }}</div><div v-if="notice" class="alert notice" role="status">{{ notice }}</div>
      <template v-if="page === 'new'">
        <div class="columns"><section class="panel">
          <div class="segmented" role="group" aria-label="生态"><button :aria-pressed="ecosystem === 'pypi'" :class="{active:ecosystem === 'pypi'}" @click="ecosystem = 'pypi'">Python / PyPI</button><button :aria-pressed="ecosystem === 'npm'" :class="{active:ecosystem === 'npm'}" @click="ecosystem = 'npm'">Node / npm</button></div>
          <label class="field">包与版本约束 <span>每行一个，或使用下方项目文件</span><textarea v-model="packages" rows="4" :placeholder="ecosystem === 'pypi' ? 'requests>=2.31\nnumpy==1.26.4' : '@scope/package@^2\nreact@18'" /></label>
          <label class="field">项目文件路径<div class="input-action"><input v-model="projectPath" :placeholder="ecosystem === 'pypi' ? 'requirements.txt / pyproject.toml / pylock.toml' : 'package.json / package-lock.json'" /><button @click="selectProject">选择</button></div></label>
          <h2>目标环境 <small>{{ targets.length }} 个</small></h2>
          <div v-if="!targets.length" class="alert notice">填写右侧参数添加目标，或应用已收藏的环境。</div>
          <div class="target-grid"><div v-for="target in targets" :key="target.environment_id" class="target"><span><strong>{{ target.runtime }} {{ target.version }}</strong><small>{{ target.platform }} · {{ target.abi }}{{ target.libc ? ` · ${target.libc}` : '' }}</small></span><button @click="editTarget(target)" :aria-label="`编辑 ${target.environment_id}`">编辑</button><button @click="targets = targets.filter(x => x.environment_id !== target.environment_id)" :aria-label="`移除 ${target.environment_id}`">×</button></div></div>
          <details class="options"><summary>解析选项</summary><label><input v-model="reuse" type="checkbox" /> 复用历史解析结果</label><label><input v-model="ignoreLock" type="checkbox" /> 忽略项目锁文件并重新求解</label><label v-if="ecosystem === 'npm'"><input v-model="productionOnly" type="checkbox" /> 仅生产依赖</label></details>
          <p class="muted">{{ ecosystem === 'pypi' ? 'Python / PyPI' : 'Node / npm' }} · {{ targets.length }} 个目标 · {{ reuse ? '复用历史解析' : '重新解析' }} · {{ ignoreLock ? '忽略锁文件' : '优先锁文件' }}</p>
          <button class="primary wide" :disabled="busy || booting" @click="start">{{ busy ? '正在提交…' : '开始解析并收集 →' }}</button>
        </section>
        <section class="panel"><h2>{{ editingTarget ? '编辑目标环境' : '添加目标环境' }}</h2><p class="muted">版本、ABI 与平台均由你选择，建议值可自由修改。</p>
          <div class="custom-grid">
            <label>运行时<input v-model="custom.runtime" list="runtimes" aria-label="运行时" /></label><label>{{ ecosystem === 'pypi' ? 'Python 解释器版本' : 'Node 版本' }}<input v-model="custom.version" placeholder="输入所需版本" aria-label="解释器版本" /></label>
            <label>平台 / 架构<input v-model="custom.platform" list="platforms" placeholder="例如 win_amd64" aria-label="平台架构" /></label><label>ABI<input v-model="custom.abi" placeholder="例如 cp313 / none" aria-label="ABI" /></label>
          </div>
          <datalist id="runtimes"><option value="cpython"/><option value="pypy"/><option value="node"/></datalist><datalist id="platforms"><option value="win_amd64"/><option value="manylinux_2_31_x86_64"/><option value="manylinux_2_28_aarch64"/><option value="linux-x64"/><option value="win32-x64"/></datalist>
          <details><summary>高级参数</summary><div class="custom-grid"><label>环境 ID（留空自动生成）<input v-model="custom.environment_id" aria-label="环境 ID" maxlength="128" /></label><label>libc（可选）<input v-model="custom.libc" placeholder="glibc / musl" aria-label="libc" /></label></div></details>
          <button :disabled="busy || booting" @click="addTarget">{{ editingTarget ? '更新目标' : '加入目标列表' }}</button>
          <details><summary>{{ editingFavorite ? '更新收藏' : '收藏此环境' }}</summary><label class="field">收藏名称<input v-model="favoriteName" maxlength="80" /></label><button :disabled="busy || booting" @click="storeFavorite">保存到本机</button></details>
          <div v-if="visibleFavorites.length"><h3>常用环境</h3><div v-for="item in visibleFavorites" :key="item.id" class="favorite-row"><button @click="applyFavorite(item)">{{ item.name }}<small>{{ item.environment.version }} · {{ item.environment.platform }} · {{ item.environment.abi }}</small></button></div></div>
        </section></div>
      </template>
      <section v-else-if="page === 'favorites'" class="panel"><h2>本机环境收藏</h2><p class="muted">收藏保存在 Exporter 数据目录中，重启后可继续使用。</p><div v-if="!saved.length" class="empty">暂无收藏。在新建导出中配置环境后保存。</div><div v-for="item in saved" :key="item.id" class="favorite-row"><button @click="loadFavorite(item)">{{ item.name }}<small>{{ item.environment.runtime }} {{ item.environment.version }} · {{ item.environment.platform }} · {{ item.environment.abi }}</small></button><button @click="loadFavorite(item)">编辑</button><button class="danger" @click="removeFavorite(item.id)">删除</button></div></section>
      <section v-else-if="page === 'history'" class="panel"><div class="toolbar"><label>状态 <select v-model="filter" @change="offset = 0; loadHistory()"><option value="">全部</option><option value="COMPLETED">已完成</option><option value="RUNNING">运行中</option><option value="FAILED">失败</option><option value="INTERRUPTED">已中断</option></select></label><button @click="loadHistory">刷新</button><small>共 {{ total }} 项</small></div><div v-if="!history.length" class="empty">暂无符合条件的任务</div><button v-for="item in history" :key="item.id" class="task-row" @click="openTask(item.id)"><span class="row-main"><strong>{{ item.request?.ecosystem }} · {{ item.request?.project_path || item.request?.constraints.join('、') || item.id }}</strong><small>{{ date(item.created_at) }} · {{ item.request?.environments.length }} 个目标 · {{ item.id.slice(0,8) }}</small></span><StatusBadge :status="item.status" /></button><div class="pager"><button :disabled="offset === 0" @click="offset -= 25; loadHistory()">上一页</button><button :disabled="offset + 25 >= total" @click="offset += 25; loadHistory()">下一页</button></div><div class="lookup"><input v-model="lookupId" placeholder="按任务 ID 查询" aria-label="任务 ID" /><button :disabled="!lookupId.trim()" @click="openTask(lookupId.trim())">查询</button></div></section>
      <section v-else-if="task" class="panel"><div class="task-head"><div><h2>{{ task.request?.ecosystem === 'npm' ? 'Node / npm' : 'Python / PyPI' }} · {{ task.request?.environments.length || '—' }} 个目标</h2><code>{{ task.id }}</code><small v-if="task.rerun_of"> · 重新运行自 {{ task.rerun_of }}</small></div><StatusBadge :status="task.status" /></div>
        <TaskStages :progress="task.progress" :status="task.status" kind="export" />
        <div v-if="task.error" class="alert error">{{ task.error.code }}：导出未完成。请核对依赖、目标环境与工具配置，再按原配置重新运行。</div>
        <div v-if="verification" class="verification-list"><div><span>依赖解析完整性</span><StatusBadge :status="verification.resolution_status" /></div><div><span>制品收集完整性</span><StatusBadge :status="verification.collection_status" /></div><div><span>离线安装验证</span><StatusBadge :status="verification.offline_install_status" /></div></div>
        <div v-if="warnings.length" class="risk-list"><strong>风险与缺口</strong><p v-for="(warning, i) in warnings" :key="i">{{ warningText(warning) }}</p></div>
        <details v-if="resolutions.length"><summary>各目标环境的依赖和制品</summary><section v-for="resolution in resolutions" :key="resolution.environment_id"><h3>{{ resolution.environment_id }} <StatusBadge :status="resolution.status" /></h3><p class="muted">依赖：{{ resolution.nodes?.join('、') || '无' }}</p><p class="muted">制品：{{ resolution.selected_artifacts?.join('、') || '无' }}</p></section></details>
        <details v-if="manifest"><summary>完整 Manifest</summary><pre>{{ JSON.stringify(manifest, null, 2) }}</pre></details>
        <details @toggle="logsOpen = ($event.target as HTMLDetailsElement).open; logsOpen && loadLogs()"><summary>风险与详细任务日志</summary><p class="muted">仅显示已记录的脱敏任务事件；不包含未采集的子进程输出。</p><div v-for="event in events" :key="event.id" class="event-row"><time>{{ date(event.created_at) }}</time><span>{{ event.operation }}</span></div><p v-if="!events.length" class="muted">此任务暂无事件记录。</p><button v-if="events.length < eventTotal" @click="loadLogs(true)">加载更多</button></details>
        <div class="actions"><button v-if="canDownload" class="primary" @click="saveBundle">{{ hasGaps ? '保存带缺口 Bundle' : '保存 Bundle ZIP' }}</button><button v-if="isDone" :disabled="busy" @click="rerun">按原配置重新运行</button><button v-if="!isDone" @click="refresh">刷新状态</button></div><p v-if="hasGaps" class="alert warning">Bundle 存在解析或制品缺口，保存前请核对风险；不能视为完整离线包。</p>
      </section>
      <footer class="footer">PackFerry · 原始制品以 SHA-256 校验 · 离线可安装性以实际验证为准</footer>
    </main>
  </div>
</template>
