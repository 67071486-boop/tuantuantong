<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, useTemplateRef } from 'vue'
import {
  f7Block as F7Block,
  f7Button as F7Button,
  f7Navbar as F7Navbar,
  f7Page as F7Page,
  f7ready,
  f7Searchbar as F7Searchbar,
  theme as framework7Theme,
} from 'framework7-vue'
import KefuDialog from './KefuDialog.vue'
import { useSessionStore } from '@/stores/session'
import { fetchOrders, type OrderListItem } from '@/api/orders'
import { reportOrderOpenEditQuietly } from '@/api/auditLogs'
import KefuOrderCard from './KefuOrderCard.vue'
import KefuOrderDetail from './KefuOrderDetail.vue'
import KefuOrderForm from './KefuOrderForm.vue'

type Mode = 'form' | 'detail' | 'edit'
type NoticeKind = 'success' | 'error' | 'info'
type KefuTheme = 'ios' | 'md'

const session = useSessionStore()
const InternalChatLauncher = defineAsyncComponent(() => import('@/v2/internalChat/InternalChatLauncherPreview.vue'))
const chatOwner = computed(() => {
  const user = session.user
  return user?.internal_chat_enabled === true ? `${user.tenant_name}:${session.identityPlane}:${user.id}` : null
})
const activeTheme = ref<KefuTheme>('md')
f7ready((app) => { activeTheme.value = app.theme === 'ios' ? 'ios' : 'md' })

function setKefuTheme(next: KefuTheme): void {
  if (next === activeTheme.value) return
  f7ready((app) => {
    // Keep the Framework7 instance and Vue forms mounted while changing appearance.
    const isIos = next === 'ios'
    app.theme = next
    app.params.theme = next
    framework7Theme.ios = isIos
    framework7Theme.md = !isIos
    const classes = document.documentElement.classList
    classes.toggle('ios', isIos)
    classes.toggle('md', !isIos)
    classes.toggle('ios-translucent-bars', isIos && Boolean(app.params.iosTranslucentBars))
    classes.toggle('ios-translucent-modals', isIos && Boolean(app.params.iosTranslucentModals))
    activeTheme.value = next
  })
}

/** 当前登录人显示名（顶栏用；取不到就空着，⛔ 不猜）。 */
const userName = computed(() => session.user?.display_name || session.user?.username || '')

/** 「读一单详情」的门：`GET /orders/<单号>/` 的 retrieve = any(两个节点)。 */
const canReadOrder = computed(() =>
  session.canNode(['order.view_detail', 'order.view_content']),
)

const mode = ref<Mode>('form')
const detailOrderNo = ref('')
/** 编辑视图当前的单号（从详情进入时 = `detailOrderNo`；回到录单页时清空）。 */
const editOrderNo = ref('')
const formRef = ref<InstanceType<typeof KefuOrderForm> | null>(null)
const editFormRef = ref<InstanceType<typeof KefuOrderForm> | null>(null)
const detailRef = useTemplateRef<InstanceType<typeof KefuOrderDetail>>('detail')
const chatAnchor = computed(() => {
  if (mode.value === 'detail') return detailRef.value?.footer
  return (mode.value === 'edit' ? editFormRef.value : formRef.value)?.footer
})

const pageNotice = ref('')
const pageNoticeKind = ref<NoticeKind>('info')
function showPageNotice(kind: NoticeKind, message: string): void {
  pageNoticeKind.value = kind
  pageNotice.value = message
}

/** 搜索结果一次取多少条（后端 `page_size`）：超过这个数就如实说「只显示前 N 条」。 */
const SEARCH_PAGE_SIZE = 20

const searchWrapEl = ref<HTMLElement | null>(null)
const searchInput = ref('')
const searchRows = ref<OrderListItem[]>([])
const searchTotal = ref(0)
const searchOpen = ref(false)
const searchLoading = ref(false)
const searchError = ref('')
/** latest-wins 令牌：在途搜索的旧响应回来不能覆盖新结果。 */
let searchToken = 0

function messageOf(e: unknown, fallback: string): string {
  return e instanceof Error && e.message !== '' ? e.message : fallback
}

async function runSearch(): Promise<void> {
  if (formRef.value?.blocksNavigation) return
  const q = searchInput.value.trim()
  if (q === '') {
    closeSearch()
    return
  }
  const my = ++searchToken
  searchOpen.value = true
  searchLoading.value = true
  searchError.value = ''
  try {
    const page = await fetchOrders({ keyword: q, page_size: SEARCH_PAGE_SIZE })
    if (my !== searchToken) return
    searchRows.value = page.items
    searchTotal.value = page.total
  } catch (e) {
    if (my !== searchToken) return
    searchRows.value = []
    searchTotal.value = 0
    searchError.value = messageOf(e, '搜索失败')
  } finally {
    if (my === searchToken) searchLoading.value = false
  }
}

