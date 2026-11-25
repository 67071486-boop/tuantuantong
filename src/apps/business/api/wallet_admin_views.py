"""客服后台的「打手钱包管理 / 调账」「游戏押金金额」与「钱包流水明细」端点（2026-10-20 `feat/taker-wallet-adjust`；流水明细 2026-10-23 `feat/wallet-ledger`）。 | 方法 | 路径 | 动作 | |---|---|---| | GET | `/api/business/wallets/` | 打手钱包列表（`?q=` 按账号 / 昵称 / 电话搜；`?sort=` 排序；显示余额、冻结、押金够不够接单） | | POST | `/api/business/wallets/<pk>/adjust/` | 加 / 减可用余额、加 / 减押金冻结（<pk> 就是打手的 user id） | | GET | `/api/business/deposit-rules/` | 每款游戏的押金金额（当前生效值 + 来源） | | PUT | `/api/business/deposit-rules/<game_id>/` | 改某款游戏的押金金额 | | GET | `/api/business/takers/<user_id>/deposit-quota/` | 某打手的**个人押金额度**（开关 + 每款游戏的全局 / 个人额度） | | PUT | `/api/business/takers/<user_id>/deposit-quota/` | 保存某打手的个人押金额度（开关 + 按游戏金额；**金额可缺席 = 只改开关**） | | GET | `/api/business/wallet-transactions/` | 跨打手的钱包流水分页明细（筛选：`q` / `kind` / `date_from` / `date_to`） | ## 排序（列表） `sort` ∈ `frozen` / `balance` / `username`（缺省 `frozen`），`order` ∈ `desc` / `asc` （缺省 `desc`）。实现要点与两条硬要求： - 金额注解**必须显式 `output_field`**：`Coalesce("taker_wallet__frozen", Value(Decimal("0.00")))`
不加会让 `DecimalField` 与字面量 `0` 混类型 ⇒ `FieldError` ⇒ 整页 500（评审抓到）； - 排序固定为：主键（按 `sort`/`order`）→ 另一个金额**降序** → `username` 升序 → `pk` （后两键只为**稳定分页**，不改变业务语义）。 ## 流水明细的范围 / 日志口径 - **流水是钱的历史证据 ⇒ 包含已软删打手的流水**。钱包列表只列在职打手（`staff_profile__deleted_at__isnull=True`），这是**有意的差异**：删掉一个打手不该让他曾经的钱说不清；两面的差异写在这里，⛔ 不要为了「一致」给流水加在职过滤。 - 本模块的读端点（四个 `GET`：钱包列表 / 押金列表 / 流水明细 / 个人押金额度）**不写操作日志**：GET 不在 `WRITE_METHODS` 里， `DELIBERATELY_NOT_LOGGED` 那张表只收写端点（与 `xgj_receipt_views.py` 同口径）。 ## 三条口径（照 `withdraw_admin_views` 的结构） 1. **只认本租户**：`User` / `Game` / `WalletTransaction` 都是租户库里的表，查询只从当前 schema 取，⛔ **不接受任何租户标识参数**（`AGENTS.md` §4）。 2. **闸门 = `finance.taker_wallet_adjust`**（`apps/rbac/catalog.py` 的自有节点， `benchmark_id=None`）：本模块 2 个 ViewSet + 3 个 APIView 都在**类体**写 `permission_classes = [IsAuthenticated, has_node(...)]`，**fail-closed**（没节点 403）。
3. **金额一律字符串出参**（与钱包 / 提现端点同口径）：`Decimal` 交给 DRF 的 JSONEncoder 会被 `float()`（`rest_framework/utils/encoders.py`）⇒ 本模块显式 `f"{value:.2f}"`，⛔ 不 `float()`。 ## 调账的三条硬要求（业务方口径，逐条有对应用例） - **原因必填**：进 `WalletTransaction.note`（钱的证据）**和**操作日志的 `reason`； - **单笔上限**：`WALLET_ADJUST_MAX_AMOUNT`（默认 1000 元；`is not None` 判据，见 `wallet.py`）； - **幂等**：入参必须带 `client_request_no`（客户端单号），同一单号重复提交**不重复记账**、返回第一次那笔的同一份结果（`duplicate=true`）。判据落在 `WalletTransaction.client_ref` 那一行 + 部分唯一索引上，详见 `apps/accounts/wallet.py::adjust_wallet`。 ## 押金规则没有自创字段「读」的口径完全复用 `apps/accounts/wallet.py::_effective_deposit`：**规则行 > 三角洲名字兜底（300）> 默认 50**。出参里的 `source` 只是把这个派生结果说出来（`rule` / `default_delta` / `default`），不是新列；「写」只改 `GameDepositRule.amount` 这一个真实字段。 ⚠️ 改押金会直接改变「谁能接单」（`deposit_gate` 的 `frozen >= 该游戏要求`）——前端同一次把这句话写在页面上。
"""
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import DecimalField, Q, Value
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from rest_framework import serializers, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.accounts.models import (
    MemberType,
    StaffProfile,
    TakerDepositQuota,
    TakerWallet,
    WalletTransaction,
)
from apps.accounts.wallet import (
    DEFAULT_DEPOSIT_AMOUNT,
    DELTA_DEPOSIT_AMOUNT,
    PERSONAL_DEPOSIT_AMOUNT,
    WalletError,
    # ⚠️ 私有名，但**有意**跨模块复用：押金取值顺序（规则行 > 三角洲名字兜底 > 默认 50）的唯一
    #    出处就是它，且它带 `rule=` 预取入参（列表页要「整页只查一次规则行」）。
    _effective_deposit,
    adjust_max_amount,
    adjust_wallet,
    deposit_amount_for_game,
    deposit_rule_rows,
    set_deposit_rule,
)
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.base import EnvelopePagination
from apps.business.models import Game, GameDepositRule
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response, paginated
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

#: 钱包管理页的权限节点（本仓**第六条**自有节点，`benchmark_id=None`）。 ⚠️ 接线位点表（`apps/rbac/wiring.py` 的 `SITES`）指向本模块五个类的
#: `permission_classes`（2 个 ViewSet + 3 个 APIView）；改名要同批改那里（`tests/test_rbac_wiring.py` 的 T2 按 AST 核对）。
WALLET_ADJUST_NODE = "finance.taker_wallet_adjust"

