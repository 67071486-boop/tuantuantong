"""租户 schema 内的业务实体。
本模块的模型在每个租户 schema 中各有一份完整表结构、不含 tenant_id 列——schema 就是隔离边界（见 D-0006）。"""
import secrets
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.business.project_tag_rules import (
    PROJECT_TAG_CATEGORY_CHOICES,
    STAT_WINDOW_CHOICES,
)


class Game(models.Model):
    """游戏。配置链的最顶层。四个开关照对标物「游戏管理」页逐列搬来（决策 D-0023，spec §3.1）：is_default / is_mobile / is_companion / disable_sms。
    ⚠️ 四个开关都已随配置页序列化器返回并上屏（GameSerializer.fields），但 disable_sms 要等短信能力、is_companion（「开启后录单页不需要录游戏账号密码」）要等录单页接它、is_default 要等录单页把它当默认选中——不要在别处假设它们已经生效。
    ⚠️ is_default 没有加「全局唯一」约束：对标实测数据里恰好一行，但「服务端是否强制」未取到证据（只读到单行开关的读码），按证据不足不动约束。"""

    name = models.CharField("游戏名称", max_length=64)
    sort_order = models.IntegerField("排序", default=0)
    is_active = models.BooleanField("是否启用", default=True)
    is_default = models.BooleanField("是否默认", default=False)
    is_mobile = models.BooleanField("是否为手机游戏", default=False)
    is_companion = models.BooleanField("是否为陪玩游戏", default=False)
    disable_sms = models.BooleanField("是否禁发短信", default=False)
    # 游戏 logo（`feat/game-logo`）：**只存对象名**（key），⛔ 不存 URL——
    # URL 是签出来的，落库等于把会过期的链接写死（同头像 / 订单截图 / 首页二维码的既有口径，
    # 见 `apps/core/storage.py` 的模块 docstring）。空串 = 没有 logo ⇒ 网页端与打手 App 回落到「首字 + 色块」；⛔ 不用 `null`（前端只判空串）。写入**只经过** `POST /api/business/games/<id>/logo`（认领），⛔ 不走通用的`PATCH /api/business/games/<id>/`：那条路没有任何前缀 / 魔数 / 大小校验。
    logo_key = models.CharField(
        "Logo 对象名", max_length=255, blank=True, default=""
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    # 「删除」= **软删除**（业务方裁定，D-0085）。置上时间戳即视为已删除：
    # 行**留在库里**，历史订单上的 `Order.game`（PROTECT）原样有效；录单与各页下拉不再出现它。
    # 置回 `None` 即恢复（`PATCH /api/business/games/<id>/ {deleted_at: null}`）。
    deleted_at = models.DateTimeField("删除时间", null=True, blank=True)

    class Meta:
        verbose_name = "游戏"
        verbose_name_plural = "游戏"
        # 「数字越大越靠前」——对标该列的表头原文就是「游戏排序(数据越大排序越靠前)」。
        # ⚠️ 这个 ordering 同时决定录单页/详情页下拉的顺序（同一条数据、同一个 Meta），
        # 所以它取反后那两处的顺序**也跟着反过来**（D-0023 的代价）。
        ordering = ["-sort_order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["name"], name="uniq_game_name"),
        ]

    def __str__(self):
        return self.name


class GameLibrary(models.Model):
    """平台游戏库候选清单（每租户一份）：新增游戏弹窗从这里选，选中后写 `Game`。

    只放候选名与字母索引，不承载租户业务数据；新增游戏只插一行，不需要改前端。
    """

    name = models.CharField("游戏名称", max_length=64, unique=True)
    first_key = models.CharField("字母索引", max_length=8, blank=True, default="")
    sort_order = models.IntegerField("排序", default=0)
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "平台游戏库"
        verbose_name_plural = "平台游戏库"
        ordering = ["-sort_order", "id"]

    def __str__(self):
        return self.name


class GameServer(models.Model):
    """区服。同一游戏内名称唯一——这是防止「微信区 / 微信 / WX区」脏化的关键。"""

    game = models.ForeignKey(
        Game, on_delete=models.PROTECT, related_name="servers", verbose_name="所属游戏"
    )
    name = models.CharField("区服名称", max_length=64)
    sort_order = models.IntegerField("排序", default=0)
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "区服"
        verbose_name_plural = "区服"
        # **刻意保持升序**：区服不在这三页里（对标没有区服维护入口），D-0023 只翻转了
        # Game / ProjectCategory / Project 三个模型 ⇒ 同一个库里两种排序方向并存，这是已知代价。
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["game", "name"], name="uniq_game_server_name"
            ),
        ]

    def __str__(self):
        return f"{self.game.name}/{self.name}"


class ProjectCategory(models.Model):
    """项目分类。挂在游戏下。三个字段照对标物「订单项目分类管理」页的弹窗搬来（决策 D-0023，spec §3.2）：assigned_platforms / title_prefix / title_suffix。
    ⚠️ 三个字段已随 ProjectCategorySerializer 可写、配置页上屏；但它们只在发单到交易平台时才有业务效果（渠道对接是 M6/三期）——不要在别处假设已生效。
    ⚠️ assigned_platforms 只存平台 id、逗号拼接（对标同时存了一份展示名 assPlatName；我们不入库展示名，改由前端用 src/frontend/src/config/platforms.ts 的字典映射——一份字典一个出处，避免名称改一次要改库里的历史行）。"""

    game = models.ForeignKey(
        Game,
        on_delete=models.PROTECT,
        related_name="categories",
        verbose_name="所属游戏",
    )
    name = models.CharField("分类名称", max_length=64)
    sort_order = models.IntegerField("排序", default=0)
    is_active = models.BooleanField("是否启用", default=True)
    assigned_platforms = models.CharField(
        "指定发平台（平台 id，逗号拼接）", max_length=128, blank=True, default=""
    )
    title_prefix = models.CharField("追加到标题前", max_length=50, blank=True, default="")
    title_suffix = models.CharField("追加到标题后", max_length=50, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    # 软删除（同 `Game`，D-0085）：见上面 `Game.deleted_at` 的注释。
    deleted_at = models.DateTimeField("删除时间", null=True, blank=True)

    class Meta:
        verbose_name = "项目分类"
        verbose_name_plural = "项目分类"
        ordering = ["-sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["game", "name"], name="uniq_project_category_name"
            ),
        ]

    def __str__(self):
        return f"{self.game.name}/{self.name}"


class Project(models.Model):
    """代练/陪玩项目。挂项目分类下。
    注意：订单上的项目不一定指向本表——见 D9（可空 FK + 自由文本覆盖）；本表只是「已配置的常用项目」，不是订单项目的唯一来源。"""

    category = models.ForeignKey(
        ProjectCategory,
        on_delete=models.PROTECT,
        related_name="projects",
        verbose_name="所属分类",
    )
    # 长度 64 → 450：对标该页弹窗的项目名是 `maxlength="450"`（D-0023；spec §3.3）。
    name = models.CharField("项目名称", max_length=450)
    base_price = models.DecimalField(
        "挂牌价", max_digits=12, decimal_places=2, null=True, blank=True
    )
    sort_order = models.IntegerField("排序", default=0)
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    # 软删除（同 `Game`，D-0085）：见上面 `Game.deleted_at` 的注释。
    deleted_at = models.DateTimeField("删除时间", null=True, blank=True)

    class Meta:
        verbose_name = "代练/陪玩项目"
        verbose_name_plural = "代练/陪玩项目"
        # 同 Game：标头原文「数字越大越靠前」（D-0023）。
        ordering = ["-sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "name"], name="uniq_project_name"
            ),
        ]

    def __str__(self):
        return f"{self.category.game.name}/{self.category.name}/{self.name}"


class OrderType(models.TextChoices):
    """订单类型。取值待业务方补全（spec §10.1 #5）。"""

    BOOST = "BOOST", "代练"
    COMPANION = "COMPANION", "陪玩"


class OrderStatus(models.TextChoices):
    """8 个互斥状态（D1）。原状态会被覆盖，恢复去向由操作人指定（D8）。本类的 choices 中文 label 由前端镜像 @/config/orderStatus 读取并映射成各页显示的中文；接口载荷既有 status 枚举名、后端也另有多处 `get_status_display()` 中文出口（takerapi / openapi / 客户抽屉等，本项目无 /admin）——业务方裁定：同一个状态在各页面用同一文案；v2 的「待抢单→待接单」措辞覆盖只落前端 V2_ORDER_STATUS_LABELS（2026-11-03）。
    ⚠️ WAITING_GRAB 的 label「待抢单」与对标 /Order/orderlist 的中文原文「待上号」（orderlist.js 的 orderStaRow5 / orderSta9，E-0005）用词不同——这是有意统一后的措辞（大厅页与后端都写「待抢单」）、不是待修的偏差。
    改 label 会改 choices，Django 会为它生成一次 AlterField 迁移：有并行线在飞时先确认迁移窗口。"""

    UNASSIGNED = "UNASSIGNED", "未分配"
    WAITING_GRAB = "WAITING_GRAB", "待抢单"
    IN_PROGRESS = "IN_PROGRESS", "代练中"
    PENDING_REVIEW = "PENDING_REVIEW", "待验收"
    COMPLETED = "COMPLETED", "已完成"
    ISSUE = "ISSUE", "问题单"
    PAUSED = "PAUSED", "已暂停"
    REFUNDED = "REFUNDED", "已退单"


class CustomerKind(models.TextChoices):
    """订单客户的新老客标签（录单时快照）：`new` 新客 / `old` 老客；空串 = 判不出（无客户或无来源）。

    ⚠️ 这里的「来源」是 `Order.order_source` 外键（一家店 = 一个来源字典项），⛔ 不看 `source` 文本。
    """

    NEW = "new", "新客"
    OLD = "old", "老客"


class PaymentStatus(models.TextChoices):
    """收款状态：纯人工标记三档、不跟退款/结账联动（业务方裁定）。库层存码、label 是中文（与 OrderStatus / SettleStatus 同口径）。
    ⚠️ 别与 Performance.SettleStatus（未结账/已结账）混：那是给打手（下家）的结账状态、方向与「向上家收款」相反。⚠️ 对标侧那套字面量是「未结算/结算中/已结算」（筛选 option 与弹窗 radio 的 value）、与本类的 label 不是同一套；前端把对标控件的中文字面量映射成这三个码、映射表落在本批的页面常量里（T7/T8：src/frontend/src/layui-island/pages/order-orderlist.ts 的 PAYMENT_STATUS_MARKS 按码取标记、PAYMENT_STATUS_BY_BENCHMARK_VALUE 把对标字面量翻成码，spec §5/C2）；不要「统一」掉任一套。"""

    UNSETTLED = "UNSETTLED", "未收款"
    SETTLING = "SETTLING", "结算中"
    SETTLED = "SETTLED", "已结款"


# 金额字段：**只有 /price/ 能改**（spec §6）。创建时可以写——录单页本来就是一次填完。
# 定义在这里而不是 API 层：金额字段属于领域知识，序列化器与解析出口都要用它。
MONEY_FIELDS = [
    "total_price",
    "received_price",
    "platform_price",
    "hall_price",
    "refund_fee",
    "fee",
]

# 金额可以为负：负值表示退款 / 冲抵 / 调整（D-0015）。
# 不要再加 ≥ 0 约束（校验器、`min_value=`、CHECK 都不行），否则退款单进不来。
# 唯一保留的金额判据是合法的两位小数（`max_digits=12, decimal_places=2`）。


class OrderQuerySet(models.QuerySet):
    """订单查询集：两个显式筛选方法（spec §4.3-C1 / D2 的落地形状）。「已删单在直读与反向外键两类读路径上不存在」这条由构造保证：默认 manager 追加 deleted_at__isnull=True。⚠️ 这条保证只覆盖经 get_queryset() 的读路径：raw()、手写 SQL 这类绕过 get_queryset() 的调用不在此列（实测 Order.objects.raw("SELECT id, order_no FROM business_order") 会把已删行带出来）。
    ⚠️ 跨关系 JOIN 不经过 manager（Django 把它编译成 SQL JOIN），spec 里那 27 处靠逐处补的条件兜住、不由这里的构造保证；all_objects 是列名白名单逃生口、使用点由 tests/test_order_read_path_net.py 钉住。"""

    def alive(self):
        """未删的订单（默认 manager 的排除条件本身）。
        ⚠️ 查询集上的方法与 manager 上的同名方法不是同一条路（实测，机制见 OrderManager.alive）：Order.objects.all().alive()、Order.objects.filter(…).alive() 走的是本方法、在已带 deleted_at IS NULL 的集合上再叠一次同一个条件——同一套机制、但这一支无害（叠的是同一个条件、结果不变；孪生的 dead() 因为叠的是相反条件而恒空，见下）。"""
        return self.filter(deleted_at__isnull=True)

    def dead(self):
        """已删的订单（回收站列表的底集）。
        ⚠️ manager 上走的是另一条路：Order.objects.dead() 走的是 OrderManager.dead()（从未经默认排除的基起算）⇒ 对；而 Order.objects.all().dead()、Order.objects.filter(game=…).dead() 走的是本方法、会在已带 deleted_at IS NULL 的集合上再叠 deleted_at IS NOT NULL ⇒ 两个条件互斥、得到静默空集（实测 SQL：WHERE deleted_at IS NULL AND game_id=1 AND deleted_at IS NOT NULL）。想在已过滤的集上取已删单，用 Order.all_objects.filter(deleted_at__isnull=False)——all_objects 是纯 models.Manager、没有 dead() / alive()。"""
        return self.filter(deleted_at__isnull=False)


class OrderManager(models.Manager.from_queryset(OrderQuerySet)):
    """默认 manager：默认排除已软删的订单。
    ⚠️ 三条必须知道的行为边界（spec §2.2.2 的四组清单就是这么来的）：①跨关系 JOIN 不经过 manager（filter(order__x=…) 走 SQL JOIN，Django 的 sql/query.py 里没有 _default_manager 的引用）⇒ 27 处逐处补条件、本 manager 帮不上；②正向 FK 对象访问走 _base_manager（related_descriptors.py）⇒ perf.order 取得到已删单；③Order.objects.filter(...).update(...) 会静默 0 行 ⇒ 已删单的资金联动写入不报错。本批的处置：判定放动作入口 + 一条「一列没变」的断言（spec §8-T3 第 12 条）。"""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)

    def alive(self):
        """未删的订单（默认 manager 的排除条件本身）。
        ⚠️ 与 OrderQuerySet.alive() 同名——from_queryset 生成的基类里已经有一份、本类类体这一份按 MRO 覆盖它（同下面 dead() 的机制）；⚠️ 必须走 self.get_queryset()、不能写成 super().get_queryset() 再叠条件：反向 manager（game.orders）的真实类型是 Django 按关系动态生成的 RelatedManager、排在 OrderManager 之前；super() 绑定 __class__ = OrderManager ⇒ 从 OrderManager 之后继续找、跳过 RelatedManager.get_queryset()，而覆盖「本游戏」的那个 FK 条件正是在那一跳叠上去的（机制与源码行号见 dead() 的注释）。
        self.get_queryset() 从 MRO 头开始找、会先命中 RelatedManager.get_queryset()（django/db/models/fields/related_descriptors.py:755；非预取分支在同一函数 :768-770 先调 super().get_queryset() 再 _apply_rel_filters()），非预取时「默认排除 + FK 条件」两条都在；纯 manager 上它同样回到本类的 get_queryset()。"""
        return self.get_queryset()

    def dead(self):
        """已删的订单（回收站列表的底集）。
        ⚠️ 本类的这个方法必须显式写出来，但机制不是「from_queryset 只在 manager 没有同名方法时才复制」：实际是 from_queryset()（django/db/models/manager.py:107-118）在造那个动态基类时就调 _get_queryset_methods()（同文件 :82-105）把 OrderQuerySet.dead() 复制成 manager 方法、塞进基类命名空间（:111-118 的 type(...) 里 **cls._get_queryset_methods(queryset_class)）；那句 if hasattr(cls, name): continue（:96-97，注释 # Only copy missing methods. 在 :95）里的 cls 是 from_queryset 的调用方——本行写的是 models.Manager.from_queryset(...) ⇒ 它是 models.Manager、不是 OrderManager ⇒ 复制照做（实测：本类 __mro__[1].__dict__ 里确实有 dead）；让本类这一份生效的是普通 MRO 覆盖（本类在生成的基类之前）。为什么要覆盖：被复制进来的生成版先走 self.get_queryset()（已带 deleted_at IS NULL 默认排除）、再叠 deleted_at IS NOT NULL ⇒ 两条件互斥、恒为空集、回收站一行都拿不到；本类这一份从 super().get_queryset()（未经默认排除）起算。
        ⚠️ super().get_queryset() 在反向 manager 上会跳过 FK 条件、所以下面把 core_filters 补回来：game.orders 的 MRO 实测为 [RelatedManager, OrderManager, ManagerFromOrderQuerySet, Manager, …]，super() 绑定 __class__ = OrderManager ⇒ 跳过排在前面的 RelatedManager.get_queryset()（django/db/models/fields/related_descriptors.py:755），而「本游戏」的 FK 条件由它在同文件 :707 的 _apply_rel_filters() 里用 :690 设好的 core_filters（{field.name: instance}）叠上去；纯 manager（Order.objects）没有 core_filters ⇒ getattr(..., None) 取到 None、与从前一致。"""
        qs = super().get_queryset().filter(deleted_at__isnull=False)
        # ⚠️ `super().get_queryset()` 在反向 manager（`game.orders`）上会跳过 FK 条件
        # （Django 的 `RelatedManager.core_filters`），必须补回来，否则把别的游戏的行一起放出来。
        core_filters = getattr(self, "core_filters", None)
        return qs.filter(**core_filters) if core_filters else qs


