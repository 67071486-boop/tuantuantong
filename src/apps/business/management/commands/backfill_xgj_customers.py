"""存量回填客户（会员）档案（2026-11-10 客户线，W2 写入点）：对已成功发过货的在库单逐单调闲管家
`order/detail`，命中 `buyer_eid` ⇒ `apps/business/customers.upsert_customer`（与 W1 同一份）建/刷新客户并连单。
⚠️ 串行 ≥0.5s（可行机同秒限流前科 E-2026-11-01）；幂等可续跑；⛔ 输出不出 secret/完整 eid/手机号（用法见 spec §2.4）。
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.business.api.xgj_views import credentials_of
from apps.business.customers import upsert_customer
from apps.business.models import (
    Customer,
    Order,
    SendResult,
    TaobaoOrderSendLog,
    XgjShopBinding,
)
from apps.business.xgj import DEFAULT_BASE_URL as XGJ_DEFAULT_BASE_URL
from apps.business.xgj import XgjError
from apps.business.xgj import order_detail as xgj_order_detail

#: 每次上游调用之间的最小间隔（秒）。可行机有同秒限流前科（模块 docstring）。
UPSTREAM_INTERVAL_SECONDS = 0.5

#: `Customer.eid` 的列宽（models.py 是唯一出处，这里引用同一数字）。
EID_MAX_LENGTH = 64


def _mask_eid(eid: str) -> str:
    """eid 打掩码（⛔ 完整 eid 不进命令输出 / 日志）：留头 4 尾 2，短串只留头 2。"""
    if len(eid) <= 8:
        return eid[:2] + "***"
    return f"{eid[:4]}***{eid[-2:]}"


class Command(BaseCommand):
    help = (
        "回填客户（会员）档案：对已成功发货的在库订单逐单调闲管家 order/detail，"
        "按 buyer_eid 建客户并连单（串行 0.5s 限速；幂等可续跑，只碰还没连客户的单）"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--schema", required=True,
            help="目标租户 schema（必填：本命令按次调上游，⛔ 不默认扫全部租户）",
        )
        parser.add_argument(
            "--limit", type=int, default=300,
            help="本轮最多回填多少个单（默认 300；全量回填挂账另排期，靠分批跑完）",
        )
        parser.add_argument(
            "--binding", type=int, default=None,
            help="只试用这一条闲管家应用绑定（XgjShopBinding 主键）；默认按「来源→店→应用」链，链上没有再依次试全部已配置绑定",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._upstream_calls = 0

    # ── 上游取数（限速在这一层收口，本命令的一切 order/detail 都经它）───────
    def _fetch_detail(self, *, tb_order_no: str, app_key: str, app_secret: str) -> dict:
        """调一次闲管家 `order/detail`；非首次调用前先等 `UPSTREAM_INTERVAL_SECONDS`。"""
        if self._upstream_calls > 0:
            self._sleep(UPSTREAM_INTERVAL_SECONDS)
        self._upstream_calls += 1
        return xgj_order_detail(
            tb_order_no,
            app_key=app_key,
            app_secret=app_secret,
            base_url=getattr(settings, "XGJ_API_BASE", "") or XGJ_DEFAULT_BASE_URL,
        )

    def _sleep(self, seconds: float) -> None:
        """独立一小步的理由：测试把限速打桩成 no-op（桩打在被查找的模块上，E-0029）。"""
        import time

        time.sleep(seconds)

    # ── 绑定选择 ──────────────────────────────────────────────────────────
    def _forced_credentials(self, binding_pk):
        """`--binding` 档：只准用这一条绑定（不存在 / 凭据不完整 ⇒ 响亮 CommandError）。"""
        binding = XgjShopBinding.objects.filter(pk=binding_pk).first()
        found = credentials_of(binding) if binding is not None else None
        if found is None:
            raise CommandError(f"--binding {binding_pk} 指向的闲管家应用不存在或凭据不完整")
        return [found]

    def _binding_for_order(self, order: Order):
        """凭据链：「来源 → XgjShop → binding」（复用 read-platform-order 的**同一份**解析，
        ⛔ 不抄第二份）；单没有来源 / 店没挂应用 ⇒ None。"""
        if order.order_source_id is None:
            return None
        from apps.business.api.platform_order_views import ReadPlatformOrderView

        _binding, credentials = ReadPlatformOrderView._binding_for_source(
            order.order_source_id
        )
        return credentials

    # ── 候选扫描 ──────────────────────────────────────────────────────────
    def _candidates(self, limit: int):
        """发货成功快照（SUCCESS）按发货时间倒序 → 对应在库订单（默认 manager 排除已删）
        且 `customer IS NULL`；`tb_order_no` 去重；截 `--limit`。"""
        seen_tb: set[str] = set()
        found: list[tuple[str, Order]] = []
        logs = TaobaoOrderSendLog.objects.filter(result=SendResult.SUCCESS).order_by(
            "-sent_at", "-id"
        )
        for log in logs.iterator():
            if len(found) >= limit:
                break
            if log.tb_order_no in seen_tb:
                continue
            seen_tb.add(log.tb_order_no)
            order = Order.objects.filter(
                order_no=log.order_no, customer_id__isnull=True
            ).first()
            if order is None:
                # 单不在 / 已软删 / 已连过客户 ⇒ 本轮无事可做（续跑判据：剩下的全是这类）。
                continue
            found.append((log.tb_order_no, order))
        return found

    # ── 单笔回填 ──────────────────────────────────────────────────────────
    def _backfill_one(
        self, tb_order_no: str, order: Order, forced_credentials
    ) -> tuple[str, bool]:
        """回填一单，返回 `(outcome, customer_was_created)`：
        outcome ∈ `linked`（已连）/ `already_linked`（UPDATE 时已非 NULL：并发重跑另一路先连）
        / `no_eid`（上游没给可用 eid）/ `failed`（上游失败）。"""
        chain_credentials = (
            None if forced_credentials is not None else self._binding_for_order(order)
        )
        if forced_credentials is not None:
            plans = [(forced_credentials[0], True)]
        elif chain_credentials is not None:
            # 链上命中的应用读不到这一单 = 最终失败（⛔ 不回落逐个试——「来源指向的应用」
            # 就是这单该用的应用，换应用读到的是别家店的同号单=静默读错，自动判定同一条口径）。
            plans = [(chain_credentials, True)]
        else:
            # 链上找不到 ⇒ 依次试已配置绑定：命中即停（订单号属于哪家应用是唯一的，
            # 与录单自动判定同一条口径；逐个失败是这条路的**正常**形态，不算最终失败）。
            plans = [
                (found, False)
                for found in (
                    credentials_of(binding)
                    for binding in XgjShopBinding.objects.order_by("id")
                )
                if found is not None
            ]
        detail = None
        last_code = None
        for credentials, is_final in plans:
            try:
                detail = self._fetch_detail(
                    tb_order_no=tb_order_no, app_key=credentials[0], app_secret=credentials[1]
                )
                break
            except XgjError as exc:
                last_code = exc.upstream_code
                # ⛔ 不记上游原文（可能回显凭据，与 read-platform-order 同一条脱敏纪律）。
                if is_final:
                    break
        if detail is None:
            self.stdout.write(
                f"上游失败 单号={tb_order_no} 系统单={order.order_no} upstream_code={last_code}"
            )
            return "failed", False
        eid = str(detail.get("buyer_eid") or "")
        nick = str(detail.get("buyer_nick") or "")
        if not eid or len(eid) > EID_MAX_LENGTH:
            # 键不在 / 空 / 超列宽：身份键不裁不兜，计数跳过（输出只带 eid 长度，不带原文）。
            self.stdout.write(f"跳过（无可用 eid）系统单={order.order_no} eid_len={len(eid)}")
            return "no_eid", False
        customer, was_created = upsert_customer(eid=eid, buyer_nick=nick)
        # 条件 UPDATE（customer IS NULL 才写）：同 eid 多单 / 并发重跑都幂等。
        linked = Order.objects.filter(pk=order.pk, customer_id__isnull=True).update(
            customer=customer
        )
        self.stdout.write(
            f"{'新建' if was_created else '更新'}客户 eid={_mask_eid(eid)} "
            f"系统单={order.order_no} 连单={'是' if linked else '否（已被连过）'}"
        )
        return ("linked" if linked else "already_linked"), was_created

    # ── 主流程 ────────────────────────────────────────────────────────────
    def handle(self, *args, **options):
        from django_tenants.utils import tenant_context

        from apps.platform.models import Tenant

        schema = options["schema"]
        tenant = Tenant.objects.filter(schema_name=schema).first()
        if tenant is None:
            raise CommandError(f"租户 schema 不存在：{schema}")
        limit = max(options["limit"], 0)
        binding_pk = options["binding"]

        with tenant_context(tenant):
            forced = (
                self._forced_credentials(binding_pk) if binding_pk is not None else None
            )
            candidates = self._candidates(limit)
            self.stdout.write(
                f"候选订单 {len(candidates)} 个（limit={limit}），开始逐单回填"
            )
            counts = {"linked": 0, "already_linked": 0, "no_eid": 0, "failed": 0}
            created = 0
            for tb_order_no, order in candidates:
                outcome, was_created = self._backfill_one(tb_order_no, order, forced)
                counts[outcome] += 1
                if was_created:
                    created += 1
            self._report(counts, created)

    def _report(self, counts: dict, created: int) -> None:
        """终态判据输出（E-0162：「没报错」不是判据）：逐项计数 + **库内复查值**收尾。"""
        self.stdout.write(
            "回填完成：候选 "
            f"{sum(counts.values())} = "
            f"连单 {counts['linked']}（新建客户 {created}） + "
            f"跳过（无 eid）{counts['no_eid']} + 上游失败 {counts['failed']} + "
            f"已被连过 {counts['already_linked']}（本轮扫描后别的写入先连了：并发重跑的幂等形态，非错误）"
        )
        # 复查值 = 直接问库，不拿本轮计数当终态（幂等续跑时两者天然不同）。
        self.stdout.write(
            f"复查：Customer 表 {Customer.objects.count()} 行；"
            f"已连客户的在库订单 {Order.objects.filter(customer__isnull=False).count()} 张"
        )
