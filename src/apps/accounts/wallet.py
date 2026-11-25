"""打手钱包业务（打手 App 线）：接单补冻、解冻 7 天冷静期（懒结算无 cron、接单作废申请）、提现申请 / 驳回 / 审核、支付宝代付发起 / 查单刷新 / 人工登记——各动作只有一处实现（`_refund_withdraw` 是提现退回的唯一实现、只退一次）。⭐ 解冻冷静期「申请后第七天才到账」、钱仍冻着（挡「接单 → 秒解冻 → 跑路」）；⭐ 提现状态机三层硬闸（只 accepted 可动作 / `confirm_amount` 等值 / `paid` 不可逆，计划 §4.2）；
⭐ 代付两层判据（`code == 0` 只代表发起成功，结果看 `data.state`：2 → paid、1 → paying 必须查单、3/4 → failed + 退款——把它当打款成功是 E-0087 同族）；押金是**账号级**冻结（补冻只补差额）；个人押金额度入口 `deposit_amount_for_user`（额度只覆盖金额、不开押金闸）；⚠️ `ensure_deposit_for_claim` 自己不开事务（边界在端点，`select_for_update` 需要事务）；**网络调用一律留在数据库事务之外**（短事务锁 → 调上游 → 短事务落结果）。
"""
import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.accounts.jeepay import (
    JeepayError,
    create_transfer,
    ensure_configured,
    query_transfer,
)
from apps.accounts.models import (
    RechargeOrder,
    StaffProfile,
    TakerDepositQuota,
    TakerWallet,
    WalletTransaction,
    WithdrawOrder,
)
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.claim import _ACTIVE_STATUSES
from apps.business.models import (
    EmpType,
    Game,
    GameDepositRule,
    JidanpuUpstreamHallOrder,
    Order,
    PeerHallMirror,
)
from apps.core.error_codes import ErrorCode

logger = logging.getLogger(__name__)

#: 没配 `GameDepositRule` 的游戏用这个金额（业务方：除三角洲外都 50）。
DEFAULT_DEPOSIT_AMOUNT = Decimal("50")

#: 「三角洲」的押金额度（业务方拍定：300）。⚠️ 这是**读层兜底**、不只靠数据迁移
#: （`provisioning` 先迁移、后 seed 建游戏 ⇒ 新租户跑 0077 时 `Game` 表还空、迁移建不出规则行）：
#: 兜底让「名字里带三角洲」在任何租户都读 300；建了规则行则以规则行为准（可调）。
DELTA_DEPOSIT_AMOUNT = Decimal("300")

#: 认作「三角洲」的名字关键字。⚠️ 业务方没有给机器标识（没有 code/enum 列），只能按名字。
DELTA_NAME_KEYWORD = "三角洲"

#: 提现最小金额（元）= 1 分（金额精度的下限，不是业务门槛）。⭐ 业务方拍板**不设提现门槛**、
#: 余额可全额提走（起因：小程序审核以「设置提现门槛，用户无法成功即时提现」驳回）。
#: ⚠️ 旧值 `1` 是实现定的、不是业务要求——⛔ 别为了省手续费抬回去。
MIN_WITHDRAW_AMOUNT = Decimal("0.01")

#: 解冻冷静期（天）。业务方拍板的**选项 A**：「申请后第七天才可回余额提现」，
#: 7 天从**申请那一刻**算。⛔ 不做成配置：业务方给的是固定口径（与提现代付那个可配置的
#: 冷静期不同，别把两者混成同一个开关）。
RELEASE_HOLD_DAYS = 7

#: 冷静期的 `timedelta` 形态（与 `RELEASE_HOLD_DAYS` 同源，⛔ 不另写一份 7 天）。
RELEASE_HOLD = timedelta(days=RELEASE_HOLD_DAYS)

#: 金额的统一精度（与列定义一致）。入口一律量化到分，⛔ 不接受第三位小数。
MONEY_QUANT = Decimal("0.01")


class WalletError(Exception):
    """钱包业务的预期失败。`code` 直接给 API 层当业务码用（与 `ClaimError` 同形）。"""

    def __init__(self, message, code=ErrorCode.BUSINESS):
        super().__init__(message)
        self.code = code


# ── 读 ────────────────────────────────────────────────────────────
#: 押金金额的**来源**（客服押金规则页的 `source` 列，机器值）。⚠️ 这是**派生值**、没有对应的模型列
#: （⛔ 不自创字段）——取值顺序的唯一出处是 `_effective_deposit()`：规则行 > 三角洲名字兜底 > 默认 50。
SOURCE_RULE = "rule"
SOURCE_DEFAULT_DELTA = "default_delta"
SOURCE_DEFAULT = "default"


#: 「调用方没传 `rule`」的哨兵。⛔ **不能用 `None`**：`None` 也是合法入参
#: （调用方预取后明确知道「这款游戏没有规则行」），两者混用会让列表页退化成 N+1。
_NOT_GIVEN = object()


def _effective_deposit(game, rule=_NOT_GIVEN) -> tuple[Decimal, str]:
    """该游戏**当前生效**的押金与它的来源。⛔ 不要在别处再写一份取值顺序。
    `rule` 可由调用方预取传入（列表页用它避免逐行查库）；不传才自己查一次；传 `None` = 调用方已确认没有规则行，⛔ 不再查库。
    """
    if rule is _NOT_GIVEN:
        rule = GameDepositRule.objects.filter(game=game).first()
    if rule is not None:
        return rule.amount, SOURCE_RULE
    if DELTA_NAME_KEYWORD in (game.name or ""):
        return DELTA_DEPOSIT_AMOUNT, SOURCE_DEFAULT_DELTA
    return DEFAULT_DEPOSIT_AMOUNT, SOURCE_DEFAULT


def deposit_amount_for_game(game) -> Decimal:
    """该游戏要求的押金。缺规则行 ⇒ `DEFAULT_DEPOSIT_AMOUNT`（不是 0、不是拒绝接单）。"""
    return _effective_deposit(game)[0]


# ── 个人押金额度（业务方拍板，计划 §2.1 / §4.1）────────────────
#: 开关开、但**没有**该游戏的 `TakerDepositQuota` 行时的额度 = **0 元**（业务方第 ③ 条：没填额度 = 免押）；
#: ⚠️ 只在开关开的分支里用；开关关的缺规则行兜底仍是 `DEFAULT_DEPOSIT_AMOUNT`（50）⛔ 别把两个「缺省」混成一个（语义相反：一个是免押、一个是 50）。
PERSONAL_DEPOSIT_AMOUNT = Decimal("0")


def deposit_quota_enabled_for(user) -> bool:
    """这个打手是否启用**个人**押金额度（`StaffProfile.deposit_quota_enabled`）。
    没有档案 / 没有用户 ⇒ `False`（走全局，存量行为）；返回真正的 `bool`（读层归一化，调用方按真值判断，`AGENTS.md` §5-E 的例外口径）；⛔ 不接受租户参数（`StaffProfile` 在租户 schema，边界由连接决定）。
    """
    user_id = getattr(user, "pk", user)
    if user_id is None:
        return False
    enabled = (
        StaffProfile.objects.filter(user_id=user_id)
        .values_list("deposit_quota_enabled", flat=True)
        .first()
    )
    return bool(enabled)


def deposit_amount_for_user(user, game) -> Decimal:
    """**某打手 × 某游戏**该冻多少押金——**接单补冻**的唯一入口（计划 §2.1）。取值顺序（⛔ 不要在视图 / 前端 / App 里再写一份）：开关开 ⇒ 有 `TakerDepositQuota` 行取行值、没行取 `PERSONAL_DEPOSIT_AMOUNT`（0 元 = 免押）；开关关（或没档案）⇒ 全局 `deposit_amount_for_game`（规则行 > 三角洲兜底 > 50）。
    读面另有批量版 `deposit_amounts_for_user`；客服钱包列表的「够不够」是**同源分档**（共用同一套，⛔ 不是第二套顺序）；⚠️ 个人额度**只覆盖金额**、不打开押金闸（`DEPOSIT_ENFORCED` / `DEPOSIT_ENFORCED_APP`）。
    """
    if not deposit_quota_enabled_for(user):
        return deposit_amount_for_game(game)
    amount = (
        TakerDepositQuota.objects.filter(user_id=getattr(user, "pk", user), game=game)
        .values_list("amount", flat=True)
        .first()
    )
    if amount is None:
        return PERSONAL_DEPOSIT_AMOUNT
    return amount


def deposit_amounts_for_user(user, games) -> dict[int, Decimal]:
    """批量版 `deposit_amount_for_user`：`{game_id: Decimal}`，给读面避免 N+1（计划 §4.2）。开关开 ⇒ **一次**取这个人的全部额度行、缺行 ⇒ 0；开关关 ⇒ **一次**取全局规则行再走 `_effective_deposit`（与逐款调用同值）；空入参 ⇒ `{}` 不查库。
    ⚠️ 取值顺序与 `deposit_amount_for_user` **逐字同源**，⛔ 别在这里另写一份兜底。
    """
    games = list(games)
    if not games:
        return {}
    game_ids = [game.pk for game in games]
    if not deposit_quota_enabled_for(user):
        rules = {
            rule.game_id: rule
            for rule in GameDepositRule.objects.filter(game_id__in=game_ids)
        }
        return {
            game.pk: _effective_deposit(game, rules.get(game.pk))[0] for game in games
        }
    amounts = dict(
        TakerDepositQuota.objects.filter(
            user_id=getattr(user, "pk", user), game_id__in=game_ids
        ).values_list("game_id", "amount")
    )
    return {
        game.pk: amounts.get(game.pk, PERSONAL_DEPOSIT_AMOUNT) for game in games
    }


