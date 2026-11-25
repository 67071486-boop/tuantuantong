<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「录单识别设置」v2（`/v2/order-recognition`；系统维护批次 A 波）。旧页 = island 页
 * `system-autodiscernset.ts`（对标 `/System/autodiscernset`）：单表无工具栏无分页（后端固定
 * 14 行），开弹窗两条路照旧；保存 PATCH 逐条，失败原样 msg 不关弹窗不刷新（计划 §3 页 3）。
 */
import { onMounted, ref } from 'vue'
import { fetchRecognitionSettings, updateRecognitionKeywords, type RecognitionItemRow } from '@/api/recognition'

/* ── 逐字文案（出处 = island 页 / 证据件 system-autodiscernset.html）──────────── */

/** 弹窗字段 label（证据件 L100）与 maxlength（L102）。 */
const FIELD_LABEL = '编辑关键词'
const KEYWORDS_MAXLENGTH = 25
/** 输入框 placeholder（证据件 L102 逐字）。 */
const KEYWORDS_PLACEHOLDER =
  '请设定能识别订单内容中的项目名称关键词，例如：账号 密码，每个关键词之间用逗号或空格分隔'
/** 保存钮（证据件 L107「保存设定」）与成功文案（island submit 的前端文案——后端成功 msg 为空串）。 */
const SAVE_BUTTON_TEXT = '保存设定'
const SAVE_OK_TEXT = '保存成功'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast，同 V2UserList；info 1500ms / error 6000ms）───────── */

const toast = ref<{ text: string; kind: 'info' | 'error' } | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | null = null

function showToast(text: string, kind: 'info' | 'error' = 'info'): void {
  if (toastTimer !== null) clearTimeout(toastTimer)
  toast.value = { text, kind }
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, kind === 'error' ? 6000 : 1500)
}

/* ── 列表（无工具栏无分页；后端固定 14 行、顺序即页面顺序，前端不再排一遍）──────── */

const rows = ref<RecognitionItemRow[]>([])
const loading = ref(false)
const loadError = ref<string | null>(null)

async function reload(): Promise<void> {
  loading.value = true
  try {
    const data = await fetchRecognitionSettings()
    rows.value = data.items
    loadError.value = null
  } catch (e) {
    rows.value = []
    loadError.value = errText(e)
    showToast(loadError.value, 'error')
  } finally {
    loading.value = false
  }
}

/* ── 关键词弹窗（两条开层路共用：行内按钮 + 第一列蓝链；失败不关层不刷新）───────── */

const dlg = ref<{ itemCode: string; typeName: string; keywords: string } | null>(null)
const saving = ref(false)

function openDlg(row: RecognitionItemRow): void {
  dlg.value = { itemCode: row.item_code, typeName: row.name, keywords: row.keywords }
}

async function saveKeywords(): Promise<void> {
  const cur = dlg.value
  if (cur === null || saving.value) return
  saving.value = true
  try {
    await updateRecognitionKeywords(cur.itemCode, cur.keywords)
    dlg.value = null
    showToast(SAVE_OK_TEXT)
    await reload()
  } catch (e) {
    // 失败原样显示后端 msg；不关弹窗、不刷新（照旧页 submit 失败分支）。
    showToast(errText(e), 'error')
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void reload()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="order-recognition">
    <!-- 列表：3 列照旧页（无工具栏、无筛选、无分页——后端固定 14 行） -->
    <section class="cv2-panel cv2-table-panel" aria-label="录单识别设置列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>识别项名称</th>
            <th>订单内容中每一行可以识别的关键词</th>
            <th class="cv2-th-center">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.item_code" class="cv2-row" :data-v2-row="row.item_code">
            <td>
              <!-- 第一列蓝链 = 旧页两条开弹窗路之一（.editrow 委托） -->
              <a
                href="#"
                class="cv2-order-link"
                :title="row.name"
                :data-v2-edit="row.item_code"
                @click.prevent="openDlg(row)"
              >{{ row.name }}</a>
            </td>
            <td :title="row.keywords">{{ row.keywords }}</td>
            <td class="cv2-td-center">
              <button
                type="button"
                class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--commit"
                :data-v2-edit-btn="row.item_code"
                title="修改关键词"
                @click="openDlg(row)"
              >修改关键词</button>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td colspan="3" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else-if="loadError !== null">加载失败：{{ loadError }}</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 关键词编辑弹窗（唯一字段；保存 PATCH 逐条；失败不关层不刷新） -->
    <el-dialog
      :model-value="dlg !== null"
      :title="dlg === null ? '' : `修改${dlg.typeName}的识别关键词`"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-keywords"
      @update:model-value="dlg = null"
    >
      <div class="cv2-field">
        <span class="cv2-field-label">{{ FIELD_LABEL }}</span>
        <el-input
          v-if="dlg !== null"
          v-model="dlg.keywords"
          :maxlength="KEYWORDS_MAXLENGTH"
          :placeholder="KEYWORDS_PLACEHOLDER"
          data-v2="keywords-input"
          @keydown.enter.prevent="void saveKeywords()"
        />
      </div>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="dlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--primary" data-v2="keywords-ok" :disabled="saving" @click="void saveKeywords()">{{ SAVE_BUTTON_TEXT }}</button>
      </div>
    </el-dialog>

    <!-- 炭火 toast（居中深条） -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 表格内锚点动作链：基态不带全局链接下划线、悬停补回（V2UserList 同款）。 */
a.cv2-order-link {
  text-decoration: none;
}

a.cv2-order-link:hover {
  text-decoration: underline;
}

/* 全局提示 = 对标 layer.msg 居中深条（V2UserList 同款；覆盖 carbon.css 顶部白卡片形态）。 */
.cv2-page :deep(.cv2-toast) {
  top: 45%;
  left: 50%;
  transform: translate(-50%, -50%);
  z-index: 3000;
  max-width: 56vw;
  padding: 12px 22px;
  border: none;
  border-radius: 6px;
  background: var(--tt2-toast-bg);
  color: var(--tt2-panel);
  font-size: 14px;
  text-align: center;
}

.cv2-page :deep(.cv2-toast--error) {
  color: var(--tt2-panel);
}
</style>
