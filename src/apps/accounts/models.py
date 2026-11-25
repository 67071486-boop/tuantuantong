"""租户内的账号档案。每个租户 schema 各一份表（与 `apps.business` 同构）。"""
from django.conf import settings
from django.db import models
from django.db.models.functions import Coalesce

from apps.accounts.choices import (
    AccountSource,
    BusinessType,
    CommissionMode,
    MemberType,
)


class StaffProfile(models.Model):
    """`auth.User` 的**一对一扩展**（字段照抄对标物「新增系统子账户」表单，spec §3.2）。
    不自定义 `AUTH_USER_MODEL`（D-0009）：中途换 user model 是伤筋动骨的数据迁移，档案走独立表。"""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_profile",
    )

    # ⭐ 开户人（自助注册线，spec §2.1 / D-0237）：谁把这个账号开进系统的。两条写路径：
    # 手工新增 = 当时操作者（`views.AccountViewSet.create`）；自助注册 = 生成链接的人事
    # （写点在 `selfreg_views`）。`SET_NULL`：开户人硬删时字段清空、档案行不连坐。
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="opened_accounts",
        verbose_name="开户人",
    )

    # ⭐ **账号来源**（D-0277）：账号从哪条路建进来的。落档案表而非 `auth.User`（本仓不换
    # `AUTH_USER_MODEL`，见类 docstring），三条建号路径都会建档案行。⚠️ 没有档案行的账号按
    # 默认 `console` 算——登录判据是「档案行 source 为 app」⛔ 不是「有没有档案」（消费方 `core/auth_views.LoginView`）。
    account_source = models.CharField(
        "账号来源",
        max_length=16,
        choices=AccountSource.choices,
        default=AccountSource.CONSOLE,
    )

    # ── 身份 / 属性 ────────────────────────────────────────────────
    id_card_no = models.CharField("身份证号", max_length=18, blank=True, default="")
    business_type = models.SmallIntegerField(
        "业务类型", choices=BusinessType.choices, default=BusinessType.BOOSTING
    )
    member_type = models.SmallIntegerField(
        "成员类型", choices=MemberType.choices, default=MemberType.INTERNAL
    )
    # ⭐ 工作室标记（业务方拍板，D-0228）：身份三分类 = 内部员工（member_type=内部）/ 打手
    # （合作且本列 False——「只要不是工作室的打手都算打手」）/ 工作室（合作且 True）。
    # ⚠️ 分类判据**先看 member_type**：筛选按合作收窄再按本列分档，内部成员勾上也不会变成工作室。
    is_studio = models.BooleanField("工作室", default=False)

    # ── 薪酬 / 提成（仅存取，本线**不做任何计算**——那是 P4 结算的属地）──
    base_salary = models.DecimalField(
        "基本底薪", max_digits=12, decimal_places=2, null=True, blank=True
    )
    commission_mode = models.SmallIntegerField(
        "提成模式", choices=CommissionMode.choices, default=CommissionMode.ORDER_PERCENT
    )
    commission_value = models.DecimalField(
        "提成金额/百分比", max_digits=12, decimal_places=2, default=0
    )

    # ── 结算账户（⚠️ 财务 + PII，本线只存不用）──────────────────────
    alipay_account = models.CharField("结算支付宝账号", max_length=64, blank=True, default="")
    alipay_name = models.CharField("结算支付宝姓名", max_length=64, blank=True, default="")
    bank_name = models.CharField("结算银行名称", max_length=64, blank=True, default="")
    bank_card_no = models.CharField("结算银行卡号", max_length=32, blank=True, default="")

    # ── 接单限制 ──────────────────────────────────────────────────
    grab_limit_dl = models.PositiveIntegerField("内部抢代练单上限", default=0)
    grab_limit_pw = models.PositiveIntegerField("内部抢陪玩单上限", default=0)
    grab_price_min = models.DecimalField(
        "内部抢单价下限", max_digits=12, decimal_places=2, null=True, blank=True
    )
    grab_price_max = models.DecimalField(
        "内部抢单价上限", max_digits=12, decimal_places=2, null=True, blank=True
    )
    grab_forbid_self_service = models.BooleanField("禁自助接单", default=False)

    # ── 排序 / 结算开关 ────────────────────────────────────────────
    # 「排序值」：指派接手人时的排列顺序，数值越大越前；对标输入框 `maxlength=4` ⇒ 上界 9999
    # 钉在序列化器上、模型 int4 不加上界（与 `grab_limit_dl` / `grab_limit_pw` 同口径，越界只能入口挡）。
    order_by = models.PositiveIntegerField("排序值", default=0)
    # 「禁止结算」= **冻结**：被冻结的人不许结账、不许给他设扣点（对标 `isProhibitSettStatus`；
    # 拦截在结算侧——绩效行不进 `settlement.settle_performances` 第 2 步的 `rows`）。
    # ⚠️ 它**不是**「接单限制」：那条限制抢单动作、这条限制结账动作，对标分成两列，本列照此独立。
    settle_forbidden = models.BooleanField("禁止结算", default=False)

    # ── 联系方式 / 其他 ────────────────────────────────────────────
    live_url = models.CharField("打手直播地址", max_length=255, blank=True, default="")
    qq = models.CharField("QQ/微信", max_length=64, blank=True, default="")
    mobile = models.CharField("联系电话", max_length=32, blank=True, default="")
    # ── 微信小程序支付────────────────────────────────
    # 小程序 openid **只用于付款**、不认身份（⛔ 不加唯一约束：共用微信 / 一人多号会撞）。
    # `wx_openid_appid` 记所属小程序：换 AppID 自动失配、重走 `POST /api/app/wallet/wx-openid/` 取（按 AppID 隔离）。
    wx_openid = models.CharField("小程序 openid", max_length=64, blank=True, default="")
    wx_openid_appid = models.CharField(
        "openid 所属小程序 AppID", max_length=64, blank=True, default=""
    )
    hired_on = models.DateField("入职日期", null=True, blank=True)
    # ⚠️ `max_length=100`（不是 64）：对标的「设置标记」输入框是
    # `maxlength: 100`，列窄于它会让 65~100 字的标记「前端放行、后端回 1000」。
    # 序列化器（`AccountWriteSerializer.tag`）是同一个数字，两处必须一起改。
    tag = models.CharField("标记", max_length=100, blank=True, default="")
    remark = models.TextField("备注说明", blank=True, default="")
    deposit_amount = models.DecimalField("已交保证金", max_digits=12, decimal_places=2, default=0)

    # ⭐ 个人押金额度**开关**（业务方拍板；计划 `docs/plans/2026-10-23-taker-deposit-quota-plan.md`）。
    # `False`（默认）= 全局按游戏押金（`GameDepositRule`），存量行为逐字不变；`True` = 这个人的按游戏
    # 额度（`TakerDepositQuota`，没行 = 0 元）。⚠️ 只决定「按谁算金额」，押金闸仍归 `deposit_gate.py`，⛔ 别混。
    deposit_quota_enabled = models.BooleanField("启用个人押金额度", default=False)

    # ── 媒体对象名（spec §5.2：⛔ 只存 key，不存 URL）──────────────
    avatar_key = models.CharField("头像对象名", max_length=255, blank=True, default="")
    wechat_qr_key = models.CharField("微信收款码对象名", max_length=255, blank=True, default="")

    # ── 录单偏好（快速指派）───────────────────────────────────────────
    # 「默认指派此人」（对标 `defAss`，D-0179）：挂在**录单的客服自己**身上——谁录单，下次他录单
    # 自动带出那个人；跟人走而非跟设备 ⇒ 落 `StaffProfile`。`SET_NULL` 不连坐档案行（CASCADE 会整行带走）。
    default_assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="默认接手人",
    )
    # ⭐ **「默认抛内部抢单」**（业务方裁定）：与 `default_assignee` 互斥的另一档——记**档位**不记人
    # （`True` ⇒ 下次录单默认选中「内部抢单」档，价格每次自己填）。两列不许同真但**不加 DB 约束**：
    # `CheckConstraint` 表达不了「写 hall 就清人」，互斥由唯一写入口 `set_default_assign_preference` 保证（读面 `defAssIsHall`）。
    default_throw_to_hall = models.BooleanField("默认抛内部抢单", default=False)

    # 游戏权限：至少一款（对标物 JS 原文「请为该用户至少选择一款游戏权限」）。
    # DB 层挡不住多对多为空，由序列化器强制。
    games = models.ManyToManyField("business.Game", related_name="+", blank=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # 「删除时间」= **软删标记**：`null` = 没删；有值 = 已删除及删除时间。为什么必须软删：
    # `Performance.user` 与 `Order.sales_user` 都是 `on_delete=PROTECT`（删用户不得带走历史订单），
    # 只要这人接过单，硬删会被数据库直接拒绝。
    deleted_at = models.DateTimeField("删除时间", null=True, blank=True)

    class Meta:
        verbose_name = "账号档案"
        verbose_name_plural = "账号档案"
        constraints = [
            # 只有两端都有值时才要求有序；缺一端一律放行。
            # ⚠️ Django 5.2.17 要求 `condition=` 是真值的 Q（`django/db/models/constraints.py:175-178`）。
            models.CheckConstraint(
                condition=(
                    models.Q(grab_price_max__isnull=True)
                    | models.Q(grab_price_min__isnull=True)
                    | models.Q(grab_price_max__gte=models.F("grab_price_min"))
                ),
                name="staffprofile_grab_price_range_ordered",
            ),
        ]

    def __str__(self):
        return f"StaffProfile({self.user_id})"


class FavoriteAssignee(models.Model):
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("operator", "assignee"), name="favorite_assignee_unique_pair"),
        ]


