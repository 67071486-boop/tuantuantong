<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useSessionStore } from '@/stores/session'
import { notifyError } from '@/api/notify'
import { fetchSiteName } from '@/api/site'
import { sanitizeRedirect } from '@/router/safe-redirect'

/**
 * 登录页——「墨带控制台」视觉（V3R 样稿）。本页**解除对标物复刻授权**：新视觉业务方逐轮选定，取值表与对比度实测见 `docs/FRONTEND-THEME.md` §10.7 与 D-0297；像素值以样稿 `d2b-v3r-refined.html`（`.tools/recon/design-login-20261024/`，不进 git）为准。与样稿的六条有意偏离（要点）：① 次要文字 `--dim` 压深为 `#566a63`（样稿值对比度 4.44:1 差线，压深后 5.0:1）② 去掉 `overflow:hidden`、根容器可滚动（statusline 随之从 absolute 改文档流 + `margin-top:auto` 压底）③ 标题允许换行 ④ 焦点色（墨色）与错误色（绯红）分离 ⑤ 错误横幅绝对定位悬浮、不占布局流（出现/消失不撑跳）⑥ 头行删与副标题重复的「内部管理系统」、标题字距 0.05em、「↵ Return」≤768 与提交中隐藏。
 * **表单内的会话状态行是登录状态机**：空闲「> session 未建立 · 等待认证」→ 提交中「> 正在验证凭据 …」→ 失败「> 登录失败」。⚠️ 失败态是**固定四字**——服务端 msg **只在 `.login-error` 一处显示**，绝不把 `errorText` 复制进状态行；带 `aria-live="polite"` 与常驻闪烁块光标（尊重 `prefers-reduced-motion`）。**行为逻辑一条未动**：账号/密码登录、回车提交、提交中禁用、错误只显示服务端 msg、redirect 加固、工作室名标题与 `document.title`。
 * ⚠️ **测试锚点清单**——下面这些是单测（`src/views/__tests__/LoginView.spec.ts`）与 e2e 的钩子，**改名前先查 spec**（`docs/plans/2026-10-24-login-redesign-v3r.md` §2）：`input[name="username"|"password"]` + `#login-username` / `#login-password`、输入框 `@keyup.enter`、表单里**唯一**的 `<button class="login-submit">`（提交中带 `aria-busy`）、错误节点 `.login-error` + `role="alert"`（文本恰为服务端 msg，全页唯一）、主标题 `.login-title`（取不到工作室名回落「团团通」；标题里的块光标 span 必须保持**空内容**——`.text()` 断言的是标题文本本身）。
 */

/** 展示用常量（2026-10-24 重设计新增，**只**给页底 statusline 用）：当日日期，
 * 本地时区格式化成 `YYYY-MM-DD`（月/日补零）。不参与任何行为逻辑。 */
const todayText = (() => {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
})()
/** 展示用常量：statusline 右侧的当前域名（`location.hostname`，不放假数据）。 */
const hostName = location.hostname

const session = useSessionStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const submitting = ref(false)
const errorText = ref('')
/** 工作室名（公开端点 `GET /api/auth/site`）。取不到就是 `''`，模板按 `|| '团团通'` 回落。 */
const studioName = ref('')

const canSubmit = computed(() => username.value !== '' && password.value !== '' && !submitting.value)

/**
 * 会话状态行（登录状态机）的文案（**纯展示派生值**，只读上面两个既有 ref）：
 * 空闲 / 提交中 / 失败三态。⚠️ 失败态是固定文案「登录失败」——服务端 msg 只在
 * `.login-error` 一处显示（红线，见文件头注释），这里**不许**插 `errorText`。
 */
const statusText = computed(() => {
  if (submitting.value) return '正在验证凭据 …'
  if (errorText.value) return '登录失败'
  return 'session 未建立 · 等待认证'
})

/**
 * 工作室名只在挂载时取一次；本文件里 `document.title` **只有这一处赋值**。⚠️ 取不到名字时**不动** `document.title`（spec：`h1` 回落「团团通」、title 不动——`index.html` 自己的那句原样留着；`h1` 的回落由模板的 `|| '团团通'` 负责）。
 * ⚠️ 这一步**不在登录路径上**：名字拿不到不拦登录（spec 的静默回落）。
 */
