// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import App from './App.vue'
import * as api from './api'

vi.mock('./api', () => ({
  HttpError: class HttpError extends Error { constructor(public status: number, public apiError: unknown) { super('HTTP error') } },
  me: vi.fn(), login: vi.fn(), logout: vi.fn(), clearLocalSession: vi.fn(),
  repositories: vi.fn(), tasks: vi.fn(), task: vi.fn(), preview: vi.fn(), files: vi.fn(),
  report: vi.fn(), operation: vi.fn(), upload: vi.fn(), confirm: vi.fn(), authorize: vi.fn(),
}))

const fakeTask = { id: 'task-1', status: 'READY', owner: 'operator', repository: 'npm-hosted', ecosystem: 'npm', created_at: '2026-10-13T00:00:00Z', updated_at: '2026-10-13T00:00:00Z' }

describe('Importer workflow UI', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api.me).mockResolvedValue('operator')
    vi.mocked(api.repositories).mockResolvedValue([{ name: 'npm-hosted', ecosystem: 'npm' }])
    vi.mocked(api.tasks).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(api.preview).mockResolvedValue({ id: 'task-1', status: 'READY', repository: 'npm-hosted', ecosystem: 'npm', package_count: 1, artifact_count: 1, warnings: [], verification: {} })
    vi.mocked(api.files).mockResolvedValue({ items: [{ artifact_id: 'sha', filename: 'is-number.tgz', status: 'PENDING' }], total: 1 })
    vi.mocked(api.report).mockResolvedValue({ ...fakeTask, status: 'COMPLETED', files: [{ artifact_id: 'sha', filename: 'is-number.tgz', status: 'UPLOADED' }] })
  })
  afterEach(() => vi.useRealTimers())

  it('uploads, previews and confirms a mocked Bundle', async () => {
    vi.mocked(api.upload).mockResolvedValue(fakeTask)
    vi.mocked(api.task).mockResolvedValueOnce(fakeTask).mockResolvedValueOnce({ ...fakeTask, status: 'COMPLETED' })
    vi.mocked(api.confirm).mockResolvedValue({ ...fakeTask, status: 'QUEUED' })
    const wrapper = mount(App)
    await flushPromises()
    const input = wrapper.find('input[type="file"]')
    Object.defineProperty(input.element, 'files', { value: [new File(['zip'], 'bundle.zip', { type: 'application/zip' })] })
    await input.trigger('change')
    await wrapper.get('button.wide').trigger('click')
    await flushPromises()
    expect(api.upload).toHaveBeenCalledWith(expect.any(File), 'npm-hosted', expect.any(String))
    expect(wrapper.text()).toContain('预检摘要')
    await wrapper.get('.actions .primary').trigger('click')
    await flushPromises()
    expect(api.confirm).toHaveBeenCalledWith('task-1', expect.any(String))
    expect(wrapper.text()).toContain('COMPLETED')
    wrapper.unmount()
  })

  it('returns to login when background polling detects session expiry', async () => {
    vi.useFakeTimers()
    const expired = new api.HttpError(401, { code: 'SESSION_EXPIRED', message: '请重新登录', retryable: false })
    vi.mocked(api.tasks).mockResolvedValueOnce({ items: [], total: 0 }).mockRejectedValueOnce(expired)
    const wrapper = mount(App)
    await flushPromises()
    expect(wrapper.text()).toContain('Bundle 导入')
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(api.clearLocalSession).toHaveBeenCalled()
    expect(wrapper.text()).toContain('登录内网中心')
    wrapper.unmount()
  })
})
