import { computed, onMounted, onUnmounted, ref } from 'vue'
import { AuthExpiredError, BizError, HttpError } from '@/api/errors'
import {
  downloadFinanceExport,
  financeExportStatus,
  queueFinanceExport,
  recentFinanceExports,
  type FinanceExportJob,
  type FinanceExportKind,
} from '@/api/financeExportJobs'

export function useFinanceExportJob(kind: FinanceExportKind) {
  const job = ref<FinanceExportJob | null>(null)
  const jobProblem = ref('')
  const busy = computed(() => job.value?.status === 'pending' || job.value?.status === 'running')
  const storageKey = `ttt-finance-export-${kind}`
  let pollTimer: ReturnType<typeof setTimeout> | undefined
  let generation = 0

  function stopPolling(): void {
    clearTimeout(pollTimer)
  }

  function schedulePoll(snapshot: number): void {
    stopPolling()
    if (busy.value) pollTimer = setTimeout(() => { void poll(snapshot) }, 3000)
  }

  async function poll(snapshot: number): Promise<void> {
    const id = job.value?.id
    if (!id || snapshot !== generation) return
    try {
      const latest = await financeExportStatus(kind, id)
      if (snapshot !== generation) return
      jobProblem.value = ''
      job.value = latest
      schedulePoll(snapshot)
    } catch (error) {
      if (snapshot !== generation) return
      const expired = error instanceof BizError && error.code === 1002
      const denied = error instanceof AuthExpiredError || (error instanceof HttpError && [403, 404].includes(error.status))
      if (expired || denied) {
        job.value = null
        return
      }
      jobProblem.value = error instanceof Error ? error.message : '暂时无法读取导出进度'
    }
  }

  async function start(filters: Record<string, string | number | boolean>): Promise<boolean> {
    const snapshot = ++generation
    stopPolling()
    const accepted = await queueFinanceExport(kind, filters)
    if (!accepted) return false
    try {
      sessionStorage.setItem(storageKey, '1')
    } catch {
      // Storage access may be restricted by the browser.
    }
    if (snapshot === generation) {
      jobProblem.value = ''
      job.value = accepted
      schedulePoll(snapshot)
    }
    return true
  }

  async function restore(): Promise<void> {
    const snapshot = generation
    try {
      if (sessionStorage.getItem(storageKey) !== '1') return
      const [latest] = await recentFinanceExports(kind)
      if (snapshot !== generation) return
      job.value = latest ?? null
      schedulePoll(snapshot)
    } catch {
      // Restoration is optional. The export button still starts a new request.
    }
  }

  async function download(): Promise<{ blob: Blob; filename: string } | null> {
    if (job.value?.status !== 'ready') return null
    return downloadFinanceExport(kind, job.value.id)
  }

  function retryProgress(): void {
    void poll(generation)
  }

  onMounted(() => { void restore() })
  onUnmounted(() => {
    generation++
    stopPolling()
  })

  return { job, jobProblem, busy, start, download, retryProgress }
}
