<script lang="ts">
import { ElDialog, type DialogInstance } from 'element-plus/es'
import 'element-plus/es/components/dialog/style/css'
import { defineComponent, h, inject, onMounted, onUnmounted, ref } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'

export default defineComponent({
  name: 'V2TabDialog',
  inheritAttrs: false,
  props: { closeOnPressEscape: { type: Boolean, default: true } },
  setup(props, { attrs, slots }) {
    const route = inject(routeLocationKey, undefined)
    const router = inject(routerKey, undefined)
    const dialog = ref<DialogInstance>()
    const ownsEscape = route?.matched.some((record) => record.name === 'v2') === true

    function onEscape(event: KeyboardEvent): void {
      if (event.key !== 'Escape' || event.defaultPrevented || route?.fullPath !== router?.currentRoute.value.fullPath) return
      if ((event.target as HTMLElement | null)?.closest?.('[aria-expanded="true"][role="combobox"]')) return
      const content = dialog.value?.dialogContentRef?.$el as HTMLElement | undefined
      const overlay = content?.closest<HTMLElement>('.el-overlay')
      if (!overlay?.getClientRects().length) return
      const overlays = Array.from(document.querySelectorAll<HTMLElement>('.cv2-main .el-modal-dialog'))
        .filter((node) => node.getClientRects().length > 0 && node.querySelector('.el-dialog'))
      const front = overlays.reduce<HTMLElement | undefined>((top, node) =>
        !top || Number(node.style.zIndex) >= Number(top.style.zIndex) ? node : top, undefined)
      if (overlay !== front || !props.closeOnPressEscape) return
      event.preventDefault()
      dialog.value?.handleClose()
    }

    if (ownsEscape) {
      onMounted(() => window.addEventListener('keydown', onEscape))
      onUnmounted(() => window.removeEventListener('keydown', onEscape))
    }

    return () => h(ElDialog, {
      ...attrs,
      ref: dialog,
      lockScroll: false,
      closeOnPressEscape: !ownsEscape && props.closeOnPressEscape,
    }, slots)
  },
})
</script>
