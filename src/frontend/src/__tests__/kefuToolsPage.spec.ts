// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * 客服工具下载页结构网（D-0345，2026-10-30）。
 *
 * 页面是**纯静态 html**：`src/frontend/public/kefu-tools/index.html` → `https://<租户域名>/kefu-tools/`，
 * 与打手 App 下载页（`public/download/index.html`，RUNBOOK「打手 App 安装包分发」）同一承载方式
 * ——nginx `location /` 的 `try_files $uri/` 直接服务，**不进 Vue 路由表**、不进 replicaRoutes
 * （自建页、无对标转录义务）。所以本网用 `readFileSync` + `DOMParser` 钉**标记**（E-0161 同款：
 * 断言打在标记上，不打在「文件文本」上）。
 *
 * 约定（D-0345）：安装包在 COS 公开桶 `kefu-tools/` 前缀下、**固定文件名**
 * `kefu-tool-setup.exe`（发新版 = 覆盖上传 + 改页面上的版本号，不用清单文件）；
 * 文件由业务方提供后按 RUNBOOK「客服工具安装包分发」§上传流程放置。
 */
const HERE = path.dirname(fileURLToPath(import.meta.url))
const PAGE_FILE = path.resolve(HERE, '../../public/kefu-tools/index.html')
/** 固定文件名的 COS 外链（写页面与登记两处；桶与 apk 同一个公开读桶）。 */
const COS_URL = 'https://tt-app-1440679012.cos.ap-shanghai.myqcloud.com/kefu-tools/kefu-tool-setup.exe'

const html = readFileSync(PAGE_FILE, 'utf8')
const doc = new DOMParser().parseFromString(html, 'text/html')

describe('客服工具下载页（public/kefu-tools/index.html）', () => {
  it('阳性对照：文件读得到、是成形的 html（防路径坏了之后全绿）', () => {
    expect(html.length).toBeGreaterThan(0)
    expect(doc.querySelector('main')).not.toBeNull()
  })

  it('页面标题与工具名：「客服宝（客服工具）」——安装包真名客服宝，入口文案客服工具（2026-10-30 业务方提供安装包后改准）', () => {
    expect(doc.querySelector('title')?.textContent).toContain('客服工具')
    const h1 = doc.querySelector('h1')
    expect(h1?.textContent?.trim()).toBe('客服宝（客服工具）')
  })

  it('版本号写在页面上（v主.次.修 形状；发新版改页面，本网不钉具体值）', () => {
    const ver = doc.querySelector('[data-test="kefu-version"]')
    expect(ver?.textContent?.trim() ?? '', '页面上必须有版本号元素').toMatch(/^v\d+\.\d+\.\d+/)
  })

  it('Windows 下载按钮：指向 COS 固定文件名、带 download 属性', () => {
    const btn = doc.querySelector('a[data-test="kefu-download"]')
    expect(btn, '必须有下载按钮').not.toBeNull()
    expect(btn?.getAttribute('href')).toBe(COS_URL)
    expect(btn?.getAttribute('download')).not.toBeNull()
    expect(btn?.textContent ?? '').toContain('Windows')
  })

  it('用途说明非空且点明「客服 / Windows 桌面端」', () => {
    const desc = doc.querySelector('[data-test="kefu-desc"]')
    const text = desc?.textContent ?? ''
    expect(text.trim().length).toBeGreaterThan(0)
    expect(text).toContain('客服')
    expect(text).toContain('Windows')
  })
})
