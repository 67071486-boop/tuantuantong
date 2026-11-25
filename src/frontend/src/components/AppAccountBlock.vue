<script setup lang="ts">
import { computed } from 'vue'
import { avatarDataUri } from '@/config/avatar'

/**
 * 侧栏顶部账号区：只画头像和「名字[职位]」。间距与字号在 shell.css，组件不写死像素；工作室名在侧栏 logo 槽，不在这里。
 * 对标在头像外包了跳「我的资料」的链接——跳转是壳的职责，本组件不包那层 `<a>`。名字后面用方括号（抓取页这一处）；顶栏右侧用圆括号，两处各按实测。
 */
const props = defineProps<{
  userName: string
  role: string
  avatarUrl: string | null
}>()

const userLabel = computed(() => (props.role === '' ? props.userName : `${props.userName}[${props.role}]`))
</script>

<template>
  <div class="shell-account">
    <div class="shell-account__photo">
      <img v-if="avatarUrl" class="shell-account__avatar" :src="avatarUrl" alt="" />
      <!-- 默认头像：按昵称哈希取纯色底 + 首字，纯前端生成（spec §8）。尺寸 72 与
           --tt-shell-account-avatar-size 同值，**这里必须写数字**（SVG 需要内联尺寸）。
           圆形由本组件外的 `.shell-account__avatar{border-radius:100%}` 给——生成器**不**画圆。 -->
      <img v-else class="shell-account__avatar" :src="avatarDataUri(userName, 72)" alt="" />
      <p class="shell-account__user">{{ userLabel }}</p>
    </div>
  </div>
</template>