def min_deposit_amount() -> Decimal:
    """本租户**最低**一档押金（「押金够不够接单」的判据 = `frozen >= 它`）。
    为什么取**最低**：客服页要回答「这个人能不能接下**至少一款**游戏的单」（押金是账号级冻结、各游戏要求不同：默认 50 / 三角洲 300）；没有任何游戏时回落 `DEFAULT_DEPOSIT_AMOUNT`（与缺规则行同一个兜底）。
    """
    games = list(Game.objects.filter(deleted_at__isnull=True).only("pk", "name"))
    if not games:
        return DEFAULT_DEPOSIT_AMOUNT
    rules = {rule.game_id: rule for rule in GameDepositRule.objects.filter(game__in=games)}
    return min(
        _effective_deposit(game, rules.get(game.pk))[0] for game in games
    )


def deposit_rule_rows() -> list[dict]:
    """押金规则页的行（每款未删除游戏一行，金额是**当前生效值**、`source` 说明它从哪来）。
    ⚠️ 只有「有没有规则行」是可写的；`default` / `default_delta` 两档是**读层兜底**，后台改一次金额就会落成 `rule` 行（这正是客服要的动作）。
    """
    games = list(
        Game.objects.filter(deleted_at__isnull=True)
        .only("pk", "name", "sort_order")
        .order_by("-sort_order", "pk")
    )
    rules = {rule.game_id: rule for rule in GameDepositRule.objects.filter(game__in=games)}
    rows = []
    for game in games:
        rule = rules.get(game.pk)
        amount, source = _effective_deposit(game, rule)
        rows.append(
            {
                "game_id": game.pk,
                "game_name": game.name,
                "amount": amount,
                "source": source,
                "rule_id": rule.pk if rule is not None else None,
                "updated_at": rule.updated_at if rule is not None else None,
            }
        )
    return rows


def set_deposit_rule(game, amount) -> tuple[GameDepositRule, bool]:
    """写入某游戏的押金规则（`amount` 允许 0：0 = 接单不需要押金，D-0015 的合法边界）；返回 `(规则行, 是否新建)`。
    ⚠️ 审计要的「改之前的生效值」由调用方在同一次请求里先读 `deposit_amount_for_game(game)`（那个值可能来自兜底——规则行根本不存在，⛔ 不能拿「旧规则行的 amount」冒充）；⚠️ `amount < 0` 一律拒（负押金会让「已冻 >= 要求」恒真，等于关掉押金闸）；⚠️ 本函数**不自创上限**（业务方只给了「默认 50 / 三角洲 300」两条口径），要加业务上限请先给口径。
    """
    amount = _quantize(amount)
    if amount < 0:
        raise WalletError("押金金额不能为负数", ErrorCode.VALIDATION)
    rule, created = GameDepositRule.objects.update_or_create(
        game=game, defaults={"amount": amount}
    )
    return rule, created


def get_wallet(user) -> TakerWallet:
    wallet, _ = TakerWallet.objects.get_or_create(user=user)
    return wallet


def _record(
    wallet: TakerWallet,
    kind: str,
    amount: Decimal,
    *,
    ref_no: str = "",
    note: str = "",
    operator=None,
    client_ref: str = "",
) -> WalletTransaction:
    return WalletTransaction.objects.create(
        wallet=wallet,
        kind=kind,
        amount=amount,
        balance_after=wallet.balance,
        frozen_after=wallet.frozen,
        ref_no=ref_no[:64],
        client_ref=client_ref[:64],
        note=note[:200],
        operator=operator,
    )


# ── 解冻冷静期（业务方选项 A）──────────────────────────
def release_available_at(wallet: TakerWallet):
    """申请到期时刻（带时区 datetime）；没申请 ⇒ `None`。⚠️ **唯一**的天数出处是 `RELEASE_HOLD`，⛔ 别在视图 / 模板里再写一次 7 天。
    """
    if wallet.release_requested_at is None:
        return None
    return wallet.release_requested_at + RELEASE_HOLD


def _settle_release_locked(wallet: TakerWallet, *, now=None) -> bool:
    """**调用方必须已持钱包行锁**：到期则 `frozen → balance`，返回「本次真的结算了」。幂等的判据是**列**（`release_requested_at` 非空且已到期）：第一次结算清空它并写一条 `release` 流水，第二次进来看到 `None` ⇒ 直接 `False`、一分不动；金额取「申请那一刻」（`release_requested_amount`）：冷静期内客服调大的部分不是这次申请的钱 ⇒ 只退 `min(申请额, 当下 frozen)`。
    ⚠️ 到期时**再复核一次有没有进行中的单**：有单则**只清申请、不结算**（钱继续冻着），同时写 `wallet.release_request_voided_by_activity` 留痕（机器来源 `system=True`，让「申请为什么没了」在操作日志里答得出来）；`frozen == 0`（或申请额为 0）仍清申请（否则一直挂着 `release_pending=true`）但**不写** 0 元流水。
    """
    requested = wallet.release_requested_at
    if requested is None:
        return False
    moment = now if now is not None else timezone.now()
    if moment < requested + RELEASE_HOLD:
        return False
    if _has_active_work(wallet.user_id):
        # 有单在跑 ⇒ 这次解冻作废（⛔ 不退钱；典型来源：申请与接单并发、或指派路径漏了取消）；静默清空
        # 要留痕：这条 `record(...)` 跑在调用方的钱包行锁 + 事务里（`settle_release_if_due` /
        # `cancel_release_on_claim`）⇒ 与清空同生共死，留痕写不进去清空也一起回滚；⚠️ 机器来源（到期复核在读取钱包时顺带跑）：`actor_id=None` 必须显式 `system=True`，否则 `record()` 的 fail-closed 判据会抛 `ValueError`；金额口径与正常结算同一套 `min(申请额, 当下 frozen)`，申请额缺失（旧行）时按「申请的就是全部冻结」处理。
        requested_amount = wallet.release_requested_amount
        voided_amount = wallet.frozen
        if requested_amount is not None and requested_amount < voided_amount:
            voided_amount = requested_amount
        record(
            log_type=ACTION_LOG_TYPES["wallet.release_request_voided_by_activity"],
            action="wallet.release_request_voided_by_activity",
            actor="system",
            actor_id=None,
            system=True,
            target_kind="user",
            target_id=wallet.user_id,
            target_name=getattr(wallet.user, "username", "") or "",
            to_value=f"{voided_amount:.2f}",
            reason=timezone.localtime(requested).strftime("%Y-%m-%d %H:%M"),
        )
        wallet.release_requested_at = None
        wallet.release_requested_amount = None
        wallet.save(
            update_fields=[
                "release_requested_at",
                "release_requested_amount",
                "updated_at",
            ]
        )
        return False
    requested_amount = wallet.release_requested_amount
    amount = wallet.frozen
    if requested_amount is not None and requested_amount < amount:
        amount = requested_amount
    wallet.release_requested_at = None
    wallet.release_requested_amount = None
    if amount > 0:
        wallet.frozen -= amount
        wallet.balance += amount
    wallet.save(
        update_fields=[
            "balance",
            "frozen",
            "release_requested_at",
            "release_requested_amount",
            "updated_at",
        ]
    )
    if amount > 0:
        _record(
            wallet,
            WalletTransaction.KIND_RELEASE,
            amount,
            note=f"申请解冻满 {RELEASE_HOLD_DAYS} 天，退回余额",
        )
    return True


def settle_release_if_due(user) -> TakerWallet:
    """懒结算 + 读钱包的唯一入口：**先结算再读**，返回最新的一行。四个入口都先调它（`GET /api/app/wallet/`、`POST /api/app/wallet/release/`、提现创建、`ensure_deposit_for_claim`）——⛔ 没有 cron，到期只能靠这些读 / 写口把账结掉。
    `select_for_update` 串行：并发同时到期时，第二个在锁上等到后看到 `release_requested_at` 已被清空 ⇒ 不会结算两次。
    """
    get_wallet(user)
    with transaction.atomic():
        wallet = TakerWallet.objects.select_for_update().get(user=user)
        _settle_release_locked(wallet)
        return wallet


# ── 客服调账（后台加减余额 / 押金冻结）────────────────────
#: 客服调账的**单笔上限**配置名（金额元）。⚠️ 判据一律 `is not None`（E-0021）：
#: `0` 是**合法上限**（= 一分钱也不许手工调），⛔ 不能被真值判断当成「没配置」。
WALLET_ADJUST_MAX_AMOUNT_SETTING = "WALLET_ADJUST_MAX_AMOUNT"

#: 调账的两个项目（机器值，API 入参 / `WalletTransaction.kind` 映射都从这里取）。
ADJUST_TARGET_BALANCE = "balance"
ADJUST_TARGET_FROZEN = "frozen"
ADJUST_TARGETS = (ADJUST_TARGET_BALANCE, ADJUST_TARGET_FROZEN)

#: 调账的两个方向（机器值）。
ADJUST_INCREASE = "increase"
ADJUST_DECREASE = "decrease"
ADJUST_DIRECTIONS = (ADJUST_INCREASE, ADJUST_DECREASE)

#: (项目, 方向) → 流水 `kind`。四个组合各一条，⛔ 不共用 `recharge` / `freeze`
#: （理由见 `WalletTransaction` 的 kind 注释）。
_ADJUST_KINDS = {
    (ADJUST_TARGET_BALANCE, ADJUST_INCREASE): WalletTransaction.KIND_ADJUST_ADD,
    (ADJUST_TARGET_BALANCE, ADJUST_DECREASE): WalletTransaction.KIND_ADJUST_SUB,
    (ADJUST_TARGET_FROZEN, ADJUST_INCREASE): WalletTransaction.KIND_FROZEN_ADD,
    (ADJUST_TARGET_FROZEN, ADJUST_DECREASE): WalletTransaction.KIND_FROZEN_SUB,
}


