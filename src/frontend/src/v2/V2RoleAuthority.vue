<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「角色职位权限」v2（`/v2/role-authority`；系统维护批次 C 波）。旧页 = island 页
 * `system-roleauthority*.ts`（列表 / 编辑 / 权限清单三文件）。列表 3 列 + 工具栏两键 +
 * 行内三键；逐动作权限门照旧页 AR_ACTION_NODES（无权限 toast）；编辑弹窗 = EP el-tree
 * 两级权限树（check-strictly 复刻「父不联动子」）替代旧 iframe（计划 §3 页 1）。
 */
import { nextTick, onMounted, ref } from 'vue'
import type { TreeInstance } from 'element-plus'
import {
  copyRoleAuthority,
  createRoleAuthority,
  deleteRoleAuthority,
  fetchPermissionTree,
  fetchRolePermissionRows,
  getRoleAuthority,
  listRoleAuthorities,
  updateRoleAuthority,
  type DashboardType,
  type PermissionNodeLeaf,
  type PermissionNodeModule,
  type RoleAuthorityDetail,
  type RoleAuthorityRow,
  type RolePermissionRow,
} from '@/api/roleAuthority'
import { useSessionStore } from '@/stores/session'

const session = useSessionStore()

/* ── 逐字文案（出处 = island 页同语义常量；本地重写不引模块）────────────────── */

/** 名称列蓝链的 title 后缀（`system-roleauthority.ts::NAME_TITLE_SUFFIX` 逐字）。 */
const NAME_TITLE_SUFFIX = '，点击可以查看权限清单'
/**
 * 五个动作各自的权限点（`system-roleauthority.ts::AR_ACTION_NODES` 同表：
 * add/edit/copy → role_authority_edit、delete → role_authority_delete、view → role_authority_view）。
 */
const AR_ACTION_NODES = {
  add: 'system.role_authority_edit',
  edit: 'system.role_authority_edit',
  copy: 'system.role_authority_edit',
  delete: 'system.role_authority_delete',
  view: 'system.role_authority_view',
} as const
type ArAction = keyof typeof AR_ACTION_NODES

/** 编辑弹窗两条校验（`system-roleauthority-edit.ts::EDIT_VALIDATION` 逐字）。 */
const EDIT_VALIDATION = {
  title: '标题不能为空',
  permissions: '请至少选择一个权限',
} as const
/** 保存的在途闸门文案（旧页 MSG_INFLIGHT 逐字）与成功文案（MSG_SAVED）。 */
const MSG_INFLIGHT = '正在保存，请稍候'
const SAVE_OK_TEXT = '保存成功'
/** 未开放叶子被点选时的提示（旧页 SHAKE_NO_NODE_MESSAGE 逐字；照常选中、不置灰）。 */
const NOT_AVAILABLE_TEXT = '尚未开放'
/** 复制弹窗：新名称上限 = 5（对齐后端 Role.name max_length=5；业务方 2026-09-21 裁决偏离对标 6）。 */
const COPY_NAME_MAXLENGTH = 5
/** 复制弹窗两条校验（`system-roleauthority.ts::COPY_VALIDATION` 逐字）。 */
const COPY_VALIDATION = {
  empty: '请填写新职位名称！',
  length: '职位名称为2-5个字符！',
} as const
const COPY_OK_TEXT = '复制成功'
/** 删除确认（旧页 confirmDelete 逐字：一行、无换行）与成功文案。 */
const DELETE_CONFIRM_TEXT =
  '真的删除吗？警告：一旦删除，所有设定为该角色的系统用户将会无法访问系统，正确做法是先将该职位的所有用户职位改成其它的职位'
const DELETE_OK_TEXT = '删除成功'

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '操作失败'
}

/* ── 提示（页面内自绘 toast，同 V2Notices；info 1500ms / error 6000ms）───────── */

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

/* ── 逐动作权限门（照旧页 requireNode：按钮可见，点击时判点、无权限 toast 并中止）── */

function canDo(action: ArAction): boolean {
  return session.canNode([AR_ACTION_NODES[action]])
}

function requireNode(action: ArAction, label: string): boolean {
  const ok = canDo(action)
  if (!ok) showToast(`无权限执行「${label}」`, 'error')
  return ok
}

