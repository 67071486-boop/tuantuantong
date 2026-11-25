// @vitest-environment jsdom
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const EVIDENCE = fs.readFileSync(
  path.resolve(HERE, '../../../../public/replica/user-myinfo.html'), 'utf8',
)

/**
 * 「功能设置」页签里那条**只读红字**在**运行期 DOM** 里的那句（`->`：原文里的 `-&gt;`
 * 到 DOM 文本里会**解码**回来，⛔ 别拿下面那条原文形状去比 DOM）。
 *
 * ⚠️ 它只是**判据的对象**（「它没了」指的就是它）；⛔ 生产代码不许拿它当选择器——
 * 文案是对标产物、会随对标改版漂（spec §10 / §13.5 的 F2 钉的就是这一条）。
 */
const RED_NOTE_TEXT = '请回到您自己的管理系统里操作绑定。功能入口：系统维护->绑定上家系统设置'
/** 同一句在**证据件原文**里的形状（`-&gt;` 是实体）。 */
const RED_NOTE_TEXT_IN_EVIDENCE = '请回到您自己的管理系统里操作绑定。功能入口：系统维护-&gt;绑定上家系统设置'

/**
 * ⚠️ **必须 `vi.hoisted`**：`vi.mock` 的工厂会被提升到文件顶部、并在 ESM import 期就执行，
 * 那时模块顶层的 `const` **还没初始化**（TDZ）⇒ 工厂里直接引用 `MSGS` / `PROFILE` / `GAMES`
 * 会抛 `Cannot access '…' before initialization`。仓内先例：`order-orderrecycle.spec.ts:139`。
 */
const {
  MSGS, FORM_RENDER, FORM_HANDLERS, PROFILE, GAMES, LAYER_OPEN, LAYER_CLOSE, LAYER_MSG,
  MEMBER_ROW, MEMBER_OPENAPI_ROW,
} = vi.hoisted(() => {
  /** 合成值（PII 守卫扫 `src/**`）。⚠️ 每个可编辑字段的值都与「证据件里原本的值」不同，
   *  否则「回填没生效」也会绿。 */
  const profile = {
    username: 'acct-one', display_name: '测试昵称甲', role: '客服',
    id_card_no: '', is_real_name: false, base_salary: '0.00',
    alipay_account: 'ali@example.com', alipay_name: '测试收款甲',
    bank_name: '测试银行甲', bank_card_no: '6222000000000000',
    qq: '10000000', mobile: '13800000000', live_url: 'https://example.invalid/room',
    games: [1],
    // 两个短时读链接：**没有**传过（`null`）↓ 各自由下面「已上传」那几条用例覆盖成合成链接。
    avatar_url: null,
    wechat_qr_url: null,
    // ⭐ 实名开关（spec §10.2；后端 `_realname_summary` 的 `realname: {enabled, remaining}`）。
    //    `enabled: true` = 业务方实测的那台**已启用**的租户（租户名是 PII，⛔ 不写进 `src/**`；
    //    原始记录只在 `.tools/denylist.txt`）——不设它的话，
    //    「点了没反应」那个真 bug 在这份夹具上根本复现不出来（按钮恒隐藏，用户也没得点）。
    //    ⚠️ `remaining` 只是形状（`number | null`），页面不读它。
    realname: { enabled: true, remaining: 4 },
  }
  return {
    MSGS: [] as string[],
    FORM_RENDER: vi.fn(),
    /**
     * `form.on(event, cb)` 登记下来的回调（`event` → 回调）。
     *
     * ⚠️ 为什么要在替身里**记下**它：本页「对接方式」那一格的切换走的是
     * **layui 的真回调** `form.on('select(memberBindingMode)')` ⇒ 判据必须驱动**页面自己登记
     * 的那个回调**（`fireSelectMode`），⛔ 不是调一拍纯函数（那样切了没反应也会绿）。
     * 与 `system-homesystemsetting.spec.ts` 的 `formHandlers` 同一处置。
     */
    FORM_HANDLERS: new Map<string, (obj: unknown) => void>(),
    // `layer.open` 的替身（每个用例在 `installLayerMock` 里装实现）：返回层索引。
    // 形参显式写出来，否则 `mock.calls` 会被推断成空元组。
    LAYER_OPEN: vi.fn((_options: unknown): number => 0),
    LAYER_CLOSE: vi.fn((_index: number): void => {}),
    /** `layer.msg` 的替身：绑定块的长提示（6000ms）走它，参数要能被断言。 */
    LAYER_MSG: vi.fn(),
    PROFILE: profile,
    GAMES: [{ id: 1, name: '测试游戏甲' }, { id: 2, name: '测试游戏乙' }],
    /**
     * 「个人绑定」那一行的夹具。**合成值**（PII 守卫扫 `src/**`）。
     *
     * ⚠️ `password_set: true` 而 `password` 键**根本不存在**（后端 `write_only`）——
     * 这正是「⛔ 不拿 `password_set` 当密码回填」那条判据要的夹具形状。
     * ⚠️ 每个字段的值都与「页面初始值/默认值」不同（否则「回填没生效」也会绿）。
     */
    MEMBER_ROW: {
      id: 9,
      jidanpu_base_url: 'https://jidanpu-a.test.invalid',
      account: 'member-a',
      account_name: '测试员工甲',
      password_set: true,
      source_name: '测试来源甲',
      // ⚠️ 这个值与实现里的默认值（100）**不同** ⇒ 「回填没生效、只是默认值摆在那儿」会被下面 ㉔ 抓到。
      throw_price_percent: 60,
      // ⭐ D-0299：与模型默认（true）同值 ⇒ 这一行钉不住「回填」，判别力在 OPENAPI_ROW 的 false。
      hall_sync_enabled: true,
      enabled: true,
      fast_poll_seconds: 7,
      reconcile_seconds: 300,
      last_run_at: '2026-09-26T10:11:12+08:00',
      last_ok_at: '2026-09-26T10:11:12+08:00',
      last_error: '来源名「测试来源甲」在鸡蛋铺里不存在',
      last_error_at: '2026-09-26T10:11:12+08:00',
      // ⭐ 后端算好的那句人话（spec §9.4 第 2 问）——**必填**（后端 `fields` 白名单里有它）。
      consumer_state: { text: '回调没在动（合成结论）', bad: true },
      checked_at: '2026-09-26T09:00:00+08:00',
      created_at: '2026-09-26T09:00:00+08:00',
      updated_at: '2026-09-26T10:11:12+08:00',
    },
    /**
     * ⭐ **②式那一行**（对接方式那条线）。**合成值**（PII 守卫扫 `src/**`）。
     *
     * ⚠️ `client_secret_set: true` 而 **`client_secret` 键根本不存在**（后端 `write_only`）——
     * 这正是「⛔ 不拿 `client_secret_set` 当密钥回填」那条判据要的夹具形状。
     * ⚠️ `callback_url` 是**合成地址**（仿形状：`https://<域名>/api/jidanpu/callback/<22 位码>`），
     * ⛔ 不是真值、也不是我们对标物的域名。
     * ⚠️ `mode` 与默认值（`password`）**不同** ⇒ 「回填没生效、只是默认值摆在那儿」会当场红。
     */
    MEMBER_OPENAPI_ROW: {
      id: 12,
      mode: 'openapi',
      jidanpu_base_url: 'https://jidanpu-openapi.test.invalid',
      account: 'member-openapi',
      account_name: '测试员工乙',
      password_set: false,
      client_id: 'cid-synthetic-member-0001',
      client_secret_set: true,
      callback_url: 'https://jidanpu-openapi.test.invalid/api/jidanpu/callback/AAAABBBBCCCCDDDDEEEEFF',
      callback_url_reason: '',
      // ⭐ 后端算好的那句人话（spec §9.4 第 2 问）：页面**原样透传**。
      //    ⚠️ 没有 `callback_last_at`——那张表的那一列**不进序列化器**（后端 `fields` 白名单），
      //    页面拿不到它 ⇒ 夹具里也**不许**出现（⛔ 不拿一个后端根本不回的键来凑判据）。
      consumer_state: { text: '回调有人在处理（合成结论）', bad: false },
      // ⚠️ 与 `MEMBER_ROW` 的『测试来源甲』不同 ⇒ ②式的回填/提交都钉得住（⛔ 不是默认名）。
      source_name: '测试来源乙',
      // ⚠️ 与 `MEMBER_ROW` 的 60 不同 ⇒ 两条夹具各钉各的回填，不会互相当成默认值。
      throw_price_percent: 70,
      // ⭐ D-0299：与模型默认（true）**相反** ⇒ 「回填没生效、只是默认开着」当场红（㊸）。
      hall_sync_enabled: false,
      enabled: false,
      fast_poll_seconds: 5,
      reconcile_seconds: 180,
      last_run_at: null,
      last_ok_at: null,
      last_error: '',
      last_error_at: null,
      checked_at: null,
      created_at: '2026-10-09T09:00:00+08:00',
      updated_at: '2026-10-09T09:00:00+08:00',
    },
  }
})

vi.mock('@/layui/island', () => ({
  bootstrapIsland: vi.fn(async () => ({
    form: {
      render: FORM_RENDER,
      on: (event: string, cb: (obj: unknown) => void): void => {
        FORM_HANDLERS.set(event, cb)
      },
    },
    layer: { open: LAYER_OPEN, close: LAYER_CLOSE, msg: LAYER_MSG },
  })),
  initIslandElements: vi.fn(),                       // no-op：页签切换走页面自己的兜底
  islandMsg: (_layer: unknown, text: string) => MSGS.push(text),
}))
vi.mock('@/api/myProfile', () => ({
  fetchMyProfile: vi.fn(), patchMyProfile: vi.fn(), changePassword: vi.fn(),
  // 图片存储线：两个真上传端点也要 mock —— ⛔ 漏一个的话页面里那一跳会打真网络。
  requestMediaTicket: vi.fn(), claimMedia: vi.fn(),
}))
/**
 * 「我的资料 → 功能设置」里那块**个人绑定**的四个出口。
 * ⛔ 漏掉任何一个的话，页面里那一跳会去打真网络（jsdom 里表现为 `XMLHttpRequest is not defined`
 * 之类的**与本次改动无关**的报错）。
 */
vi.mock('@/api/jidanpuMember', () => ({
  fetchMemberJidanpuBinding: vi.fn(),
  saveMemberJidanpuBinding: vi.fn(),
  testMemberJidanpuConnection: vi.fn(),
  syncMemberJidanpuNow: vi.fn(),
}))
/**
 * ⚠️ `@/api/jidanpuUpstream`（上家鸡蛋铺绑定那一块）**整块搬到了**「绑定上家系统设置」页
 * （`pages/system-homesystemsetting.ts`），它的出口与用例都在
 * `system-homesystemsetting.spec.ts`（那边按 URL 分流 `@/api/http`）。
 * ⛔ 不要在本页重新 mocks 它们——没人在本页打那些端点。
 */
vi.mock('@/api/config', () => ({ fetchAllGames: vi.fn(async () => GAMES) }))
/**
 * ⭐ 实名核验的唯一出口。
 * ⛔ 漏掉它的话，点了「开始认证」会去打**真网络**（jsdom 里表现为与本改动无关的报错）。
 * 本页只用这一个函数（`GET /api/auth/realname/` 那两个读端点本页不调——那两段文案不依赖它们）。
 */
vi.mock('@/api/realname', () => ({ verifyRealName: vi.fn() }))

/**
 * 压缩这一步换成**直通桩**（`D-0153` ③）：jsdom 没有 canvas、也不会真解码图片，真压缩会让
 * `loadImage` 的 `onload` 永远不来 ⇒ 用例挂死。桩的签名写全两个形参，否则 `mock.calls[0][1]`
 * 会被推断成越界（仓内先例：本文件 ④c 的注释）。
 */
vi.mock('@/layui-island/imageCompress', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/layui-island/imageCompress')>()
  return { ...actual, compressImage: vi.fn(async (file: File, _quality?: number) => file) }
})

import {
  changePassword, claimMedia, fetchMyProfile, patchMyProfile, requestMediaTicket,
} from '@/api/myProfile'
import {
  fetchMemberJidanpuBinding, saveMemberJidanpuBinding, testMemberJidanpuConnection,
} from '@/api/jidanpuMember'
import { verifyRealName } from '@/api/realname'
import { BizError } from '@/api/errors'
import { ErrorCode } from '@/config/errorCodes'
import { compressImage } from '@/layui-island/imageCompress'
import { AVATAR_UPDATED_MESSAGE_TYPE } from '@/layui-island/messageTypes'
import page, {
  BINDING_PANEL_CLASS, BINDING_PANEL_SELECTOR, BINDING_SELECTORS, EDITABLE_SELECTORS,
  MEMBER_CALLBACK_COPY_BUTTON_ID,
  MEMBER_CALLBACK_COPY_OK_TEXT,
  MEMBER_CALLBACK_MISSING_REASON_TEXT,
  MEMBER_CALLBACK_PENDING_TEXT,
  MEMBER_CALLBACK_REASON_ID,
  MEMBER_CALLBACK_ROW_ID,
  MEMBER_CALLBACK_URL_INPUT_ID,
  MEMBER_CHECK_NOT_PROBED_TEXT,
  MEMBER_CHECK_RESULT_OK_TEXT,
  MEMBER_CLIENT_ID_ITEM_ID,
  MEMBER_CLIENT_ID_REQUIRED_TEXT,
  MEMBER_CLIENT_SECRET_ITEM_ID,
  MEMBER_CLIENT_SECRET_REQUIRED_TEXT,
  MEMBER_GUIDE_ID,
  MEMBER_GUIDE_IMAGES,
  MEMBER_SOURCE_HINT_ID,
  MEMBER_TEST_EMPTY_RESULT_TEXT,
  MEMBER_TEST_RESULT_ID,
  REALNAME_BILLING_NOTICE,
  REALNAME_FAIL_TEXT,
  REALNAME_NAME_INPUT_ID,
  REALNAME_PASS_TEXT,
  REALNAME_RESULT_ID,
  REALNAME_SID_INPUT_ID,
  REALNAME_SUBMIT_BUTTON_ID,
  renderRealName,
  START_CERT_SELECTOR,
  TO_CER_CONTAINER_CLASS,
  TO_CER_SELECTOR,
} from '../user-myinfo'

/**
 * 7 个可编辑字段的**字面量**清单（键 → 证据件里的 `name` 选择器，按 key 升序）。
 *
 * ⛔ **判据不许从生产代码的 `EDITABLE_SELECTORS` 算出来**：那会让期望值与被测值同源
 * （E-0024 的自指断言）——「往 map 里多一列」时 `Object.keys(body)` 与 `Object.keys(map)`
 * 会**一起**变，用例全绿，而页面真的开始 PATCH 一个服务端不接受的键
 * （后端回 200 + `code=1000`「这些字段不能通过本接口修改：…」）。
 * 这里写死，并由下面第 ① 条把导出的 map 与这份字面量**对齐**（≠ 拿它当期望值来源）。
 */
const EDITABLE_FIELDS = [
  ['alipay_account', 'input[name="setAlipayAcc"]'],
  ['alipay_name', 'input[name="setAlipayAccName"]'],
  ['bank_card_no', 'input[name="setBankCardNo"]'],
  ['bank_name', 'input[name="setBankName"]'],
  ['live_url', 'input[name="livestreaming"]'],
  ['mobile', 'input[name="moblie"]'],
  ['qq', 'input[name="qq"]'],
] as const

/**
 * 头像读链接的**合成值**——只仿服务端签发链接的**形状**（本机 `local` 后端的 blob 端点），
 * ⛔ 不是真值、不是真 URL（`AGENTS.md` §5-C：夹具一律用合成值）。
 *
 * 两份**必须不同**：服务端每次 GET 都会**重新签**一个链接（见 `api/myProfile.ts` 的
 * `MyProfile.avatar_url` 契约），⑭ 靠「mount 那份 ≠ 重取那份」才能证明显示确实被刷新了。
 */
const AVATAR_URL = '/api/media/blob/test/users/9/avatar.abc123def456.png'
const AVATAR_URL_FRESH = '/api/media/blob/test/users/9/avatar.fresh987654321.png'

/**
 * 收款码读链接的**合成值**（同上面的口径：只仿形状，⛔ 不是真值）。
 * 两份**必须不同**：短时签名链接会过期（300s），⑲-㉒ 与 ㉒ 靠「重取那份 ≠ 打开层那份」
 * 才能证明 onerror 之后确实**换成了新链接**（而不是把旧串又写了一遍）。
 */
const WECHAT_QR_URL = '/api/media/blob/test/users/9/wechat-qr.test.png'
const WECHAT_QR_URL_FRESH = '/api/media/blob/test/users/9/wechat-qr.fresh987654321.png'

/**
 * 短时链接「换新之后仍打不开」时那句**固定**提示（逐字取生产代码里的同一句）。
 * 判据不只钉它出现，还钉它**不含技术错误串**（合成文案）；⛔ 页面上不该出现技术细节。
 */
const WECHAT_QR_FAILED_TEXT = '收款码图片加载失败，请稍后重试'

async function flush(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
}

/**
 * 夹具里「本租户**已启用**实名认证」这一个事实（`PROFILE.realname.enabled`）。
 *
 * ⚠️ 单独取出来是给 **TS** 用的：`PROFILE` 是 `vi.hoisted` 出来的普通对象，直接写
 * `PROFILE.realname.enabled` 在跨函数边界时会撞上「可能是 `undefined`」（`MyProfile.realname?` 是可选字段）
 * ⇒ 取值只此一处，判据里不重复写那个路径。
 */
const PROFILE_ENABLED: boolean = PROFILE.realname.enabled

async function mountPage(query = ''): Promise<HTMLElement> {
  window.history.replaceState({}, '', `/layui-island/page.html?page=user-myinfo${query}`)
  const root = document.createElement('div')
  document.body.appendChild(root)
  await page.mount(root, { onNavigate: vi.fn() })
  await flush()
  return root
}

/** 「游戏权限」那一格里的复选框（按 DOM 顺序，第 1 个是「全部游戏」）。 */
function gameInputs(root: HTMLElement): HTMLInputElement[] {
  const label = Array.from(root.querySelectorAll<HTMLElement>('.layui-form-label'))
    .find((el) => (el.textContent ?? '').trim() === '游戏权限')
  const item = label?.closest<HTMLElement>('.layui-form-item') ?? null
  expect(item, '证据件里应当有「游戏权限」这一格').not.toBeNull()
  return Array.from(item!.querySelectorAll<HTMLInputElement>('input[name="gamelist"]'))
}

/** 那一格里**被勾选**的那些的 `title`（值取 DOM 属性，取不到就是空串、不会静默匹配）。 */
const checkedGameTitles = (root: HTMLElement): string[] =>
  gameInputs(root).filter((el) => el.checked).map((el) => el.getAttribute('title') ?? '')

/**
 * 点某个上传控件 ⇒ 在页面 `click()` 出来的那个 `<input type=file>` 上派发 `change`。
 *
 * ⚠️ jsdom 里 `input.click()` **不会**真开文件框、也不会自己派发 `change` ⇒ 必须自己造一次
 * （与 ④c / ⑭ 同款，只是抽出来给下面三条新用例共用）。
 */
async function chooseMediaFile(root: HTMLElement, selector: string, file: File): Promise<void> {
  const el = root.querySelector<HTMLElement>(selector)
  expect(el, `证据件里应当有：${selector}`).not.toBeNull()
  el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  await flush()
  const input = document.querySelector<HTMLInputElement>('input[type=file]')
  expect(input, '点上传控件后应当出现 input[type=file]').not.toBeNull()
  Object.defineProperty(input!, 'files', { value: [file] })
  input!.dispatchEvent(new Event('change'))
  await flush()
}

/** 覆盖 `size`（造 10 MB 的真实字节太贵；`File.size` 在 jsdom 里可被 defineProperty 改写）。 */
function withSize(file: File, size: number): File {
  Object.defineProperty(file, 'size', { value: size })
  return file
}

/**
 * 直传那一跳用的 `fetch` 替身：**证据件 URL 也要照顾**（一个用例里可能挂载两次，
 * 而 `vi.stubGlobal` 在**同一用例内不会自动复位**——不照顾它，第二次 `mountPage` 会以
 * `res.text is not a function` 炸掉，症状看起来像页面坏了）。
 */
function stubUploadFetch(): ReturnType<typeof vi.fn> {
  const mock = vi.fn(async (url: string, _init?: RequestInit) => {
    if (String(url).includes('user-myinfo.html')) {
      return { ok: true, status: 200, text: async () => EVIDENCE } as unknown as Response
    }
    return { ok: true, status: 200 } as unknown as Response
  })
  vi.stubGlobal('fetch', mock)
  return mock
}

