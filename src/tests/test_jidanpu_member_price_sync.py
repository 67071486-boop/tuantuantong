"""T3 · 改了发单价 ⇒ 把价同步到**下游鸡蛋铺**（`member.price`）。

本文件回答两件事，各自独立可失败：

① **领域层的两种形态**（计划 §4-T3 Step 2 / 判据 ①②③④⑤）——
   `in_hall=True` 的单 ⇒ 先改**接单价 `price`**、再按新价**重抛**（改 `recFee`），两格都**回读钉值**；
   `in_hall=False` 的单 ⇒ **只**改接单价（⛔ 一次 `throw_order` 都不许有）；
   回读不符 / 没有链路 / `hall_price is None` 各落成什么（⛔ 不许假绿成 `done`）；
   `rearm=True` 把烧完额度的行复位。

② **四个发单价写入口的接线**（计划 §4-T3 Step 3 / 判据 ②⑥）——
   `/hall-price/`、`/assign/` 带价、`/throw-to-hall/` 带价、`/adjust-amount/` 落回 `Order.hall_price`
   **各一条**（E-0027 同族：「以后新加一个写价点会漏掉一边」是本仓明写过的坑）+
   一条走真 HTTP 的端到端（台账里出现 `member.price`，消费者收到正确的 fee）。

⚠️ `mock.patch` 一律打在**被查找的那个模块**上（E-0029）：
   - 消费者查的是 `apps.business.jidanpu.{change_order_price,throw_order,fetch_order_by_key1}`；
   - 四个写口查的是 `apps.business.jidanpu_member.queue_price_update`。
⚠️ 断言钉**值**（费用 / 状态 / 原因 / 链路标记），⛔ 不钉常量、⛔ 不断言 logger（E-0024 / §5-G）。
⚠️ 夹具一律**合成值**（E-0077）：`price-sync-*` / `https://jidanpu-price.test.invalid` / `合成项目*`。
⛔ 零真实网络。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError

from apps.accounts.choices import CommissionMode
from apps.accounts.models import StaffProfile
from apps.business import jidanpu, jidanpu_member
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import hash_token, new_token
from tests.p5_auth_helpers import grant_games, make_logged_in_client

ORDERS = "/api/business/orders"
PERFORMANCES = "/api/business/performances"

BASE_URL = "https://jidanpu-price.test.invalid"

#: 本文件走到的**端点门**（`OrderViewSet` / `PerformanceViewSet` 的每动作权限）。
NODES = (
    "order.list",
    "order.set_offer_price",        # /hall-price/
    "order.submit_payout_price",    # /assign/ 带价 + /adjust-amount/
    "order.assign_taker",           # /assign/ 的类级门
    "order.send_to_grab_hall",      # /throw-to-hall/
)

#: kind 的**字面值**就是写台账的契约（⛔ 改名会静默换掉语义；E-0024 钉值不钉常量）。
KIND_PRICE_LITERAL = "member.price"

#: 三句人话原因的**逐字**副本（⛔ 不从被测常量派生：自指断言在被断言的常量改坏时**不会红**）。
PRICE_NO_LINK_LITERAL = "本线台账里没有这张单的链路，无法改对方接单价"
#: ⭐ 改价档**自己的**「没有发单价」那句（控制方裁定：⛔ 不复用 `ASSIGN_NO_PRICE_REASON`
#: —— 那句说的是「还没录单 ⇒ 等对账补录」，而本档是给已录进去的单改价、且对账不查价）。
PRICE_NO_PRICE_LITERAL = (
    "该单没有发单价，本次没有可同步给下家的价（补上发单价后请再改一次价）"
)
PRICE_CONFIRM_LITERAL = "改接单价后回读未确认对方那张单的接单价已是目标值（写后未能确认）"
#: 写接单价被上游拒 / 回读那一跳上游报错（逐字副本）。
PRICE_FAILED_LITERAL = "鸡蛋铺拒绝了这次改接单价"
PRICE_CONFIRM_READ_FAILED_LITERAL = "改接单价后回读上游出错，本轮无法确认写后的接单价"
#: 重抛那一跳的失败 / 回读不符（逐字副本）。
THROW_FAILED_LITERAL = (
    "鸡蛋铺拒绝了这次抛单：可能是该账号缺『将订单发到内部抢单大厅』权限，"
    "请在下家鸡蛋铺里给这个职位勾上"
)
THROW_CONFIRM_LITERAL = "抛单后回读确认失败（isThrowOrder 不是 Y）"
PRICE_THROW_FEE_CONFIRM_LITERAL = (
    "重抛后回读未确认对方那张单的发单价（recFee）已是目标值（写后未能确认）"
)

#: 上游失败桩的合成 msg（`_upstream_reason` 会把它接在口径句后面 ⇒ 期望值是**整句**，
#: 形如 `<口径句>：上游合成错误甲（上游码 0）`）。
UPSTREAM_FAIL_MSG = "上游合成错误甲"
UPSTREAM_FAIL_CODE = 0


def _upstream_failure() -> ApiError:
    """一个上游失败桩（业务壳里有 `msg` + 业务码 ⇒ `_upstream_reason` 会接上那两样）。"""
    return ApiError(
        UPSTREAM_FAIL_MSG,
        body={"msg": UPSTREAM_FAIL_MSG, "code": UPSTREAM_FAIL_CODE},
        code=UPSTREAM_FAIL_CODE,
    )


def _with_upstream_reason(prefix: str) -> str:
    """`前缀：msg（上游码 N）` —— 与 `_upstream_reason` 的形状一致（⛔ 不是拿它算的）。"""
    return f"{prefix}：{UPSTREAM_FAIL_MSG}（上游码 {UPSTREAM_FAIL_CODE}）"

#: 回读桩的「调用方没设过」哨兵（`apply_change=False` 是**合法值**，⛔ 不能拿 `None` 兼任）。
_UNSET = object()


class _PriceStub:
    """`apps.business.jidanpu` 上那三个被 `_consume_price` 查找的出口的脚本替身。

    ⚠️ 它**有状态**（照 `test_jidanpu_member_consume._Upstream` 的形状）：
    `change_order_price` 成功就改自己的 `price`、`throw_order` 成功就改 `rec_fee` 与 `is_throw`
    ——消费者是**写后回读**判成败的，桩不跟着变就永远判不出 `done`。
    ⚠️ 回包里的金额刻意回 **float**（真实回包就是 JSON 数字），用来钉住
    `_remote_amount_value` 的归一化（`Decimal(str(...))`）。
    """

    def __init__(
        self, *, price="0.00", rec_fee="0.00", in_hall=False,
        apply_change=True, apply_throw=True,
        change_error=None, throw_error=None, fetch_error=None, readable=True,
    ):
        self.price = Decimal(str(price))
        self.rec_fee = Decimal(str(rec_fee))
        self.is_throw = "Y" if in_hall else "N"
        self.apply_change = apply_change
        self.apply_throw = apply_throw
        self.change_error = change_error
        self.throw_error = throw_error
        self.fetch_error = fetch_error
        self.readable = readable
        #: 回读被调用了几次（`fetch_error` 给**列表**时按调用序号取那一次要抛的错）。
        self.fetch_calls = 0
        self.calls: list[tuple[str, dict]] = []

    def names(self):
        return [name for name, _kw in self.calls]

    def kwargs_of(self, name):
        return [kw for call_name, kw in self.calls if call_name == name]

    def change_order_price(self, client, *, remote_order_id, fee):
        self.calls.append(("change_order_price", {
            "remote_order_id": remote_order_id, "fee": fee,
        }))
        if self.change_error is not None:
            raise self.change_error
        if self.apply_change:
            self.price = Decimal(str(fee))

    def throw_order(self, client, *, remote_order_id, fee):
        self.calls.append(("throw_order", {
            "remote_order_id": remote_order_id, "fee": fee,
        }))
        if self.throw_error is not None:
            raise self.throw_error
        if self.apply_throw:
            self.rec_fee = Decimal(str(fee))
            self.is_throw = "Y"

    def fetch_order_by_key1(self, client, *, key, expect_field, expect_value):
        self.calls.append(("fetch_order_by_key1", {"key": key}))
        self.fetch_calls += 1
        # ⚠️ `fetch_error` 允许给**列表**（逐次取那一跳要抛的错，`None` = 这一次不抛）：
        #    消费者有**两跳**回读（接单价那一跳 / 重抛后那一跳），用同一个开关既能测第一跳
        #    报错（给一个异常）也能测**第二跳**报错（给 `[None, exc]`）。给裸异常 = 每次都抛。
        error = self.fetch_error
        if isinstance(error, (list, tuple)):
            error = error[self.fetch_calls - 1] if self.fetch_calls <= len(error) else None
        if error is not None:
            raise error
        if not self.readable:
            return None
        return {
            "orderId": key,
            "price": float(self.price),
            "originPrice": float(self.price),
            "recFee": float(self.rec_fee),
            "isThrowOrder": self.is_throw,
        }


@contextmanager
def _stub(up):
    """把替身接到**被查找的模块** `apps.business.jidanpu` 上（E-0029）。"""
    with (
        mock.patch.object(
            jidanpu, "change_order_price", side_effect=up.change_order_price
        ),
        mock.patch.object(jidanpu, "throw_order", side_effect=up.throw_order),
        mock.patch.object(
            jidanpu, "fetch_order_by_key1", side_effect=up.fetch_order_by_key1
        ),
    ):
        yield up


#: 注入的假客户端：桩全部忽略它（只在「不接桩」的对照里会被真实出口碰到）。
CLIENT = object()

#: `jidanpu_member` 会查找的 `apps.business.jidanpu` 出口全集（消费者吃 3 个；录单 / 来源字典 /
#: 退单 / 改状态 / 客服 uid 各 1 个；加上 T3 新加的 `change_order_price` ⇒ **8** 个）。
#: `queue_price_update` 是**纯排队口**，一个都不许碰（用这 8 个桩验过）。
UPSTREAM_EXITS = (
    "add_order",
    "fetch_sources",
    "fetch_order_by_key1",
    "throw_order",
    "set_order_status",
    "release_order_to_home",
    "own_staff_uid",
    "change_order_price",
)


@contextmanager
def _all_upstreams_mocked():
    """把上面 8 个出口全打桩 ⇒ 任何一次上游调用都会在 `call_count == 0` 上现形。"""
    patchers = [mock.patch.object(jidanpu, name) for name in UPSTREAM_EXITS]
    mocks = {name: patcher.start() for name, patcher in zip(UPSTREAM_EXITS, patchers)}
    try:
        yield mocks
    finally:
        for patcher in patchers:
            patcher.stop()


def _no(n: int) -> str:
    """20 位订单号（`OrderViewSet.lookup_value_regex = r"\\d{20}"`）。"""
    return f"2026100400000000{n:04d}"


class _Fixture(TenantTestCase):
    """共用夹具：一款游戏、一个带下家绑定的代练员、若干张单与链路。"""

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="价格同步夹具游戏")
        self.taker = self._taker("price-sync-taker")
        self.binding = self._binding(self.taker)
        self.api = make_logged_in_client(
            username="price-sync-cs", groups=("客服",), nodes=NODES
        )

    # ── 账号 / 绑定 ────────────────────────────────────────────────
    def _taker(self, username, *, percent=20):
        user = get_user_model().objects.create_user(
            username=username, password="pw-price-sync"
        )
        StaffProfile.objects.create(
            user=user,
            commission_mode=CommissionMode.ORDER_PERCENT,
            commission_value=Decimal(str(percent)),
        )
        # ⭐ D-0252：`assign_order` 判「目标打手游戏范围」（无档案=无游戏=拒）
        #    ⇒ 本夹具造的打手一律持本类的游戏（专钉：tests/test_order_assign_game_gate.py）。
        grant_games(user, self.game)
        return user

    def _binding(self, user, *, enabled=True, percent=100,
                 base_url=BASE_URL, remote_user=None):
        return JidanpuMemberBinding.objects.create(
            user=remote_user or user,
            jidanpu_base_url=base_url,
            account=f"acc-{user.username}",
            password="pw-price-sync",
            source_name="团团通系统",
            enabled=enabled,
            throw_price_percent=percent,
        )

    def _order(self, no, *, status=OrderStatus.UNASSIGNED, taker=None,
               hall_price="88.88"):
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=None if hall_price is None else Decimal(hall_price),
            total_price=Decimal("100.00"),
            project_text="合成项目价格同步",
        )
        Performance.objects.create(
            order=order, user=taker, emp_type=EmpType.MAIN,
            temp_amount=Decimal("10.00"),
        )
        return order

    def _link(self, order, *, binding=None, remote_id="JDP-PRICE-0000000001",
              in_hall=False):
        return JidanpuMemberOrderLink.objects.create(
            binding=binding or self.binding,
            order=order,
            jidanpu_order_id=remote_id,
            in_hall=in_hall,
        )

    # ── 请求 ───────────────────────────────────────────────────────
    def _post_ok(self, url, payload):
        body = self.api.post(url, payload, content_type="application/json").json()
        self.assertEqual(body["code"], 0, body)
        return body

    # ── 台账 ───────────────────────────────────────────────────────
    def _queue(self, order, *, binding=None):
        return jidanpu_member.queue_member_event(
            order, binding or self.binding, kind=KIND_PRICE_LITERAL, rearm=True
        )

    def _price_events(self, order):
        return JidanpuSyncEvent.objects.filter(
            kind=KIND_PRICE_LITERAL, target_ref=order.order_no
        )


# ══ ① 结构：kind 的字面值 / 取活全集 / 消费者注册 ═══════════════════════════════


class KindRegistrationTests(_Fixture):
    def test_the_kind_literal_is_the_wire_value(self):
        self.assertEqual(jidanpu_member.KIND_PRICE, KIND_PRICE_LITERAL)

    def test_the_new_kind_is_in_the_outbox_set(self):
        """不在 `OUTBOX_KINDS` 里 ⇒ `run_outbox` 永远取不到它（静默不消费）。"""
        self.assertIn(jidanpu_member.KIND_PRICE, jidanpu_member.OUTBOX_KINDS)

    def test_the_consumer_is_registered(self):
        """注册表里缺一条 ⇒ 消费时 `ValueError: 本消费者不认识的台账类型`（响亮，但不是这里要的）。"""
        self.assertIs(
            jidanpu_member._HANDLERS[jidanpu_member.KIND_PRICE],
            jidanpu_member._consume_price,
        )


# ══ ② 触发口 `queue_price_update` ═════════════════════════════════════════════


class QueuePriceUpdateTests(_Fixture):
    def test_a_link_gets_one_event(self):
        order = self._order(_no(1))
        self._link(order)
        self.assertEqual(jidanpu_member.queue_price_update(order), 1)
        rows = self._price_events(order)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.direction, "out")
        self.assertEqual(row.target_type, "order")
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.payload["member_binding_id"], self.binding.pk)
        self.assertIs(type(row.payload["member_binding_id"]), int)

    def test_no_link_means_zero_events(self):
        """判据 ③：还没有录进去的单 ⇒ **零事件**（之后的录单自然用当前价，⛔ 不排空转行）。"""
        order = self._order(_no(2))
        self.assertEqual(jidanpu_member.queue_price_update(order), 0)
        self.assertEqual(self._price_events(order).count(), 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind=KIND_PRICE_LITERAL).count(), 0
        )

    def test_a_disabled_binding_gets_no_event(self):
        """停用的绑定不推（与 `queue_hall_launch` 的闸门口径一致）。"""
        order = self._order(_no(3))
        self._link(order, binding=self._binding(
            self._taker("price-sync-disabled"), enabled=False
        ))
        self.assertEqual(jidanpu_member.queue_price_update(order), 0)
        self.assertEqual(self._price_events(order).count(), 0)

    def test_two_links_each_get_an_event(self):
        """一张单可以有多条链路（不同下家系统）⇒ 各排一条，返回条数 = 启用中的条数。"""
        order = self._order(_no(4))
        self._link(order, remote_id="JDP-PRICE-0000000004A")
        self._link(order, binding=self._binding(
            self._taker("price-sync-other"), base_url="https://jidanpu-price-b.test.invalid"
        ), remote_id="JDP-PRICE-0000000004B")
        self.assertEqual(jidanpu_member.queue_price_update(order), 2)
        self.assertEqual(self._price_events(order).count(), 2)

    def test_queueing_touches_no_upstream_at_all(self):
        """⭐ 本函数是**纯排队口**：8 个上游出口**一个都不许碰**（评审逐条打桩验过）。

        判别力：把任何一次上游调用（或 `queue_member_event` 里将来接上的「顺手试一次」）
        挪进本函数 ⇒ 本用例当场红。
        """
        order = self._order(_no(6))
        self._link(order)
        with _all_upstreams_mocked() as stubs:
            self.assertEqual(jidanpu_member.queue_price_update(order), 1)
        for name, stub in stubs.items():
            self.assertEqual(stub.call_count, 0, f"{name} 被调到了")

    def test_a_burned_out_row_is_re_armed(self):
        """判据 ⑤：命中**已经烧完重试额度**的行 ⇒ `rearm` 把它复位（照既有 `rearm` 用例形状）。

        ⚠️ 判据钉**行上的四个值**（额度 / 状态 / 原因 / 退避锚），⛔ 不是「返回了同一行」
        ——后者正是「静默空转」那个 bug 的形状。
        """
        order = self._order(_no(5))
        self._link(order)
        event = self._queue(order)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status="failed", attempts=jidanpu_member.OUTBOX_MAX_ATTEMPTS,
            reason="改接单价时上游报错",
        )
        self.assertEqual(jidanpu_member.queue_price_update(order), 1)
        fresh = JidanpuSyncEvent.objects.get(pk=event.pk)
        self.assertEqual(fresh.attempts, 0)
        self.assertEqual(fresh.status, "pending")
        self.assertEqual(fresh.reason, "")
        self.assertIsNone(fresh.processed_at)


# ══ ③ 消费者 `_consume_price` 的两种形态 ══════════════════════════════════════


class ConcurrentPriceUpdateTests(_Fixture):
    def _replace_price_during_send(self, *, rejected):
        order = self._order(_no(95), status=OrderStatus.WAITING_GRAB)
        self._link(order, in_hall=True)
        url = f"{ORDERS}/{order.order_no}/hall-price/"
        self._post_ok(url, {"price": "77.77"})
        pending = self._price_events(order).get()

        def set_latest_price():
            self._post_ok(url, {"price": "88.88"})

        class OverlappingPrice(_PriceStub):
            replaced = False

            def change_order_price(self, client, *, remote_order_id, fee):
                if not self.replaced:
                    self.replaced = True
                    set_latest_price()
                    if rejected:
                        raise _upstream_failure()
                super().change_order_price(client, remote_order_id=remote_order_id, fee=fee)

        remote = OverlappingPrice(in_hall=True)
        with _stub(remote):
            jidanpu_member.consume_event(pending, client=CLIENT)
            latest = self._price_events(order).get()
            self.assertEqual((latest.status, latest.attempts, latest.reason), ("pending", 0, ""))
            jidanpu_member.consume_event(latest, client=CLIENT)

        latest.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("88.88"))
        self.assertEqual((remote.price, remote.rec_fee), (Decimal("88.88"), Decimal("88.88")))
        self.assertEqual(latest.status, "done")

    def _assert_a_superseded_price_timeout_does_not_fail_the_new_price(self, *, alarm):
        order = self._order(_no(96), status=OrderStatus.WAITING_GRAB)
        self._link(order, in_hall=True)
        url = f"{ORDERS}/{order.order_no}/hall-price/"
        self._post_ok(url, {"price": "77.77"})

        remote = _PriceStub(in_hall=True)

        def change_price_and_timeout(*args, **kwargs):
            self._post_ok(url, {"price": "88.88"})
            if alarm:
                raise jidanpu_member._OutboxEventTimeout
            return remote.change_order_price(*args, **kwargs)

        clock = mock.Mock()
        clock.monotonic.side_effect = (0, jidanpu_member.OUTBOX_EVENT_DEADLINE_SECONDS + 1)
        with (
            _stub(remote),
            mock.patch.object(jidanpu, "change_order_price", side_effect=change_price_and_timeout),
            mock.patch.object(jidanpu_member, "time", clock),
        ):
            stats = jidanpu_member.run_outbox(self.binding, now=datetime.now(timezone.utc), client=CLIENT)

        self.assertEqual((stats.failed, stats.deferred), (0, 1))
        pending = self._price_events(order).get()
        self.assertEqual((pending.status, pending.attempts), ("pending", 0))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")

    def test_a_superseded_price_alarm_is_not_reported_as_a_new_failure(self):
        self._assert_a_superseded_price_timeout_does_not_fail_the_new_price(alarm=True)

    def test_a_superseded_price_elapsed_timeout_is_not_reported_as_a_new_failure(self):
        self._assert_a_superseded_price_timeout_does_not_fail_the_new_price(alarm=False)

    def test_previous_success_does_not_finish_the_latest_price(self):
        self._replace_price_during_send(rejected=False)

    def test_previous_failure_does_not_delay_the_latest_price(self):
        self._replace_price_during_send(rejected=True)


class ConsumePriceNotInHallTests(_Fixture):
    """只录单（`in_hall=False`）：**只**改接单价，⛔ 一次抛单都不发。"""

    def test_price_is_sent_only_after_the_copy_is_active_again(self):
        order = self._order(_no(97), status=OrderStatus.WAITING_GRAB, hall_price="88.88")
        link = self._link(order, in_hall=False)
        self.assertEqual(jidanpu_member.queue_price_update(order), 1)
        event = self._price_events(order).get()
        link.withdrawn_at = datetime.now(timezone.utc)
        link.save(update_fields=["withdrawn_at"])
        remote = _PriceStub(price="55.55", in_hall=False)

        with _stub(remote):
            self.assertEqual(jidanpu_member.consume_event(event, client=CLIENT), "skipped")
            self.assertEqual(remote.calls, [])
            self.assertEqual(remote.price, Decimal("55.55"))

            link.withdrawn_at = None
            link.save(update_fields=["withdrawn_at"])
            self.assertEqual(jidanpu_member.queue_price_update(order), 1)
            current = self._price_events(order).get(status="pending")
            self.assertEqual(jidanpu_member.consume_event(current, client=CLIENT), "done")

        self.assertEqual(remote.price, Decimal("88.88"))

    def test_only_the_price_cell_is_written(self):
        order = self._order(_no(10), hall_price="88.88")
        link = self._link(order, in_hall=False)
        up = _PriceStub(price="0.00")
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "done")
        self.assertEqual(up.names(), ["change_order_price", "fetch_order_by_key1"])
        self.assertEqual(
            up.kwargs_of("change_order_price")[0]["fee"], Decimal("88.88")
        )
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(event.reason, "")
        # ⛔ 链路标记一个字都没动：它记的是「对方大厅里此刻有没有这张单」这个事实。
        link.refresh_from_db()
        self.assertFalse(link.in_hall)

    def test_a_readback_that_does_not_match_is_not_done(self):
        """判据 ②：替身「远端价没变」⇒ **必须** `failed`（⛔ 不许假绿成 `done`）。"""
        order = self._order(_no(11), hall_price="88.88")
        self._link(order, in_hall=False)
        up = _PriceStub(price="0.00", apply_change=False)
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.reason, PRICE_CONFIRM_LITERAL)

    def test_a_readback_that_cannot_be_read_is_not_done(self):
        """「读不到」≠「改好了」（E-0004 同族）：回读回 `None` 同样落 `failed`。"""
        order = self._order(_no(12), hall_price="88.88")
        self._link(order, in_hall=False)
        up = _PriceStub(price="88.88", readable=False)
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "failed")
        event.refresh_from_db()
        self.assertEqual(event.reason, PRICE_CONFIRM_LITERAL)

    def test_the_write_is_idempotent(self):
        """可重入：同一事件重跑写的是同一个价 ⇒ 两次都 `done`、两次写的 fee 一样。"""
        order = self._order(_no(13), hall_price="88.88")
        self._link(order, in_hall=False)
        up = _PriceStub(price="0.00")
        event = self._queue(order)

        with _stub(up):
            first = jidanpu_member.consume_event(event, client=CLIENT)
            JidanpuSyncEvent.objects.filter(pk=event.pk).update(
                status="pending", attempts=0, reason=""
            )
            event.refresh_from_db()
            second = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual((first, second), ("done", "done"))
        fees = [kw["fee"] for kw in up.kwargs_of("change_order_price")]
        self.assertEqual(fees, [Decimal("88.88"), Decimal("88.88")])


class ConsumePriceInHallTests(_Fixture):
    """还挂在对方大厅（`in_hall=True`）：接单价 + **按新价重抛**（改 `recFee`）两格都要跟。"""

    def test_both_cells_follow_and_the_link_flag_is_untouched(self):
        order = self._order(_no(20), hall_price="88.88")
        link = self._link(order, in_hall=True)
        # 抛单百分比 50 ⇒ 抛单价 = 88.88 × 50/100 = 44.44（⛔ 不是 88.88）。
        self.binding.throw_price_percent = 50
        self.binding.save(update_fields=["throw_price_percent"])
        # 远端此刻：还挂在厅里，`recFee` 是**旧价**（要能分辨「重抛生效」与「没重抛」）。
        up = _PriceStub(price="0.00", rec_fee="1.00", in_hall=True)
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "done")
        self.assertEqual(
            up.names(),
            ["change_order_price", "fetch_order_by_key1",
             "throw_order", "fetch_order_by_key1"],
        )
        # ① 接单价那一格 = `hall_price`（⛔ 不是打折后的 44.44）
        self.assertEqual(
            up.kwargs_of("change_order_price")[0]["fee"], Decimal("88.88")
        )
        # ② 抛单价那一格 = `hall_price × percent/100`
        self.assertEqual(up.kwargs_of("throw_order")[0]["fee"], Decimal("44.44"))
        self.assertEqual(up.rec_fee, Decimal("44.44"))
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        # ⛔ 链路标记一个字都没动（重抛是对**已经在大厅**的那张单改价，形态没变）。
        link.refresh_from_db()
        self.assertTrue(link.in_hall)

    def test_a_rethrow_that_does_not_land_is_not_done(self):
        """重抛后 `recFee` 没跟上 ⇒ `failed`（不是只看 `isThrowOrder == "Y"` 就算成功）。"""
        order = self._order(_no(21), hall_price="88.88")
        self._link(order, in_hall=True)
        # 写入接口不抛异常、`isThrowOrder` 也是 Y，但 **`recFee` 停在旧值** ⇒ 必须判失败。
        up = _PriceStub(price="0.00", rec_fee="1.00", in_hall=True, apply_throw=False)
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        # ⚠️ 逐字字面值（⛔ 不是 `assertIn("recFee", …)` 那种片段判据——片段证不了整句）。
        self.assertEqual(event.reason, PRICE_THROW_FEE_CONFIRM_LITERAL)

    def test_a_failed_price_cell_stops_before_the_rethrow(self):
        """接单价那一格没跟上 ⇒ **连抛单都不发**（否则等于拿一张价没对上的单去打扰对方）。"""
        order = self._order(_no(22), hall_price="88.88")
        self._link(order, in_hall=True)
        up = _PriceStub(price="0.00", in_hall=True, apply_change=False)
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "failed")
        self.assertEqual(up.names(), ["change_order_price", "fetch_order_by_key1"])

    def test_the_rethrow_fee_rounds_half_up_at_the_cent_boundary(self):
        """⭐ 抛单价的取整口径：`hall_price × percent/100` 之后**四舍五入到分**（`_throw_fee`）。

        ⚠️ 本类上面那条 `88.88 × 50% = 44.44` **恰好整除** ⇒ 它证的是「乘了百分比」，
        ⛔ 证不了取整方向。真正的判别力只在 `.005` 这种**正好半厘**的输入上：
        `0.01 × 50% = 0.005` ⇒ `ROUND_HALF_UP` 落 `0.01`（`ROUND_HALF_DOWN` / `HALF_EVEN`
        会落 `0.00`）⇒ 本用例红。判据打在**真实出口**上：`throw_order(fee=…)` 收到的
        那个值 + 重抛后回读的 `recFee` 都要是 `0.01`。
        """
        order = self._order(_no(23), hall_price="0.01")
        self._link(order, in_hall=True)
        self.binding.throw_price_percent = 50
        self.binding.save(update_fields=["throw_price_percent"])
        up = _PriceStub(price="0.01", rec_fee="0.00", in_hall=True)
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "done")
        self.assertEqual(up.kwargs_of("throw_order")[0]["fee"], Decimal("0.01"))
        self.assertEqual(up.rec_fee, Decimal("0.01"))


# ══ ③b 五条失败 / 报错分支（零覆盖档） ══════════════════════════════


class ConsumePriceFailureBranchTests(_Fixture):
    """`_consume_price` 里**每一条** `except` / 判据失败都要落 `failed` + 退避，⛔ 不许 `done`。

    评审读数：这五条分支**行为都对**（它用探针逐条证过），但**没有网**。本类逐条补上，
    判据一律是**整句** `event.reason`（逐字字面值，⛔ 不用 `assertIn` 片段）+
    `attempts == 1` + **零后续上游调用**（例如接单价那一格失败 ⇒ 一次 `throw_order` 都不许有）。
    """

    def test_a_rejected_price_write_is_failed_with_the_upstream_reason(self):
        """① 写接单价被上游拒 ⇒ `failed` + 那句口径（带上游 msg / 码），且⛔ 不继续回读。"""
        order = self._order(_no(70), hall_price="88.88")
        self._link(order, in_hall=False)
        up = _PriceStub(price="0.00", change_error=_upstream_failure())
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.reason, _with_upstream_reason(PRICE_FAILED_LITERAL))
        self.assertEqual(up.names(), ["change_order_price"])   # 零后续上游调用

    def test_a_failed_price_readback_is_failed_with_the_upstream_reason(self):
        """② 接单价**回读那一跳**报错 ⇒ 「判不了 ≠ 改好了」⇒ `failed`、⛔ 不落 `done`。"""
        order = self._order(_no(71), hall_price="88.88")
        self._link(order, in_hall=False)
        up = _PriceStub(price="88.88", fetch_error=_upstream_failure())
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(
            event.reason, _with_upstream_reason(PRICE_CONFIRM_READ_FAILED_LITERAL)
        )
        self.assertEqual(up.names(), ["change_order_price", "fetch_order_by_key1"])

    def test_a_failed_rethrow_is_failed_with_the_upstream_reason(self):
        """③ 重抛被上游拒 ⇒ `failed`，且⛔ 不再回读第二次（`recFee` 无从确认）。"""
        order = self._order(_no(72), hall_price="88.88")
        self._link(order, in_hall=True)
        up = _PriceStub(
            price="0.00", in_hall=True, throw_error=_upstream_failure()
        )
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.reason, _with_upstream_reason(THROW_FAILED_LITERAL))
        self.assertEqual(
            up.names(),
            ["change_order_price", "fetch_order_by_key1", "throw_order"],
        )

    def test_a_failed_rethrow_readback_is_failed_with_the_upstream_reason(self):
        """④ **重抛后**那一跳回读报错（第一跳正常）⇒ `failed`；两跳回读各一次。"""
        order = self._order(_no(73), hall_price="88.88")
        self._link(order, in_hall=True)
        up = _PriceStub(
            price="0.00", in_hall=True, fetch_error=[None, _upstream_failure()]
        )
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.reason, _with_upstream_reason(THROW_CONFIRM_LITERAL))
        self.assertEqual(
            up.names(),
            ["change_order_price", "fetch_order_by_key1",
             "throw_order", "fetch_order_by_key1"],
        )

    def test_a_rethrow_that_did_not_take_is_failed(self):
        """重抛前已确认在厅，写后回读离厅时仍判失败。"""
        order = self._order(_no(74), hall_price="88.88")
        self._link(order, in_hall=True)
        up = _PriceStub(price="0.00", in_hall=True, apply_throw=False)
        event = self._queue(order)

        def leave_hall(*args, **kwargs):
            up.throw_order(*args, **kwargs)
            up.is_throw = "N"

        with _stub(up), mock.patch.object(jidanpu, "throw_order", side_effect=leave_hall):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "failed")
        event.refresh_from_db()
        self.assertEqual(event.status, "failed")
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.reason, THROW_CONFIRM_LITERAL)
        self.assertEqual(
            up.names(),
            ["change_order_price", "fetch_order_by_key1",
             "throw_order", "fetch_order_by_key1"],
        )


class RemoteAmountValueTests(SimpleTestCase):
    """`_remote_amount_value` 的归一化口（判据与排障文案共用的那一格）。"""

    def test_a_numeric_json_number_becomes_a_decimal(self):
        self.assertEqual(
            jidanpu_member._remote_amount_value({"price": 44.44}, "price"),
            Decimal("44.44"),
        )

    def test_a_non_numeric_string_is_none(self):
        """非数值字符串 ⇒ `None`（覆盖 `except (InvalidOperation, ValueError)`）。

        判别力：去掉那个 `except` ⇒ 本用例抛 `InvalidOperation` 而不是断言失败。
        """
        self.assertIsNone(
            jidanpu_member._remote_amount_value({"price": "不是数字"}, "price")
        )

    def test_missing_null_and_bool_are_none(self):
        """缺键 / `null` / 布尔 ⇒ `None`（布尔那一条是刻意的：`True` 不是 1 元）。"""
        self.assertIsNone(jidanpu_member._remote_amount_value({}, "price"))
        self.assertIsNone(jidanpu_member._remote_amount_value({"price": None}, "price"))
        self.assertIsNone(jidanpu_member._remote_amount_value({"price": True}, "price"))


# ══ ④ 两种「不动作」的终态 ═══════════════════════════════════════════════════

class ConsumePriceSkipTests(_Fixture):
    def test_no_link_is_a_skip_with_a_human_reason_and_no_upstream(self):
        """判据 ③：没有链路 ⇒ `skipped` + 人话，且**零上游调用**。"""
        order = self._order(_no(30))
        up = _PriceStub(price="0.00")
        event = self._queue(order)  # 故意绕过 `queue_price_update` 造一条在飞事件

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "skipped")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(event.reason, PRICE_NO_LINK_LITERAL)
        self.assertEqual(up.calls, [])

    def test_a_missing_order_is_a_skip_with_no_upstream(self):
        """订单行已不在（本仓无硬删 ⇒ 只可能来自人工改库 / 数据损坏）⇒ `skipped`、零上游。"""
        event = JidanpuSyncEvent.objects.create(
            direction="out", kind=KIND_PRICE_LITERAL, target_type="order",
            target_ref=_no(31), status="pending",
            payload={"member_binding_id": self.binding.pk},
        )
        up = _PriceStub(price="0.00")
        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "skipped")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        self.assertEqual(up.calls, [])

    def test_a_missing_hall_price_is_a_skip_with_a_human_reason_and_no_upstream(self):
        """判据 ④：`hall_price is None` ⇒ `skipped` + 人话原因，**零上游调用**。

        ⛔ 不许把它当 0（那等于告诉下家这单不要钱），⛔ 也不许重试。
        """
        order = self._order(_no(32), hall_price=None)
        self._link(order, in_hall=False)
        up = _PriceStub(price="0.00")
        event = self._queue(order)

        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "skipped")
        event.refresh_from_db()
        self.assertEqual(event.status, "skipped")
        # ⚠️ 这一格钉的是**改价档自己的**那句（⛔ 不是指派档那句「…本次不录单：填上发单价后
        #    由对账补录」——本档是给已录进去的单改价、对账**不查价**）。
        self.assertEqual(event.reason, PRICE_NO_PRICE_LITERAL)
        self.assertEqual(event.attempts, 0)
        self.assertEqual(up.calls, [])


# ══ ⑤ 四个发单价写入口的接线（各一条） ═══════════════════════════════════════


class ChangeOrderPriceClientTests(SimpleTestCase):
    """Step 1 的客户端函数：**载荷**与**不写第二层判据**两件事。

    ⚠️ 真机复核（本机 dev 的活绑定；原始回包只在**不进 git** 的
    `.tools/recon/jidanpu-change-order-price-*.txt`）：`POST {orderId, fee}` ⇒
    `code=1`「成功修改1笔，失败0笔」，回读接单价字段 = `price`，`recFee` / `isThrowOrder`
    写前写后不变。本用例把**载荷**钉在本地（真机那条读数不依赖网络重跑）。⛔ 零真实网络。
    """

    def test_the_path_is_the_registered_one(self):
        self.assertEqual(
            jidanpu.CHANGE_ORDER_PRICE_PATH, "/OrderAjax/ChangeOrderPrice"
        )

    def test_the_payload_is_the_order_id_and_a_string_fee(self):
        client = mock.Mock()
        jidanpu.change_order_price(
            client, remote_order_id="JDP-PRICE", fee=Decimal("88.80")
        )
        client.call.assert_called_once_with(
            "POST", "/OrderAjax/ChangeOrderPrice",
            data={"orderId": "JDP-PRICE", "fee": "88.80"},
        )

    def test_a_failure_envelope_is_left_to_the_sdk(self):
        """成败**交给 SDK**（`risk=write` ⇒ `positive_code`）⇒ 本函数⛔ 不解析回包。

        判别力：`client.call` 回一个**失败信封**（`code=0`）而函数**不抛**——它压根不看回包。
        """
        client = mock.Mock()
        client.call.return_value = {"code": 0, "msg": "失败", "data": None}
        jidanpu.change_order_price(client, remote_order_id="JDP-X", fee="1.00")
        self.assertEqual(client.call.call_count, 1)


class WritePointTriggerTests(_Fixture):
    """E-0027 同族：「以后新加一个写价点会漏掉一边」——四个已知写口**逐一登记**。

    ⚠️ 打桩目标必须是**被查找的那个模块**（E-0029）：四个写口里三个写
    `jidanpu_member.queue_price_update(...)`（模块属性查找），`performance.py` 那一处是
    函数内 `from apps.business import jidanpu_member` 再属性查找 ⇒ 打
    `apps.business.jidanpu_member.queue_price_update` 都能拦到。
    """

    def _patched(self):
        return mock.patch.object(jidanpu_member, "queue_price_update")

    def test_hall_price_endpoint_calls_the_hook(self):
        """写口 #1：`/hall-price/`（列表工具栏「改发单价」）。"""
        order = self._order(_no(40), hall_price="50.00")
        with self._patched() as hook:
            self._post_ok(f"{ORDERS}/{order.order_no}/hall-price/", {"price": "66.00"})
        hook.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("66.00"))    # 前置哨兵：价真的写了

    def test_assign_with_a_price_calls_the_hook(self):
        """写口 #2：`/assign/` **带 `price`** 那一支。"""
        order = self._order(_no(41), hall_price="50.00")
        with self._patched() as hook:
            self._post_ok(
                f"{ORDERS}/{order.order_no}/assign/",
                {"user_id": self.taker.id, "price": "66.00"},
            )
        hook.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("66.00"))

    def test_assign_without_a_price_calls_no_hook(self):
        """同一端点的**不带价**那一支：⛔ 一次都不调（E-0021 的边界：`is not None`）。"""
        order = self._order(_no(42), hall_price="50.00")
        with self._patched() as hook:
            self._post_ok(
                f"{ORDERS}/{order.order_no}/assign/", {"user_id": self.taker.id}
            )
        hook.assert_not_called()

    def test_throw_to_hall_with_a_price_calls_the_hook(self):
        """写口 #3：`/throw-to-hall/` **带 `price`** 那一支（无接单人时才抛得动）。"""
        order = self._order(_no(43), status=OrderStatus.ISSUE, taker=None)
        with self._patched() as hook:
            self._post_ok(
                f"{ORDERS}/{order.order_no}/throw-to-hall/", {"price": "66.00"}
            )
        hook.assert_called_once()

    def test_throw_to_hall_without_a_price_calls_no_hook(self):
        """不带价那一支：⛔ 一次都不调（这一列没变，写了就是一条假事件）。"""
        order = self._order(_no(44), status=OrderStatus.ISSUE, taker=None)
        with self._patched() as hook:
            self._post_ok(f"{ORDERS}/{order.order_no}/throw-to-hall/", {})
        hook.assert_not_called()

    def test_adjust_amount_landing_on_hall_price_calls_the_hook(self):
        """写口 #4：`/adjust-amount/` 在**没有 `taker_price`** 时写回 `Order.hall_price`。

        提成率 20% ⇒ 反推基价 = `13.20 ÷ 20 × 100 = 66.00`，落点正是发单价那一列。
        """
        order = self._order(_no(45), hall_price="50.00", taker=self.taker)
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        with self._patched() as hook:
            self._post_ok(
                f"{PERFORMANCES}/{main.id}/adjust-amount/",
                {"amount_kind": "temp", "amount": "13.20"},
            )
        hook.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("66.00"))    # 前置哨兵：落点确实是发单价

    def test_adjust_amount_on_a_taker_price_override_calls_no_hook(self):
        """写口 #4 的**反向对照**：该行**已有 `taker_price` 覆盖价** ⇒ 落点是**绩效列**、⛔ 不调。

        判别力：把 `performance.taker_price is not None` 那一支写坏（一律落 `Order.hall_price`），
        本用例会红（hook 被调到 / 发单价那一列被改）。

        ⚠️ 这与 `test_adjust_amount_landing_on_hall_price_calls_the_hook` 是**一对**：
        只有两条都在，才能证明「落在哪一列」这个判据真的接上了（只留正向那条，
        一个「永远落 hall_price」的实现照样全绿）。
        """
        order = self._order(_no(46), hall_price="50.00", taker=self.taker)
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        main.taker_price = Decimal("40.00")
        main.save(update_fields=["taker_price"])

        with self._patched() as hook:
            self._post_ok(
                f"{PERFORMANCES}/{main.id}/adjust-amount/",
                {"amount_kind": "temp", "amount": "13.20"},
            )

        hook.assert_not_called()
        order.refresh_from_db()
        main.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("50.00"))    # 发单价那一列没动
        self.assertEqual(main.taker_price, Decimal("66.00"))    # 落点是绩效覆盖价那一列