#: 审计动作码（`apps/audit/actions.py` 的登记键）。
WALLET_ADJUST_ACTION = "finance.taker_wallet_adjust"
DEPOSIT_RULE_ACTION = "finance.taker_deposit_rule_update"
#: ⭐ 个人押金额度：开关与金额**两条码**。开关动的是「按谁的口径算」，
#: 金额动的是某一款游戏冻多少 —— 事后追查时这两件事必须能分开筛。
DEPOSIT_QUOTA_SWITCH_ACTION = "finance.taker_deposit_quota_switch"
DEPOSIT_QUOTA_AMOUNT_ACTION = "finance.taker_deposit_quota_amount"

#: 个人额度单款上限（元）= 列宽上限（`max_digits=12, decimal_places=2` ⇒ 10 位整数）。
#: ⚠️ 超过它写库会 `DataError` ⇒ 500，所以在入口就拒成业务码 1000。
DEPOSIT_QUOTA_MAX_AMOUNT = Decimal("9999999999.99")


def _money(value) -> str:
    """金额出参的唯一格式化点：两位小数、字符串（与 `app_wallet_views._money` 同口径）。"""
    return f"{value:.2f}"


def _first_error(serializer) -> str:
    for errors in serializer.errors.values():
        if errors:
            return str(errors[0])
    return "参数不正确"


def _parse_amount(value):
    """把入参收敛成 `Decimal`：**只校验格式**，业务边界（正负 / 上限）归服务层。

    ⚠️ 用 `CharField` 而不是 `DecimalField`：DRF 的 `DecimalField` 会把多余精度**四舍五入**
    （钱不能静默变），这里选择**拒绝**（与 `WithdrawRequestSerializer` 同一条理由）。
    """
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise serializers.ValidationError("金额格式不正确")
    if not amount.is_finite():
        raise serializers.ValidationError("金额格式不正确")
    if amount.as_tuple().exponent < -2:
        raise serializers.ValidationError("金额最多两位小数")
    return amount


def _parse_quota_amount(value):
    """个人押金额度的入参：复用 `_parse_amount` 的格式拒绝策略，再加业务边界。 - `_parse_amount` 已经挡住：解析失败 / 非有限数 / 多于两位小数（**拒绝，不四舍五入**）； - 本函数再挡：`< 0`（负数会让「已冻 >= 要求」恒真 = 关掉押金闸）与 > 列宽上限
    （`DEPOSIT_QUOTA_MAX_AMOUNT`，否则落库 `DataError` → 500）。 - ⚠️ **0 合法**（= 该游戏免押，D-0015 的合法边界），判据是 `< 0` 而不是「非正」。
    """
    amount = _parse_amount(value)
    if amount < 0:
        raise serializers.ValidationError("额度不能为负数")
    if amount > DEPOSIT_QUOTA_MAX_AMOUNT:
        raise serializers.ValidationError(
            f"额度不能超过 {DEPOSIT_QUOTA_MAX_AMOUNT} 元"
        )
    return amount


class WalletAdminPagination(EnvelopePagination):
    """钱包列表分页：在标准信封的 `data` 里多带单笔上限（页面据此提示）。"""

    def get_paginated_response(self, data):
        payload = paginated(
            data,
            total=self.page.paginator.count,
            page=self.page.number,
            page_size=self.get_page_size(self.request),
        )
        limit = adjust_max_amount()
        # 上限未配置（None）⇒ 不限制；给空串，前端按「没有上限」处理（同提现审核页）。
        payload["adjust_max_amount"] = "" if limit is None else _money(limit)
        return api_response(payload)


#: 列表排序键（缺省 `frozen`）与方向（缺省 `desc`）。非法值一律业务码 1000。
WALLET_SORT_CHOICES = [
    ("frozen", "押金冻结"),
    ("balance", "可用余额"),
    ("username", "打手账号"),
]
WALLET_ORDER_CHOICES = [("desc", "降序"), ("asc", "升序")]

#: 流水日界用的时区（与页面显示一致；⛔ 不按 UTC 切）。
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class _WalletListQuerySerializer(serializers.Serializer):
    """列表筛选 / 排序参数的唯一校验口（超长 `q`、非法 `sort`/`order` ⇒ 业务码 1000）。"""

    q = serializers.CharField(
        max_length=64, required=False, allow_blank=True, trim_whitespace=True
    )
    sort = serializers.ChoiceField(
        choices=WALLET_SORT_CHOICES,
        required=False,
        error_messages={"invalid_choice": "排序字段不正确"},
    )
    order = serializers.ChoiceField(
        choices=WALLET_ORDER_CHOICES,
        required=False,
        error_messages={"invalid_choice": "排序方向不正确"},
    )


class _AdjustSerializer(serializers.Serializer):
    """调账入参。

    ⚠️ **`client_request_no` 必填**：它是幂等键（见模块 docstring），⛔ 不给它默认值
    ——那会让「忘了传」的调用方在重复提交时悄悄记两笔。
    """

    target = serializers.ChoiceField(
        choices=[("balance", "可用余额"), ("frozen", "押金冻结额")],
        error_messages={
            "invalid_choice": "调账项目不正确",
            "required": "请选择调账项目",
            "null": "请选择调账项目",
        },
    )
    direction = serializers.ChoiceField(
        choices=[("increase", "增加"), ("decrease", "减少")],
        error_messages={
            "invalid_choice": "调账方向不正确",
            "required": "请选择调账方向",
            "null": "请选择调账方向",
        },
    )
    amount = serializers.CharField(
        max_length=32,
        error_messages={"required": "请填写调账金额", "blank": "请填写调账金额"},
    )
    # ⚠️ 错误文案**必须带字段名**（E-0074 口诀：不带字段名的基本都是框架内置消息）。
    reason = serializers.CharField(
        max_length=200,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={"required": "请填写调账原因", "blank": "请填写调账原因"},
    )
    client_request_no = serializers.CharField(
        max_length=64,
        allow_blank=False,
        trim_whitespace=True,
        error_messages={
            "required": "请提供客户端单号（用于防重复提交）",
            "blank": "请提供客户端单号（用于防重复提交）",
        },
    )

    def validate_amount(self, value):
        return _parse_amount(value)


