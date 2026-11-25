"""租户内统一操作日志（spec §4.2）。

本 app 在 `TENANT_APPS` 里 ⇒ 表按租户 schema 各建一份，租户上下文只来自认证态。
"""
from django.db import models
from django.utils import timezone


class LogType(models.IntegerChoices):
    """对标的 8 个日志分类，**code 逐字照抄**（RECON §1.3 / spec A2）。"""
    ACCOUNT_POOL = 5, "号库相关"
    ORDER = 10, "订单相关"
    PERFORMANCE = 20, "绩效相关"
    USER = 30, "用户相关"
    SYSTEM = 40, "系统相关"
    MEMBER = 50, "会员相关"
    GROUP = 60, "排团相关"
    OTHER = 70, "其它"


class OperationLog(models.Model):
    log_type = models.SmallIntegerField("日志分类", choices=LogType.choices)
    action = models.CharField("动作", max_length=64, db_index=True)
    target_kind = models.CharField("目标类型", max_length=16, blank=True)
    target_id = models.CharField("目标标识", max_length=64, blank=True)
    # R11：句子里要显示的**目标名称**（用户名 / 接单人 / 角色名 / 游戏 / 项目名…）；`target_id` 是对标「相关ID」列的落点（对标显示数字 id），不能兼作名字。
    # 列宽按**它真正会装的值的最大来源**取，不按「大多数时候很短」取：最宽来源 `Project.name` = **450**（`apps/business/models.py:134`，配置域 `ConfigAuditMixin` 整串写入），次宽 `auth_user.username` = **150**；原 64 比两个来源都窄 ⇒ `record()` 的 `_clip` 会**静默截断**成「名字前缀」，两个名字可能截成同一串。见 spec §16.1 R34。
    target_name = models.CharField("目标名称", max_length=450, blank=True)
    order_no = models.CharField("系统订单号", max_length=20, blank=True, db_index=True)
    source_order_no = models.CharField("来源订单号", max_length=128, blank=True)
    game_role_name = models.CharField("订单角色名", max_length=128, blank=True)
    game_name = models.CharField("所属游戏", max_length=64, blank=True, db_index=True)
    # 列宽 150 = `auth_user.username` 上限（Django 5.2 `AbstractUser`，本仓没有自定义用户模型）；原 64 是同型缺陷——`main` 的 `Order.updated_by` / `AuditLog.actor` 都因「64 < username 上限」修过（实测的 HTML 500）。
    # `_clip` 按列宽**静默截断** ⇒ 65–150 字的认证态用户名在日志里变成另一个人的名字前缀，`?actor=<真名>` 也永远查不到这一行。**不要**再按「名字一般很短」把它改窄。
    actor = models.CharField("操作用户", max_length=150, blank=True, db_index=True)
    actor_id = models.IntegerField("操作用户ID", null=True, blank=True)
    # ⭐ 审计日志线（spec §3.1）：**显示名**（昵称优先、空则登录账号），由 `log.record()` 从 `actor_id` 派生（口径唯一出处 `apps/accounts/display.py`）。
    # 另开一列而不改 `actor` 语义：`actor` 是**登录账号**，供 `?actor=` 过滤与身份追溯；昵称会被人改名，拿它当身份键会让历史行查不到（spec §5.4）。
    # ⚠️ **不回填**（spec §1.2 裁定 4）：老行 `actor_name=''`，老 `summary` 一个字不动。宽度 150 = `first_name` / `username` 两个来源的列宽（理由同上面 `actor` 段）。
    actor_name = models.CharField("操作用户昵称", max_length=150, blank=True)
    field_name = models.CharField("被改字段", max_length=64, blank=True)
    from_value = models.CharField("变更前", max_length=64, blank=True)
    to_value = models.CharField("变更后", max_length=64, blank=True)
    reason = models.CharField("原因/备注", max_length=255, blank=True)
    summary = models.CharField("操作说明", max_length=255)
    created_at = models.DateTimeField("发生时间", default=timezone.now)

    class Meta:
        verbose_name = "操作日志"
        verbose_name_plural = "操作日志"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["log_type", "created_at"]),
            models.Index(fields=["actor", "created_at"]),
            models.Index(fields=["target_kind", "target_id"]),
            # 单列 created_at：页面默认视图「不选分类、按时间倒序取 30 行」没有 log_type 谓词，
            # 上面那条复合索引的最左列用不上，ORDER BY created_at DESC 会退化成全表扫描 + 排序。
            models.Index(fields=["created_at"]),
        ]


class RequestMetricKind(models.TextChoices):
    """请求性能指标的两种记录（spec 口径 `docs/plans/2026-10-04-request-metrics.md` §3）。

    `AGG` 是**按整点汇总**的一行（同人同接口同整点同状态码只留一行）；
    `SLOW` 是**超阈值的那一次请求**逐条留痕（存精确耗时，供排障看个例）。
    """
    AGG = "agg", "按小时汇总"
    SLOW = "slow", "慢请求逐条"


