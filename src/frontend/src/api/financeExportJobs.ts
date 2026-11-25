import { HttpError } from './errors'
import { request } from './http'
import { downloadExport } from './performances'

export type FinanceExportKind = 'orders' | 'settlements'

export type FinanceExportJob = {
  id: string
  status: 'pending' | 'running' | 'ready' | 'failed'
  message: string
  created_at: string
  expires_at: string
}

type ExportFilters = Record<string, string | number | boolean>

function exportJobsUrl(kind: FinanceExportKind): string {
  return `/api/business/${kind}/export-jobs/`
}

export async function queueFinanceExport(kind: FinanceExportKind, filters: ExportFilters): Promise<FinanceExportJob | null> {
  try {
    return await request<FinanceExportJob | null>({ url: exportJobsUrl(kind), method: 'post', data: filters })
  } catch (error) {
    if (error instanceof HttpError && error.status === 404) return null
    throw error
  }
}

export function recentFinanceExports(kind: FinanceExportKind): Promise<FinanceExportJob[]> {
  return request({ url: exportJobsUrl(kind) })
}

export function financeExportStatus(kind: FinanceExportKind, id: string): Promise<FinanceExportJob> {
  return request({ url: `${exportJobsUrl(kind)}${encodeURIComponent(id)}/` })
}

export function downloadFinanceExport(kind: FinanceExportKind, id: string) {
  return downloadExport(`${exportJobsUrl(kind)}${encodeURIComponent(id)}/download/`, {})
}
