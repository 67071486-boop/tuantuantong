<script setup lang="ts">
import { f7Badge as F7Badge, f7Button as F7Button, f7Card as F7Card } from 'framework7-vue'
import type { OrderListItem } from '@/api/orders'
import { orderStatusLabel, orderStatusTagType } from '@/config/orderStatus'
import { formatMoney } from '@/config/money'
import { buyerText, displayOrderNo, shortTime } from './format'

/**
 * 订单号搜索结果的**只读卡片**。整张卡是一个 `<button>`：点哪儿都能进详情（⛔ 不再有编辑 / 指派 / 抛大厅等行内动作，那些入口已随旧列表从 `/kefu` 移除）。
 */
defineProps<{ row: OrderListItem }>()
defineEmits<{ open: [] }>()
</script>

<template>
  <F7Card outline class="kefu-search-card" :padding="false">
    <F7Button type="button" class="kefu-result-card" @click="$emit('open')">
      <span class="kefu-result-line1">
        <F7Badge class="kefu-status" :color="orderStatusTagType(row.status) === 'danger' ? 'red' : undefined">
          {{ orderStatusLabel(row.status) }}
        </F7Badge>
        <span class="kefu-result-project" :title="row.project_text">{{ row.project_text }}</span>
      </span>
      <span class="kefu-result-line2">
        <span class="kefu-result-money">¥{{ formatMoney(row.total_price) }}</span>
        <span v-if="buyerText(row)" class="kefu-result-buyer">{{ buyerText(row) }}</span>
        <span v-if="row.source" class="kefu-result-source">{{ row.source }}</span>
      </span>
      <span class="kefu-result-line3">
        <span class="kefu-result-no">{{ displayOrderNo(row) }}</span>
        <span class="kefu-result-time">{{ shortTime(row.created_at) }}</span>
      </span>
    </F7Button>
  </F7Card>
</template>

<style scoped>
.kefu-search-card.card { margin: 0; }
.kefu-result-card.button {
  box-sizing: border-box;
  display: block;
  width: 100%;
  margin: 0;
  padding: 8px 10px;
  height: auto;
  border-radius: var(--f7-card-border-radius);
  color: inherit;
  font-family: inherit;
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}
.kefu-result-card:active { background: var(--f7-list-link-pressed-bg-color); }
.kefu-result-line1,
.kefu-result-line2,
.kefu-result-line3 {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
/* ⭐ I6：331 下金额 + 买家 + 来源可能一行放不下 ⇒ 换行，⛔ 不许横向溢出。
   （旧卡 `.kefu-card-line2` 就是这么做的，`KefuConsoleView.vue:1160-1167`。） */
.kefu-result-line2 { flex-wrap: wrap; }
.kefu-result-line1 { margin-bottom: 3px; }
.kefu-result-project {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  font-size: 13px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kefu-result-money { color: var(--f7-theme-color); font-weight: 600; }
.kefu-result-buyer,
.kefu-result-source,
.kefu-result-time { color: var(--kefu-muted); }
/* ⭐ I-b：单号 + 时间一行放不下时换行（331 下不许横向溢出）。 */
.kefu-result-line3 { margin-top: 3px; justify-content: space-between; flex-wrap: wrap; }
/* ⭐ I-b：单号是 20 位数字（或更长的来源号）⇒ 必须能省略，⛔ 不许把卡片撑宽。 */
.kefu-result-no {
  min-width: 0;
  overflow: hidden;
  color: var(--kefu-muted);
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
