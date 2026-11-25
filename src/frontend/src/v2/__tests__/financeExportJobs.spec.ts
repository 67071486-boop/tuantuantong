// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FinanceExportJob } from '@/api/financeExportJobs'

const { queue, status, download, recent } = vi.hoisted(() => ({
  queue: vi.fn(), status: vi.fn(), download: vi.fn(), recent: vi.fn(),
}))

vi.mock('@/api/financeExportJobs', () => ({
  queueFinanceExport: queue, financeExportStatus: status,
  downloadFinanceExport: download, recentFinanceExports: recent,
}))

import FinanceExportJobStatus from '../FinanceExportJobStatus.vue'
import { useFinanceExportJob } from '../useFinanceExportJob'

const pending: FinanceExportJob = {
  id: '9b884f04-598d-4986-b089-a14547e4b980', status: 'pending', message: '',
  created_at: '2026-11-22T00:00:00+08:00', expires_at: '2026-11-22T02:00:00+08:00',
}
const ready: FinanceExportJob = { ...pending, status: 'ready' }

function mountExports() {
  const Component = defineComponent({
    setup() {
      const { job, start, download: save, jobProblem, retryProgress } = useFinanceExportJob('orders')
      return () => h('div', [
        h('button', { 'data-test': 'submit', onClick: () => start({ game: 3, is_urgent: false }) }, '导出'),
        job.value ? h(FinanceExportJobStatus, {
          job: job.value, problem: jobProblem.value, onDownload: save, onRetry: retryProgress,
        }) : null,
      ])
    },
  })
  return mount(Component)
}

describe('订单财务后台导出进度', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    sessionStorage.clear()
    queue.mockReset().mockResolvedValue(pending)
    status.mockReset().mockResolvedValue(ready)
    recent.mockReset().mockResolvedValue([ready])
    download.mockReset().mockResolvedValue({ blob: new Blob(), filename: 'orders.xlsx' })
  })
  afterEach(() => { vi.useRealTimers() })

  it('先显示排队进度，生成后才允许下载', async () => {
    const wrapper = mountExports()
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    expect(queue).toHaveBeenCalledWith('orders', { game: 3, is_urgent: false })
    expect(wrapper.get('[role="status"]').text()).toContain('排队')
    expect(wrapper.find('[data-v2="export-download"]').exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(3000)
    await wrapper.get('[data-v2="export-download"]').trigger('click')
    expect(status).toHaveBeenCalledWith('orders', pending.id)
    expect(download).toHaveBeenCalledWith('orders', pending.id)
    wrapper.unmount()
  })

  it('同一标签页离开后再进入仍可下载本人最近生成的文件', async () => {
    const first = mountExports()
    await first.get('[data-test="submit"]').trigger('click')
    await flushPromises()
    first.unmount()

    const next = mountExports()
    await flushPromises()
    expect(recent).toHaveBeenCalledWith('orders')
    await next.get('[data-v2="export-download"]').trigger('click')
    expect(download).toHaveBeenCalledWith('orders', ready.id)
    await vi.advanceTimersByTimeAsync(3000)
    expect(status).not.toHaveBeenCalled()
    next.unmount()
  })
})