class SelfRegistrationConfig(models.Model):
    """自助注册的**租户级单行配置**（spec §2.2，D-0237）。每租户一行，取法固定 `get_or_create(pk=1)`
    （自带 IntegrityError 重查，并发首建不会炸）。游戏池为空时**生成链接**会被 `selfreg_views` 拒掉
    ——配置行本身允许空池（先把开关存下来）。"""

    games = models.ManyToManyField("business.Game", related_name="+", blank=True)
    forbid_orders_default = models.BooleanField("新账号默认禁自助接单", default=False)
    # ⭐ 账号编号（业务方拍板「账号名称变成编号」，spec §2.1 / D-0246）：两列**都非空**才算配置了
    # 编号（`prefix` 空 = 没配）；生成规则在 `selfreg_views._generate_display_name`（注册事务内、
    # 锁配置行）。`number_prefix` 只允许字母（`^[A-Za-z]{1,5}$`）、`number_start` ≥ 1——都判在序列化器。
    number_prefix = models.CharField("编号前缀", max_length=5, blank=True, default="")
    # `number_start` ≥ 1（同上，判在序列化器）；`null` = 没配。
    number_start = models.PositiveIntegerField("起始编号", null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "自助注册配置"
        verbose_name_plural = "自助注册配置"

    def __str__(self):
        return f"SelfRegistrationConfig(pk={self.pk})"


class SelfRegistrationInvite(models.Model):
    """一次性自助注册链接的登记行（spec §2.3）。明文令牌**永不落库**：`token_hash` 只存 sha256
    （算法复用 `peer_sync.hash_token`，⛔ 不另写一份）；「状态」**不落列**——`consumed_at` 非空 ⇒ 已用、
    `now > expires_at` ⇒ 已过期、否则未使用（推导在 `selfreg_views`）。`games` 等是**生成那一刻**的快照。"""

    token_hash = models.CharField(max_length=64, unique=True)
    # `PROTECT`：开户人是这条记录的根，不许随删人消失（spec §2.3）。
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="self_registration_invites",
        verbose_name="生成人",
    )
    games = models.ManyToManyField("business.Game", related_name="+")
    # 注册人实际勾选的游戏（消费时才写）。
    chosen_games = models.ManyToManyField("business.Game", related_name="+")
    forbid_orders_default = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    consumed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = "自助注册链接"
        verbose_name_plural = "自助注册链接"

    def __str__(self):
        return f"SelfRegistrationInvite(pk={self.pk})"


