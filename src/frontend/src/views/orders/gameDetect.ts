/**
 * 「按内容里的游戏名认游戏」的纯函数（spec
 * `docs/specs/2026-10-03-addorder-auto-game-and-default-source-design.md` §3.1 的 10 条规则）。
 * 只回答一件事：**这段文本最可能对应字典里的哪一款游戏**。不联网、不查后端、不看权限，也不读页面状态——调用方（录单页）自己决定命中之后要不要切游戏。
 */

export type DetectedGame = { id: number; name: string }

/**
 * 三种结果，**没有第四种**（比如「勉强算命中」）：`hit` 最长的那一组只有一款游戏，可以切；`ambiguous` 最长的一组里有多款（含同名不同 id）——**平手不猜**，交给客服手动选；`none` 一个都没命中，**静默**（内容里没提游戏是常态）。
 */
export type GameDetectResult =
  | { kind: 'hit'; game: DetectedGame }
  | { kind: 'ambiguous'; names: string[] }
  | { kind: 'none' }

/**
 * 单个字符是不是 ASCII 字母或数字。这里**不用正则**，和下面的包含比对同一个理由：本模块的比对全部走下标扫描，一条正则都不进（名字里可能出现 `：` `.` `(` 这类字符，交给正则就要先转义；也避免 ReDoS）。
 */
function isAsciiAlnum(ch: string): boolean {
  const code = ch.charCodeAt(0)
  return (code >= 48 && code <= 57) || (code >= 65 && code <= 90) || (code >= 97 && code <= 122)
}

/** 名字是不是**全由** ASCII 字母数字组成（`LOL` / `CF` / `DNF` 是，`DNF手游` 不是）。 */
function isAsciiWordName(name: string): boolean {
  if (name === '') return false
  for (let i = 0; i < name.length; i += 1) {
    if (!isAsciiAlnum(name[i] as string)) return false
  }
  return true
}

/**
 * 在 `haystack` 里找 `needle` 的下标；找不到回 `-1`。`needle` 全是 ASCII 字母数字时（`requireBoundary`）额外要求**词形边界**：命中位置左右两侧要么不存在（串首/串尾），要么**不是** ASCII 字母数字。
 * 为什么非要这一条：`LOL` / `CF` / `DNF` 这类缩写只有两三个字母，一旦不卡边界，`LOLITA` 里的 `LOL`、`ALOL` 里的 `LOL` 都会被当成游戏名。代价方向是**少认**——客服没被自动切游戏，自己点一下就行；而切错了（尤其切到另一款游戏、顺手清掉已填的区服/分类）客服多半不会发现。**宁可不认，不可认错。**
 * 边界只卡「全 ASCII 名」：中文名（如 `原神`）夹在字母里（`a原神b`）仍算命中——中文名本身已经足够长、有区分度，再卡边界只会白白漏认。
 */
function findHitIndex(haystack: string, needle: string, requireBoundary: boolean): number {
  let from = 0
  for (;;) {
    const at = haystack.indexOf(needle, from)
    if (at < 0) return -1
    if (!requireBoundary) return at
    const before = at === 0 ? '' : (haystack[at - 1] as string)
    const afterAt = at + needle.length
    const after = afterAt >= haystack.length ? '' : (haystack[afterAt] as string)
    if ((before === '' || !isAsciiAlnum(before)) && (after === '' || !isAsciiAlnum(after))) {
      return at
    }
    // 这个位置卡住了边界（如 `LOLITA` 里的 `LOL`），从下一个下标继续找。⚠️ `at + 1` 与 `at + needle.length` 在本谓词下**完全等价**（走到这里时 `needle` 必然全是 ASCII 字母数字 ⇒ 任一重叠候选的左侧字符必然是字母数字、必然同样被卡住）。
    // 独立评审把这里改成 `at + needle.length` 做过变异，27 条用例**全绿（SURVIVED）** ⇒ 这是**等价的防御性写法**，⛔ 不是承重逻辑（`AGENTS.md` §5-E：防御性写法不要写成承重逻辑）。
    from = at + 1
  }
}

/**
 * **内置的「游戏名 → 额外关键词」表**（业务方追加要求）。商品标题里常常只写**俗称**（「崩铁」「洛克王国」「终末地」），而字典里的游戏名是官方全名（「崩坏：星穹铁道」）⇒ 只按全名比会**漏认**。
 * ⚠️ **键是「归一化之后」的游戏名**（见 `normalizeGameName`）：租户字典实测是全角冒号写法（`崩坏：星穹铁道` / `洛克王国：世界` / `明日方舟：终末地`），这里按**只留字母数字**登记 ⇒ 两边都归一化再查表。⚠️ 归一化**不折叠**版本后缀（`…Ⅱ` / `…2`）——租户若写 `崩坏：星穹铁道Ⅱ` 就对不上这里的键 ⇒ **关键词静默不生效**，只能在表里按它的实际写法**再登记一行**。⚠️ 它只影响「**认出哪一款**」；返回的 `game.name` 仍是**字典里的原样名字**（下游要用它做精确同名查找，见 spec §3.2 接入点 B）。
 * ⚠️ 表是**手工维护**的：加一款游戏的关键词 = 在这里加一行（键写只留字母数字的全名）。查表是**逐字**比较（**不**折大小写）⇒ 键里若含 ASCII 字母，必须写成租户字典里的**实际大小写**，否则静默查不到。⛔ 不是拼音/模糊纠错（那条仍然不做），就是一份写死的俗称清单。
 */
export const BUILTIN_GAME_KEYWORDS: Record<string, readonly string[]> = {
  崩坏星穹铁道: ['崩铁', '崩坏'],
  洛克王国世界: ['洛克王国'],
  明日方舟终末地: ['终末地'],
}

