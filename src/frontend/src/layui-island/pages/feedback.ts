/**
 * 【问题反馈】island 页（island key = `feedback`，路由 `/feedback`）。
 *
 * spec：`docs/specs/2026-10-08-feedback-design.md` §6.1（菜单）/ §6.2（路由）/ §6.3（本页）。
 * 契约的另一半是**实现即契约**（`src/apps/business/api/feedback_views.py`）。
 *
 * ## 这一页是**自有功能**，不是复刻
 *
 * 对标物（鸡蛋铺）的侧栏最底部没有这一条 ⇒ 本页**没有证据件**、也不走 `mountConfigLike`
 * / `artifactPage`（那是「取 `public/replica/<key>.html` → 注入 → 灌数据」的复刻专用链路）。
 * 标记由本文件自己写，风格照 `pages/open-apps.ts` 与 `pages/system-perfanalysis.ts` 的 layui 惯例。
 * island key 登记在 `config/__tests__/replicaRoutes.spec.ts` 的 `OWN_FEATURE_PAGE_KEYS`。
 *
 * ⚠️ 权限点**不是新造的**：`home.feedback`（对标 112 号点「问题反馈及建议」）本来就在
 * `apps/rbac/catalog.py` 的 `NODES` 里（spec §1.1）⇒ 对照 `config/routePermissions.ts` 的
 * 「已知近似」段，本页是**精确命中**而不是借用（与 `system-perfanalysis` 那类复用的点不同）。
 *
 * ## 一页两段（spec §6.3）
 *
 * 1. **提交区**（所有人可见）：`textarea`（必填）+ 截图区 + 「提交」按钮。
 *    - 截图三种输入里本页做**两种**：**Ctrl+V 粘贴** ＋ 点按钮选文件。
 *      ⛔ **拖拽不做**（spec §6.3 明写「不在本线范围」）。
 *    - ⚠️ `paste` 监听挂在**整个提交区容器**上，⛔ 不是只挂 `textarea`——
 *      用户截完图时焦点常常不在输入框里；先例是 `pages/order-screenshots-upload.ts`
 *      把 `paste` 挂在弹层 `root` 上（那里 470–500 行的那段读法）。
 *    - 上限：3 张 / 单张 10 MB / 只收 png、jpg、jpeg；**超限就地报出是哪一条**（spec §6.3
 *      原话「⛔ 不要静默丢弃」——静默丢会让用户以为图已经传上去了）。
 *    - 提交成功后**清空**表单与截图、并刷新下面的列表（spec §6.3）。
 * 2. **列表区**：
 *    - 所有人看**自己的**（内容 / 时间 / 状态 / 管理员备注 / 截图缩略）；
 *    - **管理员**（判据 = `/api/auth/me` 的 `is_tenant_admin`）额外看到**全部**反馈
 *      （多一列「提交人」）+ 每行的「标记已处理 / 改回未处理」与备注输入框（`PATCH`）。
 *
 * ## ⚠️ 管理员判据为什么是 `is_tenant_admin` 而**不是** `is_account_admin`
 *
 * 业务方原话是「只有**系统管理员**可以看见反馈的内容」（spec §2 第 2 行 / §4.4），
 * 而这条判据在本仓的出处就是 `apps/core/permissions.py::is_tenant_admin`。
 * 两者在**租户平面**上同源（都认「系统管理员」Group），分叉只在 `public` 平面——
 * 「语义不是一回事」⇒ ⛔ 不拿 `is_account_admin` 顶替（spec §6.3 的 ⛔ 那一条）。
 * ⚠️ 这与 `pages/open-apps.ts` 的自检门**有意不同**（那一页要的确实是账号管理员），
 * 不是笔误：两页的业务口径本来就不同。
 *
 * ## ⚠️ 前端的两个判据**都只是体验层**（`AGENTS.md` §5-D）
 *
 * 「看不看得到提交人那一列 / 有没有处置按钮」由本页判；而**真正的收口在服务端**：
 * 非管理员的 `GET` 走 queryset 层收窄（只看得到自己那些）、`PATCH` 一律 **403**
 * （spec §4.2 / §4.4）。⛔ 不在前端新造一套写权限判据——本页把后端的失败文案**如实弹出来**。
 *
 * ## 三条 island 陷阱（`AGENTS.md` §5-G，改这一页前先读）
 *
 * ① 类名与选择器分成两个常量（`class="${PANEL_CLASS}"` 拼标记、`PANEL_SELECTOR` 查节点）；
 * ② 以 `(` 开头的语句一律前置分号（`;(layer.open as …)(…)`——ASI 不会在 `)` 换行 `(` 之间插分号）；
 * ③ 顺序：`await bootstrapIsland(...)`（挂样式）→ `root.innerHTML = …`（塞标记）→
 *    `initIslandElements(layui)`（按标记里的 `lay-*` 初始化）。
 * ⚠️ 三条都有**运行期**判据（`__tests__/feedback.spec.ts`），不是靠这段注释。
 */
import { fetchMe } from '@/api/auth'
import {
  createFeedback,
  handleFeedback,
  issueTicket,
  listFeedback,
  type FeedbackRow,
  type FeedbackStatus,
} from '@/api/feedback'
import { escapeHtml } from '@/layui-island/escape'
import { putFileToStorage } from '@/layui-island/directUpload'
import { canNode } from '@/layui-island/perms'
import { bootstrapIsland, initIslandElements, islandMsg, type IslandPage } from '@/layui/island'
import type { LayuiModule, LayuiNamespace } from '@/layui/loader'

/** island key（= 文件名 = 路由 `meta.islandPage`，三处必须逐字一致）。 */
export const PAGE_KEY = 'feedback'
export const TITLE = '问题反馈'

/**
 * 本页的 RBAC 节点 code（**不是新造的**：对标 112 号点「问题反馈及建议」，spec §1.1）。
 * ⚠️ 取值的唯一权威是 `config/routePermissions.ts` 的 `ROUTE_NODE_CODES['feedback']`；
 * 这里是 **iframe 内的深链自检**用的同一份字面量（iframe 读不到 Vue 路由的 `meta`）。
 * 两处漂移由 `__tests__/feedback.spec.ts` 的一条断言钉住（⛔ 不是靠这句话）。
 */
