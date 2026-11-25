// @vitest-environment jsdom
/**
 * ⭐ **I-2（2026-09-25 评审修复）：`directUpload.ts` 同源 / 跨源两条分支的判据。**
 *
 * 背景（独立评审）：`buildUploadInit` 的两条分支此前**零覆盖**——把 `sameOrigin` 判反、
 * 或把 `credentials` 的 `include`/`omit` 对调，全仓 vitest 仍全绿。这条分支是**承重**的
 * （同源少带 `X-CSRFToken` ⇒ DRF 的 CSRF 校验拒掉 PUT；跨源多带我们的头 ⇒ COS 判签名不符）。
 *
 * ⚠️ 判据一律写**字面值**（`'X-CSRFToken'` / `'PUT'` / `'include'` / `'omit'` /
 * `'Cache-Control': 'public, max-age=86400'`）：拿本模块的常量去断言本模块的行为是自指断言
 * （`AGENTS.md` §5-G / E-0024）。⭐ 缓存头被后端签进 COS 预签名（`MEDIA_CACHE_CONTROL`）⇒
 * 两侧**逐字节一致**是承重契约，末尾的跨栈结构网读后端源码对拍。
 *
 * ⚠️ 末尾那组「唯一出处」是**结构性判据**：它读源码，钉住「`pages/user-myinfo.ts` 不再留
 * 第二份 `buildUploadInit`」这条收口（同一事实只留一处）。
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/csrf-cookie', () => ({ readCsrfCookie: vi.fn() }))

import { readCsrfCookie } from '@/api/csrf-cookie'
import { buildUploadInit, putFileToStorage } from '../directUpload'

const cookie = vi.mocked(readCsrfCookie)

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** `src/frontend/src/layui-island/__tests__` → 页面源码。 */
const USER_MYINFO_SRC = fs.readFileSync(
  path.resolve(HERE, '../pages/user-myinfo.ts'),
  'utf8',
)

function makeFile(type = 'image/jpeg', name = 'a.jpg'): File {
  return new File([new Uint8Array([0xff, 0xd8, 0xff])], name, { type })
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  cookie.mockReset()
  cookie.mockReturnValue('tok-1')
  fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => (
    { ok: true, status: 200 } as unknown as Response
  ))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('buildUploadInit：同源分支（本机 `local` 后端 ⇒ upload_url 指向我们自己的 blob 端点）', () => {
  it('相对 URL（`/…`）⇒ 带 `X-CSRFToken` 且 `credentials: include`、方法 PUT、body 是那个 File', () => {
    const file = makeFile()
    const init = buildUploadInit(file, '/api/storage/blob/demo/x.jpg')
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(file)
    expect(init.credentials).toBe('include')
    expect(init.headers).toEqual({
      'Content-Type': 'image/jpeg',
      'Cache-Control': 'public, max-age=86400',
      'X-CSRFToken': 'tok-1',
    })
  })

  it('绝对 URL 但**同 origin** ⇒ 同样带 `X-CSRFToken`、`credentials: include`', () => {
    const init = buildUploadInit(makeFile(), `${window.location.origin}/api/storage/blob/demo/x.jpg`)
    expect(init.credentials).toBe('include')
    expect((init.headers as Record<string, string>)['X-CSRFToken']).toBe('tok-1')
  })

  it('同源但**读不到** CSRF cookie ⇒ 不带那个头（不是塞空串），credentials 仍是 include', () => {
    cookie.mockReturnValue(null)
    const init = buildUploadInit(makeFile(), '/api/storage/blob/demo/x.jpg')
    expect(init.credentials).toBe('include')
    expect(init.headers).toEqual({
      'Content-Type': 'image/jpeg',
      'Cache-Control': 'public, max-age=86400',
    })
    expect((init.headers as Record<string, string>)['X-CSRFToken']).toBeUndefined()
  })
})

