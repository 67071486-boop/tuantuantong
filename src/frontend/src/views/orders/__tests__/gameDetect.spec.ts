import { describe, expect, it } from 'vitest'
import { detectGameFromText } from '../gameDetect'

/**
 * 只测**纯函数** `detectGameFromText`：spec `docs/specs/2026-10-03-addorder-auto-game-and-default-source-design.md` §3.1 的 9 条规则 ＋ §4-D 的 D1–D10。页面接线（§3.2/§3.3）另在 `pages/__tests__/order-addorder.spec.ts`。
 * 夹具纪律（`AGENTS.md` §5-C / E-0077）：**文本一律是合成值**——只用「合成商品标题甲 / 合成后缀乙」这类占位词，⛔ 不抄真实商品标题、真实来源单号；游戏名尽量取本仓已有的字典值（`src/config/game-library.ts`），⛔ 那不是客户数据。⚠️ 例外：`LOL` / `DNF` 是 spec **点名**的 ASCII 词形名（本仓字典里没有它们，当场 grep 验过），通用缩写、不含客户数据。
 * ⚠️ 一条**刻意不设判据**的地方：规则 1 的早退是**可证明的冗余**（needle 长度恒 ≥2、空串上 `indexOf` 恒 `-1` ⇒ 不早退也必然落到规则 9）。它的**行为**由「空文本 ⇒ none」「纯空白 ⇒ none」两条用例钉着；**实现的书写顺序不设判据**（免得后来者去追一个抓不到的变异）。
 */
const game = (id: number, name: string) => ({ id, name })

describe('detectGameFromText · 规则 1：空内容直接 none（一次都不比）', () => {
  it('空串 ⇒ none，即使字典非空', () => {
    expect(detectGameFromText('', [game(1, '三角洲行动')])).toEqual({ kind: 'none' })
  })

  it('只有空白字符 ⇒ none', () => {
    expect(detectGameFromText('  \t\n ', [game(1, '三角洲行动')])).toEqual({ kind: 'none' })
  })
})

describe('detectGameFromText · 规则 2：名字 trim 后不足 2 字 ⇒ 跳过这个游戏', () => {
  it('D2：单字名不参与比对——文本里出现了「原」也不算命中', () => {
    expect(detectGameFromText('合成商品标题甲 原 合成后缀乙', [game(1, '原')])).toEqual({
      kind: 'none',
    })
  })

  it('D2：单字名被跳过之后，同一字典里其余游戏照常比对', () => {
    expect(
      detectGameFromText('合成商品标题甲 原神 合成后缀乙', [game(1, '原'), game(2, '原神')]),
    ).toEqual({ kind: 'hit', game: { id: 2, name: '原神' } })
  })

  it('名字 trim 后成了空串（纯空白）⇒ 同样跳过，不炸', () => {
    expect(detectGameFromText('合成商品标题甲 合成后缀乙', [game(1, '   ')])).toEqual({
      kind: 'none',
    })
  })
})

describe('detectGameFromText · 规则 3/4：按字面包含比对（转小写、纯下标扫描）', () => {
  it('D3：商品标题里带完整游戏名 ⇒ 命中那个 id', () => {
    expect(
      detectGameFromText('合成商品标题甲｜三角洲行动 合成后缀乙', [game(42, '三角洲行动')]),
    ).toEqual({ kind: 'hit', game: { id: 42, name: '三角洲行动' } })
  })

  it('D4：文本里是小写 `lol`、字典里是 `LOL` ⇒ 命中（大小写归一）', () => {
    expect(detectGameFromText('合成商品标题甲 lol 合成后缀乙', [game(7, 'LOL')])).toEqual({
      kind: 'hit',
      game: { id: 7, name: 'LOL' },
    })
  })

  it('反过来（文本大写、字典小写）也命中', () => {
    expect(detectGameFromText('合成商品标题甲 DNF 合成后缀乙', [game(8, 'dnf')])).toEqual({
      kind: 'hit',
      game: { id: 8, name: 'dnf' },
    })
  })

  it('规则 4：名字里有全角冒号（正则元字符候选）也按字面命中', () => {
    expect(
      detectGameFromText('合成商品标题甲 崩坏：星穹铁道 合成后缀乙', [game(5, '崩坏：星穹铁道')]),
    ).toEqual({ kind: 'hit', game: { id: 5, name: '崩坏：星穹铁道' } })
  })

  it('规则 4：名字里带括号与点号也按字面命中（不被当元字符）', () => {
    expect(
      detectGameFromText('合成商品标题甲 合成游戏(测试).甲 合成后缀乙', [game(6, '合成游戏(测试).甲')]),
    ).toEqual({ kind: 'hit', game: { id: 6, name: '合成游戏(测试).甲' } })
  })
})

