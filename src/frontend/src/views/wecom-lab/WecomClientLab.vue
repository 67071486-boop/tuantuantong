<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { findLabGroups, getLabSignature, type LabGroup } from '@/api/wecomClientLab'

type Callback = (result: Record<string, unknown>) => void
type WecomSdk = {
  config: (params: Record<string, unknown>) => void
  ready: (callback: () => void) => void
  error: (callback: Callback) => void
  agentConfig: (params: Record<string, unknown>) => void
  invoke: (name: string, params: Record<string, unknown>, callback: Callback) => void
  openEnterpriseChat?: (params: Record<string, unknown>) => void
}
declare global { interface Window { wx?: WecomSdk } }

const sdkReady = ref(false)
const sdkStatus = ref('正在初始化企微客户端接口…')
const groupName = ref('')
const groups = ref<LabGroup[]>([])
const selected = ref<LabGroup | null>(null)
const memberIds = ref('')
const memberPayload = ref('')
const text = ref('【测试】这是一条客户群功能测试消息。')
const linkTitle = ref('【测试】企微客户端接口测试')
const linkDesc = ref('这是一条测试链接消息。')
const linkUrl = ref(window.location.origin + '/wecom-client-lab')
const results = ref<string[]>([])
const autoKey = 'wecom-client-lab-open-once'
const apis = ['openEnterpriseChat', 'updateEnterpriseChat', 'openExistedChatWithMsg', 'shareToExternalChat']

function note(message: string): void { results.value.unshift(message) }
function callback(name: string): Callback {
  return (value) => note(`${name}：${JSON.stringify(value)}。回调只表示接口执行结果，请在企微中核对实际操作。`)
}
function script(src: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const element = document.createElement('script')
    element.src = src
    element.onload = () => resolve()
    element.onerror = () => reject(new Error('企微客户端脚本加载失败'))
    document.head.appendChild(element)
  })
}
async function searchGroup(): Promise<void> {
  selected.value = null
  groups.value = []
  if (!groupName.value.trim()) { note('请填写完整群名。'); return }
  try {
    const found = await findLabGroups(groupName.value.trim())
    groups.value = found.groups
    if (found.too_many) note('同名群超过 5 个，请核对群名。')
    else if (found.groups.length === 0) note('本工作室没有找到该群的缓存，请检查群名和客户群同步。')
  } catch (e) { note(`查询客户群失败：${e instanceof Error ? e.message : '请重试'}`) }
}
function openChat(): void {
  const group = selected.value
  if (!group || !window.wx) { note('请先选择测试群并初始化企微接口。'); return }
  const params = { chatId: group.chat_id, userIds: memberIds.value.trim(), externalUserIds: '', groupName: '' }
  if (window.wx.openEnterpriseChat) {
    window.wx.openEnterpriseChat({ ...params, success: callback('打开会话'), fail: callback('打开会话') })
  } else window.wx.invoke('openEnterpriseChat', params, callback('打开会话'))
}
function armAutoOpen(): void {
  if (!selected.value || !sdkReady.value) { note('请先初始化并选择测试群。'); return }
  sessionStorage.setItem(autoKey, JSON.stringify({ name: selected.value.name, chatId: selected.value.chat_id, userIds: memberIds.value.trim() }))
  note('已准备：刷新一次后将在此企微页面尝试打开所选群，不会自动发送消息或修改成员。')
}
async function runAutoOpen(): Promise<void> {
  const saved = sessionStorage.getItem(autoKey)
  if (!saved) return
  sessionStorage.removeItem(autoKey)
  try {
    const data = JSON.parse(saved) as { name: string; chatId: string; userIds: string }
    const result = await findLabGroups(data.name)
    const group = result.groups.find((g) => g.chat_id === data.chatId)
    if (!group || result.too_many) { note('自动打开取消：测试群信息已变化，请重新选择。'); return }
    groupName.value = data.name
    groups.value = result.groups
    selected.value = group
    memberIds.value = data.userIds
    note('正在尝试页面加载后打开会话；是否需要确认由企微客户端决定。')
    openChat()
  } catch { note('自动打开失败，请手动重新选择测试群。') }
}
function updateMembers(): void {
  if (!sdkReady.value || !selected.value || !window.wx) { note('请先初始化并选择测试群。'); return }
  try {
    const parsed: unknown = JSON.parse(memberPayload.value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('参数必须是对象')
    const data = parsed as Record<string, unknown>
    if (data.chatId !== selected.value.chat_id || Object.keys(data).length < 2) throw new Error('参数须包含当前测试群 chatId 和已确认的成员参数')
    if (!window.confirm('即将在选中的客户群尝试变更成员，请确认测试成员和参数。')) return
    window.wx.invoke('updateEnterpriseChat', data, callback('变更群成员'))
  } catch (e) { note(`请核对变更参数：${e instanceof Error ? e.message : '格式错误'}`) }
}
function sendToGroup(): void {
  if (!sdkReady.value || !selected.value || !window.wx) { note('请先初始化并选择测试群。'); return }
  let url: URL
  try {
    url = new URL(linkUrl.value.trim())
    if (!['https:', 'http:'].includes(url.protocol)) throw new Error('unsupported protocol')
  } catch { note('请填写有效的 HTTP 或 HTTPS 测试链接地址。'); return }
  if (!window.confirm('即将打开所选客户群并尝试发送测试链接，请继续在企微中核对。')) return
  window.wx.invoke('openExistedChatWithMsg', { chatId: selected.value.chat_id, msg: { msgtype: 'link', link: { title: linkTitle.value, desc: linkDesc.value, url: url.href } } }, callback('打开群聊并发消息'))
}
function shareGroups(): void {
  if (!sdkReady.value || !selected.value || !window.wx || !text.value.trim()) { note('请选择测试群并填写消息。'); return }
  if (!window.confirm('企微将打开群发界面，请仅选择已确认的测试群。')) return
  window.wx.invoke('shareToExternalChat', { text: { content: text.value } }, callback('群发客户群'))
}
onMounted(async () => {
  try {
    if (!/wxwork/i.test(navigator.userAgent)) throw new Error('请在电脑企业微信的应用主页中打开此页面。')
    if (window.location.search) throw new Error('测试页地址不能包含查询参数，请使用不带参数的应用主页地址。')
    if (!window.wx) await script('https://res.wx.qq.com/open/js/jweixin-1.2.0.js')
    if (!window.wx?.agentConfig) await script('https://open.work.weixin.qq.com/wwopen/js/jwxwork-1.0.0.js')
    const wx = window.wx
    if (!wx) throw new Error('企微客户端接口不可用。')
    const s = await getLabSignature(window.location.origin + window.location.pathname)
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(() => reject(new Error('企微初始化超时，请检查可信域名及客户端版本。')), 15000)
      wx.error((result) => { window.clearTimeout(timer); reject(new Error(JSON.stringify(result))) })
      wx.ready(() => {
        wx.agentConfig({ corpid: s.corp_id, agentid: s.agent_id, timestamp: s.timestamp, nonceStr: s.nonce,
          signature: s.agent_signature, jsApiList: apis,
          success: () => { window.clearTimeout(timer); resolve() },
          fail: (result: unknown) => { window.clearTimeout(timer); reject(new Error(JSON.stringify(result))) } })
      })
      wx.config({ beta: true, appId: s.corp_id, timestamp: s.timestamp, nonceStr: s.nonce,
        signature: s.corp_signature, jsApiList: apis })
    })
    sdkReady.value = true
    sdkStatus.value = '企微接口已初始化；调用结果与群内实际结果需要分别核对。'
    await runAutoOpen()
  } catch (e) { sdkStatus.value = e instanceof Error ? e.message : '企微初始化失败' }
})
</script>

