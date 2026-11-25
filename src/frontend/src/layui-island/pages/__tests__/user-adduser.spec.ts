// @vitest-environment jsdom
/**
 * 【新开 / 编辑系统子账户】layui island 页的测试。
 *
 * 三层覆盖（D-0086 之后补齐了第三层）：
 *  1. **纯函数**：校验文案、载荷组装、模式判定、提成标签/提示文案；
 *  2. **结构**：证据件与渲染出来的内层标记逐字对齐对标，以及本轮改动的**顺序与缺省**；
 *  3. **驱动真交互**：`mount()` 起一遍（用假的 `bootstrapIsland` / 假 layui），
 *     真的触发 radio 事件与 `focusin`，断言标签文案变了、提示弹了。
 *
 * ⚠️ **第三层是 2026-09-22 补的**：此前这页只有前两层（原 spec 头部就写着「不测挂载」），
 * 于是「提成模式切换不动标签」「金额格不给提示」这两条**漏接的 layui 事件**谁也看不见——
 * `typecheck` / `lint` 都不检查「事件注册了没有」。这正是 E-0061 记的那类静默失败。
 *
 * 迁移对照（原 `src/views/systemUsers/__tests__/UserFormView.spec.ts` → 本文件）：
 * | 原断言 | 现在 |
 * |---|---|
 * | 新增：必填缺失时本地拦下，不发请求 | `validateForm` 的「新增：必填缺失逐条拦下」 |
 * | 新增：载荷含必填项且**不含**服务端管理字段 | `buildPayload` 的键名断言 + 无 `password`/`is_active`/`is_superuser`/`is_staff` |
 * | 编辑：挂载时按 id 拉详情并回填；提交走 PATCH | `resolveFormMode` + `fillForm` + 挂载态断言 |
 * | 编辑：PATCH 不因六个「新增必填」缺席而本地拦下 | `validateForm(..., isEdit=true)` 放行 + 「空的数值键整键不发」 |
 */
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AccountRow } from '@/api/accounts'
import {
  PAYLOAD_MAP,
  applyCommissionLabel,
  buildPayload,
  commissionLabel,
  commissionTip,
  copyToClipboard,
  credentialClipboardText,
  credentialDialogText,
  fillForm,
  gamesMarkup,
  readCheckedGames,
  readCheckedRole,
  readIsStudio,
  readCommissionMode,
  resolveFormMode,
  rolesMarkup,
  validateForm,
  type FormValues,
} from '../user-adduser'

/**
 * 替身 `navigator.clipboard`，用完**必须** `restore()`（同一文件里的用例共用 window）。
 *
 * 为什么必须自己装：jsdom **不实现** `navigator.clipboard`（真浏览器只在安全上下文里给），
 * 而 `copyToClipboard` 的兜底分支用的 `document.execCommand`，jsdom **也没有**。
 * ⇒ 不装替身时「复制成功」这条路径根本走不到，测试会以为复制坏了。
 */
function stubClipboard(
  writeText: (text: string) => Promise<void> = async () => undefined,
): { writeText: ReturnType<typeof vi.fn>; restore: () => void } {
  const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  const spy = vi.fn(writeText)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText: spy }, configurable: true })
  return {
    writeText: spy,
    restore: () => {
      if (original) Object.defineProperty(navigator, 'clipboard', original)
      else Reflect.deleteProperty(navigator as unknown as Record<string, unknown>, 'clipboard')
    },
  }
}

/** 一份「填齐了」的表单值（键 = 对标的 `name`，值 = 字符串）。 */
function filledForm(extra: FormValues = {}): FormValues {
  return {
    loginAcc: 'e2ereplicacheck',
    userName: '复刻检查',
    bussType: '1',
    userType: '0',
    autoBrokerageType: '20',
    brokerageAmount: '5',
    recOrderMaxCount: '0',
    recOrderMaxCount2: '0',
    recPriceMin: '0',
    recPriceMax: '99999',
    inputDate: '2026-09-20',
    insureAmount: '0',
    ...extra,
  }
}

function doc(html: string): Document {
  return new DOMParser().parseFromString(`<div id="root">${html}</div>`, 'text/html')
}

/** 证据件里 `bussType` 那三个 radio 的原始标记（**抄自证据件**，不是运行时渲染的）。 */
const BUSS_MARKUP =
  '<input type="radio" name="bussType" value="1" title="默认" checked=""><div lay-radio><span>专做代练</span></div>' +
  '<input type="radio" name="bussType" value="2" title="默认"><div lay-radio><span>专做陪玩</span></div>' +
  '<input type="radio" name="bussType" value="0" title="默认"><div lay-radio><span>代练+陪玩</span></div>'

describe('user-adduser：证据件与内层标记（结构对齐对标）', () => {
  it('游戏块：内层是 `div.layui-inline` + 对标内联 style，input 是 `lay-skin="tag"`', () => {
    const d = doc(gamesMarkup([{ id: 7, name: '原神', sort_order: 0, is_active: true, created_at: '' }], [7]))
    const wrap = d.querySelector('#root > .layui-inline')!
    expect(wrap.getAttribute('style')).toBe('margin-right:5px; margin-top:0px')
    const input = d.querySelector('input[name="games"]')!
    expect(input.getAttribute('lay-skin')).toBe('tag')
    expect(input.getAttribute('title')).toBe('原神')
    expect((input as HTMLInputElement).checked).toBe(true)
  })

  it('职位块：内层 style 是 `width:130px; margin-top:0px`（对标 L323），value 用职位名', () => {
    const d = doc(rolesMarkup(['客服', '系统管理员'], '客服'))
    const wrap = d.querySelector('#root > .layui-inline')!
    expect(wrap.getAttribute('style')).toBe('width:130px; margin-top:0px')
    const inputs = Array.from(d.querySelectorAll<HTMLInputElement>('input[name="userRole"]'))
    expect(inputs.map((i) => i.value)).toEqual(['客服', '系统管理员'])
    // 对标的 `lay-filter="roleFilter"` 是职位 tooltip 的钩子，不能少。
    expect(inputs[0]!.getAttribute('lay-filter')).toBe('roleFilter')
    expect(inputs.map((i) => i.checked)).toEqual([true, false])
  })

  it('从 DOM 读勾选值：游戏读 id、职位读名字（对标的 value 是名字，我们两者都要）', () => {
    const d = doc(
      gamesMarkup(
        [
          { id: 1, name: '原神', sort_order: 0, is_active: true, created_at: '' },
          { id: 2, name: '鸣潮', sort_order: 1, is_active: true, created_at: '' },
        ],
        [2],
      ) + rolesMarkup(['客服', '代练员'], '代练员'),
    )
    expect(readCheckedGames(d.getElementById('root')!)).toEqual([2])
    expect(readCheckedRole(d.getElementById('root')!)).toBe('代练员')
  })
})