describe('detectGameFromText · 规则 5：全 ASCII 名字要卡词形边界', () => {
  it('D5 反向：`LOLITA 代购` 不算命中 `LOL`（右侧是字母）', () => {
    expect(detectGameFromText('合成商品标题甲 LOLITA 代购 合成后缀乙', [game(7, 'LOL')])).toEqual({
      kind: 'none',
    })
  })

  it('D5 正向：`DNF搬砖` 算命中 `DNF`（右侧汉字不是 [A-Za-z0-9]）', () => {
    expect(detectGameFromText('合成商品标题甲 DNF搬砖 合成后缀乙', [game(8, 'DNF')])).toEqual({
      kind: 'hit',
      game: { id: 8, name: 'DNF' },
    })
  })

  it('串首 / 串尾也算边界：整段就是 `DNF`、以及「代练 LOL」结尾', () => {
    expect(detectGameFromText('DNF', [game(8, 'DNF')])).toEqual({
      kind: 'hit',
      game: { id: 8, name: 'DNF' },
    })
    expect(detectGameFromText('合成代练 LOL', [game(7, 'LOL')])).toEqual({
      kind: 'hit',
      game: { id: 7, name: 'LOL' },
    })
  })

  it('左右两侧夹数字也不算边界（`LOL2` / `2LOL` 都不命中）', () => {
    expect(detectGameFromText('合成商品标题甲 LOL2 合成后缀乙', [game(7, 'LOL')])).toEqual({
      kind: 'none',
    })
    expect(detectGameFromText('合成商品标题甲 2LOL 合成后缀乙', [game(7, 'LOL')])).toEqual({
      kind: 'none',
    })
  })

  it('边界只对全 ASCII 名生效：中文名夹在字母里照样命中', () => {
    expect(detectGameFromText('合成商品标题甲 a原神b 合成后缀乙', [game(1, '原神')])).toEqual({
      kind: 'hit',
      game: { id: 1, name: '原神' },
    })
  })
})

describe('detectGameFromText · 规则 6/7：取最长的那一组；那一组只有一款 ⇒ hit', () => {
  it('D6：`魔兽世界` 与 `魔兽世界怀旧服` 都出现 ⇒ 取长的那个', () => {
    expect(
      detectGameFromText('合成商品标题甲 魔兽世界 与 魔兽世界怀旧服 合成后缀乙', [
        game(1, '魔兽世界'),
        game(2, '魔兽世界怀旧服'),
      ]),
    ).toEqual({ kind: 'hit', game: { id: 2, name: '魔兽世界怀旧服' } })
  })

  it('规则 6：长度比较是「名字的长度」，不是「谁先命中」——短名排前面也照样输', () => {
    expect(
      detectGameFromText('合成商品标题甲 三角洲行动 合成后缀乙', [
        game(1, '三角洲'),
        game(2, '三角洲行动'),
      ]),
    ).toEqual({ kind: 'hit', game: { id: 2, name: '三角洲行动' } })
  })

  it('规则 6：长度基准是 **trim 之后**的长度——字典里那串全角空格不算「更长」', () => {
    // 字典 1 的原串是 `　　` + `原神`（两个 U+3000 全角空格，原串 length 4，trim 后 2）；字典 2 是 `三角洲`（原串 length 3）。两款都出现在文本里：
    //  · 按 **trim 后**的长度比 ⇒ 2 < 3 ⇒ id=2（三角洲）；· 按 **原串** length 比 ⇒ 4 > 3 ⇒ id=1（原神）——这条用例就是用来分辨这两者的。
    expect(
      detectGameFromText('合成商品标题甲 原神 与 三角洲 合成后缀乙', [
        game(1, '\u3000\u3000原神'),
        game(2, '三角洲'),
      ]),
    ).toEqual({ kind: 'hit', game: { id: 2, name: '三角洲' } })
  })
})

