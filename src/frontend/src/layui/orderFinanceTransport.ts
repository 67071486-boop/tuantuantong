import { AuthExpiredError, HttpError } from '@/api/errors'
import { fetchOrderFinanceTable, orderFinanceDirectConfigured, orderFinancePath } from '@/api/orderFinanceDirect'

type AjaxOptions = { url: string; type?: string; async?: boolean; dataTypes?: string[] }
type Complete = (status: number, statusText: string, responses?: { text: string }, responseHeaders?: string) => void
type AjaxTransport = (options: AjaxOptions) => {
  send: (headers: Record<string, string>, complete: Complete) => void
  abort: () => void
} | undefined

const registered = new WeakSet<object>()

export function installOrderFinanceTableTransport(jquery: unknown): void {
  if (!orderFinanceDirectConfigured() || !jquery || (typeof jquery !== 'object' && typeof jquery !== 'function')) return
  const $ = jquery as { ajaxTransport?: (types: string, transport: AjaxTransport) => void }
  if (typeof $.ajaxTransport !== 'function' || registered.has($)) return
  registered.add($)

  // Use the vendored jQuery transport hook for existing table and selector requests.
  $.ajaxTransport('+*', options => {
    if (options.type?.toUpperCase() !== 'GET' || options.async === false
      || options.dataTypes?.some(type => type !== 'json' && type !== 'text' && type !== '*')
      || !orderFinancePath(options.url)) return

    const controller = new AbortController()
    return {
      send(headers, complete) {
        void fetchOrderFinanceTable(options.url, { signal: controller.signal, headers }).then(response => {
          if (controller.signal.aborted) return
          const rawHeaders = Array.from(response.headers, ([key, value]) => key + ': ' + value).join('\r\n')
          complete(response.status, response.statusText, { text: response.text }, rawHeaders)
        }, (error: unknown) => {
          if (controller.signal.aborted) return
          const status = error instanceof AuthExpiredError ? 401 : error instanceof HttpError ? error.status : 0
          complete(status, error instanceof Error ? error.message : '订单财务请求失败')
        })
      },
      abort() {
        controller.abort()
      },
    }
  })
}
