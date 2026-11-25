<script setup lang="ts">
import ElDialog from '@/v2/OrderTabDialog.vue'
/**
 * 「设定游戏权限」弹层（v2 人员管理；系统人员批次 2026-11-05，计划 §3.1.1）。
 *
 * 旧页 = island 子页 `layui-island/pages/system-setgames.ts`（对标 `/User/SetGames`）。
 * 多选/单人共用（工具栏「设置游戏权限」传勾选 uid 数组；表格「游戏权限」列传单 uid）。
 * 行为照旧页：
 * - 全量游戏 chip 多选（`fetchAllGames`，value = 游戏名——照旧页语义）+ 首位「全部游戏」
 *   伪选项（勾它 = 全选 / 取消 = 全不选，**永不回填**——它不是任何人的游戏权限）。
 * - 回填规则：多 uid 仅当所有人游戏集合**完全一致**才预勾，否则一个不勾（`commonIds` 同口径）。
 * - 设置方式 radio 两颗（不预选）：统一更换 / 在现有基础上新增（tips 逐字，降级为常驻 hint）。
 * - 「保存设定」初始置灰，勾任一游戏即解锁；方式未选不影响按钮，由保存校验拦。
 * - 保存 = 逐 uid `updateAccount(uid, {games})`（方式 1 = 按 uid 当前集 ∪ 新选集）；
 *   当前集读取失败不吞（吞掉会让并集拿空集 ⇒ 静默删权限）。失败不关层；成功关层刷新。
 */
import { computed, onMounted, ref } from 'vue'
import { getAccount, updateAccount } from '@/api/accounts'
import { fetchAllGames, type ConfigRow } from '@/api/config'

const props = defineProps<{
  /** 要设置的人（工具栏批量 = 勾选行 uid；单元格 = 单 uid）。 */
  uids: number[]
  /** 页面级 toast 出口（炭火 toast 由父页渲染）。 */
  notify: (text: string, kind?: 'info' | 'error') => void
}>()

const emit = defineEmits<{
  /** 关层（任何原因）；父页照旧页 `end` 语义刷新列表。 */
  (e: 'close'): void
  (e: 'saved'): void
}>()

/** 伪选项取值（逐字照旧页 `ALL_GAMES_VALUE`）；它不是租户里的任何一款游戏。 */
const ALL_GAMES_VALUE = '全部游戏'

const MSG = {
  noGame: '至少选择一款游戏',
  noMode: '请选择设置方式',
  saved: '已保存',
} as const

/** 设置方式两颗 radio 的 label 与 tips（逐字照 island `setModeRadiosMarkup`）。 */
const MODE_OPTIONS = [
  {
    value: '0',
    label: '统一更换成所勾选的游戏',
    tip: '将这些子账号的游戏权限全都设置成你选择的游戏',
  },
  {
    value: '1',
    label: '在现有基础上新增勾选的游戏',
    tip: '在现在游戏权限的基础上，新增你所选择的游戏。如果原本就有这个游戏权限，将忽略',
  },
] as const

function errText(e: unknown): string {
  return e instanceof Error ? e.message : '保存失败'
}

/** 并集（当前集在前、去重不排序）——方式 1 的载荷（island `unionIds` 同口径重写）。 */
function unionIds(current: readonly number[], selected: readonly number[]): number[] {
  return [...new Set([...current, ...selected])]
}

/** 「全部一致才回填」判据（island `commonIds` 同口径：排序后的 id 串比较；单人空集 = 真 []）。 */
function commonIds(rows: readonly (readonly number[])[]): number[] | undefined {
  if (rows.length === 0) return undefined
  const key = (ids: readonly number[]): string => [...new Set(ids)].sort((a, b) => a - b).join(',')
  const firstKey = key(rows[0]!)
  return rows.every((ids) => key(ids) === firstKey) ? [...rows[0]!] : undefined
}

const games = ref<ConfigRow[]>([])
const loading = ref(true)
const checkedNames = ref<string[]>([])
const setMode = ref<'' | '0' | '1'>('')
const saving = ref(false)
/** 伪选项自己的状态（永不回填：不参与预勾，也不随真实游戏勾选自动点亮）。 */
const allPseudo = ref(false)
/** 每个 uid 的当前游戏 id（方式 1 求并集、回填判据都要用）。 */
const currentGames = ref<Map<number, number[]>>(new Map())

const gameNames = computed(() => games.value.map((g) => g.name))
const idByName = computed(() => {
  const map = new Map<string, number>()
  for (const g of games.value) map.set(g.name, g.id)
  return map
})

