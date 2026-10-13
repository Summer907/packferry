<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { authorize, clearLocalSession, confirm, files, HttpError, login, logout, me, operation, preview, report, repositories, task, tasks, upload, type FileRecord, type Preview, type Repository, type Task } from './api'

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

const taskDone = computed(() => !!current.value && ['completed', 'complete', 'succeeded', 'partial', 'failed', 'error', 'conflict'].includes(current.value.status.toLowerCase()))
const needsAuth = computed(() => !!current.value && ['needs_auth', 'needs-auth', 'waiting_auth'].includes(current.value.status.toLowerCase()))
const readyToConfirm = computed(() => !!current.value && ['ready', 'preview', 'validated', 'pending_confirmation'].includes(current.value.status.toLowerCase()))
const canSubmit = computed(() => !!selectedFile.value && !!selectedRepository.value && !working.value)

function message(e: unknown): string { return e instanceof Error ? e.message : String(e) }
function date(value?: string): string { return value ? new Date(value).toLocaleString('zh-CN') : '—' }
function clearFeedback() { error.value = ''; notice.value = '' }
function rememberPending(value: typeof pendingOperation.value) {
  pendingOperation.value = value
  if (value) sessionStorage.setItem('pf_pending_operation', JSON.stringify(value))
  else sessionStorage.removeItem('pf_pending_operation')
}
async function checkPendingOperation() {
  if (!pendingOperation.value) return
  clearFeedback()
  const pending = pendingOperation.value
  try {
    const result = await operation(pending.key)
    await refreshTask(result.task.id)
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
    actor.value = ''
    current.value = null
    taskList.value = []
    error.value = '会话已失效，请重新登录。'
  } else error.value = message(e)
}

async function signIn() {
  clearFeedback()
  loggingIn.value = true
  try {
    actor.value = await login(username.value.trim(), password.value)
    password.value = ''
    await refreshAll()
  } catch (e) { handleFailure(e) }
  finally { loggingIn.value = false }
}

async function signOut() {
  clearFeedback()
  try { await logout() } catch (e) { error.value = message(e) }
  actor.value = ''
  current.value = null
  taskList.value = []
  if (poller) window.clearInterval(poller)
}

async function refreshAll() {
  const [repos, list] = await Promise.all([repositories(), tasks()])
  repositoryList.value = repos
  taskList.value = list.items
  if (!selectedRepository.value && repos.length) selectedRepository.value = repos[0].name
}

async function selectTask(id: string) {
  clearFeedback()
  try { await refreshTask(id) }
  catch (e) { handleFailure(e) }
}

async function refreshTask(id?: string) {
  const targetId = id || current.value?.id
  if (!targetId) return
  const next = await task(targetId)
  current.value = next
  const [p, f] = await Promise.allSettled([preview(targetId), files(targetId)])
  currentPreview.value = p.status === 'fulfilled' ? p.value : null
  currentFiles.value = f.status === 'fulfilled' ? f.value.items : []
  if (taskDone.value) {
    try { currentReport.value = await report(targetId) } catch { currentReport.value = null }
  }
  const list = await tasks()
  taskList.value = list.items
}

async function submit() {
  if (!selectedFile.value || !selectedRepository.value) return
  clearFeedback()
  working.value = true
  const key = crypto.randomUUID()
  try {
    const result = await upload(selectedFile.value, selectedRepository.value, key)
    rememberPending(null)
    await refreshTask(result.id)
    notice.value = 'Bundle 已接收。请检查预检结果后确认导入。'
  } catch (e) {
    if (e instanceof HttpError) handleFailure(e)
    else {
      rememberPending({ key, kind: 'upload' })
      try {
        const result = await operation(key)
        const id = result.task?.id
        if (id) { await refreshTask(id); rememberPending(null); notice.value = '网络中断后已核实上传结果。' }
        else error.value = '上传结果尚未确认。请稍后通过任务列表或操作键查询，勿重复提交。'
      } catch { error.value = '上传结果未知。请稍后查询任务列表，勿重复提交。' }
    }
  } finally { working.value = false }
}

