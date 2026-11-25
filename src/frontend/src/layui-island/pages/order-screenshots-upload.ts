/**
 * 【上传进度图】弹层（island key `order-screenshots-upload`）——对标 `/Order/UploadPic`。
 *
 * 由订单管理页 / 我的订单页的 `.upImg` 用 `layer.open({type:2, …})` 打开，参数逐页取
 * **该页自己的**对标 JS（两页不同，见那两页里的注释）。
 *
 * ## 证据出处（值全部从证据件取，⛔ 不许凭 layui 默认值或记忆猜）
 *
 * | 项 | 出处 |
 * |---|---|
 * | 页内 `<style>` + 表单标记 | `public/replica/order-screenshots-upload.html` ← `.tools/recon/media-storage/Order__UploadPic.html`（**不进 git**）L52-165 |
 * | 上传弹层标题 | 同上 `<title>`（L5）= `上传进度图` |
 * | 18 MB / 5 MB / 四档画质 | `Order_uploadpicnew.js` L28-29 / L58-70（落在 `@/layui-island/imageCompress`，那个文件逐条标了行号） |
 * | 已上传表格 4 列与 `limit:100` | 同上 L337-358 |
 * | 行内两颗按钮 | 同上 `#baroperating` 模板（L152-155），`lay-event` = `viewPic` / `delitem` |
 * | 浏览大图那一跳的层参数 | 同上 L376-384（`type:1` / `title:false` / `closeBtn:0` / `area:['85%']` / `skin:'layui-layer-nobg'` / `shadeClose:true` / `<img width="580px" height="420px">`） |
 * | 进度条推进与收尾 | 同上 L42-43 / L307-317（`element.progress('progressBar', …)`，100% 后 800ms 归零并隐藏） |
 * | 几句用户可见文案 | 同上 L99 / L430 / L440 / L402 / L386（逐条见下面 `TEXTS`） |
 *
 * ## 与对标**有意不同**（逐条，实施后同步进 `FRONTEND-REPLICA-STANDARD` §7）
 *
 * 1. **顶部那句「提示：截图有效时间为 90 天，90 天后将自动失效……」只删「90 天」那半句，
 *    其余逐字保留**（业务方 2026-09-25 追加裁定 `D-0153` ④，证据件 L106-108）：永久保留是我们
 *    的事实 ⇒ 那半句照旧**一个字都不写**（A3），但「如果出现上传不了…再次上传会覆盖原二维码！」
 *    那半句已按证据件原标签与原内联样式补回。连带删掉的还有 4 个隐藏域（`orderId`/`gameAccId`/
 *    `sid`/`taskId`：单号改从 URL 参数取，`sid` 是对标租户标识）。
 * 2. **两颗行内按钮的图标换成 stock**（对标用私有 iconfont `layui-xiugaimima1`，我们没有；
 *    而且它把「修改密码」的字形复用到「浏览图片」和「删除」上，是它自己的笔误）。
 * 3. **直传是「预签名 PUT」**，不是对标的「表单 POST + policy/signature」——
 *    存储层按 PUT 实现，UX 不变。
 *    ⇒ 三跳顺序是 `ticket` ⇒ `fetch(PUT)` ⇒ **保存时认领**。
 * 4. ⭐ **`img_type` 在「上传那一刻」定下**（`requestScreenshotTicket` 要它来定对象名前缀 `qr.`
 *    与行类型），而对标是把 `imgType` 放到「保存」时才发给服务端（`uploadpicnew.js:455`）。
 *    保存时**只**用它做「群聊二维码仅限一张」的计数闸。⇒ 用户在传完之后再改单选，
 *    已经传上去的那几张**类型不会跟着变**（后端形态决定的代价，如实登记）。
 * 5. **认领按 `screenshot_id` 逐行发**（我们的后端一行一张图），不是对标的
 *    「一次 `imgListStr` 逗号串」；`remark` 取保存那一刻备注框的值、发给每一行。
 * 6. **「浏览图片」看的是内存里的本地对象 URL**（还没保存的图**没有**服务端读链接可签：
 *    后端只在 `GET …/screenshots` 里给**已认领**的行签 URL）⇒ 用 `URL.createObjectURL`；
 *    拿不到对象 URL 时提示照证据件那句「无法预览」（L386）。**零请求**。
 * 7. **「删除」只把该行从待保存列表移除、不发请求**：后端没有「删 `pending` 对象」的端点，
 *  那个对象由 24 小时孤儿清理任务收走。提示照证据件那句「删除成功！」（L402）。
 * 8. **保存成功后是本页清空待保存列表**（不是对标的 `location.reload()`）：整页重载会重建
 *    iframe 并丢掉「刚存了哪几张」的上下文。成功文案是本仓自拟的「保存成功」
 *    ——统一请求通道 `request()` 成功只回 `data`、拿不到服务端 `msg`（同线 `user-myinfo.ts` 的先例）。
 * 9. **超限提示里的数字用 18 / 5**：对标那句是「…控制在单张 **10MB** 以内」（L99），
 *    而它自己的 `maxSize` 就是 18 MB ⇒ 照抄那个数才是说假话。句子形状照抄，只改数。
 * 10. **不用 layui 的 `form.on('submit(savebtn)')`**（证据件那颗按钮带 `lay-submit`）：那是
 *    `layui-form` 的提交链路，会对整页跑一次 `validate()`（`user-myinfo.ts` 的 E-0074 同族风险），
 *    而本页的保存与表单校验无关 ⇒ 走**捕获态 click 委托**（`mountArtifact` / `user-myinfo` 同款）。
 *    标记里的 `lay-filter="savebtn"` 一字未改。
 * 11. ⭐ **两条「已自动过滤N张…」的过滤提示都是自拟文案**（M-2 登记，2026-09-25 评审）：
 *    · `:339`「已自动过滤N张**18MB**以上的图片」——**对标没有对应句**：`uploadpicnew.js:58-60`
 *      对超 `maxSize`（18MB）的文件**只静默 `delete filedata[index]`**，一个字都不提示
 *      （我们**多给**了提示，不是砍掉）；
 *    · `:340`「已自动过滤N张**5MB**以上的图片」——句子的**形状**照 `uploadpicnew.js:88`，
 *      但**砍掉了原句后缀**「，正在准备，请稍候...」与 `{ time: 1000 }`（本页统一 1500ms，
 *      见 `islandMsg`）。两处的 `N` 都是我们自己的计数。
 * 12. **对标那条链上的三个 loading 提示不写**（`uploadpicnew.js:51`「正在处理文件，请稍候...」
 *    / `:90`「准备上传中...」/ `:253`「正在上传文件，请稍候...」，都是 `icon:16` + `shade:0.3`
 *    + `time:0` 的常驻层）：本页只保留底部进度条那一条反馈。**代价（如实）**：没有「正在处理 /
 *    正在上传」的明确等待态，用户只看到进度条推进。
 * 13. **两条兜底错误文案是自拟的**：本页 `:160`「上传失败，请稍后重试」、管理弹层
 *    `order-screenshots-manage.ts:143`「操作失败，请稍后重试」。本仓统一通道 `request()`
 *    成功只回 `data`、失败时抛出的 `Error.message` 已是**服务端 `msg` 或我们的映射文案**；
 *    这两句只在「抛出的不是 `Error`（拿不到 message）」时兜底——对标对应位置弹的是它服务端 `msg`。
 * 14. **`paste` 监听挂在**整个弹层 `root` 上（`:395`），不是证据件那个 `#uploadInput`
 *    （`uploadpicnew.js:117` 是 `$('#uploadInput').on('paste', …)`）⇒ **弹层任意位置** Ctrl+V
 *    都会触发上传。这是**有意放宽**（弹层里没有别的可粘贴输入框），但**与对标不同**：
 *    对标只在那个粘贴框上生效。
 * 15. ⭐ **每单 20 张的本地预检**（业务方 2026-09-25 追加裁定 `D-0153` ①，`SCREENSHOT_LIMIT`）：
 *    对标**没有**任何张数闸（它的 `limit:100` 是表格分页，见 spec §3 的更正）。后端在**签发票据**
 *    与**认领**两处拦（权威，msg 逐字 `单个订单最多上传20张截图，请先删除后再上传`）；前端只是在
 *    挑文件那一跳按「已上传数量（挂载时从**既有列表接口**取，⛔ 不为这一条新开请求）+ 待保存队列」
 *    预检，一张都放不下就**零请求**提示、放得下一部分就只放行前面几张。⛔ 后端真拒了照旧原样显示
 *    它的 `msg`（`failures[0]`），不被前端这句覆盖。**代价（如实）**：那份已上传数量是挂载时的
 *    快照 ⇒ 另一个弹层里删过图、或另一个标签同时传，本页看不到；且后端计数含 `pending` 而列表端点
 *    只回 `ready` ⇒ 预检整体偏**乐观**（详见 `uploadedCount` 的 docstring）。
 *    ⚠️ **两处计数闸都按 `img_type` 数**（2026-09-25 评审 I-2 修复）：`PendingRow` 记着上传那一刻的
 *    类型，所以本会话里已传的群聊二维码**不**占订单截图那 20 个名额，而以 `imgType=1` 保存时也只数
 *    真正的二维码行——⛔ 不许退回 `rows.length`（那是把两类图混成一个数，两处都会误伤）。
 *
 * ⚠️ **三条 island 陷阱**（`AGENTS.md` §5-G）：① 类名与选择器分成两个常量；② 以 `(` 开头的
 * 语句一律前置分号；③ **先 `await bootstrapIsland(...)` 挂样式、再塞标记**，而
 * `initIslandElements(layui)` **必须**留在塞标记之后。
 */
