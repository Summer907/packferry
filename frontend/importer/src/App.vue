<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { authorize, clearLocalSession, confirm, files, HttpError, login, logout, me, operation, preview, report, repositories, task, tasks, upload, taskEvents, type FileRecord, type Preview, type Repository, type Task } from './api'

import StatusBadge from '../../shared/StatusBadge.vue'
import AppIcon from '../../shared/AppIcon.vue'
import TaskStages from '../../shared/TaskStages.vue'
import { statusText, terminal, warningText, errorText, type TaskEvent } from '../../shared/ui'
const page = ref<'new' | 'history' | 'detail'>('new')
const offset = ref(0), total = ref(0), filter = ref(''), fileOffset = ref(0), fileTotal = ref(0), fileFilter = ref('')
const events = ref<TaskEvent[]>([]), eventTotal = ref(0), logsOpen = ref(false)
const transfer = ref<number | null>(null), uploadState = ref('')
let refreshing = false, selection = 0, disposed = false, failures = 0
const username = ref('')
const password = ref('')
const actor = ref('')
const loggingIn = ref(false)
const loading = ref(true)
const working = ref(false)
const error = ref('')
const notice = ref('')
const selectedFile = ref<File | null>(null)
const selectedRepository = ref('')
const repositoryList = ref<Repository[]>([])
const taskList = ref<Task[]>([])
const current = ref<Task | null>(null)
const currentPreview = ref<Preview | null>(null)
const currentFiles = ref<FileRecord[]>([])
const currentReport = ref<unknown>(null)
const authPassword = ref('')
const showAuth = ref(false)
const pendingOperation = ref<{ key: string; kind: 'upload' | 'confirm'; taskId?: string } | null>(null)
let poller: number | undefined

const taskDone = computed(() => !!current.value && terminal(current.value.status))
const needsAuth = computed(() => !!current.value && ['needs_auth', 'needs-auth', 'waiting_auth'].includes(current.value.status.toLowerCase()))
const readyToConfirm = computed(() => !!current.value && ['ready', 'preview', 'validated', 'pending_confirmation'].includes(current.value.status.toLowerCase()))
const canSubmit = computed(() => !!selectedFile.value && !!selectedRepository.value && !working.value && !pendingOperation.value)

function message(e: unknown): string { return errorText(e) }
function date(value?: string): string { return value ? new Date(value).toLocaleString('zh-CN') : '—' }
function clearFeedback() { error.value = ''; notice.value = '' }
function rememberPending(value: typeof pendingOperation.value) {
  pendingOperation.value = value
  if (value) sessionStorage.setItem(`pf_pending_operation:${actor.value}`, JSON.stringify(value))
  else sessionStorage.removeItem(`pf_pending_operation:${actor.value}`)
}
function restorePending() {
  try {
    pendingOperation.value = JSON.parse(sessionStorage.getItem(`pf_pending_operation:${actor.value}`) || sessionStorage.getItem('pf_pending_operation') || 'null')
    if(pendingOperation.value) rememberPending(pendingOperation.value)
    sessionStorage.removeItem('pf_pending_operation')
  } catch { rememberPending(null) }
}
async function checkPendingOperation() {
  if (!pendingOperation.value) return
  clearFeedback()
  const pending = pendingOperation.value
  try {
    const result = await operation(pending.key)
    await selectTask(result.task.id)
    rememberPending(null)
    notice.value = '已核实操作结果。'
  } catch (e) {
    if (e instanceof HttpError && e.status === 404) notice.value = '操作结果尚未生成，请稍后再次核实；不要重复提交。'
    else handleFailure(e)
  }
}
function handleFailure(e: unknown) {
  if (e instanceof HttpError && e.status === 401) {
    clearLocalSession()
    actor.value = ''; page.value = 'new'; pendingOperation.value = null
    current.value = null
    currentPreview.value = null; currentFiles.value = []; currentReport.value = null; events.value = []; selection++
    taskList.value = []
    error.value = '会话已失效，请重新登录。'
  } else error.value = message(e)
}

async function signIn() {
  clearFeedback()
  loggingIn.value = true
  try {
    actor.value = await login(username.value.trim(), password.value)
    restorePending(); page.value = 'new'
    password.value = ''
    await refreshAll()
  } catch (e) { handleFailure(e) }
  finally { loggingIn.value = false }
}

