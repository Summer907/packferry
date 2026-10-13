import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createExport, establishLocalSession, getExport } from './api'

describe('local exporter API', () => {
  beforeEach(() => vi.unstubAllGlobals())

  it('exchanges a launch fragment and removes it from browser history', async () => {
    const replaceState = vi.fn()
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
    vi.stubGlobal('location', { hash: '#token=one-time-secret', pathname: '/', search: '' })
    vi.stubGlobal('history', { replaceState })
    vi.stubGlobal('fetch', fetchMock)
    await establishLocalSession()
    expect(replaceState).toHaveBeenCalledWith(null, '', '/')
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/local/session', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ token: 'one-time-secret' }),
    }))
  })

  it('submits the same constraint and environment contract exposed by the UI', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: 'job-1', status: 'QUEUED' }) })
    vi.stubGlobal('fetch', fetchMock)
    const request = { ecosystem: 'pypi' as const, constraints: ['requests>=2'], environments: [{ environment_id: 'py39-win', runtime: 'cpython', version: '3.9', platform: 'win_amd64', abi: 'cp39' }], resolution_policy: 'fresh' as const, lock_policy: 'prefer' as const, production_only: true }
    expect((await createExport(request)).id).toBe('job-1')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(request)
  })

  it('shows structured API errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 422, json: async () => ({ error: { code: 'BAD_INPUT', message: '目标环境无效', retryable: false } }) }))
    await expect(getExport('job-1')).rejects.toThrow('目标环境无效')
  })

  it('rejects and removes a malformed launch fragment', async () => {
    const replaceState = vi.fn()
    vi.stubGlobal('location', { hash: '#invalid=token', pathname: '/', search: '' })
    vi.stubGlobal('history', { replaceState })
    await expect(establishLocalSession()).rejects.toThrow('启动令牌无效')
    expect(replaceState).toHaveBeenCalledWith(null, '', '/')
  })
})