import { claimScreenshot, fetchScreenshots, requestScreenshotTicket } from '@/api/screenshots'
import { fetchArtifactHtml } from '@/layui/artifactPage'
import { putFileToStorage } from '@/layui-island/directUpload'
import { escapeHtml } from '@/layui-island/escape'
import {
  COMPRESSED_MAX_BYTES, MAX_UPLOAD_BYTES, compressImage,
} from '@/layui-island/imageCompress'
import {
  bootstrapIsland, initIslandElements, islandMsg,
  type IslandContext, type IslandPage,
} from '@/layui/island'

/** island 页 key ＝ 文件名（父页拼 URL 用；`page.ts` 的注册表按文件名发现它）。 */
export const PAGE_KEY = 'order-screenshots-upload'
/** 证据件（页内样式 + 处理过的 body 标记，见那份文件顶部的「有意改动」）。 */
export const ARTIFACT_URL = 'replica/order-screenshots-upload.html'
/** 页标题：对标 `Order__UploadPic.html:5` 的 `<title>上传进度图</title>`。 */
export const PAGE_TITLE = '上传进度图'
/** 单号走 island 的 URL 参数（父页 `encodeURIComponent` 后拼上）。 */
export const ORDER_NO_PARAM = 'order_no'

// ⚠️ E-0061 陷阱①：**类名/属性值**与**选择器**分成两个常量（`X_ID` 进模板、`X` 是选择器）。
// 写成 `id=".x"` 或 `class="${SELECTOR}"` 会造出一个永远匹配不到的标记，而
// 「点了没反应」是**静默**的 —— 页测试与 lint 都看不见。
const DROP_WRAP = '.wrap'
const FILE_INPUT_ID = 'fileInput'
const PASTE_BOX_ID = 'uploadInput'
const PASTE_BOX = `#${PASTE_BOX_ID}`
const REMARK_INPUT = '#remark'
const IMG_TYPE_NAME = 'imgType'
const TABLE_ID = 'tableList'
const BAR_TEMPLATE = '#baroperating'
const PROGRESS_BAR_ID = 'progressBar'
const PROGRESS_BAR = `#${PROGRESS_BAR_ID}`
const PROGRESS_FILTER = 'progressBar'
const SAVE_BUTTON = 'button[lay-filter="savebtn"]'

