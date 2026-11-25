"""把旧 `business.AuditLog` 的历史行搬进 `audit.OperationLog`（spec §9）。

## 为什么是「按写入者」而不是「按旧 action」映射

旧表用**同一个** `PRICE_CHANGE` 值记了两件不一样的事：
`log_price_change` 记订单**总价**，`log_performance_adjust` 记**绩效字段**。
行里唯一的信号是 `reason`，而两个写入者的**语法**不同：

- `log_performance_adjust` 只写得出 `label`（用户没填原因）或
  `f"{label}｜{reason}"`（`apps/business/audit.py:47`，分隔符是全角 `｜`）；
- `log_price_change` 写的是调用方**自由文本**（最长 255 字）。

所以判别必须**贴着写入者的语法**：`reason == label` 或 `reason.startswith(label + "｜")`。
⚠️ **不能只匹配前缀 `label`**：总价改动的 reason 完全可能是
「扣点值算错了要改总价」——只匹配短前缀会把一笔**改总价**错记成绩效扣点的前后值
（方向是「算错钱/写错人」的那一类，且测试全绿也发现不了）。
完整标签 + 紧随的 `｜` 对真实行零漏判（标签在串首，`text[:255]` 截不到它；
没原因时整串恰好等于标签）。

## 为什么这里**不**导入 `apps.audit` 的渲染器（`render_summary` / `ACTION_TEMPLATES`）

迁移是**冻结的历史记录**，不是当前代码的视图（Django 的迁移纪律）。若这里
`from apps.audit.log import render_summary`，那么一年后改一句模板文案，
在一台**全新**数据库上重跑本迁移就会给出与今天**不同**的行——历史被静默改写。
所以模板、字段标签、状态标签在这里**内联**一份；将来两者不一致是「代码演进了」，
不是「迁移写错了」。改本文件的内联副本需要一次新的数据修复迁移。
（`tests/test_operation_log_migration.py::InlineCopiesDriftTests` 是这份内联副本
与 `apps/audit/actions.py` / `OrderStatus` 的常驻对拍红线。）
⚠️ **例外（2026-10-03，审计日志线 spec §3.4）**：那次只是把 `performance.review` 的
占位符 `{actor}` → `{actor_name}` **改名**、喂进去的值仍是旧表的 `actor`
⇒ **渲染结果逐字不变**，没有数据要修，故不需要数据修复迁移（见 `_render` 的改名注）。

## R15：旧状态码翻译成中文标签

旧 `log_status_change` 收到的是 `OrderStatus` 的**英文 value**（`IN_PROGRESS`…），
而 spec §5.1 的 `order.transition` 文案要求前后值是**中文**（「与对标一致」）。
不翻译的话同一个 action 会因为行的新旧而中英混排。这里内联一份冻结映射
（逐字抄 `apps/business/models.py::OrderStatus` 的 choices，**不 import app 代码**）。
**认不出的状态码原样保留**（不猜、不抛）。

⚠️ `ReviewStatus` 的三个取值（`待审核` / `已通过` / `已驳回`）**本身就是中文**，
所以 `REVIEW` 行不需要翻译——这是查过 `models.py` 的事实，不是漏做。

## 登记的两条能力边界（旧表没有对应的列）

- 迁移出来的 `performance.adjust` 行 **`target_name` 为空**（旧表没有「绩效用户」
  这一列；`actor` 是**操作人**，拿它冒充目标会把操作人的名字写到目标位上）。
  文案因此是「修改了的X（原…，新…）」——缺口留着，不猜。
  ⚠️ **M3a 追加**：`performance.settle` / `performance.adjust_manual` 的旧行同样只有
  `performance_id`（不是一个可放进固定 `target_kind` 词表的东西：spec §4.2 没有
  `performance` 这个 kind），本迁移**不**回查 `Performance` 去反解归属人——
  那会让历史文案随业务数据（换人 / 改名）漂移，且违反「渲染输入只来自行内列」。
  这两类行也照同一条边界留 `target_name` 空（`summary` 仍**非空**、`log_type` 正确）。
- 迁移出来的所有行 **`game_name` 为空**（旧表没有游戏列）。**不**回查 `Order`
  去补：日志显示会随业务数据（改游戏名/换绑）漂移，而 spec §5.9 明确要求渲染输入
  只能来自行内列。

## 其它口径

- `created_at` **保留原值**（`OperationLog.created_at` 是 `default=timezone.now`，
  可显式赋值；spec §4.2 评审 B3）。
- `actor_id` **留空**：旧表只有名字，改名/重名都可能，反查会把留痕挂到错的人头上。
- `reason` 同行保留——含 `log_performance_adjust` 的标签前缀。那是**旧数据的原样**，
  不是新写入该学的样子：新写入走 `apps/audit/log.py`，`reason` 只放纯值（spec R10）。
- 认不出的旧 `action`（列是 `varchar(20)` + `choices`，DB 层没有 CHECK）**不猜**：
  行照搬、`log_type` 落 `70 其它`、`action` 保持旧 code、**`summary` 恒为空**。
  旧表保留一个版本，不丢数据。
- 流式 + 分批：`iterator()` 不把旧表整表拉进内存，每 `_BATCH_SIZE` 行落一次
  `bulk_create(batch_size=...)`（PostgreSQL 不会自己分批）。生产库这张表可能很大。

## ⭐ M3a（2026-09-22）：`actor` 超长的行**留给 `0006`**（修 M2 登记的残差）

本迁移在 `0005` **之前**执行，此时 `OperationLog.actor` 只有 **64** 宽，而旧表
`AuditLog.actor` 是 **150**（`main` 的 P4 写入点取的就是 `request.user.username`，
65–150 字可达）。原实现把 `actor` **原样**搬进来 ⇒ 在带这种旧行的库上**第一次**
`migrate` 会以 `DataError: value too long for type character varying(64)` 中断，
而 `0005` 排在后面、救不了它（M2 实测，spec §16.2 第 11 条）。

**处置 = 「按列宽拆分」**（两条互补判据，都不丢字）：

- 本迁移只搬 `len(actor) <= BACKFILL_ACTOR_WIDTH` 的行（放得下，不截断）；
- `0006_backfill_long_actors`（排在 `0005` **之后**）只搬 `len(actor) > BACKFILL_ACTOR_WIDTH`
  的行——那时列已加宽到 150，**原样**写入、一个字不丢。

⚠️ **为什么不是「截断」也不是「整条回填挪到 `0005` 之后」**：
① 截断会让历史行的操作人变成别人的名字前缀（静默丢数据，正是本线要修的那类缺陷）；
② 整条挪到 `0005` 之后时，本迁移在**已经应用过它**的库上不会重跑，而 `0006` 会再搬一遍
⇒ **本地开发库里已有的回填行会被复制一份**。按列宽拆分的两条判据**互斥且覆盖全表**
（每条旧行恰好被其中一条搬走一次），于是「全新库」与「已应用过 0004/0005 的库」
两条路都恰好搬一次，**幂等**。

## 有意不可逆

回填出来的行与迁移之后应用自己写的行在表里长得一样，没有能自我证明正确的反向。
写一个只会删真实数据的 `reverse_code` 比明确不可逆更糟——`migrate` 会**拒绝**反向，
而不是静默删数据。
"""
from django.db import migrations

