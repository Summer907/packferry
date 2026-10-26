// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import App from './App.vue'
import * as api from './api'
vi.mock('./api', () => ({
  establishLocalSession: vi.fn(), favorites: vi.fn(), createExport: vi.fn(), getExport: vi.fn(),
  listExports: vi.fn(), exportEvents: vi.fn(), rerunExport: vi.fn(), saveFavorite: vi.fn(), deleteFavorite: vi.fn(), downloadBundle: vi.fn(),
}))
let wrapper: VueWrapper
beforeEach(() => {
  vi.resetAllMocks()
  const storage = new Map<string,string>()
  vi.stubGlobal('localStorage', { getItem: (key: string) => storage.get(key) || null, setItem: (key: string, value: string) => storage.set(key,value), removeItem: (key: string) => storage.delete(key) })
  vi.mocked(api.establishLocalSession).mockResolvedValue()
  vi.mocked(api.favorites).mockResolvedValue({ items: [] })
  vi.mocked(api.listExports).mockResolvedValue({ items: [], total: 0 })
})
afterEach(() => { wrapper?.unmount(); vi.useRealTimers() })
async function setup() { wrapper = mount(App); await flushPromises() }
async function target() {
  await wrapper.get('input[aria-label="解释器版本"]').setValue('3.14')
  await wrapper.get('input[aria-label="平台架构"]').setValue('manylinux_2_28_aarch64')
  await wrapper.get('input[aria-label="ABI"]').setValue('cp314')
  await wrapper.findAll('button').find(x => x.text() === '加入目标列表')!.trigger('click')
}
describe('Exporter workspace', () => {
  it('requires an explicit target and submits arbitrary complete environment values', async () => {
    await setup()
    await wrapper.get('textarea').setValue('requests>=2\nnumpy==2.0')
    await wrapper.get('button.primary.wide').trigger('click')
    expect(api.createExport).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('至少添加一个完整目标环境')
    await target()
    vi.mocked(api.createExport).mockResolvedValue({ id: 'one', status: 'QUEUED' })
    vi.mocked(api.getExport).mockResolvedValue({ id: 'one', status: 'COMPLETED', progress: { stage: 'completed', operation: '完成', completed: null, total: null }, manifest: { verification: { resolution_status: 'PARTIAL', collection_status: 'PARTIAL', offline_install_status: 'NOT_VERIFIED' } } })
    await wrapper.get('button.primary.wide').trigger('click'); await flushPromises()
    expect(api.createExport).toHaveBeenCalledWith(expect.objectContaining({ constraints: ['requests>=2','numpy==2.0'], environments: [expect.objectContaining({ version: '3.14', platform: 'manylinux_2_28_aarch64', abi: 'cp314' })] }))
    expect(wrapper.text()).toContain('未执行离线安装验证')
    expect(wrapper.text()).toContain('保存带缺口 Bundle')
    expect(wrapper.find('progress').exists()).toBe(false)
  })
  it('preserves separate ecosystem drafts and target selections', async () => {
    await setup(); await wrapper.get('textarea').setValue('requests>=2'); await target()
    await wrapper.findAll('.segmented button')[1].trigger('click')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('')
    await wrapper.get('textarea').setValue('react@18')
    await wrapper.findAll('.segmented button')[0].trigger('click')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('requests>=2')
    expect(wrapper.text()).toContain('manylinux_2_28_aarch64')
    await wrapper.findAll('.segmented button')[1].trigger('click')
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('react@18')
  })
  it('saves a fully configured local favorite instead of a static preset', async () => {
    await setup()
    await wrapper.get('input[aria-label="解释器版本"]').setValue('3.15')
    await wrapper.get('input[aria-label="平台架构"]').setValue('win_arm64')
    await wrapper.get('input[aria-label="ABI"]').setValue('cp315')
    await wrapper.get('input[maxlength="80"]').setValue('Windows ARM')
    vi.mocked(api.saveFavorite).mockResolvedValue({ id: 'f', name: 'Windows ARM', environment: { environment_id: 'arm', runtime: 'cpython', version: '3.15', platform: 'win_arm64', abi: 'cp315' } })
    await wrapper.findAll('button').find(x => x.text() === '保存到本机')!.trigger('click'); await flushPromises()
    expect(api.saveFavorite).toHaveBeenCalledWith(expect.any(String), 'Windows ARM', expect.objectContaining({ version: '3.15', platform: 'win_arm64', abi: 'cp315' }))
    expect(wrapper.text()).toContain('目标环境已保存到本机')
  })
})
