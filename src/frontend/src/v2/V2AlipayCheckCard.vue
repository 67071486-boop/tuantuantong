<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「重复收款支付宝子账号检测」数据卡片（v2 人员管理；系统人员批次 2026-11-05，计划 §3.1.3，lg 弹窗）。
 *
 * 旧页 = island `system-user-list.ts` 里的 `.checkAliPayAccDiv` 卡片（`#tableList3`）。
 * 取数 `fetchAlipayDuplicates()`（无参、不看列表筛选）；**摊平渲染**（同组每行都带
 * 该组的支付宝账号与重复数量）。行为照旧页：
 * - 无筛选；5 个命名列、无勾选列、不分页。
 * - 重复数量 title 逐字；当前状态：`str4` 必须按**字符串 'true'** 判断（传布尔恒显禁用）——
 *   正常 = 蓝签（cv2-st--info）、禁用 = 灰签（cv2-st--unassigned）。
 * - 工具条橙字逐字；三颗按钮照旧页全部「尚未开放」（旧页只有接单统计卡的刷新是真刷）。
 * - 三态空文案互不相同逐字：没有重复 / 403 专属 / 检测失败。
 */
import { onMounted, ref } from 'vue'
import { HttpError } from '@/api/errors'
import {
  fetchAlipayDuplicates,
  type AlipayDuplicateGroup,
  type AlipayDuplicateUser,
} from '@/api/performances'

const props = defineProps<{
  notify: (text: string, kind?: 'info' | 'error') => void
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const EMPTY_NONE = '没有重复的收款支付宝账号'
const EMPTY_FORBIDDEN = '你没有查看重复收款支付宝的权限'
const EMPTY_FAILED = '检测失败，请稍后重试'
const ALIPAY_NOTE = '这里包含了系统所有状态下的子账号'

/** 一行 = 一个用户（island `buildAlipayCheckRows` 同口径：字段名沿用对标那套）。 */
type AlipayCheckRow = {
  str1: string
  str2: string
  str3: string
  id2: number
  str4: 'true' | 'false'
}

const rows = ref<AlipayCheckRow[]>([])
const loading = ref(false)
/** 空态文案（三态互异：查无 / 无权限 / 失败）。 */
const emptyText = ref(EMPTY_NONE)

/** 摊平：一组的 `alipay_account` 与 `count` 在同组每行都相同（island 同式）。 */
function flatten(groups: AlipayDuplicateGroup[]): AlipayCheckRow[] {
  return groups.flatMap((group) =>
    group.users.map((user: AlipayDuplicateUser) => ({
      str1: user.display_name || user.username,
      str2: group.alipay_account,
      str3: user.job,
      id2: group.count,
      str4: user.is_active ? ('true' as const) : ('false' as const),
    })),
  )
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await fetchAlipayDuplicates()
    rows.value = flatten(result.duplicates)
    emptyText.value = EMPTY_NONE
  } catch (e) {
    rows.value = []
    emptyText.value = e instanceof HttpError && e.status === 403 ? EMPTY_FORBIDDEN : EMPTY_FAILED
  } finally {
    loading.value = false
  }
}

function notOpen(label: string): void {
  props.notify(`「${label}」尚未开放`)
}

onMounted(() => {
  void load()
})
</script>

<template>
  <el-dialog
    model-value
    title="重复收款支付宝子账号检测"
    class="cv2-dialog cv2-dialog--lg"
    data-v2="dlg-alipay-check"
    @update:model-value="emit('close')"
  >
    <div class="cv2-toolbar ac-toolbar">
      <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--find" data-v2="ac-refresh" @click="notOpen('刷新列表')">
        刷新列表
      </button>
      <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--danger" data-v2="ac-disable" @click="notOpen('禁用子账号')">
        禁用子账号
      </button>
      <button type="button" class="cv2-btn cv2-btn--tool cv2-btn--danger" data-v2="ac-delete" @click="notOpen('删除用户')">
        删除用户
      </button>
      <p class="ac-note" data-v2="ac-note">{{ ALIPAY_NOTE }}</p>
    </div>

    <section class="cv2-table-panel ac-panel">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>子账号名称</th>
            <th>支付宝账号</th>
            <th class="cv2-th-center">职位</th>
            <th class="cv2-th-center">重复数量</th>
            <th class="cv2-th-center">当前状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in rows" :key="`${row.str2}-${i}`" class="cv2-row" :data-v2-ac-row="i">
            <td>{{ row.str1 }}</td>
            <td class="cv2-td-num">{{ row.str2 }}</td>
            <td class="cv2-td-center">{{ row.str3 }}</td>
            <td
              class="cv2-td-center cv2-td-num"
              :title="`此收款支付宝账号存在${row.id2}个，该子账号是其中一个`"
            >{{ row.id2 }}</td>
            <td class="cv2-td-center">
              <span
                class="cv2-st"
                :class="row.str4 === 'true' ? 'cv2-st--info' : 'cv2-st--unassigned'"
                :data-v2-ac-status="i"
              >
                <i class="cv2-dot" aria-hidden="true"></i>{{ row.str4 === 'true' ? '正常' : '禁用' }}
              </span>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td colspan="5" class="cv2-empty" data-v2="ac-empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>{{ emptyText }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </el-dialog>
</template>

<style scoped>
.ac-toolbar {
  margin-bottom: 8px;
}
.ac-note {
  margin: 0;
  color: var(--tt2-summary-orange);
  font-size: 12px;
}
.ac-panel {
  max-height: 56vh;
  overflow: auto;
}
</style>