describe('buildUploadInit：跨源分支（生产 COS 预签名）', () => {
  it('⛔ **不带**我们的 `X-CSRFToken`，且 `credentials: omit`（否则 COS 判签名不符）', () => {
    const file = makeFile('image/png', 'b.png')
    const init = buildUploadInit(file, 'https://cos.example.invalid/bucket/demo/x.png')
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(file)
    expect(init.credentials).toBe('omit')
    // 跨源**回读**那一位：不能只断言 credentials，得连头也一起钉住。
    expect((init.headers as Record<string, string>)['X-CSRFToken']).toBeUndefined()
    // ⭐ `Cache-Control` 是被签进预签名的头：跨源也**必须**带、且值逐字节一致（否则 COS 403）。
    expect(init.headers).toEqual({
      'Content-Type': 'image/png',
      'Cache-Control': 'public, max-age=86400',
    })
  })

  it('同源的 origin 只差一个端口也算跨源（`startsWith(origin)` 不得放宽成「同主机」）', () => {
    const other = `${window.location.protocol}//${window.location.hostname}:59999/api/x.jpg`
    const init = buildUploadInit(makeFile(), other)
    expect(init.credentials).toBe('omit')
    expect((init.headers as Record<string, string>)['X-CSRFToken']).toBeUndefined()
  })
})

describe('Content-Type 与 PUT 这一跳', () => {
  it('文件 type 为空 ⇒ 回落 `application/octet-stream`（不是空串）', () => {
    const init = buildUploadInit(makeFile(''), '/api/storage/blob/demo/x')
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/octet-stream')
  })

  it('`putFileToStorage` 用 PUT 把**那个 File**直传出去，成功时不抛', async () => {
    const file = makeFile()
    await putFileToStorage('/api/storage/blob/demo/x.jpg', file)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/storage/blob/demo/x.jpg')
    expect(init.method).toBe('PUT')
    expect(init.body).toBe(file)
    expect(init.credentials).toBe('include')
    expect(init).not.toHaveProperty('signal')
  })

  it('传入取消信号后，跨源 PUT 仍只使用对象存储需要的请求头', async () => {
    const url = 'https://cos.example.invalid/bucket/demo/x.png'
    const file = makeFile('image/png', 'x.png')
    const { signal } = new AbortController()

    await putFileToStorage(url, file, signal)

    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(url, {
      method: 'PUT',
      body: file,
      credentials: 'omit',
      headers: { 'Content-Type': 'image/png', 'Cache-Control': 'public, max-age=86400' },
      signal,
    })
  })

  it('⛔ 4xx/5xx 是 `fetch` 的**正常返回** ⇒ 必须自己看 `res.ok` 并抛（带状态码）', async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 403 } as unknown as Response)
    await expect(putFileToStorage('/api/storage/blob/demo/x.jpg', makeFile()))
      .rejects.toThrow('图片上传失败（HTTP 403）')
  })

  /**
   * ⭐ **Minor-1（2026-09-25 评审修复）+ Minor-3（复评订正）：本机直传 413（裸响应）显示一句
   * **固定中文文案**。**
   *
   * 现场：本机 blob 路由回**裸 413、没有 body**（`apps/core/media_blob_views.py` 的
   * `MediaBlobView.put` 三个 413 分支都是 `HttpResponse(status=413)`）⇒ 原先那句
   * `图片上传失败（HTTP 413）` 是**前端自造**的、还把状态码当文案给用户看
   * （本仓口径「失败文案不前端自造」）。
   *
   * ⚠️ **复评 Minor-3 的订正**：413 不只表示「超限」——`declared <= 0` 那条前置拒收也回 413
   * （用户在浏览器里选了个 **0 字节文件**、或分块上传没有声明值）⇒ 旧文案
   * `图片超过大小限制，请压缩后重试` 对 0 字节文件是**说假话**。新文案两种情形都说得通。
   *
   * ⚠️ 这条路由**同时服务两种上限**（`_max_bytes_for_key` 按 key 种类取：头像 / 收款码 10 MB、
   * 订单截图 18 MiB）⇒ 固定文案**不写死数字**，两种上限都说得通；真正的边界仍在后端
   * （认领端点按各自上限重判）。
   *
   * 变异：① 只判 `!res.ok`（把 413 也归到「HTTP <码>」那一支）⇒ 红；
   * ② 把 `OVERSIZE_TEXT` 改回旧文案（只说超限）⇒ 本条以字面值不等变红（已实测）。
   */
  it('⛔ 413（裸响应无 body）⇒ 固定中文文案，且「无效 / 超限」两种情形都说得通', async () => {
    fetchMock.mockResolvedValueOnce({ ok: false, status: 413 } as unknown as Response)
    const error = await putFileToStorage('/api/storage/blob/demo/orders/x.jpg', makeFile())
      .then(() => null, (e: unknown) => e as Error)
    expect(error).toBeInstanceOf(Error)
    // 钉**字面值**（不是模块里的常量——那是自指断言，E-0024）。
    expect({ message: error!.message }).toEqual({
      message: '图片无效或超过大小限制，请重新选择后重试',
    })
  })
})

