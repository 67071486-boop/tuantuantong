"""开放平台的**载荷装配与入参校验**（spec §7.1 / §7.2 / §7.3 / §17.9；T4 / T5）。

    本模块只做两件事：① 把一张订单按**逐字冻结的字段白名单**装配成 dict（对外查单）；
    ② 校验查询参数 / 请求体并装配应用管理载荷（**内部**接口）。

    ## 三条冻结口径（改动前先读，⛔ 都不是"实现细节"）

    1. **字段白名单**：本文件里出现的键就是契约的全部。⛔ 不给 C 组（`sales_agent` /
    `sales_user` / `hall_publisher` / `created_by` / `updated_by`，员工的**身份**）与
    D 组（`remark` / `deleted_at` / `deleted_by`，**内部痕迹**）——那是业务方
    的**裁决**，⛔ 不是"漏了、待补"。也⛔ **不给任何内部主键**（`id`）。
    判据（为什么 `content` 给、`remark` 不给）：**"客户写的"给、"我方员工写的"不给**。
    ⛔⭐ **现行口径（D-0320）**：顶层 `taker_name` 是上面那条员工身份口径的
    **唯一显式例外**——只暴露接手人显示名，供第三方识别外部平台（代练通）接手单、
    跳过自动验收转人工核；其余员工身份字段仍不给（对外措辞与
    `docs/OPEN-API.md` §3.7 的改准块同源，同一次改的）。
    2. **金额一律字符串两位小数；`None` 原样给 `null`**（⛔ 不用 float、⛔ 不把 `null`
    渲染成 `"0.00"`）。后半条是**必须**的：`received_price` / `platform_price` /
    `hall_price` / `fee` 四列 `null=True` 表示「还没填 / 不适用」，而 D-0015 之下
    **0 与负数是合法且有意义**的金额 ⇒ 把"未知"写成"零"会让第三方**静默读错钱**。
    3. **`costs` 的两个键都是列表**（`main` / `sales`）。⚠️ `main` **最多 1 条**（由
    `Performance` 的 `uniq_order_main_taker` 这条**部分**唯一约束保证），`sales` **0..N 条**
    ——`uniq_performance_order_user` 只约束「同单同一个 user」，换客服就会留下第二条
    ⇒ 单值形状会**静默只给第一条**（E-0004 同族；这是对外契约，第三方不会报错、
    只会以为成本就这么多）。

    ## 为什么用普通函数装配而不是 `ModelSerializer`

    契约的字段名与模型列名**不是一一对应**（`game` 取游戏**名**、`project` 取项目**名**、
    `amounts` / `costs` / `content` / `order` / `player` / `customer` 六个分组、
    `status_text` 是派生的），而 `ModelSerializer` 的 `source=` 只能拼扁平名字。
    ⇒ 装配写成一个**逐字段列出来**的函数：**多给一个键、少给一个键都会在用例里当场红**
    （用例那侧写死的是**字面量集合**，⛔ 不从本模块 import）。
"""
import secrets
from decimal import ROUND_HALF_UP, Decimal

from django.utils import timezone
from django.utils.dateparse import parse_datetime as _parse_datetime
from rest_framework import serializers

from apps.accounts.display import user_display_name
from apps.business.models import EmpType
from apps.business.peer_sync import new_token
from apps.core.storage import get_storage
from apps.openapi import outbound
from apps.openapi.models import ApiAppState, CallbackState, OpenApiDelivery

#: 金额 / 率出口的小数位（与 `apps/business/api/console_views.py` 的 `_money` 同口径）。
CENT = Decimal("0.01")

#: 来源单号查询的**上限**（spec §7.2：命中多条时回列表，**至多 20 条**）。
#: ⚠️ `total` 是**命中总数**、不是本页条数（spec §7.2 逐字冻结）——两者在 >20 条时**分叉**，
#: 所以用例里必须有一条「命中 2 条回 2 条」与一条「命中 21 条 ⇒ total=21、items 只有 20」。
SOURCE_ORDER_NO_LIMIT = 20

#: 「两个筛单参数**都没给**」的那句（T5 / D15，**唯一出处**：`OrderQuerySerializer` 的字段
#: `error_messages` 引用它）。⚠️ 文案里的两个名字**都要在**：本线之后只提 `source_order_no`
#: 会让调用方以为没有第二条合法参数（旧文案与变更理由见设计文档 D15 / §7.4）。
MISSING_ORDER_QUERY_PARAM_MSG = "缺少参数：source_order_no 或 updated_since"

#: 「两个筛单参数**同时给**」的那句（T5 / D11 的二选一）。
#: ⚠️ 与上面那句**刻意不同**：拿到前者说明**一个都没传**，拿到这句说明**传了两个**
#: ——合成一句会让调用方不知道该删哪个。
BOTH_ORDER_QUERY_PARAMS_MSG = "参数 source_order_no 与 updated_since 只能选一个"

#: 时间入参**没带时区**时的那句（T5 / §7.1 的「ISO 8601 必须带时区」）。
#: ⚠️ 为什么必须自己判，⛔ 不能只靠 DRF 的 `DateTimeField`（**实测源码行为**）：
#: `USE_TZ=True` 时 `DateTimeField.enforce_timezone`（`rest_framework/fields.py`）对
#: **naive** 的解析结果走 `timezone.make_aware(value, 当前时区)` —— 也就是把
#: `2030-01-02T10:00:00` **静默当成 Asia/Shanghai** 收下。那正是本条要拒的：调用方
#: 以为自己按 UTC 递了游标、实际被我们按本地时区解释 ⇒ **静默错窗口**（漏单或多单），
#: 而症状是"第三方说少了几张单"，我们这边一片绿。
NAIVE_UPDATED_SINCE_MSG = "时间参数 updated_since 必须带时区"


