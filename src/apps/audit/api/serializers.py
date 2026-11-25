"""`GET /api/audit/logs/`（spec §6.1）、`GET /api/audit/actors/`（spec §6.3）两条读端点与 `POST /api/audit/events/`（spec §6.2）上报端点的查询参数 / 请求体 / 行序列化器。 ⚠️ **请求参数一律过这里**（AGENTS.md §5 的 E-0010 条）：判据是「**这类**请求还会不会 500」，
不是「这个字段还会不会 500」。`?stime=garbage` 直接进 `filter()`、`?log_type=999` 直接进 `ChoiceField` 之外的路径，都可能变成 HTML 500；过一遍序列化器则统一是 **HTTP 200 + `code=1000`**（`apps.core.exceptions`）。
"""
import re

from django.contrib.auth import get_user_model
from rest_framework import serializers

from apps.accounts.display import user_display_name
from apps.audit.actions import ORDER_ACTION_LABELS
from apps.audit.display import action_label, summary_for_display
from apps.audit.log import business_field_value
from apps.audit.models import LogType, OperationLog


#: `ordering` 参数的**白名单**：参数值 → 交给 `order_by` 的字段元组（spec §6.1）。 - 词表只覆盖「操作时间」列（`system-systemlog.ts` 的 8 列里**唯一** `sort: true` 的那列），两向：`created_at` = 旧在前、`-created_at` = 新在前。
#: - **每个元组的第二个元素是稳定 tie-break**：同一 `created_at` 的多行若只按时间排，数据库不保证次序 ⇒ 翻页时同一行可能重复出现或被跳过。`OperationLog.Meta.ordering` 本来就是 `["-created_at", "-id"]`，新路径不能把 `-id` 丢掉（升序对应 `id`）。
#: - **白名单而不是自由字符串**：自由值等于「把请求值直接交给 `order_by`」（AGENTS.md §5 的 E-0010 条）。非法值由下面的 `ChoiceField` 当场拒成 **1000**（不是 500，也不是静默空集）。 - **缺省（参数不传）不在这里出现**：视图保持 `Meta.ordering`，行为与加本参数之前**逐字节相同**。
LOG_ORDERING_FIELDS: dict[str, tuple[str, ...]] = {
    "-created_at": ("-created_at", "-id"),
    "created_at": ("created_at", "id"),
}


