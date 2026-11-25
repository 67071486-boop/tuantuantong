/**
 * 【我的资料】页的 layui island 复刻（对标 `/User/myinfo`，对标 `<title>我的资料</title>`）。
 *
 * 形状：**0 张表**，纯表单页——3 个页签（基本资料 / 功能设置 / 修改密码）+ 页内隐藏块
 * （实名认证、隐私协议、扫码与 3 个隐藏域）。
 *
 * 三条必须照做的关键值（从对标取，不是 layui 默认）：页内 `<style>` 的 `.admin-info`
 * 标签列宽 **150**（不是 layui 默认 110/80，少这一条整页列宽与输入框起点全错）；
 * 字段 name 的拼写 **`moblie`**（对标的拼写错误）——照抄，别「修」；
 * 「功能设置」页签按**对标线上实测**复刻（模板里那份带 `layui-hide` 的绑定表单留在标记里当对照）。
 *
 * ⚠️ PII 已清空：`loginAcc` 的 value（手机号）、`#company`（租户名）→ 空；隐私协议正文里的租户名 → `—`。
 *
 * ⚠️ 头像 / 收款码上传：**10 MB 闸 + 客户端压缩**（头像既有四档；收款码 `max(四档, 0.8)`——
 * 收款码是给人扫的，压狠了扫不出来）；票据 `ext` 用**压缩后**的形态（⛔ 不拿原 `png` 签 JPEG 字节）。
 *
 * ⚠️ **加载完成前不许编辑**：取数之前把控件置 `disabled`，回填成功才解锁、失败保持锁定
 * （异步取数窗口里打进去的字会被服务端旧值覆盖，再保存就是把旧值/空串写回去、页面还报成功）。
 * ⛔ 不要为了「页面一到就能打字」把这道锁去掉——那是假快。
 *
 * ⚠️ 提交不走 `form.on('submit(savebtn)')`：那是 layui-form 的提交链路、会对整页 `validate()`，
 * 而 `loginAcc` 带必填校验且被有意置只读 ⇒ 走**捕获态 click 委托**。
 *
 * ## 已知会不同（如实登记）
 *
 * | # | 差异 | 理由 |
 * |---|---|---|
 * | 3 | **「微信收款码」那一格（`已上传` / `重新上传` / 点开看那张图）不逐字复刻证据件**：状态切换与开层用**本页自己的写法**（层只给 `type` / `title` / `shadeClose` / `area` / `content`，⛔ 不抄对标的 `skin:'layui-layer-lan'` / `anim:1`）；层内 DOM 也是本页自拼的一个 `<img>`。**文案逐字**照对标 i18n：`已上传`（证据件 L259 原文）/ `重新上传`（`myinfo.js` L357）/ `上传收款码`（L315）/ 层标题 `微信收款码`（L378）。⚠️ **另有一条我方新增的健壮性**：链接是 **300s 短时签名**（对标把 URL 塞在隐藏域里、从不过期）⇒ 图 `onerror` 时**重取一次**资料换新链接再试一次，仍失败就关层 + 一句固定提示（对标没有这一支）。<br>⛔ **没有为了这块改动证据件的任何标记**：`已上传` 那颗（`.lookWxImg`）与隐藏域 `#qrCode1` 证据件里本来就有。 | **业务方明确要求：这块不逐字复刻，按行为对齐**（原话「这里用我们自己的前端语言吧，不用完全复刻用 layui 了」）；同时要求「上传后要在页面上看得出来」，所以状态呈现与短时链接换新都按**行为**对齐、不按标记逐字抄。<br>对标出处：`myinfo.js` L617-618（进页面时按 `#qrCode1` 非空就 show + 改文案）、L973-974（上传成功后同样两下）、L1244-1253（点 `.lookWxImg` 开层） |
 * | 4 | ⭐ **「功能设置」页签里多了一整块「下家鸡蛋铺绑定」表单**（鸡蛋铺地址 / 账号 / 密码 / 订单来源名 / 大厅抛单百分比 / 启用开关 + 「保存」「测试连接」+ 健康状态），见文件末尾那一节 | **自有新增功能**（业务方指定这里是**唯一入口**）：代练员在本页绑**他自己那家**下家鸡蛋铺。对标那一格只有一条只读红字（「请回到您自己的管理系统里操作绑定」）——那是**租户级**上家绑定的口径，与本线的**个人级**下家绑定是两件事。⚠️ 这块的控件**取数回来之前也是 `disabled`**（与第 2 条同一口径：晚回填会覆盖用户已经打进去的字）。⛔ 证据件一个字没动，块是**运行期追加**的 |
 * | 5 | 「功能设置」页签里**没有**「上家鸡蛋铺绑定」表单——那一整块已迁往「系统维护 → 绑定上家系统设置」页，随后被业务方裁定整块删除（见该页文件头「收口登记」与 `D-0189`） | **入口迁移**（业务方裁定）：上家绑定的入口与「上家系统」这个主题同页，用户不必再绕到个人资料里找。<br>⛔ 与第 4 条（**下家**）是两条零关系的产品线，两块互不相干。<br>⛔ 本页**已无一字**属于上家那一块：标记 / 字段名 / 元素 id / 出口 import / 三支动作与它们的取数**全部随块搬走**（判据 = 本文件对上家那一套前缀**零命中**）。<br>⚠️ **权限后果**（迁移的已知代价，如实登记）：那一块现在只在一个要「绑定上家系统」那个权限节点才打得开的页面上 ⇒ 没这个节点的普通客服**不再能**自助绑上家鸡蛋铺账号 |
 * | 6 | ⭐ **「功能设置」页签里那条只读红字（「请回到您自己的管理系统里操作绑定…」）在运行期被删掉**（`removeReadonlyBindingNote`，只删那一格自己的 `blockquote`） | **业务方裁定**（spec `2026-10-08-jidanpu-official-api-binding-design.md` §10）：那句是**租户级上家绑定**的指引，与我们在同一格做的**个人级下家绑定**（第 4 条）口径不同，留在页面上会让人以为「这事得回自己系统里做」。<br>⛔ **证据件一个字未动**：`public/replica/user-myinfo.html` 是对标渲染结果的留档，改它等于伪造证据 ⇒ 只动**运行期 DOM**（与 `system-homesystemsetting.ts` 删弹窗里那条「注意」同款）。<br>⚠️ **代价**：`parity` 对拍会看到一处 `struct` 差，属**预期差异**（已登记 `docs/FRONTEND-REPLICA-STANDARD.md`）；页测按「**运行期没有、证据件里有**」两半分别钉（`__tests__/user-myinfo.spec.ts` ㉓ / ㊴ / ㊵、`__tests__/user-replica.spec.ts`）。 |
 * | 7 | ⭐ **「下家鸡蛋铺绑定」块里多了一条「对接方式」轴**（②式 = 鸡蛋铺官方 Open Platform API）：常显的「对接方式」下拉（默认①式 `账号 + 密码`）＋ ②式才出现的 `client_id` / `client_secret` / **回调地址**（只读 + 复制）三格 ＋ 「测试连接」结果区里那张**逐条权限表**；健康那一段另加一格（后端算好的 `consumer_state` 那句话） | **业务方指派**（同一份 spec；「绑定上家系统设置」页已按 §11 落地过一遍，本页是把**同一套形状**搬到**下家**这一页）。<br>形状**照抄上游那一份**（`system-homesystemsetting.ts`——⛔ 只读它、一个字没改它），⛔ 不另设计一套：两条线是两张零关系的表，但**界面口径必须一致**。<br>⚠️ **能红**：切方式那两条（`__tests__/user-myinfo.spec.ts` ㊷ / ㊿ / 51）、回调地址三态（㊹–㊻）、逐条表的「没探 ≠ 不可用」（㊼）、密钥哨兵（㊸）。<br>⭐ **真机修正两处**（业务方**真机实测**报的「选了②式之后保存不了」）：① **②式保存的载荷不许被补成 `mode:'password'`**（补了后端就按①式判、回一句「鸡蛋铺账号不能为空」，而②式那一格在界面上是隐藏的空串）—— 判据 = ㊾ 那句 `expect(body.mode).toBe('openapi')`（⚠️ 它原先 `delete body.mode` 之后才比，**恰好把这一格漏掉了**）；② 「账号」那一格的 `lay-verify` 随分支摘 / 装 —— 判据 = 55，⚠️ 它**惰性、不构成阻挡**（本页不跑 layui 的 `validate()`，见 `MEMBER_ACCOUNT_VERIFY` 的注释）。 |
 * | 8 | ⭐ **「下家鸡蛋铺绑定」块里多了一枚「同步大厅订单」开关（`lay-skin="switch"`，与「启用」同形；排在「大厅抛单百分比」之后、「启用」之前）＋ 块的布局从「左引导图 + 右表单」的两栏 flex 改为上下堆叠、全部左对齐** | **D-0299（业务方拍板）**：开关默认**开**＝存量行为不变；关 ⇒ 这份绑定**不再抛单进对方的抢单大厅**（`member.launch` 一档），指派录单 / 状态回传 / 退单 / 改价 / 截图补传等**其他一切功能照旧**；已挂对方大厅的存量单**不主动撤回**。布局两栏改堆叠是业务方报「布局有问题」的改准。<br>⚠️ 开关与载荷字段都取**后端那一格**（`hall_sync_enabled`，GET 回显 / PUT 可写）；能红的用例：`__tests__/user-myinfo.spec.ts` 的开关渲染 / 回填 / 载荷三条 ＋ 布局那条（㊷-乙 改准版）。 |
 */
import { fetchAllGames, type ConfigRow } from '@/api/config'
import { BizError } from '@/api/errors'
import {
  fetchMemberJidanpuBinding, saveMemberJidanpuBinding, testMemberJidanpuConnection,
  type JidanpuEndpointCheck, type JidanpuMemberBindingPayload, type JidanpuMemberBindingRow,
  type JidanpuSourceOption,
} from '@/api/jidanpuMember'
import {
  changePassword, claimMedia, fetchMyProfile, patchMyProfile, requestMediaTicket,
  type MediaKind, type MyProfile, type MyProfilePatch,
} from '@/api/myProfile'
import { verifyRealName } from '@/api/realname'
import { avatarDataUri } from '@/config/avatar'
import { ErrorCode } from '@/config/errorCodes'
import { fetchArtifactHtml } from '@/layui/artifactPage'
import {
  bootstrapIsland, initIslandElements, islandMsg,
  type IslandContext, type IslandPage,
} from '@/layui/island'
import { putFileToStorage } from '@/layui-island/directUpload'
import { escapeHtml } from '@/layui-island/escape'
import {
  MAX_USER_MEDIA_BYTES, compressImage, mediaQuality, userMediaTicketExt,
} from '@/layui-island/imageCompress'
import { AVATAR_UPDATED_MESSAGE_TYPE } from '@/layui-island/messageTypes'

/** 证据件（对标 `/User/myinfo` 的页内样式段 + 处理过的 body 标记）。 */
export const ARTIFACT_URL = 'replica/user-myinfo.html'
export const PAGE_TITLE = '我的资料'
/** 未实现控件的统一提示（D-0025 §5：照对标渲染 + 点了提示「尚未开放」，不置灰）。 */
export const NOT_OPEN = '尚未开放'

/** 7 个可编辑字段 → 证据件里的 `name` 选择器。⚠️ `moblie` 是**照抄对标的拼写**（L301）。 */
export const EDITABLE_SELECTORS: Record<keyof MyProfilePatch, string> = {
  alipay_account: 'input[name="setAlipayAcc"]',
  alipay_name: 'input[name="setAlipayAccName"]',
  bank_name: 'input[name="setBankName"]',
  bank_card_no: 'input[name="setBankCardNo"]',
  qq: 'input[name="qq"]',
  mobile: 'input[name="moblie"]',
  live_url: 'input[name="livestreaming"]',
}

/** 只读字段（回填但不可改）。`loginAcc` 在证据件里**可编辑**，这里显式置灰：
 *  它是登录名，自助改会牵动会话与唯一约束（spec §3-D2 的**有意偏离**）。 */
const LOGIN_ACC = 'input[name="loginAcc"]'
const SID = 'input[name="sid"]'
const BASIC_SALARY = 'input[name="basicSalary"]'

// ⚠️ E-0061 陷阱①：类名/属性值与「选择器」分成两个常量，别写成 `lay-filter=".x"`。
const SAVE_BUTTON = 'button[lay-filter="savebtn"]'
const SAVE_PWD_BUTTON = 'button[lay-filter="savePwdBtn"]'
const PWD_INPUTS = ['input[name="ypwd"]', 'input[name="newPwd"]', 'input[name="renewPwd"]'] as const
const AVATAR = '#avatar'
const PWD_TAB_ID = 'pwdSet'
/** 「尚未开放」白名单（spec §6.5）。⛔ 页签不在其中——页签切换必须照旧工作。 */
const NOT_OPEN_SELECTORS = [
  // ⚠️ `#upimgBtn` 与 `.uploadWx` 已接真上传、`.toCer` / `.startCertBtn` 已接真核验流程，
  //    都**不在**这一组——留在里面会「另一处打真端点、这里却弹『尚未开放』」（自相矛盾）。
  '.bindEmileBtn', '.changeEmileBtn',
] as const

/* ───────────────── 实名认证 ─────────────────
 *
 * 「我的资料」页上那两颗按钮（`.toCer` / `.startCertBtn`）从「尚未开放」变成**真弹层**：
 * 填姓名 + 身份证号 → 提交 `POST /api/auth/realname/verify/` → 显示结果。
 * 证据件里那一整块隐藏的 `.rnameCertDiv`（对标自己的「填姓名 + 身份证号 + 开始认证」表单）
 * 的内容**搬进真弹层**（⛔ 证据件一个字不动）。层里输入框仍带 `certName` / `certSid`
 * （保持与证据件形状一致），提交时才映射成载荷字段 `name` / `id_card_no`。
 */
/** 「点击进行实名认证」那颗（证据件 L198）。 */
export const TO_CER_SELECTOR = '.toCer'
/**
 * 上面那颗的**容器**类名。
 * ⚠️ `layui-hide` 挂在这个容器上、不挂在 `.toCer` 上——要切换显示就得改容器那一层
 * （只删 `.toCer` 自己的类**无效**）。⚠️ 定位一律**从 `.toCer` 往上找**：
 * `.layui-form-mid` 在全页不止一个，不能按它抓。
 */
export const TO_CER_CONTAINER_CLASS = 'layui-form-mid'
/** 「开始认证」那颗（证据件 L434，在隐藏块 `.rnameCertDiv` 里）。 */
export const START_CERT_SELECTOR = '.startCertBtn'

/**
 * 提交前的计费提示。**一字不许省**：厂商按「查得」计费——姓名与号码对不上也照样扣 1 次。
 * 不写这一句，用户会以为「失败不扣」、反复提交试错把租户的次数耗光。
 */
export const REALNAME_BILLING_NOTICE = '本次核验将消耗 1 次，无论核验结果一致与否'

/**
 * 弹层里两块**没填全**时的提示（我方自拟）。与后端的格式错误文案**分开发话**：
 * 用户才知道自己是少填了还是填错了。
 */
export const REALNAME_MISSING_INPUT_TEXT = '请填写真实姓名与身份证号后再提交'

/** 弹层标题（证据件里那一块没有标题 ⇒ 我方自拟的层标题）。 */
export const REALNAME_LAYER_TITLE = '实名认证'