/**
 * 归一化游戏名：**只用于「查上面那张关键词表」这一个用途**（⛔ 不是拿来跟商品标题比的）。做法 = **只留字母与数字**（Unicode 的 `\p{L}` / `\p{N}`），其余（空白、ASCII 标点、全角标点、括号 / 书名号 / 引号 / 间隔号 / 波浪号 / 全角加号……）一律去掉 ⇒ 「崩坏：星穹铁道」「崩坏 星穹铁道」「「崩坏：星穹铁道」」都指向同一份关键词。
 * ⚠️ **边界（实测，别把它当"什么写法都认"）**：它**不折叠名称变体**——罗马数字 / 版本号是**保留**的。租户若写 `崩坏：星穹铁道Ⅱ`，归一化 ≠ 表键 ⇒ 关键词**不生效**。这是刻意的：`…Ⅱ` 与 `…` 可能是**两款不同的游戏**，折叠了会认错（代价方向：少认优先于认错）。
 */
export function normalizeGameName(name: string): string {
  return name.replace(/[^\p{L}\p{N}]/gu, '')
}

/** 一个游戏的全部**候选写法**：字典里的名字 +（查表命中时）它的内置关键词。 */
function candidateNames(gameName: string): string[] {
  const trimmed = gameName.trim()
  const extra = BUILTIN_GAME_KEYWORDS[normalizeGameName(trimmed)] ?? []
  return [trimmed, ...extra]
}

/**
 * 在一段文本里按**游戏字典**（＋内置关键词）认游戏名。规则照 spec §3.1 的十条：空文本 ⇒ `none`（一次都不比）；名字 trim 后 <2 字**跳过**；两边 `toLowerCase()` 后用 `indexOf` 下标扫描判包含（**不用正则**）；全 `[A-Za-z0-9]` 名卡词形边界（见 `findHitIndex`）；命中集合取 trim 后长度**最大**的那一组——单款 ⇒ `hit`，≥2 款 ⇒ `ambiguous`（`names` 为该组各游戏名字，去重按**字典原样名**），零命中 ⇒ `none`；
 * 每个游戏的候选 = **字典名 ＋ 内置关键词**（`BUILTIN_GAME_KEYWORDS`，按归一化名字查表），一款游戏只算**一条**命中、长度取它最长的命中候选。
 * @param text 要比对的文本（录单页传的是**商品标题**或**粘贴的整段原文**）；`games` = 游戏字典（已按权限收窄的那份选项）。
 */
export function detectGameFromText(
  text: string,
  games: readonly { id: number; name: string }[],
): GameDetectResult {
  // 规则 1：没有内容就直接回，后面每一步都省了。
  if (text.trim() === '') return { kind: 'none' }

  // 规则 3：两边都转小写后比对。⚠️ 边界字符也从**这份小写文本**上取，
  // 免得某些字符（如 `İ`）lowercase 之后长度变化导致两套下标对不上。
  const haystack = text.toLowerCase()

  // 命中的游戏：留下字典里的**原样名字**（不 trim）。
  // 页面拿到 `hit` 后要把这个名字塞回去做**精确同名**查找（spec §3.2 接入点 B），
  // 回字典里的原串才靠得上；长度比较另用 trim 后的长度（规则 6）。
  const hits: { id: number; name: string; nameLength: number }[] = []
  for (const game of games) {
    /**
     * 规则 2 / 规则 10：候选 = 字典里的名字 **＋ 内置关键词**（见 `candidateNames`），不足 2 字的**候选**跳过（名字与关键词同一个门槛——单字会到处误命中）。
     * ⚠️ **一个游戏只算一条命中**，长度取它**最长**的那个命中的候选：同一款游戏的名字与关键词同时出现时，不该因为长度不同而把自己判成「平手」。
     */
    let best: number | null = null
    for (const candidate of candidateNames(game.name)) {
      const trimmed = candidate.trim()
      if (trimmed.length < 2) continue
      const needle = trimmed.toLowerCase()
      // 规则 4/5：下标扫描；全 ASCII 名额外卡词形边界。
      if (findHitIndex(haystack, needle, isAsciiWordName(needle)) < 0) continue
      if (best === null || trimmed.length > best) best = trimmed.length
    }
    if (best === null) continue
    hits.push({ id: game.id, name: game.name, nameLength: best })
  }

  // 规则 9：一款都没命中。
  if (hits.length === 0) return { kind: 'none' }

  // 规则 6：取名字最长的那一组（不是「谁先命中」，也不是「谁在字典里靠前」）。
  let longest = 0
  for (const hit of hits) {
    if (hit.nameLength > longest) longest = hit.nameLength
  }
  const longestHits = hits.filter((hit) => hit.nameLength === longest)

  // 规则 7：最长的一组只有一个游戏 ⇒ 认它。
  if (longestHits.length === 1) {
    const only = longestHits[0] as { id: number; name: string; nameLength: number }
    return { kind: 'hit', game: { id: only.id, name: only.name } }
  }

  // 规则 8：最长的一组有多款（含同名不同 id）⇒ 平手不猜。
  // 认错游戏比不认更糟：切错了客服多半不会发现（spec §3.1 规则 8 的「理由」）。
  // 去重按**字典里的原样名字**（`' 原神 '` 与 `'原神'` 算两个不同名字）——与上面回原串的口径一致。
  const names: string[] = []
  for (const hit of longestHits) {
    if (!names.includes(hit.name)) names.push(hit.name)
  }
  return { kind: 'ambiguous', names }
}
