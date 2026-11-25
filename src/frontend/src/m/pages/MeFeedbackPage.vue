<script setup lang="ts">
import {
  f7Badge as F7Badge, f7Block as F7Block, f7BlockTitle as F7BlockTitle,
  f7Button as F7Button, f7Card as F7Card, f7CardContent as F7CardContent,
  f7List as F7List, f7ListInput as F7ListInput, f7Preloader as F7Preloader,
} from 'framework7-vue'
import MobilePageBar from '../components/MobilePageBar.vue'
import { useWorkerBack } from '../router/useWorkerBack'
import { onMounted, ref } from 'vue'
import { Toast } from '../framework7Ui'
import { createFeedback, listFeedback, type FeedbackRow } from '@/api/feedback'
import { formatDateTime } from '../utils/datetime'
import {
  FEEDBACK_CONTENT_LABEL, FEEDBACK_CONTENT_MAX_LENGTH, FEEDBACK_CONTENT_PLACEHOLDER,
  FEEDBACK_EMPTY_TEXT, FEEDBACK_FAILED_TEXT, FEEDBACK_LIST_TITLE,
  FEEDBACK_NO_CONTENT_TEXT, FEEDBACK_SUBMIT_BUTTON_TEXT, FEEDBACK_SUBMITTED_TEXT,
} from '../texts/profile'

/**
 * 问题反馈（手机端 `/m/me/feedback`，spec §4；门 = `home.feedback`，后端三端点同门）。提交表单：textarea（`maxlength` 2000，桌面同值；文案逐字）；**纯文字提交**（`image_keys: []` 显式给空数组——桌面那份截图链路手机端不做，见交付报告登记）。内容为空（去空白）⇒ 提示、不发请求。
 * 列表 = `GET /api/business/feedback/`，**同一端点、按身份分流**（收口在服务端 queryset）：非管理员只看到自己的行；**管理员会收到全租户的行**（后端回什么手机端就渲染什么）⇒ 每行**标注提交人**（`created_by_name`，载荷现成）。⚠️ 两处**有意不做**：① 处置控件（`PATCH` 非管理员一律 403，⛔ 不在手机端开写入口）；② 截图缩略列（手机端纯文字视角）。
 * 空态「还没有反馈记录」；提交成功 → toast「反馈已提交，感谢反馈」+ 清空表单 + 重取列表（桌面同款）。失败 toast 后端 `msg` 原样（兜底「操作失败」）。
 */

const goBack = useWorkerBack(() => ({ name: 'm-me' }))

const content = ref('')
const submitting = ref(false)
/** 列表状态：`loading`（首屏）→ `ready` / `error`。 */
const listState = ref<'loading' | 'ready' | 'error'>('loading')
const rows = ref<FeedbackRow[]>([])
const listError = ref('')

function errText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : FEEDBACK_FAILED_TEXT
}

async function loadList(): Promise<void> {
  listError.value = ''
  listState.value = 'loading'
  try {
    const data = await listFeedback()
    rows.value = data.items
    listState.value = 'ready'
  } catch (e) {
    listState.value = 'error'
    listError.value = errText(e)
    Toast.error(listError.value)
  }
}

async function submit(): Promise<void> {
  if (submitting.value) return
  const text = content.value.trim()
  if (text === '') {
    Toast.error(FEEDBACK_NO_CONTENT_TEXT)
    return
  }
  submitting.value = true
  try {
    // `image_keys` 显式给空数组（桌面纪律：「忘了传」与「刻意不传」在后端要分得开）。
    await createFeedback({ content: text, image_keys: [] })
    content.value = ''
    Toast(FEEDBACK_SUBMITTED_TEXT)
    await loadList()
  } catch (e) {
    Toast.error(errText(e))
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  void loadList()
})
</script>

