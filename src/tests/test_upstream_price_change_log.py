"""上家订单改价记录（T2）：入向写入 + 只读端点 + 权限。

⚠️ **改准（D-0307）**：`order.update` 改价支现在动的是**客户价**
（`Order.total_price`）⇒ 该支的留痕记客户价旧→新（`note="上家改价（客户价）"`）。
接手类支（`order.assign` / `order.grab` / `order.withdraw`）仍照 P3/R10 落发单价
⇒ 那一支的留痕口径（发单价、`note="上家改价"`）**一个字未改**。

权威口径：`docs/specs/2026-10-17-order-log-names-and-upstream-price-log-design.md`
§3-D（改动清单）/ §5 判据 **12–18**（本文件逐条落）；计划 §2（T2 的七步）。

## 本文件的判据纪律（Global Constraints）

- 期望值写字面值（E-0024）；**否定断言前先给阳性对照**，否则「零行」可能由「这条路径
  根本没跑」凑出来 —— 所以每条写入用例都先断言 `Outcome.applied == 1` / 订单真的被建出来。
- 夹具一律**合成值**，且两租户用**不同值**的 `order_no` / `note` 指认（⛔ 不拿主键：
  两个 schema 的主键序列各自从 1 开始，`filter(id=…)` 会命中「另一个租户自己的那一行」）。
- 本文件的断言**只打价格记录行**，⛔ 不断言 `OperationLog.summary` 的逐字值——同一工作区
  还有 T1 在改 `apps/audit/log.py` 的渲染规则（计划 §7 的提醒），断言那边会互相踩。
"""
import ast
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business.models import (
    Game,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    UpstreamPriceChangeLog,
)
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import hash_token, new_token
from apps.platform.provisioning import provision_tenant
from tests.p5_auth_helpers import make_logged_in_client

#: 端点路径（spec §D3；**带尾斜杠**，与 `apps/business/urls.py` 的显式 `path()` 逐字一致）。
ENDPOINT = "/api/business/upstream-price-change-logs/"

#: 本页的准入节点（spec §D3 / §D5）。
NODE = "order.upstream_price_change_logs"

#: 对端单号（`PeerOrderLink` 的去重键）与本地订单号（形状照 `order_no.py`，值本身无意义）。
PEER_NO = "A-1"
LOCAL_NO = "20260926000000000041"

#: 端点的行形状（spec §D3 的 9 键 + D-0301 加的 `source_order_no`）——
#: **恰好 10 个键**（判据 16；⛔ 不放宽成 issubset）。
ROW_KEYS = {
    "order_no",
    "source_order_no",
    "game_name",
    "game_role_name",
    "before_amount",
    "after_amount",
    "source_name",
    "note",
    "occurred_at",
    "is_deleted",
}

#: `Asia/Shanghai` 无夏令时 ⇒ 固定 `+08:00` 与 `ZoneInfo("Asia/Shanghai")` 是**同一瞬时**，
#: 且不依赖本机 tzdata（与 `tests/test_order_action_log_api.py` 同源）。
CST = dt_timezone(timedelta(hours=8))

#: `apps/` 的绝对路径（判据 14 的源码扫描根）。
APPS_DIR = Path(__file__).resolve().parent.parent / "apps"


def local(*args):
    """按本地时区（Asia/Shanghai）构造一个瞬时。"""
    return datetime(*args, tzinfo=CST)


def rows():
    """本租户的全部改价记录，按写入顺序（`id`）——比 `Meta.ordering` 更适合当断言基准。"""
    return list(UpstreamPriceChangeLog.objects.order_by("id"))