def adjust_max_amount():
    """单笔调账上限（元，`Decimal`）；未配置返回 `None`（= 不设上限）。
    ⚠️ `is not None` 而不是真值：`WALLET_ADJUST_MAX_AMOUNT=0` 必须被理解为「上限 0 元」（任何一笔都超限），而不是「没配置」（同 `transfer_max_amount`）。
    """
    raw = getattr(settings, WALLET_ADJUST_MAX_AMOUNT_SETTING, None)
    if raw is None:
        return None
    return Decimal(str(raw))


def adjust_wallet(
    user,
    *,
    target: str,
    direction: str,
    amount,
    reason: str,
    client_ref_no: str,
    operator=None,
) -> tuple[TakerWallet, WalletTransaction, bool]:
    """客服手工调账：加 / 减**可用余额**或**押金冻结额**。返回 `(钱包, 流水, 是否真的记了账)`。六条硬闸（每条都有对应用例）：原因必填（进 `note` 与操作日志 `reason`）；金额必须 > 0 且最多两位小数（`amount` 是**变动量**、方向由 `direction` 表达，⛔ 不靠符号同时表达方向）；单笔上限（`WALLET_ADJUST_MAX_AMOUNT`，默认 1000，`is not None` 判据）；不许扣成负数（余额与冻结各自 >= 0）；幂等（同钱包 + 同 `client_ref_no` 只记一次，重复提交返回原流水 + `False`；换了内容 ⇒ `CONFLICT`、⛔ 不静默返回上一笔）；行锁串行（并发的不同单号不会各自读到旧余额再双双覆盖）。
    幂等落在 `WalletTransaction.client_ref` 上（流水是**钱的证据**），另有部分唯一索引 `uniq_wallettx_wallet_client_ref` 兜底——即使绕过这里的检查，同一个客户端单号也写不出第二条流水（DB 层拒 ⇒ 500 响亮而不是静默记两笔）。
    """
    reason = (reason or "").strip()
    if not reason:
        raise WalletError("请填写调账原因", ErrorCode.VALIDATION)
    if target not in ADJUST_TARGETS:
        raise WalletError("调账项目不正确", ErrorCode.VALIDATION)
    if direction not in ADJUST_DIRECTIONS:
        raise WalletError("调账方向不正确", ErrorCode.VALIDATION)
    client_ref_no = (client_ref_no or "").strip()
    if not client_ref_no:
        raise WalletError("请提供客户端单号（用于防重复提交）", ErrorCode.VALIDATION)
    amount = _quantize(amount)
    if amount <= 0:
        raise WalletError("调账金额必须大于 0", ErrorCode.VALIDATION)
    limit = adjust_max_amount()
    if limit is not None and amount > limit:
        raise WalletError(
            f"单笔调账金额不能超过 {limit} 元", ErrorCode.VALIDATION
        )

    kind = _ADJUST_KINDS[(target, direction)]
    # ⚠️ 先把钱包行建出来（`get_or_create` 自带 IntegrityError 重查），再进事务拿行锁——
    # 直接 `select_for_update().get()` 在「这个人还没有钱包」时会 500。
    get_wallet(user)
    with transaction.atomic():
        wallet = TakerWallet.objects.select_for_update().get(user=user)
        existing = (
            WalletTransaction.objects.filter(wallet=wallet, client_ref=client_ref_no)
            .order_by("pk")
            .first()
        )
        if existing is not None:
            if existing.kind != kind or existing.amount != amount:
                raise WalletError(
                    "这个客户端单号已经用于另一笔调账，请换一个单号",
                    ErrorCode.CONFLICT,
                )
            return wallet, existing, False

        delta = amount if direction == ADJUST_INCREASE else -amount
        if target == ADJUST_TARGET_BALANCE:
            after = wallet.balance + delta
            if after < 0:
                raise WalletError(
                    f"可用余额不足：当前 {wallet.balance:.2f} 元，"
                    f"不能再扣 {amount:.2f} 元",
                    ErrorCode.VALIDATION,
                )
            wallet.balance = after
        else:
            after = wallet.frozen + delta
            if after < 0:
                raise WalletError(
                    f"押金冻结额不足：当前 {wallet.frozen:.2f} 元，"
                    f"不能再减 {amount:.2f} 元",
                    ErrorCode.VALIDATION,
                )
            wallet.frozen = after
        wallet.save(update_fields=[target, "updated_at"])
        tx = _record(
            wallet,
            kind,
            amount,
            note=reason,
            operator=operator,
            client_ref=client_ref_no,
        )
        return wallet, tx, True


# ── 接单冻结 ──────────────────────────────────────────────────────
def can_cover_deposit_for_claim(user, game) -> bool:
    """只读检查，解冻结算会在余额与冻结额之间划转，二者合计不变。"""
    need = deposit_amount_for_user(user, game)
    if need <= 0:
        return True
    available = TakerWallet.objects.filter(user=user).values_list("balance", "frozen").first()
    return available is not None and sum(available) >= need


def ensure_deposit_for_claim(user, game, *, ref_no: str = "") -> TakerWallet:
    """接单前补冻结。**调用方必须已在事务里**（`select_for_update` 依赖它）。返回**最新**的钱包行；余额不足以补差额时抛 `WalletError`（码 = 抢单限制同款 `GRAB_LIMIT_EXCEEDED`，用户看到的是「你现在抢不了」这个结果）。
    """
    wallet, _ = TakerWallet.objects.select_for_update().get_or_create(user=user)
    # ⭐ 解冻冷静期：**先结算**再算「够不够」。到期未结算时 `frozen` 仍是旧值，
    # 直接拿去比会得出「够接」——而钱其实已经是余额里的了（接单补冻必须从余额重新扣）。
    _settle_release_locked(wallet)
    # ⭐ 个人押金额度：唯一入口是 `deposit_amount_for_user`（开关 / 个人行 /
    # 缺行 = 0 / 开关关回落全局，口径全在它里面，⛔ 这里不重写顺序）。
    need = deposit_amount_for_user(user, game)
    # ⚠️ 判据是「已冻 >= 该游戏要求」——⛔ 不是「已冻 > 0」：先冻了 50 的人接 300 的游戏
    # 仍然要补 250（变异正是打在这里）。
    if wallet.frozen >= need:
        return wallet

    gap = need - wallet.frozen
    if wallet.balance < gap:
        raise WalletError(
            f"押金不足：接这款游戏需要冻结 {need} 元，"
            f"你还差 {gap - wallet.balance} 元，请先充值",
            ErrorCode.GRAB_LIMIT_EXCEEDED,
        )
    wallet.balance -= gap
    wallet.frozen = need
    wallet.save(update_fields=["balance", "frozen", "updated_at"])
    _record(wallet, WalletTransaction.KIND_FREEZE, gap, ref_no=ref_no, note=f"冻结至 {need} 元")
    return wallet


# ── 解冻申请（业务方选项 A：7 天冷静期）────────────────────
def _has_active_work(user) -> str:
    """「这个人还占着活」的**唯一判据**：返回拒因文案；空串 = 可以申请解冻。
    进行中订单沿用 `claim.py::_ACTIVE_STATUSES`（⛔ 不另抄一份状态集，否则「什么算占着单」出现两套口径）；上家单的「已预占、还在打上游 / 等待转正」也算占着单——预占时已冻押金，允许解冻会解出「有单、零冻结」的窟窿（⛔ 只看 Order 不够）；团团通上家单的 claiming 同理（D-0303 的 H1），判据与上一支逐字同形（模型换一张）。
    """
    busy = Order.objects.filter(
        performances__user=user,
        performances__emp_type=EmpType.MAIN,
        status__in=_ACTIVE_STATUSES,
    ).exists()
    if busy:
        return "还有进行中的订单，全部完成后才能申请解冻"
    claiming = JidanpuUpstreamHallOrder.objects.filter(
        claiming_by=user,
        status=JidanpuUpstreamHallOrder.Status.CLAIMING,
    ).exists()
    if claiming:
        return "有正在处理的上家单，等它落定后才能申请解冻"
    peer_claiming = PeerHallMirror.objects.filter(
        claiming_by=user,
        status=PeerHallMirror.Status.CLAIMING,
    ).exists()
    if peer_claiming:
        return "有正在处理的团团通上家单，等它落定后才能申请解冻"
    return ""


def request_release(user) -> tuple[TakerWallet, bool]:
    """**申请**解冻：校验无进行中订单 → 记下申请时刻，**冻结金额一分不动**。返回 `(钱包, 本次是否真的新申请了)`；`False` = 已有一笔未到期的申请 ⇒ **幂等**返回：不写第二条流水、**不重置 7 天计时**（连点两次不能把到账日往后推）。解冻只有这一处实现（旧的同义函数 `release_deposit` 会把 `frozen` 立刻转回 `balance`，正是要挡掉的「秒解冻跑路」）。
    ⚠️ 申请后**钱仍在 `frozen` 里** ⇒ 「有没有进行中的单」申请时判一次、**到期结算时还会再复核**（`_settle_release_locked`）：这 7 天里又接了单 ⇒ 只清申请、不退钱。
    """
    get_wallet(user)
    with transaction.atomic():
        wallet = TakerWallet.objects.select_for_update().get(user=user)
        # 先把到期的那笔结掉：否则用户已经等满 7 天，再点申请会被当成「重复申请」幂等返回，
        # 而钱又没到余额——那等于把他的到账日无限往后推。
        settled = _settle_release_locked(wallet)
        if wallet.release_requested_at is not None:
            return wallet, False
        if settled:
            # 刚结算完（钱已经回余额）⇒ 这一次「申请」其实什么也不用做。
            # ⚠️ 不报「没有可解冻的押金」：钱刚刚到账，再弹一个错误会让用户以为失败了。
            return wallet, False
        if wallet.frozen <= 0:
            raise WalletError("当前没有可解冻的押金")
        blocked = _has_active_work(user)
        if blocked:
            raise WalletError(blocked)
        amount = wallet.frozen
        wallet.release_requested_at = timezone.now()
        # ⭐ 金额与时刻**同一次写**（评审口径）：到期只退这一笔，之后客服加冻的不算。
        wallet.release_requested_amount = amount
        wallet.save(
            update_fields=[
                "release_requested_at",
                "release_requested_amount",
                "updated_at",
            ]
        )
        _record(
            wallet,
            WalletTransaction.KIND_RELEASE_REQUEST,
            amount,
            note=f"申请解冻，{RELEASE_HOLD_DAYS} 天后到账",
        )
        return wallet, True


