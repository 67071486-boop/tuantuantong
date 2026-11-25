"""回调消费的**健康判据**（本线 T12）：把 §9.4 那三个问题里的第 2 个变成**一句人话**。 > spec：`docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` > §9.4（健康列要能回答的三个问题）/ §5.2（部署冲突）/ §19.2（那场冲突的现场口径） > / §9.5（权限被收回）/ §13.3-C8（`callback_watermark_at` ⛔ 不上页面）；任务书 T12。 ## 一分钟看懂它在干什么一台租户机上，「回调有没有人在消费」这件事**平时看不出来**：回调照旧落盘（接收器是独立进程），页面上的 `last_error` 也可能一直是空的（消费进程压根没跑，当然也不会报错）。spec §5.2 / §19.2 记着这条部署冲突——**同步（消费者）只在一台机器上跑，而回调按域名落到各自那台**。判据是**两列一起看**（§9.4 第 2 问）： | 列 | 含义 | 谁在写 | |---|---|---| | `callback_last_at` | 最近**真的**收到过一条本绑定的回调 | 消费者（`_write_watermark`） | | `callback_watermark_at` | 消费者的**水位**：上一批处理到哪个接收时刻 | 消费者（同一个函数） | ⇒ **后者长期不前进而前者在动** = 回调一直在来、但没有任何东西把它消费掉 = **这台机器没人消费**。 ## ⭐ 为什么由后端算好结论（⛔ 不把两列丢给前端自己比） `callback_watermark_at` 是**内部状态**，spec §4.2 与 §13.3-C8 明令：⛔ 不进序列化器、⛔ 不上页面
（照 `JidanpuMemberBinding.status_pull_at` 的先例）。⇒ 页面拿不到它，也就**没法**自己比。本模块是**唯一**的判据出处：接口只回一句已经算好的话 + 一格布尔（见下面 `ConsumerState`）。 ## ⭐ 阈值口径（多长算「长期不前进」）—— 本模块自定，理由在这里两条判据的**宽严刻意不同**（合取）： 1. **差**：`callback_last_at - callback_watermark_at > LAG_WINDOW`（默认 900 秒）， `LAG_WINDOW = max(LAG_WINDOW_FLOOR, LAG_ROUNDS × 该行自己的 reconcile_seconds)`： - `LAG_ROUNDS = 10`、「N 轮」= 那个绑定自己的 `reconcile_seconds`（兜底对账的间隔，**消费也是每轮跑一次**）⇒ 差 10 轮 = 消费进程至少 10 次没把任何一条事件的水位往前推。 ⛔ **不写死秒数**：一个把间隔调大 10 倍的绑定，它的「10 轮」本来就该跟着变。 - `LAG_WINDOW_FLOOR = 900` 秒：间隔很小时（默认 600 ⇒ 10 轮 = 6000 秒也够，而 `1` 秒的间隔只有 10 秒）兜一个下限，免得「差 11 秒」就被判成没人消费。默认参数下两条都不生效的是下限；两者取大 ⇒ 默认 = `max(900, 10 × 600) = 6000` 秒。 2. **不前进**：`now - watermark > LAG_WINDOW`（同样的窗口）—— ⭐ **这一条是「正常低频」与
「真没人消费」的分界**： ``` 正常低频（低单量工作室，两小时才一单） 12:00 来一条 ⇒ 12:00 就被消费掉 ⇒ 两个时刻都停在 12:00 ⇒ 差 = 0 真没人消费（消费进程没跑 / 一直连不上） 12:00 来一条（没人处理，水位停在上一批）… 14:00 又来三条 ⇒ 水位是几小时前的、而 callback_last_at 是刚发生的 ⇒ 差很大 + 水位很久没动 ``` ⇒ **「两小时没单」差恒为 0**（没有新事件就没有新的 `callback_last_at`）⇒ ⛔ **永远不会**被判成「没人消费」；而**只要有新回调在来**、水位却停在窗口之前 ⇒ 那台机器就是真的没人消费。 ⚠️ 两条**必须合取**：只判「差很大」会在「水位卡住之后回调也不再来了」那种形态下说假话（差值冻在一个很大的数上、永远为真）；只判「水位很旧」会**每一条没人用的②式绑定**都亮红。 ⚠️ **它⛔ 不是「多久没回调就报警」**（spec §9.4 末段明令）：本模块**一处都没有**比较 `now - callback_last_at`。回调稀不稀与「有没有人消费」是两件事，前者是正常现象。先例 = 页面「最近一次收到同步」那块（A19）：**纯信息，不按时间变红**。 ## 四种结论（`ConsumerState`） | 情形 | `bad` | 人话 | |---|---|---| | `mode="password"`（①式） | `False` | 这一行是「账号 + 密码」方式，没有回调要消费 | | 从没收到过回调（`callback_last_at is None`） | `False` | 还没收到过回调（收到了会在这里显示有没有人消费） | | **回调在来、水位长期不前进** | **`True`** | ⭐ 回调有在来，但这台机器上没有任何东西在处理它… | | 其余（水位在动 / 最近一条回调本身就很旧） | `False` | 有回调进来时都处理得了（最近一次收到：…） | ⚠️ **三档里只有第 3 档是「异常」**，另外三档都是**信息**：页面按 `bad` 决定要不要用暖色， ⛔ **不许**按时刻自己再判一次（那会把 §9.4 明禁的「按时间变红」带回来）。 ## ⛔ 本模块**不**做什么（边界，别顺手加） - ⛔ **不碰模型 / 迁移**：T1 的九列是冻结的，本模块只**读**其中两列。 - ⛔ **不写任何东西**（一次 `UPDATE` 都没有）：结论是**算出来的**，不落库。 - ⛔ **不打电话给上游**（它一个字节都不发）。 - ⛔ **不做「权限被收回」那一档**（spec §9.5 落在 `jidanpu_openapi` 的取 token 退避上，见那条注释）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from django.utils import timezone as django_timezone

from apps.business import jidanpu_openapi

#: 水位「长期不前进」的判据里那个**轮数**（× 该绑定自己的 `reconcile_seconds`）。
#: 10 轮的理由：一轮的正常延迟是**毫秒级**（水位由同一轮推），一次故障重试是一轮，
#: 而 10 轮足够盖住「一轮里撞上限 / 一次建连超时 / 一次上游抖动」这些**正常**形态。
LAG_ROUNDS = 10

#: 上面那个窗口的**下限**（秒）。理由：`reconcile_seconds` 可以被用户调到很小（下界是 1 秒，
#: 见 `jidanpu_upstream_views` 的 `min_value=1`）⇒ 不设下限时「差 11 秒」就会被判成没人消费。
#: 15 分钟 = 常驻循环（默认 3 秒一拍）的 300 拍，够长。
LAG_WINDOW_FLOOR = 900

#: ⛔ **没有** `callback_last_at` 时那句（信息，⛔ 不是异常）：我们**判不了**「有没有人消费」——
#: 一条回调都还没来过时，消费者跑没跑在数据上**看不出来**。
NEVER_RECEIVED_TEXT = "还没收到过本条绑定的回调（一条都没来过时，看不出有没有人在消费）"
#: ①式那一行（没有回调地址，也就没有回调要消费）。
PASSWORD_MODE_TEXT = "这条绑定用的是「账号 + 密码」方式，没有回调要消费"
#: ⭐ **「这台机器没人消费」**——本模块存在的唯一理由（spec §9.4 第 2 问 / §5.2 的部署冲突）。
#: ⚠️ 末句给的是**判据本身**（收到过 / 没处理过），⛔ 不猜原因（可能没跑、可能连不上）。
STALLED_TEXT = (
    "⭐ 回调有在来（最近一次收到 {last}），但这台机器上没有任何东西在处理它"
    "（最后一次处理停在 {watermark}）⇒ 请检查这台机器的鸡蛋铺同步进程有没有在跑"
)
#: 正常那一档（水位在动，或最近一条回调本身就很旧）。⚠️ 只报**时刻**，⛔ 不含任何阈值话术。
FLOWING_TEXT = "有回调进来时都处理得了（最近一次收到：{last}）"


def _as_utc(value: datetime | None) -> datetime | None:
    """库里取回来的时刻 → 带时区的 UTC（`None` ⇒ `None`）。 ⚠️ 判据是 `tzinfo is None`（⛔ 不是真值）：naive 与 aware 直接相减会抛 `TypeError`，
    而那个异常会从一个**纯读的视图**里冒出去 ⇒ 整个列表端点 500（E-0010 家族）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def lag_window(binding) -> timedelta:
    """这一行的「长期不前进」窗口（`max(LAG_WINDOW_FLOOR, LAG_ROUNDS × reconcile_seconds)`）。 ⚠️ `reconcile_seconds` 存 `0`（配置写坏）时按**这一行自己那张表**这一列的默认值算（E-0021 家族：0 是有意义的边界，它的意义是「按默认走」）。
    ⭐ **默认值必须从 `type(binding)` 取、⛔ 不许写死某一个模型**（下家线收口）：两条线各有自己那一列 `reconcile_seconds`，闸门值**不同**——上家 `JidanpuUpstreamBinding` 是 **600**、下家 `JidanpuMemberBinding` 是 **180**（本表既有的列，spec 明令⛔ 不许为了
    「对齐上家线」改成 600）。写死上家那一个会让下家行在「配置写坏」这一格上算出一个 **3.3 倍宽**的窗口 ⇒ 一台真没人消费的机器要多等 3 倍时间才在页面上红起来（**静默**延迟）。默认值的唯一出处仍是**模型列本身**（⛔ 不在这里再写一遍 600 / 180）。"""
    seconds = getattr(binding, "reconcile_seconds", 0) or 0
    if not seconds:
        seconds = type(binding)._meta.get_field("reconcile_seconds").default
    return timedelta(seconds=max(LAG_WINDOW_FLOOR, LAG_ROUNDS * int(seconds)))