# 旧 action 的三个取值（`apps/business/models.py::AuditLog.Action`）。
# ⭐ M3a 追加 P4 的三个（`main` 的 `log_settle` / `log_refund` / `log_manual_adjustment`）。
_STATUS_CHANGE = "STATUS_CHANGE"
_PRICE_CHANGE = "PRICE_CHANGE"
_REVIEW = "REVIEW"
_SETTLE = "SETTLE"
_REFUND = "REFUND"
_ADJUST = "ADJUST"

#: 本迁移写 `actor` 时那一列的**冻结宽度**（`0001_initial` 建列时是 64，
#: `0005` 才加宽到 150）。⚠️ `0006_backfill_long_actors` 用同一个常量做**互补判据**
#: （`> BACKFILL_ACTOR_WIDTH`），所以它只在这里定义一次——两处必须逐个对齐，
#: 否则「全新库」会漏行或复制行（`tests/test_operation_log_migration.py` 的
#: `test_actor_longer_than_the_column_survives_the_whole_chain` 从两端钉住它）。
BACKFILL_ACTOR_WIDTH = 64

# ⚠️ 内联副本，**不要**改成 import（见模块 docstring）。
# 逐字取自 `apps/audit/actions.py`：七个模板 + 三个字段中文标签。
_TEMPLATES = {
    "order.transition": "将订单 {order_no} 的状态从{from_value}改为{to_value}（原因：{reason}）",
    "order.change_taker_price": (
        "修改了订单接单价格（原订单价格：{from_value}元，修改后价格：{to_value}元）"
    ),
    "order.refund": "将订单 {order_no} 的累计退款额从{from_value}改为{to_value}，备注：{reason}",
    "performance.adjust": "修改了{target_name}的{field_label}（原{from_value}，新{to_value}）",
    "performance.adjust_manual": (
        "新增了{target_name}的绩效调整记录（变更前：{from_value}，变更后：{to_value}），"
        "备注：{reason}"
    ),
    "performance.review": "{actor_name}审核了订单 {order_no} 的绩效：{to_value}，备注：{reason}",
    "performance.settle": "修改了{target_name}的绩效清算状态为：{to_value}(仅更改结算状态)",
}
_FIELD_LABELS = {
    "taker_price": "接单价覆盖值",
    "deduct_mode": "扣点模式",
    "deduct_value": "扣点值",
}