/** 层里两个输入框的 id（层内元素，⛔ 与证据件里的 `#certName` / `#certSid` 不是同一个节点）。 */
export const REALNAME_NAME_INPUT_ID = 'rncCertName'
export const REALNAME_SID_INPUT_ID = 'rncCertSid'
/** 层里「开始认证」那颗的 id（同上）。 */
export const REALNAME_SUBMIT_BUTTON_ID = 'rncStartCert'
/**
 * 层里那行**结果**的 id（成功/失败都写在这里）。⛔ 不用 1500ms 就消失的浮层：
 * 核验要花钱，结果必须留在屏幕上让人核对。
 */
export const REALNAME_RESULT_ID = 'rncCertResult'

/** 「已通过实名认证」那行结果文案的**复用一个**（与 `renderRealName` 的 `true` 态逐字相同）。 */
export const REALNAME_PASS_TEXT = '已通过实名认证'

/**
 * 核验**没通过**时的提示（我方自拟）。⚠️ 必须与「成功」分得开：
 * `fail` 这一档后端**也照样扣了 1 次**（见 `REALNAME_BILLING_NOTICE`）。
 */
export const REALNAME_FAIL_TEXT = '实名认证未通过，请核对姓名与身份证号是否一致'

/** 后端那句「号码本地就不合法」（`services.RealNameInvalidInput`，业务码 `1000`）的**兜底**文案。 */
export const REALNAME_INVALID_TEXT = '身份证号格式不正确，请核对后重新提交'

/**
 * 业务码 → 人话（⛔ 不用一句「核验失败」糊过去）。`1025` 找管理员、`1026` 找客服购买、
 * `1027` 平台侧无包请联系管理员、`1029` 稍后再试（不锁定，窗口滚过自动恢复，D-0251）、
 * `1000` 用户核对后重交；`1017` = 上游故障**可重试**——⛔ 不能说「你没通过」
 * （那会把一次上游故障说成用户的错）。`1017` 这一支今天是兜底（实名路径把厂商失败翻成
 * `result: "fail"`，走不到 1017）。其余业务码**原样显示后端那句人话**（⛔ 不合成，
 * 那会把「该找谁」抹掉）。
 */
export function realNameErrorText(e: unknown): string {
  if (e instanceof BizError) {
    switch (e.code) {
      case ErrorCode.REALNAME_DISABLED:
        return '本系统尚未启用实名认证功能，请联系管理员开启'
      case ErrorCode.REALNAME_QUOTA_EXHAUSTED:
        // ⚠️ 逐字带上「余量不足」与「请联系客服购买」：业务方与 spec §14-P1 的口径。
        return '实名认证余量不足，请联系客服购买'
      case ErrorCode.REALNAME_NO_PACKAGE:
        return '平台侧暂无可用的实名认证密钥包，请联系管理员补充'
      case ErrorCode.REALNAME_RATE_LIMITED:
        // D-0251：同一用户提交过于频繁（默认 5 次/小时）。
        // ⚠️ 不锁定、窗口滚过自动恢复 ⇒ 文案只说「稍后再试」，不说「被拉黑」。
        return '尝试过于频繁，请稍后再试'
      case 1017:
        return '实名认证服务暂时不可用（上游网络异常），请稍后重试'
      case ErrorCode.VALIDATION:
        // 后端 `RealNameInvalidInput` 的原文（`services.py`）；拿不到就用同一句兜底。
        return e.message || REALNAME_INVALID_TEXT
      default:
        // 其余业务码**原样显示后端那句人话**（⛔ 不合成一句"核验失败"：
        // 那会把"该找谁"这个信息抹掉 —— 本页 `saveProfile` 的既有口径同款）。
        return e.message
    }
  }
  return errorText(e)
}

/** 两个已接真上传的控件（与 `api/myProfile.ts` 的 `MediaKind` 对应）。 */
const WECHAT_QR_UPLOAD = '.uploadWx'
/** 「已上传」那颗（证据件 L259：**出厂就带 `display:none`**，上传过才显示）。 */
const WECHAT_QR_LOOK = '.lookWxImg'
/**
 * 「微信收款码」那一格的四条文案（逐字取自对标 i18n）。⚠️ 对标的 `uploadWx` 有**两处**取值：
 * 出厂文案「上传收款码」、上传成功后「重新上传」——与下面两个常量一一对应。
 */
const WECHAT_QR_IDLE_TEXT = '上传收款码'
const WECHAT_QR_REUPLOAD_TEXT = '重新上传'
const WECHAT_QR_LAYER_TITLE = '微信收款码'
/** 层里那张图的 id（对标 `myinfo.js` L1252 的 `#WxImg`）。 */
const WECHAT_QR_IMG_ID = 'WxImg'
/**
 * 收款码链接的落点 = 证据件里本来就有的隐藏域 `#qrCode1`（对标自己也存这里）
 * ⇒ ⛔ 不新增标记、也不塞进 `dataset`。
 */
const WECHAT_QR_URL_INPUT = '#qrCode1'
/**
 * 短时签名链接**换新之后仍打不开**时那句**固定**提示。
 * ⛔ 不把服务端的错误串 / 技术细节（`Error`、状态码）给用户看——那对用户没有意义。
 */
const WECHAT_QR_FAILED_TEXT = '收款码图片加载失败，请稍后重试'

const MEDIA_SELECTORS: ReadonlyArray<readonly [string, MediaKind]> = [
  ['#upimgBtn', 'avatar'],
  [WECHAT_QR_UPLOAD, 'wechat-qr'],
] as const

/** 头像在资料页的尺寸：证据件 `#avatar` 的 `max-width:240px`（spec §6.2）。 */
export const PROFILE_AVATAR_SIZE = 240

// ══ 「下家鸡蛋铺绑定」那一块 ═════════════════════════════════════
//
// 业务方指定：代练员绑**他自己那家**下家鸡蛋铺的**唯一入口**就是本页的「功能设置」页签。
// ⚠️ 块是**运行期追加**的：⛔ 不改证据件（那是「对标渲染结果」的留档，改它等于伪造证据）。
// 视觉值取页内已有的那一份（`.admin-info` 对照块的形状），不凭空造。
// ⚠️ 这一块里还有一条**「对接方式」**轴（①式 账号 + 密码 / ②式 官方开放平台 API）——
// 同一张表、同一个端点，只是**换一套凭据**。⛔ 不往地址 / 账号那几格里塞（第二个维度）。
// 形状照「绑定上家系统设置」页已落地的那一套；两份实现各自存在、⛔ 不互相 import
// （两条零关系的产品线）。

/** 块自己的类名（⚠️ **不带点**）。 */
export const BINDING_PANEL_CLASS = 'jidanpuMemberBinding'
/**
 * 找块的**选择器**（⚠️ E-0061 陷阱①：类名与选择器分成两个常量——写成
 * `class="${BINDING_PANEL_SELECTOR}"` 会造出 `class=".x"`、永远匹配不到（静默失败））。
 */
export const BINDING_PANEL_SELECTOR = `.${BINDING_PANEL_CLASS}`

/**
 * 五个输入框 + 开关的 `name` 属性值。
 * ⚠️ 与 `BINDING_SELECTORS` **分成两份常量**：HTML 要的是**值**，选择器要的是**带属性的串**——混用就是陷阱①。
 */
export const MEMBER_FIELD_NAMES = {
  baseUrl: 'member_base_url',
  account: 'member_account',
  password: 'member_password',
  sourceName: 'member_source_name',
  throwPercent: 'member_throw_percent',
  // ⭐ D-0299：「同步大厅订单」开关——只闸「抛大厅」，其余功能照旧。
  hallSync: 'member_hall_sync',
  enabled: 'member_enabled',
} as const

/**
 * ①式「账号」那一格的 `lay-verify` 取值（②式下**摘掉**，见 `applyMemberMode`）。
 * ⚠️ **这一条是防御性的、⛔ 不是承重的**：本页没有任何路径会跑 layui 的 `validate()`
 * ⇒ 摘不摘今天行为一样；它的用途是**让语义显式**（②式下那一格是被隐藏的**空串**，
 * 留着 `required` 等于声明一件不成立的事）。
 * ⚠️ 另外三格口径各自不同、⛔ 不要「一起对齐」：「密码」不装必填（空串 = 不改是正当用法）；
 * `client_id` / `client_secret` 的必填**不能**交给 layui（只在②式且从没存过时必填，
 * layui 的 `required` 表达不了）⇒ 由 `saveMemberBinding()` 的**早挡**负责。
 */
export const MEMBER_ACCOUNT_VERIFY = 'required'

export const MEMBER_SAVE_BUTTON_ID = 'memberBindingSaveBtn'
export const MEMBER_TEST_BUTTON_ID = 'memberBindingTestBtn'
export const MEMBER_HEALTH_ID = 'memberBindingHealth'
/**
 * 「订单来源名」下拉与提示位的 `id`（**契约锚点**：测试与排障按它们判，⛔ 不按文案判）。
 * ⚠️ 与其它页面的同名常量是**各自文件里的字面量**（两条线零关系，⛔ 别互相 import）；
 * `id` 值刻意不同名，免得两个页面同处一个 DOM 时撞车。
 */
export const MEMBER_SOURCE_SELECT_ID = 'memberSourceName'
export const MEMBER_SOURCE_HINT_ID = 'memberSourceHint'

/* ══════════ 「一步步开通」向导 ══════════
 *
 * 新用户不知道「先建账号、勾哪几条权限、先加一个订单来源」，照直觉填完测试连接会红。
 * 这里放一块**可折叠的静态说明**：⚠️ 它**不是流程闸**（折叠着、点开才看，不挡老用户）；
 * ⚠️ 顺序是**承重的**：新账号「还没保存就点测试连接」会被后端直接拒掉
 * ⇒ 向导必须写 **先保存 → 再测试连接 → 再选来源 → 再保存**。
 */

/** 向导块的容器 `id`（折叠块自己；测试与排障按它找，⛔ 不按文案找）。 */
export const MEMBER_GUIDE_ID = 'memberBindingGuide'

/**
 * 静态资源前缀。`public/` 下的文件随部署基址变（可能挂在 `/console/` 这种子路径），
 * **不能**写死 `/guide/...`——写死之后换基址就静默 404（图上是空白，页面不报错）。
 */
const ASSET_BASE = import.meta.env.BASE_URL || '/'

/** 两张示意图（业务方提供，已进 `src/frontend/public/guide/`）。 */
export const MEMBER_GUIDE_IMAGES = [
  {
    src: `${ASSET_BASE}guide/jidanpu-perm-system-log.png`,
    alt: '鸡蛋铺「角色职位权限 → 修改权限」里「系统操作日志」的勾选位置',
    caption: '① 系统维护 → 角色职位权限 → 修改权限 → 勾上「系统操作日志」',
  },
  {
    src: `${ASSET_BASE}guide/jidanpu-source-add.png`,
    alt: '鸡蛋铺「订单来源管理 → 新增来源」：名称与描述填「团团通系统」、平台选「其它平台」',
    caption: '② 系统维护 → 订单来源管理 → 新增一个来源（名称/描述「团团通系统」、平台「其它平台」）',
  },
] as const

/* ══════════ 「对接方式」（②式官方开放平台 API） ══════════
 *
 * ⭐ 是**第二个维度**（同一个端点、同一行记录，只是换一套凭据）——
 * ⛔ 不往既有字段里塞（会污染字段语义）；两条轴彼此独立、⛔ 不许合并成一个常量。
 * ⚠️ 与「绑定上家系统设置」页那一套是两份各自存在的常量（⛔ 别互相 import）；
 * 字段名 / `id` 值刻意不同名，免得两个页面同处一个 DOM 时撞车。
 */
/** 「对接方式」下拉的 `name`。 */
export const MEMBER_MODE_FIELD_NAME = 'member_mode'
/** 下拉的 `lay-filter`（layui 的切换事件名是 `select(<lay-filter>)`）；与 name 分成两个常量。 */
export const MEMBER_MODE_SELECT_ID = 'memberBindingMode'
/** 「对接方式」那一格的容器 `id`（切可见性靠它）。 */
export const MEMBER_MODE_ITEM_ID = 'memberModeItem'
/** ①式（登录式）：地址 + 账号 + 密码。**默认**——业务方拍板「登录式是通用、默认」（spec §11.1）。 */
export const MEMBER_MODE_PASSWORD = 'password'
/** ②式：地址 + `client_id` + `client_secret`（鸡蛋铺官方 Open Platform API）。**可选 / 高级**。 */
export const MEMBER_MODE_OPENAPI = 'openapi'
/** 两个方式在**屏幕上**的中文（唯一出处：选项文案与任何展示都从它取）。 */
export const MEMBER_MODE_LABEL_PASSWORD = '账号 + 密码'
export const MEMBER_MODE_LABEL_OPENAPI = '官方开放平台 API'
export const MEMBER_MODE_DEFAULT = MEMBER_MODE_PASSWORD
/** 下拉的选项表（顺序 = 屏幕上的顺序）。
 *
 * ⭐ 业务方裁定：**下家绑定放弃 ②式（API 密钥）对接**——②式选项 `disabled: true` 置灰禁选
 * （存量②式行仍能如实**回显**自己的方式，⛔ 不许假装它是①式——回显错位会让一次保存
 * 把②式配置覆盖成①式）；新建 / 切换都只能走 ①式。
 */
export const MEMBER_MODE_OPTIONS: ReadonlyArray<{
  value: string; label: string; disabled?: boolean
}> = [
  { value: MEMBER_MODE_PASSWORD, label: MEMBER_MODE_LABEL_PASSWORD },
  { value: MEMBER_MODE_OPENAPI, label: MEMBER_MODE_LABEL_OPENAPI, disabled: true },
]
/** ②式的 `client_id` 那一格。 */
export const MEMBER_CLIENT_ID_FIELD_NAME = 'member_client_id'
export const MEMBER_CLIENT_ID_ITEM_ID = 'memberClientIdItem'
/** ②式的 `client_secret` 那一格。⚠️ 这一格**只写不读**（永远不回填，类型也是 `password`）。 */
export const MEMBER_CLIENT_SECRET_FIELD_NAME = 'member_client_secret'
export const MEMBER_CLIENT_SECRET_ITEM_ID = 'memberClientSecretItem'
/** 「回调地址」那一格（容器 / 只读输入 / 地址行 / 原因行 / 复制按钮）。 */
export const MEMBER_CALLBACK_ITEM_ID = 'memberCallbackItem'
export const MEMBER_CALLBACK_URL_INPUT_ID = 'memberCallbackUrl'
export const MEMBER_CALLBACK_ROW_ID = 'memberCallbackRow'
export const MEMBER_CALLBACK_REASON_ID = 'memberCallbackReason'
export const MEMBER_CALLBACK_COPY_BUTTON_ID = 'memberCallbackCopy'
/** 「测试连接」的结果区（那张逐条表落在这里）。 */
export const MEMBER_TEST_RESULT_ID = 'memberBindingTestResult'

/**
 * 「测试连接」的按钮文案（逐字沿用上家那一页的同一句）。
 * ⚠️ 括号里那半句是**承重的**：②式这一跳一个写接口都不打、凭据只从**本人已保存的那一行**取
 * ——它测的是**库里那一行**，⛔ 不是页面上刚填、还没保存的值。
 */
export const MEMBER_TEST_BUTTON_TEXT = '测试连接（用已保存的配置）'
/** 回调地址旁那句人话（用户必须知道这串地址是要**复制到鸡蛋铺那侧**的回调通知设置里）。 */
export const MEMBER_CALLBACK_HELP_TEXT =
  '把下面这个地址填到鸡蛋铺那侧的「开放平台 → 回调通知地址」里。' +
  '这个地址是这台系统专用的，请勿外传。'