/**
 * 收口判据（I-2 的第二半）：`directUpload.ts` 是这一跳的**唯一出处**。
 * 它读源码而不是跑行为 —— 因为「页面里又长出一份逐字重复的实现」是**结构性**回归，
 * 行为用例（两条腿都通）抓不到它。
 */
describe('收口：直传那一跳以 `directUpload.ts` 为唯一出处', () => {
  it('`pages/user-myinfo.ts` 引用共享实现，且**不再**自带一份 `buildUploadInit`', () => {
    expect(USER_MYINFO_SRC).toContain("from '@/layui-island/directUpload'")
    expect(USER_MYINFO_SRC).toContain('putFileToStorage')
    // 本地定义（`export function buildUploadInit` / `function buildUploadInit`）必须不存在。
    expect(USER_MYINFO_SRC).not.toMatch(/function\s+buildUploadInit/)
  })
})

/**
 * ⭐ 跨栈结构网（URL 稳定化线，2026-11-09）：直传的 `Cache-Control` 与后端签进 COS 预签名的
 * `MEDIA_CACHE_CONTROL` 必须**逐字节一致**——请求带别的值 COS 判签名不符，**所有直传全部 403**。
 * 行为用例抓不到「两边各自改值」的漂移 ⇒ 读后端源码对拍（先例：上面的 `USER_MYINFO_SRC` 读源码收口）。
 */
describe('收口：直传缓存头与后端签入值逐字节一致', () => {
  const STORAGE_SRC = fs.readFileSync(
    path.resolve(HERE, '../../../../apps/core/storage.py'),
    'utf8',
  )
  const FRONTEND_SRC = fs.readFileSync(path.resolve(HERE, '../directUpload.ts'), 'utf8')

  it('后端 `MEDIA_CACHE_CONTROL` 与前端 `UPLOAD_CACHE_CONTROL` 取同一个字面量', () => {
    const backend = STORAGE_SRC.match(/MEDIA_CACHE_CONTROL = "([^"]+)"/)?.[1]
    const frontend = FRONTEND_SRC.match(/UPLOAD_CACHE_CONTROL = '([^']+)'/)?.[1]
    // 自证夹具：两侧都真的提取到了（否则比的是 undefined == undefined 的假绿）
    expect(backend).toBeDefined()
    expect(frontend).toBeDefined()
    expect(frontend).toBe(backend)
  })

  it('`buildUploadInit` 真的把那个头放进 headers（不是只定义了常量没人用）', () => {
    expect(FRONTEND_SRC).toMatch(/'Cache-Control':\s*UPLOAD_CACHE_CONTROL/)
  })
})