def money(value):
    """金额出口：**有值 ⇒ 两位小数字符串**；`None` ⇒ `None`（见模块 docstring 第 2 条）。

    ⚠️ 判据用 `is not None`，⛔ 不用真值（E-0021 / D-0015）：`0.00` 与**负数**都是合法金额
    （负值表示冲抵），`if not value:` 会把 `0.00` 当成"没有值"而给出 `null`。
"""
    if value is None:
        return None
    return f"{Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP):.2f}"


def validate_callback_url_value(value: str) -> str:
    """`callback_url` 入参的**唯一**校验出口（保存时）：合法 ⇒ 归一化后的地址，否则 ⇒ `1000`。

    ⚠️ 它**只是一层转发**：判据（仅 https + 解析后 IP 必须公网 + 解析不出就拒）逐条长在
    `apps/openapi/outbound.py::validate_callback_url` 里，**投递前**用的是**同一个函数**
    （设计文档 §7.1 逐字要求两个时点各校验一次）。
    ⛔ **不许在这里再写一份 http / 内网判断**：两份判据必然漂移，而漂移的那一侧
    （投递时放松）就是一条**真的 SSRF 通道**。

    ⚠️ 空串合法（= 没配回调，§7.1）；归一化会去掉首尾空白（那正是投递时用的形态）。
    ⚠️ 抛 `ValidationError` ⇒ HTTP 200 + `code 1000`（本仓口径：业务失败不用 HTTP 状态码表达），
    文案就是 `CallbackUrlError` 那一句（两处**同一句**，⛔ 不各写一份）。
"""
    try:
        return outbound.validate_callback_url(value)
    except outbound.CallbackUrlError as exc:
        raise serializers.ValidationError(str(exc)) from exc


def _fk_name(obj) -> str | None:
    """可空外键的**名字**出口：没挂 ⇒ `None`（⛔ 不写空串：空串是一个真的名字位置）。

    ⚠️ 只用于 `project` / `server` / `category` 三个**可空**外键；`game` 非空、直接取。
"""
    return None if obj is None else obj.name


def _live_performances(order):
    """这张单的**未软删**绩效行（`order.performances` 的缓存，一次查询）。

    ⚠️ 软删行（`deleted_at` 非空）不进对外载荷：与全仓常规读面同一口径（软删 = 不在读面上，
    D-0144 / D-0147）。⇒ 被移进垃圾桶的接单人**不会**继续出现在第三方的成本里。
"""
    return [row for row in order.performances.all() if row.deleted_at is None]


def _cost_row(perf) -> dict:
    """一条绩效行 → 成本/提成格子（字段名逐字冻结）。"""
    return {
        "temp_amount": money(perf.temp_amount),
        "bonus_amount": money(perf.bonus_amount),
        "taker_price": money(perf.taker_price),
        "commission_rate": money(perf.commission_rate),
    }


def _costs(order) -> dict:
    """`costs` 分组：**两个键都是列表**（见模块 docstring 第 3 条）。

    ⚠️ 按 `emp_type` 分成两组、**组内按 `id` 升序**：顺序确定，第三方不必自己排
    （`Performance.Meta.ordering` 是 `-created_at, -id`，直接切片会得到"最新在前"，
    同一份数据两次调用顺序还会不一样——那是**不稳定契约**）。
    ⛔ 不 `[0]` 取第一条（那正是本节要堵的静默丢行）。
"""
    rows = sorted(_live_performances(order), key=lambda row: row.pk)
    return {
        "main": [_cost_row(row) for row in rows if row.emp_type == EmpType.MAIN],
        "sales": [_cost_row(row) for row in rows if row.emp_type == EmpType.SALES],
    }


def _taker_name(order) -> str | None:
    """接手人（打手）的**显示名** —— 顶层键 `taker_name` 的唯一取值出口（字段名逐字冻结）。

    - **接手人 = 那条 MAIN 绩效行上的归属人**：`/claim`（`claim.py` 的条件更新）与
    `/assign`（`performance.set_taker`）**都算「接手」**，写的都是同一列
    `Performance.user`（`apps/business/models.py` 不变量 ①）。
    - **判空只看人本身**（`user_id` 是否为 `None`），⛔ **不拿 `claimed_at` 推断**：
    「无主而有 `claimed_at` 的行」是**合法**的（不变量 ③——B-1 存量行），
    据此反推会把「远端平台接走」错报成「我方有人接手」。
    - **每单至多一名接手人**：部分唯一索引 `uniq_order_main_taker` 只约束「打单工资」行
    ⇒ MAIN 行**至多一条**，不存在「取哪一条」的歧义。
    - **软删行不算**（与 `_costs` 同口径）：`_live_performances` 已滤掉 `deleted_at`
    非空的行 ⇒ 被移进垃圾桶的接手人在这里也不再出现（显示为 `None`）。
    - 显示名走 `user_display_name`（`apps/accounts/display.py`，仓内唯一出处；与
    `image_row` 的 `uploaded_by_name` 同款用法——该函数**不查库**，预取由调用方的
    queryset 负责）。⚠️ 判空用 `user_id is None`：无主行**不触发**对 `user` 的
    惰性加载（列表页一页 20 条会变成 20 次多余查询）。
"""
    for row in _live_performances(order):
        if row.emp_type == EmpType.MAIN:
            return None if row.user_id is None else user_display_name(row.user)
    return None