/** 行内两颗按钮的 `lay-event`（证据件 `#baroperating` 模板里的原文）。 */
const VIEW_EVENT = 'viewPic'
const DELETE_EVENT = 'delitem'

/** 允许上传的扩展名（与后端 `core.storage.ALLOWED_EXTS` 同口径；⛔ 前端只是**早挡**）。 */
const ALLOWED_EXTS = ['jpg', 'jpeg', 'png'] as const
/** 扩展名不在白名单时的提示（**逐字**取后端同一句，`screenshot_views.claim` 的 `msg`）。 */
const BAD_EXT_TEXT = '只支持 jpg / jpeg / png'
/** `input[type=file]` 的 `accept`：证据件 `upload.render` 的 `acceptMime`（L36）。 */
const FILE_ACCEPT = 'image/jpeg,image/png'
/** 群聊二维码（`OrderScreenshot.IMG_GROUP_QR`）。 */
const GROUP_QR_TYPE = 1
/**
 * **每单订单截图上限 20 张**（业务方裁定 `D-0153` ①）。
 *
 * 口径：`img_type=0`（订单截图）的行数 ≤ 20，`pending` 与 `ready` **都算**、硬删的不计；
 * **群聊二维码（`img_type=1`）不受这 20 约束**（它仍是「每单 1 张、重传覆盖」）。
 */