class OperationLogSearchSerializer(serializers.Serializer):
    """6 个筛选 + `ordering` + 分页参数。 `stime` / `etime`（spec §6.1）：入参形如 `yyyy-MM-dd HH:mm:ss`。 `DateTimeField` 在 `USE_TZ=True` 下会对 **naive** 值执行 `timezone.make_aware(value, timezone.get_current_timezone())` （`rest_framework/fields.py:1181`；`default_timezone()` 取 `settings.TIME_ZONE` = `Asia/Shanghai`，同文件 `:1191-1192`）——这就是 spec 要的**本地时区**语义。 ⚠️ **不要**换成裸 `datetime.strptime`（解析完不做 `make_aware`）。实测（Django 5.2， `django/db/models/fields/__init__.py:1660-1684` 的 `DateTimeField.get_prep_value`）：框架对 naive 值的兜底是「按 `get_default_timezone()`（= `settings.TIME_ZONE`，本仓也是
    `Asia/Shanghai`）补成 aware，**并写一条 `RuntimeWarning`**」。也就是说—— - 瞬时**恰好是对的**（本项目 `test_operation_log_api` 的**值**断言分辨不出这一版）； - 但它把语义交给了框架的**向后兼容兜底**，测试输出也不再干净 ⇒ 那条用例额外断言「本次请求没有 naive datetime 警告」，正是钉这一点。会被**值**断言抓住的是「按 UTC 拼 aware」（如 `make_aware(value, timezone.utc)`，或对 naive 值 `replace(tzinfo=utc)`）——那会整体偏 8 小时，跨零点的筛选漏行/多行。要自己解析就必须用 `timezone.get_current_timezone()`（= `TIME_ZONE`）。 `log_type` 用 `ChoiceField`：8 个 code 之外的取值（如 `999`）当场拒成 1000，而不是静默返回空集让客户端分不清「没有匹配」与「参数写错」。`0` = 全部，由视图解释。
    `ordering`（spec §6.1 / §16.2 第 1 条）也走 `ChoiceField`：取值只有 `LOG_ORDERING_FIELDS` 的两个键（`created_at` / `-created_at`）。 **不传** = 保持 `Meta.ordering`（最新在前）；传白名单外的值（`id`、`-created_at,-id`…）当场拒成 1000——**不能**把请求值直接交给 `order_by`（AGENTS.md §5 的 E-0010 条）。
    """

    stime = serializers.DateTimeField(required=False)
    etime = serializers.DateTimeField(required=False)
    log_type = serializers.ChoiceField(
        choices=[(0, "全部"), *LogType.choices], required=False
    )
    # 排序白名单（两向，见 `LOG_ORDERING_FIELDS` 的注释）。choices 从**映射的键**派生，
    # 词表只有一个出处：加值只改那个 dict，视图与这里的校验同时跟上。
    ordering = serializers.ChoiceField(
        choices=sorted(LOG_ORDERING_FIELDS), required=False
    )
    game_name = serializers.CharField(required=False, max_length=64)
    # 多选：视图把「重复参数」与「逗号分隔」两种形状都归一成 list 再喂进来（`QueryDict.items()` 只保留每个键的最后一个值，归一化不能靠它）。 ⚠️ `actor` 的宽度**从模型元数据取**（不另抄数字）：M2 把该列 64 → 150 之后，
    # 这里若仍写死 64，用长用户名筛选会被当场拒成 `1000`——用户「按操作人查」查不到自己刚写下的行（列里存的是完整名字，筛选却按旧宽拒收）。`order_no` 的写法同源。
    actor = serializers.ListField(
        child=serializers.CharField(
            max_length=OperationLog._meta.get_field("actor").max_length
        ),
        required=False,
    )
    # ⭐（D-0301）：审计页「订单号」精确框放宽——它同时匹配 `order_no`
    # **或** `source_order_no`（视图里双列 OR），所以宽度取**两列的较大者**
    # （`source_order_no` 128 > `order_no` 20）；「非空短串」即合法，不再限 20 位数字形状。
    order_no = serializers.CharField(required=False, max_length=128)
    key = serializers.CharField(required=False, max_length=255)
    include_delete_reason = serializers.BooleanField(required=False)
    # `page` / `page_size` 只做**形状**校验（正整数）；上限由 `EnvelopePagination`
    # 的 `max_page_size=200` 截断——这里再加 `max_value` 会把「截断」变成「报错」，
    # 与项目既有分页语义不一致。`page` 交给分页器解释（越界页由 DRF 归一成业务错误）。
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.IntegerField(required=False, min_value=1)