export const FEEDBACK_NODE = 'home.feedback'

/** 页面容器的**类名**（拼进标记用）与**选择器**（查节点用）——两者不能混（陷阱 ①）。 */
export const PANEL_CLASS = 'feedbackPanel'
export const PANEL_SELECTOR = `.${PANEL_CLASS}`
/**
 * **提交区**容器的类名 / 选择器。
 *
 * ⚠️ `paste` 监听挂的是**这一个**容器（不是 `textarea`）：见文件头那一条。
 * ⚠️ 它同时是 `layui-form`（`form.render` 的根选择器就是 `.layui-form`）＋ 带 `lay-filter`
 * ⇒ 渲染复选框时可以只重渲这一块，⛔ 不用无参的全局渲染。
 */
export const FORM_CLASS = 'feedbackForm'
export const FORM_SELECTOR = `.${FORM_CLASS}`
export const FORM_FILTER = 'feedbackForm'
/** **列表区**容器的类名 / 选择器（本页的列表是手写行，不是 `layui.table`——见 `rowHtml` 的注释）。 */
export const LIST_CLASS = 'feedbackList'
export const LIST_SELECTOR = `.${LIST_CLASS}`

/** 提交区里的控件 `id`。 */
export const CONTENT_TEXTAREA_ID = 'feedbackContent'
export const SUBMIT_BUTTON_ID = 'feedbackSubmitBtn'
export const PICK_BUTTON_ID = 'feedbackPickBtn'
export const FILE_INPUT_ID = 'feedbackFileInput'
/** 待提交截图的容器 `id`（选中一张就往里追加一格缩略）。 */
export const PENDING_BOX_ID = 'feedbackPending'
/** 列表区那几个容器 `id`（表头一行 + 行容器 + 空态 + 计数）。 */
export const LIST_HEAD_ID = 'feedbackListHead'
export const LIST_BODY_ID = 'feedbackListBody'
export const LIST_EMPTY_ID = 'feedbackListEmpty'

/**
 * 列表里**每一样东西的类名**（选择器由这些常量拼，⛔ 不写字面串——那正是「类名与选择器
 * 分成两个常量」要防的漂移；这里多一层：测试与本页共用同一份常量）。
 */
export const ROW_CLASS = 'feedbackRow'
export const ACTIONS_CLASS = 'feedbackActions'
export const NOTE_INPUT_CLASS = 'feedbackNoteInput'
/** 截图格的类（`layer.photos` 的分组选择器挂在它上面，见 `reload` 里的那一次调用）。 */
export const SHOTS_CLASS = 'feedbackShots'
export const SHOTS_SELECTOR = `.${SHOTS_CLASS}`
/** 标记「这一行是处理动作那一格」的属性（`PATCH` 的按钮按它在行内定位，不靠列序号）。 */
export const ACTIONS_ATTR = 'data-feedback-actions'
/** 每颗处置按钮上的 `data-feedback-act` 取值（`handle` = 标已处理、`reopen` = 改回未处理）。 */
export const ACT_HANDLE = 'handle'
export const ACT_REOPEN = 'reopen'

/**
 * 状态 → **色标类 + 色条色值**（业务方 2026-10-08：「区分开已处理/未处理」）。
 *
 * ⚠️ **一对值只在这一处定义**（色标类与色条色值同源）：两处各抄一份色值就是
 * E-0021 的「同一事实两套判据」，而它们漂移时**只是颜色不对、不会报错**。
 * ⚠️ 键是**状态原值**（`pending` / `handled`），⛔ **不是**服务端给的中文 `status_label`：
 * 文案可以改，颜色得跟着语义走（跟着 label 会让服务端改文案时颜色静默配错）。
 * ⚠️ 色值**当场从 vendored `public/layui/css/layui.css` 取的**，⛔ 不凭记忆：
 * `.layui-bg-orange{background-color:#ffb800}`、`.layui-bg-green{background-color:#16baaa}`。
 * `__tests__/feedback.spec.ts` 里有一条把它们与该文件对拍的判据（漂了就红）。
 */
export const STATUS_STYLE: Record<FeedbackStatus, { badge: string; color: string }> = {
  pending: { badge: 'layui-badge layui-bg-orange', color: '#ffb800' },
  handled: { badge: 'layui-badge layui-bg-green', color: '#16baaa' },
}

/** 主操作按钮（标记已处理）：layui 默认就是绿底白字（`#16baaa`），用**正常尺寸**、⛔ 不用最小的 `-xs`。 */
export const BTN_HANDLE_CLASS = 'layui-btn layui-btn-sm'
/** 回退操作按钮（改回未处理）：素色（`layui-btn-primary`，白底灰字），与主操作**看得出主次**。 */
export const BTN_REOPEN_CLASS = 'layui-btn layui-btn-sm layui-btn-primary'
/** 已处理那几行的**正文淡化色**（只用于内容 / 备注两格，⛔ 不动按钮与状态色标）。 */
export const HANDLED_TEXT_COLOR = '#8c8c8c'

/** 单张截图的上限（spec §2 第 4 行裁定：单张 ≤ **10 MB**）。 */
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024
/** 一条反馈最多几张（同上裁定：**3 张**）。 */
export const MAX_IMAGES = 3
/** 只收这三种扩展名（裁定原文「只收 jpg/jpeg/png」）；`jpg` 与 `jpeg` **都收**，不是一个。 */
export const ALLOWED_EXTS = ['png', 'jpg', 'jpeg'] as const

/**
 * 页内其它用户可见文案（抽成常量只为让 spec 钉住字面值）。
 *
 * ⚠️ 限流那三条**必须各自说清是哪一条超了**（spec §6.3：超限就地提示、⛔ 不静默丢弃）。
 * 合成一句「图片不符合要求」等于让用户去猜三条里的哪一条。
 */