class InboundWriteTests(TenantTestCase):
    """判据 12–14：**只有** peer_inbound 的后续事件分支写、且只在**值真的变了**时写。

    夹具照 `tests/test_peer_inbound_update.py`：一条本地单 + 一条已应用的链行
    （`last_applied_at = T1`），于是 `T2 / T3` 的事件都会被真的应用（不是 stale 跳过）。
    """

    #: 三个时刻：`T1 < T2 < T3`；`T1` 是本地已应用到的时刻。
    T1 = datetime(2026, 10, 17, 10, 0, tzinfo=dt_timezone.utc)
    T2 = datetime(2026, 10, 17, 11, 0, tzinfo=dt_timezone.utc)
    T3 = datetime(2026, 10, 17, 12, 0, tzinfo=dt_timezone.utc)

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="上下游改价夹具游戏")
        self.kefu = get_user_model().objects.create_user(
            username="upl-price-kefu", password="pw-upl-price"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室改价",
            peer_base_url="https://peer-price-a.test.invalid",
            peer_account="peer-account-price-a",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-price-b.test.invalid",
            bound_by=self.kefu,
        )
        self.order = Order.objects.create(
            order_no=LOCAL_NO,
            game=self.game,
            status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("100.00"),
            # 本线只记**发单价**：初始 10.00（判据 12 的「改前」）。
            hall_price=Decimal("10.00"),
            game_role_name="测试角色甲",
            source_order_no=PEER_NO,
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.binding,
            peer_order_no=PEER_NO,
            last_applied_at=self.T1,
        )

    # ── 夹具构造器 ────────────────────────────────────────────────────────
    def _event(self, *, kind="order.update", occurred_at=None, ref=PEER_NO, **payload_over):
        payload = {
            "status": OrderStatus.IN_PROGRESS,
            "total_price": "100.00",
            "game_name": self.game.name,
            "hall_price": "12.00",
        }
        payload.update(payload_over)
        event = {"kind": kind, "target_ref": ref, "payload": payload}
        if occurred_at is not None:
            event["occurred_at"] = occurred_at.isoformat()
        return event

    # ── 判据 12 ──────────────────────────────────────────────────────────
    def test_an_order_update_that_changes_the_price_writes_exactly_one_row(self):
        """判据 12（⭐ D-0307 改准，2026-10-26）：`order.update` 改价 ⇒ 记**客户价**旧→新。

        - `order.update` 支改的是 `total_price`（客户价 / 进账侧）⇒ 台账记 100 → 12；
          `hall_price`（打手价）**一格不动**（仍是 10.00）；
        - 留痕的 `note` 写明「客户价」，与接手类支（记发单价、note「上家改价」）分得开。
        """
        outcome = apply_events(
            [self._event(occurred_at=self.T2, hall_price="12.00")],
            binding=self.binding,
        )
        # 阳性对照：事件真的被应用了（否则下面的「一行」可能由「什么都没跑」凑出来）。
        self.assertEqual((outcome.applied, outcome.skipped), (1, 0), outcome)

        logged = rows()
        self.assertEqual(len(logged), 1, [(r.order_no, r.before_amount, r.after_amount) for r in logged])
        row = logged[0]
        self.assertEqual(row.order_no, LOCAL_NO)
        self.assertEqual(row.game_name, "上下游改价夹具游戏")
        self.assertEqual(row.game_role_name, "测试角色甲")
        self.assertEqual(row.before_amount, Decimal("100.00"))
        self.assertEqual(row.after_amount, Decimal("12.00"))
        self.assertEqual(row.source_name, "测试工作室改价")
        self.assertEqual(row.note, "上家改价（客户价）")
        self.assertEqual(row.occurred_at, self.T2)
        # 订单本身：客户价改了、打手价没动（与留痕同生共死）。
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("12.00"))
        self.assertEqual(self.order.hall_price, Decimal("10.00"))

    def test_the_first_price_from_nothing_to_something_keeps_a_null_before(self):
        """首次从「无价」变有价：**记一行**、`before_amount` 为 `null`（spec §6-4 的口径）。

        ⭐ D-0307：`order.update` 支记的是**客户价**，而 `Order.total_price` 是**非空**列
        （客户价没有「无价」这一档）⇒ 这一条改在**接手类支**上钉：那一支仍按 P3/R10 照抄
        **发单价**（`hall_price` 可空），「从无价到有价」仍然是它的形状。
        """
        Order.all_objects.filter(order_no=LOCAL_NO).update(hall_price=None)
        outcome = apply_events(
            [self._event(kind="order.assign", occurred_at=self.T2, hall_price="8.00")],
            binding=self.binding,
        )
        self.assertEqual((outcome.applied, outcome.skipped), (1, 0), outcome)
        logged = rows()
        self.assertEqual(len(logged), 1, logged)
        self.assertIsNone(logged[0].before_amount)
        self.assertEqual(logged[0].after_amount, Decimal("8.00"))
        self.assertEqual(logged[0].note, "上家改价")

    def test_amounts_are_compared_at_the_column_scale(self):
        """对端发多位小数时，按**列精度（两位、四舍五入）**比较与落库。

        `Order.hall_price` 是 `numeric(12,2)`：`12.001` 落库后是 `12.00`。不先折精度，
        留痕会写出一行「库里的价没变、正文却是 12.00 → 12.00」的噪声（评审实测的边界）。
        ⚠️ 判别力：把 `_normalized_amount` 的 `quantize` 去掉 ⇒ 第一条事件会写一行、本用例红；
        把舍入从 `ROUND_HALF_UP` 换成默认的 `ROUND_HALF_EVEN` ⇒ 第二条的 `12.005` 会落成
        `12.00` 而不是库里的 `12.01`，断言 `after==12.01` 红。
        """
        # 先把库里的**客户价**改成 12.00：这样 12.001 折到列精度后与它**同价**（判据才有判别力）。
        # ⭐ D-0307：`order.update` 支比较与记录的都是 `total_price`。
        Order.all_objects.filter(pk=self.order.pk).update(total_price=Decimal("12.00"))
        first = apply_events(
            [self._event(occurred_at=self.T2, hall_price="12.001")],
            binding=self.binding,
        )
        self.assertEqual((first.applied, first.skipped), (1, 0), first)
        self.assertEqual(rows(), [], "12.001 与库里的 12.00 同价 ⇒ 不应留痕")
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("12.00"))

        second = apply_events(
            [self._event(occurred_at=self.T3, hall_price="12.005")],
            binding=self.binding,
        )
        self.assertEqual((second.applied, second.skipped), (1, 0), second)
        logged = rows()
        self.assertEqual(len(logged), 1, logged)
        self.assertEqual(logged[0].before_amount, Decimal("12.00"))
        # 四舍五入（half-up）：12.005 → 12.01，与 PostgreSQL numeric(12,2) 的落库值一致。
        self.assertEqual(logged[0].after_amount, Decimal("12.01"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_price, Decimal("12.01"))

    def test_a_handover_event_that_changes_the_price_also_writes_one_row(self):
        """接手类分支（`order.assign` / `grab` / `withdraw`）**同样**留痕（spec §3-D2 第 2 条）。"""
        outcome = apply_events(
            [self._event(kind="order.assign", occurred_at=self.T2, hall_price="15.00")],
            binding=self.binding,
        )
        self.assertEqual((outcome.applied, outcome.skipped), (1, 0), outcome)
        logged = rows()
        self.assertEqual(len(logged), 1, logged)
        self.assertEqual(logged[0].before_amount, Decimal("10.00"))
        self.assertEqual(logged[0].after_amount, Decimal("15.00"))
        self.assertEqual(logged[0].occurred_at, self.T2)
        # 阳性对照：这一支真的改了订单的价（不是只写了留痕）。
        self.order.refresh_from_db()
        self.assertEqual(self.order.hall_price, Decimal("15.00"))

    # ── 判据 13 ──────────────────────────────────────────────────────────
    def test_the_same_price_twice_writes_nothing_extra(self):
        """判据 13 前半：**同价重复事件 ⇒ 零行**（载荷恒带价，不比较就会刷屏）。

        判别力：把 helper 里 `_amounts_equal` 那条判据删掉 ⇒ 第二次事件会写第二行、本用例红。
        ⚠️ 比较必须先把 `before`（库里的 `Decimal("12.00")`）与 `after`（对端 JSON 的
        `"12.00"` **字符串**）归一到 `Decimal`；直接 `==` 会让这条判据恒假（判据 13 假红）。
        """
        first = apply_events(
            [self._event(occurred_at=self.T2, hall_price="12.00")],
            binding=self.binding,
        )
        self.assertEqual((first.applied, first.skipped), (1, 0), first)
        self.assertEqual(len(rows()), 1)   # 阳性对照：第一次真的写了一行

        second = apply_events(
            [self._event(occurred_at=self.T3, hall_price="12.00")],
            binding=self.binding,
        )
        # 第二条事件也是**真的被应用**的（否则「零行」可能由 stale 跳过凑出来）。
        self.assertEqual((second.applied, second.skipped), (1, 0), second)
        logged = rows()
        self.assertEqual(len(logged), 1, [(r.before_amount, r.after_amount) for r in logged])

    def test_a_new_order_writes_no_row(self):
        """判据 13 后半：**建单支不写**（没有「改前」）。"""
        outcome = apply_events(
            [
                self._event(
                    kind="order.assign",
                    occurred_at=self.T1,
                    ref="A-2",
                    hall_price="20.00",
                )
            ],
            binding=self.binding,
        )
        self.assertEqual((outcome.applied, outcome.skipped), (1, 0), outcome)
        # 阳性对照：那张单真的建出来了（并且带着对端此刻的价）。
        created = Order.objects.get(source_order_no="A-2")
        self.assertEqual(created.hall_price, Decimal("20.00"))
        self.assertEqual(rows(), [])

    def test_an_event_without_a_price_writes_no_row(self):
        """载荷没带 `hall_price` ⇒ 不写（⛔ 不把「这条事件没提价」记成「改成空」）。"""
        event = self._event(occurred_at=self.T2)
        del event["payload"]["hall_price"]
        outcome = apply_events([event], binding=self.binding)
        self.assertEqual((outcome.applied, outcome.skipped), (1, 0), outcome)
        self.assertEqual(rows(), [])

    # ── 判据 14 ──────────────────────────────────────────────────────────
    def test_the_production_write_path_of_the_price_log_is_only_peer_inbound(self):
        """判据 14：`UpstreamPriceChangeLog` 的**生产写入**只允许出现在一个文件里。

        口径（AST，⛔ 不用正则：注释 / docstring 里也有这类字样，正则会假绿）：

        1. 任何 `Call` 的目标里出现 `UpstreamPriceChangeLog` 且以 `create` / `bulk_create` /
           `update_or_create` / `get_or_create` / `update` / `save` 结尾 ⇒ 记该文件；
        2. 「先建实例再 `log.save()`」那种形态：某个**函数作用域**里既出现
           `UpstreamPriceChangeLog` 又调了 `.save()` ⇒ 记该文件。

        期望 = **恰好** `{apps/business/peer_inbound.py}`；鸡蛋铺 / 开放平台 / 闲管家 /
        本地上改价都不许写（业务方「只展示团团通↔团团通」）。
        """
        found = _upstream_price_log_write_files()
        self.assertEqual(
            found,
            {"business/peer_inbound.py"},
            "上家改价记录的生产写入只允许出现在 apps/business/peer_inbound.py；"
            f"实际命中：{sorted(found)}",
        )


#: 判据 14 关注的写入方法名（`objects.create` / `bulk_create` / `.save()` 三条都在内）。
_WRITE_METHODS = (
    "create",
    "bulk_create",
    "update_or_create",
    "get_or_create",
    "update",
    "save",
)


def _upstream_price_log_write_files() -> set[str]:
    """扫 `apps/**/*.py` 的 AST，返回**写** `UpstreamPriceChangeLog` 的文件（相对 `apps/`）。

    见 `InboundWriteTests.test_the_production_write_path_of_the_price_log_is_only_peer_inbound`
    的口径说明。⚠️ 只扫**可执行**的 `Call` / 函数体，注释与裸字符串语句不参与
    （`--name` 生成的迁移里有一个 `CreateModel(name='UpstreamPriceChangeLog')`，它是
    **字符串常量**，不是写入 ⇒ 不会误判）。
    """
    found: set[str] = set()
    for path in sorted(APPS_DIR.rglob("*.py")):
        rel = path.relative_to(APPS_DIR).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                try:
                    target = ast.unparse(node.func)
                except Exception:  # pragma: no cover - unparse 对合法 AST 不会抛
                    continue
                if "UpstreamPriceChangeLog" in target and target.endswith(
                    tuple(f".{method}" for method in _WRITE_METHODS)
                ):
                    found.add(rel)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                try:
                    body = ast.unparse(node)
                except Exception:  # pragma: no cover
                    continue
                if "UpstreamPriceChangeLog" in body and any(
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "save"
                    for inner in ast.walk(node)
                ):
                    found.add(rel)
    return found


class PriceLogApiTests(TenantTestCase):
    """判据 16–18 + 端点契约（形状 / 过滤 / 分页 / 排序 / 只读 / 空参数）。"""

    def setUp(self):
        # ⚠️ 把连接显式钉回本租户（跨域名请求不会在响应结束时还原 schema）。
        connection.set_tenant(self.tenant)
        self.game = Game.objects.create(name="改价记录读面游戏")
        self.order = Order.objects.create(
            order_no=LOCAL_NO,
            game=self.game,
            total_price=Decimal("100.00"),
            hall_price=Decimal("12.00"),
            game_role_name="读面角色甲",
        )
        self.client = make_logged_in_client(
            username="upl-price-viewer", groups=("客服",), nodes=(NODE,)
        )

    # ── 夹具构造器 ────────────────────────────────────────────────────────
    @staticmethod
    def _log(order_no=LOCAL_NO, occurred_at=None, note="上家改价", **over):
        defaults = {
            "order_no": order_no,
            "game_name": "改价记录读面游戏",
            "game_role_name": "读面角色甲",
            "before_amount": Decimal("10.00"),
            "after_amount": Decimal("12.00"),
            "source_name": "测试上家甲",
            "note": note,
            "occurred_at": occurred_at or local(2026, 10, 17, 9, 0),
        }
        defaults.update(over)
        return UpstreamPriceChangeLog.objects.create(**defaults)

    def _data(self, **params):
        query = "&".join(f"{key}={value}" for key, value in params.items())
        response = self.client.get(f"{ENDPOINT}?{query}" if query else ENDPOINT)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body["data"]

    # ── 判据 16：行形状 + is_deleted ──────────────────────────────────────
    def test_the_row_has_exactly_nine_keys_and_flags_deleted_orders(self):
        """判据 16：**恰好 9 个键**；软删单 `true`、正常单 `false`、订单不存在 `true`。

        ⚠️ **如实登记一条等价变异**（本轮实测）：把视图里的 `Order.all_objects` 换成默认
        manager（`Order.objects`）**不会**让本用例红——已软删的那张单会被默认 manager 排除，
        于是映射里没有它、`is_deleted` 走「查不到 ⇒ `True`」的兜底，**结论与按
        `deleted_at is not None` 判出来的相同**（两条路在这一格上观测等价）。
        ⇒ 「必须用 `all_objects`」这条口径由**结构网**钉住，⛔ 不由本用例钉：
        `tests/test_order_read_path_net.py::ALL_OBJECTS_SITES` 里登记了
        `business/api/upstream_price_log_views.py::_deleted_order_nos` 的 1 处
        （改成默认 manager ⇒ 那处 `all_objects` 消失 ⇒ 那条网红）。
        本用例钉的是**映射的取值方向**：把「`deleted_at is not None`」写反成 `False`
        ⇒ 软删那一行会变成 `false`，当场红。
        """
        soft_no = "20260926000000000042"
        ghost_no = "20260926000000000043"
        Order.objects.create(
            game=self.game, order_no=soft_no, total_price=Decimal("1.00")
        )
        Order.all_objects.filter(order_no=soft_no).update(deleted_at=timezone.now())

        self._log(order_no=LOCAL_NO, note="正常单的改价", occurred_at=local(2026, 10, 17, 9, 0))
        self._log(order_no=soft_no, note="软删单的改价", occurred_at=local(2026, 10, 17, 9, 1))
        self._log(order_no=ghost_no, note="订单不存在的改价", occurred_at=local(2026, 10, 17, 9, 2))

        data = self._data()
        self.assertEqual(data["total"], 3, data)   # 非空哨兵
        items = data["items"]
        self.assertEqual(len(items), 3, items)

        for row in items:
            self.assertEqual(set(row.keys()), ROW_KEYS, row)

        by_note = {row["note"]: row for row in items}
        self.assertIs(by_note["正常单的改价"]["is_deleted"], False)
        self.assertIs(by_note["软删单的改价"]["is_deleted"], True)
        # ⚠️ 订单行**不存在**也算「已不可查看」（spec §D3 末句）。
        self.assertIs(by_note["订单不存在的改价"]["is_deleted"], True)

    def test_amounts_are_decimal_strings_and_may_be_null(self):
        """`before_amount` / `after_amount` 是 DRF 默认的**十进制字符串**，可 `null`。"""
        self._log(
            occurred_at=local(2026, 10, 17, 9, 0),
            before_amount=None,
            after_amount=Decimal("8.00"),
        )
        rows_data = self._data()["items"]
        self.assertEqual(len(rows_data), 1, rows_data)   # 非空哨兵
        self.assertIsNone(rows_data[0]["before_amount"])
        self.assertEqual(rows_data[0]["after_amount"], "8.00")

    # ── 判据 17：时间含当天 / 模糊 / 分页 total ──────────────────────────
    def test_the_end_date_includes_the_whole_day(self):
        """判据 17 前半：`etime` 只给日期 ⇒ **含当天**（造一条当天 23:59 的记录仍命中）。"""
        self._log(note="当天最后一分钟的改价", occurred_at=local(2026, 10, 4, 23, 59))
        self._log(note="早一天的改价", occurred_at=local(2026, 10, 3, 23, 59))

        data = self._data(stime="2026-10-04", etime="2026-10-04")
        self.assertEqual(data["total"], 1, data)
        self.assertEqual([row["note"] for row in data["items"]], ["当天最后一分钟的改价"])

        # 正向对照：不带筛选时两行都在（否则上面的「1」可能由别的条件凑出来）。
        self.assertEqual(self._data()["total"], 2)

    def test_the_key_filters_by_order_no_substring(self):
        """判据 17 中段：`key` 按 `order_no` 模糊（`icontains`）。"""
        other_no = "20260926000000000099"
        self._log(order_no=LOCAL_NO, note="目标单的改价", occurred_at=local(2026, 10, 17, 9, 0))
        self._log(order_no=other_no, note="别的单的改价", occurred_at=local(2026, 10, 17, 9, 1))

        data = self._data(key="000000000041")
        self.assertEqual(data["total"], 1, data)
        self.assertEqual([row["order_no"] for row in data["items"]], [LOCAL_NO])

    def test_the_pagination_total_is_the_whole_filtered_set(self):
        """判据 17 后半：分页 `total` 是**筛选后的全集**，不是当前页的行数。"""
        for index in range(5):
            self._log(
                note=f"第 {index} 条改价",
                occurred_at=local(2026, 10, 17, 9, index),
            )
        data = self._data(page=1, page_size=2)
        self.assertEqual(data["total"], 5, data)
        self.assertEqual(len(data["items"]), 2, data["items"])
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 2)

    def test_rows_are_ordered_by_occurred_at_desc_then_id_desc(self):
        """端点固定排序 `-occurred_at, -id`（同一时刻的 tie-break 是 id 倒序）。"""
        same = local(2026, 10, 17, 9, 30)
        self._log(note="最旧", occurred_at=local(2026, 10, 17, 9, 0))
        self._log(note="同刻先写", occurred_at=same)
        self._log(note="同刻后写", occurred_at=same)

        notes = [row["note"] for row in self._data()["items"]]
        self.assertEqual(notes, ["同刻后写", "同刻先写", "最旧"], notes)

    # ── 查询数：is_deleted 一次批量 ───────────────────────────────────────
    def test_a_page_of_rows_costs_a_single_order_lookup(self):
        """当前页 N 行只发**一次** `business_order` 的 `order_no IN` 查询（⛔ 不逐行查）。

        判别力：把批量映射挪进 `SerializerMethodField` 逐行 `Order.all_objects.get(...)`
        ⇒ 这条 `IN` 查询消失、计数 0 ≠ 1 ⇒ 当场红。
        ⚠️ 只数含 `business_order` **且**带 `"order_no" IN` 的查询：权限判据（`user_node_codes`）
        与会话查询自己也会碰别的表，把总数当判据会掺进无关噪声。
        """
        for index in range(3):
            self._log(
                order_no=f"2026092600000000010{index}",
                note=f"第 {index} 条改价",
                occurred_at=local(2026, 10, 17, 9, index),
            )
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(ENDPOINT)
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(len(body["data"]["items"]), 3, body)   # 非空哨兵：三行都在

        lookups = [
            query["sql"]
            for query in ctx.captured_queries
            if '"business_order"' in query["sql"] and '"order_no" IN' in query["sql"]
        ]
        self.assertEqual(
            len(lookups), 1,
            f"一页 3 行只许一次 business_order 批量查询，实际 {len(lookups)} 次：{lookups}",
        )

    # ── 空参数 / 非法参数（E-0010）────────────────────────────────────────
    def test_empty_filter_values_are_treated_as_not_provided(self):
        """前端清空筛选栏会送空串——那是「没选」，不是非法值（判据 17 的邻居，E-0010）。"""
        self._log(occurred_at=local(2026, 10, 17, 9, 0))
        data = self._data(stime="", etime="", key="")
        self.assertEqual(data["total"], 1, data)

    def test_a_malformed_date_is_200_with_code_1000(self):
        """非法日期 ⇒ **200 + `code=1000`**（⛔ 不是 500、也不是静默空集）。"""
        body = self.client.get(f"{ENDPOINT}?stime=not-a-date").json()
        self.assertEqual(body["code"], 1000, body)
        body = self.client.get(f"{ENDPOINT}?etime=2026-13-45").json()
        self.assertEqual(body["code"], 1000, body)

    def test_an_overlong_key_is_rejected_without_500(self):
        """超长 `key`（> `order_no` 列宽的 20）⇒ 200 + `code=1000`。"""
        body = self.client.get(f"{ENDPOINT}?key={'9' * 21}").json()
        self.assertEqual(body["code"], 1000, body)
        # 边界：恰好 20 字仍然合法（否则「上限」被写成 19 也没人发现）。
        self.assertEqual(self.client.get(f"{ENDPOINT}?key={'9' * 20}").json()["code"], 0)

    # ── 只读 ─────────────────────────────────────────────────────────────
    def test_the_endpoint_writes_nothing(self):
        """端点只读：⛔ 不写审计行、⛔ 不写业务数据（价格记录 / 订单都逐字不变）。"""
        self._log(occurred_at=local(2026, 10, 17, 9, 0))
        before = (
            UpstreamPriceChangeLog.objects.count(),
            OperationLog.objects.count(),
            Order.all_objects.get(order_no=LOCAL_NO).updated_at,
        )
        data = self._data()
        self.assertEqual(data["total"], 1, data)   # 非空哨兵：真的读到了
        after = (
            UpstreamPriceChangeLog.objects.count(),
            OperationLog.objects.count(),
            Order.all_objects.get(order_no=LOCAL_NO).updated_at,
        )
        self.assertEqual(before, after)


