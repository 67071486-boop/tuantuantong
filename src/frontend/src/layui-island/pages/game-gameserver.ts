/**
 * 【游戏管理】页的 **layui island 复刻**（对标 `/Game/gameserver`，对标页 `<title>` 是「游戏区服」）。
 *
 * 与对标 `gameserver.js` 的对应（值一律从对标产物取，不凭 layui 默认猜）：
 *  - 工具栏 2 颗按钮（`Game__gameserver.html` L96-97）：`刷新游戏列表`（字形 `&#xe669;`）与
 *    `新增一款游戏`（红、`layui-jia` 私有图标类）；
 *  - 列 5 列（`gameserver.js` 的 `cols` **减去四个开关列**，再加我方一列「图标」，见 §四）：
 *    `gameId`(hide,fixed-left) / `gameName`(fixed-left, 蓝链但**无点击**——对标的改名入口是
 *    0 调用点死代码) / `图标` 150 居中（当前 logo 或首字占位 +「换图标」） /
 *    `游戏排序(数据越大排序越靠前)` 200 居中**行内可编辑** / 操作 100（红「删除游戏」）；
 *  - `page: false`（对标该表不翻页）、`defaultToolbar: ['filter']`（同对象写两次，后写的生效）、
 *    `text.none = '暂无数据'`。
 *
 * 功能**接我们已有的 API**（`/api/business/games/`，`api/config.ts` 的 CRUD 出口）：
 *  - 列表 GET（一次 200 条，接口上限）；
 *  - 排序行内编辑 → `PATCH {sort_order}`；
 *  - 「删除游戏」→ 先弹对标原文的确认框 `确定将游戏删除吗？`，确认后 `DELETE`；
 *  - 「新增一款游戏」→ **对标那页的 1:1 复刻**（`/Game/AddEditGame`，见下）→ `POST {name}`。
 *
 * ## 一、四个开关列**已删除**（业务方裁定）
 *
 * 对标该表有 `是否默认` / `是否为手机游戏` / `是否为陪玩游戏` / `是否禁发短信` 四个行内开关列
 * （`gameserver.js` 的 `isDefulat` / `isMoblieGame` / `isPw` / `isDisbleSendSms`）。
 * 业务方裁定**这四项从页面去掉**，所以：
 *  - 列定义里没有它们，四个 `form.on('switch(...)')` 处理器与 `SWITCHES` 一并删除；
 *  - 新增弹窗里也没有它们（对标的新增页本来就没有这四个开关，见下）；
 *  - ⚠️ **数据库字段 `is_default` / `is_mobile` / `is_companion` / `disable_sms` 保留**
 *    （`apps/business/models.py::Game`）+ 接口可写（`GameSerializer`）——只是**当前没有页面入口**。
 *    将来要重新上屏，改这一个文件即可；**不要**顺手把后端字段或迁移删掉。
 *  - ⚠️ 由此**列表页不再与对标 `gameserver.js` 的 cols 逐列相等**（8 → 4）——这是**业务方裁定的
 *    刻意偏离**，不是漏抄；`pages/__tests__/game-gameserver.spec.ts` 把它钉成 4 列，别按对标补回来。
 *
 * ## 二、新增弹窗 = 对标 `/Game/AddEditGame` 的 1:1 复刻
 *
 * 对标点「新增一款游戏」开的是 **`type:2` 的独立页**（`gameserver.js::addGame()`：
 * `area:['85%','63%']` / `skin:'layui-layer-lan'` / `anim:5` / `shade:[0.3,'#333']` /
 * `shadeClose:true` / `title:'新增游戏'`）。那页（`AddEditGame.html` + `js/addeditgame.js`）的结构：
 *
 *  1. `按字母索引`：27 个方块片（`全部` + `A`–`Z`，`charList` 在 JS 里拼，落点 `.charViewboxs`）；
 *  2. `搜索游戏名称`：一个 `lay-search` 可搜索下拉（597 项 + 空占位「请选择或搜索」）+ 一颗「搜索」按钮；
 *  3. `请选择游戏`：**597 个 `<input type="radio" name="games">`**（每项 `.layui-inline` 宽 200px，
 *     带 `data-name` / `data-firstKey`），点「确定添加到本系统」提交；
 *  4. 列表下方一句橙色 `blockquote`；页脚居中一颗「确定添加到本系统」。
 *
 * 它**不是自由填游戏名**——只能从平台游戏库（597 款，`@/config/game-library.ts`）里挑一个。
 * 这一点与前一版实现（自己填名称 + 两个开关）**不同**，是本次按业务方「1:1 复刻」重做的。
 *
 * 对标那两个**高亮**动作照做（它**不是筛选**，只改字色，列表始终 597 项全在）：
 *  - 点字母片 → `data-firstKey` 等于该字母的那些项内层 `div` 加 `sel`（橙色 `#FDA601`）；
 *  - 下拉选中 → 同名的项加 `sel2`（红色 `#ff0000`、24px），并弹 `已找到，请注意下方红色字体`。
 *
 * ### 三处**刻意不复刻对标缺陷**的地方（`FRONTEND-REPLICA-STANDARD.md` §5：缺陷不复刻、但要登记）
 *
 * | 对标 | 我们 | 理由 |
 * |---|---|---|
 * | 「搜索」按钮**是死按钮**（`lay-submit lay-filter="seachBtn"`，对标全仓无 `submit(seachBtn)` 处理器） | 接成与「下拉选中」**同一个动作**（高亮，不弹提示） | 点了没反应会被当成坏了；外观零变化 |
 * | 点「全部」时 `carchart='全部'` 匹配不到任何 `data-firstKey` ⇒ 高亮全清 | 显式写成「清空高亮」 | 视觉结果与对标相同，只是把它表达成有意为之 |
 * | 「新增成功」后的引导语要求去「更改游戏权限」设权限（对标那套是**按人按游戏**授可见性） | 只 `layer.alert('新增成功')` → 关层 → 刷新列表 | 我方**没有**按人按游戏的可见性机制，照抄那句会把用户指到一个不存在的功能上 |
 * | 表里没有「图标」列 | **新增一列「图标」**：当前 logo（没有就首字占位）+「换图标」 | 业务方 2026-10-20 要游戏 logo 两端展示；对标那版没有上传口 |
 *
 * 另有两处**标记级**偏离：`blockquote` 的「联系**鸡蛋铺**客服」按产品名改成「联系**团团通**客服」；
 * 表单加 `onsubmit="return false"`（对标那页没有，而它那边是独立 iframe 文档、提交只毁它自己；
 * 我们的弹层住在岛页文档里，回车触发的隐式提交会把整个页面导航走）。四处都记在
 * `docs/specs/2026-09-22-game-add-game-picker-design.md` 的偏差表里。
 *
 * ⚠️ 那 597 项里有 **8 项** `data-firstKey` 是 `*`（7 个）或小写 `b`（1 个）⇒ 点任何字母片都
 * 高亮不到它们（照抄对标的脏数据，见 `config/game-library.ts` 文件头）。
 *
 * ## 四、游戏图标（2026-10-20 新增，我方功能；对标没有）
 *
 * 列表多一列「图标」：有 `logo_url` 就显示圆角方块图，没有就显示名称首字色块。
 * 点「换图标」走**后端三步**（与订单截图 / 头像同一条直传口径，⛔ 不是 multipart）：
 *
 *  1. `POST /api/business/games/<id>/logo/ticket`，载荷 `{ext}`（**扩展名，不带点**；后端
 *     `GameLogoTicketSerializer` 只认这一个键）⇒ `{key, upload_url, expires_in}`
 *     （`upload_url` 本机形态是 `/api/media/blob/<key>`，生产是 COS 预签名）；
 *  2. `PUT upload_url`，body = **文件原始字节**（`putFileToStorage`，同源补 CSRF、跨源不带我们的头）；
 *  3. `POST /api/business/games/<id>/logo`，载荷 `{key}` 认领。
 *
 * 传完重取列表（`reload()`）⇒ 该行图标变成服务端签发的 `logo_url`；失败把后端 `msg` 原样交给
 * `layer.msg`（`BizError.message`），⛔ 不自造文案。
 */
