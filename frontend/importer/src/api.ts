export type ApiError = { code: string; message: string; retryable: boolean }
export type Repository = { name: string; ecosystem: 'pypi' | 'npm' }
export type Task = {
  id: string
  status: string
  owner: string
  repository: string
  ecosystem: string
  created_at: string
  updated_at: string
  zip_sha256?: string
  error?: string | ApiError | null
}
export type Preview = {
  id: string
  status: string
  repository: string
  ecosystem: string
  package_count: number
  artifact_count: number
  warnings: unknown[]
  verification: unknown
}
export type FileRecord = { artifact_id: string; filename: string; status: string; error?: string | null }
export type TaskFiles = { items: FileRecord[]; total: number }
export type Report = Task & { files: FileRecord[] }

export class HttpError extends Error {
  constructor(public status: number, public apiError: ApiError) { super(apiError.message) }
}

let csrfToken = sessionStorage.getItem('pf_csrf') || ''

export function clearLocalSession(): void {
  csrfToken = ''
  sessionStorage.removeItem('pf_csrf')
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = init.method?.toUpperCase() || 'GET'
  const headers = new Headers(init.headers)
  if (method !== 'GET' && method !== 'HEAD' && csrfToken) headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch(`/api/v1${path}`, { ...init, headers, credentials: 'same-origin' })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const apiError: ApiError = payload.error || { code: 'HTTP_ERROR', message: `请求失败（HTTP ${response.status}）`, retryable: false }
    throw new HttpError(response.status, apiError)
  }
  return payload as T
}

export async function login(username: string, password: string): Promise<string> {
  const result = await request<{ username: string; csrf_token: string }>('/auth/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }),
  })
  csrfToken = result.csrf_token
  sessionStorage.setItem('pf_csrf', csrfToken)
  return result.username
}

export async function logout(): Promise<void> {
  try { await request('/auth/logout', { method: 'POST' }) }
  finally { clearLocalSession() }
}

export async function me(): Promise<string> {
  const result = await request<{ username: string }>('/auth/me')
  return result.username
}

export function repositories(): Promise<Repository[]> { return request('/repositories') }
export function tasks(): Promise<{ items: Task[]; total: number }> { return request('/imports') }
export function task(id: string): Promise<Task> { return request(`/imports/${encodeURIComponent(id)}`) }
export function preview(id: string): Promise<Preview> { return request(`/imports/${encodeURIComponent(id)}/preview`) }
export function files(id: string): Promise<TaskFiles> { return request(`/imports/${encodeURIComponent(id)}/files`) }
export function report(id: string): Promise<Report> { return request(`/imports/${encodeURIComponent(id)}/report`) }
export function operation(key: string): Promise<{ operation: string; task: Task }> { return request(`/operations/${encodeURIComponent(key)}`) }

export async function upload(bundle: File, repository: string, key: string): Promise<Task> {
  const body = new FormData()
  body.set('bundle', bundle)
  body.set('repository', repository)
  return request('/imports', { method: 'POST', headers: { 'Idempotency-Key': key }, body })
}

export function confirm(id: string, key: string): Promise<Task> {
  return request(`/imports/${encodeURIComponent(id)}/confirm`, { method: 'POST', headers: { 'Idempotency-Key': key } })
}

export function authorize(id: string, password: string): Promise<Task> {
  return request(`/imports/${encodeURIComponent(id)}/authorize`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }),
  })
}