# ══ ⑥ 端到端：真 HTTP 改价 ⇒ 台账 ⇒ 消费者收到正确的 fee ══════════════════════


class EndToEndTests(_Fixture):
    def test_changing_the_hall_price_reaches_the_consumer_with_the_right_fee(self):
        order = self._order(_no(50), hall_price="50.00")
        self._link(order, in_hall=False)
        self._post_ok(f"{ORDERS}/{order.order_no}/hall-price/", {"price": "66.00"})

        rows = self._price_events(order)
        self.assertEqual(rows.count(), 1)
        event = rows.get()
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.payload["member_binding_id"], self.binding.pk)

        up = _PriceStub(price="50.00")
        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "done")
        self.assertEqual(
            up.kwargs_of("change_order_price")[0]["fee"], Decimal("66.00")
        )
        event.refresh_from_db()
        self.assertEqual(event.status, "done")

    def test_the_whole_chain_also_works_through_run_outbox(self):
        """走**真正的取活口** `run_outbox`（不只 `consume_event`）：`OUTBOX_KINDS` 真的含它。"""
        order = self._order(_no(51), hall_price="50.00")
        self._link(order, in_hall=False)
        self._post_ok(f"{ORDERS}/{order.order_no}/hall-price/", {"price": "66.00"})

        up = _PriceStub(price="50.00")
        with _stub(up):
            stats = jidanpu_member.run_outbox(
                self.binding, now=None, client=CLIENT
            )

        self.assertEqual(stats.done, 1)
        self.assertEqual(
            up.kwargs_of("change_order_price")[0]["fee"], Decimal("66.00")
        )


