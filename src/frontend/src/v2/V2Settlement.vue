<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { listMyPayroll, type PayrollTransfer } from '@/api/onlinePayroll'
import PayrollTransferTable from './PayrollTransferTable.vue'

const rows = ref<PayrollTransfer[]>([])
const page = ref(1)
const count = ref(0)
const loading = ref(false)
const error = ref('')

async function load(next = page.value): Promise<void> {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    const result = await listMyPayroll(next)
    rows.value = result.items
    count.value = result.count
    page.value = next
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '读取在线结算记录失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => { void load() })
</script>

<template>
  <div class="cv2-page" data-v2-page="settlement">
    <div class="cv2-toolbar">
      <button type="button" class="cv2-btn" :disabled="loading" @click="load()">刷新记录</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <PayrollTransferTable v-else :transfers="rows" :empty="loading ? '读取中…' : '暂无结算打款记录'" />
    <div v-if="count > 25" class="cv2-toolbar" aria-label="在线结算记录分页">
      <button type="button" class="cv2-btn" :disabled="loading || page === 1" @click="load(page - 1)">上一页</button>
      <span>第 {{ page }} 页，共 {{ count }} 条</span>
      <button type="button" class="cv2-btn" :disabled="loading || page * 25 >= count" @click="load(page + 1)">下一页</button>
    </div>
  </div>
</template>