def order_payload(order) -> dict:
    """把一张订单装配成对外载荷（字段白名单见模块 docstring 第 1 条）。

    ⚠️ 时间一律 ISO 8601（`isoformat()`，带时区偏移）；可空时间没值 ⇒ `None`。
    ⚠️ `status_text` 取 `Order.get_status_display()`：它由 Django 从**同一个**
    `OrderStatus.choices`（模型字段的 `choices`）取，⛔ **不是**第三份 `dict(OrderStatus.choices)`
    ——仓里已有两处那样的副本（`order_writes` 与 `jidanpu_member` 各一份），
    再造一份就等于同一件事有三处出处（spec §7.1 的 ⚠️ / E-0021 同族）。
    它认不出的码**原样返回**（Django 的 `_get_FIELD_display` 语义），与那两处的口径一致。
"""
    return {
        "order_no": order.order_no,
        "source_order_no": order.source_order_no,
        "status": order.status,
        "status_text": order.get_status_display(),
        "game": order.game.name,
        "project": _fk_name(order.project),
        "total_price": money(order.total_price),
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
        "source": order.source,
        # ── 订单侧金额，6 个，⛔ 不设禁区（业务方 ：金额本来就全归租户所有）──
        "amounts": {
            "total_price": money(order.total_price),
            "received_price": money(order.received_price),
            "platform_price": money(order.platform_price),
            "hall_price": money(order.hall_price),
            "refund_fee": money(order.refund_fee),
            "fee": money(order.fee),
        },
        # ── 成本与提成（打手 / 客服两类绩效行；**列表**，见模块 docstring 第 3 条）──
        "costs": _costs(order),
        # ── ⭐ 接手人显示名（D-0320，顶层新增；只增不改）──────────────
        # 值 = 接手人（打手）的**显示名**：`user_display_name`（昵称优先、空串回落
        # 登录账号，`apps/accounts/display.py`，仓内唯一出处）。
        # `null` = **无人接手**：无 MAIN 绩效行、或行上无归属人——判空**只看人本身**，
        # ⛔ 不拿 `claimed_at` 推断（「无主而有 `claimed_at`」的行合法：
        # `apps/business/models.py` 不变量 ③）。
        # 每单**至多一名**接手人，由部分唯一索引 `uniq_order_main_taker`
        # （`apps/business/models.py` 的 `Performance.Meta.constraints`）保证。
        # ⚠️ 它是员工身份口径（裁决）的**唯一显式例外**：只暴露接手人
        # 显示名，供第三方识别外部平台（代练通）接手单、跳过自动验收转人工核；
        # 其余员工身份字段仍不给（同一次的对外改准：`docs/OPEN-API.md` §3.7）。
        "taker_name": _taker_name(order),
        # ── 订单内容（**客户写的** ⇒ 给；「我方员工写的」`remark` ⛔ 不给）──
        "content": {
            "content": order.content,
            "demand": order.demand,
            "time_requirement": order.time_requirement,
        },
        # ── 订单业务描述（§17.9 的 A 组，业务方 第三轮裁定「给」）──
        "order": {
            "order_type": order.order_type,
            "server": _fk_name(order.server),
            "category": _fk_name(order.category),
            "project_text": order.project_text,
            "is_urgent": order.is_urgent,
            "tag": order.tag,
            "completed_at": order.completed_at.isoformat() if order.completed_at else None,
            "payment_method": order.payment_method,
            "payment_status": order.payment_status,
        },
        # ── 玩家账号信息（§17.9 的 B 组其一）──
        # ⚠️ `current_info_1` / `current_info_2` **原样透传、我方不解释语义**
        # （模型注释原文：「业务方要求保留但未定义语义」）⇒ 契约里也只承诺
        # 「是什么就传什么」，⛔ 不写"它代表 X"。
        "player": {
            "game_account": order.game_account,
            "game_sub_account": order.game_sub_account,
            "game_role_name": order.game_role_name,
            "current_info_1": order.current_info_1,
            "current_info_2": order.current_info_2,
        },
        # ── 客户联系方式 ──
        "customer": {
            "customer_phone": order.customer_phone,
            "customer_qq": order.customer_qq,
            "customer_ww": order.customer_ww,
        },
    }