/**
 * ⛔ **这些断言钉的是「本轮改过的形状」**（D-0086）——它们读的是 `public/replica/` 下的
 * **证据件实物**，不是运行时 DOM。故意读实物：本轮那个「入职日期恒为 20 号」的缺陷
 * 就是证据件里写死的 `value` 造成的，而当时的测试只看代码、看不到它。
 */
describe('user-adduser：证据件的形状（业务类型顺序 / 入职日期默认值 / 短信格已删）', () => {
  it('业务类型三项的顺序是 1 专做代练 → 2 专做陪玩 → 0 代练+陪玩，且**默认选中专做代练**', () => {
    const html = readArtifact()
    const d = new DOMParser().parseFromString(html, 'text/html')
    const inputs = Array.from(d.querySelectorAll<HTMLInputElement>('input[name="bussType"]'))
    expect(inputs.map((i) => i.value)).toEqual(['1', '2', '0'])
    const checked = inputs.filter((i) => i.hasAttribute('checked'))
    expect(checked.map((i) => i.value)).toEqual(['1'])
  })

  it('入职日期**不带** `value` 属性（否则挂载时的「值空才补今天」永远不成立）', () => {
    const html = readArtifact()
    const d = new DOMParser().parseFromString(html, 'text/html')
    const hiredOn = d.querySelector('input[name="inputDate"]')!
    expect(hiredOn.hasAttribute('value')).toBe(false)
    // 反证：这个框确实是可改的普通文本框（不是只读控件）。
    expect(hiredOn.getAttribute('readonly')).toBe(null)
    expect(hiredOn.getAttribute('disabled')).toBe(null)
  })

  /**
   * ⚠️ **这一条补的是「守护网的漏洞」**（独立评审 2026-09-22 指出）：
   * 页面靠 `form.on('radio(autoBrokerageTypeFilter)')` 改标签，而真实 layui 的事件名是
   * `"radio(" + <该 radio 的 lay-filter> + ")"`（vendored `layui.js` 的 radio 分支）。
   * 第三层那个「假 layui」测试是按**字符串**记回调的 ⇒ 把证据件上的 `lay-filter` 属性**删掉**，
   * 它照样全绿，而真页面里点了标签不动、零红。所以必须在**实物**上钉这个属性。
   * 同类的先例：本文件上面那条职位块断言已经钉了 `lay-filter="roleFilter"`。
   */
  it('提成模式三个 radio 都带着 `lay-filter="autoBrokerageTypeFilter"`（页面的事件名靠它）', () => {
    const d = new DOMParser().parseFromString(readArtifact(), 'text/html')
    const inputs = Array.from(d.querySelectorAll<HTMLInputElement>('input[name="autoBrokerageType"]'))
    expect(inputs.map((i) => i.value)).toEqual(['10', '20', '30'])
    expect(inputs.map((i) => i.getAttribute('lay-filter'))).toEqual([
      'autoBrokerageTypeFilter',
      'autoBrokerageTypeFilter',
      'autoBrokerageTypeFilter',
    ])
  })

  it('提成金额那一格有 `id="brokerageAmount"`（`layer.tips` 的锚点 / 对标同款）', () => {
    const d = new DOMParser().parseFromString(readArtifact(), 'text/html')
    expect(d.querySelector('input[name="brokerageAmount"]')!.getAttribute('id')).toBe('brokerageAmount')
  })

  it('「合作用户」选项有意不渲染（D-0246）：option 留在 DOM 但整个装进隐藏容器，「内部成员」仍可见', () => {
    const d = new DOMParser().parseFromString(readArtifact(), 'text/html')
    // ⛔ 判据打在 **DOM** 上，不打字符串（E-0161：先去掉注释再看标记——
    //    本格上方的 D-0246 注释里就写着「合作用户」四个字，`toContain` 会被自己的注释弄红）。
    const hiddenOption = d.querySelector('input[name="userType"][value="1"]')
    expect(hiddenOption, 'value=1 的 option 必须留在 DOM（member_type=1 回填要命中）').not.toBe(null)
    // 隐藏容器：`display:none` 的 span 把 input 包住（layui 渲染出的壳插在 input 之后，
    // 仍在这个 span 里 ⇒ 一并隐藏；`[lay-radio]` 原标签本就被 layui.css 隐藏）。
    const wrapper = hiddenOption!.closest<HTMLElement>('span[style*="display:none"]')
    expect(wrapper, 'value=1 必须装进 display:none 的容器').not.toBe(null)
    // 它的 lay-radio 标签（渲染文案的出处）也在同一个隐藏容器里。
    const label = hiddenOption!.nextElementSibling
    expect(label?.getAttribute('lay-radio')).not.toBe(null)
    expect(wrapper!.contains(label!)).toBe(true)
    // 「内部成员」（value=0）仍在、**不在**任何隐藏容器里（页面上看得见的那个选项）。
    const visibleOption = d.querySelector('input[name="userType"][value="0"]')
    expect(visibleOption).not.toBe(null)
    expect(visibleOption!.closest('span[style*="display:none"]')).toBe(null)
    // 新增默认仍是 value=0（member_type 提交默认 0 不变）。
    expect(visibleOption!.hasAttribute('checked')).toBe(true)
    expect(hiddenOption!.hasAttribute('checked')).toBe(false)
  })

  it('「录的单不发短信」已从证据件里删掉（前端不再渲染这一格）', () => {
    const html = readArtifact()
    const d = new DOMParser().parseFromString(html, 'text/html')
    // ⚠️ 判据钉在 **DOM** 上，不是 `not.toContain(...)` 字符串：
    //    证据件的头部注释里**故意**留着 `addOrderNotSendSms` 这个名字（记「我们删了哪一格」），
    //    用 `toContain` 会被自己的注释弄红（反过来若改注释迁就测试，那一笔历史就没了）。
    expect(d.querySelector('input[name="addOrderNotSendSms"]')).toBe(null)
    // 那两个 radio 的 label 文案也不再出现在**表单**里（注释里提到它不算——判据读 `#LAY_app`）。
    expect(d.querySelector('#LAY_app')!.textContent).not.toContain('录的单不发短信')
    // 阳性对照：同一个表单里别的 radio 还在，证明这张 DOM 是解析成功了的、不是空壳。
    expect(d.querySelector('input[name="bussType"]')).not.toBe(null)
  })
})

/** 读证据件实物（`public/replica/user-adduser.html`）。
 *  ⚠️ 本文件在 `src/frontend/src/layui-island/pages/__tests__/`，所以到 `src/frontend/` 是**四层**
 *  （`pii-guard.spec.ts` 在 `src/config/__tests__/`，那里是三层——别照抄那个数字）。 */
function readArtifact(): string {
  const here = path.dirname(fileURLToPath(import.meta.url))
  return readFileSync(path.resolve(here, '../../../../public/replica/user-adduser.html'), 'utf8')
}