# ══ ⑧ peer **入方向**也在改我方发单价 ⇒ 同样要跟到第三方鸡蛋铺（评审曾抓的漏点） ═════

#: peer 侧对端单号与两个时刻（`T1 < T2`；事件必须**严格晚于** `link.last_applied_at`）。
PEER_NO = "PEER-PRICE-1"
PEER_T1 = datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc)
PEER_T2 = datetime(2026, 10, 4, 11, 0, tzinfo=timezone.utc)


class PeerInboundPriceTriggerTests(_Fixture):
    """一张单**同时**挂两条链路：`PeerOrderLink`（上家团团通推给我）+ 我这条鸡蛋铺下家链路。

    ⚠️ 这就是评审论证的**可达性**：上家改价 ⇒ 入方向改我方 `hall_price` ⇒ 我下家那一格
    会停在旧价（业务方报的症状的另一扇门）。⛔ 这不是「peer 之间转发」——
    转发给下游租户仍是 peer 线明确的「本期不做转发」。
    """

    def _peer_binding(self, *, order):
        user = get_user_model().objects.create_user(
            username="price-sync-peer-owner", password="pw-price-sync"
        )
        binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上游工作室甲",
            peer_base_url="https://peer-price.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-price-local.test.invalid",
            bound_by=user,
        )
        PeerOrderLink.objects.create(
            order=order, upstream_binding=binding, peer_order_no=PEER_NO,
            last_applied_at=PEER_T1,
        )
        return binding

    def _peer_event(self, *, kind, hall_price):
        """一条**入方向**事件（同一张单的后续事件：带 `occurred_at` 才会被应用）。"""
        return {
            "kind": kind,
            "target_ref": PEER_NO,
            "occurred_at": PEER_T2.isoformat(),
            "payload": {
                "status": str(OrderStatus.IN_PROGRESS),
                "hall_price": hall_price,
                "game_name": self.game.name,
            },
        }

    def test_an_inbound_order_update_only_changes_the_customer_price(self):
        """⭐ **2026-10-26 改准（D-0307）**：入向 `order.update` 改的是**客户价**（`total_price`），
        **不动**我们给打手的 `hall_price`；因此**不再**排 `member.price`（我们推给下家的发单价没变，
        排一条只会把旧价重推一遍）。接手类事件（`order.assign` 那条分支）仍照抄发单价、仍排队。
        """
        order = self._order(_no(80), hall_price="50.00")
        self._link(order, in_hall=False)            # 我推给我下家的鸡蛋铺
        binding = self._peer_binding(order=order)

        result = apply_events(
            [self._peer_event(kind="order.update", hall_price="66.00")],
            binding=binding,
        )

        self.assertEqual(result.applied, 1, result)
        order.refresh_from_db()
        self.assertEqual(order.total_price, Decimal("66.00"))   # 客户价被改了
        self.assertEqual(order.hall_price, Decimal("50.00"))    # 打手价一格不动
        self.assertEqual(self._price_events(order).count(), 0)  # 发单价没变 ⇒ 不排 

    def test_an_inbound_assign_queues_member_price(self):
        """接手类事件（第二条写 `hall_price` 的分支）同样要排一条。"""
        order = self._order(_no(81), hall_price="50.00")
        self._link(order, in_hall=False)
        binding = self._peer_binding(order=order)

        result = apply_events(
            [self._peer_event(kind="order.assign", hall_price="66.00")],
            binding=binding,
        )

        self.assertEqual(result.applied, 1, result)
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("66.00"))
        self.assertEqual(self._price_events(order).count(), 1)

    def test_an_inbound_event_without_a_price_queues_nothing(self):
        """反向对照：**不带价**的入方向事件 ⛔ 不排（`is not None` 的边界，E-0021）。"""
        order = self._order(_no(82), hall_price="50.00")
        self._link(order, in_hall=False)
        binding = self._peer_binding(order=order)

        event = self._peer_event(kind="order.update", hall_price=None)
        result = apply_events([event], binding=binding)

        self.assertEqual(result.applied, 1, result)
        order.refresh_from_db()
        self.assertEqual(order.hall_price, Decimal("50.00"))
        self.assertEqual(self._price_events(order).count(), 0)


