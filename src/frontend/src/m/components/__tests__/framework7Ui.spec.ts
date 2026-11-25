// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { f7ready } from 'framework7-vue'
import { expect, it, vi } from 'vitest'
import { installFramework7ForTest } from '../../styles/__tests__/framework7Setup'
import { confirm, Toast } from '../../framework7Ui'
import MobilePhotoBrowser from '../MobilePhotoBrowser.vue'

installFramework7ForTest()

it('服务端文案在确认框和提示里按纯文本显示', () => {
  const message = '<img src=x onerror="alert(1)"> 他&她'
  const action = vi.fn()
  confirm({ title: '抢单确认', content: message, confirmBtn: '接单', cancelBtn: '再想想', onConfirm: action })
  Toast.error({ message, duration: 6000 })

  const dialog = document.querySelector<HTMLElement>('.dialog[role="alertdialog"]')!
  const toast = document.querySelector<HTMLElement>('.toast[role="status"]')!
  expect(dialog.querySelector('.dialog-text')?.textContent).toBe(message)
  expect(toast.querySelector('.toast-text')?.textContent).toBe(message)
  expect(dialog.querySelector('img')).toBeNull()
  expect(toast.querySelector('img')).toBeNull()

  dialog.querySelector<HTMLElement>('.dialog-button:last-child')!.click()
  expect(action).toHaveBeenCalledOnce()
  f7ready((app) => app.toast.get(toast).close(false))
})

it('Framework7 大图预览从指定图片打开，图片链接按属性值解析，离开时清理遮罩', async () => {
  const injectedUrl = 'https://example.test/img?name="onerror="alert(1)&n=2'
  const wrapper = mount(MobilePhotoBrowser, {
    props: { visible: true, images: ['/first.png', injectedUrl], index: 1, showIndex: true },
  })
  const popup = document.querySelector<HTMLElement>('.photo-browser-popup[role="dialog"]')!
  expect(popup.querySelector('.popup-close')?.getAttribute('aria-label')).toBe('关闭图片预览')
  const photo = popup.querySelector<HTMLImageElement>('.swiper-slide-active img')!
  expect(photo.getAttribute('src')).toBe(injectedUrl)
  expect(photo.hasAttribute('onerror')).toBe(false)

  wrapper.unmount()
  await flushPromises()
  expect(document.querySelector('.photo-browser-popup')).toBeNull()
  expect(document.querySelector('.popup-backdrop.backdrop-in')).toBeNull()
})