/* ── 列表（3 列；接口不分页一次取全；空态「暂无数据」照旧页 text.none）────────── */

const rows = ref<RoleAuthorityRow[]>([])
const loading = ref(false)

async function reload(): Promise<void> {
  loading.value = true
  try {
    rows.value = await listRoleAuthorities()
  } catch (e) {
    rows.value = []
    showToast(errText(e), 'error')
  } finally {
    loading.value = false
  }
}

/* ── 编辑弹窗（名称 / 说明 / 可作为销售客服 / 两级权限树 el-tree check-strictly）──
 *
 * 树三口径（计划 §3 页 1「审阅补」）：
 * ① `available=false` 的叶子**照常可点选**（不 disabled），被勾上时 toast「尚未开放」；
 *    叶子名后的橙色 `(说明)` 后缀照搬（色映射炭火 token，见模板 .ra-note）。
 * ② ⚠️ 口径差登记：旧页自绘树「点击已选中的父节点会清空它全部子节点并收起」是 layui
 *    时代的手写语义，EP `check-strictly`（父子互不联动）复刻不了 —— v2 只保证
 *    「父不联动子、子不影响父」，旧页那条「点父清子」不再有，计划已登记。
 * ③ 回填：树数据就绪后 **nextTick** 再 `setCheckedKeys`（组件挂载先于数据是无害的，
 *    但勾选集合必须等树按 modules 渲染完才设）；弹窗 `destroy-on-close` 每次重开都是
 *    新实例 ⇒ 无「复用实例勾选残留」（等价旧页 setCheckedKeys([]) 清残）；且
 *    check-strictly 下 set 不级联，恰好是「父不联动子」的回填语义。
 */

const editDlg = ref<{
  roleId: number
  name: string
  remark: string
  canBeSalesAgent: boolean
  dashboardType: DashboardType
  codes: string[]
} | null>(null)
const saving = ref(false)
const treeLoading = ref(false)

/** 目录树（GET /api/permission-nodes/）缓存：全角色共用一份，首次开弹窗时取。 */
const modules = ref<PermissionNodeModule[]>([])
const treeRef = ref<TreeInstance | null>(null)

const TREE_PROPS = { label: 'name', children: 'children' } as const

function editDlgTitle(): string {
  return editDlg.value !== null && editDlg.value.roleId > 0 ? '编辑权限' : '新增一个角色职位'
}

/** 栏目（顶层模块）永不提示「尚未开放」：栏目的 available 恒为 false、没有信息量，只看叶子。 */
function isModuleNode(node: PermissionNodeLeaf): node is PermissionNodeModule {
  return Array.isArray((node as PermissionNodeModule).children)
}

/** 叶子被勾上且 available=false 时提示一次（照旧页 bindTree 的 onLeafSelected）。 */
function onTreeCheck(data: PermissionNodeLeaf): void {
  if (isModuleNode(data)) return
  if (data.available !== false) return
  const checked = treeRef.value?.getCheckedKeys(false) ?? []
  if (checked.includes(data.code)) showToast(NOT_AVAILABLE_TEXT)
}

async function ensureModules(): Promise<void> {
  if (modules.value.length > 0) return
  treeLoading.value = true
  try {
    modules.value = await fetchPermissionTree()
  } finally {
    treeLoading.value = false
  }
}

async function openEditDlg(roleId: number): Promise<void> {
  if (!requireNode(roleId > 0 ? 'edit' : 'add', roleId > 0 ? '修改权限' : '新增一个角色职位')) return
  editDlg.value = { roleId, name: '', remark: '', canBeSalesAgent: false, dashboardType: '', codes: [] }
  // 并发取：目录树（所有节点）+ 详情（已勾选 code / 三格现值，仅编辑态）——照旧页 mount。
  try {
    const [, detail] = await Promise.all([
      ensureModules(),
      roleId > 0 ? getRoleAuthority(roleId) : Promise.resolve<RoleAuthorityDetail | null>(null),
    ])
    const dlg = editDlg.value
    if (dlg === null) return
    dlg.name = detail?.name ?? ''
    dlg.remark = detail?.remark ?? ''
    dlg.canBeSalesAgent = detail?.can_be_sales_agent ?? false
    dlg.dashboardType = detail?.dashboard_type ?? ''
    dlg.codes = detail?.permission_codes ?? []
    // 树数据就绪后 nextTick 再回填勾选（树三口径③；check-strictly 下 set 不级联）。
    await nextTick()
    treeRef.value?.setCheckedKeys(dlg.codes)
  } catch (e) {
    showToast(errText(e), 'error')
    editDlg.value = null
  }
}