# 内联副本：`OrderStatus` 的 value → 中文 label（逐字抄 `apps/business/models.py`；R15）。
# ⚠️ 这张表**刻意保留** `WAITING_GRAB` = 「待抢单」这个当前 label——模型 docstring 登记
# 了它与对标原文「待上号」的已知不一致（前端订单页另有覆盖表）。迁移抄的是**模型今天
# 的事实**；等模型改 label 时，`InlineCopiesDriftTests` 会红，逼一次显式决定
# （历史行要不要跟着再修一次）。
_ORDER_STATUS_LABELS = {
    "UNASSIGNED": "未分配",
    "WAITING_GRAB": "待抢单",
    "IN_PROGRESS": "代练中",
    "PENDING_REVIEW": "待验收",
    "COMPLETED": "已完成",
    "ISSUE": "问题单",
    "PAUSED": "已暂停",
    "REFUNDED": "已退单",
}

# 调用方传给 `log_performance_adjust` 的 `label`（实测取值见
# `apps/business/api/performance_views.py:218-225`）→ `Performance` 的真实列名。
# ⚠️ 按**完整标签 + 紧随的 `｜`** 匹配（见模块 docstring）：`扣点值` 不能当成
# 「以 `扣点值` 开头」的自由文本理由。`扣点模式` / `扣点值` 互不为前缀，顺序无关。
_PERFORMANCE_LABELS = (
    ("接单价（绩效覆盖）", "taker_price"),
    ("扣点模式", "deduct_mode"),
    ("扣点值", "deduct_value"),
)
_SEPARATOR = "｜"  # 全角竖线：`apps/business/audit.py:47` 的 `f"{label}｜{reason}"`

_OTHER = 70  # LogType.OTHER「其它」——认不出的旧 action 的归类（同样内联）。

# 新 action 的日志分类（spec §9 的映射表）。
_ORDER_LOG_TYPE = 10
_PERFORMANCE_LOG_TYPE = 20

# 生产库这张表可能很大 ⇒ 流式读 + 分批写（PostgreSQL 不会自己分批）。
_BATCH_SIZE = 1000


def _performance_field(reason):
    """返回 `reason` 命中**写入者语法**的绩效列名；没命中返回 `None`。"""
    for label, field in _PERFORMANCE_LABELS:
        if reason == label or reason.startswith(label + _SEPARATOR):
            return field
    return None


def _classify(action, reason):
    """把一条旧行分成 `(新 action, 新 log_type, field_name)`；认不出 ⇒ `None`。"""
    if action == _STATUS_CHANGE:
        return "order.transition", _ORDER_LOG_TYPE, "status"
    if action == _PRICE_CHANGE:
        field = _performance_field(reason)
        if field is not None:
            return "performance.adjust", _PERFORMANCE_LOG_TYPE, field
        return "order.change_taker_price", _ORDER_LOG_TYPE, "total_price"
    if action == _REVIEW:
        return "performance.review", _PERFORMANCE_LOG_TYPE, "review_status"
    # ⭐ M3a：P4 的三个旧 action 现在有**真正的**新动作可归（原先落 `70 其它` + 空 summary）。
    if action == _SETTLE:
        # 绩效清算状态（旧行 from/to = 「未结账」/「已结账」）。
        return "performance.settle", _PERFORMANCE_LOG_TYPE, "settle_status"
    if action == _REFUND:
        # **订单级**事件（旧行 from/to = `Order.refund_fee` 的旧/新累计额）。
        return "order.refund", _ORDER_LOG_TYPE, ""
    if action == _ADJUST:
        # 财务新增一条 `PerformanceAdjustment`（旧行 from/to = 调整前/调整后的值）。
        return "performance.adjust_manual", _PERFORMANCE_LOG_TYPE, ""
    # 旧列是 varchar(20) + choices（DB 层没有 CHECK）⇒ 脏值会走到这里。
    return None


def _translate_status(value):
    """`OrderStatus` 的英文 value → 中文 label；**认不出/空值原样返回**（不猜、不抛）。"""
    if not value:
        return value or ""
    return _ORDER_STATUS_LABELS.get(value, value)


