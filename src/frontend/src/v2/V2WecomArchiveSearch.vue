<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import V2WecomArchivePane from './V2WecomArchivePane.vue'
import { archiveAccess } from './wecomArchive'

const router = useRouter()
const allowed = ref(false)
const checking = ref(true)
const accessError = ref('')
const initialQuery = ref('')
const searchKey = ref(0)

function prefillFromOrder(fullPath: string) {
  const history = router.options.history
  const keyword = history.state.wecomArchiveKeyword
  if (typeof keyword !== 'string' || !keyword) return
  history.replace(fullPath, { wecomArchiveKeyword: null })
  initialQuery.value = keyword
  searchKey.value += 1
}

async function checkAccess() {
  checking.value = true
  accessError.value = ''
  try {
    allowed.value = (await archiveAccess()).enabled
  } catch {
    allowed.value = false
    accessError.value = '聊天记录查看权限加载失败'
  } finally {
    checking.value = false
  }
}

watch(router.currentRoute, ({ name, fullPath }) => {
  if (name === 'v2-wecom-archive') prefillFromOrder(fullPath)
}, { immediate: true })
onMounted(() => { void checkAccess() })
</script>

<template>
  <main class="wecom-archive-page" aria-label="查找聊天记录">
    <p v-if="checking" role="status">正在核对聊天记录查看权限…</p>
    <div v-else-if="accessError" role="alert">
      <p>{{ accessError }}</p>
      <button type="button" class="cv2-btn cv2-btn--sm" @click="checkAccess">重试</button>
    </div>
    <p v-else-if="!allowed">本账号暂未开通聊天记录查看权限。</p>
    <V2WecomArchivePane v-else :key="searchKey" :initial-query="initialQuery" />
  </main>
</template>

<style scoped>
.wecom-archive-page { padding: 0 16px 16px; }
</style>