import { GAME_LIBRARY, type LibraryGame } from '@/config/game-library'
import { fetchGameLibrary } from '@/api/gameLibrary'
import { putFileToStorage } from '@/layui-island/directUpload'
import { escapeHtml } from '@/layui-island/escape'
import type { IslandContext, IslandPage } from '@/layui/island'
import {
  DELETED_NOTE,
  apiRequest,
  createConfigRow,
  deleteConfigRow,
  listAllConfigPage,
  loadConfigArtifact,
  mountConfigLike,
  openDeletedLayer,
  patchConfigRow,
  type ConfigCol,
} from '@/layui-island/config-replica'

const RESOURCE = 'games' as const

/** 新增弹窗的**证据件 key**（= `public/replica/game-addeditgame.html`）。 */
export const PICKER_ARTIFACT = 'game-addeditgame'

/**
 * 弹层里那层包裹容器的**类名**（不带点：它进 `class="…"`）。
 *
 * ⚠️ 与 `PICKER_SELECTOR` **必须分开两个常量**：写成 `class="${PICKER_SELECTOR}"` 会造出
 * `class=".x"`，选择器永远匹配不到、弹层**静默打不开**（E-0061 ①）。
 */
export const PICKER_CLASS = 'game-addedit-game'
/** 同一个容器的 CSS 选择器（带点）。 */
export const PICKER_SELECTOR = `.${PICKER_CLASS}`
/** 597 个单选的落点容器 id：**我方的挂点**（对标那页没有 id，靠 DOM 结构取）。 */
export const PICKER_LIST_ID = 'gamePickList'

/**
 * 「换图标」临时 `<input type="file">` 的**类名**（不带点：它进 `class="…"`）。
 *
 * ⚠️ 与 `LOGO_INPUT_SELECTOR` **必须分开两个常量**：写成 `class="${LOGO_INPUT_SELECTOR}"`
 * 会造出 `class=".x"`，测试与样式都匹配不到（E-0061 ①）。
 */
export const LOGO_INPUT_CLASS = 'game-logo-file'
/** 同一个 input 的 CSS 选择器（带点）。 */
export const LOGO_INPUT_SELECTOR = `.${LOGO_INPUT_CLASS}`