/**
 * `layer.open` / `layer.close` 的替身，按**真实 layui 的最小形状**把层建出来：
 * 标题进 `.layui-layer-title`、`content` 进 `.layui-layer-content`，然后调 `success(layero, index)`
 * （`layero` 只需 `find(selector)`——与生产代码用的那个形状一致，同 `order-orderlist.ts:3076`）。
 *
 * 为什么不是「记录参数」式的纯替身：判据要打在**真实出口**上——层里的 `<img>` 必须是生产代码
 * 插进去的那一个，`onerror` 也要能真的派发到它身上（`AGENTS.md` §G：断言打在真实出口）。
 */
function installLayerMock(): void {
  const layers = new Map<number, HTMLElement>()
  let seq = 0
  vi.mocked(LAYER_OPEN).mockReset().mockImplementation((options: unknown): number => {
    const opts = options as {
      title?: string
      content?: string
      success?: (layero: { find: (s: string) => { 0?: HTMLElement } }, index: number) => void
    }
    const el = document.createElement('div')
    el.className = 'layui-layer'
    el.innerHTML =
      `<div class="layui-layer-title">${opts.title ?? ''}</div>` +
      `<div class="layui-layer-content">${opts.content ?? ''}</div>`
    document.body.appendChild(el)
    const index = (seq += 1)
    layers.set(index, el)
    opts.success?.({ find: (selector: string) => ({ 0: el.querySelector(selector) ?? undefined }) }, index)
    return index
  })
  vi.mocked(LAYER_CLOSE).mockReset().mockImplementation((index: number): void => {
    layers.get(index)?.remove()
    layers.delete(index)
  })
}

/** 当前文档里最后打开的那个层（`layer.open` 替身建出来的真实 DOM）。 */
function openedLayer(): HTMLElement | null {
  const layers = document.querySelectorAll<HTMLElement>('.layui-layer')
  return layers.length > 0 ? layers[layers.length - 1]! : null
}

/** 「微信收款码」那一格的呈现：`已上传` 那颗的可见性 + 上传那颗的文案。 */
function wechatQrState(root: HTMLElement): { lookDisplay: string; uploadText: string } {
  const look = root.querySelector<HTMLElement>('.lookWxImg')
  const upload = root.querySelector<HTMLElement>('.uploadWx')
  // 非空哨兵：选择器取不到时直接红（而不是让下面读到 `null` 报一个看不懂的错）。
  expect(look, '证据件里应当有 .lookWxImg').not.toBeNull()
  expect(upload, '证据件里应当有 .uploadWx').not.toBeNull()
  return {
    lookDisplay: look!.style.display,
    uploadText: (upload!.textContent ?? '').trim(),
  }
}

/**
 * `window.location` 的替身：只记录**写入与导航方法调用**（同 `components/__tests__/AppTabsBar.spec.ts`
 * 里那份，⛔ 不能 `vi.spyOn(window.location, 'reload')`——本机 jsdom 实测 `Cannot redefine property`）。
 *
 * ⚠️ **必须在 `mountPage()` 之后装**：本页挂载会走 `history.replaceState`，而把它换掉之后再换
 * `location` 会让那条路径失真。装/还原由调用方 `try/finally` 负责。
 */
function installLocationSpy(): { touched: string[]; restore: () => void } {
  const original = Object.getOwnPropertyDescriptor(window, 'location')
  const touched: string[] = []
  const target: Record<string, unknown> = { href: 'http://localhost/' }
  for (const method of ['reload', 'assign', 'replace'] as const) {
    target[method] = (): void => {
      touched.push(`${method}()`)
    }
  }
  Object.defineProperty(window, 'location', {
    value: new Proxy(target, {
      get: (t, prop) => t[prop as string],
      set: (t, prop, value) => {
        touched.push(`set:${String(prop)}`)
        t[prop as string] = value
        return true
      },
    }),
    configurable: true,
  })
  return {
    touched,
    restore: () => {
      if (original) Object.defineProperty(window, 'location', original)
    },
  }
}

beforeEach(() => {
  document.body.innerHTML = ''
  MSGS.length = 0
  FORM_RENDER.mockClear()
  FORM_HANDLERS.clear()
  LAYER_MSG.mockClear()
  installLayerMock()
  vi.mocked(fetchMyProfile).mockReset().mockResolvedValue(PROFILE as never)
  vi.mocked(patchMyProfile).mockReset().mockResolvedValue(PROFILE as never)
  vi.mocked(changePassword).mockReset().mockResolvedValue(null as never)
  // 个人绑定：默认「还没绑定」（后端真会回 `null`）——⛔ 默认给一行会把这些用例
  // 与「空态」那条判据混在一起（且老用例会凭空多出一些回填值）。
  vi.mocked(fetchMemberJidanpuBinding).mockReset().mockResolvedValue(null)
  vi.mocked(saveMemberJidanpuBinding).mockReset().mockResolvedValue(MEMBER_ROW as never)
  vi.mocked(testMemberJidanpuConnection).mockReset().mockResolvedValue({
    ...MEMBER_DETECT_OK,
  })
  // ⚠️ 上家绑定那一块的四个出口的默认桩**已随块搬走**（见上面那条 `vi.mock` 的说明）。
  // ⭐ 实名核验的唯一出口：默认回「通过」——单独给「失败 / 业务码」那几条用例
  //    各自 `mockResolvedValueOnce` / `mockRejectedValueOnce` 覆盖（⛔ 不靠"上一条留下的桩"）。
  vi.mocked(verifyRealName).mockReset().mockResolvedValue({
    result: 'pass', id_card_masked: '110***1X', remaining: 4,
  })
  // 压缩默认**回落原图**（`mockReset` 会把实现一起清掉 ⇒ 必须补回来，否则返回 `undefined`）。
  vi.mocked(compressImage).mockReset().mockImplementation(async (file: File) => file)
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (String(url).includes('user-myinfo.html')) {
      return { ok: true, text: async () => EVIDENCE } as unknown as Response
    }
    throw new Error(`未预期的 fetch：${String(url)}`)
  }))
})

afterEach(() => vi.unstubAllGlobals())