describe('detectGameFromText · 规则 8：最长的一组有多款 ⇒ ambiguous，不猜', () => {
  it('D7：两款等长游戏都在文本里 ⇒ ambiguous，names 按入参顺序', () => {
    expect(
      detectGameFromText('合成商品标题甲 王者荣耀 与 和平精英 合成后缀乙', [
        game(1, '王者荣耀'),
        game(2, '和平精英'),
      ]),
    ).toEqual({ kind: 'ambiguous', names: ['王者荣耀', '和平精英'] })
  })

  it('D7：入参顺序反了，names 也跟着反（按入参顺序，不是文本里出现的先后）', () => {
    expect(
      detectGameFromText('合成商品标题甲 王者荣耀 与 和平精英 合成后缀乙', [
        game(2, '和平精英'),
        game(1, '王者荣耀'),
      ]),
    ).toEqual({ kind: 'ambiguous', names: ['和平精英', '王者荣耀'] })
  })

  it('D7：更短的那款即使也命中，也不进 ambiguous 名单（只报最长的一组）', () => {
    expect(
      detectGameFromText('合成商品标题甲 王者荣耀 与 和平精英 合成后缀乙', [
        game(3, '荣耀'),
        game(1, '王者荣耀'),
        game(2, '和平精英'),
      ]),
    ).toEqual({ kind: 'ambiguous', names: ['王者荣耀', '和平精英'] })
  })

  it('D10：同名不同 id 的两款都被命中 ⇒ ambiguous，names 去重后只剩一个名字', () => {
    expect(
      detectGameFromText('合成商品标题甲 合成重名游戏 合成后缀乙', [
        game(1, '合成重名游戏'),
        game(2, '合成重名游戏'),
      ]),
    ).toEqual({ kind: 'ambiguous', names: ['合成重名游戏'] })
  })

  it('规则 8：`原神` 与 ` 原神 ` 是两个不同的字典名 ⇒ 去重键用**原样名**，两条都留在 names 里', () => {
    // 合成文本 `原神代练 原神 收尾` 同时含 `原神`（下标 0）与 ` 原神 `（两侧都是空格）⇒ 两款都命中，trim 后等长（都是 2）⇒ 进同一组。names 按入参顺序保留**各自的字典原样名**：
    // 去重若改用 trim 后的名，第二条会被当成重复丢掉 ⇒ names 只剩 ['原神']，与页面「平手让客服选」的口径不符（同一款游戏的两个字典条目对客服本就是两个不同选项）。
    expect(
      detectGameFromText('原神代练 原神 收尾', [game(1, '原神'), game(2, ' 原神 ')]),
    ).toEqual({ kind: 'ambiguous', names: ['原神', ' 原神 '] })
  })
})

describe('detectGameFromText · 规则 9：一个都没命中 ⇒ none', () => {
  it('D8：文本里一款游戏名都没有 ⇒ none（静默是常态）', () => {
    expect(
      detectGameFromText('合成商品标题甲 合成后缀乙', [
        game(1, '三角洲行动'),
        game(2, '王者荣耀'),
      ]),
    ).toEqual({ kind: 'none' })
  })

  it('字典为空数组 ⇒ none，不炸', () => {
    expect(detectGameFromText('合成商品标题甲 三角洲行动', [])).toEqual({ kind: 'none' })
  })
})