/**
 * 只收 jpg / png（与头像那条同一口径；后端认领时还会按魔数重判，这里只是文件框的过滤）。
 * ⛔ 不是安全边界——边界在服务端。
 */
export const LOGO_ACCEPT = 'image/jpeg,image/png'

/** 取票的返回（后端 `games/<id>/logo/ticket`）。 */
export type GameLogoTicket = {
  /** 后端生成的对象名；认领时原样交回。 */
  key: string
  /** 浏览器直传目标（本机 = `/api/media/blob/<key>`，生产 = COS 预签名 URL）。 */
  upload_url: string
  expires_in?: number
}

/** 我们 API 的游戏行（`GameSerializer` 的字段）。 */
export type GameRow = {
  id: number
  name: string
  /** 服务端签发的图标读链接（空串 / `null` = 没传过，渲染首字占位）。 */
  logo_url?: string | null
  sort_order: number
  is_active: boolean
  /** ⚠️ `is_default` / `is_mobile` / `is_companion` / `disable_sms` 接口照旧返回、**页面不再渲染**
   * （业务方裁定去掉那 4 列；字段本身保留，见文件头 §一）。 */
  is_default: boolean
  is_mobile: boolean
  is_companion: boolean
  disable_sms: boolean
  created_at: string
  /** 软删除时间（`null` = 没删）。见 D-0085。 */
  deleted_at: string | null
  /** 引用计数（后端只读字段）：删除前那句警告靠它们说清后果。 */
  order_count: number
  category_count: number
  project_count: number
}

/**
 * 删除确认的文案：对标原文那句 + **后果与引用计数**（业务方裁定
 * 「允许删除，但要给一个警告，以后可恢复」，见 D-0085）。
 *
 * 三件事必须说清，缺一件用户就是在瞎点：**下面有什么**、**删了会怎样**、**去哪恢复**。
 * 抽成纯函数是为了让 spec 直接钉住它（不是靠 grep 源码里的字符串自证）。
 */
export function deleteWarning(row: GameRow): string {
  const parts: string[] = []
  if (row.category_count > 0) parts.push(`${row.category_count} 个项目分类`)
  if (row.project_count > 0) parts.push(`${row.project_count} 个常用项目`)
  if (row.order_count > 0) parts.push(`${row.order_count} 笔订单`)
  const inUse = parts.length > 0 ? `<br>该游戏下面有 ${parts.join('、')}。` : ''
  return (
    '确定将游戏删除吗？' +
    inUse +
    '<br>删除后它会（连同下面的分类与项目）从录单和各页下拉里消失；' +
    DELETED_NOTE
  )
}

/* ───────────────────────── 游戏图标（我方功能，见文件头 §四） ───────────────────────── */

/**
 * 没有 logo 时的占位字：名称首字；空名给「游」（与 App 的 `initialOf` 同口径）。
 *
 * ⚠️ 不写成 `name[0]`：`name` 可能带首尾空白（用户输入），空白单元格看起来像没渲染。
 */
export function gameLogoInitial(name: string): string {
  const trimmed = name.trim()
  return trimmed === '' ? '游' : trimmed.slice(0, 1)
}

/** 方块尺寸 / 圆角跟 App 首页那张占位块**同形**（32px 是小表格里的等比缩小版）。 */
const LOGO_TILE_STYLE =
  'display:inline-block;width:32px;height:32px;line-height:32px;border-radius:6px;vertical-align:middle;'

/**
 * 「图标」列的一格：当前 logo 或首字色块 + 「换图标」。
 *
 * 纯函数（可被 spec 直接钉住，不用起 DOM）：`logo_url` 为空串 / `null` / 全空白都回落占位，
 * ⛔ 不渲染 `<img src="">`（空 src 会让浏览器把当前页面地址当图片请求一次，并在格子里留个破图）。
 */
export function logoCellHtml(row: GameRow): string {
  const url = (row.logo_url ?? '').trim()
  const tile =
    url === ''
      ? `<span style="${LOGO_TILE_STYLE}background:#EAF1FE;color:#2B6DE5;font-weight:bold;text-align:center">` +
        `${escapeHtml(gameLogoInitial(row.name))}</span>`
      : `<img src="${escapeHtml(url)}" alt="" style="${LOGO_TILE_STYLE}object-fit:cover">`
  return (
    `${tile}` +
    `<a href="javascript:void(0);" style="color:#1E9FFF;margin-left:6px"` +
    ` data-act="logo" data-id="${row.id}" title="上传新图标">换图标</a>`
  )
}

/**
 * 文件名 → 扩展名（**不带点、小写**）。
 *
 * 后端 `GameLogoTicketSerializer` 只收 `ext` 一个键，且按 `ALLOWED_EXTS`
 * （`jpg` / `jpeg` / `png`）判；⛔ 不拿 `file.type` 去签——扩展名才是对象名的形状。
 * 没有点（如 `logo`）⇒ 空串，让后端回它那句 `只支持 jpg / jpeg / png`。
 */
export function logoFileExt(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : ''
}