class OperationLogRowSerializer(serializers.ModelSerializer):
    """列表行的 8 个对标显示字段 + `action` + `actor_display`（spec §3 的对标列 ↔ 我方列，§6.1 / §11.1）。 - `created_at` 走 DRF `DateTimeField`，按当前时区（`Asia/Shanghai`）渲染，尾部 `+08:00`。订单详情那条路是模型直读 `isoformat()`、不经 DRF，库里读出的是 UTC，尾部 `+00:00`。
    两条路偏移不同，前端按串里自带的偏移换算，不直接切字符。 - `action` 前端暂时不用，但重放测试要读它，所以显式带上。
    """

    #: 「操作用户」列**显示用**的昵称。只读派生字段，不落库、不参与筛选。 ⚠️ 取不到人（`actor_id` 为空 / 用户已删）时**回退行里存的 `actor`**——历史行也必须能显示，不能变成空白（spec §11.1）。
    #: ⚠️ 昵称由视图**一次性批量**解析后经 `context["actor_display_names"]` 注入（`apps/audit/display.py::actor_display_names`）：这里**不许**逐行 `User.objects.get(...)`——一页 200 行会打 200 次库（spec §11.1）。
    actor_display = serializers.SerializerMethodField()

    #: ⭐（D-0341）：「来源订单号」列的**读时回填**（D7 口径，与同文件的 `OrderActionLogRowSerializer.get_source_order_no` 同一族）：日志列**有值就用日志列的** （记下来的那一刻的值优先），为空才按 `order_no` 取订单**当前**的来源单号——日志行的 `source_order_no` 只有**少数写点**会填（⭐ 评审 F-1 改准：括号配平全扫 `record(...)` 实得 **5 个写点**——`order.create` ×2（录单 + 拆分子单）、拆分首段 `order.update`、发货回执 `order.ship_result`、peer 同步族；逐条 `文件:行`
    #: 见 `apps/audit/api/views.py::_order_backfill` 的 docstring。绝大多数日志行为非 create 写点 ⇒ 该列多数为空），不回填的话这一列对绝大多数订单类日志恒空。回填映射由视图**一次性批量**解析后经 `context["order_backfill"]` 注入（照 `actor_display` 的既有手法，⛔ 不许逐行查库）。 ⚠️ 前端再拿它对内置单号做兜底（`source_order_no || order_no`，D-0301 的「载荷给原值、展示回退在前端做」分工）——两个键都是**原值**，这里不拼展示值。
    source_order_no = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()

    def get_actor_display(self, obj):
        names = self.context.get("actor_display_names") or {}
        return names.get(obj.actor_id) or obj.actor_name or obj.actor or "系统"

    def _backfill(self, obj):
        """本行的订单当前值（视图批量解析得来）；没有就回空字典。"""
        return (self.context.get("order_backfill") or {}).get(obj.order_no, {})

    def get_summary(self, obj):
        return summary_for_display(obj, source_order_no=self.get_source_order_no(obj))

    def get_source_order_no(self, obj):
        # 日志列有值 ⇒ 用它；为空才回填订单当前值（同 `OrderActionLogRowSerializer`）。
        return obj.source_order_no or self._backfill(obj).get("source_order_no", "")

    class Meta:
        model = OperationLog
        fields = (
            "target_id",
            "log_type",
            "source_order_no",
            "game_role_name",
            "game_name",
            "summary",
            "actor",
            "created_at",
            "action",
            # 只读显示派生字段（spec §11.1）。追加在末尾：既有 9 个键的名字与顺序不动。
            "actor_display",
            # ⭐（D-0341）：内置单号原值（行里本就有的列，首次对外）。「来源订单号」列的**前端兜底**取它：日志快照与回填都拿不到来源号时，列里回退显示内置单号而不是空格（业务方 ：
            # 「订单号都采用来源订单号 兜底采用内置订单号」，D-0301 的同一口径）。 ⛔ 它只是展示兜底：搜索（`order_no` 参数）与寻址键不因它改变。
            "order_no",
        )


class OperationLogWithDeleteReasonSerializer(OperationLogRowSerializer):
    delete_reason = serializers.SerializerMethodField()

    def get_delete_reason(self, obj):
        return obj.reason if obj.action == "order.delete" else ""

    class Meta(OperationLogRowSerializer.Meta):
        fields = (*OperationLogRowSerializer.Meta.fields, "delete_reason")