class RequestMetric(models.Model):
    """请求性能指标（中文名同 `verbose_name`）——「谁、什么时候、哪个接口、多久」。口径与取舍（spec §2/§3，⛔ 不要改成「每请求一行」）：
    **写入是数据库侧的一次 UPSERT**（`apps/audit/metrics.py::upsert_request_metric`，`INSERT … ON CONFLICT … DO UPDATE` 原语）：命中已有行就 `count += 1` / `total_ms += 耗时` / `max_ms = GREATEST(max_ms, 耗时)` / 对应直方图桶 `+= 1`，⛔ **不许「先读再写」**（并发下会丢更新）。表基数 = 「小时数 × 路由数 × 状态码数 × 人数」+ 慢请求条数——「路由数**有界**」靠中间件「解析不到就写固定哨兵」（`apps/core/request_metrics.py::UNRESOLVED_ROUTE`）：旧实现回落 `request.path`，404 路径可以互不相同 ⇒ 基数**无界**（实测 50 个互不相同的 404 路径 = 50 行），⛔ 别把回落 `request.path` 加回来。`user_id` 用 `0` 表示未登录、⛔ 不用 `null=True`（NULL 在唯一约束里**互不相等**，匿名行的 UPSERT 永远不合并、退化成每请求一行）；`route` 存**接口模板**（`request.resolver_match.route`）⛔ 不存带真实 id 的路径（基数是接口数不是订单数，聚合才有意义）；直方图六桶（`hist_*`）是**近似分位数**用的，⛔ 不另存原始数据；本表在 `TENANT_APPS` ⇒ 按租户 schema 各建一份。
    ⚠️ **不设 `Meta.ordering`**（有意）：模型级默认排序会被带进 `values()` / `annotate()` 的 `GROUP BY`（Django 文档明写的坑），性能分析的聚合查询会被它悄悄改结果。
    """

    kind = models.CharField(
        "指标类型", max_length=8, choices=RequestMetricKind.choices
    )
    # `agg` = 该时刻所在**整点**；`slow` = 那一次请求的时刻（精确到微秒）。
    # 单列索引由 `db_index=True` 提供（`bucket_start` 是页面时间范围筛选的第一谓词）；
    # 复合面另有 `(username, bucket_start)`（按人查要它）。
    bucket_start = models.DateTimeField("统计起点", db_index=True)
    route = models.CharField("接口模板", max_length=255)
    method = models.CharField("请求方法", max_length=8)
    status_code = models.PositiveSmallIntegerField("状态码")
    # ⚠️ **`0` = 未登录/匿名**（见类 docstring：唯一约束里的 `NULL` 互不相等）。
    # ⛔ 不要改成 `null=True`；踩过与理由在 spec §2。
    user_id = models.IntegerField("用户ID", default=0)
    # 用户名**快照**。列宽 150 是 `auth_user.username` 的列宽（Django 5.2 `AbstractUser`）——
    # 先例是同文件的 `actor`（64 → 150，理由见那里的注释），⛔ 别按「名字一般很短」改窄。
    username = models.CharField("用户名快照", max_length=150, blank=True)
    count = models.PositiveIntegerField("请求次数", default=0)
    total_ms = models.BigIntegerField("总耗时毫秒", default=0)
    max_ms = models.PositiveIntegerField("最大耗时毫秒", default=0)
    # 分档直方图（`<100 / <200 / <500 / <1000 / <3000 / >=3000` 毫秒）。
    # ⚠️ 字段名与顺序被 `apps/audit/metrics.py::HIST_FIELDS` 逐字依赖（那里的 SQL 按它拼列名）。
    hist_lt100 = models.PositiveIntegerField("耗时<100ms次数", default=0)
    hist_lt200 = models.PositiveIntegerField("耗时<200ms次数", default=0)
    hist_lt500 = models.PositiveIntegerField("耗时<500ms次数", default=0)
    hist_lt1000 = models.PositiveIntegerField("耗时<1000ms次数", default=0)
    hist_lt3000 = models.PositiveIntegerField("耗时<3000ms次数", default=0)
    hist_gte3000 = models.PositiveIntegerField("耗时>=3000ms次数", default=0)
    # ⚠️ 两列都由 `metrics.upsert_request_metric` 的 SQL 用 `NOW()` 显式赋值：写入走的是
    # `INSERT … ON CONFLICT` 原语，ORM 的 `auto_now` / `auto_now_add` 在那条路上**不生效**
    # ⇒ 这里刻意用 `default=timezone.now`（不写 `auto_now*`），免得读代码的人以为 Django 会维护它。
    created_at = models.DateTimeField("创建时间", default=timezone.now)
    updated_at = models.DateTimeField("更新时间", default=timezone.now)

    class Meta:
        verbose_name = "请求性能指标"
        verbose_name_plural = "请求性能指标"
        constraints = [
            # 唯一键（spec §2）：同「类型 + 整点 + 接口 + 方法 + 状态码 + 人」只留一行。
            # ⚠️ 列清单与 `metrics._UNIQUE_FIELDS` 是同一组字面量（SQL 的 `ON CONFLICT` 要它）。
            models.UniqueConstraint(
                fields=[
                    "kind",
                    "bucket_start",
                    "route",
                    "method",
                    "status_code",
                    "user_id",
                ],
                name="uniq_request_metric_key",
            ),
        ]
        indexes = [
            # 按人查（`by_user` / 页面按人筛选）。单列 `bucket_start` 由字段上的
            # `db_index=True` 提供（复合索引的最左列是 username，服务不了只按时间的查询）。
            models.Index(fields=["username", "bucket_start"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind}:{self.method} {self.route}@{self.bucket_start:%Y-%m-%d %H}"