/** ① 取票：`POST games/<id>/logo/ticket`，载荷**只有** `{ext}`（见后端序列化器）。 */
export function requestGameLogoTicket(id: number, file: File): Promise<GameLogoTicket> {
  return apiRequest<GameLogoTicket>(`/api/business/games/${id}/logo/ticket`, {
    method: 'post',
    body: { ext: logoFileExt(file.name) },
  })
}

/** ③ 认领：`POST games/<id>/logo`，key 是①签发时那个对象名。 */
export function claimGameLogo(id: number, key: string): Promise<unknown> {
  return apiRequest<unknown>(`/api/business/games/${id}/logo`, {
    method: 'post',
    body: { key },
  })
}

/**
 * 主表列定义（对标 `gameserver.js` 的 `cols` **减去四个开关列**，见文件头 §一）。
 *
 * `field` **不等于** API 键（对标用的是 `gameId`/`orderNum`…）：
 * `templet` 直接读自己那一行的真实字段，`field` 只用于 layui 内部的数据索引与
 * `hide:true` 列的 `colspan` 计数——**改 `field` 打不破渲染**，改 `templet` 才会。
 */
export const COLS: ConfigCol[] = [
  // 对标 `{field:'gameId', title:'游戏Id', width:'', fixed:'left', hide:true}`。
  // ⚠️ `width: ''` 是**空串**（对标原文），layui 对它取默认宽度；照抄会得到 NaN，所以不写。
  { field: 'gameId', title: '游戏Id', fixed: 'left', hide: true, templet: (d: GameRow) => `${d.id}` },
  {
    field: 'gameName',
    title: '游戏名称',
    fixed: 'left',
    // 对标 templet：蓝链 + `margin-left:10px` + `title=""`（空 title 属性照抄），**无点击处理**。
    templet: (d: GameRow) =>
      `<a href="javascript:void(0);" class="" style="color:#1E9FFF;margin-left: 10px;" title="" data-id="${d.id}">${escapeHtml(d.name)}</a>`,
  },
  // **我方新增**（对标没有这一列，见文件头 §四）：当前图标 +「换图标」。
  {
    field: 'logo',
    title: '图标',
    width: 150,
    align: 'center',
    templet: (d: GameRow) => logoCellHtml(d),
  },
  {
    field: 'orderNum',
    title: '游戏排序(数据越大排序越靠前)',
    width: 200,
    align: 'center',
    edit: true,
    // ⚠️ **必须给 templet**：`edit:true` 只让单元格可编辑，**不会**替你把值渲染进去
    // （实测：没有 templet 时 `<div class="layui-table-cell">` 是空的，双击看到的是空输入框）。
    // 对标那一列写的是 `edit: true` **且没有 templet**，但它的 `orderNum` 字段名与数据键同名，
    // layui 的默认渲染就会把值填上；我们的列 `field` 取的是对标的名字（`orderNum`）而真键是
    // `sort_order`，所以要显式把值渲染出来——这是**复刻渲染结果**，不是复刻它的拼写。
    templet: (d: GameRow) => `${d.sort_order}`,
  },
  // 对标 `{fixed:'', title:'操作', width:100, toolbar:'#baroperating', unresize:true}`。
  // `fixed:''`（不固定）与 layui 默认同值 ⇒ 不写；`ui.none=暂无数据` 由 text 给。
  { title: '操作', width: 100, templet: (d: GameRow) => rowActions(d.id) },
]

/* ───────────────────────── 新增弹窗（对标 `/Game/AddEditGame`） ───────────────────────── */

/** 对标 `addeditgame.js` 的 `charList`：`全部` + A–Z，**顺序即对标顺序**。 */
export const ADD_CHARS = [
  '全部', 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M',
  'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z',
] as const

/** 字母索引里那一项「全部」（对标 `carchart` 的初值）。 */
export const ALL_CHAR = '全部'

/** 对标下拉选中后那句 `layer.msg`（读码原文）。 */
export const FOUND_MESSAGE = '已找到，请注意下方红色字体'
/** 对标没选游戏就提交时的提示原文（`addeditgame.js` L74）。 */
export const NOT_PICKED_MESSAGE = '请选择要添加的游戏'
/** 新增成功后我们自己给的那句（对标那句还带「更改游戏权限」引导，我方没有该功能，见文件头）。 */
export const CREATED_MESSAGE = '新增成功'

/** 字母索引的方块片（对标 `addeditgame.js` L23-28 拼出来的 `<span class='item'>`）。 */
export function buildCharChipsHtml(chars: readonly string[] = ADD_CHARS): string {
  return chars.map((c) => `<span class='item'>${escapeHtml(c)}</span>`).join('')
}

/**
 * 可搜索下拉的 `<option>`（对标是服务端直出；我们运行期灌）。
 *
 * ⚠️ 空占位「请选择或搜索」（`value=""`）**在证据件里**（静态标记），这里只出游戏项——
 * 否则每次重开弹层会多灌一个占位。
 */
export function buildGameOptionsHtml(library: readonly LibraryGame[] = GAME_LIBRARY): string {
  return library
    .map(([, name]) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)
    .join('')
}