<template>
  <section class="m-fb">
    <MobilePageBar title="问题反馈" back-test-id="feedback-back" @back="goBack" />

    <F7List strong inset outline data-testid="feedback-form">
      <F7ListInput
        v-model:value="content"
        class="m-fb-field__textarea"
        input-id="m-feedback-content"
        type="textarea"
        :maxlength="FEEDBACK_CONTENT_MAX_LENGTH"
        :placeholder="FEEDBACK_CONTENT_PLACEHOLDER"
        data-testid="feedback-content"
      >
        <template #label><label for="m-feedback-content">{{ FEEDBACK_CONTENT_LABEL }}</label></template>
      </F7ListInput>
    </F7List>
    <F7Block inset>
      <F7Button type="button" large round fill :disabled.prop="submitting" data-testid="feedback-submit" @click="submit">
        {{ FEEDBACK_SUBMIT_BUTTON_TEXT }}
      </F7Button>
    </F7Block>

    <F7BlockTitle data-testid="feedback-list-title">{{ FEEDBACK_LIST_TITLE }}</F7BlockTitle>
    <F7Block v-if="listState === 'loading'" inset class="m-fb__empty" role="status" data-testid="feedback-loading">
      <F7Preloader size="20" aria-hidden="true" /> 加载中…
    </F7Block>
    <F7Block v-else-if="listState === 'error'" inset class="m-fb__retry" data-testid="feedback-error">
      <p class="m-fb__retry-text" role="alert">{{ listError }}</p>
      <F7Button type="button" round outline small data-testid="feedback-retry" @click="loadList">重试</F7Button>
    </F7Block>
    <template v-else>
      <F7Card v-for="row in rows" :key="row.id" outline class="m-fb-row" :data-feedback-id="row.id">
        <F7CardContent>
          <header class="m-fb-row__meta">
            <F7Badge :color="row.status === 'handled' ? 'green' : 'orange'" data-testid="feedback-row-status">
              {{ row.status_label }}
            </F7Badge>
            <time :datetime="row.created_at">{{ formatDateTime(row.created_at) }}</time>
          </header>
          <p class="m-fb-row__content">{{ row.content }}</p>
          <p class="m-fb-row__creator" data-testid="feedback-row-creator">提交人：{{ row.created_by_name }}</p>
          <p v-if="row.admin_note !== ''" class="m-fb-row__note" data-testid="feedback-row-note">
            管理员备注：{{ row.admin_note }}
          </p>
        </F7CardContent>
      </F7Card>
      <F7Block v-if="rows.length === 0" inset class="m-fb__empty" data-testid="feedback-empty">
        {{ FEEDBACK_EMPTY_TEXT }}
      </F7Block>
    </template>
  </section>
</template>

<style scoped>
.m-fb {
  min-height: 100dvh;
  padding-bottom: 16px;
}

.m-fb-row {
  --f7-card-outline-border-color: var(--m-border);
  margin-inline: var(--f7-block-margin-horizontal, 16px);
  margin-block-end: 10px;
}

.m-fb-field__textarea :deep(textarea) {
  min-height: 96px;
  resize: none;
}

.m-fb__empty {
  margin: 0;
  padding: 24px 0;
  font-size: 13px;
  color: var(--m-muted);
  text-align: center;
}

.m-fb__retry {
  margin-top: 8px;
  text-align: center;
}

.m-fb__retry-text {
  margin: 0 0 6px;
  font-size: 13px;
  color: var(--f7-color-red);
}

.m-fb-row__content {
  margin: 10px 0 0;
  font-size: 15px;
  line-height: 1.5;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.m-fb-row__meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.m-fb-row__meta time {
  font-size: 12px;
  color: var(--m-muted);
}

.m-fb-row__creator {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--m-muted);
}

.m-fb-row__note {
  margin: 8px 0 0;
  padding: 6px 8px;
  font-size: 12px;
  line-height: 18px;
  color: var(--m-muted);
  background: var(--m-accent-surface);
  overflow-wrap: anywhere;
  border-radius: 6px;
}
</style>
