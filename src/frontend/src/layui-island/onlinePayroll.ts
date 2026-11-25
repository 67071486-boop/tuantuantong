import {
  cancelPayroll, createPayroll, keepPayrollPaymentCredentials, openPayrollBrowserPayment, PAYROLL_AUTO_QR_ATTEMPTS,
  PAYROLL_BROWSER_BLOCKED, PAYROLL_BROWSER_BUTTON, PAYROLL_CLOSE_HINT,
  PAYROLL_CREATING_HINT, PAYROLL_WAITING_HINT, payrollCategoryLabel, payrollQrRefreshStopped, payrollRecipientTotals,
  payrollStatusLabel,
  listMyPayroll, listPayroll, preparePayrollPayment, previewPayroll, readPayroll,
  type PayrollBatch, type PayrollGatewayBatch, type PayrollPreviewTransfer, type PayrollSelection, type PayrollTransfer,
} from '@/api/onlinePayroll'
import { isMobileUserAgent } from '@/mobileRedirect'

export const legacyPayrollUrl = (id: string): string =>
  `${import.meta.env.BASE_URL}agents/online-pay-detail?${new URLSearchParams({ batch: id })}`

export function textElement<K extends keyof HTMLElementTagNameMap>(tag: K, text: string): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag)
  element.textContent = text
  return element
}

function statusMessage(text: string): HTMLParagraphElement {
  const node = textElement('p', text)
  node.setAttribute('role', 'status')
  return node
}

function button(label: string, action: () => void): HTMLButtonElement {
  const result = textElement('button', label)
  result.type = 'button'
  result.className = 'layui-btn layui-btn-sm layui-btn-primary'
  result.onclick = action
  return result
}

function table(headers: string[], cells: (string | HTMLElement)[][]): HTMLTableElement {
  const result = document.createElement('table')
  result.className = 'layui-table'
  const header = result.createTHead().insertRow()
  headers.forEach((value) => header.append(textElement('th', value)))
  const body = result.createTBody()
  cells.forEach((values) => {
    const row = body.insertRow()
    values.forEach((value) => row.insertCell().append(value))
  })
  return result
}

function itemsTable(transfer: PayrollPreviewTransfer): HTMLTableElement {
  return table(['关联订单', '生效绩效', '扣点', '未结调整', '实付金额'],
    transfer.items.map((row) => [row.order_no, row.bonus, row.deduction, row.adjustment, row.amount]))
}

export function transferList(transfers: PayrollTransfer[]): HTMLElement {
  const host = document.createElement('div')
  if (!transfers.length) {
    host.append(textElement('p', '暂无结算打款记录'))
    return host
  }
  for (const transfer of transfers) {
    const details = document.createElement('details')
    const status = transfer.status === 'succeeded' && !transfer.finalized_at
      ? '打款成功，待记账'
      : payrollStatusLabel(transfer.status)
    details.append(textElement('summary', `${transfer.recipient_name} · ${transfer.amount} 元 · ${status}`))
    details.append(table(['收款账号', '计全付转账单号', '支付宝打款流水号', '成功打款时间'], [[
      transfer.account_masked, transfer.transfer_id || '—', transfer.channel_order_no || '—',
      transfer.success_time?.replace('T', ' ').slice(0, 19) || '—',
    ]]))
    if (transfer.last_error) details.append(textElement('p', transfer.last_error))
    details.append(itemsTable(transfer))
    host.append(details)
  }
  return host
}

export function closeHint(batch: PayrollBatch): HTMLElement {
  const node = textElement('p', PAYROLL_CLOSE_HINT)
  const link = textElement('a', '查看付款单')
  link.href = legacyPayrollUrl(batch.id)
  link.target = '_top'
  node.append(link)
  return node
}