class _DepositRuleSerializer(serializers.Serializer):
    """押金金额入参：格式在这里校验，`< 0` 由服务层拒（业务码 1000）。"""

    amount = serializers.CharField(max_length=32)

    def validate_amount(self, value):
        return _parse_amount(value)


class _DepositQuotaSerializer(serializers.Serializer):
    """个人押金额度入参（计划 §5.1）：`enabled` 必填 bool、`amounts` **可选**「游戏 id → 金额」。 ⭐ **`amounts` 缺席 = 只改开关、一个 `TakerDepositQuota` 行都不动**（2026-10-23 追加）：人员管理页的「个人押金额度」行内开关只发 `{"enabled": …}`，⛔ 不能因为「没传金额」
    就把这个人的每游戏额度清掉（钱相关；缺省语义必须是「不改」而不是「改成 0」）。 `amounts` 显式传 `{}` 与不传等价（都没东西可写）；传了非空对象才按行 upsert。 ⚠️ 这里只校验**形状**；「游戏 id 是不是这个打手有权限的」「金额边界」在视图里逐条判，
    全部通过后才进事务 ⇒ 任何校验失败都 0 写入（计划 §5.1 最后一条）。 ⚠️ 金额**不用** DRF 的 `DecimalField`：它会把多余精度**四舍五入**，钱不能静默变（同 `_parse_amount` 的理由）⇒ 值原样收下、由 `_parse_quota_amount` 解析并拒绝。
    """

    enabled = serializers.BooleanField(
        error_messages={
            "required": "请选择是否启用个人押金额度",
            "invalid": "「启用个人押金额度」必须是布尔值",
        }
    )
    amounts = serializers.DictField(
        required=False,
        # ⚠️ **不挂 `child=`**：DRF 默认的 `_UnvalidatedField` 只收形状、值原样交给
        # `_parse_quota_amount`。挂 `CharField` 的话「值超长 / 空串」会在嵌套层报错，
        # 而 `_first_error` 只认 `errors[0]`（嵌套是 dict）⇒ 会变成 500。
        error_messages={
            "not_a_dict": "各游戏的个人额度必须是对象（游戏 id → 金额）",
        },
    )


def _deposit_quota_payload(user, profile) -> dict:
    """个人押金额度的出参（GET 与 PUT 同形，计划 §5.1）。 - `games` 只列**这个打手有权限且未软删**的游戏（与 App 钱包同一支取面）； - `global_amount` 永远是全局生效值（规则行 > 三角洲兜底 > 50）；
    - `amount` 是**个人行**的金额；没有行 ⇒ `null`（前端显示空，保存时按 0）。 - `enabled=false` 时也返回已存的个人金额（方便开关打开后接着用）。
    """
    games = list(profile.games.filter(deleted_at__isnull=True).order_by("id"))
    rules = (
        {
            rule.game_id: rule
            for rule in GameDepositRule.objects.filter(
                game_id__in=[game.pk for game in games]
            )
        }
        if games
        else {}
    )
    quotas = (
        dict(
            TakerDepositQuota.objects.filter(
                user=user, game_id__in=[game.pk for game in games]
            ).values_list("game_id", "amount")
        )
        if games
        else {}
    )
    return {
        "user_id": user.pk,
        "username": user.get_username(),
        "display_name": user_display_name(user),
        "enabled": bool(profile.deposit_quota_enabled),
        "games": [
            {
                "game_id": game.pk,
                "game_name": game.name,
                "global_amount": _money(
                    _effective_deposit(game, rules.get(game.pk))[0]
                ),
                "amount": _money(quotas[game.pk]) if game.pk in quotas else None,
            }
            for game in games
        ],
    }


def _quota_switch_label(enabled) -> str:
    """开关的审计用中文值（模板 `…{from_value} → {to_value}` 里的「关 / 开」）。"""
    return "开" if enabled else "关"


def _profile_games(user) -> list:
    """该打手勾了的、**未软删**的游戏（列表页已 `prefetch_related` 预取，⛔ 不逐行查库）。

    与 App 侧 `/api/app/wallet/` 的取面同一支（`profile.games.filter(deleted_at__isnull=True)`）：
    软删的游戏打手也接不了，不能把它要求的额度算进「这个人够不够接单」。
    """
    profile = getattr(user, "staff_profile", None)
    if profile is None:
        return []
    return [game for game in profile.games.all() if game.deleted_at is None]


def _required_deposits(users) -> dict:
    """每个打手「押金够不够」的判据：**他用的游戏里要求最大的那一档**；一款都没有 ⇒ `None`。 - 为什么取**最大**：客服页要回答「这个人现在接得了单吗」，只有冻到他自己最贵那一款才一款都不被挡；取全租户**最低**档会放过「只勾了 300 那款、却只冻了 50」的人（P1-9）。 - 取值顺序**不在这里重写**：一律走 `wallet._effective_deposit`（规则行 > 三角洲名字兜底 > 默认 50），与 App 接单前的闸（`wallet.ensure_deposit_for_claim`）同源。 - ⭐ **个人押金额度（业务方，计划 §4.2）**：开关开 ⇒ 该人各游戏的**个人额度**
    （缺行 = 0）取 max；开关关 ⇒ 继续用全局。本函数只做「按人分档」，取金额的兜底值直接用 `PERSONAL_DEPOSIT_AMOUNT` / `_effective_deposit`（与 `wallet.deposit_amounts_for_user` 同源的两个符号，⛔ 不另写 0 / 50 的字面量）。 - 一款游戏都没勾 ⇒ `None`（**未知**），⛔ **不回落 0**：0 会被读成「接单不需要押金」，与「我们不知道他要多少」是两件事。 - ⚠️ 每页只查**一次**规则行 + 一次开关 + 一次个人额度行（⛔ 逐行查）：N+1 由
    `tests.test_taker_wallet_admin_api.WalletListDepositCriterionTests` 的查询数对比守着。
    """
    games = {}
    for user in users:
        for game in _profile_games(user):
            games[game.pk] = game
    rules = (
        {rule.game_id: rule for rule in GameDepositRule.objects.filter(game_id__in=games)}
        if games
        else {}
    )
    user_ids = [user.pk for user in users]
    # ⭐ 个人额度：整页**一次**取开关、**一次**取额度行（⛔ 不逐人 / 逐游戏查库）。
    enabled_user_ids = set(
        StaffProfile.objects.filter(
            user_id__in=user_ids, deposit_quota_enabled=True
        ).values_list("user_id", flat=True)
    )
    personal_amounts = {}
    if enabled_user_ids and games:
        # ⚠️ `values_list` 三列 ⇒ 先按 `(user_id, game_id)` 组装，⛔ 不能直接 `dict(...)`
        #    （`dict()` 只吃二元组，三元组会当场 `ValueError`）。
        personal_amounts = {
            (user_id, game_id): amount
            for user_id, game_id, amount in TakerDepositQuota.objects.filter(
                user_id__in=enabled_user_ids, game_id__in=games
            ).values_list("user_id", "game_id", "amount")
        }
    required = {}
    for user in users:
        user_games = _profile_games(user)
        if user.pk in enabled_user_ids:
            amounts = [
                personal_amounts.get(
                    (user.pk, game.pk), PERSONAL_DEPOSIT_AMOUNT
                )
                for game in user_games
            ]
        else:
            amounts = [
                _effective_deposit(game, rules.get(game.pk))[0]
                for game in user_games
            ]
        required[user.pk] = max(amounts) if amounts else None
    return required


