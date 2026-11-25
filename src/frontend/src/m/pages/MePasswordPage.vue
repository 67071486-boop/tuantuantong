<script setup lang="ts">
import { f7Block as F7Block, f7BlockFooter as F7BlockFooter, f7Button as F7Button, f7List as F7List, f7ListInput as F7ListInput } from 'framework7-vue'
import MobilePageBar from '../components/MobilePageBar.vue'
import { useWorkerBack } from '../router/useWorkerBack'
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { Toast } from '../framework7Ui'
import { useSessionStore } from '@/stores/session'
import { changePassword } from '@/api/myProfile'
import {
  PASSWORD_CHANGED_TEXT, PASSWORD_CONFIRM_MISMATCH_TEXT,
  PASSWORD_LENGTH_TEXT, PASSWORD_SAME_AS_OLD_TEXT,
} from '../texts/profile'

/**
 * 修改密码（手机端 `/m/me/password`，spec §3）。规则照 D-0149⑥（长度 6–18、新旧必须不同、两次一致）：三道**前端先校验**（省往返；文案句式照后端 `auth_views.py` 的同三句，登记在 `m/texts/profile.ts`）。⛔ 权威判据在后端：它还判「原密码不正确」（先老后新）等，本页把后端 `msg` **原样**显示，⛔ 不自己造失败文案。
 * 成功：toast「修改成功」→ `session.logout()`（服务端终结会话 + 本地清空；失败也照走——本地态由 logout 自己收口）→ `m-login`。
 */

const MIN_LENGTH = 6
const MAX_LENGTH = 18

const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
/** 提交在途闸（确认期间禁点）。 */
const submitting = ref(false)

const session = useSessionStore()
const router = useRouter()

function errText(e: unknown): string {
  return e instanceof Error && e.message ? e.message : '操作失败'
}

async function submit(): Promise<void> {
  if (submitting.value) return
  // 三道早挡的顺序照后端（长度 → 两次一致 → 新旧不同；「原密码不正确」只有后端能判）。
  if (newPassword.value.length < MIN_LENGTH || newPassword.value.length > MAX_LENGTH) {
    Toast.error(PASSWORD_LENGTH_TEXT)
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    Toast.error(PASSWORD_CONFIRM_MISMATCH_TEXT)
    return
  }
  if (newPassword.value === oldPassword.value) {
    Toast.error(PASSWORD_SAME_AS_OLD_TEXT)
    return
  }
  submitting.value = true
  try {
    await changePassword({
      old_password: oldPassword.value,
      new_password: newPassword.value,
      confirm_password: confirmPassword.value,
    })
    oldPassword.value = ''
    newPassword.value = ''
    confirmPassword.value = ''
    Toast(PASSWORD_CHANGED_TEXT)
    // 安全口径：改密后回登录页。logout 失败（网络）也照走——会话由服务端超时收口。
    try {
      await session.logout()
    } catch {
      // 刻意留空：不让 logout 的失败把「修改成功」变成失败态。
    }
    void router.replace({ name: 'm-login' })
  } catch (e) {
    Toast.error(errText(e))
  } finally {
    submitting.value = false
  }
}

const goBack = useWorkerBack(() => ({ name: 'm-me' }))
</script>

<template>
  <section class="m-mepwd">
    <MobilePageBar title="修改密码" back-test-id="password-back" @back="goBack" />

    <F7List id="m-password-form" form strong inset outline dividers data-testid="password-form" @submit.prevent="submit">
      <F7ListInput
        v-model:value="oldPassword"
        input-id="m-password-old"
        name="old_password"
        type="password"
        autocomplete="current-password"
        maxlength="18"
        placeholder="请输入当前登录密码"
        data-testid="password-old"
      >
        <template #label><label for="m-password-old">原登录密码</label></template>
      </F7ListInput>
      <F7ListInput
        v-model:value="newPassword"
        input-id="m-password-new"
        name="new_password"
        type="password"
        autocomplete="new-password"
        maxlength="18"
        placeholder="请输入新的登录密码"
        data-testid="password-new"
      >
        <template #label><label for="m-password-new">新登录密码</label></template>
      </F7ListInput>
      <F7ListInput
        v-model:value="confirmPassword"
        input-id="m-password-confirm"
        name="confirm_password"
        type="password"
        autocomplete="new-password"
        maxlength="18"
        placeholder="请重复输入一遍新密码"
        data-testid="password-confirm"
      >
        <template #label><label for="m-password-confirm">确认新密码</label></template>
      </F7ListInput>
    </F7List>
    <F7BlockFooter>密码长度 6–18 位，且不能与原密码相同</F7BlockFooter>
    <F7Block inset>
      <F7Button type="submit" form="m-password-form" large round fill :disabled.prop="submitting" data-testid="password-submit">
        确认更改
      </F7Button>
    </F7Block>
  </section>
</template>

<style scoped>
.m-mepwd {
  min-height: 100dvh;
  padding-bottom: 16px;
}
</style>