describe('user-adduser：新增 / 编辑 的模式判定（路由参数拿不到时读父窗口 URL）', () => {
  it('父窗口是 `/system-users/9/edit` → 编辑态、id=9', () => {
    expect(resolveFormMode('?page=user-adduser', 'http://127.0.0.1:5173/system-users/9/edit')).toEqual({
      mode: 'edit',
      id: 9,
    })
  })

  it('宿主拼出来的 `?id=` 优先（`LayuiIslandHost` 会把路由参数拼进 iframe 的 query）', () => {
    expect(resolveFormMode('?page=user-adduser&id=9', '')).toEqual({ mode: 'edit', id: 9 })
  })

  it('直接打开 iframe（referrer 为空、也没有 id）→ 新增态', () => {
    expect(resolveFormMode('?page=user-adduser', '')).toEqual({ mode: 'new' })
  })

  it('非数字/0 不算 id', () => {
    expect(resolveFormMode('?page=user-adduser&id=abc', '')).toEqual({ mode: 'new' })
    expect(resolveFormMode('?page=user-adduser&id=0', '')).toEqual({ mode: 'new' })
  })
})

describe('user-adduser：本地校验（文案与判据取自对标 adduser.js L510-657）', () => {
  it('新增：必填缺失逐条拦下（原断言「必填缺失时本地拦下，不发请求」的迁移）', () => {
    const cases: [FormValues, string][] = [
      [{}, '登录账号不能为空'],
      [filledForm({ loginAcc: '' }), '登录账号不能为空'],
      [filledForm({ loginAcc: 'a' }), '登录账号长度最小是2位，请补齐'],
      [filledForm({ loginAcc: '复刻$检查' }), '登录账号不能有特殊字符'],
      [filledForm({ userName: '' }), '姓名/名称不能为空'],
      [filledForm({ userName: '复刻 检查' }), '姓名/名称不能有空格'],
      [filledForm({ userName: '_复刻' }), '姓名/名称首尾不能出现下划线'],
      [filledForm({ moblie: '12345' }), '输入的联系电话格式不正确，要么请留空'],
      [filledForm({ recPriceMin: 'abc' }), '内部抢单价格范围格式不正确，只能填数字0到99999'],
      [filledForm({ brokerageAmount: '五块' }), '提成金额/百分比只能填数字'],
      [filledForm({ inputDate: '' }), '请选择入职日期'],
      [filledForm({ brokerageAmount: '' }), '请填写提成金额/百分比'],
    ]
    for (const [values, expected] of cases) {
      expect(validateForm(values, [1], '客服', false)).toBe(expected)
    }
    // 游戏与职位是 DOM 状态，不在 `values` 里 —— 各自单独一例。
    expect(validateForm(filledForm(), [], '客服', false)).toBe('请为该用户至少选择一款游戏权限')
    expect(validateForm(filledForm(), [1], '', false)).toBe('请为该用户选择一个职位权限')
  })

  it('填齐后放行（新增）', () => {
    expect(validateForm(filledForm(), [1], '客服', false)).toBe('')
  })

  it('编辑：不因「新增必填」缺席而拦下（PATCH 缺席 = 保持原值）', () => {
    const sparse: FormValues = {
      loginAcc: 'admin',
      userName: '管理员',
      inputDate: '',
      bussType: '1',
      recPriceMin: '0',
      recPriceMax: '99999',
    }
    expect(validateForm(sparse, [1], '系统管理员', true)).toBe('')
  })
})

describe('user-adduser：提成模式的标签与提示文案（对标 i18n 与 adduser.js L587-608）', () => {
  it('标签：10 →「固定提成金额」，20/30 →「百分比数值」', () => {
    expect(commissionLabel(10)).toBe('固定提成金额')
    expect(commissionLabel(20)).toBe('百分比数值')
    expect(commissionLabel(30)).toBe('百分比数值')
  })

  it('提示：10 → 固定金额那句；20/30 → 百分比那句（逐字，含全角标点）', () => {
    expect(commissionTip(10)).toBe('请输入固定的金额，例如：5、10，默认为0')
    expect(commissionTip(20)).toBe('请输入百分比纯数字（不要有百分号），例如：5，代表5%')
    expect(commissionTip(30)).toBe('请输入百分比纯数字（不要有百分号），例如：5，代表5%')
  })

  it('从 DOM 读当前模式：没有选中项时回落 20（我方后端默认模式）', () => {
    const empty = doc('').getElementById('root')!
    expect(readCommissionMode(empty)).toBe(20)
    const picked = doc(
      '<input type="radio" name="autoBrokerageType" value="10" checked>' +
        '<input type="radio" name="autoBrokerageType" value="20">',
    ).getElementById('root')!
    expect(readCommissionMode(picked)).toBe(10)
  })

  it('`applyCommissionLabel` 把文案写进 `.tcLabel`（挂载时与切换时都走它）', () => {
    for (const [mode, expected] of [
      ['10', '固定提成金额'],
      ['20', '百分比数值'],
      ['30', '百分比数值'],
    ] as const) {
      const root = doc(
        `<label class="layui-form-label tcLabel">提成金额/百分比</label>` +
          `<input type="radio" name="autoBrokerageType" value="${mode}" checked>`,
      ).getElementById('root')!
      applyCommissionLabel(root)
      expect(root.querySelector('.tcLabel')!.innerHTML).toBe(expected)
    }
  })
})

/**
 * ⚠️ **第三层：驱动真交互**。这一层用**假 layui**（不加载 vendored 的 3.9MB `layui.js`）：
 * `bootstrapIsland` 被 mock 掉，`form.on` 把回调记下来由测试手动触发。
 *
 * **它能抓到什么**：事件根本没注册（页面里没有那个 `form.on`）⇒ 回调没人记 ⇒ 断言红。
 * **它抓不到什么（如实）**：真实 layui 内部的 `radio(...)` 事件会不会带我们假设的
 * `{ value }` 形状、`layer.tips` 的锚点解析是否正常——那些要真浏览器才算（登记在
 * spec §5 的人工目视验收里）。
 */
