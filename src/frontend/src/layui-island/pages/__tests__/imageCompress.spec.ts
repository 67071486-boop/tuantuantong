// @vitest-environment jsdom
/**
 * 客户端分档压缩的**取值**钉子（spec §3.2 / Global Constraints）。
 *
 * 档位逐字取自对标 `assets_JS2019_Order_uploadpicnew.js:58-70`（同一份逻辑在粘贴那一支
 * `:152-161` 又抄了一遍）：
 * ```js
 * let quality = 0.15;
 * if (file.size >= 5242880 && file.size < 8388608) quality = 0.25;   // 5–8 MB
 * if (file.size >= 2097152 && file.size < 5242880) quality = 0.4;    // 2–5 MB
 * if (file.size < 2097152) quality = 0.6;                            // < 2 MB
 * ```
 * ⚠️ 判据必须打在**边界值**上：只在「随便一个大数」上断言，0.65/0.4 这类错档看不出来（E-0021）。
 *
 * ⚠️ `compressImage()` 本身是 canvas 实现，jsdom 里没有 canvas ⇒ 那部分只钉「base64 ⇒ File」
 * 这半步（真正的画质压缩由真浏览器验收，见 spec §11「真浏览器人眼验收」）。
 */
import { describe, expect, it, vi } from 'vitest'
// ⚠️ 共享零件**故意不放在 `pages/` 下**（那个目录的 `*.ts` 会被 `import.meta.glob` 当成页面注册）
// ⇒ 从 `pages/__tests__/` 上去两层。
import {
  COMPRESSED_MAX_BYTES, MAX_UPLOAD_BYTES, MAX_USER_MEDIA_BYTES, base64ToFile,
  compressImage, mediaQuality, qualityForSize, userMediaTicketExt,
} from '../../imageCompress'

describe('qualityForSize：四档与边界值', () => {
  it('档位逐字取自对标（含每个边界的两侧）', () => {
    // 每行 = [字节数, 期望画质]；边界两侧各一条，⛔ 不许只写中间值。
    const cases: Array<[number, number]> = [
      [0, 0.6],
      [1, 0.6],
      [2097151, 0.6], // 2 MB - 1
      [2097152, 0.4], // 2 MB
      [5242879, 0.4], // 5 MB - 1
      [5242880, 0.25], // 5 MB
      [8388607, 0.25], // 8 MB - 1
      [8388608, 0.15], // 8 MB
      [18 * 1024 * 1024, 0.15],
    ]
    expect(cases.map(([size]) => qualityForSize(size))).toEqual(cases.map(([, q]) => q))
  })
})

describe('两个上限常量与证据件一致', () => {
  it('原图 18 MB / 压缩后 5 MB（`uploadpicnew.js:28-29`）', () => {
    expect(MAX_UPLOAD_BYTES).toBe(18 * 1024 * 1024)
    expect(COMPRESSED_MAX_BYTES).toBe(5 * 1024 * 1024)
  })
})

describe('base64ToFile：把 canvas 的 dataURL 换回 File', () => {
  it('保留文件名与 MIME、字节数 = 解码长度（对齐对标 `photoCompress.js:47-59`）', () => {
    // `atob('/9j/4A==')` = 4 字节（ff d8 ff e0）
    const file = base64ToFile('data:image/jpeg;base64,/9j/4A==', 'a.jpg')
    expect(file.name).toBe('a.jpg')
    expect(file.type).toBe('image/jpeg')
    expect(file.size).toBe(4)
  })
})

/**
 * 头像 / 微信收款码的上限与画质口径（业务方裁定 `D-0153` ②③）。
 *
 * ⚠️ 判据一律写**字面值**（0.6 / 0.8 / 10485760），⛔ 不拿生产常量当期望值——
 * 那是自指断言，常量被改坏时不会红（E-0024）。
 */
describe('用户媒体（头像 / 收款码）：10 MB 上限与画质口径', () => {
  it('上限常量 = 10 MB（与订单截图的 18 MiB **分开**）', () => {
    expect(MAX_USER_MEDIA_BYTES).toBe(10 * 1024 * 1024)
    // 非恒真哨兵：它必须**不等于**订单截图那个上限，否则「分开」这句话是假的。
    expect(MAX_USER_MEDIA_BYTES).not.toBe(MAX_UPLOAD_BYTES)
  })

  it('头像用既有四档：**可以低于 0.8**（与收款码互斥的那一半）', () => {
    const sizes = [1024, 2097152, 5242880, 8388608, 10 * 1024 * 1024]
    // 逐档照 `qualityForSize` 的既有取值（含边界），只是外面套一层「按 kind 取」。
    expect(sizes.map((size) => mediaQuality('avatar', size)))
      .toEqual([0.6, 0.4, 0.25, 0.15, 0.15])
    // 关键：**确实取到了 < 0.8 的值**，否则下面收款码那条「下限」判据不成互斥（两条会同时真）。
    expect(sizes.map((size) => mediaQuality('avatar', size)).some((q) => q < 0.8)).toBe(true)
  })

  it('收款码画质**不低于 0.8**（有意与头像不同：给人扫的，压狠了扫不出来）', () => {
    const sizes = [1024, 2097152, 5242880, 8388608, 10 * 1024 * 1024]
    expect(sizes.map((size) => mediaQuality('wechat-qr', size)))
      .toEqual([0.8, 0.8, 0.8, 0.8, 0.8])
    // 同一个文件、同一个尺寸，两种 kind 必须给出**不同**的画质（否则这一步是假分支）。
    expect([mediaQuality('avatar', 1024), mediaQuality('wechat-qr', 1024)]).toEqual([0.6, 0.8])
  })

  it('票据 ext：压缩成功（拿到新 File）⇒ `jpg`；回落原图（同一个 File）⇒ 原扩展名', () => {
    // 压缩成功：canvas 一律 `toDataURL('image/jpeg')` ⇒ 字节是 JPEG，哪怕原文件叫 `.png`。
    const original = new File([new Uint8Array([1])], 'tuan.png', { type: 'image/png' })
    const compressed = new File([new Uint8Array([2])], 'tuan.png', { type: 'image/jpeg' })
    expect(userMediaTicketExt(original, compressed)).toBe('jpg')
    // 压缩失败回落：`compressImage` 原样返回**同一个对象** ⇒ 字节还是原图的字节，扩展名跟着原文件。
    expect(userMediaTicketExt(original, original)).toBe('png')
    const upper = new File([new Uint8Array([1])], 'QR.JPEG', { type: 'image/jpeg' })
    expect(userMediaTicketExt(upper, upper)).toBe('jpeg') // 回落时也照既有 `extOf` 的口径小写化
  })
})