onMounted(async () => {
  studioName.value = await fetchSiteName()
  if (studioName.value) document.title = `团团通 - ${studioName.value}`
})

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  errorText.value = ''
  try {
    await session.login(username.value, password.value)
    const target = sanitizeRedirect(route.query.redirect, (p) => {
      const resolved = router.resolve(p)
      // `router/index.ts` 有 catch-all `/:pathMatch(.*)*` → `not-found`，所以**只判 `matched.length > 0`
      // 在真路由表下恒真**：未注册的 redirect（如 `/nope`，以及今天尚未接线的 `/orders/new`）会落到
      // 404 兜底页而不是 home。必须把兜底路由本身也算作「不命中」。
      return resolved.matched.length > 0 && resolved.name !== 'not-found'
    })
    await router.replace(target ?? { name: 'home' })
  } catch (e) {
    // 只显示服务端 msg（凭证错/用户停用/租户停用；后者是守卫那句，见 spec §3.3）
    errorText.value = e instanceof Error ? e.message : '登录失败'
    if (!(e instanceof Error)) notifyError(e)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <!-- 装饰层：墨带 / 大十字 / 四角十字。全部 aria-hidden、pointer-events:none，不参与交互。
         大十字与底部两个角十字在 ≤768px 隐藏（防与内容/页脚相碰，评审修复轮 A4）。 -->
    <div class="login-band" aria-hidden="true"></div>
    <svg class="login-bigplus" width="220" height="220" viewBox="0 0 220 220" aria-hidden="true">
      <path d="M110 0V220 M0 110H220" stroke="currentColor" stroke-width="1" />
    </svg>
    <svg class="login-cross login-cross--tl" viewBox="0 0 17 17" aria-hidden="true">
      <path d="M8.5 0V17 M0 8.5H17" stroke="currentColor" stroke-width="1" />
    </svg>
    <svg class="login-cross login-cross--tr" viewBox="0 0 17 17" aria-hidden="true">
      <path d="M8.5 0V17 M0 8.5H17" stroke="currentColor" stroke-width="1" />
    </svg>
    <svg class="login-cross login-cross--bl" viewBox="0 0 17 17" aria-hidden="true">
      <path d="M8.5 0V17 M0 8.5H17" stroke="currentColor" stroke-width="1" />
    </svg>
    <svg class="login-cross login-cross--br" viewBox="0 0 17 17" aria-hidden="true">
      <path d="M8.5 0V17 M0 8.5H17" stroke="currentColor" stroke-width="1" />
    </svg>

    <main class="login-panel">
      <div class="login-head login-mono">
        <span>团团通 <i>/</i> WEB</span>
      </div>

      <!-- 块光标 span 必须保持空内容（单测断言 `.login-title` 的文本恰为标题本身），见文件头注释 -->
      <h1 class="login-title">{{ studioName || '团团通' }}<span class="login-cursor" aria-hidden="true"></span></h1>
      <p class="login-sub">内部管理系统（非平台）</p>

      <!-- 表单片：骑在墨带下缘上 -->
      <div class="login-sheet">
        <form class="login-form" novalidate @submit.prevent="submit">
          <!-- 错误**只**显示服务端 msg（spec §5.2）；`role=alert` 让读屏在它出现时念出来。
               绝对定位悬浮在表单上方、不占布局流——出现/消失时表单片与按钮一格不动。 -->
          <p v-if="errorText" class="login-error" role="alert">{{ errorText }}</p>

          <div class="login-row">
            <span class="login-idx login-mono">01</span>
            <label class="login-lbl" for="login-username">账号</label>
            <span class="login-leader" aria-hidden="true"></span>
            <input
              id="login-username"
              v-model="username"
              class="login-input"
              name="username"
              type="text"
              autocomplete="username"
              placeholder="请输入您的登录账号"
              :disabled="submitting"
              @keyup.enter="submit"
            />
          </div>

          <div class="login-row">
            <span class="login-idx login-mono">02</span>
            <label class="login-lbl" for="login-password">密码</label>
            <span class="login-leader" aria-hidden="true"></span>
            <input
              id="login-password"
              v-model="password"
              class="login-input"
              name="password"
              type="password"
              autocomplete="current-password"
              placeholder="请输入您的登录密码"
              :disabled="submitting"
              @keyup.enter="submit"
            />
          </div>

          <!-- 会话状态行 = 登录状态机（空闲/提交中/失败，见 statusText）。
               `aria-live=polite` 让读屏感知状态切换；失败态是固定四字，服务端 msg 只在
               上面的 `.login-error` 一处显示 -->
          <p class="login-session login-mono" aria-live="polite">
            <span aria-hidden="true">&gt;</span>{{ statusText }}<span class="login-scursor" aria-hidden="true"></span>
          </p>

          <!-- `type="submit"` + 表单上的 `@submit.prevent` 是给**浏览器**的：真实的表单提交才会让密码管理器弹「保存密码」。`@click` 是给**测试环境**的：jsdom 里点击 `type=submit` 不会派发 `submit` 事件。
               两者同时存在不会重复发请求——`submit()` 第一行就把 `submitting` 同步置位，第二次调用直接早返回。 -->
          <button
            type="submit"
            class="login-submit"
            :disabled="!canSubmit"
            :aria-busy="submitting"
            @click="submit"
          >
            {{ submitting ? '登录中…' : '立即登录' }}<span class="login-ret login-mono" aria-hidden="true">↵ Return</span>
          </button>
        </form>
      </div>
    </main>

    <!-- statusline：终端底栏语言，左「今天也在好好接单」提示符 + 右当日日期与当前域名
         （真实取值，不放假数据）。登录状态机在表单内的会话状态行上。 -->
    <footer class="login-statusline login-mono">
      <span class="login-prompt"><i class="login-prompt-dot" aria-hidden="true"></i>今天也在好好接单</span>
      <span class="login-host">{{ todayText }} · {{ hostName }}</span>
    </footer>
  </div>
</template>

<style scoped>
/*
 * 值来源：V3R 样稿 `.tools/recon/design-login-20261024/d2b-v3r-refined.html`
 * （2026-10-24 业务方逐轮选定）。除文件头注释列出的有意偏离外，取值照抄样稿；
 * 取值表与对比度实测记录见 `docs/FRONTEND-THEME.md` §10.7。
 */

/* 全局样式没有 `body{margin:0}` 重置：浏览器默认的 8px 外边距让 `min-height:100vh` 的页面总高超出视口 → 常驻垂直滚动条，且墨带四周露出一圈白边（样稿里墨带是全出血的）。
 * 不动全局样式，用 `:has()` 只对**含登录页的文档**把 body 外边距清零，别的页面零波及；若将来全局加了 margin 重置，这条可整体删除。⚠️ 用 `:global()` 包裹：裸写 `body:has(.login-page)` 会被 scoped 编译器追加 data-v 属性、变成永不命中的选择器（body 上没有组件作用域属性）。 */
:global(body:has(.login-page)) {
  margin: 0;
}
.login-page {
  --paper: #eef1ef;
  --ink: #22352e;
  --txt: #1d2a26;
  /* 对比度修正：样稿 #5f736c 在纸面 4.44:1 差线，压深一档后 5.0:1 过 4.5 判据（spec §3） */
  --dim: #566a63;
  --line: rgba(29, 42, 38, 0.26);
  --accent: #c73a4f;
  --paper-soft: rgba(238, 241, 239, 0.75);

  position: relative;
  min-height: 100vh;
  /* 纵向 flex + statusline 的 margin-top:auto：内容不足一屏时压到页底，
     超过一屏时跟在内容后面——不用样稿的 absolute + overflow:hidden（要允许滚动） */
  display: flex;
  flex-direction: column;
  background: var(--paper);
  color: var(--txt);
  font-family: 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
}

.login-mono {
  font-family: 'Menlo', 'Monaco', 'Courier New', monospace;
}

/* —— 装饰层 —— */
.login-band {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 45%;
  background: var(--ink);
}

.login-bigplus {
  position: absolute;
  right: 6%;
  top: 15%;
  color: rgba(238, 241, 239, 0.16);
  pointer-events: none;
}

.login-cross {
  position: fixed;
  width: 17px;
  height: 17px;
  pointer-events: none;
}

.login-cross--tl { left: 18px; top: 18px; color: rgba(238, 241, 239, 0.7); }
.login-cross--tr { right: 18px; top: 18px; color: rgba(238, 241, 239, 0.7); }
.login-cross--bl { left: 18px; bottom: 18px; color: rgba(29, 42, 38, 0.6); }
.login-cross--br { right: 18px; bottom: 18px; color: rgba(29, 42, 38, 0.6); }

/* —— 面板 —— */
.login-panel {
  /* position:relative：把自己抬到装饰层之上（同为定位元素时按 DOM 序绘制） */
  position: relative;
  width: 560px;
  max-width: 100%;
  margin: 0 auto;
  padding: 104px 0 40px;
}

.login-head {
  display: flex;
  align-items: baseline;
  font-size: 12px;
  letter-spacing: 0.22em;
  color: var(--paper-soft);
  margin-bottom: 36px;
}

.login-head i {
  color: var(--accent);
  font-style: normal;
}

.login-title {
  margin: 0;
  color: var(--paper);
  font-size: 52px;
  font-weight: 600;
  letter-spacing: 0.05em;
  line-height: 1.2;
  /* 样稿此处 white-space:nowrap —— 有意去掉：长工作室名允许换行（spec §3） */
}

/* 块光标：字符宽的实心块，坐在基线上——「等输入」的暗号。**保持空内容**（文件头注释）。
   与 statusline 光标同一组 blink 参数（A6：标题光标也常驻闪烁）。 */
.login-cursor {
  display: inline-block;
  width: 24px;
  height: 42px;
  margin-left: 18px;
  background: var(--accent);
  transform: translateY(2px);
  animation: login-blink 1.1s steps(2, start) infinite;
}

.login-sub {
  margin: 16px 0 0;
  color: var(--paper-soft);
  font-size: 13px;
  letter-spacing: 0.26em;
}

/* —— 表单片 —— */
.login-sheet {
  /* margin-top 给错误横幅（悬浮在表单上方）留出常驻净空：横幅出现时subtitle 不被压住 */
  margin-top: 54px;
  background: var(--paper);
  border: 1px solid var(--line);
  box-shadow: 0 20px 48px rgba(29, 42, 38, 0.2);
  padding: 10px 30px 30px;
}

.login-form {
  position: relative;
  /* 「防错误横幅撑跳」判据（评审修复轮 A8）：错误横幅是绝对定位（不占布局流），
     本盒高度恒等于**无错误时的自然高度**——下面各档的值是浏览器在 1440 / ≤768 / 390
     宽度下实测的自然高度，把盒子钉死，横幅出现/消失时表单片与按钮一格不动。 */
  min-height: 242px;
}

/* 错误横幅：绯红浅底深字（文字在浅底上 ≈7:1，过 4.5 判据）；悬浮在表单上方、不占布局流 */
.login-error {
  position: absolute;
  left: 0;
  right: 0;
  bottom: calc(100% + 10px);
  margin: 0;
  padding: 8px 12px;
  font-size: 13px;
  line-height: 1.5;
  color: #8a2130;
  background: #f6e4e7;
  border: 1px solid #e5b6bd;
  box-shadow: 0 10px 24px rgba(29, 42, 38, 0.28);
}

.login-row {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 19px 2px;
  border-bottom: 1px dashed var(--line);
  /* 焦点把底线加粗到 2px 时用 -1px 外边距抵消增量（见 :focus-within）：
     行盒总高恒定，聚焦/失焦一格不跳 */
  transition: border-color 0.15s, border-bottom-width 0.15s, margin-bottom 0.15s;
}

/* 键盘焦点可见（A3：焦点用墨色，与绯红错误色分离）：底线加粗为 2px 实线墨色 + 编号点亮 */
.login-row:focus-within {
  border-bottom: 2px solid var(--txt);
  margin-bottom: -1px;
}

.login-row:focus-within .login-idx {
  color: var(--txt);
  font-weight: 700;
}

.login-idx {
  font-size: 12px;
  color: var(--dim);
  flex: 0 0 22px;
}

.login-lbl {
  font-size: 14px;
  letter-spacing: 0.32em;
  flex: 0 0 auto;
}

.login-leader {
  flex: 0 1 64px;
  border-bottom: 1px dotted rgba(29, 42, 38, 0.4);
  transform: translateY(4px);
}

.login-input {
  flex: 1;
  min-width: 0;
  height: auto;
  padding: 4px 0;
  background: transparent;
  border: 0;
  outline: none;
  color: var(--txt);
  font-family: 'Menlo', 'Monaco', monospace;
  font-size: 15px;
  /* A3：caret 也用墨色，绯红不进输入区 */
  caret-color: var(--txt);
}

.login-input::placeholder {
  color: rgba(86, 106, 99, 0.55);
}

.login-input:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* 会话状态行 = 登录状态机（三态见 statusText），坐在 02 行与按钮之间（样稿原位） */
.login-session {
  margin: 20px 2px 24px;
  font-size: 12px;
  color: var(--dim);
  display: flex;
  align-items: center;
  gap: 8px;
}

.login-submit {
  width: 100%;
  height: 52px;
  cursor: pointer;
  border: 0;
  background: var(--ink);
  color: var(--paper);
  font-family: inherit;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.42em;
  text-indent: 0.42em;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  transition: background-color 0.18s;
}

.login-submit .login-ret {
  position: absolute;
  right: 16px;
  font-family: 'Menlo', 'Monaco', monospace;
  color: rgba(238, 241, 239, 0.7);
  font-size: 13px;
  letter-spacing: 0;
  text-indent: 0;
}

/* 提交中隐藏 Return 提示（「登录中…」不需要回车暗示）；≤768 断点整段隐藏（触屏无回车键） */
.login-submit[aria-busy='true'] .login-ret {
  display: none;
}

.login-submit:hover:not(:disabled) {
  background: var(--accent);
}

.login-submit:focus-visible {
  /* A3：焦点环用墨色，不占绯红 */
  outline: 2px solid var(--ink);
  outline-offset: 3px;
}

/* A7：禁用态 = 文字降透明度（背景保持墨色不变）+ not-allowed，不再整钮降透明 */
.login-submit:disabled {
  cursor: not-allowed;
  color: rgba(238, 241, 239, 0.55);
}

/* —— statusline：左「今天也在好好接单」提示符 + 右侧当日日期与域名 —— */
.login-statusline {
  position: relative;
  /* margin-top:auto 把它压到 flex 列的底部（内容不足一屏时） */
  margin: auto auto 44px;
  width: 560px;
  max-width: 100%;
  box-sizing: border-box;
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 12px;
  letter-spacing: 0.14em;
  color: var(--dim);
  white-space: nowrap;
}

.login-prompt {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 0 0 auto;
}

.login-prompt-dot {
  width: 8px;
  height: 8px;
  background: var(--accent);
  display: inline-block;
}

/* 会话状态行的常驻闪烁块光标（与标题光标同一组参数） */
.login-scursor {
  width: 8px;
  height: 15px;
  background: var(--accent);
  display: inline-block;
  animation: login-blink 1.1s steps(2, start) infinite;
}

@keyframes login-blink {
  to { visibility: hidden; }
}

/* 尊重系统的「减少动态效果」偏好：两处闪烁光标都静止（spec §3） */
@media (prefers-reduced-motion: reduce) {
  .login-scursor,
  .login-cursor {
    animation: none;
  }
}

.login-host {
  /* A5：右侧字距砍半（评审：>0.12em 散架） */
  letter-spacing: 0.05em;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* —— 响应式（spec §3：≤768px 缩标题/光标，表单片与 statusline 90vw，缩字号；
        A4：≤768 隐藏大十字与底部两角十字、statusline 内边距 ≥28px；
        ≤768 起隐藏 ↵Return（触屏无回车键）；≤480 引导线收短为 12px、行距收紧） —— */
@media (max-width: 768px) {
  .login-bigplus,
  .login-cross--bl,
  .login-cross--br {
    display: none;
  }

  .login-panel {
    width: 90vw;
    padding-top: 72px;
  }

  .login-head {
    margin-bottom: 24px;
    letter-spacing: 0.16em;
  }

  .login-title {
    font-size: clamp(30px, 8vw, 40px);
  }

  .login-cursor {
    width: 16px;
    height: 26px;
    margin-left: 10px;
    transform: translateY(1px);
  }

  .login-sub {
    font-size: 12px;
    letter-spacing: 0.18em;
  }

  .login-sheet {
    margin-top: 48px;
    padding: 6px 18px 20px;
    box-shadow: 0 12px 32px rgba(29, 42, 38, 0.18);
  }

  .login-row {
    gap: 10px;
    padding: 16px 0;
  }

  .login-lbl {
    letter-spacing: 0.2em;
  }

  .login-submit {
    height: 48px;
    margin-top: 32px;
    letter-spacing: 0.28em;
    text-indent: 0.28em;
  }

  /* 平板也多是触屏：↵Return 从 ≤768 起整段隐藏（提交中另有 [aria-busy] 规则） */
  .login-submit .login-ret {
    display: none;
  }

  .login-statusline {
    width: 90vw;
    /* A4：左右 ≥28px 内边距避免压角（配合 border-box，总宽仍 90vw 不溢出） */
    padding: 0 28px;
    font-size: 11px;
    margin-bottom: 24px;
  }

  .login-form {
    /* ≤768 档自然高度（浏览器 600 宽实测） */
    min-height: 234px;
  }
}

@media (max-width: 480px) {
  /* leader 不整段隐藏：保留 12px 短点线段（评审：留一点仪式感） */
  .login-leader {
    flex-basis: 12px;
  }

  .login-row {
    gap: 8px;
    padding: 14px 0;
  }

  .login-form {
    min-height: 220px;
  }

  .login-session {
    margin: 16px 0 18px;
    font-size: 11px;
  }

  .login-statusline {
    /* 窄屏再缩一档并去字距，保证「日期 · 域名」放得下（放不下由
       .login-host 的省略号兜底，不允许出现横向滚动） */
    font-size: 10px;
    letter-spacing: 0;
  }

  .login-statusline .login-host {
    letter-spacing: 0;
  }
}
</style>
