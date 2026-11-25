<script setup lang="ts">
import {
  f7Badge as F7Badge, f7BlockTitle as F7BlockTitle, f7Button as F7Button,
  f7Icon as F7Icon, f7List as F7List, f7ListItem as F7ListItem,
} from 'framework7-vue'
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { fetchMyProfile, type MyProfile } from '@/api/myProfile'
import { avatarDataUri } from '@/config/avatar'
import { REALNAME_NOT_TEXT, REALNAME_PASS_TEXT } from '../texts/profile'

const session = useSessionStore()
const router = useRouter()

/** 实名徽标的数据源（会话里没有 ⇒ 单独取一次资料；失败 ⇒ 徽标不渲染）。 */
const isRealName = ref<boolean | null>(null)
/**
 * 资料里的头像链接（评审 Low-1：换完头像回到聚合页要看到新的——会话载荷是登录那一刻的
 * 快照、可能比 `/profile` 旧 ⇒ **优先用它**，会话那份只是还没取到资料时的回落）。
 */
const profileAvatarUrl = ref<string | null>(null)

onMounted(async () => {
  try {
    const profile: MyProfile = await fetchMyProfile()
    isRealName.value = profile.is_real_name
    profileAvatarUrl.value = profile.avatar_url
  } catch {
    isRealName.value = null
  }
})

/** 头像：资料/会话里传过 ⇒ 服务端短时链接；否则按昵称生成的色块（⛔ 不在前端拼 URL）。 */
const avatarSrc = computed(() => {
  const uploaded = profileAvatarUrl.value ?? session.user?.avatar_url
  if (typeof uploaded === 'string' && uploaded !== '') return uploaded
  const name = session.user?.display_name || session.user?.username || ''
  return avatarDataUri(name, 96)
})

const displayName = computed(() => session.user?.display_name ?? '')
const username = computed(() => session.user?.username ?? '')
const accountEntries = [
  { key: 'profile', title: '我的资料', icon: 'person_crop_circle' },
  { key: 'password', title: '修改密码', icon: 'lock' },
]
const supportEntries = computed(() => [
  ...(session.isWorker && session.canNode(['my_profile'])
    ? [{ key: 'notifications', title: '消息通知', icon: 'bell' }]
    : []),
  ...(session.canNode(['home.feedback'])
    ? [{ key: 'feedback', title: '问题反馈', icon: 'chat_bubble' }]
    : []),
])
const groups = computed(() => [
  { label: '账号设置', entries: accountEntries },
  { label: '通知与反馈', entries: supportEntries.value, includeAppDownload: true },
])

async function onLogout(): Promise<void> {
  await session.logout()
  void router.replace({ name: 'm-login' })
}
</script>

<template>
  <section class="m-me">
    <F7List strong inset outline class="m-me-settings m-me-identity" data-testid="me-identity">
      <F7ListItem media-item>
        <template #media><img class="m-me-avatar" :src="avatarSrc" alt="头像" data-testid="me-avatar" /></template>
        <template #title><span data-testid="me-display-name">{{ displayName || username }}</span></template>
        <template v-if="displayName && displayName !== username" #subtitle>
          <span data-testid="me-username">{{ username }}</span>
        </template>
        <template v-if="isRealName !== null" #footer>
          <F7Badge :color="isRealName ? 'green' : 'orange'" data-testid="me-realname-badge">
            {{ isRealName ? `✓ ${REALNAME_PASS_TEXT}` : REALNAME_NOT_TEXT }}
          </F7Badge>
        </template>
      </F7ListItem>
    </F7List>

    <div class="m-me-menu" data-testid="me-menu">
      <template v-for="group in groups" :key="group.label">
        <F7BlockTitle>{{ group.label }}</F7BlockTitle>
        <F7List strong inset outline dividers class="m-me-settings">
          <template #list>
            <RouterLink v-for="entry in group.entries" :key="entry.key" v-slot="{ href, navigate }"
              custom :to="{ name: `m-me-${entry.key}` }">
              <F7ListItem :link="href" external :title="entry.title" :data-testid="`me-entry-${entry.key}`"
                @click="navigate">
                <template #media>
                  <span class="m-me-symbol" aria-hidden="true"><F7Icon :f7="entry.icon" :size="18" /></span>
                </template>
              </F7ListItem>
            </RouterLink>
            <F7ListItem v-if="group.includeAppDownload" link="/download/" external title="下载 App（安卓）">
              <template #media>
                <span class="m-me-symbol" aria-hidden="true"><F7Icon f7="arrow_down_circle" :size="18" /></span>
              </template>
            </F7ListItem>
          </template>
        </F7List>
      </template>
    </div>

    <F7Button large round tonal type="button" class="m-me-logout" data-testid="me-logout" @click="onLogout">
      退出登录
    </F7Button>
  </section>
</template>

<style scoped>
.m-me-settings.list {
  --f7-list-item-min-height: 52px;
  --f7-list-item-border-color: var(--m-border);
  --f7-list-outline-border-color: var(--m-border);
  --f7-list-inset-border-radius: 18px;
  --f7-list-inset-side-margin: 0px;
  --f7-list-margin-vertical: 8px;
}

.m-me-settings :deep(.item-link:focus-visible) {
  outline: 2px solid var(--f7-theme-color);
  outline-offset: -3px;
}

.m-me-identity.list {
  --f7-list-media-item-padding-horizontal: 16px;
  --f7-list-media-item-padding-vertical: 14px;
  --f7-list-item-title-font-size: 20px;
  --f7-list-item-title-font-weight: 650;
  --f7-list-item-subtitle-font-size: 13px;
  --f7-list-item-subtitle-text-color: var(--m-muted);
  margin-block-start: 0;
}

.m-me-identity :deep(.item-content) {
  align-items: center;
}

.m-me-identity :deep(.item-footer) {
  margin-top: 6px;
}

.m-me-avatar {
  width: 60px;
  height: 60px;
  border-radius: 50%;
  object-fit: cover;
}

.m-me-menu .block-title {
  margin-inline: 12px;
  margin-block: 22px 8px;
}

.m-me-symbol {
  display: grid;
  width: 32px;
  height: 32px;
  place-items: center;
  border-radius: 9px;
  color: var(--f7-theme-color);
  background: var(--m-accent-surface);
}

.m-me-logout.button {
  margin-top: 24px;
}
</style>
