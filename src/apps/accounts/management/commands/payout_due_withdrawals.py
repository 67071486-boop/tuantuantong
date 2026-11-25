"""`payout_due_withdrawals`：押金提现的**超时自动审核 + 到期自动代付**（生产形态：systemd timer；`--tenant x` 只跑一个租户排障）。做什么（逐条对应规格 §2.2）：① 选 `state=approved` ∧ `payout_after <= now` ∧ `transfer_started_at IS NULL` ∧ `transfer_attempts < 上限` 的单；② 该用户**还有别的进行中提现单**（`pending` / `approved` / `paying`）⇒ 跳过、记一行 info（冷静期内有新单 / 有争议 ⇒ 这一轮不打，⛔ 不延后、不取消）；③ 调 `wallet.start_transfer(order, operator=None, client_ip="127.0.0.1", confirm_amount=order.amount)`——状态机、金额回显、`mch_order_no` 唯一都复用它，⛔ 本命令不自己写状态；④ 失败（`WalletError` / `JeepayError`）⇒ `transfer_attempts += 1` + 原因写进 `transfer_error`，**不回滚状态**；到上限就停手、留人工。
开关：`SystemSwitch` 的 `WITHDRAW_AUTO_PAYOUT`（默认 `"Y"`，业务方临时口径）关着时一条都不发（连 `start_transfer` 都不调）；环境开关 `WITHDRAW_AUTO_TRANSFER`（默认 `False`）关着时同样**整批干净跳过**——不取到期单、不计 `transfer_attempts`、不写 `transfer_error`（否则「关一周再开」会让一批单被失败计数静默挂掉）；`start_transfer` 自己也有一道同样的闸（防其它调用方绕过），本命令的早退只是让「关着」这一轮**不留任何已尝试痕迹**。⚠️ 与规格的一处偏离（如实登记）：规格 §2.2 第 2 条写的 `claim._ACTIVE_STATUSES` 是**订单**状态集、套到 `WithdrawOrder.state` 上永远不命中（两类枚举取值完全不同）⇒ 本命令按同一意图取「提现单自己的进行中三态」，⛔ 没有 import `_ACTIVE_STATUSES` 去拼恒假过滤。幂等：`start_transfer` 的 `approved → paying` 行锁 + `mch_order_no` 唯一约束兜住重复发起，本命令不额外加锁；取数条件里的 `transfer_started_at IS NULL` 挡「上一轮请求还在途」。
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import F
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, tenant_context

from apps.accounts.jeepay import JeepayError
from apps.accounts.models import WithdrawOrder
from apps.accounts.wallet import (
    WalletError,
    auto_approve_due_withdrawals,
    auto_transfer_enabled,
    start_transfer,
)
from apps.business.system_switches import switch_enabled
from apps.platform.models import Tenant

#: 自动代付开关的 key（`apps.business.system_switches.SWITCHES` 的注册表项）。
AUTO_PAYOUT_SWITCH = "WITHDRAW_AUTO_PAYOUT"

#: 连续失败多少轮之后停手、留人工（规格 §2.2 的「N 次」；实现定 5，够覆盖一次上游抖动）。
MAX_TRANSFER_ATTEMPTS = 5

#: 「进行中的提现单」三态（与 `WithdrawOrder` 的状态机同源，⛔ 不用订单的状态集）。
WITHDRAW_ACTIVE_STATES = (
    WithdrawOrder.STATE_PENDING,
    WithdrawOrder.STATE_APPROVED,
    WithdrawOrder.STATE_PAYING,
)

#: 系统发起代付时填的客户端 IP（Jeepay 要这个字段；不是真实客户端）。
SYSTEM_CLIENT_IP = "127.0.0.1"


class Command(BaseCommand):
    help = "按冷静期自动发起押金提现的支付宝代付（开关 WITHDRAW_AUTO_PAYOUT，默认开）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            default=None,
            help="只跑一个租户 schema（排障）；不在库里 ⇒ 响亮报错",
        )

    def handle(self, *args, **options):
        unexpected = False
        for tenant in self._iter_tenants(options.get("tenant")):
            # ⚠️ 逐租户独立异常边界：一个租户失败**不影响**别的租户。
            # ⛔ 只写异常类型名：`str(exc)` 可能带上游回包 / 凭据片段。
            try:
                with tenant_context(tenant):
                    if self._run_tenant(tenant):
                        unexpected = True
            except Exception as exc:  # noqa: BLE001 —— 逐租户异常边界
                unexpected = True
                self.stderr.write(
                    f"{tenant.schema_name}: 本轮失败（{type(exc).__name__}），下一个租户继续"
                )
        if unexpected:
            raise CommandError("有租户的本轮出现未预期失败，详见上方汇总", returncode=1)

    def _iter_tenants(self, schema):
        tenants = Tenant.objects.exclude(schema_name=get_public_schema_name())
        if schema:
            tenants = tenants.filter(schema_name=schema)
            if not tenants.exists():
                raise CommandError(f"没有这个租户 schema：{schema}")
        return list(tenants.order_by("schema_name"))

    def _run_tenant(self, tenant) -> bool:
        """在一个租户里跑一轮。返回「是否出现未预期失败」（退出码契约用）。"""
        # ⭐ 超时自动审核：**长期规则**，必须排在 `WITHDRAW_AUTO_PAYOUT`
        # 这个临时开关判据**之前**——关掉自动代付只该停打款，不该停审核。
        # ⚠️ 必须在 `tenant_context` 之内（`handle` 已建立）；本函数直接查租户表。
        reviewed = auto_approve_due_withdrawals()
        if reviewed:
            self.stdout.write(
                f"{tenant.schema_name}: 超时自动通过 {reviewed} 笔提现"
            )

        # ⭐ 代付总开关：关着就**整批干净跳过**。⛔ 不能靠 `start_transfer` 抛错落到下面的失败分支：那会白白消耗 `transfer_attempts`、写 `transfer_error`，5 轮后这批单再也不被取到——「关一周再开」会静默挂掉一批提现，比原来的 bug 更隐蔽。
        if not auto_transfer_enabled():
            self.stdout.write(
                f"{tenant.schema_name}: WITHDRAW_AUTO_TRANSFER=关 ⇒ 本轮不发起代付"
                "（转人工打款）"
            )
            return False

        if not switch_enabled(AUTO_PAYOUT_SWITCH):
            self.stdout.write(
                f"{tenant.schema_name}: {AUTO_PAYOUT_SWITCH}=关 ⇒ 本轮一条都不发"
            )
            return False

        due = list(
            WithdrawOrder.objects.filter(
                state=WithdrawOrder.STATE_APPROVED,
                payout_after__isnull=False,
                payout_after__lte=timezone.now(),
                transfer_started_at__isnull=True,
                transfer_attempts__lt=MAX_TRANSFER_ATTEMPTS,
            )
            .select_related("user")
            .order_by("payout_after", "pk")
        )
        sent = skipped = failed = 0
        for order in due:
            # ⛔ **没有「快路单豁免」**（spec §8.2：前三单快路整段下线）：所有单一律受同一条「同用户还有别的进行中提现 ⇒ 本轮跳过」判据约束。⚠️ 历史上 `fast_lane=True` 的行仍在库里（列保留、行不动），它们**不再**享有任何特权；本轮被跳过的那张单会在另一张走完之后的下一次 timer 里被付掉，钱不会丢。
            if self._has_another_active_withdrawal(order):
                skipped += 1
                self.stdout.write(
                    f"{tenant.schema_name}: 提现单 {order.pk} 跳过 —— "
                    "该用户还有进行中的提现单"
                )
                continue
            try:
                start_transfer(
                    order,
                    # ⚠️ 没有「系统账号」这个对象：传 `None` ⇒ 失败留痕落 `system=True` 的
                    # 机器行（`record()` 的 fail-closed 契约），⛔ 不借一个真人的账号冒充。
                    operator=None,
                    client_ip=SYSTEM_CLIENT_IP,
                    confirm_amount=order.amount,
                )
            except (WalletError, JeepayError) as exc:
                failed += 1
                attempts = self._record_failure(order, str(exc))
                self.stderr.write(
                    f"{tenant.schema_name}: 提现单 {order.pk} 代付失败"
                    f"（第 {attempts} 次）：{exc}"
                )
                if attempts >= MAX_TRANSFER_ATTEMPTS:
                    self.stderr.write(
                        f"{tenant.schema_name}: 提现单 {order.pk} 已连续失败 "
                        f"{attempts} 次 ⇒ **停手**，请人工处理"
                    )
            else:
                sent += 1
                order.refresh_from_db()
                self.stdout.write(
                    f"{tenant.schema_name}: 提现单 {order.pk} 已发起代付"
                    f"（状态 {order.state}）"
                )
        self.stdout.write(
            f"{tenant.schema_name}: 到期 {len(due)} · 发起 {sent} · 跳过 {skipped}"
            f" · 失败 {failed}"
        )
        return False

    def _has_another_active_withdrawal(self, order) -> bool:
        """该用户除了这张单以外，还有没有进行中的提现单。"""
        return (
            WithdrawOrder.objects.filter(
                user_id=order.user_id, state__in=WITHDRAW_ACTIVE_STATES
            )
            .exclude(pk=order.pk)
            .exists()
        )

    def _record_failure(self, order, message: str) -> int:
        """失败计数 +1 并把原因写回单子；返回**累计**尝试次数。
        ⚠️ 只更新 `approved` / `paying` 两态：并发下另一条路可能已经把这单落成 `paid` / `failed`，那时把失败原因写上去会与终态自相矛盾（同 `wallet._store_transfer_error` 的口径）。
        """
        WithdrawOrder.objects.filter(
            pk=order.pk,
            state__in=(WithdrawOrder.STATE_APPROVED, WithdrawOrder.STATE_PAYING),
        ).update(
            transfer_attempts=F("transfer_attempts") + 1,
            transfer_error=(message or "")[:200],
        )
        return (
            WithdrawOrder.objects.filter(pk=order.pk)
            .values_list("transfer_attempts", flat=True)
            .first()
            or 0
        )