describe('detectGameFromText · 规则 2 的 trim 同时作用于比对', () => {
  it('D9：字典名两侧带空格 ` 原神 ` ⇒ 按 trim 后的名字命中，且 `hit.game.name` 是字典原样名（带空格）', () => {
    // 这里**冻结**命中的 `game.name`：回的是**字典里的原样字符串**（两侧的半角空格不动）。
    // 页面拿到 `hit` 后要把它塞回**同一份字典**做**精确同名**查找（spec §3.2 接入点 B），
    // ⇒ 一旦实现改成回 trim 过的名字，那条查找就落空（静默不切游戏）⇒ 必须逐字相等，不用 toMatchObject。
    expect(detectGameFromText('合成商品标题甲 原神 合成后缀乙', [game(9, ' 原神 ')])).toEqual({
      kind: 'hit',
      game: { id: 9, name: ' 原神 ' },
    })
  })

  it('规则 2：名字两侧的空格不参与比对——文本里是「原神」而两侧不是空格，仍应命中', () => {
    // 这条与上面那条**必须分辨**：上面那条文本里恰好含 ` 原神 `（两边都有空格），拿**原样名**去比也能命中 ⇒ 单靠它证明不了「比对前先 trim」。
    // 这里的合成文本两侧都不是空格（前者是汉字「合成甲」、后者是「合成乙」）：只有用 trim 后的 `原神` 当 needle 才命中；用原样名 ` 原神 ` 比 ⇒ none。
    expect(detectGameFromText('合成甲原神合成乙', [game(9, ' 原神 ')])).toEqual({
      kind: 'hit',
      game: { id: 9, name: ' 原神 ' },
    })
  })
})

describe('detectGameFromText · 规则 10：内置关键词（业务方 2026-10-03 追加要求）', () => {
  it('D14：字典名带全角冒号（`崩坏：星穹铁道`）、文本里只写了俗称「崩铁」⇒ 认它，name 仍回字典原样名', () => {
    // 关键词表的**键**是归一化后的名字（去掉 `：` 等标点）⇒ 租户怎么写都能对上表。
    // `name` 必须回字典原样串：页面要拿它回**同一份字典**做精确同名查找（spec §3.2 接入点 B）。
    expect(detectGameFromText('合成商品标题甲 崩铁 双人代练', [game(3, '崩坏：星穹铁道')])).toEqual({
      kind: 'hit',
      game: { id: 3, name: '崩坏：星穹铁道' },
    })
  })

  it('D15：`崩坏：星穹铁道` 的第二个关键词「崩坏」也认', () => {
    expect(detectGameFromText('合成标题甲 崩坏 代练', [game(3, '崩坏：星穹铁道')])).toEqual({
      kind: 'hit',
      game: { id: 3, name: '崩坏：星穹铁道' },
    })
  })

  it('D16：`洛克王国：世界` 用关键词「洛克王国」认', () => {
    expect(detectGameFromText('合成标题甲 洛克王国 代练', [game(10, '洛克王国：世界')])).toEqual({
      kind: 'hit',
      game: { id: 10, name: '洛克王国：世界' },
    })
  })

  it('D17：`明日方舟：终末地` 用关键词「终末地」认', () => {
    expect(detectGameFromText('合成标题甲 终末地 代练', [game(12, '明日方舟：终末地')])).toEqual({
      kind: 'hit',
      game: { id: 12, name: '明日方舟：终末地' },
    })
  })

  it('D18：字典里**没有**那款游戏 ⇒ 关键词不生效（none）——关键词只认字典里已有的游戏', () => {
    expect(detectGameFromText('合成标题甲 崩铁 代练', [game(1, '原神')])).toEqual({ kind: 'none' })
  })

  it('D19：关键词与另一款游戏的名字同时命中 ⇒ 仍按**长度**取最长（`崩坏3` 赢过「崩坏」）', () => {
    expect(
      detectGameFromText('合成标题甲 崩坏3 代练', [
        game(3, '崩坏：星穹铁道'),
        game(9, '崩坏3'),
      ]),
    ).toEqual({ kind: 'hit', game: { id: 9, name: '崩坏3' } })
  })

  it('D20：同一款游戏的**两个关键词同时出现** ⇒ 只算**一条**命中（⛔ 不能把自己判成"平手"）', () => {
    // 这一条钉的是「一个游戏只算一条命中」：若按"每个候选各算一条命中"写，
    // `崩坏：星穹铁道` 会以「崩铁」「崩坏」两条等长（都是 2）的命中进最长组 ⇒ 明明只有一款游戏，
    // 却回 `ambiguous`、页面提示「出现了多款游戏（崩坏：星穹铁道），请手动选择」——**荒谬的提示**。
    expect(
      detectGameFromText('合成标题甲 崩铁 崩坏 代练', [game(3, '崩坏：星穹铁道'), game(1, '原神')]),
    ).toEqual({ kind: 'hit', game: { id: 3, name: '崩坏：星穹铁道' } })
  })
})