/**
 * 还没保存过绑定时回调格显示的那句话（我方自拟）：那种状态下后端还没有一行、没有原因可给。
 * ⛔ 绝不允许画一个空格子。
 */
export const MEMBER_CALLBACK_PENDING_TEXT = '保存一次之后，这里会显示要填进鸡蛋铺那侧的回调地址。'
/**
 * 兜底原因（**不可达的防御分支**）：后端不变式是「地址与原因恰好一个非空」；
 * 万一两个都空，⛔ 也不许画一个空格子 ⇒ 响亮说出来，让缺陷**看得见**。
 */
export const MEMBER_CALLBACK_MISSING_REASON_TEXT =
  '回调地址暂时取不到（后端没给出原因），请把这条反馈给管理员'
/** 复制成功 / 失败的两句提示（我方自拟：对标与本页原先都没有这一格）。 */
export const MEMBER_CALLBACK_COPY_OK_TEXT = '已复制回调地址'
export const MEMBER_CALLBACK_COPY_FAIL_TEXT = '复制失败，请手动选中复制'
/** ②式那两格的早挡文案（**与后端序列化器同字**：前端只做早挡、权威判据在后端）。 */
export const MEMBER_CLIENT_ID_REQUIRED_TEXT = '请填鸡蛋铺给你的 client_id'
export const MEMBER_CLIENT_SECRET_REQUIRED_TEXT =
  '请填鸡蛋铺给你的 client_secret（只在保存时填一次，之后不会再显示）'
/** 逐条表的三档结果（**前端**按后端的 `probed` / `ok` 两个布尔翻译，⛔ 不是另写一套结论）。 */
export const MEMBER_CHECK_RESULT_OK_TEXT = '可用'
export const MEMBER_CHECK_RESULT_FAIL_TEXT = '不可用'
/**
 * **没探过**那一档（`probed=false`）。
 * ⚠️ 必须**与「不可用」分开显示**：未探测的行 `ok` 同样是 `false` ⇒ 照 `ok` 渲染会把
 * 「这一发根本没打」说成「这个接口不可用」——**指错方向**（用户会去找一个根本不需要开的权限）。
 */
export const MEMBER_CHECK_NOT_PROBED_TEXT = '没探'
/** 逐条表那一行的四列（顺序 = 屏幕上的顺序）。 */
export const MEMBER_CHECK_COLUMNS = ['接口', '结果', '原因', '该做什么'] as const
/** 「点了测试连接、后端什么结论都没给」那一档的文案（⛔ 不许让用户点了按钮而屏幕上什么都没有）。 */
export const MEMBER_TEST_EMPTY_RESULT_TEXT = '这次检测没有返回任何结论，请把这条反馈给管理员'

/** 块里所有可寻址元素的选择器（由上面的常量拼出，⛔ 不另抄一份字面量）。 */
export const BINDING_SELECTORS = {
  baseUrl: `input[name="${MEMBER_FIELD_NAMES.baseUrl}"]`,
  account: `input[name="${MEMBER_FIELD_NAMES.account}"]`,
  password: `input[name="${MEMBER_FIELD_NAMES.password}"]`,
    /**
   * ⚠️ 「订单来源名」是**下拉**：选择器写的是 **`select`**，⛔ 不能留 `input` 前缀——
   * 那样读取 / 上锁清单会**静默取空**（选错标签名不报错，只是什么都不发生）。
   * 标签名写全（`select[...]`）是**故意的**：标签换了当场红，而不是靠人读标记。
     */
  sourceName: `select[name="${MEMBER_FIELD_NAMES.sourceName}"]`,
  throwPercent: `input[name="${MEMBER_FIELD_NAMES.throwPercent}"]`,
  // ⭐ D-0299：「同步大厅订单」开关（与「启用」同形的 `lay-skin="switch"` 复选框）。
  hallSync: `input[name="${MEMBER_FIELD_NAMES.hallSync}"]`,
  enabled: `input[name="${MEMBER_FIELD_NAMES.enabled}"]`,
  // ⭐「对接方式」那一轴（②式：官方开放平台 API）。
  mode: `select[name="${MEMBER_MODE_FIELD_NAME}"]`,
  clientId: `input[name="${MEMBER_CLIENT_ID_FIELD_NAME}"]`,
  clientSecret: `input[name="${MEMBER_CLIENT_SECRET_FIELD_NAME}"]`,
  save: `#${MEMBER_SAVE_BUTTON_ID}`,
  test: `#${MEMBER_TEST_BUTTON_ID}`,
  testResult: `#${MEMBER_TEST_RESULT_ID}`,
  health: `#${MEMBER_HEALTH_ID}`,
} as const

/** 缺地址 / 账号时的提示（前端只做**早挡**，权威判据在后端序列化器）。 */
export const MEMBER_MISSING_TEXT = '请先填鸡蛋铺地址与账号'

/**
 * 「同步大厅订单」开关那一格的提示文案（**任务书原文逐字**）。
 * 一句话说清：**照旧** = 指派录单仍同步；**停了** = 不再抛进对方的抢单大厅。
 */
export const MEMBER_HALL_SYNC_HINT =
  '关闭后：指派给你的单仍会同步到对方系统，但不会抛进对方的抢单大厅'

/**
 * 「订单来源名」的默认值：**唯一出处是后端**（序列化器把空串回落到它）。
 * 这里只是**显示副本**（新绑定还没保存时就得让用户看到它）。两份常量各自存在，⛔ 别互相 import。
 */
const SOURCE_NAME_DEFAULT = '团团通系统'

/**
 * 绑定块的失败提示停留时长：**6000ms**（不用本页默认的 1500ms）：
 * 检测不过时后端**逐条点名缺哪条权限**，那一句很长，用户要照着它去下家鸡蛋铺改权限。
 */
const BINDING_ERROR_MS = 6000

/**
 * 「一步步开通（3 分钟）」那一块（**纯函数**，便于测试直接对拍；在绑定块**上方**渲染）。
 * ⚠️ 默认**收起**；两张图走 `ASSET_BASE`（⛔ 不写死 `/guide/`）。
 * ⚠️ 权限清单**三条都要写**（探测真打这三跳，缺任何一条「测试连接」都会红、用户不知道是哪条）；
 * 第 3 步的**先后次序**照实写（先保存 → 再测试连接 → 再选来源 → 再保存）。
 */
export function memberGuideHtml(): string {
  const figure = (img: (typeof MEMBER_GUIDE_IMAGES)[number]): string =>
    '<figure style="margin:8px 0 0;max-width:520px">' +
    `<img src="${img.src}" alt="${img.alt}"` +
    ' style="width:100%;border:1px solid #e6e6e6;border-radius:2px">' +
    `<figcaption style="color:#666;font-size:12px;margin-top:4px">${img.caption}</figcaption>` +
    '</figure>'
  return (
    // ⭐ 布局改上下堆叠后，这块**占整行**、贴左；`max-width:520px` 保留、⛔ 不加居中 margin。
    `<details id="${MEMBER_GUIDE_ID}" open style="margin:12px 0 0;max-width:520px">` +
    '<summary style="cursor:pointer;font-weight:600;color:#16b777">一步步开通（3 分钟）</summary>' +
    '<ol style="margin:8px 0 0 18px;line-height:22px;font-size:13px">' +
    '<li>在鸡蛋铺建一个<b>客服账号</b>（建议专开一个，别用老板号），要有「订单管理」基础功能；' +
    '另外勾上「<b>系统日志</b>」——它只用来两边对账同步，不做别的。' +
    figure(MEMBER_GUIDE_IMAGES[0]) +
    '</li>' +
    '<li>鸡蛋铺 → 系统维护 → <b>订单来源管理 → 新增来源</b>：名称和描述都填「<b>团团通系统</b>」，' +
    '平台选「<b>其它平台</b>」。' +
    figure(MEMBER_GUIDE_IMAGES[1]) +
    '</li>' +
    '<li>回到团团通：选「<b>账号 + 密码</b>」→ 填网址和账号密码 → <b>先保存</b> → ' +
    '<b>再测试连接</b> → <b>再选来源</b>（选刚建的那个）→ <b>再保存</b>。' +
    '</li>' +
    '<li>「大厅抛单百分比」填 <b>100</b>，勾上「<b>启用</b>」，保存。' +
    '</li>' +
    '</ol></details>'
  )
}

/** 块的表单标记（纯函数，便于测试直接对拍；`name` / `class` 都取上面那两个常量）。 */
export function memberBindingHtml(): string {
  const field = (
    label: string, name: string, input: string, hint: string, itemId = '',
  ): string =>
    `<div class="layui-form-item"${itemId === '' ? '' : ` id="${itemId}"`}>` +
    `<label class="layui-form-label" style="padding:8px 10px">${label}</label>` +
    '<div class="layui-input-block">' +
    input.replace('$NAME', name) +
    `<div class="layui-form-mid layui-word-aux">${hint}</div>` +
    '</div></div>'
  /** 那一格的标签（**不含** `$NAME` 替换：下面两格的 `name` 已是字面量）。 */
  const label = (text: string): string =>
    `<label class="layui-form-label" style="padding:8px 10px">${text}</label>`
  return (
    // ⚠️ `admin-info` 是**页内样式**的挂钩（`.admin-info .layui-form-item .layui-form-label
    //    { width:150px }` 靠它生效）⇒ 少了这个类，本块的标签列宽与整页不一致。
    `<div class="${BINDING_PANEL_CLASS} admin-info" style="margin-top:20px">` +
    '<blockquote class="layui-elem-quote" style="color:#FF5722;font-weight:600"><p>' +
    '绑定你自己那家下家鸡蛋铺的账号：绑定并启用之后，本系统指派给你的单会同步过去，' +
    '两边的订单状态会互相同步。' +
    '</p></blockquote>' +
    // ⭐ 「对接方式」排在最前面（先看到怎么对接、再看到要填哪几格）。
    //    选项文案逐字取 `MEMBER_MODE_OPTIONS`；`lay-filter` 取 `MEMBER_MODE_SELECT_ID`（layui 切换事件名）。
    '<div class="layui-form-item" id="' + MEMBER_MODE_ITEM_ID + '">' +
    label('对接方式') +
    '<div class="layui-input-block">' +
    `<select name="${MEMBER_MODE_FIELD_NAME}" id="${MEMBER_MODE_SELECT_ID}"` +
    ` lay-filter="${MEMBER_MODE_SELECT_ID}" class="layui-input layui-input-shadow">` +
    MEMBER_MODE_OPTIONS
      .map((option) =>
        `<option value="${option.value}"${option.disabled ? ' disabled' : ''}>${option.label}</option>`)
      .join('') +
    '</select>' +
    '<div class="layui-form-mid layui-word-aux">' +
    '「账号 + 密码」用鸡蛋铺的登录方式对接。切换不会丢掉已经填好的内容。' +
    '</div>' +
    '</div></div>' +
    field(
      '鸡蛋铺地址', MEMBER_FIELD_NAMES.baseUrl,
      '<input type="text" name="$NAME" autocomplete="off" maxlength="255"' +
      ' placeholder="https://你的工作室.jidanpu.com（要带 https://）"' +
      ' class="layui-input layui-input-shadow">',
      '结尾的 / 可以不写，保存时会去掉。',
    ) +
    // ⭐ ②式那三格**紧随地址之后**（地址是两个分支共用的一格，紧挨着放「另半套凭据」最不容易看错）。
    field(
      'client_id', MEMBER_CLIENT_ID_FIELD_NAME,
      '<input type="text" name="$NAME" autocomplete="off" maxlength="150"' +
      ' placeholder="鸡蛋铺给你的 client_id" class="layui-input layui-input-shadow">',
      '鸡蛋铺开放平台给你的应用身份，不是密钥、可以显示。',
      MEMBER_CLIENT_ID_ITEM_ID,
    ) +
    field(
      'client_secret', MEMBER_CLIENT_SECRET_FIELD_NAME,
      '<input type="password" name="$NAME" autocomplete="new-password" maxlength="255"' +
      ' class="layui-input layui-input-shadow"' +
      ' placeholder="留空 = 不改（已保存的密钥不会再显示出来）">',
      '密钥：为了安全保存后不再回显；<b>留空表示不修改</b>，要换就直接填新的。',
      MEMBER_CLIENT_SECRET_ITEM_ID,
    ) +
    // ⭐ 「回调地址」那一格三层结构：① 常显一句人话；② 地址行（地址为空时整行**不显示**：
    //    ⛔ 不显示半截地址、⛔ 不画空格子）；③ 原因行（显示**后端给的原因**，⛔ 前端不另写文案）。
    `<div class="layui-form-item" id="${MEMBER_CALLBACK_ITEM_ID}">` +
    label('回调地址') +
    '<div class="layui-input-block">' +
    `<div style="font-size:12px;line-height:18px;color:#999">${MEMBER_CALLBACK_HELP_TEXT}</div>` +
    `<div id="${MEMBER_CALLBACK_ROW_ID}" style="display:none;margin-top:6px">` +
    `<input type="text" id="${MEMBER_CALLBACK_URL_INPUT_ID}"` +
    ' class="layui-input layui-input-shadow" readonly' +
    ' style="display:inline-block;width:calc(100% - 64px);vertical-align:middle">' +
    `<button type="button" id="${MEMBER_CALLBACK_COPY_BUTTON_ID}"` +
    ' class="layui-btn layui-btn-xs" style="margin-left:6px">复制</button>' +
    '</div>' +
    `<div id="${MEMBER_CALLBACK_REASON_ID}" style="display:none;margin-top:6px;` +
    'font-size:12px;line-height:18px;color:#c62828"></div>' +
    '</div></div>' +
    field(
      '账号', MEMBER_FIELD_NAMES.account,
      '<input type="text" name="$NAME" autocomplete="off" maxlength="150"' +
      ` lay-verify="${MEMBER_ACCOUNT_VERIFY}"` +
      ' placeholder="鸡蛋铺里的登录名（不是姓名）" class="layui-input layui-input-shadow">',
      '建议在鸡蛋铺里单独建一个专用账号。',
    ) +
    field(
      '密码', MEMBER_FIELD_NAMES.password,
      '<input type="password" name="$NAME" autocomplete="new-password" maxlength="255"' +
      ' class="layui-input layui-input-shadow">',
      '为了安全，保存后不再回显；<b>留空表示不修改</b>，要换就直接填新的。',
    ) +
    // ⭐ 「订单来源名」是**下拉**（⛔ 不再是输入框）；提示位要**有 id 的锚点** ⇒ 这一格手写、结构与 `field()` 同形。
    '<div class="layui-form-item">' +
    '<label class="layui-form-label" style="padding:8px 10px">订单来源名</label>' +
    '<div class="layui-input-block">' +
    `<select name="${MEMBER_FIELD_NAMES.sourceName}" id="${MEMBER_SOURCE_SELECT_ID}"` +
    ' class="layui-input layui-input-shadow"></select>' +
    `<div class="layui-form-mid layui-word-aux" id="${MEMBER_SOURCE_HINT_ID}">` +
    `${MEMBER_SOURCE_HINT_IDLE}</div>` +
    '</div></div>' +
    field(
      '大厅抛单百分比', MEMBER_FIELD_NAMES.throwPercent,
      '<input type="number" name="$NAME" min="1" max="100" step="1"' +
      ' class="layui-input layui-input-shadow" placeholder="100">',
      '<b>只影响大厅镜像</b>：抛到对方大厅时按「我方发单价 × 这个百分比」；' +
      '指派给他、或他自己接的单<b>不打折</b>（那些只录单、接单价就是发单价）。1–100，默认 100。',
    ) +
    // ⭐⭐ 「同步大厅订单」开关：**排在「大厅抛单百分比」之后、「启用」之前**（三格连读 =
    //    「大厅那一档」的完整配置）。这颗只闸「抛大厅」（指派录单 / 状态回传 / 退单 / 改价等照旧）；默认**开**。
    '<div class="layui-form-item">' +
    `<label class="layui-form-label" style="padding:8px 10px">同步大厅订单</label>` +
    '<div class="layui-input-block">' +
    `<input type="checkbox" name="${MEMBER_FIELD_NAMES.hallSync}" lay-skin="switch"` +
    ' lay-text="开|关">' +
    `<div class="layui-form-mid layui-word-aux">${MEMBER_HALL_SYNC_HINT}</div>` +
    '</div></div>' +
    '<div class="layui-form-item">' +
    `<label class="layui-form-label" style="padding:8px 10px">启用</label>` +
    '<div class="layui-input-block">' +
    `<input type="checkbox" name="${MEMBER_FIELD_NAMES.enabled}" lay-skin="switch"` +
    ' lay-text="启用|停用">' +
    '</div></div>' +
    '<div class="layui-form-item"><div class="layui-input-block">' +
    `<button type="button" class="layui-btn layui-btn-info w-lg" id="${MEMBER_SAVE_BUTTON_ID}">` +
    '保存</button>' +
    `<button type="button" class="layui-btn layui-btn-primary w-lg" id="${MEMBER_TEST_BUTTON_ID}">` +
    `${MEMBER_TEST_BUTTON_TEXT}</button>` +
    // ⭐ ②式那张**逐条权限表**落在这里；一开始 `display:none`（没点过就什么都不显示，⛔ 不假装已有结论）。
    `<div id="${MEMBER_TEST_RESULT_ID}" style="display:none;margin-top:6px"></div>` +
    '</div></div>' +
    `<blockquote class="layui-elem-quote layui-quote-nm" id="${MEMBER_HEALTH_ID}"></blockquote>` +
    '</div>'
  )
}

