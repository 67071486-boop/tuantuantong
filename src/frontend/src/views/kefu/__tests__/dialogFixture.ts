import { afterAll, beforeAll } from 'vitest'
import { createApp, h } from 'vue'
import { f7App as F7App } from 'framework7-vue'
import { DOMWrapper, type VueWrapper } from '@vue/test-utils'
import '../framework7'

export function provideFramework7(theme: 'ios' | 'md' = 'ios'): void {
  const host = document.createElement('div')
  const app = createApp({ render: () => h(F7App, { theme }) })
  const matchMedia = Object.getOwnPropertyDescriptor(window, 'matchMedia')
  beforeAll(() => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        addEventListener() {},
        removeEventListener() {},
      }),
    })
    document.body.append(host)
    app.mount(host)
  })
  afterAll(() => {
    app.unmount()
    host.remove()
    if (matchMedia) Object.defineProperty(window, 'matchMedia', matchMedia)
    else Reflect.deleteProperty(window, 'matchMedia')
  })
}

export function popupOption(value: number | null): DOMWrapper<Element> {
  const element = document.querySelector(`.kefu-dialog.modal-in button[data-option-id="${value ?? 'empty'}"]`)
  if (!element) throw new Error(`Missing visible option ${value}`)
  return new DOMWrapper(element)
}

export async function pickSelect(wrapper: VueWrapper, selector: string, value: number | null): Promise<void> {
  await wrapper.get(selector).trigger('click')
  await popupOption(value).trigger('click')
}
