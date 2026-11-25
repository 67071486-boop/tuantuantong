/**
 * 订单截图上传的**客户端分档压缩**（spec §3.2 / Global Constraints）；
 * 头像 / 微信收款码（`D-0153` ③）复用同一份实现，只换「上限」与「画质来源」两个入参。
 *
 * ⚠️ **它故意不放在 `layui-island/pages/` 下**：那个目录的 `*.ts` 会被
 * `layui-island/page.ts` 的 `import.meta.glob('./pages/*.ts')` 注册成一个 island 页面
 * （key = 文件名），而 `config/__tests__/replicaRoutes.spec.ts` 有一条「pages 下每个文件
 * 都要有映射表项」的守卫 ⇒ 共享零件放进去会让它红（同 `layui-island/layerHost.ts` 的理由）。
 *
 * 取值逐字取自对标 `assets_JS2019_Order_uploadpicnew.js`：
 *  - `:28` `maxSize = 18 * 1024 * 1024`（原图上限）
 *  - `:29` `compressMaxSize = 5 * 1024 * 1024`（压缩后上限）
 *  - `:58-70` 四档画质（同一份逻辑在粘贴那一支 `:152-161` 又抄了一遍）
 *
 * ⚠️ 前端压缩是**体验**、不是边界：18 MB / 10 MB / jpg|jpeg|png 的权威判据都在**后端**
 * （`core.storage.MAX_IMAGE_BYTES` / `MAX_USER_MEDIA_BYTES` + 魔数嗅探，spec §7
 * 「服务端取值一律照 §3.2」）。
 */

/** 原图上限 18 MB（`uploadpicnew.js:28`）。 */
export const MAX_UPLOAD_BYTES = 18 * 1024 * 1024

/** 压缩后上限 5 MB（`uploadpicnew.js:29`）。 */
export const COMPRESSED_MAX_BYTES = 5 * 1024 * 1024

/**
 * 头像 / 微信收款码的单张上限 **10 MB**（业务方裁定，`D-0153` ②）。
 *
 * ⚠️ **它是后端的权威值**（`apps/core` 的 `MAX_USER_MEDIA_BYTES`）在前端的**同值副本**，
 * 只为「发起票据请求之前就拦下」这一层早反馈；⛔ 不构成边界——后端在**认领**与**本机直传路由**
 * 两处各判一次（生产形态浏览器直传**不经过我们服务器**，签发端点拿不到文件大小）。
 * ⇒ 后端拒了要**原样显示它的 msg**（`request()` 抛出的 `Error.message` 就是它），
 * 业务方给的那句与下面页面里那句是**同一事实的两处字面值**，改数值要**同一次**改两处。
 */
export const MAX_USER_MEDIA_BYTES = 10 * 1024 * 1024

/**
 * 微信收款码的**画质下限**：0.8。
 *
 * ⚠️ **这是有意与头像不同**：收款码是**给人扫的**，JPEG 压狠了会扫不出来（`D-0153` ③）——
 * 头像压到 0.15 只是糊一点，收款码压到 0.15 可能直接不可扫。⚠️ 该风险**只有逻辑判据、
 * 没有真机验证**（需真机扫一次），登记在 spec §11。
 */
export const WECHAT_QR_MIN_QUALITY = 0.8

/** 用户媒体（头像 / 微信收款码）——与 `api/myProfile.ts` 的 `MediaKind` 同一集合。 */
export type UserMediaKind = 'avatar' | 'wechat-qr'

const TWO_MB = 2 * 1024 * 1024
const FIVE_MB = 5 * 1024 * 1024
const EIGHT_MB = 8 * 1024 * 1024

/**
 * 按**原图**字节数挑画质（0–1）。
 *
 * 逐字照对标那四个 `if` 的**顺序与边界**（默认 0.15，然后 5–8 MB → 0.25、
 * 2–5 MB → 0.4、< 2 MB → 0.6）。边界判据用 `>=` / `<`，⛔ 不要「顺手」改成 `>`。
 */
export function qualityForSize(size: number): number {
  if (size >= FIVE_MB && size < EIGHT_MB) return 0.25
  if (size >= TWO_MB && size < FIVE_MB) return 0.4
  if (size < TWO_MB) return 0.6
  return 0.15
}

/**
 * 头像 / 收款码的画质（`D-0153` ③）：**同一套四档**，但收款码有**下限 0.8**。
 *
 * - 头像 = `qualityForSize(size)`（与对标同源，可以低到 0.15）；
 * - 收款码 = `max(qualityForSize(size), 0.8)`——**有意与头像不同**，理由是扫码可读性
 *   （见 `WECHAT_QR_MIN_QUALITY` 的注释）。
 *
 * 两者都**不改尺寸**（`compressImage` 只重绘 + 降画质）。
 */
export function mediaQuality(kind: UserMediaKind, size: number): number {
  const base = qualityForSize(size)
  return kind === 'wechat-qr' ? Math.max(base, WECHAT_QR_MIN_QUALITY) : base
}

/** 取扩展名（小写、去点）——与 `pages/order-screenshots-upload.ts::extOf` 同口径。 */
function extOf(name: string): string {
  return (name.split('.').pop() ?? '').toLowerCase()
}