const SCREENSHOT_LIMIT = 20
/**
 * 超上限的提示 —— **逐字**取后端同一条（`D-0153` ①：后端在**签发票据**与**认领**两处都拦）。
 *
 * ⚠️ **后端是权威**：`request()` 失败时抛出的 `Error.message` 就是它的 `msg`，
 * 照旧**原样显示**（`failures[0]`）——⛔ 不许拿这一句去覆盖后端的拒绝。
 * 下面这次预检只为**省掉第 21 张的往返**（早反馈、零请求），与后端那句是**同一事实的
 * 两处字面值**，改数值要**同一次**改两处。
 */
const LIMIT_TEXT = '单个订单最多上传20张截图，请先删除后再上传'
/** 100% 之后多久归零并隐藏进度条（证据件 L314-317 的 `setTimeout(…, 800)`）。 */
const PROGRESS_HIDE_DELAY_MS = 800

/**
 * 用户可见文案，**逐字**取自证据件（出处逐条标注）。抽成常量只为让 spec 能钉字面值。
 */
export const TEXTS = {
  /** `uploadpicnew.js:430`：一张都没传就点保存。 */
  noFile: '请先上传图片再进行保存',
  /** `uploadpicnew.js:440`：群聊二维码只能有一张。 */
  groupQrLimit: '群聊二维码仅限一张，再次上传会覆盖原二维码！',
  /** `uploadpicnew.js:402`：删掉一张待保存的图。 */
  deleted: '删除成功！',
  /** `uploadpicnew.js:386`：拿不到可浏览的地址。 */
  noPreview: '无法预览',
  /** `uploadpicnew.js:351`：表格「上传状态」列的绿字。 */
  waitingSave: '上传成功(等待保存)',
  /** 本仓自拟（服务端 `msg` 拿不到；同线 `user-myinfo.ts` 的先例）。 */
  saved: '保存成功',
  /** 没有单号（直接打开本页的 deep-link）——对标不存在这条路径。 */
  noOrder: '缺少订单号，无法上传订单截图',
} as const

/**
 * 一行「已上传、还没认领」的图。`preview_url` 是本地的对象 URL（见「有意不同」第 6 条）。
 *
 * ⚠️ **`img_type` 必须记在这一行上**（2026-09-25 评审 I-2 修复）：`img_type` 在上传那一刻
 * 定下（有意不同第 4 条），而两处计数闸都要**按类型**数——`rows.length` 会把订单截图与
 * 群聊二维码混成一个数（后果：二维码占掉订单截图的名额 / 反过来误报「二维码仅限一张」）。
 */
type PendingRow = {
  name: string
  size: number
  screenshot_id: number
  img_type: number
  preview_url: string
}

/** 取扩展名（小写、去点）。 */
function extOf(name: string): string {
  return (name.split('.').pop() ?? '').toLowerCase()
}

function errorText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : '上传失败，请稍后重试'
}

/** 本地对象 URL（拿不到就空串——调用方按证据件那句「无法预览」提示）。 */
function previewUrlOf(file: File): string {
  try {
    return typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : ''
  } catch {
    return ''
  }
}

/**
 * 释放一行的对象 URL。
 *
 * ⚠️ **谁负责复位**（`AGENTS.md` §5-D / `E-0079` 的问法）：`createObjectURL` 出来的 blob URL
 * 会把那块内存**钉住直到页面卸载**，弹层却可能来回开关 ⇒ 每一行在**离开列表时**都要显式
 * `revokeObjectURL`（认领成功、被删掉、以及整页卸载三条路都要走到）。
 */
function releasePreview(row: PendingRow): void {
  if (row.preview_url === '') return
  try {
    URL.revokeObjectURL?.(row.preview_url)
  } catch {
    // 释放失败不影响任何用户可见行为（浏览器不支持 / URL 已被释放）。
  }
}