/** 把接口给的时刻显示成人看的样子（`YYYY-MM-DD HH:mm`，与本仓订单页同一处口径）。 */
function memberMoment(value: string): string {
  return value.replace('T', ' ').slice(0, 16)
}

/**
 * 健康状态那一段（**纯函数，便于测试**）：上次同步时间 / 上次失败原因（人话）/ 当前间隔。
 * ⚠️ **空态要如实**：`last_run_at` 为空写「还没跑过同步」、`last_error` 为空写「暂无失败记录」
 * ——那些列还没跑过时空是**真实状态**，⛔ 不写成「正常」或绿色对勾。
 * ⚠️ `last_error` 可能带用户输入 ⇒ 过 `escapeHtml`。
 */
export function memberHealthHtml(row: JidanpuMemberBindingRow | null): string {
  const cell = (label: string, value: string): string =>
    '<div style="display:inline-block;min-width:210px;margin:0 20px 6px 0">' +
    `<span style="color:#999">${label}：</span><b>${value}</b></div>`
  if (row === null) {
    // 还没绑定：⛔ 不编「上次同步」那种时间，也不把间隔写成 0（那是「配了但没跑」的假话）。
    return cell('绑定状态', '还没绑定')
  }
  const run = row.last_run_at === null ? '—（还没跑过同步）' : escapeHtml(memberMoment(row.last_run_at))
  const error = row.last_error === ''
    ? '—（暂无失败记录）'
    : escapeHtml(row.last_error) +
      (row.last_error_at === null ? '' : `（${escapeHtml(memberMoment(row.last_error_at))}）`)
    /**
   * ⭐ ②式那一格。⚠️ **`consumer_state.text` 是后端算好的一句人话** ⇒ **原样透传**
   * （⛔ 前端不加工、也不拿时刻自己再判一次）。⚠️ **一个时刻比较都没有**：不写「多久没收到」、
   * 没有阈值、**不按时间变红**（低单量工作室两小时没单是**正常**的）；那一句为空时**一个字都不编**、
   * 那一格直接不出；**没有第二个「最近收到回调」的时间格**（那一列不进序列化器，页面拿不到）。
     */
  const consumer = row.consumer_state.text
  return (
    cell('绑定状态', row.enabled ? '已启用' : '未启用') +
    cell('上次同步', run) +
    // 「当前生效的间隔」是**只读展示**（页面没有这两个输入框）。
    cell('当前间隔', `快路 ${row.fast_poll_seconds} 秒 / 对账 ${row.reconcile_seconds} 秒`) +
    cell('大厅抛单百分比', `${row.throw_price_percent}%`) +
    cell('上次失败原因', error) +
    // ⭐ 最后那一格只在**有内容**时才出现（⛔ 不画空格子、也不编值）。
    (consumer ? cell('回调消费', escapeHtml(consumer)) : '')
  )
}

/** 健康状态上屏（两态都显式写：只写一态的实现回不去，同 `renderRealName` 的手法）。 */
export function renderMemberHealth(root: HTMLElement, row: JidanpuMemberBindingRow | null): void {
  const el = root.querySelector<HTMLElement>(BINDING_SELECTORS.health)
  if (el) el.innerHTML = memberHealthHtml(row)
}

// ══ 「对接方式」那一轴（①式 账号+密码 / ②式 官方开放平台 API）════════════
//
// ⚠️ 形状照「绑定上家系统设置」页那一套（界面口径必须一致）；两份实现**各自存在**、
// ⛔ 不互相 import（那样会让一条线的改动意外改到另一条）。

/**
 * 按对接方式切各格的 `display`：
 * `password`（默认）显示①式各格、隐藏②式那三格与结果表；`openapi` 相反（还隐藏**订单来源名**
 * ——②式探针不返回来源字典，留着会误导）。
 * ⚠️ **判据是 `display:none` 那一层**（⛔ **不禁用**、⛔ **不清值**）：两个分支共用同一份 DOM
 * 与同一个提交出口 ⇒ 禁用或清值会让「切过去再切回来」丢掉用户刚填的东西。
 * ⚠️ 认不出的取值一律按**默认那一支**处理（⛔ 不留「两套凭据都露着」的中间态）。
 * ⚠️ 「账号」格的 `lay-verify` 必填口径随分支摘 / 装——只动属性，与「不清值」同一条纪律。
 */
export function applyMemberMode(root: HTMLElement, mode: string): void {
  const panel = root.querySelector<HTMLElement>(BINDING_PANEL_SELECTOR)
  if (panel === null) return
  const isOpenapi = mode === MEMBER_MODE_OPENAPI
  /** 按 `name` 找那一格（⛔ 不按「第 n 个 `.layui-form-item`」现算：索引会随格子增减漂）。 */
  const item = (name: string): HTMLElement | null => {
    const field = panel.querySelector<HTMLElement>(`[name="${name}"]`)
    return field?.closest<HTMLElement>('.layui-form-item') ?? null
  }
  const setVisible = (el: HTMLElement | null, visible: boolean): void => {
    if (el !== null) el.style.display = visible ? '' : 'none'
  }
  // 两套凭据**互斥显示**（②式那一路不登录 ⇒ 账号 / 密码两格在②式下没有任何用处）。
  setVisible(item(MEMBER_FIELD_NAMES.account), !isOpenapi)
  setVisible(item(MEMBER_FIELD_NAMES.password), !isOpenapi)
  // ⭐ 「账号」格的必填口径随分支摘 / 装：②式下它是被隐藏的**空串**，留着 `required` 等于声明
  //    一件不成立的事；切回①式要**装回去**。⚠️ 只动属性、⛔ 不清值、⛔ 不禁用；密码那一格⛔ 不装。
  const accountInput = panel.querySelector<HTMLInputElement>(
    `[name="${MEMBER_FIELD_NAMES.account}"]`,
  )
  if (accountInput !== null) {
    if (isOpenapi) accountInput.removeAttribute('lay-verify')
    else accountInput.setAttribute('lay-verify', MEMBER_ACCOUNT_VERIFY)
  }
  setVisible(panel.querySelector<HTMLElement>(`#${MEMBER_CLIENT_ID_ITEM_ID}`), isOpenapi)
  setVisible(panel.querySelector<HTMLElement>(`#${MEMBER_CLIENT_SECRET_ITEM_ID}`), isOpenapi)
  setVisible(panel.querySelector<HTMLElement>(`#${MEMBER_CALLBACK_ITEM_ID}`), isOpenapi)
  // ⭐「订单来源名」只在①式下显示：②式的探测出参里**没有**来源字典 ⇒ 留着会让用户以为
  //    「②式也能选来源」而底下那一格根本不会更新。只动 `display`，⛔ 不清值、⛔ 不禁用。
  setVisible(item(MEMBER_FIELD_NAMES.sourceName), !isOpenapi)
  // ⭐ ①式下那张**结果表**也收起来（它是②式的结论）；收的只是可见性，内容不清（切回来还是同一结论）。
  setVisible(panel.querySelector<HTMLElement>(`#${MEMBER_TEST_RESULT_ID}`), isOpenapi)
}

/**
 * ⭐ **①式**载荷要不要补一个 `mode: 'password'`（**只**在库里那一行本来就是②式时）。
 *
 * - ⛔ 不补的后果（**静默**）：②式绑定切回①式保存 ⇒ 载荷里没有 `mode` ⇒ 后端按**库里那一格**
 *   （仍是 `openapi`）走 ⇒ 页面弹「保存成功」而引擎**仍走②式**、新填的①式凭据永远不会被用；
 * - ⛔ **反向也要判**：库里那一行本来就是①式（或还没有行）时**一个键都不加**（载荷逐字不变）。
 *
 * ⚠️ 判据只有 `mode` 这一格（⛔ 不用 `client_id` 非空反推——切回①式后它还在，真值判据会把
 * ①式用户当成②式）。
 *
 * ⚠️ **`submittedMode` 是承重的**：②式的载荷**自己就带** `mode: 'openapi'` ⇒ **②式那一支永远不补**
 * （无条件补会**盖掉**它、后端按①式判，回一句「鸡蛋铺账号不能为空」——挡住用户保存的正是这一处）。
 * ⛔ 两个形参都**不许**给默认值：默认值会让「漏传」重新变成一次**静默**的错方式。
 */
export function memberPasswordModePatch(
  storedMode: string, submittedMode: string,
): { mode?: string } {
  if (submittedMode === MEMBER_MODE_OPENAPI) return {}
  return storedMode === MEMBER_MODE_OPENAPI ? { mode: MEMBER_MODE_PASSWORD } : {}
}

/**
 * 回调地址那一格该显示什么（**纯函数**，便于用真值钉住）。
 * 有地址 ⇒ `{url, reason: ''}`（值**只**来自后端算好的 `callback_url`，⛔ 前端一个字符都不拼）；
 * 没地址 ⇒ `reason` **优先用后端给的人话**（⛔ 绝不显示空串）；还没保存过 ⇒ 用
 * `MEMBER_CALLBACK_PENDING_TEXT`（后端还没有一行、没有原因可给）；两个都空 ⇒ 兜底文案。
 */
export function memberCallbackCellState(
  row: Pick<JidanpuMemberBindingRow, 'callback_url' | 'callback_url_reason'> | null,
): { url: string; reason: string } {
  if (row === null) return { url: '', reason: MEMBER_CALLBACK_PENDING_TEXT }
  const url = row.callback_url ?? ''
  if (url !== '') return { url, reason: '' }
  return { url: '', reason: row.callback_url_reason || MEMBER_CALLBACK_MISSING_REASON_TEXT }
}

/**
 * 回调地址那一格上屏（**两态都显式写**：只写一态的实现回不去）。
 * ⚠️ 地址为空 ⇒ **连那一行都不显示**（不显示半截地址，也不显示一个空格子）。
 * ⚠️ 这一格**与对接方式无关**（它是「这一行有没有地址 / 为什么没有」）：切方式时⛔ 不重算它。
 */
export function renderMemberCallbackCell(
  root: HTMLElement,
  row: Pick<JidanpuMemberBindingRow, 'callback_url' | 'callback_url_reason'> | null,
): void {
  const panel = root.querySelector<HTMLElement>(BINDING_PANEL_SELECTOR)
  if (panel === null) return
  const state = memberCallbackCellState(row)
  const urlInput = panel.querySelector<HTMLInputElement>(`#${MEMBER_CALLBACK_URL_INPUT_ID}`)
  if (urlInput) urlInput.value = state.url
  const row_ = panel.querySelector<HTMLElement>(`#${MEMBER_CALLBACK_ROW_ID}`)
  if (row_) row_.style.display = state.url === '' ? 'none' : ''
  const reason = panel.querySelector<HTMLElement>(`#${MEMBER_CALLBACK_REASON_ID}`)
  if (reason) {
    reason.textContent = state.reason
    reason.style.display = state.reason === '' ? 'none' : ''
  }
}

/**
 * 把后端那一行**逐条**渲染成三个字之一（⛔ 不另写一套结论）。
 * ⚠️ **`probed` 必须先判**：未探测的行 `ok` 同样是 `false` ⇒ 照 `ok` 渲染会**指错方向**。
 */
export function memberCheckResultText(check: Pick<JidanpuEndpointCheck, 'probed' | 'ok'>): string {
  if (!check.probed) return MEMBER_CHECK_NOT_PROBED_TEXT
  return check.ok ? MEMBER_CHECK_RESULT_OK_TEXT : MEMBER_CHECK_RESULT_FAIL_TEXT
}

/**
 * ②式「测试连接」的**逐条权限表**（四列：接口 / 结果 / 原因 / 该做什么）。
 * ⚠️ **`label` / `reason` / `message` / `action` 全部直接用后端给的**（⛔ 前端一个字都不改写）；
 * 三处文本可能是**对方（鸡蛋铺）的原话** ⇒ 一律 `escapeHtml`，⛔ 不拿第三方文本拼 HTML。
 * ⚠️ `reason`（一句话原因）与 `message`（给用户看的话，含去哪开权限）**都要显示**，⛔ 不合成一句。
 */
