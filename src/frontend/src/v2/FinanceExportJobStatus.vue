<script setup lang="ts">
import type { FinanceExportJob } from '@/api/financeExportJobs'

defineProps<{ job: FinanceExportJob; problem: string }>()
defineEmits<{ download: []; retry: [] }>()
</script>

<template>
  <button v-if="job.status === 'ready'" type="button" class="cv2-btn cv2-btn--tool" data-v2="export-download" @click="$emit('download')">
    下载导出文件
  </button>
  <button v-else-if="problem" type="button" class="cv2-btn cv2-btn--tool" data-v2="export-retry" @click="$emit('retry')">
    刷新导出进度
  </button>
  <span v-else role="status" data-v2="export-job-status">
    {{ job.status === 'failed' ? job.message : job.status === 'running' ? '正在生成导出文件' : '导出排队中' }}
  </span>
</template>