class Order(models.Model):
    """订单。每个租户 schema 各有一份，无 tenant_id 列。"""

    # --- 标识 ---
    order_no = models.CharField("订单号", max_length=20, unique=True, editable=False)
    # 不设 db_index：Meta 里的复合索引 `(status, -created_at)` 的最左前缀就是 `status`，
    # 单列那棵 b-tree 只是每次写入时多维护一棵树（现在删免费，上线后重建要锁表）。
    status = models.CharField(
        "状态",
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.UNASSIGNED,
    )
    order_type = models.CharField(
        "订单类型", max_length=20, choices=OrderType.choices, blank=True
    )
    #: ⭐ 拆单组 ID（拆单）。空串 = 不是拆单（语义开关）；非空 = 同一拆分组的成员（组 ID 组内共用同一个值）；
    #: 取值形状复用 order_no.generate_order_no() 的产出（20 位纯数字）、一次请求内生成一次、组内共用；⚠️ 它是组标识、不是订单号 ⛔ 不许拿它当订单号用。
    #: ⛔ 刻意不加 is_split 布尔列（同一事实不要两列判据）、⛔ 也不加父子外键——本线的「主单」是显示层概念（组内 id 最小者）、不是数据模型上的父子方向（对标实测同样看不到父子方向）；⛔ 排序一个字不改（Meta.ordering 仍是 -created_at, -id）：同组的行靠列表读面的折叠+显式展开聚在一起、不靠排序位置。
    split_id = models.CharField(
        "拆单组标识", max_length=32, blank=True, default="", db_index=True
    )
    split_kind = models.CharField(
        "拆单组类型", max_length=12, blank=True, default="", db_default="", editable=False
    )
    # 即使主单软删，仍按原主单的来源号发货并归档成功记录。
    split_ship_main_order_no = models.CharField(
        "拆单发货主订单号", max_length=20, null=True, blank=True, editable=False
    )

    penalty_approval = models.OneToOneField(
        "Approval", on_delete=models.PROTECT, null=True, blank=True, editable=False,
        related_name="generated_order", verbose_name="来源罚款审批",
    )

    # --- 关联配置（PROTECT：配置被删不得带走历史订单）---
    game = models.ForeignKey(
        Game, on_delete=models.PROTECT, related_name="orders", verbose_name="游戏"
    )
    server = models.ForeignKey(
        GameServer,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
        verbose_name="区服",
    )
    category = models.ForeignKey(
        ProjectCategory,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
        verbose_name="项目分类",
    )
    # D9：可空 FK + 自由文本，**以 project_text 为准**
    project = models.ForeignKey(
        Project,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
        verbose_name="代练/陪玩项目",
    )
    # 上限 900 是有意偏离对标录单页的 220（D-0218，已取代早先的 300）。
    # 不是列宽写错：别改回 220 或 300。跟随列宽的地方与这一列同一次改。
    project_text = models.CharField(
        "项目名称（自由文本，权威值）", max_length=900, blank=True
    )

    # --- 订单内容 ---
    content = models.TextField("客户原始需求", blank=True)
    demand = models.TextField("需求描述（打手看的）", blank=True)
    time_requirement = models.CharField("时间要求", max_length=64, blank=True)

    # --- 玩家账号（D6：无密码字段）---
    game_account = models.CharField("游戏账号", max_length=128, blank=True)
    game_sub_account = models.CharField("子账号", max_length=128, blank=True)
    game_role_name = models.CharField("角色名", max_length=128, blank=True)
    # 业务方要求保留但未定义语义（spec §3.2.1）——不要给它俩赋予业务含义
    current_info_1 = models.CharField("当前信息1", max_length=128, blank=True)
    current_info_2 = models.CharField("当前信息2", max_length=128, blank=True)

    # --- 客户快照（D7：内嵌，不建客户表）---
    customer_phone = models.CharField("联系电话", max_length=64, blank=True)
    customer_qq = models.CharField("QQ/微信", max_length=64, blank=True)
    customer_ww = models.CharField("旺旺/YY", max_length=64, blank=True)
    #: 客户（会员）档案，身份键 = 闲管家 `buyer_eid`（2026-11-10 客户线）。可空：
    #  手工录单 / 开放平台 / 同步面的单没有买家标识，如实不连；SET_NULL：删档案不带走订单。
    customer = models.ForeignKey(
        "Customer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
        verbose_name="客户（闲鱼买家档案）",
    )
    #: 新老客标签的**录单时快照**（`CustomerKind`）：同来源 + 同客户有未删除历史单 ⇒ 老客，否则新客。
    #: 空串 = 判不出（无客户档案 / 无订单来源）。快照语义：后来的单不回改历史标签。
    customer_kind = models.CharField(
        "新老客标签",
        max_length=8,
        choices=CustomerKind.choices,
        blank=True,
        default="",
        db_default="",
    )
    #: True = 这一单的标签由录单时的 `customer_kind_override` 手动指定（优先于自动判定）。
    customer_kind_is_manual = models.BooleanField(
        "新老客标签为手动指定", default=False, db_default=False
    )
    wecom_notice_journal = models.JSONField(default=list, db_default=[], blank=True)
    wecom_notice_pending = models.BooleanField(default=False, db_default=False, db_index=True)

    # --- 价格（D2：一次建全；金额一律 Decimal；**可以为负**，见上方金额注释）---
    total_price = models.DecimalField(
        "订单总价",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    received_price = models.DecimalField(
        "接单实收款",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    platform_price = models.DecimalField(
        "发平台价格",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    hall_price = models.DecimalField(
        "发内部抢单大厅价",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    refund_fee = models.DecimalField(
        "累计退款额",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    fee = models.DecimalField(
        "产生的手续费",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    payment_method = models.CharField("支付方式", max_length=32, blank=True)
    payment_status = models.CharField(
        "收款状态",
        max_length=16,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNSETTLED,
    )
    #: ⭐ D-0359（上家收款同步）：连续「上家查无此单」的次数（①式查无 / 已退单、
    #: peer 对端查无都算——退单后不可能再结账，同一档收敛）。计数 ≥
    #: `RECEIPT_SYNC_NOTFOUND_GIVEUP`（默认 3）⇒ **夜间**候选查询不再收该单；⚠️ 列不清零、无清零写入口：到期语义由手动按钮兜底（手动跑**不看**这一列——显式动作优先，计划 §2.3）。⚠️ 只有「查无」这一档递增；查到行 / 未结 / 出错都不动它。
    receipt_sync_notfound_count = models.IntegerField("上家收款查无计数", default=0)

    # --- 归属与备注 ---
    source = models.CharField("订单来源", max_length=64, blank=True)
    # D3（订单来源字典化）：来源字典的可空外键。**刻意不给 PROTECT**——业务方 R7 要
    # 「允许删除来源」，删除后 FK 置空、`source` 名字快照留下（D5）。
    # ⚠️ 写入语义（三条 + 不变式 D7）的**唯一出处**是 `apps/business/order_source.py`的 `source_snapshot_overrides()`：create 与 PATCH 共用它，不要在视图里各写一遍。
    order_source = models.ForeignKey(
        "business.OrderSource",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
        verbose_name="订单来源字典",
    )
    # spec §3.2.2：P2 不建唯一约束，只建普通索引便于检索
    source_order_no = models.CharField(
        "来源单号", max_length=128, blank=True, db_index=True
    )
    # 保留同一次下家请求，直到对端明确确认成功或拒绝。
    peer_direct_pending = models.JSONField("下家直派待确认", null=True, blank=True)

    #: ⭐（业务方拍板）：镜像卡派生的隐藏单——非空 = 这张单是团团通大厅里那张「上家推来的镜像卡」（PeerHallMirror）在本租户的订单投影。
    #: 它存在的唯一目的是：大厅里有单（含镜像卡）时、也把这张单同步给所有开了 JidanpuMemberBinding.hall_sync_enabled 的下家鸡蛋铺（jidanpu_member.queue_hall_launch 的闸门只有一处 ⛔ 这里不另写过滤）；⚠️ 它不得出现在面向人的订单读面上（订单管理列表/统计/导出统一 exclude(hall_card__isnull=False)；详情按单号仍可打开）。
    #: 生命周期（判据见 apps/business/hall_shadow.py）：卡落成 listed（推送支/拉取支共用 _land_hall_throw）⇒ 建（状态 WAITING_GRAB）；本租户打手把卡抢走、卡转正 ⇒ 同一张单接着用（_promote_claiming_mirror：挂打手/写展示价/建绩效行/IN_PROGRESS、并清空本列——转正后它就是一张正常的我方订单、必须回到列表里）；卡 gone（上家摘牌/关开关/解绑/卡死回收按证据下架）⇒ 本单落 REFUNDED（清空前仍算隐藏单：退掉的派生单不该在列表里冒充一笔真退单）；SET_NULL：镜像卡行被删（人工/数据修复）不得把订单一起带走。
    hall_card = models.OneToOneField(
        "PeerHallMirror",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shadow_order",
        verbose_name="来源的团团通大厅镜像卡（隐藏单专用）",
    )
    #: ⭐（D-0325，业务方拍板）：上家鸡蛋铺镜像卡派生的隐藏单——非空 = 这张单是上家大厅镜像行（JidanpuUpstreamHallOrder）在本租户的订单投影；与 hall_card（peer 镜像卡那条线）并列且互斥：一张隐藏单要么是 peer 卡派生、要么是上家镜像派生（两列同时非空 = 数据被手改；两条线零关系口径、各自的生命周期唯一入口保证互斥）。
    #: 它存在的唯一目的是：上家镜像卡挂在我方大厅期间、把这张单同步给所有开了 JidanpuMemberBinding.hall_sync_enabled 的下家鸡蛋铺（复用 queue_hall_launch；⚠️ 硬闸：绝不推回该卡的来源上家地址，upstream_hall_shadow 传 exclude_addresses）；⚠️ 它不得出现在面向人的订单读面上（与 hall_card 同一批 exclude(...isnull=False) 读点，登记：tests/test_order_read_path_net.py）。
    #: 生命周期（判据见 apps/business/upstream_hall_shadow.py）：镜像行落成 listed（新建/gone 复活/skipped→listed）⇒ 建（WAITING_GRAB）或复活同一张、并抛给合格下家绑定，展示价 <= 0（0 元卡/负价卡）⇒ 不派生、只记台账（D-0325）；我方打手接卡转正 ⇒ 同一张单接着用（promote_upstream_mirror 复用优先支：挂打手/建绩效行/IN_PROGRESS、并清空本列——转正后它就是我方真单、回到列表）；镜像行 gone（消失/被别家接走/回环拦截/僵尸卡下架/接单被拒）⇒ 本单落 REFUNDED（清空前仍算隐藏面：退掉的派生单不该在列表里冒充一笔真退单）；SET_NULL：镜像行被删（人工/数据修复）不得把订单一起带走。
    upstream_hall_card = models.OneToOneField(
        "JidanpuUpstreamHallOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="shadow_order",
        verbose_name="来源的上家鸡蛋铺镜像卡（隐藏单专用）",
    )
    sales_agent = models.CharField("接单销售客服", max_length=64, blank=True)
    # 销售提成归属人（P4-1）。`sales_agent` 是**文本快照**（P2 无用户表时的做法），
    # 本字段才是真正的用户引用；两者语义不同，不要合并。
    # PROTECT：删用户不得带走历史订单（与 game/server 等配置外键同一口径）。
    sales_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sales_orders",
        verbose_name="销售提成归属人",
    )
    is_urgent = models.BooleanField("是否加急", default=False)
    remark = models.TextField("重要备注", blank=True)
    tag = models.CharField("自定义标记", max_length=128, blank=True)
    #: ⭐（第三方平台订单管理页「更改标签」）：订单标签。
    #: 对标把「标签」（/OrderAjax/UpdateOrderLabel 的 label 文本、候选来自该游戏的标签字典 /SystemAjax/GetOrderLabelList）与「标记」（upOrderTips 的自由文本）当两件事；本仓此前只有后者（tag）；合成一列会让页面上的「改标签」与「改标记」互相覆盖 ⇒ 两个按钮做同一件事、所以这里新开一列。
    #: ⚠️ 本版只落到写入：本页 25 列是像素级复刻（对标这一页没有标签列）、订单列表的标签列早在 D-0093 被业务方裁掉 ⇒ 没有读面；将来要展示/筛选它时必须同一次补读面 + 「这一列由谁写」的判据（AGENTS.md §5-G 的列写入契约）⛔ 不要只挂一个显示列。blank + 默认空串 = 「没有标签」；长度 64 是本仓自定（对标没给列宽、取 tag 的一半、足够装「微」「包上分」这类短标签）。
    order_label = models.CharField("订单标签", max_length=64, blank=True, default="")

    # --- 时间与操作人（P2 无用户表，操作人先存文本，见 spec §10.1 #8）---
    # 上限 150 = `User.username` 的 `max_length`。这一列装用户名
    # （录单写 `request.user.username`）。更窄会让长用户名录单时 500。
    created_by = models.CharField("创建人", max_length=150, blank=True)
    updated_by = models.CharField("最后修改人", max_length=150, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)
    # 「删除」= **软删**（业务方裁定，D-0144；对标是软删，见 spec §2.1.3）。
    # 行**留在库里** + 打删除时间；`deleted_by` 是**快照用户名**，与 `created_by` / `updated_by`
    # 同形同宽（`CharField(150, blank=True)`）——不设 FK（删账号不得带走历史留痕）。⛔ 行只增不减：**不做** purge（D1），清理期见 spec §9-R4。
    deleted_at = models.DateTimeField("删除时间", null=True, blank=True)
    deleted_by = models.CharField("删除人", max_length=150, blank=True)
    # 完单时间（管理订单页「完单时间」列）。写入点只有一个：`apply_transition` 里
    # `target == COMPLETED` 写、`from_status == COMPLETED` 清（spec §6）。
    # ⚠️ 该函数的 `save(update_fields=[...])` **只写列出的列**——新列不加进去就是「值只在内存对象上、没落库」的静默丢弃（本项目最忌的失败形态）。
    completed_at = models.DateTimeField("完单时间", null=True, blank=True)
    # 发单时间（报酬页「发单时间」档筛的就是它）= 这张单第一次被发出去给打手的那一刻（业务方拍板选 A：「抛到内部抢单大厅」或「第一次接手」、取先发生的）。
    # 写入点两处、逐条枚举（grep 实测；⛔ 新增第三条写路径时必须回来核）：①抛到大厅：order_status.apply_transition 里 hall_publisher is not None 那一支——/throw-to-hall/、通用 /transition/（目标 WAITING_GRAB）、录单即抛单三条路都经它、所以收口一处覆盖三条；②第一次接手：claim.py::claim_order（/claim）与 performance.py::set_taker（/assign、上家线同步指派）——写的是与同行 claimed_at 同一时刻。
    # ⚠️ 只写第一次（if published_at is None）：重抛/改派都不覆盖（与 hall_publisher 的「保留最后一次」有意不同：这一列是筛选维度、语义是「这张单最早什么时候发出去的」）。⚠️ 清理路径 = 无（与上面 completed_at 离开已完成会清有意不同）：撤回/回池/退单都不清它——清掉会让历史按这一档筛不出来。⚠️ 该函数的 save(update_fields=[...]) 只写列出的列——新列不加进去就是「值只在内存对象上、没落库」的静默丢弃（本项目最忌的失败形态）。
    published_at = models.DateTimeField("发单时间", null=True, blank=True)
    # 交单时间（报酬页「交单时间」档筛的就是它）= 订单第一次进入「待验收」（PENDING_REVIEW）的那一刻（业务方拍板选 A：新增订单列 + 从操作日志回填历史）。
    # 写入点一处：order_status.apply_transition 里 target == PENDING_REVIEW 那一支——它是「进状态」的唯一收口（/submit/、/transition/、/batch/transition/ 与任何内部路径都经它）⇒ 写在这里一处覆盖全部入口；⛔ 不要在三个视图里各写一遍（同一事实两处判据必然漂移，E-0021 的形状）。
    # ⚠️ 只写第一次（if submitted_at is None）：重进「待验收」不覆盖；⚠️ 清理路径 = 无（与 completed_at 有意不同）：已验收/已完成/已退单都不清它——这一列是筛选维度、清掉会让历史按这一档筛不出来；⚠️ 同上：必须列进 apply_transition 的 update_fields。
    submitted_at = models.DateTimeField("交单时间", null=True, blank=True)
    # 发单人 = 最后一次发单动作的那个人（管理订单页「发单人」列）。
    # 写入点两处（D-0369：指派（含改派）也算发单动作，保留最后一次）：
    #   ① 抛大厅：`order_status.apply_transition` 的 `hall_publisher` 漏斗参数（`throw_to_hall` / 通用 `/transition/` 目标 `WAITING_GRAB`，认证态用户）；② 指派 / 改派：`order_writes.assign_order` 的直写点（`ctx.user` = 操作人；机器 / 开放平台路径 ctx `user=None` ⇒ 天然不写）。**清理路径 = 无**（业务方裁定 8「保留最后一次」）：撤回 / 退款 / 回池 /抢单都不动它，再抛一次或再指派一次则覆盖成新的人。PROTECT：删用户不得带走历史订单（与 `sales_user` 同一口径）。
    hall_publisher = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_orders",
        verbose_name="发单人（抛到大厅的人）",
    )

    #: 马上来查码队列的持久退避（D-0356）：「在此之前不再自动查码」的截止时刻。
    #: 与入队时铺的进程内 cache 冷却并存、互不替代——cache 是快路径防连击、本列是跨进程持久的兜底（定时命令每分钟是新进程、看不见别的进程的 cache）。
    #: ⭐ 业务方「撤退避」口径：普通失败一律不写（每轮全量逐个问本身就是节流、下一轮自然重试）；本列唯一写入点 = apps/business/order_qr_fetch.py 的 _suppress_fetching、仅对端 429 结局调它、写 max(now + 60s, now + retry_after)（无 Retry-After 头时兜底 60s）⛔ 不把对端给的窗口压小。没有清零写入口（有意）：到期即自然失效；查到后候选查询本就不再选中该单、残留的旧时刻无害；缺省 None = 从未退避过；判据一律用 isnull / 与时刻比较 ⛔ 不要拿真值判断（E-0021 同族：时刻零值不进本列）。
    qr_fetch_suppress_until = models.DateTimeField(
        "查码退避截止", null=True, blank=True, default=None
    )

    #: 默认（排除已删）与逃生口（**看得到已删**）。⚠️ `all_objects` 的使用点是**列名白名单**、
    #: 不是惯例：当下 grep 实测命中 `selectors.py`（回收站底集）与 `api/order_views.py`
    #: （按主键取已删单）；使用点由 `tests/test_order_read_path_net.py` 的登记网钉住（多一处少一处都会红）。
    objects = OrderManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = "订单"
        verbose_name_plural = "订单"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["status", "-created_at"], name="idx_order_status_ct"
            ),
            # 回收站列表的形状是「deleted_at IS NOT NULL + ORDER BY deleted_at DESC」⇒ 用**部分索引**。
            # ⛔ 不给 deleted_at 加 db_index=True：全表绝大多数行是 NULL，普通 b-tree 对 `IS NULL`
            #    没有选择度，只是每次写入多维护一棵树。
            models.Index(
                fields=["-deleted_at"],
                name="idx_order_deleted_at",
                condition=models.Q(deleted_at__isnull=False),
            ),
            models.Index(
                fields=["split_ship_main_order_no"],
                name="idx_order_split_ship_main",
                condition=models.Q(split_ship_main_order_no__isnull=False),
            ),
            # 新老客自动判定：同来源 + 同客户 + 未删除，按 id 判「有没有历史单」。
            models.Index(
                fields=["order_source", "customer", "id"],
                name="idx_order_kind_lookup",
            ),
        ]

    def __str__(self):
        return self.order_no


class Customer(models.Model):
    """闲鱼买家客户档案（身份键 = 闲管家 `buyer_eid`，闲鱼体系内唯一的用户标识；
    依据 docs/XGJ-INTEGRATION.md §5 与 2026-11-10 业务方拍板「不许按昵称去重」）。每个租户 schema 一份。
    ⛔ 不建订单数 / 消费额 / 末单时间等聚合列——那些一律查询期从订单聚合，避免「第二份事实」；
    电话等仍在订单快照里，行展示取「筛选范围内最近一单」的值。`buyer_nick` 上限 128 是余量
    （上游昵称长度无保证），写入点一律裁到列宽、不因昵称挡单。"""

    eid = models.CharField("闲鱼买家唯一标识", max_length=64)
    buyer_nick = models.CharField("买家昵称最新快照", max_length=128, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后更新时间", auto_now=True)

    class Meta:
        verbose_name = "客户（会员）"
        verbose_name_plural = "客户（会员）"
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["eid"], name="uniq_customer_eid"),
        ]

    def __str__(self):
        return f"客户:{self.buyer_nick or self.pk}"


class CustomerSourceOrder(models.Model):
    """闲管家来源订单快照（**没进系统**的老订单，2026-11-11 客户线第五批）。

    唯一写入方 = 收割运维脚本（经 `customers.upsert_source_order`，契约网钉写入点）。
    进抽屉与会员行合并统计（第六批：口径唯一实现 `customers.valid_source_orders`——
    剔已退款/已关闭 + 在库系统单同号者，同单两面只算一张）；分析报表仍只认系统订单。
    CASCADE：客户删除功能今天不存在；真做删除时来源单随档案走（没有孤儿快照的意义）。
    """

    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="source_orders", verbose_name="客户"
    )
    source_order_no = models.CharField("来源单号（闲管家侧，租户内唯一）", max_length=64)
    order_status = models.SmallIntegerField("来源单状态（上游原值）", default=0)
    total_amount = models.DecimalField(
        "金额（元）", max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    updated_time = models.DateTimeField("来源侧更新时间", null=True, blank=True)
    shop_name = models.CharField("来源店铺名", max_length=128, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后更新时间", auto_now=True)

    class Meta:
        verbose_name = "客户来源订单"
        verbose_name_plural = "客户来源订单"
        ordering = ["-updated_time", "-id"]  # ⚠️ Postgres DESC 下 NULL 排最前；读面另用 nulls_last
        constraints = [
            models.UniqueConstraint(
                fields=["source_order_no"], name="uniq_customer_source_order_no"
            ),
        ]

    def __str__(self):
        return f"来源单:{self.source_order_no}"


class EmpType(models.TextChoices):
    """绩效的收入类型。取值沿用 docs/GLOSSARY.md 的口径。
    对标物接口返回的是「效绩提成/接单提成」（读码，见 JIDANPU-REFERENCE §6.6.1）、那是它自己的用词；我们用代练员端的叫法。"""

    MAIN = "打单工资", "打单工资"        # 代练员的
    SALES = "销售提成", "销售提成"        # 客服的


class ReviewStatus(models.TextChoices):
    PENDING = "待审核", "待审核"
    APPROVED = "已通过", "已通过"
    REJECTED = "已驳回", "已驳回"


class SettleStatus(models.TextChoices):
    UNSETTLED = "未结账", "未结账"
    SETTLED = "已结账", "已结账"


class Performance(models.Model):
    """绩效：某个订单 × 某个人产生的应得报酬记录（spec §3.1）。
    一个订单只能被一人接手由 uniq_order_main_taker 这条部分唯一索引保证：它只约束「打单工资」行、所以客服的「销售提成」行可以并存（D-0011 / D-0012）。"""

    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        related_name="performances",
        verbose_name="订单",
    )
    # 为空 = 还没有归属人（代练员那条在抢单/指派前一直是空的）
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="performances",
        verbose_name="绩效归属人",
    )
    # 接单时刻。写 `user`（抢单或指派）必须同一次写它，清 `user` 必须同一次清它。
    # 不要当成「有 user ⟺ 有 claimed_at」：存量行可以无主而有时刻，历史行也可以为空。
    # 远端接手现在直接删掉无主 MAIN 行，不再新写这种时刻。可空，不要回填猜出来的时刻（D-0092）。客服行不写它：那里的 user 是销售归属人。不要加「有 user 就必须有 claimed_at」的库约束：存量行过不了，改价 / 审核 / 结账也不动这一列。
    claimed_at = models.DateTimeField("接单时刻", null=True, blank=True, db_index=True)
    emp_type = models.CharField(
        "收入类型", max_length=16, choices=EmpType.choices, default=EmpType.MAIN
    )
    temp_amount = models.DecimalField(
        "预计绩效", max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    bonus_amount = models.DecimalField(
        "生效绩效", max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    deduct_mode = models.CharField("扣点模式", max_length=1, blank=True, default="")
    deduct_value = models.DecimalField(
        "扣点值", max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    taker_price = models.DecimalField(
        "接单价覆盖值", max_digits=12, decimal_places=2, null=True, blank=True
    )
    #: 对标 assignation.js 的 hours「时间要求(小时)」：每人一份、不是订单级的 Order.time_requirement（那是自由文本 CharField(64)、装得下「24小时」「当天」这类非数字——把它当整数小时解析会在写入时造出解析失败分支、所以本批两列各管各的）。
    #: ⚠️ 可空 = 「没填 / 已被清除」；下界 1 加在入口序列化器的 min_value=1 上（对标原文：时间要求（小时）最小为1小时，请重输）、不在列上。
    #: ⛔ 不加 CheckConstraint(hours >= 1)：清除接单人时要把它清空 ⇒ 列上必须有「空」这个状态、而 DB 约束既拦不住「填了 0」（NULL 不参与比较）也留不住「清除」。
    hours = models.PositiveIntegerField("时间要求（小时）", null=True, blank=True)

    #: 对标 `assignation.js` 的 `project`「接手项目(点击可单独设定)」：**每人一份**。
    #: ⚠️ 不是 `Order.project_text`（订单级）也不是 `PerformanceListSerializer.dl_project`
    #: （那个只读键 = **订单**项目）。宽度取 128，与 `Order.project_text` 同（`Order.project_text` 后已放宽到 900，D-0218；本列有意不动）。⭐ 900（D-0218 漏面补齐 / E-0239）：本列是「接手项目」的**指派快照**，预填值来自 `Order.project_text`（已 900）⇒ 指派弹窗对 129–900 字的单此前必撞 128 被拒。
    project = models.CharField("接手项目", max_length=900, blank=True, default="")

    # P4-2：**提成率快照**（百分数，如 12.34 表示 12.34%）。
    # 可空 = 「还没读到/不适用」；`is not None` 才是「有值」的判据——
    # 0 与负数在这里同样有意义（D-0015 的口径），不要用真值判断（E-0021）。⚠️ 这是**只有一列**的追加：不动下面三条约束与两条既有 `Meta.permissions`。
    commission_rate = models.DecimalField(
        "提成率（%）", max_digits=5, decimal_places=2, null=True, blank=True
    )
    review_status = models.CharField(
        "审核状态",
        max_length=8,
        null=True,
        blank=True,
        default=None,
        choices=ReviewStatus.choices,
    )
    # ⚠️ 同上：装的是**用户名**（`/review` 写认证态 `request.user.username`）⇒ 150（Ruling 8）。
    reviewed_by = models.CharField("审核人", max_length=150, blank=True)
    reviewed_at = models.DateTimeField("审核时间", null=True, blank=True)
    settle_status = models.CharField(
        "结账状态", max_length=8, default=SettleStatus.UNSETTLED, choices=SettleStatus.choices
    )
    # P4-FE-0：结算记录页的「结算人 / 结算时间」两列（对标 /Emp/billsummary）。
    # ⚠️ 镜像上面 reviewed_by/reviewed_at 那一对的命名与类型（不发明新词）：settled_by 装的是用户名（settle_performances(..., actor=) 的入参）、长度 150 同 reviewed_by；settled_at 可空 = 「还没结过账」。
    # ⚠️ blank=True / null=True 都是为了让迁移可空、无需回填（存量行 = 「未结账 ⇒ 无结算人」）。⛔ 不加索引：结账路径只按 id 批量 UPDATE、没有「按结算人/时间筛」的查询需求（结算列表的筛选维度见 SettlementSearchSerializer、里面没有这两列）。
    settled_by = models.CharField("结算人", max_length=150, blank=True)
    settlement_method = models.CharField(max_length=16, blank=True, default="", db_default="")
    online_payroll_batch = models.ForeignKey(
        "OnlinePayrollBatch", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reserved_performances",
    )
    settled_at = models.DateTimeField("结算时间", null=True, blank=True)
    # P4-FE-4：结账那一刻这条绩效的实结金额快照（业务方选定方案 B）。
    # 语义 = settlement_amount.payable_net(...) 在本行被结账那一刻的值（bonus_amount − 扣点额 + 当时该行的全部未结账调整记录）。为什么需要它：应付净额只算未结账调整、而结账会把调整翻成「已结账」⇒ 结完再看同一行、当初参与结算的那笔调整不再计入（佣金 100 + 一条 −30 的冲减、实付 70、结账后活值变回 100）；结算记录页是财务对账/留痕用的页、历史数字不该随后来的调整变动、所以抄一份存下来。
    # ⚠️ 可空（null=True）：未结账的行没有快照、存量行也没有 ⇒ 迁移是「加一个可空列、无需回填」。⛔ 不加任何 ≥0 校验 / min_value / CHECK（D-0015）：单行净额可以为负（同一归属人的组和 ≥0 才放行、所以组里可以有一行是负的）⇒ 快照必须能存负数。⚠️ 只由 settle_performances 第 5 步写、且只写本轮真的结掉的行：「只结调整记录」时已结账行不在其中、它的快照与 settled_at/settled_by 一样不许被覆盖（P4-FE-0 同一条口径）。
    settled_amount = models.DecimalField(
        "实结金额", max_digits=12, decimal_places=2, null=True, blank=True
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)
    # 与订单**同批**软删：`isDelEmp="Y"` 时把该单的绩效行一起标记（spec §6.1），
    # 随订单一起恢复（spec §6.2 的对称性）。两列同形同宽，理由同上。
    deleted_at = models.DateTimeField("删除时间", null=True, blank=True)
    deleted_by = models.CharField("删除人", max_length=150, blank=True)

    class Meta:
        verbose_name = "绩效"
        verbose_name_plural = "绩效"
        ordering = ["-created_at", "-id"]
        indexes = [
            # 结算日汇总 / 来源分解两端点（`settlement_views.SettlementDailySummaryView`
            # / `SettlementDailySourcesView`）的聚合底表索引（D-0350，迁移 0126）：
            # 基集形状 = `settle_status='已结账'`（等值）+ `settled_at` 半开区间（范围、裸列），列序按 b-tree 惯例「等值在前、范围在后」。选型与锁窗口径见迁移 0126 的 docstring。
            models.Index(
                fields=["settle_status", "settled_at"],
                name="idx_perf_settle_settledat",
            ),
        ]
        constraints = [
            # ① 同一订单同一个人只能有一条绩效（NULL 在唯一约束里互不相等，
            #    所以「还没人接」的若干行不会互相冲突）
            models.UniqueConstraint(
                fields=["order", "user"], name="uniq_performance_order_user"
            ),
            # ② ⭐ 一个订单只能被一人接手：只约束「打单工资」行
            models.UniqueConstraint(
                fields=["order"],
                condition=Q(emp_type=EmpType.MAIN),
                name="uniq_order_main_taker",
            ),
            # ③ 没有归属人就不可能有审核状态。
            #    `review_status` 用 NULL 表示「还没有可审的东西」（spec §3.1），
            #    所以这里判的是 `IS NULL`，不是空字符串——空串在 CheckConstraint 里会被判成「有审核状态」，从而把「未接单」的合法行挡在门外。
            models.CheckConstraint(
                condition=Q(review_status__isnull=True) | Q(user__isnull=False),
                name="performance_review_needs_user",
            ),
        ]
        # 两条能改变金额的动作单独收口（spec §4.3）。
        # ⚠️ Meta.permissions 里的自定义权限原样使用给定 codename、不会被拼成 override_taker_price_performance——django/contrib/auth/management/__init__.py:19 是 [*_get_builtin_permissions(opts), *opts.permissions]、只有内建权限才经 get_permission_codename()（同文件 :22-35）⇒ 完整串是 business.override_taker_price / business.review_performance；有测试钉住字面值（tests/test_biz_permissions.py）。
        permissions = [
            ("override_taker_price", "可以改接单价"),
            ("review_performance", "可以审核绩效"),
        ]

    @property
    def is_settled(self):
        """这一行是否已结账。点名「已结账」的那几处用它、不是全仓唯一判法。三个字段取并集：状态已结账、或 settled_at / settled_amount 有值——只看状态列会把「已结账但没写时刻」或「状态改回未结账却留着快照」的脏行当成未结账；order_stats._unsettled_amount 与 settlement._reject_message 仍只看状态列、对「未结账却留着金额」的脏行会和本属性相反，统一它们是改钱的口径。
        放在模型上是为了多处引用又不让领域模块互相 import 成环；属性不是列、不产生迁移。"""
        return (
            self.settle_status == SettleStatus.SETTLED
            or self.settled_at is not None
            or self.settled_amount is not None
        )

    def __str__(self):
        return f"{self.order_id}-{self.emp_type}-{self.user_id or '未接'}"


class PerformanceSettleRequest(models.Model):
    """打手对「已完成·未结账」的绩效申请提前结算（spec §3.4）。⚠️ 申请不改变任何金额与状态（spec §3.4 第 7 条）：它只是给财务看的一条「这个人想早点拿这笔钱」的记录；真正的打款仍走财务的「改结算状态」（settlement.settle_performances）、那次结账顺带把本行置 settled（同一次事务）。
    performance 是 FK+唯一（实现成 OneToOneField——物理形状相同、且不触发 fields.W342，见字段声明处）：一单只能有一条申请——重复申请由数据库直接拒绝（接口层先查一次给出业务码、唯一约束是并发下的兜底，E-0022 同族的「谓词在库里重判」）。⚠️ user 是申请人=绩效行归属人（不是谁点的按钮）：额度按人算（settings.EARLY_SETTLE_LIMIT）、所以这一列必须自己存一份而不是每处 JOIN 回 performance.user——后者在「清除接单人」之后会变 NULL、那时申请人的额度就查不出来了；⚠️ user 用 PROTECT（账号是凭据、不作物理删除，全仓同口径）、performance 同样 PROTECT（绩效行是资金凭据，OrderRefund 那族同口径）。
    state 三档（spec 表）：pending（待财务处理）/ settled（财务结账时自动置）/ cancelled（预留：当前没有取消入口 ⛔ 不要凭空造一个）；额度按「申请过就算」计（spec §3.4 规则 1：不计状态）⇒ cancelled 也占额度。"""

    class State(models.TextChoices):
        PENDING = "pending", "待处理"
        SETTLED = "settled", "已结账"
        CANCELLED = "cancelled", "已取消"

    # ⚠️ 写成 `OneToOneField` 而不是 `ForeignKey(unique=True)`：两者建出的**物理形状完全相同**
    #    （外键列 + 唯一索引 + `PROTECT`），但后者会触发 Django 的 `fields.W342` 系统检查警告
    #    （`makemigrations` 当场报过）⇒ 用前者表达同一个不变式，`manage.py check` 保持零警告。它同时给出反向一对一访问器 `performance.settle_request`（列表载荷的`settle_requested_at` 读的就是它）。
    performance = models.OneToOneField(
        Performance,
        on_delete=models.PROTECT,
        related_name="settle_request",
        verbose_name="绩效",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="performance_settle_requests",
        verbose_name="申请人",
    )
    state = models.CharField(
        "状态", max_length=16, choices=State.choices, default=State.PENDING
    )
    #: 申请时刻。`db_index=True`：额度判定按人筛、列表按人排序，都是本列 / user 列。
    created_at = models.DateTimeField("申请时间", auto_now_add=True, db_index=True)
    #: 财务结账那一刻（`settlement.settle_performances` 与绩效行同一个 `settled_at`）。
    #: `null` = 还没结账（`pending` / `cancelled`）。
    settled_at = models.DateTimeField("结账时间", null=True, blank=True)

    class Meta:
        verbose_name = "提前结算申请"
        verbose_name_plural = "提前结算申请"
        ordering = ["-created_at", "-id"]
        indexes = [
            # 额度判定（`user` + 计数）与「我的申请列表」都走这一条。
            models.Index(fields=["user", "state"], name="perf_settle_req_user_state"),
        ]

    def __str__(self):
        return f"{self.performance_id}-{self.state}"


class OrderRefund(models.Model):
    """订单退款事件（P4-2；spec §3.2）。一次退款一行：request_id 是调用方的幂等键、同值第二次写入由数据库拒绝；唯一性只有一处来源：Meta.constraints 里具名的 uniq_order_refund_request_id（spec §3.2 逐字指定了这个名字）——⛔ 不要再给字段加 unique=True：两者不变式完全相同、同时存在会在该列上建出两棵唯一索引（外加 PostgreSQL 后端为 unique=True 的字符列生成的 ..._like 索引）纯属浪费、具名约束还多一个稳定引用名。
    ⚠️ amount 没有任何下界约束（D-0015 / spec §3.9）：退款/冲抵/调整的金额可以为负——不要给它加 MinValueValidator / CheckConstraint(>=0) / 序列化器 min_value，加回去就会把业务需要的金额重新挡死（那正是 D-0015 要修的缺陷）。
    ⚠️ 不声明 Meta.permissions（Ruling H / D-0028）：P4 的权限闸门等 RBAC 基座、判据走 apps/rbac 的 has_node(code)、不新增 Django codename。"""

    class Kind(models.TextChoices):
        PARTIAL = "PARTIAL", "部分退款"
        # ⚠️ **D-0181起 `FULL` 不再有新行**：退单不再建
        # `OrderRefund(kind=FULL)` 事件行（含上家跟退）。取值**保留**——历史行仍在用，
        # 删它要做数据迁移。
        FULL = "FULL", "全额退款"
        # P4-M2（业务方选定候选 A「回池时把累计退款额复位，把重做当成新的一轮」）：
        # 退单 → 回池那一刻冲销**上一轮**的累计退款额（`amount = -复位前的 refund_fee`）。
        # 它保住 `refund_fee = Σ OrderRefund.amount`。全额退款不再写新行（D-0181），今天由部分退款和本冲销这对行守住；复位只发生在回池。⛔ 只**追加**取值：上面两个是 P4 以来的对外契约，不动它们的取值/语义。
        REVERSAL = "REVERSAL", "退款冲销（退单回池）"
        # ⭐ （D-0312）：部分退款的入参从「本次退款额（累加）」
        # 改成「**累计退款总额**（绝对值）」⇒ 台账记的是**带符号的差额** `total − 旧累计`。
        # 带符号是硬要求：`refund_fee = Σ OrderRefund.amount`（类 docstring / spec §3.1）靠它才成立（例如已退 500 再设成 50 ⇒ 新行 `-450`）。⛔ 只**追加**取值：上面三个是 P4 以来的对外契约，不动它们的取值/语义。
        SET = "SET", "设置累计退款额"

    # PROTECT（spec §3.7）：订单不作物理删除，退款记录是资金凭据。
    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        related_name="refunds",
        verbose_name="订单",
    )
    kind = models.CharField("退款类型", max_length=16, choices=Kind.choices)
    # ⚠️ 只有 DecimalField，**没有** validators、没有 CHECK（见类 docstring）。
    # ⭐ （D-0312）：`kind=SET` 的行记的是**带符号差额**（`新累计 − 旧累计`），
    # 因此同一张单上这一列可能有正有负；`Σ amount == Order.refund_fee` 仍是硬不变量。
    amount = models.DecimalField("退款金额", max_digits=12, decimal_places=2)
    # 必填：退款必须说明原因（`blank=False`，不留空默认）。
    reason = models.CharField("退款原因", max_length=255)
    operator = models.CharField("操作人", max_length=64, blank=True)
    # 幂等键：64 位足够容纳 UUID + 前缀。唯一性**只由 `Meta.constraints` 的具名约束**保证
    # （⛔ 不要加 `unique=True`：会变成两棵唯一索引，见类 docstring）。
    request_id = models.CharField("请求幂等号", max_length=64)
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "订单退款"
        verbose_name_plural = "订单退款"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["request_id"], name="uniq_order_refund_request_id"
            ),
        ]
        # ⛔ 刻意**没有** `permissions = [...]`（Ruling H / D-0028）。

    def __str__(self):
        return f"{self.order_id}-{self.kind}-{self.amount}"


class LegacySplitRefundPolicy(models.Model):
    """记录从零开始采用组内退款口径的旧式拆单子单。"""

    order = models.ForeignKey(Order, on_delete=models.PROTECT)
    split_id = models.CharField(max_length=32)
    refund_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['order', 'split_id'], name='uniq_legacy_split_refund_policy')]