/**
 * 用户媒体直传**票据**要用的扩展名。
 *
 * ⚠️ 压缩成功时 canvas 一律输出 JPEG（`toDataURL('image/jpeg')`），而 `base64ToFile` **保留原文件名**
 * （订单截图页要靠它显示「文件名称」/ 发 `original_name`）⇒ 名字里的扩展名**与真实字节不符**。
 * 本函数按**压缩有没有真的发生**取扩展名：
 *  - `prepared !== original` ⇒ 压缩成功 ⇒ 字节是 JPEG ⇒ `jpg`；
 *  - `prepared === original` ⇒ **失败回落原图**（`compressImage` 的三个失败分支都 `return file`
 *    原对象）⇒ 字节就是原字节 ⇒ 取原文件的扩展名。这是「名实一致」的那一侧，不是将就。
 *
 * ⛔ **不要**拿它去改订单截图页：那一页有**既有的**同类不一致（用原扩展名签 `.png` 的键装 JPEG 字节），
 * 是另一条待处理项（`D-0153` ③ 明令本线不动它）。
 */
export function userMediaTicketExt(original: File, prepared: File): string {
  return prepared === original ? extOf(original.name) : 'jpg'
}

/**
 * 把 canvas 的 `dataURL` 换回 `File`（逐字照对标 `photoCompress.js:47-59` 的 `base64ToFile`）。
 *
 * 保留**原文件名**：列表里显示的「文件名称」与认领时的 `original_name` 都用它。
 * ⚠️ 编码类型跟着 canvas 走（`toDataURL('image/jpeg')` ⇒ 一律 `image/jpeg`），
 * 所以 `png` 上传后落库的其实是 jpeg 字节 —— 对标同此（`photoCompress.js:42`）。
 */
export function base64ToFile(base64: string, filename: string): File {
  const [, payload = ''] = base64.split(',')
  const mime = /:(.*?);/.exec(base64)?.[1] ?? 'image/jpeg'
  const binary = atob(payload)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  return new File([bytes], filename, { type: mime })
}

/** 读成 dataURL（`FileReader`）。 */
function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(new Error('图片读取失败'))
    reader.readAsDataURL(file)
  })
}

/** 解码成 `<img>`（`naturalWidth/Height` 用来保住原始尺寸，对标不缩放）。 */
function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => reject(new Error('图片解码失败'))
    image.src = src
  })
}

/**
 * 按档位压缩一张图（canvas 重绘 + `toDataURL('image/jpeg', quality)`）。
 *
 * ⚠️ **失败一律回落原图**（读不了 / 解不了 / 拿不到 2d 上下文，例如 jsdom 里没有 canvas）。
 * 理由：原图已经过了 `MAX_UPLOAD_BYTES` 这一关、后端也收得下 18 MB，而**压缩只是体验优化**
 * ⇒ 为了压缩失败把用户挑的图丢掉，是「静默丢数据」那一类缺陷（`AGENTS.md` §5 的反面）。
 * 调用方仍要自己判 `COMPRESSED_MAX_BYTES`（压缩成功但压不下去的那一档，对标会过滤掉）。
 *
 * `quality` 不传时按**原图字节数**取既有四档（订单截图页的用法）；用户媒体（头像 / 收款码）
 * 由调用方用 `mediaQuality(kind, size)` 显式传入——收款码那条**下限 0.8** 就落在那个入参上。
 * ⛔ 这条可选入参**不改**订单截图页的行为，只是把「画质从哪来」交回调用方。
 *
 * ⚠️ **`drawImage` 之前必须先铺一层白底**（2026-09-25 评审 I-4 修复）：
 * `toDataURL('image/jpeg')` 输出的 **JPEG 没有 alpha 通道**，画布上任何**透明**像素在编码时都会
 * 被合成成**黑**。微信收款码**常常是透明底 PNG** ⇒ 深色码点落在黑底上 ⇒ **整张不可扫**；
 * 而 `WECHAT_QR_MIN_QUALITY`（0.8）只管压缩率、**完全管不了**这件事。
 * 先 `fillStyle = '#fff'` + `fillRect(0, 0, w, h)` 把画布刷白，再 `drawImage` 把图画上去，
 * 透明区域就合成到白底上。
 *
 * ⚠️ **这会同时改变订单截图那一侧的压缩产物**（同一个函数、同一个行为，⚠️ 归因如实）：
 * 对**不透明**的图（绝大多数照片级截图）没有任何可见影响；对**透明**的图则是修复
 * ——它原先压出来是黑底（`photoCompress.js` 对标同样不铺白底，本波只是把这个缺陷
 * 用到了「给人扫」的收款码上，所以不再照抄它）。
 * ⛔ **仍然不改尺寸**（`D-0153` ③ 不变）：只是多一次整幅填充。
 */
export async function compressImage(file: File, quality?: number): Promise<File> {
  try {
    const dataUrl = await readAsDataUrl(file)
    const image = await loadImage(dataUrl)
    const canvas = document.createElement('canvas')
    canvas.width = image.naturalWidth || image.width
    canvas.height = image.naturalHeight || image.height
    const ctx = canvas.getContext('2d')
    if (!ctx) return file // 没有 2d 上下文（jsdom / 老浏览器）⇒ 原图
    // 白底必须在 `drawImage` **之前**（JPEG 无 alpha ⇒ 透明像素会变黑，见 docstring）。
    ctx.fillStyle = '#fff'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height)
    return base64ToFile(
      canvas.toDataURL('image/jpeg', quality ?? qualityForSize(file.size)),
      file.name,
    )
  } catch {
    return file
  }
}
