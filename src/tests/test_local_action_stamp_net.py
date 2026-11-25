"""⭐ A8 / L-6（2026-10-25 审计批）：「上家迟到事件会覆盖的那几列」的**本地写点穷举网**。

## 为什么存在

`PeerOrderLink.local_action_at`（「我方最后一次本地动作时刻」）原来是**只有状态机**
（`order_status.apply_transition`）在盖——本地用户改完备注 / 项目 / 电话 / QQ / 发单价，
一条**更旧**的上家事件照样能把那几列冲掉（L-6 现场：闸 `peer_inbound.apply_events` 的
并列 `stale` 判据只认被状态机推进过的时刻）。

⇒ 本文件把两件事钉死：

1. **一个事实一处实现**：盖时刻的唯一实现是
   `apps/business/order_status.py::stamp_local_action`（`apply_transition` 尾部的内联写法
   已收口进去），所有本地写点调它；
2. **写点一条不漏**：六列（`CONFLICT_PRONE_FIELDS` = `_DOWNSTREAM_BOUNDED_FIELDS`
   ∪ `remark` / `content`）的**全部本地写点**按 E-0232 纪律逐条登记——每一条要么
   `stamped`（该作用域必须真的调 `stamp_local_action`），要么 `exempt:` 带**显式理由**。
   新增一个写点 ⇒ 本网当场红，逼登记者回答「这条该不该盖」。

## 扫描面（如实声明，⛔ 别读成「全仓所有写入都在网里」）

- **属性赋值** `<obj>.<field> = …`（六列字面名）——本仓这些列的更新支全是这个形状；
- **queryset `.update(...)` 的字面键**（接收者源码以 `Order.objects` / `Order.all_objects`
  开头）——今天 **0 处**（实测读数，不是结构性保证）；
- ⛔ **扫描面看不见的形状**（逐条在 `PINNED_WRITE_POINTS` 里人工登记，同拍对账）：
  ① `Order.objects.create(...)` 的 kwargs——建**新单**还没有 `PeerOrderLink`，
     本列没有消费方（录单 / 拆分子单 / 鸡蛋铺①②式建单都属这类，有意不盖）；
  ② **动态赋值 / 动态键**：`update_order_fields` 的 `setattr(order, field, value)` 循环
     与 `jidanpu_upstream._pull_content` 的 `.update(**updates)`——前者 pinned 为
     stamped、后者 pinned 为 exempt 并有**专条断言**钉它「不得改调
     `stamp_local_action`」（鸡蛋铺回拉是同步事实，盖了会把时间线搅浑）；
  ③ 非本模型的同名列（`Role.remark` / `WithdrawOrder.remark` / `OrderScreenshot.remark`）
     ——字面扫描命中但不在本闸管辖面，豁免表逐条登记理由；
  ④ ⭐ N5：`hall_shadow.sync_hidden_order_price` 的 `qs = Order.objects.filter(...)` →
     `qs.update(hall_price=…)`——`.update` 的接收者是变量、不是 `Order.objects` 开头
     ⇒ **落库本体**扫不到；同作用域的实例回填（`order.hall_price = …`）是同一件事的配套、
     扫描可见，两处按**同一条**盲区登记 pinned 为 exempt（同步事实推导，不是本地动作），
     并有**专条反向钉子**钉它「不得改调 `stamp_local_action`」。

⚠️ 夹具一律合成值；断言打在**数据库行**上（`refresh_from_db`，E-0009 / E-0024）。
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timezone
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    Game,
    Order,
    OrderStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
)
from apps.business.order_status import CONFLICT_PRONE_FIELDS
from apps.business.order_writes import (
    WriteContext,
    change_hall_price,
    update_order_fields,
)
from apps.business.peer_inbound import SKIP_STALE, apply_events
from apps.business.peer_sync import hash_token, new_token

_SRC = pathlib.Path(__file__).resolve().parents[1]
_APPS = _SRC / "apps"

#: 六列 = 计划 A8 的字段面（`_DOWNSTREAM_BOUNDED_FIELDS` + remark / content）。
#: ⚠️ 从生产常量派生（`order_status.CONFLICT_PRONE_FIELDS`）⇒ 生产清单改了本网跟着改、
#: 不会两边漂；「恰好这六个」的钉子在下面那条断言里。
_SCAN_FIELDS = frozenset(CONFLICT_PRONE_FIELDS)


# ── AST 扫描（形状照 tests/test_order_time_fields.py 的既有手法）────────────────
def _scopes(path: pathlib.Path) -> dict[str, ast.AST]:
    """按 `Class.method` / `func` 的形状扫出 `{作用域名: 节点}`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, ast.AST] = {}

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{scope}.{child.name}" if scope else child.name
                found[name] = child
                walk(child, name)
            else:
                walk(child, scope)

    walk(tree, "")
    return found


