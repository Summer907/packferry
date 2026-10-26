export type Ecosystem = 'pypi' | 'npm'
export type Environment = {
  environment_id: string
  runtime: string
  version: string
  platform: string
  abi: string
  libc?: string
}
export type ExportRequest = {
  ecosystem: Ecosystem
  constraints: string[]
  environments: Environment[]
  project_path?: string
  resolution_policy: 'fresh' | 'reuse'
  lock_policy: 'prefer' | 'ignore'
  production_only: boolean
}
export type ExportTask = {
  id: string
  status: string
  created_at?: string
  updated_at?: string
  request?: ExportRequest
  rerun_of?: string
  progress?: import('../../shared/ui').Progress | null
  manifest?: Record<string, unknown>
  error?: { code: string; message: string; retryable: boolean }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'same-origin', ...init })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = payload.error
    throw new Error(error?.code ? `${error.message || '操作未完成，请检查输入'}（${error.code}）` : `请求失败（HTTP ${response.status}），请刷新状态后重试。`)
  }
  return payload as T
}

export async function establishLocalSession(): Promise<void> {
  const hash = location.hash.replace(/^#/, '')
  if (!hash) return
  const params = new URLSearchParams(hash)
  const token = params.get('token')
  history.replaceState(null, '', location.pathname + location.search)
  if (!token) throw new Error('启动令牌无效，请重新打开 PackFerry。')
  await api('/local/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  })
}

export function createExport(request: ExportRequest): Promise<ExportTask> {
  return api('/exports', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
}

export function getExport(id: string): Promise<ExportTask> {
  return api(`/exports/${encodeURIComponent(id)}`)
}

export function downloadBundle(id: string): void {
  location.assign(`/api/v1/exports/${encodeURIComponent(id)}/bundle`)
}
export type Favorite = { id: string; name: string; environment: Environment }
export function listExports(offset = 0, status = ''): Promise<{ items: ExportTask[]; total: number }> { return api(`/exports?limit=25&offset=${offset}&status=${encodeURIComponent(status)}`) }
export function exportEvents(id: string, offset = 0): Promise<{ items: import('../../shared/ui').TaskEvent[]; total: number }> { return api(`/exports/${encodeURIComponent(id)}/events?offset=${offset}&limit=100`) }
export function rerunExport(id: string): Promise<ExportTask> { return api(`/exports/${encodeURIComponent(id)}/rerun`, { method: 'POST' }) }
export function favorites(): Promise<{ items: Favorite[] }> { return api('/favorites') }
export function saveFavorite(id: string, name: string, environment: Environment): Promise<Favorite> { return api(`/favorites/${encodeURIComponent(id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, environment }) }) }
export function deleteFavorite(id: string): Promise<void> { return api(`/favorites/${encodeURIComponent(id)}`, { method: 'DELETE' }) }