def _wallet_row(user, wallet, *, deposit_required) -> dict:
    """钱包列表的一行。 `deposit_enough` = `frozen >= 该打手所用游戏里要求的最大额度`（见 `_required_deposits`）。
    `deposit_required is None` = 这个人一款游戏都没勾 ⇒ 押金要求**未知**，此时 `deposit_enough` 也回 `None`（⛔ 不写 `False`：那是「不够」，会骗客服去做不必要的补冻）。
    """
    balance = wallet.balance if wallet is not None else Decimal("0")
    frozen = wallet.frozen if wallet is not None else Decimal("0")
    # ⚠️ 边界判据用 `is not None`（E-0021）：0 是合法且有意义的一档（= 接单不需要冻结）。
    known = deposit_required is not None
    return {
        "user_id": user.pk,
        "username": user.get_username(),
        "display_name": user_display_name(user),
        "mobile": getattr(getattr(user, "staff_profile", None), "mobile", "") or "",
        # 金额一律字符串（见模块 docstring 第 3 条）。
        "balance": _money(balance),
        "frozen": _money(frozen),
        "deposit_required": _money(deposit_required) if known else None,
        "deposit_enough": (frozen >= deposit_required) if known else None,
        # ⚠️ D-0302：手拼时刻一律 `local_iso`（+08:00）；前端截串直显。
        "updated_at": local_iso(wallet.updated_at) if wallet is not None else None,
    }


def _wallet_money_expression(field: str):
    """钱包金额的排序注解：没有钱包行 ⇒ `0.00`（⛔ 不让 NULL 决定整页顺序）。

    ⚠️ `output_field` **必须显式**给：`Coalesce` 里一边是 `DecimalField`、一边是字面量，
    不给类型会让 Django 抛 `FieldError: Expression contains mixed types`（整页 500）。
    """
    return Coalesce(
        f"taker_wallet__{field}",
        Value(Decimal("0.00")),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )


def _snapshots(tx: WalletTransaction):
    """从一条调账流水反推「变动前 / 后」四个数：`(余额前, 余额后, 冻结前, 冻结后)`。

    ⚠️ 四个 kind 各一条分支，⛔ 不靠调用方传：第一次与重复提交必须得出同一份结果
    （幂等语义），而重复提交时手上只有流水、没有当时的钱包对象。
    """
    amount = tx.amount
    if tx.kind == WalletTransaction.KIND_ADJUST_ADD:
        return tx.balance_after - amount, tx.balance_after, tx.frozen_after, tx.frozen_after
    if tx.kind == WalletTransaction.KIND_ADJUST_SUB:
        return tx.balance_after + amount, tx.balance_after, tx.frozen_after, tx.frozen_after
    if tx.kind == WalletTransaction.KIND_FROZEN_ADD:
        return tx.balance_after, tx.balance_after, tx.frozen_after - amount, tx.frozen_after
    # KIND_FROZEN_SUB（`adjust_wallet` 只会写上面四种，这里是它）
    return tx.balance_after, tx.balance_after, tx.frozen_after + amount, tx.frozen_after


def _adjust_payload(user, tx: WalletTransaction, *, duplicate: bool) -> dict:
    """调账结果：**第一次与重复提交回同一份**（幂等），before/after 从流水快照反推。

    ⚠️ `after` 取流水上的快照（`*_after`），⛔ 不取「现在」的钱包值：重复提交发生在
    后续又有别的调账之后时，回「现在」的余额会让客服以为这次也动了钱。
    """
    balance_before, balance_after, frozen_before, frozen_after = _snapshots(tx)
    return {
        "user_id": user.pk,
        "username": user.get_username(),
        "display_name": user_display_name(user),
        "transaction_id": tx.pk,
        "kind": tx.kind,
        "amount": _money(tx.amount),
        "reason": tx.note,
        "balance_before": _money(balance_before),
        "balance_after": _money(balance_after),
        "frozen_before": _money(frozen_before),
        "frozen_after": _money(frozen_after),
        # ⭐ 幂等命中 ⇒ `true` + 第一次那一行；页面据此说「这笔已经记过账，没有重复扣/加」。
        "duplicate": duplicate,
        # ⚠️ D-0302：手拼时刻一律 `local_iso`（+08:00）。
        "created_at": local_iso(tx.created_at),
    }


