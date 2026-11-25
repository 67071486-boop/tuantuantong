/**
 * 【管理订单截图】弹层（island key `order-screenshots-manage`）——对标 `/Order/PicMange`。
 *
 * 由订单管理页 / 我的订单页的 `.mangeImg` 用 `layer.open({type:2, …})` 打开（层参数逐页取
 * **该页自己的**对标 JS）。
 *
 * ## 证据出处（值全部从证据件取）
 *
 * | 项 | 出处 |
 * |---|---|
 * | 页内 `<style>`（含核心的 `li img { width:200px; height:160px }` 与 `li { height:248px }`）+ 表单标记 | `public/replica/order-screenshots-manage.html` ← `.tools/recon/media-storage/Order__PicMange.html`（**不进 git**）L51-125 |
 * | 页标题 | 同上的 `<title>`（L5）= `管理订单截图` |
 * | 删除确认文案（**逐字**） | `.tools/recon/media-storage/assets_JS2019_Order_picmange.js` L27 |
 * | 单张 `<li>` 的结构（时间 / 备注 `.picnote` / 删除 / 勾选框） | `Order__PicMange.html` L96-113 |
 * | 全选 / 批量删除 / 打包下载三处行为 | `picmange.js` L66-72 / L73-116 / L60-65 |
 * | 大图浏览 | `picmange.js` L8-11：`layer.photos({photos:'.imgList', anim:5})` |
 *
 * ## 与对标**有意不同**（逐条，实施后同步进 `FRONTEND-REPLICA-STANDARD` §7）
 *
 * 1. **顶部那条 90 天提示的 `blockquote` 整条不渲染**：它只有一句
 *    「提示：距离上传时间超过90天的将会被自动删除，会出现无法浏览的情况」，是 A3 明确要
 *    **不写**的那类假承诺（我们永久保留；spec §4-5「不显示『截图有效时间 90 天』提示」）。
 *    代价：页面上少一个 `layui-elem-quote` 灰底块。
 * 2. **「由{上传人}上传」那一行不渲染**：`GET …/screenshots` 的载荷只给 `uploaded_by`
 *    （用户 **id**），没有显示名；拿 id 冒充人名是假陈述，故**那一段整段不拼**
 *    （⛔ 不编名字、也不用当前登录人冒充）。`<li>` 其余四段（时间 / 备注 / 删除 / 勾选）照抄。
 * 3. **批量删除是逐张 `DELETE /api/business/orders/screenshots/<id>`**（一张一发），
 *    不是对标的「`ids` 逗号串发一次 `/OrderAjax/DelPicMultiple`」——本仓没有批量删除端点。
 *    代价：勾 20 张就 20 发请求；语义等价（后端逐张记一条操作日志，与单删同一条路径）。
 * 4. **打包下载照任务书用 `window.open`**（对标 `picmange.js:61` 也是 `window.open`），
 *    之后按对标 L62-63 关掉承载自己的那个层。⚠️ **已知代价**：`window.open` 拿不到响应体
 *    ⇒ 没有 `order.download_screenshots_zip`(132) 节点时，后端回的统一信封会被浏览器摊在
 *    新标签页里（不是一句可读提示）。同仓导出之所以改走「先请求再落 Blob」是**另一个**原因
 *    （同一个 URL 两种形状，见 `api/performances.ts:492`），本端点成功时形状唯一。
 * 5. **`img_type` 不参与本页渲染**（对标也不显示）：每张图是订单截图还是群聊二维码，
 *    在证据件的 `<li>` 里**没有**对应位置（只有备注那一格）⇒ 照抄，不自己加标记。
 * 6. **不用 `form.on('submit(importBtn)' / 'submit(delbtns)')`**（证据件那两颗按钮都带
 *    `lay-submit`）：那是 `layui-form` 的提交链路，会对整页跑一次 `validate()`
 *    （`user-myinfo.ts` 的 E-0074 同族风险），而本页三处动作与表单校验无关 ⇒
 *    走**捕获态 click 委托**（`mountArtifact` / `user-myinfo` 同款）。标记里的
 *    `lay-filter` 一字未改。全选那一格走 `change` 委托（`click` 的捕获阶段读不到**切换后**的
 *    `checked`，这是一个会静默算错的坑）。
 * 7. ⭐ **那半个 `.viewPic` 处理器不复刻**（M-6 登记，2026-09-25 评审）：证据件
 *    `picmange.js:21-24` 给 `.viewPic` 绑了 `viewOrderPic($('#orderId').val(), $('#taskId').val())`，
 *    但 `viewOrderPic` 在该页的三个依赖（`layui.js` / `common.js` / `picmange.js`，见
 *    证据件 L20-21/L127）里**从未定义**（三处 `grep` 全 0 命中）⇒ 那是一段点了就
 *    `ReferenceError` 的**死代码**。本页只留证据件顶部那条真在干活的
 *    `layer.photos({ photos: '.imgList', anim: 5 })`（`picmange.js:8-11`）；点大图仍按对标行为放大浏览。
 * 8. **对标的 `re` 防连点标志不复刻**：`picmange.js:5` 的 `var re = true` 是一面**跨动作的
 *    会话级旗标**（`:30-32` / `:87-89` 在读它、拦掉同一时刻的第二个删除请求，两组 ajax 回调
 *    再把它置回真）。本页的删除是 `layer.confirm` → 逐张 `DELETE` → `load()` 重取：确认层本身
 *    就把用户这一下串行化了，而并发点两行**无害**（两发 DELETE 各自幂等，重取后列表就是真状态）。
 *    ⛔ 不逐字照抄 —— 按 `AGENTS.md` §5-D 的问法，照抄一面跨动作的会话级旗标要**另外接管它的
 *    复位时机**（对标那份住在它自己的 `layui.use` 闭包里），而本页不需要它（同族 E-0079）。
 * 9. **`:143` 的兜底错误文案是自拟的**（M-2 登记）：「操作失败，请稍后重试」。本仓统一通道
 *    `request()` 成功只回 `data`、失败时抛出的 `Error.message` 已是**服务端 `msg` 或我们的
 *    映射文案**；这句只在「抛出的不是 `Error`（拿不到 message）」时兜底——对标对应位置弹的是
 *    它服务端 `msg`。
 *
 * ⚠️ **三条 island 陷阱**（`AGENTS.md` §5-G）：① 类名与选择器分成两个常量；② 以 `(` 开头的
 * 语句一律前置分号；③ **先 `await bootstrapIsland(...)` 挂样式、再塞标记**，而
 * `initIslandElements(layui)` **必须**留在塞标记之后。
 */