async function saveRole(): Promise<void> {
  const dlg = editDlg.value
  if (dlg === null) return
  const name = dlg.name.trim()
  if (name === '') {
    showToast(EDIT_VALIDATION.title)
    return
  }
  const codes = (treeRef.value?.getCheckedKeys(false) ?? []) as string[]
  if (codes.length < 1) {
    showToast(EDIT_VALIDATION.permissions)
    return
  }
  if (saving.value) {
    showToast(MSG_INFLIGHT)
    return
  }
  saving.value = true
  try {
    const payload = {
      name,
      remark: dlg.remark,
      permission_codes: codes,
      // 必带（PATCH 缺席 = 保持库里的值，漏带会让取消勾选静默不生效）——照旧页 buildSavePayload。
      can_be_sales_agent: dlg.canBeSalesAgent,
      dashboard_type: dlg.dashboardType,
    }
    if (dlg.roleId > 0) await updateRoleAuthority(dlg.roleId, payload)
    else await createRoleAuthority(payload)
    editDlg.value = null
    showToast(SAVE_OK_TEXT)
    await reload()
  } catch (e) {
    // 失败原样显示后端 msg，不关弹窗（照旧页 submit 失败分支）。
    showToast(errText(e), 'error')
  } finally {
    saving.value = false
  }
}

/* ── 权限清单弹窗（标题「<角色名>的权限清单」；5 列；顶级「所属栏目」后端给字符串「无」）── */

const viewDlg = ref<{ roleId: number; roleName: string; rows: RolePermissionRow[] } | null>(null)
const viewLoading = ref(false)

const VIEW_COLS: ReadonlyArray<{ label: string; key: keyof RolePermissionRow; center?: boolean }> = [
  { label: '拥有权限名称', key: 'name' },
  { label: '级别', key: 'level', center: true },
  { label: '所属栏目', key: 'parent_name' },
  { label: '权限说明', key: 'note' },
  { label: '排序', key: 'order_by', center: true },
]

/** 对标三元 `res.menuLevel == 0 ? "一级" : "二级"`（`system-roleauthority-view.ts::levelLabel`）。 */
function levelLabel(level: number): string {
  return level === 0 ? '一级' : '二级'
}

function viewCellText(row: RolePermissionRow, key: keyof RolePermissionRow): string {
  if (key === 'level') return levelLabel(row.level)
  return String(row[key])
}

async function openViewDlg(row: RoleAuthorityRow): Promise<void> {
  if (!requireNode('view', '查看权限清单')) return
  viewDlg.value = { roleId: row.id, roleName: row.name, rows: [] }
  viewLoading.value = true
  try {
    viewDlg.value.rows = await fetchRolePermissionRows(row.id)
  } catch (e) {
    showToast(errText(e), 'error')
    viewDlg.value = null
  } finally {
    viewLoading.value = false
  }
}

/* ── 复制弹窗（当前名称 disabled 预填 / 新名称 maxlength 5 / 说明 50；判据照 validateCopyName）── */

const copyDlg = ref<{ roleId: number; roleName: string; newName: string; remark: string } | null>(null)
const copying = ref(false)

/** 新职位名称校验：返回 null = 通过（判据与旧页 validateCopyName 一致：空 → empty，>5 → length）。 */
function validateCopyName(jobName: string): string | null {
  if (jobName === '') return COPY_VALIDATION.empty
  if (jobName.length > COPY_NAME_MAXLENGTH || jobName.length < 1) return COPY_VALIDATION.length
  return null
}

function openCopyDlg(row: RoleAuthorityRow): void {
  if (!requireNode('copy', '复制职位')) return
  copyDlg.value = { roleId: row.id, roleName: row.name, newName: '', remark: '' }
}