describe('user-adduser：挂载后驱动真交互（标签 / 焦点提示 / 双态出口）', () => {
  /**
   * 造一个最小可用的假 layui，并把页面挂起来；返回触发事件用的把手。
   *
   * ⭐ Task 13 之后本函数多了两个可选入参（`values` / `createResult`）与几个把手
   * （`navigate` / `openArgs` / `alertArgs`）——保存成功那条路要能走完（`form.val` 得回真表单值），
   * 新增成功后那个凭据弹窗的 `cb` 从 `alertArgs` 里取（2026-09-23 起它由 `layer.alert` 弹出）。
   * 既有三层的调用方式没变。
   */
  async function mountWithFakeLayui(
    account: AccountRow | null,
    artifactHtml: string,
    extra: {
      /** 假 `form.val('sform')` 的返回值（真 layui 读的是表单的实际值）。 */
      values?: FormValues
      /** `createAccount` 的解析值（新增态成功后要弹「账号创建成功」层）。 */
      createResult?: { account: { username?: string }; initial_password: string }
    } = {},
  ) {
    const onHandlers = new Map<string, (obj: { value?: string; elem?: HTMLInputElement }) => void>()
    const tips = vi.fn()
    const msg = vi.fn()
    const navigate = vi.fn()
    /**
     * 每次 `layer.open` 的入参。本页 2026-09-23 起**不再**用它弹凭据层（改回对标那种 `layer.alert`），
     * 这个把手留着当哨兵：「不该再有人自己拼 `layer.open` 弹提示」。
     */
    const openArgs: Record<string, unknown>[] = []
    /**
     * 每次 `layer.alert(content, opts, cb)` 的入参（2026-09-23 复刻对标之后的凭据弹窗）。
     * `cb` 由用例**手动触发** = 用户点那颗「确定」。
     */
    const alertArgs: { content: string; opts: unknown; cb: (i: number) => void }[] = []
    const fakeLayui = {
      form: {
        val: () => extra.values ?? {},
        render: vi.fn(),
        on: (event: string, cb: (obj: { value?: string; elem?: HTMLInputElement }) => void) => {
          onHandlers.set(event, cb)
        },
      },
      layer: {
        tips,
        msg,
        open: vi.fn((o: unknown) => {
          openArgs.push(o as Record<string, unknown>)
          return openArgs.length
        }),
        /**
         * `layer.alert(content, opts, cb)`——新增成功后那个「一次性凭据」弹窗。
         * ⚠️ 本页**故意**不再自己拼 `layer.open({btn:['返回人员管理']})`：对标用的是**标准 alert**
         * （标题「信息」、`icon:1`、一颗「确定」、右上角关闭 X），复刻它是业务方 2026-09-23 的诉求。
         */
        alert: vi.fn((content: string, opts: unknown, cb: (i: number) => void) => {
          alertArgs.push({ content, opts, cb })
          return alertArgs.length
        }),
        close: vi.fn(),
      },
      laydate: { render: vi.fn() },
    }
    vi.resetModules()
    vi.doMock('@/layui/island', () => ({
      bootstrapIsland: async () => fakeLayui,
      initIslandElements: () => undefined,
    }))
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(artifactHtml, { status: 200 })),
    )
    vi.doMock('@/api/accounts', () => ({
      // ⚠️ **这些桩必须真的返回值（返回 Promise），不能写裸 `vi.fn()`**。
      //    实测（2026-09-22，探针 + 独立评审复核）：裸 `vi.fn()` 调一次得到 `undefined`，
      //    而页面里是 `fetchAccountRoles().catch(...)` ⇒ 直接
      //    `TypeError: Cannot read properties of undefined (reading 'catch')`，
      //    **挂载整段抛错**（不是静默降级），三条挂载用例全红。
      //    ⛔ 本注释上一版把原因解释成「vitest 会把不带类型签名的 `vi.fn()` 判成仅类型导出而丢掉」——
      //    **那句是错的**：类型注解在运行时两种写法都被擦除，vitest 的 `doMock` 工厂返回什么就是什么
      //    （实测探针：裸 `vi.fn()` 拿到的仍是 `function`）。真实差别只有「桩有没有返回值」。
      //    ⛔ 另一处曾写「**静默**退化成新增态」也是错的：`getAccount` 返回 `undefined` 时
      //    `.catch(...)` 会响亮抛错，不会安静地当新建态。
      createAccount: async (_payload: unknown) =>
        extra.createResult ?? { account: {}, initial_password: '' },
      fetchAccountRoles: async (): Promise<string[]> => ['客服', '代练员'],
      getAccount: async (_id: number): Promise<AccountRow | null> => account,
      updateAccount: async (_id: number, _payload: unknown) => ({}),
    }))
    vi.doMock('@/api/config', () => ({
      fetchGames: vi.fn(async () => [
        { id: 1, name: '原神', sort_order: 0, is_active: true, created_at: '' },
      ]),
      fetchAllGames: vi.fn(async () => [
        { id: 1, name: '原神', sort_order: 0, is_active: true, created_at: '' },
      ]),
    }))

    const { mountUserFormPage: mountFresh } = await import('../user-adduser')
    const root = document.createElement('div')
    document.body.appendChild(root)
    // ⚠️ `mode` 必须跟着账号走：给了一份详情却仍按「新建」挂载时，`getAccount` **根本不会被调用**
    //    （编辑与新建共用本模块，靠的就是这个 mode）⇒ 那样测的是新建态，回填压根没发生。
    const mode = account
      ? ({ mode: 'edit', id: account.id } as const)
      : ({ mode: 'new' } as const)
    const unmount = await mountFresh(root, { onNavigate: navigate, mode })
    return { root, onHandlers, tips, msg, unmount, fakeLayui, navigate, openArgs, alertArgs }
  }

  it('挂载时标签**主动算一次**（编辑一个「固定金额」的账号，不能停在「提成金额/百分比」）', async () => {
    const html = readArtifact()
    const account: AccountRow = {
      id: 9,
      username: 'kefu01',
      display_name: '客服一号',
      is_active: true,
      is_tenant_admin: false,
      role: '客服',
      games: [1],
      last_login: null,
      date_joined: '',
      commission_mode: 10,
    }
    const { root } = await mountWithFakeLayui(account, html)
    expect(root.querySelector('.tcLabel')!.innerHTML).toBe('固定提成金额')
    vi.doUnmock('@/layui/island')
    vi.doUnmock('@/api/accounts')
    vi.doUnmock('@/api/config')
    vi.unstubAllGlobals()
  })

  it('游戏权限多选取的是**全量**（fetchAllGames），⛔ 不是按当前用户 game_ids 收窄的 fetchGames（防死锁）', async () => {
    const html = readArtifact()
    await mountWithFakeLayui(null, html)
    const cfg = await import('@/api/config')
    expect(vi.mocked(cfg.fetchAllGames)).toHaveBeenCalled()
    expect(vi.mocked(cfg.fetchGames)).not.toHaveBeenCalled()
    vi.doUnmock('@/layui/island')
    vi.doUnmock('@/api/accounts')
    vi.doUnmock('@/api/config')
    vi.unstubAllGlobals()
  })

  it('切到「固定金额」→ 标签变「固定提成金额」；切到百分比 → 变回「百分比数值」', async () => {
    const html = readArtifact()
    const { root, onHandlers } = await mountWithFakeLayui(null, html)
    const handler = onHandlers.get('radio(autoBrokerageTypeFilter)')
    expect(handler, '页面必须注册 `radio(autoBrokerageTypeFilter)`，否则点了标签不动').toBeTypeOf('function')

    const fixed = root.querySelector<HTMLInputElement>('input[name="autoBrokerageType"][value="10"]')!
    fixed.checked = true
    handler!({ value: '10' })
    expect(root.querySelector('.tcLabel')!.innerHTML).toBe('固定提成金额')

    const percent = root.querySelector<HTMLInputElement>('input[name="autoBrokerageType"][value="20"]')!
    fixed.checked = false
    percent.checked = true
    handler!({ value: '20' })
    expect(root.querySelector('.tcLabel')!.innerHTML).toBe('百分比数值')
    vi.doUnmock('@/layui/island')
    vi.doUnmock('@/api/accounts')
    vi.doUnmock('@/api/config')
    vi.unstubAllGlobals()
  })

  it('提成金额格获得焦点时弹提示，文案与 `time` 随当前模式变', async () => {
    const html = readArtifact()
    const { root, tips } = await mountWithFakeLayui(null, html)
    const amount = root.querySelector<HTMLInputElement>('input[name="brokerageAmount"]')!

    // 默认模式 20（证据件里 `value="20"` 带 checked）。
    amount.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    expect(tips).toHaveBeenCalledTimes(1)
    expect(tips.mock.calls[0]![0]).toBe('请输入百分比纯数字（不要有百分号），例如：5，代表5%')
    expect(tips.mock.calls[0]![2]).toMatchObject({ tips: [3, '#FF6E97'], time: 4000, tipsMore: false })

    // 换到固定金额模式，再来一次。
    const fixed = root.querySelector<HTMLInputElement>('input[name="autoBrokerageType"][value="10"]')!
    root.querySelector<HTMLInputElement>('input[name="autoBrokerageType"][value="20"]')!.checked = false
    fixed.checked = true
    amount.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    expect(tips).toHaveBeenCalledTimes(2)
    expect(tips.mock.calls[1]![0]).toBe('请输入固定的金额，例如：5、10，默认为0')
    expect(tips.mock.calls[1]![2]).toMatchObject({ time: 3000 })
    vi.doUnmock('@/layui/island')
    vi.doUnmock('@/api/accounts')
    vi.doUnmock('@/api/config')
    vi.unstubAllGlobals()
  })

  /* ⭐ Task 13：「保存后回列表」的**双态出口** ─────────────────────────────── */

  /**
   * 本页同时服务两条路线：① 整页路由 `/system-users/new`、`/system-users/:id/edit`
   * （父窗口是 Vue 壳，`onNavigate` 有效）；② 人员管理岛页的 `layer.open({type:2})` 弹层
   * （父窗口是另一个岛页，`island:navigate` 的 postMessage **没人监听** ⇒ 旧实现在弹层里是
   * 「保存成功却什么都不发生」）。
   *
   * ⚠️ jsdom 里 `window.parent === window`，所以弹层态用例先 `Object.defineProperty` 装一个
   * 假父层。**桩有没有被读到**由 `getFrameIndex` 的调用 + `close('7')` 一起证明：桩没接上时
   * 两条断言都拿不到，用例会真的红（不是「看起来绿」）。
   */
  describe('保存后回列表：弹层里关自己 / 整页路由仍 onNavigate', () => {
    /** jsdom 原本的 `parent` 描述符；同一文件里的用例共用 window，用完必须装回去。 */
    const ORIGINAL_PARENT = Object.getOwnPropertyDescriptor(window, 'parent')!

    afterEach(() => {
      Object.defineProperty(window, 'parent', ORIGINAL_PARENT)
    })

    /** 装一个假父层（`index` 是 `layer.getFrameIndex` 的返回值）。 */
    function stubParentLayer(index: unknown): {
      close: ReturnType<typeof vi.fn>
      getFrameIndex: ReturnType<typeof vi.fn>
    } {
      const close = vi.fn()
      const getFrameIndex = vi.fn(() => index)
      Object.defineProperty(window, 'parent', {
        value: { layer: { getFrameIndex, close } },
        configurable: true,
      })
      return { close, getFrameIndex }
    }

    /** 让 promise 链跑完（替身都是已 resolve 的 promise）。 */
    const flush = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0))

    /** 触发提交（真的走页面注册的 `submit(savebtn)` 回调）。 */
    function submitSave(
      handlers: Map<string, (obj: { value?: string; elem?: HTMLInputElement }) => void>,
    ): void {
      const handler = handlers.get('submit(savebtn)')
      expect(handler, '页面必须注册 `submit(savebtn)`').toBeTypeOf('function')
      // 假 layui 记回调时用的签名是 `{ value?, elem? }`，而本页注册的那个只读 `field`
      // （`readTextValues` 会去调 `form.val`）⇒ 事件对象本身在运行时没人看，用 `as never` 过类型。
      handler!({} as never)
    }

    function cleanupModules(): void {
      vi.doUnmock('@/layui/island')
      vi.doUnmock('@/api/accounts')
      vi.doUnmock('@/api/config')
      vi.unstubAllGlobals()
    }

    const EDIT_ACCOUNT: AccountRow = {
      id: 9,
      username: 'kefu01',
      display_name: '客服一号',
      is_active: true,
      is_tenant_admin: false,
      role: '客服',
      games: [1],
      last_login: null,
      date_joined: '',
    }

    it('弹层模式（编辑）：保存成功 ⇒ 关掉承载自己的外层（`close(getFrameIndex(name))`），不再 `onNavigate`', async () => {
      const { close, getFrameIndex } = stubParentLayer('7')
      const { onHandlers, msg, navigate } = await mountWithFakeLayui(EDIT_ACCOUNT, readArtifact(), {
        values: filledForm(),
      })

      submitSave(onHandlers)
      await flush()

      expect(msg).toHaveBeenCalledWith('已保存', { time: 2000 })
      expect(getFrameIndex, '桩没接上：页面没有读 `window.parent.layer`').toHaveBeenCalled()
      expect(close).toHaveBeenCalledWith('7')
      expect(
        navigate,
        '弹层里 `onNavigate` 只是发给人员管理岛页的 postMessage，那边不监听 ⇒ 不能再依赖它',
      ).not.toHaveBeenCalled()
      cleanupModules()
    })

    it('整页模式（编辑）：父层拿不到层索引 ⇒ 仍 `onNavigate("system-users")`，且**不**去关别人的层', async () => {
      const { close } = stubParentLayer(undefined)
      const { onHandlers, navigate } = await mountWithFakeLayui(EDIT_ACCOUNT, readArtifact(), {
        values: filledForm(),
      })

      submitSave(onHandlers)
      await flush()

      expect(navigate).toHaveBeenCalledWith('system-users')
      expect(close).not.toHaveBeenCalled()
      cleanupModules()
    })

    it('弹层模式（新增）：弹**标准 `layer.alert`**；点「确定」⇒ 复制凭据 + 提示，1.2s 后关的是**外层**', async () => {
      const { close } = stubParentLayer('7')
      const clipboard = stubClipboard()
      vi.useFakeTimers()
      try {
        const { root, onHandlers, alertArgs, msg, navigate } = await mountWithFakeLayui(
          null,
          readArtifact(),
          {
            values: filledForm(),
            createResult: { account: { username: 'newbie' }, initial_password: 'Abc12345' },
          },
        )
        // 新增态没有回填，游戏与职位得用户自己点（这里直接置 DOM 状态，等价于那两次点击）。
        root.querySelector<HTMLInputElement>('input[name="games"]')!.checked = true
        root.querySelector<HTMLInputElement>('input[name="userRole"]')!.checked = true

        submitSave(onHandlers)
        await vi.advanceTimersByTimeAsync(0)

        // ① 弹的是 `layer.alert`，不是自造的 `layer.open({btn:[…]})`（对标 `adduser.js:713`）。
        expect(alertArgs).toHaveLength(1)
        const { content, opts, cb } = alertArgs[0]!
        // ② 正文**逐字**等于对标服务端那句（2026-09-23 在对标站实测抄回）。
        expect(content).toBe('新开子账户成功，该用户登录账号：newbie，初始登录密码：Abc12345')
        // ③ 「标准 alert」的实参：**只有** `{icon:1}`（对标也没传标题，标题「信息」是 layui 默认值）
        //    ⇒ 绿色对勾 + 标题「信息」+ 默认那颗「确定」+ 关闭 X。
        //    ⛔ 不能传 `closeBtn:0`、也不能再塞我方自拟的灰字行——`toEqual` 恰好相等就是这两条钉子。
        expect(opts).toEqual({ icon: 1 })

        // ④ 「复制」与「离开」**都挂在点「确定」之后**（对标把两件事都写进 `yes` 回调）。
        expect(clipboard.writeText).not.toHaveBeenCalled()
        expect(close).not.toHaveBeenCalled()

        cb(1)
        await vi.advanceTimersByTimeAsync(0)
        // ⑤ 进剪贴板的是对标 `data.copyStr` 的后两段（开头那截网址是有意去掉的，见函数注释）；
        //    提示文案与时长逐字照抄对标（`adduser.js:292-293` / `:735-737`）。
        expect(clipboard.writeText).toHaveBeenCalledWith('你的登录账号：newbie 初始登录密码：Abc12345')
        expect(msg).toHaveBeenCalledWith('已成功复制到剪贴板', { time: 4000 })

        // ⑥ 复制完**先等 1.2s**（对标 `data.copyStr ? 1200 : 0`）再回列表，好让提示看得见。
        expect(close).not.toHaveBeenCalled()
        await vi.advanceTimersByTimeAsync(1200)
        expect(close).toHaveBeenCalledWith('7')
        expect(navigate).not.toHaveBeenCalled()
      } finally {
        vi.useRealTimers()
        clipboard.restore()
        cleanupModules()
      }
    })

    it('复制失败时提示「复制失败了」（1000ms），但**仍然**回列表（不能把人卡在弹窗里）', async () => {
      const { close } = stubParentLayer('7')
      const clipboard = stubClipboard(async () => {
        throw new Error('NotAllowedError')
      })
      vi.useFakeTimers()
      try {
        const { root, onHandlers, alertArgs, msg } = await mountWithFakeLayui(null, readArtifact(), {
          values: filledForm(),
          createResult: { account: { username: 'newbie' }, initial_password: 'Abc12345' },
        })
        root.querySelector<HTMLInputElement>('input[name="games"]')!.checked = true
        root.querySelector<HTMLInputElement>('input[name="userRole"]')!.checked = true

        submitSave(onHandlers)
        await vi.advanceTimersByTimeAsync(0)
        alertArgs[0]!.cb(1)
        await vi.advanceTimersByTimeAsync(0)

        // jsdom 里 `document.execCommand` 也不存在 ⇒ 兜底那条也失败 ⇒ 走「复制失败了」。
        expect(msg).toHaveBeenCalledWith('复制失败了', { time: 1000 })
        await vi.advanceTimersByTimeAsync(1200)
        expect(close).toHaveBeenCalledWith('7')
      } finally {
        vi.useRealTimers()
        clipboard.restore()
        cleanupModules()
      }
    })

    it('弹窗正文里的账号会被转义（账号是用户可控值，`layer.alert` 走 innerHTML）', async () => {
      stubParentLayer(undefined)
      const clipboard = stubClipboard()
      vi.useFakeTimers()
      try {
        const { root, onHandlers, alertArgs } = await mountWithFakeLayui(null, readArtifact(), {
          values: filledForm(),
          createResult: {
            account: { username: '<img src=x onerror=alert(1)>' },
            initial_password: 'Abc12345',
          },
        })
        root.querySelector<HTMLInputElement>('input[name="games"]')!.checked = true
        root.querySelector<HTMLInputElement>('input[name="userRole"]')!.checked = true

        submitSave(onHandlers)
        await vi.advanceTimersByTimeAsync(0)

        const content = alertArgs[0]!.content
        expect(content).toContain('&lt;img src=x onerror=alert(1)&gt;')
        expect(content).not.toContain('<img')
      } finally {
        vi.useRealTimers()
        clipboard.restore()
        cleanupModules()
      }
    })

    it('整页模式（新增）：同一套「点确定」流程 ⇒ 不关任何层，仍 `onNavigate("system-users")`', async () => {
      const { close } = stubParentLayer(undefined)
      const clipboard = stubClipboard()
      vi.useFakeTimers()
      try {
        const { root, onHandlers, alertArgs, navigate } = await mountWithFakeLayui(null, readArtifact(), {
          values: filledForm(),
          createResult: { account: { username: 'newbie' }, initial_password: 'Abc12345' },
        })
        root.querySelector<HTMLInputElement>('input[name="games"]')!.checked = true
        root.querySelector<HTMLInputElement>('input[name="userRole"]')!.checked = true

        submitSave(onHandlers)
        await vi.advanceTimersByTimeAsync(0)
        alertArgs[0]!.cb(1)
        await vi.advanceTimersByTimeAsync(1200)

        expect(navigate).toHaveBeenCalledWith('system-users')
        expect(close).not.toHaveBeenCalled()
      } finally {
        vi.useRealTimers()
        clipboard.restore()
        cleanupModules()
      }
    })
  })

  /* ── 编辑态：对标那几个 `lay-verify="required"` 的框**绝不能被回填成空串** ───────────── */

  it('编辑态回填后：凡是 `lay-verify` 带 `required` 的输入框都**不许是空值**（否则 layui 拦下提交）', async () => {
    // 复现（业务方 2026-09-23 报：「编辑子账户点『保存资料』→ 必填项不能为空，看不出哪些是必填，
    // 全部填了后还是出现」）：账号那几列在库里可以是 `null`（`base_salary` / `grab_price_min` /
    // `grab_price_max` 都是 `null=True`），而 `fillForm` 把 `null` 写成**空串** ⇒
    // layui 提交前的 `validate($('*[lay-verify]'))` 用它的 `required` 规则直接失败：
    //   `required:function(e){if(!/[\S]+/.test(e)||e===undefined||null===e)return '必填项不能为空'}`
    // （`public/layui/layui.js`，**读码**复核；其余规则都是 `if(e&&…)` ⇒ 空值只被 `required` 挡）
    // ⇒ 页面注册的 `submit(savebtn)` 回调**根本不会执行**，所以「全填了也没用」。
    // ⛔ 最狠的一处是 `basicSalary`：那一格在证据件里是 `style="display:none"`（对标原文亦然），
    //    用户**看不到也填不了** ⇒ 编辑态永远存不了。对标的服务端**总会渲染一个值**
    //    （证据件里那几个框带 `value="0"` / `value="99999"`），所以它那边 `required` 从不生效。
    const account: AccountRow = {
      id: 9,
      username: 'kefu01',
      display_name: '客服一号',
      is_active: true,
      is_tenant_admin: false,
      role: '客服',
      games: [1],
      last_login: null,
      date_joined: '',
    }
    const { root } = await mountWithFakeLayui(account, readArtifact())

    // 判据打在**真实出口**上：layui 真正读的就是这几个属性（不是 `fillForm` 的中间产物）。
    const emptyRequired = [...root.querySelectorAll<HTMLInputElement>('input[lay-verify]')]
      .filter((el) => (el.getAttribute('lay-verify') ?? '').split('|').includes('required'))
      .filter((el) => el.value.trim() === '')
      .map((el) => el.getAttribute('name'))
    expect(
      emptyRequired,
      '这些带 `required` 的框被回填成空串 ⇒ layui 会拦下提交，而消息笼统说「必填项不能为空」',
    ).toEqual([])

    // 期望值**写字面值**（E-0024）：回落的必须是**对标证据件里的原值**，不是随便一个数。
    const value = (n: string): string =>
      root.querySelector<HTMLInputElement>(`input[name="${n}"]`)!.value
    expect(value('basicSalary')).toBe('0')
    expect(value('recPriceMin')).toBe('0')
    expect(value('recPriceMax')).toBe('99999')

    vi.doUnmock('@/layui/island')
    vi.doUnmock('@/api/accounts')
    vi.doUnmock('@/api/config')
    vi.unstubAllGlobals()
  })
})

