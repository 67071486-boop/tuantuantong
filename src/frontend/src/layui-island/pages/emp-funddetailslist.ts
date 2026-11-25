/**
 * 【团团通资金明细与提现】页的 **layui 复刻页**（对标物 `/Emp/funddetailslist`，**证据件渲染**）。
 * ⭐ **2026-10-30 改名（业务方指令，D-0339）**：原显示名「鸡蛋铺资金明细与提现」改
 * 「团团通资金明细与提现」——只动显示层（菜单文案 + 页面标题），对标 key / 路由 / 功能零改动；
 * 对标菜单名与 `<title>` 原文都是「鸡蛋铺资金明细与提现」⇒ 属**有意偏离**，登记见本文件
 * `PAGE_TITLE` 注、`config/replicaRoutes.ts` 该条 note 与 `config/menu.ts` 该行注释。
 *
 * 形状：**单表列表页**（引用块「去提现」+ 4 个筛选项 + 1 张 7 列表 + `#total` 统计行）。
 * 证据：
 *  - 标记与页内两段 `<style>` → `public/replica/emp-funddetailslist.html`
 *    （源 `.tools/recon/suqing1/html/Emp__funddetailslist.html` **L24-41 + L52-125 + L126-189**，
 *    逐字；**只删了 L174 的 `#jdpDesc` 隐藏域**，见下）
 *  - 列定义 → `.tools/recon/suqing1/assets/JS2019/Emp/funddetailslistmang.js` **L48-74**
 *  - `table.render` 参数 → 同 JS **L42-88**；`#total` 模板 → **L85**
 *  - 工具栏模板 `#toolbarDemo`（含 `#total` 槽）→ HTML **L181-186**
 *
 * ## 逐项取值（全部取自 `funddetailslistmang.js`）
 *
 * | 项 | 值 | 出处 |
 * |---|---|---|
 * | `toolbar` | `'#toolbarDemo'` | L46 |
 * | `defaultToolbar` | `['filter','exports']` | L45 `[]` 被 L47 `['filter','exports']` 覆盖（后写生效） |
 * | `page` | `{ limit: 20, limits: [20,50,100,200,300,500] }` | **L75-77**（六项且**不是**从 10 起，逐字） |
 * | 列宽 | 200/200/60/80/80/140/**自动** | L50/52/57/63/66/69/72 |
 * | `Remark` 列 | **无 `width`**（自动列，实测渲染 883px） | L72 |
 *
 * `isPc`（L13-16）在本页**只被 `.pay` 弹窗的 `area` 用到**（L148），本页 cols 里**没有任何三元**；
 * 而 `.pay` 弹窗是**死代码**（HTML 里不存在 `.pay` 元素，`grep -n 'pay' html/funddetailslist.html` 无匹配），
 * 所以 `isPc` 对本页渲染**没有任何影响**。
 *
 * ## PII / 凭据处置（本页唯一需要清空的一页）
 *
 * - 🚫 **`<input type="hidden" id="jdpDesc" value="…">`（HTML L174）已从证据件删除**：
 *   那是对标服务端下发的 **Base64 充值令牌（凭据）**，既不是标记也不是配置枚举；草稿 `dropped` 也已登记。
 *   后果：`.pay` 弹窗（本来就不可达）的 `content` 拼不出令牌；我们也不发任何写请求。
 * - **`#amtMsg`（余额文案容器）只复刻文案骨架、值一律用 `—`**：对标由 `getJdpBalance()`（L103-121）POST
 *   `/EmpAjax/GetBalanceAndFreeAmount` 后写入
 *   `当前系统绑定的鸡蛋铺官方账户为…，该账户当前可用余额…元，当前冻结余额…元` —— 那是**租户财务数据**
 *   （账号 + 可用/冻结余额），我们**不取、不发请求**。按本任务 PII 口径「真实值一律清空、可见空槽用 `—`」，
 *   这里保留**一句话的模板**（`billsummary.js` L115 的字符串骨架逐字）而把三处值换成 `—` —— 这样
 *   `.layui-elem-quote` 那一块仍是**一行高**，下面所有块的 y 与对标一致（空着会少 24px、整页上移）。
 *   实测对标此刻渲染的是「…账户为undefined，…余额0元，…冻结余额0元」（他们接口没返回），
 *   **那两个 0 与 `undefined` 都不抄**——它们是接口数据的字符串化产物，不是模板常量。
 * - `#sDate`/`#eDate`/`#key` 的服务端初值是静态日期与空串（非 PII）；表格为服务端空表，无数据值。
 *
 * ## `#total` 统计行（**有残余结构差异，已判定为数据派生**）
 *
 * 对标 `done`（**L85**）写 `'【统计】共' + res.count + '条记录，共入账' + res.Insum + '元，共出账' +
 * res.Outsum + '元'`，落点是 `#toolbarDemo` 模板里的 `<div style="color:#ff6a00" id="total">`（HTML L185）。
 * 我们无接口 ⇒ 用空数据 **0**（不编造、也**不抄**对标的真实金额）。
 * ⚠️ 对标该租户的数据是活的：本轮实测同一路由两次分别渲染出
 * `【统计】共13条记录，共入账1000.00元，共出账933.00元` 与 `【统计】共0条记录，共入账null元，共出账0元`
 * —— 后者的 `null` 是接口字段为空时的 JS 字符串化产物。`parity` 的 `#total` 判据会先抹掉数字再比模板，
 * 因此当对标处于「有金额」或「null」态时，本页会留下 **1 条 data 派生的 `struct`**
 * （`#total 文案模板`），且它随对标数据在变、不是我们这侧的渲染缺陷。**这是已知且唯一的非数据差异。**
 *
 * ## 弹窗
 *
 * 两个弹窗都在 JS 里：A「去提现」= `layer.alert`（**活的**，L153-160，正文会把租户账号 `currentAcc`
 * 渲染进去 —— 我们无该数据，按全局口径点它只提示「「去提现」尚未开放」）；B「鸡蛋铺余额充值」=
 * `layer.open`（**死代码**，HTML 无 `.pay` 元素）。均不建弹窗 DOM。
 */