describe('detectGameFromText · 规则 10 的补充判据（2026-10-03 独立审查后补）', () => {
  it('D21：本款游戏的**关键词**与另一款**更长的真名**同时命中 ⇒ 取最长（`崩坏3` 赢过关键词「崩坏」）', () => {
    // 这条钉的是 `best` 那句比较（候选里取**最长**的那个）。⚠️ **独立评审实测**：把它改成「最后命中的候选胜」时，**全量 2731 条一条都不红**（判据缺口）⇒ 补这一条。
    // 危害现场（评审的真跑反例）：字典同时有 `崩坏：星穹铁道`（关键词「崩坏」2 字）与 `崩坏3`（真名 3 字），商品标题两款都提到 ⇒ 正确实现回 **id 3**（7 字全名最长），坏实现回 **id 9（静默切错游戏）**。
    expect(
      detectGameFromText('合成标题甲 崩坏：星穹铁道 崩坏3 双人代练', [
        game(3, '崩坏：星穹铁道'),
        game(9, '崩坏3'),
      ]),
    ).toEqual({ kind: 'hit', game: { id: 3, name: '崩坏：星穹铁道' } })
  })

  it('D22：字典名外面套着装饰（`「」` / 波浪号 / 全角加号 / 全角空格）也认得出关键词', () => {
    // 归一化 = **只留字母数字**（`\p{L}` / `\p{N}`）⇒ 上面这些装饰一律被去掉。
    // ⚠️ 旧实现只去掉一张手写标点表，`「」`、`～`、`＋` 都不在里面 ⇒ 关键词**静默失效**（评审探针 P6/P7）。
    for (const name of ['「崩坏：星穹铁道」', '崩坏～星穹铁道', '崩坏＋星穹铁道', '崩坏\u3000星穹铁道']) {
      expect(detectGameFromText('合成标题甲 崩铁 代练', [game(3, name)])).toEqual({
        kind: 'hit',
        game: { id: 3, name },
      })
    }
  })

  it('D23：**边界（刻意）**：名称带版本后缀 `崩坏：星穹铁道Ⅱ` ⇒ 关键词**不生效**', () => {
    // 归一化**不折叠**名称变体（罗马数字 / 版本号保留）：`…Ⅱ` 与 `…` 可能是**两款不同的游戏**，
    // 折了就会认错（代价方向：少认优先于认错）。要覆盖它只能在 `BUILTIN_GAME_KEYWORDS` 里
    // **按实际写法再登记一行**。这条用例把这个边界**钉死**，免得后来者以为"什么写法都认"。
    expect(detectGameFromText('合成标题甲 崩铁 代练', [game(3, '崩坏：星穹铁道Ⅱ')])).toEqual({
      kind: 'none',
    })
  })
})
