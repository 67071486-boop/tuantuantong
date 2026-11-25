import { expect, test } from '@playwright/test'

const orderNo = '20261112000000000011'
const order = {
  order_no: orderNo, source_order_no: '', source: '测试来源', taker_name: '', order_type: '',
  status: 'IN_PROGRESS', game: 1, game_name: '测试游戏', server: null, project_text: '测试项目',
  total_price: '60.00', refund_fee: '0.00', customer_phone: '', is_urgent: false,
  created_at: '2026-11-12T10:00:00+08:00', game_role_name: '', customer_ww: '', hall_price: '45.00',
  sales_agent: '', created_by: '', remark: '合成备注', net_received: '60.00', hall_publisher_name: '',
  completed_at: null, time_requirement: '', payment_status: 'UNSETTLED', payment_method: '',
  content: '测试代练内容', demand: '', game_account: 'play-fixture', game_sub_account: '',
}
const user = {
  id: 1, username: 'localfake', display_name: '本地测试', is_tenant_admin: true, is_account_admin: true,
  roles: [], permissions: [], menu_permissions: [], game_ids: [1], identity_plane: 'tenant_a',
}

test.beforeEach(async ({ page, baseURL }) => {
  await page.setViewportSize({ width: 1100, height: 800 })
  await page.route(new URL('/api/**', baseURL).href, async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/\/$/, '')
    const emptyPage = { items: [], total: 0, page: 1, page_size: 50 }
    let data: unknown = emptyPage
    if (path === '/api/auth/me') data = { user }
    else if (path === '/api/business/orders') data = { ...emptyPage, items: [order], total: 1, summary: null }
    else if (path === '/api/business/orders/' + orderNo) data = order
    else if (path === '/api/business/games') data = { ...emptyPage, items: [{ id: 1, name: '测试游戏' }], total: 1 }
    else if (path === `/api/wecom/orders/${orderNo}/groups`) data = {
      groups: [{
        chat_id: 'test-group', name: '测试企微群', owner: '1',
        members: Array.from({ length: 14 }, (_, index) => ({ userid: String(index + 1), name: `测试成员 ${index + 1}` })),
      }],
      too_many: false, next_after: null,
    }
    else if (path === `/api/wecom/orders/${orderNo}/group-qr`) data = { status: 'unavailable', qr_url: '', message: '测试群码不可用' }
    else if (path === `/api/wecom/orders/${orderNo}/chat/groups`) data = {
      enabled: true, groups: [{ chat_id: 'test-group', name: '测试企微群', match: 'linked', proof: 'test-proof' }], complete: true,
    }
    else if (path === `/api/wecom/orders/${orderNo}/chat/signature`) data = {
      verified: true, corp_id: 'test-corp', agent_id: 1, timestamp: 1, nonce: 'test-nonce',
      corp_signature: 'test-corp-signature', agent_signature: 'test-agent-signature',
    }
    else if (path === `/api/wecom/orders/${orderNo}/chat/messages`) data = {
      messages: [{ msgid: 'test-message', secret_key: 'test-key', sent_at: '2026-11-12T10:00:00+08:00' }], next_before: 1,
    }
    await route.fulfill({ json: { code: 0, msg: '', data } })
  })

  await page.goto('/v2/order-manage')
})

test('短视口下编辑订单可以上下滚动，且不显示滚动条', async ({ page }) => {
  await page.getByRole('button', { name: '编辑订单', exact: true }).click({ timeout: 10_000 })
  const dialog = page.getByRole('dialog', { name: /^编辑订单/ })
  const frame = dialog.locator('.el-dialog')
  const body = dialog.locator('.el-dialog__body')
  const save = dialog.getByRole('button', { name: '提交保存' })
  const top = dialog.getByRole('button', { name: '手工录入订单' })
  await expect(dialog.getByPlaceholder('可选择上面的项目', { exact: false })).toHaveValue('测试项目')

  for (const height of [440, 200]) {
    await test.step(`窗口高度 ${height}px`, async () => {
      await page.setViewportSize({ width: 1100, height })
      await expect(top).toBeInViewport({ ratio: 1 })
      await expect(save).not.toBeInViewport()

      await frame.hover({ position: { x: 100, y: 100 } })
      await page.mouse.wheel(0, 3200)
      await expect(save).toBeInViewport({ ratio: 1 })
      await save.click({ trial: true })
      await expect(body).toHaveCSS('scrollbar-width', 'none')
      await expect(frame).toHaveCSS('scrollbar-width', 'none')

      await page.mouse.wheel(0, -3200)
      await expect(top).toBeInViewport({ ratio: 1 })
    })
  }
})

test('极矮窗口的订单详情仍可滚动查看内容', async ({ page }) => {
  await page.getByRole('link', { name: orderNo, exact: true }).press('Enter')
  const dialog = page.getByRole('dialog', { name: `${orderNo}订单详情` })
  const content = dialog.getByText('测试代练内容', { exact: true })
  const pane = dialog.locator('.dtl-face-content')
  await expect(content).toBeVisible()
  await page.setViewportSize({ width: 1100, height: 200 })

  await expect(content).not.toBeInViewport()
  await expect(pane).toHaveCSS('scrollbar-width', 'none')
  await dialog.locator('.el-dialog').hover({ position: { x: 300, y: 100 } })
  await expect(async () => {
    await page.mouse.wheel(0, 800)
    await expect(content).toBeInViewport({ ratio: 1, timeout: 500 })
  }).toPass({ timeout: 4_000 })
})