async function approve() {
  if (!current.value) return
  clearFeedback()
  working.value = true
  const id = current.value.id
  const key = crypto.randomUUID()
  try { await confirm(id, key); rememberPending(null); await refreshTask(id); notice.value = '已确认，任务正在后台执行。' }
  catch (e) {
    if (e instanceof HttpError) handleFailure(e)
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

onMounted(async () => {
  try { pendingOperation.value = JSON.parse(sessionStorage.getItem('pf_pending_operation') || 'null') }
  catch { rememberPending(null) }
  try { actor.value = await me(); await refreshAll() }
  catch { actor.value = '' }
  finally { loading.value = false }
  poller = window.setInterval(async () => {
    if (!actor.value) return
    try { if (current.value && !taskDone.value) await refreshTask(); else taskList.value = (await tasks()).items }
    catch (e) { if (e instanceof HttpError && e.status === 401) handleFailure(e) }
  }, 2000)
})
onUnmounted(() => { if (poller) window.clearInterval(poller) })
</script>

<template>
  <div class="shell">
    <header class="topbar"><div class="brand"><span class="brand-mark">PF</span><span>PackFerry</span><small>内网导入中心</small></div><div v-if="actor" class="account"><span class="account-dot"></span>{{ actor }}<button @click="signOut">退出</button></div></header>
    <main v-if="loading" class="loading">正在读取会话…</main>
    <main v-else-if="!actor" class="login-layout"><div class="intro"><p class="eyebrow">IMPORT CONTROL</p><h1>让交付到达<br><em>正确的位置。</em></h1><p>使用你的 Nexus 账号登录，核对 Bundle 与目标仓库后开始导入。</p><div class="flow"><span>上传 ZIP</span><i>→</i><span>安全预检</span><i>→</i><span>确认发布</span></div></div><form class="login-card" @submit.prevent="signIn"><div class="card-icon">↗</div><h2>登录内网中心</h2><p>操作会关联你的真实身份，并写入审计记录。</p><label>用户名<input v-model="username" autocomplete="username" required /></label><label>密码<input v-model="password" type="password" autocomplete="current-password" required /></label><div v-if="error" class="alert error" role="alert">{{ error }}</div><button class="primary" :disabled="loggingIn">{{ loggingIn ? '正在验证…' : '使用 Nexus 账号登录' }}</button></form></main>
    <main v-else class="workspace">
      <div class="page-head"><div><p class="eyebrow">INTERNAL WORKSPACE</p><h1>Bundle 导入</h1><p>安全校验、目标核对、逐文件发布与结果追踪。</p></div><div class="page-meta"><span>单向人工传入</span><strong>01 / 03</strong></div></div>
      <div v-if="error" class="alert error" role="alert">{{ error }}</div><div v-if="notice" class="alert notice" role="status">{{ notice }}</div>
      <div v-if="pendingOperation" class="alert notice" role="status">上次{{ pendingOperation.kind === 'upload' ? '上传' : '确认' }}结果待核实。<button class="secondary" @click="checkPendingOperation">查询操作结果</button></div>
      <div class="workspace-grid"><div class="left-column"><section class="panel upload-panel"><div class="heading"><span class="number">01</span><div><h2>提交 Bundle</h2><p>上传外网准备端生成的 ZIP</p></div></div><label class="dropzone"><input type="file" accept=".zip,application/zip" @change="selectedFile = ($event.target as HTMLInputElement).files?.[0] || null" /><span class="upload-icon">⇧</span><strong>{{ selectedFile?.name || '选择 Bundle ZIP' }}</strong><small>{{ selectedFile ? `${(selectedFile.size / 1024 / 1024).toFixed(1)} MB` : '点击选择本机文件' }}</small></label><label class="field">目标 Hosted 仓库<select v-model="selectedRepository"><option disabled value="">请选择仓库</option><option v-for="repo in repositoryList" :key="repo.name" :value="repo.name">{{ repo.name }} · {{ repo.ecosystem }}</option></select></label><button class="primary wide" :disabled="!canSubmit" @click="submit">{{ working ? '处理中…' : '上传并预检' }} <span>→</span></button></section>
      <section class="panel task-list"><div class="heading compact"><span class="number">02</span><div><h2>我的任务</h2><p>上传后可随时重新打开</p></div></div><div v-if="!taskList.length" class="no-tasks">还没有导入任务。</div><button v-for="item in taskList" :key="item.id" class="task-row" :class="{selected: current?.id === item.id}" @click="selectTask(item.id)"><span class="row-icon">□</span><span class="row-main"><strong>{{ item.repository }}</strong><small>{{ date(item.created_at) }} · {{ item.id.slice(0, 8) }}</small></span><span class="pill">{{ item.status }}</span></button></section></div>
      <section class="panel detail-panel"><div class="heading"><span class="number">03</span><div><h2>预检与执行</h2><p>确认后由后台持续执行</p></div></div><div v-if="!current" class="empty"><div class="empty-symbol">◎</div><h3>选择一个任务</h3><p>查看预检、风险和文件级发布结果。</p></div><template v-else><div class="task-title"><div><span class="mini-label">TASK ID</span><code>{{ current.id }}</code></div><span class="pill accent">{{ current.status }}</span></div><div class="summary-grid"><div><span>目标仓库</span><strong>{{ current.repository }}</strong></div><div><span>生态</span><strong>{{ current.ecosystem }}</strong></div><div><span>提交时间</span><strong>{{ date(current.created_at) }}</strong></div><div><span>ZIP SHA-256</span><code>{{ current.zip_sha256 ? current.zip_sha256.slice(0, 16) + '…' : '—' }}</code></div></div><div v-if="currentPreview" class="preview"><h3>预检摘要</h3><div class="preview-numbers"><div><strong>{{ currentPreview.package_count }}</strong><span>包</span></div><div><strong>{{ currentPreview.artifact_count }}</strong><span>文件</span></div><div><strong>{{ currentPreview.warnings?.length || 0 }}</strong><span>提醒</span></div></div><div v-if="currentPreview.warnings?.length" class="warning-list"><strong>需要核对</strong><p v-for="(warning, index) in currentPreview.warnings" :key="index">{{ typeof warning === 'string' ? warning : JSON.stringify(warning) }}</p></div><details v-if="currentPreview.verification"><summary>校验详情</summary><pre>{{ JSON.stringify(currentPreview.verification, null, 2) }}</pre></details></div><div class="actions"><button v-if="readyToConfirm" class="primary" :disabled="working" @click="approve">确认并开始发布 →</button><button v-if="needsAuth" class="primary" @click="showAuth = !showAuth">重新授权</button><button class="secondary" @click="refreshTask()">刷新状态</button></div><form v-if="showAuth" class="reauthorize" @submit.prevent="reauthorize"><label>使用当前账号密码重新授权<input v-model="authPassword" type="password" autocomplete="current-password" required /></label><button class="primary" :disabled="working">提交授权</button></form><div v-if="currentFiles.length" class="file-results"><h3>文件结果 <span>{{ currentFiles.length }}</span></h3><div v-for="file in currentFiles" :key="file.artifact_id" class="file-row"><span class="file-dot" :class="file.status.toLowerCase()"></span><span class="filename">{{ file.filename }}<small v-if="file.error">{{ file.error }}</small></span><strong>{{ file.status }}</strong></div></div><details v-if="currentReport" class="report"><summary>查看完整报告</summary><pre>{{ JSON.stringify(currentReport, null, 2) }}</pre></details></template></section></div>
    </main><footer>PackFerry · 用户仅能查看自己的任务 · 原始文件上传前后均校验</footer>
  </div>
</template>