export const MESSAGES = {
  /** 内容为空（必填）。 */
  noContent: '请描述你遇到的问题',
  /** 张数超限（带上限数字，用户才知道该怎么办）。 */
  tooManyImages: `一条反馈最多 ${MAX_IMAGES} 张截图`,
  /** 单张体积超限（带 MB 数字，同上）。 */
  imageTooLarge: `单张截图不能超过 ${MAX_IMAGE_BYTES / (1024 * 1024)} MB`,
  /**
   * 类型不在白名单。
   *
   * ⚠️ **这句必须与后端拒收时那句逐字相同**（2026-10-08 红队 F3）：后端是
   * `apps/business/api/feedback_views.py::IMAGE_TYPE_LIMIT_MSG`，取值与头像那条先例
   * `apps/core/profile_views.py` 的同一句**逐字相同**。
   * 两处各留一套措辞 ⇒ 同一件「图不对」的事，用户会看到**两种说法**
   * （前端预检一次、后端拒收一次），而两句话读起来像两条不同的规则。
   * ⚠️ 词序是 `jpg / jpeg / png`（**不是** `png / jpg / jpeg`）——照抄，别顺手"排整齐"。
   * ⚠️ 末尾**不带句号**（后端那句也没有）。判据见 `__tests__/feedback.spec.ts` 里那条
   * 「与后端逐字相同」的**字面量**断言（⛔ 不是从生产常量派生的自指断言）。
   */
  imageBadType: '只支持 jpg / jpeg / png',
  /** 提交成功。 */
  submitted: '反馈已提交，感谢反馈',
  /** 兜底失败文案（后端文案通常已经有了）。 */
  failed: '操作失败',
  /** 列表为空时的空态文案（空态≠取数失败，`AGENTS.md` §5-E）。 */
  empty: '还没有反馈记录',
  /** 处置成功。 */
  handled: '已标记为已处理',
  /** 改回未处理。 */
  reopened: '已改回未处理',
} as const

/** 待提交的一格截图（`upload_url` 已 PUT 成功、`key` 已拿到）。 */
export type PendingImage = {
  key: string
  /** 本机预览用的对象 URL（拿不到时是空串——那一格就只显示文件名）。 */
  preview_url: string
  /** 原始文件名（只用于显示与 `alt`）。 */
  name: string
}

/**
 * 从文件名取扩展名（**去点、小写**）。
 *
 * ⚠️ 与 `pages/order-screenshots-upload.ts` 的 `extOf` 同形——本页不 import 它是因为
 * 那是**页面模块之间**的依赖（`directUpload.ts` 的文件头写过这个理由：跨页面 import 会把
 * 整页连同它的证据件依赖一起拖进来）。⚠️ 代价如实登记：**两处同形的小函数**，改一处要想到另一处。
 *
 * ⚠️ **「推不出扩展名」回空串**（不是回原名）：调用方要用它区分「名字里没有点」「点后面是空的」
 * 这两种"没有扩展名"，好走 `extOfFallback` 的 MIME 兜底。
 * ⚠️ ⛔ **不要拿它单独当"这张图能不能传"的判据**：判据是 `resolveExt`（见下），
 * 它把「名字 → MIME」两条路合起来，正是本页 2026-10-08 修掉的那个缺陷
 * （粘贴进来的图常常**没有文件名**，只看名字会把一张 PNG 判成"格式不对"）。
 */
export function extOf(name: string): string {
  const dot = name.lastIndexOf('.')
  // 没有点 / 点在头或尾 / 点后面是空的 ⇒ 没有扩展名（⛔ 不要把整段名字当 ext）。
  if (dot <= 0 || dot === name.length - 1) return ''
  return name.slice(dot + 1).toLowerCase()
}

/** 本机预览的对象 URL（拿不到就空串——那一格退化成只显示文件名，⛔ 不是崩）。 */
function previewUrlOf(file: File): string {
  try {
    return typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : ''
  } catch {
    return ''
  }
}

/** 文件体积是否超限（`is not None` 同族：`0` 字节的文件走「类型/签发」那条路，不在这里被误判）。 */
export function isTooLarge(file: File): boolean {
  return file.size > MAX_IMAGE_BYTES
}

/**
 * **MIME → 扩展名**的兜底表（只在「名字里没有扩展名」时用）。
 *
 * ⚠️ 三条纪律：
 * 1. **取值与 `docs/specs/2026-10-08-feedback-design.md` §2 第 4 行的裁定逐字一致**
 *    （只收 `jpg` / `jpeg` / `png`）；⛔ 不新造第四种写法（如 `image/pjpeg`）。
 * 2. ⛔ **不比后端宽**：表里只有这三种；`image/gif` / `image/webp` 之类**不映射**
 *    ⇒ 它们**一定**会被判成「格式不支持」，而那一句是**对的**（用户的图确实不是我们收的那几种）。
 * 3. ⚠️ `image/jpeg` → **`jpeg`**（不是 `jpg`）：与 `ALLOWED_EXTS` 里的那个写法对齐，
 *    两种值后端都收，但只留一种写法少一处漂移面。
 */
const MIME_EXT: Readonly<Record<string, string>> = {
  'image/png': 'png',
  'image/jpeg': 'jpeg',
}

/**
 * 这次要**发给签发端点**的扩展名（`null` = 判断不出可用的那一种）。
 *
 * 判据顺序：① 文件名的扩展名合法 ⇒ 用它；② 否则看 `file.type` 查 `MIME_EXT`；
 * ③ 都不行 ⇒ `null`（调用方**就地**提示格式不支持，⛔ 不许带着空 ext 去打后端——
 * 那会换来一句**归因错误**的后端文案：用户粘的明明是一张 PNG，却看到「图片格式不正确」）。
 *
 * ⚠️ 为什么必须有这条兜底（2026-10-08 红队 F2 实测）：浏览器剪贴板在**部分环境**
 * （远程桌面 / Linux）给出的 `File.name` 就是**空的**，也可能是 `image` / `blob` 这种没有点的名字；
 * 而那时 `file.type` **是准的**。只看文件名 ⇒ 一张 PNG 会被前端判成"格式不对"。
 * ⚠️ 代价如实登记：`file.type` 是**客户端自报**的（可以伪造）⇒ 它只用来**决定 ext**，
 * ⛔ 不构成"这张图是图片"的判据；真正的魔数判据在服务端（spec §4.3 第 5 条）。
 */