#: 「只给到日」的日期串形状（`2030-01-02`）——用来判「这个 `etime` 是日期还是时刻」。
#: ⚠️ 用 `fullmatch` 而不是 `search`：只要串里出现别的东西（时间部分、时区）就**不是**这一档，
#: 那时按 DRF 自己的口径解析（⛔ 不替调用方猜）。
_DATE_ONLY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class InclusiveEndDateTimeField(serializers.DateTimeField):
    """`etime` 专用：**只给日期**（`2030-01-02`）表示「**含当天**」。 ## 为什么需要它（业务方报过的缺陷）页面上那两个日期框是 `<input type="date">`（证据件 `lay-verify="date"`），前端发的是**纯日期** （`stime=2030-01-01&etime=2030-01-02`）。而 DRF 的 `DateTimeField` 解析纯日期时会**补成当天 `00:00:00`** ⇒ `created_at <= 00:00:00` 把**当天全天的记录全排除掉**。业务方原话：「选中了 9 月 4 到 10 月 4 但是没有包括当天四号的记录」。
    ⚠️ **修在这里而不是前端**（「唯一出处」的取舍）：`etime` 的语义是「操作时间 **≤** etime」，而用户选的是**日期** ⇒「只给日期」时的正确语义就是「含当天」，`00:00:00` 是**解析器的默认填充、不是用户意图**。改在接口这一层，任何调用方（页面 / 脚本 / 以后的别处）都不会再踩； ⛔ 反过来只在前端补 `23:59:59` 的话，下一个调用方还会踩同一脚。 ⚠️ **`stime` 不用它**：只给日期时 `00:00:00` 正好是「含当天」的**起点**，本来就是对的。 ⚠️ **给到时刻的输入照原样**（`2030-01-02T12:00:00`、带时区的串都不动）：那时用户表达的是
    一个明确时刻，⛔ 不替他扩到当天结束。 ⚠️ `.999999` 是**完整覆盖**、不是近似：PostgreSQL 的 `timestamptz` 精度就是**微秒** （`django/db/backends/postgresql/base.py` 的 `Database.datetime_cast` 口径），不存在比它更细的值会被漏掉。
    """

    def to_internal_value(self, value):
        if isinstance(value, str) and _DATE_ONLY_RE.fullmatch(value.strip()):
            # 交回**父类**去解析补好的串 ⇒ 时区处理与「别处传进来的时刻」**同一套**
            # （⛔ 不在这里自己 `make_aware`：那会把 DRF 的时区口径抄成第二份）。
            value = f"{value.strip()}T23:59:59.999999"
        return super().to_internal_value(value)


class OrderActionLogSearchSerializer(serializers.Serializer):
    """`GET /api/audit/order-action-logs/` 的查询参数（交易订单操作记录线 spec §4.2）。 ⚠️ **请求来源的值一律过这里**（AGENTS.md §5 的 E-0010 条）：非法值统一由 DRF `ValidationError` 翻成 **HTTP 200 + `code=1000`**，⛔ 不是 500、也不是静默空集。形状照同文件的 `OperationLogSearchSerializer`（两条读端点同族），差异只有：这里**没有** `log_type` / `game_name` / `order_no` / `key`，多了 `key1`（见下）。 ⚠️ `action` 用 `ChoiceField`，词表**从 `ORDER_ACTION_LABELS` 的键派生**（两处一个出处）：
    传不在表里的动作码当场拒成 `1000`，⛔ **不能**静默当「全部」——那会让用户以为筛过了。 ⚠️ `actor` 是**整值相等**（spec §4.2），不是子串、也不是多选：本页那个下拉是单选。 ⚠️ `key1` 同时搜「系统单号 / 交易单号」两列（`order_no` 或 `source_order_no`，icontains）， ≤255 与 `summary` 列的口径同源。 ⚠️ `etime` 用 `InclusiveEndDateTimeField`（**含当天**，见那个类的 docstring）；`stime` 用普通 `DateTimeField`（当天 `00:00:00` 就是正确的起点）。
    """

    stime = serializers.DateTimeField(required=False)
    etime = InclusiveEndDateTimeField(required=False)
    action = serializers.ChoiceField(
        choices=sorted(ORDER_ACTION_LABELS), required=False
    )
    # 宽度**从模型元数据取**，不另抄数字（同 `OperationLogSearchSerializer.actor` 的理由）：
    # 写死会在列宽变化时把合法用户名拒成 1000。
    actor = serializers.CharField(
        required=False, max_length=OperationLog._meta.get_field("actor").max_length
    )
    key1 = serializers.CharField(required=False, max_length=255)
    # 排序白名单与 `/api/audit/logs/` 共用同一个映射（`LOG_ORDERING_FIELDS`，两处一个出处）。
    ordering = serializers.ChoiceField(
        choices=sorted(LOG_ORDERING_FIELDS), required=False
    )
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.IntegerField(required=False, min_value=1)