class TakerWalletAdminViewSet(viewsets.GenericViewSet):
    """打手钱包列表 + 调账。"""

    #: ⚠️ 类体装配（T2 的「类.类属性」形态）；节点 code 是模块常量，T2 会向下解引用一层。
    permission_classes = [IsAuthenticated, has_node(WALLET_ADJUST_NODE)]

    pagination_class = WalletAdminPagination
    http_method_names = ["get", "post", "head", "options"]

    def permission_denied(self, request, message=None, code=None):
        """给 403 一句可辨的文案（必须转调 `super()`：它先判认证并抛 401）。"""
        super().permission_denied(
            request, message="你没有打手钱包调账的权限", code=code
        )

    def get_queryset(self):
        """本租户的**打手**（合作用户、未软删）。 ⚠️ 只列 `member_type=合作用户`：客服页要处理的是打手 / 工作室的钱包，不是内部客服与管理员。⛔ **不接受租户参数**——租户上下文只来自认证态（`AGENTS.md` §4）。
        ⚠️ 软删的账号不列：他们进不了 App、也接不了单；要处理历史余额是另一件事（本批不做）。 ⚠️ `staff_profile__games` 必须**预取**：列表页的「押金够不够」按**这个人自己的游戏** 算（`_required_deposits`），逐行查就是 N+1。
        """
        return (
            get_user_model()
            .objects.filter(
                staff_profile__member_type=MemberType.PARTNER,
                staff_profile__deleted_at__isnull=True,
            )
            .select_related("staff_profile")
            .prefetch_related("staff_profile__games")
            .order_by("username")
        )

    # ── 读：列表 ──────────────────────────────────────────────────
    def list(self, request, *args, **kwargs):
        raw = {key: value for key, value in request.query_params.items() if value != ""}
        params = _WalletListQuerySerializer(data=raw)
        if not params.is_valid():
            return api_response(None, msg=_first_error(params), code=ErrorCode.VALIDATION)

        queryset = self.get_queryset()
        keyword = params.validated_data.get("q") or ""
        if keyword:
            queryset = queryset.filter(
                Q(username__icontains=keyword)
                | Q(first_name__icontains=keyword)
                | Q(staff_profile__mobile__icontains=keyword)
            )

        # 排序：注解两个金额（没有钱包行 ⇒ 0）后按「主键 → 另一个金额降序 → username → pk」。
        # ⚠️ `output_field` 必须显式给（见模块 docstring「排序」段）：`DecimalField` 与字面量 `0`
        #    混类型会让 Django 抛 `FieldError` ⇒ 整页 500。
        queryset = queryset.annotate(
            frozen_amount=_wallet_money_expression("frozen"),
            balance_amount=_wallet_money_expression("balance"),
        )
        sort = params.validated_data.get("sort", "frozen")
        order = params.validated_data.get("order", "desc")
        prefix = "" if order == "asc" else "-"
        if sort == "username":
            # 「主键」是账号；副键按缺省金额键（冻结）降序，只为稳定分页。
            queryset = queryset.order_by(f"{prefix}username", "-frozen_amount", "pk")
        else:
            primary = "frozen_amount" if sort == "frozen" else "balance_amount"
            secondary = "balance_amount" if sort == "frozen" else "frozen_amount"
            queryset = queryset.order_by(
                f"{prefix}{primary}", f"-{secondary}", "username", "pk"
            )

        # 每页只查一次「每个打手的押金要求」与钱包行（⛔ 不逐行查库）：游戏走 prefetch，
        # 押金规则整页一次查询，见 `_required_deposits`。
        page = self.paginate_queryset(queryset)
        required = _required_deposits(page)
        wallets = {
            wallet.user_id: wallet
            for wallet in TakerWallet.objects.filter(
                user_id__in=[user.pk for user in page]
            )
        }
        rows = [
            _wallet_row(user, wallets.get(user.pk), deposit_required=required.get(user.pk))
            for user in page
        ]
        return self.get_paginated_response(rows)

    # ── 写：调账 ──────────────────────────────────────────────────
    @action(detail=True, methods=["post"], url_path="adjust")
    def adjust(self, request, pk=None):
        """加 / 减余额或押金冻结。重复提交（同一 `client_request_no`）⇒ 原样回第一次的结果。"""
        user = self.get_object()
        serializer = _AdjustSerializer(
            data=request.data if isinstance(request.data, dict) else {}
        )
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)
        data = serializer.validated_data
        try:
            _wallet, tx, created = adjust_wallet(
                user,
                target=data["target"],
                direction=data["direction"],
                amount=data["amount"],
                reason=data["reason"],
                client_ref_no=data["client_request_no"],
                operator=request.user,
            )
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        # ⚠️ 只有**真的记了账**才写审计：重复提交是幂等命中，不是第二次操作
        #    （否则同一笔会留下两行「调整了打手钱包」，钱的证据与日志对不上）。
        if created:
            balance_before, balance_after, frozen_before, frozen_after = _snapshots(tx)
            if data["target"] == "balance":
                before, after = balance_before, balance_after
            else:
                before, after = frozen_before, frozen_after
            self._record_adjust(
                user,
                target=data["target"],
                before=before,
                after=after,
                reason=data["reason"],
            )
        return api_response(_adjust_payload(user, tx, duplicate=not created))

    # ── 审计埋点（一次真的记账恰好一行）──────────────────────────
    def _record_adjust(self, user, *, target, before, after, reason) -> None:
        """写一条操作日志。**重复提交（幂等命中）不写**，见调用点的注释。

        ⚠️ 本助手名已登记进 `tests/test_operation_log_coverage.py` 的
        `_RECORD_FORWARDERS`；⛔ 改名要同批改那里，否则本端点会被判「声称已埋点却找不到埋点」。
        """
        record(
            log_type=ACTION_LOG_TYPES[WALLET_ADJUST_ACTION],
            action=WALLET_ADJUST_ACTION,
            actor=self.request.user.get_username(),
            actor_id=self.request.user.pk,
            target_kind="taker_wallet",
            target_id=str(user.pk),
            target_name=user_display_name(user),
            field_name=target,
            from_value=_money(before),
            to_value=_money(after),
            reason=reason,
        )