async function signOut() {
  clearFeedback()
  try { await logout() } catch (e) { error.value = message(e) }
  actor.value = ''; page.value = 'new'; pendingOperation.value = null
  current.value = null
  taskList.value = []
  currentPreview.value = null; currentFiles.value = []; currentReport.value = null; events.value = []; selection++; selectedFile.value = null; selectedRepository.value = ''; authPassword.value = ''
}

async function refreshAll() {
  const [repos, list] = await Promise.all([repositories(), tasks(offset.value, filter.value)])
  repositoryList.value = repos
  taskList.value = list.items; total.value = list.total
  if (!selectedRepository.value && repos.length) selectedRepository.value = repos[0].name
}

async function fetchHistory() {
  const owner = actor.value, position = offset.value, status = filter.value
  const list = await tasks(position, status)
  if(owner === actor.value && position === offset.value && status === filter.value && !disposed) { taskList.value = list.items; total.value = list.total }
}
async function loadHistory() {
  try { await fetchHistory() } catch(e) { handleFailure(e) }
}
async function loadLogs(more = false) {
  const id = current.value?.id; if(!id) return
  try { const result = await taskEvents(id, more ? events.value.length : 0); if(current.value?.id === id) { events.value = more ? [...events.value, ...result.items] : result.items; eventTotal.value = result.total } } catch(e) { handleFailure(e) }
}
async function loadFiles() {
  const id = current.value?.id; if(!id) return
  const position = fileOffset.value, status = fileFilter.value
  try { const result = await files(id, position, status); if(current.value?.id === id && position === fileOffset.value && status === fileFilter.value) { currentFiles.value = result.items; fileTotal.value = result.total } } catch(e) { handleFailure(e) }
}
async function selectTask(id: string) {
  clearFeedback(); page.value = 'detail'; fileOffset.value = 0; fileFilter.value = ''; logsOpen.value = false
  currentPreview.value = null; currentFiles.value = []; currentReport.value = null; events.value = []
  try { await refreshTask(id) } catch(e) { handleFailure(e) }
}
async function refreshTask(id?: string) {
  const targetId = id || current.value?.id; if(!targetId) return
  const ticket = id ? ++selection : selection
  const position = fileOffset.value, status = fileFilter.value
  const next = await task(targetId)
  if(ticket !== selection || disposed || !actor.value) return
  current.value = next
  sessionStorage.setItem(`pf_import_task:${actor.value}`, targetId)
  const [p, f] = await Promise.allSettled([preview(targetId), files(targetId, position, status)])
  if(ticket !== selection || current.value?.id !== targetId || disposed || !actor.value) return
  currentPreview.value = p.status === 'fulfilled' ? p.value : null
  if(f.status === 'fulfilled' && position === fileOffset.value && status === fileFilter.value) { currentFiles.value = f.value.items; fileTotal.value = f.value.total }
  for(const result of [p, f]) if(result.status === 'rejected' && result.reason instanceof HttpError && result.reason.status === 401) { handleFailure(result.reason); return }
  if(logsOpen.value) await loadLogs()
}
function chooseFile(file?: File) {
  if(!file) return
  if(!file.name.toLowerCase().endsWith('.zip')) { error.value = '请选择 Bundle ZIP 文件。'; return }
  selectedFile.value = file; error.value = ''
}
function drop(event: DragEvent) { if(!working.value) chooseFile(event.dataTransfer?.files?.[0]) }
async function showReport() {
  const id = current.value?.id; if(!id) return
  try { const result = await report(id); if(current.value?.id === id) currentReport.value = result } catch(e) { handleFailure(e) }
}

async function submit() {
  if (!selectedFile.value || !selectedRepository.value) return
  clearFeedback()
  working.value = true
  const key = crypto.randomUUID()
  rememberPending({ key, kind: 'upload' }); transfer.value = null; uploadState.value = '正在上传'
  try {
    const result = await upload(selectedFile.value, selectedRepository.value, key, value => { transfer.value = value; uploadState.value = value === 100 ? '服务端预检中' : '正在上传' })
    rememberPending(null)
    await selectTask(result.id)
    notice.value = 'Bundle 已接收。请检查预检结果后确认导入。'
  } catch (e) {
    if (e instanceof HttpError && e.status < 500) { rememberPending(null); handleFailure(e) }
    else {
      rememberPending({ key, kind: 'upload' })
      try {
        const result = await operation(key)
        const id = result.task?.id
        if (id) { await refreshTask(id); rememberPending(null); notice.value = '网络中断后已核实上传结果。' }
        else error.value = '上传结果尚未确认。请稍后通过任务列表或操作键查询，勿重复提交。'
      } catch { error.value = '上传结果未知。请稍后查询任务列表，勿重复提交。' }
    }
  } finally { working.value = false; uploadState.value = '' }
}