function closeSearch(): void {
  searchToken += 1
  searchOpen.value = false
  searchRows.value = []
  searchTotal.value = 0
  searchError.value = ''
  searchLoading.value = false
}

/** ⭐ B1：无权限时必须**先关浮层**再提示，否则提示被绝对定位的浮层盖住（点了看不到反馈）。 */
function openDetail(orderNo: string): void {
  if (formRef.value?.blocksNavigation) return
  if (!canReadOrder.value) {
    closeSearch()
    showPageNotice('error', '没有查看订单详情的权限')
    return
  }
  detailOrderNo.value = orderNo
  mode.value = 'detail'
  closeSearch()
}

function openOrderFromChat(orderNo: string): void {
  if (mode.value === 'edit' || formRef.value?.blocksNavigation) {
    showPageNotice('info', '请先处理当前订单，再查看聊天中的订单')
    return
  }
  openDetail(orderNo)
}

function backToForm(): void {
  mode.value = 'form'
  detailOrderNo.value = ''
  editOrderNo.value = ''
}

/* ── 编辑视图（业务方需求「详情 → 二次编辑」；默认值调度侧代定、业务方过目） ── */

/**
 * 详情底部「编辑」⇒ 进编辑视图。审计与主控台对齐：**进入编辑态这一刻**上报 `order.open_edit`（`reportOrderOpenEditQuietly` 的静默语义见 `api/auditLogs.ts`——写失败不阻塞业务）。
 * ⚠️ 只在真进入编辑态时报，⛔ 不在详情加载时报（详情是「看」，那会写出假的「进入编辑页」日志）。
 */
function openEdit(): void {
  const no = detailOrderNo.value
  if (no === '') return
  editOrderNo.value = no
  mode.value = 'edit'
  reportOrderOpenEditQuietly(no)
}

/**
 * 编辑视图回详情（保存成功与「取消」共用）：复用 `openDetail` 重取——详情视图是 `v-if`，切回来的那次重挂会自己重新 `fetchOrder`（⭐ 判据打在重取上，「切回来没报错」不算数）。
 * `openDetail` 里的读权限闸对「刚编辑完回来」同样成立（能进详情的人本来就过了那道闸，这里只是同一条路的复走）。
 */
function backToDetailFromEdit(orderNo: string): void {
  openDetail(orderNo)
}

/**
 * 编辑保存成功：表单带出后端成功信封的 `msg`（H1 同族出口——后端可能在成功码里带
 * 「代练通订单未能撤回」这类警告，⛔ 不能吞掉）。回详情并重取。
 */
function onEditSaved(payload: { orderNo: string; message: string }): void {
  showPageNotice('success', `${payload.message}（单号 ${payload.orderNo}）`)
  backToDetailFromEdit(payload.orderNo)
}

/**
 * 编辑「取消」：**不做草稿二次确认**——录单草稿在另一个常驻实例里、根本没被碰过；
 * 编辑表单自身随 `v-if` 卸载即弃，确认弹窗只会让人以为要清他正在录的那张单。
 */
function onEditCancel(): void {
  backToDetailFromEdit(editOrderNo.value)
}

function onEditBack(): void {
  if (!editFormRef.value?.blocksNavigation) onEditCancel()
}

function onFormSaved(payload: { orderNo: string; message: string }): void {
  showPageNotice('success', `${payload.message}（单号 ${payload.orderNo}）`)
  formRef.value?.resetForm()
}

function onFormCreatedFailed(message: string): void {
  showPageNotice('error', message)
  formRef.value?.resetForm()
}

const confirmReset = ref(false)

function onFormCancel(): void {
  if (formRef.value?.hasDraft()) {
    confirmReset.value = true
    return
  }
  formRef.value?.resetForm()
}

function discardDraft(): void {
  confirmReset.value = false
  formRef.value?.resetForm()
}

function onSearchInput(event: Event): void {
  const input = event.target as HTMLInputElement
  if (input.value.trim() === '') closeSearch()
}

/** 点搜索浮层外部关闭（M3）；组件卸载时移掉监听（⛔ 不跨用例 / 跨页面累积）。 */
function onDocumentPointerDown(e: PointerEvent): void {
  if (!searchOpen.value) return
  const el = searchWrapEl.value
  if (el !== null && e.target instanceof Node && el.contains(e.target)) return
  closeSearch()
}