class PerformanceAdjustment(models.Model):
    """绩效调整事件（P4-2；spec §3.3）：退款联动冲账 + 财务手工调整。一次调整一行，amount 是带符号的增减量（正=加、负=减）。
    ⚠️ amount 没有任何下界约束（D-0015 / spec §3.9）——同 OrderRefund，不要加 MinValueValidator / CheckConstraint / 序列化器 min_value。
    ⚠️ 不声明 Meta.permissions（Ruling H / D-0028）。"""

    class Kind(models.TextChoices):
        PARTIAL_REFUND = "PARTIAL_REFUND", "部分退款冲账"
        FULL_REFUND = "FULL_REFUND", "全额退款冲账"
        MANUAL = "MANUAL", "手工调整"

    # PROTECT（spec §3.7）：绩效是结算依据，调整记录不得因删绩效而消失。
    performance = models.ForeignKey(
        Performance,
        on_delete=models.PROTECT,
        related_name="adjustments",
        verbose_name="绩效",
    )
    # 可空：`MANUAL` 调整不挂在某次退款上。PROTECT：退款事件不可删，删了就对不上账。
    refund = models.ForeignKey(
        OrderRefund,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="adjustments",
        verbose_name="关联退款",
    )
    kind = models.CharField("调整类型", max_length=16, choices=Kind.choices)
    # 带符号的调整量；落列默认 0.00。同样**没有**下界约束（见类 docstring）。
    amount = models.DecimalField(
        "调整金额（带符号）",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    reason = models.CharField("调整原因", max_length=255)
    operator = models.CharField("操作人", max_length=64, blank=True)
    # 客户端幂等键（spec §7 的 POST 行要求幂等；spec §3.2 明令**不许**按值判重）。
    # 可空：退款联动生成的行没有客户端 `request_id`（它们各有自己的幂等来源：`OrderRefund`）。
    # 唯一性**只有一处来源**：下面的 `Meta.constraints` 那条具名约束（⛔ 不要再加 `unique=True`——P4-2 在 `OrderRefund` 上踩过「双机制 ⇒ 判别力实验全绿」）。
    request_id = models.CharField(
        "请求幂等键", max_length=64, null=True, blank=True
    )
    # 复用既有 `SettleStatus`（`未结账` / `已结账`）——**不新建枚举**（spec §3.3）。
    settle_status = models.CharField(
        "结账状态",
        max_length=8,
        default=SettleStatus.UNSETTLED,
        choices=SettleStatus.choices,
    )
    settled_at = models.DateTimeField("结账时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "绩效调整"
        verbose_name_plural = "绩效调整"
        ordering = ["-created_at", "-id"]
        # 📌 `request_id` 的唯一机制（P4-11 裁定 V）。**为什么没有 `condition=`**：
        #    PostgreSQL 的唯一索引对 `NULL` 默认 `NULLS DISTINCT` ⇒ 多条
        #    `request_id IS NULL` 的行能共存（退款联动会建多条），少一个能写错的从句。⛔ 与**金额**无关：`amount` 仍然一个字都没有约束（D-0015，见类 docstring）。⛔ **没有** `permissions`（Ruling H / D-0028）。
        constraints = [
            models.UniqueConstraint(
                fields=["request_id"], name="uniq_perf_adjustment_request_id"
            ),
        ]

    def __str__(self):
        return f"{self.performance_id}-{self.kind}-{self.amount}"


class AuditLog(models.Model):
    """历史审计留痕。生产代码不再写这张表：新动作走 apps/audit/log.py::record（OperationLog）；旧行已迁进新表、订单 /audit/ 读的也是新表、不要再给本表的 Action 加值。
    order_no 存字符串、performance_id 存裸整数、都不建外键：留痕不跟引用行一起消失。"""

    class Action(models.TextChoices):
        STATUS_CHANGE = "STATUS_CHANGE", "状态变更"
        PRICE_CHANGE = "PRICE_CHANGE", "改价"
        # P3b B1-6：审核绩效（spec §8.3 的「报单审核」→ 绩效审核）。
        # ⚠️ 追加取值要**走迁移**（choices 变了），且**不要动**前两个值（既有契约）。
        REVIEW = "REVIEW", "审核"
        # P4-3（spec §3.6）：结算发放是 `REQUIREMENTS.md` §5 规则 4 要求必须留痕的动作。
        # ⛔ 上面三个值是既有对外契约，**不要动**——只在后面追加。
        SETTLE = "SETTLE", "结账"
        REFUND = "REFUND", "退款"
        ADJUST = "ADJUST", "绩效调整"

    action = models.CharField("动作", max_length=20, choices=Action.choices, db_index=True)
    order_no = models.CharField("订单号", max_length=20, db_index=True)
    # P4-3（spec §3.6）：绩效是「订单 × 人」（D-0011），一条订单可以同时有两条绩效
    # （打手的 + 客服的）——只用 `order_no` 留痕会丢掉「结的是哪一条」。
    # ⚠️ **刻意不建 FK**（可空裸整数）：留痕不能被引用行阻塞（`PROTECT`），也不能因引用行消失而断链（`SET_NULL` 还会**改写**留痕）——与 `order_no` 存字符串同理。
    performance_id = models.IntegerField("绩效 id", null=True, blank=True)
    # 装的是用户名 ⇒ 150，与 `User.username` 同顶。
    actor = models.CharField("操作人", max_length=150, blank=True)
    reason = models.CharField("原因/备注", max_length=255, blank=True)
    from_value = models.CharField("变更前", max_length=64, blank=True)
    to_value = models.CharField("变更后", max_length=64, blank=True)
    created_at = models.DateTimeField("发生时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "审计留痕"
        verbose_name_plural = "审计留痕"
        ordering = ["-created_at", "-id"]


class NotificationKind(models.TextChoices):
    """站内通知的三类（spec §8.2）：**被指派 / 被抢走 / 待审核**。"""

    ASSIGNED = "被指派", "被指派"
    GRABBED = "被抢走", "被抢走"
    PENDING_REVIEW = "待审核", "待审核"


class Notification(models.Model):
    """站内通知（spec §3.5 / §3.6 / §8.2）。⚠️ 通知不保证送达、是提示而非凭据：收件人解析不到就不写行、将来还会按保留期（created_at）清理旧行；不要拿它当审计/结算依据——审计看 OperationLog（apps/audit，唯一写入出口 apps/audit/log.py::record(...)）、结算看 Performance；通知少一条不该让任何业务动作失败。
    外键删除策略由 spec §3.6 写死：user 是 CASCADE（通知随收件人消亡）、order 是 SET_NULL（通知不构成凭据、不该被订单阻塞）。
    payload 只放展示用信息（至少 order_no）——⛔ 不要把金额塞进去：那会让它看起来像一份可对账的凭据、而它明确不是。"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name="收件人",
    )
    kind = models.CharField("类型", max_length=16, choices=NotificationKind.choices)
    order = models.ForeignKey(
        Order,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="notifications",
        verbose_name="关联订单",
    )
    payload = models.JSONField("展示用载荷", default=dict, blank=True)
    read_at = models.DateTimeField("已读时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "站内通知"
        verbose_name_plural = "站内通知"
        ordering = ["-created_at", "-id"]
        indexes = [
            # 列表只按 `user` 过滤。未读角标按 `user` + `read_at IS NULL` 计数，所以留这个复合索引。
            models.Index(fields=["user", "read_at"], name="idx_notification_user_read"),
        ]

    def __str__(self):
        return f"{self.user_id}-{self.kind}-{self.order_id or '-'}"


class SystemNotice(models.Model):
    """系统内部公告（对标 /System/systemnotice 页）。整租户可见、没有已读态（与对标一致）：主页「通知和公告」卡片取最近 3 条，发布/编辑/删除只对持 system.publish_notice 的人开放。
    ⚠️ 对标弹窗只有「公告内容」一个输入框、title 由服务端从内容里派生（首行+截断）、不是用户填的字段；派生逻辑只有一处：api/notice_views.py::derive_title。
    ⚠️ 标称上限 1000 字照抄对标 maxlength="1000"；模型用 TextField 是因为公告是长文本、1000 的上限在序列化器里判（库层不强制）。"""

    title = models.CharField("公告标题", max_length=200)
    content = models.TextField("公告内容")
    created_by = models.CharField("发布人", max_length=150, blank=True, default="")
    created_at = models.DateTimeField("发布时间", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "系统内部公告"
        verbose_name_plural = "系统内部公告"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.title


class HomeQrCode(models.Model):
    """后台首页「二维码横幅」的配置行（自造功能、无对标；决策 D-0262）。业务方原话要点：首页顶部横幅三格——QQ 机器人（每租户至多一张）+ QQ 群（一个游戏一张、可多张、前端轮播）+ 微信公众号（每租户至多一张）；持 home.qr_manage 的人可增/改/删/停启用、其他人只能看；只存对象名（media_key）⛔ 不存 URL（同头像/反馈截图口径——URL 是签出来的、落库等于把会过期的链接写死）。
    约束（两条都落在库层、测试钉得住）：qq_bot / wechat_mp 每租户至多一条 = UniqueConstraint(condition=…)（uniq_home_qr_singleton_kind；API 层另有友好业务码 1001 前置判、库层这条是并发兜底）；qq_group 必须 game 非空、另两种必须为空 = CheckConstraint（chk_home_qr_game_matches_kind）——群码没有游戏名就没法轮播展示、固定两格带游戏列则是脏数据（API 层序列化器同批判、双保险）。
    外键删除策略（本线选定、写在代码里）：game 用 PROTECT（与 Order.game 同一款）——本仓「删除游戏」是软删（Game.deleted_at，D-0085）、正常路径根本走不到硬删；真要硬删时引用它的群码必须先由人删掉——群码的整个意义就是「这个游戏的群」、游戏没了还把码留在首页横幅上轮播比让它删不掉糟得多；⛔ 不用 SET_NULL（会产出一条「没有游戏的群码」、正是上面 CheckConstraint 禁止的形状）。created_by 是用户名快照（CharField、不是 FK）、口径同 SystemNotice.created_by：删账号不带走横幅配置。"""

    class Kind(models.TextChoices):
        QQ_BOT = "qq_bot", "QQ机器人"
        QQ_GROUP = "qq_group", "QQ群"
        WECHAT_MP = "wechat_mp", "微信公众号"

    #: 每租户**至多一条**的种类（另一条 `qq_group` 随便加，见模块 docstring 的约束说明）。
    SINGLETON_KINDS = (Kind.QQ_BOT, Kind.WECHAT_MP)

    kind = models.CharField("种类", max_length=16, choices=Kind.choices)
    game = models.ForeignKey(
        Game,
        null=True,
        blank=True,
        # 选择理由见类 docstring「外键删除策略」。
        on_delete=models.PROTECT,
        related_name="home_qr_codes",
        verbose_name="关联游戏（仅 QQ 群码用）",
    )
    media_key = models.CharField("媒体对象名", max_length=255)
    sort_order = models.PositiveIntegerField(
        "排序（数字小的在前）", default=0, validators=[MinValueValidator(0)]
    )
    is_enabled = models.BooleanField("是否启用", default=True)
    created_by = models.CharField("配置人", max_length=150, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "首页二维码"
        verbose_name_plural = "首页二维码"
        # 群码轮播顺序 = `sort_order` 升序（数字小的在前），同序按 id 稳定。
        # ⚠️ 与 `Game` 的「数字越大越靠前」**方向相反**且**有意**：Game 那条方向照抄对标，
        #    本表没有对标、取最直白的升序（`AGENTS.md` §5-C：不拿惯例当证据）。
        ordering = ["sort_order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind"],
                condition=Q(kind__in=("qq_bot", "wechat_mp")),
                name="uniq_home_qr_singleton_kind",
            ),
            models.CheckConstraint(
                check=Q(kind="qq_group", game__isnull=False)
                | (Q(kind__in=("qq_bot", "wechat_mp")) & Q(game__isnull=True)),
                name="chk_home_qr_game_matches_kind",
            ),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}-{self.game_id or '-'}-{self.media_key}"


class OrderPushSetting(models.Model):
    """自动发消息到微信群的规则（对标 /System/OrderPushGroupMsgSet）。一处有意偏离对标：对标只认企业微信群机器人的 webhookUrl（外加一个我们没有的内部抢单群聊 groupId）、本表把「发到哪里」抽成 channel：wecom = 企业微信群机器人（载荷 = 企微 text/markdown 格式）；custom = 任意 HTTP(S) Webhook（载荷是本文档约定的通用 JSON，见 apps/business/order_push.py 的模块 docstring）。
    timing 的取值照抄对标标签：0 / 10 / 20 / 30；我方 0 只发大厅（「重要变化」只匹配 30）、今天没有交易平台 ⇒ 20 不发；不要把 0 补成「大厅+重要变化」、否则这一档会多收一类事件。
    ⚠️ games 必填（对标「至少要选择一个游戏」）；空 = 不匹配任何订单、不是「全部游戏」。"""

    class Channel(models.TextChoices):
        WECOM = "wecom", "企业微信群机器人"
        CUSTOM = "custom", "自定义 Webhook"

    class Timing(models.IntegerChoices):
        HALL_AND_PLATFORM = 0, "发内部抢单大厅+交易平台时"
        HALL = 10, "只发内部抢单大厅时"
        PLATFORM = 20, "发交易平台时"
        IMPORTANT = 30, "交易订单产生重要变化时"

    class MsgType(models.TextChoices):
        MARKDOWN = "markdown", "Markdown 模式"
        TEXT = "text", "文本模式"

    name = models.CharField("规则名称", max_length=50, blank=True, default="")
    channel = models.CharField(
        "渠道类型", max_length=16, choices=Channel.choices, default=Channel.WECOM
    )
    webhook_url = models.CharField("Webhook 地址", max_length=500)
    games = models.ManyToManyField(
        Game, related_name="push_settings", verbose_name="限定游戏"
    )
    timing = models.IntegerField("发送时机", choices=Timing.choices)
    msg_type = models.CharField(
        "消息类型", max_length=16, choices=MsgType.choices, default=MsgType.MARKDOWN
    )
    call_all = models.BooleanField("是否@全员", default=False)
    remark = models.CharField("追加说明", max_length=100, blank=True, default="")
    is_active = models.BooleanField("启用状态", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "自动发消息到微信群设置"
        verbose_name_plural = "自动发消息到微信群设置"
        ordering = ["id"]

    def __str__(self):
        return self.name or f"推送规则#{self.pk}"


class ShopType(models.IntegerChoices):
    """「哪种店铺」的 9 个取值（对标「订单来源管理」页 9 个 radio 的 value，实测；spec §2.3）。
    ⚠️ 用 IntegerChoices（数值码）、不是 TextChoices：对标载荷里就是数值、且列表那一列还要按码映射文案（spec §2.3）。
    ⚠️ 列表里 0 的文案是「非店铺/淘宝/天猫」、与本枚举 TAOBAO 的 label「淘宝/天猫」不同——这是对标自身的不一致、逐字两套都照抄（spec §2.3 的 📌）、不要在这里「统一」。"""

    TAOBAO = 0, "淘宝/天猫"
    PDD = 10, "拼多多"
    JD = 15, "京东"
    XIANYU = 20, "闲鱼"
    DOUYIN = 30, "抖音"
    KUAISHOU = 40, "快手"
    VIP = 50, "唯品会"
    XHS = 60, "小红书"
    OTHER = 70, "其它平台"


class OrderSource(models.Model):
    """订单来源字典（spec §3）。每个租户 schema 一份，name 在租户内唯一（R9）。
    ⚠️ do_not_send_sms / sms_sign 不建字段（R8：短信两列只渲染+提示「尚未开放」；存了却没有消费方的字段会被下一个读者当成「已生效」——AGENTS.md §5）。
    ⚠️ order_by 的值域 0–999 是序列化器的判据、不是模型的（模型就是整型列）：依据对标 maxlength=3 + common.js 的 VerifyInt（spec §3）。"""

    name = models.CharField("来源名称", max_length=10)
    remark = models.CharField("来源描述", max_length=50, blank=True, default="")
    order_by = models.IntegerField("排序数值", default=0)
    shop_type = models.SmallIntegerField(
        "哪种店铺", choices=ShopType.choices, default=ShopType.TAOBAO
    )
    # 旧版单选入口保留自己的归属。新版读取它和多选归属的并集。
    group = models.ForeignKey(
        "OrderSourceGroup", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sources", verbose_name="公司店铺分组",
    )
    groups = models.ManyToManyField(
        "OrderSourceGroup", blank=True, related_name="member_sources",
        verbose_name="公司店铺分组",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "订单来源"
        verbose_name_plural = "订单来源"
        ordering = ["-order_by", "id"]
        constraints = [
            models.UniqueConstraint(fields=["name"], name="uniq_order_source_name"),
        ]

    def __str__(self):
        return self.name


class OrderSourceGroup(models.Model):
    """公司店铺分组字典。"""

    name = models.CharField("分组名称", max_length=20)
    order_by = models.IntegerField("排序数值", default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "公司店铺分组"
        verbose_name_plural = "公司店铺分组"
        ordering = ["-order_by", "id"]
        constraints = [
            models.UniqueConstraint(fields=["name"], name="uniq_order_source_group_name"),
        ]

    def __str__(self):
        return self.name


class ShopBinding(models.Model):
    """店铺对接（对标「其它店商平台对接」页表格的一行）。每个租户 schema 一份。
    ⚠️ 平台级凭证不在这里：AppId/AppSecret 是团团通（开发者）的、全平台一套、放 .env（AGISO_APP_ID / AGISO_APP_SECRET）不入库；AGISO_APP_SECRET 不下发（一次都不进浏览器）、AGISO_APP_ID 以只读字段下发（租户改不了）；租户要填的凭据只有 access_token；弹层里的 AppId 由 GET /api/business/shop-bindings/platform-config/ 预填（零绑定也有值）、逐行 platform_app_id 只是兜底、留给表格那一列。
    ⚠️ 不建 expires_at：手动授权流程拿不到 ExpiresIn（那是自动授权才返回的）、建一个永远为空的字段会被下一个读者当成「已生效」（OrderSource 的短信两列同一条纪律）。⚠️ shop_id 本轮不写入、不展示：留给下一轮自动授权（它的返回里带 ShopId）。"""

    platform = models.SmallIntegerField("平台", choices=ShopType.choices, default=ShopType.XIANYU)
    shop_name = models.CharField("店铺ID或昵称", max_length=64, blank=True, default="")
    shop_id = models.CharField("阿奇索侧店铺ID", max_length=64, blank=True, default="")
    order_source = models.ForeignKey(
        OrderSource, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shop_bindings", verbose_name="对应订单来源",
    )
    access_token = models.CharField("授权码", max_length=512, blank=True, default="")
    remark = models.CharField("备注说明", max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "店铺对接"
        verbose_name_plural = "店铺对接"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.get_platform_display()}:{self.shop_name or self.pk}"


class XgjShopBinding(models.Model):
    """闲管家店铺对接（对标页：菜单【第三方平台】→「闲管家店铺对接」）。每个租户 schema 一份。
    与上面那张 ShopBinding（阿奇索）的结构差别是本质的、所以是两张表而不是加一个 provider 列（spec §3-D1）：阿奇索的应用凭证是平台级（团团通的 AppId/AppSecret、放 .env）、租户侧凭据是 access_token（授权码、粘贴）；闲管家的应用凭证是租户级（租户自己在闲管家建的自研应用）、租户侧凭据是 app_key + app_secret。⚠️ app_secret 只写不读（spec §3-D2）：序列化器把它标成 write_only、接口永不回显、页面只显示一个「已配置/未配置」的布尔（与阿奇索 AGISO_APP_SECRET 同一条纪律，D-0061 / spec §9-S1）。⚠️ app_secret 的 PATCH 语义是「空串=不改」（spec §9-S4）：否则一次「只改备注」的编辑会把密钥静默清掉、而页面显示不出来（回显是关的）——这类"无声丢数据"只有测试能拦。
    ⚠️ 两个推送 URL 由服务端生成（request.build_absolute_uri()，spec §3-D5）、不是租户填的：它们是我们要暴露给闲管家的回调地址；字段入库是为了「记录在案」、并且接收端本轮不存在（docs/specs/2026-09-22-xgj-shop-integration-design.md §12-1）。⚠️ 不建 seller_id：官方文档说自研应用忽略它（spec §2.2）、不建一个恒空的字段——那会被下一个读者当成「已生效」（同 ShopBinding 不建 expires_at 的纪律）。⚠️ 本表没有 order_source 字段（已拆除，spec per-source §3-D3）：一个应用最多绑 30 家店、「一个应用↔一个来源」就是这条线要修的错模型——来源归店铺（见下面 XgjShop.order_source）、读单要用哪个应用走「来源 → XgjShop → binding」这条链；⚠️ 这是破坏性变更：升级后租户必须跑 migrate_schemas --tenant、再到对接页点一次「同步店铺」（或重存那一行、保存会自动同步）重建「店↔来源」（spec per-source §7）。"""

    shop_name = models.CharField("店铺名称", max_length=64, blank=True, default="")
    app_key = models.CharField("AppKey", max_length=64, blank=True, default="")
    app_secret = models.CharField("AppSecret", max_length=128, blank=True, default="")
    order_push_url = models.CharField("订单消息推送URL", max_length=255, blank=True, default="")
    product_push_url = models.CharField("商品消息推送URL", max_length=255, blank=True, default="")
    remark = models.CharField("备注说明", max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "闲管家店铺对接"
        verbose_name_plural = "闲管家店铺对接"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"闲管家:{self.shop_name or self.pk}"


class XgjShop(models.Model):
    """闲管家应用下的一家店铺（一个应用最多 30 家店）。每个租户 schema 一份。
    为什么必须有这张表（业务方反馈）：闲管家是「一个应用↔最多 30 家店铺」、而旧模型是「一个应用↔一个订单来源」⇒ 30 家店的单挤进同一个来源、分不开；改成「一个来源=一家店」之后需要有个地方记住「哪家店↔哪个来源」、这张表就是那个地方。⚠️ 认店用 user_identity（唯一）、不用店铺名称（spec D2）：官方文档说它「闲鱼内唯一且不变」、而店铺名称/会员名「存在变更的可能性」；业务方要求「店铺改名不要新增来源、还归到老来源」——只有拿不变的那个当键才做得到；实测（只读探针）：27 家店 user_identity 无重复、5/5 张真实订单的 seller_eid 都能命中这里的某一行、且订单里没有「店铺名称」（只有卖家标识+卖家会员名）⇒ 想拿店名当来源名必须查一次店铺列表（这就是同步的由来）。
    ⚠️ order_source 用 SET_NULL：来源被租户删掉（R8 允许）时这一行要留着——下次录单按标识还认得出是同一家店、重建来源并回填、而不是把这家店忘掉。⚠️ 录单「按来源反查应用」走的是本表的 order_source（XgjShopBinding 上的同名字段已拆除）：一个来源指向的那家店 → 它的 binding 就是该用哪个应用。⚠️ binding 用 SET_NULL（可空）而不是 CASCADE：租户「删掉这一行再重新加一遍」（换密钥、手滑删掉）是很自然的操作、而 CASCADE 会把店行一起删掉、把来源留成孤儿 ⇒ 重新同步时同一家店会拿到「甲店(2)」、历史单挂旧来源、新单挂新来源、「一个来源=一家店」当场断链（实测复现）；改成 SET_NULL 之后店行留着、重新同步按 user_identity 认回它并复用原来那个来源；代价：删了对接之后店行会短暂处于「没有所属应用」的状态（binding IS NULL）——那时它不出现在对接页的店铺表里（那张表按应用查）、重新同步一次即可恢复。"""

    binding = models.ForeignKey(
        XgjShopBinding, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shops", verbose_name="所属应用",
    )
    user_identity = models.CharField("闲鱼会员标识", max_length=64, unique=True)
    shop_name = models.CharField("店铺名称", max_length=64, blank=True, default="")
    user_name = models.CharField("会员名", max_length=64, blank=True, default="")
    order_source = models.ForeignKey(
        OrderSource, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="xgj_shops", verbose_name="对应订单来源",
    )
    # ⚠️ 下面两个是订购状态快照、只为 spec per-source §3-D11 的「标黄提醒」而存：业务方 R10 要求写明「店铺版本须为铂金版或 ERP 专业版才能识别订单」、但上游没有返回版本名（只有 is_valid / is_trial / valid_end_time）⇒ 我们不判定版本、只把「订购看起来无效 / 已过期」标出来、并在页面上写明标黄 ≠ 不是铂金版。
    # 字段名与语义出自官方文档（user/authorize/list 的 schema）：is_valid = 是否有效订购中（boolean）；valid_end_time = 订购有效结束时间（int32、单位秒的 Unix 时间戳；实测/doc 例子里有 0 = 没有有效订购）。
    # null = 还没同步过（或上游这次没给这个键）⇒ 不标黄（缺数据不等于异常）。
    is_valid = models.BooleanField("是否有效订购中", null=True, blank=True)
    valid_end_time = models.PositiveBigIntegerField("订购有效结束时间(秒)", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "闲管家店铺"
        verbose_name_plural = "闲管家店铺"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"闲管家店铺:{self.shop_name or self.user_identity}"


class XgjReceiptRecord(models.Model):
    """闲管家「交易成功」推送的记账行（spec 2026-10-22-xgj-receipt-detail-and-finance-merge-design.md §4-D2 / §7）。每个租户 schema 一份；一次 22 推送一行、重复推送更新旧行（刷新交易成功时间/店铺三元组/匹配结果；金额由回查线程刷新）⛔ 不重复计数。
    为什么必须有这张表（业务方口述 R3–R7）：推送报文里没有任何金额字段、没有店铺名称字段（厂商文档实拉核对，spec §2.1）⇒「这笔钱到了哪家店、到了多少钱」必须由我们自己记下来——金额靠回查（出站 order/detail，spec §12 F1-D：买家实付减已退）、店名靠快照+读时现解析（§4-D9）。
    ⚠️ 唯一约束挂在 (app_key, order_no) 上（H-2）：租户「删绑定再重建」是常规操作（XgjShop 的 docstring 明说）、重建后是新 binding 行 ⇒ 约束挂 binding 挡不住同单重推双算；app_key 跨绑定重建稳定、才是钱面统计的承重闸；binding 用 SET_NULL（可空）：删绑定不丢钱账（同 XgjShop.binding 的取舍）、app_key 快照让约束不依赖任何存活行。⚠️ success_time 存 aware UTC（USE_TZ=True 的库层口径）；它来自推送的 modify_time（秒级时间戳）、统计桶按 Asia/Shanghai 的本地日（spec §4-D6）。"""

    binding = models.ForeignKey(
        XgjShopBinding,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="receipt_records",
        verbose_name="所属应用",
    )
    #: 推送方 `appid` 的 **strip 快照**（唯一约束的一半；不依赖 binding 行的存续）。
    app_key = models.CharField("AppKey快照", max_length=64)
    #: 推送的闲鱼订单号（唯一约束的另一半；列宽与 `Order.source_order_no` 同源 128）。
    order_no = models.CharField("闲鱼订单号", max_length=128)
    #: 店铺三元组（spec §4-D9 / §12 F-2）：推送的 `seller_id` 是**商家级** ID（生产探针：
    #: 在 `XgjShop.user_identity` 里零命中）⇒ 只留作**行级事实**，不再当统计分组键；
    #: `shop_name_snapshot` 是记账当时的店名快照（展示名读时现解析、回退快照——店名会变，`XgjShop` docstring 明说）。
    seller_id = models.CharField("商家ID(推送原文)", max_length=64, blank=True, default="")
    user_name = models.CharField("闲鱼会员名", max_length=64, blank=True, default="")
    shop_name_snapshot = models.CharField("店铺名称快照", max_length=64, blank=True, default="")
    #: 店铺标识 = 回查详情响应的 **`seller_eid`**（密文串，与 `XgjShop.user_identity` 同源、
    #: 生产实弹已证命中）。**统计分组键**（spec §12 F2-D）；**可空**：尚未回查 / 详情取不到
    #: `seller_eid` 的行 = 未识别，汇总里单独成「未识别店铺」桶，补数完成后收敛为空。
    shop_identity = models.CharField(
        "店铺标识（详情 seller_eid）", max_length=128, null=True, blank=True, db_index=True
    )
    #: 交易成功时间（推送 `modify_time` 秒级时间戳 → aware UTC）。**统计桶的键**，加索引。
    success_time = models.DateTimeField("交易成功时间", null=True, blank=True, db_index=True)
    #: 实收金额 = **买家实付（`pay_amount`，分）− 已退（`refund_amount`，分）**，分→元
    #: （spec §12 F1-D：`xyb_seller_amount` 对普通单恒为 0，已废弃）。**可空**：空 =
    #: 待回查 / 回查失败 / 详情认不出 `pay_amount`（补查命令兜底）。`Decimal(12,2)`与 `Order` 金额列同宽（`MONEY_FIELDS` 那批列的既有口径）；**负差照存**（D-0015）。
    amount = models.DecimalField(
        "实收金额", max_digits=12, decimal_places=2, null=True, blank=True
    )
    #: 金额回查成功的时刻（补查判据；空 = 这一行还没回查到过）。
    amount_fetched_at = models.DateTimeField("金额回查时间", null=True, blank=True)
    #: 按推送单号命中的系统订单数（`_matching_orders` 的命中行数）。
    matched_count = models.PositiveIntegerField("命中订单数", default=0)
    #: 逐单结果摘要（JSON 数组：`[{"order_no": …, "result": "updated"|"refunded_skip"|"settled_skip"|"skipped"}, …]`）。
    match_result = models.JSONField("匹配结果摘要", default=list, blank=True)
    #: 推送原文（§4-D9：字段超出文档时留证据；⛔ 不含密钥——推送 body 本来就没有）。
    raw_push = models.JSONField("推送原文", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "闲管家收款明细"
        verbose_name_plural = "闲管家收款明细"
        ordering = ["-success_time", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["app_key", "order_no"],
                name="unique_xgj_receipt_appkey_orderno",
            ),
        ]

    def __str__(self) -> str:
        return f"闲管家收款:{self.order_no or self.pk}"


class SendResult(models.IntegerChoices):
    """发货结果两值（spec §3.1 的 result、取值与对标同值）。
    ⚠️ 取值 1=成功 / 0=失败是对标自己的枚举（spec §3.1）——⛔ 不要换成 True/False 或改值：前端筛选（result=-1/0/1）与对标逐值对应。"""
    FAILED = 0, "失败"
    SUCCESS = 1, "成功"


class TaobaoOrderSendLog(models.Model):
    """闲管家「无需物流发货」的发货记录（spec §3.1；对标页【淘宝发货记录】）。每个租户 schema 一份。
    什么时候写它：订单进入「已完成」时自动发一次货、以及页面上点「重试发货」时（两个入口共用 apps/business/xgj_ship.py 的同一份实现）；成功与失败都留一行、失败原因进 remark（spec §3.1 / D4）。
    ⚠️ order_no 刻意不建 FK（spec §3.1）：与 AuditLog.order_no 同一口径——留痕不该被引用行的存续影响（订单硬删/软删之后发货记录仍要能查、能展示）；tb_order_no 是发货时实际用的那个号的快照、不是回读订单的当前值。⚠️ 自动触发时 operator 恒为「系统」（spec §3.1）；页面重试写当前登录人的显示名。"""

    #: 系统订单号（spec §3.1；列宽 20 = `Order.order_no` 的宽度，⛔ 不建 FK）。
    order_no = models.CharField("系统订单号", max_length=20, db_index=True)
    #: 淘宝/闲鱼订单号 = 本次**实际发给上游**的那个号（`Order.source_order_no` 的快照）。
    tb_order_no = models.CharField("淘宝订单号", max_length=128, db_index=True)
    #: 「发货原参数」：本次发给上游的请求参数（**不含** app_secret / sign，spec §3.1）。
    #: 没调上游（无绑定 / 无来源单号 / 已发货跳过）时是空串——⛔ 不写一个没发出去的参数。
    send_params = models.TextField("发货原参数", blank=True, default="")
    store_name = models.CharField("发货店铺", max_length=64, blank=True, default="")
    result = models.SmallIntegerField("发货结果", choices=SendResult.choices)
    sent_at = models.DateTimeField("发货时间")
    operator = models.CharField("发货人", max_length=150)
    remark = models.CharField("备注说明", max_length=255, blank=True, default="")
    created_at = models.DateTimeField("落库时间", auto_now_add=True)

    class Meta:
        verbose_name = "淘宝发货记录"
        verbose_name_plural = "淘宝发货记录"
        # 新写的行在最前（与其余列表页同口径）；`-id` 是稳定 tie-break（同一时刻的多行翻页不漂）。
        ordering = ["-sent_at", "-id"]
        indexes = [
            # spec §3.1 的三个筛选（发货时间 / 结果 / 搜索）各自要一棵树；`order_no` 服务按单查历史。
            models.Index(fields=["sent_at"], name="idx_tbsend_sent_at"),
            models.Index(fields=["result"], name="idx_tbsend_result"),
        ]

    def __str__(self) -> str:
        return f"淘宝发货记录:{self.order_no}#{self.pk}"


class RecognitionKeyword(models.Model):
    """录单识别项的关键词覆盖（每个租户一份；只存被改过的那几行，spec §4）。
    ⚠️ 为什么不是每租户预置 14 行：预置要改租户供给流程+写数据迁移+给新租户挂钩子、三处成本换一个「表里行数更整齐」；默认值在 intake_rules.RECOGNITION_ITEMS（单一出处）、本表只放覆盖：没有这一行 = 用默认值。
    ⚠️ keywords 允许空串：清空 = 这一项不再产出任何识别规则（合法且有意义、不是"没填"）。⚠️ 不加 updated_by：谁改的由操作日志回答（spec §3-D5）、配置表不重复记一份人。"""

    item_code = models.CharField("识别项", max_length=32, unique=True)
    keywords = models.CharField("识别关键词", max_length=255, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "录单识别关键词"
        verbose_name_plural = "录单识别关键词"

    def __str__(self):
        return f"RecognitionKeyword({self.item_code})"


class SystemSwitch(models.Model):
    """系统设置开关的覆盖行（每个租户一份；只存被改过的那一行，D-0223）。
    ⚠️ 为什么不是每租户预置一行：与 RecognitionKeyword 同一条理由——预置要改供给流程+写数据迁移+给新租户挂钩子、三处成本换一个「表里行数更整齐」；默认值在 apps/business/system_switches.py 的注册表（单一出处）、本表只放覆盖：没有这一行 = 用注册表默认值。
    ⚠️ value 只收 "Y" / "N" 两个串（写入侧由 API 校验；读取侧 switch_enabled() 按 =="Y" 判、其余任何值都等价于关、fail-closed）。⚠️ 不加 updated_by：谁改的由操作日志回答（与 RecognitionKeyword 同口径）、配置表不重复记一份人。"""

    key = models.CharField("开关键", max_length=32, unique=True)
    value = models.CharField("开关值", max_length=8)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "系统设置开关"
        verbose_name_plural = "系统设置开关"

    def __str__(self):
        return f"SystemSwitch({self.key}={self.value})"


# ── 鸡蛋铺同步台账 ────────────────────────────────────────────────────────────
# 租户级那几张表（`JidanpuBinding` / `JidanpuOrderLink` / `RemoteHallOrder` /
# `JidanpuUserLink` / `JidanpuLogHit`）已经删掉，别按旧文加回来。本节只剩 `JidanpuSyncEvent`，两条个人绑定线共用。人员映射不落库。


#: source_name 的默认值（业务方 D8）= 「团团通系统」。⭐ 搬家（同名、同值、同语义、值一字未改）：本常量原住在已删除的 JidanpuBinding 上（类属性 SOURCE_NAME_DEFAULT）、那张表被删 ⇒ 搬到本模块级。
#: 宿主选择理由：三个借用方（jidanpu.py / jidanpu_member.py / api/jidanpu_member_views.py）本来就 from apps.business.models import … ⇒ 搬到这里不新增任何 import 边、不会成环；而搬到 jidanpu_common.py 会与既有的 jidanpu_common → jidanpu 那条边成环（jidanpu_common.py 顶层 from apps.business import jidanpu）。
#: ⚠️ 唯一出处：检测（jidanpu.detect_binding / jidanpu_member.detect_binding）与录单（jidanpu_member._record_own_order）都读它 ⛔ 别在别处再写一份字面值。
SOURCE_NAME_DEFAULT = "团团通系统"


# ⛔ **这里原来有 `class JidanpuBinding`（租户级鸡蛋铺同步的凭据 / 开关 / 游标，spec §4.1）
#    ——业务方裁定「租户级鸡蛋铺线整条拆除」，这张表连同模型一起删除**
#    （删表迁移 = `0057_remove_jidanpuorderlink_order_and_more`；`SOURCE_NAME_DEFAULT` 已按同名同值搬到本模块级，原文见 git 历史，⛔ 别按 P1 旧文再加回来）。

# ⛔ **这里原来有 `class JidanpuOrderLink`（两边单号的对应关系，spec §4.2）——同批按业务方
#    裁定删除**（同一迁移里的第二个 `DeleteModel`；⛔ `order.jidanpu_link` 这条
#    反向关系今天不存在，原文见 git 历史）。


# ⛔ **这里原来有 `class JidanpuLogHit`（鸡蛋铺快路的日志去重键表，plan §4.1）——已随
#    业务方裁定删除**：它随租户级鸡蛋铺线整条拆除，**全仓零代码级使用者**，且生产实测
#    当时两个租户库都是 **0 行** ⇒ 删除零数据损失（删表迁移 =`0058_delete_jidanpuloghit`；原文见 git 历史）。


# ⛔ 人员同步整条已取消（spec `2026-09-25-jidanpu-hall-sync-design.md` §17.2，删表迁移 = 0042）：指派时实时拉鸡蛋铺人员、不落库、不建映射，「谁接了」只按远端标识（数值 uid + 显示名）记台账与系统操作日志。
# ⛔ 别把那张人员映射表加回来——tests/test_jidanpu_money.py 的 NoPersonnelWriterStructureTests 会红；⛔ 也不要给这些日志/台账里的历史行做「清理」（AGENTS.md §4：不删历史记录）。


class JidanpuSyncEvent(models.Model):
    """outbox + 同步台账 + 待处理清单（一张表，spec §4.4）。
    ⚠️ 为什么合成一张（spec §4.4）：outbox 与台账的生命周期同形（建→处理→落状态）、拆两张会造出一份必须同步的重复事实。
    ⚠️ target_ref 存字符串（我方 order_no 或 User.username）不建 FK：删单不得连带删台账（spec §4.4 明文）。⚠️ 覆盖留痕不写在这里：字段改前/改后进订单域既有审计（spec §4.4 末段）。"""

    class Direction(models.TextChoices):
        OUT = "out", "团团通→鸡蛋铺"
        IN = "in", "鸡蛋铺→团团通"

    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        DONE = "done", "已完成"
        FAILED = "failed", "失败"
        SKIPPED = "skipped", "已跳过"

    direction = models.CharField("方向", max_length=8, choices=Direction.choices)
    kind = models.CharField("类型", max_length=32)
    target_type = models.CharField("对象类型", max_length=16)
    target_ref = models.CharField("对象标识", max_length=191)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField("尝试次数", default=0)
    reason = models.CharField("原因", max_length=255, blank=True, default="")
    payload = models.JSONField("载荷", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    processed_at = models.DateTimeField("处理时刻", null=True, blank=True)
    #: 执行租约的到期时刻（修复波 2，C7）。非空 = 某个执行者正在执行这条事件、其他执行者要等它过期才准接管；None = 没人持有；取值与推算见 jidanpu_hall.OUTBOX_LEASE_SECONDS / IMMEDIATE_LEASE_SECONDS。
    #: ⚠️ 它不是状态：status 的四个取值、语义、取活口径与前端标签一个字都没改（加第五个状态值会让「取活口径 / 前端标签 / 文档 / 既有断言」全都得跟着动、而这里要表达的只是「这一刻谁在执行」、与「这件事的结论」是两件事）。
    #: ⚠️ 抢租约是条件更新 + rowcount == 1（claim_event_for_execution）、收尾（_finish_event / 撤下的成功落库）带所有权地清掉它（修复波 3，A3）。
    lease_expires_at = models.DateTimeField("执行租约到期时刻", null=True, blank=True)
    #: 租约持有者（修复波 3，A3/B6）：赢下租约的那次执行自己的所有权令牌（jidanpu_hall.new_lease_token() 造的随机串；⛔ 不是用户标识、⛔ 不进任何对外文案）。
    #: 为什么要有它：lease_expires_at 只说「到什么时候为止」、说不出「是谁在持有」——没有身份、收尾就只能无条件清租约、于是「没持租约的调用方（/claim 的请求路径）会把别人的租约清掉」（实测过的第三种残差）。
    #: 语义：lease_owner == 自己 才准续租/收尾；抢租约时写自己、续租时只续自己、收尾带所有权条件（jidanpu_hall._owned_event_filter：lease_owner == 自己 或 lease_owner IS NULL）；⚠️ 与 lease_expires_at 同生同灭：收尾一律把两列一起置空 ⛔ 不留「有权到期时刻、没持有者」的半截状态；⚠️ 可空、不回填：存量行两列都是 NULL = 没人持有 ⇒ 照旧可被抢（迁移 0045）。
    lease_owner = models.CharField(
        "执行租约持有者", max_length=64, null=True, blank=True
    )

    class Meta:
        verbose_name = "鸡蛋铺同步台账"
        verbose_name_plural = "鸡蛋铺同步台账"
        ordering = ["-created_at", "-id"]
        indexes = [
            # 工人取活：按状态 + 时间（spec §4.4）。
            models.Index(fields=["status", "created_at"], name="jdp_event_status_idx"),
            # 页面按单查（spec §4.4）。
            models.Index(
                fields=["target_type", "target_ref", "created_at"],
                name="jdp_event_target_idx",
            ),
        ]
        constraints = [
            # ⭐ 待重拉队列的库级幂等：log.repull 的未收敛活口（pending / failed）按我方订单号唯一。
            # ⚠️ 为什么要有它：应用层 jidanpu_logs.queue_repull 是 check-then-act（先 exists() 再 create）⇒ 两个并发执行者（例如 T5 的定时轮 + 一次手工 run_fast_path）可能都查到「没有未收敛行」、然后各插一条（TOCTOU）；两条 log.repull ⇒ T4 对同一张单重复收敛（绩效回退一类「带符号累加」的动作会重复回退）⇒ 用库级约束兜底。
            # ⚠️ 为什么是「部分」唯一而不是全局唯一：done / skipped 重复出现是正常的——同一张单可以被多次改动、多次重拉完成、每次都留一条历史 ⇒ 唯一性只能加在「还没收敛」的活口上（status ∈ (pending, failed)）。⚠️ 为什么只限 log.repull + target_type="order"（不是所有 kind）：这是唯一一条已实测出 TOCTOU 的路径、且它的活口语义就是「一张单只排一次」；其它 kind（hall.launch / hall.withdraw / peer.*）的活口语义各不相同（重抛/重试会各自留行）、把它们一起收进唯一索引是改它们的语义、超出本修复波的范围、也可能在有存量的库上直接加不上约束；⚠️ 条件里逐字重复了应用层的判重口径（jidanpu_logs._live_repull_exists）：两处必须同时改（由用例 …database_level_unique_guard… 与 …concurrent_enqueue… 两路钉住）。
            models.UniqueConstraint(
                fields=["target_ref"],
                condition=models.Q(
                    kind="log.repull",
                    target_type="order",
                    status__in=("pending", "failed"),
                ),
                name="uniq_jidanpu_repull_live",
            ),
        ]

    def __str__(self) -> str:
        return f"鸡蛋铺同步台账:{self.direction}/{self.kind}/{self.target_ref}"


# ── 下游鸡蛋铺 · 代练员个人绑定 ──
# 租户级那套表已经删掉。本线自己的两张表不要和上家个人绑定掺在一起。

#: 日志快路间隔（秒）的**唯一出处**，也是 `JidanpuMemberBinding.fast_poll_seconds` 的默认值
#: （spec §4.1 的 A14）。⚠️ 后续模块一律 import 这个常量，⛔ 不要再写字面量 `3`。
MEMBER_FAST_POLL_SECONDS = 3

#: 定时对账间隔（秒）的**唯一出处**，也是 `JidanpuMemberBinding.reconcile_seconds` 的默认值
#: （spec §4.1 的 D-a）。⚠️ 同上：后续模块 import 它，⛔ 不要再写字面量 `180`。
MEMBER_RECONCILE_SECONDS = 180

#: ⭐ 状态回拉间隔（秒）的唯一出处、也是 JidanpuMemberBinding.status_pull_seconds 的默认值。为什么单独一列 ⛔ 不复用 reconcile_seconds：真机实测两条路要的频率根本不同——回拉要 10 分钟一次（远端「已完成」要尽快落到我方「待验收」）、而补漏对账是低频兜底（一轮要枚举/排几百条候选事件、跑勤了就是白打上游）；共用一列时只能二选一：调成 600 ⇒ 每 10 分钟排 450+ 条候选、调成 3600 ⇒ 回拉也变成一小时一次 ⇒ 拆成两列、各有各的锚。
#: ⚠️ 默认 600 是照真机验证过的回拉节奏取的（10 分钟）、对账那一列的默认值一个字都没动（仍是 MEMBER_RECONCILE_SECONDS = 180：它是已上线那条线的既有旋钮）。
MEMBER_STATUS_PULL_SECONDS = 600

#: 回调随机码的字节数（spec docs/specs/2026-10-08-jidanpu-official-api-binding-design.md §5.1）：secrets.token_urlsafe(16) ⇒ 22 个字符、字母表 A-Za-z0-9-_、熵 ≈ 128 位。
#: ⚠️ 这个 22 不只是「好看」：deploy/nginx/tuantuantong.conf 的 nginx location 用定长 22 的正则 [A-Za-z0-9_-]{22} 钉住路径形状 ⇒ 改这里必须同一次改那个模板。
#: ⚠️ 与上家线那个 UPSTREAM_CALLBACK_CODE_BYTES 是两个常量、值相同：两条线零关系（上面「下游鸡蛋铺·代练员个人绑定线」那段注释的口径）⇒ 不让一条线的常量被另一条线牵走；唯一的判据是接收器那一侧（apps/callback/wsgi.py::CALLBACK_CODE_LENGTH）：两边对不上时 callback_path_of_code 会回空串（地址算不出来 ⛔ 不是拼一个半截地址）。
MEMBER_CALLBACK_CODE_BYTES = 16


class JidanpuMemberBinding(models.Model):
    """代练员自己那套下家鸡蛋铺的绑定（一人一份）。
    没有「对方账号 uid」：不在对方系统里指派任何人。对接方式有登录式和官方 API 两套、列形状跟上家绑定表一样、但行不要互相掺。"""

    class Mode(models.TextChoices):
        """对接方式（spec §2.1）——⚠️ 取值刻意用两个英文词 ⛔ 不用 0/1：这一列会出现在 API 载荷、日志、管理命令输出与页面上、0/1 在排查时读不出方向。
        ⚠️ 与上家线那个 JidanpuUpstreamBinding.Mode 值相同但各自一份（零关系口径，见类 docstring）：⛔ 不互相 import——那会让一条线的枚举被另一条线的改动牵走。"""

        PASSWORD = "password", "账号 + 密码"
        OPENAPI = "openapi", "官方开放平台 API"

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="jidanpu_member_binding")
    jidanpu_base_url = models.CharField("下家鸡蛋铺地址", max_length=255)
    account = models.CharField("下家账号", max_length=150)
    account_name = models.CharField("下家账号显示名", max_length=150, blank=True, default="")
    password = models.CharField("密码", max_length=255)          # 只写不读
    source_name = models.CharField("订单来源名称", max_length=64, default="团团通系统")
    enabled = models.BooleanField("启用同步", default=False)
    # ── 对接方式 ①登录式 / ②官方 Open Platform API（spec §2 / §4.2）──
    #: ⭐ 这一列是「用哪套凭证」的唯一判据（spec §2.1）。⛔ 判据一律写 if binding.mode == "openapi" ⛔ 不许写 if binding.client_id: 那种真值判据（E-0021 家族：切回登录式之后 client_id 还在 ⇒ 那一句会静默走官方 API）。
    #: ⚠️ 默认值 "password" 是承重的：这张表在加列之前全是登录式绑定 ⇒ 默认值写成 "openapi" 会让存量行在迁移那一刻被 ADD COLUMN … DEFAULT 回填成②式（静默换路）。⚠️ 两套凭证允许同时留着（切来切去不用重输）⇒「库里还剩一个旧密码」在 SQL 里看不出来。
    mode = models.CharField(
        "对接方式", max_length=16, choices=Mode.choices, default=Mode.PASSWORD,
    )
    #: 官方 API 的 `client_id`（spec §4.2）：它是**身份**、不是密钥 ⇒ **可读**（页面上要回显）。
    #: 长度 150 = 与 `account` 同一口径。
    client_id = models.CharField("官方API client_id", max_length=150, blank=True, default="")
    #: ⭐ **只写不读**（spec §4.2 / §13.3-C3）：序列化器侧 `write_only`、响应里连**键**都不出现
    #: （只回 `client_secret_set` 布尔）—— 与上面那行 `password` **同一套口径**。
    #: ⚠️ 这一列**本身就是**不可读形态的承重面：它同时是回调验签用的那把密钥（spec §19.1 实测：`sign` 就是用 `client_secret` 签的）⇒ ⛔ 不加第二列回调密钥、⛔ 不进日志 / 不进异常文本。⚠️ 本任务**只做列本身**：序列化器接这一列（`write_only` + `client_secret_set`）是**后续任务**。
    client_secret = models.CharField("官方API client_secret", max_length=255, blank=True, default="")
    #: 每份官方 API 绑定一串随机码（spec §5.1）、拼进回调地址 https://<租户域名>/api/jidanpu/callback/<码>。
    #: ⭐ 改口径（业务方裁定、不是「一行一个码」）：同一个租户 + 同一个 jidanpu_base_url ⇒ 共用同一个码（也就是共用一个回调地址）——同一个下家鸡蛋铺系统上只有一处「回调地址」设置项、多个打手绑同一个域名时只能填同一个地址 ⇒ 各发一个码等于要人家把同一个框填好几遍（根本填不了）。
    #: ⚠️ 因此原先那条 UniqueConstraint(["callback_code"]) 的部分唯一索引已删除（理由与残差见下面 Meta.constraints 里那段说明）：新口径下「多行共用一个码」与「码唯一」在 PostgreSQL 里无法同时表达（唯一索引按行判、而这里的不变式是「码是域名的函数」）；⚠️ 码由我们生成（_ensure_callback_code）、用户看不见 ⇒ ⛔ 别把它做成必填入口字段。
    callback_code = models.CharField("回调随机码", max_length=64, blank=True, default="")
    #: 这串码**什么时候发的**（spec §4.2；页面要显示「回调地址是什么时候生成的」）。可空、不回填。
    callback_code_at = models.DateTimeField("回调码生成时刻", null=True, blank=True)
    #: ⭐ 消费者的**水位**：上一次处理到哪个**接收时刻**（spec §5.5）。
    #: ⚠️ **内部状态**：⛔ 不进序列化器、⛔ 不上页面（照本表 `status_pull_at` 的既有先例）
    #: ——它与 `callback_last_at` 一起回答「这台机器有没有人在消费」（spec §9.4）。
    callback_watermark_at = models.DateTimeField("回调消费水位", null=True, blank=True)
    #: 最近**真的**收到过一条**本绑定**的回调（健康列；spec §9.4 回答「对面还在推吗」）。
    #: ⚠️ 与 `last_ok_at` **不是一回事**：那是「同步成功」。⚠️ ⛔ 不许做成「多久没回调就报警」
    #: （低单量工作室两小时没单是正常的，spec §9.4 末段 / N9）。
    callback_last_at = models.DateTimeField("最近一次收到回调", null=True, blank=True)
    #: 累计处理过的回调条数（排查用；spec §4.2）：⛔ 它不是审计表，与 `callback_last_at` 同一次写入。
    callback_count = models.PositiveIntegerField("累计处理回调条数", default=0)
    #: ⭐ 已消费到的字节偏移（生产性能缺陷修复）：事件文件（events.jsonl）里下一条还没读的字节位置 ⇒ 消费者每轮从这里 seek 往后读 ⛔ 不再把整份留档从头扫一遍、全部解析进内存（现网那个文件 198MB / 十几万行）。
    #: 口径（唯一出处是 apps.business.jidanpu_openapi_consume）：值 = 最后一条完整处理过的行的末尾——半行/坏行/中途停下 ⇒ 停在那一行之前（下一轮重读 ⛔ 不跳过）；文件变短/回退 ⇒ 与水位一起清零；只是文件位置、不是业务状态 ⛔ 不进序列化器、⛔ 不上页面；默认 0 是承重的：存量行在迁移那一刻就是 0 ⇒ 首轮从头读（大文件走「只读末尾」那一支）。
    callback_file_offset = models.BigIntegerField("已消费到的字节偏移", default=0)
    # ⚠️ **这里刻意不再加 `reconcile_seconds`**（spec §4.2 在上家线列了它，本表**已经有了**）：
    #    本类下面那一列 `reconcile_seconds` 就是，默认 **180**、verbose「对账间隔(秒)」
    #    ——语义与本线一致（定时对账的间隔），⇒ **直接复用它当②式兜底对账的闸门**。⛔ 加同名列会撞 `FieldDoesNotExist` / 直接是重复定义；⛔ 也不要为了「对齐上家线」把它的默认值改成 600 —— 那会改掉**已上线那条下家线**的既有节拍（默认 180）。
    fast_poll_seconds = models.PositiveIntegerField("日志快路间隔(秒)", default=MEMBER_FAST_POLL_SECONDS)
    #: ⭐ **状态回拉**的间隔（秒）：只闸 ③ `pull_statuses`。
    #: ⚠️ 与它上面那格 `status_pull_at` **不是一回事**：那是「上次成功拉完的时刻」**水位**
    #: （决定取数窗），这是「多久跑一次」的**旋钮**。两者名字像，⛔ 别混。⚠️ 默认 600（10 分钟）= 真机验证过的回拉节奏；对账仍是下面那一列（默认 180）。
    status_pull_seconds = models.PositiveIntegerField(
        "状态回拉间隔(秒)", default=MEMBER_STATUS_PULL_SECONDS,
    )
    #: ⚠️ **本列同时也当「②官方 API 那条路的兜底对账」的闸门**（spec
    #: `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §8）：两条路说的是同一件事
    #: （「多久对一次账」）⇒ **复用本列**，⛔ 不许再加一列同名的第二把闸门。⚠️ 默认值**仍是 180**（⛔ 不为了对齐上家线那个「兜底对账间隔(秒)」而改成 600）：本列是**已上线那条下家线**的既有旋钮，改默认值等于改它的节拍。
    reconcile_seconds = models.PositiveIntegerField("对账间隔(秒)", default=MEMBER_RECONCILE_SECONDS)
    #: 大厅订单的**抛单百分比**（业务方裁定）：抛给对方的抛单价 = **我方发单价 × 本值 / 100**。
    #: ⚠️ 只作用于「大厅镜像」那一档；**指派 / 他自己接**那两档只录单、不抛单（接单价 = 我方发单价）。
    #: 范围 1–100（⛔ 0 会把一张免费单真送进对方大厅，与 `LAUNCH_NO_PRICE_REASON` 同族口径）；默认 100 = 不打折（与引入本列之前的行为**等价**）。模型层与序列化器**两头卡**（E-0021 同族：只在视图层卡，将来多一个写入点就又漏了）。
    throw_price_percent = models.PositiveSmallIntegerField(
        "抛单百分比(%)",
        default=100,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    #: ⭐「同步大厅订单」开关（D-0299，业务方拍板）：默认 True＝存量行为不变。
    #: 语义 = 只闸「抛大厅」那两条路（触发点① queue_hall_launch 与对账 to_launch 档、判据在 jidanpu_member；关 ⇒ 不再排 member.launch）；⛔ 不影响其他一切功能：assign（指派录单）/ status（状态回传）/ withdraw（退单）/ unlist（撤下）/ price（改价）/ project（接手项目）/ pics（截图补传）一条都不读它。
    #: ⚠️ 关掉后不主动撤回已挂对方大厅的存量单（D-0299 明文）⇒ 对账那侧不许把期望收口成「对方不该有这张单」（那会排退单）；存量清理待业务方另行拍板。
    hall_sync_enabled = models.BooleanField("同步大厅订单", default=True)
    log_cursor = models.CharField("快路游标", max_length=64, blank=True, default="")
    #: 「下游已完成」这一路的水位：上次成功拉完并把结果应用完的时刻（「拉全且无错」才推进；处理集合为空时也推进——那时现存链路没有一张需要拉）。
    #: 下一次的取数窗（的实现口径）：有水位 ⇒ since = 本列 − 3 分钟重叠（那 3 分钟是给「水位前 1 分钟完成」的边界单留的）；无水位 ⇒ now − 3 天；最后卡在 457 天以内（服务端硬限 460 天、卡口留出日期取整的约 2 天余量）。
    #: ⚠️ 真相在 jidanpu_member._pull_statuses 那几行 ⛔ 别按更早的 min(…, now−3天) 口径读；⚠️ 内部状态：⛔ 不进序列化器、⛔ 不上页面、⛔ 不做成可调旋钮。
    status_pull_at = models.DateTimeField("完成状态拉取水位", null=True, blank=True)
    #: ⭐ 这个下家账号在对方店铺里的员工 uid（业务方裁定「录单时填绑定账号自己」）。
    #: 它是录单表单 JSON 里 inputOrderKefu 的值：对方那一格要的是员工 uid、不是姓名文本（jidanpu.KEFU_CANDIDATES_PATH 的注释有依据）、而 own_staff_uid 认出来的就是它。
    #: ⚠️ 这是缓存、不是配置：⛔ 请求体不许写它（序列化器里 read_only）、⛔ 不在 _PUT_WRITE_FIELDS 里（那会把并发写进去的值覆盖回旧值）、换绑（改账号/改地址）时必须清掉（旧账号的 uid 对新账号是另一个人）；⚠️ 空串 = 还没取到（或认不出来）⇒ jidanpu_member 会在下次录单时再取一次。
    input_order_kefu = models.CharField(
        "下家录入客服uid", max_length=64, blank=True, default=""
    )
    last_run_at = models.DateTimeField("上次运行时刻", null=True, blank=True)
    last_ok_at = models.DateTimeField("上次成功时刻", null=True, blank=True)
    last_error = models.TextField("上次失败原因", blank=True, default="")
    last_error_at = models.DateTimeField("上次失败时刻", null=True, blank=True)
    checked_at = models.DateTimeField("上次检测通过时刻", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            #: ⭐ 「地址 + 账号」这条唯一性只对①式生效（业务方裁定）：②式（官方开放平台 API）下同一个上家域名要能被多个打手各绑一条、而它那两格在界面上是隐藏的（account 是空串）⇒ 无条件的那条约束会把第二条②式绑定直接拒掉、而回的文案是「该鸡蛋铺账号已被 X 绑定…」（读起来像在说账号、实际是模式冲突）。
            #: 「部分」的条件为什么是 mode="password"：①①式的行为一个字不变——同一个域名 + 同一个非空账号仍然不许重复绑（判据见 tests/test_jidanpu_member_models.py 与 API 那一侧的冲突用例）；②②式的唯一性不靠账号——它靠「一个域名一个回调码」（见 _ensure_callback_code）：每条绑定自己的回调地址是同一个域名共用的那一串码（业务方）。
            models.UniqueConstraint(
                fields=["jidanpu_base_url", "account"], condition=Q(mode="password"),
                name="uniq_jdp_member_account",
            ),
            # ⛔ 这里原先还有一条 UniqueConstraint(["callback_code"], condition=Q(mode="openapi")) 的部分唯一索引、已按业务方裁定的新口径删掉（原句在此保留：「这条索引是承重的：没有它，回调地址的 22 位码会重复 ⇒ 同一个地址能命中两份绑定」）。
            # 为什么必须删：新口径是「同一个租户 + 同一个 jidanpu_base_url 共用一个码」（同一个下家鸡蛋铺系统只有一处回调地址设置项 ⇒ 多个打手只能填同一个地址）⇒「多行共用一个码」是设计要的形态、而按行的唯一索引恰好禁止它——留着它、第二条同域名的②式绑定照样建不上（只是把拒绝从「账号」挪到了「码」）；⚠️ 新口径下 DB 层无法表达这条不变式：唯一索引按行判、而这里的不变式是「码是域名的函数」（f(域名) = 码、同一个域名的多行取同一个值——这在 PostgreSQL 里没有对应的约束形状：(域名, 码) 唯一会禁止同域名的第二行、而 (码) 唯一会禁止共用）⇒ 这条不变式的落点是唯一写入点 _ensure_callback_code（复用同域名的码 + 生成时不让码跨域名复用）、残差如实登记在它的 docstring 里。
            # ⚠️ 不要为了「把约束加回来」改成 (jidanpu_base_url, callback_code) 唯一：那个组合在同域名的两行上完全相同、加了等于禁止「一个域名多个打手」。
        ]

    def save(self, *args, **kwargs):
        """落库前补上官方 API 那条路必需的回调码（spec §4.3 第 2 行：「保存时自动生成」）。
        ⚠️ 为什么生成写在模型这一层：callback_code 是我们生成的、用户看不见 ⛔ 不是一个入口字段 ⇒ 漏生成的后果是静默的（回调地址里没有码 ⇒ 那条回调 404、而页面上一切正常）；放在这里任何写入点（序列化器/管理命令/shell）都覆盖到。
        ⚠️ 判据与「按域名复用」的全部规则在 _ensure_callback_code 里（⛔ 不在这上面另判一套）；⚠️ 形状照上家线 JidanpuUpstreamBinding.save（两条线各自一份实现 ⛔ 不互相 import；上家那一条仍是「一行一个码」、本次口径只改下家这一条线）。"""
        self._ensure_callback_code()
        if self._callback_code_generated and kwargs.get("update_fields") is not None:
            # ⚠️ 走 `update_fields` 的写入点（序列化器的 PUT 那一支）不补这一句，
            # 刚生成（或刚对齐）的码**不会落库** ⇒ 又一次静默形态。
            kwargs["update_fields"] = list(kwargs["update_fields"]) + [
                "callback_code", "callback_code_at",
            ]
        return super().save(*args, **kwargs)

    def _ensure_callback_code(self) -> None:
        """⭐ 按域名保证回调码：②式行同域名共用一个码（幂等；①式行一个字节都不写）。为什么是「一个域名一个码」（业务方裁定、不是「一行一个码」）：同一个下家鸡蛋铺系统上只有一处「回调地址」设置项、多个打手绑同一个域名时只能填同一个地址（按「每条绑定一串码」各发一个等于要人家把同一个框填好几遍、根本填不了）。四条分支（判据只有两样：self.mode 与同域名的兄弟行）：①式一个字节都不写；②式本行还没有码+同域名已有码 ⇒ 复用兄弟行那一串（连 callback_code_at 一起抄）；②式两边都没有 ⇒ 生成一串新的（secrets）＋写到同域名其它②式行上（它们此前没有码）；②式本行已有码 ⇒ 默认不动它（换码 = 用户此前填进鸡蛋铺的地址失效、只有人能决定）。
        ⚠️ 唯一例外：本行已有码但换了地址（库里那一行的 jidanpu_base_url 与新值不同）且新域名下已有别的码 ⇒ 本行的码改成新域名那个——不这么做一次「改地址」就会造出「同一个域名两串码」（判据必须读库里那一格的旧值：走到这里时 self 上已是新值）。⚠️ 存量（本口径之前造出来的「同域名两串码」）本函数不会改写它们（stored_base_url == self.jidanpu_base_url ⇒ 走「不动它」那一支）——那串码已经发给用户了、自动挑一串覆盖另一串是替用户决定（代价如实登记：那种存量行各自显示各自的地址；要人工对齐时把两行里任意一行的地址先改成别的域名再改回来、新口径那一支就会把它对齐到兄弟行那一串）。
        ⚠️ 残差（如实登记、取代原先那条部分唯一索引）：DB 层不再有「码」的唯一约束 ⇒ ①同一个域名内的多行由本函数保证一致 ②跨域名的重复码由生成循环挡住（生成时问一次库）；唯一没被挡住的窗口是「两个进程同一瞬间各自生成」——概率 ≈ 2^-128（token_urlsafe(16)）且它只是让两个域名撞同一串码（接收器密钥文件里那一行会被后写的覆盖 ⇒ 其中一边的回调验签不过、台账上看得见）⛔ 不会静默丢单。⚠️ 同域名两行字面不同的密钥（理论上不该出现：同一个鸡蛋铺系统发的是同一套凭据）新口径下共用同一个码、而 shared/callback-keys.env 是一个码一行（_plan_keys_write 是整行替换 ⛔ 不是「第一条胜出」）⇒ 另一行的回调验签不过（接收器回 401；消费侧台账上是一条 …openapi_unsigned + 绑定 last_error ⛔ 不是静默）⇒ 同一个域名下的多个打手必须用同一套 client_id / client_secret。"""
        self._callback_code_generated = False
        if self.mode != self.Mode.OPENAPI:
            return
        sibling = self._domain_code_sibling()
        if not self.callback_code:
            if sibling is not None:
                # ⭐ 复用同域名已有的码（新口径的核心：**一个域名一个码**）。
                self.callback_code = sibling.callback_code
                self.callback_code_at = sibling.callback_code_at
            else:
                self.callback_code = self._fresh_callback_code()
                self.callback_code_at = timezone.now()
            self._callback_code_generated = True
            self._propagate_code_to_domain()
            return
        # 本行已有码 ⇒ 只有「换了地址、且新域名下已经有别的码」这一支才动它（见 docstring）。
        if sibling is None or sibling.callback_code == self.callback_code:
            return
        if self._stored_base_url() == self.jidanpu_base_url:
            return  # 存量：同域名两串码，⛔ 不动（那串码已经发出去了，见 docstring）
        self.callback_code = sibling.callback_code
        self.callback_code_at = sibling.callback_code_at
        self._callback_code_generated = True

    def _domain_code_sibling(self):
        """同域名、别的②式行里 pk 最小的那一行（callback_code 非空的才行）；没有 ⇒ None。
        ⚠️ 排序按 pk 升序（先绑的那一条胜出）：存量行里可能不止一串码、判据必须稳定（⛔ 不按时间 / 不按 callback_code_at 之外的东西：那些会变、同一个域名两次保存会挑到不同的码）。"""
        rows = JidanpuMemberBinding.objects.filter(
            mode=self.Mode.OPENAPI, jidanpu_base_url=self.jidanpu_base_url,
        ).exclude(callback_code="")
        if self.pk is not None:
            rows = rows.exclude(pk=self.pk)
        return rows.order_by("pk").first()

    def _stored_base_url(self) -> str:
        """**库里**这一行的 `jidanpu_base_url`（本行还没落库时是新值 ⇒ 判「换没换地址」要读它）。"""
        if self.pk is None:
            return self.jidanpu_base_url
        stored = (
            JidanpuMemberBinding.objects.filter(pk=self.pk)
            .values_list("jidanpu_base_url", flat=True)
            .first()
        )
        return self.jidanpu_base_url if stored is None else stored

    def _fresh_callback_code(self) -> str:
        """生成一串没被别的域名用过的码（⭐ 这是原先那条部分唯一索引的替代判据）。
        ⚠️ 用 secrets（密码学安全）⛔ 不用 random；⛔ 不用 uuid4().hex（长且不好看）；⛔ 不许把订单号 / 时间戳 / 用户 id / sharding 信息拼进码里（那会让它可枚举）——三条都是 spec §5.1 明写的。
        ⚠️ 「别的域名用过」= 同一个码出现在另一个 jidanpu_base_url 上；同域名的兄弟行共用同一个码是要的形态 ⇒ ⛔ 不能拿「码已存在」当判据（那会把共用判成撞码）。"""
        while True:
            code = secrets.token_urlsafe(MEMBER_CALLBACK_CODE_BYTES)
            taken = (
                JidanpuMemberBinding.objects.filter(callback_code=code)
                .exclude(jidanpu_base_url=self.jidanpu_base_url)
                .exists()
            )
            if not taken:
                return code

    def _propagate_code_to_domain(self) -> None:
        """把本行这一次拿到的码写到同域名其它②式行上（它们此前没有码；见 docstring 那张表）。
        ⚠️ 一次 UPDATE（⛔ 不逐行 save()：那会把每一行的 updated_at 都碰一遍、也会把「谁改的」这条线索弄糊）；⛔ 不动 mode / 别的列。
        ⚠️ 目标只限「码为空」的行：已经有码的行一律不动（换码 = 用户已填的地址失效）。"""
        rows = JidanpuMemberBinding.objects.filter(
            mode=self.Mode.OPENAPI, jidanpu_base_url=self.jidanpu_base_url, callback_code="",
        )
        if self.pk is not None:
            rows = rows.exclude(pk=self.pk)
        rows.update(callback_code=self.callback_code, callback_code_at=self.callback_code_at)


class ActiveJidanpuMemberLinkManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(retired_at__isnull=True)


class JidanpuMemberOrderLink(models.Model):
    """代练员个人绑定自己的链路。租户级 JidanpuOrderLink 已经删掉、不要加回来。
    in_hall = 对方系统里这张单此刻挂在他家大厅（本地标记，由快路 / 对账更新）。"""

    binding = models.ForeignKey(JidanpuMemberBinding, on_delete=models.CASCADE, related_name="links")
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="jidanpu_member_links")
    jidanpu_order_id = models.CharField("鸡蛋铺订单ID", max_length=64)
    retired_at = models.DateTimeField("换绑隔离时间", null=True, blank=True)
    objects = ActiveJidanpuMemberLinkManager()
    all_objects = models.Manager()
    in_hall = models.BooleanField("对方大厅里挂着", default=False)
    listed_at = models.DateTimeField("上次上大厅时刻", null=True, blank=True)
    last_in_at = models.DateTimeField("上次拉取时刻", null=True, blank=True)
    last_out_at = models.DateTimeField("上次推送时刻", null=True, blank=True)
    #: ⭐⭐ 加这一列（修「同一张单被反复退」那个环、真机实测：8710 条退单事件只牵涉 185 张单、95% 是重复排放；根因是退单办成之后没有任何东西记得「办成了」⇒ 每 180 秒的对账又按「对方不该再有这张单」把它排一遍）。
    #: 语义只有一条：这张单在这一份绑定上「上一次退成」的时刻（None = 从没退成过）；它是终态标记 ⛔ 不是「在飞」标记——在飞由台账（JidanpuSyncEvent 的 pending/failed）管、两者的判据刻意分开。
    #: ⚠️ 回置（可逆）只有一处：jidanpu_member._consume_launch 成功那一支（对方系统里又有这张单了 ⇒ 上一次那个「已退」的结论作废）。⛔ 不复用现成的那四列、各自有别的语义（理由写在这四列各自的注释里、别改）：in_hall 记的是对方大厅此刻的事实（退单只是它的一个成因：撤下/对方打手接走同样会把它写成 False）⇒ 拿它当「退过」的判据会把「撤下过」误判成「退过」；listed_at / last_in_at / last_out_at 是三个动作时刻（上大厅/上次拉取/上次推送）⛔ 没有一个表示「退单办成了」这个终态；last_out_at 不再参与回声抑制（E-0292 删掉了时间窗）⇒ 现在就只是「上次推送时刻」这个事实 ⛔ 不要往里写第二种语义。
    withdrawn_at = models.DateTimeField("上次退单完成时刻", null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["binding", "order"], condition=Q(retired_at__isnull=True), name="uniq_jdp_member_link_order"),
            models.UniqueConstraint(fields=["binding", "jidanpu_order_id"], condition=Q(retired_at__isnull=True), name="uniq_jdp_member_link_remote"),
        ]


# ── 上家鸡蛋铺 · 客服个人绑定 ──
# 租户级那套表已经删掉。本线和代练员个人绑定（`JidanpuMemberBinding`）不互相读、不共享行。

#: 上家大厅全量拉取间隔（秒）的唯一出处，也是 `hall_poll_seconds` 的默认值。
#: 默认 30 秒（D-0363）：只把恰好等于上一代默认 120 的行收成 30，手工改过的值不动。
#: 大厅尾部快路已退役（D-0366），现存读路只有逐游戏的全量轮，大约 88 次请求/分钟。对端有过 WAF 封 IP；要放慢就改这一列，不要改代码。不要再写字面量 30。
UPSTREAM_HALL_POLL_SECONDS = 30

#: 「我的订单」拉取间隔（秒）的**唯一出处**，也是 `JidanpuUpstreamBinding.my_orders_poll_seconds`
#: 的默认值（spec §10；✅ T10 已定）：那一份只有几行、却是**状态变化的主来源** ⇒ 要快。
#: ⚠️ 后续模块 import 它，⛔ 不要再写字面量 `60`。
UPSTREAM_MY_ORDERS_POLL_SECONDS = 60

#: 官方 Open Platform API 那条路的**兜底对账间隔（秒）的唯一出处**，也是
#: `JidanpuUpstreamBinding.reconcile_seconds` 的默认值（spec
#: `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §8.3：⚠️ 该节写的是`OPENAPI_RECONCILE_SECONDS`，落地时按本文件既有的 `UPSTREAM_*` 命名前缀写全，见 T1 报告「偏离 spec 的地方」）。⚠️ 后续模块 import 它，⛔ 不要再写字面量 `600`。
UPSTREAM_OPENAPI_RECONCILE_SECONDS = 600

#: 回调随机码的字节数（spec §5.1）：`secrets.token_urlsafe(16)` ⇒ **22 个字符**、
#: 字母表 `A-Za-z0-9-_`、熵 ≈ 128 位。⚠️ 这个 22 不只是「好看」：`deploy/nginx/tuantuantong.conf`
#: 的 nginx location 用**定长 22 的正则** `[A-Za-z0-9_-]{22}` 钉住路径形状 ⇒ 改这里必须**同一次**改那个模板（`apps/callback` 那条线同口径）。
UPSTREAM_CALLBACK_CODE_BYTES = 16


class JidanpuUpstreamBinding(models.Model):
    """客服绑的上家鸡蛋铺打手账号（租户内多行，D-0256）。user 是录入人、不是「这行属于谁」；一人一行会在换绑第二个上家时静默覆盖（E-0255）；同一 (地址, 账号) 由 uniq_jdp_upstream_account 挡重复；同地址由 id 最小的启用行拉大厅（dedupe_owner）。
    ⚠️ 地址列名刻意与 JidanpuMemberBinding 同名（jidanpu_base_url）：读码可知 apps/business/jidanpu.py 的 open_client_with_http 读的就是这个名字 ⇒ 本线不需要第三个建连函数；另加的只读属性 base_url 是为了让同模块的 open_client / credentials_of（那两个读 base_url）也直接可用——那两个函数读的字段名不一样、这是既有的不对称 ⛔ 本线不改它们。
    ⛔ 没有「上家账号 uid」列（spec §4.1）：本线从不在上家系统里指派/占住任何人（大厅接单与提交完成都用自己的账号）。"""

    class Mode(models.TextChoices):
        """对接方式（spec §2.1）——⚠️ 取值**刻意用两个英文词**，⛔ 不用 `0/1`：这一列会出现在
        API 载荷、日志、管理命令输出与页面上，`0/1` 在排查时读不出方向（照
        `JidanpuUpstreamHallOrder.Status` 的既有形状）。"""

        PASSWORD = "password", "账号 + 密码"
        OPENAPI = "openapi", "官方开放平台 API"

    #: 录入人。必须是 ForeignKey：OneToOne 会挡住同一客服绑第二个上家。
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="jidanpu_upstream_binding")
    jidanpu_base_url = models.CharField("上家鸡蛋铺地址", max_length=255)
    account = models.CharField("上家账号", max_length=150)
    account_name = models.CharField("上家账号显示名", max_length=150, blank=True, default="")
    #: ⭐ 
    #: 人填的**上家系统名称**（如「初音网游」）——同一租户绑多家上家鸡蛋铺时用来区分。
    #: ⚠️ 与 `account_name` 是**两回事**：那一列是同步引擎缓存的上家账号显示名（**只读**，序列化器钉在 `read_only_fields`，见 `jidanpu_upstream_views.py`）；本列由人在「绑定上家系统设置」弹窗里填、随行级 POST / PUT 落库。选填：空串 = 没起名，统一列表的 `system_name` 先取本列、再回落 `account_name`。
    name = models.CharField("上家系统名称", max_length=100, blank=True, default="")
    password = models.CharField("密码", max_length=255)          # 只写不读
    enabled = models.BooleanField("启用同步", default=False)
    # ── 对接方式 ①登录式 / ②官方 Open Platform API（spec `docs/specs/2026-10-08-jidanpu-official-api-binding-design.md` §2 / §4.2）──
    #: ⭐ **这一列是「用哪套凭证」的唯一判据**（spec §2.1）。⛔ 判据一律写
    #: `if binding.mode == "openapi"`，⛔ **不许**写 `if binding.client_id:` 那种真值判据（E-0021 家族：切回登录式之后 `client_id` 还在 ⇒ 那一句会**静默走官方 API**）。⚠️ 两套凭证**允许同时留着**（切来切去不用重输）⇒「库里还剩一个旧密码」在 SQL 里看不出来。
    mode = models.CharField(
        "对接方式", max_length=16, choices=Mode.choices, default=Mode.PASSWORD,
    )
    #: 官方 API 的 `client_id`（spec §4.2）：它是**身份**、不是密钥 ⇒ **可读**（页面上要回显）。
    #: 长度 150 = 与 `account` 同一口径。
    client_id = models.CharField("官方API client_id", max_length=150, blank=True, default="")
    #: ⭐ **只写不读**（spec §4.2 / §13.3-C3）：序列化器侧 `write_only`、响应里连**键**都不出现
    #: （只回 `client_secret_set` 布尔）—— 与 `password` **同一套口径**（本类 `password` 那一行
    #: 尾部的注释「只写不读」与 `apps/business/api/jidanpu_upstream_views.py` 模块 docstring 都写着这条）。⚠️ 这一列**本身就是**不可读形态的承重面：它同时是回调验签用的那把密钥（spec §19.1 实测：`sign` 就是用 `client_secret` 签的）⇒ ⛔ 不加第二列回调密钥、⛔ 不进日志 / 不进异常文本。
    client_secret = models.CharField("官方API client_secret", max_length=255, blank=True, default="")
    #: 每份官方 API 绑定一串随机码（spec §5.1），拼进回调地址
    #: `https://<租户域名>/api/jidanpu/callback/<码>`。
    #: ⚠️ **唯一性靠下面那条部分唯一索引**，⛔ **不用** `unique=True`：多行 `""` 会互相撞（PostgreSQL 上 `''` **不是** NULL）⇒ 加不上去（spec §4.2 明文）。⚠️ 码由**我们**生成（`_ensure_callback_code`）、用户看不见 ⇒ ⛔ 别把它做成必填入口字段。
    callback_code = models.CharField("回调随机码", max_length=64, blank=True, default="")
    #: 这串码**什么时候发的**（spec §4.2；页面要显示「回调地址是什么时候生成的」）。可空、不回填。
    callback_code_at = models.DateTimeField("回调码生成时刻", null=True, blank=True)
    #: ⭐ 消费者的**水位**：上一次处理到哪个**接收时刻**（spec §5.5）。
    #: ⚠️ **内部状态**：⛔ 不进序列化器、⛔ 不上页面（照 `JidanpuMemberBinding.status_pull_at`
    #: 的既有先例）——它与 `callback_last_at` 一起回答「这台机器有没有人在消费」（spec §9.4）。
    callback_watermark_at = models.DateTimeField("回调消费水位", null=True, blank=True)
    #: 最近**真的**收到过一条**本绑定**的回调（健康列；spec §9.4 回答「对面还在推吗」）。
    #: ⚠️ 与 `last_ok_at` **不是一回事**：那是「同步成功」。⚠️ ⛔ 不许做成「多久没回调就报警」
    #: （低单量工作室两小时没单是正常的，spec §9.4 末段 / N9）。
    callback_last_at = models.DateTimeField("最近一次收到回调", null=True, blank=True)
    #: 累计处理过的回调条数（排查用；spec §4.2）：⛔ 它不是审计表，与 `callback_last_at` 同一次写入。
    callback_count = models.PositiveIntegerField("累计处理回调条数", default=0)
    #: ⭐ **已消费到的字节偏移**（生产性能缺陷修复）：事件文件（`events.jsonl`）
    #: 里**下一条还没读**的字节位置 ⇒ 消费者每轮从这里 `seek` 往后读，⛔ **不再把整份留档
    #: 从头扫一遍、全部解析进内存**（现网那个文件 198MB / 十几万行）。口径（唯一出处是 `apps.business.jidanpu_openapi_consume`）：同 `JidanpuMemberBinding`那一列的注释（两条线共用同一个引擎 ⇒ 列名 / 语义**逐字相同**，只差表）。
    callback_file_offset = models.BigIntegerField("已消费到的字节偏移", default=0)
    #: 官方 API 那条路的兜底对账间隔（spec §8.3；⚠️ 与登录式的 `hall_poll_seconds` /
    #: `my_orders_poll_seconds` 是**三件不同的事**，⛔ 别合并）。默认值从常量取（⛔ 不写字面量）。
    reconcile_seconds = models.PositiveIntegerField(
        "兜底对账间隔(秒)", default=UPSTREAM_OPENAPI_RECONCILE_SECONDS
    )
    #: 上家价的勾兑百分比（业务方裁定）：我方发单价（Order.hall_price = 打手接单能拿多少）= 上家 recFee × 本值 / 100；例：上家给 100、本值 50 ⇒ 打手拿 50（我方留 50）；本值 100 ⇒ 原价接手；⛔ 客户价（total_price）不受本值影响：它仍照抄 recFee（A9 / §15.1 那一半不变）。
    #: ⚠️ 与下家线的 JidanpuMemberBinding.throw_price_percent 方向相反（两条线零关系 ⇒ 读码时别把两列当同一个东西、也 ⛔ 不要「顺手统一」成同名）：下家线 throw_price_percent = 「往外地抛」时按比例打折——作用在我们给出的抛单价上（业务方）；本列 hall_price_percent = 「往里接」时按比例打折——作用在我们拿到的发单价上（业务方）；方向相反 ⇒ 复用一个列名会让注释与读码都对不上、故刻意另起名。
    #: 范围 1–100（⛔ 0 等于把一张免费单发进我方大厅、与 LAUNCH_NO_PRICE_REASON 同族口径）；默认 100 = 不打折（与引入本列之前的行为等价）；模型层与序列化器两头卡（照下家线 throw_price_percent 的既有做法；E-0021 同族：只在视图层卡、多一个写入点就又漏了）；⚠️ 兜底口径照下家线先例：读侧一律 binding.hall_price_percent or 100（校验器只在 full_clean() 跑、save()/queryset.update() 不跑 ⇒ shell 直写 0 要回落到 100 ⛔ 不让 0 生效）。
    hall_price_percent = models.PositiveSmallIntegerField(
        "发单价百分比(%)",
        default=100,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    game_rules = models.JSONField("按游戏同步规则", null=True, blank=True)
    # 存 0 = 配置写坏，闸门按本列默认值兜底。不要按「每轮都跑」，那会把常驻循环打成紧循环。
    # 不加 min/max 校验器。兜底吃「模型 + 列名」，不要写死另一张表的默认间隔。
    hall_poll_seconds = models.PositiveIntegerField("大厅拉取间隔(秒)", default=UPSTREAM_HALL_POLL_SECONDS)
    my_orders_poll_seconds = models.PositiveIntegerField("我的订单拉取间隔(秒)",
                                                         default=UPSTREAM_MY_ORDERS_POLL_SECONDS)
    identity_revision = models.PositiveIntegerField("绑定身份版本", default=0, db_default=0)
    hall_cursor = models.CharField("大厅水位(createTime)", max_length=64, blank=True, default="")
    my_orders_progress = models.JSONField("指派订单补拉进度", null=True, blank=True)
    last_run_at = models.DateTimeField("上次运行时刻", null=True, blank=True)
    last_ok_at = models.DateTimeField("上次成功时刻", null=True, blank=True)
    last_error = models.TextField("上次失败原因", blank=True, default="")
    last_error_at = models.DateTimeField("上次失败时刻", null=True, blank=True)
    checked_at = models.DateTimeField("上次检测通过时刻", null=True, blank=True)
    #: ⭐ D-0261：这份绑定配的订单来源字典项——上家单落池/转正（jidanpu_upstream.record_assigned_order / promote_upstream_mirror，E-0232 穷举的恰好两个建单点）建出的我方订单自动带 order_source + source 名字快照。
    #: ⚠️ 可空 = fail-soft：未配置 ⇒ 来的单照旧无来源（⛔ 不挡接单、⛔ 不回落到别的来源）。⚠️ SET_NULL：字典项被删（R8 允许）⇒ 绑定行的 FK 置空、后续单无来源；已有单不受影响（Order.order_source 同样 SET_NULL、但 Order.source 的名字快照留下——快照列就是为这一刻存在的）。
    #: ⚠️ related_name="+"（无反向消费）：没有「列出配了这个来源的绑定」的页面、建反向访问器就是死代码（照 OrderSource 已有的两个 related_name="+" 先例）。
    order_source = models.ForeignKey(
        OrderSource, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+", verbose_name="订单来源",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["jidanpu_base_url", "account"],
                                    name="uniq_jdp_upstream_account"),
            #: ⭐ 部分唯一索引（spec §4.2 / §4.4 第 2 步）：只对 mode="openapi" 的行生效。「部分」的条件为什么必须是这一条（两条都是实测过的后果）：
            #: ①存量与未来的登录式行全部 callback_code=""、而 PostgreSQL 里 '' 不是 NULL ⇒ 无条件的 unique=True / 全表唯一约束会让第二行登录式绑定直接建不上（既有行为被改坏、而迁移那一刻库里已经有这样的行）；
            #: ②码只有官方 API 那条路才用得到（回调地址只属于②式）⇒ 对①式行谈唯一性本身没意义。⚠️ 这条索引是承重的：没有它、回调地址的 22 位码会重复 ⇒ 同一个地址能命中两份绑定（spec §5.1 第 1 条「随机码存在的意义就是不可枚举且唯一」）。
            models.UniqueConstraint(fields=["callback_code"], condition=Q(mode="openapi"),
                                    name="uniq_jdp_upstream_callback_code"),
        ]

    def save(self, *args, **kwargs):
        """落库前补上官方 API 那条路必需的随机码（spec §4.3 第 2 行：「保存时自动生成」）。
        ⚠️ 为什么生成写在模型这一层：callback_code 是我们生成的、用户看不见 ⛔ 不是一个入口字段 ⇒ 漏生成的后果是静默的（回调地址里没有码 ⇒ 那条回调 404、而页面上一切正常）；放在这里任何写入点（序列化器/管理命令/shell）都覆盖到。
        ⚠️ 判据是 mode == "openapi"（⛔ 不是 client_id 真值判据，见 mode 那一列的注释）；⚠️ 已经有一串码的行不动它（换码 = 用户此前填进鸡蛋铺的地址失效、只有人能决定）。"""
        self._ensure_callback_code()
        if self._callback_code_generated and kwargs.get("update_fields") is not None:
            # ⚠️ 走 `update_fields` 的写入点（`jidanpu_upstream_views` 的 PUT 那一支）不补这一句，
            # 刚生成的码**不会落库** ⇒ 又一次静默形态。
            kwargs["update_fields"] = list(kwargs["update_fields"]) + [
                "callback_code", "callback_code_at",
            ]
        return super().save(*args, **kwargs)

    def _ensure_callback_code(self) -> None:
        """`mode="openapi"` 且还没有码 ⇒ 生成一串（幂等；①式行**一个字节都不写**）。"""
        self._callback_code_generated = False
        if self.mode != self.Mode.OPENAPI or self.callback_code:
            return
        # ⚠️ 用 `secrets`（密码学安全），⛔ 不用 `random`；⛔ 不用 uuid4().hex（长且不好看）；
        # ⛔ 不许把订单号 / 时间戳 / 用户 id / sharding 信息拼进码里（那会让它可枚举）——
        # 三条都是 spec §5.1 明写的。
        self.callback_code = secrets.token_urlsafe(UPSTREAM_CALLBACK_CODE_BYTES)
        self.callback_code_at = timezone.now()
        self._callback_code_generated = True

    def current_identity_rows(self):
        """验证手里的会话仍属于该绑定的当前身份。"""
        return type(self).objects.filter(
            pk=self.pk,
            jidanpu_base_url=self.jidanpu_base_url,
            account=self.account,
            mode=self.mode,
            identity_revision=self.identity_revision,
        )

    @property
    def base_url(self) -> str:
        """只读别名：让 `jidanpu.open_client` / `jidanpu.credentials_of` 直接可用（见类 docstring）。"""
        return self.jidanpu_base_url


class ActiveJidanpuUpstreamLinkManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(retired_at__isnull=True)


class JidanpuUpstreamOrderLink(models.Model):
    """上家个人绑定自己的链路。不要和代练员线的 JidanpuMemberOrderLink 合成一张；租户级 JidanpuOrderLink 已经删掉。in_hall = 「这张单此刻还挂在上家大厅」（本地标记）。
    ⚠️ 两条唯一约束都带 binding（⛔ 与租户级那条线的「远端单号全局唯一」不同）：不同上家地址是两套系统、单号各自生成 ⇒ 全局唯一会让两家上家的单互撞、而且报出来的是 IntegrityError 那种「像是我们写错了」的形态（spec §4.2 的补句）。"""

    binding = models.ForeignKey(JidanpuUpstreamBinding, on_delete=models.CASCADE, related_name="links")
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="jidanpu_upstream_links")
    upstream_order_id = models.CharField("上家订单ID", max_length=64)
    retired_at = models.DateTimeField("换绑隔离时间", null=True, blank=True)
    objects = ActiveJidanpuUpstreamLinkManager()
    all_objects = models.Manager()
    in_hall = models.BooleanField("上家大厅里挂着", default=False)
    #: ⭐ 连续「上家单号 + 来源单号都查不到」的计数（三连判定「上家已移除接手人」）。
    miss_count = models.PositiveSmallIntegerField("连续查不到次数", default=0)
    miss_since = models.DateTimeField("连续查不到起始时刻", null=True, blank=True)
    miss_at = models.DateTimeField("上次查不到时刻", null=True, blank=True)

    completed_submitted_at = models.DateTimeField("提交完成时刻", null=True, blank=True)
    terminated_at = models.DateTimeField("停止跟踪时刻", null=True, blank=True)
    last_in_at = models.DateTimeField("上次拉取时刻", null=True, blank=True)
    last_partial_probe_at = models.DateTimeField("上次未齐列表的单号核对时刻", null=True, blank=True)
    last_out_at = models.DateTimeField("上次推送时刻", null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["binding", "order"], condition=Q(retired_at__isnull=True),
                name="uniq_jdp_upstream_link_order",
            ),
            models.UniqueConstraint(
                fields=["binding", "upstream_order_id"], condition=Q(retired_at__isnull=True),
                name="uniq_jdp_upstream_link_remote",
            ),
        ]

    @property
    def is_terminated(self) -> bool:
        """终态判据（spec §4.2 / §5.4）：`terminated_at` 置上即停止跟踪。"""
        return self.terminated_at is not None


class JidanpuUpstreamHallOrder(models.Model):
    """上家大厅单在我方的镜像行（spec §4.3 选甲）。不是我方订单。
    ⚠️ 幂等键 = UniqueConstraint(upstream_base_url, upstream_order_id)：同一家上家的同一张单只有一行镜像；换一家上家 ⇒ 另一行（单号各自生成、会撞）。⚠️ 归属由 owner_binding 显式表达（＝当前负责拉大厅的那一份启用绑定，A2 的地址级去重）、且它是 SET_NULL：镜像行必须活下来（spec §8.4 第 3 行「删号不连带删镜像行」；停用/换绑时置空或转给同地址下一份）。
    promoted 不再复活：已经转成我方订单、复活会重复接单；gone 在任何一轮拉取里重新见到就恢复为 listed（清 gone_at、刷新投影、记台账）；rescue_gone_mirrors 只做存量批量兜底。"""

    class Status(models.TextChoices):
        LISTED = "listed", "已挂进大厅"
        CLAIMING = "claiming", "正在接单"
        PROMOTED = "promoted", "已转正"
        GONE = "gone", "已消失"
        SKIPPED = "skipped", "已跳过"

    upstream_base_url = models.CharField("上家鸡蛋铺地址", max_length=255)
    upstream_order_id = models.CharField("上家单号", max_length=64)
    owner_binding = models.ForeignKey(
        JidanpuUpstreamBinding, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="hall_orders", verbose_name="当前负责拉取的绑定",
    )
    status = models.CharField("状态", max_length=16, choices=Status.choices, default=Status.LISTED)
    #: 按游戏名四级匹配对上的我方游戏（spec §16.1）。对不上 ⇒ NULL + `skipped`（⛔ 不自动建游戏）。
    game = models.ForeignKey(
        Game, on_delete=models.PROTECT, null=True, blank=True, related_name="+", verbose_name="游戏",
    )
    remote_game_area = models.CharField("上家区", max_length=64, blank=True, default="")
    remote_game_server = models.CharField("上家服", max_length=64, blank=True, default="")
    project_text = models.CharField("项目名称", max_length=300, blank=True, default="")
    remote_remark = models.CharField("上家备注", max_length=255, blank=True, default="")
    remote_role_name = models.CharField("上家角色名", max_length=128, blank=True, default="")
    #: 上家 `recFee`。NULL = 上家没给价，原样留下这个事实。
    #: 大厅展示价缺价按 0.00 算，抢单走价格区间，不走「NULL 接单价」那一支。本列不参与那道闸。
    remote_rec_fee = models.DecimalField("上家接单价", max_digits=12, decimal_places=2,
                                         null=True, blank=True)
    hall_price_percent_at_sync = models.PositiveSmallIntegerField(
        "同步时发单价百分比", null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    remote_created_at = models.DateTimeField("上家发布时间", null=True, blank=True)
    #: 上家那一行**原样**（照它给的样子显示 + 排障用）。⛔ 不进列表响应。
    upstream_payload = models.JSONField("上家原始行", null=True, blank=True)
    claiming_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+", verbose_name="正在接单的我方打手",
    )
    claiming_at = models.DateTimeField("开始接单时刻", null=True, blank=True)
    promoted_order = models.OneToOneField(
        Order, on_delete=models.PROTECT, null=True, blank=True,
        related_name="jidanpu_upstream_hall_origin", verbose_name="转正后的我方订单",
    )
    gone_at = models.DateTimeField("离开上家大厅时刻", null=True, blank=True)
    last_error = models.CharField("最近一次失败原因", max_length=255, blank=True, default="")
    first_seen_at = models.DateTimeField("第一次挂进大厅时刻")
    last_seen_at = models.DateTimeField("最近一次仍在大厅时刻", null=True, blank=True)

    class Meta:
        verbose_name = "上家大厅镜像单"
        verbose_name_plural = verbose_name
        ordering = ["-first_seen_at", "-id"]
        # 大厅读口的部分索引（第五轮审计 N4 / framework W-02）：读口
        # `upstream_hall_queryset` 的形状就是「status='listed' + ORDER BY first_seen_at DESC,
        # id DESC」⇒ 索引必须带同一条件、同一列清单（只建 status 单列吃不到排序键）。⛔ 不给 first_seen_at 加裸 db_index：gone/promoted 历史行只增不减（W-04 保留期待拍板），全量 b-tree 会随历史行数一起稀释，部分索引把扫描集钉在「在厅行」上。
        indexes = [
            models.Index(
                fields=["-first_seen_at", "-id"],
                # ⚠️ 这里写不了嵌套的 `Status.LISTED`（类体不构成闭包，Meta 里看不到它）
                #    ⇒ 用字面量，与迁移 0125 的条件逐字同源。
                condition=models.Q(status="listed"),
                name="idx_jdp_upstream_hall_listed",
            ),
        ]
        constraints = [
            models.UniqueConstraint(fields=["upstream_base_url", "upstream_order_id"],
                                    name="uniq_jdp_upstream_hall_order"),
        ]

    def __str__(self) -> str:
        return f"上家大厅镜像单:{self.upstream_base_url}/{self.upstream_order_id}"


# ── 上下游订单互通（spec `docs/specs/2026-09-25-upstream-downstream-order-sync-design.md` §6）──
# ⚠️ 四张表全部落**租户 schema**，不带任何租户列（`AGENTS.md` §4）。
# ⚠️ `peer_account` **不是**唯一约束：同一个上家账号可以被本租户重复绑（第二次握手覆盖令牌）。⚠️ 名字前缀 `Peer` 是刻意的：⛔ 不要与鸡蛋铺那套 `Jidanpu*` 合并（两条独立的线，spec §15）。


class PeerUpstreamBinding(models.Model):
    """本租户作为下家的一条绑定（spec §6.1）。这一行就是页面上那五列（对标 HomeCompany / HomeHost / HomeAcc / UserName / BindTimeStr）加健康状态。
    令牌两列与 PeerDownstreamBinding 完全对称（spec §9-S3 / 裁决 R15）：token_in_hash = 对方向我带的那套（上家推单时带的 token_a）⇒ 只验证、存哈希；token_out = 我向对方带的那套（握手时我生成的 token_b）⇒ 解绑时要原样外发、存明文（存哈希会让两个方向都验不上）。"""

    peer_name = models.CharField("上家系统名称", max_length=20)
    peer_base_url = models.CharField("上家系统域名", max_length=255)
    peer_account = models.CharField("上家系统子账号", max_length=150)
    #: ⭐ **M-1（同步审计）：握手密码「验完即弃」，⛔ 不再存明文**。
    #: 这一列存的是能登录**上游租户后台**的真实口令（不是通道专用密钥），下游库一旦泄露
    #: 就等于直接登进上家——功能上不需要存（每次绑定强制重填，序列化器 `write_only`）⇒ 唯一写点已删（页面绑定的视图），存量明文由迁移清空（`password_set` 回填布尔后置空）。⚠️ 列本身**保留**（`blank=True, default=""`）：撤销列又是一张迁移，而空列已经没有读者；「库里无明文」的判据在 `tests/test_peer_binding_api.py` 与 `tests/test_peer_models.py`。
    password = models.CharField("握手用密码", max_length=255, blank=True, default="")
    #: ⭐ **M-1 同批**：页面「要不要重填密码」读**这一列**（布尔），⛔ 不再从明文列
    #: `bool(obj.password)` 推导。存量回填与清空都在迁移里一次做完。
    password_set = models.BooleanField("已填握手密码", default=False)
    token_in_hash = models.CharField("上家发给我的令牌（哈希）", max_length=128)
    token_out = models.CharField("我发给上家的令牌", max_length=128)
    local_base_url = models.CharField("我方域名", max_length=255)
    bound_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="peer_bindings", verbose_name="绑定操作人",
    )
    bound_at = models.DateTimeField("绑定时间", auto_now_add=True)
    enabled = models.BooleanField("启用同步", default=True)
    last_ok_at = models.DateTimeField("上次成功时刻", null=True, blank=True)
    last_error = models.TextField("上次失败原因", blank=True, default="")
    last_error_at = models.DateTimeField("上次失败时刻", null=True, blank=True)
    consecutive_failures = models.PositiveSmallIntegerField("连续失败次数", default=0)
    deleted_at = models.DateTimeField("解绑时间", null=True, blank=True)
    #: ⭐ **接收侧**的大厅镜像总开关（D-0303 的增量 P1，`default=False`）：**默认关** ⇒
    #: 关着 = 行为等同「没有这条功能」——`hall.*` 事件整批跳过、大厅读口不投影、
    #: 接单端点拒绝（三处闸同一条口径）。⚠️ 与上家侧的 `PeerDownstreamBinding.hall_push_enabled`（`default=True`）**方向相反**：那一列管「我推不推给别人」（推式、绑了就该看到），本列管「我收不收别人的」（收进来会把对端的大厅卡片摆到我方打手面前 ⇒ 默认关，业务方拍板）。
    hall_sync_enabled = models.BooleanField("同步上家抢单大厅", default=False)
    #: 大厅展示价的百分比（1–100、默认 100 = 原价）：打手看到的价格 = 上家发单价（PeerHallMirror.remote_hall_price）× 本值 / 100、两位小数 ROUND_HALF_UP；唯一算法出处 = peer_hall.display_price（⛔ 别在别处再算一份）。
    #: 范围 1–100（⛔ 0 = 白送一张单）：模型层与绑定页的 PUT 序列化器两头卡（照鸡蛋铺 JidanpuUpstreamBinding.hall_price_percent 的既有做法）。
    #: ⚠️ 校验器只在 full_clean() 跑、save() / queryset.update() 不跑 ⇒ 读侧 display_price 对「0 / 空」按 100 兜底（与鸡蛋铺那条读侧口径同源）。
    hall_price_percent = models.PositiveSmallIntegerField(
        "大厅价格百分比",
        default=100,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    game_rules = models.JSONField("按游戏同步规则", null=True, blank=True)
    direct_taker_assignment_enabled = models.BooleanField("允许上家直派打手", default=False, db_default=False)
    #: ⭐ 大厅全量拉取兜底的两列（计划 2026-10-25-peer-hall-pull §5）。
    #: hall_pull_requested_at = 接收开关 hall_sync_enabled 由关变开那一刻的持久标记：有值 ⇒ 我们还欠一次「从开关打开起算的完整全量」、只有完整走完一轮（游标到尽头、没撞预算/页数上限、本页无失败）才清掉它；它保证「翻转只触发一次全量」、不靠进程内存（重启进程不会把欠的那一次丢掉）。
    #: hall_pull_next_at = 下一次允许拉取的时刻（5 分钟节拍 / 失败 10 分钟退避 / 老对端 404 的 45 分钟 TTL）；NULL = 立即到期：迁移回填 NULL ⇒ 发版后既有的「开关已开」绑定会自动补一轮（计划 §5 / 三审 M5）；⛔ 不用 now.minute % 5 判节拍。
    hall_pull_requested_at = models.DateTimeField(
        "开关翻转后待全量拉取时刻", null=True, blank=True
    )
    hall_pull_next_at = models.DateTimeField("下次可拉取时刻", null=True, blank=True)
    #: ⭐ B2 / M-7+L-11（同步审计）：大厅慢路拉取的健康两列。
    #: hall_pull_last_ok_at = 最近一次完整走完一轮拉取的时刻：巡检脚本展示它（deploy/inspect_remote.sh）、几个月不前进 = 下家的大厅几个月没刷新——这在那之前是数据面上零信号的（L-11 的现场、只能翻 journalctl）。
    #: hall_pull_fail_count = 连续拉取失败轮数（完整成功一轮即清零+盖章）：数到 peer_worker.HALL_PULL_DEAD_AFTER_FAILURES（30、退避 10 分钟 ≈ 5 小时）就把这份绑定名下 listed 的镜像行判 gone（复用 peer_hall.unlist_binding_mirrors、可逆——上家恢复后「重见即复活」的既有机制照旧对它生效）；绑定本身不解：停绑是双向 kill、恢复要重走握手、判死只下架卡（M-7 的验收口径）。⚠️ 刻意不写 last_error / consecutive_failures：那两列是入站健康（接收事件的账、_record_inbound_health 写）与投递的账、拉取再记一本会把三本账搅成一团；拉取健康的出口 = 本两列 + 巡检展示。
    hall_pull_last_ok_at = models.DateTimeField(
        "上次完整拉取成功时刻", null=True, blank=True
    )
    hall_pull_fail_count = models.IntegerField("大厅拉取连续失败轮数", default=0)
    #: ⭐ D-0359（上家收款同步）：对端网页查询密码（用绑定子账号登录对端 /api/auth/login 查订单收款状态）。
    #: ⚠️ 与①式绑定 password 同一口径：明文存储、只写不读（序列化器 write_only、响应只回 receipt_query_password_set 布尔；PUT「空串 = 不改」照 jidanpu_upstream_views._BLANK_MEANS_KEEP 先例）。
    #: ⚠️ 同族同风险如实登记：本线不新造加密机制（与 JidanpuUpstreamBinding.password 同一档风险，计划 §2.7）；⛔ 不进日志 / 不进异常文案。
    receipt_query_password = models.CharField(
        "收款查询密码", max_length=255, blank=True, default=""
    )
    #: 「查询密码已配置」的布尔（页面 / 运维判断「要不要配」，⛔ 不从明文列真值推导）。
    receipt_query_password_set = models.BooleanField("已配收款查询密码", default=False)
    #: ⭐ 五项预检的持久标记（计划 §2.3「预检标记落点由实现代理给定，必须可核」）：五项 = ①对端子账号 active、②account_source 非 app（app 来源网页登录永久 401/1034）、③未开 restrict_to_self_created、④游戏作用域覆盖候选单、⑤域名 https。
    #: ⚠️ 三项（②③④）住对端库、我方运行期核不了 ⇒ 只能人工预检后置位；置位流程（RUNBOOK「上家收款同步」节）：配好查询密码 → 按 RUNBOOK 配方真机登录 + 查一单 → 五项全过 ⇒ PATCH 本列为 True。
    #: 承重闸：peer 通道「对端查无」只在 receipt_query_precheck_ok=True 时递增 Order.receipt_sync_notfound_count——对端读路径有两个静默回空闸（游戏作用域 / restrict_to_self_created，order_views.py:868 / order_flags.py:68）、未过预检的绑定查无可能是「看不见」而不是「没有」⇒ 查无只留痕、不烧放弃计数（M3）；⚠️ 与「查询密码已配」是两件事：配了密码 ≠ 预检过了。
    receipt_query_precheck_ok = models.BooleanField("收款查询预检通过", default=False)

    class Meta:
        verbose_name = "上下游绑定（我方为下家）"
        verbose_name_plural = verbose_name
        ordering = ["-bound_at", "-id"]

    def __str__(self) -> str:
        return f"上游绑定:{self.peer_name}"


class PeerDownstreamBinding(models.Model):
    """本租户作为上家的一条绑定（spec §6.2，没有界面）。
    ⚠️ account 是 OneToOne：一个账号只能被一个下家绑（spec A9，业务方裁定「不能」）。⚠️ token_in_hash 存哈希（我只验证、不发送）；token_out 存明文（每次推给下家要带）。"""

    class State(models.TextChoices):
        ACTIVE = "active", "生效"
        REVOKED = "revoked", "已解除"

    account = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="peer_downstream", verbose_name="下家用的账号",
    )
    peer_base_url = models.CharField("下家域名", max_length=255)
    token_in_hash = models.CharField("下家给我的令牌（哈希）", max_length=128)
    token_out = models.CharField("我发给下家的令牌", max_length=128)
    state = models.CharField(
        "状态", max_length=16, choices=State.choices, default=State.ACTIVE
    )
    bound_at = models.DateTimeField("绑定时间", auto_now_add=True)
    revoked_at = models.DateTimeField("解除时间", null=True, blank=True)
    last_ok_at = models.DateTimeField("上次成功时刻", null=True, blank=True)
    last_error = models.TextField("上次失败原因", blank=True, default="")
    last_error_at = models.DateTimeField("上次失败时刻", null=True, blank=True)
    consecutive_failures = models.PositiveSmallIntegerField("连续失败次数", default=0)
    #: 大厅镜像推送开关（D-0303，spec §5）：`default=True` ⇒ **存量绑定升级后即开始镜像**
    #: （业务方要的就是这个——绑定了就该看到对方的厅）。关掉 ⇒ 该下家不再收到
    #: `hall.throw` / `hall.take` / `hall.reprice`；已经投出去的行不受影响。
    hall_push_enabled = models.BooleanField("推送大厅镜像", default=True)

    class Meta:
        verbose_name = "上下游绑定（我方为上家）"
        verbose_name_plural = verbose_name
        ordering = ["-bound_at", "-id"]

    def __str__(self) -> str:
        return f"下游绑定:{self.peer_base_url}"


class PeerOrderLink(models.Model):
    """两边单号的对应（spec §6.3，技术去重的地基）。
    ⚠️ order 是 ForeignKey 不是 OneToOne：A→B→C 时 B 的一条单可以同时链上家与下家（spec §14-R4；用例 tests/test_peer_models.py::PeerOrderLinkConstraintTests::test_one_order_can_link_both_an_upstream_and_a_downstream 证明这条链真的能落库）。"""

    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="peer_links",
        verbose_name="团团通订单",
    )
    upstream_binding = models.ForeignKey(
        PeerUpstreamBinding, null=True, blank=True, on_delete=models.CASCADE,
        related_name="order_links", verbose_name="上游绑定",
    )
    downstream_binding = models.ForeignKey(
        PeerDownstreamBinding, null=True, blank=True, on_delete=models.CASCADE,
        related_name="order_links", verbose_name="下游绑定",
    )
    peer_order_no = models.CharField("对端单号", max_length=64)
    upstream_internal_order_no = models.CharField("上家系统单号", max_length=64, blank=True, default="", db_default="")
    #: ⭐ P4：这张单在链上最初那一跳的单号。同一张单在链上各租户眼里用它做共享身份（截图共享前缀按它分段）；首跳 = 对端单号本身（载荷里没有 source_order_no 时退回 peer_order_no）、其后的跳取载荷的 source_order_no。
    #: ⚠️ 精确的可达范围只有三级链（A→B→C）：B 建单时把 source_order_no 写成 A 的单号、B 推给 C 时送的就是 B 自己这一列 ⇒ C 存的仍是 A 的单号；第 4 级起（A→B→C→D）收到的是「上两级那个租户（祖辈）的单号」——实测 D 收到的是 B 的单号（不是 C）：发送侧送的是本单自己的 source_order_no、而中间租户那一列存的是它直接上家的单号、于是每传一跳只往前挪一级；该实测值由 tests/test_peer_p4_chain.py 的 ThreeTenantChainTests 钉住（已知残差）；⛔ 不要读成「任何链长都等于最初那一跳」、也不是「倒数第二个租户的单号」（A→B→C→D 下那会是 C、而实测是 B）。
    #: ⚠️ 与 peer_order_no 的分工是语义上的（⛔ 不是在数值上恒不相等）：本列记链的起点、peer_order_no 记直接上家——首跳与兜底时两者恰好同值；列宽 64 = peer_order_no 的宽度：值来自 payload["source_order_no"]（源列 128）或 peer_order_no（64）、写列前一律按列宽（从模型元数据取）截断 ⛔ 与本地生成的 Order.order_no（20）无关。
    origin_order_no = models.CharField("链上最初单号", max_length=64, blank=True, default="")
    last_in_at = models.DateTimeField("上次拉取时刻", null=True, blank=True)
    last_out_at = models.DateTimeField("上次推送时刻", null=True, blank=True)
    remote_action_at = models.DateTimeField("对端动作时刻", null=True, blank=True)
    local_action_at = models.DateTimeField("我方动作时刻", null=True, blank=True)
    #: 本地已应用到的事件时刻（spec §16.3）：为空 = P1 建的行，视为比任何事件都旧。
    last_applied_at = models.DateTimeField("本地已应用到的事件时刻", null=True, blank=True)
    hall_price_percent_at_sync = models.PositiveSmallIntegerField(
        "同步时发单价百分比", null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "上下游订单对应"
        verbose_name_plural = verbose_name
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(upstream_binding__isnull=False, downstream_binding__isnull=True)
                    | models.Q(upstream_binding__isnull=True, downstream_binding__isnull=False)
                ),
                name="peer_link_exactly_one_binding",
            ),
            models.UniqueConstraint(
                fields=["upstream_binding", "order"], name="uniq_peer_link_up_order"
            ),
            models.UniqueConstraint(
                fields=["upstream_binding", "peer_order_no"], name="uniq_peer_link_up_peer"
            ),
            models.UniqueConstraint(
                fields=["upstream_binding", "upstream_internal_order_no"],
                condition=~models.Q(upstream_internal_order_no=""), name="uniq_peer_link_up_internal_no",
            ),
            models.UniqueConstraint(
                fields=["downstream_binding", "order"], name="uniq_peer_link_down_order"
            ),
            models.UniqueConstraint(
                fields=["downstream_binding", "peer_order_no"], name="uniq_peer_link_down_peer"
            ),
        ]

    def __str__(self) -> str:
        return f"上下游订单对应:{self.peer_order_no}"


class PeerSyncEvent(models.Model):
    """outbox + 台账（一张表，spec §6.4）。
    ⚠️ 为什么不拆：outbox 与台账的生命周期同形（建→处理→落状态）、拆两张会造出一份必须同步的重复事实（同 JidanpuSyncEvent 的理由）。
    ⚠️ binding_id 存字符串 ⛔ 不建 FK：一张表要同时指向两种绑定（spec §6.4）。"""

    class Status(models.TextChoices):
        PENDING = "pending", "待投递"
        DONE = "done", "已投递"
        FAILED = "failed", "投递失败"

    binding_role = models.CharField("绑定角色", max_length=16)   # upstream / downstream
    binding_id = models.CharField("绑定标识", max_length=64)
    direction = models.CharField("方向", max_length=8)           # out / in
    kind = models.CharField("类型", max_length=32)
    target_type = models.CharField("对象类型", max_length=16)
    target_ref = models.CharField("对象标识", max_length=191)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField("尝试次数", default=0)
    reason = models.CharField("原因", max_length=255, blank=True, default="")
    payload = models.JSONField("载荷", null=True, blank=True)
    #: 事件级元数据（spec §16.2 / §16.3）：`{"occurred_at": ISO, "withdraw_reason": 枚举, "leave_reason": "assigned"}`。
    #: ⚠️ ⛔ 不要把**订单字段**塞进来——那些走 `payload` 的 §10.1 白名单。
    meta = models.JSONField("事件元数据", default=dict, blank=True)
    #: 下一次可投递时刻（spec §16.6 的退避）：为空 = 立刻可投；投递中被占用作租约。
    next_attempt_at = models.DateTimeField("下次可投递时刻", null=True, blank=True)
    idempotency_key = models.CharField("幂等键", max_length=64)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    processed_at = models.DateTimeField("处理时刻", null=True, blank=True)

    class Meta:
        verbose_name = "上下游同步台账"
        verbose_name_plural = verbose_name
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "created_at"], name="peer_event_status_idx"),
            models.Index(
                fields=["binding_role", "binding_id", "idempotency_key"],
                name="peer_event_idem_idx",
            ),
            models.Index(fields=["status", "next_attempt_at"], name="peer_event_due_idx"),
            #: ⭐ C10 / M-11：证据查询索引。
            #: 抢单判据的两个 exists()（_hall_throw_delivered：hall.throw 推送证据或 hall.pull 拉取证据）都按 (binding_role, binding_id, kind, target_ref) 打点——没有它、这一查在只增不减的台账上是全表扫（取活每轮都打）。
            #: ⚠️ 与下面那条部分唯一约束（uniq_peer_event_hall_pull）不是同一张表：唯一约束只在 kind='hall.pull' 行上（部分索引）、判据还要查 hall.throw ⇒ 两把都要有；⚠️ 非 _CONCURRENTLY 建索引有锁风险 ⇒ 迁移要在维护窗跑（口径见 docs/RUNBOOK.md「审计批 C」一节）。
            models.Index(
                fields=["binding_role", "binding_id", "kind", "target_ref"],
                name="peer_event_evidence_idx",
            ),
        ]
        constraints = [
            #: ⭐ Z20 拉取证据（计划 2026-10-25-peer-hall-pull §2）：上家大厅拉取端点为真正返回的每一张卡写一条 direction="in" / kind="hall.pull" 证据；抢单判据是「推送过 或 拉取过」；同一份绑定 + 同一张卡只留一行（bulk_create(ignore_conflicts=True) 依赖它防写放大）。
            #: ⚠️ 条件只覆盖 hall.pull：hall.throw 等台账行不受影响（它们本来就没有这个唯一性需求、且 kind 值域会继续长）。
            #: ⚠️ 形状照本模型既有的四条绑定约束（UniqueConstraint + condition）⛔ 不放进 indexes——test_peer_models 那张「三条索引的恰好集合」会红。
            models.UniqueConstraint(
                fields=["binding_role", "binding_id", "kind", "target_ref"],
                condition=models.Q(kind="hall.pull"),
                name="uniq_peer_event_hall_pull",
            ),
        ]

    def __str__(self) -> str:
        return f"上下游同步台账:{self.direction}/{self.kind}/{self.target_ref}"


class PeerHallMirror(models.Model):
    """团团通上家大厅在我方（下家）的镜像行（D-0303，spec docs/specs/2026-10-25-peer-hall-mirror.md §3.1）。不是我方订单。
    ⛔ 不复用 JidanpuUpstreamHallOrder：两条线零关系口径（鸡蛋铺上家线靠轮询拉取、按 recFee 勾兑价格；本线是推式、价格原样透传）——合并两张表会让两种取数形状共用一套判据、迟早互相打架（spec §3.1 点名）。⚠️ 幂等键 = UniqueConstraint(peer_base_url, peer_order_no)：同一家上家的同一张单只有一行镜像、换一家上家 ⇒ 另一行（单号各自生成、会撞）；peer_base_url 独立成列（⛔ 不经绑定 FK 取）：镜像行独立于绑定存活——解绑/停用后行照旧在（照鸡蛋铺「删号不删行」口径，spec §5）、只是不再有新事件来更新它。
    ⭐ 终态口径从第一天对齐 D-0300（⛔ 不写「终态不复活」的旧口径）：gone 的行重新见到（再来 hall.throw / 铺底重见）⇒ 自动复活回 listed（条件更新，落法照 jidanpu_upstream.sync_hall 行循环的那一支）；promoted 永不复活（已转成我方正式订单、复活会造成重复接单）；claiming 不动（它的收口在 claim 应答链上）。⚠️ skipped 在本线没有写入点（如实登记）：鸡蛋铺线的 skipped 是「游戏四级匹配对不上」那一档；本线游戏按名字精确传、解析不到就自动建（A14 resolve_game）、建不出无名游戏的形状在接收侧已被 bad_shape 硬跳过、落不下行 ⇒ 这个枚举值保留（spec §3.1 点名）但 v1 无写点。"""

    class Status(models.TextChoices):
        LISTED = "listed", "已挂进大厅"
        CLAIMING = "claiming", "正在接单"
        PROMOTED = "promoted", "已转正"
        GONE = "gone", "已消失"
        SKIPPED = "skipped", "已跳过"

    peer_upstream_binding = models.ForeignKey(
        PeerUpstreamBinding, on_delete=models.PROTECT, related_name="hall_mirrors",
        verbose_name="归属的上家绑定",
    )
    #: 列宽照 `PeerUpstreamBinding.peer_base_url`（255）：值就是从那一列抄下来的快照。
    peer_base_url = models.CharField("上家系统域名", max_length=255)
    #: 列宽照 `PeerOrderLink.peer_order_no`（64）：本线的 `target_ref` 就是它，
    #: 接收侧的查找键形状闸按这一列判（⛔ 绝不截断查找键，同 `_reference_key_shape_error`）。
    peer_order_no = models.CharField("上家单号", max_length=64)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.LISTED
    )
    #: ⭐ P5：这张卡在转发链上的跳数（源头 = 0、每往下转发一次 +1）。取值来自上游 hall.throw 载荷里的 hop（纯控制字段、不是 PII）：缺失/坏形状 ⇒ 0（单跳/老版本对端的行为逐字不变）；本行再往下广播时载荷带 hop + 1（peer_dispatch.mirror_hall_payload）。
    #: ⚠️ 承重（share-design §3「环与深度」）：两个租户互绑时大厅会无限互相镜像 ⇒ peer_inbound 在 hop > PEER_HALL_HOP_LIMIT（5）时不建卡、不转发、只记一条 peer.hall_hop_limit 台账。
    hop = models.PositiveSmallIntegerField("转发跳数", default=0)
    #: 收方 `resolve_game`（A14 自动建）对上的我方游戏，照 peer 落单同款。
    game = models.ForeignKey(
        Game, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+", verbose_name="游戏",
    )
    #: 上家行原样（`peer_sync.order_payload` 的产物）。照鸡蛋铺 `upstream_payload` 口径。
    payload = models.JSONField("上家原始行", null=True, blank=True)
    #: 上家发单价 = 我方展示接单价；**v1 不打折**（spec §6 待拍板项①）。
    #: ⚠️ `null=True`：上家没给价（`Order.hall_price` 可空）时它就是 NULL——原样存
    #: 「上家没给」这个事实；抢单闸走 `check_grab_limits` 的「NULL 接单价」那一支，⛔ 不学鸡蛋铺的「缺价按 0.00 算」（本线没有勾兑比例，无价就是无价）。
    remote_hall_price = models.DecimalField(
        "上家发单价", max_digits=12, decimal_places=2, null=True, blank=True
    )
    hall_price_percent_at_sync = models.PositiveSmallIntegerField(
        "同步时发单价百分比", null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )
    claiming_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="+", verbose_name="正在接单的我方打手",
    )
    claiming_at = models.DateTimeField("开始接单时刻", null=True, blank=True)
    #: ⭐ P4 二次转发（多跳，spec 2026-10-25-peer-hall-share-design.md §5.3.1 第 1 条）：发起这次接单的下一跳绑定（我方是中间层时才有值）。
    #: C→B→A 链上、C 在我方这张卡上点接单 ⇒ 本列 = C 的那份 PeerDownstreamBinding；交单时把真单指派给它的 account（account 是 C 在本租户的账号）、并按本层展示价推给 C。
    #: ⚠️ 与 claiming_by 互斥（写点不同：claim_peer_mirror 写 claiming_by、_forward_mirror_claim 写本列）：前者是「我方大厅里点单的人」、后者是「下游租户在要」；SET_NULL 照 spec 的列口径：绑定被删（下家解绑）不该把历史镜像行一起拖倒。
    claim_downstream = models.ForeignKey(
        "PeerDownstreamBinding", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="发起接单的下家绑定",
    )
    #: 本层**向上家转发**的那条 `hall.claim` 的标识（uuid hex；`blank` / `""` = 没有）。
    #: ⚠️ 只是**排障标识**，不参与判据：v1 协议的 claim 应答载荷是空对象
    #: （`peer_dispatch.enqueue_hall_claim_reply`），上家不会把它回传 ⇒ 别指望拿它对齐两边。与本列同生共死：写 = 开始转发、清 = 这一跳收口（refused / 回收 / 转正后不再用）。
    claim_id = models.CharField(
        "多跳转发的接单标识", max_length=64, blank=True, default=""
    )
    promoted_order = models.OneToOneField(
        Order, on_delete=models.PROTECT, null=True, blank=True,
        related_name="peer_hall_origin", verbose_name="转正后的我方订单",
    )
    gone_at = models.DateTimeField("离开上家大厅时刻", null=True, blank=True)
    last_error = models.CharField(
        "最近一次失败原因", max_length=255, blank=True, default=""
    )
    first_seen_at = models.DateTimeField("第一次挂进大厅时刻")
    last_seen_at = models.DateTimeField("最近一次仍在大厅时刻", null=True, blank=True)

    class Meta:
        verbose_name = "团团通上家大厅镜像单"
        verbose_name_plural = verbose_name
        ordering = ["-first_seen_at", "-id"]
        # 大厅读口的部分索引（第五轮审计 N4 / framework W-02）：读口
        # `peer_hall_queryset` 的形状就是「status='listed' + ORDER BY first_seen_at DESC,
        # id DESC」⇒ 索引必须带同一条件、同一列清单（照鸡蛋铺那张镜像表同形状）。下家拉取口（`peer_views` 的 hall.pull）按 `first_seen_at ASC, pk ASC` 排——b-tree 可反向扫，同一把索引也吃得到，不另建第二棵。
        indexes = [
            models.Index(
                fields=["-first_seen_at", "-id"],
                # ⚠️ 同鸡蛋铺那张表：Meta 里写不了嵌套的 `Status.LISTED` ⇒ 用字面量。
                condition=models.Q(status="listed"),
                name="idx_peer_hall_mirror_listed",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["peer_base_url", "peer_order_no"], name="uniq_peer_hall_mirror"
            ),
        ]

    def __str__(self) -> str:
        return f"团团通上家大厅镜像单:{self.peer_base_url}/{self.peer_order_no}"


class ScreenshotBrowserGrant(models.Model):
    """只允许给一笔订单上传进度截图的浏览器凭据。"""

    order = models.ForeignKey("business.Order", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    host = models.CharField(max_length=255)
    source_session_key = models.CharField(max_length=40)
    challenge_digest = models.CharField(max_length=64, unique=True)
    browser_digest = models.CharField(max_length=64, unique=True, null=True)
    expires_at = models.DateTimeField(db_index=True)


class OrderScreenshot(models.Model):
    """订单上的一张图（订单截图 / 群聊二维码）。spec §5.1。
    ⛔ 只存 object_key 不存 URL；⛔ 不新增 PII 列（对象名里不含玩家信息）。
    ⚠️ 此处不声明 Meta.ordering（M-6）：排序由两条读路径各自的 order_by("pk") 决定——列表（screenshot_views.py 的 list_items）与打包下载（screenshot_zip.py 的 download）都是最旧在前；此处不声明、免得声明与实现相反。"""

    IMG_ORDER = 0
    IMG_GROUP_QR = 1
    IMG_TYPE_CHOICES = ((IMG_ORDER, "订单截图"), (IMG_GROUP_QR, "群聊二维码"))

    STATUS_PENDING = "pending"
    STATUS_READY = "ready"
    STATUS_CHOICES = ((STATUS_PENDING, "待认领"), (STATUS_READY, "已认领"))

    order = models.ForeignKey("business.Order", on_delete=models.CASCADE, related_name="screenshots")
    img_type = models.SmallIntegerField("图片类型", choices=IMG_TYPE_CHOICES, default=IMG_ORDER)
    object_key = models.CharField("对象名", max_length=255, unique=True)
    #: ⭐ **外部幂等键**（例
    #: `dailiantong:<push_id>:<progress_id>`）。空串 = 本仓自己上传的截图（不参与外部去重）。
    #: 同一单的同一外部标识只允许一行（唯一约束见 `Meta.constraints`）。
    external_id = models.CharField("外部标识", max_length=64, blank=True, default="")
    original_name = models.CharField("原文件名", max_length=255, blank=True, default="")
    size_bytes = models.PositiveBigIntegerField("字节数", default=0)
    content_type = models.CharField("内容类型", max_length=64, blank=True, default="")
    remark = models.CharField("图片备注", max_length=50, blank=True, default="")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                    related_name="uploaded_screenshots")
    status = models.CharField("状态", max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    # ⚠️ 用 `auto_now_add=True`：**与本文件里其它 `created_at` 同一惯例**（`timezone` 未导入）。
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    ready_at = models.DateTimeField("认领时间", null=True, blank=True)

    class Meta:
        verbose_name = "订单截图"
        verbose_name_plural = "订单截图"
        constraints = [
            # ⭐ **每单最多一张 `ready` 群二维码**。
            # ⚠️ 条件里写**字面量** `1` / `"ready"`（嵌套 Meta 看不到外层类的
            #    `IMG_GROUP_QR` / `STATUS_READY` 命名空间，写它们会 `NameError`）。业务依据：一张单只需要一张群码（同口径先例 `api/my_orders_views.py`）；事故现场：17041 那张单的群码 2→4→8→…→193 行。
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(img_type=1, status="ready"),
                name="uniq_order_ready_group_qr",
            ),
            # ⭐ 外部同步图（代练通进度图）按「单 + 外部标识」去重；
            # 本仓自己上传的行 `external_id=""`，不参与这条约束。
            models.UniqueConstraint(
                fields=["order", "external_id"],
                condition=~models.Q(external_id=""),
                name="uniq_order_screenshot_external",
            ),
        ]

    def __str__(self) -> str:
        return f"订单截图:{self.order_id}/{self.object_key}"


# ⛔ **这里原来有 `class RemoteHallOrder`（远端大厅单在我方的镜像行，spec §3.1）——已随
#    租户级鸡蛋铺线整条拆除、按业务方裁定整表删除**（删表迁移 =
#    `0057_remove_jidanpuorderlink_order_and_more` 里的第三个 `DeleteModel`；内嵌枚举`SourceSystem` / `Status`、幂等键与索引随类一起消失，原文见 git 历史）。
class FeedbackStatus(models.TextChoices):
    """反馈的两态（spec `2026-10-08-feedback-design.md` §3）。"""

    PENDING = "pending", "未处理"
    HANDLED = "handled", "已处理"


class Feedback(models.Model):
    """一条问题反馈（业务方指令；租户库内，schema 即边界）。
    ⛔ 不加 tenant_id 列：本表在每个租户 schema 中各有一份、schema 就是隔离边界（与本模块其余业务实体同口径；先例是 tests/test_tenant_isolation.py 里「订单表没有 tenant_id 列」那条）。
    ⚠️ 两个外键的删除语义刻意不同、别统一：created_by 用 PROTECT——反馈是别人对系统的反馈、删员工账号不该连带删掉它（同族先例：Performance.user / Order.sales_user 都是 PROTECT；代价是「硬删一个提过反馈的用户」会抛 ProtectedError——账号线的移除走的是软删、所以这条路径只在数据层被直接删时才出现）；handled_by 用 SET_NULL——处理人走人就置空、反馈本身必须留着（它是审计痕迹）。"""

    content = models.TextField("反馈内容")
    status = models.CharField(
        "状态", max_length=16, choices=FeedbackStatus.choices, default=FeedbackStatus.PENDING
    )
    admin_note = models.TextField("管理员备注", blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="feedbacks", verbose_name="提交人",
    )
    created_at = models.DateTimeField("提交时间", auto_now_add=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="handled_feedbacks", verbose_name="处理人",
    )
    handled_at = models.DateTimeField("处理时间", null=True, blank=True)

    class Meta:
        verbose_name = "问题反馈"
        verbose_name_plural = "问题反馈"
        # 「最新的在最上面」：列表按它倒序（spec §4.2）。
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"反馈:{self.pk}"


class FeedbackImage(models.Model):
    """反馈截图：只存**对象名**（⛔ 不存 URL——URL 是签出来的，落库等于把会过期的链接写死）。"""

    feedback = models.ForeignKey(
        Feedback, on_delete=models.CASCADE, related_name="images", verbose_name="所属反馈"
    )
    object_key = models.CharField("对象名", max_length=255)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "反馈截图"
        verbose_name_plural = "反馈截图"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"反馈截图:{self.object_key}"


# ── 价格看板中心（设计稿 `docs/plans/2026-10-11-price-board-center.md` §2）──────
# ⛔ **四张表全部落租户 schema**（`apps.business` 在 TENANT_APPS 里），不带任何租户列：
#    与 `business_order` 同库同 schema ⇒ 统计不需要任何跨租户查询（`AGENTS.md` §1 / §4）。⛔ **订单写入路径一个字不改**：这四张表都是 `Order` 的**派生数据**，由定时任务写（设计稿 §3），录单路径上什么都不做。⚠️ **四张表建在同一个迁移里**（计划的硬要求），⛔ 不拆成四个。


class OrderTaggingStatus(models.TextChoices):
    """`OrderTagging` 的处理状态（设计稿 §2.1 表 A）。"""

    PENDING = "PENDING", "待归类"
    DONE = "DONE", "已归类"
    FAILED = "FAILED", "模型失败"


class ProjectTagDict(models.Model):
    """项目词典（设计稿 §2.2）：一个游戏内的标准项目名 + 它的别名。形态照 RecognitionKeyword（本模块上方那一个）：不加租户列、不记 updated_by（谁改的由操作日志回答）、不作软删之外的花样——作废就 is_active=False ⛔ 不删行（历史的 OrderProjectTag 还指着它）。
    ⚠️ 本表不由数据迁移预置：它是每个租户自己订单里长出来的业务内容（设计稿 §9.6「一租户一套词典」）、只由 import_project_tag_dict 命令灌进来 ⇒ 新租户开通时是空的（冷启动、已知且可接受）；照 RecognitionKeyword 那套「默认值在代码、租户只存覆盖」的是噪音词表（project_tag_rules.NOISE_PHRASES）与项目类别枚举 ⛔ 不是本表。
    ⚠️ name 里必须带规模（业务方明确要求，设计稿 §2.2 纪律 1）：丢了规模价格区间会被撑爆——实测「部门升级」合并后 P25=33 / P75=230、带上规模后收敛到 230~280；这是数据纪律、库层没有约束能守它、靠人工确认页（P2）。"""

    game = models.ForeignKey(
        Game, on_delete=models.PROTECT, related_name="project_tags", verbose_name="游戏"
    )
    name = models.CharField("标准项目名", max_length=128)
    #: 逗号分隔，与 `RecognitionKeyword.keywords` 同形。**允许空串**：只有一个名字、
    #: 没有任何别名时就是空的（合法且有意义，不是"没填"）。
    aliases = models.CharField("别名（逗号分隔）", max_length=255, blank=True, default="")
    #: 空串 = 还没分类（合法，不是"没填"，与 `aliases` 同口径）。
    #: ⚠️ `choices` 在**库层不构成约束**（Django 只在表单层校验），写入侧仍要自己传对的值。
    category = models.CharField(
        "类别", max_length=32, choices=PROJECT_TAG_CATEGORY_CHOICES, blank=True, default=""
    )
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "项目词典"
        verbose_name_plural = "项目词典"
        constraints = [
            # 一个游戏内标准名唯一（设计稿 §2.2）。⚠️ **不含 `game` 之外的作用域**：
            # 不同游戏的项目名互不相干（`5部门升4` 在 A 游戏与 B 游戏是两行）。
            models.UniqueConstraint(
                fields=["game", "name"], name="uniq_projecttagdict_game_name"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.game_id}:{self.name}"


class OrderTagging(models.Model):
    """一行一单：清洗结果 + 处理状态（设计稿 §2.1 表 A）。⚠️ 这张表就是「待归类队列」：status=PENDING 的行是定时任务②的输入；⛔ 不要为了表干净就不落行——否则「多少单没归上类」这个最重要的健康指标就看不见了（设计稿 §2.1 的原话）。
    ⚠️ content_clean 允许空串：实测约 12% 的订单正文整段都是客服模板、洗完什么都不剩（project_tag_cleaning 的模块 docstring 有这条口径与它的落点）；空串与 status=PENDING 一起出现是正常状态 ⛔ 不是异常、也不代表没跑过。
    ⚠️ game 是冗余列（order.game 就有）：设计稿 §2.1 明写它的目的是「避免统计时 join 订单」⇒ ⛔ 别为了"去掉冗余"把它删掉。"""

    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name="tagging", verbose_name="订单"
    )
    game = models.ForeignKey(
        Game, on_delete=models.PROTECT, related_name="order_taggings", verbose_name="游戏"
    )
    content_clean = models.TextField("清洗后的内容", blank=True, default="")
    status = models.CharField(
        "处理状态",
        max_length=20,
        choices=OrderTaggingStatus.choices,
        default=OrderTaggingStatus.PENDING,
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "订单归类"
        verbose_name_plural = "订单归类"
        indexes = [
            # 「捞 PENDING 的行」是定时任务②每一轮的入口（设计稿 §4 任务②）
            # ⇒ 给它一棵树。⚠️ 这条索引**不在设计稿的列清单里**，是执行期加的：
            # 那一句既然点明了这个访问形状，不建树会让队列扫描随表线性增长。
            models.Index(fields=["status"], name="idx_ptagging_status"),
        ]

    def __str__(self) -> str:
        return f"订单归类:{self.order_id}({self.status})"


class OrderProjectTag(models.Model):
    """一行一单×项目：归类结果 + 摊分金额（设计稿 §2.1 表 B）。⚠️ is_allocated 是如实登记的代价：True = 由组合单按样本均价摊分而来（估算、不是真实成交）、False = 单项单直接取原值；⛔ 看板不许把摊分值当真实成交价展示而不加区分（设计稿 §2.1 的硬要求）。
    ⚠️ 金额列不加「必须 > 0」的约束：项目口径 D-0015 之下 0 与负数都是合法值（退款/冲抵/调整，E-0021）⇒ 判「有没有值」一律 is not None ⛔ 不用真值判断。⚠️ allocated_hall 可空：Order.hall_price 本身可空 ⇒ 「这单没有发价」是真实状态、那时它是 NULL 而不是 0（0 是合法成交价 ⛔ 不许拿 0 冒充「没有」）；allocated_total 不可空：Order.total_price 有默认值、永不为 NULL。
    ⚠️ 本表只承载「已经归到某个项目」的行：匹配不上词典的订单不进这里、它停在 OrderTagging.status=PENDING（设计稿 §2.1 明写表 A 就是待归类队列）⇒ tag 因此不可空、UniqueConstraint(tagging, tag) 也就没有「NULL 不参与去重」的坑。"""

    tagging = models.ForeignKey(
        OrderTagging,
        on_delete=models.CASCADE,
        related_name="project_tags",
        verbose_name="订单归类",
    )
    tag = models.ForeignKey(
        ProjectTagDict,
        on_delete=models.PROTECT,
        related_name="order_tags",
        verbose_name="项目",
    )
    allocated_total = models.DecimalField("摊分后的接价", max_digits=12, decimal_places=2)
    allocated_hall = models.DecimalField(
        "摊分后的发价（发大厅价口径）",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    is_allocated = models.BooleanField("是否由组合单摊分而来", default=False)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "订单项目归类"
        verbose_name_plural = "订单项目归类"
        constraints = [
            # 同一单同一个项目不许重复（设计稿 §2.1）。
            models.UniqueConstraint(
                fields=["tagging", "tag"], name="uniq_orderprojecttag_tagging_tag"
            ),
        ]

    def __str__(self) -> str:
        return f"订单项目归类:{self.tagging_id}->{self.tag_id}"


class ProjectTagStat(models.Model):
    """预聚合的价格统计（设计稿 §2.3）：看板只读它。⚠️ 为什么必须预聚合：3 万单/月 ⇒ 一年 36 万行、实时跑 percentile_cont 全表扫约 1~2 秒、用户每开一次都转圈；预聚合后看板查询是毫秒级（设计稿 §2.3）。
    三条口径纪律（都是项目里踩过的坑）：①⛔ 不用均值、用中位数+四分位——实测同类项目价格能差 4 倍以上、均值会被带飞；②⛔ 判「有没有值」必须 is not None（D-0015：金额 0 与负数都合法，E-0021）⇒ 六个分位列全部可空：Order.hall_price 本身可空、一个窗口里可能一条发价样本都没有、那时 hp* 是 NULL 而不是 0（0 是合法成交价 ⛔ 不许拿它冒充「没有」）。
    ③⛔ 不存「没有样本」的行：sample_count 低于 MIN_SAMPLE_COUNT（=3，业务方定的）不落行、读层显示「样本不足」而不是给个数——实测很多项目只有 1~2 单、照它报价比不给更糟。"""

    game = models.ForeignKey(
        Game, on_delete=models.PROTECT, related_name="project_tag_stats", verbose_name="游戏"
    )
    tag = models.ForeignKey(
        ProjectTagDict,
        on_delete=models.PROTECT,
        related_name="stats",
        verbose_name="项目",
    )
    #: 五档：30 / 60 / 90 / 180 / 360（业务方定，设计稿 §8.1 ②）。
    #: ⚠️ `choices` 只是**文档性**的，库层不构成约束 ⇒ 写入侧自己要只传这五档。
    window_days = models.SmallIntegerField("时间窗（天）", choices=STAT_WINDOW_CHOICES)
    sample_count = models.IntegerField("样本量")
    tp25 = models.DecimalField("接价 P25", max_digits=12, decimal_places=2, null=True, blank=True)
    tp50 = models.DecimalField("接价 P50", max_digits=12, decimal_places=2, null=True, blank=True)
    tp75 = models.DecimalField("接价 P75", max_digits=12, decimal_places=2, null=True, blank=True)
    hp25 = models.DecimalField("发价 P25", max_digits=12, decimal_places=2, null=True, blank=True)
    hp50 = models.DecimalField("发价 P50", max_digits=12, decimal_places=2, null=True, blank=True)
    hp75 = models.DecimalField("发价 P75", max_digits=12, decimal_places=2, null=True, blank=True)
    computed_at = models.DateTimeField("计算时间", auto_now=True)

    class Meta:
        verbose_name = "项目价格统计"
        verbose_name_plural = "项目价格统计"
        constraints = [
            # 一个游戏 × 一个项目 × 一档时间窗只许一行（设计稿 §2.3）。
            models.UniqueConstraint(
                fields=["game", "tag", "window_days"],
                name="uniq_projecttagstat_game_tag_window",
            ),
        ]

    def __str__(self) -> str:
        return f"项目价格统计:{self.tag_id}@{self.window_days}天(n={self.sample_count})"


class UpstreamPriceChangeLog(models.Model):
    """上家改价留痕（spec 2026-10-17-order-log-names-and-upstream-price-log-design.md §3-D1）。业务方三问三答冻结的口径（spec §1）：只记上家改价实际动的那一列——order.update 改价支自 D-0307 起改的是客户价（Order.total_price）⇒ 记客户价旧→新、接手类支仍照抄发单价 ⇒ 记 Order.hall_price（⚠️ 同一列语义只由写入点决定：column 形参、note 写明「上家改价（客户价）」⛔ 别从金额大小反推是哪一列）；只记团团通↔团团通（唯一写入出口 apps/business/peer_inbound.py 的入向事件；鸡蛋铺/开放平台/闲管家/本地上改价都不写——判据 14 用源码扫描钉住）；只记值真的变了的变动（before == after 不写）；建单首次定价不写（没有「改前」）；历史无此类记录、不回溯（本线不写数据迁移）。
    ⛔ 不建 FK（spec §D1 末条）：订单可被软删、而对端单号不是主键；本表是留痕 ⛔ 不随订单改名/删除漂移 ⇒ 读面要判「这张单还在不在」只能读时回查（apps/business/api/upstream_price_log_views.py 的 is_deleted、一次批量查询）。
    ⚠️ 列宽从来源列取（spec §D1 的对照表）：Order.order_no 20 / Order.game_name 64 / Order.game_role_name 128 / PeerUpstreamBinding.peer_name 20 / Order.hall_price 12,2；写入侧一律经 peer_inbound._clip_to_column 按模型元数据截断（⛔ 不手抄数字）。"""

    order_no = models.CharField("系统订单号", max_length=20, db_index=True)
    game_name = models.CharField("游戏名称", max_length=64, blank=True)
    game_role_name = models.CharField("角色名", max_length=128, blank=True)
    #: 改前 / 改后**金额**（口径见类 docstring：D-0307 起 `order.update` 支记客户价、
    #: 接手类支记发单价）。可空：首次从「无价」变有价时 `before_amount` 是 `None`
    #: （页面显示 `—`，spec §6-4 如实登记）。⛔ 别拿 0 冒充「没有」（E-0021 同族：0 是合法成交价）。⚠️ 列 `verbose_name` 仍是「改前/改后发单价」——改它会产出一条迁移，而零迁移是硬门禁；页面标签随前端零改动一起留着，语义以本 docstring 与 `note` 列为准。
    before_amount = models.DecimalField(
        "改前发单价", max_digits=12, decimal_places=2, null=True, blank=True
    )
    after_amount = models.DecimalField(
        "改后发单价", max_digits=12, decimal_places=2, null=True, blank=True
    )
    source_name = models.CharField("上家系统名称", max_length=20, blank=True)
    note = models.CharField("说明", max_length=255, blank=True)
    #: 事件的业务时刻（⛔ **不是** `order.updated_at`）：取事件顶层 `occurred_at`，缺失回落 `now()`。
    occurred_at = models.DateTimeField("变动时间", db_index=True)
    created_at = models.DateTimeField("记录时间", auto_now_add=True)

    class Meta:
        verbose_name = "上家订单改价记录"
        verbose_name_plural = verbose_name
        #: 与端点固定排序 `-occurred_at, -id` 同源（同一时刻的多行靠 id 稳定 tie-break）。
        ordering = ["-occurred_at", "-id"]

    def __str__(self) -> str:
        return f"上家改价:{self.order_no}:{self.before_amount}→{self.after_amount}"


class GameDepositRule(models.Model):
    """按游戏配置的接单押金（打手 App 线）。
    为什么独立一张表、不往 Game 加列：Game 的列清单被几条「列恰好集合」的结构网钉着（改列要同一次改网）；押金是钱包域的规则、不是游戏自身的属性；独立表也让「这台机器还没配这条规则」有明确语义：没有行 = 用默认值（DEFAULT_DEPOSIT_AMOUNT，见 apps/accounts/wallet.py）。
    ⚠️ 改这个金额不影响已冻结的钱：冻结发生在接单那一刻、只增不减（解冻是唯一的反向动作、且要人主动申请）。"""

    game = models.OneToOneField(
        Game, on_delete=models.CASCADE, related_name="deposit_rule", verbose_name="游戏"
    )
    amount = models.DecimalField("押金金额", max_digits=12, decimal_places=2, default=50)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "游戏押金规则"
        verbose_name_plural = "游戏押金规则"

    def __str__(self):
        return f"GameDepositRule({self.game_id} {self.amount})"


class ChannelProfile(models.Model):
    """工作室频道简介（单行，频道文章作品线）。
    为什么单行、且不加外键：频道是整个租户的展示面（一个工作室 = 一个租户 = 一个 schema）、不是某个用户/某个对象的属性；用 pk=1 的约定行表达「本租户那份简介」、读侧一律走 ChannelProfile.objects.get_or_create(pk=1)——⛔ 不要给它加 OneToOneField(Tenant)：Tenant 在 public schema、本表在租户 schema、跨 schema 外键在 django-tenants 下不成立、而 schema 本身已经就是隔离边界（见 D-0006）。
    ⚠️ name（工作室名）不在这里存：它取 connection.tenant.name（唯一出处）、存一份副本就会漂——平台侧改了租户名、频道头部还挂着旧名字。⚠️ 简介是纯文本（≤200 字）、不是富文本：移动端只渲染一行两行的说明、富文本会带来一套它与正文各自独立的清洗口径。"""

    intro = models.CharField("频道简介", max_length=200, blank=True, default="")
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "频道资料"
        verbose_name_plural = "频道资料"

    def __str__(self) -> str:
        return f"ChannelProfile({self.intro[:20]})"


class ChannelArticle(models.Model):
    """频道文章作品。数据形态的两条硬口径（业务方确认）：正文 content_html 是白名单 HTML、落库前必须经 apps/business/article_content.sanitize_html 清洗（唯一出口）——⛔ 视图里不许再写第二份清洗/解析逻辑（写入与读取共用 article_content、否则两边会漂）；图片只存对象名（cover_key 与正文里的 <img data-key="…">）⛔ 不存 URL——URL 是签出来的、落库等于把会过期的链接写死（与头像/订单截图/游戏 logo 同一条口径，见 apps/core/storage.py 的模块 docstring）；正文里的 key 由 parse_blocks 在读时换成短期签名 URL、管理端编辑时走 media 字段。
    published_at 只写一次：首次发布时落时刻、之后下架回草稿、再发布都不覆盖它（「这篇什么时候第一次发出去」是历史事实、被覆盖就查不回来了）；下架只改 status ⛔ 不清 published_at（计划 §2 判据 4）。
    created_by 是用户名字符串而不是外键：文章是内容、不是凭据、作者账号被删/改名后这篇仍应挂在频道上（与 OperationLog.actor 的留痕口径同向）⛔ 别加 ForeignKey(settings.AUTH_USER_MODEL, on_delete=CASCADE)——那会连带删文章；read_count 只在公开详情读取时 +1（管理端 GET 不计数，计划 §2 判据 2）。"""

    class Status(models.TextChoices):
        """两档状态：草稿 / 已发布。⛔ 没有「定时发布」「回收站」——本线不做（计划 §3）。"""

        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"

    title = models.CharField("标题", max_length=80)
    intro = models.CharField("摘要", max_length=200, blank=True, default="")
    #: 只存对象名；空串 = 无封面（前端只判空串，⛔ 不用 `null`）。
    cover_key = models.CharField("封面对象名", max_length=255, blank=True, default="")
    #: 白名单 HTML（唯一清洗出口 `article_content.sanitize_html`）。
    content_html = models.TextField("正文", blank=True, default="")
    #: `db_index=True`：公开列表与详情都按它筛（读侧唯一的收窄条件）。
    status = models.CharField(
        "状态",
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    #: 首次发布时刻（只写一次；下架保留，见类的 docstring）。
    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    read_count = models.PositiveIntegerField("阅读数", default=0)
    created_by = models.CharField("发布人", max_length=150, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "频道文章"
        verbose_name_plural = "频道文章"
        #: 管理端列表的默认顺序；公开列表按 `-published_at, -id`（见 `channel_views`），
        #: 那是**另一个**事实（已发布那一批的先后），所以不写在这里。
        ordering = ["-updated_at", "-id"]

    def __str__(self) -> str:
        return f"ChannelArticle({self.pk}:{self.title})"


# ── 接单大厅：离厅快照 + 模拟单（D-0296）────────────────────
# ⛔ 两张表都落**租户 schema**（`apps.business` 在 TENANT_APPS 里），不带任何租户列。
# ⚠️ 写入点的**收拢口**是 `apps/business/hall_snapshot.py` / `hall_simulated.py`（⛔ 不把写入逻辑散在调用点）；模拟单**不建 `Order` 行、不进状态机 / 结算 / 对账**——它只是大厅里的一张演示卡，任何把它当真单接进资金面的路径都是洞。


class HallExitSnapshot(models.Model):
    """「离厅那一刻的大厅行」快照（灰卡的数据源；D-0296）。它是什么：一张单（原生单/上家镜像单）离开接单大厅的那一刻、把它在大厅里呈现的那一行（键集与 HallListSerializer 输出恰好同形，见 payload）原样存下来 ⇒ include_exited=1 的大厅读口把这一行作为灰卡排在可接单之后。
    写入点（枚举即契约；收拢口 = hall_snapshot.py）：原生单——apply_transition 成功转换且 from == WAITING_GRAB 且目标不是它 ⇒ claimed（进「代练中」）或 withdrawn（其余目标，含 /withdraw/、/transition/、批量、机器接手之外的一切出边）；原生单软删——recycle.soft_delete_order 首次删除分支（软删不经状态机、单也会从大厅消失 ⇒ 同样要留卡）；上家镜像——claim_upstream_mirror 的 listed → claiming 条件更新成功 ⇒ claimed、_mark_gone 仅当原状态是 listed 时（条件更新保证）⇒ withdrawn、claiming → promoted 不重复写（离开大厅的那一刻已经写过）；救回——rescue_gone_mirrors 把 gone 行复活回 listed ⇒ 删除该镜像的快照（显式 filter 删、防「一单双卡」）。
    ⚠️ 两个外键都 SET_NULL 可空：快照是历史事实、不阻止订单/镜像行被清理；source 键回答「这一行原本是哪一路的」；⚠️ 快照不是订单、它不参与任何资金面。"""

    class Source(models.TextChoices):
        NATIVE = "native", "原生单"
        UPSTREAM = "upstream", "上家镜像单"

    class ExitKind(models.TextChoices):
        CLAIMED = "claimed", "被接走"
        WITHDRAWN = "withdrawn", "被撤回 / 离厅"

    order = models.ForeignKey(
        Order, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="hall_exit_snapshots", verbose_name="原生订单",
    )
    mirror = models.ForeignKey(
        JidanpuUpstreamHallOrder, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="hall_exit_snapshots", verbose_name="上家镜像单",
    )
    source = models.CharField(
        "行来源", max_length=16, choices=Source.choices,
    )
    exit_kind = models.CharField(
        "离厅方式", max_length=16, choices=ExitKind.choices,
    )
    game = models.ForeignKey(
        Game, on_delete=models.PROTECT, related_name="+", verbose_name="游戏",
    )
    #: 离厅那一刻的**大厅行载荷**（键集与 `HallListSerializer` 输出恰好同形）。
    #: ⚠️ 存的是「在厅视图」：`status` 恒 `WAITING_GRAB`、`taker_name` 空、
    #: `hall_price` 是在厅展示价——覆写逻辑与理由见 `hall_snapshot.py`。
    payload = models.JSONField("离厅时的大厅行载荷")
    exited_at = models.DateTimeField("离厅时刻", db_index=True, default=timezone.now)

    class Meta:
        verbose_name = "大厅离厅快照"
        verbose_name_plural = verbose_name
        ordering = ["-exited_at"]

    def __str__(self) -> str:
        return f"HallExitSnapshot({self.source}:{self.pk})"


class HallSimulatedOrder(models.Model):
    """大厅模拟单（演示卡；D-0296）。它是什么 / 不是什么：一张仿真的大厅卡——按本租户历史订单的价格与项目形态生成、让大厅不空；⛔ 不建 Order 行、不进状态机/结算/对账：它没有 MAIN 绩效行、没有客户，接它的「假接手」端点只把卡片置 retired 并回「已被接手」⛔ 不会产生任何资金动作。
    ⚠️ 零 PII：payload 里的客户联系方式一律空串 / null（生成侧 hall_simulated.py 保证）。"""

    class Status(models.TextChoices):
        ACTIVE = "active", "在厅"
        RETIRED = "retired", "已离厅"

    game = models.ForeignKey(
        Game, on_delete=models.PROTECT,
        related_name="simulated_hall_orders", verbose_name="游戏",
    )
    #: 20 位仿真号（形状照 `Order.order_no` 的生成规则；唯一约束在本表，且生成侧保证
    #: **不与 `Order.order_no` 相撞**——判据见 `hall_simulated._unique_display_order_no`）。
    display_order_no = models.CharField("展示单号", max_length=20, unique=True)
    server = models.ForeignKey(
        GameServer, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="区服",
    )
    project_text = models.TextField("项目名称")
    price = models.DecimalField("大厅展示价", max_digits=12, decimal_places=2)
    is_urgent = models.BooleanField("是否加急", default=False)
    time_requirement = models.CharField("时间要求", max_length=64, blank=True, default="")
    remark = models.TextField("备注", blank=True, default="")
    #: 复刻来源（合成卡不是凭空造的：项目形态来自真实历史单）。源单被删 ⇒ 卡照常活着。
    source_order = models.ForeignKey(
        Order, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="simulated_hall_orders", verbose_name="复刻来源订单",
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.ACTIVE,
        db_index=True,
    )
    generated_at = models.DateTimeField("生成时刻", default=timezone.now, db_index=True)
    retired_at = models.DateTimeField("离厅时刻", null=True, blank=True)
    #: 大厅行载荷（键集与 `HallListSerializer` 输出恰好同形；生成侧构建，零 PII）。
    payload = models.JSONField("大厅行载荷")

    class Meta:
        verbose_name = "大厅模拟单"
        verbose_name_plural = verbose_name
        ordering = ["-generated_at"]

    def __str__(self) -> str:
        return f"HallSimulatedOrder({self.display_order_no}:{self.status})"


# ── 交易平台对接设置（「发平台设置 → 交易平台对接设置」页）──
# 计划：docs/plans/2026-10-26-platform-integration-settings.md T1；侦察（不进 git）：.superpowers/recon/dailiantong-config-page.md。
# ⛔ 两张表都落租户 schema（apps.business 在 TENANT_APPS 里）、不带任何租户列。字段取值与文案的证据件是 src/frontend/public/replica/system-tripartiteplatformsetting.html、每个字段的 help_text 都给了 文件:行；⚠️ 证据件里没有的字段 ⛔ 不要凭「行业惯例」补。🔴 凭据红线（AGENTS.md §4）：app_secret / pay_password_md5 只写不读（接口层用 write_only + *_set 布尔）⛔ 不进 __str__ / repr / 日志 / 异常文案。


class PlatformIntegration(models.Model):
    """平台对接账号：一行 = 本租户在某个交易平台上的一个发单账号（多行）。
    行数语义：同一 platform 允许多行——证据件 :13 的引文原文就是「允许相同平台添加多个发单账号」（⚠️ 那句是纯文本、同一份证据件里没有与它对应的开关/name/value（侦察报告 §1.4）⇒ ⛔ 不要把它做成可配置项）；同一 platform 下 is_default=True 至多一行——证据件 :29 的开关提示原文是「相同平台只能有1个默认账号」、该约束由 Meta.constraints 的部分唯一索引在数据库层保证（不是接口层的君子协定）。
    凭据：app_secret 是平台给的密钥、只写不读（写入口径由接口层保证；模型照存）；pay_password_md5 只存 MD5(支付密码) 这一层、不存明文——算法与来源见该字段注释。平台 id 取自 src/frontend/src/config/platforms.ts:16-30（本仓「指定发平台」字典）、代练通 = 10；⚠️ 证据件 :68-87 的「要对接的平台」radio 只渲染了 7 个（10/30/80/90/93/50/60）；本枚举按计划 T1 的要求取 platforms.ts 的 13 个值——多出来的 6 个只表示「页面暂不提供对接」、不改变 platforms.ts 的语义（计划 §1 第 4 条：页面选项照抄 7 个、只有代练通能真保存）。"""

    class Platform(models.IntegerChoices):
        """平台 id 与文案：逐字照 `src/frontend/src/config/platforms.ts:16-30`。"""

        QUANNENGDAI = 8, "全能代"
        DAILIANTONG = 10, "代练通"
        DAILIANMAMA = 30, "代练妈妈"
        GULU = 50, "咕噜代练"
        YIDAILIAN = 60, "易代练"
        DAILIANBAO = 70, "代练宝"
        DAILIANWANZI = 80, "代练丸子"
        QUIDAILIAN = 90, "趣代练"
        DAILIANDUO = 91, "代练多"
        DAIYOUTONG = 92, "代游通"
        TIANWAN = 93, "天玩代练"
        TONGTIAN = 94, "通天代练"
        XIANGDAILIAN = 95, "享代练"

    #: 证据件 `:68-87` 的 radio 组「要对接的平台」；**没有默认值**——保存前必须选平台
    #: （对标校验：`tripartiteplatformsetting.js:340-349`，侦察报告 §1.2）。
    #: `db_index=True`：列表按平台筛选 / 按平台排序都走它（`Meta.ordering` 的首列）。
    platform = models.PositiveSmallIntegerField(
        "平台", choices=Platform.choices, db_index=True
    )
    #: 证据件 `:88-96`（`plaftacc`，对标 maxlength 30；本列按计划 T1 取 64）。
    account = models.CharField("平台账号/ID", max_length=64)
    #: 证据件 `:109-114`（`appId`，对标 maxlength 20；本列按计划 T1 取 64）。
    app_id = models.CharField("AppId", max_length=64, blank=True, default="")
    #: 证据件 `:115-120`（`appSecret`，输入框 `maxlength=1000`）⇒ 本列取 1000。
    #: ⛔ 不要按同类凭据列的「128」惯例收窄：真实密钥一旦超长会在写库时报
    #: `DataError`（接口层表现为 500），而改列要**逐租户库**再跑一次迁移。⛔ **只写不读**：接口层（T2）用 `write_only` 并只回 `has_secret` 布尔。空串 = 未配置；编辑留空 = 不改（既有先例`apps/business/api/serializers.py` 的 `XgjShopBinding` 段）。
    app_secret = models.CharField(
        "AppSecret",
        max_length=1000,
        blank=True,
        default="",
        help_text="只写不读：保存后接口 / 日志 / __str__ 都不回显。",
    )
    #: 算法：MD5(支付密码) 的 32 位小写十六进制；不存明文、不回显。
    #: 来源：docs/DAILIANTONG-API.md §1「支付密码」一行——PayPass = MD5( MD5(支付密码) + 商户UserID )（32 位小写）；该文档同一节写明「只需存 MD5(支付密码) 这一层」⇒ 本列存内层摘要、发单时再用商户 UserID 拼出 PayPass；明文永不入库、永不进日志。
    #: 证据件 :103-108（plaftpaypwd，新增时「请输入交易平台的支付密码」、编辑时「若没有改过，请不要动此处」）；列表第 4 列 isFillPlatmPayPwd 的函数模板读 has_password（src/frontend/src/layui-island/pages/system-tripartiteplatformsetting.ts:302-315）；空串 = 还没设置。
    pay_password_md5 = models.CharField(
        "支付密码摘要",
        max_length=32,
        blank=True,
        default="",
        help_text="MD5(支付密码) 的 32 位小写十六进制；算法与来源见本字段上方的注释。",
    )
    #: 证据件 `:20-25`（`#switchTpl1`：`status == 0` 才 checked ⇒ 对标 0=启用）。
    #: 本列改名成正向布尔：True = 启用。
    #: ⚠️ 默认 True 是**本仓自定：新建即启用**——证据件与实施计划都没给「新增行时的初值」。
    is_enabled = models.BooleanField("是否启用", default=True)
    #: 证据件 `:26-31`（`#switchTpl5`：`isDefault == 'Y'` 才 checked）。
    is_default = models.BooleanField("默认账号", default=False)
    #: 证据件 `:32-37`（`#switchTpl2`：`isAutoAccep == 0` 才 checked ⇒ 对标 0=自动验收）。
    #: ⚠️ 对标的**原始值方向与正向布尔相反**；本列 True = 同步验收（自动）。
    sync_accept = models.BooleanField("同步验收", default=False)
    #: 证据件 `:38-43`（`#switchTpl3`：`isNotifyPushMan == 1` 才 checked ⇒ 1=需要短信）。
    sms_notify = models.BooleanField("接单短信通知", default=False)
    #: 证据件 `:44-49`（`#switchTpl4`：`isAutoSelect == 0` 才 checked ⇒ 对标 0=包含）。
    #: ⚠️ 同 `sync_accept`：对标原始值方向相反；本列 True = 发单时包含本平台。
    include_push = models.BooleanField("发单包含", default=False)
    #: 证据件 `:127-133`（`concactQQ`，对标 maxlength 50；本列按计划 T1 取 64）。
    contact_qq = models.CharField(
        "发单联系QQ", max_length=64, blank=True, default=""
    )
    #: 证据件 `:134-139`（`concactPhone`，对标 maxlength 50；本列按计划 T1 取 64）。
    contact_phone = models.CharField(
        "发单联系电话", max_length=64, blank=True, default=""
    )
    #: 证据件 `:121-126`（`remark`，对标 maxlength 100；本列按计划 T1 取 255）。
    remark = models.CharField("备注说明", max_length=255, blank=True, default="")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "平台对接账号"
        verbose_name_plural = verbose_name
        ordering = ["platform", "id"]
        constraints = [
            #: 同平台至多一个默认账号（证据件 `:29`「相同平台只能有1个默认账号」）。
            #: ⚠️ 条件只含 `is_default=True` ⇒ 同平台**可以**有很多非默认行。
            models.UniqueConstraint(
                fields=["platform"],
                condition=Q(is_default=True),
                name="uniq_platform_integration_default",
            ),
        ]

    def __str__(self) -> str:
        # ⛔ 凭据（`app_secret` / `pay_password_md5`）一律不出现在这里。
        return f"PlatformIntegration({self.get_platform_display()}:{self.account})"


class PlatformPushSettings(models.Model):
    """「这里的设置项会针对所有平台生效」——9 组单选（每租户单行）。证据件：src/frontend/public/replica/system-tripartiteplatformsetting.html:149-247；业务方口中的「6 项」= 顶层编号 1–6、其中 1.1 / 1.2 属 1、2.1 属 2 ⇒ 实际 9 组 radio（侦察报告 §1.3）。
    为什么单行、怎么取：照 QQBotBinding.get_solo() / ChannelProfile(pk=1) 的既有形态——pk=1 约定行、读侧一律 get_solo()（get_or_create、并发下也不会出第二行）⛔ 不手工建多行；⚠️ 与 SystemSwitch 的「只存改过的覆盖行」不同：本表是整行 9 列的配置行——页面每一组都要回填真值、不是「没有行就用注册表默认」。值编码（⚠️ 两套 ⛔ 不要强行归一）：证据件同一块里混用了 Y/N 与 0/1——Y/N 的 4 组落成 BooleanField（True = Y）、0/1 的 5 组落成带 choices 的 SmallIntegerField（原值存 0 / 1）；⚠️ 每组「哪个值算开」看字段 help_text：证据件里 0 有时是「否」（SK156 / SK157）、有时是「不限制 / 不发送」（SK155 / SK158）、SK159 的 0 反而是「检测」——⛔ 不要按「0 = 关」的直觉取反。
    本版语义（一键抛单计划 §3 的「生效范围总表」）：业务方曾拍板「9 组只存不生效」（计划 §1 第 3 条）；第 6 条不再只存：auto_send_group_qr_after_accept 是「接手后自动发送」的总闸（见 apps.business.dailiantong_qr）；⭐（H3-1）第 4 条也不再只存：auto_zero_refund_on_conflict 是「撞单 0 撤」的总闸——每轮 dailiantong_sync 在回读之后扫「同一订单下多条已接手（远端 12/13）的推送」、保留最早接单那条、其余全部 LevelOrderCancel(Flag=0, PayLevelBal=0, RepEnsureBal=0)（实现见 apps.business.dailiantong_push.zero_refund_conflicting_pushes）、开关关 ⇒ 该段零上游调用；其余 7 组（quick_push_enabled / legacy_simple_mode / check_game_region / forbid_push_when_double_gold_zero / duplicate_push_check / select_jidanpu_center_by_default / block_push_completed_orders）仍只存不生效、页面上必须分区标明（计划 §3）；另外三列（default_power_level / default_limit_accept / require_template）是真生效的：发单页默认值 + 发单时真的写进上游载荷（apps.business.dailiantong_push.push_order）。"""

    class YesNo(models.IntegerChoices):
        """`0/1` 组的「否 / 是」（证据件 SK156 / SK157 的标题就是「否」「是」）。"""

        NO = 0, "否"
        YES = 1, "是"

    class DuplicateCheck(models.IntegerChoices):
        """`0/1` 组：`SK159`（`0` = 检测，`1` = 不做检测）。"""

        DETECT = 0, "检测"
        SKIP = 1, "不做检测"

    class CompletedPushLimit(models.IntegerChoices):
        """`0/1` 组：`SK155`（`0` = 不限制，`1` = 限制）。"""

        UNLIMITED = 0, "不限制"
        LIMIT = 1, "限制"

    class GroupQrSend(models.IntegerChoices):
        """`0/1` 组：`SK158`（`0` = 不发送，`1` = 自动发）。"""

        NONE = 0, "不发送"
        AUTO = 1, "自动发"

    #: 1、录单页面是否需要支持快速发单到交易平台。证据件 `:159-160`（`SK132`：
    #: `N` = 关 / `Y` = 开，对标默认 `Y` 开）⇒ True = 开，默认 True。
    quick_push_enabled = models.BooleanField(
        "录单页快速发单",
        default=True,
        help_text="证据件 SK132（html:159-160）：Y=开（默认）/ N=关。",
    )
    #: 1.1、发单到交易平台界面是否需要启用传统简易模式。证据件 `:168-169`
    #: （`SK156`：`0` = 否（默认）/ `1` = 是）。
    legacy_simple_mode = models.SmallIntegerField(
        "传统简易模式",
        choices=YesNo.choices,
        default=YesNo.NO,
        help_text="证据件 SK156（html:168-169）：0=否（默认）/ 1=是。",
    )
    #: 1.2、发单到交易平台时是否需要检测游戏大区准确性。证据件 `:177-178`
    #: （`SK157`：`0` = 否（默认）/ `1` = 是）。
    check_game_region = models.SmallIntegerField(
        "检测游戏大区准确性",
        choices=YesNo.choices,
        default=YesNo.NO,
        help_text="证据件 SK157（html:177-178）：0=否（默认）/ 1=是。",
    )
    #: 2、发单时若双金任意一个为 0 是否禁止发出。证据件 `:186-187`
    #: （`SK107`：`Y` = 禁止发单 / `N` = 不限制（默认））⇒ True = 禁止发单，默认 False。
    forbid_push_when_double_gold_zero = models.BooleanField(
        "双金为 0 禁止发单",
        default=False,
        help_text="证据件 SK107（html:186-187）：True=Y=禁止发单 / False=N=不限制（默认）。",
    )
    #: 2.1、发相同平台的订单是否需要检测重复发单。证据件 `:197-198`
    #: （`SK159`：`0` = 检测（默认）/ `1` = 不做检测）。
    duplicate_push_check = models.SmallIntegerField(
        "检测重复发单",
        choices=DuplicateCheck.choices,
        default=DuplicateCheck.DETECT,
        help_text="证据件 SK159（html:197-198）：0=检测（默认）/ 1=不做检测。",
    )
    #: 3、发单时是否默认勾选鸡蛋铺交易中心。证据件 `:207-208`
    #: （`SK106`：`Y` = 勾选 / `N` = 不勾选（默认））⇒ True = 勾选，默认 False。
    select_jidanpu_center_by_default = models.BooleanField(
        "默认勾选鸡蛋铺交易中心",
        default=False,
        help_text="证据件 SK106（html:207-208）：True=Y=勾选 / False=N=不勾选（默认）。",
    )
    #: 4、撞单时除最早接手的平台外、其它平台是否全申请 0 撤。证据件 :216-217（SK145：Y = 0撤（默认）/ N = 不自动处理撤销）⇒ True = 0撤、默认 True。
    #: ⭐ 真生效（H3-1）：唯一读取点是 apps.business.dailiantong_push.zero_refund_conflicting_pushes（每轮 dailiantong_sync 的一段）；False ⇒ 那一段什么都不做（零上游调用）。
    #: ⚠️ 默认值沿用证据件的 Y（⛔ 不因为「这是自动撤单」就私自改成 False：对标的默认勾选状态也是页面必须 1:1 复刻的一部分）。
    auto_zero_refund_on_conflict = models.BooleanField(
        "撞单自动 0 撤",
        default=True,
        help_text="证据件 SK145（html:216-217）：True=Y=0撤（默认）/ False=N=不自动处理撤销。",
    )
    #: 5、已完成的订单是否不允许再次发到交易平台。证据件 `:226-227`
    #: （`SK155`：`0` = 不限制（默认）/ `1` = 限制）。
    block_push_completed_orders = models.SmallIntegerField(
        "已完成订单不允许再发",
        choices=CompletedPushLimit.choices,
        default=CompletedPushLimit.UNLIMITED,
        help_text="证据件 SK155（html:226-227）：0=不限制（默认）/ 1=限制。",
    )
    #: 6、平台接手后是否自动把订单群二维码发到平台。证据件 `:236-237`
    #: （`SK158`：`0` = 不发送（默认）/ `1` = 自动发）。
    auto_send_group_qr_after_accept = models.SmallIntegerField(
        "平台接手后自动发群二维码",
        choices=GroupQrSend.choices,
        default=GroupQrSend.NONE,
        help_text="证据件 SK158（html:236-237）：0=不发送（默认）/ 1=自动发。",
    )
    # ⭐ 新增三列，**真生效**（不是「只存」那 8 条）。
    #: 「要求最低完单数」的全局默认值（发单页带出；客服可只改价格直接提交）。
    #: 业务方口径：默认 **1**（零完单的新打手接不了，是**有意的**行为收紧，见计划 §7）。发单时作为可选入参 `PowerLevel` 上送（上游语义 =「接单者完单结算数 ≥ 此值」）。
    default_power_level = models.PositiveIntegerField(
        "默认要求最低完单数",
        default=1,
        help_text="发单页「要求最低完单数」的默认值；0 = 不限制。",
    )
    #: 「拒绝新手和介入率」的全局默认值：`null` = **不设置**（不发 `LimitAccept` 参数）。
    #: 语义照上游文档：拒绝客服介入**百分比大于**此值的人接单。
    #: ⚠️ 用 `null` 而不是 `0` 表达「不设置」：`0` 是「拒绝任何有过介入的人」，与「不设置」是两件事（D-0015：数值边界一律 `is not None`，别用真值判）。
    default_limit_accept = models.PositiveSmallIntegerField(
        "默认拒绝新手和介入率",
        null=True,
        blank=True,
        default=None,
        help_text="发单页「拒绝新手和介入率」的默认值；留空 = 不设置（不发该参数）。",
    )
    #: 「代练/陪玩要求」的**唯一一份全局模板**：发单时写进 `Actors` 第五段。
    #: 空串 = 不配置 ⇒ 沿用本线既有占位文案（`dailiantong_push.ACTORS_REQUIREMENT`）。
    require_template = models.TextField(
        "代练/陪玩要求模板",
        blank=True,
        default="",
        help_text="发单时写进代练通 Actors 第五段；留空 = 用系统默认占位文案。",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "平台发单全局设置"
        verbose_name_plural = verbose_name

    @classmethod
    def get_solo(cls) -> "PlatformPushSettings":
        """取本租户唯一一行（没有就建默认行，幂等）。⛔ 单行配置的唯一入口。"""
        return cls.objects.get_or_create(pk=1)[0]

    def __str__(self) -> str:
        return f"PlatformPushSettings(#{self.pk})"


class PlatformAutoReplyRule(models.Model):
    """交易平台自动回复规则：一个游戏 × 一个时机一条（T13）。证据件：src/frontend/public/replica/system-platformautoreply.html（对标 /System/PlatformAutoReply、「新增自动回复」弹层 .modifyParamDiv）；本表只承载 v1 真的要做的那几格、弹层里其余控件（专用人员类型/上传图片/用在哪些平台的多选）的处理逐条写在下面与页面模块的「与对标不同」表里。
    时机码值（⛔ 别按直觉改成 10 / 20）：证据件弹层的 radio 原文（html:98-101）——name="replyOcc" 的 value：0 = 接单后、10 = 申请验收后、15 = 提交异常后、20 = 申请撤销后；value 就是对标表单提交给服务端的值 ⇒ 本枚举照抄 0 / 10 这两个真正做的时机；⚠️ 任务书正文里写的「10 接单后 / 20 申请验收后」与证据件不一致，按 AGENTS.md §5-E（枚举值先找中文原文、以证据为准）取证据件的 0 / 10、并在交付报告里登记这处差异；⚠️ 15 / 20 本版不做（发送引擎还没做、只做了两个时机）⇒ 不进 choices；页面仍照对标渲染那两颗 radio、点选/保存时明确提示「尚未开放」（标准 §5：不置灰、不隐藏）、登记在页面模块的差异表里。
    约束（一键抛单计划 §2.2）：同一 (platform, game, trigger) 只能配一条、且数据库层保证——game is NULL = 「全部游戏」单独算一个组合（与「某款游戏」不冲突）；停用的也算（唯一约束不带「只看启用中」的条件——否则「停用一条再配一条」就能绕过、发送引擎的两套话术照样会同时命中、这正是要防的）；NULL 在 PostgreSQL 的普通唯一索引里互不相等 ⇒ 这里必须 nulls_distinct=False（PG 15+ 的 NULLS NOT DISTINCT；本仓 PG 16）；⛔ 别退回「应用层查一遍就算」：并发窗口只有库层挡得住；⚠️ 行为收紧的代价：既有重复数据会挡迁移 ⇒ 0112 的 RunPython 先查重、有重复就带清理方法响亮失败（生产当前 0 条规则，见计划 §2.2 / §7）。图片：image_key 存对象名（⛔ 不存 URL）、口径同 OrderScreenshot.object_key——URL 是签出来的、会过期、落库等于把过期链接写死；空串 = 没有图片；⚠️ 本版没有上传端点（页面那颗「上传」按钮只提示「尚未开放」）⇒ 这一列今天只会被接口调用方写入、页面读侧只回 has_image + 现签的短时效链接。"""

    class Trigger(models.IntegerChoices):
        """回复时机（码值与文案**逐字照证据件** `html:98-101`，只收 v1 做的两个）。"""

        AFTER_ACCEPT = 0, "接单后"
        AFTER_REVIEW_REQUEST = 10, "申请验收后"

    #: 平台 id（字典与 `PlatformIntegration.Platform` 同一份：代练通 = `10`）。
    #: 默认 `10`（T13：本页对标的就是代练通）。
    #: ⚠️ **接口只收代练通**（`platform_auto_reply_views.ENABLED_PLATFORMS`；M-1 收窄）：本页要的是「**能跑引擎**」的平台，今天只有代练通有真客户端；那 13 个 checkbox 里其它平台进库只会变成页面选不出来、引擎也不会用的幽灵行。
    platform = models.PositiveSmallIntegerField(
        "平台",
        choices=PlatformIntegration.Platform.choices,
        default=PlatformIntegration.Platform.DAILIANTONG,
        db_index=True,
    )
    #: `null` = **全部游戏**（弹层下拉里我们补的那一项；证据件下拉没有「全部游戏」，
    #: 那是对标服务端只渲染本租户游戏 ⇒ 见页面模块的差异表）。
    #: ⚠️ `PROTECT`：游戏被规则引用时删不掉（与区服 / 项目同一口径），⛔ 不 `SET_NULL`——那会把「这条规则限定某款游戏」静默变成「全部游戏」（打手会在别的游戏里收到话术）。
    game = models.ForeignKey(
        Game,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="platform_auto_reply_rules",
        verbose_name="限定游戏",
    )
    #: 回复时机，见 `Trigger`。`db_index=True`：发送引擎将来一定按它筛。
    trigger = models.SmallIntegerField(
        "回复时机", choices=Trigger.choices, db_index=True
    )
    #: 回复正文（必填）。长度上限 **200** 照证据件 `#replycontent` 的 `maxlength="200"`
    #: （批量弹层的 `#batchContent` 也是 200）。
    content = models.TextField(
        "回复内容",
        max_length=200,
        help_text="证据件 #replycontent 的 maxlength=200。",
    )
    #: 可选图片的**对象名**（见类 docstring 的「图片」）。空串 = 没有图片。
    #: ⚠️ 写侧由接口做**两道护栏**（本租户前缀 + 对象存在），见
    #: `platform_auto_reply_views.PlatformAutoReplyRuleSerializer.validate_image_key`。⛔ 本列不接受 URL、也不接受别的租户前缀的对象名（共享 COS 桶下那是跨租户读攻击面）。
    image_key = models.CharField(
        "图片对象名", max_length=255, blank=True, default=""
    )
    #: 正向布尔（证据件 `#switchTpl1` 是 `ReplyStatus == 0` 才 checked ⇒ 对标 0=启用，
    #: 那层反向编码只在页面/接口的映射里做，模型存正向值）。
    is_enabled = models.BooleanField("启用状态", default=True)
    #: 「排序值」。⚠️ 证据件的表格没有这一列（对标的排序值只存在它自己的服务端），
    #: 所以本列**不上屏**、也**不出现在接口的读写字段里**（T13 只要求模型有它）。
    sort_order = models.IntegerField("排序", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "交易平台自动回复规则"
        verbose_name_plural = verbose_name
        # 小到大；同值按主键（插入顺序）——页面的表格是 `page:false`，顺序即服务端顺序。
        ordering = ["sort_order", "id"]
        constraints = [
            #: ⭐ 同一 `(platform, game, trigger)` 只能一条。
            #: `nulls_distinct=False` ⇒ `game IS NULL`（全部游戏）也参与去重；
            #: 不带 `is_enabled` 条件 ⇒ **停用的也算一个组合**（防「停用再配一条」绕过）。
            models.UniqueConstraint(
                fields=["platform", "game", "trigger"],
                name="uniq_platform_auto_reply_rule_combo",
                nulls_distinct=False,
            ),
        ]

    def __str__(self) -> str:
        # ⛔ 正文（`content`，用户自由文本）不进 `__str__`：它会出现在 admin / 日志 / 异常里。
        game = self.game.name if self.game_id else "全部游戏"
        return (
            f"PlatformAutoReplyRule({self.get_platform_display()}:"
            f"{game}:{self.get_trigger_display()})"
        )


class DailiantongZoneMap(models.Model):
    """我方「游戏 + 区服」→ 代练通区服编码（ZoneServerID）的对照表。
    为什么按需落、不预灌：代练通的 gamezoneserver 一次回 4327 条区服 / 297 款游戏（docs/DAILIANTONG-API.md §4 #1）、而其中的 ServerName 是他们的写法（例：「英雄联盟/电信/艾欧尼亚」）、与我们的游戏 / 区服不是一张表（§8 的能力矩阵把「游戏区服自动映射」判为半自动）⇒ 不做全量预灌、由用户在选择区服时选一次就记住：下次同一 (game, server) 自动带出。
    约束：(game, server) 唯一：同一区服只能记一个代练通编码（改选就更新这一行）——这是数据库层的唯一索引、不是接口层的君子协定。⚠️ 本表落租户库（TENANT_APPS 的 business）：每个租户各自的选法互不影响。"""

    game = models.ForeignKey(
        Game,
        on_delete=models.PROTECT,
        related_name="dailiantong_zone_maps",
        verbose_name="游戏",
    )
    server = models.ForeignKey(
        GameServer,
        on_delete=models.PROTECT,
        related_name="dailiantong_zone_maps",
        verbose_name="区服",
    )
    #: 代练通 `gamezoneserver` 里的 `ZoneServerID`（实测形如 `100100010000000`，
    #: 15 位纯数字；列宽 32 给未来的其它形态留余量，⛔ 不在这里做格式校验）。
    zone_server_id = models.CharField("代练通区服编码", max_length=32)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "代练通区服对照"
        verbose_name_plural = verbose_name
        ordering = ["game_id", "server_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["game", "server"],
                name="uniq_dailiantong_zone_map_game_server",
            ),
        ]

    def __str__(self) -> str:
        return f"DailiantongZoneMap({self.game_id}/{self.server_id} -> {self.zone_server_id})"


class DailiantongPush(models.Model):
    """一条「本地订单 → 代练通远端订单」的抛单链路行。状态机：pending = 已经落了本地行、正在发（或发到一半进程死了）——✅ 挡住、超过 5 分钟由同步命令回收成 unknown（不重发）；sent = 上游明确回了订单号——✅ 挡住（不能重复抛）；failed = 上游明确回业务失败（DailiantongError）——❌ 不挡（可以改完再抛）；unknown = 超时/连接失败、不知道成没成——✅ 挡住（⛔ 永不自动重试）；closed = 已在代练通删除（未接手的单、即时退款；T10b 撤回）——❌ 不挡（这条链路已经结束）。
    ⛔ unknown 是人必须来看的一档：实测外部单号 ExOrderNo 在详情/列表里都查不回来（docs/DAILIANTONG-API.md §6.1 第 4 条 / R11）、超时后重发 = 真钱重复建单；「同一 (order, platform) 至多一条有效推送」由 Meta.constraints 的部分唯一索引在数据库层保证（state in pending/sent/unknown）⛔ 不要只在 Python 里查一遍。
    轮询闸门（is_active）：远端到终态（Status=17 已结算 / CancelStatus ∈ {12,15,16} 撤销已完成）时 refresh_push_status() 会把 is_active 置 False；dailiantong_sync 只轮询 is_active=True 的 sent / unknown 行 ⇒ 历史终态单不再每 30 秒白打上游；⚠️ 回调事件仍会强制回读一次（终态判定以回读结果为准）；⛔ 不要用 is_active 替代 state、也不要把它并进 ACTIVE_STATES（那会让终态单可以再抛一次）；⭐ h1：撤销终态集合从「只认 16 强制撤销」放宽到 12/15/16——协商已处理/客服已处理同样是撤销完成、否则那张单会一直轮询、状态永远停在 16。凭据红线：last_error 只存上游原文（DailiantongError.message）或调用方给的中文说明 ⛔ 绝不存 app_secret / pay_password_md5 / 派生的 pay_pass（AGENTS.md §4）；本表的 __str__ 也只拼单号和状态。"""

    class State(models.TextChoices):
        PENDING = "pending", "发送中"
        SENT = "sent", "已发出"
        FAILED = "failed", "发送失败"
        UNKNOWN = "unknown", "状态未知"
        #: 已在代练通**删除**（T10b：未接手单的撤回 = 删除并即时退款）。
        #: ⚠️ 它**不在** `ACTIVE_STATES` / 部分唯一索引的条件里：链路已经结束，
        #: 本地不再轮询、也不再挡住「重新抛单」（如果业务上真要重抛，那是一张新链路行）。
        CLOSED = "closed", "已关闭"

    #: 「有效推送」= 这几种状态。**必须**与 `Meta.constraints` 里的条件逐字一致
    #: （`Meta.constraints` 是库层判据，服务层读的是本常量；改一处必须同一次改另一处）。
    ACTIVE_STATES = (
        State.PENDING,
        State.SENT,
        State.UNKNOWN,
    )

    order = models.ForeignKey(
        Order,
        on_delete=models.PROTECT,
        related_name="dailiantong_pushes",
        verbose_name="订单",
    )
    #: 平台 id（代练通 = 10；取值同 `PlatformIntegration.Platform`）。
    #: ⛔ 不做 FK：本地单可以抛多个平台，而平台配置行可能被删 / 换；链路行必须留下。
    platform = models.PositiveSmallIntegerField(
        "平台", choices=PlatformIntegration.Platform.choices, default=10
    )
    #: 上游订单号（`LevelOrderAdd` 成功回包的 `Err`，实测 20 位纯数字）。
    #: **超时未拿到单号时必须为空**（`null`，不是空串）：多个空串会撞 `unique`，
    #: 而 PostgreSQL 的 `unique` 允许多个 `NULL` —— 那些行正是「不知道该不该人工对账」的行。
    serial_no = models.CharField(
        "上游订单号", max_length=32, null=True, blank=True, default=None, unique=True
    )
    state = models.CharField(
        "抛单状态", max_length=16, choices=State.choices, default=State.PENDING
    )
    # 随推送保留发单时的权限，避免回读平台待接单时自动挂进内部大厅。
    platform_only = models.BooleanField("仅发交易平台", default=False, db_default=False)
    price = models.DecimalField("代练价格", max_digits=12, decimal_places=2)
    ensure1 = models.DecimalField(
        "保证金1", max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    ensure2 = models.DecimalField(
        "保证金2", max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    time_limit_hours = models.PositiveIntegerField("时限（小时）", default=24)
    #: 我方订单号（发给上游的 `ExOrderNo`，实测 ≤50 字符；我们的单号是 20 位）。
    ex_order_no = models.CharField("外部单号", max_length=50, blank=True, default="")
    #: 上游 `Status` / `CancelStatus`（码表见 `docs/DAILIANTONG-API.md` §5）。
    #: 可空 = 「还没回读过」；`is not None` 才是「有值」的判据（0 不在码表里，但别用真值）。
    remote_status = models.IntegerField("上游状态", null=True, blank=True)
    remote_cancel_status = models.IntegerField("上游撤销状态", null=True, blank=True)
    #: ⭐ h13：最近一次撤销协商是谁发起的（读面 cancel_initiator）。取值只有三个（见 dailiantong_push.CANCEL_INITIATOR_*）：
    #: - "taker" = 接单打手发起（上游详情 CancelUID == AcceptUID，真机实测）；
    #: - "us" = 我方发起（CancelUID == CreateUID，真机实测：我方 Flag=0 后 CancelUID 变成我方 CreateUID）；- "" = 无法判定（CancelUID 空、或与两个 UID 都不等——例如客服代操作）。⚠️ 它是最近一次发起方：实测 CancelStatus 回到 10（撤销被取消/协商已处理）后 CancelUID 仍保留旧值 ⇒ 「能不能同意撤销」必须同时看 remote_cancel_status == 11、唯一判据在 dailiantong_push.agree_cancel_availability；空串 = 还没回读到/判不了 ⛔ 不拿它反推「没人发起过撤销」。
    cancel_initiator = models.CharField(
        "撤销发起方", max_length=8, blank=True, default=""
    )
    #: ⭐ dlt-d：**接单打手的用户 ID**（上游详情 / 回调里的 `AcceptUID`）。
    #: 拉黑打手（`AddBlackList`）的 `UID` 参数只能从这里取——文档原话「可从
    #: `LevelOrderDetail` 接口中的 `AcceptUID` 字段得到」。空串 = 还没回读到 / 这单还没被接手（⛔ 不用它猜「没人接」，只表示「我们不知道」）。⚠️ 它是**对方的用户 ID**，不是我们的单号、也不是账号名 ⇒ 凭据红线不涉及它。
    accept_uid = models.CharField(
        "接单打手 UID", max_length=32, blank=True, default=""
    )
    #: 是否还值得定时轮询。终态（远端 Status=17 已结算 / CancelStatus ∈ {12,15,16} 撤销已完成）后置 False：refresh_push_status 只写 remote_*、不会让 sent 退出、若不加这道闸、每条历史抛单都会被每 30 秒白打一次上游。
    #: ⚠️ 回调事件仍会强制回读一次（回调是闹钟；终态判定由回读结果决定）、所以 is_active=False 只表示「轮询不再碰它」⛔ 不表示「以后永远不回读」。
    #: ⚠️ 它与 ACTIVE_STATES 是两件事：ACTIVE_STATES 管部分唯一索引（能否再抛单）、is_active 管轮询闸门；终态行仍算「有效推送」⇒ 同一张单不能再抛一次。
    is_active = models.BooleanField("是否继续轮询", default=True)
    last_synced_at = models.DateTimeField("最后回读时间", null=True, blank=True)
    #: 上游原文（⛔ 不含凭据）。200 字足够装实测出现过的 `Err`；截断在服务层，
    #: ⛔ 不靠数据库 `DataError`（那会变成 500）。
    last_error = models.CharField("最后一次错误", max_length=200, blank=True, default="")
    #: ⭐ D8（业务方口径）：**协商撤销申请已提交**的本地时刻。
    #: 非空 = 客服在上游提交过撤销申请（`LevelOrderCancel` 的 `Flag=0`）⇒ 允许重发
    #: （新建一条推送行），并且被接管的旧行不再参与「本地状态以谁为准」的仲裁。⚠️ 这是**本地依据**：⛔ 不要只拿远端 `CancelStatus=11` 猜（远端码会被覆盖 / 丢失）；历史行若只有远端 11，重发放行那一步会把当时时刻补记进来（见 `_raise_if_already_pushed`）。
    cancel_requested_at = models.DateTimeField(
        "协商撤销申请提交时间", null=True, blank=True
    )
    #: ⭐ D8：这条推送**接管**的是哪一条旧推送（重发时新行指向被撤销的那条）。
    #: 非空只用于排障与「本地状态只认最新未结束推送」的判定；旧行**照旧**轮询
    #: （打手可能不同意撤销 ⇒ 还要走客服介入），⛔ 不把它置 `closed`。
    replaces_push = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="replaced_by",
        verbose_name="接管的旧推送",
    )
    # ── 远端多字段（Rev.9 §2）──────────────────────────────
    #: 上游接单人 UID / 昵称（`LevelOrderDetail` 的 `AcceptUID` / `AcceptUserName`）。
    #: 空的含义是「远端还没人接 / 详情没回传」——⛔ 不编一个。
    remote_taker_uid = models.CharField(
        "上游接单人 UID", max_length=64, blank=True, default=""
    )
    remote_taker_name = models.CharField(
        "上游接单人昵称", max_length=128, blank=True, default=""
    )
    #: 上游接单时间（`LevelOrderDetail` 的 `StartDate`，形如 `2015-11-26 23:09:40`）。
    #: 解析不了 ⇒ `None`（⛔ 不拿「回读时刻」冒充接单时刻）。
    remote_accepted_at = models.DateTimeField("上游接单时间", null=True, blank=True)
    #: 上游撤销原因（`LevelOrderDetail` 的 `CancelReason`；⛔ 不再混装 `CancelCmt`）。
    #: ⚠️ 只存可读原文，⛔ 不含凭据。
    remote_cancel_reason = models.CharField(
        "上游撤销原因", max_length=255, blank=True, default=""
    )
    #: ⭐ 真机（业务方）：上游**拼好的撤销要求原文**（`LevelOrderDetail` 的
    #: `CancelCmt`，例：「接单者"新手USR…"申请撤销,需发单者支出:0.00, 接单者支出:0.00.
    #: 请尽快协商解决.…撤销原因：好的」）。客服点「同意撤销」时要原样看到它。回读时若详情**没有** `CancelCmt` 键 ⇒ 退回 `CancelReason`；两者都没有 ⇒ 空串。⚠️ 原样展示、⛔ 不改写（金额 / 要求都是上游原话）；列宽上限只防脏数据。
    remote_cancel_cmt = models.CharField(
        "上游撤销要求原文", max_length=512, blank=True, default=""
    )
    #: ⭐ 
    #: 上游**没有独立的「已结」字段**，文档只有 `PayLevelBal`（支付代练费）/
    #: `RepEnsureBal`（赔偿保证金），且只在撤销上下文里有意义 ⇒ 原样落库，**读面只在 `Status ∈ {16 撤销中, 17 已结算}` 时下发**（其它状态给 `null`，避免把「申请撤销时想给的金额」当成「已结金额」显示）。解析不出 / 详情为空 ⇒ `None`（前端显示「—」，⛔ 不写 0 冒充）。
    remote_pay_level_bal = models.DecimalField(
        "上游支付代练费", max_digits=12, decimal_places=2, null=True, blank=True
    )
    remote_rep_ensure_bal = models.DecimalField(
        "上游赔偿保证金", max_digits=12, decimal_places=2, null=True, blank=True
    )
    #: ⭐ **（业务方）**：代练通详情的 `AcceptOverCount`（打手完成单数）/
    #: `CustCancelCount`（投诉 / 取消次数）。页面直接展示；缺失 / 解析不出 ⇒ `None`
    #: （⛔ 不写 0 冒充「完成 0 单」）。
    accept_over_count = models.IntegerField("打手完成单数", null=True, blank=True)
    cust_cancel_count = models.IntegerField("打手投诉/取消次数", null=True, blank=True)
    #: ⭐ **（业务方）**：**本地计算**的验收倒计时起点——远端状态**首次进入 13**
    #: （等待验收）时记 `timezone.now()`；离开 13 时清空（下次再进 13 重新计时）。
    #: ⛔ 不同步上游（上游没有这个字段）；终点由读面按 **48 小时**（业务方）算。
    remote_verify_started_at = models.DateTimeField(
        "远端首次进入待验收的时间", null=True, blank=True
    )
    #: ⭐ **最近一次同步的业务结论**（中文一句话：本地状态回写成什么 / 并发抢单收口结果）。
    #: 为什么另开一列而不写 `last_error`：回读成功会把 `last_error` 清空（30 秒一轮），
    #: 而「本地打手已被代练通接管」这类结论需要留在页面上让人看见。⚠️ 它不是错误列：失败原因仍走 `last_error` / `qr_last_error` / `auto_reply_last_error`。
    remote_note = models.CharField("最近一次同步结论", max_length=200, blank=True, default="")
    #: ⭐ A4（Rev.9 §4）：**本地打手先接**时，为「把代练通订单撤掉」这件事留下的成功时刻。
    #: 非空 = 已经向代练通发起过撤回（未接手已删除 / 已接手已提交协商撤销），⛔ 不再重复发。
    #: ⚠️ 与 `state=closed` 分工：删除成功时 `state` 变成 `closed`；**协商撤销**成功后远端还活着（要等打手 / 客服处理），只能靠本列挡住重复申请。
    claim_withdraw_at = models.DateTimeField(
        "因本地先接而撤回代练通的时间", null=True, blank=True
    )
    #: 上述撤回的连续失败次数（有界）：达到上限后停止自动重试，原因写进 `remote_note`。
    claim_withdraw_attempts = models.PositiveSmallIntegerField(
        "撤回代练通连续失败次数", default=0
    )
    # ── ⭐ 协商撤销满 24 小时 ⇒ 自动申请客服介入 ─────────────────
    #: 业务开关（**默认关**，业务方口径）：客服在发起协商撤销时**主动勾选**
    #: 才会代发（`auto_arbitrate=True`）。⚠️ 为什么默认关：业务方原话「**若不勾选，代练通 24 小时自动客服介入**」——不勾选时上游自己会转客服介入，我们再去申请就是**重复申请**。`False` ⇒ 定时任务**永远不碰**这条推送（⛔ 不是「这一轮不碰」）。
    auto_arbitrate = models.BooleanField("协商撤销后自动申请仲裁", default=False)
    #: ⚠️ 自动仲裁的两个门槛都从本类上面的 `cancel_requested_at`（⭐ D8 那一列）算起：
    #: +1 小时才允许调上游、+24 小时才自动进入仲裁；`None` = 不是本系统发起的撤销
    #: ⇒ **不自动仲裁**（fail-safe）。⛔ 合并时**不要再加一列同名字段**（D8 已加）。自动申请成功（或被判定无需申请）的时刻 = **只做一次**的幂等键。非空后定时任务不再碰这一行（即使远端撤销状态还是「协商中」）。
    auto_arbitrated_at = models.DateTimeField(
        "自动申请仲裁的时间", null=True, blank=True
    )
    #: 自动申请**失败**次数（有界重试）。到 `AUTO_ARBITRATE_MAX_ATTEMPTS` 后停止，
    #: 可读原因留在 `auto_arbitrate_note`。
    auto_arbitrate_attempts = models.PositiveSmallIntegerField(
        "自动申请仲裁失败次数", default=0
    )
    #: 失败退避：这个时刻之前不再打上游（每轮扫「`next_at` 为空或已到期」的行）。
    auto_arbitrate_next_at = models.DateTimeField(
        "下次允许自动申请仲裁时间", null=True, blank=True
    )
    #: **可读结论**（成功 / 最近一次失败原因 / 停止重试的原因）。
    #: ⚠️ 为什么不用 `remote_note` / `last_error`：前者会被每轮同步的业务结论覆盖、
    #: 后者每次回读成功都会被清空（30 秒一轮）⇒ 「为什么没再自动仲裁」活不过一轮。⛔ 不含凭据；只放中文说明或上游 `Err` 原文。
    auto_arbitrate_note = models.CharField(
        "自动申请仲裁结论", max_length=200, blank=True, default=""
    )
    # ── ⭐ H3-1：撞单 0 撤（设置开关 auto_zero_refund_on_conflict）──
    #: 「撞单 0 撤」已发起并回读核验通过的时刻 = 这条推送的终态幂等键：非空 ⇒ 定时任务不再对它发起 0 撤（即使远端状态还停在 12 / 13——协商撤销本就是「等打手同意」的过程态、重复申请只会打扰对方）。
    #: ⚠️ 它与 cancel_requested_at（D8 的人工协商撤销）刻意分开：后者非空会让这条推送退出「同单同平台至多一条有效推送」的部分唯一索引 ⇒ 变成可以重发；撞单 0 撤是系统替业务方做的决定 ⛔ 不该顺手把这张单放行到「可重发」。
    conflict_zero_refund_at = models.DateTimeField(
        "撞单0撤发起时间", null=True, blank=True
    )
    #: 「撞单 0 撤」连续失败次数（**有界重试**）：到上限就停手，
    #: 可读原因留在 `conflict_zero_refund_note`（⛔ 不每 30 秒刷上游）。
    conflict_zero_refund_attempts = models.PositiveSmallIntegerField(
        "撞单0撤连续失败次数", default=0
    )
    #: 失败退避：这个时刻之前不再打上游（与自动仲裁段同形）。
    conflict_zero_refund_next_at = models.DateTimeField(
        "下次允许撞单0撤时间", null=True, blank=True
    )
    #: **可读结论 / 最近一次失败原因**（成功也写一句，便于页面与排障）。
    #: ⚠️ 不用 `remote_note` / `last_error`：前者每轮同步都会被业务结论覆盖、后者每次
    #: 回读成功都会被清空（30 秒一轮）⇒ 「为什么没再 0 撤」活不过一轮。⛔ 不含凭据。
    conflict_zero_refund_note = models.CharField(
        "撞单0撤结论", max_length=200, blank=True, default=""
    )
    #: ⭐ T12：接手后发送的成功时刻（终态幂等键）。⭐（B1）：「留言 + 图」是一条逻辑、一个标记——本列非空 = 这条推送的接手后发送已经完成（图 + 规则话术都处理过）⛔ 不再发；null = 还没发成功过（开关关 / 没有可发内容 / 任何一步失败 / 还没试过）。
    #: ⚠️ 「发送中」的库层占位是下面的 qr_send_started_at / qr_claim_token：发送前回读 + 条件 UPDATE 占位、进程被杀也不会把已发步骤再放一遍。
    #: ⚠️ 用可空时间而不是布尔：排障时要能看出「什么时候发的」。
    qr_sent_at = models.DateTimeField("接手后发送时间", null=True, blank=True)
    #: ⭐ T12：接手后发送**最近一次失败原因**（可读中文 / 上游 `Err` 原文；⛔ 不含凭据 / 对象名）。
    #: 为什么另存一份而不只写 `last_error`：`refresh_push_status()` 每次回读成功都会清空
    #: `last_error`（30 秒一轮），QR 的失败原因活不过一轮 ⇒ 页面看不到、也没法用来判「同一个失败原因只写一条审计」。`send_accept_qr` 两个列一起写，成功时一起清空。⚠️ 这两个 QR 列已由读面 `GET /api/business/platform-pushes/` 下发（`PlatformPushRowSerializer`），页面 tooltip / 人工排查才真的看得到失败原因。
    qr_last_error = models.CharField(
        "接手后发送最后一次错误", max_length=200, blank=True, default=""
    )
    #: ⭐ T14：**自动回复**最近一次失败原因（可读中文 / 上游 `Err` 原文；
    #: ⛔ 不含凭据 / 对象名）。与 `qr_last_error` 同理：`last_error` 每轮回读成功都会被
    #: `refresh_push_status()` 清空，自动回复的失败原因必须另存一份，才能既在页面上看到，又用于「同一个失败原因只写一条审计」。成功时清空。
    auto_reply_last_error = models.CharField(
        "自动回复最后一次错误", max_length=200, blank=True, default=""
    )
    #: ⭐ 审计修复：接手后发送的**发送占位**（库层幂等）。
    #: `send_accept_qr` 在**动上游之前**用条件 UPDATE 把它写成当前时刻 + `qr_claim_token`：
    #: - 两个进程同时看到「还没发」时，只有一个能把占位写成功，另一个直接退出；- 进程在发送中途被杀 ⇒ 占位留在这一列，下一轮**先回读**（留言指纹 + 进度图对象名），已发出去的步骤不再重放；占位超过 `QR_CLAIM_STALE_SECONDS` 才允许被重新占用。
    qr_send_started_at = models.DateTimeField("接手后发送占位时间", null=True, blank=True)
    #: 占位令牌（随机串）：失败 / 成功清理占位时只清**自己那次**的，⛔ 不误删别的进程的占位。
    qr_claim_token = models.CharField(
        "接手后发送占位令牌", max_length=32, blank=True, default=""
    )
    #: 连续失败次数（有界重试）：成功 / 换了一张群码时归零；达到上限后停止自动重发并把
    #: 可读原因写进 `qr_last_error`（等人工处理，⛔ 不再每 30 秒白打上游 + 重发）。
    qr_attempts = models.PositiveIntegerField("接手后发送连续失败次数", default=0)
    #: 上一次尝试发送的**来源键**（图来源 + 存储键 + 规则 id）。换图 / 换规则 = 新的失败预算：
    #: `send_accept_qr` 发现它与本轮算出的不同时把 `qr_attempts` 归零。
    qr_retry_source_key = models.CharField(
        "接手后发送重试来源键", max_length=255, blank=True, default=""
    )
    #: ⭐ 审计修复：上游回读的**退避 / 上限**（P3-12）。
    #: 连续失败次数；成功回读归零。`next_sync_at` 之前同步命令不再白打上游
    #: （退避 1 / 5 / 15 / 60 分钟，达到上限后停止轮询并把原因写进 `last_error`）。
    sync_attempts = models.PositiveIntegerField("状态回读连续失败次数", default=0)
    next_sync_at = models.DateTimeField("下次允许回读时间", null=True, blank=True)
    attempts = models.PositiveIntegerField("累计尝试次数", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("最后修改时间", auto_now=True)

    class Meta:
        verbose_name = "代练通抛单"
        verbose_name_plural = verbose_name
        ordering = ["-created_at", "-id"]
        constraints = [
            #: 同一订单在同一平台至多一条有效推送（pending / sent / unknown）；failed 不在条件里 ⇒ 失败后可以再抛（会新增一行、历史行保留）。
            #: ⭐ D8：已申请协商撤销的行（cancel_requested_at 非空）退出这条唯一约束——重发时旧行还在轮询（打手可能不同意撤销）、新行必须能与它同时有效；旧行一旦被撤销完成 / 关闭、重发仍受下面这条约束。
            #: ⛔ 这是数据库层的部分唯一索引；服务层的前置闸只是把常见路径变成中文业务错误、两者都必须存在（并发窗口只有库层能挡）。
            models.UniqueConstraint(
                fields=["order", "platform"],
                condition=Q(
                    state__in=[
                        "pending",
                        "sent",
                        "unknown",
                    ]
                )
                & Q(cancel_requested_at__isnull=True),
                name="uniq_dailiantong_push_active",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"DailiantongPush(order={self.order_id}, "
            f"state={self.state}, serial={self.serial_no})"
        )


class DailiantongAutoReplySent(models.Model):
    """「这条推送已经按这条规则自动回复过」的幂等台账（T14）。为什么是独立表而不是推送行上的一个时间列：自动回复的幂等键是 (推送, 规则)、不是「推送」——一条推送会经历多个远端状态，⭐（B1）起本表只承载「申请验收后」(trigger=10) 那一档（Status=13）——「接单后」已并入 dailiantong_qr.send_accept_qr、用推送行上的 qr_sent_at 单标记；同一档里「具体游戏」的规则优先于「全部游戏」的规则（见 apps.business.dailiantong_auto_reply）⇒ 也不能只按 push 记一次。
    唯一约束：(push, rule) 数据库层唯一。⭐ 审计修复：这一行现在在发送之前就插入（占位）、不再是「发送成功后才写」——①占位（state=pending）→ 动上游 → 成功把 state 改成 sent；②上游失败 → 删掉自己这次占位（下一轮可重试）；③进程在发送中途被杀 → 占位留在库里（state=pending）、下一轮先回读进度图/留言、已发出去的步骤不重放、占位超过 AUTO_REPLY_CLAIM_STALE_SECONDS 才可被重新占用——这样「发出去之后、写台账之前进程被杀」不再导致下一轮重复发送。
    删除语义：push / rule 都是 CASCADE——台账是「这次发送」的派生记录、推送链路行或规则被删掉后它就没有意义了；⚠️ 审计（OperationLog）不受影响、仍保留成功/失败留痕。"""

    class State(models.TextChoices):
        #: 已占位、正在发送（进程被杀会停在这一档）。
        PENDING = "pending", "发送中"
        #: 整段发送成功（终态）。
        SENT = "sent", "已发送"

    push = models.ForeignKey(
        DailiantongPush,
        on_delete=models.CASCADE,
        related_name="auto_reply_sents",
        verbose_name="推送行",
    )
    rule = models.ForeignKey(
        PlatformAutoReplyRule,
        on_delete=models.CASCADE,
        related_name="dailiantong_send_records",
        verbose_name="回复规则",
    )
    #: ⭐ 占位 / 成功两档（旧行迁移成 `sent`）。
    state = models.CharField(
        "台账状态", max_length=16, choices=State.choices, default=State.SENT
    )
    #: 占位令牌：失败清理 / 成功收尾只动**自己那次**占位，⛔ 不误删别的进程的。
    claim_token = models.CharField("占位令牌", max_length=32, blank=True, default="")
    #: 占位时间（`pending` 时 = 占用时刻；`sent` 时 = 最近一次占用 / 发送时刻）。
    #: 用显式默认值（而不是 `auto_now_add`）：测试与排障要能钉住这个瞬时。
    sent_at = models.DateTimeField("发送时间", default=timezone.now)

    class Meta:
        verbose_name = "代练通自动回复发送记录"
        verbose_name_plural = verbose_name
        ordering = ["-sent_at", "-id"]
        constraints = [
            #: ⛔ 这不是君子协定：并发 / 进程重启时只有库层能挡住第二条。
            models.UniqueConstraint(
                fields=["push", "rule"],
                name="uniq_dailiantong_auto_reply_sent",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"DailiantongAutoReplySent(push={self.push_id}, "
            f"rule={self.rule_id}, sent_at={self.sent_at})"
        )


class OrderQrFetchConfig(models.Model):
    """「马上来 self-app」查码通道的本租户配置（每租户一行；D-0348）。订单缺群二维码（OrderScreenshot 无 img_type=1 的 ready 行）时、在自然触发点（打手/客服打开群码区块、接单成功）异步拉一次对端的查码接口；本行持有它的三样配置（总开关/基址/密钥）；未配置（无行 / enabled=False / api_key 或 base_url 空）⇒ 触发点第一道闸就返回、全链路零动作（完全向后兼容）。
    形状母本（照抄先例 ⛔ 不另造）：WechatMpConfig（apps/wechatmp/models.py）——tenant_schema 列级 unique=True 挡并发双行、密钥只写不读（序列化器层保证、模型照存）、enabled 默认 False（fail-closed）。
    与 SystemSwitch 的分工（D-0348 §1.3 的选型）：总开关不放 SystemSwitch——那个机制值域只有 Y/N、api_key / base_url 无处安放；拆成「开关行 + 配置行」会出现两处真值；本行 = 单一事实源（WechatMpConfig 同款取舍）。"""
    #: 租户标识 = 租户 schema 名。⚠️ 只来自 `connection.schema_name`，
    #: ⛔ 绝不接受请求参数传入的租户标识（`AGENTS.md` §4）。
    tenant_schema = models.CharField("租户标识", max_length=64, unique=True)
    #: 对端基址（生产 = `https://malailai.suqing.chat`；**空 = 未配置**）。校验在接口层
    #: （`order_push.validate_webhook_url` 同款：http(s)、挡回环 / 链路本地 / 云元数据）。
    base_url = models.CharField("对端基址", max_length=200, blank=True, default="")
    #: 对端查码密钥（`Authorization: Bearer <api_key>`）。⚠️ **只写不读**：接口 GET 只回
    #: 掩码；PATCH **留空 = 不改**（WechatMpConfig.appsecret 的同一套口径）。
    api_key = models.CharField("查码密钥", max_length=128, blank=True, default="")
    #: 总开关，**fail-closed 默认关**：配置没配好之前触发点零动作。
    enabled = models.BooleanField("启用马上来查码", default=False)
    #: **401 闩**：对端回「密钥无效」的时刻。置位后停触发；本行任何一次成功的配置保存
    #: （PATCH）都会清它（=「停触发直到配置变更」的落地）。`None` = 从未被拒过。
    #: ⚠️ 判据用 `is not None`（E-0021）。
    auth_failed_at = models.DateTimeField("密钥被对端拒绝时刻", null=True, blank=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "马上来查码配置"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"OrderQrFetchConfig({self.tenant_schema}, enabled={self.enabled})"


class ReceiptSyncConfig(models.Model):
    """「上家收款同步」的本租户配置（每租户一行；D-0359）。夜间定时查收款（tuantuantong-receipt-sync.timer → manage.py sync_upstream_receipts）的租户闸：enabled=False ⇒ 该租户当晚零上游调用（timer 照转但空跑）。
    与查码先例（OrderQrFetchConfig）有意相反的默认值（计划 §3 的 S5 登记）：OrderQrFetchConfig.enabled 默认 False（fail-closed：配置没配好之前零动作）；本行 enabled 默认 True——业务方要全租户默认生效（上线即开始向上家查收款）、不要的租户显式关；偏离已在计划登记 ⛔ 不要「统一」成 False。
    读侧语义（钉死 ⛔ 不留双解）：无行 = 视为启用——读侧一律 receipt_sync_config()（get_or_create 补行）⛔ 不要写「first() 为 None ⇒ 关」的第二套判据。"""
    #: 租户标识 = 租户 schema 名。⚠️ 只来自 `connection.schema_name`，
    #: ⛔ 绝不接受请求参数传入的租户标识（`AGENTS.md` §4）。
    tenant_schema = models.CharField("租户标识", max_length=64, unique=True)
    #: 租户闸，**默认 True**（业务方要全租户默认生效；与查码先例有意相反，见类 docstring）。
    enabled = models.BooleanField("启用上家收款同步", default=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "上家收款同步配置"
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"ReceiptSyncConfig({self.tenant_schema}, enabled={self.enabled})"
class AlipaySettlementAuthorization(models.Model):
    """租户的付款支付宝授权记录，不保存控制服务凭据或授权链接。"""

    request_id = models.UUIDField(unique=True)
    account_ciphertext = models.TextField()
    real_name_ciphertext = models.TextField()
    status = models.CharField(max_length=16, default="pending")
    next_action = models.CharField(max_length=16, blank=True, default="")
    requires_platform_review = models.BooleanField(default=False)
    last_error = models.CharField(max_length=120, blank=True, default="")
    revision = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    authorized_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]


class OnlinePayrollBatch(models.Model):
    class Category(models.TextChoices):
        TAKER = "taker", "打手报酬"
        CUSTOMER_SERVICE = "customer_service", "客服报酬"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request_id = models.UUIDField(unique=True)
    selection_hash = models.CharField(max_length=64, db_index=True)
    fingerprint = models.CharField(max_length=64)
    category = models.CharField(max_length=24, choices=Category)
    authorization = models.ForeignKey(AlipaySettlementAuthorization, on_delete=models.PROTECT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    payer_masked = models.CharField(max_length=140)
    created_at = models.DateTimeField(auto_now_add=True)


class OnlinePayrollRequest(models.Model):
    request_id = models.UUIDField(primary_key=True)
    batch = models.ForeignKey(OnlinePayrollBatch, on_delete=models.PROTECT)
    fingerprint = models.CharField(max_length=64)
    payer_id = models.PositiveBigIntegerField()


class OnlinePayrollGatewayBatch(models.Model):
    class Status(models.TextChoices):
        STAGED = "staged", "待制单"
        SEALING = "sealing", "正在获取付款码"
        PAYABLE = "payable", "可继续付款"
        UNKNOWN = "unknown", "待核对"
        CLOSED = "closed", "已关闭"

    batch = models.ForeignKey(OnlinePayrollBatch, on_delete=models.PROTECT, related_name="gateway_batches")
    mch_batch_no = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.STAGED)
    payment_ciphertext = models.TextField(blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class OnlinePayrollTransfer(models.Model):
    class Status(models.TextChoices):
        STAGED = "staged", "待制单"
        SUBMITTING = "submitting", "制单中"
        CREATED = "created", "已制单"
        PROCESSING = "processing", "转账中"
        SUCCEEDED = "succeeded", "转账成功"
        FAILED = "failed", "转账失败"
        CLOSED = "closed", "已关闭"
        UNKNOWN = "unknown", "待核对"

    gateway_batch = models.ForeignKey(OnlinePayrollGatewayBatch, on_delete=models.PROTECT, related_name="transfers")
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recipient_name = models.CharField(max_length=150)
    account_masked = models.CharField(max_length=140)
    recipient_ciphertext = models.TextField()
    mch_order_no = models.CharField(max_length=64, unique=True)
    amount = models.DecimalField(max_digits=16, decimal_places=2)
    status = models.CharField(max_length=16, choices=Status, default=Status.STAGED, db_index=True)
    transfer_id = models.CharField(max_length=128, blank=True)
    channel_order_no = models.CharField(max_length=128, blank=True)
    success_time = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class OnlinePayrollItem(models.Model):
    transfer = models.ForeignKey(OnlinePayrollTransfer, on_delete=models.PROTECT, related_name="items")
    performance = models.ForeignKey(Performance, on_delete=models.PROTECT, related_name="online_payroll_items")
    order_no = models.CharField(max_length=64)
    bonus = models.DecimalField(max_digits=12, decimal_places=2)
    deduction = models.DecimalField(max_digits=12, decimal_places=2)
    adjustment = models.DecimalField(max_digits=12, decimal_places=2)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["performance"], condition=Q(released_at__isnull=True),
                name="online_payroll_one_reservation",
            ),
        ]


class OnlinePayrollAdjustment(models.Model):
    item = models.ForeignKey(OnlinePayrollItem, on_delete=models.PROTECT, related_name="adjustments")
    adjustment = models.ForeignKey(PerformanceAdjustment, on_delete=models.PROTECT)


class ApprovalKind(models.TextChoices):
    PENALTY = "penalty", "罚款"
    REFUND = "refund", "退款处理"


class ApprovalStatus(models.TextChoices):
    PENDING = "pending", "待处理"
    APPROVED = "approved", "已通过"
    REJECTED = "rejected", "已驳回"


class PenaltyTargetType(models.TextChoices):
    STUDIO = "studio", "下家工作室"
    WORKER = "worker", "打手"


class ApprovalSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    approvers = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="+",
    )
    available_cc = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="+",
    )
    default_cc = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="+",
    )
    refund_handler = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(pk=1), name="approval_settings_singleton")]


class Approval(models.Model):
    kind = models.CharField(max_length=8, choices=ApprovalKind.choices)
    status = models.CharField(max_length=8, choices=ApprovalStatus.choices, default=ApprovalStatus.PENDING)
    original_order = models.ForeignKey(
        Order, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    refund_source_no = models.CharField(max_length=128, blank=True, default="")
    initiator = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    cc = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="+")
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    target_type = models.CharField(max_length=8, choices=PenaltyTargetType.choices, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    reason = models.TextField()
    objection = models.TextField(blank=True)
    remark = models.TextField(blank=True)
    comment = models.TextField(blank=True)
    order_data = models.JSONField(default=dict, blank=True)
    image_keys = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
            models.UniqueConstraint(
                fields=["original_order", "refund_source_no"],
                condition=Q(kind=ApprovalKind.REFUND),
                name="uniq_refund_approval_source",
            ),
        ]
        indexes = [
            models.Index(fields=["approver", "status", "-created_at"], name="approval_inbox_idx"),
        ]


from apps.business.export_job_models import FinanceExportJob  # noqa: E402, F401
