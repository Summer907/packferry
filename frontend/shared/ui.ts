export type Progress = { stage: string; operation: string; completed: number | null; total: number | null; updated_at?: string }
export type TaskEvent = { id: number; created_at: string; stage?: string; operation?: string; event?: string; artifact_id?: string }
const names: Record<string, string> = {
  QUEUED: '排队中', RUNNING: '运行中', COMPLETED: '已完成', PARTIAL: '部分完成', FAILED: '失败', INTERRUPTED: '已中断',
  READY: '等待确认', NEEDS_AUTH: '需要授权', EXPIRED: '已过期', PENDING: '待处理', CHECKING: '检查远端', UPLOADING: '发布中', VERIFYING: '核验中',
  UPLOADED: '已发布并核验', SKIPPED: '已存在，跳过', CONFLICT: '内容冲突', UNKNOWN: '结果未知', COMPLETE: '完整', NOT_VERIFIED: '未执行离线安装验证', VERIFIED: '已验证', BLOCKED: '验证受阻',
}
export function statusText(value?: string): string { return value ? names[value.toUpperCase()] || value : '—' }
export function terminal(value: string): boolean { return ['COMPLETED', 'PARTIAL', 'FAILED', 'INTERRUPTED', 'EXPIRED', 'CONFLICT'].includes(value.toUpperCase()) }
export function date(value?: string): string { return value ? new Date(value).toLocaleString('zh-CN') : '—' }
export function errorText(value: unknown): string {
  if (value && typeof value === 'object' && 'message' in value) return String(value.message)
  return typeof value === 'string' ? value : '操作未完成，请刷新状态后重试。'
}
export function warningText(value: unknown): string {
  if (typeof value === 'string') return value
  if (value && typeof value === 'object') { const v = value as Record<string, unknown>; return String(v.message || v.code || '请查看完整报告') }
  return '请查看完整报告'
}