export function buildMemberChecksTableHtml(
  checks: readonly JidanpuEndpointCheck[],
): string {
  const head = MEMBER_CHECK_COLUMNS.map((title) => `<th>${title}</th>`).join('')
  const body = checks
    .map((check) => {
      const detail =
        check.message !== ''
          ? `<div style="margin-top:4px">${escapeHtml(check.message)}</div>`
          : ''
      return (
        '<tr>' +
        `<td>${escapeHtml(check.label)}</td>` +
        `<td>${escapeHtml(memberCheckResultText(check))}</td>` +
        `<td>${escapeHtml(check.reason)}${detail}</td>` +
        `<td>${escapeHtml(check.action)}</td>` +
        '</tr>'
      )
    })
    .join('')
  return (
    '<table class="layui-table" lay-size="sm" style="margin:0">' +
    `<thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
  )
}

/**
 * 「测试连接」结果区要显示的 HTML（**纯函数**）。
 * ②式 ⇒ 那张表；拿不到 `checks` ⇒ 退回逐条 `messages`（⛔ **绝不静默**：点了按钮必须有东西出来）。
 */
export function memberTestResultHtml(result: {
  messages?: readonly string[]
  checks?: readonly JidanpuEndpointCheck[]
}): string {
  const checks = result.checks ?? []
  if (checks.length > 0) return buildMemberChecksTableHtml(checks)
  const messages = result.messages ?? []
  if (messages.length === 0) {
    return (
      '<div style="font-size:12px;line-height:18px;color:#c62828">' +
      `${MEMBER_TEST_EMPTY_RESULT_TEXT}</div>`
    )
  }
  return messages
    .map(
      (text) =>
        `<div style="font-size:12px;line-height:18px;margin:2px 0">${escapeHtml(text)}</div>`,
    )
    .join('')
}

/** 结果区上屏（**只在②式**那条路径上被调用；`display` 由 `applyMemberMode` 管）。 */
export function renderMemberTestResult(root: HTMLElement, html: string): void {
  const box = root.querySelector<HTMLElement>(`#${MEMBER_TEST_RESULT_ID}`)
  if (!box) return
  box.innerHTML = html
}

// ══ 「订单来源名」下拉 ═════════════════════════════════════════
//
// ⚠️ 与已删除的租户级实现**同形**但两份各自存在（两条线零关系、⛔ 别互相 import）；
// 本文件这一套**照旧生效**，且与 `@/api/jidanpuMember` 那条线自洽。

/** 这一格的状态：**对方来源字典**（`null` = 还没拉到过）+ 「那一跳读到了没有」。 */
export type MemberSourceDict = {
  options: JidanpuSourceOption[] | null
  ok: boolean
}

/** 干净的初值（还没点过「测试连接」）。⚠️ 用**函数**而不是共享常量：⛔ 别让两个页面共用一个可变对象。 */
export function emptyMemberSourceDict(): MemberSourceDict {
  return { options: null, ok: false }
}

/** 提示位三态（**纯函数，便于测试**）。 */
export type MemberSourceHint = { text: string; warn: boolean }

/** 还没点过「测试连接」时的常态提示（= 标记里那一句的**唯一出处**）。 */
export const MEMBER_SOURCE_HINT_IDLE =
  '录单时写进鸡蛋铺的来源名；点「测试连接」可拉取对方来源名单后在这里选。'
/** 字典这一跳没读到（与「对方真的没有」是两件事）。 */
export const MEMBER_SOURCE_HINT_UNREADABLE =
  '这次没读到对方的来源名单（不影响保存）；可再点一次「测试连接」。'

/**
 * 「订单来源名」提示位的三支：
 * ① **同名多条** ⇒ 如实说「无法区分」（对方端点没有 id，⛔ 不假装可区分）；
 * ② **对方已无此来源** ⇒ 保留那一项并提示「请重选」，⛔ 不悄悄清空；
 * ③ **字典没读到** ⇒ ⛔ **一个字的「对方已无此来源」都不许说**——那是把「没读到」说成
 *    「对方删了」；少了这一支，一次**登录失败**就会让页面喊「对方已无此来源」。
 */
export function memberSourceHint(
  dict: MemberSourceDict,
  current: string,
): MemberSourceHint {
  if (dict.options === null) return { text: MEMBER_SOURCE_HINT_IDLE, warn: false }
  if (!dict.ok) return { text: MEMBER_SOURCE_HINT_UNREADABLE, warn: true }
  const labels = dict.options.map((item) => item.label)
  const counted = new Map<string, number>()
  for (const label of labels) counted.set(label, (counted.get(label) ?? 0) + 1)
  const duplicated = Array.from(counted.values()).filter((count) => count > 1)
  const worst = duplicated.length === 0 ? 0 : Math.max(...duplicated)
  const lines: string[] = []
  if (worst > 1) {
    lines.push(
      `对方系统里有 ${worst} 个同名来源，我们无法区分它们；` +
        '请到对方系统改成不同名字后再选',
    )
  }
  if (current !== '' && !labels.includes(current)) lines.push('对方已无此来源，请重选')
  return lines.length === 0
    ? { text: MEMBER_SOURCE_HINT_IDLE, warn: false }
    : { text: lines.join('；'), warn: true }
}

/** 刷新提示位（内容与颜色都出自 `memberSourceHint`，⛔ 不在这里拼文案）。 */
export function refreshMemberSourceHint(root: HTMLElement, dict: MemberSourceDict): void {
  const hint = root.querySelector<HTMLElement>(`#${MEMBER_SOURCE_HINT_ID}`)
  if (hint === null) return
  const current = root.querySelector<HTMLSelectElement>(BINDING_SELECTORS.sourceName)?.value ?? ''
  const state = memberSourceHint(dict, current.trim())
  hint.textContent = state.text
  // ⚠️ 颜色取**本页既有**的警示色（`#FF5722`）：⛔ 不发明新颜色、⛔ 不引 Element Plus。
  hint.style.color = state.warn ? '#FF5722' : ''
}

/**
 * 重建「订单来源名」下拉的选项并选中 `selected`（+ 刷新提示位）。
 * ⚠️ **这一格永远有一个非空选中值**：两处 PUT 对空串的语义**相反**——租户级 = 「不动它」，
 * 本页这条 = **落回默认名「团团通系统」** ⇒ 下拉能提交空值的话，用户「什么都没动」的一次保存
 * 就会把自定义来源名**静默改回默认值**。所以：① 当前名字不在字典里也保留并选中；
 * ② 选项用 `createElement` + `textContent` 造（来源名来自上游，⛔ 不拼 HTML）；
 * ③ 同名多项**都保留**（⛔ 不假装可区分、也不去重）。
 */
export function renderMemberSourceOptions(
  root: HTMLElement,
  selected: string,
  dict: MemberSourceDict,
): void {
  const select = root.querySelector<HTMLSelectElement>(BINDING_SELECTORS.sourceName)
  if (select === null) return
  const option = (text: string, optionValue: string): HTMLOptionElement => {
    const node = document.createElement('option')
    node.textContent = text
    node.value = optionValue
    return node
  }
  // ⚠️ 空串 ⇒ **默认名**（与 `fillMemberBinding` 既有那一行同口径；后端也会把空串落成它）。
  const wanted = selected.trim() === '' ? SOURCE_NAME_DEFAULT : selected
  select.innerHTML = ''
  const labels: string[] = []
  for (const item of dict.options ?? []) {
    select.appendChild(option(item.label, item.label))
    labels.push(item.label)
  }
  if (!labels.includes(wanted)) select.appendChild(option(wanted, wanted))
  select.value = wanted
  refreshMemberSourceHint(root, dict)
}

/**
 * 载荷 → 表单。⚠️ **密码框永远留空**（后端 `write_only`；只有一个只读替身 `password_set` 布尔
 * ——⛔ 把它当密码回填就是往输入框里写 `true`/`false`）。
 * ⚠️ `sourceDict` 给了就用它填下拉；⛔ **没有它也要把库里那个名字填上并选中**
 * （那一格永远不许是空的，见 `renderMemberSourceOptions`）。
 */
export function fillMemberBinding(
  root: HTMLElement,
  row: JidanpuMemberBindingRow | null,
  sourceDict: MemberSourceDict = emptyMemberSourceDict(),
): void {
  const set = (selector: string, value: string): void => {
    const el = input(root, selector)
    if (el) el.value = value
  }
  set(BINDING_SELECTORS.baseUrl, row?.jidanpu_base_url ?? '')
  set(BINDING_SELECTORS.account, row?.account ?? '')
  // ⭐ ②式两格：`client_id` 是身份 ⇒ 回显；`client_secret` 是密钥 ⇒ **永远空串**（后端只回布尔，回填会显示 true/false）。
  set(BINDING_SELECTORS.clientId, row?.client_id ?? '')
  const secret = input(root, BINDING_SELECTORS.clientSecret)
  if (secret) {
    secret.value = ''
    secret.placeholder = row?.client_secret_set === true
      ? '已配置，留空表示不修改'
      : '请输入鸡蛋铺给你的 client_secret'
  }
  // ⭐ 可见性跟着**库里那一行**的方式走（⛔ 不是无脑回默认值——否则②式用户一进页面看到的是
  //    「账号 + 密码」，一保存就把自己切回①式）。⚠️ **下拉自己的 `.value` 也要写**：
  //    `applyMemberMode` 不读写这个值而 `readMemberMode` 从 DOM 读 ⇒ 少了这一句会**静默走错方式**。
  //    必须排在两个 `set` **之后**（切可见性只动 display，值已经写进去了）。
  const mode = row?.mode === MEMBER_MODE_OPENAPI ? MEMBER_MODE_OPENAPI : MEMBER_MODE_DEFAULT
  const modeSelect = root.querySelector<HTMLSelectElement>(BINDING_SELECTORS.mode)
  if (modeSelect) modeSelect.value = mode
  applyMemberMode(root, mode)
  // ⭐ 回调地址那一格（值只来自后端；算不出来时显示后端给的原因；还没保存过显示那句人话）。
  renderMemberCallbackCell(root, row)
  // ⭐ 来源名（**下拉**，D-0177）：新行给显示副本「团团通系统」（后端也会把空串落到同一个默认值）。
  renderMemberSourceOptions(root, row?.source_name ?? '', sourceDict)
  // 百分比：库里那个值；新行 / 空值给后端默认的 100（空着会让 `Number('')` 落成 0＝非法）。
  set(BINDING_SELECTORS.throwPercent, String(row?.throw_price_percent ?? 100))
  const password = input(root, BINDING_SELECTORS.password)
  if (password) {
    password.value = ''
    password.placeholder = row?.password_set === true
      ? '已配置，留空表示不修改'
      : '请输入鸡蛋铺账号的密码'
  }
  // ⭐ D-0299：「同步大厅订单」回填**库里那一格**（取不到 ⇒ 开）；⛔ 不写成恒 true（会静默打开用户关掉的开关）。
  const hallSync = input(root, BINDING_SELECTORS.hallSync)
  if (hallSync) hallSync.checked = row?.hall_sync_enabled ?? true
  const enabled = input(root, BINDING_SELECTORS.enabled)
  if (enabled) enabled.checked = row?.enabled ?? false
  renderMemberHealth(root, row)
}

/** 表单 → 载荷（DOM 是唯一真相：layui 的 `data.field` 在 iframe 岛页里会取空）。 */
export function readMemberBindingPayload(root: HTMLElement): JidanpuMemberBindingPayload {
  const value = (selector: string): string => (input(root, selector)?.value ?? '').trim()
  return {
    jidanpu_base_url: value(BINDING_SELECTORS.baseUrl),
    account: value(BINDING_SELECTORS.account),
    // ⚠️ 空串**照传**：它是后端的「不改」（spec §8-S4）。⛔ 不要在这里过滤掉这个键。
    password: input(root, BINDING_SELECTORS.password)?.value ?? '',
    source_name: value(BINDING_SELECTORS.sourceName),
    // 空串 ⇒ 100（后端那一列的默认值）；范围校验以后端为准（前端只做早挡）。
    throw_price_percent: Number(value(BINDING_SELECTORS.throwPercent) || '100'),
    // ⭐ D-0299：开关随 DOM 走（取不到 ⇒ true＝后端模型默认；⛔ false 会把「没渲染出来」当成「不想同步」）。
    hall_sync_enabled: input(root, BINDING_SELECTORS.hallSync)?.checked ?? true,
    enabled: input(root, BINDING_SELECTORS.enabled)?.checked ?? false,
  }
}

/**
 * ⭐ ②式的载荷（`mode="openapi"` 那一支）。
 * ⛔ **载荷里没有 `password`**（②式那一路不登录）。
 * ⚠️ `client_secret` **不 trim**（密钥里的空格是密钥的一部分）、**空串 = 不改**。
 * ⚠️ `account` 照旧带上页面里那个值——⛔ 发空串会把①式那一套用户名**静默清掉**。
 */
export function readMemberOpenApiPayload(root: HTMLElement): JidanpuMemberBindingPayload {
  const value = (selector: string): string => (input(root, selector)?.value ?? '').trim()
  return {
    mode: MEMBER_MODE_OPENAPI,
    jidanpu_base_url: value(BINDING_SELECTORS.baseUrl),
    account: value(BINDING_SELECTORS.account),
    client_id: value(BINDING_SELECTORS.clientId),
    client_secret: input(root, BINDING_SELECTORS.clientSecret)?.value ?? '',
    source_name: value(BINDING_SELECTORS.sourceName),
    throw_price_percent: Number(value(BINDING_SELECTORS.throwPercent) || '100'),
    // ⭐ D-0299：②式那支**同样带上**这个开关（两条凭据轴共用同一格配置）。
    hall_sync_enabled: input(root, BINDING_SELECTORS.hallSync)?.checked ?? true,
    enabled: input(root, BINDING_SELECTORS.enabled)?.checked ?? false,
  }
}

/** 块里当前选中的对接方式（认不出的取值按**默认那一支**处理，⛔ 不留中间态）。 */
export function readMemberMode(root: HTMLElement): string {
  const value = root.querySelector<HTMLSelectElement>(BINDING_SELECTORS.mode)?.value ?? ''
  return value === MEMBER_MODE_OPENAPI ? MEMBER_MODE_OPENAPI : MEMBER_MODE_DEFAULT
}

/**
 * 绑定块里「取数之前先上锁」的那一批：①式三格 + ②式三格 + 开关 + 两颗按钮。
 * 与 `LOADING_LOCKED_CONTROLS` **同一口径、各自一份清单**（⛔ 不合并）：标记一注入就可编辑，
 * 而回填要等取数回来 ⇒ 窗口里打进去的字会被服务端旧值**静默覆盖**。
 */
const MEMBER_LOCKED_CONTROLS: readonly string[] = [
  BINDING_SELECTORS.baseUrl,
  BINDING_SELECTORS.account,
  BINDING_SELECTORS.password,
  // ⭐ ②式那两格与「对接方式」格同样是「回填会被覆盖」的窗口。⚠️ 回调地址那一格**不上锁**：
  //    它是只读展示 + 复制按钮——锁了等于「连自己那串地址都不让复制」。
  BINDING_SELECTORS.mode,
  BINDING_SELECTORS.clientId,
  BINDING_SELECTORS.clientSecret,
  BINDING_SELECTORS.sourceName,
  BINDING_SELECTORS.throwPercent,
  // ⭐ 「同步大厅订单」开关同样上锁（先关掉、取数回来说开着 ⇒ 一保存就把关掉的动作静默吞掉）。
  BINDING_SELECTORS.hallSync,
  BINDING_SELECTORS.enabled,
  BINDING_SELECTORS.save,
  BINDING_SELECTORS.test,
]

/** 上锁 / 解锁（只碰 `MEMBER_LOCKED_CONTROLS`；「保存」另加**看得见的**禁用态，同 `setLoadingLock`）。 */
export function setMemberBindingLock(root: HTMLElement, locked: boolean): void {
  for (const selector of MEMBER_LOCKED_CONTROLS) {
    const el = root.querySelector<HTMLElement>(selector)
    if (!el) continue
    if (locked) el.setAttribute('disabled', 'disabled')
    else el.removeAttribute('disabled')
  }
  root.querySelector(BINDING_SELECTORS.save)?.classList.toggle('layui-btn-disabled', locked)
}

/** 「功能设置」页签的**标题文字**（⚠️ 它是**页面自己的**页签名，不是那一格里的文案）。 */
const FEATURE_TAB_TITLE = '功能设置'

/**
 * 「功能设置」页签的那个面板（= 与页签标题**同序**的 `.layui-tab-item`）。
 * ⚠️ 靠**页签标题**定位，⛔ 不是硬编码索引——对标重新导出证据件时页签顺序会变。
 * @returns 找不到就是 `null`（调用方各自决定怎么处置）。
 */
function featureSettingsPanel(root: HTMLElement): HTMLElement | null {
  const titles = Array.from(root.querySelectorAll<HTMLElement>('.layui-tab-title > li'))
  const index = titles.findIndex((li) => li.textContent?.trim() === FEATURE_TAB_TITLE)
  if (index < 0) return null
  return root.querySelectorAll<HTMLElement>('.layui-tab-item')[index] ?? null
}

/**
 * 「功能设置」页签里那条**只读红字**的**结构定位**（业务方裁定要删掉它）。
 * 形状 = 「**显示出来的**那一格 `.admin-info` **自己的** `blockquote.layui-elem-quote`」，
 * 三个限定各有理由（少任何一个都会**删多**，而删多是**静默**的）：`:not(.layui-hide)` 排除
 * 证据件里带 `layui-hide` 的对照块；`>` 只认直接子；只在「功能设置」面板里找（「修改密码」
 * 页签里还有一条同形的「初始密码」提示，不限定面板会顺手删掉）。
 * ⛔ **不用那句中文文案当选择器**（文案会随对标改版漂、第二处同样的话会一起删）；
 * ⛔ 也不用索引（证据件重新导出时会变）。
 */
const READONLY_NOTE_SELECTOR = '.admin-info:not(.layui-hide) > blockquote.layui-elem-quote'

/**
 * ⭐ 把那条只读红字从**运行期 DOM** 里删掉（业务方裁定）。⛔ 不改证据件（那是**对标渲染结果的留档**，
 * 改它等于伪造证据）——只删 `blockquote` 本身，留下空的 `.admin-info` 外壳。
 * ⚠️ 只在标记注入**之后**调。@returns 真的删掉了没有（`false` = 证据件结构变了 ⇒ 页测会当场红）。
 */
export function removeReadonlyBindingNote(root: HTMLElement): boolean {
  const note = featureSettingsPanel(root)?.querySelector<HTMLElement>(READONLY_NOTE_SELECTOR)
  if (!note) return false
  note.remove()
  return true
}

/**
 * 把块追加进「功能设置」页签（放在右列——那一列本来就是留白）。⚠️ 只在标记注入**之后**调；
 * `form.render()` 必须在它之后（开关是 `lay-skin="switch"` 的复选框，不重渲就不出现）。
 * @returns 真的插进去了没有（`false` = 证据件的结构变了，两条测试会当场红）。
 */
export function injectMemberBindingBlock(root: HTMLElement): boolean {
  const panel = featureSettingsPanel(root)
  if (!panel) return false
  const columns = Array.from(panel.querySelectorAll<HTMLElement>('.layui-col-md6'))
  const host = columns[1] ?? panel
  // ⭐ 布局改准：**上下堆叠、全部左对齐**（两栏 flex 会把表单挤到右边）。实现 = 不包 flex 容器、
  //    直接按块级流追加两个兄弟（间距各自自带，视觉值不新造）。
  host.insertAdjacentHTML('beforeend', memberGuideHtml() + memberBindingHtml())
  // ⭐ 标记一进来就把**可见性**与**回调地址那一格**摆成「还没取数」的样子：
  //    ②式那三格先 `display:none`、回调格显示「保存一次之后…」⇒ 注入与取数之间**不会出现空格子**。
  applyMemberMode(root, MEMBER_MODE_DEFAULT)
  renderMemberCallbackCell(root, null)
  return true
}

/**
 * 「取数之前先上锁」的那一批控件 = **7 个可编辑输入框 + 「保存更改」按钮**。
 * 为什么需要（**产品缺陷修复，不是测试手段**）：标记一注入这些框就可编辑，而回填要等取数回来
 * ⇒ 窗口里打字的人会被**服务端旧值静默覆盖**；再保存就把旧值/空串写回去、页面还报「保存成功」。
 * ⛔ **只碰这一批**：`loginAcc`（永久只读）与证据件里本来就 `disabled` 的几个不在其中——别顺手打开；
 * ⛔ 「修改密码」页签**不在**这一批里：它没有回填、不存在这个窗口，必须随时可用。
 */
const LOADING_LOCKED_CONTROLS: readonly string[] = [
  ...Object.values(EDITABLE_SELECTORS),
  SAVE_BUTTON,
]

/** 上锁 / 解锁（只碰 `LOADING_LOCKED_CONTROLS`）。 */
function setLoadingLock(root: HTMLElement, locked: boolean): void {
  for (const selector of LOADING_LOCKED_CONTROLS) {
    const el = root.querySelector<HTMLElement>(selector)
    if (!el) continue
    if (locked) el.setAttribute('disabled', 'disabled')
    else el.removeAttribute('disabled')
  }
  // ⚠️ **「保存更改」还必须有看得见的禁用态**：`layui.css` 里**没有** `:disabled` 规则，
  // 只加属性时这个按钮与可点时**长得一模一样**、点击被静默吞掉（取数失败后更是永久停在这个状态）。
  // 用 layui 自带的 `.layui-btn-disabled`（带 `!important`，能盖过 `.layui-btn`）——⛔ 不要自己发明 CSS。
  root.querySelector(SAVE_BUTTON)?.classList.toggle('layui-btn-disabled', locked)
}

function input(root: HTMLElement, selector: string): HTMLInputElement | null {
  return root.querySelector<HTMLInputElement>(selector)
}

/** 按**标签文字**找到那个 `.layui-form-item`（证据件里没有可用的 id/class 锚点）。 */
function itemByLabel(root: HTMLElement, label: string): HTMLElement | null {
  const hit = Array.from(root.querySelectorAll<HTMLElement>('.layui-form-label'))
    .find((el) => (el.textContent ?? '').trim() === label)
  return hit?.closest<HTMLElement>('.layui-form-item') ?? null
}

function errorText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : '请求失败，请稍后重试'
}

