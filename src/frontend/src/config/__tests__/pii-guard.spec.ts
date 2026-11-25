import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * **防复发守卫**：对标租户的真实标识（PII、店铺名）不得再被写回 `src/`。
 *
 * 两条**独立**的语料 + 判据（这里**不写任何一条真值**）：
 *
 * 1. **真值黑名单**（`DENY_CORPUS` / `denylistHits`）：黑名单读自仓库外
 *    `.tools/denylist.txt`（`.tools/` 已被 `.gitignore` 忽略，故真值不进 git）。
 *    语料 = **整个 `src/**` 的全部可读文本文件**——**不限扩展名、也不排除 `*.spec.ts`**
 *    （真值不挑文件类型：写进 `pyproject.toml` 或某个 spec 文件同样是泄漏），只排
 *    `node_modules` / `dist` / `__pycache__` / `.venv`；逐个按 UTF-8 严格试读，
 *    **读不了的记进 `skipped`（不静默）**。判据 = 逐行包含黑名单任一条 ⇒
 *    报 `文件:行号 命中黑名单第 N 条`（报**序号**，不回显值）。
 * 2. **内建形状模式**（`SHAPE_CORPUS` / `builtinHits`）：硬编码的**形状**正则
 *    （11 位手机号 / 18 位身份证 / 19 位数字串 / `@` 邮箱）。
 *    ⛔ **只套前端两半**（`src/frontend/src` + `src/frontend/public/replica`），按扩展名
 *    白名单取文本并**排除 `*.spec.ts`**——⛔ 这个排除项**只属于形状面**（本文件就在前端，
 *    否则会命中自己的正则字面量）；理由（实测 89 行后端合法假数据会误报）见
 *    `BUILTIN_PATTERNS` 上方。
 *
 * **skip 只有一种形态**：`.tools/denylist.txt` **文件不存在** ⇒ 真值那一半 skip
 * （`readDenylist` 返回 `{ state: 'missing' }`，并 `console.warn` 说明后果与修法）。
 * 「文件存在但没有任何有效条目」**不是 skip，是红**（用例里 `entries.length > 0` 断言）。
 *
 * ⚠️ **真值这一半是「本地才有效」的（如实写明）**：`.tools/` 不进 git ⇒ 在**没有
 * `.tools/denylist.txt` 的环境**（新签出 / worktree / 任何未布置该文件的运行环境）里，
 * 这条用例**永远 skip**。也就是说 **「真值已被清掉」这件事在没有那份黑名单的环境里
 * 没有任何机械保证**——它只在布置了黑名单的本地环境里被检查。
 * ⛔ 有意**不**改成「缺文件即红」（会把这类环境打挂），也**不**改成「空文件算通过」。
 * 【推断】若日后接了 CI：本仓**今天没有任何 CI 配置**（无 `.github/workflows` 等，
 * 无法从仓库验证），故「CI 里会 skip」只是按「CI 通常做全新签出」的**推断**。
 *
 * 证据件（`public/replica/**`）额外禁**店铺名后缀模式**，但判据钉在 **`<option>` 元素**上，
 * 不钉裸后缀：证据件里 `title="淘宝店"` / `title="天猫店"`（平台名标签，
 * `gameaccounts-accountlist.html`）是合法内容，裸后缀判据会误报它
 * （见本文件底部 `EVIDENCE_SHOP_SUFFIX` 的注释）。
 * ⚠️ 2026-09-23 本线下线 4 个系统设置页，原先还举的「`当你淘宝店购买了…`（正文文案）」一例
 * 随 `system-taobaoshipsetting.html` 一起删除，故不再作为例子；判据本身未变。
 *
 * ⛔ **扫描面有意排除 `docs/**` 与 `.agents/notes/**`**（**任何扫描根都不含它们**，
 * 不是「命中后按理由文本豁免」——那种后门见 E-0065）。理由：按 `AGENTS.md` §4
 * 「**不删历史记录**」（`docs/` 与 `.agents/notes/` 的条目**只增不改**），
 * 这两处是**历史文档 / 过程台账**，实测分别有 59 / 4 处真值（扩面当时）。
 * 把它们扫进来只会逼人回头删历史；将来要清洗是**另一次性的、由人裁决的动作**，
 * 不该挂在每条分支都要跑的门禁上。
 *
 * ⚠️ **有意登记的两条残差（如实，不在断言里）**：
 *
 * 1. **recon 子域标签**：黑名单收的是**完整域名**这类真值。对标租户的**子域标签**
 *    **是**某条真值（完整域名那条）的**子串**，但**不等于**任何一条；而本判据是
 *    「行包含**整条**真值」⇒ 该标签**不会**被命中（结论成立，理由如此——**不是**
 *    「它不是真值」）。实测它出现在 `src/frontend/` 下 **86 个文件**、**150 次**
 *    （`src/frontend/src` 83 个 + `src/frontend/public` 3 个）的「证据出处」注释里
 *    （形如 `.tools/recon/<标签>/…`，那个本地目录**确实**叫这个名字），**有意未清**，
 *    也不在黑名单里。⇒ 这 86 个文件**今天不会**因它变红。⚠️ **若将来**把判定改成
 *    按 token / 子串匹配、或把该标签本身收录进黑名单，这 86 个文件会**当场变红**
 *    （届时是一次**跨线**的成规模清洗，与所有在飞分支大面积冲突，**须由人裁决**）。
 * 2. **`src/frontend/public/vendor/formselects-v4.css` 的出处注释里那个完整域名 URL
 *    已按本线脱敏**（占位为 `<对标租户域名>`，写了「域名已脱敏」）——那是本线扩到整个
 *    `src/` 后新抓到并清掉的**第 19 处**真值（vendored 正文一字未动）。
 */