describe('user-adduser：提交载荷', () => {
  it('载荷键名与 `AccountWritePayload` 对齐；**不含**服务端管理字段（原断言「载荷不含 password/is_active」的迁移）', () => {
    const payload = buildPayload(filledForm(), [3, 4], '客服') as unknown as Record<string, unknown>
    expect(payload.username).toBe('e2ereplicacheck')
    expect(payload.display_name).toBe('复刻检查')
    expect(payload.role).toBe('客服')
    expect(payload.games).toEqual([3, 4])
    expect(payload.business_type).toBe(1)
    expect(payload.commission_mode).toBe(20)
    expect(payload.commission_value).toBe(5)
    expect(payload.hired_on).toBe('2026-09-20')
    expect(payload.deposit_amount).toBe(0)
    // 服务端管理字段（`SERVER_MANAGED_FIELDS`）一个都不许出现，出现即被后端拒（1000）。
    for (const key of ['password', 'is_superuser', 'is_staff', 'is_active']) {
      expect(payload).not.toHaveProperty(key)
    }
    // 「录的单不发短信」的字段已彻底删除（前后端 + 数据库列，D-0086）。
    expect(payload).not.toHaveProperty('no_sms_on_intake')
  })

  it('工作室勾选（D-0228）：DOM 勾上 ⇒ is_studio=true；不勾 ⇒ false；控件不存在 ⇒ false（默认档=打手）', () => {
    const withChecked = doc(
      '<div id="root"><input type="checkbox" name="isStudio" checked></div>',
    )
    const withUnchecked = doc(
      '<div id="root"><input type="checkbox" name="isStudio"></div>',
    )
    const without = doc('<div id="root"><input type="text" name="loginAcc"></div>')
    expect(readIsStudio(withChecked.getElementById('root')!)).toBe(true)
    expect(readIsStudio(withUnchecked.getElementById('root')!)).toBe(false)
    expect(readIsStudio(without.getElementById('root')!)).toBe(false)
    expect(
      (buildPayload(filledForm(), [1], '客服', true) as Record<string, unknown>).is_studio,
    ).toBe(true)
    expect(
      (buildPayload(filledForm(), [1], '客服') as Record<string, unknown>).is_studio,
    ).toBe(false)
  })

  it('业务类型：radio 读不到时兜底 **1（专做代练）**，与后端默认值同源', () => {
    const noRadio = filledForm({ bussType: '' })
    expect((buildPayload(noRadio, [1], '客服') as Record<string, unknown>).business_type).toBe(1)
    // 显式选了「代练+陪玩」（0）时**必须原样发 0**：默认值变了 ≠ 这一项被禁掉。
    expect(
      (buildPayload(filledForm({ bussType: '0' }), [1], '客服') as Record<string, unknown>).business_type,
    ).toBe(0)
  })

  it('空的可空数值键**整键不发**（不是发 null = 清空），有值的才发', () => {
    const payload = buildPayload(
      filledForm({ basicSalary: '', recPriceMin: '', recPriceMax: '', insureAmount: '' }),
      [1],
      '客服',
    ) as unknown as Record<string, unknown>
    for (const key of ['base_salary', 'grab_price_min', 'grab_price_max', 'deposit_amount']) {
      expect(payload).not.toHaveProperty(key)
    }
    const full = buildPayload(filledForm({ basicSalary: '3000' }), [1], '客服') as unknown as Record<
      string,
      unknown
    >
    expect(full.base_salary).toBe(3000)
  })

  it('字段映射表与这里列出的键**恰好相等**（多一个少一个都要红）', () => {
    // ⚠️ 标题里**不写条数**：这条断言本身就是「集合相等」，写死一个数字只会多一个
    //    需要同步维护的常量，而它一旦写错（本文件上一版就写成「22 条」而实际 23 条）
    //    会变成一句假话（E-0011 同族）。要看条数用 `Object.keys(PAYLOAD_MAP).length`。
    expect(Object.keys(PAYLOAD_MAP).sort()).toEqual(
      [
        'autoBrokerageType',
        'basicSalary',
        'brokerageAmount',
        'bussType',
        'inputDate',
        'insureAmount',
        'livestreaming',
        'loginAcc',
        'moblie',
        'qq',
        'recOrderMaxCount',
        'recOrderMaxCount2',
        'recPriceMax',
        'recPriceMin',
        'remark',
        'setAlipayAcc',
        'setAlipayAccName',
        'setBankCardNo',
        'setBankName',
        'sid',
        'tips',
        'userName',
        'userType',
      ].sort(),
    )
  })
})