<template>
  <main class="lab">
    <h1>企微客户端测试</h1>
    <p>仅供在电脑企微中测试。各接口参数和电脑端支持情况尚待实测；只操作专门确认的测试群，企微弹出的确认步骤仍由本人完成。</p>
    <p role="status">{{ sdkStatus }}</p>
    <section><h2>1. 选择客户群</h2>
      <label>完整群名 <input v-model="groupName" data-test="group-name" /></label>
      <button type="button" @click="searchGroup">查找</button>
      <div v-for="group in groups" :key="group.chat_id"><label><input v-model="selected" type="radio" :value="group" /> {{ group.name }}（{{ group.chat_id }}）</label></div>
    </section>
    <section><h2>2. 打开已有会话</h2>
      <label>企微成员 userid（多个用分号分隔，按接口要求填写） <input v-model="memberIds" /></label>
      <button type="button" :disabled="!sdkReady || !selected" @click="openChat">打开会话</button>
      <button type="button" :disabled="!sdkReady || !selected" @click="armAutoOpen">下次刷新时自动尝试打开</button>
    </section>
    <section><h2>3. 变更群成员</h2>
      <p>仅使用已确认的测试成员。成员参数需先按企微文档确认；下方 JSON 中 chatId 必须等于所选群。</p>
      <label>接口参数 JSON <textarea v-model="memberPayload" rows="4" placeholder="填写已核实的接口参数" /></label>
      <button type="button" :disabled="!sdkReady || !selected" @click="updateMembers">尝试变更群成员</button>
    </section>
    <section><h2>4. 群消息和群发</h2>
      <p>打开群并发送只支持链接消息，默认链接指向本测试页。</p>
      <label>测试链接标题 <input v-model="linkTitle" data-test="link-title" /></label>
      <label>测试链接摘要 <input v-model="linkDesc" data-test="link-desc" /></label>
      <label>测试链接地址 <input v-model="linkUrl" data-test="link-url" /></label>
      <button type="button" :disabled="!sdkReady || !selected" @click="sendToGroup">打开已有群并尝试发送链接</button>
      <label>测试消息 <textarea v-model="text" rows="3" /></label>
      <button type="button" :disabled="!sdkReady || !selected" @click="shareGroups">打开客户群群发</button>
      <p>企微群发选择器不受此页面限制，请在企微界面确认并只选择指定的测试群；是否需要确认及是否真正送达请在企微核对。</p>
    </section>
    <section><h2>调用记录</h2><p v-if="results.length === 0">还没有调用记录。</p><p v-for="(result, index) in results" :key="index">{{ result }}</p></section>
  </main>
</template>

<style scoped>
.lab { max-width: 960px; margin: 0 auto; padding: 24px; font-family: sans-serif; color: #263238; }
section { border: 1px solid #d8ddd9; border-radius: 8px; padding: 16px; margin: 16px 0; }
label { display: block; margin: 8px 0; }
input:not([type='radio']), textarea { width: 100%; padding: 8px; box-sizing: border-box; }
button { padding: 8px 14px; margin: 5px 8px 5px 0; cursor: pointer; }
button:disabled { cursor: not-allowed; }
</style>