export function mountLegacyBatch(
  host: HTMLElement,
  initial: PayrollBatch,
  onUpdate: (batch: PayrollBatch) => void,
  autoShowPayment = false,
): () => void {
  let disposed = false
  let busy = false
  let batch = initial
  let showing = false
  let autoQrReady = false
  let allowAutoPrepare = true
  const autoQrAttempts = new Map<PayrollGatewayBatch['id'], number>()
  let awaitingQr: PayrollGatewayBatch['id'] | undefined
  let paymentProgress: { id: PayrollGatewayBatch['id']; label: string } | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  let pollSequence = 0
  let autoQrId: PayrollGatewayBatch['id'] | undefined
  const desktop = !isMobileUserAgent(navigator.userAgent)
  const body = document.createElement('div')
  const error = textElement('p', '')
  error.setAttribute('role', 'alert')
  host.replaceChildren(error, body)

  function render(): void {
    const top = textElement('p', `${payrollStatusLabel(batch.status)} · ${batch.total} 元 · 付款支付宝 ${batch.payer_masked}`)
    if (batch.active) top.append(button('刷新打款结果', () => { void refresh() }))
    if (batch.can_cancel) {
      top.append(button(showing ? '确认取消' : '取消本次付款', () => {
        if (showing) {
          void perform(() => cancelPayroll(batch.id))
        } else {
          showing = true
          render()
        }
      }))
      if (showing) top.append(button('返回', () => { showing = false; render() }))
    }
    top.querySelectorAll('button').forEach((element) => { element.disabled = busy })
    body.replaceChildren(top)
    if (autoShowPayment && batch.status === 'preparing') body.append(statusMessage(PAYROLL_WAITING_HINT))
    if (showing) body.append(textElement('p', '只会取消尚未向计全付制单的明细，已提交明细仍需核对。'))
    for (const [index, part] of batch.gateway_batches.entries()) {
      body.append(textElement('h3', `批次 ${index + 1} · ${payrollStatusLabel(part.status)}`))
      if (part.last_error) body.append(textElement('p', part.last_error))
      if (paymentProgress?.id === part.id) body.append(statusMessage(paymentProgress.label))
      else if (awaitingQr === part.id) body.append(statusMessage('正在重新生成付款二维码，请稍候…'))
      if (part.can_prepare_payment) {
        const methods = document.createElement('div')
        methods.className = 'layui-row layui-col-space12'
        methods.append(scanSection(part))
        if (desktop) methods.append(browserSection(part))
        body.append(methods)
      }
      body.append(transferList(part.transfers))
    }
  }

  function scanSection(part: PayrollGatewayBatch): HTMLElement {
    const section = document.createElement('section')
    if (desktop) section.className = 'layui-col-sm6'
    section.setAttribute('aria-label', '手机支付宝扫码')
    section.append(textElement('h4', '手机支付宝扫码'), textElement('p', '用选定的付款支付宝扫码，核对金额后确认。'))
    const scanButton = part.payment_qr_png_base64
      ? button('重新获取付款码', () => {
        void perform(async () => {
          const result = await preparePayrollPayment(batch.id, part.id, true)
          awaitingQr = result.active ? part.id : undefined
          return result
        }, { id: part.id, label: '正在重新生成付款二维码，请稍候…' })
      })
      : button('获取付款码', () => { prepare(part) })
    scanButton.disabled = busy || autoQrId === part.id
    if (part.payment_qr_png_base64) {
      const image = document.createElement('img')
      image.width = 200
      image.height = 200
      image.alt = '支付宝付款二维码'
      image.src = `data:image/png;base64,${part.payment_qr_png_base64}`
      const qr = document.createElement('div')
      qr.append(image)
      section.append(qr)
    } else if (autoQrId === part.id) {
      section.append(statusMessage('正在获取付款二维码，请稍候…'))
    }
    section.append(scanButton)
    return section
  }

  function browserSection(part: PayrollGatewayBatch): HTMLElement {
    const section = document.createElement('section')
    section.className = 'layui-col-sm6'
    section.setAttribute('aria-label', '电脑浏览器付款')
    section.append(textElement('h4', '电脑浏览器付款'), textElement('p', '在浏览器打开支付宝付款页，核对金额后确认。'))
    if (part.payment_pc_url) {
      const link = textElement('a', PAYROLL_BROWSER_BUTTON)
      link.className = 'layui-btn layui-btn-sm'
      link.href = part.payment_pc_url
      link.target = '_blank'
      link.rel = 'noopener noreferrer'
      section.append(link)
    } else {
      const open = button(PAYROLL_BROWSER_BUTTON, () => { prepareDesktop(part) })
      open.className = 'layui-btn layui-btn-sm'
      open.disabled = busy
      section.append(open)
    }
    return section
  }

  function prepare(part: PayrollGatewayBatch): void {
    void perform(() => preparePayrollPayment(batch.id, part.id), {
      id: part.id, label: '正在获取付款二维码，请稍候…',
    })
  }

  function prepareDesktop(part: PayrollGatewayBatch): void {
    void perform(async () => {
      const result = await openPayrollBrowserPayment(
        () => preparePayrollPayment(batch.id, part.id, false, true), part.id,
        () => !disposed && host.isConnected,
      )
      if (result.blocked) error.textContent = PAYROLL_BROWSER_BLOCKED
      return result.batch
    }, { id: part.id, label: '正在获取电脑付款链接，请稍候…' })
  }

  function schedule(): void {
    clearTimeout(timer)
    if (!disposed && !busy && autoQrId === undefined && batch.active) {
      timer = setTimeout(() => {
        if (!host.isConnected) {
          cleanup()
          return
        }
        void pollStatus()
      }, 3000)
    }
  }

  function maybeShowPayment(): void {
    if (busy || autoQrId !== undefined) return
    const refreshed = batch.gateway_batches.find((part) => part.id === awaitingQr && part.can_prepare_payment)
    if (refreshed) {
      awaitingQr = undefined
      void prepareAutomaticQr(refreshed)
      return
    }
    if (!autoShowPayment || autoQrReady || !allowAutoPrepare) return
    allowAutoPrepare = false
    const first = batch.gateway_batches.find((part) =>
      part.can_prepare_payment && (autoQrAttempts.get(part.id) ?? 0) < PAYROLL_AUTO_QR_ATTEMPTS,
    )
    if (!first) return
    autoQrAttempts.set(first.id, (autoQrAttempts.get(first.id) ?? 0) + 1)
    void prepareAutomaticQr(first)
  }

  async function prepareAutomaticQr(part: PayrollGatewayBatch): Promise<void> {
    const sequence = ++pollSequence
    autoQrId = part.id
    error.textContent = ''
    render()
    try {
      const result = await preparePayrollPayment(batch.id, part.id)
      if (!disposed && sequence === pollSequence) applyBatch(result)
    } catch (cause) {
      if (!disposed && sequence === pollSequence) {
        error.textContent = cause instanceof Error ? cause.message : '获取付款码失败'
      }
    } finally {
      if (!disposed && sequence === pollSequence) {
        autoQrId = undefined
        render()
        schedule()
      }
    }
  }

  async function pollStatus(): Promise<void> {
    const sequence = ++pollSequence
    try {
      const result = await readPayroll(batch.id)
      if (disposed || sequence !== pollSequence) return
      allowAutoPrepare = true
      applyBatch(result)
      render()
      maybeShowPayment()
    } catch {
      // The next poll retries transient status errors.
    } finally {
      if (sequence === pollSequence) schedule()
    }
  }

  function refresh(): Promise<void> {
    return perform(async () => {
      const result = await readPayroll(batch.id)
      allowAutoPrepare = true
      return result
    })
  }

  function applyBatch(result: PayrollBatch): void {
    batch = keepPayrollPaymentCredentials(batch, result)
    if (batch.gateway_batches.some((part) => part.payment_qr_png_base64)) autoQrReady = true
    if (!batch.active || payrollQrRefreshStopped(batch.gateway_batches.find((part) => part.id === awaitingQr))) {
      awaitingQr = undefined
    }
    onUpdate(batch)
  }

  async function perform(
    action: () => Promise<PayrollBatch>, progress?: typeof paymentProgress,
  ): Promise<void> {
    if (busy || disposed) return
    // Ignore status reads that started before this action.
    ++pollSequence
    clearTimeout(timer)
    autoQrId = undefined
    busy = true
    paymentProgress = progress
    error.textContent = ''
    render()
    try {
      const result = await action()
      if (disposed) return
      applyBatch(result)
      showing = false
    } catch (cause) {
      if (!disposed) error.textContent = cause instanceof Error ? cause.message : '请查看原付款单状态'
    } finally {
      busy = false
      paymentProgress = undefined
      if (!disposed) {
        render()
        schedule()
        maybeShowPayment()
      }
    }
  }

  function cleanup(): void {
    disposed = true
    clearTimeout(timer)
  }

  autoQrReady = initial.gateway_batches.some((part) => part.payment_qr_png_base64)
  render()
  schedule()
  maybeShowPayment()
  return cleanup
}

