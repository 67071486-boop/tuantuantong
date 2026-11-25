import { expect, type Page } from '@playwright/test'

/**
 * 跨规格共享的**读写后端**小工具（目前只有「按名字查字典」这一类）。
 *
 * ⛔ 为什么放在夹具层而不是各 spec 各写一份（终审 Minor）：`02-golden-path` 与 `03-permission`
 * 都需要「拿界面/用例里写的那款游戏名，换成它当前的主键」。两处各写一份会像 `switchAccount`
 * 那样**各自漂移**（那处的教训见 `fixtures/auth.ts`）⇒ 只留这一份。
 * ⚠️ 判据一律是**名字**，⛔ 不是写死的主键：写死 PK 的断言在换库 / 重新 seed 后会变成假红，
 * 而它本来也证明不了「就是这一款」。
 */
type Envelope<T> = { code: number; msg: string; data: T }

/** 从**游戏字典**接口按**名字**取那一款游戏的 id。 */
export async function gameIdByName(page: Page, name: string): Promise<number> {
  const res = await page.request.get('/api/business/games/?page_size=200')
  expect(res.status(), '读游戏字典失败').toBe(200)
  const body = (await res.json()) as Envelope<{ items: Array<{ id: number; name: string }> }>
  expect(body.code, `读游戏字典的业务码非 0：${body.msg}`).toBe(0)
  const hit = body.data.items.find((row) => row.name === name)
  expect(hit, `游戏字典里应当有「${name}」`).toBeTruthy()
  return hit!.id
}
