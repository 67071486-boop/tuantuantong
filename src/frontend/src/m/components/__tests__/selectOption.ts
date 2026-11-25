import type { VueWrapper } from '@vue/test-utils'

export async function openOptions(wrapper: VueWrapper, testId: string) {
  const trigger = wrapper.get(`[data-testid="${testId}"]`)
  await trigger.trigger('click')
  return wrapper.get(`[id="${trigger.attributes('aria-controls')}"]`)
}

export async function selectOption(wrapper: VueWrapper, testId: string, value: string | number): Promise<void> {
  const panel = await openOptions(wrapper, testId)
  await panel.get(`button[data-option-value="${value}"]`).trigger('click')
}