def cancel_release_on_claim(user) -> bool:
    """接单成功时调用：冷静期内的解冻申请**作废**；返回「是否真的取消了一条未到期申请」。返回 `False` 的四种情况：本来没有钱包 / 没有申请 / 抢锁后已被别的请求清掉 / **已到期**（结算或「有单只清申请」——钱的事已另有处置，不算「取消」）。
    ⚠️ 必须**无条件拿钱包行锁**：无锁前置判「没有申请」会让接单与申请并发时两边都成功（申请存活 + 单接下了，第 7 天押金照退）⇒ 任何接单都先 `select_for_update()`（与 `request_release` 串行）；没有钱包行 ⇒ 直接 `False`（⛔ 不建行：接单不该凭空造钱包）。⚠️ **到期先结算**：等满 7 天的人接单不能继续冻他的钱（结算后接单补冻会从余额重新扣，同 `ensure_deposit_for_claim` 的理由）；⚠️ 事务边界在**调用方**，本函数为「没有外层事务的直连调用」自带 `atomic()`（`select_for_update` 的硬约束，见模块 docstring）。
    """
    user_id = getattr(user, "pk", user)
    with transaction.atomic():
        wallet = (
            TakerWallet.objects.select_for_update().filter(user_id=user_id).first()
        )
        if wallet is None or wallet.release_requested_at is None:
            return False
        # 到期 ⇒ 由结算逻辑处置（可能结算、也可能「有单只清申请」）；两种都不算「取消」。
        _settle_release_locked(wallet)
        if wallet.release_requested_at is None:
            return False
        wallet.release_requested_at = None
        wallet.release_requested_amount = None
        wallet.save(
            update_fields=[
                "release_requested_at",
                "release_requested_amount",
                "updated_at",
            ]
        )
        return True


# ── 充值入账（Jeepay 回调）──────────────────────────────────────
def credit_recharge(order: RechargeOrder, *, pay_order_id: str = "") -> TakerWallet:
    """支付回调成功时入账。**幂等**：只有 `pending` 单会被处理。
    ⚠️ 幂等的判据是**状态**（`pending → paid`），不是"回调来没来过"：Jeepay 会重试（0/30/60/90/120/150 秒），同一个 `mchOrderNo` 可能收到多次。
    """
    with transaction.atomic():
        order = RechargeOrder.objects.select_for_update().get(pk=order.pk)
        wallet, _ = TakerWallet.objects.select_for_update().get_or_create(user_id=order.user_id)
        if order.state != RechargeOrder.STATE_PENDING:
            return wallet
        order.state = RechargeOrder.STATE_PAID
        order.paid_at = timezone.now()
        if pay_order_id:
            # 回填 Jeepay 的 `payOrderId`（对账要用；评审 Minor 7：这一列原先没有写入点）。
            order.jeepay_order_no = pay_order_id[:40]
        order.save(update_fields=["state", "paid_at", "jeepay_order_no"])
        wallet.balance += order.amount
        wallet.save(update_fields=["balance", "updated_at"])
        _record(
            wallet,
            WalletTransaction.KIND_RECHARGE,
            order.amount,
            ref_no=order.mch_order_no,
            note="押金充值",
        )
        return wallet


# ── 前三单快路：**整段已下线**（spec §8.2）────────────────────
# ⛔ 这一段原先是「前三单快路」的额度算法（`fast_lane_*` 系列与三个配置常量），业务方拍板**整段下线**：新提现
# 一律走「申请 → 人工审核 → 冷静期 → 代付」，⛔ 不要「顺手」恢复（产品口径已被业务方撤销，原实现见 git 历史）；⚠️ `WithdrawOrder.fast_lane` **列保留**、今天恒 `False`；`/api/app/wallet/` 仍回 `fast_lane_remaining` 但恒 `"0.00"`（老 App 不崩，见 `apps/accounts/app_wallet_views.py`）。


# ── 提现申请 ──────────────────────────────────────────────────────
def request_withdraw(user, amount: Decimal) -> WithdrawOrder:
    """申请提现：**立即扣减可用余额**（防重复提交把同一笔钱提两次），单子待人工审核。
    驳回时由 `reject_withdraw` 把钱退回余额；通过后由人工打款。
    """
    amount = _quantize(amount)
    if amount < MIN_WITHDRAW_AMOUNT:
        raise WalletError(f"提现金额不能低于 {MIN_WITHDRAW_AMOUNT} 元", ErrorCode.VALIDATION)
    profile = StaffProfile.objects.filter(user=user).first()
    if profile is None or not profile.alipay_account or not profile.alipay_name:
        raise WalletError("请先在「我的资料」里填好支付宝账号和姓名", ErrorCode.VALIDATION)

    with transaction.atomic():
        wallet, _ = TakerWallet.objects.select_for_update().get_or_create(user=user)
        # ⭐ 解冻冷静期：提现创建也是**懒结算入口**之一——到期未结算时余额里
        # 还没有那笔钱，不先结算会让「钱已经该到账了，却提不出来」看起来像 bug。
        _settle_release_locked(wallet)
        # ⭐ **先判负余额，再判够不够**：负余额 = 有退款 / 手工调整待冲账（`accounts.payout` 扣回
        # 已发的工资、允许扣成负数）⇒ 这时哪怕提 1 元也是在提「不属于他的钱」，必须给一句明确的文案，
        # ⛔ 不能笼统报「可用余额不足」（用户会以为是金额填大了）。
        if wallet.balance < 0:
            raise WalletError(
                "余额为负（有退款待冲账），不能提现", ErrorCode.VALIDATION
            )
        if wallet.balance < amount:
            raise WalletError("可用余额不足", ErrorCode.VALIDATION)
        # ⛔ **已不再有「前三单快路」**（spec §8.2）：所有提现申请一律 `pending`（等人工审核），没有自动
        # 通过、没有跳过冷静期；`fast_lane` 列仍写 `False`（列保留、历史行不动，新行不再有 `True`）。
        # ⚠️ `payout_after` 留空：它只在**审核通过**时由 `approve_withdraw` 写（`approved_at + 冷静期`），申请这一刻还不知道什么时候能付。
        order = WithdrawOrder.objects.create(
            user=user,
            amount=amount,
            alipay_account=profile.alipay_account,
            alipay_name=profile.alipay_name,
            fast_lane=False,
            state=WithdrawOrder.STATE_PENDING,
        )
        wallet.balance -= amount
        wallet.save(update_fields=["balance", "updated_at"])
        _record(
            wallet,
            WalletTransaction.KIND_WITHDRAW,
            amount,
            ref_no=f"W{order.pk}",
            # 流水是**事件记录**，不是状态：这里只写「提现申请」，状态看提现记录
            # （改口径：原文「（待审核）」会一直挂在流水上，打款成功后看起来像没同步）。
            note="提现申请",
        )
        return order


def _quantize(amount) -> Decimal:
    """把入参收敛成「分」为单位的 Decimal。`NaN` / `Inf` / 超范围一律拒绝。
    入口序列化器已经挡了一层；这里是**服务层自己的兜底**（无 HTTP 的调用点也存在），同时保证落库精度只有两位。
    """
    if amount is None:
        raise WalletError("金额不能为空", ErrorCode.VALIDATION)
    if not isinstance(amount, Decimal):
        try:
            amount = Decimal(str(amount))
        except Exception as exc:  # noqa: BLE001 - 任何解析失败都翻成业务错误
            raise WalletError("金额格式不正确", ErrorCode.VALIDATION) from exc
    if not amount.is_finite():
        raise WalletError("金额格式不正确", ErrorCode.VALIDATION)
    if amount.as_tuple().exponent < -2:
        # 多于两位小数：直接拒绝，⛔ 不静默四舍五入（钱不能悄悄变少/变多）。
        raise WalletError("金额最多两位小数", ErrorCode.VALIDATION)
    if abs(amount) >= Decimal("10000000000"):
        raise WalletError("金额超出范围", ErrorCode.VALIDATION)
    return amount.quantize(MONEY_QUANT)


# ── 提现审核：通过（含解冻冷静期）──────────────────
#: 自动代付冷静期的配置名（分钟）。⚠️ 判据一律 `is not None`（E-0021）：
#: `0` 是合法值（审核通过即到期），⛔ 不能被真值判断当成「没配置」。
WITHDRAW_PAYOUT_DELAY_SETTING = "WITHDRAW_PAYOUT_DELAY_MINUTES"

#: 冷静期兜底（分钟）= 3 天。配置缺失 / 读不出时用它（⛔ 不写死在函数体里）。
#: ⭐ 业务方改口径：正常提现「审核通过后 3 天内自动打款」（原 T+7）。
DEFAULT_PAYOUT_DELAY_MINUTES = 3 * 24 * 60

#: 超时自动审核的配置名（小时）。`pending` 超过这个时长没人处理 ⇒ 自动通过。
WITHDRAW_REVIEW_TIMEOUT_SETTING = "WITHDRAW_REVIEW_TIMEOUT_HOURS"