class PriceLogPermissionTests(TenantTestCase):
    """判据 18：持 `order.upstream_price_change_logs` 的账号 200；不持 ⇒ 403（带阳性对照）。"""

    def setUp(self):
        connection.set_tenant(self.tenant)

    def test_a_user_holding_the_node_gets_200(self):
        """阳性对照：**只**持本页节点就够（⛔ 不需要 `order.list` 之类的其它节点）。"""
        client = make_logged_in_client(
            username="upl-price-holder", groups=("客服",), nodes=(NODE,)
        )
        response = client.get(ENDPOINT)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], 0)

    def test_a_user_without_the_node_gets_403(self):
        """不持节点 ⇒ 403（本仓的准入失败口径：**HTTP 403 + `code=403`**）。

        ⚠️ 本仓的 `code` 段里 `1006` 是「绩效状态不允许结账」（`ErrorCode.SETTLE_STATE_INVALID`），
        不是权限码；权限 / 认证类失败一律**保留 HTTP 状态码当 code**
        （`apps/core/exceptions.py` 的模块 docstring 第 1 条）。判据 18 要的是
        「**不是 200**」，本仓的等价形态是 `403 + code=403`。

        ⚠️ 阳性对照：**同一个组、只多一个节点**的账号立刻 200 ⇒ 403 不是「端点坏了」。
        """
        denied = make_logged_in_client(username="upl-price-denied", groups=("客服",), nodes=())
        response = denied.get(ENDPOINT)
        self.assertEqual(response.status_code, 403, response.content[:300])
        self.assertEqual(response.json()["code"], 403, response.json())

        allowed = make_logged_in_client(
            username="upl-price-allowed", groups=("客服",), nodes=(NODE,)
        )
        self.assertEqual(allowed.get(ENDPOINT).status_code, 200)