onMounted(() => document.addEventListener('pointerdown', onDocumentPointerDown))
onBeforeUnmount(() => document.removeEventListener('pointerdown', onDocumentPointerDown))
</script>

<template>
  <F7Page class="kefu-page" :page-content="false">
    <F7Navbar :key="mode" class="kefu-topbar" :sliding="false">
      <template v-if="mode !== 'form'" #left>
        <F7Button
          type="button"
          class="kefu-back"
          :disabled.prop="mode === 'edit' && editFormRef?.blocksNavigation"
          @click="mode === 'detail' ? backToForm() : onEditBack()"
        >
          ‹ 返回
        </F7Button>
      </template>
      <template #title>
        <h1 class="kefu-title">{{ mode === 'form' ? '快捷录单' : mode === 'detail' ? '订单详情' : '编辑订单' }}</h1>
      </template>
      <template #right>
        <div class="kefu-theme-toggle" role="group" aria-label="页面风格" @pointerdown.stop>
          <F7Button type="button" small :fill="activeTheme === 'ios'" :aria-pressed="activeTheme === 'ios'" @click="setKefuTheme('ios')">iOS</F7Button>
          <F7Button type="button" small :fill="activeTheme === 'md'" :aria-pressed="activeTheme === 'md'" @click="setKefuTheme('md')">Material</F7Button>
        </div>
        <span v-if="mode === 'form'" class="kefu-user" :title="userName">{{ userName }}</span>
      </template>
    </F7Navbar>

    <!-- ⭐ 页级提示在顶栏正下方（原来只住在录单视图里）——编辑保存的
         回执（可能带「代练通订单未能撤回」警告）要在**详情视图**里看得见。 -->
    <p v-if="pageNotice" class="kefu-notice" :class="`is-${pageNoticeKind}`" role="alert">
      {{ pageNotice }}
    </p>

    <!-- 录单模式：内容区**不再滚动**，改成撑满剩余高度的 flex 列（表单自己分「可滚内容 +
         固定操作条」两段）；详情 / 编辑模式：滚动交给各自的内部滚动区（I12）。 -->
    <main class="kefu-body" :class="mode === 'form' ? 'is-form-mode' : 'is-detail-mode'">
      <!-- ⛔ 不要用 v-if 把表单卸载：详情来回切必须保留录单草稿。
           表单区用 v-show，详情与编辑视图用 v-if。 -->
      <div v-show="mode === 'form'" class="kefu-form-view">
        <div ref="searchWrapEl" class="kefu-search-wrap" @keyup.esc="closeSearch">
          <F7Searchbar
            v-model:value="searchInput"
            class="kefu-searchbar"
            custom-search
            :outline="false"
            :disable-button="false"
            :backdrop="false"
            placeholder="输入订单号搜索"
            role="search"
            aria-label="搜索订单号"
            @submit.prevent="runSearch"
            @input="onSearchInput"
            @searchbar:clear="closeSearch"
          >
            <template #inner-end>
              <F7Button
                type="submit"
                small round tonal
                class="kefu-search-btn"
                :aria-expanded="searchOpen"
                aria-controls="kefu-search-panel"
              >搜索</F7Button>
            </template>
          </F7Searchbar>
          <F7Block
            v-if="searchOpen"
            id="kefu-search-panel"
            strong inset
            class="kefu-search-panel"
            role="region"
            aria-label="搜索结果"
          >
            <div class="kefu-search-panel-head">
              <span>搜索结果</span>
              <F7Button
                type="button"
                class="kefu-search-close"
                aria-label="关闭搜索结果"
                @click="closeSearch"
              >
                关闭
              </F7Button>
            </div>
            <p v-if="searchLoading" class="kefu-hint">搜索中…</p>
            <p v-else-if="searchError" class="kefu-search-empty is-error">{{ searchError }}</p>
            <p v-else-if="searchRows.length === 0" class="kefu-search-empty">没查到订单</p>
            <template v-else>
              <!-- 判据与文案都用**上限**（SEARCH_PAGE_SIZE），不是本次实际回来的条数：
                   后端可能因为权限/软删少回几行，那时说「只显示前 1 条」会误导。 -->
              <p v-if="searchTotal > SEARCH_PAGE_SIZE" class="kefu-hint">
                共 {{ searchTotal }} 条，只显示前 {{ SEARCH_PAGE_SIZE }} 条；请输入更完整的订单号。
              </p>
              <ul class="kefu-search-cards">
                <li v-for="row in searchRows" :key="row.order_no">
                  <KefuOrderCard :row="row" @open="openDetail(row.order_no)" />
                </li>
              </ul>
            </template>
          </F7Block>
        </div>
        <div class="kefu-form-host">
          <KefuOrderForm
            ref="formRef"
            :order-no="''"
            @saved="onFormSaved"
            @created-failed="onFormCreatedFailed"
            @back="onFormCancel"
          />
        </div>
      </div>
      <KefuOrderDetail
        v-if="mode === 'detail'"
        ref="detail"
        :order-no="detailOrderNo"
        @back="backToForm"
        @edit="openEdit"
      />
      <!-- ⭐ 编辑视图 = **独立的第二表单实例**（⛔ 不复用上面那个 v-show 常驻
           实例——那里装着还没保存的录单草稿，加载订单会把它整个覆盖掉）。保存成功与
           取消都回详情；`created-failed` 只有建单路径会发，这里不接。 -->
      <div v-if="mode === 'edit'" class="kefu-edit-host">
        <KefuOrderForm ref="editFormRef" :order-no="editOrderNo" @saved="onEditSaved" @back="onEditCancel" />
      </div>
    </main>
    <KefuDialog v-model:open="confirmReset" title="清空录单">
      <p>当前录入还没保存，确定清空？</p>
      <div class="kefu-confirm-actions">
        <F7Button type="button" autofocus @click="confirmReset = false">继续填写</F7Button>
        <F7Button type="button" fill @click="discardDraft">清空</F7Button>
      </div>
    </KefuDialog>
    <InternalChatLauncher
      v-if="chatOwner && session.user"
      :key="chatOwner"
      production
      :identity="session.user"
      :open-request="0"
      position-scope="kefu"
      :default-anchor="chatAnchor"
      @open-order="openOrderFromChat"
    />
  </F7Page>