async function submitCopy(): Promise<void> {
  const dlg = copyDlg.value
  if (dlg === null || copying.value) return
  const invalid = validateCopyName(dlg.newName)
  if (invalid !== null) {
    showToast(invalid)
    return
  }
  copying.value = true
  try {
    // 载荷只有 {name, remark}：权限与「可作为销售客服」标记由后端 copy 端点从源角色整份抄。
    await copyRoleAuthority(dlg.roleId, { name: dlg.newName, remark: dlg.remark })
    copyDlg.value = null
    showToast(COPY_OK_TEXT)
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
  } finally {
    copying.value = false
  }
}

/* ── 删除（确认问句逐字；成功「删除成功」）────────────────────────────────────── */

const delDlg = ref<RoleAuthorityRow | null>(null)

function onRowDelete(row: RoleAuthorityRow): void {
  if (!requireNode('delete', '删除职位')) return
  delDlg.value = row
}

async function confirmDelete(): Promise<void> {
  const row = delDlg.value
  if (row === null) return
  try {
    await deleteRoleAuthority(row.id)
    delDlg.value = null
    showToast(DELETE_OK_TEXT)
    await reload()
  } catch (e) {
    showToast(errText(e), 'error')
  }
}

onMounted(() => {
  void reload()
})
</script>

