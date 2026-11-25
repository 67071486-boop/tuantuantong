import { request } from './http'
import type { Page } from './unwrap'

export type ChannelProfile = {
  name: string
  intro: string
  article_count: number
  avatar_text: string
}

export type ChannelArticleSummary = {
  id: number
  title: string
  intro: string
  cover_url: string
  published_at: string | null
}

export type ChannelArticleSpan = { text: string; bold: boolean; italic: boolean }
export type ChannelArticleBlock =
  | { type: 'p' | 'h2' | 'h3' | 'li'; spans: ChannelArticleSpan[] }
  | { type: 'img'; url: string }

export type ChannelArticle = Omit<ChannelArticleSummary, 'intro'> & {
  studio_name: string
  blocks: ChannelArticleBlock[]
}

export function fetchChannelProfile(): Promise<ChannelProfile> {
  return request({ method: 'GET', url: '/api/channel/profile/' })
}

export function fetchChannelArticles(page: number): Promise<Page<ChannelArticleSummary>> {
  return request({ method: 'GET', url: '/api/channel/articles/', params: { page } })
}

export function fetchChannelArticle(id: string): Promise<ChannelArticle> {
  return request({ method: 'GET', url: `/api/channel/articles/${encodeURIComponent(id)}/` })
}

export function channelImageUrl(value: string): string {
  const blobPrefix = '/api/media/blob/'
  if (value.startsWith(blobPrefix)) {
    const url = new URL(value, window.location.origin)
    return url.pathname.startsWith(blobPrefix) ? value : ''
  }
  if (!value.startsWith('https://')) return ''
  try {
    const url = new URL(value)
    return !url.username && !url.password ? value : ''
  } catch {
    return ''
  }
}