def _walk_own(node):
    """子树全部节点，⛔ 不下钻进嵌套的函数 / 类体（作用域自己算自己的）。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield child
        yield from _walk_own(child)


def _iter_app_files():
    """`apps/**` 的生产 `.py`（⛔ 不含迁移）。"""
    for path in sorted(_APPS.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        yield str(path.relative_to(_APPS)), path


def _called_names(node) -> set[str]:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _is_order_queryset_update(node: ast.Call) -> bool:
    """`.update(...)` 且接收者源码以 `Order.objects` / `Order.all_objects` 开头。"""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "update"):
        return False
    try:
        receiver = ast.unparse(func.value)
    except Exception:  # pragma: no cover - unparse 失败按不匹配处理
        return False
    return receiver.startswith(("Order.objects", "Order.all_objects"))


def _scan_write_scopes() -> set[tuple[str, str]]:
    """六列写点所在的 `(相对路径, 作用域)` 集合（扫描面与盲区见模块 docstring）。"""
    hits: set[tuple[str, str]] = set()
    for rel, path in _iter_app_files():
        for name, node in _scopes(path).items():
            for sub in _walk_own(node):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr in _SCAN_FIELDS
                        ):
                            hits.add((rel, name))
                elif isinstance(sub, ast.Call) and _is_order_queryset_update(sub):
                    for kw in sub.keywords:
                        if kw.arg in _SCAN_FIELDS:
                            hits.add((rel, name))
    return hits


# ── 穷举表（E-0232：每条写点都要有登记，理由写在这里 ⛔ 不许「没登记」）──────────
#: `verdict` 以 `"stamped"` 开头 ⇒ 该作用域必须调 `stamp_local_action`；
#: 以 `"exempt:"` 开头 ⇒ 冒号后面是**人读理由**（为什么不盖）。
#: ⚠️ 表 = 扫描命中 ∪ `PINNED_WRITE_POINTS`（扫描面盲区的人工登记，见 docstring ②）。
ENUMERATED_WRITE_POINTS: dict[tuple[str, str], str] = {
    # ── 本批接线的**本地**写点（同事务盖「我方动作时刻」）──
    ("business/order_writes.py", "change_hall_price"): "stamped",
    ("business/order_writes.py", "assign_order"): "stamped",
    ("business/performance.py", "apply_amount_to_effective_base"): "stamped",
    ("business/api/order_views.py", "OrderViewSet.throw_to_hall"): "stamped",
    # ⭐ dl-A：发单页填了「同发内部抢单价」时，抛代练通前补发内部大厅那一步
    # 会把 `Order.hall_price` 写成页面值（**本地人工动作**）⇒ 必须盖时刻。
    # ⚠️ 判据是 `stamped`：该作用域里要真的调 `stamp_local_action`（删掉那行本网会红）。
    ("business/dailiantong_push.py", "ensure_internal_hall"): "stamped",
    ("business/api/order_views.py", "OrderViewSet.split_local_order"): "stamped",
    ("business/api/order_views.py", "OrderViewSet.split_priced_order"): "stamped",
    ("business/jidanpu_first_assignment.py", "_recorded_link"): (
        "exempt:未落库的订单副本，只为构造鸡蛋铺录单载荷；确认成功后通过 assign_order 落价并盖时刻"
    ),
    # ── **同步驱动**的写入（对端事实到达，⛔ 不盖——口径出处：
    #    `order_status.stamp_local_action` docstring 的纪律段）──
    ("business/peer_inbound.py", "apply_events"): (
        "exempt:peer 上家事件的落库本身（对端事实到达，不是本地动作）"
    ),
    ("business/peer_inbound.py", "_promote_claiming_mirror"): (
        "exempt:抢单转正是事件时间线的一部分（随事件落地，不是本地动作）"
    ),
    ("business/hall_shadow.py", "ensure_for_card"): (
        "exempt:大厅卡落地时给镜像卡建/复活那张隐藏单（对端事实到达，不是本地动作）"
    ),
    # ⭐ D-0325上家镜像卡隐藏单线的对价口——D-0325 合入时漏登记、
    #    本线（D-0329）补齐（登记的是**已存在**的落点，没有改行为）。
    #    判据照 peer 线 `sync_hidden_order_price` 的同一条豁免（PINNED 那条）：
    #    对端卡价 + 本层百分比推导，不是本地人工动作；本地人工改价走
    #    `order_writes.change_hall_price` 那条 stamped 路。
    ("business/upstream_hall_shadow.py", "sync_price_for_card"): (
        "exempt:上家改价 / 换绑定百分比传导到隐藏单的 hall_price"
        "（对端卡价 + 本层百分比推导，不是本地动作；照 peer 线"
        " sync_hidden_order_price 的同一条豁免）"
    ),
    ("business/jidanpu_upstream.py", "_promote_accepted_pool_order"): (
        "exempt:上家确认同次大厅接单后，按已展示的镜像价格补齐未分配单。"
        "这笔发单价来自镜像和绑定比例，沿用上家大厅转正口径。"
    ),
    # ── **非 Order 模型**的同名列（字面扫描命中、但不属于本闸的管辖面）──
    ("business/api/screenshot_views.py", "claim_screenshot"): (
        "exempt:OrderScreenshot.remark（截图行的展示备注，不是 Order 的六列）"
    ),
    ("accounts/wallet.py", "reject_withdraw"): (
        "exempt:WithdrawOrder.remark（提现单备注，不是 Order 的六列）"
    ),
    ("accounts/wallet.py", "mark_withdraw_paid"): (
        "exempt:WithdrawOrder.remark（提现单备注，不是 Order 的六列）"
    ),
    ("accounts/wallet.py", "start_transfer"): (
        "exempt:WithdrawOrder.remark（提现单备注，不是 Order 的六列；D-0336 先扣再打"
        "在发起时把实打金额写进提现单备注，属提现单自己的备注列）"
    ),
    ("rbac/services.py", "rename_role"): (
        "exempt:Role.remark（角色备注，不是 Order 的六列）"
    ),
    ("platform/management/commands/seed_e2e.py", "_seed_minrole"): (
        "exempt:Role.remark（种子命令，不是 Order 的六列）"
    ),
    ("platform/management/commands/seed_e2e.py", "_seed_gamescope"): (
        "exempt:Role.remark（种子命令，不是 Order 的六列）"
    ),
}

#: 扫描面**看不见**、但同样受本闸管辖的写点（人工登记，与上表同一套 verdict 语义）。
PINNED_WRITE_POINTS: dict[tuple[str, str], str] = {
    # 动态 `setattr(order, field, value)` 循环——本地编辑的主落库口（单笔 / 批量 /
    # 开放平台共用），判据 = 「改的列里真有冲突面六列」。
    ("business/order_writes.py", "update_order_fields"): "stamped-via-dispatch",
    # 动态 `.update(**updates)`（`CONTENT_FIELDS` 循环）——鸡蛋铺回拉「只补我方为空」，
    # 同步驱动、有意不盖；专条断言钉它不得改调 stamp（见下面那条测试）。
    ("business/jidanpu_upstream.py", "_pull_content"): (
        "exempt:鸡蛋铺回拉的只补空字段（同步事实到达，不是本地动作；动态键扫描看不见，"
        "专条断言钉它不得改调 stamp_local_action）"
    ),
    # ⭐ N5（R3-M6）：`hall.reprice` / 改百分比传导到隐藏单的 `hall_price`（六列之一）。
    # 落库本体是 `qs = Order.objects.filter(...)` → `qs.update(hall_price=…)`，本网的
    # `.update` 只认接收者源码以 `Order.objects` / `Order.all_objects` 开头的形状 ⇒ 扫不到；
    # 同作用域的实例回填是同一件事的配套，按「扫描面盲区」人工登记。
    # ⛔ 不登记成 stamped：这不是本地动作，盖「我方动作时刻」会把上家迟到事件的 stale
    # 判据搅乱（与 `_pull_content` 同一条反向钉子）；专条断言钉它不得改调 stamp。
    ("business/hall_shadow.py", "sync_hidden_order_price"): (
        "exempt:大厅改价 / 百分比传导落隐藏单的价（对端卡价 + 本层百分比推导，"
        "不是本地人工动作；本地人工改价走 order_writes.change_hall_price 那条 stamped 路）"
    ),
}

# Edited-order entry points share one content side-effect funnel. The dispatcher
# owns the actual stamp; keep these edges explicit so neither entry point can bypass it.
STAMP_DISPATCH_CALLERS = {
    ("business/order_writes.py", "update_order_fields"),
    ("business/order_writes.py", "_update_order_fields_with_price"),
}
STAMP_DISPATCH_TARGET = ("business/order_writes.py", "_dispatch_content_update")


class WritePointEnumerationTests(SimpleTestCase):
    """结构网：扫描结果 ⇔ 穷举表恰好对拍（每条钉子都有「能红」的路径）。"""

    def test_the_field_set_is_exactly_the_planned_six(self):
        """字段面恰好计划那六个（多了 / 少了都红——字段面变了必须回来重登记写点）。"""
        self.assertEqual(
            frozenset(CONFLICT_PRONE_FIELDS),
            {"hall_price", "customer_phone", "customer_qq", "project_text",
             "remark", "content"},
        )

    def test_every_scan_hit_is_registered(self):
        """扫描出的每一条写点都在表里（新写点没登记 ⇒ 红，逼回答「该不该盖」）。"""
        hits = _scan_write_scopes()
        self.assertTrue(hits, "扫描命中为空 ⇒ 网子恒真（扫描面坏了）")
        registered = {**ENUMERATED_WRITE_POINTS, **PINNED_WRITE_POINTS}
        missing = sorted(hits - registered.keys())
        self.assertEqual(
            missing, [],
            "这些六列写点没有登记（stamped 或 exempt:理由）："
            f"{missing}；登记处 = tests/test_local_action_stamp_net.py",
        )

    def test_stamped_scopes_really_call_the_unique_implementation(self):
        """登记为 stamped 的作用域**真的**调 `stamp_local_action`（⛔ 不是注释里声称）。"""
        registered = {**ENUMERATED_WRITE_POINTS, **PINNED_WRITE_POINTS}
        checked = 0
        for (rel, scope), verdict in sorted(registered.items()):
            if not verdict.startswith("stamped"):
                continue
            node = _scopes(_APPS / rel).get(scope)
            self.assertIsNotNone(
                node, f"{rel}:{scope} 不存在了 ⇒ 登记表过期，请改准"
            )
            if (rel, scope) in STAMP_DISPATCH_CALLERS:
                self.assertIn(
                    "_dispatch_content_update", _called_names(node),
                    f"{rel}:{scope} 必须经过共用内容分发器",
                )
                checked += 1
                continue
            self.assertIn(
                "stamp_local_action", _called_names(node),
                f"{rel}:{scope} 登记为 stamped 但没有调唯一实现 stamp_local_action",
            )
            checked += 1
        funnel = _scopes(_APPS / STAMP_DISPATCH_TARGET[0]).get(STAMP_DISPATCH_TARGET[1])
        self.assertIsNotNone(funnel, "共用内容分发器不存在")
        self.assertIn(
            "stamp_local_action", _called_names(funnel),
            "共用内容分发器必须调用唯一实现 stamp_local_action",
        )
        order_writes = _scopes(_APPS / STAMP_DISPATCH_TARGET[0])
        actual_callers = {
            (STAMP_DISPATCH_TARGET[0], scope)
            for scope, candidate in order_writes.items()
            if "_dispatch_content_update" in _called_names(candidate)
        }
        self.assertEqual(
            actual_callers,
            STAMP_DISPATCH_CALLERS,
            "本地 PATCH 与拆分组价格 PATCH 必须且只能经共用内容分发器",
        )
        self.assertGreaterEqual(checked, 6)   # 自证非空：本批接线 6 处都要被查到

    def test_exempt_entries_all_carry_explicit_reasons(self):
        """豁免必须**显式写理由**（⛔ 不许空豁免 / 拼错的 verdict）。"""
        registered = {**ENUMERATED_WRITE_POINTS, **PINNED_WRITE_POINTS}
        bad = sorted(
            key for key, verdict in registered.items()
            if not (verdict.startswith("stamped") or (
                verdict.startswith("exempt:") and verdict[len("exempt:"):].strip()
            ))
        )
        self.assertEqual(bad, [], f"这些登记既不是 stamped 也没写豁免理由：{bad}")

    def test_jidanpu_pull_content_never_stamps_the_local_action_clock(self):
        """⭐ 反向钉子：鸡蛋铺回拉「只补空」**不得**改调 `stamp_local_action`。

        盖了会把「鸡蛋铺上游的事实到达」记成团团通上家线上的「我方动作」，
        把随后合法的 peer 事件误判成 stale（时间线搅浑）。
        """
        from apps.business import jidanpu_upstream

        node = _scopes(_APPS / "business" / "jidanpu_upstream.py").get("_pull_content")
        self.assertIsNotNone(node)
        self.assertNotIn("stamp_local_action", _called_names(node))

    def test_sync_hidden_order_price_never_stamps_the_local_action_clock(self):
        """⭐ N5 反向钉子：大涨价 / 百分比传导到隐藏单**不得**盖「我方本地动作时刻」。

        它写的是 `hall_price`（六列之一），但那是**同步事实推导**（上家价 × 本层百分比），
        不是本地人工改价。盖了会让随后合法的 peer 事件被误判 stale（时间线搅浑）。
        ⚠️ 本网对这个作用域的 `.update()` 本体扫不到（盲区登记在 `PINNED_WRITE_POINTS`），
        所以这条反向钉子就是它的机器判据——临时在函数里加一句 `stamp_local_action(...)`
        本用例当场红。
        """
        node = _scopes(_APPS / "business" / "hall_shadow.py").get(
            "sync_hidden_order_price"
        )
        self.assertIsNotNone(node)
        self.assertNotIn("stamp_local_action", _called_names(node))


# ── 行为判据：本地编辑盖时刻 → 迟到的上家事件被 stale 挡住 ──────────────────────
class LocalActionStampBehaviorTests(TenantTestCase):
    """本批的行为判据（红检：不盖时刻时，那条事件会**应用**并冲掉本地编辑）。"""

    T_LAST_APPLIED = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
    #: 事件时刻：晚于 `last_applied_at`（过第一道 stale 闸）、早于本地编辑（被第二道挡）。
    T_EVENT = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.game = Game.objects.create(name="盖章网夹具游戏")
        operator = get_user_model().objects.create_user(
            username="stamp-net-operator", password="pw-syn"
        )
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家丙",
            peer_base_url="https://peer-stamp.test.invalid",
            peer_account="syn-stamp-up",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-stamp-local.test.invalid",
            bound_by=operator,
        )
        self.order = Order.objects.create(
            order_no="20261025000000000051",
            game=self.game,
            status=OrderStatus.WAITING_GRAB,
            remark="原备注",
            project_text="测试项目甲",
        )
        self.link = PeerOrderLink.objects.create(
            order=self.order,
            upstream_binding=self.binding,
            peer_order_no="STAMP-1",
            last_applied_at=self.T_LAST_APPLIED,
        )
        self.editor = get_user_model().objects.create_user(
            username="stamp-net-editor", password="pw-syn"
        )
        from tests.p5_auth_helpers import grant_games

        grant_games(self.editor, self.game)
        self.ctx = WriteContext(
            actor="测试编辑人",
            log_actor=self.editor.get_username(),
            log_actor_id=self.editor.pk,
        )

    def _late_event(self):
        """一条「迟到」的上家事件：过 `last_applied_at` 闸、撞 `local_action_at` 闸。"""
        return {
            "kind": "order.assign",
            "target_ref": "STAMP-1",
            "occurred_at": self.T_EVENT.isoformat(),
            "payload": {
                "status": OrderStatus.IN_PROGRESS,
                "hall_price": "50.00",
                "remark": "上家的旧备注",
                "game_name": self.game.name,
            },
        }

    def test_a_local_remark_edit_stamps_and_the_late_event_is_refused(self):
        """改备注 ⇒ `local_action_at` 被盖 ⇒ 那条本可应用的迟到事件被 stale 挡住。"""
        outcome = update_order_fields(
            self.order,
            validated={"remark": "本地新备注"},
            request_user=self.editor,
            ctx=self.ctx,
            dry_run=False,
        )
        self.assertTrue(outcome.ok, outcome.msg)
        self.link.refresh_from_db()
        # ⭐ 盖章判据（数据库行）：本地编辑真的推进了「我方动作时刻」。
        self.assertIsNotNone(self.link.local_action_at)
        self.assertGreater(self.link.local_action_at, self.T_EVENT)
        # 迟到的上家事件：过第一道闸、被第二道挡住，本地备注**不被冲掉**。
        result = apply_events([self._late_event()], binding=self.binding)
        self.assertEqual(result.skipped, 1, result)
        self.assertEqual(result.skipped_details[0]["code"], SKIP_STALE)
        row = Order.objects.get(pk=self.order.pk)
        self.assertEqual(row.remark, "本地新备注")

    def test_change_hall_price_stamps_local_action_too(self):
        """`/hall-price/` 同闸：本地改价也推进「我方动作时刻」。"""
        self.assertIsNone(self.link.local_action_at)
        outcome = change_hall_price(
            self.order, price=Decimal("66.00"), ctx=self.ctx, dry_run=False
        )
        self.assertTrue(outcome.ok, outcome.msg)
        self.link.refresh_from_db()
        self.assertIsNotNone(self.link.local_action_at)
        self.assertEqual(
            Order.objects.get(pk=self.order.pk).hall_price, Decimal("66.00")
        )

    def test_an_edit_that_changes_none_of_the_six_fields_stamps_nothing(self):
        """对照（闸不误伤）：改的不是冲突面六列（如 `order_type`）⇒ 不盖这一列。"""
        outcome = update_order_fields(
            self.order,
            validated={"order_type": "BOOST"},
            request_user=self.editor,
            ctx=self.ctx,
            dry_run=False,
        )
        self.assertTrue(outcome.ok, outcome.msg)
        self.link.refresh_from_db()
        self.assertIsNone(self.link.local_action_at)