/**
 * ⭐ **I-4（2026-09-25 评审修复）：JPEG 没有 alpha 通道 ⇒ 透明像素必须先合成到白底上。**
 *
 * 背景（独立评审，canvas 桩逐操作记录）：`ctx.drawImage(...)` 之前**没有任何填充** ⇒
 * 透明底 PNG（微信收款码**常常就是**透明底）的透明部分在 `toDataURL('image/jpeg')` 里被合成成
 * **黑**，深色码点落在黑底上 ⇒ 整张**不可扫**。我们加的 `quality ≥ 0.8` 下限对这件事**毫无作用**
 * （它只影响压缩率，不影响透明像素合成成什么颜色）。
 *
 * 判据钉**操作顺序**：先 `fillStyle = '#fff'` + `fillRect(0, 0, w, h)`，**后** `drawImage`。
 * 变异：删掉那次填充 ⇒ 只记到 `drawImage` ⇒ 红。
 *
 * ⚠️ 操作名与色值都写**字面值**（`'fillRect'` / `'#fff'`）——拿本模块的常量去断言本模块的行为
 * 是自指断言（`AGENTS.md` §5-G / E-0024）。
 */
describe('compressImage：透明图先铺白底、再 drawImage（顺序敏感）', () => {
  it('先 `fillStyle=#fff` + `fillRect(整张画布)`，后 `drawImage`；且确实走到 JPEG 编码', async () => {
    /** 逐操作记录（`fillStyle` 取**调用那一刻**的值，不是事后的）。 */
    const ops: Array<{ op: string; fillStyle?: string; args?: number[] }> = []
    const ctx = {
      fillStyle: '',
      fillRect: (...args: number[]): void => {
        ops.push({ op: 'fillRect', fillStyle: ctx.fillStyle, args })
      },
      drawImage: (..._args: unknown[]): void => {
        ops.push({ op: 'drawImage', fillStyle: ctx.fillStyle })
      },
    }
    const getContext = vi
      .spyOn(HTMLCanvasElement.prototype, 'getContext')
      .mockReturnValue(ctx as unknown as CanvasRenderingContext2D)
    const toDataURL = vi
      .spyOn(HTMLCanvasElement.prototype, 'toDataURL')
      .mockReturnValue('data:image/jpeg;base64,/9j/4A==')
    /**
     * jsdom 不会真的解码图片 ⇒ 用桩在 `src` 落地后异步触发 `onload`
     * （`loadImage` 是先挂 `onload`、再赋 `src`，所以这里 `this.onload` 一定已经在了）。
     */
    class FakeImage {
      onload: (() => void) | null = null
      onerror: (() => void) | null = null
      naturalWidth = 4
      naturalHeight = 3
      width = 4
      height = 3
      set src(_value: string) {
        Promise.resolve().then(() => this.onload?.())
      }
    }
    vi.stubGlobal('Image', FakeImage)
    try {
      const file = new File([new Uint8Array([0xff, 0xd8, 0xff])], 'qr.png', { type: 'image/png' })
      await compressImage(file, 0.8)

      // 判据一：**顺序**是「先铺底、后画图」（变异：删掉填充 ⇒ 这里只剩 `['drawImage']` ⇒ 红）。
      expect({ order: ops.map((o) => o.op) }).toEqual({ order: ['fillRect', 'drawImage'] })
      // 判据二：铺的是**白**底、且铺满整张画布（4×3 —— 与 `naturalWidth/Height` 一致，⛔ 不改尺寸）。
      expect({ fillStyle: ops[0]!.fillStyle, args: ops[0]!.args })
        .toEqual({ fillStyle: '#fff', args: [0, 0, 4, 3] })
      // 非空哨兵：确实走到了 JPEG 编码那一跳（否则上面两条可能只是「中途失败、原样回落原图」）。
      expect(toDataURL).toHaveBeenCalledWith('image/jpeg', 0.8)
    } finally {
      getContext.mockRestore()
      toDataURL.mockRestore()
      vi.unstubAllGlobals()
    }
  })
})