const HERE = path.dirname(fileURLToPath(import.meta.url))
/** `src/frontend`（本文件位于 `src/frontend/src/config/__tests__/`）。 */
const FRONTEND_ROOT = path.resolve(HERE, '../../..')
/** 仓库根（`src/frontend/../..`）。所有报告路径都相对它，前端与后端同一套。 */
const REPO_ROOT = path.resolve(FRONTEND_ROOT, '../..')
const EVIDENCE_ROOT = path.join(FRONTEND_ROOT, 'public/replica')

/**
 * **真值黑名单**的扫描根：**整个 `src/`**（`apps` / `tests` / `config` / `frontend` /
 * `manage.py` 全在内）。判据是「代码可能写在 `src/` 的任何地方」。
 */
const DENYLIST_SCAN_ROOTS = [path.join(REPO_ROOT, 'src')]

/** **形状模式**的扫描根：只前端两半（理由见 `BUILTIN_PATTERNS`）。 */
const SHAPE_SCAN_ROOTS = [
  path.join(FRONTEND_ROOT, 'src'),
  path.join(FRONTEND_ROOT, 'public/replica'),
]

/** 仓库根的 `.tools/denylist.txt`。 */
const DENYLIST_PATH = path.join(REPO_ROOT, '.tools/denylist.txt')

const SKIP_DIRS = new Set(['node_modules', 'dist', '__pycache__', '.venv'])
/** 形状面的扩展名白名单——⛔ **真值面不用它**（真值面收全部可读文本）。 */
const SHAPE_TEXT_EXT = new Set([
  '.ts', '.vue', '.css', '.js', '.html', '.json', '.md', '.txt', '.svg', '.py',
])

interface ScannedFile {
  /** 相对**仓库根**的路径，只用于报告（不含真值）。 */
  file: string
  lines: string[]
}

/** 一条语料 + **不静默**的跳过清单（按 UTF-8 读不了的文件）。 */
interface Corpus {
  files: ScannedFile[]
  /** 按 UTF-8 读不了而跳过的文件（相对 `REPO_ROOT`）——每条语料都在用例里断言它。 */
  skipped: string[]
}

const utf8 = new TextDecoder('utf-8', { fatal: true })

/** 严格按 UTF-8 试读；读不了（二进制 / 非 UTF-8）返回 `null`。 */
function readTextOrNull(abs: string): string | null {
  try {
    return utf8.decode(readFileSync(abs))
  } catch {
    return null
  }
}

