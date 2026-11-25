/**
 * 手机端「我的」标签四页的用户可见文案 —— **逐字取自桌面 island 页**（spec 文案铁律：⛔ 自编，桌面有的提示手机端同句）。
 * ⚠️ 是**值的副本**而不是 import 桌面模块：那些常量住在 `layui-island/pages/*.ts`，import 会把整个 island 页（连同 layui 加载器）拖进手机端依赖图——红线「`m/` 下不引 layui」。只抄值并把出处钉在注释里；两侧 spec 各自按**字面值**断言（E-0024），任何一侧改字都会被它自己的 spec 抓住。
 */

/* ── 我的资料（桌面 `user-myinfo.ts` / `public/replica/user-myinfo.html`） ── */

/** 保存成功（桌面 `saveProfile` 成功 toast 逐字）。 */
export const PROFILE_SAVED_TEXT = '保存成功'

/** 媒体上传成功（桌面 `handleMediaUpload` 成功 toast 逐字）。 */
export const MEDIA_UPLOADED_TEXT = '上传成功'

/** 媒体 10MB 前置闸（桌面 `MEDIA_TOO_LARGE_TEXT` 逐字；后端在认领与直传路由两处是权威）。 */
export const MEDIA_TOO_LARGE_TEXT = '头像与收款码最大支持10MB，请压缩后重试'

/** 媒体扩展名早挡（桌面 `handleMediaUpload` 同句）。 */
export const MEDIA_BAD_EXT_TEXT = '只支持 jpg / jpeg / png'

/** 媒体上传失败且拿不到 `Error.message` 时的兜底（桌面同句）。 */
export const MEDIA_FAILED_TEXT = '上传失败'

/** 收款码两态（桌面 `WECHAT_QR_IDLE_TEXT` / `WECHAT_QR_REUPLOAD_TEXT` 逐字）。 */
export const WECHAT_QR_IDLE_TEXT = '上传收款码'
export const WECHAT_QR_REUPLOAD_TEXT = '重新上传'
/** 收款码层标题（桌面 `WECHAT_QR_LAYER_TITLE` 逐字）。 */
export const WECHAT_QR_LAYER_TITLE = '微信收款码'

/** 「是否实名」两态（桌面 `renderRealName` 逐字：`REALNAME_PASS_TEXT` / 「未实名」）。 */
export const REALNAME_PASS_TEXT = '已通过实名认证'
export const REALNAME_NOT_TEXT = '未实名'
/** 「点击进行实名认证」按钮文案（证据件 `.toCer` 的原文）。 */
export const REALNAME_START_BUTTON_TEXT = '点击进行实名认证'

/** 实名弹层顶部那句（证据件 `user-myinfo.html` L419-421 逐字）。 */
export const REALNAME_FORM_NOTICE =
  '请填写本人真实信息，虚假信息将可能导致商家无法给你结算，只支持中国大陆身份'
/** 弹层两个输入框的 label / placeholder（证据件 L424/L426、L431/L433 逐字）。 */
export const REALNAME_NAME_LABEL = '真实姓名'
export const REALNAME_NAME_PLACEHOLDER = '您的真实姓名'
export const REALNAME_SID_LABEL = '身份证号'
export const REALNAME_SID_PLACEHOLDER = '您的身份证号'
/** 提交按钮（证据件 L434 逐字）。 */
export const REALNAME_SUBMIT_TEXT = '开始认证'

/**
 * ⭐ 提交前的计费提示（桌面 `REALNAME_BILLING_NOTICE` **一字不许省**：厂商按「查得」计费，
 * 姓名与号码对不上也照样扣 1 次；不写这一句用户会以为「失败不扣」而反复试错耗光次数）。
 */
export const REALNAME_BILLING_NOTICE = '本次核验将消耗 1 次，无论核验结果一致与否'

/** 弹层两块没填全时的提示（桌面 `REALNAME_MISSING_INPUT_TEXT` 逐字，我方自拟那一半）。 */
export const REALNAME_MISSING_INPUT_TEXT = '请填写真实姓名与身份证号后再提交'

/** 核验没通过（后端 `result:"fail"`，这次也照样扣了）的提示（桌面 `REALNAME_FAIL_TEXT` 逐字）。 */
export const REALNAME_FAIL_TEXT = '实名认证未通过，请核对姓名与身份证号是否一致'

/** 号码本地就不合法（后端业务码 1000）的兜底（桌面 `REALNAME_INVALID_TEXT` 逐字）。 */
export const REALNAME_INVALID_TEXT = '身份证号格式不正确，请核对后重新提交'

/** 资料取数失败且拿不到 `Error.message` 时的兜底（桌面 `errorText` 同句）。 */
export const PROFILE_LOAD_FAILED_TEXT = '请求失败，请稍后重试'

/* ── 修改密码（后端 `apps/core/auth_views.py` 的四句权威文案 + 桌面成功 toast） ── */

/** 改密成功（桌面 `savePassword` 成功 toast 逐字）。 */
export const PASSWORD_CHANGED_TEXT = '修改成功'

/**
 * 前端早挡的三句（spec §3：规则照 D-0149⑥——长度 6–18、新旧不同、两次一致；**文案句式照后端** `auth_views.py` 的同三句，属「自拟沿用」、此处登记）。
 * ⛔ 权威判据在后端，前端这三道只是省往返；后端拒了照旧原样显示它的 msg。
 */
export const PASSWORD_LENGTH_TEXT = '新密码长度需为 6–18 位'
export const PASSWORD_CONFIRM_MISMATCH_TEXT = '两次输入的新密码不一致'
export const PASSWORD_SAME_AS_OLD_TEXT = '新密码不能与原密码相同'

/* ── 问题反馈（桌面 `feedback.ts::MESSAGES` 逐字） ── */

/** 提交成功（桌面 `MESSAGES.submitted` 逐字）。 */
export const FEEDBACK_SUBMITTED_TEXT = '反馈已提交，感谢反馈'
/** 内容没填就点提交（桌面 `MESSAGES.noContent` 逐字，与 placeholder 同句）。 */
export const FEEDBACK_NO_CONTENT_TEXT = '请描述你遇到的问题'
/** 列表空态（桌面 `MESSAGES.empty` 逐字）。 */
export const FEEDBACK_EMPTY_TEXT = '还没有反馈记录'
/** 失败兜底（桌面 `MESSAGES.failed` 逐字；后端有 msg 时原样显示它）。 */
export const FEEDBACK_FAILED_TEXT = '操作失败'
/** 内容字数上限（桌面 textarea 的 `maxlength="2000"`，值同源）。 */
export const FEEDBACK_CONTENT_MAX_LENGTH = 2000
/** 内容框 placeholder / label（桌面标记逐字）。 */
export const FEEDBACK_CONTENT_PLACEHOLDER = '请描述你遇到的问题'
export const FEEDBACK_CONTENT_LABEL = '问题描述'
/** 提交按钮（桌面标记逐字）。 */
export const FEEDBACK_SUBMIT_BUTTON_TEXT = '提交'
/** 列表区标题（桌面标记逐字）。 */
export const FEEDBACK_LIST_TITLE = '反馈记录'