<template>
  <div class="cv2-page" data-v2-page="role-authority">
    <!-- 工具栏两键（证据件 #toolbarDemo 逐字：刷新列表 / 新增一个角色职位） -->
    <div class="cv2-toolbar" data-v2="toolbar">
      <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--find" data-v2="refresh" :disabled="loading" @click="void reload()">刷新列表</button>
      <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="add" @click="void openEditDlg(0)">新增一个角色职位</button>
    </div>

    <!-- 主表：3 列照旧页（名称蓝链 / 角色说明 / 操作）；接口不分页一次取全 -->
    <section class="cv2-panel cv2-table-panel" aria-label="角色职位权限列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>角色职位名称</th>
            <th>角色说明</th>
            <th class="cv2-th-center ra-actions-col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id" class="cv2-row" :data-v2-row="row.id">
            <td>
              <a
                href="#"
                class="cv2-order-link"
                :title="`${row.name}${NAME_TITLE_SUFFIX}`"
                :data-v2-view="row.id"
                @click.prevent="void openViewDlg(row)"
              >{{ row.name }}</a>
            </td>
            <td :title="row.remark">{{ row.remark }}</td>
            <td class="cv2-td-center ra-actions">
              <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--sm cv2-btn--commit" :data-v2-edit-btn="row.id" title="编辑权限" @click="void openEditDlg(row.id)">修改权限</button>
              <button type="button" class="cv2-btn cv2-btn--sm cv2-btn--quiet" :data-v2-copy-btn="row.id" title="复制为新职位" @click="void openCopyDlg(row)">复制职位</button>
              <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--sm" :data-v2-del-btn="row.id" title="删除该角色职位" @click="void onRowDelete(row)">删除职位</button>
            </td>
          </tr>
          <tr v-if="rows.length === 0">
            <td colspan="3" class="cv2-empty" data-v2="empty">
              <template v-if="loading">正在加载…</template>
              <template v-else>暂无数据</template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 编辑弹窗（替代旧 iframe；两级权限树 = EP el-tree + show-checkbox + check-strictly） -->
    <el-dialog
      :model-value="editDlg !== null"
      :title="editDlg === null ? '' : editDlgTitle()"
      class="cv2-dialog cv2-dialog--lg ra-edit-dlg"
      destroy-on-close
      data-v2="dlg-edit"
      @update:model-value="editDlg = null"
    >
      <template v-if="editDlg !== null">
        <div class="cv2-field ra-form-field">
          <span class="cv2-field-label">角色职位名称</span>
          <el-input
            v-model="editDlg.name"
            :maxlength="5"
            placeholder="该角色职位的名称"
            data-v2="name-input"
          />
        </div>
        <div class="cv2-field ra-form-field">
          <span class="cv2-field-label">角色说明</span>
          <el-input
            v-model="editDlg.remark"
            :maxlength="50"
            placeholder="对该角色的作用描述"
            data-v2="remark-input"
          />
        </div>
        <div class="cv2-field ra-form-field">
          <span class="cv2-field-label">可作为销售客服</span>
          <el-switch
            v-model="editDlg.canBeSalesAgent"
            active-text="是"
            inactive-text="否"
            data-v2="sales-agent-switch"
          />
        </div>
        <div class="cv2-field ra-form-field">
          <label class="cv2-field-label" for="role-dashboard-type">首页看板</label>
          <div>
            <select id="role-dashboard-type" v-model="editDlg.dashboardType" class="cv2-input" data-v2="dashboard-type">
              <option value="">默认首页</option>
              <option value="worker">打手首页</option>
              <option value="work">工作看板</option>
            </select>
            <p class="ra-dashboard-help">看板仅决定首页布局，数据权限仍以角色权限为准。</p>
          </div>
        </div>
        <div class="cv2-field ra-form-field ra-form-field--top">
          <span class="cv2-field-label">设置其权限</span>
          <div class="ra-tree" data-v2="perm-tree">
            <el-tree
              v-if="modules.length > 0"
              ref="treeRef"
              :data="modules"
              node-key="code"
              show-checkbox
              check-strictly
              default-expand-all
              :props="TREE_PROPS"
              @check="onTreeCheck"
            >
              <template #default="{ data }">
                <span class="ra-node" :data-v2-node="data.code">{{ data.name }}<span v-if="!Array.isArray(data.children)" class="ra-node-note">({{ data.note }})</span></span>
              </template>
            </el-tree>
            <p v-else class="ra-tree-loading">{{ treeLoading ? '正在加载…' : '暂无数据' }}</p>
          </div>
        </div>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="editDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="role-save" :disabled="saving" @click="void saveRole()">保存</button>
        </div>
      </template>
    </el-dialog>

    <!-- 权限清单弹窗（5 列照旧页 buildViewCols；顶级「所属栏目」= 后端字符串「无」原样渲染） -->
    <el-dialog
      :model-value="viewDlg !== null"
      :title="viewDlg === null ? '' : `${viewDlg.roleName}的权限清单`"
      class="cv2-dialog cv2-dialog--lg"
      destroy-on-close
      data-v2="dlg-view"
      @update:model-value="viewDlg = null"
    >
      <section class="cv2-table-panel" aria-label="权限清单">
        <table class="cv2-table">
          <thead>
            <tr>
              <th v-for="col in VIEW_COLS" :key="col.key" :class="{ 'cv2-th-center': col.center }">{{ col.label }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, i) in viewDlg?.rows ?? []" :key="i" class="cv2-row">
              <td v-for="col in VIEW_COLS" :key="col.key" :class="{ 'cv2-td-center': col.center }">{{ viewCellText(row, col.key) }}</td>
            </tr>
            <tr v-if="(viewDlg?.rows.length ?? 0) === 0">
              <td colspan="5" class="cv2-empty" data-v2="view-empty">
                <template v-if="viewLoading">正在加载…</template>
                <template v-else>暂无数据</template>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </el-dialog>

    <!-- 复制弹窗（标题「复制成新职位」；名称上限 5 = 对齐后端，既有裁决偏离对标 6） -->
    <el-dialog
      :model-value="copyDlg !== null"
      title="复制成新职位"
      class="cv2-dialog cv2-dialog--md"
      destroy-on-close
      data-v2="dlg-copy"
      @update:model-value="copyDlg = null"
    >
      <template v-if="copyDlg !== null">
        <div class="cv2-field ra-form-field">
          <span class="cv2-field-label">当前角色职位名称</span>
          <el-input :model-value="copyDlg.roleName" disabled data-v2="copy-current" />
        </div>
        <div class="cv2-field ra-form-field">
          <span class="cv2-field-label">新角色职位名称</span>
          <el-input
            v-model="copyDlg.newName"
            :maxlength="COPY_NAME_MAXLENGTH"
            placeholder="新角色职位名称"
            data-v2="copy-name-input"
          />
        </div>
        <div class="cv2-field ra-form-field">
          <span class="cv2-field-label">角色职位说明</span>
          <el-input
            v-model="copyDlg.remark"
            :maxlength="50"
            placeholder="请输入角色职位说明"
            data-v2="copy-remark-input"
          />
        </div>
        <div class="cv2-dlg-actions">
          <button type="button" class="cv2-btn cv2-btn--quiet" @click="copyDlg = null">取消</button>
          <button type="button" class="cv2-btn cv2-btn--primary cv2-btn--commit" data-v2="copy-submit" :disabled="copying" @click="void submitCopy()">提交</button>
        </div>
      </template>
    </el-dialog>

    <!-- 删除确认（问句逐字照旧页 layer.confirm；默认按钮 确定 / 取消） -->
    <el-dialog
      :model-value="delDlg !== null"
      title="提示"
      class="cv2-dialog cv2-dialog--sm"
      destroy-on-close
      data-v2="dlg-delete"
      @update:model-value="delDlg = null"
    >
      <p class="cv2-dlg-text" data-v2="del-question">{{ DELETE_CONFIRM_TEXT }}</p>
      <div class="cv2-dlg-actions">
        <button type="button" class="cv2-btn cv2-btn--quiet" @click="delDlg = null">取消</button>
        <button type="button" class="cv2-btn cv2-btn--danger cv2-btn--commit" data-v2="del-confirm" @click="void confirmDelete()">确定</button>
      </div>
    </el-dialog>

    <!-- 炭火 toast（居中深条） -->
    <div v-if="toast" class="cv2-toast" :class="{ 'cv2-toast--error': toast.kind === 'error' }" role="status" data-v2="toast">
      {{ toast.text }}
    </div>
  </div>