export function mountLegacyPreview(
  host: HTMLElement,
  selection: PayrollSelection,
  onUpdate: (batch: PayrollBatch) => void,
): () => void {
  let disposed = false
  let stopBatch: (() => void) | undefined
  let requestId: string | undefined
  const error = textElement('p', '')
  error.setAttribute('role', 'alert')
  const body = document.createElement('div')
  body.append(statusMessage('正在核对待结报酬…'))
  host.replaceChildren(error, body)

  function openBatch(batch: PayrollBatch): void {
    onUpdate(batch)
    stopBatch = mountLegacyBatch(body, batch, onUpdate, true)
  }

  async function load(): Promise<void> {
    try {
      const preview = await previewPayroll(selection)
      if (disposed) return
      if ('existing_batch_id' in preview) {
        const batch = await readPayroll(preview.existing_batch_id)
        if (!disposed) openBatch(batch)
        return
      }
      const fingerprint = preview.fingerprint
      const payer = document.createElement('select')
      payer.setAttribute('aria-label', '付款支付宝')
      payer.append(new Option('请选择付款支付宝', '', true, true))
      preview.payers.forEach((item) => payer.append(new Option(`${item.real_name}（${item.account_masked}）`, String(item.id))))
      if (preview.payers.length === 1) payer.selectedIndex = 1
      let sending = false
      const confirm = button('确认制单', () => { void submit() })
      confirm.disabled = !payer.value || !preview.transfers.length
      payer.onchange = () => { confirm.disabled = !payer.value || !preview.transfers.length }
      body.replaceChildren(textElement('p', `${payrollCategoryLabel(selection.category)} · 实付合计 ${preview.total} 元`), payer)
      if (preview.transfers.length) {
        const rows = preview.transfers.map(payrollRecipientTotals).map((recipient) => [
          recipient.recipient_name, recipient.account_masked, recipient.bonus, recipient.deduction,
          recipient.adjustment, recipient.amount,
        ])
        body.append(table(['收款人', '收款支付宝', '生效绩效', '扣点', '未结调整', '实付金额'], rows))
      }
      if (!preview.payers.length) body.append(textElement('p', '没有已授权且可用的付款支付宝。'))
      const progress = statusMessage(PAYROLL_CREATING_HINT)
      progress.hidden = true
      body.append(confirm, progress)
      async function submit(): Promise<void> {
        if (sending || disposed || !payer.value) return
        sending = true
        confirm.disabled = true
        confirm.textContent = '正在制单…'
        progress.hidden = false
        payer.disabled = true
        error.textContent = ''
        try {
          requestId ??= crypto.randomUUID()
          const result = await createPayroll({ ...selection, fingerprint, payer_id: Number(payer.value), request_id: requestId })
          if (!disposed) openBatch(result)
          else onUpdate(result)
        } catch (cause) {
          if (!disposed) error.textContent = cause instanceof Error ? cause.message : '制单结果未确认，请用本次选择重试'
        } finally {
          sending = false
          confirm.disabled = false
          confirm.textContent = '确认制单'
          progress.hidden = true
        }
      }
    } catch (cause) {
      if (disposed) return
      body.textContent = ''
      error.textContent = cause instanceof Error ? cause.message : '读取结算预览失败'
    }
  }

  void load()
  return () => { disposed = true; stopBatch?.() }
}