/**
 * 「微信收款码」那一格的**两态呈现**（两态都显式写：只写一态的实现回不去）。
 * 已上传 ⇒ 「已上传」显示 + 上传那颗文案变「重新上传」（⛔ 不是新按钮：仍是证据件里那一个）；
 * 未上传 ⇒ 藏起来 + 文案「上传收款码」——⛔ 不留一颗点了没反应的死按钮。
 * 链接存在证据件本来就有的隐藏域 `#qrCode1` 里；开层的处理器在点击那一刻才读它。
 */
export function renderWechatQr(root: HTMLElement, url: string | null | undefined): void {
  const look = root.querySelector<HTMLElement>(WECHAT_QR_LOOK)
  const upload = root.querySelector<HTMLElement>(WECHAT_QR_UPLOAD)
  const store = input(root, WECHAT_QR_URL_INPUT)
  if (typeof url === 'string' && url !== '') {
    if (look) look.style.display = ''
    if (upload) upload.textContent = WECHAT_QR_REUPLOAD_TEXT
    if (store) store.value = url
  } else {
    if (look) look.style.display = 'none'
    if (upload) upload.textContent = WECHAT_QR_IDLE_TEXT
    if (store) store.value = ''
  }
}

/** 回填（spec §6.2 的映射表）。 */
export function fillProfile(root: HTMLElement, data: MyProfile): void {
  const set = (selector: string, value: string): void => {
    const el = input(root, selector)
    if (el) el.value = value
  }
  set(LOGIN_ACC, data.username)
  set(SID, data.id_card_no)
  set(BASIC_SALARY, data.base_salary == null ? '0.00' : Number(data.base_salary).toFixed(2))
  for (const [key, selector] of Object.entries(EDITABLE_SELECTORS)) {
    set(selector, String(data[key as keyof MyProfilePatch] ?? ''))
  }
  const avatar = root.querySelector<HTMLImageElement>(AVATAR)
  if (avatar) {
    // 传过头像 ⇒ 用**服务端签发的**短时效 URL；没传过才回落生成的色块。⛔ 两个来源都不在前端拼 URL。
    avatar.src = data.avatar_url
      ?? avatarDataUri(data.display_name || data.username, PROFILE_AVATAR_SIZE)
  }
  // 微信收款码那一格的两态（`wechat_qr_url` 已在资料载荷里，⛔ 无需新请求、⛔ 无需改后端）。
  renderWechatQr(root, data.wechat_qr_url)
}

/** 允许上传的扩展名（与后端 `ALLOWED_EXTS` 同口径；⛔ 前端只是**早挡**，边界仍在服务端）。 */
const ALLOWED_UPLOAD_EXTS = ['jpg', 'jpeg', 'png'] as const

/**
 * 头像 / 收款码的单张上限提示 **10 MB**。
 * ⚠️ **后端是权威**（认领与直传路由两处各判一次）；这一句只是**发起票据请求之前**的前置提示。
 * ⛔ 后端真拒了要**原样显示它的 msg**，⛔ 不许拿这一句去覆盖；两边文案是同一事实的两处字面值，
 * 改数值要**同一次**改两处。
 */
const MEDIA_TOO_LARGE_TEXT = '头像与收款码最大支持10MB，请压缩后重试'

/**
 * 直传那一跳只在 `@/layui-island/directUpload` 里有一份实现，本页直接用它
 * （⛔ 不在本文件里保留第二份——同一事实只留一处）。
 */

/** 选文件 → 要凭证 → 直传 → 认领 → 刷新显示。⛔ 全程不经过我们服务器的图片字节。 */
async function handleMediaUpload(
  kind: MediaKind, root: HTMLElement, notify: (text: string) => void,
): Promise<void> {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = 'image/jpeg,image/png'
  // ⚠️ **必须插进文档**再 `click()`：游离节点在部分浏览器上不弹文件框；用完立刻移除。
  input.style.display = 'none'
  document.body.appendChild(input)
  input.addEventListener('change', () => {
    void (async () => {
      const file = input.files?.[0]
      if (!file) return
      const ext = (file.name.split('.').pop() ?? '').toLowerCase()
      if (!(ALLOWED_UPLOAD_EXTS as readonly string[]).includes(ext)) {
        notify('只支持 jpg / jpeg / png')
        return
      }
      try {
        // ① 10 MB 上限：在**发起票据请求之前**拦（早反馈、零请求）；写在 try 里是为了仍走 finally 移除临时 input。
        if (file.size > MAX_USER_MEDIA_BYTES) {
          notify(MEDIA_TOO_LARGE_TEXT)
          return
        }
        // ② 压缩：头像用既有四档；收款码 `max(四档, 0.8)`（给人扫的，压狠了扫不出来）。
        //    两者都**不改尺寸**；压缩失败原样返回同一个 `File`。⛔ 压缩实现只有一份，这里不另写。
        const prepared = await compressImage(file, mediaQuality(kind, file.size))
        // ③ 票据的 `ext` 必须用**压缩后**的形态（成功一律 JPEG ⇒ jpg）；⛔ 不拿原扩展名签 JPEG 字节。
        const ticket = await requestMediaTicket(kind, userMediaTicketExt(file, prepared))
        await putFileToStorage(ticket.upload_url, prepared)
        await claimMedia(kind, ticket.key)
        // 顶栏头像同步：**头像认领成功后**通知壳（另一个文档）重取会话用户。
        // ⚠️ 只有头像走这条；消息类型用**两端共用的常量**（⛔ 不再写字面量）；本页不监听 ⇒ 卸载无需摘。
        if (kind === 'avatar') {
          window.parent.postMessage(
            { type: AVATAR_UPDATED_MESSAGE_TYPE },
            window.location.origin,
          )
        }
        notify('上传成功')
        // ⚠️ **只刷新该刷新的那一格**（整页 reload 会把用户正在填的 7 个输入框静默丢掉）。
        // ⚠️ 刷新这一跳单独 try/catch：认领已经成功 ⇒ 刷新失败**绝不能**盖掉「上传成功」——刻意静默。
        try {
          const fresh = await fetchMyProfile()
          if (kind === 'avatar') {
            if (fresh.avatar_url) {
              const avatar = root.querySelector<HTMLImageElement>(AVATAR)
              if (avatar) avatar.src = fresh.avatar_url
            }
          } else {
            // ⭐ 收款码：认领成功后**当场**把那一格变成「已上传」（以前只有头像会刷、传完看不出变化）。
            //    ⛔ 同样不刷新整页（7 个输入框会被静默丢掉）。
            renderWechatQr(root, fresh.wechat_qr_url)
          }
        } catch {
          // 刻意留空：刷新失败不改成功提示、不重试、不上抛（原因见上）。
        }
      } catch (error) {
        notify(error instanceof Error ? error.message : '上传失败')
      } finally {
        input.remove()
      }
    })()
  })
  input.click()
}

/**
 * 「是否实名」两态（**纯函数**：两态都显式写，输出只由入参决定）。
 *
 * ⭐ 现口径（业务方真值截图）：`true`（已实名）态的三个值——图标 **`layui-icon-vercode`**（绿盾）、
 * 色值 **`#16baaa`**（= layui 自带 `.layui-bg-green` 的同源值，不是新引入的第三种绿）、
 * 文案 **「已通过实名认证」**。⛔ 三个值**缺一不可**：只改一个就是半新半旧的样子。
 *
 * ⭐ 第三参 `enabled` = 「点击进行实名认证」的**显示条件**：`enabled && !isRealName` ⇒ 显示，
 * 其余一律隐藏。⚠️「已实名也隐藏」是**必须的**（否则两颗同时出现、自相矛盾）。
 * ⛔ 证据等级 = **推断**（对标语料里没有这颗按钮的显示条件代码）：按对标自己那句配置文案
 * 「只有启用了子账号那边才能发起实名认证」推断为「启用时才显示」；若业务方见过别的样子，改这一处即可。
 */
export function renderRealName(
  root: HTMLElement,
  isRealName: boolean,
  enabled: boolean,
): void {
  const item = itemByLabel(root, '是否实名')
  if (!item) return
  const icon = item.querySelector<HTMLElement>('i.layui-icon')
  const text = item.querySelector<HTMLElement>('.layui-input-inline span')
  // ⚠️ `#ff5722` 是**证据件里「未实名」的原值**：`false` 那支写成它，是为了让本函数能把
  // `true` 改回证据件原样（保存后服务端若回 `false`）。⛔ 只写 `true` 分支的实现**回不去**
  //（那正是「verified 用户被静默显示成未实名」的反方向缺陷）。
  const color = isRealName ? '#16baaa' : '#ff5722'
  if (icon) {
    icon.classList.toggle('layui-icon-tips-fill', !isRealName)
    icon.classList.toggle('layui-icon-vercode', isRealName)
    icon.style.color = color
  }
  if (text) {
    text.textContent = isRealName ? REALNAME_PASS_TEXT : '未实名'
    text.style.color = color
  }
  // ⭐ 那颗按钮的**显示条件**（证据等级 = 推断）：从**容器**上删/加类（⛔ 不动证据件、⛔ 不改按钮本身）。
  const container = realNameContainer(item)
  container?.classList.toggle('layui-hide', !(enabled && !isRealName))
}

/**
 * 「是否实名」那一格里，`.toCer` 的**容器**。
 * ⛔ 不用 `:has()` 一类的现代选择器：定位从 `.toCer` 自己**往上走**，老浏览器也照样工作；
 * ⛔ 也不按 `layui-form-mid` 在全页抓（不止一个）。找不到就回 `null`（证据件改版时**不抛异常**）。
 */
function realNameContainer(item: HTMLElement): HTMLElement | null {
  const button = item.querySelector<HTMLElement>(TO_CER_SELECTOR)
  const parent = button?.parentElement ?? null
  return parent?.classList.contains(TO_CER_CONTAINER_CLASS) ? parent : null
}

