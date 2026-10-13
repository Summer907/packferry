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
  manifest?: Record<string, unknown>
  error?: { code: string; message: string; retryable: boolean }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, { credentials: 'same-origin', ...init })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = payload.error
    throw new Error(error?.message || `请求失败（HTTP ${response.status}）`)
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