/**
 * 597 个单选（对标 `AddEditGame.html` 的 `<input type="radio" name="games" …>` 逐项）。
 *
 * ⚠️ 对标每项还带 `data-isPw` / `data-isMoblie`（喂「是否陪玩 / 是否手机」两个开关）——
 * 那两个开关本次已删（文件头 §一），所以**刻意不搬**（见 `config/game-library.ts` 文件头）。
 * 其余属性（`name` / `value` / `title` / `data-name` / `data-firstKey` / `lay-filter=""`）逐字照抄。
 */
export function buildGameRadiosHtml(library: readonly LibraryGame[] = GAME_LIBRARY): string {
  return library
    .map(
      ([firstKey, name]) =>
        `<div class="layui-inline" style="width:200px">
                            <input type="radio" name="games" value="${escapeHtml(name)}" title="${escapeHtml(name)}" data-name="${escapeHtml(name)}" data-firstKey="${escapeHtml(firstKey)}" lay-filter="" />
                        </div>`,
    )
    .join('\n                        ')
}

/**
 * 「没选游戏」的判据（对标 `addeditgame.js` L73：`gameName == null || undefined || ''`）。
 * **纯函数**，返回值是提示文案或 `null`（通过）——抽出来是为了让 spec 直接钉住行为。
 */
export function pickedGameError(value: string | null | undefined): string | null {
  if (value === null || value === undefined || value === '') return NOT_PICKED_MESSAGE
  return null
}

/** 弹层里那个包裹容器（它住在 `.layui-layer-content` 下，**不在 `root` 里**）。 */
function findPicker(): HTMLElement | null {
  return (
    document.querySelector<HTMLElement>(`.layui-layer-content > ${PICKER_SELECTOR}`) ??
    document.querySelector<HTMLElement>(PICKER_SELECTOR)
  )
}

/**
 * 取「每个单选的标题 div」，**顺序与 `radioInputsOf` 一一对应**。
 *
 * layui 把 `<input type="radio">` 渲染成
 * `<div class="layui-unselect layui-form-radio"><i …></i><div>标题</div></div>`
 * （`public/layui/layui.js` 里的 `radio:function(e){…}` 模板），对标高亮加的就是内层那个 `div` 的类。
 */
function radioDivsOf(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('.layui-form-radio > div'))
}

/** 弹层里 597 个单选（**文档顺序**，与 `radioDivsOf` 同序）。 */
function radioInputsOf(container: HTMLElement): HTMLInputElement[] {
  return Array.from(container.querySelectorAll<HTMLInputElement>('input[name="games"]'))
}

/** 清掉两种高亮（对标 `initselgames()` 与 `select(seachGame)` 开头各清一次）。 */
export function clearHighlights(container: HTMLElement): void {
  for (const div of radioDivsOf(container)) div.classList.remove('sel', 'sel2')
}

/**
 * 点字母片：`data-firstKey` 等于该字母的项加 `sel`（橙色）。
 *
 * ⚠️ 对标用 jQuery 的 `.data('firstkey')` 取 `data-firstKey`（jQuery 拼出的小写属性名
 * `data-firstkey` 在 HTML 文档里**大小写不敏感**，所以取得到）。这里照同一条路走
 * `getAttribute('data-firstkey')`——**不要**改成 `dataset.firstKey` / `dataset.firstkey`：
 * `dataset` 是**大小写敏感**的，拼错了症状是「点字母永远不高亮」且不报错。
 *
 * `全部` 一项：对标 `carchart='全部'` 谁也匹配不到 ⇒ 落到「清空高亮」，见文件头偏差表。
 */
export function highlightByLetter(container: HTMLElement, letter: string): void {
  clearHighlights(container)
  if (letter === ALL_CHAR) return
  const inputs = radioInputsOf(container)
  const divs = radioDivsOf(container)
  inputs.forEach((input, i) => {
    if ((input.getAttribute('data-firstkey') ?? '') === letter) divs[i]?.classList.add('sel')
  })
}

/** 下拉选中：同名项加 `sel2`（红、24px）。返回**命中个数**（决定要不要弹「已找到」）。 */
export function highlightByValue(container: HTMLElement, value: string): number {
  clearHighlights(container)
  if (value === '') return 0
  const inputs = radioInputsOf(container)
  const divs = radioDivsOf(container)
  let hits = 0
  inputs.forEach((input, i) => {
    if ((input.getAttribute('data-name') ?? '') === value) {
      divs[i]?.classList.add('sel2')
      hits += 1
    }
  })
  return hits
}

/** 弹层里那个可搜索下拉。 */
function pickerSelectOf(container: HTMLElement): HTMLSelectElement | null {
  return container.querySelector<HTMLSelectElement>('select[lay-filter="seachGame"]')
}

/** 把游戏库灌进弹层：字母片 + 下拉项 + 597 个单选。 */
export function fillPicker(container: HTMLElement, library: readonly LibraryGame[] = GAME_LIBRARY): void {
  const picked = container.querySelector<HTMLInputElement>('input[name="games"]:checked')?.value ?? ''
  const chips = container.querySelector<HTMLElement>('.charViewboxs')
  if (chips) {
    chips.innerHTML = buildCharChipsHtml()
    // 对标 `$('.charViewboxs .item').eq(0).addClass('active')`：默认点亮「全部」。
    chips.querySelector<HTMLElement>('.item')?.classList.add('active')
  }
  const select = pickerSelectOf(container)
  if (select) {
    const placeholder = select.querySelector('option[value=""]')?.outerHTML ?? '<option value="">请选择或搜索</option>'
    select.innerHTML = placeholder + buildGameOptionsHtml(library)
  }
  const list = container.querySelector<HTMLElement>(`#${PICKER_LIST_ID}`)
  if (list) {
    list.innerHTML = buildGameRadiosHtml(library)
    if (picked) {
      const radio = Array.from(list.querySelectorAll<HTMLInputElement>('input[name="games"]'))
        .find((input) => input.value === picked)
      if (radio) radio.checked = true
    }
  }
}