/** 游戏权限那一格**重建**（spec §6.4）：证据件里那 14 行是**对标租户**的游戏。 */
export function renderGames(
  root: HTMLElement,
  all: readonly ConfigRow[],
  mine: readonly number[],
  form: { render: (type?: string) => void },
): void {
  const block = itemByLabel(root, '游戏权限')?.querySelector<HTMLElement>('.layui-input-block')
  if (!block) return
  const owned = new Set(mine)
  const allChecked = all.length > 0 && all.every((game) => owned.has(game.id))
  // 类名 / `style` / `lay-skin` / `name` 逐字沿用证据件的形状，只换数据。
  const cell = (title: string, checked: boolean): string =>
    '<div class="layui-inline" style="width:110px; margin-top:0px">' +
    `<input type="checkbox" disabled value="${escapeHtml(title)}" name="gamelist" ` +
    `lay-skin="primary" title="${escapeHtml(title)}"${checked ? ' checked=""' : ''}></div>`
  block.innerHTML =
    cell('全部游戏', allChecked) + all.map((g) => cell(g.name, owned.has(g.id))).join('')
  form.render('checkbox') // `lay-skin="primary"` 必须重渲，否则复选框不出现
}

/**
 * 实名核验弹层的内容（纯函数、无副作用 ⇒ 可单独测）。
 * 文案**逐字照抄证据件**那块隐藏表单（提示句 / 「真实姓名」/「身份证号」/「开始认证」）；
 * ⚠️ 只有一行是我方新增：`REALNAME_BILLING_NOTICE`（计费提示），排在按钮上方、输入框下方
 * ——用户填完号码、手指落在按钮之前一定会看到。
 */
function realNameCertLayerHtml(): string {
  return (
    '<div class="layui-form-pane" style="padding:10px 8px">' +
    // ⚠️ 那句 `-&gt;` 一类实体在证据件里没有；这里逐字照抄原文（含标点）。
    '<blockquote class="layui-elem-quote"><span>请填写本人真实信息，' +
    '虚假信息将可能导致商家无法给你结算，只支持中国大陆身份</span></blockquote>' +
    '<div class="layui-form-item">' +
    '<label class="layui-form-label">真实姓名</label>' +
    '<div class="layui-input-block">' +
    `<input type="text" id="${REALNAME_NAME_INPUT_ID}" name="certName" autocomplete="off" ` +
    `placeholder="您的真实姓名" class="layui-input"></div></div>` +
    '<div class="layui-form-item">' +
    '<label class="layui-form-label">身份证号</label>' +
    '<div class="layui-input-block">' +
    `<input type="text" id="${REALNAME_SID_INPUT_ID}" name="certSid" autocomplete="off" ` +
    `placeholder="您的身份证号" class="layui-input"></div></div>` +
    // ⭐ 提交前的计费提示（`color:#ff5722` = 本页「未实名」那一格的同一个强调色）。
    `<div style="margin:6px 0 0 10px;color:#ff5722" data-realname-billing-notice>` +
    `${REALNAME_BILLING_NOTICE}</div>` +
    '<div class="layui-form-item" style="margin-top:14px">' +
    '<div class="layui-input-block">' +
    `<div class="layui-btn layui-btn-info w-lg" id="${REALNAME_SUBMIT_BUTTON_ID}">开始认证</div>` +
    '</div></div>' +
    // 结果那一行：**空着出厂**（还没提交过），提交后由 `setRealNameResult` 写。
    `<div id="${REALNAME_RESULT_ID}" style="margin:6px 0 0 10px;font-weight:600"></div>` +
    '</div>'
  )
}