# ═════════════════════════════════════════════════════════════════════════════
# 进度图一项的对外载荷（**唯一**的装配出口；T4 落、T10a 搬到本模块）
# ═════════════════════════════════════════════════════════════════════════════
def image_row(row) -> dict:
    """一张进度图（`OrderScreenshot`）的对外载荷：**恰好这七个键**。

    | 键 | 来源 |
    |---|---|
    | `img_type` | 行上的取值（`0` = 订单进度图；`1` = 群聊二维码）。通用应用只有 `0`；锁定的企微群码应用会给出 `1` |
    | `url` | **预签名直链**（`get_storage().presign_get()`；生产 COS 为整点对齐的 1–2 小时，本机 300 秒） |
    | `original_name` / `size_bytes` / `remark` | 行上的列（认领那一刻落的值） |
    | `uploaded_by_name` | **显示名**（`apps.accounts.display.user_display_name`，仓内唯一出处） |
    | `created_at` | ISO 8601（带时区偏移） |

    ## ⭐ T10a为什么它在本模块（**搬迁**，⛔ 不是新增第二份装配）

    这段原先是一个**内联 lambda**（住在 `order_extra_views.OrderImagesView.get` 里）。
    T10a 新增的**认领**端点（`POST .../images/`）也要回"该图片项"，而契约要求它与列表项
    **同一个装配出口**（设计文档 §13.10-3；`AGENTS.md` §5-E：⛔ 不许拼第二份）
    ⇒ 抽成本函数，列表端点与认领端点各调它一次。

    ⚠️ **为什么落在这里**（而不是留在 `order_extra_views.py`）：认领端点住
    `apps/openapi/api/order_image_views.py`，而 **`order_extra_views` 要 import 认领那个
    mixin**（`POST` 必须与 T4 的 `GET` **同住一个视图类**）⇒ 装配函数若留在
    `order_extra_views` 里就成了**循环 import**。本模块是"对外载荷装配"的既有出口
    （`order_payload` / `money` / `_iso` 都在这里），且它**不 import 任何 `api/*`**。

    ## 两条不变的契约事实（都别读错）

    - ⛔ **不给内部主键 `id`**、⛔ **不给账号主键**（上传人给显示名）；
    - `img_type` **照给**。通用应用只收 `0`；锁定应用（`kind=wecom_group_qr`）只收 `1`。
      认领与列表走这同一个出口，所以载荷里两种值都会出现。分叉在
      `order_image_views` 按 `kind` 决定。
"""
    storage = get_storage()
    return {
        "img_type": row.img_type,
        "url": storage.presign_get(row.object_key).url,
        "original_name": row.original_name,
        "size_bytes": row.size_bytes,
        "remark": row.remark,
        # ⚠️ **显示名**（共用口径），⛔ 不是 `row.uploaded_by_id`——理由见类 docstring。
        "uploaded_by_name": user_display_name(row.uploaded_by),
        "created_at": row.created_at.isoformat(),
    }


class UpdatedSinceField(serializers.DateTimeField):
    """ISO 8601 时间入参，**必须带时区**（T5 / §7.1）。

    ## 为什么是 DRF 字段的子类，而不是视图里一句 `datetime.fromisoformat`

    ① **链路上的纪律**：请求来源的值进类型化的列 / 查询之前一律过**序列化器**
    （`AGENTS.md` §5-E / E-0010）—— 视图里手搓一个 `fromisoformat` 就是在校验口之外
    又开一个入口（下层抛的英文 `ValueError` 会变成 500，而"参数不合法"不是 500）。
    ② **DRF 的 `DateTimeField` 必须被收窄**，理由与实测见 `NAIVE_UPDATED_SINCE_MSG` 的注释：
    它默认把无时区的值**静默**按当前时区补上（`USE_TZ=True` 时），那正是本条要拒的。
    ⚠️ 因此本类只加**一个**判断（原始值有没有带时区），其余（接受哪些 ISO 8601 写法、
    空值怎么走、输出成什么）一律沿用父类 —— ⛔ 不复刻一份解析逻辑。

    ## ⛔ 判断必须打在**原始值**上（实测踩到的形状）

    父类 `to_internal_value` 的返回值**永远**是 aware 的（无时区的那种已被它
    `make_aware` 补成 `Asia/Shanghai`）⇒ 在**返回值**上查 `is_aware` **恒真**、等于没查。
    实测（本机，`USE_TZ=True` / `TIME_ZONE=Asia/Shanghai`）：

    ```
    UpdatedSinceField().run_validation("2030-01-02T10:00:00")
    -> datetime(2026, 10, 9, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")) # aware=True
    ```

    所以先自己 `parse_datetime(原始字符串)` 判一次（它只认 ISO 8601、且**不补时区**，
    naive 的输入照样回 naive 的 datetime），再交给父类做真正的解析与校验
    ——⛔ 不在本类里也做一遍"格式对不对"（那是父类的事，两处判会漂）。

    ## 三条边界（都被用例钉着）

    - **`abc` / 空串** ⇒ `parse_datetime` 回 `None` ⇒ 交给父类 ⇒ 父类的 `invalid` 分支
    ⇒ `1000` + `NAIVE_UPDATED_SINCE_MSG`（本类**不**替它报错：父类才是格式的判据）；
    - **`2030-01-02T10:00:00`（无时区）** ⇒ `is_naive` 命中 ⇒ `1000` + 同一句
    ——⛔ 不是"按本地时区收下"（那会让游标窗口静默偏移，症状是第三方少拿到单）；
    - **`2030-01-02T10:00:00+08:00` / `…Z`** ⇒ 原样交给父类 ⇒ 收下并归一到 UTC。
"""

    def to_internal_value(self, value):
        # ⚠️ 只对**字符串**做这一次预判：非字符串（已经是 `datetime` 的调用方，例如仓内
        # 直接调序列化器的地方）没有"时区写在文本里"这件事，交给父类按对象判。
        if isinstance(value, str):
            parsed = _parse_datetime(value)
            # ⚠️ `None` ⇒ 格式就不对 ⇒ **不在这里报错**（那是父类的判据）。
            if parsed is not None and timezone.is_naive(parsed):
                self.fail("invalid")
        return super().to_internal_value(value)