/**
 * **已知二进制资源**——两张**显式枚举**的白名单，按本仓实测用到的取最小集合：
 * - `KNOWN_BINARY_EXT`：**扩展名**；
 * - `KNOWN_BINARY_NAMES`：**整个文件名**（`.DS_Store` 这类**没有扩展名**的 OS 元数据）。
 *
 * 判据是「按 UTF-8 **读不了的**文件，必须命中这两张表**之一**」——**不是**「跳过数 < N」，
 * 也**不是**「无扩展名就跳过」「读不了就跳过」：
 * - 代理判据（跳过数）**两头都不对**（评审实测）：多放 30 个 `.png` 会误红；
 *   混进一个 GBK / UTF-16 编码的**文本**文件只让计数 +1 ⇒ 真值被**静默漏掉**；
 * - 「无扩展名 / 读不了就跳过」更糟：会把「一个 GBK 编码的源码文本文件」也静默放过。
 *
 * 为什么这 6 种扩展名是二进制（实测读不了的资源，共 12 个）：
 * - `.png` 位图（favicon / 引导图 / 图标）、`.ico` 图标；
 * - `.eot` / `.ttf` / `.woff` / `.woff2` 字体（layui 的 `iconfont`）。
 *
 * 为什么这几个**名字**一定不是我们要扫的**源码文本**（实测：macOS 主检出另有 4 个，
 * worktree / 新签出里没有 ⇒ 只有「本机主检出」这个环境会暴露它们）：
 * - `.DS_Store`：macOS Finder 在**每个访问过的目录**里生成的**二进制**窗口/图标状态数据库；
 *   **没有扩展名**（`path.extname('.DS_Store')` 是空串 ⇒ 扩展名表天然抓不到它）；
 *   已被 `.gitignore` 忽略，任何 macOS 开发机上都会有。
 * - `Thumbs.db` / `desktop.ini`：Windows 资源管理器的**二进制**缩略图缓存 / 目录显示配置
 *   （本仓目前没有，但同类、同样与源码无关；**显式枚举**它们是为了不让下一个 Windows 同事
 *   踩同一个坑——**不是因为**「凡是 OS 文件都放过」）。
 *
 * ⚠️ 「按 UTF-8 读不了」在这里是**正常**的；**两张表都命不中**的文件读不了才红——那通常意味着
 * 「编码变了 / 混进了非文本」⇒ 真值面可能静默漏扫。
 */
const KNOWN_BINARY_EXT = new Set(['.ico', '.png', '.eot', '.ttf', '.woff', '.woff2'])
/** OS / 资源管理器元数据的**文件名**白名单（**小写**比较；理由见上方）。 */
const KNOWN_BINARY_NAMES = new Set(['.ds_store', 'thumbs.db', 'desktop.ini'])

/** 读不了的文件是否属于「已知二进制 / OS 元数据」：**扩展名表或文件名表**命中即可。 */
function isKnownBinary(relPath: string): boolean {
  const name = path.basename(relPath).toLowerCase()
  return KNOWN_BINARY_NAMES.has(name) || KNOWN_BINARY_EXT.has(path.extname(name))
}

/**
 * **真值语料**：`src/**` 的**全部可读文本文件**——**不限扩展名、也不排除 `*.spec.ts`**。
 * 读不了的记进 `skipped`（实测：资源 12 个 + **macOS 主检出的 `.DS_Store` 4 个 = 16 个**；
 * worktree / 新签出里只有前 12 个）。
 */
function collectDenylistCorpus(): Corpus {
  const files: ScannedFile[] = []
  const skipped: string[] = []
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (SKIP_DIRS.has(entry.name)) continue
      const abs = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        walk(abs)
        continue
      }
      const text = readTextOrNull(abs)
      if (text === null) {
        skipped.push(path.relative(REPO_ROOT, abs))
        continue
      }
      files.push({ file: path.relative(REPO_ROOT, abs), lines: text.split('\n') })
    }
  }
  for (const root of DENYLIST_SCAN_ROOTS) walk(root)
  return { files, skipped }
}

