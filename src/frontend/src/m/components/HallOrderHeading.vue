<script setup lang="ts">
import { f7Badge as F7Badge } from 'framework7-vue'
import type { HallListItem } from '@/api/hall'

defineProps<{
  row: Pick<HallListItem, 'is_urgent' | 'is_exited' | 'is_preferred' | 'hall_price'>
  game: string
  claimed: boolean
  meta?: string
}>()
</script>

<template>
  <header
    class="m-hall-order-heading"
    :class="{
      'm-hall-order-heading--unavailable': row.is_exited || claimed,
      'm-hall-order-heading--wide-price': row.hall_price !== null && row.hall_price.length > 7,
    }"
  >
    <div class="m-hall-order-heading__tags">
      <F7Badge class="m-hall-order-heading__game" :title="game">
        <span>{{ game }}</span>
      </F7Badge>
      <F7Badge v-if="row.is_urgent" class="m-hall-order-heading__urgent">加急</F7Badge>
      <F7Badge v-if="row.is_exited" class="m-hall-order-heading__status" data-testid="hall-exit-mark">已被抢</F7Badge>
      <F7Badge v-else-if="claimed" class="m-hall-order-heading__status" data-testid="hall-peer-claimed-mark">已接下</F7Badge>
      <F7Badge v-if="row.is_preferred" data-testid="hall-pref-badge">优选</F7Badge>
    </div>
    <span v-if="meta" class="m-hall-order-heading__meta">{{ meta }}</span>
    <span v-if="row.hall_price !== null" class="m-hall-order-heading__price"><span>¥</span>{{ row.hall_price }}</span>
    <span v-else class="m-hall-order-heading__unpriced">未定价</span>
    <div v-if="$slots.action" class="m-hall-order-heading__action">
      <slot name="action" />
    </div>
  </header>
</template>

<style scoped>
.m-hall-order-heading {
  display: grid;
  grid-template-areas: "tags price action" "meta price action";
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-content: center;
  align-items: center;
  column-gap: 6px;
  row-gap: 2px;
  min-width: 0;
}

.m-hall-order-heading__tags {
  grid-area: tags;
  display: flex;
  align-items: center;
  gap: 4px;
  min-width: 0;
}

.m-hall-order-heading__tags .badge {
  flex: none;
  min-width: 0;
  white-space: nowrap;
  --f7-badge-font-size: 11px;
  --f7-badge-bg-color: #f3f1f6;
  --f7-badge-text-color: var(--m-muted);
}

.m-hall-order-heading__tags .m-hall-order-heading__game {
  flex: 0 1 auto;
}

.m-hall-order-heading__game span,
.m-hall-order-heading__meta {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.m-hall-order-heading__tags .m-hall-order-heading__urgent {
  --f7-badge-bg-color: #fff1e9;
  --f7-badge-text-color: #a84525;
}

.m-hall-order-heading__tags .m-hall-order-heading__status {
  --f7-badge-bg-color: #eeecf1;
}

.m-hall-order-heading__meta {
  grid-area: meta;
  color: var(--m-muted);
  font-size: 12px;
  line-height: 16px;
}

.m-hall-order-heading__price,
.m-hall-order-heading__unpriced {
  grid-area: price;
  justify-self: end;
  white-space: nowrap;
}

.m-hall-order-heading__price {
  color: var(--f7-theme-color);
  font-size: 22px;
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}

.m-hall-order-heading__price span {
  margin-inline-end: 2px;
  font-size: 13px;
}

.m-hall-order-heading--wide-price .m-hall-order-heading__price {
  font-size: 17px;
}

.m-hall-order-heading__unpriced,
.m-hall-order-heading--unavailable .m-hall-order-heading__price {
  color: var(--m-muted);
}

.m-hall-order-heading__unpriced {
  font-size: 12px;
}

.m-hall-order-heading__action {
  grid-area: action;
}

.m-hall-order-heading:not(:has(.m-hall-order-heading__action)) {
  grid-template-columns: minmax(0, 1fr) auto;
  grid-template-areas: "tags price" "meta price";
}

@container (max-width: 330px) {
  /* Wide prices share the second row with metadata so the tags can stay together. */
  .m-hall-order-heading--wide-price {
    grid-template-areas: "tags tags action" "meta price action";
  }

  .m-hall-order-heading--wide-price:not(:has(.m-hall-order-heading__action)) {
    grid-template-areas: "tags tags" "meta price";
  }
}

@container (max-width: 290px) {
  .m-hall-order-heading:not(.m-hall-order-heading--wide-price) .m-hall-order-heading__price {
    font-size: 18px;
  }
}
</style>
