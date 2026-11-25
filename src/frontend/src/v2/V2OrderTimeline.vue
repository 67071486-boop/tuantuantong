<script setup lang="ts">
import type { OrderTimeline, TimelineItem } from '@/api/auditTimeline'
import { orderStatusClass } from '@/v2/orderStatusAppearance'

defineProps<{ timeline: OrderTimeline }>()

/** ISO 串 → 本地 `MM-DD HH:mm:ss`；解析不了原样返回（同 V2Messages 的口径）。 */
function formatAt(value: string | null): string {
  if (!value) return '—'
  const at = new Date(value)
  if (Number.isNaN(at.getTime())) return value
  const pad = (n: number): string => String(n).padStart(2, '0')
  return (
    `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
    `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`
  )
}

function extraActor(item: TimelineItem): string {
  const actor = typeof item.actor_name === 'string' && item.actor_name
    ? item.actor_name
    : typeof item.actor === 'string' ? item.actor : ''
  return actor && !item.summary.startsWith(actor) && !item.summary.startsWith(`【${actor}】`) ? actor : ''
}

const statuses = new Map<string, { label: string; style?: string }>([
  ['done', { label: '成功', style: 'cv2-st--accept' }],
  ['sent', { label: '已发出', style: 'cv2-st--accept' }],
  ['failed', { label: '失败', style: 'cv2-st--error' }],
  ['pending', { label: '进行中', style: 'cv2-st--doing' }],
  ['claiming', { label: '接单确认中', style: 'cv2-st--doing' }],
  ['in_progress', { label: '进行中', style: 'cv2-st--doing' }],
  ['skipped', { label: '已跳过', style: 'cv2-st--refund' }],
  ['gone', { label: '已消失', style: 'cv2-st--refund' }],
  ['closed', { label: '已关闭', style: 'cv2-st--done' }],
  ['listed', { label: '待抢单' }],
  ['promoted', { label: '已录入' }],
  ['linked', { label: '已关联' }],
  ['terminated', { label: '关联已结束' }],
  ['grouped', { label: '已分组' }],
  ['ok', { label: '成功' }],
  ['active', { label: '在大厅' }],
  ['retired', { label: '已离开大厅' }],
])

function statusClass(item: TimelineItem): string {
  return statuses.get(item.status)?.style ?? ''
}

function statusText(item: TimelineItem): string {
  if (!item.status) return ''
  return statuses.get(item.status)?.label ?? '状态待核对'
}

function kindLabel(item: TimelineItem): string {
  return item.kind_label && item.kind_label !== item.kind ? item.kind_label : '订单记录'
}

function summaryText(item: TimelineItem): string {
  return item.summary === item.kind ? '' : item.summary
}

/** 相关链接的紧凑描述（related.peer_links 的展示投影）。 */
function peerLinkText(link: {
  binding_role: string
  binding_id: number | string
  peer_order_no: string
}): string {
  const role = link.binding_role === 'upstream' ? '上家' : '下家'
  return `${role}绑定 ${link.binding_id} · 对端单号 ${link.peer_order_no}`
}
</script>

<template>
  <!-- 单据摘要卡 -->
  <section class="cv2-tl-card" data-v2="order-card">
    <div class="cv2-tl-card-main">
      <span class="cv2-tl-order-label">系统单号</span>
      <span class="cv2-tl-order-no" data-v2="order-no">{{ timeline.order.order_no }}</span>
      <span
        v-if="timeline.order.is_deleted"
        class="cv2-st cv2-st--error cv2-st--static"
        data-v2="deleted-badge"
      ><i class="cv2-dot" aria-hidden="true"></i>已删除</span>
      <span
        v-else
        class="cv2-st cv2-st--static cv2-st--mark cv2-order-status"
        :class="orderStatusClass(timeline.order.status)"
        data-v2="status-badge"
      ><i class="cv2-dot" aria-hidden="true"></i>{{ timeline.order.status_label }}</span>
    </div>
    <dl class="cv2-tl-meta">
      <div class="cv2-tl-meta-row">
        <dt>来源单号</dt>
        <dd data-v2="source-order-no">{{ timeline.order.source_order_no || '—' }}</dd>
      </div>
      <div class="cv2-tl-meta-row">
        <dt>游戏 / 项目</dt>
        <dd>{{ timeline.order.game_name || '—' }}{{ timeline.order.project_text ? ` · ${timeline.order.project_text}` : '' }}</dd>
      </div>
      <div class="cv2-tl-meta-row">
        <dt>接手人</dt>
        <dd>{{ timeline.order.taker_display || '—' }}</dd>
      </div>
      <div class="cv2-tl-meta-row">
        <dt>录入 / 最近变动</dt>
        <dd>{{ formatAt(timeline.order.created_at) }} / {{ formatAt(timeline.order.updated_at) }}</dd>
      </div>
    </dl>
    <p
      v-if="timeline.related.peer_links.length"
      class="cv2-tl-links"
      data-v2="peer-links"
    >
      {{ timeline.related.peer_links.map(peerLinkText).join('；') }}
    </p>
  </section>

  <!-- 熔断红条（§1.4 健康条：挂起的逐条红签，健康的一行带过） -->
  <section v-if="timeline.bindings_health.length" class="cv2-tl-breakers" data-v2="breakers">
    <div
      v-for="b in timeline.bindings_health"
      :key="b.binding_id"
      class="cv2-tl-breaker"
      :class="{ 'cv2-tl-breaker--hot': b.suspended }"
      :data-v2-breaker="b.binding_id"
      :data-v2-suspended="b.suspended ? 'true' : 'false'"
    >
      <span class="cv2-st cv2-st--static" :class="b.suspended ? 'cv2-st--error' : 'cv2-st--done'">
        <i class="cv2-dot" aria-hidden="true"></i>{{ b.suspended ? '熔断挂起' : '同步正常' }}
      </span>
      <span class="cv2-tl-breaker-store">{{ b.store }}</span>
      <span v-if="b.suspended" class="cv2-tl-breaker-detail">{{ b.detail }}</span>
    </div>
  </section>

  <!-- 竖排时间线（节顺序 = 后端 §1.6 固定顺序；空节照出） -->
  <section
    v-for="section in timeline.sections"
    :key="section.key"
    class="cv2-tl-section"
    :data-v2-section="section.key"
  >
    <header class="cv2-tl-section-head">
      <h2 class="cv2-tl-section-title">{{ section.label }}</h2>
      <span
        v-if="section.truncated"
        class="cv2-st cv2-st--static cv2-st--pending"
        data-v2="truncated-badge"
      ><i class="cv2-dot" aria-hidden="true"></i>只显示最新 {{ section.items.length }} 条，更早的已截断</span>
    </header>
    <ol v-if="section.items.length" class="cv2-tl-list">
      <li
        v-for="(item, i) in section.items"
        :key="i"
        class="cv2-tl-item"
        :data-v2-item="item.kind"
      >
        <span class="cv2-tl-at">{{ formatAt(item.at) }}</span>
        <span
          v-if="statusText(item)"
          class="cv2-st cv2-st--static"
          :class="statusClass(item)"
        ><i v-if="statusClass(item)" class="cv2-dot" aria-hidden="true"></i>{{ statusText(item) }}</span>
        <span class="cv2-tl-kind" :data-v2-kind-label="kindLabel(item)">{{ kindLabel(item) }}</span>
        <span class="cv2-tl-summary">
          {{ summaryText(item) }}<template v-if="extraActor(item)">（{{ extraActor(item) }}）</template>
        </span>
      </li>
    </ol>
    <p v-else class="cv2-tl-none">这一节暂时没有留痕。</p>
  </section>
</template>

<style scoped>
.cv2-tl-card {
  margin-top: 12px;
  padding: 14px 16px;
  border: 1px solid var(--tt2-line);
  border-radius: 8px;
  background: var(--tt2-panel);
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cv2-tl-card-main {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.cv2-tl-order-label {
  color: var(--tt2-dim);
}

.cv2-tl-order-no {
  font-size: 17px;
  font-weight: 700;
  letter-spacing: 0.04em;
  font-variant-numeric: tabular-nums;
}

/* 状态签在本页是纯展示（不带点击语义）⇒ 压掉组件类的 pointer 光标。 */
.cv2-st--static {
  cursor: default;
}

.cv2-tl-meta {
  margin: 0;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 4px 24px;
}

.cv2-tl-meta-row {
  display: flex;
  gap: 8px;
  min-width: 0;
}

.cv2-tl-meta-row dt {
  color: var(--tt2-dim);
  flex: none;
}

.cv2-tl-meta-row dd {
  margin: 0;
  min-width: 0;
  overflow-wrap: anywhere;
}

.cv2-tl-links {
  margin: 0;
  color: var(--tt2-dim);
  font-size: 12px;
  overflow-wrap: anywhere;
}

.cv2-tl-breakers {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.cv2-tl-breaker {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid var(--tt2-line);
  border-radius: 8px;
  flex-wrap: wrap;
}

.cv2-tl-breaker--hot {
  border-color: var(--tt2-st-error-line);
  background: var(--tt2-st-error-bg);
}

.cv2-tl-breaker-store {
  font-weight: 600;
}

.cv2-tl-breaker-detail {
  color: var(--tt2-st-error);
  overflow-wrap: anywhere;
}

.cv2-tl-section {
  margin-top: 16px;
}

.cv2-tl-section-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}

.cv2-tl-section-title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
}

.cv2-tl-list {
  list-style: none;
  margin: 8px 0 0;
  padding: 0;
  border-left: 2px solid var(--tt2-line);
}

.cv2-tl-item {
  position: relative;
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 6px 0 6px 16px;
  flex-wrap: wrap;
}

/* 时间线竖轴上的点（自建布局的唯一样式件；色取中性线 token） */
.cv2-tl-item::before {
  content: '';
  position: absolute;
  left: -5px;
  top: 12px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--tt2-line);
}

.cv2-tl-at {
  flex: none;
  color: var(--tt2-dim);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
}

.cv2-tl-kind {
  flex: none;
  font-weight: 600;
}

.cv2-tl-summary {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--tt2-dim);
}

.cv2-tl-none {
  margin: 8px 0 0;
  color: var(--tt2-dim);
  font-size: 12px;
}
</style>