describe('我的资料页（真交互）', () => {
  it('① 装载后把 7 个可编辑字段 + 只读字段回填成接口的值', async () => {
    const root = await mountPage()
    // 生产 map 与**字面量**清单逐项对齐（E-0024：⛔ 期望值不取自被测的那份 map）。
    // 往 map / `MyProfilePatch` 里加一列 ⇒ 本行红；改错某个选择器 ⇒ 下面的回填行红。
    expect(EDITABLE_SELECTORS).toEqual(Object.fromEntries(EDITABLE_FIELDS))
    for (const [key, selector] of EDITABLE_FIELDS) {
      const el = root.querySelector<HTMLInputElement>(selector)
      expect(el, `选择器取不到：${selector}`).not.toBeNull()
      // 把 key 包进对象里比：失败信息里能直接看出是**哪一格**没回填对。
      // ⚠️ 不用 `expect(value, key)` 的第二参形式——`vitest/valid-expect` 只认**字符串字面量**的
      // 第二参，传变量会是一条 lint error（`eslint .` 门禁会红）。
      const expected = String(PROFILE[key as keyof typeof PROFILE])
      expect({ [key]: el!.value }).toEqual({ [key]: expected })
    }
    // 只读那三格
    expect(root.querySelector<HTMLInputElement>('input[name="loginAcc"]')!.value).toBe('acct-one')
    expect(root.querySelector<HTMLInputElement>('input[name="basicSalary"]')!.value).toBe('0.00')
    // 登录账号置为只读（有意偏离，spec §3-D2）
    expect(root.querySelector<HTMLInputElement>('input[name="loginAcc"]')!.disabled).toBe(true)
    // 头像是生成的 data URI
    expect(root.querySelector<HTMLImageElement>('#avatar')!.getAttribute('src'))
      .toMatch(/^data:image\/svg\+xml/)
    // 游戏权限那一格**重建**（spec §6.4）：不是回填，而是 fetchAllGames × `profile.games`。
    // ⛔ 不写这条的话，把那一格整个弄坏（选择器永远匹配不到 / 不重建）也全绿。
    const inputs = gameInputs(root)
    // 非空哨兵：`GAMES` 两款 + 「全部游戏」= 3 行（夹具自带 15 行对标游戏的证据件被整格换掉）。
    expect(inputs.map((el) => el.getAttribute('title'))).toEqual([
      '全部游戏',
      ...GAMES.map((g) => g.name),
    ])
    // `PROFILE.games = [1]`（只勾了「测试游戏甲」）⇒ 未全勾时「全部游戏」**不**勾。
    expect(checkedGameTitles(root)).toEqual(['测试游戏甲'])
    // A4：`lay-skin="primary"` 的复选框必须重渲——`renderGames` 里那句 `form.render('checkbox')`
    // 被删掉时这一格在**成功保存后**会保持未渲染态（E2E 紧接着 reload ⇒ 它抓不到）。
    // ⛔ 不加这条的话，`FORM_RENDER` 只是被注入、被清空，从来没有判据。
    expect(FORM_RENDER).toHaveBeenCalledWith('checkbox')
  })

  it('② 改一个框后点「保存更改」⇒ PATCH 的 body 恰好是那 7 个键', async () => {
    const root = await mountPage()
    root.querySelector<HTMLInputElement>('input[name="moblie"]')!.value = '13900000000'
    root.querySelector<HTMLElement>('button[lay-filter="savebtn"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(patchMyProfile).toHaveBeenCalledTimes(1)
    const body = vi.mocked(patchMyProfile).mock.calls[0]![0] as Record<string, string>
    // ⛔ 期望值是**字面量**（不是 `Object.keys(EDITABLE_SELECTORS)`——那样与生产代码同源，
    // 多一列会两边一起变、全绿，见 E-0024 与 `EDITABLE_FIELDS` 的注释）。
    expect(Object.keys(body).sort()).toEqual(EDITABLE_FIELDS.map(([key]) => key).sort())
    expect(body.mobile).toBe('13900000000')
  })

  it('③ 改密页签填三次点「确认更改」⇒ 三个值原样发出，成功后清空', async () => {
    const root = await mountPage('&tab=password')
    root.querySelector<HTMLInputElement>('input[name="ypwd"]')!.value = 'old-pw-1'
    root.querySelector<HTMLInputElement>('input[name="newPwd"]')!.value = 'new-pw-1'
    root.querySelector<HTMLInputElement>('input[name="renewPwd"]')!.value = 'new-pw-1'
    root.querySelector<HTMLElement>('button[lay-filter="savePwdBtn"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(changePassword).toHaveBeenCalledWith({
      old_password: 'old-pw-1', new_password: 'new-pw-1', confirm_password: 'new-pw-1',
    })
    expect(root.querySelector<HTMLInputElement>('input[name="ypwd"]')!.value).toBe('')
  })

  it('④ 两个「尚未开放」白名单控件**逐个**点 ⇒ 都弹「尚未开放」且零写请求', async () => {
    const root = await mountPage()
    // ⚠️ 白名单只收「仍是『尚未开放』的那几颗」：
    // · 图片存储线：`#upimgBtn`（更换头像）与 `.uploadWx`（上传收款码）已接真上传
    //    ⇒ 由下面 ④b 单独钉；
    // · 实名认证线：`.toCer`（点击进行实名认证）与 `.startCertBtn`（开始认证）
    //    已接真核验流程 ⇒ 由下面 ⑯–21 单独钉。
    // ⛔ 这两颗**不许**再留在本表里：它们已经会开真弹层，留在这里就是**假红**；
    //    ⛔ 反过来，`.bindEmileBtn` / `.changeEmileBtn`（绑定 / 更换邮箱）**必须留在这里**
    //    ——那两颗仍是「尚未开放」（判据见 ⑧）。
    // ⚠️ 期望的那句提示里带的是**按钮自己的文本**（生产代码取 `textContent`）⇒ 这里逐字取
    //    证据件上那两颗的正文：`去绑定` / `更换绑定`——⛔ 不是我方对功能的称呼。
    const table: Array<[string, string]> = [
      ['.bindEmileBtn', '去绑定'],
      ['.changeEmileBtn', '更换绑定'],
    ]
    // 非空哨兵：两项都真的跑到（表被误删成空表时下面的循环恒真）。
    expect(table).toHaveLength(2)
    for (const [selector, label] of table) {
      const el = root.querySelector<HTMLElement>(selector)
      expect(el, `证据件里应当有白名单控件：${selector}`).not.toBeNull()
      // ⛔ **逐个清空 toast**：不清的话「这一次没弹」会被**上一条**留下的提示顶替，
      // 整条判据变成恒真（实测：把 `.uploadWx` 从生产白名单删掉时它照样绿）。
      MSGS.length = 0
      el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      // ⭐ 判据逐字钉「「<按钮文案>」尚未开放」（只判 `includes('尚未开放')`
      //    会把「弹了哪一句」漏掉）——按钮文案取自**证据件**（不是生产代码的常量）。
      expect({ [selector]: MSGS.length }).toEqual({ [selector]: 1 })
      expect({ [selector]: MSGS[0] }).toEqual({ [selector]: `「${label}」尚未开放` })
      // 用对象包一下：失败信息里能直接看出是**哪个选择器**没弹提示（E-0024 族：判据钉值）。
      expect({ [selector]: vi.mocked(patchMyProfile).mock.calls.length }).toEqual({ [selector]: 0 })
      expect({ [selector]: vi.mocked(changePassword).mock.calls.length }).toEqual({ [selector]: 0 })
    }
  })

  it('④b 头像 / 收款码两个控件点下去 ⇒ **要上传凭证**（不再是「尚未开放」）', async () => {
    const root = await mountPage()
    // 两条都要驱动：`.uploadWx` 是另一个**可见**控件，只钉 `#upimgBtn` 会让它静默退化（E-0061 族）。
    for (const [selector] of [['#upimgBtn', 'avatar'], ['.uploadWx', 'wechat-qr']] as const) {
      MSGS.length = 0
      vi.mocked(requestMediaTicket).mockClear()
      const el = root.querySelector<HTMLElement>(selector)
      expect(el, `证据件里应当有：${selector}`).not.toBeNull()
      el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      // ⛔ 不能只断言"没弹尚未开放"——那是恒真判据；要钉**它到底做了什么**。
      expect({ [selector]: (MSGS[0] ?? '').includes('尚未开放') }).toEqual({ [selector]: false })
      // ⚠️ `handleMediaUpload` 会先造 `<input type=file>` 再 `click()`（jsdom 里不会真开文件框），
      //    所以这里钉的是「**点了之后没有走「尚未开放」那条路**」+「没误发资料写请求」；
      //    「真的发起 ticket 请求」由下面的 ④c 用文件事件直驱（否则这条只是恒真的空断言）。
      expect({ [selector]: vi.mocked(patchMyProfile).mock.calls.length }).toEqual({ [selector]: 0 })
    }
  })

  it('④c 选中文件后依次走 ticket → 直传 → 认领（三条腿都要发）', async () => {
    const root = await mountPage()
    vi.mocked(requestMediaTicket).mockResolvedValue({
      key: 'test-schema/users/1/avatar.abc.jpg',
      upload_url: 'https://cos.example.invalid/bucket/avatar.abc.jpg',
      expires_in: 600,
    })
    vi.mocked(claimMedia).mockResolvedValue({ key: 'test-schema/users/1/avatar.abc.jpg' })
    // ⚠️ 形参要显式写出来：`vi.fn(async () => …)` 会让 `mock.calls` 推断成**空元组**，
    //    下面取 `calls[0][0]` 时 TS 直接报「no element at index 0」。
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({ ok: true, status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    const el = root.querySelector<HTMLElement>('#upimgBtn')!
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    // 页面 `click()` 出来的 `input[type=file]` 在 jsdom 里不会派发 change ⇒ 自己造一次
    const input = document.querySelector<HTMLInputElement>('input[type=file]')!
    Object.defineProperty(input, 'files', {
      value: [new File([new Uint8Array([0xff, 0xd8, 0xff])], 'a.jpg', { type: 'image/jpeg' })],
    })
    input.dispatchEvent(new Event('change'))
    await flush()

    expect(vi.mocked(requestMediaTicket).mock.calls[0]).toEqual(['avatar', 'jpg'])
    // 直传那一跳打到**服务端给的 URL**上（⛔ 不是我们自己的 API 前缀）
    expect(fetchMock.mock.calls[0]?.[0]).toBe('https://cos.example.invalid/bucket/avatar.abc.jpg')
    expect(vi.mocked(claimMedia).mock.calls[0]).toEqual(['avatar', 'test-schema/users/1/avatar.abc.jpg'])
  })

  /**
   * `D-0153` ②③：头像 / 收款码的 **10 MB 上限**必须在**发起票据请求之前**
   * 用文件大小拦下（早反馈、零请求）。判据打在**边界两侧**（E-0021）：`10 MB + 1` 必拦、
   * `10 MB`（上界**内含**）必放行——只测一侧的话，把判据写成 `>=` 或写成 MB 以外的单位都不会红。
   */
  it('⑯ >10MB 的头像 / 收款码 ⇒ 零请求 + 早反馈提示（与后端同一句）', async () => {
    for (const [selector] of [['#upimgBtn'], ['.uploadWx']] as const) {
      const fetchMock = stubUploadFetch()
      const root = await mountPage()
      vi.mocked(requestMediaTicket).mockClear()
      vi.mocked(claimMedia).mockClear()
      MSGS.length = 0
      const fetchBefore = fetchMock.mock.calls.length
      await chooseMediaFile(root, selector, withSize(
        new File([new Uint8Array([0xff])], 'oops.png', { type: 'image/png' }),
        10 * 1024 * 1024 + 1,
      ))
      // 三跳**一条都不许发**（票据 / 直传 / 认领）——「零请求」不是「只有票据没发」。
      // ⚠️ 直传那一跳按**增量**算：挂载本身取证据件也走 `fetch`，绝对次数不是 0。
      expect({ [selector]: vi.mocked(requestMediaTicket).mock.calls.length }).toEqual({ [selector]: 0 })
      expect({ [selector]: fetchMock.mock.calls.length - fetchBefore }).toEqual({ [selector]: 0 })
      expect({ [selector]: vi.mocked(claimMedia).mock.calls.length }).toEqual({ [selector]: 0 })
      expect({ [selector]: MSGS.includes('头像与收款码最大支持10MB，请压缩后重试') })
        .toEqual({ [selector]: true })
    }
  })

  it('⑯b ≤10MB ⇒ 压缩 → 票据（ext 用**压缩后**的 `jpg`，不是原文件那个名）→ 直传 → 认领', async () => {
    for (const [selector, kind] of [['#upimgBtn', 'avatar'], ['.uploadWx', 'wechat-qr']] as const) {
      const fetchMock = stubUploadFetch()
      const root = await mountPage()
      vi.mocked(requestMediaTicket).mockClear()
      vi.mocked(claimMedia).mockClear()
      vi.mocked(requestMediaTicket).mockResolvedValue({
        key: `test-schema/users/1/${kind}.abc.jpg`,
        upload_url: `https://cos.example.invalid/bucket/${kind}.abc.jpg`,
        expires_in: 600,
      })
      vi.mocked(claimMedia).mockResolvedValue({ key: `test-schema/users/1/${kind}.abc.jpg` })
      // 压缩**成功**的形态：返回一个**新对象**（字节已是 JPEG，但 `base64ToFile` 保留了原 `.png` 名）。
      vi.mocked(compressImage).mockImplementation(
        async () => new File([new Uint8Array([0xff, 0xd8])], 'oops.png', { type: 'image/jpeg' }),
      )

      await chooseMediaFile(root, selector, withSize(
        new File([new Uint8Array([0xff])], 'oops.png', { type: 'image/png' }), 1024 * 1024,
      ))

      expect({ [selector]: vi.mocked(requestMediaTicket).mock.calls[0] })
        .toEqual({ [selector]: [kind, 'jpg'] })
      expect({ [selector]: fetchMock.mock.calls.map((call) => call[0]).includes(
        `https://cos.example.invalid/bucket/${kind}.abc.jpg`,
      ) }).toEqual({ [selector]: true })
      expect({ [selector]: vi.mocked(claimMedia).mock.calls[0] })
        .toEqual({ [selector]: [kind, `test-schema/users/1/${kind}.abc.jpg`] })
    }
  })

  it('⑯c 压缩失败回落原图 ⇒ ext 取**原文件**的扩展名（字节确实还是原图的）', async () => {
    const root = await mountPage()
    vi.mocked(requestMediaTicket).mockClear()
    vi.mocked(requestMediaTicket).mockResolvedValue({
      key: 'test-schema/users/1/avatar.abc.png',
      upload_url: 'https://cos.example.invalid/bucket/avatar.abc.png',
      expires_in: 600,
    })
    vi.mocked(claimMedia).mockClear()
    vi.mocked(claimMedia).mockResolvedValue({ key: 'test-schema/users/1/avatar.abc.png' })
    vi.stubGlobal('fetch', vi.fn(async (_url: string, _init?: RequestInit) => ({ ok: true, status: 200 })))
    // ⛔ 不覆盖桩：`beforeEach` 那个默认实现就是「原样返回**同一个对象**」＝ 回落原图。

    await chooseMediaFile(root, '#upimgBtn', withSize(
      new File([new Uint8Array([0xff])], 'oops.png', { type: 'image/png' }), 1024 * 1024,
    ))

    expect(vi.mocked(requestMediaTicket).mock.calls[0]).toEqual(['avatar', 'png'])
  })

  /**
   * ⑰ **两条画质判据必须互相排除**：头像走既有四档（1 MB ⇒ 0.6，**可以低于 0.8**），
   * 收款码有**下限 0.8**。若把收款码也实现成 `qualityForSize(size)`，第二条红；
   * 若把头像也实现成 `max(…, 0.8)`，第一条红。只写其中一条都不足以钉住「有意不同」。
   */
  it('⑰ 画质：头像可以低于 0.8、收款码不低于 0.8（两条互相排除）', async () => {
    const root = await mountPage()
    const oneMb = (name: string): File =>
      withSize(new File([new Uint8Array([0xff])], name, { type: 'image/png' }), 1024 * 1024)

    vi.mocked(requestMediaTicket).mockClear()
    vi.mocked(requestMediaTicket).mockResolvedValue({
      key: 'test-schema/users/1/media.abc.png',
      upload_url: 'https://cos.example.invalid/bucket/media.abc.png',
      expires_in: 600,
    })
    vi.mocked(claimMedia).mockClear()
    vi.mocked(claimMedia).mockResolvedValue({ key: 'test-schema/users/1/media.abc.png' })

    vi.mocked(compressImage).mockClear()
    await chooseMediaFile(root, '#upimgBtn', oneMb('avatar.png'))
    // 1 MB 是既有四档里的 < 2 MB 档 ⇒ 0.6（确凿地 < 0.8）。
    expect({ avatar: vi.mocked(compressImage).mock.calls[0]?.[1] }).toEqual({ avatar: 0.6 })

    vi.mocked(compressImage).mockClear()
    await chooseMediaFile(root, '.uploadWx', oneMb('qr.png'))
    // 同一个尺寸，收款码被抬到下限 0.8（`max(0.6, 0.8)`）。
    expect({ wechatQr: vi.mocked(compressImage).mock.calls[0]?.[1] }).toEqual({ wechatQr: 0.8 })
  })

  /**
   * ⭐ **认领成功之后的「刷新」失败，不得覆盖「上传成功」。**
   *
   * 若重新取资料那一跳失败也弹错误，成功提示会被盖掉 ⇒ 用户以为没传上去。
   * 这条钉「成功提示仍在 + 不出现失败提示」。
   */
  it('⑱ 认领成功但**刷新失败** ⇒ 成功提示仍在、不出现失败提示', async () => {
    // mount 那一次成功、上传成功后的那次刷新失败（各一次）。
    vi.mocked(fetchMyProfile).mockReset()
      .mockResolvedValueOnce(PROFILE as never)
      .mockRejectedValueOnce(new Error('刷新失败（合成文案）'))
    const root = await mountPage()
    vi.mocked(requestMediaTicket).mockResolvedValue({
      key: 'test-schema/users/9/avatar.abc.jpg',
      upload_url: 'https://cos.example.invalid/bucket/avatar.abc.jpg',
      expires_in: 600,
    })
    vi.mocked(claimMedia).mockResolvedValue({ key: 'test-schema/users/9/avatar.abc.jpg' })
    stubUploadFetch()

    await chooseMediaFile(root, '#upimgBtn',
      new File([new Uint8Array([0xff, 0xd8, 0xff])], 'a.jpg', { type: 'image/jpeg' }))

    // 前置哨兵：认领真的成功了，且刷新那一跳真的发了、也真的失败了（否则下面两条是恒真）。
    expect({ claims: vi.mocked(claimMedia).mock.calls.length }).toEqual({ claims: 1 })
    expect({ profileFetches: vi.mocked(fetchMyProfile).mock.calls.length })
      .toEqual({ profileFetches: 2 })
    // 判据一：成功提示还在。
    expect(MSGS).toContain('上传成功')
    // 判据二：⛔ **没有**那句失败提示（变异：刷新那一跳不单独 try/catch ⇒ 这句会出现 ⇒ 红）。
    expect(MSGS).not.toContain('刷新失败（合成文案）')
  })

  /**
   * ⑲ **未上传态**（收款码传完「看不出来」那一档的回归闸）：
   * 那一格只有「上传收款码」，「已上传」那颗**不显示**（证据件里它自带 `display:none`）。
   * ⛔ 不留一颗点了没反应的死按钮。
   *
   * ⚠️ **两态都写**（同 ⑫ 的手法）：证据件里那一格的静态默认本来就是「未上传」，所以**只**测一次
   * mount 的「未上传」是**恒真**的判据。这里先让它处于「已上传」，再拿一次**不带链接**的保存回执
   * 把它送未上传态——两个分支都要真的被写到，才证明这一格不是「进去一次就定死」。
   */
  it('⑲ 未上传 ⇒ 只有「上传收款码」，「已上传」不显示（保存回不带链接 ⇒ 回到未上传态）', async () => {
    vi.mocked(fetchMyProfile).mockResolvedValue(
      { ...PROFILE, wechat_qr_url: WECHAT_QR_URL } as never,
    )
    const root = await mountPage()
    // 前置哨兵：先确实到了「已上传」这一态（否则下面的断言只是证据件的静态默认）。
    expect(wechatQrState(root)).toEqual({ lookDisplay: '', uploadText: '重新上传' })

    // 服务端保存回**不带**收款码链接的资料 ⇒ 那一格必须回到未上传态（⛔ 不留死按钮）。
    vi.mocked(patchMyProfile).mockResolvedValue(PROFILE as never)
    root.querySelector<HTMLElement>('button[lay-filter="savebtn"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect({ patches: vi.mocked(patchMyProfile).mock.calls.length }).toEqual({ patches: 1 })
    expect(wechatQrState(root)).toEqual({ lookDisplay: 'none', uploadText: '上传收款码' })
  })

  /**
   * ⑳ **已上传态**（载荷带 `wechat_qr_url`）：上传那颗的文案变「重新上传」（仍是同一颗按钮）、
   * 出现「已上传」；点它开层看图——标题逐字「微信收款码」、层里 `<img>` 的 `src` 就是那个链接。
   */
  it('⑳ 载荷带 wechat_qr_url ⇒「重新上传」+「已上传」；点「已上传」开层看图', async () => {
    vi.mocked(fetchMyProfile).mockResolvedValue(
      { ...PROFILE, wechat_qr_url: WECHAT_QR_URL } as never,
    )
    const root = await mountPage()
    // 两颗按钮的**文案逐字**照对标（`已上传` / `重新上传`）；`display:''` = 显示出来。
    expect(wechatQrState(root)).toEqual({ lookDisplay: '', uploadText: '重新上传' })
    expect((root.querySelector<HTMLElement>('.lookWxImg')!.textContent ?? '').trim()).toBe('已上传')

    root.querySelector<HTMLElement>('.lookWxImg')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()

    // 开层**只此一次**（点一下开一个层）。
    expect({ opens: vi.mocked(LAYER_OPEN).mock.calls.length }).toEqual({ opens: 1 })
    const layerEl = openedLayer()
    expect(layerEl, '点「已上传」应当开层').not.toBeNull()
    expect({ title: (layerEl!.querySelector('.layui-layer-title')?.textContent ?? '').trim() })
      .toEqual({ title: '微信收款码' })
    // 判据钉**值**：层里那张图的 `src` 必须等于载荷给的那个链接（⛔ 不是色板 / 空串）。
    expect({ imgSrc: layerEl!.querySelector<HTMLImageElement>('#WxImg')?.getAttribute('src') })
      .toEqual({ imgSrc: WECHAT_QR_URL })
  })

  /**
   * ㉑ **bug 的核心**：收款码走完 ticket → 直传 → 认领之后，那一格**当场**变成「已上传 / 重新上传」
   * （靠认领成功后重取一次资料），**且没有刷新整页**——整页 `reload()` 会把用户正在填的 7 个框丢掉。
   */
  it('㉑ 收款码上传成功后**当场**变「已上传」（重取资料，且不刷新整页）', async () => {
    const root = await mountPage() // mount 那份：没传过 ⇒ 只有「上传收款码」
    expect(wechatQrState(root)).toEqual({ lookDisplay: 'none', uploadText: '上传收款码' })

    vi.mocked(requestMediaTicket).mockResolvedValue({
      key: 'test-schema/users/9/wechat-qr.abc.jpg',
      upload_url: 'https://cos.example.invalid/bucket/wechat-qr.abc.jpg',
      expires_in: 600,
    })
    vi.mocked(claimMedia).mockResolvedValue({ key: 'test-schema/users/9/wechat-qr.abc.jpg' })
    stubUploadFetch()
    // ⚠️ **不 `mockReset`**：mount 那一次的调用记录要留着数（同 ⑭ 的手法）。
    vi.mocked(fetchMyProfile).mockResolvedValue(
      { ...PROFILE, wechat_qr_url: WECHAT_QR_URL_FRESH } as never,
    )

    // 装 `location` 替身（必须在 mount 之后，见 `installLocationSpy` 的注释）。
    const locationSpy = installLocationSpy()
    try {
      await chooseMediaFile(root, '.uploadWx',
        new File([new Uint8Array([0xff, 0xd8, 0xff])], 'qr.jpg', { type: 'image/jpeg' }))

      // ① 重取发生了：mount 1 次 + 认领成功后 1 次。
      expect({ profileFetches: vi.mocked(fetchMyProfile).mock.calls.length })
        .toEqual({ profileFetches: 2 })
      // ② 那一格**当场**变（⛔ 不是「下次进页面才变」）。
      expect(wechatQrState(root)).toEqual({ lookDisplay: '', uploadText: '重新上传' })
      // ③ 没有刷新整页（`reload` / `assign` / `replace` / 任何 `location` 写入都算导航）。
      expect({ navigations: locationSpy.touched }).toEqual({ navigations: [] })
    } finally {
      locationSpy.restore()
    }
  })

  /**
   * ㉒ 短时签名链接（300s）过期 ⇒ 图片 `onerror`：**重取一次**资料换新链接再试一次；
   * 仍失败 ⇒ 关层 + 一句**固定**提示（⛔ 不留空白层、不把技术错误串给用户看）。
   */
  it('㉒ 图片 onerror ⇒ 重取一次换新链接再试；仍失败 ⇒ 关层 + 固定提示', async () => {
    // mount 那份带旧链接、重取那份带新链接（两份**不同**，否则「换了」与「没换」看不出来）。
    vi.mocked(fetchMyProfile).mockReset()
      .mockResolvedValueOnce({ ...PROFILE, wechat_qr_url: WECHAT_QR_URL } as never)
      .mockResolvedValue({ ...PROFILE, wechat_qr_url: WECHAT_QR_URL_FRESH } as never)
    const root = await mountPage()
    root.querySelector<HTMLElement>('.lookWxImg')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    const img = document.querySelector<HTMLImageElement>('#WxImg')
    expect(img, '层里应当有那张收款码图').not.toBeNull()
    expect({ imgSrc: img!.getAttribute('src') }).toEqual({ imgSrc: WECHAT_QR_URL })

    // 第一次失败 ⇒ 重取一次资料、把同一张图换成**新链接**再试一次（层不动、不弹提示）。
    MSGS.length = 0
    img!.dispatchEvent(new Event('error'))
    await flush()
    expect({ profileFetches: vi.mocked(fetchMyProfile).mock.calls.length })
      .toEqual({ profileFetches: 2 })
    expect({ imgSrc: img!.getAttribute('src') }).toEqual({ imgSrc: WECHAT_QR_URL_FRESH })
    expect({ layers: document.querySelectorAll('.layui-layer').length }).toEqual({ layers: 1 })
    expect(MSGS).toEqual([])

    // 换过新链接**仍失败** ⇒ 关层 + 那句固定提示（⛔ 不是空层、也不是技术错误串）。
    img!.dispatchEvent(new Event('error'))
    await flush()
    expect({ layers: document.querySelectorAll('.layui-layer').length }).toEqual({ layers: 0 })
    expect(MSGS).toEqual([WECHAT_QR_FAILED_TEXT])
    expect({ technical: MSGS.some((m) => m.includes('Error')) }).toEqual({ technical: false })
  })

  it('⑤ `?tab=password` 进来时第三个面板是 `layui-show`', async () => {
    const root = await mountPage('&tab=password')
    const items = root.querySelectorAll<HTMLElement>('.layui-tab-item')
    expect(items[2]!.classList.contains('layui-show')).toBe(true)
    expect(items[0]!.classList.contains('layui-show')).toBe(false)
  })

  it('⑥ 逐个点页签标题 ⇒ **零 toast**（接住 artifact-tab-title 原来守的那条判据）', async () => {
    const root = await mountPage()
    const tabs = Array.from(root.querySelectorAll<HTMLElement>('.layui-tab-title li'))
    // ⚠️ **非空哨兵**：选择器匹配不到任何页签时 `MSGS` 天然为空 ⇒ 「零 toast」是**恒真**的。
    // 先钉住确实是那三个页签（文案逐字）。
    expect(tabs.map((li) => (li.textContent ?? '').trim())).toEqual([
      '基本资料', '功能设置', '修改密码',
    ])
    for (const li of tabs) {
      li.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    }
    expect(MSGS).toEqual([])
  })

  it('⑦ `base_salary` 是 JSON number（含 `0`）时也渲染成两位小数', async () => {
    // ⚠️ 后端把 `Decimal` 交给 DRF 编码 ⇒ 实际到达的是 **number**（可能是 `0`），
    // 不是证据件里那个字符串 `"0.00"`（spec 已为此更正）。只测夹具里那个字符串的话，
    // 把 `Number(x).toFixed(2)` 换成 `String(x)` 也全绿 ⇒ 这一条专门钉住那个格式。
    vi.mocked(fetchMyProfile).mockResolvedValue({ ...PROFILE, base_salary: 0 } as never)
    const zero = await mountPage()
    expect(zero.querySelector<HTMLInputElement>('input[name="basicSalary"]')!.value).toBe('0.00')

    vi.mocked(fetchMyProfile).mockResolvedValue({ ...PROFILE, base_salary: 5000.5 } as never)
    const half = await mountPage()
    expect(half.querySelector<HTMLInputElement>('input[name="basicSalary"]')!.value).toBe('5000.50')
  })

  it('⑧ 游戏权限：「全部游戏」只在**没有**未勾的游戏时 checked', async () => {
    // 与第 ① 条互为对照：那条是「只勾了一款」⇒ 首格不勾。只留 ① 的话，
    // 把 `allChecked` 写死成 `false` 也全绿 ⇒ 这一条钉住另一分支（spec §6.4）。
    vi.mocked(fetchMyProfile).mockResolvedValue({ ...PROFILE, games: [1, 2] } as never)
    const root = await mountPage()
    expect(gameInputs(root).map((el) => el.getAttribute('title'))).toEqual([
      '全部游戏', '测试游戏甲', '测试游戏乙',
    ])
    expect(checkedGameTitles(root)).toEqual(['全部游戏', '测试游戏甲', '测试游戏乙'])

    // 一款都没勾 ⇒ 首格同样**不**勾（`allChecked` 要求 `all.length > 0`）。
    vi.mocked(fetchMyProfile).mockResolvedValue({ ...PROFILE, games: [] } as never)
    const none = await mountPage()
    expect(checkedGameTitles(none)).toEqual([])
  })

  /**
   * ⑨ / ⑩ 钉住**加载窗口**。
   *
   * 背景（真浏览器实测）：标记一注入这些框就可编辑，而 `fillProfile()` 要等
   * `GET /api/auth/profile` 回来才跑 ⇒ 窗口内打进去的字会被**服务端旧值覆盖**，
   * 再点保存就把旧值/空串写回去、页面还报「保存成功」。
   * 修法 = 取数前 `disabled`，回填成功才解锁。下面两条**必须能失败**：把生产代码里那次
   * `setLoadingLock(root, true)` 删掉，⑨ 的第一段就红。
   */
  it('⑨ 取数未回来之前：7 个可编辑框与「保存更改」都 disabled；回填成功后都解除', async () => {
    // 卡住 `fetchMyProfile` ⇒ 页面停在「标记已注入、回填还没跑」这个真实窗口里。
    let release!: (value: unknown) => void
    vi.mocked(fetchMyProfile).mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }) as never,
    )
    const root = document.createElement('div')
    document.body.appendChild(root)
    const mounting = page.mount(root, { onNavigate: vi.fn() })
    await flush()
    await flush()
    const save = root.querySelector<HTMLButtonElement>('button[lay-filter="savebtn"]')
    expect(save, '标记还没注入（设计上注入发生在取数之前）').not.toBeNull()
    // 窗口内：7 个可编辑框全部 disabled（逐个报「是哪一格」）。
    for (const [key, selector] of EDITABLE_FIELDS) {
      const el = root.querySelector<HTMLInputElement>(selector)!
      expect({ [key]: el.disabled }).toEqual({ [key]: true })
    }
    expect({ save: save!.disabled }).toEqual({ save: true })
    // A2：光有 `disabled` 属性不够——`.layui-btn` 自己设了背景/字色/光标，而 `layui.css` 没有
    // `:disabled` 规则 ⇒ 锁着的按钮看起来与可点的**一模一样**、浏览器却把点击静默吞掉。
    // 生产代码改用 layui 自带的 `.layui-btn-disabled`（`background:#fbfbfb;color:#d2d2d2`）。
    expect({ saveDisabledClass: save!.classList.contains('layui-btn-disabled') })
      .toEqual({ saveDisabledClass: true })
    // 「修改密码」页签**不受这道锁影响**（它没有回填、不存在这个窗口）。
    expect(root.querySelector<HTMLButtonElement>('button[lay-filter="savePwdBtn"]')!.disabled)
      .toBe(false)

    // 回填落地 ⇒ 解锁（且只有成功那一支解锁）
    release(PROFILE)
    await mounting
    await flush()
    for (const [key, selector] of EDITABLE_FIELDS) {
      const el = root.querySelector<HTMLInputElement>(selector)!
      expect({ [key]: el.disabled }).toEqual({ [key]: false })
    }
    expect({ save: save!.disabled }).toEqual({ save: false })
    // 解锁时那个「看得见的禁用态」也必须摘掉（只 `removeAttribute` 不摘类 ⇒ 按钮灰着却能点）。
    expect({ saveDisabledClass: save!.classList.contains('layui-btn-disabled') })
      .toEqual({ saveDisabledClass: false })
  })

  it('⑩ 取数失败 ⇒ 保持 disabled 并弹出那句 msg（拿不到旧值就不许在会被覆盖的表单上保存）', async () => {
    vi.mocked(fetchMyProfile).mockRejectedValue(new Error('取数失败（合成文案）'))
    const root = await mountPage()
    expect(MSGS).toContain('取数失败（合成文案）')
    for (const [key, selector] of EDITABLE_FIELDS) {
      const el = root.querySelector<HTMLInputElement>(selector)!
      expect({ [key]: el.disabled }).toEqual({ [key]: true })
    }
    expect({ save: root.querySelector<HTMLButtonElement>('button[lay-filter="savebtn"]')!.disabled })
      .toEqual({ save: true })
    // A2 的第二半：失败后按钮**永久**停在这个状态（直到刷新），所以那个「看得见的禁用态」
    // 更必须有——否则用户看到的是一个与可点时一模一样、点了却毫无反应的按钮。
    expect({
      saveDisabledClass:
        root.querySelector<HTMLElement>('button[lay-filter="savebtn"]')!
          .classList.contains('layui-btn-disabled'),
    }).toEqual({ saveDisabledClass: true })
    // A1：取数失败时那一格也**必须**被重建（生产代码把它挪到了 `if (profile)` 之外）。
    // 否则证据件里 14 个 `checked` 的**对标租户游戏**会被下面的 `form.render()` 当成
    // 「用户自己的权限」渲染上屏——那是对权限的假陈述（spec §6.4 要防的就是它）。
    // 此时 `fetchAllGames()` 成功 ⇒ 应渲染成我们的两款、且一个都不勾（没有资料 ≠ 有权限）。
    expect(gameInputs(root).map((el) => el.getAttribute('title')))
      .toEqual(['全部游戏', ...GAMES.map((g) => g.name)])
    expect(checkedGameTitles(root)).toEqual([])
  })

  /**
   * ⑪ 加载窗口内点「确认更改」**也有反应**（把点击委托提到 `await` 之前的那条判断的判据）。
   * 「修改密码」页签没有回填、没有禁用态 ⇒ 委托挂晚了的话，窗口内点它会**静默无反应**。
   * ⛔ 把生产代码里那次早期 `root.addEventListener(...)` 挪回 `await` 之后，本条就红。
   */
  it('⑪ 加载窗口内点「确认更改」照样发出请求（委托在 await 之前就挂上了）', async () => {
    let release!: (value: unknown) => void
    vi.mocked(fetchMyProfile).mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }) as never,
    )
    const root = document.createElement('div')
    document.body.appendChild(root)
    const mounting = page.mount(root, { onNavigate: vi.fn() })
    await flush()
    await flush()
    root.querySelector<HTMLInputElement>('input[name="ypwd"]')!.value = 'old-pw-1'
    root.querySelector<HTMLInputElement>('input[name="newPwd"]')!.value = 'new-pw-1'
    root.querySelector<HTMLInputElement>('input[name="renewPwd"]')!.value = 'new-pw-1'
    root.querySelector<HTMLElement>('button[lay-filter="savePwdBtn"]')!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(changePassword).toHaveBeenCalledWith({
      old_password: 'old-pw-1', new_password: 'new-pw-1', confirm_password: 'new-pw-1',
    })
    release(PROFILE)
    await mounting
  })

  /**
   * ⑫ 「是否实名」的**已实名**那一支（spec §6.3）。
   *
   * ⭐ `true` 那一态的**图标类名 / 色值 / 文案**是**截图真值**（实名认证线 spec §14-P8）：
   * 图标 `layui-icon-vercode`、色值 `#16baaa`、文案「已通过实名认证」。
   *
   * ⛔ **三个值缺一不可**：只改其中一个（例如留着旧绿 `#009688`）在页面上就是"盾牌配旧绿"，
   * 而用户看的是画面、不是我们的意图。本用例逐字钉三个值 ⇒ 只改两处的提交会当场红。
   * ⚠️ 同时钉住「两态都写」：先 `true` 再 `false`，DOM 必须回到证据件的原样式。
   * 只写 `true` 分支的实现（原形态）在第二步永远回不去。
   */
  it('⑫ `is_real_name` 两态：true ⇒ 绿盾 + #16baaa + 「已通过实名认证」，再 false ⇒ 回到未实名原样式', async () => {
    vi.mocked(fetchMyProfile).mockResolvedValue({ ...PROFILE, is_real_name: true } as never)
    const root = await mountPage()
    const label = Array.from(root.querySelectorAll<HTMLElement>('.layui-form-label'))
      .find((el) => (el.textContent ?? '').trim() === '是否实名')
    const item = label?.closest<HTMLElement>('.layui-form-item') ?? null
    expect(item, '证据件里应当有「是否实名」这一格').not.toBeNull()
    const icon = item!.querySelector<HTMLElement>('i.layui-icon')!
    const text = item!.querySelector<HTMLElement>('.layui-input-inline span')!
    // ⚠️ jsdom 把 `style.color` 归一成 `rgb(...)`（`#16baaa` → `rgb(22, 186, 170)`、
    // `#009688` → `rgb(0, 150, 136)`、`#ff5722` → `rgb(255, 87, 34)`）⇒ 判据按归一后的字面量写
    // （仍钉住**值**，不是恒真）。⭐ 这里**写死字面量**、不从生产代码的常量算（E-0024 的自指断言）。
    const OK_COLOR = 'rgb(22, 186, 170)' // = #16baaa（2026-10-10 截图真值）
    const TIPS_COLOR = 'rgb(255, 87, 34)' // = #ff5722（证据件原值）
    // 已实名：文案 + 图标 + 色值三处一起换。
    expect({ text: text.textContent }).toEqual({ text: '已通过实名认证' })
    expect({ color: text.style.color }).toEqual({ color: OK_COLOR })
    expect({ iconVercode: icon.classList.contains('layui-icon-vercode') }).toEqual({ iconVercode: true })
    expect({ iconOkCircle: icon.classList.contains('layui-icon-ok-circle') }).toEqual({ iconOkCircle: false })
    expect({ tips: icon.classList.contains('layui-icon-tips-fill') }).toEqual({ tips: false })
    expect({ iconColor: icon.style.color }).toEqual({ iconColor: OK_COLOR })

    // 纯函数两态：`false` 必须把三处都改回证据件原值（`#ff5722` + 未实名 + tips 图标）。
    // ⚠️ 第三参（`enabled`）按本夹具 = `true`（已启用）传 ⇒ 这一支是「启用 + 未实名」，
    //    那颗按钮**应当显示**（判据在下面 ㉓ 组，本行只管文案/图标那三处不因它而失真）。
    renderRealName(root, false, PROFILE_ENABLED)
    expect({ text: text.textContent }).toEqual({ text: '未实名' })
    expect({ color: text.style.color }).toEqual({ color: TIPS_COLOR })
    expect({ icon: icon.classList.contains('layui-icon-tips-fill') }).toEqual({ icon: true })
    expect({ ok: icon.classList.contains('layui-icon-vercode') }).toEqual({ ok: false })
    expect({ iconColor: icon.style.color }).toEqual({ iconColor: TIPS_COLOR })
  })

  /**
   * ⑬ `avatar_url` 非空 ⇒ 头像用**服务端签发的**链接，而不是生成的色板 data URI。
   *
   * ⚠️ 夹具 `PROFILE` **没有 `avatar_url` 键** ⇒ 「读它并使用」这一支必须有**单独**的
   * 用例才验得出来（缺键时走不到那一支）。这条把那一支钉住。
   * ⚠️ 链接是合成值（`AVATAR_URL`），只仿形状。
   */
  it('⑬ `avatar_url` 非空 ⇒ 头像用服务端签发的链接（不是色板 data URI）', async () => {
    vi.mocked(fetchMyProfile).mockResolvedValue({ ...PROFILE, avatar_url: AVATAR_URL } as never)
    const root = await mountPage()
    // 钉**值**：等于签发链接本身；等于它就必然**不是** `data:image/svg+xml…`（第 ① 条钉那一支）。
    expect({ avatarSrc: root.querySelector<HTMLImageElement>('#avatar')!.getAttribute('src') })
      .toEqual({ avatarSrc: AVATAR_URL })
  })

  /**
   * ⑭ 上传成功后**重新取一次资料**，并把头像换成重取到的新链接。
   *
   * ⚠️ 「上传成功后页面显示有没有跟着变」必须显式钉住：同时钉两件事——
   *  1. 那次 profile **重取**确实发生了（`mount` 1 次 + 上传成功后 1 次 = 2 次）；
   *  2. 显示随之更新成**重取到的**新链接（⛔ 不是 `mount` 时那份，也不是色板）。
   */
  it('⑭ 上传成功后重新取一次资料，头像换成重取到的新链接', async () => {
    const root = await mountPage() // 首次 `fetchMyProfile` ⇒ `PROFILE`（`avatar_url` 为 `null`）
    vi.mocked(requestMediaTicket).mockResolvedValue({
      key: 'test-schema/users/9/avatar.abc.jpg',
      upload_url: 'https://cos.example.invalid/bucket/avatar.abc.jpg',
      expires_in: 600,
    })
    vi.mocked(claimMedia).mockResolvedValue({ key: 'test-schema/users/9/avatar.abc.jpg' })
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({ ok: true, status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    // ⚠️ **不 `mockReset`**：那会连 `mount` 那一次的调用记录一起清掉，下面就没得数了
    //    ——只换返回值，调用史留着。
    vi.mocked(fetchMyProfile).mockResolvedValue(
      { ...PROFILE, avatar_url: AVATAR_URL_FRESH } as never,
    )

    const el = root.querySelector<HTMLElement>('#upimgBtn')!
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    // 页面 `click()` 出来的 `input[type=file]` 在 jsdom 里不派发 change ⇒ 自己造一次（同 ④c）。
    const input = document.querySelector<HTMLInputElement>('input[type=file]')!
    Object.defineProperty(input, 'files', {
      value: [new File([new Uint8Array([0xff, 0xd8, 0xff])], 'a.jpg', { type: 'image/jpeg' })],
    })
    input.dispatchEvent(new Event('change'))
    await flush()

    // ① 重取发生了：`mount` 那次 + 上传成功后这次。
    expect({ profileFetches: vi.mocked(fetchMyProfile).mock.calls.length })
      .toEqual({ profileFetches: 2 })
    // ② 显示跟着变：`src` 是**重取到的**那一个。
    expect({ avatarSrc: root.querySelector<HTMLImageElement>('#avatar')!.getAttribute('src') })
      .toEqual({ avatarSrc: AVATAR_URL_FRESH })
  })

  /**
   * ⑮ 头像**认领成功后**由岛页通知壳（顶栏头像同步线）。
   *
   * 红线：业务方要的是「传完新头像，右上角跟着变」——岛页与壳是两个文档，
   * 唯一的通道是 `postMessage`（与 `island:navigate` / `island:order-saved` 同形）。
   * 判据打在真实出口上：`window.postMessage` 的调用（jsdom 里 `window.parent === window`，
   * 所以 spy `window.postMessage` 就接在同一个出口上）。
   * ⚠️ 消息类型 import 那份**两端共用的常量**，⛔ 不在测试里另写一遍字符串字面值。
   */
  it('⑮ 头像认领成功后 postMessage 通知壳（类型走两端共用常量）', async () => {
    const postSpy = vi.spyOn(window, 'postMessage').mockImplementation((() => {}) as never)
    try {
      const root = await mountPage()
      // ⚠️ `claimMedia` **不在** `beforeEach` 的 reset 名单里
      //    ⇒ 哨兵只能取**增量**，⛔ 不能断言绝对次数（那会把前一条用例的历史算进来）。
      const claimsBefore = vi.mocked(claimMedia).mock.calls.length
      vi.mocked(requestMediaTicket).mockResolvedValue({
        key: 'test-schema/users/9/avatar.abc.jpg',
        upload_url: 'https://cos.example.invalid/bucket/avatar.abc.jpg',
        expires_in: 600,
      })
      vi.mocked(claimMedia).mockResolvedValue({ key: 'test-schema/users/9/avatar.abc.jpg' })
      vi.stubGlobal('fetch', vi.fn(async (_url: string, _init?: RequestInit) => ({
        ok: true, status: 200,
      })))
      vi.mocked(fetchMyProfile).mockResolvedValue(
        { ...PROFILE, avatar_url: AVATAR_URL_FRESH } as never,
      )

      root.querySelector<HTMLElement>('#upimgBtn')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      // `click()` 出来的 `input[type=file]` 在 jsdom 里不派发 change ⇒ 自己造一次（同 ⑭）。
      const input = document.querySelector<HTMLInputElement>('input[type=file]')!
      Object.defineProperty(input, 'files', {
        value: [new File([new Uint8Array([0xff, 0xd8, 0xff])], 'a.jpg', { type: 'image/jpeg' })],
      })
      input.dispatchEvent(new Event('change'))
      await flush()

      // 前置哨兵：认领真的发生了（否则下面的断言可能来自「压根没走成功路径」）。
      expect({ claims: vi.mocked(claimMedia).mock.calls.length - claimsBefore })
        .toEqual({ claims: 1 })
      // 真实出口：通知壳，目标是**同源**（与 `page.ts::onNavigate` 同一形状）。
      expect(postSpy).toHaveBeenCalledWith(
        { type: AVATAR_UPDATED_MESSAGE_TYPE },
        window.location.origin,
      )
    } finally {
      postSpy.mockRestore()
    }
  })
})