describe('user-adduser：编辑回填', () => {
  it('按 `AccountRow` 回填到对标 name 的输入框（金额字符串 → 规范化数字串）', () => {
    const account: AccountRow = {
      id: 9,
      username: 'dashou01',
      display_name: '代练员一号',
      is_active: true,
      is_tenant_admin: false,
      role: '代练员',
      games: [2],
      last_login: null,
      date_joined: '',
      commission_value: '5.00',
      grab_limit_dl: 3,
      grab_limit_pw: 4,
      deposit_amount: '0.00',
      grab_price_min: '1.50',
      grab_price_max: '99999.00',
      hired_on: '2026-09-01',
      base_salary: '3000.00',
    }
    const d = doc(
      BUSS_MARKUP +
        gamesMarkup(
          [
            { id: 1, name: '原神', sort_order: 0, is_active: true, created_at: '' },
            { id: 2, name: '鸣潮', sort_order: 1, is_active: true, created_at: '' },
          ],
          [],
        ) +
        rolesMarkup(['客服', '代练员'], ''),
    )
    const root = d.getElementById('root') as HTMLElement
    // `fillForm` 只认 `input[name=…]`，先把证据件里的输入框补齐（真实页面由 HTML 提供）。
    root.insertAdjacentHTML(
      'beforeend',
      [
        'loginAcc',
        'userName',
        'sid',
        'brokerageAmount',
        'setAlipayAcc',
        'setAlipayAccName',
        'setBankName',
        'setBankCardNo',
        'recOrderMaxCount',
        'recOrderMaxCount2',
        'recPriceMin',
        'recPriceMax',
        'livestreaming',
        'qq',
        'moblie',
        'inputDate',
        'tips',
        'remark',
        'insureAmount',
        'basicSalary',
      ]
        .map((n) => `<input type="text" name="${n}">`)
        .join('') +
        '<input type="radio" name="userType" value="0">' +
        '<input type="radio" name="autoBrokerageType" value="20">',
    )
    fillForm(root, account)

    const value = (name: string): string => root.querySelector<HTMLInputElement>(`input[name="${name}"]`)!.value
    expect(value('loginAcc')).toBe('dashou01')
    expect(value('userName')).toBe('代练员一号')
    expect(value('inputDate')).toBe('2026-09-01')
    // `DecimalField` 回的是字符串（`5.00`）：直接塞进 `maxlength=6` 的输入框会被对标的
    // `lay-verify="number"` 判成非法，所以 `fillForm` 归一成 `5`。
    expect(value('brokerageAmount')).toBe('5')
    expect(value('insureAmount')).toBe('0')
    expect(value('recPriceMin')).toBe('1.5')
    expect(value('basicSalary')).toBe('3000')
    // 勾选状态：游戏按 id、职位按名字、radio 按值。
    expect(root.querySelector<HTMLInputElement>('input[name="games"][value="2"]')!.checked).toBe(true)
    expect(root.querySelector<HTMLInputElement>('input[name="games"][value="1"]')!.checked).toBe(false)
    expect(root.querySelector<HTMLInputElement>('input[name="userRole"][value="代练员"]')!.checked).toBe(true)
  })
})