/** **已上传表格**的 4 列（证据件 `uploadpicnew.js:337-356` 的 `cols`，逐条对齐）。 */
function buildCols(): unknown[] {
  return [[
    {
      field: 'name', title: '文件名称', align: 'left', unresize: true,
      templet: (res: PendingRow): string => `<span>${escapeHtml(res.name)}</span>`,
    },
    {
      field: 'size', title: '大小', align: 'center', sort: false, unresize: true,
      templet: (res: PendingRow): string =>
        `<span>${(res.size / 1024 / 1024).toFixed(2)}MB</span>`,
    },
    {
      field: 'remark', title: '上传状态', align: 'center', sort: false, unresize: true,
      templet: (): string => `<span style="color:#5FB878">${TEXTS.waitingSave}</span>`,
    },
    // `fixed: ''` 是对标原样（空串 = 不固定），照抄。
    { fixed: '', title: '操作', width: 150, toolbar: BAR_TEMPLATE, unresize: true },
  ]]
}

/** 从剪贴板/拖拽事件里取图片文件（只认 `image/*`）。 */
function imageFilesOf(items: ArrayLike<{ kind?: string; type?: string; getAsFile?: () => File | null }>): File[] {
  const out: File[] = []
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i]
    if (!item || item.kind !== 'file' || !(item.type ?? '').startsWith('image/')) continue
    const file = item.getAsFile?.()
    if (file) out.push(file)
  }
  return out
}

