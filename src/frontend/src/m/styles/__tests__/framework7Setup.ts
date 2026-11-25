import '../../framework7'
import { f7App } from 'framework7-vue'
import { createApp } from 'vue'
import { afterAll, beforeAll } from 'vitest'

export function installFramework7ForTest(): void {
  const host = document.createElement('div')
  const app = createApp(f7App, { theme: 'ios' })
  const matchMediaDescriptor = Object.getOwnPropertyDescriptor(window, 'matchMedia')
  beforeAll(() => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: (media: string) => ({
        media, matches: false, addEventListener() {}, removeEventListener() {},
      }),
    })
    document.body.append(host)
    app.mount(host)
  })
  afterAll(() => {
    app.unmount()
    host.remove()
    if (matchMediaDescriptor) Object.defineProperty(window, 'matchMedia', matchMediaDescriptor)
    else Reflect.deleteProperty(window, 'matchMedia')
  })
}