</template>

<style scoped>
/* 本页新增的形状收在这里；色一律走 carbon.css 的 token（⛔ 不写裸色值）。 */

/* 表格内锚点动作链：基态不带全局链接下划线、悬停补回（V2UserList 同款）。 */
a.cv2-order-link {
  text-decoration: none;
}

a.cv2-order-link:hover {
  text-decoration: underline;
}

td.ra-actions {
  white-space: nowrap;
}

td.ra-actions .cv2-btn--sm + .cv2-btn--sm {
  margin-left: -1px;
}

th.ra-actions-col {
  width: 300px;
}

/* 编辑弹窗加大（业务方回验：两级权限树内容多）：宽 min(1100px, 94vw)、高吃满可用区
 * （carbon.css 全局 max-height 兜底，矮视口不触底）；body 拉成 flex 列，余高全部给树区。 */
.cv2-page :deep(.el-dialog.ra-edit-dlg) {
  width: min(1100px, 94vw);
  height: min(80vh, 900px);
}

.cv2-page :deep(.el-dialog.ra-edit-dlg .el-dialog__body) {
  display: flex;
  flex-direction: column;
}

/* 弹窗表单格：与 filter 面的窄输入区分开（V2GameProjects 的 gp-form-field 同款手法）。 */
.ra-form-field {
  grid-template-columns: 120px 1fr;
  margin-bottom: 12px;
}

.ra-dashboard-help {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--tt2-dim);
}

/* 树字段吃 body 余高（minmax(0,1fr) 防树把行撑爆、滚动收在树内）；标签仍纵置顶。 */
.ra-form-field--top {
  flex: 1 1 auto;
  min-height: 0;
  align-items: stretch;
  grid-template-rows: minmax(0, 1fr);
}

/* 两级权限树容器：高度由上面 flex 链给定，超出在树内滚动（旧页 iframe 90% 高的等价物），
 * 树的展开态照旧全展开。 */
.ra-tree {
  overflow: auto;
  min-height: 120px;
  width: 100%;
}

.ra-tree-loading {
  margin: 0;
  font-size: 13px;
  color: var(--tt2-dim);
}

/* 叶子名后的 (说明) 后缀：旧页 label 的橙色字面色 → 炭火 token（accept 橙族）。 */
.ra-node-note {
  margin-left: 4px;
  color: var(--tt2-st-accept-dot);
}

/* 全局提示 = 对标 layer.msg 居中深条（V2Notices 同款；覆盖 carbon.css 顶部白卡片形态）。 */
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