class AppToken(models.Model):
    """App 客户端令牌（打手 App 线）：现有 API 全走 Django Session（Cookie + CSRF），App 没有
    浏览器会话 ⇒ 必须有一条 Bearer 通道，本表是它的唯一凭据来源。明文令牌**永不落库**（`key_hash`
    = sha256，复用 `peer_sync.hash_token`）；过期 = `expires_at` 到点、撤销 = `revoked_at` 非空，都不删行留痕."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="app_tokens",
        verbose_name="所属账号",
    )
    key_hash = models.CharField(max_length=64, unique=True)
    client = models.CharField("客户端", max_length=32, default="app-android")
    created_at = models.DateTimeField("签发时间", auto_now_add=True)
    expires_at = models.DateTimeField("过期时间")
    last_used_at = models.DateTimeField("最后使用时间", null=True, blank=True)
    revoked_at = models.DateTimeField("撤销时间", null=True, blank=True)

    class Meta:
        verbose_name = "App 令牌"
        verbose_name_plural = "App 令牌"
        indexes = [
            models.Index(fields=["user", "revoked_at"], name="apptoken_user_revoked_idx"),
        ]

    def __str__(self):
        return f"AppToken(pk={self.pk}, user={self.user_id})"


class SmsCode(models.Model):
    """短信验证码（打手 App 线）。只存**哈希**不存明文（与 `AppToken` 同一条纪律：泄漏库 ≠ 泄漏
    验证码）。「状态」不落枚举列：`used_at` 非空 ⇒ 已用、`now > expires_at` ⇒ 已过期、否则有效；
    同一手机号可有多条历史行（重发），校验取**最新一条未用且未过期**的。"""

    PURPOSE_LOGIN = "login"
    PURPOSE_CHOICES = [(PURPOSE_LOGIN, "登录")]

    mobile = models.CharField("手机号", max_length=32, db_index=True)
    code_hash = models.CharField(max_length=64)
    purpose = models.CharField(
        "用途", max_length=16, choices=PURPOSE_CHOICES, default=PURPOSE_LOGIN
    )
    created_at = models.DateTimeField("发送时间", auto_now_add=True)
    expires_at = models.DateTimeField("过期时间")
    used_at = models.DateTimeField("使用时间", null=True, blank=True)
    request_ip = models.GenericIPAddressField("请求 IP", null=True, blank=True)
    #: 校验失败次数。**到上限就作废这条码**：6 位码 5 分钟内可暴力枚举，
    #: 失败不计数等于把验证码当成一个 10^6 空间的密码在公网上敞开。
    attempts = models.PositiveSmallIntegerField("失败次数", default=0)

    class Meta:
        verbose_name = "短信验证码"
        verbose_name_plural = "短信验证码"
        indexes = [
            models.Index(fields=["mobile", "created_at"], name="smscode_mobile_created_idx"),
        ]

    def __str__(self):
        return f"SmsCode(pk={self.pk}, mobile={self.mobile})"


class TakerWallet(models.Model):
    """打手钱包（打手 App 线）：`balance` 可用 + `frozen` 押金冻结缺一不可（口径
    `docs/plans/2026-10-19-taker-app-wallet-plan.md` §2.1）；不按订单逐笔冻结（同游戏多单共用一笔押金）。
    ⭐ 解冻冷静期：申请时钱仍冻着、满 7 天懒结算转回、期内接单作废；判据全在 `wallet.py`，⛔ 别处自己算（金额 D-0015）。"""

    #: `PROTECT`：钱包里有余额/押金记录，删账号不能静默带走它。
    #: ⚠️ 与「流水」一起挡住硬删：要删打手得先清空钱（业务上本来也该如此）。
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="taker_wallet",
        verbose_name="打手",
    )
    balance = models.DecimalField("可用余额", max_digits=12, decimal_places=2, default=0)
    frozen = models.DecimalField("押金冻结额", max_digits=12, decimal_places=2, default=0)
    #: ⭐ 解冻申请的**计时起点**（业务方拍板，选项 A）。`None` = 没有待生效的申请。
    #: 满 7 天由懒结算把 `frozen` 转回 `balance` 并清空它；冷期内接单也清空它（申请作废）。
    #: ⚠️ 它**不是**「已解冻」的标记：非空时钱仍然在 `frozen` 里（这正是选项 A 的意思）。
    release_requested_at = models.DateTimeField(
        "申请解冻时间", null=True, blank=True
    )
    #: ⭐ 申请那一刻要退的**金额**（业务方拍板，选项 A；评审补充）。
    #: 结算取 `min(它, frozen)`：冷静期内客服 `frozen_add` 调大的那部分**不是**这次申请的
    #: 钱，⛔ 不能一起退进余额。冻结额被调成 0 的边界 ⇒ 清申请、不写 0 元流水。
    release_requested_amount = models.DecimalField(
        "申请解冻金额",
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "打手钱包"
        verbose_name_plural = "打手钱包"

    def __str__(self):
        return f"TakerWallet(user={self.user_id})"


class TakerDepositQuota(models.Model):
    """打手 × 游戏的**个人押金额度**（业务方拍板，计划 `docs/plans/2026-10-23-taker-deposit-quota-plan.md`）。
    语义唯一出处 `wallet.deposit_amount_for_user`：开关开 + 有行 ⇒ 用本行 `amount`；没行 ⇒ 0 元；开关关 ⇒ 不看本表。
    放 accounts 因跟人走；`user` / `game` 均 `CASCADE`、软删不删行（软删游戏不参与读面，过滤在调用方）。"""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="deposit_quotas",
        verbose_name="打手",
    )
    game = models.ForeignKey(
        "business.Game",
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name="游戏",
    )
    amount = models.DecimalField(
        "个人押金额度", max_digits=12, decimal_places=2, default=0
    )
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "个人游戏押金额度"
        verbose_name_plural = "个人游戏押金额度"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "game"],
                name="uniq_taker_deposit_quota_user_game",
            ),
        ]

    def __str__(self):
        return f"TakerDepositQuota(user={self.user_id}, game={self.game_id})"


class WalletTransaction(models.Model):
    """钱包流水：**每一次金额变化都必须留一行**（人工扣款尤其）。`amount` 记变动量的**绝对值**、方向由
    `kind` 决定；`balance_after` / `frozen_after` 记快照——只累加发现不了「中间被谁改过」。⚠️ 例外：
    `earning_adjust` 的 `amount` **带符号**（对应 `PerformanceAdjustment`，追回负 / 补差正），⛔ 别拿通例去读。"""

    KIND_RECHARGE = "recharge"
    KIND_WITHDRAW = "withdraw"
    KIND_FREEZE = "freeze"
    KIND_RELEASE = "release"
    #: ⭐ **申请解冻**（业务方拍板，选项 A）：只记「打手点了申请」这个事件，**一分钱都不动**
    #: （`balance_after` / `frozen_after` 与申请前相同）。⚠️ 必须与 `KIND_RELEASE` 分开：`release`
    #: 是**钱真的回余额**的那一刻（满 7 天懒结算）；混用会让「申请了」看起来像「已到账」，对账无法区分（E-0011）。
    KIND_RELEASE_REQUEST = "release_request"
    KIND_DEDUCT = "deduct"
    KIND_WITHDRAW_REFUND = "withdraw_refund"
    # ⭐ 客服后台手工调账四个 kind。**为什么不复用现有四条**：流水是「钱的证据」，按 `kind`
    # 才能把「Jeepay 回调充进来的钱」与「客服手工加的钱」分开对账（复用 `recharge` 会把两种来源
    # 读成同一件事，E-0011）；`freeze` / `release` 是打手自己的语义，客服改冻结必须能单独筛出。
    KIND_ADJUST_ADD = "adjust_add"
    KIND_ADJUST_SUB = "adjust_sub"
    KIND_FROZEN_ADD = "frozen_add"
    KIND_FROZEN_SUB = "frozen_sub"
    # ⭐ 打手工资入账（`feat/wallet-single-payout`）：验收通过自动入账。
    # ⚠️ 与 `adjust_add`（客服手工加款）**必须分开**：流水是钱的证据，对账时要能区分
    # 「订单挣的」与「后台补的」。说明 / 关联单号：`ref_no = P<绩效id>`。
    KIND_EARNING = "earning"
    # ⭐ 已结账绩效的调整冲账（退款 / 手工调整）：`amount` 是**带符号**的变动量
    # （规格只给了这一个 kind，无法像别的 kind 那样「绝对值 + kind 表方向」）。
    # 说明 / 关联单号：`ref_no = A<调整记录id>`。
    KIND_EARNING_ADJUST = "earning_adjust"
    # ⭐ 提现打款「先扣再打」的欠款抵扣（D-0336）：发起代付前先把未了结的退款欠款（负余额）抵掉。
    # `amount` **带符号**（与 `earning_adjust` 同通融）：`+` = 发起时抵扣（负余额归零）；`-` = 代付
    # 同步失败时欠款记回。`ref_no = W{提现单id}` 与 `withdraw_refund` 同族 ref，不撞 `_refund_withdraw` 幂等判据（按 kind+ref 查）。
    KIND_WITHDRAW_OFFSET = "withdraw_offset"
    KIND_CHOICES = [
        (KIND_RECHARGE, "充值"),
        (KIND_WITHDRAW, "提现"),
        (KIND_FREEZE, "冻结押金"),
        (KIND_RELEASE, "解冻押金"),
        (KIND_RELEASE_REQUEST, "申请解冻"),
        (KIND_DEDUCT, "扣款"),
        #: 提现被驳回，钱退回余额（申请时就扣了余额，没有这条退回路径钱会卡住）。
        (KIND_WITHDRAW_REFUND, "提现退回"),
        (KIND_ADJUST_ADD, "后台加款"),
        (KIND_ADJUST_SUB, "后台扣款"),
        (KIND_FROZEN_ADD, "后台加押金"),
        (KIND_FROZEN_SUB, "后台减押金"),
        (KIND_EARNING, "工资入账"),
        (KIND_EARNING_ADJUST, "工资调整"),
        (KIND_WITHDRAW_OFFSET, "提现抵扣欠款"),
    ]

    #: `PROTECT`：流水是钱的证据，删钱包不该把它带走（评审 Minor 5：与充值/提现单同口径）。
    wallet = models.ForeignKey(
        TakerWallet, on_delete=models.PROTECT, related_name="transactions", verbose_name="钱包"
    )
    kind = models.CharField("类型", max_length=16, choices=KIND_CHOICES)
    amount = models.DecimalField("金额", max_digits=12, decimal_places=2)
    balance_after = models.DecimalField("变动后余额", max_digits=12, decimal_places=2)
    frozen_after = models.DecimalField("变动后冻结额", max_digits=12, decimal_places=2)
    #: 关联单号（充值单 / 提现单 / 订单号）。手工扣款时由操作人填说明，故可空。
    ref_no = models.CharField("关联单号", max_length=64, blank=True, default="")
    #: ⭐ 客户端单号（客服调账的**幂等键**）。空串 = 这条流水不走幂等登记（既有路径一个不用改）；
    #: 非空时由部分唯一索引 `uniq_wallettx_wallet_client_ref` 兜住「同钱包 + 同客户端单号只记一次」。
    #: ⚠️ 幂等的**真判据**是这一行（钱的证据），见 `apps/accounts/wallet.py::adjust_wallet` 的 docstring。
    client_ref = models.CharField("客户端单号", max_length=64, blank=True, default="")
    note = models.CharField("说明", max_length=200, blank=True, default="")
    #: 操作人：人工扣款/审核时是客服；系统动作（充值回调、冻结）留空。
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="操作人",
    )
    created_at = models.DateTimeField("时间", auto_now_add=True)

    class Meta:
        verbose_name = "钱包流水"
        verbose_name_plural = "钱包流水"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["wallet", "created_at"], name="wallettx_wallet_created_idx")]
        constraints = [
            # ⭐ 客服调账幂等：同一个钱包 + 同一个客户端单号只能有一条流水。
            # `condition=~Q(client_ref="")`：既有流水（空串）不参与唯一性 ⇒ 加这条约束
            # 不动任何存量数据，也不影响充值 / 提现那些不用客户端单号的路径。
            models.UniqueConstraint(
                fields=["wallet", "client_ref"],
                condition=~models.Q(client_ref=""),
                name="uniq_wallettx_wallet_client_ref",
            ),
        ]

    def __str__(self):
        return f"WalletTransaction({self.kind} {self.amount})"


class RechargeOrder(models.Model):
    """押金充值单（走业务方已有的 Jeepay）。`mch_order_no` = 我方订单号（提交给 Jeepay 的
    `mchOrderNo`，全租户唯一）；`jeepay_order_no` = Jeepay 的 `payOrderId`（回调时回填）。"""

    STATE_PENDING = "pending"
    STATE_PAID = "paid"
    STATE_FAILED = "failed"
    STATE_CLOSED = "closed"
    STATE_CHOICES = [
        (STATE_PENDING, "待支付"),
        (STATE_PAID, "已支付"),
        (STATE_FAILED, "支付失败"),
        (STATE_CLOSED, "已关闭"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recharge_orders",
        verbose_name="打手",
    )
    amount = models.DecimalField("金额", max_digits=12, decimal_places=2)
    mch_order_no = models.CharField("商户订单号", max_length=40, unique=True)
    jeepay_order_no = models.CharField("Jeepay 订单号", max_length=40, blank=True, default="")
    way_code = models.CharField("支付方式", max_length=16, blank=True, default="")
    state = models.CharField("状态", max_length=16, choices=STATE_CHOICES, default=STATE_PENDING)
    paid_at = models.DateTimeField("支付时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "充值单"
        verbose_name_plural = "充值单"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"RechargeOrder({self.mch_order_no} {self.amount})"


class WithdrawOrder(models.Model):
    """提现单：**人工审核 + 人工 / 支付宝代付打款**。收款账户取档案支付宝的**申请时刻快照**（之后改
    资料不影响已提交的单）。状态机 pending → approved → paying → paid / failed / rejected / interrupted
    逐支语义见下面各 `STATE_*` 注释与 `wallet.py`；`paid` **不可逆** ⛔；代付对账 / 在途留痕见各列注释。"""

    STATE_PENDING = "pending"
    STATE_APPROVED = "approved"
    #: 转账中（已发起代付、上游还没给终态；`state=1` 必须调查单确认，⛔ 不是成功）。
    STATE_PAYING = "paying"
    STATE_PAID = "paid"
    #: 转账失败（钱已退回余额）。与 `rejected` 分开：这一档是**系统打过但上游失败**。
    STATE_FAILED = "failed"
    STATE_REJECTED = "rejected"
    #: 财务中断：`approved` 上、打款前被人工拦下，钱已退回余额。
    #: ⚠️ 与 `rejected` 分开：驳回只对 `pending`（还没审核），中断只对 `approved`。
    STATE_INTERRUPTED = "interrupted"
    STATE_CHOICES = [
        (STATE_PENDING, "待审核"),
        (STATE_APPROVED, "审核通过，打款中"),
        (STATE_PAYING, "转账中"),
        (STATE_PAID, "已打款"),
        (STATE_FAILED, "转账失败"),
        (STATE_REJECTED, "已驳回"),
        (STATE_INTERRUPTED, "已中断"),
    ]

    #: 打款渠道：人工转账 / Jeepay 支付宝代付。默认人工（开关默认关，见 `WITHDRAW_AUTO_TRANSFER`）。
    CHANNEL_MANUAL = "manual"
    CHANNEL_JEEPAY_ALIPAY = "jeepay_alipay"
    CHANNEL_CHOICES = [
        (CHANNEL_MANUAL, "人工打款"),
        (CHANNEL_JEEPAY_ALIPAY, "支付宝代付"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="withdraw_orders",
        verbose_name="打手",
    )
    amount = models.DecimalField("金额", max_digits=12, decimal_places=2)
    alipay_account = models.CharField("支付宝账号", max_length=64)
    alipay_name = models.CharField("支付宝姓名", max_length=64)
    state = models.CharField("状态", max_length=16, choices=STATE_CHOICES, default=STATE_PENDING)
    #: ⛔ 前三单「快路」已下线（spec §8.2）：列**保留**、历史行不动；`wallet.request_withdraw`
    #: 恒写 `False`，新提现一律走「审核 → 冷静期 → 代付」，本列只剩读历史行一个用途（管理端列表照旧显示）。
    #: ⚠️ 后台不提供改这一列的动作，⛔ 别为「恢复快路」重新写 `True`（历史语义：提现改造增量 §2.3）。
    fast_lane = models.BooleanField("前三单快路", default=False)
    #: ⚠️ `null=True` 是**必须**的：迁移前的存量提现单与纯人工打款的单都没有商户转账单号，
    #: 而「唯一」约束在 PostgreSQL 上允许多个 NULL。非空值仍然唯一 ⇒ 重复发起代付照样被挡。
    mch_order_no = models.CharField(
        "商户转账单号", max_length=64, unique=True, null=True, blank=True, default=None
    )
    #: 上游平台转账单号（对账用）。
    transfer_id = models.CharField("平台转账单号", max_length=64, blank=True, default="")
    #: 上游失败原因（错误码 / 描述）。⚠️ 只在失败档写，成功时清空。
    transfer_error = models.CharField("转账失败原因", max_length=200, blank=True, default="")
    #: 代付请求的**在途标记**：发起 HTTP **之前**写入、拿到回应后清空。为什么需要：
    #: `paying` 同时表示「代付在途」与「人工可兜底」，撞在一起会「上游付一次 + 人工再付一次」。
    #: 列表的 `transfer_inflight` 与 `mark-paid` 的 120 秒闸都读这一列。
    transfer_started_at = models.DateTimeField("代付发起时刻", null=True, blank=True)
    channel = models.CharField(
        "打款渠道", max_length=16, choices=CHANNEL_CHOICES, default=CHANNEL_MANUAL
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="审核人",
    )
    reviewed_at = models.DateTimeField("审核时间", null=True, blank=True)
    #: ⭐ 审核通过的时刻（`feat/wallet-single-payout` §2.1）。与 `reviewed_at`
    #: **同时写、同一个时刻**；单独一列是因为 `payout_after` 的算法写的是它（语义更窄：
    #: 「通过」而不是「审核过」），且将来若加「审核驳回后又通过」时不用改口径。
    approved_at = models.DateTimeField("审核通过时间", null=True, blank=True)
    #: ⭐ 自动代付的最早时刻 = `approved_at + 冷静期`（分钟数见 `WITHDRAW_PAYOUT_DELAY_MINUTES`，
    #: 默认 4320 = 3 天；要测试就改 env，⛔ 代码里不写死）。空 = 不需要自动付款（存量单 / 人工单）；
    #: `payout_due_withdrawals` 只捞 `payout_after <= now` 的单。
    payout_after = models.DateTimeField("自动代付时间", null=True, blank=True)
    #: ⭐ 自动代付**连续失败**次数：命令每失败一次 +1，到上限就停手留人工。
    #: ⚠️ 只在 `approved` / `paying` 两态上累加（终态单不再重试）。
    transfer_attempts = models.PositiveSmallIntegerField("代付尝试次数", default=0)
    paid_at = models.DateTimeField("打款时间", null=True, blank=True)
    #: ⭐ 财务中断的时刻与原因：只在 `interrupted` 上写；钱已退回余额。
    interrupted_at = models.DateTimeField("中断时间", null=True, blank=True)
    interrupt_reason = models.CharField("中断原因", max_length=200, blank=True, default="")
    #: 打款凭证号（支付宝转账单号）或驳回原因——人工流程的唯一留痕，必填在审核动作里校验。
    remark = models.CharField("备注", max_length=200, blank=True, default="")
    created_at = models.DateTimeField("申请时间", auto_now_add=True)

    class Meta:
        verbose_name = "提现单"
        verbose_name_plural = "提现单"
        ordering = ["-created_at", "-id"]
        #: 审核页按状态筛、按时间倒序（计划 §3：迁移一条 + 本索引）。
        indexes = [
            models.Index(fields=["state", "created_at"], name="withdraw_state_created_idx"),
        ]

    def __str__(self):
        return f"WithdrawOrder({self.pk} {self.amount})"


class WxPusherBinding(models.Model):
    """每个打手在本工作室、指定 WxPusher 应用的绑定流程。"""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    app_id = models.CharField(max_length=19, blank=True, default="")
    uid = models.CharField(max_length=128, null=True)
    scan_bound_uid = models.CharField(max_length=128, null=True)
    challenge = models.CharField(max_length=32, blank=True, default="")
    qr_code = models.TextField(blank=True, default="")
    pending_uid = models.CharField(max_length=128, blank=True, default="")
    code_digest = models.CharField(max_length=64, blank=True, default="")
    delivery_confirmed = models.BooleanField(default=False)
    code_failures = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField(null=True)
    last_start_at = models.DateTimeField(null=True)
    last_scan_at = models.DateTimeField(null=True)
    last_test_at = models.DateTimeField(null=True)
    last_link_start_at = models.DateTimeField(null=True)
    last_link_code_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["app_id", "user"], name="wxpusher_binding_app_user"),
            models.UniqueConstraint(fields=["app_id", "uid"], name="wxpusher_binding_app_uid"),
            models.UniqueConstraint(
                models.F("app_id"), Coalesce("uid", "scan_bound_uid"), name="wxpusher_binding_effective_uid",
            ),
            models.CheckConstraint(
                condition=models.Q(uid__isnull=True) | models.Q(scan_bound_uid__isnull=True),
                name="wxpusher_binding_one_uid_source",
            ),
        ]

    @property
    def effective_uid(self):
        return self.uid or self.scan_bound_uid

    @property
    def requires_trusted_browser(self):
        return self.scan_bound_uid is not None

    def clear_challenge(self):
        self.challenge = self.qr_code = self.pending_uid = self.code_digest = ""
        self.code_failures = 0
        self.delivery_confirmed = False
        self.expires_at = None


class WxPusherLinkAttempt(models.Model):
    """A pending browser subscription, independent of the active recipient."""

    binding = models.OneToOneField(WxPusherBinding, on_delete=models.CASCADE)
    challenge = models.CharField(max_length=32, unique=True)
    expires_at = models.DateTimeField()
    verified_uid = models.CharField(max_length=128, blank=True, default="")
    checked_at = models.DateTimeField(null=True)
    code_uid = models.CharField(max_length=128, blank=True, default="")
    code_digest = models.CharField(max_length=64, blank=True, default="")
    code_expires_at = models.DateTimeField(null=True)
    code_failures = models.PositiveSmallIntegerField(default=0)


class WxPusherLinkRecipientThrottle(models.Model):
    """One receipt limit for a subscriber across all accounts in the studio."""

    digest = models.CharField(max_length=64, primary_key=True)
    blocked_until = models.DateTimeField(db_index=True)


class WxPusherLoginNonce(models.Model):
    """已使用的一键登录随机串，仅保留摘要。"""

    digest = models.CharField(max_length=64, primary_key=True)
    expires_at = models.DateTimeField(db_index=True)
    accepted_at = models.DateTimeField(null=True)


class WxPusherPreference(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True)
    order_enabled = models.BooleanField(default=True)
    settlement_enabled = models.BooleanField(default=True)
    disabled_order_events = models.JSONField(default=list)
    staff_enabled = models.BooleanField(default=False, db_default=False)
    disabled_staff_events = models.JSONField(default=list, db_default=[])
    hall_enabled_since = models.DateTimeField(null=True)


class WxPusherHallSubscription(models.Model):
    preference = models.ForeignKey(WxPusherPreference, on_delete=models.CASCADE, related_name="hall_games")
    game = models.ForeignKey("business.Game", on_delete=models.CASCADE)
    selected_at = models.DateTimeField()
    price_min = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    price_max = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    include_keywords = models.JSONField(default=list)
    exclude_keywords = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["preference", "game"], name="wxpusher_hall_user_game"),
        ]


class WxPusherHallPublication(models.Model):
    identity = models.CharField(max_length=80, unique=True)
    source = models.CharField(max_length=16)
    ref = models.CharField(max_length=20)
    app_id = models.CharField(max_length=19)
    game = models.ForeignKey("business.Game", on_delete=models.CASCADE)
    listed_at = models.DateTimeField()
    processed_at = models.DateTimeField(null=True)
    retry_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=["id"], condition=models.Q(processed_at__isnull=True),
                         name="wxpusher_hall_unprocessed"),
        ]


class WxPusherNotice(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待发送"
        ATTEMPTED = "attempted", "已开始推送"
        SENT = "sent", "已受理"
        SKIPPED = "skipped", "不再发送"
        UNCONFIRMED = "unconfirmed", "受理结果未确认"

    class SkipReason(models.TextChoices):
        APP_UNCONFIGURED = "app_unconfigured", "工作室未配置 WxPusher"
        WORKSPACE_DISABLED = "workspace_disabled", "工作室关闭推送"
        PERSONAL_DISABLED = "personal_disabled", "接收人关闭推送"
        NOT_BOUND = "not_bound", "接收人未绑定"
        BINDING_CHANGED = "binding_changed", "绑定已变更"
        RECIPIENT_UNAVAILABLE = "recipient_unavailable", "接收人已停用或不再负责"
        HALL_UNAVAILABLE = "hall_unavailable", "订单已离开大厅或接收人无抢单资格"
        HALL_RULE_CHANGED = "hall_rule_changed", "订单不再符合订阅筛选"
        GROUP_UNAVAILABLE = "group_unavailable", "已退群、消息已撤回或群聊已停用"
        GROUP_ALREADY_READ = "group_already_read", "消息已读"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    binding = models.ForeignKey(WxPusherBinding, null=True, blank=True, on_delete=models.SET_NULL)
    binding_started_at = models.DateTimeField(null=True)
    kind = models.CharField(max_length=32)
    reference = models.CharField(max_length=255)
    order_no = models.CharField(max_length=20, blank=True, default="")
    details = models.JSONField(default=dict, db_default={}, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    attempted_at = models.DateTimeField(null=True)
    skip_reason = models.CharField(max_length=32, choices=SkipReason.choices, blank=True, default="", db_default="")
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "kind", "reference"], name="wxpusher_notice_event_user"),
        ]
        indexes = [
            models.Index(fields=["id"], condition=models.Q(status="pending"), name="wxpusher_notice_pending"),
            models.Index(fields=["order_no"], name="wxpusher_notice_order_no"),
            models.Index(fields=["user", "-id"], name="wxpusher_notice_inbox"),
        ]


class WxPusherTenantSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    app_id = models.CharField(max_length=19, blank=True, default="")
    app_token_encrypted = models.TextField(blank=True, default="")
    signing_key_encrypted = models.TextField(blank=True, default="")
    subscribe_url = models.URLField(max_length=200, blank=True, default="")
    qr_url = models.URLField(max_length=512, blank=True, default="")
    push_enabled = models.BooleanField(default=False)
    login_enabled = models.BooleanField(default=False)
    binding_prompt_enabled = models.BooleanField(default=False, db_default=False)
    order_enabled = models.BooleanField(default=True)
    settlement_enabled = models.BooleanField(default=True)
    disabled_order_events = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="wxpusher_settings_singleton")]