export async function mount(root: HTMLElement, _ctx: IslandContext): Promise<() => void> {
  document.title = PAGE_TITLE

  // ① 先挂样式（`layui.css` 是 bootstrapIsland 运行期插的 `<link>`）——顺序反了会 FOUC（E-0061 陷阱③）
  const layui = await bootstrapIsland(['form', 'layer'])
  const form = layui.form as unknown as {
    render: (type?: string) => void
    on: (event: string, cb: (obj: unknown) => void) => void
  }
  const layer = layui.layer as Record<string, unknown>
  const msg = (text: string): void => islandMsg(layer, text)
    /**
   * 绑定块专用的错误提示：**6000ms**（⛔ 不是 `islandMsg` 的 1500ms）——
   * 检测不过时后端把缺项**逐条点名**，那一句很长、还要照着它去改权限；1500ms 一闪而过等于没提示。
   * ⚠️ E-0061 陷阱②：以 `(` 开头的语句**前置分号**。
     */
  const bindingErr = (text: string): void => {
    ;(layer.msg as (t: string, o?: unknown) => void)(text, { time: BINDING_ERROR_MS })
  }

  // ② 再塞标记，③ 然后才 `initIslandElements`（页签绑定**必须**在标记之后）
  root.innerHTML = await fetchArtifactHtml(ARTIFACT_URL)
  initIslandElements(layui)

  // ⭐ ④ 追加「下家鸡蛋铺绑定」块（⛔ 不改证据件）。**必须在 `form.render()` 之前**：
  //    启用开关是 `lay-skin="switch"` 的复选框，不重渲就不出现（那会让「开关」这一格静默消失）。
  injectMemberBindingBlock(root)
    /* ⭐ 「对接方式」下拉的切换事件。
     *
     * ⚠️ 事件名是 layui 的既定路径 `select(<lay-filter>)`。
     * ⚠️ 回调里取 `obj.value`（**刚选中的值**）——⛔ 不回头读 `<select>.value`
     *    （那要依赖「layui 已经先写好原 select」这个没写进文档的顺序）。
     * ⚠️ 注册必须排在**标记注入之后**（回调里要在真 DOM 上找那几格）。
     */
  ;(form.on as (event: string, cb: (obj: unknown) => void) => void)(
    `select(${MEMBER_MODE_SELECT_ID})`,
    (obj: unknown) => {
      const value = (obj as { value?: unknown }).value
      applyMemberMode(root, typeof value === 'string' ? value : '')
    },
  )
  // ⭐ 同一次把证据件自带的那条只读红字删掉（业务方裁定）。⛔ 证据件一个字都不动：
  //    删的是**运行期 DOM**里的那个 blockquote。必须在标记注入之后。
  removeReadonlyBindingNote(root)
  // ⑤ 它的取数也是异步的 ⇒ 与「基本资料」同一口径先上锁（窗口里打进去的字会被回填覆盖）。
  setMemberBindingLock(root, true)

  // ⚠️ 这里原本还有一块「上家鸡蛋铺绑定」——先搬到「绑定上家系统设置」页、同日被业务方裁定
  //    整块删掉。⛔ 不要再往本页加回任何上家那一套标记。

  // 登录账号只读（有意偏离，spec §3-D2）
  input(root, LOGIN_ACC)?.setAttribute('disabled', 'disabled')

  // ④ **取数之前先上锁**（**产品缺陷的修复，不是测试手段**）：回填是异步的，标记一注入这些框
  //    就已可编辑 ⇒ 先打字会被服务端旧值覆盖、再保存就把旧值写回去。⛔ 取数失败**保持** disabled
  //    （拿不到旧值就不该在一个会被覆盖的表单上保存）+ 照旧弹 msg；成功才解锁。
  setLoadingLock(root, true)

  let games: ConfigRow[] = []

  const onClick = (event: Event): void => {
    const target = event.target as HTMLElement | null
    if (!target || typeof target.closest !== 'function') return
    const stop = (): void => {
      event.preventDefault()
      event.stopPropagation()
    }
    if (target.closest(SAVE_BUTTON)) {
      stop()
      void saveProfile()
      return
    }
    if (target.closest(SAVE_PWD_BUTTON)) {
      stop()
      void savePassword()
      return
    }
    // ⭐ 「下家鸡蛋铺绑定」两颗按钮。走**捕获态 click 委托**：
    //    ⛔ 不用 `form.on('submit(...)')`——那会对**整页**跑 `validate()`，而 `loginAcc`
    //    带必填校验且被有意置只读。
    if (target.closest(BINDING_SELECTORS.save)) {
      stop()
      void saveMemberBinding()
      return
    }
    if (target.closest(BINDING_SELECTORS.test)) {
      stop()
      void testMemberBinding()
      return
    }
    // ⭐ 「回调地址」那一格的复制按钮：地址是只读的，用户**只能**靠这一颗把它
    //    带走（填到鸡蛋铺那侧的「开放平台 → 回调通知地址」里）。
    if (target.closest(`#${MEMBER_CALLBACK_COPY_BUTTON_ID}`)) {
      stop()
      copyMemberCallbackUrl()
      return
    }
    // ⭐ 「上家鸡蛋铺绑定」那三颗按钮**已随块删掉**（D-0189）⇒ 本页与上家那一套没有任何分支。
    // 「已上传」那颗：点它开层看那张收款码。⛔ 未上传时它是 `display:none`，
    // 这条分支到不了；`openWechatQr` 自己还有一道「没有链接就不开空白层」的闸。
    if (target.closest(WECHAT_QR_LOOK)) {
      stop()
      openWechatQr()
      return
    }
    const media = MEDIA_SELECTORS
      .map(([selector, kind]) => [target.closest<HTMLElement>(selector), kind] as const)
      .find(([el]) => el !== null)
    if (media) {
      stop()
      void handleMediaUpload(media[1], root, msg)
      return
    }
    // ⭐ 实名认证：两颗从「尚未开放」改成**开真弹层**。⚠️ 必须排在 `notOpen` 那一组**之前**，
    //    且这两颗已从 `NOT_OPEN_SELECTORS` 里摘掉——两处都做才叫接真（只摘一处 = 自相矛盾）。
    if (target.closest(TO_CER_SELECTOR) || target.closest(START_CERT_SELECTOR)) {
      stop()
      openRealNameCert()
      return
    }
    const notOpen = NOT_OPEN_SELECTORS
      .map((selector) => target.closest<HTMLElement>(selector))
      .find((el): el is HTMLElement => el !== null)
    if (notOpen) {
      stop()
      msg(`「${(notOpen.textContent ?? '').trim() || '该功能'}」${NOT_OPEN}`)
    }
    // ⚠️ 其余可点元素**一律不拦**：页签切换必须照旧工作（spec §6.5）。
  }
  // 捕获阶段（layui 自己的委托在冒泡阶段）。⚠️ **必须在 await 之前挂**：挂晚了的话，加载窗口内
  // 点「确认更改」（修改密码页签**没有**禁用态）会**什么都不发生、也没有提示**；挂早之后这一支
  // 照常走到 `savePassword()`、由后端回「原密码不正确」——两条路都不是静默的。
  root.addEventListener('click', onClick, true)

    /**
   * 打开「微信收款码」看图层（对标 `myinfo.js` 的层参数；文案逐字、层参数与层内 DOM 用**本页自己的写法**）。
   *
   * ⚠️ 短时链接的健壮性（签名链接 300s 就过期）：图 `onerror` ⇒ **重取一次**资料换新链接、
   * 同一张图上再试一次；仍失败 ⇒ **关掉这个层** + 一句**固定**提示。
   * ⛔ 不留一个空白层；⛔ 不把服务端的技术错误串给用户看。
     */
  function openWechatQr(): void {
    const src = input(root, WECHAT_QR_URL_INPUT)?.value ?? ''
    if (src === '') {
      // 防御（正常**不可达**：未上传时那颗按钮是 `display:none`，点不到）——真被点到也不开空白层。
      msg(WECHAT_QR_FAILED_TEXT)
      return
    }
    /** 层索引：重试用尽要关掉它，所以在闭包里留着。 */
    let index = 0
    /** **这一层**是否已经换过一次链接（⛔ 不能放进监听器的闭包参数：那会永远停在初始值）。 */
    let retried = false
    const giveUp = (): void => {
      ;(layer.close as (i: number) => void)(index)
      msg(WECHAT_QR_FAILED_TEXT)
    }
    const openImage = (url: string): void => {
      index = (layer.open as (o: unknown) => number)({
        type: 1,
        title: WECHAT_QR_LAYER_TITLE,
        shadeClose: true,
        area: ['500px', '500px'],
        content:
          `<div style="padding:10px;text-align:center">` +
          `<img id="${WECHAT_QR_IMG_ID}" src="${escapeHtml(url)}" alt="微信收款码"` +
          ` style="max-width:480px;max-height:480px"></div>`,
        // layui 的实参是 `(layero, index)`，且 `layero` 是 **jQuery 对象**（同 `order-orderlist.ts:3076`）。
        success: (layero?: { find?: (s: string) => { 0?: HTMLImageElement } }) => {
          const img = layero?.find?.(`#${WECHAT_QR_IMG_ID}`)?.[0]
          if (!img) return
          img.addEventListener('error', () => {
            // 已经换过一次还是打不开 ⇒ 收场（关层 + 固定提示）。
            if (retried) {
              giveUp()
              return
            }
            // 先记下「换过了」再重取：重取过程中再报一次错也走收场那一支（不会无限重试）。
            retried = true
            void (async () => {
              try {
                const fresh = await fetchMyProfile()
                const next = fresh.wechat_qr_url
                if (typeof next !== 'string' || next === '') {
                  giveUp()
                  return
                }
                // 顺手把那一格也换成新链接（下次点开用的就是它）。
                renderWechatQr(root, next)
                img.src = next
              } catch {
                giveUp()
              }
            })()
          })
        },
      })
    }
    openImage(src)
  }

    /**
   * ⭐ 实名认证的真弹层。
   * 形状：层里的两块 + 「开始认证」+ 顶部提示句**逐字取自证据件自己那个隐藏块**（文案不是我方新写的）；
   * ⛔ 证据件一个字不动（那一块仍在页面上、`display:none`）。
   * 三件必须记住的事：① 提交前必须让用户看见计费提示（**不一致也算一次**）；② 结果要**留在屏幕上**
   * （⛔ 不用 1500ms 就消失的浮层——这是花了一次钱的动作）；③ 失败**按业务码分开发话**
   * （`realNameErrorText`），⛔ 不用一句「核验失败」把"该找谁"抹掉。
     */
  function openRealNameCert(): void {
    ;(layer.open as (o: unknown) => number)({
      type: 1,
      title: REALNAME_LAYER_TITLE,
      shadeClose: true,
      area: ['560px', '400px'],
      content: realNameCertLayerHtml(),
      // layui 的实参是 `(layero, index)`；`layero` 是 **jQuery 对象**（同本页 `openWechatQr`）。
      success: (layero?: { find?: (s: string) => { 0?: HTMLElement } }) => {
        const button = layero?.find?.(`#${REALNAME_SUBMIT_BUTTON_ID}`)?.[0]
        if (!button) return
        // ⛔ 直接挂在**层里那颗按钮**上，不挂文档级委托：层不在 `root` 里 ⇒ `root` 的委托
        //    根本收不到层里的点击。幂等：内容每次开层重新生成（`dataset` 防同一节点复用）。
        if (button.dataset.rncBound === '1') return
        button.dataset.rncBound = '1'
        button.addEventListener('click', () => {
          void submitRealNameCert()
        })
      },
    })
  }

    /**
   * 提交一次核验：读层里那两个输入框 → `POST /api/auth/realname/verify/` → 结果上屏。
   * ⚠️ **前端只做"填没填全"这一道早挡**（权威判据在后端：格式、校验位、出生日期一律后端判）。
   * ⛔ 不在前端抄一份身份证正则——那是**第二个出处**，两边迟早不一致。
     */
  async function submitRealNameCert(): Promise<void> {
    // ⚠️ `getElementById` 回来的是 `HTMLElement`（没有 `value`）⇒ 按本页既有写法用
    //    `querySelector<HTMLInputElement>` 取：层里就是 `input` 本身（id 挂在 `input` 上）。
    const name =
      document.querySelector<HTMLInputElement>(`#${REALNAME_NAME_INPUT_ID}`)?.value.trim() ?? ''
    const idCardNo =
      document.querySelector<HTMLInputElement>(`#${REALNAME_SID_INPUT_ID}`)?.value.trim() ?? ''
    if (name === '' || idCardNo === '') {
      setRealNameResult(REALNAME_MISSING_INPUT_TEXT)
      return
    }
    try {
      const result = await verifyRealName({ name, id_card_no: idCardNo })
      if (result.result === 'pass') {
        // ⭐ 三个值一起改（`renderRealName` 纯函数）：页面当场变成「已通过实名认证」。
        // ⚠️ 第三参沿用**取数时那一份**的开关（不是拿核验结果猜）。
        renderRealName(root, true, realNameEnabled)
        // 余量沿用后端这次返回的读数（`null` = 读不到 ⇒ 不假装是 0，`E-0021`）。
        setRealNameResult(
          result.remaining === null
            ? REALNAME_PASS_TEXT
            : `${REALNAME_PASS_TEXT}（剩余核验次数 ${result.remaining} 次）`,
        )
        return
      }
      // `result: "fail"` —— 核验确实做了、这次也照样扣了。⛔ 页面状态**不动**（没通过就是没通过）。
      setRealNameResult(
        result.remaining === null
          ? REALNAME_FAIL_TEXT
          : `${REALNAME_FAIL_TEXT}（剩余核验次数 ${result.remaining} 次）`,
      )
    } catch (e) {
      // 业务失败按码给人话；其余沿用本页既有的 `errorText`（⛔ 不改写成「核验失败」）。
      setRealNameResult(realNameErrorText(e))
    }
  }

  /** 把一行结果写进层里那块 `#rncCertResult`（层不在页面上时静默 no-op）。 */
  function setRealNameResult(text: string): void {
    const slot = document.getElementById(REALNAME_RESULT_ID)
    if (slot) slot.textContent = text
  }

    /**
   * 「订单来源名」下拉的**对方字典**状态：初始「还没拉到过」。
   * ⚠️ 住在 `mount` 的闭包里而不是模块级：模块级可变对象会让**上一次挂载的字典**漏到下一次。
     */
  const memberSourceDict = emptyMemberSourceDict()

    /**
   * 块自己的**那一行**（`null` = 还没绑定 / 这次没读到）。
   * ⚠️ 必须住在 `mount` 的闭包里：「②→① 切回来要不要补 `mode`」读它——模块级会让
   * **上一次挂载的那一行**漏到下一次（症状：切回①式时静默不补 `mode`）。
     */
  let bindingRow: JidanpuMemberBindingRow | null = null

  const [profile, , binding] = await Promise.all([
    fetchMyProfile().catch((e: unknown) => {
      msg(errorText(e)) // 失败只提示：页面骨架照常显示（spec §6.1-5）；**且不解锁**（见上）
      return null
    }),
    fetchAllGames()
      .then((rows) => {
        games = rows
      })
      .catch(() => {
        games = []
      }),
    // ⭐ 绑定块自己的取数（与上面两条**并发**）。⚠️ 成功回 `null` 是**合法结果**（还没绑定）⇒
    //    必须与「失败」分开：失败时**不解锁**（拿不到库里的配置就不该放行一个会覆盖它的表单）。
    fetchMemberJidanpuBinding().then(
      (row) => ({ ok: true as const, row }),
      (error: unknown) => ({ ok: false as const, message: errorText(error) }),
    ),
  ])
    /**
   * ⭐ 本租户**是否启用实名认证**（载荷里 `realname.enabled`）。
   * ⛔ **不用真值判断偷懒**（`!!profile?.realname`）：那会把「没有这个键」与「有键但 false」混成一件事；
   * 也⛔ 不默认成 `true`——拿不到就**不显示**那颗按钮（fail-closed）。
     */
  const realNameEnabled = profile?.realname?.enabled ?? false
  if (profile) {
    fillProfile(root, profile)
    renderRealName(root, profile.is_real_name, realNameEnabled)
    setLoadingLock(root, false) // 回填落地 ⇒ 解锁（只在成功这一支）
  }
  // ⭐ 绑定块：回填 + 解锁（只有成功那一支解锁，同上一行那条口径）。
  if (binding.ok) {
    bindingRow = binding.row
    fillMemberBinding(root, binding.row, memberSourceDict)
    setMemberBindingLock(root, false)
  } else {
    bindingErr(binding.message)
  }
  // ⛔ **这一格必须在 `if (profile)` 之外重建**（产品缺陷修复）：
  // 证据件里那 14 行全部勾选（它们是**对标那个租户**的游戏权限）；取数失败时若跳过重建，
  // `form.render()` 就把**别的租户的游戏**当成用户自己的权限渲染上屏——那是对权限的**假陈述**。
  // 拿不到资料时按「我们自己的游戏库、一个都不勾」渲染（没有数据 ≠ 有权限）。
  renderGames(root, games, profile?.games ?? [], form)
  form.render()

  async function saveProfile(): Promise<void> {
    const payload: Partial<MyProfilePatch> = {}
    for (const [key, selector] of Object.entries(EDITABLE_SELECTORS)) {
      const el = input(root, selector)
      if (el) payload[key as keyof MyProfilePatch] = el.value
    }
    try {
      const updated = await patchMyProfile(payload)
      fillProfile(root, updated)
      // 开关取**这次响应**里的那份（与首屏同一读法；PATCH 回的也是同一份 `profile` 载荷）。
      renderRealName(root, updated.is_real_name, updated.realname?.enabled ?? false)
      renderGames(root, games, updated.games, form)
      msg('保存成功')
    } catch (e) {
      msg(errorText(e)) // 只回显服务端 msg，前端不造文案（D8）
    }
  }

    /**
   * ⭐ 「下家鸡蛋铺绑定」——保存（按**对接方式**分两支）。
   * 前端只做**早挡**（地址必填；①式要账号、②式要 `client_id`，密钥只在从没存过时必填）
   * ——权威判据一律在后端序列化器。
   * `enabled=True` 时后端**先同步跑一次检测**：不过就**一行都不落**并逐条点名 ⇒ 原样显示它，
   * ⛔ 不改写成「保存失败」（用户不知道要去改什么）。
   * 成功后再回填一次（后端会归一化地址、把空来源名落成默认名 ⇒ 不回填看起来像「没保存上」）。
   * ⭐ ②式的「启用」开关**不置灰、不由页面判能不能启用**：一律以**这次 PUT 的返回**为准。
     */
  async function saveMemberBinding(): Promise<void> {
    const mode = readMemberMode(root)
    const payload = mode === MEMBER_MODE_OPENAPI
      ? readMemberOpenApiPayload(root)
      : readMemberBindingPayload(root)
    if (payload.jidanpu_base_url === '') {
      msg(MEMBER_MISSING_TEXT)
      return
    }
    if (mode === MEMBER_MODE_OPENAPI) {
      if ((payload.client_id ?? '') === '') {
        msg(MEMBER_CLIENT_ID_REQUIRED_TEXT)
        return
      }
      // ⚠️ 密钥**只在从没存过**时必填（空串 = 不改是正常用法：每次改地址 / 关开关都逼用户
      //    重填密钥，正是 spec §11.2 明写要避免的）。
      if (bindingRow?.client_secret_set !== true && (payload.client_secret ?? '') === '') {
        msg(MEMBER_CLIENT_SECRET_REQUIRED_TEXT)
        return
      }
    } else if (payload.account === '') {
      msg(MEMBER_MISSING_TEXT)
      return
    }
    try {
      const saved = await saveMemberJidanpuBinding(
        // ⭐ **②→① 切回来**那一跳补一个 `mode: 'password'`（理由见 `memberPasswordModePatch`）：
        //    不补 ⇒ 后端按**库里那一格**（仍是 `openapi`）走 ⇒ 页面说保存成功，而这一套新填的
        //    ①式凭据**永远不会被用**（静默）。库里本来就是①式时**一个键都不加**（载荷逐字不变）。
        //    ⛔ **②式那一支绝不能补**：会**盖掉**②式载荷自己的 `mode: 'openapi'` ⇒ 后端按①式判、
        //    用户看到「鸡蛋铺账号不能为空」，而这次请求是②式的、填的东西一个都没错。
        { ...payload, ...memberPasswordModePatch(bindingRow?.mode ?? '', mode) },
      )
      // ⭐ 用**响应那一行**刷新本页状态 ⇒ 刚保存的改动立刻反映到回调地址那一格；
      //    第一次保存时这正是用户**第一次**看到那个地址的时刻。
      bindingRow = saved
      // ⚠️ 带上**已经拉到的那份字典**：否则回填会把下拉退回只剩一项（看起来像「字典白拉了」）。
      fillMemberBinding(root, saved, memberSourceDict)
      // 开关的勾选态是程序化改的 ⇒ 必须重渲，否则 layui 那个开关还停在旧样子。
      form.render()
      msg('保存成功')
    } catch (e) {
      bindingErr(errorText(e))
    }
  }

    /**
   * 「回调地址」那一格的复制（只读输入 ⇒ 这一颗是用户带走那串地址的唯一出口）。
   * ⚠️ **缺 `navigator.clipboard` 不是静默失败**（jsdom / 非安全上下文里它不存在）⇒ 两条路都给提示。
     */
  function copyMemberCallbackUrl(): void {
    const text = root.querySelector<HTMLInputElement>(`#${MEMBER_CALLBACK_URL_INPUT_ID}`)?.value ?? ''
    if (text === '') {
      // 防御（正常**不可达**：地址为空时那一行整个不显示，点不到）——真被点到也不静默。
      msg(MEMBER_CALLBACK_COPY_FAIL_TEXT)
      return
    }
    const clipboard: { writeText?: (value: string) => Promise<void> } | undefined =
      navigator.clipboard
    if (!clipboard || typeof clipboard.writeText !== 'function') {
      msg(MEMBER_CALLBACK_COPY_FAIL_TEXT)
      return
    }
    const writeText = clipboard.writeText
    try {
      void Promise.resolve(writeText.call(clipboard, text)).then(
        () => msg(MEMBER_CALLBACK_COPY_OK_TEXT),
        () => msg(MEMBER_CALLBACK_COPY_FAIL_TEXT),
      )
    } catch {
      msg(MEMBER_CALLBACK_COPY_FAIL_TEXT)
    }
  }

    /**
   * 「测试连接」：跑一遍绑定检测，把结论说出来 + 用响应里的字典填「订单来源名」下拉。
   * ⚠️ `ok === false` 是**一条正常结论**（信封 `code` 恒 0），不是请求失败：`messages` 是后端
   * 逐条点名的人话 ⇒ 必须显示出来，⛔ 不能吞掉说成「测试失败」。
   * ⚠️ ①式带**密码框里此刻填的**值（未保存也能查）与**下拉里此刻选的**来源名；⛔ 地址 / 账号
   * **仍然**不传（后端拒收）。密码只用于**本次建连**：⛔ 不落库、⛔ 不回显。
   * ⭐ **②式那一支**两个参数都不传（空体）——它测的是**库里那一行**，结论是**那张逐条表**。
     */
  async function testMemberBinding(): Promise<void> {
    const mode = readMemberMode(root)
    try {
      const isOpenapi = mode === MEMBER_MODE_OPENAPI
      const password = isOpenapi
        ? ''
        : input(root, BINDING_SELECTORS.password)?.value ?? ''
      // 空串 ⇒ 默认名（下拉里永远有值，这里是防御；与 `renderMemberSourceOptions` 同口径）。
      const sourceName = isOpenapi
        ? ''
        : (root.querySelector<HTMLSelectElement>(BINDING_SELECTORS.sourceName)?.value ?? '')
            .trim() || SOURCE_NAME_DEFAULT
      const result = await testMemberJidanpuConnection(password, sourceName)
      // ⭐ 字典来自这次响应 ⇒ 下拉立刻可选、提示位立刻更新。
      //    ⚠️ `source_options_ok` **必须**收下：它是「字典没读到」与「对方真的没有」的**唯一**判据
      //    ——少了它，一次登录失败会让提示位喊「对方已无此来源」。
      //    ⚠️ **②式那一支的响应里根本没有这三个键** ⇒ 只在**真的有 `source_options`** 时才动字典
      //    （否则会把「这一路没拉字典」说成「没读到对方的来源名单」，而用户根本没在①式上点过测试）。
      if (result.source_options !== undefined) {
        memberSourceDict.options = result.source_options
        memberSourceDict.ok = result.source_options_ok === true
      }
      renderMemberSourceOptions(root, sourceName, memberSourceDict)
      // 下拉是 layui 的影子 DOM：不重渲用户看到的还是旧选项（与开关那一格同一处置）。
      form.render()
      // ⭐ ②式：把那张**逐条权限表**摆出来（全部由后端的 `checks` / `messages` 决定，⛔ 前端不另判一套）。
      if (isOpenapi) {
        renderMemberTestResult(root, memberTestResultHtml(result))
        applyMemberMode(root, mode)
      }
      if (result.ok) {
        msg('连接成功')
        return
      }
      // ⚠️ ②式下那张表已经逐条说清了 ⇒ 不再叠一句笼统的话；①式照旧把后端点名的几句原样弹出来。
      if (isOpenapi) return
      bindingErr(result.messages.join('；') || '测试连接没通过，请检查上面填的地址与账号')
    } catch (e) {
      bindingErr(errorText(e))
    }
  }

  async function savePassword(): Promise<void> {
    const [oldEl, newEl, confirmEl] = PWD_INPUTS.map((s) => input(root, s))
    const oldPassword = oldEl?.value ?? ''
    const newPassword = newEl?.value ?? ''
    const confirmPassword = confirmEl?.value ?? ''
    // 即时校验只省一次往返；**权威判据在后端**（同文案，见 spec §5.3）
    if (newPassword !== confirmPassword) {
      msg('两次输入的新密码不一致')
      return
    }
    try {
      await changePassword({
        old_password: oldPassword, new_password: newPassword, confirm_password: confirmPassword,
      })
      for (const el of [oldEl, newEl, confirmEl]) if (el) el.value = ''
      msg('修改成功')
    } catch (e) {
      msg(errorText(e))
    }
  }

  /** `?tab=password` 深链。 */
  function activatePasswordTab(): void {
    const tab = root.querySelector<HTMLElement>(`#${PWD_TAB_ID}`)
    if (!tab) return
    tab.click() // 真 layui：`initIslandElements` 的委托会切面板
    const panels = Array.from(root.querySelectorAll<HTMLElement>('.layui-tab-item'))
    const panel = panels[2]
    if (!panel || panel.classList.contains('layui-show')) return
    // 兜底：程序化 click 没被接管时（单测里 `initIslandElements` 是替身）直接切类。
    root.querySelectorAll<HTMLElement>('.layui-tab-title li')
      .forEach((li) => li.classList.remove('layui-this'))
    panels.forEach((item) => item.classList.remove('layui-show'))
    tab.classList.add('layui-this')
    panel.classList.add('layui-show')
  }

  // ⛔ 点击委托**不在这里**挂：已上移到取数之前（见上面那段 `root.addEventListener`）。

  if (new URLSearchParams(window.location.search).get('tab') === 'password') {
    activatePasswordTab()
  }

  return () => {
    root.removeEventListener('click', onClick, true)
    root.innerHTML = ''
  }
}

export default { mount } satisfies IslandPage