import { deleteScreenshot, downloadScreenshotZip, fetchScreenshots, screenshotZipUrl, type ScreenshotItem } from '@/api/screenshots'
import { orderFinanceDirectConfigured } from '@/api/orderFinanceDirect'
import { fetchArtifactHtml } from '@/layui/artifactPage'
import { escapeHtml } from '@/layui-island/escape'
import { closeIslandLayer, type LayerHostWindow } from '@/layui-island/layerHost'
import {
  bootstrapIsland, initIslandElements, islandMsg,
  type IslandContext, type IslandPage,
} from '@/layui/island'

/** island 页 key ＝ 文件名。 */
export const PAGE_KEY = 'order-screenshots-manage'
/** 证据件（页内样式 + 处理过的 body 标记，改动见那份文件顶部）。 */
export const ARTIFACT_URL = 'replica/order-screenshots-manage.html'
/** 页标题：对标 `Order__PicMange.html:5` 的 `<title>管理订单截图</title>`。 */
export const PAGE_TITLE = '管理订单截图'
/** 单号走 island 的 URL 参数。 */
export const ORDER_NO_PARAM = 'order_no'

/**
 * 删除确认文案（**逐字**取自 `assets_JS2019_Order_picmange.js:27`，单删与批删**同一句**）。
 * ⛔ 一个标点都不许改。
 */
export const DELETE_CONFIRM_TEXT = '确定要删除吗？提示：一旦删除，将不可恢复！'