export function resolveExt(file: File): string | null {
  const byName = extOf(file.name)
  if ((ALLOWED_EXTS as readonly string[]).includes(byName)) return byName
  return MIME_EXT[(file.type ?? '').toLowerCase()] ?? null
}

/**
 * 扩展名是否在白名单里（**只看名字那一半**，保留给单测与 `rejectReason` 阅读）。
 * ⚠️ 判断"这张到底能不能传"请用 `resolveExt`（它把 MIME 兜底也算进来）。
 */
export function isAllowedExt(name: string): boolean {
  return (ALLOWED_EXTS as readonly string[]).includes(extOf(name))
}

/**
 * 一次「选到文件」的**前置闸**（纯函数，⛔ 只有这一处判据）。
 *
 * 返回 `null` = 放行；否则返回**该说哪一句**。
 * ⚠️ 类型判据走 `resolveExt`（名字 → MIME 兜底），⛔ 不是只看 `isAllowedExt(file.name)`：
 * 后者会把「没有文件名的粘贴图」整类误判（红队 F2）。
 * ⚠️ 三条的**顺序**是承重的：先说"这张太大"还是"张数满了"，不能让用户为了同一张图
 * 反复试两个不同的原因。本页顺序 = ① 类型 → ② 体积 → ③ 张数（类型是用户能立刻改的，
 * 张数取决于屏幕上已有的东西）。
 * ⚠️ `pendingCount` 必须由调用方在**入库那一刻**传（F1 的排队之后，pending 可能已经变了）。
 */
export function rejectReason(file: File, pendingCount: number): string | null {
  if (resolveExt(file) === null) return MESSAGES.imageBadType
  if (isTooLarge(file)) return MESSAGES.imageTooLarge
  if (pendingCount >= MAX_IMAGES) return MESSAGES.tooManyImages
  return null
}

/**
 * 从一次 `paste` 事件里取出**图片**文件（纯函数）。
 *
 * ⚠️ 判据照先例 `pages/order-screenshots-upload.ts`：`clipboardData.items` 里
 * `type` 以 `image/` 开头的项取 `getAsFile()`；**没有图片项就回空数组**——
 * 调用方据此**静默不管**（用户粘一段文字进来是很正常的，弹「粘贴失败」是噪音，spec §6.3）。
 * ⚠️ `kind !== 'file'` 的项要跳过：剪贴板里文字项也是 `items` 的一员。
 */
export function imageFilesOf(
  items: ArrayLike<{ kind?: string; type?: string; getAsFile?: () => File | null }>,
): File[] {
  const out: File[] = []
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i]
    if (!item || item.kind !== 'file' || !(item.type ?? '').startsWith('image/')) continue
    const file = item.getAsFile?.()
    if (file) out.push(file)
  }
  return out
}

/** ISO 8601 → `2026-10-08 12:00:00`（只截到秒；时区偏移留在原地，不做时区换算）。 */
export function formatTime(value: string): string {
  return escapeHtml(value.replace('T', ' ').slice(0, 19))
}

/**
 * 截图那一格的**标记**（纯函数）。
 *
 * ⚠️ 尺寸用**内联 style**（本页没有自己的 CSS 文件，也不想为一个缩略图新开一个）；
 * `max-height:44px` 是为了让一行里三张缩略图不至于把列表撑高。
 * ⚠️ `url` 是服务端现签的读链接 ⇒ **转义后**放进 `src`；`key` 也一样转义（它进 `title`）。
 * ⛔ 两者都**不进** `innerHTML` 之外的 DOM 属性以外的通道（页面不回传它们）。
 * ⚠️ **点击预览（2026-10-12，业务方要求「可点击预览不是下载」）**：裸 `<img>` 外不再包
 * `<a target="_blank">`——点击行为交给 `layer.photos` 弹层（`reload` 里按分组选择器绑定，
 * 先例 = `order-screenshots-manage.ts` 照对标 `picmange.js` 的同一条），在本页内放大浏览，
 * ⛔ 不依赖「浏览器对新标签里这个 URL 展示还是下载」的行为。
 */
export function imagesHtml(images: readonly { key: string; url: string }[]): string {
  if (images.length === 0) return '<span style="color:#999">—</span>'
  return images
    .map(
      (img) =>
        `<img src="${escapeHtml(img.url)}" alt="反馈截图" title="${escapeHtml(img.key)}" ` +
        `style="max-height:44px;max-width:80px;margin-right:4px;border:1px solid #e2e2e2;cursor:pointer">`,
    )
    .join('')
}

/**
 * 待提交截图的**一格**（纯函数）。
 *
 * ⚠️ `preview_url` 为空时**不渲染 `<img>`**（`src=""` 会让浏览器去请求当前页面，
 * 有的浏览器还会显示一个破图标）⇒ 退化成文件名。
 */
export function pendingHtml(image: PendingImage, index: number): string {
  const thumb =
    image.preview_url === ''
      ? ''
      : `<img src="${escapeHtml(image.preview_url)}" alt="待提交截图" ` +
        `style="max-height:44px;max-width:80px;border:1px solid #e2e2e2;vertical-align:middle">`
  return (
    `<span class="layui-inline" style="margin:0 8px 4px 0">` +
    `${thumb}<span style="color:#666;margin-left:2px">${escapeHtml(image.name)}</span>` +
    `<a href="javascript:;" data-feedback-remove="${index}" style="color:#FF5722;margin-left:4px">删除</a>` +
    `</span>`
  )
}