/**
 * **形状语料**：只前端两半，按扩展名白名单取文本，并**排除 `*.spec.ts`**
 * （这条排除**只属于形状面**，见文件头注释）。
 *
 * 与真值面**口径对称**：同样用 `fatal` 解码并记录 `skipped`——白名单里的扩展名
 * （`.ts/.vue/.css/.js/.html/.json/.md/.txt/.svg/.py`）**本该都是文本**，读不了就是异常，
 * 用例里断言 `skipped` 为空（否则那些文件会被解成乱码而**静默不扫**）。
 */
function collectShapeCorpus(): Corpus {
  const files: ScannedFile[] = []
  const skipped: string[] = []
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (SKIP_DIRS.has(entry.name)) continue
      const abs = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        walk(abs)
        continue
      }
      if (!SHAPE_TEXT_EXT.has(path.extname(entry.name)) || entry.name.endsWith('.spec.ts')) continue
      const text = readTextOrNull(abs)
      if (text === null) {
        skipped.push(path.relative(REPO_ROOT, abs))
        continue
      }
      files.push({ file: path.relative(REPO_ROOT, abs), lines: text.split('\n') })
    }
  }
  for (const root of SHAPE_SCAN_ROOTS) walk(root)
  return { files, skipped }
}

const DENY_CORPUS = collectDenylistCorpus()
const SHAPE_CORPUS = collectShapeCorpus()

/**
 * 实测（2026-09-22 扩到「整个 `src/**` 的全部可读文本」后）：
 * 真值语料 **627** 个文件（遍历 639，跳过的 12 个都是二进制资源），其中 `.py` **290**；
 * 形状语料 **205** 个（`src/frontend/src` 139 + `public/replica` 66）。
 */
const MIN_DENY_FILES = 600
const MIN_DENY_PY_FILES = 250
const MIN_SHAPE_FILES = 150

/**
 * 本轮清掉真值的文件——它们必须**真的**在真值语料里。
 * 否则「0 命中」可能只是「根本没被扫」（假通过）。
 */
const FORMER_TRUE_VALUE_FILES = [
  'src/apps/audit/actions.py',
  'src/tests/test_operation_log_model.py',
  'src/tests/test_operation_log_write_paths.py',
  // 扩到整个 `src/` 后才抓到的第 19 处（vendored CSS 的出处注释里的对标域名）。
  'src/frontend/public/vendor/formselects-v4.css',
]

/**
 * **生产判定**：语料里逐行包含黑名单任一条 ⇒ `文件:行号 命中黑名单第 N 条`。
 * 报告**序号**而不是值本身（免得真值随失败信息漏进日志 / CI）。
 * ⚠️ 下面有两条**正对照**直接喂它合成语料，确保这条路径**自己会命中**
 * （只证明「语料非空」是不够的，见 E-0065 同族）。
 */
function denylistHits(corpus: readonly ScannedFile[], denylist: readonly string[]): string[] {
  const hits: string[] = []
  denylist.forEach((value, order) => {
    for (const { file, lines } of corpus) {
      lines.forEach((line, index) => {
        if (line.includes(value)) hits.push(`${file}:${index + 1} 命中黑名单第 ${order + 1} 条`)
      })
    }
  })
  return hits
}

/**
 * 内建模式：只描述**形状**。全部按**逐行**匹配，报告 `文件:行号`（不回显命中内容，
 * 免得真值随失败信息漏进日志/CI）。
 *
 * ⛔ **只在前端两半上跑**（`SHAPE_CORPUS`）。为什么不套后端（实测）：
 * 把这 4 条套到 `src/apps` + `src/tests` 上会命中 **89 行完全合法**的内容——
 * 测试必须自己造这些形状的数据：假手机号、假 19 位 APPID（闲管家订单号）、
 * 假 18 位串（如 `A000000000000000001`）、假邮箱（`…@example.com`），
 * 外加 `apps/business/intake_rules.py` 里那条**手机号正则 / 示例本身**。
 * 形状判据的用途是「证据件里不该出现真形状」，而后端代码**需要**写这些形状。
 * ⛔ 这不是「后端不查」：后端仍被**真值黑名单**的逐行包含判定覆盖。
 */