#: 跨租户夹具的两个 schema / 域名（与 `tests/test_order_recycle.py` 的同族用例同形）。
SCHEMA_B = "upl_price_iso_b"
DOMAIN_B = "upl-price-iso-b.test.local"
#: 两租户**不同值**的订单号与说明（判据 15：⛔ 不拿主键指认别家行）。
CROSS_A_NO = "20261017000000000301"
CROSS_B_NO = "20261017000000000302"
CROSS_A_NOTE = "A 租户自己的改价"
CROSS_B_NOTE = "B 租户自己的改价"


class PriceLogCrossTenantTests(TenantTestCase):
    """判据 15：A 租户的改价行在 B 租户端点里**查不到**，反之亦然。

    ⚠️ 指认用**租户内唯一、两租户不同值**的 `order_no` / `note`（⛔ 不用主键：
    两个 schema 的主键序列各自从 1 开始）。
    ⚠️ 跨域名请求**不会**在响应结束时还原连接上的 schema ⇒ 每条 B 侧操作都用
    `_b_call()` 的 `try/finally` 把连接钉回租户 A。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="上家改价隔离租户 B", domain=DOMAIN_B
        )

    def setUp(self):
        connection.set_tenant(self.tenant)
        UpstreamPriceChangeLog.objects.create(
            order_no=CROSS_A_NO,
            game_name="A 的游戏",
            game_role_name="A 的角色",
            before_amount=Decimal("10.00"),
            after_amount=Decimal("11.00"),
            source_name="A 的上家",
            note=CROSS_A_NOTE,
            occurred_at=local(2026, 10, 17, 9, 0),
        )
        self.client_a = make_logged_in_client(
            username="upl-price-iso-a", groups=("客服",), nodes=(NODE,)
        )
        try:
            with tenant_context(self.tenant_b):
                UpstreamPriceChangeLog.objects.create(
                    order_no=CROSS_B_NO,
                    game_name="B 的游戏",
                    game_role_name="B 的角色",
                    before_amount=Decimal("20.00"),
                    after_amount=Decimal("22.00"),
                    source_name="B 的上家",
                    note=CROSS_B_NOTE,
                    occurred_at=local(2026, 10, 17, 9, 0),
                )
                self.client_b = make_logged_in_client(
                    domain=DOMAIN_B,
                    username="upl-price-iso-b",
                    groups=("客服",),
                    nodes=(NODE,),
                )
        finally:
            connection.set_tenant(self.tenant)

        # 夹具自证：两行各只在一个 schema 里（否则下面的否定断言可能空洞通过）。
        self.assertTrue(
            UpstreamPriceChangeLog.objects.filter(note=CROSS_A_NOTE).exists()
        )
        self.assertFalse(
            UpstreamPriceChangeLog.objects.filter(note=CROSS_B_NOTE).exists()
        )
        self.assertTrue(
            self._b_call(
                lambda: UpstreamPriceChangeLog.objects.filter(
                    note=CROSS_B_NOTE
                ).exists()
            )
        )

    def _b_call(self, function):
        try:
            with tenant_context(self.tenant_b):
                return function()
        finally:
            connection.set_tenant(self.tenant)

    def test_each_tenant_only_sees_its_own_rows(self):
        body_a = self.client_a.get(ENDPOINT).json()
        self.assertEqual(body_a["code"], 0, body_a)
        notes_a = [row["note"] for row in body_a["data"]["items"]]
        self.assertEqual(notes_a, [CROSS_A_NOTE], notes_a)
        order_nos_a = [row["order_no"] for row in body_a["data"]["items"]]
        self.assertEqual(order_nos_a, [CROSS_A_NO], order_nos_a)

        body_b = self.client_b.get(ENDPOINT).json()
        self.assertEqual(body_b["code"], 0, body_b)
        notes_b = [row["note"] for row in body_b["data"]["items"]]
        self.assertEqual(notes_b, [CROSS_B_NOTE], notes_b)
        order_nos_b = [row["order_no"] for row in body_b["data"]["items"]]
        self.assertEqual(order_nos_b, [CROSS_B_NO], order_nos_b)

        # 反向：两边的响应里都**没有**对方那一行（用不同的显示值指认）。
        self.assertNotIn(CROSS_B_NOTE, notes_a)
        self.assertNotIn(CROSS_B_NO, order_nos_a)
        self.assertNotIn(CROSS_A_NOTE, notes_b)
        self.assertNotIn(CROSS_A_NO, order_nos_b)


class PriceLogSourceScanTests(SimpleTestCase):
    """判据 14 的静态那一半单独留一条：扫描器「至少看到一个文件」不空转。

    ⚠️ 扫描器本身若因为路径写错而什么都没扫到，上面那条「恰好等于白名单」的
    `assertEqual` 仍会红（`set()` ≠ 白名单），但错误信息会指向错误的地方 ⇒ 这里把
    「peer_inbound 确实被扫到」单独立一条，让归因一眼可见。
    """

    def test_the_scanner_really_sees_the_allowed_file(self):
        found = _upstream_price_log_write_files()
        self.assertIn("business/peer_inbound.py", found, sorted(found))