class OrderActionLogRowSerializer(serializers.ModelSerializer):
    """交易订单操作记录的行形状——**恰好 11 个键**（spec §4.3）。 ⛔ `order_no` 才是「系统单号」（spec D4）：`target_id` 不是单号列——实测 `order.delete` / `order.restore` 装的是 Order 主键、`order.claim` / `order.clear_taker` 装的是 user 主键（spec §2.2）。 ⛔ 「订单标题」列**换成**了「变更内容」= `from_value` → `to_value`（spec D5，业务方 R4）；两列都空时该格留空，由前端负责，这里如实回空串。
    ⚠️ `game_name` / `source_order_no` 是**读时回填**（spec D7）：日志列**有值就用日志列的**，为空才按 `order_no` 取订单当前值。回填表由视图**一次性批量**解析后经 `context["order_backfill"]` 注入（照 `actor_display` 的既有手法，⛔ 不许逐行查库）。 ⚠️ `actor_display` 由视图注入的 `context["actor_display_names"]` 提供——与 `/api/audit/logs/` 的 `OperationLogRowSerializer` **同一份实现**（spec §4.3 明文要求「⛔ 不另写一份」）。
    ⚠️ `created_at` 走 DRF 默认 ISO-8601 + **当前时区**（`Asia/Shanghai`，尾部 `+08:00`），与 `OperationLogRowSerializer` 同源；⛔ 不要改成模型直读的 `isoformat()`（那是 UTC）。
    """

    # 列表用短名，未登记的技术动作仍可用原始键查找。
    action_label = serializers.SerializerMethodField()

    #: 「操作用户」列**显示用**的昵称；取不到人时回退行里存的 `actor`（同既有实现）。
    actor_display = serializers.SerializerMethodField()

    #: D7 读时回填的两个显示列（日志列为空才取订单当前值）。
    game_name = serializers.SerializerMethodField()
    source_order_no = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()

    from_value = serializers.SerializerMethodField()
    to_value = serializers.SerializerMethodField()

    def get_from_value(self, obj):
        return business_field_value(obj.field_name, obj.from_value)

    def get_to_value(self, obj):
        return business_field_value(obj.field_name, obj.to_value)

    def get_action_label(self, obj):
        return action_label(obj.action)

    def get_actor_display(self, obj):
        names = self.context.get("actor_display_names") or {}
        return names.get(obj.actor_id) or obj.actor_name or obj.actor or "系统"

    def _backfill(self, obj):
        """本行的订单当前值（视图批量解析得来）；没有就回空字典。"""
        return (self.context.get("order_backfill") or {}).get(obj.order_no, {})

    def get_game_name(self, obj):
        # 日志列有值 ⇒ 用它（「记下来的那一刻」的值优先，spec D7）；为空才回填。
        return obj.game_name or self._backfill(obj).get("game_name", "")

    def get_summary(self, obj):
        return summary_for_display(obj, source_order_no=self.get_source_order_no(obj))

    def get_source_order_no(self, obj):
        return obj.source_order_no or self._backfill(obj).get("source_order_no", "")

    class Meta:
        model = OperationLog
        fields = (
            "order_no",
            "source_order_no",
            "game_name",
            "from_value",
            "to_value",
            "action",
            "action_label",
            "summary",
            "actor",
            "actor_display",
            "created_at",
        )