#: 超时自动审核的兜底（小时）。
DEFAULT_REVIEW_TIMEOUT_HOURS = 24

#: 自动审核的**存量闸**（ISO 时间字符串）。空 = 不自动审核任何单。
#: ⚠️ 这是上线安全阀：不设下界会把历史 `pending` 单一次性全放行（然后 3 天后自动打款）。
WITHDRAW_AUTO_REVIEW_SINCE_SETTING = "WITHDRAW_AUTO_REVIEW_SINCE"


def payout_delay_minutes() -> int:
    """审核通过到自动代付之间的冷静期（分钟）。⚠️ **可配置**（业务方验收时改 `.env` 即可，⛔ 代码里不写死 3 天）；负数按 0 处理（0 = 审核通过即到期）；配置不是整数 ⇒ 回落默认 3 天并记 warning——⛔ 不静默按 0（那会让冷静期消失、立刻打款，是钱上的危险方向）。
    """
    raw = getattr(settings, WITHDRAW_PAYOUT_DELAY_SETTING, DEFAULT_PAYOUT_DELAY_MINUTES)
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r 不是整数，冷静期按默认 %s 分钟处理",
            WITHDRAW_PAYOUT_DELAY_SETTING,
            raw,
            DEFAULT_PAYOUT_DELAY_MINUTES,
        )
        return DEFAULT_PAYOUT_DELAY_MINUTES
    return max(minutes, 0)


def review_timeout_hours() -> int:
    """`pending` 多久没人处理就自动通过（小时）。负数按 0 处理（= 立即通过）。
    配置缺失 / 不是整数 ⇒ 回落 24 小时并记 warning（⛔ 不静默按 0，那会立刻放行）。
    """
    raw = getattr(settings, WITHDRAW_REVIEW_TIMEOUT_SETTING, DEFAULT_REVIEW_TIMEOUT_HOURS)
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r 不是整数，超时审核按默认 %s 小时处理",
            WITHDRAW_REVIEW_TIMEOUT_SETTING,
            raw,
            DEFAULT_REVIEW_TIMEOUT_HOURS,
        )
        return DEFAULT_REVIEW_TIMEOUT_HOURS
    return max(hours, 0)


def auto_review_since():
    """自动审核的**存量下界**（带时区的 datetime）；`None` = 不自动审核。⚠️ **空配置就是不审**——上线安全阀：不设下界会把上线前的历史 `pending` 单一次性全放行（然后 3 天后自动打款）。
    解析不出 ⇒ 记 warning 并返回 `None`（安全方向：宁可不审，也不误放行存量单）。
    """
    raw = str(getattr(settings, WITHDRAW_AUTO_REVIEW_SINCE_SETTING, "") or "").strip()
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        logger.warning(
            "%s=%r 不是 ISO 时间，自动审核本轮不执行",
            WITHDRAW_AUTO_REVIEW_SINCE_SETTING,
            raw,
        )
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def approve_withdraw(order, *, operator) -> WithdrawOrder:
    """审核通过：`pending → approved`，记审核人 / 审核时刻 / **自动代付时刻**。只有 `pending` 能通过；已通过 / 已打款 / 已驳回的单再点一次一律 `CONFLICT`（计划 §7 第 5 条）。
    ⭐ 同时写 `approved_at` 与 `payout_after = approved_at + 冷静期`（冷静期来自 `WITHDRAW_PAYOUT_DELAY_MINUTES`）⇒ 管理命令 `payout_due_withdrawals` 到期才发。
    """
    with transaction.atomic():
        order = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        if order.state != WithdrawOrder.STATE_PENDING:
            raise WalletError("只有待审核的提现单能通过", ErrorCode.CONFLICT)
        approved_at = timezone.now()
        order.state = WithdrawOrder.STATE_APPROVED
        order.reviewer = operator
        order.reviewed_at = approved_at
        order.approved_at = approved_at
        order.payout_after = approved_at + timedelta(minutes=payout_delay_minutes())
        order.save(
            update_fields=[
                "state",
                "reviewer",
                "reviewed_at",
                "approved_at",
                "payout_after",
            ]
        )
        return order


def auto_approve_due_withdrawals(now=None) -> int:
    """把**超时未处理**的 `pending` 提现单自动通过，返回本次通过的条数。两个闸（缺一不可）：① **时间下界** `WITHDRAW_AUTO_REVIEW_SINCE`（空 = 不执行；⛔ 没有它上线首轮会把历史存量单一次性全放行、3 天后自动打款）；② **超时** `WITHDRAW_REVIEW_TIMEOUT_HOURS`（默认 24）。必须在 `payout_due_withdrawals` 的 `WITHDRAW_AUTO_PAYOUT` 开关判据**之前**执行（开关关掉只该停自动打款、不停自动审核）。
    调用点负责放进 `tenant_context`（直接查租户库）；`approve_withdraw` 本身不写审计（那是视图的职责）⇒ 这里**必须补一条**（机器来源显式 `system=True`），否则「系统自动通过」在操作日志里完全看不见。
    """
    since = auto_review_since()
    if since is None:
        return 0
    moment = now or timezone.now()
    cutoff = moment - timedelta(hours=review_timeout_hours())

    pending_ids = list(
        WithdrawOrder.objects.filter(
            state=WithdrawOrder.STATE_PENDING,
            created_at__lte=cutoff,
            created_at__gte=since,
        ).values_list("pk", flat=True)
    )
    approved = 0
    for pk in pending_ids:
        try:
            order = approve_withdraw(WithdrawOrder.objects.get(pk=pk), operator=None)
        except WalletError:
            # 并发下已被客服处理 / 状态已变 ⇒ 跳过，不影响同批其它单。
            logger.info("提现单 %s 自动审核跳过：状态已变", pk)
            continue
        record(
            log_type=ACTION_LOG_TYPES["finance.withdraw_approve"],
            action="finance.withdraw_approve",
            actor="system",
            actor_id=None,
            system=True,
            target_kind="withdraw_order",
            target_id=order.pk,
            target_name=getattr(order.user, "username", "") or "",
            to_value=f"{order.amount:.2f}",
            reason="超时自动通过",
        )
        approved += 1
    return approved


# ── 提现驳回（退款抽成 `_refund_withdraw`）────
def reject_withdraw(order, *, operator, remark: str = "") -> WithdrawOrder:
    """驳回提现单：**把钱退回可用余额**（申请时已扣，不退回就等于没收）。
    只有 `pending` 单能驳回；已通过 / 已打款的单要退钱得走人工转账，⛔ 不在本函数里「顺手改状态」——那会让账实不符。
    """
    if not remark.strip():
        raise WalletError("驳回必须填原因", ErrorCode.VALIDATION)
    with transaction.atomic():
        order = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        if order.state != WithdrawOrder.STATE_PENDING:
            raise WalletError("只有待审核的提现单能驳回", ErrorCode.CONFLICT)
        order.state = WithdrawOrder.STATE_REJECTED
        order.reviewer = operator
        order.reviewed_at = timezone.now()
        order.remark = remark[:200]
        order.save(update_fields=["state", "reviewer", "reviewed_at", "remark"])
        _refund_withdraw(
            order, operator=operator, note=f"提现驳回退回：{remark}"
        )
        return order


# ── 财务中断────────────────────────────────────────
def interrupt_withdraw(
    order, *, operator, reason: str, record_audit: bool = True
) -> WithdrawOrder:
    """财务中断：`approved → interrupted`，钱退回余额。只允许 `approved`（`pending` 走 `reject_withdraw`——钱与状态的效果完全等价，⛔ 不重复造动作；`paying` / `paid` 钱已在路上 / 已出去 ⇒ `CONFLICT`）；幂等：`_refund_withdraw` 按 `ref_no=W{pk}` 的流水判，重复中断只退一次。
    ⚠️ `record_audit` 默认 `True`（退款自动中断走这里、必须留痕）；**后台端点传 `False`**、由视图的 `_record_withdraw_review` 写——覆盖网按名字解析「视图方法里有没有埋点助手」，服务层写了视图不写会被判成「端点没埋点」。
    """
    reason = (reason or "").strip()
    if not reason:
        raise WalletError("中断必须填原因", ErrorCode.VALIDATION)
    with transaction.atomic():
        order = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        if order.state != WithdrawOrder.STATE_APPROVED:
            raise WalletError(
                "只有审核通过、还没打款的提现单能中断", ErrorCode.CONFLICT
            )
        order.state = WithdrawOrder.STATE_INTERRUPTED
        order.interrupted_at = timezone.now()
        order.interrupt_reason = reason[:200]
        order.save(update_fields=["state", "interrupted_at", "interrupt_reason"])
        _refund_withdraw(order, operator=operator, note=f"提现中断退回：{reason}")
        if record_audit:
            if operator is None:
                actor, actor_id, system = "system", None, True
            else:
                actor, actor_id, system = operator.username, operator.pk, False
            record(
                log_type=ACTION_LOG_TYPES["finance.withdraw_interrupt"],
                action="finance.withdraw_interrupt",
                actor=actor,
                actor_id=actor_id,
                system=system,
                target_kind="withdraw_order",
                target_id=order.pk,
                target_name=getattr(order.user, "username", "") or "",
                to_value=f"{order.amount:.2f}",
                reason=reason,
            )
        return order


def interrupt_open_withdrawals(user_id, *, reason: str, operator=None) -> int:
    """把该用户所有 `approved`（已通过、未打款）的提现单中断，返回中断条数。退款钩子用它：先把压在这些单里的钱退回余额，再让退款冲账（余额可能被扣成负）；
    `pending` 不动（还没审核，退款后负余额闸会自然挡住提现）、`paying` 不动（钱已在路上）；⚠️ 传 `user_id` 而不是 User（退款钩子手里只有 `Performance.user_id`）。
    """
    ids = list(
        WithdrawOrder.objects.filter(
            user_id=user_id, state=WithdrawOrder.STATE_APPROVED
        ).values_list("pk", flat=True)
    )
    interrupted = 0
    for pk in ids:
        try:
            interrupt_withdraw(
                WithdrawOrder.objects.get(pk=pk), operator=operator, reason=reason
            )
        except WalletError:
            # 并发下已被打款 / 已变状态 ⇒ 跳过，不影响其它单。
            continue
        interrupted += 1
    return interrupted