/**
 * 列表的**一行**（纯函数）。
 *
 * ## 为什么列表是手写行而不是 `layui.table`
 *
 * 这一页的列**随身份变**（管理员多一列「提交人」、多一整格处置控件），而 `layui.table` 的
 * `cols` 是 `render` 那一刻定死的；更要紧的是**截图缩略**那一格要塞 `<img>`，
 * 表格的 `templet` 虽然也能做，但「非管理员不渲染那一列」就得重渲整张表。
 * 手写行的代价如实登记：**本页没有表头的排序/分页能力**（业务方要的就是一份流水，
 * 不是可筛选的大表；spec §10 也没要分页/导出）。
 *
 * ⚠️ 每个来自服务端的字段**一律过 `escapeHtml`**——`content` / `admin_note` /
 * `created_by_name` 都是用户输入，而这一段走 `innerHTML`（存储型 XSS 的典型入口）。
 * ⚠️ 备注输入框的**当前值**要放进 `value`（转义后）：不预填的话管理员一点「标记已处理」
 * 就会把他刚写的备注**丢掉**，而那是静默的。
 * ⚠️ 处置按钮的 `data-feedback-act` 只有两个取值（`handle` / `reopen`），
 * 由**当前状态**决定是哪一颗：`pending` ⇒ 显示「标记已处理」，`handled` ⇒ 显示「改回未处理」。
 * 与 `open-apps.ts` 的 `stateActionLabel` 同一个做法（**一个**按钮、文案与动作由行状态定），
 * ⛔ 不是「两颗按钮里藏一颗」。
 */
export function rowHtml(row: FeedbackRow, opts: { isAdmin: boolean }): string {
  const note = escapeHtml(row.admin_note)
  const style = STATUS_STYLE[row.status]
  const isHandled = row.status === 'handled'
  // 「已处理」的行把**正文**淡化一档（内容 / 备注两格），状态色标与按钮保持原样——
  // 扫一眼就能分出哪几条还没人管（业务方 2026-10-08 的要求）。
  const dim = isHandled ? ` style="color:${HANDLED_TEXT_COLOR}"` : ''
  const cells = [
    // 第一格带一条**状态色条**（与色标同色）：整行在视觉上有归属，不用先看状态列。
    `<td style="border-left:3px solid ${style.color}">${formatTime(row.created_at)}</td>`,
    `<td${dim}>${escapeHtml(row.content)}</td>`,
    `<td><span class="${style.badge}">${escapeHtml(row.status_label)}</span></td>`,
    `<td${dim}>${note === '' ? '<span style="color:#999">—</span>' : note}</td>`,
    `<td class="${SHOTS_CLASS}">${imagesHtml(row.images)}</td>`,
  ]
  if (opts.isAdmin) {
    // ⚠️ 「提交人」列**只对管理员存在**：非管理员的行里**一个字都不出现**（含表头那半——
    //    表头由 `listHeadHtml` 按同一个 `isAdmin` 生成）。判据见 spec §6.5 第 5 条。
    cells.splice(1, 0, `<td>${escapeHtml(row.created_by_name)}</td>`)
    cells.push(
      `<td class="${ACTIONS_CLASS}" ${ACTIONS_ATTR}="${row.id}">` +
        `<input type="text" class="${NOTE_INPUT_CLASS}" maxlength="200" ` +
        `placeholder="处理备注（可不填）" value="${note}" ` +
        `style="width:180px;height:30px;line-height:30px;display:inline-block;vertical-align:middle">` +
        // ⚠️ `<i>` 与文案之间⛔ **不留空白**：`textContent` 要保持恰好是那四个字
        //    （页测试钉的就是它，多一个空格就红——这是有意的，别把判据放宽）。
        `<a href="javascript:;" class="${isHandled ? BTN_REOPEN_CLASS : BTN_HANDLE_CLASS}" ` +
        `style="margin-left:6px;vertical-align:middle" ` +
        `data-feedback-act="${isHandled ? ACT_REOPEN : ACT_HANDLE}" data-feedback-id="${row.id}">` +
        `<i class="layui-icon ${isHandled ? 'layui-icon-refresh' : 'layui-icon-ok'}"></i>` +
        `${isHandled ? '改回未处理' : '标记已处理'}</a>` +
        `</td>`,
    )
  }
  return `<tr class="${ROW_CLASS}" data-feedback-row="${row.id}">${cells.join('')}</tr>`
}

/** 表头那一行（与 `rowHtml` 的列**同一份判据**：`isAdmin` 决定有没有「提交人」）。 */
export function listHeadHtml(isAdmin: boolean): string {
  const heads = ['提交时间', '反馈内容', '状态', '管理员备注', '截图']
  if (isAdmin) {
    heads.splice(1, 0, '提交人')
    heads.push('处理')
  }
  return heads.map((h) => `<th>${h}</th>`).join('')
}

/**
 * 整个列表区的标记（纯函数）。
 *
 * ⚠️ **空态要说出来**：`items.length === 0` 时显示 `MESSAGES.empty`，
 * ⛔ 不是留一块空白——空白会被读成"这一列没数据 / 页面坏了"（`AGENTS.md` §5-E 的
 * 「空结果不等于没有数据」，那一页的管理员备注同款处理）。
 */
export function listHtml(rows: readonly FeedbackRow[], opts: { isAdmin: boolean }): string {
  if (rows.length === 0) {
    return `<tr><td colspan="${opts.isAdmin ? 7 : 5}" style="text-align:center;color:#999">` +
      `${MESSAGES.empty}</td></tr>`
  }
  return rows.map((row) => rowHtml(row, opts)).join('')
}

/**
 * 页面标记。
 *
 * ⚠️ 提交区那一段是 **`.layui-form`**（不是普通的 div）：`form.render(type, filter)` 的根
 * 选择器就是 `.layui-form`（vendored layui 的 `h=".layui-form"`）⇒ 少了它，
 * 里面那些 `lay-skin` 的裸 input **不会被渲染**，页面上就是一堆原生控件。
 * ⚠️ 提交区**没有** `lay-submit` / `lay-filter` 的表单提交语义：本页的「提交」是一颗普通按钮，
 * 由本文件自己挂 `click`（⛔ 不走 layui 的 `form.on('submit(...)')`——那会要求把 `textarea`
 * 放进它认得的字段结构里，而本页的载荷形状（`content` + `image_keys`）是我们自己的）。
 */