import { artifactPage, type ArtifactCol } from '@/layui/artifactPage'
import { bootstrapIsland, type IslandContext, type IslandPage } from '@/layui/island'

/** 逐条照抄 `funddetailslistmang.js` L48-74（`unresize:false`/`sort:false` 是 layui 默认值，故不写）。 */
export const COLS: ArtifactCol[] = [
  { field: 'PayOrder_ID', title: '支付流水号', width: 200, align: 'center' },
  { field: 'OrderID', title: '相关订单号', width: 200, align: 'center' },
  { field: 'FundsType', title: '资金方向', width: 60, align: 'center' },
  { field: 'Amount', title: '金额', width: 80, align: 'center' },
  // ⚠️ `Banlance` 是对标接口契约里的原文拼写（少了 `a`），照抄不改（L66）。
  { field: 'Banlance', title: '可用余额', width: 80, align: 'center' },
  { field: 'CreateTime', title: '时间', width: 140, align: 'center' },
  // L72：无 `width`（自动列，实测渲染 883px = 表宽减去所有定宽列）。
  { field: 'Remark', title: '备注说明', align: 'center' },
]

/** `table.render` 的参数（供自检/测试断言；逐条取自 `funddetailslistmang.js`，见上）。 */
export const TABLE: {
  elem: string
  toolbar: string
  defaultToolbar: string[]
  page: { limit: number; limits: number[] }
} = {
  elem: '#tableList',
  toolbar: '#toolbarDemo',
  // L45 `[]` → L47 `['filter','exports']`（后写生效；右上角是「筛选列 + 导出」两个图标）。
  defaultToolbar: ['filter', 'exports'],
  // L75-77：`page:true, limit:20, limits:[20,50,100,200,300,500]`（**六项、从 20 起**，逐字）。
  page: { limit: 20, limits: [20, 50, 100, 200, 300, 500] },
}

const base = artifactPage({
  url: 'replica/emp-funddetailslist.html',
  tables: [{ ...TABLE, cols: COLS }],
})

/**
 * 页面标题（iframe 的 `document.title`）。
 *
 * ⭐ **有意偏离（2026-10-30，业务方指令改名，D-0339）**：对标服务端渲染的
 * `<title>` 原文是「鸡蛋铺资金明细与提现」（`Emp__funddetailslist.html` L5）；业务方指令我方
 * 显示名改「团团通资金明细与提现」（菜单文案 + 页面标题，只动显示层）⇒ 本页的
 * `document.title` 随我方名字走，**对标原文留在本注与 `config/replicaRoutes.ts` 该条
 * `benchmarkTitle` 备查**。与 T7（`order-coreordercenter` 改名「第三方平台订单管理」）同族先例
 * ——差别只在 T7 的页内 `document.title` 保留了对标原文，本页按本次指令**连页面标题一起改**。
 */
export const PAGE_TITLE = '团团通资金明细与提现'

export default {
  mount: async (root: HTMLElement, ctx: IslandContext) => {
    // 原注释「对标 <title> 是服务端渲染的」指的是取值来源；2026-10-30 起按业务方指令显示
    // 我方名（有意偏离，见上面 PAGE_TITLE 的登记注）。
    document.title = PAGE_TITLE
    const unmount = await base.mount(root, ctx)
    await bootstrapIsland(['form'])

    // `#total`：逐字照抄 `funddetailslistmang.js` L85 的模板，数字用我们的空数据（0）。
    // 残余的 `struct`（对标金额位是小数/`null`）见文件头「`#total` 统计行」—— 数据派生，不是渲染缺陷。
    const total = root.querySelector<HTMLElement>('#total')
    if (total) total.innerHTML = '【统计】共0条记录，共入账0元，共出账0元'

    // `#amtMsg`：只复刻一句文案骨架，三处值一律 `—`（租户账号与余额，我们不取；见文件头 PII 段）。
    // 保留这一行是为了让 `.layui-elem-quote` 的高度与对标一致（空着整页会上移 24px）。
    const amtMsg = root.querySelector<HTMLElement>('#amtMsg')
    if (amtMsg) {
      amtMsg.innerHTML =
        '当前系统绑定的鸡蛋铺官方账户为—，该账户当前可用余额—元，当前冻结余额—元'
    }
    return unmount
  },
} satisfies IslandPage