class OrderQuerySerializer(serializers.Serializer):
    """`GET /api/open/v1/orders/` 的查询参数校验（**本端点唯一的入参**；T5 起两个筛单参数）。

    ## 两个筛单参数，**二选一**（T5 / D11）

    | 参数 | 形态 | 语义 |
    |---|---|---|
    | `source_order_no` | 可选（本线之前是**必填**） | 单号**精确匹配**（`items` + `total` 那一档） |
    | `updated_since` | 可选，ISO 8601、**必须带时区** | 按时间**增量**拉单（`count`/`results` + 分页那一档） |

    - **一个都没传** ⇒ `1000` + `MISSING_ORDER_QUERY_PARAM_MSG`（⛔ 不是静默回全部订单）；
    - **两个都传** ⇒ `1000` + `BOTH_ORDER_QUERY_PARAMS_MSG`（两种语义混在一起没有明确定义）；
    - ⛔ 两句文案**刻意不同**：前者说明"一个都没传"、后者说明"传了两个"，调用方据此知道该删哪个。

    ## ⛔ 字段级 `required` 为什么都**不能**写（它们替不掉上面那条 `validate()`）

    两个字段**各自**都不是必填，只有"至少一个"是必填 —— 那是**跨字段**约束，DRF 里只有
    `validate()` 表达得了。⚠️ 反过来也成立：把 `required=True` 挂在任一个字段上，
    另一条合法分支（只给另一个参数）会**当场被判成缺参数** —— 本线的两档正例**同时**红，
    所以这条约束的位置不是风格问题。
    ⚠️⭐ **追加（企微群码线，评审 Minor 收口）**：正因为这条约束**只**住在
    `validate()` 里，字段上原先那句 `error_messages={"required": …}` 是一条**恒不生效**的
    死配置（`required=False` ⇒ DRF 永远不抛 `required`）⇒ 已删除。原文与理由见
    `source_order_no` 字段声明上方那段注释（⛔ 不是"忘了写"）。

    ## ⚠️ **「没传」与「传了空串」必须分开**（spec §7.2；`AGENTS.md` §5-E 的 `is not None` 同族）

    - **传了空串**（`?source_order_no=`）⇒ `allow_blank=True` **放行** ⇒ 按**空串**查
    （命中 0 条 ⇒ 空列表 + `total=0`）。
    ⛔ 判据**不能**用真值（`if not value:`）：那会把空串当成"没传"而**静默回全部订单**
    ——一条越权面，而且症状是"查什么都返回一堆单"。
    ⚠️ `max_length=128` 与 `Order.source_order_no` 的列宽**同值**：超长值直接回 `1000`，
    不让它进 `filter()`（E-0010：请求来源的值进类型化查询前一律过序列化器）。
    ⚠️ `updated_since` 的**空串**走的是「格式非法」那一档（回 `1000`）——空串不是一个时间值，
    ⛔ 不把它当成"没传"（那会静默切成另一档语义，同族缺陷见 `AGENTS.md` §5-E）。
"""

    # ⛔⭐ **现行口径（企微群码线，评审 Minor 收口）**：本字段原先还挂着
    # `error_messages={"required": MISSING_ORDER_QUERY_PARAM_MSG}` —— 那句配置**从来不会生效**：
    # 字段是 `required=False`，而 DRF 只在 `self.required` 为真时才抛 `required` 那一档
    # （`rest_framework/fields.py::Field.validate_empty_values`）⇒ 「一个都没传」这一档
    # **只**由下面那个跨字段 `validate()` 接管（`MISSING_ORDER_QUERY_PARAM_MSG` 就是给它用的）。
    # ⇒ 按 `AGENTS.md` §5-E「防御性写法不要写成承重逻辑」把这条**死配置**删掉；⚠️ 常量本身与
    # `validate()` 的两条分支**一个字都没动**。
    source_order_no = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
    )
    updated_since = UpdatedSinceField(
        required=False,
        # ⚠️ 覆盖 DRF 那句英文默认文案：本仓对外文案带**字段名**（`AGENTS.md` §5-G 的判别口诀）。
        error_messages={"invalid": NAIVE_UPDATED_SINCE_MSG},
    )

    def validate(self, attrs):
        """`source_order_no` 与 `updated_since` **二选一**（D11）。

    ⚠️ 判据用 `is not None`（⛔ 不用真值，E-0021）：`source_order_no=""` 是**一个真的查询值**
    ∈ `attrs` ⇒ 只给空串这一个参数时走的是 `items`/`total` 那一档（既有行为），
    ⛔ 不会被这里判成"没传"（后者会让那条既有用例红）。
    ⚠️ 错误经 `apps.core.exceptions._first_message` 压成**一句** `msg`
    ⇒ 两条分支各给一句**互不相同**的话，判据才打得在"是哪一档"上（⛔ 不是只看 `code=1000`）。
"""
        given = [
            name
            for name in ("source_order_no", "updated_since")
            if attrs.get(name) is not None
        ]
        if not given:
            raise serializers.ValidationError(MISSING_ORDER_QUERY_PARAM_MSG)
        if len(given) > 1:
            raise serializers.ValidationError(BOTH_ORDER_QUERY_PARAMS_MSG)
        return attrs


# ═════════════════════════════════════════════════════════════════════════════
# 应用管理端点（**内部**接口，spec §7.3；T5）的入参校验与载荷装配
# ═════════════════════════════════════════════════════════════════════════════
# 与上半部分（对外查单）的差别只有一条，但要看清：这里的载荷是给**租户自己**看的
# 管理界面用的，⛔ 不是对外契约 ⇒ 字段可以随页面演进，而 `order_payload` 那条不许。

#: 应用名的列宽（与 `ApiApp.name` 同值）：超长直接回 `1000`，⛔ 不让它进 `filter()` / `INSERT`。
APP_NAME_MAX_LENGTH = 50

#: IP 白名单的**入库上限**。⚠️ `ApiApp.ip_allowlist` 是 `TextField`（没有列宽），
#: 而请求来源的值进列之前要有一道界（E-0010 家族：没有界 ⇒ 一个超大 body 被原样写库）。
#: ⚠️ 这是我方定的上限、**不是对外契约**；⛔ 这里**不校验格式**——白名单条目的解析
#: （每行一个、行内按逗号/空白分）只有一份实现，在 `apps.openapi.authentication`
#: （T3），在这里再写一遍就是第二份判据（E-0021）。
IP_ALLOWLIST_MAX_LENGTH = 2000

