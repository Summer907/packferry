// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import TaskStages from '../../shared/TaskStages.vue'
describe('truthful progress', () => {
  it('does not invent a bar without a reliable denominator', () => {
    const wrapper = mount(TaskStages, { props: { kind: 'export', status: 'RUNNING', progress: { stage: 'resolving', operation: '解析目标', completed: 12, total: null } } })
    expect(wrapper.find('progress').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂无可靠总数')
    wrapper.unmount()
  })
  it('labels file completion separately from success', () => {
    const wrapper = mount(TaskStages, { props: { kind: 'import', status: 'PARTIAL', progress: { stage: 'completed', operation: '存在失败项', completed: 12, total: 12 } } })
    expect(wrapper.get('progress').attributes('value')).toBe('12')
    expect(wrapper.text()).toContain('处理完成不代表全部成功')
    expect(wrapper.findAll('.finished')).toHaveLength(0)
    wrapper.unmount()
  })
  it('does not reconstruct stages for a legacy task', () => {
    const wrapper = mount(TaskStages, { props: { kind: 'export', status: 'COMPLETED' } })
    expect(wrapper.findAll('.stage')).toHaveLength(0)
    expect(wrapper.text()).toContain('没有阶段记录')
    wrapper.unmount()
  })
})