# ══ ⑦ 对照：不接桩时真实出口会被走到（E-0029 的「桩接没接上」判据） ═══════════


class StubWiringControlTests(_Fixture):
    def test_without_the_stub_the_real_client_function_is_used(self):
        """不接桩 ⇒ 消费者打到的是真 `jidanpu.change_order_price`（`CLIENT` 没有 `.call`）。

        判别力：`_stub` 没接到**被查找的模块**上时，本用例会**静默通过**
        （消费者碰不到真出口）⇒ 这条就是「桩接上了」的对照。
        """
        order = self._order(_no(60), hall_price="88.88")
        self._link(order, in_hall=False)
        event = self._queue(order)

        with self.assertRaises(AttributeError):
            jidanpu_member.consume_event(event, client=CLIENT)


# ══ ⑨ 触发点⑨的消费端联动（D-0326 / E-0301）══════════════════════════
#
# 排队那一半（PUT 旧值快照判变 + 三道闸）在 `test_jidanpu_member_triggers.py::PercentRepriceTests`；
# 本节钉的是**闭环的另一半**：绑定「抛单百分比」真的变了 ⇒ 排出的那条 `member.price`
# 由**既有消费者**按**新**百分比重抛钉值（`_throw_fee` 消费那一刻现算；消费端零改动）。
# ⚠️ 桩接线照既有 price 消费测试（`_PriceStub` 打在**被查找的模块** `apps.business.jidanpu`
# 上，E-0029）。