#: 回调地址的入库上限（⭐ T8）。**与模型列宽 `ApiApp.callback_url`（200）同值**，
#: 理由逐字写在那一列的注释里：页面输入框的限长与库列宽同源，
#: ⛔ 不会出现"页面让填、库里静默截断"。真需要更长要**改列宽 + 配迁移**，那是显式动作。
CALLBACK_URL_MAX_LENGTH = 200

#: `AppKey` 的字节数：`secrets.token_hex(8)` ⇒ **16 位小写 hex**（§5.3 的定值）。
APP_KEY_BYTES = 8


def generate_app_key() -> str:
    """`AppKey` = `secrets.token_hex(8)` ⇒ 16 位小写 hex（§5.3 的定值，⛔ 不另选原语）。

    ⚠️ **它不是机密**：明文入库 + 唯一索引（§5.4）⇒ 可以出现在日志 / 留痕 / 排障会话里；
    唯一的机密是 `app_secret`。所以下面的创建留痕把 `app_key` 写进 `to_value` 是合规的。
"""
    return secrets.token_hex(APP_KEY_BYTES)


def generate_app_secret() -> str:
    """`AppSecret` = **复用 peer 通道的同一个原语**（`peer_sync.new_token()`，§5.3）。

    ⚠️ spec §5.3 明写「复用现成原语，⛔ 不新造一套随机源」⇒ 这里**不自己**调 `secrets`，
    改随机源会让两处的强度与字符集各自演进（`new_token()` = `token_urlsafe(32)`，43 字符，
    列宽 64 够）。
"""
    return new_token()


def _iso(value):
    """可空时间出口：没值 ⇒ `None`（⛔ 不写空串：空串是一个真的时间位置）。"""
    return None if value is None else value.isoformat()


def app_row_payload(app) -> dict:
    """一行应用的载荷（列表每一行 / 详情 / `PATCH` / 创建 / 轮换）。

    ⛔ **现行口径（业务方指令）：密钥明文、列表与详情都回显**：
    原口径曾是「密钥只在**创建 / 轮换**那一次响应里出现、列表与详情永不回显」，页面提示
    「只显示这一次」。业务方 的原话是「**密钥改成明文展示 可以随时获取
    不要加那个只能看一次的限制**」⇒ 本函数**现在**回明文密钥，且**合并掉**了原先那份
    「行载荷 + 一个键」的第二个装配函数（`app_secret_payload`，同形两份装配是 E-0021 同族）。
    ⚠️ 代价逐条登记在 spec `docs/specs/2026-10-07-open-platform-api-design.md` 的**§34 改准块**
    （谁能读到密钥 / 密钥会落到响应体里的哪些地方 / 「泄露只能靠人工轮换」更依赖人的纪律）。
    ⛔ **没有放松的那条**：密钥**仍然不得**进任何**留痕 / 操作日志 / 错误消息**
    （判据 = `tests/test_openapi_app_api.py::AppSecretNeverLoggedTests`）。

    ## ⛔ 五组键已于 T10 删除（设计文档 §13.1）

    本函数原先还回五组与**授权范围**有关的键：两个**概要**（权限项数与游戏范围，
    含「已配置 N 项」/「N 款」/「全部游戏」三句中文）、另外两个**当前勾选态回读**清单
    （节点 code 与游戏主键），以及一个"全部游戏"模式的布尔。
    它们服务的是**两个整份替换的配置端点**（权限设置 / 游戏范围）—— 那两个端点与模型上那三列
    在 T10 一起删除（§4-V2：一个应用凭据 = 本租户全部业务数据）⇒ 这五组键**没有消费者**了。
    ⚠️ **删键的依据（⛔ 不是"看着没用"）**：那两个 `PUT` 是它们**唯一**的写入侧，
    而读侧的页面（T9b）已经同一次删掉了那两块 UI（`093e53aa`）。
    ⚠️ 代价（如实登记）：**列表页从此看不出一个应用"能干什么"** —— 它今天能干什么，
    答案恒为「该租户全部业务数据」（§4-V2），页面用一句 `FULL_TENANT_NOTICE` 说破。
    旧键的逐条语义（"空集 vs 没配"那套）见 git 历史与 §13.1 / §35 改准块。
"""
    return {
        "id": app.pk,
        "name": app.name,
        "app_key": app.app_key,
        # ⭐ 明文密钥（业务方改口径；见本函数 docstring）：
        # 列表 / 详情 / PATCH / 创建 / 轮换**都是**这一个装配出口 ⇒
        # 「随时可以获取」。⛔ 仍然绝不进留痕 / 日志 / 错误消息。
        "app_secret": app.app_secret,
        "state": app.state,
        "state_text": app.get_state_display(),
        "ip_allowlist": app.ip_allowlist,
        # ⭐ T8（设计文档 §7.1 / §8 的后端面）：回调配置的两列。页面（T9）要用它们渲染
        # 「回调地址」输入框与「回调开关」，⛔ 缺了它们页面只能显示空 —— 而"显示空"与
        # "服务端没配"是两件事（那两个键后来在 T10 随 v1 面删掉了）。
        # ⚠️ `callback_state` 给的是**机器值**（`enabled` / `disabled`）；页面要显示中文自己
        # 查一张两行的表即可，⛔ 这里不再加一个 `callback_state_text`（与 `state_text` 不同：
        # 那个是既有关键字，不是本任务新开的形状）。
        "callback_url": app.callback_url,
        "callback_state": app.callback_state,
        "last_used_at": _iso(app.last_used_at),
        "secret_rotated_at": _iso(app.secret_rotated_at),
        "created_at": app.created_at.isoformat(),
    }