/**
 * 复制凭据到剪贴板（2026-09-23 按对标复刻「点确定自动复制」时新增）。
 *
 * ⚠️ 这里有**一条有意偏离**：对标用 `ClipboardJS` 插件，我们**不引它**（本仓规矩：不擅自引运行时依赖），
 * 改用平台 API + `execCommand` 兜底 ⇒ 三条分支都必须有判据（否则静默失败就是「点了确定什么也没发生」）。
 */
describe('user-adduser：合作用户选项隐藏后的回填与提交（D-0246）', () => {
  it('编辑 member_type=1 的旧账号 ⇒ 回填命中**隐藏**的 option（不炸），提交时 member_type 原样保住 1', () => {
    const d = doc(
      BUSS_MARKUP +
        '<span style="display:none">' +
        '<input type="radio" name="userType" value="0" checked="">' +
        '<input type="radio" name="userType" value="1">' +
        '</span>',
    )
    const root = d.getElementById('root') as HTMLElement
    const account = {
      id: 9, username: 'co-op-01', display_name: '合作方甲', is_active: true,
      is_tenant_admin: false, role: '代练员', games: [], last_login: null,
      date_joined: '', commission_value: '0.00', grab_limit_dl: 0, grab_limit_pw: 0,
      deposit_amount: '0.00', grab_price_min: null, grab_price_max: null,
      hired_on: null, base_salary: null, member_type: 1,
    } as unknown as AccountRow
    expect(() => fillForm(root, account)).not.toThrow()
    const hiddenRadio = root.querySelector<HTMLInputElement>('input[name="userType"][value="1"]')!
    expect(hiddenRadio.checked).toBe(true)
    // 提交形态：表单读出的 userType='1' ⇒ 载荷 member_type=1（保存不把旧账号改成 0）。
    expect(buildPayload({ userType: '1' } as FormValues, [], '代练员').member_type).toBe(1)
  })
})

