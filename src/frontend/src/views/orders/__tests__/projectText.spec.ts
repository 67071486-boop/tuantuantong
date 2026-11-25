// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { toggleProjectName } from '../projectText'

/**
 * 「常用项目」与「项目名称」之间的拼/删（录单岛页与客服精简工作台共用同一份实现）。
 * ⚠️ 这里的**前缀同名**用例是评审实测出来的误删形态：项目 A「星耀」是 B「星耀上王者」的前缀，旧的子串 `replace` 删法会把 B 改成「上王者」。
 */
describe('toggleProjectName', () => {
  it('勾选：空文本就是名字；非空按 / 拼接', () => {
    expect(toggleProjectName('', '星耀上王者', true)).toBe('星耀上王者')
    expect(toggleProjectName('星耀', '星耀上王者', true)).toBe('星耀/星耀上王者')
  })

  it('勾选：同一个整词已在文本里 ⇒ 不重复拼', () => {
    expect(toggleProjectName('星耀/星耀上王者', '星耀上王者', true)).toBe('星耀/星耀上王者')
  })

  it('取消：按 / 分词做整词删除，前缀同名的另一项不受影响', () => {
    expect(toggleProjectName('星耀/星耀上王者', '星耀', false)).toBe('星耀上王者')
    expect(toggleProjectName('星耀/星耀上王者', '星耀上王者', false)).toBe('星耀')
  })

  it('取消：名字不在文本里 ⇒ 原样返回；删空后是空串', () => {
    expect(toggleProjectName('星耀', '别的项目', false)).toBe('星耀')
    expect(toggleProjectName('星耀', '星耀', false)).toBe('')
  })

  it('空名字不拼也不删（防数据源给了空名把 / 拼坏）', () => {
    expect(toggleProjectName('星耀', '', true)).toBe('星耀')
    expect(toggleProjectName('星耀/', '', false)).toBe('星耀/')
  })
})
