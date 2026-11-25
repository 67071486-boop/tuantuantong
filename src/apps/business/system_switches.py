"""系统设置开关的注册表与读取口（D-0223，2026-10-10 业务方裁定）。 **这是数据 + 一个读取口，不是逻辑。** 每个开关 = 一条 `SwitchDef`（key / 页面文案 / 默认值 / 对标 SK 编号）；租户的覆盖值在 `SystemSwitch` 表（只存被改过的行）。 ## 为什么长这样（照 `RecognitionKeyword` + `intake_rules.RECOGNITION_ITEMS` 的既定形状） - **注册表是默认值的单一出处**：没有覆盖行 = 用默认值，不预置数据行； - **读取只查当前连接（租户 schema）**，⛔ 不接受任何租户参数（`AGENTS.md` §4 租户边界）； - `switch_enabled()` 的判据是 `value == "Y"`：未知值一律当关（fail-closed）。 ## 首条开关（D-0223 的业务口径，逐条都是裁定） | 项 | 值 | 出处 | |---|---|---| | key | `forbid_duplicate_source_order_no` | 本线命名 | | 默认 | `"N"`（默认允许重复，与对标文案「默认允许」一致） | 业务方 第 1 条 | | 语义 | **完全相同的来源单号一律拦**（不看订单来源）——⚠️ **有意偏离对标** SK32（对标是「相同订单来源 + 相同来源单号」才拦，证据件 `.tools/recon/suqing1/html/System__systemsetting.html` L240-245） | 业务方 第 2 条 |
| 只拦手工 | 管理端录单（POST /orders/）与手工编辑订单（PATCH 改来源单号）拦；自动通道（上下游 peer 同步、鸡蛋铺上家同步）与开放平台建改单不受管 | 业务方 第 3 条 | ## 第二条开关：闲管家录单识别拒「待付款」（业务方裁定）业务方原话：「闲管家录单功能应该加一个 录单识别订单的时候 如果订单是**待付款**状态 应该拒绝识别」，并追加「但是这是个开关 在那个页面可以人工配置」⇒ **做成系统开关、默认开**（「应该拒绝」是基线，开关只是让他能关掉）。 | 项 | 值 | 出处 | |---|---|---| | key | `reject_unpaid_xgj_order` | 本线命名 | | 默认 | `"Y"`（**默认拒绝**；业务方说「应该拒绝」，开关是给他关掉用的） | 业务方 | | 语义 | 录单页「读取订单」走**闲管家**那条腿时，上游状态 = **11（待付款）** ⇒ 拒绝识别（业务码 + 一句人话，不返回表单字段） | 业务方 | | 只拦闲管家 | ⛔ **阿奇索那条腿不受本开关管**：两家的状态码语义不同、不可比（本仓已有明文登记） | 业务方 | | fail-open | 上游**没给**状态码 / 给了**未知**值 ⇒ **放行**（只拦明确的待付款） | spec 线 F §3.2 |
| 配置页 | 【第三方平台】→「闲管家店铺对接」页（该页是**自有功能**，没有对标证据件） | 业务方 | ⚠️ 本开关的 `replica_sk` 是**空串**：那个字段的用途是「前端按它在**对标证据件**里找对应的 radio」，而对标物里没有这一项（配置页是自有页、没有证据件）⇒ **没有可填的 SK 编号**。 ⛔ 不要硬塞一个别人的 SK 号（`system-systemsetting.ts` 按 `replica_sk == "SK32"` 找它自己那一条，塞重了会让那一页找到错的行）。 ## ⛔ 曾有第六条开关 `hall_upstream_throw`（D-0329），2026-10-29 撤除（业务方拍板改准）业务方原话口径：「**同步订单是一整个功能**」——上家镜像卡抛下家（D-0325）随发版自动生效，⛔ 不设租户总闸；「收不收」由下家绑定上**既有的**两开关管（`JidanpuMemberBinding.enabled` + `hall_sync_enabled`，那才是用户心智里的阀）。存量不补抛的判据保留为**常量水位**（`upstream_hall_shadow.UPSTREAM_THROW_SINCE` = 第五车发版时刻），闸本体仍在 `upstream_hall_shadow.ensure_for_card`。 2026-10-29 20:30 后该 key 不再是合法开关（PATCH 它 ⇒ 业务码 1000，`KeyError` 同口径）。 ## 竞态残差（如实登记，D-0223）闸是建单/改单**之前**的一次 `exists()` 查询，没有数据库唯一约束（P2 有意不建， `Order.source_order_no` 只有普通索引）⇒ 两个真正并发的同号建单可能都通过。手工场景可接受（前端已有防重复提交）；要绝对拦需加唯一约束——那是**另一个决策**， ⛔ 不要顺手加。"""
from dataclasses import dataclass
from datetime import datetime

from apps.business.models import SystemSwitch