const BUILTIN_PATTERNS: ReadonlyArray<{ name: string; re: RegExp }> = [
  { name: '手机号（11 位，1[3-9] 开头）', re: /(?<!\d)1[3-9]\d{9}(?!\d)/ },
  { name: '身份证号（18 位）', re: /(?<!\d)\d{17}[\dXx](?!\d)/ },
  { name: 'APPID 形状（19 位数字串）', re: /(?<!\d)\d{19}(?!\d)/ },
  { name: '邮箱地址', re: /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/ },
]

/**
 * 店铺名后缀（内建模式，非真值）。只在**证据件的 `<option>` 元素**上使用：
 * 证据件清洗前，租户的店铺来源就是 `#orderSoures2` 里的 `<option value="<店名>">`，
 * 所以「option 里出现店铺后缀」正是「店铺名被写回来」的形状。
 * 裸文本判据会误报 `title="淘宝店"` / `title="天猫店"`（平台名标签，
 * `gameaccounts-accountlist.html`）等。
 * （2026-09-23 本线下线 4 个系统设置页后，原举例「`淘宝店对接设置`（菜单文案）」已不存在，
 * 故换成仍在语料里的那条——判据本身未变。）
 */
const EVIDENCE_SHOP_SUFFIX = /(?:淘宝店|天猫店|旗舰店|专营店|专卖店)/
const OPTION_ELEMENT = /<option\b[\s\S]*?<\/option>/g

function builtinHits(): string[] {
  const hits: string[] = []
  for (const { file, lines } of SHAPE_CORPUS.files) {
    lines.forEach((line, index) => {
      for (const { name, re } of BUILTIN_PATTERNS) {
        if (re.test(line)) hits.push(`${file}:${index + 1} 命中「${name}」`)
      }
    })
  }
  return hits
}

/** 证据件里「店铺名形态」的 option（`文件:行号`；不回显店名本身）。 */
function evidenceShopHits(): string[] {
  const hits: string[] = []
  for (const { file, lines } of DENY_CORPUS.files) {
    if (!path.resolve(REPO_ROOT, file).startsWith(EVIDENCE_ROOT)) continue
    const text = lines.join('\n')
    OPTION_ELEMENT.lastIndex = 0
    let match: RegExpExecArray | null
    while ((match = OPTION_ELEMENT.exec(text)) !== null) {
      if (!EVIDENCE_SHOP_SUFFIX.test(match[0])) continue
      const line = text.slice(0, match.index).split('\n').length
      hits.push(`${file}:${line}`)
    }
  }
  return hits
}

/**
 * 黑名单读取结果——**skip 只有 `missing` 一种形态**：
 * - `{ state: 'missing' }`：文件不存在 ⇒ 用例 skip（先 `console.warn` 说清后果与修法）。
 * - `{ state: 'present', entries }`：文件存在 ⇒ **永不 skip**；即使 `entries` 为空，
 *   也由用例体内的断言把它**变红**（空黑名单会让「0 命中」恒真）。
 */
type DenylistRead = { state: 'missing' } | { state: 'present'; entries: string[] }

/**
 * 读真值黑名单（跳过空行与 `#` 注释）。
 *
 * ⛔ 缺文件时**不失败**（这类环境里没有 `.tools/`，改红会把它们打挂），但**必须响亮**——
 * 静默 skip 会让人把「没跑」读成「跑过且 0 命中」。
 */
function readDenylist(): DenylistRead {
  if (!existsSync(DENYLIST_PATH)) {
    console.warn(
      [
        '',
        '⚠️ [pii-guard] 未找到 .tools/denylist.txt —— 真值黑名单那一半**整体没有跑**！',
        `   期望路径：${DENYLIST_PATH}`,
        '   这**不是**「跑过且 0 命中」：对标租户真值在本环境里**完全没有被检查**。',
        '   修法（`.tools/` 不进 git ⇒ 新签出 / worktree 里没有它）：',
        '     从主检出把 `.tools/denylist.txt` 拷到**本仓库根**（worktree 就是 worktree 根）的同名路径，',
        '     或把该文件软链过去。',
        '',
      ].join('\n'),
    )
    return { state: 'missing' }
  }
  const entries = readFileSync(DENYLIST_PATH, 'utf8')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '' && !line.startsWith('#'))
  return { state: 'present', entries }
}

const DENYLIST = readDenylist()