# ── 提现退回：唯一实现，且只退一次──────────────────────
def _refund_withdraw(order, *, operator, note: str) -> bool:
    """把提现金额退回可用余额并写一条 `WITHDRAW_REFUND` 流水。**只退一次**。⚠️ **本函数自己拿单据行锁**（调用方已 `select_for_update` 时同一事务里可重入；没锁的调用方也不会与并发退款撞车）。
    幂等的判据是**流水**（`ref_no = W{pk}` 已有一条 `WITHDRAW_REFUND` 就返回 `False`、一分不动）——⛔ **不靠迁移 / 唯一约束保证唯一**（存量数据没核过，加约束风险不值）：退款要是被重复走到（刷新与人工重试并发、或将来有人给 `failed` 加「重新发起」），只判状态会把同一笔钱退两次，而流水是**钱的证据**、判它最硬。返回 `True` = 本次真的退了钱。
    """
    with transaction.atomic():
        locked = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        ref_no = f"W{locked.pk}"
        already = WalletTransaction.objects.filter(
            wallet__user_id=locked.user_id,
            kind=WalletTransaction.KIND_WITHDRAW_REFUND,
            ref_no=ref_no,
        ).exists()
        if already:
            return False
        wallet, _ = TakerWallet.objects.select_for_update().get_or_create(
            user_id=locked.user_id
        )
        wallet.balance += locked.amount
        wallet.save(update_fields=["balance", "updated_at"])
        _record(
            wallet,
            WalletTransaction.KIND_WITHDRAW_REFUND,
            locked.amount,
            ref_no=ref_no,
            note=note[:200],
            operator=operator,
        )
    return True


# ── 支付宝代付：配置 / 状态应用─────────────────────────
#: 代付单笔上限的配置名（金额元）。⚠️ 判据一律 `is not None`（E-0021）：
#: `0` 是**合法上限**（= 一分钱也不许代付），⛔ 不能被真值判断当成「没配置」。
TRANSFER_MAX_AMOUNT_SETTING = "WITHDRAW_TRANSFER_MAX_AMOUNT"


def auto_transfer_enabled() -> bool:
    """代付总开关（默认关）。**关着时只能登记人工打款**（计划 §6）。"""
    return bool(getattr(settings, "WITHDRAW_AUTO_TRANSFER", False))


def transfer_max_amount():
    """单笔代付上限（元，`Decimal`）；未配置返回 `None`（= 不设上限）。
    ⚠️ `is not None` 而不是真值：`WITHDRAW_TRANSFER_MAX_AMOUNT=0` 必须被理解为「上限 0 元」（任何一笔都超限），而不是「没配置」。
    """
    raw = getattr(settings, TRANSFER_MAX_AMOUNT_SETTING, None)
    if raw is None:
        return None
    return Decimal(str(raw))


#: 「代付请求在途」窗口（秒）：只防**我方 HTTP 请求还没回来**那一段（上游可能已收到、只是回包慢）；
#: 超时后不再假设在途、改为**先查单**再决定能不能人工兜底。取值：Jeepay 调用超时 10 秒（`jeepay.py`），
#: 120 秒 = 12 倍余量——覆盖排队 / 重试 / 慢回包，又不让客服在一笔已经失败的代付上干等太久。
TRANSFER_INFLIGHT_SECONDS = 120


def transfer_inflight(order) -> bool:
    """这张单是否处于「代付请求在途」窗口（列表的 `transfer_inflight` 与人工打款闸共用）。判据 = `state=paying` ∧ `transfer_started_at` 非空 ∧ 距今 < `TRANSFER_INFLIGHT_SECONDS`。
    ⛔ 不能只判 `state=paying`：`paying` 也可能是「上游已回 `state=1`、正在等查单」——那时人工打款要**先查单**，而不是一律拒绝。
    """
    if order.state != WithdrawOrder.STATE_PAYING or order.transfer_started_at is None:
        return False
    age = (timezone.now() - order.transfer_started_at).total_seconds()
    return age < TRANSFER_INFLIGHT_SECONDS


# ── 先扣再打（业务方拍板，D-0336）──────────────────────
def refund_debt(balance) -> Decimal:
    """该打手「未了结的退款欠款」＝ 钱包**负余额**的绝对值；非负余额 ⇒ 0（D-0336）。负余额的生产路径只有一个：`accounts.payout.payout_adjustment` 的**冲账**（退单回冲已发的工资 / 已结账绩效，允许扣成负数——那是追回已发出去的钱）；
    客服调账有「不许扣成负」的闸、充值 / 提现退回只加不减 ⇒ **负余额 = 追而未得的退款**，这就是「未了结的退款欠款」的全部来源；`0` 不是欠款（D-0015：0 是合法边界），只有真负数才算。
    """
    balance = Decimal(balance)
    if balance >= 0:
        return Decimal("0")
    return -balance


def _record_debt_hold(order, operator, debt: Decimal) -> None:
    """「欠款 ≥ 单额 ⇒ 本轮不打」的台账人话（D-0336）。动作沿用 `finance.withdraw_transfer`（「发起代付」这一跳的结果族，失败也是这一跳的结果——与 `_record_transfer_failure` 同一条纪律）。
    人为操作必须带 `actor_id`，调用方没给操作人时按机器来源显式 `system=True`（`record()` 的 fail-closed 契约）。
    """
    actor_id = getattr(operator, "pk", None)
    record(
        log_type=ACTION_LOG_TYPES["finance.withdraw_transfer"],
        action="finance.withdraw_transfer",
        actor=getattr(operator, "username", "") or "系统",
        actor_id=actor_id,
        system=actor_id is None,
        target_kind="withdraw_order",
        target_id=str(order.pk),
        target_name=getattr(order.user, "username", "") or "",
        to_value=f"{order.amount:.2f}",
        reason=(
            f"退款欠款 {debt:.2f} 元未了结，提现 {order.amount:.2f} 元"
            "不足以先扣，本轮暂停打款"
        ),
    )


def _transfer_state_text(data) -> str:
    """上游 `data.state` 的字符串形态（可能回 `int` 或 `"2"`，统一转 `str`）。"""
    raw = data.get("state")
    return "" if raw is None else str(raw)


def _transfer_error_text(data) -> str:
    """把上游失败档的错误码 / 描述收成一行（留给 `transfer_error` 列）。⚠️ 字段名以 **Jeepay 源码 / 官方文档**为准：转账单响应里是 `errMsg` / `errCode`（在 `TransferOrderRS.java:68/71` 核过）。
    `msg` 只作**兜底**——早期夹具曾把 `errorMsg` 当字段名，那是错的（夹具固化错字段会让正确实现反向变红）。
    """
    state = _transfer_state_text(data)
    detail = data.get("errMsg") or data.get("msg") or ""
    code = data.get("errCode") or ""
    suffix = f"（errCode={code}）" if code != "" else ""
    return f"state={state}：{detail or '上游未给原因'}{suffix}"[:200]


def _apply_transfer_state(
    order: WithdrawOrder, data: dict, *, operator=None, offset: Decimal = Decimal("0")
) -> WithdrawOrder:
    """把上游 `data.state` 落到本地状态机（**调用方必须持单行锁**）。⭐ 判据（计划 §2 / §4.2，逐条可红）：`state=2` → `paid`（清空 `transfer_error`、记 `paid_at`）；`state=1/0` → `paying`（⛔ 不落 paid、不动钱包，必须查单确认）；`state=3/4` → `failed` + `_refund_withdraw`（只退一次由它兜）；其它**不采信** ⇒ 抛 `WalletError`、单子停在 `paying`（可再查单）。
    ⭐ `offset`（D-0336「先扣再打」）= 发起时抵掉的退款欠款额，**只有同步回应**才知道：`state=3/4` 退款退回的是**单据全额**、欠款已被抵掉且钱并没有少打 ⇒ 必须把欠款**记回**；查单路径拿不到发起时刻的欠款额（没有列可存，零迁移约束）⇒ 传默认 `0` = 不记回（残差登记在 D-0336：平台少收一笔「已收回的欠款」，量级 = 欠款额）。
    """
    state = _transfer_state_text(data)
    transfer_id = str(data.get("transferId") or data.get("transfer_id") or "")[:64]

    if state == "2":
        order.state = WithdrawOrder.STATE_PAID
        order.paid_at = timezone.now()
        order.transfer_id = transfer_id
        order.transfer_error = ""
        # ⭐ 拿到回应就必须清掉在途标记（否则列表会一直显示「代付请求在途」）。
        order.transfer_started_at = None
        order.save(
            update_fields=[
                "state", "paid_at", "transfer_id", "transfer_error",
                "transfer_started_at",
            ]
        )
        return order

    if state in ("0", "1"):
        # ⚠️ `state=1` = 转账中 ⇒ **必须查单**确认，⛔ 这里绝不能落 paid（计划 §7 第 1 条）。
        order.state = WithdrawOrder.STATE_PAYING
        if transfer_id:
            order.transfer_id = transfer_id
        order.transfer_error = ""
        order.transfer_started_at = None
        order.save(
            update_fields=[
                "state", "transfer_id", "transfer_error", "transfer_started_at",
            ]
        )
        return order

    if state in ("3", "4"):
        order.state = WithdrawOrder.STATE_FAILED
        order.transfer_id = transfer_id
        order.transfer_error = _transfer_error_text(data)
        order.transfer_started_at = None
        order.save(
            update_fields=[
                "state", "transfer_id", "transfer_error", "transfer_started_at",
            ]
        )
        # ⚠️ 先落 failed 再退款：即使退款这一步抛错，状态也已经是「失败」，不会停在转账中
        # 让下一个人以为钱还在路上。
        _refund_withdraw(
            order, operator=operator, note=f"代付失败退回：{order.transfer_error}"
        )
        if offset > 0:
            # ⭐ 同步失败回补（D-0336）：退款退回的是**单据全额**，发起时抵掉的那笔欠款并没有从
            # 这笔打款里真的收到——钱没有少打 ⇒ 欠款要**记回**负余额、等下一轮再「先扣」。
            # 与发起抵扣同事务、同 `ref_no`、同 kind（带符号：这条是负）。
            wallet, _ = TakerWallet.objects.select_for_update().get_or_create(
                user_id=order.user_id
            )
            wallet.balance -= offset
            wallet.save(update_fields=["balance", "updated_at"])
            _record(
                wallet,
                WalletTransaction.KIND_WITHDRAW_OFFSET,
                -offset,
                ref_no=f"W{order.pk}",
                note="代付同步失败，先扣的退款欠款记回",
                operator=operator,
            )
        return order

    raise WalletError(
        f"上游返回的转账状态无法识别（state={state!r}），请稍后刷新或人工核对",
        ErrorCode.BUSINESS,
    )