AUTO_REFUND_PLATFORM_ORDER = "auto_refund_platform_order"


@dataclass(frozen=True)
class SwitchDef:
    """一个系统设置开关的声明。"""

    #: 开关的唯一键（`SystemSwitch.key`；API 路径里也用它）。
    key: str
    #: 页面文案（系统设置页那一行的说明）。
    name: str
    #: 默认值（`"Y"` / `"N"`）；没有覆盖行时用它。
    default: str
    #: 对标的 SK 编号（登记用：前端按它在证据件里找到对应的 radio）。 ⚠️ **空串 = 本仓自有开关、对标物没有对应项**——这类开关的页面落点只能是**自建页**
    #: （自建页没有证据件），⛔ 别拿着空串去证据件里找 radio、也⛔ 不要为了"填满"塞别人的 SK 号（`system-systemsetting.ts` 按 `replica_sk == "SK32"` 找它自己那一条，塞重了会让那一页找到错行）。
    replica_sk: str
    #: 一句说明（注册表自查用，不进页面）。
    description: str


#: 开关定义清单（唯一出处；新增开关 = 这里加一条，表里**不预置**任何行）。
_SWITCH_DEFS: tuple[SwitchDef, ...] = (
    SwitchDef(
        key="forbid_duplicate_source_order_no",
        name="录单时禁止录入相同的来源单号",
        default="N",
        replica_sk="SK32",
        description=(
            "开启后：管理端录单与手工编辑订单时，完全相同的来源单号（不看订单来源）一律拦；"
            "空来源单号不拦；回收站（已软删）的订单不占号；"
            "自动通道（上下游 / 鸡蛋铺上家同步）与开放平台建改单不受本开关管（D-0223）"
        ),
    ),
    SwitchDef(
        key="reject_unpaid_xgj_order",
        name="录单识别闲管家订单时，待付款状态直接拒绝",
        default="Y",
        # ⚠️ 空串 = 「对标物里没有对应项」（本开关落在**自有页**「闲管家店铺对接」上，没有证据件）。
        replica_sk="",
        description=(
            "开启后：录单页「读取订单」走闲管家那条腿时，上游订单状态为 11（待付款）一律拒绝识别"
            "（业务码 + 一句人话，不返回表单字段）；上游没给状态码或给了未知值一律放行（fail-open）；"
            "阿奇索那条腿不受本开关管（两家的状态码语义不同、不可比）"
        ),
    ),
    SwitchDef(
        key="WITHDRAW_AUTO_PAYOUT",
        name="提现审核通过后按冷静期自动代付（默认 T+3）",
        # ⭐ 默认 **开**（业务方追加口径：「开关开就行了」；Jeepay 转账已实测通过）。
        # ⚠️ 这是**临时口径**：验收完可以关（关掉时 `payout_due_withdrawals` 一条都不发）。
        default="Y",
        # ⚠️ 空串 = 本仓自有开关、对标物没有对应项（没有证据件里的 radio）。
        replica_sk="",
        description=(
            "开启后：`payout_due_withdrawals` 定时任务把「已通过 + 冷静期已到 + 没有在途单」的"
            "提现单自动发起 Jeepay 支付宝代付；冷静期分钟数见 env "
            "WITHDRAW_PAYOUT_DELAY_MINUTES（默认 4320 = 3 天），关闭后任务一条都不发、"
            "提现仍走人工审核 / 人工打款（2026-10-21 临时口径，验收完可关）"
        ),
    ),
    SwitchDef(
        key="hall_simulated_orders",
        name="接单大厅生成模拟单（演示卡）",
        # ⭐ 默认 **关**：生成是显式动作——模拟卡会改写大厅的可见内容，默认不该发生。
        default="N",
        # ⚠️ 空串 = 本仓自有开关、对标物没有对应项（没有证据件里的 radio）。
        replica_sk="",
        description=(
            "开启后：`jidanpu_sync` 每轮（2 小时锚）给有历史订单的游戏补充大厅模拟卡"
            "（每游戏随机 2~3 张、按历史单加价 1.3~1.5 生成、零客户信息），"
            "在厅满 2 小时下架进灰区、再挂 2 小时删除，离厅快照保留 7 天；"
            "模拟卡不建订单行、不进状态机 / 结算 / 对账（D-0296）"
        ),
    ),
    SwitchDef(
        key="hall_preferred_orders",
        name="接单大厅优选订单（高价标记与接单闸）",
        # ⭐ 默认 **开**（业务方下线指令是「把优选关掉」⇒ 开关是给他关用的；
        #    默认 Y = 存量行为一字不变——现有租户不登记任何行，优选照旧）。
        default="Y",
        # ⚠️ 空串 = 本仓自有开关、对标物没有对应项（没有证据件里的 radio）。
        replica_sk="",
        description=(
            "开启后：接单大厅维持优选订单（D-0296）——发单价 > 200 的行亮优选角标、"
            "个人打手接优选单要先完成过订单、优选单强制冻结押金（不依赖租户押金总闸）。"
            "关闭后：大厅所有行不再标优选（优选角标与「只看优选」筛选消失），"
            "接单侧跳过优选完成数闸与优选强制押金——高价单按普通接单规则走"
            "（普通押金闸开着时照样拦）、/assign 指派与机器接手照旧、模拟单生成不受影响"
            "（D-0328，业务方 2026-10-29 知情：关 = 高价单回归普通接单规则）"
        ),
    ),
    # ⛔ 第六条 `hall_upstream_throw`（D-0329）已撤除（业务方拍板
    #    「同步订单是一整个功能」）：功能随发版自动生效，闸与水位见
    #    `upstream_hall_shadow.UPSTREAM_THROW_SINCE`。别再往注册表里加回它。
    SwitchDef(
        key=AUTO_REFUND_PLATFORM_ORDER,
        name="平台退款后自动退单",
        default="Y",
        replica_sk="",
        description=(
            "开启后：收到平台退款通知时尝试退单；代练通未接手先撤回，已接手交人工处理。"
            "关闭后：不自动退单，也不发起退款处理待办。"
        ),
    ),
)

