import { beforeEach, describe, expect, it, vi } from 'vitest'

const storage = new Map<string, string>()
const sessionStorageMock = {
  getItem: (key: string) => storage.get(key) || null,
  setItem: (key: string, value: string) => { storage.set(key, value) },
  removeItem: (key: string) => { storage.delete(key) },
}

describe('importer browser API', () => {
  beforeEach(() => {
    vi.resetModules()
    storage.clear()
    vi.stubGlobal('sessionStorage', sessionStorageMock)
  })

  it('sends the CSRF token on cookie authenticated writes', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ username: 'operator', csrf_token: 'csrf-value' }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: 'task-1', status: 'QUEUED' }) })
    vi.stubGlobal('fetch', fetchMock)
    const api = await import('./api')
    await api.login('operator', 'password')
    await api.confirm('task-1', 'operation-key')
    const headers = fetchMock.mock.calls[1][1].headers as Headers
    expect(headers.get('X-CSRF-Token')).toBe('csrf-value')
    expect(headers.get('Idempotency-Key')).toBe('operation-key')
    expect(storage.get('pf_csrf')).toBe('csrf-value')
  })

  it('returns structured API errors to the caller', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 403, json: async () => ({ error: { code: 'CSRF_REJECTED', message: '请求来源无效', retryable: false } }) }))
    const api = await import('./api')
    await expect(api.repositories()).rejects.toMatchObject({ status: 403, apiError: { code: 'CSRF_REJECTED' } })
  })

  it('clears an expired session and accepts a new login token', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ username: 'operator', csrf_token: 'old-csrf' }) })
      .mockResolvedValueOnce({ ok: false, status: 401, json: async () => ({ error: { code: 'SESSION_EXPIRED', message: '请重新登录', retryable: false } }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ username: 'operator', csrf_token: 'new-csrf' }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: 'task-1', status: 'QUEUED' }) })
    vi.stubGlobal('fetch', fetchMock)
    const api = await import('./api')
    await api.login('operator', 'password')
    await expect(api.me()).rejects.toMatchObject({ status: 401 })
    api.clearLocalSession()
    expect(storage.has('pf_csrf')).toBe(false)
    await api.login('operator', 'password')
    await api.confirm('task-1', 'retry-key')
    expect((fetchMock.mock.calls[3][1].headers as Headers).get('X-CSRF-Token')).toBe('new-csrf')
  })

  it('supports a pending operation lookup before and after a result appears', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 404, json: async () => ({ error: { code: 'NOT_FOUND', message: '操作不存在', retryable: false } }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ operation: 'upload', task: { id: 'task-2', status: 'READY' } }) })
    vi.stubGlobal('fetch', fetchMock)
    const api = await import('./api')
    await expect(api.operation('same-key')).rejects.toMatchObject({ status: 404 })
    expect((await api.operation('same-key')).task.id).toBe('task-2')
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual(['/api/v1/operations/same-key', '/api/v1/operations/same-key'])
  })
})
