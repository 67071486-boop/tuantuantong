<script setup lang="ts">
import type { VisitorReceptionPanel } from './visitorReception'

const props = defineProps<{ panel: VisitorReceptionPanel }>()

function chooseTarget(event: Event) {
  const id = (event.target as HTMLSelectElement).value
  props.panel.chooseTarget(id ? Number(id) : '')
}
</script>

<template>
  <div v-if="panel.error && !panel.transferring" class="visitor-reception-error" role="alert">
    {{ panel.error }}
    <button type="button" class="cv2-btn cv2-btn--sm" @click="panel.dismissError()">关闭提示</button>
  </div>
  <ElDialog
    :model-value="panel.transferring"
    title="转接会话"
    class="cv2-dialog cv2-dialog--sm"
    :close-on-click-modal="!panel.busy"
    :close-on-press-escape="!panel.busy"
    :show-close="!panel.busy"
    :append-to-body="true"
    @update:model-value="!$event && panel.cancel()"
  >
    <p v-if="panel.loading" role="status">正在读取同席位可接待人员…</p>
    <label v-else-if="panel.candidates.length" class="visitor-reception-recipient">
      转接给
      <select :value="panel.target" class="cv2-input" :disabled="panel.busy" @change="chooseTarget">
        <option v-for="candidate in panel.candidates" :key="candidate.id" :value="candidate.id">{{ candidate.name }}</option>
      </select>
    </label>
    <p v-else-if="!panel.error">当前没有同席位的可接待人员。</p>
    <p v-if="panel.error" role="alert">{{ panel.error }}</p>
    <template v-if="panel.history.length">
      <p>交接记录</p>
      <ul>
        <li v-for="entry in panel.history" :key="entry.transferred_at">
          {{ entry.from_name ?? entry.from_id }} → {{ entry.to_name ?? entry.to_id }}
        </li>
      </ul>
    </template>
    <template #footer>
      <button class="cv2-btn" type="button" :disabled="panel.busy" @click="panel.cancel()">取消</button>
      <button class="cv2-btn cv2-btn--primary" type="button"
        :disabled="panel.busy || panel.loading || panel.target === ''" @click="panel.transfer()">确认转接</button>
    </template>
  </ElDialog>
</template>

<style scoped>
.visitor-reception-recipient {
  display: grid;
  gap: 6px;
}
.visitor-reception-error {
  position: absolute;
  z-index: 2;
  top: 8px;
  left: 50%;
  width: max-content;
  max-width: calc(100% - 20px);
  transform: translateX(-50%);
  background: var(--tt2-panel);
  color: var(--tt2-st-error);
  border: 1px solid var(--tt2-line);
  border-radius: 8px;
  padding: 6px 8px;
}
</style>