#: 全部开关的注册表（key → 定义）。
SWITCHES: dict[str, SwitchDef] = {switch.key: switch for switch in _SWITCH_DEFS}


def switch_state(key: str) -> tuple[bool, datetime | None]:
    """**一条查询**同时拿「开关现在开不开」与「覆盖行最后修改时刻」。回 `(是否开启, 覆盖行 updated_at)`；无覆盖行 ⇒ `(注册表默认判, None)`。 ⚠️ 值判据（`== "Y"`，fail-closed）**只有这一处实现**：`switch_enabled` 委托本函数
    取第 0 位。调用方既要值又要行的时刻时用本函数，⛔ 不要「`switch_enabled` + 再查一遍行」（那是第二条同样的查询）。 ⚠️ D-0329 的水位消费方（`upstream_hall_shadow` 的开关闸）已于 2026-10-29 撤除（总闸改准为常量水位、自动生效）⇒ 今天本函数的唯一调用方是 `switch_enabled`
    的委托与钉住这条委托的用例——保留它是因为「一条查询拿两样」的形状可复用， ⛔ 别为「没人用」而删（删了 `switch_enabled` 就得回写成两条查询路径）。 ⚠️ 只查**当前连接**（租户 schema），不接受任何租户参数（`AGENTS.md` §4）。 ⚠️ 未知 key 抛 `KeyError`（与 `switch_enabled` 同一条口径：让它响亮地炸）。"""
    row = SystemSwitch.objects.filter(key=key).first()
    if row is None:
        return SWITCHES[key].default == "Y", None
    return row.value == "Y", row.updated_at


def switch_enabled(key: str) -> bool:
    """这个开关现在开不开：覆盖行 `value == "Y"` 才真，无行取注册表默认。（值判据的唯一实现在 `switch_state` 里，本函数委托它取第 0 位。）
    ⚠️ 只查**当前连接**（租户 schema），不接受任何租户参数（`AGENTS.md` §4）。 ⚠️ 未知 key 抛 `KeyError`（调用方都是本仓内部、key 是字面量，让它响亮地炸
    比静默回 False 好——那会表现为「开关永远关」的假行为）。"""
    return switch_state(key)[0]


def switch_rows() -> list[dict]:
    """注册表 ∪ 覆盖行，给列表端点（顺序 = 注册表顺序）。覆盖行的值**只在该值仍是 `"Y"` / `"N"` 时**生效——理论上 API 已挡住其它值（唯一写点是 `system_switch_views.py` 的 `SystemSwitchDetailView.patch`，它只收这两个串），
    这里再收一道口：脏值一律当 `"N"`（关），与 `switch_enabled` 的 `value == "Y"` **同判**。 ⚠️ 这是**防御性口径、今天不可达**（脏值只能由直接改库产生，API 路径到不了它）：
    写它的目的是维持「列表端点与闸读同一个值」这条不变式——对默认 `"N"` 的开关，脏值改前改后都落成 `"N"`；对默认 `"Y"` 的开关，回落默认值会与闸分叉（列表显"开"、闸当"关"），所以这里统一取"当关"。⛔ 别把它叙述成"不加就会出事"。"""
    overrides = {row.key: row.value for row in SystemSwitch.objects.all()}
    rows = []
    for switch in _SWITCH_DEFS:
        value = overrides.get(switch.key, switch.default)
        if value not in ("Y", "N"):
            value = "N"
        rows.append(
            {
                "key": switch.key,
                "name": switch.name,
                "value": value,
                "default": switch.default,
                "replica_sk": switch.replica_sk,
            }
        )
    return rows