@dataclass(frozen=True)
class ConsumerState:
    """「这台机器有没有人在消费」的结论（spec §9.4 第 2 问）。 - `text`：**给人看的一句话**（页面上原样显示，⛔ 前端不加工、不按时刻再判一次）；
    - `bad`：⭐ 这一档是不是**要人管**的那一档（今天只有「回调在来、没人处理」为真）。 ⚠️ 页面的暖色**只**由它决定（⛔ 不是由时刻决定）； - `last_at` / `watermark_at`：两个**内部读数**（后端算结论时用的那两个值）。
    ⚠️ `watermark_at` ⛔ **不进任何接口响应**（spec §13.3-C8）。留在这里只为「结论是怎么来的」可复核（用例 / shell 排障），⛔ 别让序列化器把它读到。"""

    text: str
    bad: bool = False
    last_at: datetime | None = None
    watermark_at: datetime | None = None


def _agree(binding) -> bool:
    """这一行是不是②式（`mode="openapi"`）。⚠️ 判据只有 `mode`（spec §2.1）。"""
    return jidanpu_openapi.is_openapi_binding(binding)


def _stamp(value: datetime | None) -> str:
    """人话里的时刻（`Y-m-d H:i`，**项目时区** `Asia/Shanghai`）。 ⚠️ D-0302：这句话嵌在 STALLED / FLOWING 文本里**给人看**（绑定健康页），
    旧写法按 UTC 出墙钟、比页面上的本地钟点**差 8 小时** ⇒ 先 `localtime` 再格式化。 ⚠️ 只改**展示**：`consumer_state` 里判据用的减法仍是 UTC 时刻运算，不在这里。
    ⛔ 不引前端那个 `formatDateTime`（那是另一个进程的事）。"""
    if value is None:
        return "—"
    return django_timezone.localtime(value).strftime("%Y-%m-%d %H:%M")