const legacyPayrollStatusColors: Record<string, string> = {
  preparing: '#585858',
  ready: '#E03636',
  processing: '#219227',
  needs_review: '#ff6a00',
  partial: '#ff6a00',
  succeeded: '#0094ff',
  failed: '#ff0000',
  closed: '#585858',
}

export function mountLegacyRecords(
  host: HTMLElement,
  side: HTMLElement,
  own: boolean,
  initialId?: string,
): () => void {
  let disposed = false
  let page = 1
  let loading = false
  let detailRequest = 0
  let stopBatch: (() => void) | undefined
  const actions = document.createElement('div')
  const content = document.createElement('div')
  const error = textElement('p', '')
  error.setAttribute('role', 'alert')
  host.append(actions, error, content)

  async function detail(id: string, reveal = false): Promise<void> {
    const request = ++detailRequest
    stopBatch?.()
    side.textContent = '正在读取付款单…'
    if (reveal) side.scrollIntoView?.({ block: 'nearest' })
    try {
      const batch = await readPayroll(id)
      if (!disposed && request === detailRequest) stopBatch = mountLegacyBatch(side, batch, () => undefined, true)
    } catch (cause) {
      if (!disposed && request === detailRequest) {
        side.textContent = cause instanceof Error ? cause.message : '读取付款单明细失败'
      }
    }
  }

  async function load(next: number): Promise<void> {
    if (disposed || loading) return
    loading = true
    error.textContent = ''
    try {
      let count
      if (own) {
        const result = await listMyPayroll(next)
        if (disposed) return
        count = result.count
        content.replaceChildren(transferList(result.items))
      } else {
        const result = await listPayroll(next)
        if (disposed) return
        count = result.count
        if (!result.items.length) {
          content.textContent = '暂无付款单'
        } else {
          const list = table(['报酬类别', '实付合计', '付款状态', '操作'], result.items.map((batch) => {
            const status = textElement('span', payrollStatusLabel(batch.status))
            status.className = 'lableb'
            status.style.backgroundColor = legacyPayrollStatusColors[batch.status] ?? '#ff6a00'
            const view = textElement('button', '查看付款单')
            view.type = 'button'
            view.style.cssText = 'padding:0;border:0;background:none;color:#009688;font:inherit;cursor:pointer'
            view.onclick = (event) => {
              event.stopPropagation()
              void detail(batch.id, true)
            }
            return [payrollCategoryLabel(batch.category), `${batch.total} 元`, status, view]
          }))
          const body = list.tBodies.item(0)
          for (const [index, batch] of result.items.entries()) {
            const row = body?.rows.item(index)
            if (!row) continue
            row.style.cursor = 'pointer'
            row.onclick = () => { void detail(batch.id, true) }
          }
          content.replaceChildren(list)
        }
      }
      page = next
      actions.replaceChildren(button('刷新记录', () => { void load(page) }))
      if (count > 25) {
        const previous = button('上一页', () => { void load(page - 1) })
        const following = button('下一页', () => { void load(page + 1) })
        previous.disabled = page <= 1
        following.disabled = page * 25 >= count
        actions.append(previous, textElement('span', `第 ${page} 页，共 ${count} 条`), following)
      }
    } catch (cause) {
      if (!disposed) {
        error.textContent = cause instanceof Error ? cause.message : '读取结算记录失败'
        actions.replaceChildren(button('重试', () => { void load(page) }))
      }
    } finally {
      loading = false
    }
  }

  content.textContent = '读取中…'
  void load(page)
  if (!own && initialId) void detail(initialId)
  return () => {
    disposed = true
    stopBatch?.()
  }
}