# ⛔ **`app_secret_payload` 已于 删除**（业务方指令「密钥改成明文展示 可以随时获取
# 不要加那个只能看一次的限制」；代价逐条登记在 spec 的 **§34 改准块**）。
# 它原来是「`app_row_payload` + 一个 `app_secret`」的第二个装配出口，原 docstring 原文：
# 「**创建 / 轮换那两次**的响应载荷：在行载荷之上**多一个** `app_secret`（明文）。
# ⚠️ 这是 `app_secret` 会出现在响应里的**仅有两个出口**（§5.3）；页面必须显著提示
# 「新密钥只显示这一次」。⛔ 别把它并进 `app_row_payload`——那会让**每次列表**都回显密钥，
# 等于把"永不回显"这条纪律作废（而且列表是高频读端点）。」
# 并掉的**理由**（不是"顺手"）：改口径之后它与 `app_row_payload` 的返回**逐键相同**
# ⇒ 留着就是**同形两份装配**（E-0021 同族：一处改了、另一处不改则静默漂）。
# 调用点全部改走 `app_row_payload`；⛔ 别复活这个空壳（要变体就给**真的不同**的键集合）。


# ═════════════════════════════════════════════════════════════════════════════
# 推送台账（`OpenApiDelivery`）—— 两个内部管理端点（T9a）的载荷与筛选
# ═════════════════════════════════════════════════════════════════════════════

#: `reason` 在**页面上**的展示上限（字符）。⚠️ 这是**页面口径**、不是列宽：
#: 列那一侧的上限是 `apps/openapi/dispatch.py::MAX_REASON_CHARS`（2000，写入方截断）。
#: 两个数字是**两件事**，⛔ 别合并成一个常量（合并会让"页面看到多长"随写入侧口径漂）。
DELIVERY_REASON_DISPLAY_CHARS = 200

#: 截断标记（**唯一**一处；页面据此分辨"原文到此为止"与"被截断了"）。
DELIVERY_REASON_ELLIPSIS = "…"


def delivery_reason_text(reason: str) -> str:
    """`reason` 的**展示**截断（页面口径的**唯一**出处，⛔ 别在视图 / 端点里再截一次）。

    为什么截：`reason` 是**诊断文本**（对方回执、超时、SSRF 拒收那句……），页面那一栏是
    一行小字。不截时一行台账可以把表格撑成几屏（写入侧允许 2000 字符），而**整段诊断
    排版到页面上就不再是"能读的诊断"**。库里的原文**一个字都不动**（要全文去查库 / 后台）。

    为什么要**加标记**：只截不标会让截断后的文本看起来像一条完整的诊断
    （`AGENTS.md` §5-C 的同族：静默少给比不写更糟）。

    ⚠️ **判据取 `len(...) >` 而不是"长度够不够好看"**：边界值（恰好等于上限）**不截**、
    也不加标记 ⇒ 那条边界是可断言的（见 `tests/test_openapi_delivery_admin.py`）。
"""
    text = reason or ""
    if len(text) <= DELIVERY_REASON_DISPLAY_CHARS:
        return text
    return text[:DELIVERY_REASON_DISPLAY_CHARS] + DELIVERY_REASON_ELLIPSIS


def delivery_row_payload(delivery) -> dict:
    """推送台账**一行**的载荷（列表每一行 / 重投响应）——本函数的键集就是那份契约。

    ## 键集（**恰好**这 13 个，⛔ 不多不少）

    | 键 | 是什么 |
    |---|---|
    | `id` | 台账行主键（重投按钮要拿它拼 URL） |
    | `event` / `event_text` | 事件**机器值**（`order.status_changed`）/ 中文名 |
    | `order_no` / `source_order_no` | 我方单号 / 对端的来源单号（可为空串） |
    | `status` / `status_text` | 状态**机器值**（`pending`/`done`/`failed`）/ 中文名 |
    | `attempts` | 已尝试投递次数 |
    | `reason` | **上一次失败的诊断文本**（过 `delivery_reason_text`，只给页面看） |
    | `response_status` | 对方最近一次的 HTTP 回执码（`null` = 还没拿到回执） |
    | `next_attempt_at` | 下一次可投递时刻（`null` = 立刻可投 / 已终态） |
    | `created_at` / `processed_at` | 入队时刻 / 上一次落终态的时刻（`null` = 还没落过） |

    ⚠️ `event`/`status` 给机器值、`*_text` 给中文，**两个都要**：页面按机器值决定按钮可不可点
    与筛选（⛔ 不许拿中文当判据 —— 改一句文案就会静默失效），显示用中文（页面不做映射表）。

    ## ⛔ 刻意**不在**载荷里的两样（各有理由，⛔ 别"顺手带上"）

    1. **`payload`**（待投递的报文原文）：里面装的是**订单业务数据**（状态、金额、客户信息…），
    而这一页要回答的只是"推没推成"。把它带上等于把订单数据**复制一份**到管理页的响应里
    —— 页面的可见面就是拿到它的人的面（设计文档 §8 的"最近推送记录"没有这一栏）。
    2. **任何凭据类字段**（`app_secret` / `app_key` / 签名）：本行是**台账**、与凭据无关；
    密钥的展示出口今天只有一个（`app_row_payload`），⛔ 本函数不开第二个
    （判据 = `tests/test_openapi_delivery_admin.py` 里逐条递归扫键 + 全响应文本扫密钥）。

    ## 时间格式

    四个时间点一律走 `_iso()`（`None` ⇒ `None`，⛔ 不写空串：空串是一个真的时间位置）。
"""
    return {
        "id": delivery.pk,
        "event": delivery.event,
        # 中文名取枚举自身（⛔ 不抄一份 `dict(Event.choices)`）
        "event_text": delivery.get_event_display(),
        "order_no": delivery.order_no,
        "source_order_no": delivery.source_order_no,
        "status": delivery.status,
        "status_text": delivery.get_status_display(),
        "attempts": delivery.attempts,
        "reason": delivery_reason_text(delivery.reason),
        "response_status": delivery.response_status,
        "next_attempt_at": _iso(delivery.next_attempt_at),
        "created_at": _iso(delivery.created_at),
        "processed_at": _iso(delivery.processed_at),
    }