async function approve() {
  if (!current.value || pendingOperation.value) return
  clearFeedback()
  working.value = true
  const id = current.value.id
  const key = crypto.randomUUID()
  rememberPending({ key, kind: 'confirm', taskId: id })
  try { await confirm(id, key); rememberPending(null); await refreshTask(id); notice.value = '已确认，任务正在后台执行。' }
  catch (e) {
    if (e instanceof HttpError && e.status < 500) { rememberPending(null); handleFailure(e) }
    else {
      rememberPending({ key, kind: 'confirm', taskId: id })
      try { await operation(key); await refreshTask(id); rememberPending(null); notice.value = '已核实确认操作。' }
      catch { error.value = '确认结果未知，请先刷新任务状态，勿重复确认。' }
    }
  } finally { working.value = false }
}

async function reauthorize() {
  if (!current.value || !authPassword.value) return
  clearFeedback()
  working.value = true
  try { await authorize(current.value.id, authPassword.value); authPassword.value = ''; showAuth.value = false; await refreshTask(); notice.value = '已重新授权。' }
  catch (e) { handleFailure(e) }
  finally { working.value = false }
}

function schedule() {
  if(poller) clearTimeout(poller)
  if(!disposed) poller = window.setTimeout(poll, Math.min(30000, 2000 * 2 ** failures))
}
async function poll() {
  if(refreshing || disposed) return
  refreshing = true
  try {
    if(actor.value) {
      if(current.value && !taskDone.value && current.value.status !== 'READY' && current.value.status !== 'NEEDS_AUTH') await refreshTask()
      else await fetchHistory()
      failures = 0
    }
  } catch(e) { failures = Math.min(failures + 1, 4); if(e instanceof HttpError && e.status === 401) handleFailure(e); else notice.value = '状态更新失败，保留最后状态并自动重试。' }
  finally { refreshing = false; schedule() }
}
onMounted(async () => {
  try { actor.value = await me(); restorePending(); await refreshAll(); const id = sessionStorage.getItem(`pf_import_task:${actor.value}`); if(id) await refreshTask(id) }
  catch(e) { if(e instanceof HttpError && e.status === 401) actor.value = ''; else error.value = '工作区加载失败，请刷新页面重试。' }
  finally { loading.value = false; schedule() }
})
onUnmounted(() => { disposed = true; selection++; if(poller) clearTimeout(poller) })
</script>