class OperationLogActorSearchSerializer(serializers.Serializer):
    """`GET /api/audit/actors/` 的查询参数（spec §6.3）——**只有一个** `key`。 `max_length` 取 `auth_user.username` 的列宽 **150**（Django 5.2 的 `AbstractUser.username`，本仓没有自定义用户模型）：本端点查的就是 `username` /
    `first_name`（昵称）两列，两者的列宽同为 150（spec §11.1 的按昵称搜索）。超一位就拒成 `1000`，而不是截断后静默查一个永远匹配不到的值——与 `OperationLogSearchSerializer.key` 对 `summary`（255）的口径同源（E-0010）。
    """

    key = serializers.CharField(required=False, max_length=150)


class OperationLogActorRowSerializer(serializers.ModelSerializer):
    """人员枚举行的**全部**字段：`id` + `username` + `display_name`（spec §6.3 / §11.1）。 ⚠️ **不要**改成 `fields = "__all__"`，也不要顺手加字段：账号表那一侧还挂着手机号 / 身份证号 / 收款账号等 PII，而本端点的准入**只是** `system.logs`
    （不是账号管理员）——形状在这里是一道白名单，多一个字段就是一次泄漏。前端多选提交时传的是 `username`（历史行的 `actor_id` 可能为空，spec §6.3）， `id` 只是给下拉做稳定 key 用。
    """

    #: 下拉**显示**用的昵称（`first_name`，空则账号名）。
    #: ⚠️ 这不是放宽 PII 白名单：昵称本来就是账号列表页公开显示的一列；
    #: 白名单仍然**逐条枚举**（见 `Meta.fields`），不许改成 `fields = "__all__"`。
    display_name = serializers.SerializerMethodField()

    def get_display_name(self, obj):
        # 口径的唯一出处：`apps.accounts.display.user_display_name`（昵称优先、空串回落账号）。
        return user_display_name(obj)

    class Meta:
        model = get_user_model()
        fields = ("id", "username", "display_name")


#: 允许**前端上报**的读动作白名单（spec §6.2）。今天恰好一条：`order.open_edit` （「进入编辑订单页面」——**只有浏览器能感知**，见 spec §7.3）。 ⚠️ **不要把 `order.view_credentials` 加进来**：那条读动作由订单详情视图**服务端**写
#: （`OrderViewSet.retrieve`，不可伪造）。在这里接受它，等于让任何客户端伪造「看过游戏账号密码」这条记录——这正是 spec §7.3 把两条腿分开的原因。加新动作前先回答「它是不是只有前端能感知」：服务端可感知的一律留在服务端。
#: 它是**命名常量**（不是散在 `if` 里的字面量）：测试直接钉这个集合，加一个动作只改这一行。
FRONTEND_REPORTABLE_READ_ACTIONS = frozenset({"order.open_edit"})


class OperationLogEventSerializer(serializers.Serializer):
    """`POST /api/audit/events/` 的请求体（spec §6.2）：**只有** `action` 与 `order_no`。 - `actor` / `actor_id` / tenant / schema **不在字段表里** ⇒ 请求体里带了也被丢弃（DRF 的 `Serializer` 只读声明过的键，未知键静默忽略）。落库的操作人与 schema **一律取认证态**——「不可伪造」是靠**没有入口**做到的，不是靠读了再校验。 - `action` 用 `ChoiceField`：不在白名单里的动作（包括注册表里的写动作、以及 `order.view_credentials` 这条必须留在服务端的读动作）当场拒成 `1000`。
    - `order_no` 与 `OperationLog.order_no` **列宽一致**（从模型元数据取，不另抄数字）：超一位就拒成 `1000`，而不是截断后静默写一个与请求不同的订单号（E-0010；相邻的 `OperationLogSearchSerializer` 对 `order_no` 是同一口径）。空串按「没传」处理——`order_no` 是可选键。
    """

    action = serializers.ChoiceField(choices=sorted(FRONTEND_REPORTABLE_READ_ACTIONS))
    order_no = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=OperationLog._meta.get_field("order_no").max_length,
    )