export function buildHtml(): string {
  return `
<div class="${PANEL_CLASS}" style="padding:10px 15px">
  <div class="${FORM_CLASS} layui-form" id="${FORM_FILTER}" lay-filter="${FORM_FILTER}">
    <div class="layui-form-item layui-form-text">
      <label class="layui-form-label" style="width:auto">问题描述</label>
      <div class="layui-input-block">
        <textarea id="${CONTENT_TEXTAREA_ID}" class="layui-textarea" rows="4"
                  placeholder="请描述你遇到的问题" maxlength="2000"></textarea>
      </div>
    </div>
    <div class="layui-form-item">
      <label class="layui-form-label" style="width:auto">截图</label>
      <div class="layui-input-block">
        <div id="${PENDING_BOX_ID}" style="line-height:24px"></div>
        <button type="button" class="layui-btn layui-btn-sm layui-btn-primary" id="${PICK_BUTTON_ID}">
          选择图片
        </button>
        <input type="file" id="${FILE_INPUT_ID}" accept="image/png,image/jpeg" multiple
               style="display:none">
        <div class="layui-word-aux" style="color:#999">
          可以直接把截图<b>粘贴</b>到这个区域（截图工具截完按 Ctrl+V）；最多 ${MAX_IMAGES} 张，
          单张不超过 ${MAX_IMAGE_BYTES / (1024 * 1024)} MB，只支持 png / jpg。
        </div>
      </div>
    </div>
    <div class="layui-form-item">
      <div class="layui-input-block">
        <button type="button" class="layui-btn" id="${SUBMIT_BUTTON_ID}">提交</button>
      </div>
    </div>
  </div>

  <div style="font-size:14px;font-weight:700;margin:16px 0 6px">反馈记录</div>
  <div class="${LIST_CLASS}" id="${LIST_EMPTY_ID}">
    <table class="layui-table" style="margin:0">
      <thead><tr id="${LIST_HEAD_ID}"></tr></thead>
      <tbody id="${LIST_BODY_ID}"></tbody>
    </table>
  </div>
</div>`
}

/** 无权限时的自检提示（页面被深链打开时用；与路由那道门同源，⛔ 不是第二套判据）。 */
export const NO_ACCESS_MESSAGE = '无权限查看问题反馈'

export const __testables = {
  buildHtml,
  extOf,
  formatTime,
  imageFilesOf,
  imagesHtml,
  isAllowedExt,
  isTooLarge,
  listHeadHtml,
  listHtml,
  pendingHtml,
  rejectReason,
  resolveExt,
  rowHtml,
}