/** 保存钮：勾任一游戏即解锁（伪选项勾上会展开成全部真实游戏，同样解锁）。 */
const saveDisabled = computed(() => checkedNames.value.length === 0)

function onPseudoChange(on: boolean | string | number): void {
  checkedNames.value = on === true ? [...gameNames.value] : []
}

/** selectedIds = 勾选的游戏名换回 id（伪选项不在内——它已展开成真实游戏）。 */
function selectedIds(): number[] {
  return checkedNames.value
    .map((name) => idByName.value.get(name))
    .filter((id): id is number => id !== undefined)
}

async function onSave(): Promise<void> {
  if (saving.value) return
  const ids = selectedIds()
  if (ids.length < 1) {
    props.notify(MSG.noGame)
    return
  }
  if (setMode.value === '') {
    props.notify(MSG.noMode)
    return
  }
  saving.value = true
  try {
    for (const uid of props.uids) {
      const payload =
        setMode.value === '1' ? unionIds(currentGames.value.get(uid) ?? [], ids) : ids
      await updateAccount(uid, { games: payload })
    }
  } catch (e) {
    // 失败不关层：让用户改一改重试（关层刷新挂在父页的 close/saved 事件上）。
    props.notify(errText(e), 'error')
    saving.value = false
    return
  }
  props.notify(MSG.saved)
  saving.value = false
  emit('saved')
}

onMounted(() => {
  void (async () => {
    try {
      const [gameRows, ...accountRows] = await Promise.all([
        fetchAllGames(),
        ...props.uids.map((id) => getAccount(id)),
      ])
      games.value = gameRows
      for (const row of accountRows) currentGames.value.set(row.id, row.games ?? [])
      const nameById = new Map(gameRows.map((g) => [g.id, g.name]))
      const common = commonIds(props.uids.map((id) => currentGames.value.get(id) ?? []))
      checkedNames.value = (common ?? [])
        .map((id) => nameById.get(id))
        .filter((n): n is string => n !== undefined)
    } catch (e) {
      // 当前集读取失败不吞：并集口径下吞掉 = 静默删掉已有权限，直接关层报错。
      props.notify(errText(e), 'error')
      emit('close')
      return
    } finally {
      loading.value = false
    }
  })()
})
</script>

<template>
  <el-dialog
    model-value
    title="设定游戏权限"
    class="cv2-dialog cv2-dialog--md"
    data-v2="dlg-set-games"
    @update:model-value="emit('close')"
  >
    <div class="sg-block">
      <p class="sg-label">勾选游戏</p>
      <p v-if="loading" class="cv2-field-hint">正在加载…</p>
      <template v-else>
        <el-checkbox
          :model-value="allPseudo"
          class="sg-pseudo"
          data-v2="games-all"
          @change="onPseudoChange"
        >{{ ALL_GAMES_VALUE }}</el-checkbox>
        <el-checkbox-group v-model="checkedNames" class="sg-games" data-v2="games-group">
          <el-checkbox v-for="name in gameNames" :key="name" :value="name" :data-v2-game="name">
            {{ name }}
          </el-checkbox>
        </el-checkbox-group>
      </template>
    </div>
    <div class="sg-block">
      <p class="sg-label">设置方式</p>
      <div v-for="m in MODE_OPTIONS" :key="m.value" class="sg-mode">
        <label class="cv2-radio">
          <input v-model="setMode" type="radio" name="v2-setgames-mode" :value="m.value" />
          {{ m.label }}
        </label>
        <p class="cv2-field-hint sg-mode-tip">{{ m.tip }}</p>
      </div>
    </div>
    <div class="cv2-dlg-actions">
      <button
        type="button"
        class="cv2-btn cv2-btn--primary cv2-btn--commit"
        data-v2="set-games-save"
        :disabled="saveDisabled || saving"
        @click="onSave"
      >保存设定</button>
    </div>
  </el-dialog>
</template>

<style scoped>
.sg-block {
  margin-bottom: 14px;
}
.sg-label {
  margin: 0 0 6px;
  font-size: 13px;
  color: var(--tt2-dim);
}
.sg-pseudo {
  margin-right: 0;
}
.sg-games {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 14px;
  margin-top: 6px;
}
.sg-games :deep(.el-checkbox) {
  margin-right: 0;
  height: 28px;
}
.sg-mode {
  margin-bottom: 10px;
}
.sg-mode-tip {
  margin: 2px 0 0 24px;
}
</style>