def _store_transfer_error(order_pk, message: str) -> None:
    """把「发起 / 查单请求本身失败」的原因写进单子（不采信、不落 paid）。
    ⚠️ 条件里带 `state=paying`：并发下另一条路可能已经把这单落成 `paid` / `failed`，这时不能把失败原因写到终态单上（会与 `transfer_id` / 退款结果自相矛盾）。
    """
    WithdrawOrder.objects.filter(
        pk=order_pk, state=WithdrawOrder.STATE_PAYING
    ).update(transfer_error=(message or "")[:200], transfer_started_at=None)


def _record_transfer_failure(order, operator, reason: str) -> None:
    """代付发起失败 / 请求异常时留一条审计。⚠️ 动作沿用审核那族 `finance.withdraw_transfer`（**不是**新造一条）：对读日志的人这就是「发起代付」这一跳的结果、只是失败——`reason` 里带错误原因即可。
    人为操作必须带 `actor_id`，调用方没给操作人时按机器来源显式 `system=True`（`record()` 的 fail-closed 契约，见 `apps/audit/log.py`）。
    """
    actor_id = getattr(operator, "pk", None)
    actor = getattr(operator, "username", "") or "系统"
    record(
        log_type=ACTION_LOG_TYPES["finance.withdraw_transfer"],
        action="finance.withdraw_transfer",
        actor=actor,
        actor_id=actor_id,
        system=actor_id is None,
        target_kind="withdraw_order",
        target_id=str(order.pk),
        target_name=getattr(order.user, "username", "") or "",
        to_value=f"{order.amount:.2f}",
        reason=(reason or "")[:200],
    )


def start_transfer(order, *, operator, client_ip: str, confirm_amount) -> WithdrawOrder:
    """发起支付宝代付：`approved → paying`（再由上游 `state` 决定是否 `paid` / `failed`）。硬闸（计划 §4.2）：① 代付总开关 `WITHDRAW_AUTO_TRANSFER` 关 ⇒ 一条都不发起（闸只放这里——人工登记打款不受它影响，但任何走到「真发钱」的调用都会被挡）；② 只接受 `approved`（第二次点击必然落到非 `approved` ⇒ `CONFLICT`，只发一笔）；③ `confirm_amount` 必须**等于**单子金额（防界面选错行）；④ 发起前落 `mchOrderNo = TW{pk}-{6位随机}`（唯一约束兜重复提交；随机后缀防两台租户机共用同一个 Jeepay 商户号时撞号）；⑤ 上游 `state=2/1/3-4` 的落法见 `_apply_transfer_state`；⑥ HTTP 之前写 `transfer_started_at`、拿到回应后清空（在途窗口）；⑦ ⭐ **先扣再打**（业务方拍板，D-0336）：发起前先扣未了结的退款欠款（`refund_debt`）——欠款 ≥ 单额 ⇒ 不打（台账 + 拒绝）；`0 < 欠款 < 单额` ⇒ 发起时把欠款从钱包抵掉、只发 `payable = 单额 − 欠款`（人工登记打款 `mark_withdraw_paid` 不在这道闸里：那是「人已经在外面转了账」的事后登记，发起时抵扣的单把实打金额写进 `remark` 供登记时核对）。
    顺序承重：**先落 `paying` 再调上游**——并发 / 连点时第二个请求看到 `paying` 直接 `CONFLICT`、⛔ 不会再发一笔；代价是上游请求本身失败时单子停在 `paying`（**有意**：还能查单 / 人工登记收场，退回 `approved` 会让人再点一次、可能真的打两笔）。
    """
    # ⭐ 代付总开关：⛔ 放在任何数据库动作之前——
    # 关着时连配置自检都不跑，保证「上游零调用」；定时命令也会在这一步被挡下。
    if not auto_transfer_enabled():
        logger.warning(
            "代付总开关 WITHDRAW_AUTO_TRANSFER 关闭：提现单 %s 不发起代付，请人工打款",
            order.pk,
        )
        raise WalletError("自动代付未开启，请登记人工打款", ErrorCode.BUSINESS)

    confirm = _quantize(confirm_amount)
    with transaction.atomic():
        order = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        if order.state != WithdrawOrder.STATE_APPROVED:
            raise WalletError("只有已通过的提现单能发起代付", ErrorCode.CONFLICT)
        # ⚠️ 金额回显必须是**等值**，⛔ 不是「不小于」/「四舍五入后相等」。
        if confirm != order.amount:
            raise WalletError("金额与提现单不一致，请刷新后重试", ErrorCode.VALIDATION)
        limit = transfer_max_amount()
        if limit is not None and order.amount > limit:
            # 文案逐字照计划 §9 第 1 条（页面此时只显示「登记打款」）。
            raise WalletError("超过代付单笔上限，请人工打款", ErrorCode.VALIDATION)

        # ⭐ 先扣再打（业务方拍板，D-0336）：⛔ 不能照原额打（欠款的定义与来源见 `refund_debt`）；⚠️ 必须在持
        # **订单行锁**的同一事务里再拿**钱包行锁**（锁序 订单 → 钱包，与全局锁序一致），否则会与并发的退款冲账各读各的余额。两态（每态都有用例）：`欠款 >= 单额` ⇒ 不打——本事务**一个字节都不写**，出了事务再写台账（`_record_debt_hold`）并拒绝（写在这里面会随 raise 一起回滚，E-0022 同族：留痕必须落在能提交的事务里）；`0 < 欠款 < 单额` ⇒ 在**发起时**就把欠款从钱包抵掉（负余额归零 + 带符号 `withdraw_offset` 流水），Jeepay 只发 `payable`；
        # ⭐ 抵扣放在**发起时**而不是落 `paid` 时：「转账中 → 后查单落 paid」是主流路径（查单支拿不到发起时刻的欠款额，也没有列可存）⇒ 发起时抵掉，之后**任何**路径落 `paid` 都天然平账（代价：同步失败那一支要把欠款记回，见 `_apply_transfer_state`）。
        debt_wallet = (
            TakerWallet.objects.select_for_update()
            .filter(user_id=order.user_id)
            .first()
        )
        debt = (
            Decimal("0")
            if debt_wallet is None
            else refund_debt(debt_wallet.balance)
        )
        offset = Decimal("0")
        payable = order.amount
        hold_debt = Decimal("0")
        if debt >= order.amount:
            hold_debt = debt
        else:
            if debt > 0:
                offset = debt
                payable = order.amount - offset

            # ⚠️ 配置自检放在改状态之前：否则一张单会被推进 paying 却从来没发出去。
            try:
                ensure_configured()
            except JeepayError as exc:
                raise WalletError(str(exc), exc.code) from exc

            # ⭐ 评审建议 5：不能只用 `TW{pk}`——两台租户机可能共用同一个 Jeepay 商户号，
            # 各自库里的 pk 会撞成同一个 `mchOrderNo`，后发的那台永远被上游判重复。
            # 6 位 token_hex = 16^6 ≈ 1677 万种；列宽 64 足够。重试沿用已落库的值。
            mch_order_no = order.mch_order_no or f"TW{order.pk}-{secrets.token_hex(3)}"
            order.mch_order_no = mch_order_no
            order.channel = WithdrawOrder.CHANNEL_JEEPAY_ALIPAY
            order.state = WithdrawOrder.STATE_PAYING
            order.transfer_error = ""
            if offset > 0:
                # 先扣（D-0336）：欠款从钱包抵掉（负余额归零）+ 一条带符号流水 +
                # `remark` 留痕（人工兜底登记打款时，操作员在这张单上能看到实打金额）。
                debt_wallet.balance += offset
                debt_wallet.save(update_fields=["balance", "updated_at"])
                _record(
                    debt_wallet,
                    WalletTransaction.KIND_WITHDRAW_OFFSET,
                    offset,
                    ref_no=f"W{order.pk}",
                    note=f"发起代付先扣退款欠款：实打 {payable:.2f} 元",
                    operator=operator,
                )
                order.remark = f"扣退款欠款 {offset:.2f} 元，实打 {payable:.2f} 元"
            # ⭐ HTTP **之前**写在途标记（与 `paying` 同一次提交）。
            order.transfer_started_at = timezone.now()
            order.save(
                update_fields=[
                    "mch_order_no", "channel", "state", "transfer_error",
                    "transfer_started_at", "remark",
                ]
            )

    if hold_debt:
        # 台账在事务**外**写（autocommit 才落得住），写完再响亮拒绝。
        _record_debt_hold(order, operator, hold_debt)
        raise WalletError(
            f"该打手有退款欠款 {hold_debt:.2f} 元未了结，"
            f"提现 {order.amount:.2f} 元不足以先扣，本轮暂停打款",
            ErrorCode.BUSINESS,
        )

    try:
        data = create_transfer(
            mch_order_no=mch_order_no,
            amount=payable,
            account_no=order.alipay_account,
            account_name=order.alipay_name,
            desc=f"打手提现 {order.pk}",
            client_ip=client_ip,
        )
    except JeepayError as exc:
        # 发起失败：留原因、**不采信**（单子停在 paying，等查单或人工打款）。
        _store_transfer_error(order.pk, str(exc))
        _record_transfer_failure(order, operator, str(exc))
        raise WalletError(str(exc), exc.code) from exc
    except Exception as exc:  # noqa: BLE001 - 非预期失败要留痕、但仍必须 500 响亮冒上去
        # 请求异常也要留痕（⛔ 不吞异常——E-0010：未预期的仍 500）。
        _store_transfer_error(order.pk, str(exc))
        _record_transfer_failure(order, operator, str(exc))
        raise

    with transaction.atomic():
        order = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        if order.state != WithdrawOrder.STATE_PAYING:
            # 并发下另一条路已经落了终态 ⇒ 不要把它的结果盖回去。
            return order
        # ⭐ `offset=offset`（D-0336）：这是**同步回应**，发起时抵掉的欠款额在作用域里
        # ⇒ state=3/4 时能把欠款记回。查单路径（`refresh_transfer`）拿不到 ⇒ 传默认 0。
        return _apply_transfer_state(order, data, operator=operator, offset=offset)