def consumer_state(binding, *, now: datetime) -> ConsumerState:
    """算一行绑定的「有没有人在消费」（spec §9.4 第 2 问；判据与阈值见模块 docstring）。 ⚠️ **纯读**：不落库、不打上游、不写日志。
    ⚠️ **只有「回调在来 + 水位长期不前进」这一档是异常**（`bad=True`）： - `mode="password"` / 从没收到过回调 ⇒ 我们**判不了**或**不适用** ⇒ 信息档；
    - 回调很旧（低单量工作室两小时没单）⇒ 差恒为 0 ⇒ 信息档（spec §9.4 末段：⛔ 不做成报警）。"""
    if not _agree(binding):
        return ConsumerState(text=PASSWORD_MODE_TEXT)
    now = _as_utc(now) or datetime.now(timezone.utc)
    last = _as_utc(getattr(binding, "callback_last_at", None))
    watermark = _as_utc(getattr(binding, "callback_watermark_at", None))
    if last is None:
        return ConsumerState(text=NEVER_RECEIVED_TEXT)
    window = lag_window(binding)
    if watermark is not None and (last - watermark) > window and (now - watermark) > window:
        return ConsumerState(
            text=STALLED_TEXT.format(last=_stamp(last), watermark=_stamp(watermark)),
            bad=True, last_at=last, watermark_at=watermark,
        )
    return ConsumerState(text=FLOWING_TEXT.format(last=_stamp(last)), last_at=last, watermark_at=watermark)


__all__ = [
    "ConsumerState",
    "FLOWING_TEXT",
    "LAG_ROUNDS",
    "LAG_WINDOW_FLOOR",
    "NEVER_RECEIVED_TEXT",
    "PASSWORD_MODE_TEXT",
    "STALLED_TEXT",
    "consumer_state",
    "lag_window",
]