/** 对标 `#baroperating` 的**逐字**标记（图标 + 文案 + `style="margin-right:3px"`）。 */
function rowActions(id: number): string {
  return `<a class="layui-btn layui-btn-xs layui-btn-danger" style="margin-right:3px" data-act="del" data-id="${id}" title="删除该游戏"><i class="layui-icon layui-icon-delete"></i>删除游戏</a>`
}

export async function mountGameServerPage(
  root: HTMLElement,
  ctx: IslandContext,
): Promise<() => void> {
  return mountConfigLike(
    root,
    {
      key: 'game-gameserver',
      // 对标是服务端渲染的 <title>游戏区服</title>（`Game__gameserver.html` L5）；
      // 菜单名是「游戏管理」。菜单/路由名以我们的为准，这里取对标 title。
      title: '游戏区服',
      tableSpec: {
        elem: '#tableList',
        toolbar: '#toolbarDemo',
        defaultToolbar: ['filter'],
        page: false,
        cols: [COLS],
        text: { none: '暂无数据' },
      },
      onReady: ({ root: el, form, table, layer }) => {
        const msg = (text: string): void => {
          ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: 1500 })
        }
        /**
         * 新增弹层是否正在提交（对标那个 `re` 重入闸）。
         *
         * ⚠️ **它住在 `onReady` 的闭包里，活得比弹层久** ⇒ 每次提交**结束**都必须复位
         * （见 `submitCreate` 末尾的 `finally`）。对标那个 `re` 只在失败分支复位，在它那边看不出来：
         * 它的新增页是 `type:2` 的独立 iframe 文档，重新打开 = **新文档** = `re` 重新初始化为 `true`；
         * 我们按 spec 偏差表把它搬进了**同文档**的 `type:1` 弹层 ⇒ 不复位就变成
         * 「新增一款成功后，再新增一款点『确定添加到本系统』没反应」（业务方 2026-09-24 报的 bug，见 E-0079）。
         */
        let saving = false

        /**
         * 重新取数并重画（对标的 `reloadOrderList()`）。返回**是否真的取了**——
         * 「刷新游戏列表」按钮据此决定要不要补一句 `已刷新`（失败了就别盖住错误提示）。
         */
        async function reload(): Promise<boolean> {
          try {
            // **自动翻页取全量**（业务方 2026-09-22 的兜底要求）：接口上限 200，超了不再
            // 只显示前 200 条——真到荒唐规模（`CONFIG_MAX_PAGES` 页）才停下并如实提示。
            const page = await listAllConfigPage<GameRow>(RESOURCE)
            ;(table.reloadData as (id: string, o: unknown) => void)('tableList', {
              data: page.items,
              page: false,
            })
            if (page.truncated) {
              msg(`数据太多，只显示前 ${page.items.length} 条（共 ${page.total} 条）`)
            }
            return true
          } catch (e) {
            msg(e instanceof Error ? e.message : '加载失败')
            return false
          }
        }

        /**
         * 「刷新游戏列表」：重取表格 + 给一句 `已刷新`（业务方要求三页的刷新重做）。
         * 对标那颗按钮点了**页面看不出任何变化**（内容一样就以为坏了）。
         */
        async function refreshAll(): Promise<void> {
          if (await reload()) msg('已刷新')
        }

        /**
         * 「换图标」：弹文件框 → 取票 → 直传原始字节 → 认领 → 重取列表（该行图标随之更新）。
         *
         * 三步的端点与载荷见文件头 §四。⛔ 这里**不压缩、不改尺寸**：图标本来就只有几十像素，
         * 压缩只会让边角发糊；后端认领时会按魔数与上限重判（前端不冒充边界）。
         *
         * ⚠️ **`<input>` 每次点击新建、插进文档再 `click()`**：游离节点在部分浏览器里不弹文件框
         * （同 `pages/user-myinfo.ts` 的头像上传，那个写法已过评审）。
         */
        function uploadLogo(id: number): void {
          const input = document.createElement('input')
          input.type = 'file'
          input.accept = LOGO_ACCEPT
          input.className = LOGO_INPUT_CLASS
          input.style.display = 'none'
          document.body.appendChild(input)
          input.addEventListener('change', () => {
            void (async () => {
              const file = input.files?.[0]
              if (!file) {
                input.remove()
                return
              }
              try {
                const ticket = await requestGameLogoTicket(id, file)
                // ② 原始字节直传（⛔ 不是 multipart）：同源补 CSRF / 跨源不带我们的头由该模块收口。
                await putFileToStorage(ticket.upload_url, file)
                await claimGameLogo(id, ticket.key)
                msg('图标已更新')
                await reload()
              } catch (e) {
                // 失败给**后端 msg**（`apiRequest` 抛 BizError，`putFileToStorage` 给 413 固定文案）。
                msg(e instanceof Error ? e.message : '图标上传失败')
                // 重取一次：不留「换掉了」的假象（该行仍是服务端的那张图）。
                await reload()
              } finally {
                input.remove()
              }
            })()
          })
          input.click()
        }

        /** 行内排序 → `PATCH` 单字段 → 重画。 */
        async function patch(id: number, payload: Record<string, unknown>, ok: string): Promise<void> {
          try {
            await patchConfigRow(RESOURCE, id, payload)
            msg(ok)
            await reload()
          } catch (e) {
            msg(e instanceof Error ? e.message : '保存失败')
            // 失败时也要重画：layui 已经把单元格改成用户输入的值了，不刷回来就是**假象**。
            await reload()
          }
        }

        /** 新增弹窗（对标 `addGame()` 的层参数 + 对标子页的标记）。 */
        async function openCreate(): Promise<void> {
          let html: string
          try {
            html = await loadConfigArtifact(PICKER_ARTIFACT)
          } catch (e) {
            msg(e instanceof Error ? e.message : '弹窗打开失败')
            return
          }
          ;(layer.open as (o: unknown) => void)({
            type: 1,
            title: '新增游戏',
            // 四个层参数逐字取对标 `addGame()`（`type` 由 2 改 1：对标那页是独立 iframe 文档，
            // 我们在岛页文档里用同宽高的表单层承载同一份标记——外观等价，见 spec 偏差表）。
            area: ['85%', '63%'],
            skin: 'layui-layer-lan',
            anim: 5,
            shade: [0.3, '#333'],
            shadeClose: true,
            content: `<div class="${PICKER_CLASS}">${html}</div>`,
            success: () => {
              const box = findPicker()
              if (!box) return
              fillPicker(box)
              // ⚠️ 弹层内容是刚插进文档的：不 `form.render()`，那 597 个单选与那个下拉都还是生 input
              // （看到的会是浏览器原生圆点，与对标渲染出来的不是同一版式）。
              ;(form.render as (type?: string) => void)()
              void fetchGameLibrary()
                .then((rows) => {
                  if (rows.length === 0) return
                  fillPicker(box, rows.map((row) => [row.first_key, row.name]))
                  ;(form.render as (type?: string) => void)()
                })
                .catch(() => { /* 接口失败保留内置清单 */ })
            },
          })
        }

        /** 「确定添加到本系统」：只发游戏名（对标发的是 `gameName` + 它两个隐藏标记，见文件头 §一）。 */
        function submitCreate(): void {
          const box = findPicker()
          const checked = box?.querySelector<HTMLInputElement>('input[name="games"]:checked') ?? null
          const invalid = pickedGameError(checked?.value)
          if (invalid !== null) {
            msg(invalid)
            return
          }
          if (saving) return
          saving = true
          void createConfigRow<GameRow>(RESOURCE, { name: checked!.value })
            .then(
              () => {
                // 对标：`layer.alert(...)` 的确定回调里 `layer.closeAll()` + 重载列表（本层随之关掉）。
                ;(layer.alert as (t: string, cb: () => void) => void)(CREATED_MESSAGE, () => {
                  ;(layer.closeAll as () => void)()
                  void reload()
                })
              },
              (e: Error) => {
                // 失败**不关层**（对标也是：`re=true` + `layer.msg(data.msg)`，用户可以改选后重试）。
                msg(e.message)
              },
            )
            // ⚠️ **复位挂在 `finally` 上，不能只写在失败分支里**：本页的重入闸活在弹层之外
            // （见 `saving` 的声明处）——只在失败分支复位，成功一次之后**这一页再也提交不了**，
            // 而且返回得**静默**（没有请求、没有提示）。同仓先例：`pages/system-roleauthority-edit.ts`、
            // `pages/order-myorderlist.ts` 的 `inflight`/`submitting` 都是 try/finally 口径。
            .finally(() => {
              saving = false
            })
        }

        /** 按 id 找当前列表里那一行（点击委托只带 `data-id`；警告文案要引用计数）。 */
        function rowById(id: number): GameRow | null {
          const rows =
            ((table.cache as Record<string, GameRow[]> | undefined)?.['tableList'] ?? []) as GameRow[]
          return rows.find((r) => r.id === id) ?? null
        }

        /**
         * 删除 = **软删除**（D-0085）：先弹警告（带引用计数），确认后 `DELETE`。
         *
         * 对标原文只有一句「确定将游戏删除吗？」——业务方裁定「允许删除，但要给
         * 一个警告，以后可恢复」，所以这句扩写了：**下面有什么 / 删了会怎样 / 去哪恢复**。
         */
        function confirmRemove(id: number): void {
          const row = rowById(id)
          const text = row
            ? deleteWarning(row)
            : '确定将游戏删除吗？<br>删除后它会从录单和各页下拉里消失；' + DELETED_NOTE
          ;(layer.confirm as (t: string, o: unknown, cb: (i: number) => void) => void)(
            text,
            { title: '提示', btn: ['确定', '取消'] },
            (index: number) => {
              ;(layer.close as (i: number) => void)(index)
              void deleteConfigRow(RESOURCE, id).then(
                async () => {
                  msg('删除成功')
                  await reload()
                },
                (e: Error) => {
                  msg(e.message)
                  // 失败了就重画，让行如实留在列表里（不留下「删掉了」的假象）。
                  void reload()
                },
              )
            },
          )
        }

        /** 工具栏「已删除」：列出软删除过的游戏，可逐个恢复（对标没有这个功能，我们自己的）。 */
        function openDeleted(): void {
          void openDeletedLayer({
            layer,
            resource: RESOURCE,
            title: '已删除的游戏',
            onRestored: reload,
          })
        }

        // ── 弹层内的三件事：字母片 / 下拉 / 保存 ──
        // ⚠️ 三者都在**弹层**里（`layer` 把节点挂在 `document.body` 下，**不在 `root` 里**）
        // ⇒ 字母片用 `document` 级委托 + `closest(PICKER_SELECTOR)` 定位；两个 `form.on` 注册
        // **一次**即可（layui 的事件表是全局的，注册一次对后来插入的同 filter 节点都生效；
        // 注册多次会**叠加**回调 → 一次点击弹多条提示）。
        const onDocClick = (event: Event): void => {
          const target = event.target as HTMLElement | null
          const chip = target?.closest<HTMLElement>('.charViewboxs .item')
          if (!chip) return
          const box = chip.closest<HTMLElement>(PICKER_SELECTOR)
          if (!box) return
          box.querySelectorAll<HTMLElement>('.charViewboxs .item').forEach((c) => c.classList.remove('active'))
          chip.classList.add('active')
          highlightByLetter(box, (chip.textContent ?? '').trim())
        }
        document.addEventListener('click', onDocClick)

        ;(form.on as (e: string, cb: (obj: { value?: string }) => void) => void)(
          'select(seachGame)',
          (obj) => {
            const box = findPicker()
            if (!box) return
            // 对标对**同名项逐个**弹提示（清单里 `卡厄思梦境` 出现两次 ⇒ 对标会弹两次）；
            // 我们正文与提示都照做，只是提示弹一次。
            if (highlightByValue(box, obj.value ?? '') > 0) msg(FOUND_MESSAGE)
          },
        )
        // 对标那颗「搜索」是死按钮（全仓无 `submit(seachBtn)` 处理器）；我们接成与下拉选中同一动作。
        ;(form.on as (e: string, cb: () => void) => void)('submit(seachBtn)', () => {
          const box = findPicker()
          if (box) highlightByValue(box, pickerSelectOf(box)?.value ?? '')
          return false
        })
        ;(form.on as (e: string, cb: () => void) => void)('submit(savebtn)', () => {
          submitCreate()
          return false
        })

        // ── 表格事件：layui 的 `toolbar` / `tool` 自带 `lay-event` 委托，不会吞冒泡 ──
        ;(table.on as (e: string, cb: (obj: { event: string; data?: GameRow }) => void) => void)(
          'toolbar(tableList)',
          (obj) => {
            if (obj.event === 'refresh') void refreshAll()
            else if (obj.event === 'addGameBtn') void openCreate()
            else if (obj.event === 'deletedBtn') openDeleted()
          },
        )
        ;(table.on as (e: string, cb: (obj: { event: string; data: GameRow }) => void) => void)(
          'tool(tableList)',
          (obj) => {
            if (obj.event === 'submitDel') confirmRemove(obj.data.id)
          },
        )
        // 行内排序：只在 change（回车/失焦）提交，载荷只有 `sort_order`（对标 `UpdateGameOrderNum`）。
        ;(table.on as (e: string, cb: (obj: { data: GameRow; value: string; field: string }) => void) => void)(
          'edit(tableList)',
          (obj) => {
            if (obj.field !== 'orderNum') return
            const value = obj.value.trim() === '' ? 0 : Number(obj.value)
            if (!Number.isInteger(value)) {
              msg('排序值只能是数字')
              void reload()
              return
            }

            void patch(obj.data.id, { sort_order: value }, '排序已保存')
          },
        )

        // 我们自己的行内链接（layui 的 `tool` 事件只认 `lay-event`，这里用 data 属性更直白）。
        const onRowClick = (event: Event): void => {
          const target = event.target as HTMLElement
          const logoLink = target.closest<HTMLElement>('a[data-act="logo"]')
          if (logoLink) {
            event.preventDefault()
            const id = Number(logoLink.dataset.id ?? 0)
            if (Number.isInteger(id) && id > 0) uploadLogo(id)
            return
          }
          const a = target.closest<HTMLElement>('a[data-act="del"]')
          if (!a) return
          event.preventDefault()
          confirmRemove(Number(a.dataset.id ?? 0))
        }
        el.addEventListener('click', onRowClick)

        void reload()
        return () => {
          el.removeEventListener('click', onRowClick)
          document.removeEventListener('click', onDocClick)
        }
      },
    },
    ctx,
  )
}

export function mount(root: HTMLElement, ctx: IslandContext): Promise<() => void> {
  return mountGameServerPage(root, ctx)
}

export default { mount } satisfies IslandPage