/**
 * ⭐ 「我的资料 → 功能设置」里那块**代练员个人绑定**
 * （spec §9.2，业务方指定的**唯一入口**）。
 *
 * 这是一块**新东西**（本页对标的「功能设置」只有一条只读红字）⇒ 它是**可见偏离**，
 * 逐条登记在 `user-myinfo.ts` 文件头的「已知会不同」表与
 * `docs/FRONTEND-REPLICA-STANDARD.md` §7。
 *
 * ⚠️ 下面每条都走**真交互**：派发真 `click` 事件（打在生产代码挂的那个捕获态委托上），
 * 再断言**请求体**与**提示文案**；⛔ 不写「元素在 DOM 里就算过」的空断言
 * （页面「点了没反应」是**静默**的，见 `AGENTS.md` §5-G）。
 */
/**
 * 「个人绑定测试连接全过」的回包夹具（含 D-0177 新增的字典三个键）。
 *
 * ⚠️ `source_options` 里**故意不含** `MEMBER_ROW.source_name`（『测试来源甲』）：
 * 这样「对方已无此来源 ⇒ 保留该项并标红」那一支在**默认夹具**下就能被覆盖到
 * （⛔ 不需要每个用例各造一份），而「字典里有它」那一档由用例显式覆盖。
 */
const MEMBER_DETECT_OK = {
  ok: true,
  messages: [] as string[],
  source_options: [
    { name: 't-测试来源乙', label: '测试来源乙' },
    { name: 'h-合成来源丙', label: '合成来源丙' },
  ],
  source_options_ok: true,
  source_ok: false,
}

