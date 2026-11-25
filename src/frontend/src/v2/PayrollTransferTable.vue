<script setup lang="ts">
import { ref } from 'vue'
import { payrollStatusLabel, type PayrollTransfer } from '@/api/onlinePayroll'

defineProps<{ transfers: PayrollTransfer[]; empty?: string }>()
const expanded = ref<string | number | null>(null)
</script>

<template>
  <div class="cv2-panel cv2-table-panel payroll-transfers">
    <table class="cv2-table" aria-label="支付宝转账明细">
      <thead>
        <tr>
          <th>结算对象</th>
          <th>打款金额</th>
          <th>收款账号</th>
          <th>打款状态</th>
          <th>计全付转账单号</th>
          <th>支付宝打款流水号</th>
          <th>成功打款时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="transfer in transfers" :key="transfer.id">
          <tr>
            <td>{{ transfer.recipient_name }}</td>
            <td class="cv2-td-num">{{ transfer.amount }} 元</td>
            <td>{{ transfer.account_masked }}</td>
            <td>
              <span>{{ transfer.status === 'succeeded' && !transfer.finalized_at ? '打款成功，待记账' : payrollStatusLabel(transfer.status) }}</span>
              <small v-if="transfer.last_error" class="payroll-error">{{ transfer.last_error }}</small>
            </td>
            <td>{{ transfer.transfer_id || '—' }}</td>
            <td>{{ transfer.channel_order_no || '—' }}</td>
            <td>{{ transfer.success_time ? transfer.success_time.replace('T', ' ').slice(0, 19) : '—' }}</td>
            <td>
              <button
                type="button"
                class="cv2-btn"
                :aria-expanded="expanded === transfer.id"
                @click="expanded = expanded === transfer.id ? null : transfer.id"
              >{{ expanded === transfer.id ? '收起订单' : '查看关联订单' }}</button>
            </td>
          </tr>
          <tr v-if="expanded === transfer.id">
            <td colspan="8">
              <table class="cv2-table" aria-label="该笔打款的业务订单">
                <thead><tr><th>关联订单</th><th>生效绩效</th><th>扣点</th><th>未结调整</th><th>实付金额</th></tr></thead>
                <tbody>
                  <tr v-for="item in transfer.items" :key="item.performance_id">
                    <td>{{ item.order_no }}</td>
                    <td>{{ item.bonus }}</td>
                    <td>{{ item.deduction }}</td>
                    <td>{{ item.adjustment }}</td>
                    <td>{{ item.amount }}</td>
                  </tr>
                </tbody>
              </table>
            </td>
          </tr>
        </template>
        <tr v-if="!transfers.length"><td colspan="8" class="cv2-empty">{{ empty ?? '暂无结算打款记录' }}</td></tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.payroll-transfers { overflow-x: auto; }
.payroll-error { display: block; }
</style>
