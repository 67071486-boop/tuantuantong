<script setup lang="ts">
import { computed, ref } from 'vue'
import { BizError } from '@/api/errors'
import {
  saveVisitorSeats, type VisitorSeat, type VisitorSeatKind, type VisitorSettings,
} from '@/api/internalChatVisitors'

const props = defineProps<{ settings: VisitorSettings }>()
const emit = defineEmits<{ saved: [settings: VisitorSettings] }>()

const seatNames = { hr: '人事客服', orders: '订单客服' }
const seatKinds = Object.keys(seatNames) as VisitorSeatKind[]
const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
const timePattern = /^(?:[01]\d|2[0-3]):[0-5]\d$/
type SeatDraft = Omit<VisitorSeat, 'capacity'> & { capacity: number | '' }

const saved = ref(props.settings)
const seats = ref<SeatDraft[]>([])
const picks = ref<Record<VisitorSeatKind, number | ''>>({ hr: '', orders: '' })
const saving = ref(false)
const stale = ref(false)
const error = ref('')
const savedMessage = ref(false)
const names = computed(() => new Map([
  ...(saved.value?.unavailable_contacts ?? []), ...(saved.value?.candidates ?? []),
].map(({ id, name }) => [id, name])))
const active = computed(() => new Set((saved.value?.candidates ?? []).map(({ id }) => id)))
const locked = computed(() => saving.value || stale.value)
const hasChanges = computed(() => JSON.stringify(seats.value) !== JSON.stringify(saved.value.seats))
const incomplete = computed(() => seats.value.some(seat =>
  typeof seat.capacity !== 'number' || !Number.isInteger(seat.capacity) || seat.capacity < 1 || seat.capacity > 1000 ||
  seat.shifts.some(({ start, end }) =>
    !timePattern.test(start) || (!timePattern.test(end) && end !== '24:00') || start === end,
  ),
))

function contactName(id: number) {
  return names.value.get(id) ?? '账号 ' + id
}

function available(seat: SeatDraft) {
  return (saved.value?.candidates ?? []).filter(({ id }) => !seat.contact_ids.includes(id))
}

function apply(settings: VisitorSettings) {
  const configured = settings.seats
  if (!Array.isArray(configured)) {
    stale.value = true
    error.value = '客服席位配置版本已变更，请刷新页面后再配置。'
    return false
  }
  stale.value = false
  const current = seatKinds.map(kind => configured.find(seat => seat.kind === kind) ?? {
    kind, contact_ids: [], capacity: 10, shifts: [],
  })
  saved.value = { ...settings, seats: configured.length ? current : [] }
  seats.value = current.map(seat => ({ ...seat, contact_ids: [...seat.contact_ids], shifts: seat.shifts.map(shift => ({ ...shift })) }))
  picks.value = { hr: '', orders: '' }
  return true
}

function addContact(seat: SeatDraft) {
  const id = picks.value[seat.kind]
  if (id === '' || !available(seat).some(item => item.id === id)) return
  seat.contact_ids.push(id)
  picks.value[seat.kind] = ''
}

function removeContact(seat: SeatDraft, id: number) {
  seat.contact_ids = seat.contact_ids.filter(candidate => candidate !== id)
  seat.shifts = seat.shifts.filter(shift => shift.contact_id !== id)
}

function addShift(seat: SeatDraft) {
  const contactId = seat.contact_ids[0]
  if (contactId === undefined) return
  seat.shifts.push({ contact_id: contactId, weekday: 0, start: '09:00', end: '18:00' })
}

async function save() {
  if (locked.value || !hasChanges.value || incomplete.value) return
  saving.value = true
  error.value = ''
  savedMessage.value = false
  try {
    const payload: VisitorSeat[] = seats.value.map(seat => ({ ...seat, capacity: Number(seat.capacity) }))
    const result = await saveVisitorSeats(payload)
    savedMessage.value = apply(result)
    if (savedMessage.value) emit('saved', result)
  } catch (cause) {
    error.value = cause instanceof BizError ? cause.message : '保存客服席位配置失败，请重试。'
  } finally {
    saving.value = false
  }
}

apply(props.settings)
</script>