describe('功能设置里的「下家鸡蛋铺绑定」块（下游鸡蛋铺线 Task 9）', () => {
  /**
   * 输入框 + 开关的**字面量**选择器（name → selector）。
   *
   * ⛔ 期望值不取自生产代码的 `BINDING_SELECTORS`（E-0024 自指断言：两边同源时，
   * 「选择器写错」会两边一起变、用例全绿）。下面 ㉓ 把生产 map 与这份字面量**对齐**。
   */
  const FIELDS = {
    baseUrl: 'input[name="member_base_url"]',
    account: 'input[name="member_account"]',
    password: 'input[name="member_password"]',
    // ⭐ D-0177 起「订单来源名」是**下拉**（⛔ 不再是输入框）。
    sourceName: 'select[name="member_source_name"]',
    throwPercent: 'input[name="member_throw_percent"]',
    // ⭐ D-0299：「同步大厅订单」开关（与「启用」同形的 lay-skin=switch；name 不同名）。
    hallSync: 'input[name="member_hall_sync"]',
    enabled: 'input[name="member_enabled"]',
    // ⭐「对接方式」那一轴（②式：官方开放平台 API）。
    mode: 'select[name="member_mode"]',
    clientId: 'input[name="member_client_id"]',
    clientSecret: 'input[name="member_client_secret"]',
  } as const

  /** 绑定块自己（= ㉓ 钉住的那个 class；⛔ 选择器里那个点由生产常量给，别写死）。 */
  const panel = (root: HTMLElement): HTMLElement | null =>
    root.querySelector<HTMLElement>(BINDING_PANEL_SELECTOR)

  /**
   * 取绑定块里那一格（选择器取不到就立刻红，而不是让下面读到 `null`）。
   *
   * ⚠️ 返回类型是**输入框或下拉**：D-0177 起「订单来源名」是 `<select>`。
   * 需要 `.type` 这类**只有 input 有**的属性时用下面的 `inputField()`。
   */
  function field(root: HTMLElement, selector: string): HTMLInputElement | HTMLSelectElement {
    const el = root.querySelector<HTMLInputElement | HTMLSelectElement>(selector)
    expect(el, `绑定块里取不到：${selector}`).not.toBeNull()
    return el!
  }

  /** 同上，但**确定那一格是输入框**（`.type` / `.min` / `.max` 只有 input 有）。 */
  function inputField(root: HTMLElement, selector: string): HTMLInputElement {
    const el = root.querySelector<HTMLInputElement>(selector)
    expect(el, `绑定块里取不到输入框：${selector}`).not.toBeNull()
    return el!
  }

  /** 来源名那一格（**下拉**）：选项文字与选中值都从这里读。 */
  const sourceSelect = (root: HTMLElement): HTMLSelectElement =>
    field(root, FIELDS.sourceName) as HTMLSelectElement

  /** 点一颗绑定块的按钮（走页面挂在 `root` 上的**捕获态**委托）。 */
  async function clickButton(root: HTMLElement, selector: string): Promise<void> {
    const el = root.querySelector<HTMLElement>(selector)
    expect(el, `绑定块里取不到按钮：${selector}`).not.toBeNull()
    el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
  }

  it('㉓ 块落在「功能设置」页签里：类名/选择器分成两个常量、八格 + 开关 + 两颗按钮都在', async () => {
    const root = await mountPage()
    // ① **E-0061 陷阱①**：`class="${SELECTOR}"` 会造出 `class=".x"`、永远匹配不到，
    //    而症状是「页面点了没反应」这种**静默**失败 ⇒ 这里先钉住两个常量确实分开了、
    //    且块上的 `class` 属性**不含前导点**。
    expect(BINDING_PANEL_SELECTOR).toBe(`.${BINDING_PANEL_CLASS}`)
    expect(BINDING_PANEL_CLASS.startsWith('.')).toBe(false)
    const block = panel(root)
    expect(block, '功能设置页签里没有生产代码注入的绑定块').not.toBeNull()
    expect(block!.classList.contains(BINDING_PANEL_CLASS)).toBe(true)

    // 生产 map 与**字面量**清单逐项对齐（同 ① 对 `EDITABLE_SELECTORS` 的手法）。
    expect(BINDING_SELECTORS).toEqual({
      baseUrl: FIELDS.baseUrl,
      account: FIELDS.account,
      password: FIELDS.password,
      sourceName: FIELDS.sourceName,
      throwPercent: FIELDS.throwPercent,
      hallSync: FIELDS.hallSync,
      enabled: FIELDS.enabled,
      mode: FIELDS.mode,
      clientId: FIELDS.clientId,
      clientSecret: FIELDS.clientSecret,
      save: '#memberBindingSaveBtn',
      test: '#memberBindingTestBtn',
      testResult: '#memberBindingTestResult',
      health: '#memberBindingHealth',
    })
    for (const selector of Object.values(FIELDS)) field(root, selector)
    // 它在**第 2 个**页签面板里（`功能设置`；三个页签的顺序见 ⑥）。
    const panels = Array.from(root.querySelectorAll<HTMLElement>('.layui-tab-item'))
    expect(panels[1]!.contains(block!)).toBe(true)
    // ⭐ **有意偏离**（spec §10）：证据件里那段只读红字在**运行期被删**
    //    ⇒ 这个页签里**一个字都不剩**。⛔ 证据件本身**一字未动**（「运行期没有 / 证据件里有」
    //    两半分别钉在下面 ㊴ 与 `user-replica.spec.ts`）。
    expect(panels[1]!.textContent).not.toContain(RED_NOTE_TEXT)
    // 密码框是 `type=password`（不把凭据明码摆在屏幕上）。
    expect(inputField(root, FIELDS.password).type).toBe('password')
    // 两颗按钮的文案（业务方原话：「保存」「测试连接」）。
    expect(root.querySelector('#memberBindingSaveBtn')!.textContent).toContain('保存')
    expect(root.querySelector('#memberBindingTestBtn')!.textContent).toContain('测试连接')
  })

  it('㉔ 回填那一行的值；**密码框永远是空串**（`password_set` 只是布尔，⛔ 不当密码回填）', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    const root = await mountPage()
    expect(field(root, FIELDS.baseUrl).value).toBe(MEMBER_ROW.jidanpu_base_url)
    expect(field(root, FIELDS.account).value).toBe(MEMBER_ROW.account)
    expect(field(root, FIELDS.sourceName).value).toBe(MEMBER_ROW.source_name)
    // 百分比回填的是**库里那个值**（60），⛔ 不是实现里那个「默认 100」——把 `set()` 那一行
    // 删掉、或写成固定 `'100'`，这里就红（夹具值与默认值故意不同）。
    expect(field(root, FIELDS.throwPercent).value).toBe('60')
    // 输入框自己的边界与后端序列化器是**两套**判据：这里钉 HTML 属性那一套（浏览器能挡住明显的手滑）。
    expect(inputField(root, FIELDS.throwPercent).type).toBe('number')
    expect(inputField(root, FIELDS.throwPercent).min).toBe('1')
    expect(inputField(root, FIELDS.throwPercent).max).toBe('100')
    // ⭐ D-0299：「同步大厅订单」回填库里那一格（本行夹具与默认同值；判别力在 ㊸ 的 false）。
    expect(inputField(root, FIELDS.hallSync).checked).toBe(true)
    expect(inputField(root, FIELDS.enabled).checked).toBe(true)
    // ⛔ 承重判据：后端**不回**密码（`write_only`）。把 `password_set` 当密码回填的话，
    //    这里会变成 `true`/`false` 字符串——一条「密码是布尔」的假话。
    expect(field(root, FIELDS.password).value).toBe('')
    // 已配置 ⇒ 给一句人话提示（不是把值摆出来）。
    expect(inputField(root, FIELDS.password).placeholder).toContain('留空')
  })

  it('㉕ 点「保存」⇒ PUT 的 body 来自 DOM（密码空串照传 = 「不改」）', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    const root = await mountPage()
    field(root, FIELDS.baseUrl).value = 'https://jidanpu-b.test.invalid'
    field(root, FIELDS.account).value = 'member-b'
    // ⭐ 来源名这一格现在是**下拉**（D-0177）：新名字要**先从对方字典里选出来**
    //    （⛔ 不能凭空往里打——对没有匹配 option 的 select 赋 `.value` 是**静默变成空串**，
    //    而那正好会踩中「个人侧空串 = 落回默认名」那条静默改写）。
    await clickButton(root, BINDING_SELECTORS.test)
    field(root, FIELDS.sourceName).value = '测试来源乙'
    // 输入框给的是**字符串**（DOM 的 `value` 恒为字符串）⇒ 这一格专门验「页面把它转成了数字」。
    field(root, FIELDS.throwPercent).value = '80'
    inputField(root, FIELDS.enabled).checked = false
    await clickButton(root, BINDING_SELECTORS.save)

    expect(saveMemberJidanpuBinding).toHaveBeenCalledTimes(1)
    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    // 期望值是**字面量**（E-0024）：⛔ 不用 `new Set(...)` / `Object.keys(生产 map)` 当期望值。
    expect(body).toEqual({
      jidanpu_base_url: 'https://jidanpu-b.test.invalid',
      account: 'member-b',
      password: '',
      source_name: '测试来源乙',
      // 数字 80（`Number('80')`），⛔ 不是字符串 `'80'`——后端那一列是整数列，字符串会撞校验。
      throw_price_percent: 80,
      // ⭐ D-0299：开关状态随 DOM 上屏（本用例没动它 ⇒ 回填的 true 原样发出）。
      hall_sync_enabled: true,
      enabled: false,
    })
    // ⛔ 凭据不进 URL（spec §8.5）：载荷**只有**那六个业务键——多一个键（比如把地址塞进
    //    `params`）就会出现在 URL 上，而 URL 会被网关与访问日志记住。类型层与数据层另有
    //    契约用例（`api/__tests__/jidanpuMember.spec.ts` 钉 URL + 恰好六个键）。
    expect(Object.keys(body).sort()).toEqual([
      'account', 'enabled', 'hall_sync_enabled', 'jidanpu_base_url', 'password',
      'source_name', 'throw_price_percent',
    ])
  })

  it('㉕-乙 百分比留空 ⇒ 按 100 提交（空串经 `Number` 会落成 0，那会被后端判成「小于 1」而整单保存失败）', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(null as never)
    const root = await mountPage()
    field(root, FIELDS.baseUrl).value = 'https://jidanpu-b.test.invalid'
    field(root, FIELDS.account).value = 'member-b'
    // 用户把这一格删空（`type=number` 的输入框允许空）。
    field(root, FIELDS.throwPercent).value = ''
    await clickButton(root, BINDING_SELECTORS.save)

    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    // 判据钉在**值**上：⛔ 不是「没报错」，而是载荷里那个数正好是 100（删掉 `|| '100'` 会变成 0）。
    expect({ percent: body.throw_price_percent }).toEqual({ percent: 100 })
  })

  it('㉕-丙 ⭐「同步大厅订单」开关（D-0299）：还没绑定时默认**开**；关掉再保存 ⇒ 载荷如实带 `false`', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(null as never)
    const root = await mountPage()
    field(root, FIELDS.baseUrl).value = 'https://jidanpu-b.test.invalid'
    field(root, FIELDS.account).value = 'member-b'
    // 新行（还没绑定）⇒ 开关默认**开**（＝后端模型默认 True，存量行为不变）。
    expect(inputField(root, FIELDS.hallSync).checked).toBe(true)
    // 用户关掉再保存 ⇒ 载荷如实说 `false`。
    inputField(root, FIELDS.hallSync).checked = false
    await clickButton(root, BINDING_SELECTORS.save)
    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    // ⛔ 能红：回填写成恒 true（本条第一条断言红）／载荷漏这一格或写死 true（下一行红）。
    expect(body.hall_sync_enabled).toBe(false)
  })

  it('㉖ 地址或账号为空时**不发请求**，只提示（不拿注定被拒的请求去打后端）', async () => {
    const root = await mountPage()
    await clickButton(root, BINDING_SELECTORS.save)
    expect(saveMemberJidanpuBinding).not.toHaveBeenCalled()
    expect(MSGS).toContain('请先填鸡蛋铺地址与账号')
  })

  it('㉗ 保存被检测挡下 ⇒ **原样显示后端点名的那句话**（⛔ 不是笼统的「保存失败」）', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    // 后端 `enabled=True` 时同步跑一次检测，不过就**一行都不落**并把缺项逐条点名（人话）。
    vi.mocked(saveMemberJidanpuBinding).mockRejectedValue(
      new Error('该账号打不开「录入订单」页，说明没有对应权限，请在下家鸡蛋铺里给这个职位勾上'),
    )
    const root = await mountPage()
    LAYER_MSG.mockClear()
    await clickButton(root, BINDING_SELECTORS.save)

    // 判据打在**真实出口**（`layer.msg` 的实参）上：文案是后端那句原文，
    // 且停留 6000ms（点名缺项是长句，1500ms 一闪而过等于没提示）。
    expect(LAYER_MSG).toHaveBeenCalledWith(
      '该账号打不开「录入订单」页，说明没有对应权限，请在下家鸡蛋铺里给这个职位勾上',
      { time: 6000 },
    )
    expect(MSGS).not.toContain('保存成功')
  })

  it('㉘ 点「测试连接」：`ok:false` ⇒ 逐条显示后端的 messages；`ok:true` ⇒ 成功提示', async () => {
    const root = await mountPage()
    // 检测没过是**一条正常结论**（信封 code 恒 0）⇒ 页面必须把它说出来。
    vi.mocked(testMemberJidanpuConnection).mockResolvedValue({
      ...MEMBER_DETECT_OK,
      ok: false,
      messages: ['鸡蛋铺里没有名为「团团通系统」的订单来源，请先创建'],
      source_ok: false,
    })
    LAYER_MSG.mockClear()
    await clickButton(root, BINDING_SELECTORS.test)
    expect(testMemberJidanpuConnection).toHaveBeenCalledTimes(1)
    expect(LAYER_MSG).toHaveBeenCalledWith(
      '鸡蛋铺里没有名为「团团通系统」的订单来源，请先创建',
      { time: 6000 },
    )

    vi.mocked(testMemberJidanpuConnection).mockResolvedValue({ ...MEMBER_DETECT_OK })
    MSGS.length = 0
    LAYER_MSG.mockClear()
    await clickButton(root, BINDING_SELECTORS.test)
    // 全过 ⇒ 一句「连接成功」（走本页默认的 1500ms 中性提示，⛔ 不是 6000ms 的错误提示）。
    expect(MSGS).toContain('连接成功')
    expect(LAYER_MSG).not.toHaveBeenCalled()
  })

  it('㉙ 健康状态：上次同步时间 / 上次失败原因（人话）/ 当前间隔，都在块里', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    const root = await mountPage()
    const health = root.querySelector<HTMLElement>(BINDING_SELECTORS.health)
    expect(health, '绑定块里没有健康状态那一段').not.toBeNull()
    const text = health!.textContent ?? ''
    // 时间按本仓既有约定格式化（`YYYY-MM-DD HH:mm`，与订单页 `created_at` 同一处口径）。
    expect(text).toContain('2026-09-26 10:11')
    expect(text).toContain(MEMBER_ROW.last_error)
    // 当前生效的间隔：两个数都要在（快路 7 / 对账 300，与夹具同源但不是自指——
    // 把实现里的字段名写错成 `log_poll_seconds`（那是**租户级**那条线的列名）这里会变空）。
    expect(text).toContain('7')
    expect(text).toContain('300')
    // 抛单百分比也要在：夹具是 60 ⇒ 钉住它是**读**来的，⛔ 不是把默认值 100 摆上去。
    expect(text).toContain('60%')
  })

  // ── ⭐「订单来源名」下拉（D-0177）──────────────────────────────
  // ⚠️ 这一组全部**驱动真交互**（挂载 + 真 DOM + 真回调），⛔ 不只断言「函数被调用」：
  //    下拉「点了没反应」是静默的（`AGENTS.md` §5-G）。

  const sourceHintEl = (root: HTMLElement): HTMLElement =>
    root.querySelector<HTMLElement>(`#${MEMBER_SOURCE_HINT_ID}`)!

  it('㉝ 点「测试连接」⇒ 下拉被**真字典**填满，并把库里那个名字保留 + 选中', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    const root = await mountPage()
    // 还没测过连接 ⇒ 下拉里**只有**库里那个名字（⛔ 不许空着，见 ㉞）
    expect(Array.from(sourceSelect(root).options).map((o) => o.textContent))
      .toEqual(['测试来源甲'])
    await clickButton(root, BINDING_SELECTORS.test)
    const texts = Array.from(sourceSelect(root).options).map((o) => o.textContent)
    expect(texts).toContain('测试来源乙')
    expect(texts).toContain('合成来源丙')
    // 库里那个名字**不在**字典里 ⇒ 它仍在选项里、并且仍被选中（⛔ 不悄悄清空）
    expect(texts).toContain('测试来源甲')
    expect(sourceSelect(root).value).toBe('测试来源甲')
    expect(sourceHintEl(root).textContent).toContain('对方已无此来源，请重选')
    expect(sourceHintEl(root).style.color).toBe('rgb(255, 87, 34)')
  })

  it('㉞ ⭐ 库里有自定义来源名 + **没动下拉** ⇒ 保存时提交的仍是那个名字（⛔ 不是空串、也不是默认名）', async () => {
    // ⚠️ 这是本页最贵的一条静默改写：本页 PUT 的 `validate_source_name` 把**空串**解释成
    //    「落回默认名『团团通系统』」⇒ 一旦下拉能提交空值（或回填顺序反了），用户
    //    「什么都没动」的一次保存就会把自定义来源名**改回默认值**。
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    const root = await mountPage()
    expect(sourceSelect(root).value).toBe('测试来源甲')
    await clickButton(root, BINDING_SELECTORS.save)
    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    expect(body.source_name).toBe('测试来源甲')
    expect(body.source_name).not.toBe('')
    expect(body.source_name).not.toBe('团团通系统')
  })

  it('㉟ ⭐ 从字典里选一个 → 保存：提交的是**选中的那个 label**', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    const root = await mountPage()
    await clickButton(root, BINDING_SELECTORS.test)
    sourceSelect(root).value = '合成来源丙'
    await clickButton(root, BINDING_SELECTORS.save)
    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    expect(body.source_name).toBe('合成来源丙')
  })

  it('㊱ ⭐ 同名多条 ⇒ 提示「我们无法区分它们」，同名项**都留在**下拉里', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    vi.mocked(testMemberJidanpuConnection).mockResolvedValue({
      ...MEMBER_DETECT_OK,
      source_options: [
        { name: 't-合成同名', label: '合成同名' },
        { name: 'x-合成同名', label: '合成同名' },
      ],
    })
    const root = await mountPage()
    await clickButton(root, BINDING_SELECTORS.test)
    expect(Array.from(sourceSelect(root).options).map((o) => o.textContent))
      .toEqual(['合成同名', '合成同名', '测试来源甲'])
    expect(sourceHintEl(root).textContent).toContain('2 个同名来源')
    expect(sourceHintEl(root).textContent).toContain('无法区分')
    expect(sourceHintEl(root).style.color).toBe('rgb(255, 87, 34)')
  })

  it('㊲ ⭐ 字典**没读到** ⇒ ⛔ 一个字都不许说「对方已无此来源」', async () => {
    // ⚠️ 登录就没成时字典也是空的（`source_options_ok=false`）⇒ 少了这个判据，
    //    页面会把「没读到」说成「对方删了」（`AGENTS.md` §5-E）。
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    vi.mocked(testMemberJidanpuConnection).mockResolvedValue({
      ...MEMBER_DETECT_OK,
      ok: false,
      messages: ['登录失败：账号或密码错误'],
      source_options: [],
      source_options_ok: false,
      source_ok: false,
    })
    const root = await mountPage()
    await clickButton(root, BINDING_SELECTORS.test)
    expect(sourceHintEl(root).textContent).not.toContain('对方已无此来源')
    expect(sourceHintEl(root).textContent).toContain('没读到')
    // 那一项仍然保留并选中（⛔ 不因为「没读到」就把用户的值弄没）
    expect(sourceSelect(root).value).toBe('测试来源甲')
  })

  it('㊳ 测试连接把**密码框里此刻填的**密码 + **下拉里此刻选的**来源名一起送去', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    const root = await mountPage()
    inputField(root, FIELDS.password).value = '测试刚换的密码'
    await clickButton(root, BINDING_SELECTORS.test)
    sourceSelect(root).value = '合成来源丙'
    vi.mocked(testMemberJidanpuConnection).mockClear()
    inputField(root, FIELDS.password).value = '测试刚换的密码乙'
    await clickButton(root, BINDING_SELECTORS.test)
    const call = vi.mocked(testMemberJidanpuConnection).mock.calls[0]!
    expect(call[0]).toBe('测试刚换的密码乙')
    expect(call[1]).toBe('合成来源丙')
  })

  it('㉚ 还没绑定时如实写「还没绑定」：来源框给默认名、百分比给 100、开关不勾、其余字段不编值', async () => {
    // `beforeEach` 的默认就是 `null`（后端真会这么回）。
    const root = await mountPage()
    expect(field(root, FIELDS.baseUrl).value).toBe('')
    expect(field(root, FIELDS.account).value).toBe('')
    // 空着会让后端回落到默认名 ⇒ 输入框与库里就是两个名字，用户会以为没保存上。
    expect(field(root, FIELDS.sourceName).value).toBe('团团通系统')
    // 百分比这一格**给** 100：它不是「编出来的值」而是后端的默认值（`default=100`），
    // 摆出来用户才知道自己不填会按什么算；空着还会让 `Number('')` 落成 0（那是非法值）。
    expect(field(root, FIELDS.throwPercent).value).toBe('100')
    expect(field(root, FIELDS.password).value).toBe('')
    expect(inputField(root, FIELDS.enabled).checked).toBe(false)
    expect(root.querySelector(BINDING_SELECTORS.health)!.textContent).toContain('还没绑定')
  })

  /**
   * ㉛ 与「基本资料」同一口径：**取数回来之前不许编辑**（标记一注入输入框就可编辑，而回填要等
   * GET 回来 ⇒ 窗口里打进去的字会被服务端旧值覆盖）。
   * 把生产代码里那句 `setMemberBindingLock(root, true)` 删掉，本条第一段就红。
   */
  it('㉛ 绑定取数未回来之前：五个框 + 开关 + 保存都 disabled；回来后都解除', async () => {
    let release!: (value: unknown) => void
    vi.mocked(fetchMemberJidanpuBinding).mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }) as never,
    )
    const root = document.createElement('div')
    document.body.appendChild(root)
    const mounting = page.mount(root, { onNavigate: vi.fn() })
    await flush()
    await flush()
    // 标记已注入（注入发生在取数之前）⇒ 窗口是**真实存在**的。
    const block = panel(root)
    expect(block, '绑定块还没注入（设计上注入发生在取数之前）').not.toBeNull()
    for (const selector of [
      FIELDS.baseUrl, FIELDS.account, FIELDS.password, FIELDS.sourceName, FIELDS.throwPercent,
    ]) {
      expect({ [selector]: field(root, selector).disabled }).toEqual({ [selector]: true })
    }
    const save = root.querySelector<HTMLButtonElement>('#memberBindingSaveBtn')!
    expect(save.disabled).toBe(true)
    // `layui.css` 没有 `:disabled` 规则 ⇒ 光有属性时按钮看起来与可点时**一模一样**（A2）。
    expect(save.classList.contains('layui-btn-disabled')).toBe(true)

    release(MEMBER_ROW)
    await mounting
    await flush()
    for (const selector of [
      FIELDS.baseUrl, FIELDS.account, FIELDS.password, FIELDS.sourceName, FIELDS.throwPercent,
    ]) {
      expect({ [selector]: field(root, selector).disabled }).toEqual({ [selector]: false })
    }
    expect(root.querySelector<HTMLButtonElement>('#memberBindingSaveBtn')!.disabled).toBe(false)
  })

  /**
   * 「功能设置」那个面板（⛔ **不**借生产代码的常量找它：按**页签标题**与页签**同序**取，
   * 与下面这两条判据要验的「删对了没有」互不共源）。
   */
  function featurePanel(root: HTMLElement): HTMLElement {
    const titles = Array.from(root.querySelectorAll<HTMLElement>('.layui-tab-title > li'))
    const index = titles.findIndex((li) => (li.textContent ?? '').trim() === '功能设置')
    expect(index, '页签标题里没有「功能设置」').toBeGreaterThanOrEqual(0)
    const panels = Array.from(root.querySelectorAll<HTMLElement>('.layui-tab-item'))
    expect(panels[index], '「功能设置」页签没有对应的面板').toBeDefined()
    return panels[index]!
  }

  /**
   * ⭐ **spec §13.5 的 F1**：那条只读红字**运行期 DOM 里一个字都没有**、而**证据件里必须有**。
   *
   * 为什么写成「一对」而不是只写一半（spec §10.2 的口径）：
   *  - 「运行期没有」单独成立**不说明我们做对了**——把证据件那一行删掉、什么代码都不写，同样成立，
   *    而那正是本仓最不能做的事（证据件是「对标渲染结果」的留档，改它＝伪造证据，§5-D）；
   *  - 「证据件里有」就是**我们没动证据件**这条的机械判据（配 `user-replica.spec.ts` 那一条）。
   *
   * ⚠️ **能红**（实测读数见交付报告）：把 `mount()` 里那句 `removeReadonlyBindingNote(root)` 删掉
   * ⇒ ㉓ / ㊴ / ㊵ 一起红（㊵ 也钉了「真的那一条没了」）；把证据件那一行删掉 ⇒ 本用例第一段红。
   */
  it('㊴ ⭐【F1】那段只读红字：运行期一个字不剩，而证据件里一字未动（配对 `user-replica.spec.ts`）', async () => {
    // ── ① 证据件里**必须有**（= 我们没动证据件；⚠️ 比的是**原文**，实体不解码）──
    expect(EVIDENCE).toContain(RED_NOTE_TEXT_IN_EVIDENCE)
    expect(EVIDENCE).toContain('color:#E03636')

    const root = await mountPage()

    // ── ② 运行期**一个字都不剩**（判据打在**整页**文本上，比只看那一个页签更严）──
    expect(root.textContent).not.toContain(RED_NOTE_TEXT)
    // 那一格现在只剩空壳：**左列**里连一条 blockquote 都没有
    // （⚠️ 限定到左列：我们注入的那一块自己也是个 `.admin-info`，里面还有一条健康状态的
    //  `blockquote` —— 宽范围的 `.admin-info blockquote` 会**命中它**、给出假红/假绿）。
    const leftColumn = featurePanel(root).querySelector<HTMLElement>('.layui-col-md6')
    expect(leftColumn, '「功能设置」左列不见了（取证结构变了）').not.toBeNull()
    expect(leftColumn!.querySelector('blockquote'), '左列那条红字还在').toBeNull()
    expect(leftColumn!.textContent?.trim()).toBe('')

    // ── ③ 「别的东西还在」：⛔ 不是「把整页/整块都删了」也算过 ──
    // 三个页签标题照旧（顺序也照旧）。
    expect(
      Array.from(root.querySelectorAll<HTMLElement>('.layui-tab-title > li'))
        .map((li) => (li.textContent ?? '').trim()),
    ).toEqual(['基本资料', '功能设置', '修改密码'])
    // 我们那块（运行期追加）仍在，且它自己的按钮还在。
    expect(root.querySelector(BINDING_PANEL_SELECTOR)).not.toBeNull()
    expect(root.querySelector('#memberBindingSaveBtn')!.textContent).toContain('保存')
    // 证据件里那个**带 `layui-hide` 的对照块**（同一个面板里也有 `blockquote.layui-elem-quote`）还在。
    expect(root.textContent).toContain('关注鸡蛋铺公众号')
    // ⚠️ **「修改密码」页签里那条同形的红字提示还在**——这条专抓「不加面板限定」的写法
    //    （证据件里它也是 `.admin-info > blockquote.layui-elem-quote`，只是 `color:#ff0000`）。
    expect(root.textContent).toContain('如果你的登录密码为初始密码')
  })

  /**
   * ⭐ **spec §13.5 的 F2**：删的是**那一个** `blockquote`，⛔ 不是「按文案宽删」。
   *
   * 做法：把证据件原文**换成**一个「别处也有一句同样的话」的变体再挂载（诱饵落在**同一个面板**
   * 的右列里，比落在别的页签更刁），然后钉住——
   *  - **真的那一条**（左列 `.admin-info` 自己那条）没了；
   *  - **诱饵原样留着**（它的父节点不是 `.admin-info`，所以按结构定位不会命中它）。
   *
   * ⚠️ **能红**（实测读数见报告）：把定位换成 spec §10.2 那段**按文案匹配**
   * （`root.querySelectorAll('.layui-tab-item p')` + `includes(文案)` + `closest('blockquote').remove()`）
   * 或换成面板内的**宽选择器**（`panel.querySelectorAll('blockquote')`）⇒ 诱饵被一起删掉 ⇒ 本用例红。
   */
  it('㊵ ⭐【F2】只删那一个 `blockquote`：别处一句同样的话**原样留着**（⛔ 不按文案宽删）', async () => {
    const DECOY_ID = 'decoySameTextNote'
    const ANCHOR = '<div class="admin-info layui-hide" style="margin-top:20px">'
    // 锚点必须**恰好命中 1 次**（否则下面的替换要么没生效、要么改到了别处，读数不指向本条判据）。
    expect(EVIDENCE.split(ANCHOR).length - 1, `证据件里锚点应恰好 1 次：${ANCHOR}`).toBe(1)
    const decoy = `<blockquote class="layui-elem-quote" id="${DECOY_ID}"><p>${RED_NOTE_TEXT_IN_EVIDENCE}</p></blockquote>`
    const withDecoy = EVIDENCE.replace(ANCHOR, decoy + ANCHOR)
    expect(withDecoy).not.toBe(EVIDENCE)
    expect(withDecoy).toContain(`id="${DECOY_ID}"`)
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (String(url).includes('user-myinfo.html')) {
        return { ok: true, text: async () => withDecoy } as unknown as Response
      }
      throw new Error(`未预期的 fetch：${String(url)}`)
    }))

    const root = await mountPage()

    // 诱饵**必须还在 DOM 里**：上面已经证过它进了取回的标记（`withDecoy` 里有那个 id）
    // ⇒ 这里取不到它，只能是**被一起删掉了**（宽删）。
    const decoyEl = root.querySelector<HTMLElement>(`#${DECOY_ID}`)
    expect(decoyEl, '诱饵不在 DOM 里 ⇒ 被「宽删」带走了（按文案匹配 / 面板内全删都会这样）').not.toBeNull()
    expect(decoyEl!.textContent).toContain(RED_NOTE_TEXT)
    // 而**真的那一条**（那一格 `.admin-info` 自己挂着的 blockquote）没了。
    const leftColumn = featurePanel(root).querySelector<HTMLElement>('.layui-col-md6')
    expect(leftColumn, '「功能设置」左列不见了（取证结构变了）').not.toBeNull()
    expect(leftColumn!.querySelector('blockquote'), '左列那条红字还在 ⇒ 删的不是它').toBeNull()
    // 只剩空壳：剥掉源码缩进的空白之后**一个字都不剩**（⚠️ 两侧都按同一套归一化比，
    // ⛔ 不是「一侧 trim、另一侧拿含空白的期望值」）。
    expect(leftColumn!.textContent?.trim()).toBe('')
    expect(leftColumn!.textContent).not.toContain(RED_NOTE_TEXT)
    // 诱饵仍在**原位**（同一个面板里、右列）。
    expect(featurePanel(root).contains(decoyEl!)).toBe(true)
  })

  /* ──────────────────────────────────────────────────────────────────────────────
   * ⭐ 「对接方式」那一条轴（②式 = 鸡蛋铺官方开放平台 API；spec §11.1–§11.5）
   *
   * ⚠️ 这一组全部**驱动真交互**（挂载 → 真 DOM → 页面自己用 `form.on('select(…)')` 登记的
   * 那个真回调）：
   *  - 切下拉**不调纯函数**（`fireSelectMode` 走 `FORM_HANDLERS` 里那条登记的回调）——
   *    「切了没反应」是本仓最恨的**静默**形态（`AGENTS.md` §5-G）；
   *  - 复制按钮走**真 DOM 事件**（挂在 `root` 上的捕获态委托）；
   *  - 每条判据都钉在**值与可见性**上，⛔ 不写「元素在 DOM 里就算过」的空断言。
   *
   * ⚠️ 三条陷阱照 §5-G：① 类名 / 选择器分成两个常量（`FIELDS.mode` 是**字面量**，
   * 与生产 map 逐项对齐由 ㉓ 守）；② 以 `(` 开头的语句**前置分号**；
   * ③ 本文件沿用「`await bootstrapIsland(...)` 挂样式 → 再塞标记」的既有顺序
   * （`mountPage` 走的就是真 `mount`，`initIslandElements` 在标记之后才被调）。
   * ────────────────────────────────────────────────────────────────────────────── */

  /**
   * 剪贴板替身（②式那一格的「复制」）。
   *
   * ⚠️ jsdom 里**没有** `navigator.clipboard`（真浏览器只有安全上下文才有）⇒ 不装它的话
   * 页面会走「失败提示」那一支，而那条判据就变成了在测替身自己（E-0029 家族）。
   * ⚠️ 装法用 `Object.defineProperty`（`navigator` 是只读 getter，直接赋值会被静默丢掉）。
   */
  function stubMemberClipboard(): ReturnType<typeof vi.fn> {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
      writable: true,
    })
    return writeText
  }

  /**
   * 切一次「对接方式」。
   *
   * ⚠️ 顺序照**真交互**（读码 vendored `public/layui/layui.js` 的 select 分支：用户点中某个
   * `<dd>` 时先 `y.val(a)` 把原 `<select>` 的值改成新值，**再** `layui.event.call(this, $,
   * "select(" + lay-filter + ")", {elem, value, othis})`）⇒ 这里也必须先改 `<select>.value`
   * 再驱动页面登记的**那个真回调**；否则 `saveMemberBinding()` 读到的还是旧方式，
   * 测的就不是用户看到的那条路径。
   * ⚠️ 事件名与 `value` 都钉**字面**，⛔ 不用生产常量拼（那样常量被改坏时这条也一起变、
   * 判据变成自指）。
   */
  function fireSelectMode(root: HTMLElement, value: string): void {
    const select = root.querySelector<HTMLSelectElement>('[name="member_mode"]')
    expect(select, '绑定块里没有「对接方式」下拉').not.toBeNull()
    select!.value = value
    const cb = FORM_HANDLERS.get('select(memberBindingMode)')
    expect(cb, '本页没有登记 select(memberBindingMode) 处理器 ⇒ 切「对接方式」没反应').toBeTruthy()
    cb!({ value })
  }

  /** 绑定块里某一格所属的 `.layui-form-item`（切换可见性的判据要打在**那一格**上）。 */
  function itemOf(root: HTMLElement, name: string): HTMLElement {
    const field = root.querySelector<HTMLElement>(`[name="${name}"]`)
    expect(field, `绑定块里没有 name=${name} 的那一格`).not.toBeNull()
    const item = field!.closest<HTMLElement>('.layui-form-item')
    expect(item, `name=${name} 不在 .layui-form-item 里`).not.toBeNull()
    return item!
  }

  /** 按 `id` 取某一格（`client_id` / `client_secret` / 回调地址那三格有 id）。 */
  function itemById(root: HTMLElement, id: string): HTMLElement {
    const el = root.querySelector<HTMLElement>(`#${id}`)
    expect(el, `绑定块里没有 #${id} 那一格`).not.toBeNull()
    return el!
  }

  /**
   * ②式那三格 + 账号 / 密码两格 + **订单来源名**那一格的 `display`
   * （`''` = 显示、`'none'` = 隐藏）。
   *
   * ⚠️ 「订单来源名」随对接方式切：①式显示、②式隐藏（②式探针不返回来源字典）。
   */
  function bindingVisibility(root: HTMLElement): Record<string, string> {
    return {
      account: itemOf(root, 'member_account').style.display,
      password: itemOf(root, 'member_password').style.display,
      source: itemOf(root, 'member_source_name').style.display,
      clientId: itemById(root, MEMBER_CLIENT_ID_ITEM_ID).style.display,
      clientSecret: itemById(root, MEMBER_CLIENT_SECRET_ITEM_ID).style.display,
      callback: itemById(root, 'memberCallbackItem').style.display,
    }
  }

  /** 「回调地址」那一格的三处状态（只读输入的值 / 地址行 / 原因行）。 */
  function callbackCellState(root: HTMLElement): {
    url: string
    rowDisplay: string
    reason: string
    reasonDisplay: string
    readonly: boolean
  } {
    const urlInput = root.querySelector<HTMLInputElement>(`#${MEMBER_CALLBACK_URL_INPUT_ID}`)
    const row = root.querySelector<HTMLElement>(`#${MEMBER_CALLBACK_ROW_ID}`)
    const reason = root.querySelector<HTMLElement>(`#${MEMBER_CALLBACK_REASON_ID}`)
    // 非空哨兵：取不到就直接红，而不是让下面读到 `null` 报一个看不懂的错。
    expect(urlInput, '标记里应当有回调地址那个只读输入').not.toBeNull()
    expect(row, '标记里应当有回调地址那一行').not.toBeNull()
    expect(reason, '标记里应当有回调地址的原因行').not.toBeNull()
    return {
      url: urlInput!.value,
      rowDisplay: row!.style.display,
      reason: reason!.textContent ?? '',
      reasonDisplay: reason!.style.display,
      readonly: urlInput!.readOnly,
    }
  }

  it('㊶ 默认是①式：账号 / 密码格在，②式那三格 `display:none`（⛔ 不是 disabled）', async () => {
    const root = await mountPage()
    // 下拉的选项文案与机器值（**字面量**钉住：⛔ 不用生产常量当期望值）。
    expect(Array.from(field(root, FIELDS.mode).querySelectorAll('option'))
      .map((o) => [o.value, o.textContent]))
      .toEqual([['password', '账号 + 密码'], ['openapi', '官方开放平台 API']])
    // ⭐ ②式**禁选**（业务方裁定放弃 API 密钥对接）：选项保留是为了
    //    存量②式行回显（禁用的选项被选中时照样如实显示），但用户在下拉里选不到它。
    const modeSelect = field(root, FIELDS.mode)
    expect(modeSelect.querySelector('option[value="openapi"]')?.hasAttribute('disabled')).toBe(true)
    expect(modeSelect.querySelector('option[value="password"]')?.hasAttribute('disabled')).toBe(false)
    expect(field(root, FIELDS.mode).value).toBe('password')
    const visible = bindingVisibility(root)
    expect(visible.account).toBe('')
    expect(visible.password).toBe('')
    // ⭐ ①式下「订单来源名」**显示**（它的字典来自①式的测试连接探针）。
    expect(visible.source).toBe('')
    expect(visible.clientId).toBe('none')
    expect(visible.clientSecret).toBe('none')
    expect(visible.callback).toBe('none')
    // ⛔ **不是 `disabled`**（spec §11.2 的 ⛔ 那一栏）：隐藏不等于禁用——两套凭据共用同一个提交
    //    出口，禁用会在切回来的那一刻留下一个「看得见但打不了字」的框。
    expect(inputField(root, FIELDS.clientId).disabled).toBe(false)
    expect(inputField(root, FIELDS.clientSecret).disabled).toBe(false)
    expect(field(root, FIELDS.account).disabled).toBe(false)
    expect(field(root, FIELDS.password).disabled).toBe(false)
  })

  it('㊷ 切到②式 ⇒ 账号 / 密码 `display:none`、新三格出现；切回来 ⇒ **填过的值一个都没丢**（要能红）', async () => {
    const root = await mountPage()
    // 先在①式填上东西（切走再切回来必须原样还在）。
    field(root, FIELDS.account).value = 'member-keep'
    field(root, FIELDS.password).value = 'pw-keep'
    field(root, FIELDS.baseUrl).value = 'https://jidanpu-keep.test.invalid'

    fireSelectMode(root, 'openapi')
    const openapi = bindingVisibility(root)
    expect(openapi.account).toBe('none')
    expect(openapi.password).toBe('none')
    // ⭐ ②式下「订单来源名」**隐藏**：探针不返回来源字典，留着会让人以为能选。
    expect(openapi.source).toBe('none')
    expect(openapi.clientId).toBe('')
    expect(openapi.clientSecret).toBe('')
    expect(openapi.callback).toBe('')

    fireSelectMode(root, 'password')
    const back = bindingVisibility(root)
    expect(back.account).toBe('')
    expect(back.password).toBe('')
    // ⭐ 切回①式 ⇒ 来源名那一格**又显示**（只动 `display`，值不清、不禁用）。
    expect(back.source).toBe('')
    expect(back.clientId).toBe('none')
    expect(back.clientSecret).toBe('none')
    // ⛔ **值还在**（切可见性只动 `display`：清值 / 禁用都会在这里红）。
    expect(field(root, FIELDS.account).value).toBe('member-keep')
    expect(field(root, FIELDS.password).value).toBe('pw-keep')
    expect(field(root, FIELDS.baseUrl).value).toBe('https://jidanpu-keep.test.invalid')
  })

  it('㊷-乙 引导块与绑定块**上下堆叠、全部左对齐**（D-0299 把两栏 flex 改准）；要点写全但**不出现技术路径**', async () => {
    const root = await mountPage()
    const block = panel(root)
    expect(block, '功能设置页签里没有绑定块').not.toBeNull()
    const guide = root.querySelector<HTMLElement>(`#${MEMBER_GUIDE_ID}`)
    expect(guide, '绑定块左边应当有一块「一步步开通」').not.toBeNull()

    // ① 位置（D-0299）：引导在上、表单在下——
    //    **同一个父节点下的两个兄弟**，中间没有 flex 两栏容器。块级流天然上下排、左对齐。
    //    ⛔ 能红：恢复旧的两栏 flex 包裹 ⇒ 父节点不一致 / 兄弟关系断开，两条都红。
    const host = guide!.parentElement
    expect(host, '引导块与绑定块不在同一个父节点里（上下堆叠被破坏）').toBe(block!.parentElement)
    expect(guide!.nextElementSibling).toBe(block)
    expect(host!.style.display).not.toBe('flex')
    // ② **直接展开**（不再折叠，业务方要求「展示出来」）。
    expect(guide!.tagName).toBe('DETAILS')
    expect(guide!.hasAttribute('open')).toBe(true)

    const text = guide!.textContent ?? ''
    expect(text).toContain('一步步开通')
    // ③ 业务方明确要求：**⛔ 不写 /Order/addorder 这类技术路径**。
    expect(text).not.toContain('/Order/')
    expect(text).not.toContain('/System/')
    // ④ 要点仍在：系统日志（只用于对账同步）、新增来源、三个字面值。
    expect(text).toContain('系统日志')
    expect(text).toContain('对账')
    for (const word of ['新增来源', '团团通系统', '其它平台']) {
      expect(text, `向导里缺「${word}」`).toContain(word)
    }
    // ⑤ 次序承重：先保存 → 再测试连接 → 再选来源 → 再保存。
    const order = ['先保存', '再测试连接', '再选来源', '再保存']
      .map((word) => text.indexOf(word))
    expect(order.every((i) => i >= 0), `向导缺步骤词：${JSON.stringify(order)}`).toBe(true)
    expect(order).toEqual([...order].sort((a, b) => a - b))
  })

  it('㊷-丙 两张引导图**真在仓内**，且路径由 `ASSET_BASE` 拼出（⛔ 不写死 `/guide/`）', async () => {
    const root = await mountPage()
    const guide = root.querySelector<HTMLElement>(`#${MEMBER_GUIDE_ID}`)
    expect(guide).not.toBeNull()
    const imgs = Array.from(guide!.querySelectorAll<HTMLImageElement>('img'))
    expect(imgs).toHaveLength(2)
    expect(MEMBER_GUIDE_IMAGES).toHaveLength(2)
    // 两张**不同**的图（复制同一张两次也算"两张都在"）。
    expect(new Set(MEMBER_GUIDE_IMAGES.map((img) => img.src)).size).toBe(2)
    // 渲染出来的 `src` 就是常量里那两个（顺序也钉住：①权限位置、②新增来源）。
    expect(imgs.map((el) => el.getAttribute('src')))
      .toEqual(MEMBER_GUIDE_IMAGES.map((img) => img.src))
    for (const img of MEMBER_GUIDE_IMAGES) {
      // 路径由 `import.meta.env.BASE_URL` 起头（子路径部署时才会跟着变）。
      expect(img.src.startsWith(import.meta.env.BASE_URL)).toBe(true)
      const abs = path.resolve(HERE, '../../../../public', img.src.replace(/^\//, ''))
      expect(fs.existsSync(abs), `${img.src} 指向的文件不在磁盘上（页面只会显示一块空白）`).toBe(true)
      expect(fs.statSync(abs).size, `${img.src} 是空文件`).toBeGreaterThan(1000)
    }
    // ⭐ 承重判据：两条 `src` 都必须是**由 `ASSET_BASE` 拼出来的**。
    //    vitest 里 `BASE_URL` 恰好就是 `/` ⇒ 只看渲染出来的 `src` **分不出**写没写死；
    //    而写死之后换基址是**静默 404**（页面不报错、图上是空白）⇒ 判据只能打在源码上。
    //    ⚠️ 用**正向**匹配（各自那一条 `src:` 都以 `${ASSET_BASE}guide/` 开头），
    //    ⛔ 不用「源码里不许出现 /guide/」那种反向串——注释里提到路径就会误伤。
    const pageSource = fs.readFileSync(path.resolve(HERE, '../user-myinfo.ts'), 'utf8')
    expect(pageSource).toContain('import.meta.env.BASE_URL')
    for (const file of ['jidanpu-perm-system-log.png', 'jidanpu-source-add.png']) {
      expect(
        pageSource,
        `${file} 的 src 不是由 ASSET_BASE 拼出来的（写死 /guide/ 会在子路径部署下静默 404）`,
      ).toContain('src: `${ASSET_BASE}guide/' + file + '`')
    }
  })

  it('㊸ ②式的两格进得了字、`client_secret` 是 password 型；⛔ 页面上**一个字都不回显密钥**', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_OPENAPI_ROW as never)
    const root = await mountPage()
    // 回填：`client_id` 是身份 ⇒ 回显；`mode` 回**库里那一行**的方式（⛔ 不是默认值——
    // 否则②式用户一进页面看到「账号 + 密码」，一保存就把自己切回①式）。
    expect(field(root, FIELDS.mode).value).toBe('openapi')
    expect(field(root, FIELDS.clientId).value).toBe(MEMBER_OPENAPI_ROW.client_id)
    expect(inputField(root, FIELDS.clientSecret).type).toBe('password')
    // ⛔ 承重判据之一：后端只回 `client_secret_set`（布尔），⛔ 拿它当密钥回填就会显示 `true`/`false`。
    expect(inputField(root, FIELDS.clientSecret).value).toBe('')
    expect(inputField(root, FIELDS.clientSecret).placeholder).toContain('留空')
    // ⭐ D-0299：②式回填**同样**走「库里那一格」——本行夹具是 false（与默认 true 相反）⇒
    //    「回填没生效、只是默认开着」在这里红。
    expect(inputField(root, FIELDS.hallSync).checked).toBe(false)

    // ⛔ **哨兵判据**：把合成密钥敲进那一格 ⇒ 它只许出现在**写请求的载荷**里，
    //    页面标记（`innerHTML`）里一个字都不许有（凭据泄漏 = 一行标记就够）。
    const sentinel = 'sentinel-secret-member-do-not-render'
    inputField(root, FIELDS.clientSecret).value = sentinel
    await clickButton(root, BINDING_SELECTORS.save)
    expect(root.innerHTML).not.toContain(sentinel)
    expect(MSGS.join('|')).not.toContain(sentinel)
    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    expect(body.client_secret).toBe(sentinel)
  })

  it('㊹ 回调地址那一格：`readonly` + 复制按钮；点「复制」⇒ **真的写到剪贴板**（要能红）', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_OPENAPI_ROW as never)
    const writeText = stubMemberClipboard()
    const root = await mountPage()

    const state = callbackCellState(root)
    // 值**只**来自后端算好的 `callback_url`（⛔ 前端一个字符都不拼）。
    expect(state.url).toBe(MEMBER_OPENAPI_ROW.callback_url)
    expect(state.readonly).toBe(true)
    expect(state.rowDisplay).toBe('')
    expect(state.reasonDisplay).toBe('none')

    const button = root.querySelector<HTMLElement>(`#${MEMBER_CALLBACK_COPY_BUTTON_ID}`)
    expect(button, '回调地址那一格应当有一颗「复制」按钮').not.toBeNull()
    button!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText.mock.calls[0]![0]).toBe(MEMBER_OPENAPI_ROW.callback_url)
    // 提示走**真实出口**（`islandMsg` → 本页替身记下那句话）。
    expect(MSGS).toContain(MEMBER_CALLBACK_COPY_OK_TEXT)
  })

  it('㊺ 地址拿不到 ⇒ 显示的是**后端给的原因**（⛔ 前端不另写文案、⛔ 不画空格子、⛔ 不显示半截地址）', async () => {
    const REASON = '这条绑定还没有回调码（合成原因）'
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue({
      ...MEMBER_OPENAPI_ROW, callback_url: '', callback_url_reason: REASON,
    } as never)
    const root = await mountPage()
    const state = callbackCellState(root)
    expect(state.rowDisplay).toBe('none')
    expect(state.url).toBe('')
    expect(state.reason).toBe(REASON)
    expect(state.reasonDisplay).toBe('')
    // ⛔ 兜底文案**不该**出现（后端给了原因就该用它的）。
    expect(state.reason).not.toBe(MEMBER_CALLBACK_MISSING_REASON_TEXT)
  })

  it('㊻ 还没保存过 ⇒ 那句「保存一次之后…」；⛔ 不显示成空白（要能红）', async () => {
    // `beforeEach` 的默认就是 `null`（后端真会这么回）。
    const root = await mountPage()
    const state = callbackCellState(root)
    expect(state.rowDisplay).toBe('none')
    expect(state.url).toBe('')
    expect(state.reason).toBe(MEMBER_CALLBACK_PENDING_TEXT)
    expect(state.reason).not.toBe('')
    expect(state.reasonDisplay).toBe('')
  })

  it('㊼ ②式点「测试连接」⇒ 逐条表**恰好 5 行、顺序固定**，`probed=false` 那行渲染成「没探」（要能红）', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_OPENAPI_ROW as never)
    // 夹具①：**五行**，其中第一行「没探」（`probed=false` 而 `ok` 同样是 `false`）。
    const checks = [
      { step: 'token', label: '取 token', probed: false, ok: false, kind: '',
        reason: '上一跳没通过，这一发没打', message: '', action: '先把上面的凭据填对' },
      { step: 'GetGames', label: '游戏列表', probed: true, ok: false, kind: 'permission',
        reason: '没有权限', message: '开放平台的权限还没配全（游戏列表）', action: '去鸡蛋铺开这一项' },
      { step: 'GetProject', label: '常用项目', probed: true, ok: true, kind: '',
        reason: '通了', message: '', action: '' },
      { step: 'GetOrderStatus', label: '查订单状态', probed: true, ok: false, kind: 'other',
        reason: '对方原话：系统繁忙', message: '这一项缺了，状态同步跑不起来', action: '稍后重试' },
      { step: 'GetOrderProcessImgs', label: '进度图', probed: true, ok: true, kind: '',
        reason: '', message: '', action: '' },
    ]
    // ⚠️ ②式的响应**没有**字典那三个键（后端 `{ok, messages, actions, checks}`）——
    //    夹具照**真实键集合**给，⛔ 不塞三个后端不会回的键（那会让「页面把这一路当①式处理」
    //    这种缺陷静默通过）。
    vi.mocked(testMemberJidanpuConnection).mockResolvedValue({
      ok: false, messages: [], checks,
    })
    const root = await mountPage()
    fireSelectMode(root, 'openapi')
    await clickButton(root, BINDING_SELECTORS.test)

    // ⚠️ ②式这一支**两个参数都不传**（空体）：它测的是**已保存的配置**。
    expect(vi.mocked(testMemberJidanpuConnection).mock.calls[0]).toEqual(['', ''])
    // ⛔ 「这一路没拉字典」不许被说成「没读到对方的来源名单」（`AGENTS.md` §5-E）：
    //    提示位仍是常态那句、且**不变暖色**。
    const hint = root.querySelector<HTMLElement>(`#${MEMBER_SOURCE_HINT_ID}`)!
    expect(hint.textContent).not.toContain('没读到')
    expect(hint.style.color).toBe('')
    const box = root.querySelector<HTMLElement>(`#${MEMBER_TEST_RESULT_ID}`)
    expect(box, '绑定块里没有测试连接的结果区').not.toBeNull()
    // 四列按屏幕顺序（⛔ 期望值是**字面量**，不取生产常量）。
    expect(Array.from(box!.querySelectorAll('thead th')).map((th) => th.textContent))
      .toEqual(['接口', '结果', '原因', '该做什么'])
    const rows = Array.from(box!.querySelectorAll('tbody tr'))
    expect(rows).toHaveLength(5)
    const cells = rows.map((tr) => Array.from(tr.querySelectorAll('td')).map((td) => td.textContent))
    // **顺序固定**：五行按后端的顺序原样摆（⛔ 前端不重排、不过滤）。
    expect(cells.map((c) => c[0])).toEqual([
      '取 token', '游戏列表', '常用项目', '查订单状态', '进度图',
    ])
    // ⭐ **未探测 ≠ 不可用**：第一行 `ok` 也是 `false`，但必须渲染成「没探」。
    expect(cells[0]![1]).toBe(MEMBER_CHECK_NOT_PROBED_TEXT)
    expect(cells[0]![1]).not.toBe('不可用')
    expect(cells[1]![1]).toBe('不可用')
    expect(cells[2]![1]).toBe(MEMBER_CHECK_RESULT_OK_TEXT)
    // `reason` / `message` / `action` **原样透传**（⛔ 前端一个字都不改写）：
    // `message` 接在同一条「原因」列里（两段都要显示）。
    expect(cells[1]![2]).toContain('没有权限')
    expect(cells[1]![2]).toContain('开放平台的权限还没配全（游戏列表）')
    expect(cells[1]![3]).toBe('去鸡蛋铺开这一项')
    expect(cells[3]![2]).toContain('对方原话：系统繁忙')
  })

  it('㊽ 拿不到 `checks` ⇒ 退回逐条 `messages`；两样都没有 ⇒ 一句「没有结论」（⛔ 绝不静默）', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_OPENAPI_ROW as never)
    const root = await mountPage()
    fireSelectMode(root, 'openapi')

    // ① 后端这一发没给 `checks`（键整个不在）⇒ 用逐条 `messages`。
    vi.mocked(testMemberJidanpuConnection).mockResolvedValue({
      ...MEMBER_DETECT_OK, ok: false, messages: ['连不上对方的开放平台（合成原因）'],
    })
    await clickButton(root, BINDING_SELECTORS.test)
    const box = root.querySelector<HTMLElement>(`#${MEMBER_TEST_RESULT_ID}`)!
    expect(box.querySelectorAll('tbody tr')).toHaveLength(0)
    expect(box.textContent).toContain('连不上对方的开放平台（合成原因）')

    // ② 连 `messages` 都是空的（正常路径不可达）⇒ 也必须有一句明确的话。
    vi.mocked(testMemberJidanpuConnection).mockResolvedValue({
      ...MEMBER_DETECT_OK, ok: false, messages: [],
    })
    await clickButton(root, BINDING_SELECTORS.test)
    expect(root.querySelector<HTMLElement>(`#${MEMBER_TEST_RESULT_ID}`)!.textContent)
      .toContain(MEMBER_TEST_EMPTY_RESULT_TEXT)
  })

  it('㊾ ⭐②式保存 ⇒ 载荷的 `mode` **必须是 `openapi`**、含 `client_id` / `client_secret`，⛔ 不含 `password`', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_OPENAPI_ROW as never)
    const root = await mountPage()
    // 打开②式、改一格地址（让它有东西可写），密钥留空 = 不改（库里已配过）。
    fireSelectMode(root, 'openapi')
    field(root, FIELDS.clientId).value = 'cid-synthetic-member-0002'
    await clickButton(root, BINDING_SELECTORS.save)

    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    // ⭐⭐ **`mode` 逐字比**——「挡住用户保存」那处缺陷就落在这一格上：
    //    库里那一行是②式（`MEMBER_OPENAPI_ROW.mode === 'openapi'`）、页面也在②式
    //    ⇒ 载荷必须说 `openapi`。⛔ **不许**再 `delete body.mode` 之后才比（那样
    //    补丁把 `openapi` 盖成 `password` 也绿）：后端收到 `password` 就按①式判 `account`，
    //    而「账号」那一格在②式界面上是隐藏的空串 ⇒ 用户填得没错，回来的却是「鸡蛋铺账号不能为空」。
    //    ⚠️ 这条**能红**：把 `saveMemberBinding` 里 `memberPasswordModePatch(...)` 的
    //    `submittedMode` 那个实参去掉（恢复无条件补）⇒ `mode` 变 `password`，本条红。
    expect(body.mode).toBe('openapi')
    expect(body).toEqual({
      mode: 'openapi',
      jidanpu_base_url: MEMBER_OPENAPI_ROW.jidanpu_base_url,
      // ⚠️ 账号那一格在②式下**不显示**，但值照旧带上（⛔ 发空串会把①式那一套用户名静默清掉）。
      account: MEMBER_OPENAPI_ROW.account,
      client_id: 'cid-synthetic-member-0002',
      client_secret: '',
      source_name: MEMBER_OPENAPI_ROW.source_name,
      throw_price_percent: MEMBER_OPENAPI_ROW.throw_price_percent,
      // ⭐ D-0299：②式载荷**同样带上**这个开关（它不随对接方式变）。
      hall_sync_enabled: MEMBER_OPENAPI_ROW.hall_sync_enabled,
      enabled: false,
    })
    // 键集**恰好八个**：⛔ ②式那一路不登录 ⇒ 载荷里**没有** `password`（spec §13.5-F5）。
    expect(Object.keys(body).sort()).toEqual([
      'account', 'client_id', 'client_secret', 'enabled', 'hall_sync_enabled',
      'jidanpu_base_url', 'mode', 'source_name', 'throw_price_percent',
    ])
    expect('password' in body).toBe(false)
    // ⚠️ 这里**不**在保存之后读那一格：保存成功会 `fillMemberBinding(saved)`，
    //    而回填会**按响应那一行重写**方式那一格（本用例的响应夹具就是 `MEMBER_OPENAPI_ROW`、
    //    里面 `mode='openapi'`）——那是**预期**行为（保存的返回是权威），
    //    ⛔ 不是「切回①式」的证据。`client_id` / `client_secret` 在载荷里，已经证明走的是②式那一支。
  })

  it('㊿ ⭐ ②→① 切回来保存 ⇒ 补一个 `mode: "password"`（不补就是静默不生效；要能红）', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_OPENAPI_ROW as never)
    const root = await mountPage()
    // 库里那一行是②式 ⇒ 切回①式后填好①式那两格再保存。
    fireSelectMode(root, 'password')
    field(root, FIELDS.account).value = 'member-back-to-password'
    field(root, FIELDS.password).value = 'pw-back'
    await clickButton(root, BINDING_SELECTORS.save)

    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    // 不补 `mode` ⇒ 后端起 token 按**库里那一格**（仍是 `openapi`）走 ⇒ 页面说「保存成功」，
    // 而这套刚填的①式凭据**永远不会被用**。
    expect(body.mode).toBe('password')
    expect(body.client_id).toBeUndefined()
    expect(body.client_secret).toBeUndefined()
  })

  it('51 ⭐ 库里本来就是①式 ⇒ 保存的载荷**逐字不变**（⛔ 一个 `mode` 键都不加）', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_ROW as never)
    const root = await mountPage()
    field(root, FIELDS.account).value = 'member-plain'
    await clickButton(root, BINDING_SELECTORS.save)

    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    // 两向都要判（spec §13.5-F4）：只判②→①那一向的话，把 `mode` 无条件塞进载荷也能全绿，
    // 而那一支的请求体就不再「逐字不变」了。
    expect(Object.keys(body).sort()).toEqual([
      'account', 'enabled', 'hall_sync_enabled', 'jidanpu_base_url', 'password',
      'source_name', 'throw_price_percent',
    ])
    expect(body.mode).toBeUndefined()
  })

  it('52 ②式缺 `client_id` ⇒ 早挡、零请求；后来填上就放行', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue({
      ...MEMBER_OPENAPI_ROW, client_id: '',
    } as never)
    const root = await mountPage()
    fireSelectMode(root, 'openapi')
    await clickButton(root, BINDING_SELECTORS.save)
    expect(saveMemberJidanpuBinding).not.toHaveBeenCalled()
    expect(MSGS).toContain(MEMBER_CLIENT_ID_REQUIRED_TEXT)

    field(root, FIELDS.clientId).value = 'cid-synthetic-member-0003'
    await clickButton(root, BINDING_SELECTORS.save)
    expect(saveMemberJidanpuBinding).toHaveBeenCalledTimes(1)
  })

  it('53 ②式从没存过密钥 ⇒ 留空也要早挡（那句与后端同字）；库里配过则留空是正常用法', async () => {
    // ① 从没存过（`client_secret_set: false`）⇒ 留空必须拦。
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue({
      ...MEMBER_OPENAPI_ROW, client_secret_set: false,
    } as never)
    const root = await mountPage()
    fireSelectMode(root, 'openapi')
    await clickButton(root, BINDING_SELECTORS.save)
    expect(saveMemberJidanpuBinding).not.toHaveBeenCalled()
    expect(MSGS).toContain(MEMBER_CLIENT_SECRET_REQUIRED_TEXT)

    // ② 库里配过（夹具默认 `true`）⇒ 留空**放行**（空串 = 不改；⛔ 不逼用户每次改地址都重填密钥）。
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_OPENAPI_ROW as never)
    const root2 = await mountPage()
    fireSelectMode(root2, 'openapi')
    await clickButton(root2, BINDING_SELECTORS.save)
    expect(saveMemberJidanpuBinding).toHaveBeenCalledTimes(1)
    const body = vi.mocked(saveMemberJidanpuBinding).mock.calls[0]![0] as Record<string, unknown>
    expect(body.client_secret).toBe('')
  })

  it('54 健康那一格：后端那句 `consumer_state` **原样透传**，⛔ 不按时间变红', async () => {
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue(MEMBER_OPENAPI_ROW as never)
    const root = await mountPage()
    const health = root.querySelector<HTMLElement>(BINDING_SELECTORS.health)!
    // 原样透传：⛔ 前端一个字都不改写、也不拿时刻自己再判一次。
    expect(health.textContent).toContain(MEMBER_OPENAPI_ROW.consumer_state.text)
    // ⛔ 那一格**一个时刻比较都没有**（没有阈值 / 心跳 / 「多久没收到」），
    //    也**没有暖色**：后端夹具 `bad: false` 而前端**根本没读 `bad`**（它不参与任何渲染决定）。
    expect(health.querySelector('[style*="FF5722"]')).toBeNull()
    // ⛔ 页面上不许出现「最近收到回调」——那张表那一列不进序列化器，页面拿不到
    //    （⛔ 不为了好看去编一个时刻）。
    expect(health.textContent).not.toContain('最近收到回调')

    // 那一句为空（后端这一跳没结论）⇒ ⛔ 一个字都不编，那一格直接不出。
    vi.mocked(fetchMemberJidanpuBinding).mockResolvedValue({
      ...MEMBER_ROW, consumer_state: { text: '', bad: false },
    } as never)
    const root2 = await mountPage()
    expect(root2.querySelector<HTMLElement>(BINDING_SELECTORS.health)!.textContent)
      .not.toContain('回调消费')
  })

  it('55 ⭐「账号」那格的 `lay-verify` 随对接方式摘 / 装；⛔ 密码 / ②式两格**不装**（口径各不相同）', async () => {
    // ⚠️ **本条钉的是「属性」，⛔ 不是「保存能不能过」**：本页**没有**任何路径会跑 layui 的
    //    `validate()`（保存那颗是 `type="button"`、无 `lay-submit`，点击在 `root` 捕获态被
    //    `stop()` 拦下）⇒ 这个属性今天是**惰性**的。钉它的理由是**语义**：②式下「账号」那一格
    //    是被 `display:none` 藏起来的空串，留着一个 `required` 等于声明一件**不成立**的事。
    // ⚠️ 两条都要能红：① 把 `applyMemberMode` 里摘属性那两句去掉 ⇒ 切到②式后仍有 `required`（第一段红）；
    //    ② 把「切回①式装回去」那半去掉 ⇒ 切回来没有 `required`（第二段红）。
    const root = await mountPage()
    const account = inputField(root, FIELDS.account)

    // ①式（默认）⇒ 必填在（①式下账号确实是必填的：后端 `validate_account` 会拒空串）。
    expect(account.getAttribute('lay-verify')).toBe('required')

    // 切到②式 ⇒ 摘掉（那一格被藏起来、值是空串）。
    fireSelectMode(root, 'openapi')
    expect(account.getAttribute('lay-verify')).toBeNull()
    expect(account.closest<HTMLElement>('.layui-form-item')!.style.display).toBe('none')

    // 切回①式 ⇒ **装回来**（⛔ 不是「摘了就永远没了」）。
    fireSelectMode(root, 'password')
    expect(account.getAttribute('lay-verify')).toBe('required')

    // ⛔ **另外三格一个都不许装 `required`**（口径各自不同，见 `MEMBER_ACCOUNT_VERIFY` 的注释）：
    //    密码 / `client_secret` 的「空串 = 不改」是正当用法；`client_id` 的必填只在
    //    「②式且从没存过」时成立 —— layui 的 `required` 表达不了这些条件，装了就是把
    //    正常保存挡下来。它们由 `saveMemberBinding()` 里的早挡负责（52 / 53 两条钉的就是它）。
    for (const selector of [FIELDS.password, FIELDS.clientId, FIELDS.clientSecret]) {
      expect(inputField(root, selector).getAttribute('lay-verify')).toBeNull()
    }
  })

  /* ══════════════════ 实名认证（spec §10.2） ══════════════════
   *
   * 这一组钉的是「`.toCer` / `.startCertBtn` 从『尚未开放』变成真流程」这件事的**每一段**：
   * 开层（⑯）→ 提交前的计费提示（17）→ 发出的请求载荷（18）→ `pass`（19）→ `fail`（20）
   * → 业务码（21）→ **不许把另外两颗顺手改掉**（⑧）。
   *
   * ⚠️ **每条都要能独立红**：桩只在本条里设（`beforeEach` 的默认是「通过 / 余 4 次」），
   * 且判据钉的是**值**（DOM 文本 / 请求载荷 / 图标类名与色值），不是"某个函数被调过"。
   * ⚠️ 合成值：姓名 `测试姓名甲`、号码 `11010119900307561X`（**形状**像真号、不是任何真人的号，
   * `AGENTS.md` §5-C：夹具一律用合成值）。
   */

  /** 在**弹层里**那两个输入框填值，再点层里的「开始认证」。 */
  async function fillAndSubmitCert(name: string, idCardNo: string): Promise<void> {
    const nameInput = document.querySelector<HTMLInputElement>(`#${REALNAME_NAME_INPUT_ID}`)
    const sidInput = document.querySelector<HTMLInputElement>(`#${REALNAME_SID_INPUT_ID}`)
    expect(nameInput, '弹层里应当有「真实姓名」输入框').not.toBeNull()
    expect(sidInput, '弹层里应当有「身份证号」输入框').not.toBeNull()
    nameInput!.value = name
    sidInput!.value = idCardNo
    const submit = document.querySelector<HTMLElement>(`#${REALNAME_SUBMIT_BUTTON_ID}`)
    expect(submit, '弹层里应当有「开始认证」那颗').not.toBeNull()
    // ⚠️ 层挂在 `document.body` 下、**不在 `root` 里** ⇒ 判据必须驱动**层里那颗真按钮**
    //    （驱动 `root` 上的委托等于什么都没驱动，那正是 E-0061 的"点了没反应"形态）。
    submit!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
  }

  /** 层里那一行结果（`null` = 层里没有那个节点）。 */
  function certResult(): string | null {
    const slot = document.getElementById(REALNAME_RESULT_ID)
    return slot ? (slot.textContent ?? '') : null
  }

  it('⑯ 点「点击进行实名认证」⇒ 开**真弹层**（⛔ 不再是「尚未开放」），两个输入框都在', async () => {
    const root = await mountPage()
    const el = root.querySelector<HTMLElement>(TO_CER_SELECTOR)
    expect(el, `证据件里应当有：${TO_CER_SELECTOR}`).not.toBeNull()
    MSGS.length = 0
    el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    // ⛔ **逐字**钉「不是尚未开放」：只判 `MSGS.length === 0` 的话，"弹了别的提示"也会绿
    //    （而用户看到的仍然是一句"这功能没做"）。
    expect({ toast: MSGS[0] }).toEqual({ toast: undefined })
    const layer = openedLayer()
    expect(layer, '点那颗应当开一个弹层').not.toBeNull()
    // 层里那两个输入框（按 id 取：层是 `document.body` 下的节点、不在 `root` 里）。
    expect(document.querySelector(`#${REALNAME_NAME_INPUT_ID}`)).not.toBeNull()
    expect(document.querySelector(`#${REALNAME_SID_INPUT_ID}`)).not.toBeNull()
    // 层里那颗按钮的文案逐字取自证据件——⛔ 不是我方新写的文案。
    expect(document.querySelector(`#${REALNAME_SUBMIT_BUTTON_ID}`)?.textContent).toBe('开始认证')
  })

  /**
   * ⭐ 第二颗入口：`.startCertBtn`（「开始认证」，在一个整块 `display:none` 的
   * `.rnameCertDiv` 里）。
   *
   * ⚠️ **为什么要单钉一颗看着"到不了"的按钮**：生产代码里两条分支写在**同一个 `if`** 里
   * （`target.closest(TO_CER_SELECTOR) || target.closest(START_CERT_SELECTOR)`）——
   * 少了后半句时，页面上**没有任何东西会红**（那颗藏在 `display:none` 的块里，用户点不到），
   * 而 `START_CERT_SELECTOR` 这个常量就成了一个**没人用的摆设**（eslint 会报 unused，
   * 但那只说明"没引用"，不说明"这条分支对"）。所以这里用**程序化点击**把它驱动一次，
   * 钉「两条入口都开同一个真弹层」。
   */
  it('⑯b `.startCertBtn`（第二颗入口）也开真弹层——⛔ 不是只接了 `.toCer`', async () => {
    const root = await mountPage()
    const el = root.querySelector<HTMLElement>(START_CERT_SELECTOR)
    expect(el, `证据件里应当有：${START_CERT_SELECTOR}`).not.toBeNull()
    MSGS.length = 0
    el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect({ toast: MSGS[0] }).toEqual({ toast: undefined })
    expect(openedLayer(), '点「开始认证」也应当开同一个真弹层').not.toBeNull()
    expect(document.querySelector(`#${REALNAME_SUBMIT_BUTTON_ID}`)?.textContent).toBe('开始认证')
  })

  it('17 ⭐ 弹层里**有**提交前那句计费提示（钉「值在」：不一致也算一次）', async () => {
    const root = await mountPage()
    root.querySelector<HTMLElement>(TO_CER_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    const layer = openedLayer()
    expect(layer, '点那颗应当开一个弹层').not.toBeNull()
    // ⛔ 钉的是**那一句原文**（常量与文案必须逐字一致）：真实厂商按"查得"计费、**不一致也算一次**
    //    （spec §5 末段）⇒ 这句话省掉，用户会以为"失败不扣"，反复提交把租户买的次数耗光。
    expect(layer!.textContent).toContain(REALNAME_BILLING_NOTICE)
    expect(REALNAME_BILLING_NOTICE).toBe('本次核验将消耗 1 次，无论核验结果一致与否')
    // ⚠️ 它必须在**按钮之前**就可见（用户点提交之前就看得到）——判据：DOM 顺序上提示先于按钮。
    const notice = layer!.querySelector('[data-realname-billing-notice]')
    const submit = layer!.querySelector(`#${REALNAME_SUBMIT_BUTTON_ID}`)
    expect(notice, '提示应当是层里一个真节点').not.toBeNull()
    expect(submit, '层里应当有提交按钮').not.toBeNull()
    expect(
      notice!.compareDocumentPosition(submit!) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('18 提交 ⇒ 发出 `POST /api/auth/realname/verify/`，载荷含**用户填的**姓名与身份证号', async () => {
    const root = await mountPage()
    root.querySelector<HTMLElement>(TO_CER_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    await fillAndSubmitCert('测试姓名甲', '11010119900307561X')
    expect(vi.mocked(verifyRealName).mock.calls.length).toBe(1)
    // ⚠️ 载荷**恰好**这两个键（`name` / `id_card_no`——后端序列化器的字段名，⛔ 不是
    //    页内控件名 `certName` / `certSid`）；值 = 用户刚打进去的那两个。
    expect(vi.mocked(verifyRealName).mock.calls[0]![0]).toEqual({
      name: '测试姓名甲',
      id_card_no: '11010119900307561X',
    })
  })

  it('19 结果 `pass` ⇒ 页面上出现「已通过实名认证」（那一格 + 层里那一行都要有）', async () => {
    vi.mocked(verifyRealName).mockResolvedValueOnce({
      result: 'pass', id_card_masked: '110***1X', remaining: 3,
    })
    const root = await mountPage()
    root.querySelector<HTMLElement>(TO_CER_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    await fillAndSubmitCert('测试姓名甲', '11010119900307561X')

    // ① 页面上「是否实名」那一格当场变成已通过（与 ⑫ 同一组**值**）。
    const item = Array.from(root.querySelectorAll<HTMLElement>('.layui-form-label'))
      .find((el) => (el.textContent ?? '').trim() === '是否实名')
      ?.closest<HTMLElement>('.layui-form-item') ?? null
    expect(item, '证据件里应当有「是否实名」这一格').not.toBeNull()
    expect({ text: item!.querySelector('.layui-input-inline span')!.textContent })
      .toEqual({ text: REALNAME_PASS_TEXT })
    expect({ icon: item!.querySelector('i.layui-icon')!.classList.contains('layui-icon-vercode') })
      .toEqual({ icon: true })
    // ② 层里那一行结果（含后端这次返回的剩余次数）。
    expect(certResult()).toBe(`${REALNAME_PASS_TEXT}（剩余核验次数 3 次）`)
  })

  it('20 结果 `fail` ⇒ 出现失败提示，且⛔ **不出现**「已通过实名认证」', async () => {
    vi.mocked(verifyRealName).mockResolvedValueOnce({
      result: 'fail', id_card_masked: '110***1X', remaining: 2,
    })
    const root = await mountPage()
    root.querySelector<HTMLElement>(TO_CER_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    await fillAndSubmitCert('测试姓名甲', '11010119900307561X')

    expect(certResult()).toBe(`${REALNAME_FAIL_TEXT}（剩余核验次数 2 次）`)
    // ⛔ 关键的一半：**不许**显示成功 —— 三层都要查（层里 / 页面那一格 / 整份文档）。
    //    只查层里的话，「页面那一格被顺手写成已通过」照样绿。
    expect(certResult()).not.toContain(REALNAME_PASS_TEXT)
    const item = Array.from(root.querySelectorAll<HTMLElement>('.layui-form-label'))
      .find((el) => (el.textContent ?? '').trim() === '是否实名')
      ?.closest<HTMLElement>('.layui-form-item') ?? null
    expect({ text: item!.querySelector('.layui-input-inline span')!.textContent })
      .toEqual({ text: '未实名' })
    expect(document.body.textContent).not.toContain(REALNAME_PASS_TEXT)
  })

  it('21 ⭐ 业务码 `1026`（余量不足）⇒ 出现「余量不足 / 请联系客服购买」且**不显示成功**', async () => {
    vi.mocked(verifyRealName).mockRejectedValueOnce(
      new BizError(ErrorCode.REALNAME_QUOTA_EXHAUSTED, '本租户实名认证余量不足（已用 5 次、共 5 次，请联系客服购买）'),
    )
    const root = await mountPage()
    root.querySelector<HTMLElement>(TO_CER_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    await fillAndSubmitCert('测试姓名甲', '11010119900307561X')

    const text = certResult() ?? ''
    expect(text).toContain('余量不足')
    // ⭐ 业务方口径（spec §14-P1）：余量不足时要明确说「请联系客服购买」——用户知道下一步找谁。
    expect(text).toContain('请联系客服购买')
    expect(text).not.toContain(REALNAME_PASS_TEXT)
    expect(document.body.textContent).not.toContain(REALNAME_PASS_TEXT)
  })

  it('22 ⭐ 另外几档业务码各给**不同的人话**（1025 找管理员 / 1027 平台无包 / 1029 稍后再试 / 1000 号码不对 / 1017 可重试）', async () => {
    const root = await mountPage()
    // 各档独立（⛔ 不写成一个循环里的"随便哪句都行"）：每档断言它**该出现**的那句与
    // **不该出现**的那句（"该找谁"这个信息不许被抹成一句"核验失败"）。
    const cases: Array<[number, string, string]> = [
      // [业务码, 必须出现的关键词, ⛔ 不许出现的关键词]
      [ErrorCode.REALNAME_DISABLED, '尚未启用实名认证功能，请联系管理员', '请联系客服购买'],
      [ErrorCode.REALNAME_NO_PACKAGE, '暂无可用', '请联系客服购买'],
      // D-0251：`1029` 提交过于频繁 ⇒ 只说「稍后再试」（不锁定、窗口滚过自动恢复），
      // ⛔ 不说「被拉黑」、也不借 `1026` 让人去催客服买次数。
      [ErrorCode.REALNAME_RATE_LIMITED, '过于频繁', '请联系客服购买'],
      // ⚠️ `1000` 这一档页面**原样透传后端那句人话**（`realNameErrorText` 的 `VALIDATION` 支：
      //    `e.message || 兜底`）⇒ 判定的是「后端原文出现在了屏幕上」，⛔ 不是判定我方某句文案。
      //    这样钉的理由：后端那句是**唯一出处**（`services.py::RealNameInvalidInput`），
      //    前端另写一句就会有两个出处、迟早漂移。
      [ErrorCode.VALIDATION, '后端原文（夹具）', '请联系客服购买'],
      [1017, '请稍后重试', '未通过'],
    ]
    for (const [code, must, mustNot] of cases) {
      MSGS.length = 0
      vi.mocked(verifyRealName).mockRejectedValueOnce(new BizError(code, '后端原文（夹具）'))
      // 每一档都**重新开一次层**（否则上一条的层还在、结果行是同一个节点，读数会串）。
      document.querySelectorAll('.layui-layer').forEach((el) => el.remove())
      root.querySelector<HTMLElement>(TO_CER_SELECTOR)!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await fillAndSubmitCert('测试姓名甲', '11010119900307561X')
      const text = certResult() ?? ''
      expect({ code, text: text.includes(must) }).toEqual({ code, text: true })
      expect({ code, leaked: text.includes(mustNot) }).toEqual({ code, leaked: false })
      expect({ code, ok: text.includes(REALNAME_PASS_TEXT) }).toEqual({ code, ok: false })
    }
  })

  it('⑧ `.bindEmileBtn` / `.changeEmileBtn` **仍然**弹「尚未开放」（本线没顺手把它们改掉）', async () => {
    const root = await mountPage()
    // ⛔ 这一条是**反向判据**：实名认证线只该动 `.toCer` / `.startCertBtn`。
    //    两颗邮箱按钮**必须**还在「尚未开放」白名单里 —— 它们各自有别的线在做，
    //    "顺手接上"会让那两条线撞车（且用户会以为绑定邮箱已经能用了）。
    for (const [selector, label] of [
      ['.bindEmileBtn', '去绑定'],
      ['.changeEmileBtn', '更换绑定'],
    ] as const) {
      MSGS.length = 0
      document.querySelectorAll('.layui-layer').forEach((el) => el.remove())
      const el = root.querySelector<HTMLElement>(selector)
      expect(el, `证据件里应当有：${selector}`).not.toBeNull()
      el!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      expect({ [selector]: MSGS[0] }).toEqual({ [selector]: `「${label}」尚未开放` })
      // ⛔ 点了这两颗**不许**有任何层冒出来、也不许发核验请求。
      expect({ [selector]: openedLayer() }).toEqual({ [selector]: null })
      expect({ [selector]: vi.mocked(verifyRealName).mock.calls.length })
        .toEqual({ [selector]: 0 })
    }
  })

  /* ═════════ 实名入口那颗按钮的**显示条件** ═════════
   *
   * 现象（线上实测）：租户已开实名开关、额度与密钥包都配好了，用户进「我的信息」页
   * **点不了实名** —— 「是否实名」那一格只显示红色的「未实名」，右边那颗「点击进行实名认证」
   * **不显示**。
   *
   * 根因：证据件把那颗按钮的**容器写死了 `layui-hide`**，
   * 而 `renderRealName` 只改图标与文案、从没动过那个类 ⇒ 按钮永远隐藏。
   * 规则与证据等级（**推断**，`myinfo.js` 里 `layui-hide` 0 命中）写在 `renderRealName` 的 docstring 里。
   *
   * ⚠️ **这一组的杀手判据是 ㉓①/㉓④**：可见性判据必须能在「恒隐藏」的实现下变红
   * （下面 ⑯ 那些老用例是**直接对隐藏元素派发事件**，jsdom 不看可见性 ⇒ 它们在恒隐藏下照样绿）。
   */

  /** 那颗按钮的容器**类名**（写死字面量，⛔ 不从生产代码的常量算——`E-0024` 的自指断言）。 */
  const CONTAINER_CLASS = 'layui-form-mid'
  /**
   * `.toCer` 的容器（证据件原文：`<div class="layui-form-mid layui-hide" style="padding: 0!important;">`）。
   *
   * ⛔ **不用 `offsetParent` 当唯一判据**：本项目的 island 页测试**不加载 `layui.css`**
   * （页面模块只塞标记、不 `bootstrapIsland`）⇒ `.layui-hide{display:none}` 这条规则在 jsdom 里
   * **不存在**，`getComputedStyle` 读不到它，`offsetParent` 更是 jsdom 未实现的属性（恒 `null`）。
   * 于是「可见性」在本测试环境里的**真实载体**就是那个**类名**（+ 内联 `display`，两者都查）。
   * 证明力边界（如实）：它钉的是「`.layui-hide` 在不在」，**不是**真浏览器里的像素级可见性
   * ——后者的兜底是 `docs/FRONTEND-REPLICA-STANDARD.md` §6 的人工并排比对。
   */
  function toCerContainer(root: HTMLElement): HTMLElement {
    const button = root.querySelector<HTMLElement>(TO_CER_SELECTOR)
    expect(button, `证据件里应当有：${TO_CER_SELECTOR}`).not.toBeNull()
    const container = button!.parentElement
    expect(container, '那颗按钮应当有父容器（`layui-hide` 挂在这一层）').not.toBeNull()
    return container!
  }

  /** 「看不见」= 带 `layui-hide`（或内联 `display:none`）；两者都读，任一成立即算隐藏。 */
  function hiddenState(el: HTMLElement): { hidden: boolean; display: string } {
    return {
      hidden: el.classList.contains('layui-hide') || el.style.display === 'none',
      display: el.style.display,
    }
  }

  it('㉓① `enabled=true` + 未实名 ⇒ 那颗「点击进行实名认证」**可见**（容器上没有 `layui-hide`）', async () => {
    // ⓪ 「可见」**不是**出厂默认态 —— 判据钉在**证据件原文**上（⛔ 不是运行期 DOM：那已经是改过的）：
    //    那一格出厂就带 `layui-hide`（这正是业务方实测那个 bug 的来源）。
    //    ⚠️ 这里同时钉住 `TO_CER_CONTAINER_CLASS` 这个常量的**值**（写死字面量，`E-0024` 的反面）。
    const containerClass = toCerContainer(
      (() => {
        const holder = document.createElement('div')
        holder.innerHTML = EVIDENCE
        return holder
      })(),
    ).className
    expect({ containerClass }).toEqual({ containerClass: 'layui-form-mid layui-hide' })
    expect({ constant: TO_CER_CONTAINER_CLASS }).toEqual({ constant: CONTAINER_CLASS })

    const root = await mountPage() // 夹具 `PROFILE.realname.enabled = true`、`is_real_name = false`
    // ① `mount` 里那次 `renderRealName(root, false, true)` 必须把 `layui-hide` **摘掉**
    //    （这就是业务方实测那个 bug 的判据：页面自己把按钮显示出来）。
    const container = toCerContainer(root)
    const state = hiddenState(container)
    expect({ enabled: true, isRealName: false, ...state }).toEqual({
      enabled: true, isRealName: false, hidden: false, display: '',
    })
    // ② 纯函数再驱动一次同一组入参 ⇒ 幂等（⛔ 不是"只有首屏那一次碰巧对了"）。
    renderRealName(root, false, true)
    expect({ hidden: hiddenState(toCerContainer(root)).hidden }).toEqual({ hidden: false })
    // ③ 按钮本身还是证据件里那一颗（⛔ 我们只切显示，不换标记）。
    expect(container.querySelector(TO_CER_SELECTOR)?.textContent).toBe('点击进行实名认证')
  })

  it('㉓② `enabled=false` + 未实名 ⇒ 那颗按钮**仍然隐藏**（容器上仍有 `layui-hide`）', async () => {
    const root = await mountPage()
    renderRealName(root, false, false)
    expect({ enabled: false, ...hiddenState(toCerContainer(root)) }).toEqual({
      enabled: false, hidden: true, display: '',
    })
  })

  it('㉓③ 已实名 ⇒ 那颗按钮隐藏，且那一格文案是「已通过实名认证」', async () => {
    const root = await mountPage()
    // 已实名这一态**必须**把按钮藏起来：否则页面上同时出现「已通过实名认证」与
    // 「点击进行实名认证」两颗，自相矛盾（业务方口径）。
    renderRealName(root, true, true)
    const item = toCerContainer(root).closest<HTMLElement>('.layui-form-item')!
    expect({ text: item.querySelector('.layui-input-inline span')!.textContent })
      .toEqual({ text: REALNAME_PASS_TEXT })
    expect({ isRealName: true, ...hiddenState(toCerContainer(root)) }).toEqual({
      isRealName: true, hidden: true, display: '',
    })
    // ⚠️ 未启用 + 未实名这一档也顺手钉一次「恒隐藏」的反面（同一函数的另一组入参）。
    renderRealName(root, false, false)
    expect({ hidden: hiddenState(toCerContainer(root)).hidden }).toEqual({ hidden: true })
  })

  it('㉓④ ⭐ 驱动真交互：`enabled=true` + 未实名时**点那颗按钮** ⇒ 真的开出实名弹层', async () => {
    // ⛔ 这一条与 ㉓① 是**两件事**：㉓① 证明「看得见」，本条证明「点了真有用」。
    //    只测 ㉓① 的话，一个「摘掉类但没挂点击委托」的实现照样绿（`E-0061` 的"点了没反应"形态）。
    const root = await mountPage()
    const container = toCerContainer(root)
    expect({ hidden: hiddenState(container).hidden }).toEqual({ hidden: false })
    MSGS.length = 0
    // ⛔ 驱动的是**页面自己挂的那个委托**（点可见的容器里的那颗按钮，事件冒泡上去）——
    //    不是直接调 `openRealNameCert()` 一拍（那样"委托没挂上"也会绿）。
    container.querySelector<HTMLElement>(TO_CER_SELECTOR)!
      .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    expect({ toast: MSGS[0] }).toEqual({ toast: undefined }) // ⛔ 不是「尚未开放」
    const layer = openedLayer()
    expect(layer, '点了那颗应当开一个弹层').not.toBeNull()
    expect(document.querySelector(`#${REALNAME_NAME_INPUT_ID}`)).not.toBeNull()
    expect(document.querySelector(`#${REALNAME_SID_INPUT_ID}`)).not.toBeNull()
    expect(document.querySelector(`#${REALNAME_SUBMIT_BUTTON_ID}`)?.textContent).toBe('开始认证')
  })
})
