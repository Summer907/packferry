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
    vi.resetAllMocks()
    sessionStorage.clear()
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
    expect(api.upload).toHaveBeenCalledWith(expect.any(File), 'npm-hosted', expect.any(String), expect.any(Function))
    expect(wrapper.text()).toContain('预检摘要')
    await wrapper.get('.actions .primary').trigger('click')
    await flushPromises()
    expect(api.confirm).toHaveBeenCalledWith('task-1', expect.any(String))
    expect(wrapper.text()).toContain('已完成')
    wrapper.unmount()
  })

  it('returns to login when background polling detects session expiry', async () => {
    vi.useFakeTimers()
    const expired = new api.HttpError(401, { code: 'SESSION_EXPIRED', message: '请重新登录', retryable: false })
    vi.mocked(api.tasks).mockResolvedValueOnce({ items: [], total: 0 }).mockRejectedValueOnce(expired)
    const wrapper = mount(App)
    await flushPromises()
    expect(wrapper.text()).toContain('新建导入')
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(api.clearLocalSession).toHaveBeenCalled()
    expect(wrapper.text()).toContain('登录内网中心')
    wrapper.unmount()
  })
  it('blocks repeat uploads until an unknown operation result is reconciled', async () => {
    vi.mocked(api.upload).mockRejectedValue(new api.HttpError(502, { code: 'GATEWAY', message: 'response lost', retryable: true }))
    vi.mocked(api.operation).mockRejectedValue(new api.HttpError(404, { code: 'NOT_FOUND', message: 'not ready', retryable: false }))
    const wrapper = mount(App); await flushPromises()
    const input = wrapper.get('input[type="file"]')
    Object.defineProperty(input.element, 'files', { value: [new File(['zip'], 'bundle.zip')] })
    await input.trigger('change'); await wrapper.get('button.wide').trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('结果待核实')
    expect(wrapper.get('button.wide').attributes('disabled')).toBeDefined()
    const calls = vi.mocked(api.upload).mock.calls
    expect(JSON.parse(sessionStorage.getItem('pf_pending_operation:operator')!).key).toBe(calls[0][2])
    vi.mocked(api.operation).mockResolvedValue({ operation: 'upload', task: fakeTask })
    vi.mocked(api.task).mockResolvedValue(fakeTask)
    await wrapper.findAll('button').find(x => x.text() === '查询操作结果')!.trigger('click'); await flushPromises()
    expect(wrapper.text()).toContain('预检摘要')
    expect(api.upload).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem('pf_pending_operation:operator')).toBeNull()
    wrapper.unmount()
  })

  it('keeps the selected task when an older detail response arrives late', async () => {
    const second = { ...fakeTask, id: 'task-2', repository: 'second-repo' }
    vi.mocked(api.tasks).mockResolvedValue({ items: [fakeTask, second], total: 2 })
    let resolveFirst!: (value: typeof fakeTask) => void
    vi.mocked(api.task).mockImplementation(id => id === 'task-1' ? new Promise(resolve => { resolveFirst = resolve }) : Promise.resolve(second))
    const wrapper = mount(App); await flushPromises()
    const history = () => wrapper.findAll('.sidebar button').find(x => x.text().includes('最近任务'))!
    await history().trigger('click'); await flushPromises()
    await wrapper.findAll('.task-row')[0].trigger('click')
    await history().trigger('click'); await flushPromises()
    await wrapper.findAll('.task-row')[1].trigger('click'); await flushPromises()
    resolveFirst(fakeTask); await flushPromises()
    expect(wrapper.get('.task-title').text()).toContain('second-repo')
    expect(wrapper.get('.task-title').text()).not.toContain('task-1')
    wrapper.unmount()
  })

})