<template>
  <section class="cv2-panel visitor-seat-settings" aria-labelledby="visitor-seat-settings-title">
    <div class="cv2-panel-head">
      <h2 id="visitor-seat-settings-title" class="cv2-panel-title">访客客服席位</h2>
    </div>
    <p class="visitor-settings-hint">访客发消息后，系统在对应席位的当班客服中自动分配。无人可接待时，留言进入队列。</p>
    <form class="visitor-settings-form" @submit.prevent="save">
      <fieldset v-for="seat in seats" :key="seat.kind" :data-seat="seat.kind" class="visitor-settings-seat" :disabled="locked">
        <legend>{{ seatNames[seat.kind] }}</legend>
        <div class="visitor-settings-field">
          <label :for="'visitor-seat-' + seat.kind + '-contact'">接待人员</label>
          <div class="visitor-settings-actions">
            <select :id="'visitor-seat-' + seat.kind + '-contact'" v-model="picks[seat.kind]" class="cv2-input"
              :disabled="!available(seat).length">
              <option value="">{{ available(seat).length ? '选择接待人员' : '没有其他可选人员' }}</option>
              <option v-for="{ id, name } in available(seat)" :key="id" :value="id">{{ name }}</option>
            </select>
            <button type="button" class="cv2-btn" :aria-label="'添加' + seatNames[seat.kind] + '接待人员'"
              :disabled="picks[seat.kind] === ''" @click="addContact(seat)">添加</button>
          </div>
          <ul v-if="seat.contact_ids.length" class="visitor-settings-members">
            <li v-for="id in seat.contact_ids" :key="id">
              {{ contactName(id) }}<span v-if="!active.has(id)" class="visitor-settings-hint">（暂不可接待）</span>
              <button type="button" class="cv2-btn cv2-btn--sm" :aria-label="'移除' + seatNames[seat.kind] + contactName(id)"
                @click="removeContact(seat, id)">移除</button>
            </li>
          </ul>
        </div>
        <div class="visitor-settings-field">
          <label :for="'visitor-seat-' + seat.kind + '-capacity'">每人同时接待上限</label>
          <input :id="'visitor-seat-' + seat.kind + '-capacity'" v-model.number="seat.capacity" class="cv2-input visitor-seat-capacity"
            type="number" min="1" max="1000" step="1" required />
        </div>
        <div class="visitor-settings-field">
          <span>每周班次</span>
          <p class="visitor-settings-hint">未设置班次时，所有接待人员全天可接待。设置后，按各人的班次接待。结束早于开始表示跨天，全天可填 00:00 至 24:00。</p>
          <div v-for="(shift, index) in seat.shifts" :key="index" class="visitor-settings-shift">
            <select v-model="shift.contact_id" class="cv2-input" aria-label="班次接待人员">
              <option v-for="id in seat.contact_ids" :key="id" :value="id">{{ contactName(id) }}</option>
            </select>
            <select v-model="shift.weekday" class="cv2-input" aria-label="星期">
              <option v-for="(day, dayIndex) in weekdays" :key="day" :value="dayIndex">{{ day }}</option>
            </select>
            <input v-model="shift.start" class="cv2-input" type="time" aria-label="开始时间" required />
            <input v-model.trim="shift.end" class="cv2-input" type="text" placeholder="HH:mm" maxlength="5" aria-label="结束时间" required />
            <button type="button" class="cv2-btn cv2-btn--sm" :aria-label="'移除第 ' + (index + 1) + ' 条班次'"
              @click="seat.shifts.splice(index, 1)">移除</button>
          </div>
          <button type="button" class="cv2-btn visitor-settings-add-shift" :aria-label="'添加' + seatNames[seat.kind] + '班次'"
            :disabled="!seat.contact_ids.length" @click="addShift(seat)">添加班次</button>
        </div>
      </fieldset>
      <div class="visitor-settings-actions">
        <button type="submit" class="cv2-btn cv2-btn--primary" :disabled="locked || !hasChanges || incomplete">
          {{ saving ? '保存中…' : '保存席位配置' }}
        </button>
        <p v-if="incomplete" class="visitor-settings-hint">请填写有效的接待上限和班次时间。</p>
        <p v-else-if="savedMessage && !hasChanges" role="status">客服席位配置已保存</p>
      </div>
    </form>
    <p v-if="error" role="alert" class="visitor-settings-error">{{ error }}</p>
  </section>
</template>

<style scoped>
.visitor-seat-settings { max-width: 820px; margin-top: 16px; }
.visitor-settings-hint { color: var(--tt2-dim); margin-block: 0 4px; }
.visitor-settings-form, .visitor-settings-seat, .visitor-settings-field { display: grid; gap: 14px; }
.visitor-settings-field { gap: 6px; }
.visitor-settings-seat {
  min-inline-size: 0;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
}
.visitor-settings-seat legend { padding-inline: 6px; font-weight: 700; color: var(--tt2-ink); }
.visitor-settings-actions, .visitor-settings-shift, .visitor-settings-members, .visitor-settings-members li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.visitor-settings-actions select { width: min(100%, 280px); }
.visitor-settings-shift input { width: 110px; }
.visitor-seat-capacity { width: 100px; }
.visitor-settings-add-shift { justify-self: start; }
.visitor-settings-members { margin: 0; padding: 0; list-style: none; }
.visitor-settings-members li {
  padding: 4px 8px;
  border: 1px solid var(--tt2-line);
  border-radius: 4px;
}
.visitor-settings-error { color: var(--tt2-st-error); }
</style>