def refresh_transfer(order) -> WithdrawOrder:
    """查单刷新：`paying → paid / failed`。`failed` 是「已退款」终态 ⇒ 原样返回（**重复刷新不会重复退款**，计划 §7 第 2 条）；`paid` **拒绝刷新**（已打款没有可刷的东西，⛔ 不返回成功让人以为查到了新状态）；
    只有 `paying` 能查单（其它状态一律 `CONFLICT`）；查单请求本身失败 ⇒ 留 `transfer_error`、单子停在 `paying`（可再刷新）。
    """
    with transaction.atomic():
        order = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        if order.state == WithdrawOrder.STATE_FAILED:
            return order
        if order.state == WithdrawOrder.STATE_PAID:
            raise WalletError("这张提现单已经打款，不能刷新状态", ErrorCode.CONFLICT)
        if order.state != WithdrawOrder.STATE_PAYING:
            raise WalletError("只有转账中的提现单能刷新状态", ErrorCode.CONFLICT)
        if transfer_inflight(order):
            # ⭐ 发起请求还在途，此刻查单可能查到「还没受理」的中间态，
            # 也可能把在途标记清掉 ⇒ 等它回来再说。
            raise WalletError("该单正在发起代付，请稍后刷新状态", ErrorCode.CONFLICT)
        if not order.mch_order_no:
            raise WalletError("这张提现单没有商户转账单号，无法查单", ErrorCode.CONFLICT)
        mch_order_no = order.mch_order_no

    try:
        data = query_transfer(mch_order_no)
    except JeepayError as exc:
        _store_transfer_error(order.pk, str(exc))
        raise WalletError(str(exc), exc.code) from exc

    with transaction.atomic():
        order = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        if order.state != WithdrawOrder.STATE_PAYING:
            return order
        # ⚠️ 查单路径不带 `offset`（D-0336 的残差，登记在 `_apply_transfer_state` docstring）：
        # 发起时刻抵掉的欠款额没有列可存 ⇒ 查单落 3/4 时不把欠款记回（平台少收一笔），
        # 落 2 时天然平账（欠款在发起时已抵）。
        return _apply_transfer_state(order, data)


# ── 人工登记打款────────────────────────
def mark_withdraw_paid(order, *, operator, transfer_no: str, remark: str = "") -> WithdrawOrder:
    """登记人工打款：`approved / paying → paid`。**打款单号必填**。⛔ `paying` 不能直接当「可以人工兜底」——它有两个完全不同的子状态，撞在一起会**上游付一次 + 人工再付一次**：① 我方 HTTP 请求在途（`transfer_started_at` 非空且 <120 秒）⇒ 直接拒「请稍后刷新」；② 请求已回来、上游说 `state=1` ⇒ **先查单**（`1/0` 仍在途 ⇒ 拒；`2` 已成功 ⇒ 本地落 `paid` 再拒，⛔ 不许人工再打一次；`3/4` 已失败 ⇒ 落 `failed` + 退款再拒；查单报错 / 上游查不到 ⇒ **才允许**人工登记——「代付根本没发出去」那一档）。
    登记成功后 `channel` 置回 `manual`（保留 `transfer_id` 留痕）；`paid` 是终态、不可逆（重复登记一律 `CONFLICT`）。
    """
    transfer_no = (transfer_no or "").strip()
    if not transfer_no:
        raise WalletError("请填写打款单号", ErrorCode.VALIDATION)

    with transaction.atomic():
        locked = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        if locked.state == WithdrawOrder.STATE_PAID:
            raise WalletError("这张提现单已经打款，不能重复登记", ErrorCode.CONFLICT)
        if locked.state not in (
            WithdrawOrder.STATE_APPROVED,
            WithdrawOrder.STATE_PAYING,
        ):
            raise WalletError(
                "只有已通过 / 转账中的提现单能登记人工打款", ErrorCode.CONFLICT
            )
        # ⭐ 首段快照：两段之间隔着 HTTP 查单 / 别的人工动作，
        # `locked` 出事务即作废。终段拿它和**重新读到的**状态比，才能认出
        # 「我读到 approved 之后，别处已经把它推进 paying」。
        snapshot_state = locked.state
        was_paying = locked.state == WithdrawOrder.STATE_PAYING
        mch_order_no = locked.mch_order_no
        if was_paying and transfer_inflight(locked):
            # 窗口 1：我方请求还没回来 —— 绝不允许人工再打一笔。
            raise WalletError("该单正在发起代付，请稍后刷新状态", ErrorCode.CONFLICT)

    if was_paying and mch_order_no:
        # 窗口 2：请求已回来、上游可能已受理 ⇒ 先查单，再决定能不能人工兜底。
        try:
            data = query_transfer(mch_order_no)
        except JeepayError:
            # 查单报错 / 上游没有这张单 ⇒ 视为「代付没发出去」，允许人工登记。
            data = None
        if data is not None:
            upstream_state = _transfer_state_text(data)
            if upstream_state in ("0", "1"):
                raise WalletError(
                    "该单代付仍在进行中，请先刷新状态后再决定是否人工打款",
                    ErrorCode.CONFLICT,
                )
            if upstream_state == "2":
                with transaction.atomic():
                    current = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
                    if current.state == WithdrawOrder.STATE_PAYING:
                        _apply_transfer_state(current, data, operator=operator)
                raise WalletError(
                    "该单代付已成功，无需人工登记打款", ErrorCode.CONFLICT
                )
            if upstream_state in ("3", "4"):
                with transaction.atomic():
                    current = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
                    if current.state == WithdrawOrder.STATE_PAYING:
                        _apply_transfer_state(current, data, operator=operator)
                raise WalletError(
                    "该单代付已失败并已退回余额，无需人工登记打款",
                    ErrorCode.CONFLICT,
                )
            raise WalletError(
                "查单返回的转账状态无法识别，请稍后刷新或人工核对",
                ErrorCode.BUSINESS,
            )

    with transaction.atomic():
        current = WithdrawOrder.objects.select_for_update().get(pk=order.pk)
        if current.state == WithdrawOrder.STATE_PAID:
            raise WalletError("这张提现单已经打款，不能重复登记", ErrorCode.CONFLICT)
        if current.state not in (
            WithdrawOrder.STATE_APPROVED,
            WithdrawOrder.STATE_PAYING,
        ):
            raise WalletError(
                "只有已通过 / 转账中的提现单能登记人工打款", ErrorCode.CONFLICT
            )
        # ⭐ 跨事务复查：首段读到的 `approved` 已经不成立
        # —— 两次拿锁之间另一条路（定时命令 / 别的客服）把它推进了 `paying`，
        # 再往下写 `paid` 就是「上游付一次 + 人工再付一次」。
        if (
            current.state == WithdrawOrder.STATE_PAYING
            and snapshot_state == WithdrawOrder.STATE_APPROVED
        ):
            raise WalletError("这笔正在自动打款，请刷新", ErrorCode.CONFLICT)
        # 首段读到的 `paying` 当时不在途，但窗口里另一条路又写上了在途标记 ⇒ 同样不能人工打。
        if transfer_inflight(current):
            raise WalletError("这笔正在自动打款，请刷新", ErrorCode.CONFLICT)
        current.state = WithdrawOrder.STATE_PAID
        current.paid_at = timezone.now()
        # ⭐ 人工兜底成功后渠道回到 manual（`transfer_id` 保留，仍能查出「发起过代付」）。
        current.channel = WithdrawOrder.CHANNEL_MANUAL
        current.transfer_started_at = None
        # 人工打款单号落在 `remark`（它是人工流程的凭证；`transfer_id` 只放上游平台单号）。
        # 备注非必填，但要保留「谁/voucher」：单号一定进，附加备注按需接在后面。
        text = f"人工打款单号：{transfer_no}"
        if remark.strip():
            text += f"；{remark.strip()}"
        current.remark = text[:200]
        current.transfer_error = ""
        current.save(
            update_fields=[
                "state", "paid_at", "channel", "transfer_started_at", "remark",
                "transfer_error",
            ]
        )
        return current