<template>
  <div class="shell">
    <aside class="sidebar" aria-label="主导航"><div class="brand"><span class="brand-mark">PF</span>PackFerry</div><template v-if="actor"><button :class="{active:page === 'new'}" @click="page = 'new'"><AppIcon name="new" /> 新建导入</button><button :class="{active:page === 'history'}" @click="page = 'history'; loadHistory()"><AppIcon name="history" /> 最近任务</button><button v-if="current" :class="{active:page === 'detail'}" @click="page = 'detail'"><AppIcon name="task" /> 当前任务 <StatusBadge :status="current.status" /></button></template><div class="spacer"/><template v-if="actor"><small>{{ actor }}</small><button :disabled="working" @click="signOut">退出登录</button></template><small>Importer · 内网导入端<br>Nexus Hosted 仓库</small></aside>
    <main class="main">
      <header class="page-head"><div><h1>{{ !actor ? 'Bundle 导入' : page === 'new' ? '新建导入' : page === 'history' ? '最近任务' : '导入任务' }}</h1><p>核对交付内容，发布并核验每个原始制品。</p></div><small>内网端</small></header>
      <div v-if="loading" class="busy-bar" role="status">正在读取会话…</div>
      <div v-if="error" class="alert error" role="alert">{{ error }}</div><div v-if="notice" class="alert notice" role="status">{{ notice }}</div>
      <div v-if="!loading && !actor" class="login-layout"><form class="panel login-card" @submit.prevent="signIn"><h2>登录内网中心</h2><p class="muted">使用 Nexus 账号登录，操作关联你的身份。</p><label>用户名<input v-model="username" autocomplete="username" required /></label><label>密码<input v-model="password" type="password" autocomplete="current-password" required /></label><button class="primary" :disabled="loggingIn">{{ loggingIn ? '正在验证…' : '使用 Nexus 账号登录' }}</button></form></div>
      <template v-if="actor && !loading">
        <div v-if="pendingOperation" class="alert warning" role="status">上次{{ pendingOperation.kind === 'upload' ? '上传' : '确认' }}结果待核实，请勿重复提交。 <button @click="checkPendingOperation">查询操作结果</button></div>
        <div v-if="page === 'new'" class="columns"><section class="panel"><h2>提交 Bundle</h2><p class="muted">选择外网准备端生成的 ZIP，并指定目标 Hosted 仓库。</p><label class="dropzone" @dragover.prevent @drop.prevent="drop"><input type="file" accept=".zip,application/zip" :disabled="working" @change="chooseFile(($event.target as HTMLInputElement).files?.[0])" /><AppIcon name="upload" /><strong>{{ selectedFile?.name || '拖放 Bundle ZIP，或点击选择' }}</strong><small>{{ selectedFile ? `${(selectedFile.size / 1024 / 1024).toFixed(1)} MB` : '文件将经过服务端完整性预检' }}</small></label><label class="field">目标 Hosted 仓库<select v-model="selectedRepository" :disabled="working"><option disabled value="">请选择仓库</option><option v-for="repo in repositoryList" :key="repo.name" :value="repo.name">{{ repo.name }} · {{ repo.ecosystem }}</option></select></label><div v-if="uploadState" class="busy-bar" role="status"><span class="spinner" />{{ uploadState }}<progress v-if="transfer !== null && transfer < 100" :value="transfer" max="100" aria-label="上传进度" /><small v-if="transfer !== null && transfer < 100">{{ transfer }}%</small></div><button class="primary wide" :disabled="!canSubmit" @click="submit">{{ working ? '正在处理…' : '上传并预检 →' }}</button></section><section class="panel"><h2>导入流程</h2><p>1. 上传 Bundle 并检查校验结果。</p><p>2. 核对目标仓库、完整性和风险后确认。</p><p>3. 后台逐文件发布并核验远端内容。</p><p class="muted">浏览器关闭后，已确认的任务继续由后台执行。发布结果不明时先核实，避免重复提交。</p></section></div>
        <section v-else-if="page === 'history'" class="panel"><div class="toolbar"><label>状态 <select v-model="filter" @change="offset = 0; loadHistory()"><option value="">全部</option><option v-for="state in ['READY','QUEUED','RUNNING','COMPLETED','PARTIAL','FAILED','NEEDS_AUTH','INTERRUPTED','EXPIRED']" :key="state" :value="state">{{ statusText(state) }}</option></select></label><button @click="loadHistory">刷新</button><small>共 {{ total }} 项</small></div><div v-if="!taskList.length" class="empty">暂无符合条件的任务</div><button v-for="item in taskList" :key="item.id" class="task-row" @click="selectTask(item.id)"><span class="row-main"><strong>{{ item.repository }} · {{ item.ecosystem }}</strong><small>{{ date(item.created_at) }} · {{ item.id.slice(0,8) }}</small></span><StatusBadge :status="item.status" /></button><div class="pager"><button :disabled="offset === 0" @click="offset -= 25; loadHistory()">上一页</button><button :disabled="offset + 25 >= total" @click="offset += 25; loadHistory()">下一页</button></div></section>
        <section v-else-if="current" class="panel detail-panel"><div class="task-title"><div><h2>{{ current.repository }}</h2><code>{{ current.id }}</code></div><StatusBadge :status="current.status" /></div><div class="summary-grid"><div><span>目标仓库</span><strong>{{ current.repository }}</strong></div><div><span>生态</span><strong>{{ current.ecosystem }}</strong></div><div><span>提交时间</span><strong>{{ date(current.created_at) }}</strong></div><div><span>ZIP SHA-256</span><code>{{ current.zip_sha256 || '—' }}</code></div></div>
          <TaskStages :progress="current.progress" :status="current.status" kind="import" />
          <div v-if="current.error" class="alert error">{{ errorText(current.error) }}</div>
          <div v-if="currentPreview" class="preview"><h3>预检摘要</h3><div class="preview-numbers"><div><strong>{{ currentPreview.package_count }}</strong><span>包</span></div><div><strong>{{ currentPreview.artifact_count }}</strong><span>制品</span></div><div><strong>{{ currentPreview.warnings?.length || 0 }}</strong><span>风险提示</span></div></div><div v-if="currentPreview.verification" class="verification-list"><div v-for="(label,key) in {resolution_status:'依赖解析完整性', collection_status:'制品收集完整性', offline_install_status:'离线安装验证'}" :key="key"><span>{{ label }}</span><StatusBadge :status="String((currentPreview.verification as Record<string,unknown>)[key] || 'NOT_VERIFIED')" /></div></div><div v-if="currentPreview.warnings?.length" class="warning-list"><strong>需要核对</strong><p v-for="(warning,i) in currentPreview.warnings" :key="i">{{ warningText(warning) }}</p></div></div>
          <div v-if="readyToConfirm" class="confirm-box"><strong>确认发布到 {{ current.repository }}</strong><p class="muted">请核对上述生态、完整性与风险。确认后后台将检查远端内容，并发布缺失制品；同身份内容冲突会单独报告。</p><div class="actions"><button class="primary" :disabled="working || !!pendingOperation" @click="approve">确认并开始发布 →</button></div></div>
          <div class="actions"><button v-if="needsAuth" class="primary" @click="showAuth = !showAuth">重新授权并恢复</button><button @click="selectTask(current.id)">刷新状态</button><button v-if="taskDone && current.status !== 'COMPLETED'" @click="page = 'new'; notice = '请选择原 Bundle 创建新任务。系统会重新检查远端内容，不直接重发结果未知的制品。'">重新提交 Bundle</button></div>
          <form v-if="showAuth" class="panel" @submit.prevent="reauthorize"><label class="field">使用当前账号密码重新授权<input v-model="authPassword" type="password" autocomplete="current-password" required /></label><button class="primary" :disabled="working">提交授权</button></form>
          <div v-if="current.counts" class="results-counts"><span v-for="(count,state) in current.counts" :key="state"><StatusBadge :status="String(state)" /> {{ count }}</span></div>
          <p v-if="current.counts?.UNKNOWN" class="alert warning">存在发布结果未知的制品，请先核实远端内容；未知结果不能视为成功。</p><p v-if="current.counts?.CONFLICT" class="alert warning">存在同身份内容冲突，请检查目标仓库中的制品。</p>
          <div class="file-results"><h3>文件结果</h3><div class="toolbar"><label>筛选 <select v-model="fileFilter" @change="fileOffset = 0; loadFiles()"><option value="">全部</option><option v-for="state in ['PENDING','CHECKING','UPLOADING','VERIFYING','UPLOADED','SKIPPED','FAILED','CONFLICT','UNKNOWN']" :key="state" :value="state">{{ statusText(state) }}</option></select></label><small>共 {{ fileTotal }} 项</small></div><div v-for="file in currentFiles" :key="file.artifact_id" class="file-row"><span class="filename">{{ file.filename }}<small v-if="file.error">{{ file.error }}</small></span><StatusBadge :status="file.status" /></div><p v-if="!currentFiles.length" class="muted">暂无符合条件的文件。</p><div class="pager"><button :disabled="fileOffset === 0" @click="fileOffset -= 100; loadFiles()">上一页</button><button :disabled="fileOffset + 100 >= fileTotal" @click="fileOffset += 100; loadFiles()">下一页</button></div></div>
          <details @toggle="logsOpen = ($event.target as HTMLDetailsElement).open; logsOpen && loadLogs()"><summary>详细任务日志</summary><div v-for="event in events" :key="event.id" class="event-row"><time>{{ date(event.created_at) }}</time><span>{{ event.event }} {{ event.artifact_id || '' }}</span></div><p v-if="!events.length" class="muted">暂无事件记录。</p><button v-if="events.length < eventTotal" @click="loadLogs(true)">加载更多</button></details>
          <details @toggle="($event.target as HTMLDetailsElement).open && showReport()"><summary>完整报告</summary><pre v-if="currentReport">{{ JSON.stringify(currentReport, null, 2) }}</pre></details>
        </section>
      </template><footer class="footer">PackFerry · 任务按权限隔离 · Nexus 发布结果独立核验</footer>
    </main>
  </div>
</template>