BINDING_URL = "/api/business/member/jidanpu-binding/"


class PercentRepriceConsumerLinkTests(_Fixture):
    def _percent_binding_with_in_hall_card(self, *, percent):
        """一份**已启用**、百分比=`percent` 的绑定（主人另建一个账号）＋ 一张
        在厅（`in_hall=True`）且本地待抢单的链路卡。回 `(owner_client, binding, order)`。"""
        owner_client = make_logged_in_client(username="price-sync-pct-owner")
        owner = get_user_model().objects.get(username="price-sync-pct-owner")
        #: ⚠️ 地址必须与夹具 `self.binding` 的不同：`(地址, 账号)` 有**部分唯一索引**
        #: （`mode="password"` 那条）⇒ 同址同号会当场 IntegrityError（夹具坏了必须响亮，
        #: 但响亮在 setUp 之外更难归因）——合成地址另起一个。
        binding = self._binding(
            self.taker, remote_user=owner, percent=percent,
            base_url="https://jidanpu-pct.test.invalid",
        )
        order = self._order(_no(90), status=OrderStatus.WAITING_GRAB, hall_price="88.88")
        self._link(order, binding=binding, remote_id="JDP-PRICE-0000000009", in_hall=True)
        return owner_client, binding, order

    def _change_percent(self, client, binding, percent):
        """真 HTTP：对本人那一行 PUT 新百分比（检测过桩；PUT 保持启用 ⇒ 不走启用补推）。"""
        payload = {
            "jidanpu_base_url": binding.jidanpu_base_url,
            "account": binding.account,
            "enabled": True,
            "throw_price_percent": percent,
        }
        with mock.patch.object(
            jidanpu_member,
            "detect_binding_with_login_identity",
            return_value=(jidanpu_member.DetectionReport(ok=True), None),
        ):
            res = client.put(BINDING_URL, payload, content_type="application/json")
        body = res.json()
        self.assertEqual(body["code"], 0, body)
        binding.refresh_from_db()
        self.assertEqual(binding.throw_price_percent, percent)

    def test_a_percent_change_put_ends_in_a_rethrow_at_the_new_percent(self):
        """⭐ 闭环：100→85 的 PUT ⇒ 一条 `member.price` ⇒ 消费后重抛费 = 88.88 × 85% = **75.55**。

        判别力（三段各自可红）：
        ① 排队段：摘掉视图里的触发调用 ⇒ 台账行数断言红（0 ≠ 1）；
        ② 消费段：把 `_member_rethrow_in_hall` 里的 `_throw_fee` 换成 `order.hall_price`
           （不吃百分比）⇒ `throw_order` 的 fee 断言红（88.88 ≠ 75.55）；
        ③ 接线段：桩没接在被查找的模块上 ⇒ `StubWiringControlTests` 同族的对照会红。

        ⚠️ 断言钉**值**（E-0024）：接单价那一格 = 我方发单价 88.88（与百分比无关），
        重抛那一格 = 88.88 × 85/100 = 75.548 → 四舍五入到分 **75.55**（HALF_UP）。
        """
        client, binding, order = self._percent_binding_with_in_hall_card(percent=100)

        self._change_percent(client, binding, 85)

        rows = self._price_events(order)
        self.assertEqual(rows.count(), 1)
        event = rows.get()
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.payload["member_binding_id"], binding.pk)

        #: 消费现场的远端态：旧价还挂着（接单价 66 / 抛单价 66）、单还 在厅（isThrowOrder=Y）。
        up = _PriceStub(price="66.00", rec_fee="66.00", in_hall=True)
        with _stub(up):
            outcome = jidanpu_member.consume_event(event, client=CLIENT)

        self.assertEqual(outcome, "done")
        self.assertEqual(
            up.kwargs_of("change_order_price")[0]["fee"], Decimal("88.88")
        )
        self.assertEqual(
            up.kwargs_of("throw_order")[0]["fee"], Decimal("75.55"),
            "重抛费必须按**改后**的百分比现算（88.88 × 85% = 75.55）",
        )
        event.refresh_from_db()
        self.assertEqual(event.status, "done")
        self.assertEqual(up.kwargs_of("throw_order")[0]["remote_order_id"], "JDP-PRICE-0000000009")