// ⚠️ E-0061 陷阱①：类名/属性值与选择器分成两个常量。
const CHECK_ALL_ID = 'checkall'
const CHECK_ALL = `#${CHECK_ALL_ID}`
const CHECK_BOX = 'input[name="ck"]'
const IMAGE_LIST = '.imgList'
const DELETE_LINK = 'a.del'
const ZIP_BUTTON = 'button[lay-filter="importBtn"]'
const BATCH_DELETE_BUTTON = 'button[lay-filter="delbtns"]'
const VIEW_LINK = 'a.viewPic'

/**
 * 用户可见文案。`noSelection` 逐字取自 `picmange.js:82`（我们**没有**服务端 `msg` 可弹 ⇒
 * 删除成功/失败只靠「那一行消失」+ 失败时后端 `msg` 原样回显，见文件头与 spec §5）。
 */
export const TEXTS = {
  /** `picmange.js:82`：一张都没勾就点批量删除。 */
  noSelection: '请勾选图片再批量删除',
} as const

/** 时间渲染成对标那种 `2026/9/25 14:38:16`（不补前导零，与证据件 L101 一致）。 */
export function formatShotTime(iso: string): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return iso
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${at.getFullYear()}/${at.getMonth() + 1}/${at.getDate()} ` +
    `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`
}

/**
 * 一张图的 `<li>`（结构逐字照证据件 L96-113；`由{上传人}上传` 那一段不拼，见文件头第 2 条）。
 *
 * ⚠️ `original_name` / `remark` 是上传方的自由文本 ⇒ 正文与属性**两处**都过 `escapeHtml`
 * （与 `order-orderlist.ts` 里各自由文本列的处置同口径）。
 */
export function itemHtml(item: ScreenshotItem): string {
  const time = formatShotTime(item.created_at)
  const remark = escapeHtml(item.remark)
  const title = escapeHtml(`上传于${time}，备注：${item.remark} 点击可查看大图`)
  const url = escapeHtml(item.url)
  const thumbnail = escapeHtml(item.thumbnail_url || item.url)
  return (
    '<li>' +
    `<a href="#" class="viewPic" title="${title}">` +
    `<img src="${thumbnail}" layer-src="${url}" loading="lazy" title="${title}" />` +
    '</a>' +
    `<div>${escapeHtml(time)}</div>` +
    `<div class="picnote"><span title="${remark}">${remark}</span></div>` +
    '<div>' +
    `<a href="javascript:void(0)" data-id="${item.id}" class="layui-btn layui-btn-primary layui-btn-xs del">删除</a>` +
    `<input type="checkbox" name="ck" lay-skin="primary" value="${item.id}" title="选择">` +
    '</div>' +
    '</li>'
  )
}

function errorText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : '操作失败，请稍后重试'
}

export async function mount(root: HTMLElement, _ctx: IslandContext): Promise<() => void> {
  document.title = PAGE_TITLE

  // ① 先挂样式（`layui.css` 是 bootstrapIsland 运行期插的 `<link>`）——顺序反了会 FOUC（陷阱③）
  const layui = await bootstrapIsland(['form', 'layer'])
  const form = layui.form as unknown as { render: (type?: string) => void }
  const layer = layui.layer as unknown as Record<string, unknown>
  const msg = (text: string): void => islandMsg(layer, text)

  const orderNo = new URLSearchParams(window.location.search).get(ORDER_NO_PARAM) ?? ''

  // ② 再塞标记，③ 然后才 `initIslandElements`
  root.innerHTML = await fetchArtifactHtml(ARTIFACT_URL)
  initIslandElements(layui)

  /** 按后端列表重渲染 `<ul class="imgList">`，并把新一批 `<img>` 交给 `layer.photos`（L8-11）。 */
  const renderList = (items: ScreenshotItem[]): void => {
    const list = root.querySelector<HTMLElement>(IMAGE_LIST)
    if (!list) return
    list.innerHTML = items.map(itemHtml).join('')
    // ⚠️ **`#checkall` 必须跟着复位**：它在 `.imgList` **外面**，重渲染不会碰到它
    // ⇒ 不复位就会出现「全选还勾着、底下一条都没勾」的假状态（对标整页 `location.reload()`，
    // 所以它那边不存在这个问题——这是一条**换承载方式才实体化**的状态，见 `E-0079`）。
    const all = root.querySelector<HTMLInputElement>(CHECK_ALL)
    if (all) all.checked = false
    form.render('checkbox')
    ;(layer.photos as (o: unknown) => void)({ photos: IMAGE_LIST, anim: 5 })
  }

  async function load(): Promise<void> {
    if (orderNo === '') {
      renderList([])
      return
    }
    try {
      const data = await fetchScreenshots(orderNo)
      renderList(data.items)
    } catch (e) {
      msg(errorText(e))
    }
  }

  await load()

  /** 被勾选的那些图的 id（`value` 是后端主键）。 */
  const checkedIds = (): number[] =>
    Array.from(root.querySelectorAll<HTMLInputElement>(CHECK_BOX))
      .filter((box) => box.checked)
      .map((box) => Number(box.value))
      .filter((id) => Number.isFinite(id))

  /** 逐张删（见文件头第 3 条），删完重取列表。 */
  async function removeAll(ids: number[]): Promise<void> {
    const failures: string[] = []
    await Promise.all(ids.map(async (id) => {
      try {
        await deleteScreenshot(id)
      } catch (e) {
        failures.push(errorText(e))
      }
    }))
    await load()
    if (failures.length > 0) msg(failures[0]!)
  }

  /** 同一句确认文案、同一个出口（单删与批删共用；对标也是同一句，`picmange.js:27`）。 */
  const confirmDelete = (ids: number[]): void => {
    ;(layer.confirm as (text: string, opts: unknown, cb: () => void) => void)(
      DELETE_CONFIRM_TEXT,
      { btn: ['确定', '取消'] },
      () => {
        void removeAll(ids)
      },
    )
  }

  /* ── 三处动作：捕获态 click 委托（见文件头第 6 条） ── */

  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement | null
    if (!target || typeof target.closest !== 'function') return
    const stop = (): void => {
      event.preventDefault()
      event.stopPropagation()
    }
    if (target.closest(BATCH_DELETE_BUTTON)) {
      stop()
      const ids = checkedIds()
      if (ids.length === 0) {
        msg(TEXTS.noSelection)
        return
      }
      confirmDelete(ids)
      return
    }
    if (target.closest(ZIP_BUTTON)) {
      stop()
      const close = () => closeIslandLayer(window as unknown as LayerHostWindow)
      if (!orderFinanceDirectConfigured()) {
        window.open(screenshotZipUrl(orderNo), '_blank')
        close()
        return
      }
      void downloadScreenshotZip(orderNo).then(({ blob, filename }) => {
        const url = URL.createObjectURL(blob)
        const link = document.createElement('a')
        link.href = url
        link.download = filename
        link.click()
        setTimeout(() => URL.revokeObjectURL(url), 1000)
        close()
      }, (error: unknown) => msg(error instanceof Error ? error.message : '订单截图下载失败'))
      return
    }
    const del = target.closest<HTMLElement>(DELETE_LINK)
    if (del) {
      stop()
      const id = Number(del.dataset.id ?? '')
      if (Number.isFinite(id)) confirmDelete([id])
      return
    }
    // 大图浏览由证据件里那条 `layer.photos` 承担；这里只拦掉 `href="#"` 的默认跳转。
    if (target.closest(VIEW_LINK)) event.preventDefault()
  }

  /**
   * 全选走 `change`（**不是** click）：捕获阶段读到的 `checked` 还是**切换前**的值，
   * 拿它去同步每一行会把全选算反 —— 一个不会报错、只会静默算错的坑。
   */
  const onChange = (event: Event): void => {
    const target = event.target as HTMLInputElement | null
    if (!target || typeof target.closest !== 'function') return
    if (!target.closest(CHECK_ALL)) return
    const boxes = root.querySelectorAll<HTMLInputElement>(CHECK_BOX)
    boxes.forEach((box) => {
      box.checked = target.checked
    })
    form.render('checkbox')
  }

  root.addEventListener('click', onClick, true)
  root.addEventListener('change', onChange)

  return () => {
    root.removeEventListener('click', onClick, true)
    root.removeEventListener('change', onChange)
    root.innerHTML = ''
  }
}

export default { mount } satisfies IslandPage