class _WalletTransactionQuerySerializer(serializers.Serializer):
    """流水明细的筛选参数（唯一校验口；非法 `kind` / 日期 ⇒ 业务码 1000，⛔ 不是 500）。"""

    q = serializers.CharField(
        max_length=64, required=False, allow_blank=True, trim_whitespace=True
    )
    #: 类型白名单**只有一个出处**：`WalletTransaction.KIND_CHOICES`（⛔ 这里不重抄 13 种中文）。
    kind = serializers.ChoiceField(
        choices=WalletTransaction.KIND_CHOICES,
        required=False,
        error_messages={"invalid_choice": "流水类型不正确"},
    )
    date_from = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        error_messages={"invalid": "date_from 格式必须是 YYYY-MM-DD"},
    )
    date_to = serializers.DateField(
        input_formats=["%Y-%m-%d"],
        required=False,
        error_messages={"invalid": "date_to 格式必须是 YYYY-MM-DD"},
    )

    def validate(self, attrs):
        """起 > 止 ⇒ 说真话的业务错（⛔ 不静默交换、也不回空表）。"""
        date_from = attrs.get("date_from")
        date_to = attrs.get("date_to")
        if date_from is not None and date_to is not None and date_from > date_to:
            raise serializers.ValidationError(
                {"date_from": "date_from 不能晚于 date_to"}
            )
        return attrs


class WalletTransactionPagination(EnvelopePagination):
    """流水明细分页：在信封的 `data` 里多带**全部**类型选项（前端下拉的唯一出处）。"""

    def get_paginated_response(self, data):
        payload = paginated(
            data,
            total=self.page.paginator.count,
            page=self.page.number,
            page_size=self.get_page_size(self.request),
        )
        # ⭐ 供前端下拉：`{value,label}` 直接取自 `KIND_CHOICES`（⛔ 前端不另抄 13 种中文）。
        payload["kinds"] = [
            {"value": value, "label": label}
            for value, label in WalletTransaction.KIND_CHOICES
        ]
        return api_response(payload)


def _transaction_row(tx: WalletTransaction) -> dict:
    """流水明细的一行（金额一律两位小数字符串）。 ⚠️ `kind_label` 走 `get_kind_display()`（由 `KIND_CHOICES` 生成）——本文件**不另抄词表**。
    ⚠️ `wallet` / `wallet__user` / `wallet__user__staff_profile` 由 `get_queryset` 的 `select_related` 预取（⛔ 逐行查库，见 `WalletTransactionListNPlusOneTests`）。
    """
    user = tx.wallet.user
    profile = getattr(user, "staff_profile", None)
    return {
        "id": tx.pk,
        "user_id": user.pk,
        "username": user.get_username(),
        "display_name": user_display_name(user),
        "mobile": getattr(profile, "mobile", "") or "",
        "kind": tx.kind,
        "kind_label": tx.get_kind_display(),
        "amount": _money(tx.amount),
        "balance_after": _money(tx.balance_after),
        "frozen_after": _money(tx.frozen_after),
        "note": tx.note,
        "ref_no": tx.ref_no,
        # ⚠️ D-0302：手拼时刻一律 `local_iso`（+08:00）；页面截串直显。
        "created_at": local_iso(tx.created_at),
    }


class WalletTransactionAdminViewSet(viewsets.GenericViewSet):
    """跨打手的钱包流水明细（只读；分页 + `q` / `kind` / `date_from` / `date_to`）。 ⚠️ **范围比钱包列表宽**：流水是钱的历史证据 ⇒ **包含已软删打手**的流水
    （钱包列表只列在职），差异见模块 docstring「流水明细的范围 / 日志口径」段。 ⚠️ 闸门复用 `finance.taker_wallet_adjust`（⛔ 不新增节点）。
    """

    permission_classes = [IsAuthenticated, has_node(WALLET_ADJUST_NODE)]

    pagination_class = WalletTransactionPagination
    http_method_names = ["get", "head", "options"]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有打手钱包调账的权限", code=code
        )

    def get_queryset(self):
        """本租户的钱包流水（含已软删打手的流水）+ 预取 user / staff_profile（⛔ 不许 N+1）。"""
        return WalletTransaction.objects.select_related(
            "wallet__user", "wallet__user__staff_profile"
        ).order_by("-created_at", "-id")

    def list(self, request, *args, **kwargs):
        raw = {key: value for key, value in request.query_params.items() if value != ""}
        params = _WalletTransactionQuerySerializer(data=raw)
        if not params.is_valid():
            return api_response(None, msg=_first_error(params), code=ErrorCode.VALIDATION)

        queryset = self.get_queryset()
        keyword = params.validated_data.get("q") or ""
        if keyword:
            queryset = queryset.filter(
                Q(wallet__user__username__icontains=keyword)
                | Q(wallet__user__first_name__icontains=keyword)
                | Q(wallet__user__staff_profile__mobile__icontains=keyword)
            )
        kind = params.validated_data.get("kind")
        if kind:
            queryset = queryset.filter(kind=kind)
        # 日界按 Asia/Shanghai：当天 00:00 含、次日 00:00 不含（⛔ 不按 UTC 切）。
        date_from = params.validated_data.get("date_from")
        if date_from is not None:
            queryset = queryset.filter(
                created_at__gte=datetime.combine(date_from, time.min, tzinfo=_SHANGHAI)
            )
        date_to = params.validated_data.get("date_to")
        if date_to is not None:
            # ⚠️ 用「当天 23:59:59.999999（含）」而不是「次日 00:00（不含）」：`DateField`    不限 `max_value`，`date_to=9999-12-31` 时 `date_to + timedelta(days=1)`    会抛 `OverflowError` ⇒ 500（评审抓到）。
            #    同族残差（本线⛔ 不改，只登记）：    `apps/business/xgj_receipt_stats.py` 的 `window_bounds` 与    `apps/audit/api/request_metric_views.py` 的同名写法仍是「次日 00:00 不含」，
            #    日期上界同样没有 `max_value` 保护。
            queryset = queryset.filter(
                created_at__lte=datetime.combine(date_to, time.max, tzinfo=_SHANGHAI)
            )

        page = self.paginate_queryset(queryset)
        return self.get_paginated_response([_transaction_row(tx) for tx in page])