test('普通窗口的小弹窗在内容区居中且操作可达', async ({ page }) => {
  await page.getByRole('button', { name: '发到内部接单大厅', exact: true }).click({ timeout: 10_000 })
  const dialog = page.getByRole('dialog', { name: '发到内部接单大厅' })
  await expect(dialog.getByRole('button', { name: '确定发单' })).toBeInViewport({ ratio: 1 })
  await expect.poll(() => dialog.locator('.el-dialog').evaluate((element) => {
    const box = element.getBoundingClientRect()
    const overlay = element.parentElement!.getBoundingClientRect()
    return Math.abs(box.top - overlay.top - (overlay.bottom - box.bottom))
  })).toBeLessThan(2)
  await dialog.getByRole('button', { name: '取消' }).click()
  await expect(dialog).toBeHidden()
})

test('矮窗口可滚动企微群资料和已加载的聊天记录', async ({ page }) => {
  await page.route('https://open.work.weixin.qq.com/wwopen/js/jwxwork-1.0.0.js', (route) => route.fulfill({
    contentType: 'application/javascript',
    body: `window.ww = {
      register() {}, async initOpenData() {}, createWWLoginPanel() {},
      async createScrollViewContext() { return { scrollIntoView() {} } },
      createOpenDataFrameFactory() { return {
        createOpenDataFrame({ el: mount, handleMounted, handleUpdated }) {
          const el = document.createElement('iframe')
          mount.append(el)
          queueMicrotask(() => { handleMounted(); handleUpdated() })
          return {
            el, dispose() { el.remove() },
            async setData({ messages }) {
              el.srcdoc = messages.length ? '<p>测试聊天消息</p><div style="height:800px"></div>' : ''
              handleUpdated()
            },
          }
        },
      } },
    }`,
  }))
  await page.getByRole('link', { name: orderNo, exact: true }).press('Enter')
  const dialog = page.getByRole('dialog', { name: `${orderNo}订单详情` })
  await dialog.getByRole('tab', { name: '企微群聊' }).click()
  const details = dialog.getByRole('complementary', { name: '群资料和二维码' })
  const groupName = details.getByRole('heading', { name: '测试企微群' })
  const retry = details.getByRole('button', { name: '重试' })
  const archive = dialog.locator('.wecom-chat')
  const pane = dialog.locator('.dtl-face-content:has(.wecom-chat)')
  const frame = archive.getByTitle('企微群聊聊天记录')
  const message = frame.contentFrame().getByText('测试聊天消息')
  await expect(message).toBeInViewport()
  await expect(retry).toBeInViewport()

  const detailsBox = (await details.boundingBox())!
  const archiveBox = (await archive.boundingBox())!
  expect(detailsBox.x + detailsBox.width).toBeLessThan(archiveBox.x)
  expect(detailsBox.y).toBeCloseTo(archiveBox.y, 0)
  await message.hover()
  await page.mouse.wheel(0, 240)
  await expect.poll(() => message.evaluate(() => window.scrollY)).toBeGreaterThan(0)
  await message.evaluate(() => window.scrollTo(0, 0))

  await page.setViewportSize({ width: 1100, height: 440 })
  await expect(retry).not.toBeInViewport()
  await details.hover({ position: { x: 36, y: 6 } })
  await page.mouse.wheel(0, 900)
  await expect(retry).toBeInViewport({ ratio: 1 })
  await retry.click({ trial: true })

  for (const viewport of [{ width: 1100, height: 200 }, { width: 850, height: 440 }, { width: 850, height: 200 }]) {
    await test.step(`窗口 ${viewport.width} × ${viewport.height}`, async () => {
      await page.setViewportSize(viewport)
      await groupName.scrollIntoViewIfNeeded()
      await expect(retry).not.toBeInViewport()
      await retry.scrollIntoViewIfNeeded()
      await expect(retry).toBeInViewport({ ratio: 1 })
      await retry.click({ trial: true })
      await expect(details).toHaveCSS('scrollbar-width', 'none')
      await expect(pane).toHaveCSS('scrollbar-width', 'none')
      await expect(archive).toHaveCSS('overflow-y', 'hidden')

      await expect.poll(async () => (await archive.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(260)
      await expect.poll(async () => (await frame.boundingBox())?.height ?? 0).toBeGreaterThanOrEqual(120)
      await frame.scrollIntoViewIfNeeded()
      if (viewport.width === 850 && viewport.height === 200) {
        // 中心对齐高于可视区的 iframe 会遮住首行，滚外层空白把它带回可视区。
        const bounds = (await pane.boundingBox())!
        await page.mouse.move(bounds.x + 6, Math.max(0, bounds.y) + 8)
        await expect(async () => {
          await page.mouse.wheel(0, -20)
          await expect(message).toBeInViewport({ ratio: 1, timeout: 400 })
        }).toPass({ timeout: 2_500 })
      }
      await expect(message).toBeInViewport({ ratio: 1 })
    })
  }
})
