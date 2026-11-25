// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { defineComponent, inject, ref } from 'vue'
import { expect, it, vi } from 'vitest'
import { routeLocationKey, routerKey } from 'vue-router'
import OrderDetailDialog from '../OrderDetailDialog.vue'
import OrderTabDialog from '../OrderTabDialog.vue'
import { openInternalChatPreview } from '../internalChat/launcherPreview'

const cachedPath = '/v2/my-orders'
const ChatButton = defineComponent({
  setup() {
    const open = inject(openInternalChatPreview)
    return { open }
  },
  template: '<button @click="open(81)">打开会话</button>',
})

it.each([true, false])('切走页签后忽略滞后的订单群交接，是否已开始关闭：%s', async alreadyClosing => {
  const open = vi.fn()
  const currentRoute = ref({ fullPath: cachedPath })
  const wrapper = mount(OrderDetailDialog, {
    props: { modelValue: true },
    slots: { default: ChatButton },
    global: {
      provide: {
        [routeLocationKey as symbol]: { fullPath: cachedPath },
        [routerKey as symbol]: { currentRoute },
        [openInternalChatPreview as symbol]: open,
      },
      stubs: { OrderTabDialog: { template: '<div><slot /></div>' } },
    },
  })
  const chatButton = wrapper.get('button')
  const dialog = wrapper.getComponent(OrderTabDialog)
  try {
    if (alreadyClosing) await chatButton.trigger('click')
    currentRoute.value = { fullPath: '/v2/approvals' }
    if (!alreadyClosing) await chatButton.trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual(alreadyClosing ? [[false]] : undefined)
    currentRoute.value = { fullPath: cachedPath }
    dialog.vm.$emit('closed')
    expect(open).not.toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})