</template>

<style scoped>
.kefu-page.page {
  position: fixed;
  inset: 0;
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 420px;
  margin-inline: auto;
  overflow: hidden;
}
.kefu-topbar.navbar { position: relative; flex: none; }
.kefu-topbar :deep(.navbar-inner) { gap: 5px; padding-block: 5px; }
.kefu-topbar :deep(.navbar-bg) { pointer-events: none; }
.kefu-topbar :deep(.navbar-inner > .right) {
  min-width: 0;
  margin-inline-start: auto;
  padding-inline: 8px;
  gap: 6px;
}
.kefu-title { margin: 0; font: inherit; }
.kefu-theme-toggle {
  display: flex;
  flex: none;
  align-items: center;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--kefu-divider);
  border-radius: 20px;
}
.kefu-theme-toggle .button {
  width: auto;
  border-radius: 16px;
  font-size: 11px;
}
.kefu-user {
  max-width: 65px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--kefu-muted);
  font-size: 11px;
}
.kefu-topbar .kefu-back {
  flex: none;
  width: auto;
  min-width: 40px;
  height: 34px;
  padding-inline: 6px;
  border-radius: 18px;
  white-space: nowrap;
}
.kefu-body,
.kefu-form-view,
.kefu-form-host,
.kefu-edit-host {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
  min-width: 0;
}
.kefu-notice {
  flex: none;
  margin: 5px 8px 0;
  padding: 5px 8px;
  border-radius: 10px;
  overflow-wrap: anywhere;
  font-size: 12px;
  background: var(--f7-list-strong-bg-color);
}
.kefu-notice.is-error { color: var(--kefu-error); }
.kefu-notice.is-success { color: var(--f7-theme-color); }
.kefu-search-wrap { position: relative; flex: none; padding: 3px 0; }
.kefu-searchbar.searchbar { margin: 0; background-color: transparent; }
.kefu-searchbar::after,
.kefu-searchbar::before { display: none; }
.kefu-searchbar :deep(.searchbar-inner) { gap: 5px; padding-inline: 8px; }
.kefu-search-btn { flex: none; width: auto; }
.kefu-search-panel.block {
  position: absolute;
  z-index: 320;
  top: calc(100% - 2px);
  inset-inline: 8px;
  max-height: 50vh;
  margin: 0;
  padding: 7px;
  overflow-y: auto;
  box-shadow: var(--f7-card-box-shadow);
}
.kefu-search-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 6px;
  margin-bottom: 5px;
  font-size: 12px;
  color: var(--kefu-muted);
}
.kefu-search-close { width: auto; }
.kefu-search-empty { margin: 4px 0; }
.kefu-search-empty.is-error { color: var(--kefu-error); }
.kefu-search-cards { display: grid; gap: 4px; margin: 0; padding: 0; list-style: none; }
.kefu-confirm-actions { display: flex; justify-content: flex-end; gap: 6px; }
</style>
