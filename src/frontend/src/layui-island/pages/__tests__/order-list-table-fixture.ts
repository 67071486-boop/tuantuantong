type OrderRow = { order_no: string }

type TableOptions = {
  before?: () => void
  done: (response: { count: number; data: readonly OrderRow[] }, page: number, count: number) => void
}

export function renderOrderRows(
  root: HTMLElement,
  renderOptions: unknown,
  rows: readonly OrderRow[],
  page = 1,
): HTMLInputElement[] {
  const options = renderOptions as TableOptions
  options.before?.()
  root.querySelector('.layui-table-view')?.remove()
  const view = document.createElement('div')
  view.className = 'layui-table-view'
  view.innerHTML = '<div class="layui-table-header"><input type="checkbox" name="layTableCheckbox"></div>'
    + '<div class="layui-table-main layui-table-body"><table><tbody></tbody></table></div>'
  root.appendChild(view)
  const body = view.querySelector('tbody')!
  const boxes = rows.map(({ order_no }, index) => {
    const row = body.insertRow()
    row.dataset.index = String(index)
    const box = document.createElement('input')
    box.type = 'checkbox'
    box.name = 'layTableCheckbox'
    row.insertCell().appendChild(box)
    const line = document.createElement('div')
    line.className = 'orderLine'
    line.id = `order_${order_no}`
    row.insertCell().appendChild(line)
    return box
  })
  options.done({ count: rows.length, data: rows }, page, rows.length)
  return boxes
}
