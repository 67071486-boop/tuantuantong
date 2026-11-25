import { artifactPage } from '@/layui/artifactPage'
import type { IslandContext, IslandPage } from '@/layui/island'
import { mountLegacyRecords } from '../onlinePayroll'

const base = artifactPage({ url: 'replica/emp-onlinepaytorecdetail.html' })

export default {
  mount: async (root: HTMLElement, ctx: IslandContext): Promise<() => void> => {
    document.title = '在线打款明细记录'
    const unmount = await base.mount(root, ctx)
    const list = root.querySelector<HTMLElement>('#tableList')?.parentElement
    const detail = root.querySelector<HTMLElement>('#tableList2')?.parentElement
    if (!list || !detail) return unmount
    root.querySelector('.layui-form-pane')?.setAttribute('hidden', '')
    list.replaceChildren()
    detail.replaceChildren()
    const summary = root.querySelector('.total1')
    if (summary) summary.textContent = '在线打款明细记录'
    const batch = new URLSearchParams(window.location.search).get('batch') ?? undefined
    const stop = mountLegacyRecords(list, detail, false, batch)
    return () => {
      stop()
      unmount()
    }
  },
} satisfies IslandPage
