import type { User } from '@/api/auth'

export interface HallFilter {
  min: string
  max: string
  include: string
  exclude: string
}

export const EMPTY_HALL_FILTER: Readonly<HallFilter> = { min: '', max: '', include: '', exclude: '' }

export function hallFilterKey(user: User | null, game: number | null): string | null {
  if (!user?.identity_plane || game === null) return null
  return 'tt_h5_hall_filter_v1:' + JSON.stringify([user.identity_plane, user.id, game])
}

export function splitHallKeywords(value: string): string[] {
  return value.split(/[,，、\s]+/).filter(Boolean)
}

function isHallPrice(value: string): boolean {
  return value === '' || (value.length <= 12 && /^\d{1,10}(\.\d{1,2})?$/.test(value))
}

function priceInCents(value: string): number {
  const [whole, fraction = ''] = value.split('.')
  return Number(whole) * 100 + Number(fraction.padEnd(2, '0'))
}

export function validateHallFilter(filter: HallFilter): string {
  if (!isHallPrice(filter.min) || !isHallPrice(filter.max)) {
    return '请输入有效金额，最多保留两位小数'
  }
  if (filter.min && filter.max && priceInCents(filter.min) > priceInCents(filter.max)) {
    return '最低价不能高于最高价'
  }
  return ''
}

export function readHallFilter(key: string): HallFilter {
  try {
    const raw = localStorage.getItem(key)
    if (raw !== null) {
      const { min, max, include, exclude } = JSON.parse(raw)
      if (typeof min === 'string' && typeof max === 'string'
        && typeof include === 'string' && typeof exclude === 'string') {
        const filter = { min, max, include, exclude }
        if (!validateHallFilter(filter)) return filter
      }
    }
  } catch {
    // 浏览器禁用存储或旧值损坏时按无筛选处理。
  }
  return { ...EMPTY_HALL_FILTER }
}

export function writeHallFilter(key: string, filter: HallFilter): void {
  try {
    if (filter.min || filter.max || filter.include || filter.exclude) {
      localStorage.setItem(key, JSON.stringify(filter))
    } else {
      localStorage.removeItem(key)
    }
  } catch {
    // 存储失败时仍可在当次访问中筛选。
  }
}