export async function mount(root: HTMLElement, _ctx: IslandContext): Promise<() => void> {
  document.title = PAGE_TITLE

  // ① 先挂样式（`layui.css` 是 bootstrapIsland 运行期插的 `<link>`）——顺序反了会 FOUC（陷阱③）
  const layui = await bootstrapIsland(['form', 'layer', 'element', 'table'])
  const form = layui.form as unknown as { render: (type?: string) => void }
  const layer = layui.layer as unknown as Record<string, unknown>
  const table = layui.table as unknown as Record<string, unknown>
  // ⚠️ `element` **不在** `LayuiNamespace` 的类型里（那个类型只声明了 form/table/layer/laydate/
  // dropdown/laytpl/util）⇒ 与 `layui/island.ts::initIslandElements` 同一手法转一次；
  // 运行期它确实在（`bootstrapIsland(['…','element'])` 之后 layui 会把模块挂到命名空间上）。
  const element = (layui as unknown as Record<string, unknown>).element as Record<string, unknown>
  const msg = (text: string): void => islandMsg(layer, text)

  const orderNo = new URLSearchParams(window.location.search).get(ORDER_NO_PARAM) ?? ''

  // ② 再塞标记，③ 然后才 `initIslandElements`
  root.innerHTML = await fetchArtifactHtml(ARTIFACT_URL)
  initIslandElements(layui)

  // 单选/复选框必须重渲一次（layui 的 `input[type=radio]` 要 `form.render` 才长成对标的样式）。
  form.render()

  /**
   * **已上传的订单截图数量**（`D-0153` ① 的本地预检要它）——从**既有的列表接口**取
   * （`GET …/screenshots`），⛔ 不为这一条新开端点 / 新请求。**只数 `img_type=0`**
   * （群聊二维码不占这 20 个名额）；本页本次会话里还没保存的那些由 `rows` **按 `img_type`** 补进去。
   *
   * ⚠️ **这是挂载那一刻的快照，而且整个预检偏「乐观」**（2026-09-25 评审订正）：
   * 后端的计数口径是「该订单 `img_type=0` 的**全部**行（`pending` + `ready`）」，
   * 而列表端点**只回 `ready`** ⇒ 本页能看到的 = 这份快照 + 本页自己的 `rows`，
   * **看不见**别处留下、还没认领的 `pending` 行（孤儿清理任务**没有调度**，`docs/STATUS.md` 挂账）
   * ⇒ 前端算出的「已上传数」可能**小于**后端的真实行数，即预检**可能放行一张后端会拒的图**。
   * 反方向（另一个弹层刚删过图 ⇒ 本页快照偏大）也存在。两个方向都是**早反馈**上的偏差，
   * **已知代价**：终态**后端是权威**（签发与认领两处都拦，它拒了照旧原样显示它的 `msg`，
   * 见 `failures[0]`），预检只负责省掉明显注定被拒的那一次往返。
   * 取列表失败就按 0 算（拿不到数量 ≠ 有 20 张），绝不因此把用户挡在门外。
   */
  let uploadedCount = 0
  if (orderNo !== '') {
    try {
      const data = await fetchScreenshots(orderNo)
      uploadedCount = data.items.filter((item) => item.img_type === 0).length
    } catch {
      uploadedCount = 0
    }
  }

  /* ── 待保存的行（闭包状态：本页是 `type:1` 同文档弹层，卸载时随 DOM 一起消失） ── */

  let rows: PendingRow[] = []

  const bar = root.querySelector<HTMLElement>(PROGRESS_BAR)
  const setProgress = (percent: number): void => {
    if (bar) bar.style.display = 'block'
    ;(element.progress as (filter: string, value: string) => void)(PROGRESS_FILTER, `${percent}%`)
  }
  const finishProgress = (): void => {
    window.setTimeout(() => {
      if (bar) bar.style.display = 'none'
      ;(element.progress as (filter: string, value: string) => void)(PROGRESS_FILTER, '0%')
    }, PROGRESS_HIDE_DELAY_MS)
  }

  const renderRows = (): void => {
    // 对标是 `tableList.reload({data, where:{}, page:false})`（L417-425），照抄。
    ;(table.reload as (id: string, o: unknown) => void)(TABLE_ID, {
      data: rows, where: {}, page: false,
    })
  }

  ;(table.render as (o: unknown) => void)({
    elem: `#${TABLE_ID}`,
    data: rows,
    // 我们只做桌面后台 ⇒ 取对标的 PC 分支（`isPc ? 'full' : 160`，L335）。
    height: 'full',
    defaultToolbar: [],
    cols: buildCols(),
    page: false,
    limit: 100,
  })

  /* ── 文件输入：证据件里那条 `#fileInput` 规则（铺满 `.wrap`、完全透明）的原意 ── */

  const fileInput = document.createElement('input')
  fileInput.type = 'file'
  fileInput.id = FILE_INPUT_ID
  fileInput.multiple = true
  fileInput.accept = FILE_ACCEPT
  root.querySelector<HTMLElement>(DROP_WRAP)?.appendChild(fileInput)
  // 「点击上传图片文件」与「拖到此处」两条路都落到这一个 `change` 上（透明输入铺满拖拽区，
  // 所以点击与原生拖放都由它接住；下面的 `drop` 委托只负责 `preventDefault` + 兜底）。
  fileInput.addEventListener('change', () => {
    void handleFiles(Array.from(fileInput.files ?? []))
  })

  const currentImgType = (): number =>
    root.querySelector<HTMLInputElement>(`input[name="${IMG_TYPE_NAME}"]:checked`)?.value === '1'
      ? GROUP_QR_TYPE
      : 0

  /* ── 三跳：ticket ⇒ PUT ⇒（保存时）认领 ── */

  async function handleFiles(files: File[]): Promise<void> {
    if (files.length === 0) return
    if (orderNo === '') {
      msg(TEXTS.noOrder)
      return
    }
    const imgType = currentImgType()
    setProgress(0)
    const accepted: File[] = []
    let badExt = 0
    let overOriginal = 0
    let overCompressed = 0
    for (const file of files) {
      const ext = extOf(file.name)
      // ⚠️ 只在**名字带扩展名**时早挡（拖动/粘贴进来的图可能没有扩展名）——没有扩展名的前端
      // 无从判断，放它过去由**签发端点**兜底。
      // ⚠️ **兜底的实际形态（2026-09-25 整体评审 M-5 订正）**：空值**先被 DRF 的字段校验拦下**
      // ——`ScreenshotTicketSerializer.ext`（`screenshot_serializers.py:12`）是
      // `CharField(max_length=8)`、**没有 `allow_blank`** ⇒ `ser.is_valid(raise_exception=True)`
      // （`screenshot_views.py:129-130`）抛的是**字段级**「该字段不能为空。」，
      // 用户看到的是**字段级提示**（⛔ 不是「只支持 jpg / jpeg / png」）。
      // `issue_ticket` 里那句 `ext not in ALLOWED_EXTS`（`screenshot_views.py:132`）管的是
      // **非空但不在白名单**的值（那一支才回「只支持 jpg / jpeg / png」）；对**空串不可达**。
      // ⇒ 空扩展名在**签发起**就被拦，根本走不到直传与认领。⛔ 别给 serializer 加 `allow_blank`
      // （那会把空串放进白名单那一支，改掉行为）。
      // ⛔ **原注释断言空扩展名会在签发端点被回「只支持 jpg / jpeg / png」，与实现不符**（M-5 订正）：
      //    魔数嗅探确实在（认领端点 `screenshot_views.py:211` 按对象头几个字节判，不信
      //    `Content-Type`），但它是**给已经拿到凭证的文件**做的第二道内容闸，
      //    **不是**空扩展名的兜底路径（「我的资料」那条链的同类判据在
      //    `apps/core/profile_views.py:293`）。
      if (ext !== '' && !(ALLOWED_EXTS as readonly string[]).includes(ext)) {
        badExt += 1
        continue
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        overOriginal += 1
        continue
      }
      const prepared = await compressImage(file)
      if (prepared.size > COMPRESSED_MAX_BYTES) {
        overCompressed += 1
        continue
      }
      accepted.push(prepared)
    }
    if (badExt > 0) msg(BAD_EXT_TEXT)
    if (overOriginal > 0) msg(`已自动过滤${overOriginal}张18MB以上的图片`)
    if (overCompressed > 0) msg(`已自动过滤${overCompressed}张5MB以上的图片`)
    if (accepted.length === 0) {
      finishProgress()
      // ⚠️ 只在**确实有**「太大」被过滤时才弹这句：否则「全是 .gif」这一支会被它
      // **覆盖掉上一条**（`layer.msg` 是后来的盖先前的 ⇒ 用户只看到「图片都太大了」，
      // 而真正的原因（格式不对）被吞掉。同族坑见 `order-orderlist.ts` 的成功提示顺序）。
      if (overOriginal + overCompressed > 0) {
        msg('图片都太大了，请把图片大小控制在单张18MB以内')
      }
      return
    }

    /**
     * ⭐ `D-0153` ①：**每单 20 张的本地预检**（后端在签发与认领两处拦，是权威）。
     *
     * 已上传数量 = 挂载时那份列表快照 + 本次会话里还没保存的队列（`rows`）——
     * 两边都**只数订单截图**：群聊二维码不占这 20 个名额（它自己的闸在保存那一步）。
     * ⚠️ `rows` 那一半必须按 `img_type` 过滤（评审 I-2 修复）：`PendingRow` 现在记着上传那一刻的
     * 类型，⛔ 不能再用 `rows.length` 把两类图混成一个数（那会让 1 张二维码白占掉一个名额）。
     * `room` 是**这一批还能放几张**：一张都放不下就整批不发（零请求），
     * 放得下一部分就只放行前面几张（超出的那些**一次往返都不发**）。
     * ⛔ 后端真拒了照旧走 `failures[0]` 原样显示它的 msg。
     */
    let uploadable = accepted
    if (imgType !== GROUP_QR_TYPE) {
      const pendingShots = rows.filter((row) => row.img_type !== GROUP_QR_TYPE).length
      const room = SCREENSHOT_LIMIT - (uploadedCount + pendingShots)
      if (room <= 0) {
        finishProgress()
        msg(LIMIT_TEXT)
        return
      }
      if (accepted.length > room) {
        uploadable = accepted.slice(0, room)
        msg(LIMIT_TEXT)
      }
    }
    let done = 0
    const failures: string[] = []
    await Promise.all(uploadable.map(async (file) => {
      try {
        const ticket = await requestScreenshotTicket(orderNo, extOf(file.name), imgType)
        await putFileToStorage(ticket.upload_url, file)
        rows = [...rows, {
          name: file.name,
          size: file.size,
          screenshot_id: ticket.screenshot_id,
          // 类型在上传那一刻定下（有意不同第 4 条）⇒ 记在这一行上，供两处计数闸**按类型**数。
          img_type: imgType,
          preview_url: previewUrlOf(file),
        }]
        done += 1
        setProgress(Math.round((done * 100) / uploadable.length))
        renderRows()
      } catch (e) {
        failures.push(errorText(e))
      }
    }))
    finishProgress()
    if (failures.length > 0) msg(failures[0]!)
  }

  const onDragOver = (event: Event): void => {
    // 不拦的话浏览器会**导航到那个文件**（整页跳走）。
    event.preventDefault()
  }
  const onDrop = (event: Event): void => {
    event.preventDefault()
    const transfer = (event as Event & { dataTransfer?: { files?: ArrayLike<File> } }).dataTransfer
    void handleFiles(Array.from(transfer?.files ?? []))
  }
  const onPaste = (event: Event): void => {
    const clipboard = (event as Event & { clipboardData?: { items?: ArrayLike<{
      kind?: string; type?: string; getAsFile?: () => File | null
    }> } }).clipboardData
    const files = clipboard?.items ? imageFilesOf(clipboard.items) : []
    if (files.length === 0) return
    event.preventDefault()
    void handleFiles(files)
  }

  root.addEventListener('dragover', onDragOver)
  root.addEventListener('drop', onDrop)
  root.addEventListener('paste', onPaste)

  // 证据件 L114-116：粘贴框只当粘贴目标，用户敲进去的任何东西立刻清掉。
  root.querySelector(PASTE_BOX)?.addEventListener('input', (event) => {
    const box = event.target as HTMLElement
    box.innerHTML = ''
  })

  /* ── 保存 = 逐行认领 ── */

  async function save(): Promise<void> {
    if (rows.length === 0) {
      msg(TEXTS.noFile)
      return
    }
    const imgType = currentImgType()
    // ⚠️ **按 `img_type` 数**（评审 I-2 修复）：只用 `rows.length` 会把订单截图也数进「二维码」里，
    // 于是「1 张二维码 + N 张订单截图」以 `imgType=1` 保存时会被**误导性地**挡住——提示说
    // 「群聊二维码仅限一张」，而实际只有一张（终态由后端行锁 + 淘汰收口，但那句提示是错的）。
    if (imgType === GROUP_QR_TYPE && rows.filter((row) => row.img_type === GROUP_QR_TYPE).length > 1) {
      msg(TEXTS.groupQrLimit)
      return
    }
    const remark = root.querySelector<HTMLInputElement>(REMARK_INPUT)?.value ?? ''
    const remaining: PendingRow[] = []
    const failures: string[] = []
    await Promise.all(rows.map(async (row) => {
      try {
        await claimScreenshot(orderNo, {
          screenshot_id: row.screenshot_id,
          original_name: row.name,
          remark,
        })
        // 这一行已经落库 ⇒ 它的本地预览地址不再需要（见 `releasePreview` 的注释）。
        releasePreview(row)
      } catch (e) {
        remaining.push(row)
        failures.push(errorText(e))
      }
    }))
    rows = remaining
    renderRows()
    msg(failures.length > 0 ? failures[0]! : TEXTS.saved)
  }

  /* ── 行内两颗按钮（走 layui 的表格 tool 通道——它们在对标里是 `#baroperating` 模板） ── */

  function openPreview(row: PendingRow): void {
    if (row.preview_url === '') {
      msg(TEXTS.noPreview)
      return
    }
    const url = escapeHtml(row.preview_url)
    // 参数逐条取证据件 L376-384（那一跳原来还要 `GetAliOssFileView` 换签名地址，我们没有那个端点）。
    ;(layer.open as (o: unknown) => void)({
      type: 1,
      title: false,
      closeBtn: 0,
      area: ['85%'],
      skin: 'layui-layer-nobg',
      shadeClose: true,
      content: `<a href="${url}" target="_blank" title="点击可以新开窗口浏览大图">` +
        `<img src="${url}" width="580px" height="420px"></a>`,
    })
  }

  function dropRow(row: PendingRow): void {
    releasePreview(row)
    rows = rows.filter((item) => item !== row)
    renderRows()
    msg(TEXTS.deleted)
  }

  ;(table.on as (
    e: string,
    cb: (obj: { event?: string; data?: PendingRow }) => void,
  ) => void)(`tool(${TABLE_ID})`, (obj) => {
    const row = obj.data
    if (!row) return
    if (obj.event === VIEW_EVENT) {
      openPreview(row)
      return
    }
    if (obj.event === DELETE_EVENT) dropRow(row)
  })

  /* ── 保存按钮：捕获态 click 委托（见文件头「有意不同」第 10 条） ── */

  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement | null
    if (!target || typeof target.closest !== 'function') return
    if (!target.closest(SAVE_BUTTON)) return
    event.preventDefault()
    event.stopPropagation()
    void save()
  }
  root.addEventListener('click', onClick, true)

  return () => {
    root.removeEventListener('dragover', onDragOver)
    root.removeEventListener('drop', onDrop)
    root.removeEventListener('paste', onPaste)
    root.removeEventListener('click', onClick, true)
    // 卸载时把还没被删除/认领的那些本地预览地址释放掉（否则 blob 会被钉到页面卸载）。
    rows.forEach(releasePreview)
    rows = []
    root.innerHTML = ''
  }
}

export default { mount } satisfies IslandPage