class DepositRuleListView(APIView):
    """押金金额列表（每款未删除游戏一行，金额是**当前生效值**）。"""

    permission_classes = [IsAuthenticated, has_node(WALLET_ADJUST_NODE)]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有打手钱包调账的权限", code=code
        )

    def get(self, request):
        rows = [
            {
                "game_id": row["game_id"],
                "game_name": row["game_name"],
                "amount": _money(row["amount"]),
                "source": row["source"],
                "rule_id": row["rule_id"],
                # ⚠️ D-0302：`deposit_rule_rows()` 给的是裸 datetime，
                #    手拼出口在此换 `local_iso`（+08:00）——规则源（`apps/accounts/wallet.py`）
                #    是领域层，时刻的显示形状只在视图行装配处换。
                "updated_at": local_iso(row["updated_at"]),
            }
            for row in deposit_rule_rows()
        ]
        return api_response(
            {
                "items": rows,
                # 两档兜底的取值也回出去：页面要能解释「为什么这款是 50 / 300」，
                # 而且这两个数字**只有一个出处**（`wallet.py` 的常量），⛔ 不在前端镜像。
                "default_amount": _money(DEFAULT_DEPOSIT_AMOUNT),
                "delta_amount": _money(DELTA_DEPOSIT_AMOUNT),
            }
        )


class DepositRuleUpdateView(APIView):
    """改某款游戏的押金金额（只写 `GameDepositRule.amount` 这一个真实字段）。"""

    permission_classes = [IsAuthenticated, has_node(WALLET_ADJUST_NODE)]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有打手钱包调账的权限", code=code
        )

    def put(self, request, game_id):
        # 「不存在 / 已软删」⇒ 与提现审核同口径的业务错误（`get_object_or_404` 经统一异常
        # 处理器落 HTTP 200 + 业务码 1002），⛔ 不给跨租户的 403 差异。
        game = get_object_or_404(
            Game, pk=game_id, deleted_at__isnull=True
        )
        serializer = _DepositRuleSerializer(
            data=request.data if isinstance(request.data, dict) else {}
        )
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)
        # 改之前先量一次**生效值**：审计要写「打手看到的门槛从多少变成多少」，
        # 而不是「规则行的旧值」（新建规则时旧规则行根本不存在）。
        before = deposit_amount_for_game(game)
        try:
            rule, created = set_deposit_rule(
                game, serializer.validated_data["amount"]
            )
        except WalletError as exc:
            return api_response(None, msg=str(exc), code=exc.code)
        self._record_rule_update(game, before=before, after=rule.amount)
        return api_response(
            {
                "game_id": game.pk,
                "game_name": game.name,
                "amount": _money(rule.amount),
                "source": "rule",
                "rule_id": rule.pk,
                # ⚠️ D-0302：手拼时刻一律 `local_iso`（+08:00）。
                "updated_at": local_iso(rule.updated_at),
                # `created` 只是本轮是否新建了规则行（前端提示用，不参与判据）。
                "created": created,
            }
        )

    def _record_rule_update(self, game, *, before, after) -> None:
        """写一条操作日志（⛔ 改押金会影响「谁能接单」，必须留痕）。

        ⚠️ 本助手名已登记进 `tests/test_operation_log_coverage.py` 的
        `_RECORD_FORWARDERS`；⛔ 改名要同批改那里。
        """
        record(
            log_type=ACTION_LOG_TYPES[DEPOSIT_RULE_ACTION],
            action=DEPOSIT_RULE_ACTION,
            actor=self.request.user.get_username(),
            actor_id=self.request.user.pk,
            target_kind="game_deposit_rule",
            target_id=str(game.pk),
            target_name=game.name,
            from_value=_money(before),
            to_value=_money(after),
        )