export default {
  mount: async (root: HTMLElement, _ctx): Promise<() => void> => {
    document.title = TITLE
    // ⚠️ 顺序是硬要求（`AGENTS.md` §5-G 陷阱 ③）：先挂样式（`bootstrapIsland`），再塞页面标记，
    //    最后 `initIslandElements`（它要按标记里的 `lay-*` / `layui-form` 初始化）。
    const layui: LayuiNamespace = await bootstrapIsland(['layer', 'form'])
    const layer = layui.layer as LayuiModule
    // ⚠️ `form` 模块**要加载**（上面那句）但本页**不直接调它**：能变成 layui 样式的那些控件
    //    （`layui-textarea` / 两个按钮）本来就是纯 CSS，不需要 `form.render`；
    //    而页面里没有 `lay-skin` 的裸 checkbox / radio / select。⛔ 所以这里**不**留一个
    //    未使用的 `form` 句柄（`no-unused-vars` 也拦着），免得后来者以为"漏调了 render"。
    const msg = (text: string): void => islandMsg(layer as unknown as Record<string, unknown>, text)

    /**
     * ⚠️ **本页自己的门禁**：iframe 可被深链（`page.html?page=feedback`）打开，
     * 那一刻路由的 `meta.permissions` 不生效（先例与理由：`pages/open-apps.ts` 的同名自检）。
     * ⚠️ 注入标记必须排在门禁**之后**：`layui-island/page.ts` 丢掉 `mount` 的返回值，
     * 拒绝分支返回的 cleanup 永远不会被调用 ⇒ 先注入就会让被拒的深链看到整个页面壳。
     * ⚠️ 这里判的是**节点** `home.feedback`（能不能进这一页）；**管理员判据是另一条**
     * （`is_tenant_admin`，决定列与按钮），两者不互相替代：`canNode` 对管理员短路为真，
     * 但持 `home.feedback` 的**非**管理员也照样要能进来提交。
     */
    const me = await fetchMe()
    if (!canNode(me, FEEDBACK_NODE)) {
      msg(NO_ACCESS_MESSAGE)
      return () => {}
    }
    const isAdmin = me.is_tenant_admin === true

    root.innerHTML = buildHtml()
    initIslandElements(layui)

    /** 待提交的截图（已 PUT 成功、拿到 key 的那些）。 */
    let pending: PendingImage[] = []
    /** 在途闸门：提交（防连点写两次）。⚠️ 上传**不用**闸门，用下面的队列（见 F1 那段）。 */
    let submitting = false

    const contentBox = root.querySelector<HTMLTextAreaElement>(`#${CONTENT_TEXTAREA_ID}`)
    const pendingBox = root.querySelector<HTMLElement>(`#${PENDING_BOX_ID}`)
    const fileInput = root.querySelector<HTMLInputElement>(`#${FILE_INPUT_ID}`)
    const formPanel = root.querySelector<HTMLElement>(FORM_SELECTOR)

    /** 把待提交列表铺到那一块（唯一出口，⛔ 别在别处再拼一次）。 */
    function renderPending(): void {
      if (!pendingBox) return
      pendingBox.innerHTML = pending.map((img, i) => pendingHtml(img, i)).join('')
    }

    /** 取列表并铺开（唯一的取数出口）。 */
    async function reload(): Promise<void> {
      const payload = await listFeedback()
      const head = root.querySelector<HTMLElement>(`#${LIST_HEAD_ID}`)
      const body = root.querySelector<HTMLElement>(`#${LIST_BODY_ID}`)
      // ⚠️ 表头与行**用同一个 `isAdmin`**：两处各判一次就会有"多了列头没有格"的中间态。
      if (head) head.innerHTML = listHeadHtml(isAdmin)
      if (body) body.innerHTML = listHtml(payload.items, { isAdmin })
      // 截图**点击预览**：每一次重渲都要重绑一次 `layer.photos`（分组选择器
      // 挂在截图格的类上，常量见 `SHOTS_SELECTOR`）；先例 = `order-screenshots-manage.ts`
      // 照对标 `picmange.js` 的同一条。空列表 / 无图时选择器命中的格里没有 `<img>`，无害。
      ;(layui.layer.photos as unknown as (o: unknown) => void)({
        photos: SHOTS_SELECTOR,
        anim: 5,
      })
    }

    /**
     * 收下一张**已经上传成功**的图（`key` 从签发端点拿、字节已 PUT 到 `upload_url`）。
     *
     * 顺序照 spec §6.3：先 `POST .../media/ticket/` 拿 `upload_url`，再 `putFileToStorage`，
     * **成功之后**才挂进待提交列表——⛔ 不许先挂后传（那会让用户看到一张其实没上去的缩略图，
     * 提交时后端回"文件没传上来"）。
     * ⚠️ `ext` 走 `resolveExt`（名字 → MIME 兜底），⛔ 不用 `extOf(file.name)` 直接发：
     * 粘贴进来的图**常常没有文件名**，那样会发出一个后端必拒的空 ext（红队 F2）。
     * ⚠️ `resolveExt` 回 `null` 理论上到不了这里（`acceptFiles` 已经拦过），但**排队的批次**是
     * 在处理那一刻才判的 ⇒ 这里再判一次并**跳过**，⛔ 不带着 `null` 去打后端。
     */
    async function uploadOne(file: File): Promise<void> {
      const ext = resolveExt(file)
      if (ext === null) {
        msg(MESSAGES.imageBadType)
        return
      }
      const ticket = await issueTicket(ext)
      await putFileToStorage(ticket.upload_url, file)
      pending = [...pending, { key: ticket.key, preview_url: previewUrlOf(file), name: file.name }]
      renderPending()
    }

    /**
     * 收下**一批**文件（按钮选的 / 粘贴来的都走这里 ⇒ **两条入口一条实现**）。
     *
     * ⚠️ 逐张判 `rejectReason`：超限的**当场**说出是哪一条（spec §6.3），
     * 而**其余合规的那几张照常上传**——⛔ 不是"有一张不合规就整批丢掉"
     * （用户一次选 4 张时，丢掉前 3 张合规的才是更糟的行为）。
     * ⚠️ 逐张**串行**上传（不是 `Promise.all`）：`pending` 的追加顺序就是用户在屏幕上看到的顺序，
     * 并发完成顺序会让缩略图乱序；而这里最多 3 张、单张 ≤10 MB，串行的代价可以接受。
     * ⚠️ **张数判据在这一刻求值**（`rejectReason(file, pending.length)`）：这是**承重**的——
     * 本函数会被排队（见 `acceptFiles`），入队那一刻的 `pending` 到这时可能已经变了
     * （前一批上传完成了）。⛔ 别把 `rejectReason` 的求值提到入队时。
     */
    async function acceptFilesInner(files: readonly File[]): Promise<void> {
      for (const file of files) {
        const reason = rejectReason(file, pending.length)
        if (reason !== null) {
          msg(reason)
          continue
        }
        try {
          await uploadOne(file)
        } catch (e) {
          // ⚠️ 上传失败**必须说出来**（⛔ 不静默）：用户以为图在手，提交时才发现少一张。
          msg(e instanceof Error && e.message ? e.message : MESSAGES.failed)
        }
      }
    }

    /**
     * 上传的**串行队列**（本页唯一入口）。
     *
     * ## 为什么是队列而不是"在途就丢掉"（2026-10-08 红队 F1，**重要**）
     *
     * 原实现是 `if (uploading) return`：上一批截图还在串行 PUT（最多 3 张 × 10 MB，可数秒）时，
     * 用户再按一次 Ctrl+V ⇒ **第二批一个提示都没有地丢掉**。用户以为粘上了、提交时才发现少一张。
     * 这直接违反 spec §6.3 的「⛔ 不要静默丢弃」，而且踩的正是业务方最在意的那条交互路径
     * （原话「一般截图工具都是截图后 Ctrl v 的」）。
     *
     * ⇒ 改成**排队**：后到的批次串到前一批之后（`queue = queue.then(...)`）。
     * **两个性质同时保住**：
     *  - **来得晚 ≠ 被丢掉**（每批都会被处理，判据见 `__tests__/feedback.spec.ts` 的
     *    「第一批还在传时再粘一张 ⇒ 第二批最终也传上去」那条）；
     *  - **仍然串行**（顺序就是用户看到的顺序）。
     *
     * ⛔ **不要**改成"并发跑两个 `acceptFiles`"来绕过丢掉：那会打乱缩略图顺序，
     * 也让这个队列失去意义（当初加闸就是为了顺序）。
     * ⚠️ `queue` 必须**吞掉**自己的 rejection（`then(work, work)` 的两支都接）：
     * 一次失败若留在链上，会让**后续每一次**粘贴都直接跳到 catch、**静默不传**——
     * 那正是本函数要消灭的那种缺陷形状。
     */
    let queue: Promise<void> = Promise.resolve()
    function acceptFiles(files: readonly File[]): Promise<void> {
      const run = (): Promise<void> => acceptFilesInner(files)
      const next = queue.then(run, run)
      // 链上留一份**已收口**的 promise（失败在 `acceptFilesInner` 里逐张消化掉了，这里只是兜底）。
      queue = next.then(
        () => undefined,
        () => undefined,
      )
      return next
    }

    // ── 提交区事件 ────────────────────────────────────────────────────────────

    root.querySelector<HTMLButtonElement>(`#${PICK_BUTTON_ID}`)?.addEventListener('click', () => {
      fileInput?.click()
    })

    fileInput?.addEventListener('change', () => {
      const files = Array.from(fileInput.files ?? [])
      // ⚠️ 清空 `value`：不清的话「同一个文件选第二次」不会触发 `change`（浏览器按值判"没变"），
      //    表现是"点了选文件没反应"。
      fileInput.value = ''
      void acceptFiles(files)
    })

    /**
     * **Ctrl+V 粘贴**。⚠️ 挂在**提交区容器**上（不是 `textarea`）——见文件头那一条。
     * ⚠️ **没有图片项就静默不管**（spec §6.3）：粘一段文字进来是很正常的操作，
     * 弹「粘贴失败」是噪音。所以这里**没有** `else { msg(...) }` 那一支。
     * ⚠️ 也**不 `preventDefault`** 当没有图片项时——那会把用户往 textarea 里粘文字的正常行为挡掉。
     * 有图片项时才 `preventDefault`（防浏览器把图片的"文件路径/文件名"当文本插进输入框）。
     */
    const onPaste = (event: Event): void => {
      const clipboard = (
        event as Event & {
          clipboardData?: {
            items?: ArrayLike<{ kind?: string; type?: string; getAsFile?: () => File | null }>
          }
        }
      ).clipboardData
      const files = clipboard?.items ? imageFilesOf(clipboard.items) : []
      if (files.length === 0) return
      event.preventDefault()
      void acceptFiles(files)
    }
    formPanel?.addEventListener('paste', onPaste)

    /** 删掉一张待提交的图（按 `data-feedback-remove` 的**下标**认领，⛔ 不按 key——key 可能重复传）。 */
    pendingBox?.addEventListener('click', (event) => {
      const el = (event.target as HTMLElement | null)?.closest?.(
        '[data-feedback-remove]',
      ) as HTMLElement | null
      if (!el) return
      const index = Number(el.getAttribute('data-feedback-remove'))
      if (!Number.isInteger(index) || index < 0 || index >= pending.length) return
      pending = pending.filter((_, i) => i !== index)
      renderPending()
    })

    /**
     * 提交（spec §4.3 的载荷：`{content, image_keys}`）。
     *
     * ⚠️ 「清空」在**成功之后**才做：失败就把用户写的东西留着（清掉是最气人的那种失败）。
     * ⚠️ 成功之后**先清空、再刷新列表**：刷新失败不该让表单又变回有内容（那看起来像没提交成功）。
     */
    async function submit(): Promise<void> {
      if (submitting) return
      const content = (contentBox?.value ?? '').trim()
      if (content === '') {
        msg(MESSAGES.noContent)
        return
      }
      submitting = true
      try {
        await createFeedback({ content, image_keys: pending.map((img) => img.key) })
        if (contentBox) contentBox.value = ''
        pending = []
        renderPending()
        msg(MESSAGES.submitted)
        try {
          await reload()
        } catch (e) {
          msg(e instanceof Error ? e.message : MESSAGES.failed)
        }
      } catch (e) {
        msg(e instanceof Error ? e.message : MESSAGES.failed)
      } finally {
        submitting = false
      }
    }

    root.querySelector<HTMLButtonElement>(`#${SUBMIT_BUTTON_ID}`)?.addEventListener('click', () => {
      void submit()
    })

    // ── 列表区事件（只有管理员的行里才有那些控件）────────────────────────────────

    /**
     * 处置某一条（`PATCH`，spec §4.4）。
     *
     * ⚠️ 备注从**本行的输入框**读（DOM 是唯一事实），⛔ 不从内存里那份行数据推：
     * 用户改了备注再点按钮，那条路会把改动**静默丢掉**（同 `open-apps.ts` 的
     * `gamesModeFromDom` 那条注释的理由）。
     * ⚠️ 状态那两个值直接写契约里的枚举字面量（`handled` / `pending`），⛔ 不从前端的中文名反推。
     */
    async function act(id: number, act_: string, rowEl: HTMLElement | null): Promise<void> {
      const status: FeedbackStatus = act_ === ACT_REOPEN ? 'pending' : 'handled'
      const noteEl = rowEl?.querySelector<HTMLInputElement>(`.${NOTE_INPUT_CLASS}`)
      const adminNote = noteEl?.value ?? ''
      try {
        // ⚠️ 两个键**都给**（一次改状态 + 备注）：契约允许（spec §4.4「两者都可选，但至少要给一个」）。
        //    ⛔ 不给备注时不能只发 `{status}` 就以为"备注不变"——本页的输入框里就是用户看到的当前值，
        //    他清空它就是要清空（E-0021 同族：键在不在 ≠ 值真假）。
        await handleFeedback(id, { status, admin_note: adminNote })
        msg(status === 'handled' ? MESSAGES.handled : MESSAGES.reopened)
        await reload()
      } catch (e) {
        // ⛔ 非管理员打到这个端点回 403（spec §4.4）⇒ **如实显示后端文案**，
        //    不在前端新造一套写权限判据（`AGENTS.md` §5-D）。
        msg(e instanceof Error ? e.message : MESSAGES.failed)
      }
    }

    // ⚠️ 监听挂在**行容器**上（一条委托），⛔ 不是逐行挂：列表每次 `innerHTML` 重铺，
    //    逐行挂的那些闭包会随节点一起被丢掉（第二行起就"点了没反应"，且**静默**）。
    root.querySelector<HTMLElement>(`#${LIST_BODY_ID}`)?.addEventListener('click', (event) => {
      const el = (event.target as HTMLElement | null)?.closest?.(
        '[data-feedback-act]',
      ) as HTMLElement | null
      if (!el) return
      const id = Number(el.getAttribute('data-feedback-id'))
      const action = el.getAttribute('data-feedback-act') ?? ''
      if (!Number.isInteger(id) || id <= 0) return
      const rowEl = el.closest<HTMLElement>(`.${ROW_CLASS}`)
      void act(id, action, rowEl)
    })

    // 首屏表头与列表：表头先铺一次（空列表时也要有列名，⛔ 不是一片白）。
    const headBox = root.querySelector<HTMLElement>(`#${LIST_HEAD_ID}`)
    if (headBox) headBox.innerHTML = listHeadHtml(isAdmin)
    try {
      await reload()
    } catch (e) {
      msg(e instanceof Error ? e.message : MESSAGES.failed)
    }

    return () => {
      // ⚠️ 卸载时把挂在**容器**上的那个 `paste` 摘掉：不摘的话页面被反复挂载/卸载时
      //    旧闭包仍会触发（一次粘贴发两轮上传，且**静默**）。同款理由见 `open-apps.ts`
      //    关于「别把监听挂到 document 上」的那段注释。
      formPanel?.removeEventListener('paste', onPaste)
      root.innerHTML = ''
    }
  },
} satisfies IslandPage