class DeliveryStatusFilterSerializer(serializers.Serializer):
    """推送台账列表的 `status` 筛选入参（**唯一**的校验口）。

    ⚠️ **请求来源的值一律过序列化器**（`AGENTS.md` §5-E / E-0010）：`?status=…` 直接进
    `filter(status=…)` 今天不会炸，但那是"碰巧"（列的比较行为），不是判据。这里把它钉成
    **枚举**：非法值 ⇒ `1000` + 一句带字段名的人话（⛔ 不是静默忽略 —— 静默忽略会让页面
    以为"筛了"，而列表里其实混着别的状态）。

    ⚠️ `required=False`：**不传 = 不筛**。
    ⚠️⭐ **空串（`?status=`）也等于"不筛"**（**实测**，⛔ 不是本端点的选择）：DRF 的
    `Field.get_value` 对 HTML 形状的输入（`QueryDict` 就是）有一支
    `ret == '' and not self.required ⇒ empty`（那个 `empty` 是"缺席"哨兵）⇒ **全仓**每一个
    非必填字段上的空串都是这个语义。⛔ 本端点**不特判**它 —— 特判会让"空串"这一种输入
    在这里与其余每一个可选字段上分叉。判据钉在
    `tests/test_openapi_delivery_admin.py::test_an_empty_status_is_treated_as_not_provided`。
"""

    status = serializers.ChoiceField(
        choices=OpenApiDelivery.Status.choices,
        required=False,
        error_messages={"invalid_choice": "状态筛选值不合法：{input}"},
    )


class AppCreateSerializer(serializers.Serializer):
    """`POST /api/open/apps/`：入参**只要应用名**（§7.3）；⭐ T8 起可**顺带**给回调配置。

    ⚠️ `app_key` / `app_secret` **不是入参**：由服务端按 §5.3 生成（⛔ 不让调用方指定，
    否则"密钥可预测"会变成一条静默的配置项）。多余键被 DRF 忽略（与全仓口径一致）。

    ## ⭐ T8 追加的两个可选字段（设计文档 §7.1 / §8）

    「先建应用、再到列表里逐个改回调地址」是两步；一次填完更省事，且与 `PATCH` 那条路
    **共用同一套校验与同一份文案**（`validate_callback_url_value`）。
    ⚠️ 两个字段都**可选**（缺席 = 用模型默认值：地址空、开关 `enabled`）
    ——⛔ 判据用 `"字段" in validated_data`，**不是**真值（E-0021）：
    `callback_url=""` 是**合法值**（= 没配回调），拿真值判会把它当成"没传"。
"""

    name = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=APP_NAME_MAX_LENGTH,
        error_messages={"required": "缺少参数：name", "blank": "应用名不能为空"},
    )
    # ⚠️ `max_length` 与模型列宽（200）同源：页面输入框的限长与库列宽同一个数，
    # ⛔ 不会出现"页面让填、库里静默截断"。
    callback_url = serializers.CharField(
        required=False, allow_blank=True, max_length=CALLBACK_URL_MAX_LENGTH,
        validators=[validate_callback_url_value],
    )
    callback_state = serializers.ChoiceField(choices=CallbackState.choices, required=False)


class AppUpdateSerializer(serializers.Serializer):
    """`PATCH /api/open/apps/<pk>/`：**改名 / 停用启用 / 改 IP 白名单 / 改回调配置**（§7.3 / §7.1）。

    ⚠️ 字段**全部 `required=False`**：缺席 = **保持库里的值**（PATCH 语义，
    与角色那条 `PATCH` 的全仓口径一致）。
    ⛔ 调用方的判据必须是 `"state" in validated_data`（**不是**真值判断）：
    空串 `ip_allowlist` 与空串 `callback_url` 都是**合法值**（空 = 不限制 / 没配回调，§5.1 / §7.1）
    ⇒ 用真值判断会把「清空白名单 / 清掉回调地址」当成"没传"而**静默不生效**。
"""

    name = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=APP_NAME_MAX_LENGTH,
        error_messages={"blank": "应用名不能为空"},
    )
    state = serializers.ChoiceField(choices=ApiAppState.choices, required=False)
    ip_allowlist = serializers.CharField(
        required=False, allow_blank=True, max_length=IP_ALLOWLIST_MAX_LENGTH
    )
    # ⭐ T8：回调地址（**仅 https + SSRF 校验**，与投递前**同一个**校验函数）与回调开关。
    callback_url = serializers.CharField(
        required=False, allow_blank=True, max_length=CALLBACK_URL_MAX_LENGTH,
        validators=[validate_callback_url_value],
    )
    callback_state = serializers.ChoiceField(choices=CallbackState.choices, required=False)