describe('user-adduser：复制凭据到剪贴板', () => {
  it('优先用 `navigator.clipboard.writeText`，成功回 `true`', async () => {
    const { writeText, restore } = stubClipboard()
    try {
      await expect(copyToClipboard('凭据串')).resolves.toBe(true)
      expect(writeText).toHaveBeenCalledWith('凭据串')
    } finally {
      restore()
    }
  })

  it('`navigator.clipboard` 被拒（非安全上下文 / 无权限）⇒ 落回 `execCommand("copy")`', async () => {
    const { restore } = stubClipboard(async () => {
      throw new Error('NotAllowedError')
    })
    const execCommand = vi.fn(() => true)
    // jsdom 既没有 `navigator.clipboard` 也没有 `document.execCommand`，兜底那条要自己装。
    ;(document as unknown as Record<string, unknown>).execCommand = execCommand
    try {
      await expect(copyToClipboard('凭据串')).resolves.toBe(true)
      expect(execCommand).toHaveBeenCalledWith('copy')
      // 兜底用的临时 `textarea` 必须被清掉（别在页面上留垃圾节点）。
      expect(document.querySelectorAll('textarea')).toHaveLength(0)
    } finally {
      Reflect.deleteProperty(document as unknown as Record<string, unknown>, 'execCommand')
      restore()
    }
  })

  it('两条路都失败 ⇒ 回 `false`（调用方据此提示「复制失败了」）', async () => {
    const { restore } = stubClipboard(async () => {
      throw new Error('NotAllowedError')
    })
    try {
      await expect(copyToClipboard('凭据串')).resolves.toBe(false)
    } finally {
      restore()
    }
  })
})

/**
 * 两条凭据文案的**纯函数**判据（期望值写字面值，见 E-0024：别拿同一个符号自指）。
 *
 * 出处都是 2026-09-23 在对标站实测抄回的原文（原文带对标租户标识 ⇒ 按 PII 守卫不入库，这里只写模板）：
 * - 弹窗正文（对标服务端 `data.msg`）：`新开子账户成功，该用户登录账号：<账号>，初始登录密码：<密码>`
 * - 进剪贴板（对标 `data.copyStr`）：`<租户公司名>管理系统网址：<域名> 你的登录账号：<账号> 初始登录密码：<密码>`
 *   ⇒ 我方**有意去掉**开头那截（业务方裁定），只留后两段。
 */
describe('user-adduser：凭据文案', () => {
  it('弹窗正文逐字等于对标那句（全角逗号 + 全角冒号）', () => {
    expect(credentialDialogText('newbie', 'Abc12345')).toBe(
      '新开子账户成功，该用户登录账号：newbie，初始登录密码：Abc12345',
    )
  })

  it('进剪贴板的是「你的登录账号：X 初始登录密码：Y」（半角空格分隔，照抄对标的对应两段）', () => {
    expect(credentialClipboardText('newbie', 'Abc12345')).toBe(
      '你的登录账号：newbie 初始登录密码：Abc12345',
    )
  })
})