describe(
  'PII 防复发守卫（真值 = 整个 src/** 的全部可读文本；形状 = 前端两半）',
  () => {
    it('正对照：判定函数对「含黑名单值的合成语料」报出**恰好 1 处**——否则下面「0 命中」可能只是判据不会命中', () => {
      // 合成值（**不是**任何真值），只用来证明判定路径自身能命中。
      const fakeValue = 'PII-GUARD-POSITIVE-CONTROL-SENTINEL'
      const synthetic: ScannedFile[] = [
        { file: 'src/synthetic/hit.ts', lines: ['无关行', `这里含 ${fakeValue} 一次`, '无关行'] },
        { file: 'src/synthetic/miss.ts', lines: ['这一行没有命中值', '这一行也没有'] },
      ]
      expect(denylistHits(synthetic, [fakeValue, 'ANOTHER-SENTINEL-NOT-PRESENT'])).toEqual([
        'src/synthetic/hit.ts:2 命中黑名单第 1 条',
      ])
    })

    it('真值语料「非空、覆盖前端/后端/config/入口/非代码文本/spec 文件」——否则 0 命中是假通过', () => {
      const files = DENY_CORPUS.files.map((f) => f.file)
      const pyFiles = files.filter((f) => path.extname(f) === '.py')
      expect(files.length, '真值语料文件数（实测 627）').toBeGreaterThan(MIN_DENY_FILES)
      expect(pyFiles.length, '真值语料里的 .py 数（实测 290）').toBeGreaterThan(MIN_DENY_PY_FILES)
      // 全覆盖：只扫 apps 会漏掉 tests / config / 入口。
      expect(files.some((f) => f.startsWith('src/apps/')), '语料里必须含 src/apps/**').toBe(true)
      expect(files.some((f) => f.startsWith('src/tests/')), '语料里必须含 src/tests/**').toBe(true)
      expect(files.some((f) => f.startsWith('src/config/')), '语料里必须含 src/config/**').toBe(true)
      expect(files, '语料里必须含 src/manage.py').toContain('src/manage.py')
      // ⛔ 真值面**不限扩展名、也不排除 `*.spec.ts`**——这两条把口径钉死（反例见评审 Important 2）。
      expect(files, '真值语料必须含非代码文本（如 src/pyproject.toml）').toContain(
        'src/pyproject.toml',
      )
      expect(
        files.some((f) => f.endsWith('.spec.ts')),
        '真值语料必须含 *.spec.ts（这一侧的排除只属于形状面）',
      ).toBe(true)
      // 清掉真值的文件必须在语料里（否则「0 命中」= 没扫）。
      expect(files, `语料里必须含这些改前含真值的文件：${FORMER_TRUE_VALUE_FILES.join(' / ')}`)
        .toEqual(expect.arrayContaining(FORMER_TRUE_VALUE_FILES))
      // 前端两半也要在。
      expect(
        files.some((f) => f.startsWith('src/frontend/src/')),
        '语料里必须含 src/frontend/src/**',
      ).toBe(true)
      expect(
        files.some((f) => f.startsWith('src/frontend/public/replica/')),
        '语料里必须含 src/frontend/public/replica/**',
      ).toBe(true)
      expect(new Set(files).size, '语料内路径必须唯一').toBe(files.length)
      // ⛔ 结构不变量：`DENYLIST.state` 必须与「文件在磁盘上是否存在」**一一对应**——
      // 否则可以谎报 `missing` 让真值用例静默 skip（评审实测过这种假绿）。
      // 放在这条**不 skip** 的覆盖度用例里，保证它永远会跑。
      expect(
        existsSync(DENYLIST_PATH),
        '`DENYLIST.state === present` 当且仅当 .tools/denylist.txt 真的存在（谎报 missing 会静默 skip）',
      ).toBe(DENYLIST.state === 'present')
      // 跳过的不可读文件**不静默**，判据是**实义的**：每一项都必须命中
      // `KNOWN_BINARY_EXT`（扩展名）**或** `KNOWN_BINARY_NAMES`（文件名）——不是「跳过数 < N」，
      // 也不是「无扩展名 / 读不了就放过」（见那两个常量的注释）。
      const offenders = DENY_CORPUS.skipped.filter((f) => !isKnownBinary(f))
      expect(
        offenders,
        `按 UTF-8 读不了、又不属于已知二进制（扩展名：${[...KNOWN_BINARY_EXT].join(' ')}；文件名：${[
          ...KNOWN_BINARY_NAMES,
        ].join(' ')}）的文件：${
          offenders.map((f) => `${f} [ext=${path.extname(f) || '无'}]`).join(' / ') || '无'
        }（全部跳过项：${
          DENY_CORPUS.skipped.map((f) => `${f} [ext=${path.extname(f) || '无'}]`).join(' / ') || '无'
        }）`,
      ).toEqual([])
      // `docs/` 与 `.agents/notes/` **有意排除**（§4 不删历史记录）：确认没被扫进来。
      expect(
        files.some((f) => f.startsWith('docs/') || f.startsWith('.agents/')),
        '语料里不得含 docs/** 或 .agents/**（§4：不删历史记录）',
      ).toBe(false)
    })

    it('形状语料「非空、两半都在、不含 *.spec.ts、且没有读不了的」——否则形状 0 命中是假通过', () => {
      const files = SHAPE_CORPUS.files.map((f) => f.file)
      expect(files.length, '形状语料文件数（实测 205）').toBeGreaterThan(MIN_SHAPE_FILES)
      expect(
        files.some((f) => f.startsWith('src/frontend/src/')),
        '形状语料必须含 src/frontend/src/**',
      ).toBe(true)
      expect(
        files.some((f) => f.startsWith('src/frontend/public/replica/')),
        '形状语料必须含 src/frontend/public/replica/**',
      ).toBe(true)
      expect(
        files.every((f) => !f.endsWith('.spec.ts')),
        '形状语料不得含 *.spec.ts（否则会命中本文件自己的正则字面量）',
      ).toBe(true)
      expect(new Set(files).size, '形状语料内路径必须唯一').toBe(files.length)
      // 形状白名单里的扩展名（`.ts/.vue/.css/…`）**本该都是文本**：读不了就是异常
      // （会被解成乱码而**静默不扫**）⇒ 这里必须为空。
      expect(
        SHAPE_CORPUS.skipped,
        `形状语料里按 UTF-8 读不了的文件（白名单扩展名本该都是文本）：${SHAPE_CORPUS.skipped.join(' / ') || '无'}`,
      ).toEqual([])
    })

    it('内建 PII 形状模式（仅前端两半）：0 命中', () => {
      expect(builtinHits()).toEqual([])
    })

    it('证据件：没有任何「店铺名形态」的 option', () => {
      expect(evidenceShopHits()).toEqual([])
    })

    it.skipIf(DENYLIST.state === 'missing')(
      '真值黑名单（.tools/denylist.txt，不进 git）：整个 src/** 的全部可读文本 0 命中',
      () => {
        const denylist = DENYLIST.state === 'present' ? DENYLIST.entries : []
        // 文件存在但没有任何有效条目 ⇒ **红**（绝不能变成 skip：空黑名单会让「0 命中」恒真）。
        expect(
          denylist.length,
          '黑名单文件存在但没有任何有效条目 ⇒ 红（空黑名单会让 0 命中恒真通过）',
        ).toBeGreaterThan(0)
        // 正对照（用**运行期读到的**第 1 条造内存语料，不落盘、不回显）：判定必须命中它。
        // 只传这一条，保证「恰好 1 处」不受「别的条目恰好是本条的更长超串」影响。
        const first = denylist[0]
        expect(first, '黑名单第 1 条必须存在（上面已断言 entries 非空）').toBeTypeOf('string')
        const probeDenylist: string[] = first === undefined ? [] : [first]
        expect(
          denylistHits([{ file: 'src/synthetic/probe.ts', lines: [`正对照：${first ?? ''}`] }], probeDenylist),
          '真值判定必须能命中运行期读到的黑名单第 1 条',
        ).toEqual(['src/synthetic/probe.ts:1 命中黑名单第 1 条'])
        expect(denylistHits(DENY_CORPUS.files, denylist)).toEqual([])
      },
    )
  },
)
