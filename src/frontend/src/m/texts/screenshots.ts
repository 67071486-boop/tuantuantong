/**
 * 手机端订单截图页的用户可见文案 —— **逐字取自桌面 island 页**（spec 文案铁律：⛔ 自编，桌面有的提示手机端同句）。
 * ⚠️ 是**值的副本**而不是 import：常量住在 `layui-island/pages/order-screenshots-*.ts`，import 会把整个 island 页（连同 layui 加载器）拖进手机端依赖图——红线「`m/` 下不引 layui」。只抄值并把出处钉在注释里；两侧 spec 各自按**字面值**断言（E-0024），任何一侧改字都会被它自己的 spec 抓住。
 */

/** 扩展名不在白名单（桌面 `order-screenshots-upload.ts::BAD_EXT_TEXT`，同后端 claim 的 msg）。 */
export const BAD_EXT_TEXT = '只支持 jpg / jpeg / png'

/** 每单 20 张超限（桌面 `order-screenshots-upload.ts::LIMIT_TEXT` 逐字；
 *  后端在签发与认领两处拦同一句——后端是权威，前端这句只是省往返的早反馈）。 */
export const LIMIT_TEXT = '单个订单最多上传20张截图，请先删除后再上传'

/** 原图超 18MiB 的过滤提示（桌面上传页同句，N 是调用方自己的计数）。 */
export function overOriginalText(n: number): string {
  return `已自动过滤${n}张18MB以上的图片`
}

/** 压缩后仍超 5MiB 的过滤提示（桌面上传页同句）。 */
export function overCompressedText(n: number): string {
  return `已自动过滤${n}张5MB以上的图片`
}

/** 一张都没传成且有「太大」被滤掉时的收尾提示（桌面上传页同句，数字 18 与后端上限一致）。 */
export const ALL_TOO_LARGE_TEXT = '图片都太大了，请把图片大小控制在单张18MB以内'

/** 上传请求失败且抛出的不是 `Error` 时的兜底（桌面上传页同句）。 */
export const FALLBACK_ERROR_TEXT = '上传失败，请稍后重试'

/** 删除确认（桌面 `order-screenshots-manage.ts::DELETE_CONFIRM_TEXT` 逐字；对标
 *  `picmange.js:27`，单删与批删同一句）。⛔ 一个标点都不许改。 */
export const DELETE_CONFIRM_TEXT = '确定要删除吗？提示：一旦删除，将不可恢复！'

/** 列表空态（M2 spec §3.1 点名「还没有截图」）。 */
export const EMPTY_TEXT = '还没有截图'