def _render(action, *, actor, target_name, order_no, field_name, from_value, to_value, reason):
    """内联渲染（spec §9「重放渲染」）；没有模板 ⇒ 空串（不猜）。

    ⚠️ **改名注（2026-10-03，审计日志线 spec §3.4）**：`performance.review` 的占位符
    随 `apps/audit/actions.py` 一起从 `{actor}` 改成 `{actor_name}`（那一列的语义
    在新代码里就叫「显示名」）。这里喂进去的**仍然是旧表的 `actor`**
    （= 当时的**登录账号**，历史行没有昵称可查），所以**渲染结果逐字不变**
    ⇒ 不需要新的数据修复迁移、已应用过本迁移的库也不受影响
    （`tests/test_operation_log_migration.py` 的 `InlineCopiesDriftTests` 与
    `test_legacy_rows_are_backfilled_by_writer` 的字面期望从两头钉住这一点）。
    """
    template = _TEMPLATES.get(action)
    if template is None:
        return ""
    # ⭐ D-0337（2026-10-30）同款剔除：`order.transition` 模板带可选的「（原因：{reason}）」段，
    #    旧行 `reason` 为空 ⇒ 与 `apps/audit/log.py::render_summary` 同一条「format 前
    #    整段剔除」的渲染期防御（精确字面段，非泛化正则）⇒ 回填渲染结果与旧文案
    #    **逐字相同**（既有 summary 口径零漂移；dict 本身仍与注册表逐字相等，由
    #    `InlineCopiesDriftTests` 两头钉住）。
    if not (reason or ""):
        template = template.replace("（原因：{reason}）", "")
    return template.format(
        # 历史行只有登录账号这一列可用 ⇒ 它同时充当 `{actor_name}` 的取值（输出与改名前一模一样）。
        actor_name=actor or "",
        target_name=target_name or "",
        order_no=order_no or "",
        field_label=_FIELD_LABELS.get(field_name, field_name),
        from_value=from_value or "",
        to_value=to_value or "",
        reason=reason or "",
    )


def _build(old, OperationLog, summary_width):
    """把一条旧行变成一条（未落库的）`OperationLog`。"""
    reason = old.reason or ""
    from_value, to_value = old.from_value, old.to_value
    classified = _classify(old.action, reason)

    if classified is None:
        # 认不出的旧 action：行照搬、动作保持旧 code、文案**硬置空**。
        # ⚠️ **不经过** `_render`：否则一个恰好等于某个新 code 的脏值
        # （如 `order.transition`）会渲染出文案，而 `log_type` 是 70，自相矛盾。
        action, log_type, field_name, summary = old.action, _OTHER, "", ""
    else:
        action, log_type, field_name = classified
        if action == "order.transition":
            # R15：旧行存英文状态码，新文案要中文（spec §5.1「与对标一致」）。
            from_value = _translate_status(from_value)
            to_value = _translate_status(to_value)
        summary = _render(
            action,
            actor=old.actor,
            # 旧表没有「目标名字」列——**不拿 `actor` 冒充目标**（actor 是操作人）。
            target_name="",
            order_no=old.order_no,
            field_name=field_name,
            from_value=from_value,
            to_value=to_value,
            reason=reason,
        )
        # `summary` 是 varchar(255)，而 `performance.review` 的渲染含整段 `reason`
        # （旧列也是 255）⇒ 必须按列宽截断，否则超长旧行会让迁移以 `DataError`
        # 中断（E-0010 的同族纪律）。
        summary = summary[:summary_width] if summary_width else summary

    return OperationLog(
        log_type=log_type,
        action=action,
        target_kind="order",
        target_id=old.order_no,
        target_name="",
        order_no=old.order_no,
        actor=old.actor,
        actor_id=None,
        field_name=field_name,
        from_value=from_value,
        to_value=to_value,
        reason=old.reason,
        summary=summary,
        created_at=old.created_at,
    )


def backfill(apps, schema_editor):
    LegacyAuditLog = apps.get_model("business", "AuditLog")
    OperationLog = apps.get_model("audit", "OperationLog")
    summary_width = OperationLog._meta.get_field("summary").max_length

    batch = []
    # `iterator()` 流式读（不把旧表整表 materialise 进内存），每 `_BATCH_SIZE` 行写一批。
    for old in LegacyAuditLog.objects.order_by("id").iterator(chunk_size=_BATCH_SIZE):
        # ⭐ M3a：放不进**当时**那一列（64）的 `actor` **留字给 `0006`**——
        # 在这里写会 `DataError` 中断整个 `migrate`（M2 实测），截断又会静默丢操作人。
        if len(old.actor or "") > BACKFILL_ACTOR_WIDTH:
            continue
        batch.append(_build(old, OperationLog, summary_width))
        if len(batch) >= _BATCH_SIZE:
            OperationLog.objects.bulk_create(batch, batch_size=_BATCH_SIZE)
            batch.clear()
    if batch:
        OperationLog.objects.bulk_create(batch, batch_size=_BATCH_SIZE)


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0003_operationlog_target_name"),
        # 创建 `business.AuditLog` 的那条迁移（spec §9 评审 M10）：两个 app 都住在
        # 租户 schema，依赖写错会 `LookupError` 或读到空表。
        ("business", "0005_auditlog"),
    ]

    operations = [
        migrations.RunPython(backfill),
    ]