class TakerDepositQuotaView(APIView):
    """某个打手的**个人押金额度**：读（GET）与保存（PUT），计划 §5.1。 ## 口径（⛔ 不在这里重写取值顺序） - 开关关 ⇒ 接单按系统全局的按游戏押金（`GameDepositRule`）； - 开关开 ⇒ 按**这个人**的按游戏额度（`TakerDepositQuota`）；**没填 = 0 元**（该游戏免押）。 - 真正「该冻多少」的接单补冻入口是 `apps/accounts/wallet.py::deposit_amount_for_user`；读面另有批量版（`deposit_amounts_for_user`，App 钱包用）与客服列表的同源分档；本视图只负责读写配置，**不**判断接单。 ## 三条实现边界 1. **只认本租户**：`User` / `Game` / `TakerDepositQuota` 都在租户 schema 里，查询只从当前
    schema 取，⛔ **不接受任何租户标识参数**（`AGENTS.md` §4）。别的租户的 user_id ⇒ 1002。 2. **只接受这个打手当前有权限且未软删的游戏**（与 App 钱包同一支取面）：其它 id ⇒ 1000。只 upsert 传了的游戏，⛔ **不删除**没传的行（避免误清历史配置）。 ⭐ `amounts` **整个缺席 = 只改开关**：一个 `TakerDepositQuota` 行都不动（人员管理页的行内开关只发 `{"enabled": …}`；⛔ 不能把「没传金额」读成「金额清 0」）。 3. **任何校验失败 ⇒ 0 写入**：全部校验在 `transaction.atomic()` **之前**做完；审计只记真的变化（开关一条、每款真的变了的游戏一条），幂等保存不刷屏。 ⚠️ 审计写入口就在本类的 `put` 方法里（`record(...)` 直接写在方法体内， `tests/test_operation_log_coverage.py` 按 AST 抓得到）；⛔ 不要挪进助手却漏登记。
    ⚠️ 权限节点**复用** `finance.taker_wallet_adjust`（与打手钱包 / 全局押金规则同一个权限面）， ⛔ 不新增 RBAC 节点（计划 §5.2）。
    """

    permission_classes = [IsAuthenticated, has_node(WALLET_ADJUST_NODE)]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(
            request, message="你没有打手钱包调账的权限", code=code
        )

    def _quota_target(self, user_id):
        """取「本租户的账号 + 未软删的档案」：`(user, profile, error_response)`。 ⚠️ **软删的账号也当「不存在」**（同样回业务码 1002）：`StaffProfile.deleted_at` 非空 =
        已回收，不能再给这个人配额度（回收站里的账号接不了单，给他配额度只会让恢复后拿到一份没人预期的配置）。三种失败**各给一句自己的话**，排障时不必再猜是哪一种。
        """
        user = get_user_model().objects.filter(pk=user_id).first()
        if user is None:
            return (
                None,
                None,
                api_response(None, msg="账号不存在", code=ErrorCode.BUSINESS),
            )
        profile = StaffProfile.objects.filter(user=user).first()
        if profile is None:
            return (
                user,
                None,
                api_response(
                    None, msg="该账号没有打手档案", code=ErrorCode.BUSINESS
                ),
            )
        if profile.deleted_at is not None:
            return (
                user,
                profile,
                api_response(
                    None,
                    msg="该账号已删除，不能配置个人押金额度",
                    code=ErrorCode.BUSINESS,
                ),
            )
        return user, profile, None

    def get(self, request, user_id):
        user, profile, error = self._quota_target(user_id)
        if error is not None:
            return error
        return api_response(_deposit_quota_payload(user, profile))

    def put(self, request, user_id):
        user, profile, error = self._quota_target(user_id)
        if error is not None:
            return error

        serializer = _DepositQuotaSerializer(
            data=request.data if isinstance(request.data, dict) else {}
        )
        if not serializer.is_valid():
            return api_response(None, msg=_first_error(serializer), code=ErrorCode.VALIDATION)
        enabled = serializer.validated_data["enabled"]
        # ⭐ `amounts` 缺席 ⇒ 只改开关（追加：人员管理页的行内开关就走这一档）。
        #    ⛔ 不能把「没传」当成「传了空对象再按 0 覆盖」：那会把这个人每款游戏已配的
        #    个人额度静默清成 0（钱相关，缺省必须是「不改」，见 AGENTS §5-E / E-0027 同型）。
        amounts_supplied = "amounts" in serializer.validated_data

        # ⚠️ 取值面：这个打手**当前**有权限且未软删的游戏（与 App 钱包同一支）。
        allowed_games = {
            game.pk: game
            for game in profile.games.filter(deleted_at__isnull=True)
        }
        parsed = {}
        for raw_key, raw_value in (serializer.validated_data.get("amounts") or {}).items():
            try:
                game_id = int(str(raw_key).strip())
            except (TypeError, ValueError):
                return api_response(
                    None,
                    msg=f"游戏 id 不正确：{raw_key}",
                    code=ErrorCode.VALIDATION,
                )
            game = allowed_games.get(game_id)
            if game is None:
                return api_response(
                    None,
                    msg=f"这款游戏不在该打手的游戏权限里：{raw_key}",
                    code=ErrorCode.VALIDATION,
                )
            try:
                parsed[game_id] = _parse_quota_amount(raw_value)
            except serializers.ValidationError as exc:
                detail = exc.detail[0] if exc.detail else "金额格式不正确"
                return api_response(
                    None, msg=f"{game.name}：{detail}", code=ErrorCode.VALIDATION
                )

        # ⭐ 全部校验通过 ⇒ 才进事务写库（任何一条校验失败都 0 写入）。
        quota_changes = []
        with transaction.atomic():
            # 锁档案行：同一个人的并发保存串行化（开关与额度一起来，读旧值 → 写新值）。
            profile = StaffProfile.objects.select_for_update().get(pk=profile.pk)
            old_enabled = bool(profile.deposit_quota_enabled)
            if old_enabled != bool(enabled):
                profile.deposit_quota_enabled = enabled
                profile.save(update_fields=["deposit_quota_enabled", "updated_at"])
            # ⭐ 只改开关的那一档（`amounts` 缺席）：**整段额度写入跳过**——一行都不读、不写、
            #    不删；`parsed` 必然为空，这里再加 `amounts_supplied` 是让「不改额度」的意图
            #    在代码上可读（判据落在用例：只切开关后库里每游戏金额逐值不变）。
            if amounts_supplied and parsed:
                existing = {
                    row.game_id: row
                    for row in TakerDepositQuota.objects.select_for_update().filter(
                        user=user, game_id__in=parsed
                    )
                }
                for game_id in sorted(parsed):
                    amount = parsed[game_id]
                    row = existing.get(game_id)
                    # 旧值 = 个人行金额；没有行 ⇒ 0（这正是开关开时的生效值）。
                    before = row.amount if row is not None else PERSONAL_DEPOSIT_AMOUNT
                    if row is None:
                        # ⚠️ 「没有行」与「显式 0 行」在**生效值**上等价（都是 0），但落库形态不同：    前端每次保存都会提交全部展示游戏、空输入框归一成 "0" ⇒ 若这里给    「没行 + 0」也建行，第一次保存后所有游戏都会变成 `0.00`，把计划 §7.2
                        #    要求的「空白 + placeholder 0」呈现永久换掉，而且这次落库没有审计    （0 → 0 不是变化）。⇒ 没行且金额 0 时**跳过**，保持「无行 = 0」。    只有「原本有行、改设 0」与「原本没行、改设正数」才落库。
                        if amount == PERSONAL_DEPOSIT_AMOUNT:
                            continue
                        TakerDepositQuota.objects.create(
                            user=user, game=allowed_games[game_id], amount=amount
                        )
                    elif row.amount != amount:
                        # 已有行（加锁取到的）就地改：比 `update_or_create` 少一次 SELECT。
                        row.amount = amount
                        row.save(update_fields=["amount", "updated_at"])
                    if before != amount:
                        quota_changes.append((allowed_games[game_id], before, amount))

            # ⭐ 审计（计划 §6）：**只有真的变化才写**；actor 是当前登录人（人为操作，
            #    ⛔ 不传 `system=True`）。开关与金额各一条码，事后能分开筛。
            if old_enabled != bool(enabled):
                record(
                    log_type=ACTION_LOG_TYPES[DEPOSIT_QUOTA_SWITCH_ACTION],
                    action=DEPOSIT_QUOTA_SWITCH_ACTION,
                    actor=request.user.get_username(),
                    actor_id=request.user.pk,
                    target_kind="taker_wallet",
                    target_id=str(user.pk),
                    target_name=user_display_name(user),
                    from_value=_quota_switch_label(old_enabled),
                    to_value=_quota_switch_label(enabled),
                )
            for game, before, after in quota_changes:
                record(
                    log_type=ACTION_LOG_TYPES[DEPOSIT_QUOTA_AMOUNT_ACTION],
                    action=DEPOSIT_QUOTA_AMOUNT_ACTION,
                    actor=request.user.get_username(),
                    actor_id=request.user.pk,
                    target_kind="taker_wallet",
                    target_id=str(user.pk),
                    target_name=user_display_name(user),
                    game_name=game.name,
                    from_value=_money(before),
                    to_value=_money(after),
                )

        return api_response(_deposit_quota_payload(user, profile))
