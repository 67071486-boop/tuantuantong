"""批次 5 · 线 E 修复轮：两列时间轴的**唯一实现** + **进入「待验收」的全部路径**都要写它。

## 这一轮为什么存在

线 E 第一次落地时，`submitted_at` 只有 `order_status.apply_transition` 一个写入点，
而**对端同步进来**的单有两条路能把状态直接写成 `PENDING_REVIEW`、**绕过**那个收口
（`peer_inbound` 的建单支与上行回传支）⇒ 那些单按「交单时间」**永远筛不出来**。
这与批次 4 把「验收时间」接到生产上零写入的 `Performance.reviewed_at` 是**同一个缺陷类**：
**列表上能选一档 ≠ 那一档有数据**。

⇒ 本文件把两件事钉死：
1. **一个事实一处实现**：`apps/business/order_time_fields.py` 的 `mark_published` /
   `mark_submitted`，所有写入点都调它（⛔ 不再各自写一份 `IS NULL` 判据）；
2. **进入「待验收」的路径一条不漏**：结构性网按「订单状态直写落点」的**重数**与
   「写入点清单」对拍（新增一条直写 ⇒ 当场红，逼登记者回答「这条会不会写进待验收」）。

⚠️ 断言一律打在**数据库行**上（`refresh_from_db()` / 重取），⛔ 不是看内存对象——
本批的坑之一就是「值只在内存里、库里没有」。
⚠️ 夹具一律**合成值**（`测试*` / `*.test.invalid` / `SYN-*`）。
"""
from __future__ import annotations

import ast
import collections
import pathlib
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business import order_time_fields
from apps.business.models import (
    EmpType,
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_inbound import apply_events, apply_upstream_events
from apps.business.peer_sync import hash_token, new_token

_SRC = pathlib.Path(__file__).resolve().parents[1]
_APPS = _SRC / "apps"

#: 本文件统一的合成单号。
LOCAL_NO = "20261015000000000091"
PEER_NO = "SYN-TF-1"
UP_ORDER_NO = "SYN-TF-2"
GAME_NAME = "测试时间轴游戏"


# ── 共用夹具 ────────────────────────────────────────────────────────────────
class _Base(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name=GAME_NAME)

    def _order(self, number=LOCAL_NO, *, status=OrderStatus.UNASSIGNED, **kw):
        return Order.objects.create(
            game=self.game, order_no=number, status=status,
            total_price=Decimal("100.00"), hall_price=Decimal("50.00"), **kw,
        )

    def _row(self, order):
        """从数据库**重新取行**——本文件唯一的断言对象。"""
        return Order.objects.get(pk=order.pk)


# ── ① 共用实现本身 ──────────────────────────────────────────────────────────
class MarkFunctionsTests(_Base):
    """`order_time_fields` 两个函数的口径（只写第一次 / 不清空 / 内存同步 / 状态自判）。"""

    def test_mark_published_writes_once_and_returns_whether_it_wrote(self):
        """第一次写 ⇒ 回 `True` 且**库里**有值；第二次（另一个时刻）⇒ 回 `False`、值不变。"""
        order = self._order()
        first = timezone.now() - timedelta(days=3)
        self.assertTrue(order_time_fields.mark_published(order, moment=first))
        self.assertEqual(self._row(order).published_at, first)
        # 内存对象也被同步（响应序列化器读它）——否则接口会回一个与库里不一致的 null。
        self.assertEqual(order.published_at, first)
        later = timezone.now()
        self.assertFalse(order_time_fields.mark_published(order, moment=later))
        self.assertEqual(self._row(order).published_at, first)   # 只写第一次

    def test_mark_submitted_only_writes_when_the_status_is_pending_review(self):
        """⭐ **状态自判**：不是「待验收」⇒ 一行不写、回 `False`（调用方不必各自判一次）。"""
        order = self._order(status=OrderStatus.IN_PROGRESS)
        self.assertFalse(order_time_fields.mark_submitted(order))
        self.assertIsNone(self._row(order).submitted_at)

        order.status = str(OrderStatus.PENDING_REVIEW)
        moment = timezone.now() - timedelta(hours=2)
        self.assertTrue(order_time_fields.mark_submitted(order, moment=moment))
        self.assertEqual(self._row(order).submitted_at, moment)

    def test_mark_submitted_is_idempotent(self):
        """第二次进「待验收」**不覆盖**（值钉死，⛔ 不是只断「非空」）。"""
        order = self._order(status=OrderStatus.PENDING_REVIEW)
        first = timezone.now() - timedelta(days=5)
        self.assertTrue(order_time_fields.mark_submitted(order, moment=first))
        self.assertFalse(
            order_time_fields.mark_submitted(order, moment=timezone.now())
        )
        self.assertEqual(self._row(order).submitted_at, first)

    def test_neither_marker_ever_clears_the_column(self):
        """⛔ **不清空**：把值写好之后调一万次都不会变 `NULL`（与 `completed_at` 有意不同）。"""
        order = self._order(status=OrderStatus.PENDING_REVIEW)
        moment = timezone.now() - timedelta(days=1)
        order_time_fields.mark_submitted(order, moment=moment)
        order_time_fields.mark_published(order, moment=moment)
        for _ in range(3):
            order_time_fields.mark_submitted(order)
            order_time_fields.mark_published(order)
        row = self._row(order)
        self.assertEqual(row.submitted_at, moment)
        self.assertEqual(row.published_at, moment)

    def test_a_stale_in_memory_object_cannot_clobber_the_stored_value(self):
        """⭐ 「只写第一次」的判据在 **SQL 的 `WHERE` 里**，⛔ 不在内存里。

        判别力：把 `_mark` 的 `filter(..., <列>__isnull=True)` 去掉（改成只按 `pk` 过滤），
        本用例当场红 —— 这正是「调用方手上那份 `order` 是陈旧的」那个真实形状
        （`claim_order` 先写库、`apply_transition` 随后拿旧对象再写一次）。
        """
        fresh = self._order()
        order_time_fields.mark_submitted(
            _with_status(fresh, OrderStatus.PENDING_REVIEW),
            moment=timezone.now() - timedelta(days=9),
        )
        stored = self._row(fresh).submitted_at
        self.assertIsNotNone(stored)
        # 另一份**陈旧**实例（仍以为自己是 `None`）再来一次 ⇒ 不许覆盖。
        stale = Order.objects.get(pk=fresh.pk)
        stale.submitted_at = None                     # 模拟「读得早」的那份副本
        order_time_fields.mark_submitted(
            _with_status(stale, OrderStatus.PENDING_REVIEW)
        )
        self.assertEqual(self._row(fresh).submitted_at, stored)


def _with_status(order, status):
    """把内存对象的状态改成 `status` 并原样返回（`mark_submitted` 读内存值）。"""
    order.status = str(status)
    return order


# ── ② 对端同步的两条路径（本轮修掉的两个洞）─────────────────────────────────
class PeerInboundSubmittedAtTests(_Base):
    """下家接收侧与「待验收」的关系（⭐ A2 / M-2 改准，2026-10-25）。

    **历史**：本类原钉「peer_inbound 绕过状态机写待验收的两条路都必须写
    `submitted_at`」——那是在「下家接受集不盖建单支 /
    未知 kind 支」的旧前提下补的交单时刻。
    **A2 / M-2（D-0305 裁定）把那两扇门关了**：`raw_status` 照抄路必须过
    `DOWNSTREAM_ACCEPTED_STATUSES`（不含「待验收」）⇒ 下家侧再也写不出待验收
    （下家自己的待验收只能由本系统改）⇒ 本类改钉**闸本身**；交单时刻的活写入点
    只剩状态机与上行回传支（本文件「洞②」那组用例仍钉着后者）。
    """

    T0 = timezone.now().replace(microsecond=0) - timedelta(days=2)

    def setUp(self):
        super().setUp()
        self.kefu = get_user_model().objects.create_user(
            username="syn-tf-kefu", password="pw-syn-tf"
        )
        self.upstream_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试时间轴工作室",
            peer_base_url="https://peer-tf.test.invalid",
            peer_account="peer-account-tf",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-tf-local.test.invalid",
            bound_by=self.kefu,
        )

    def _downstream_event(self, *, status, kind="order.update", occurred_at=None):
        return {
            "kind": kind,
            "target_ref": PEER_NO,
            "occurred_at": (occurred_at or self.T0).isoformat(),
            "payload": {
                "status": status,
                "total_price": "88.00",
                "game_name": GAME_NAME,
            },
        }

    # ── 洞 ①：`apply_events` 的**建单支**（⭐ A2 改准：集外状态整条拒之门外）──────
    def test_a_first_event_with_a_status_outside_the_accepted_set_creates_nothing(self):
        """⭐ A2 / M-2：首条 `order.update` + 集外状态（待验收）⇒ 硬跳过、**不落单**。

        原用例钉「建单支把单直接落成待验收 + 留交单时刻」——那扇门已随 A2 关闭
        （`raw_status` 照抄路必须过 `DOWNSTREAM_ACCEPTED_STATUSES`）。
        判别力：把那道闸删掉，本用例红（单会建出来、状态是待验收）。
        """
        outcome = apply_events(
            [self._downstream_event(status=OrderStatus.PENDING_REVIEW)],
            binding=self.upstream_binding,
        )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(
            outcome.skipped_details[0]["code"], "status_not_accepted_for_kind"
        )
        self.assertFalse(Order.objects.filter(source_order_no=PEER_NO).exists())

    def test_the_gated_first_event_is_rejected_again_on_replay(self):
        """重放同一条集外首条事件 ⇒ 仍被拒（幂等拒绝：不建单、不重复落单）。"""
        first = apply_events(
            [self._downstream_event(status=OrderStatus.PENDING_REVIEW)],
            binding=self.upstream_binding,
        )
        self.assertEqual(first.applied, 0, first)
        second = apply_events(
            [self._downstream_event(
                status=OrderStatus.PENDING_REVIEW,
                occurred_at=self.T0 + timedelta(hours=5),
            )],
            binding=self.upstream_binding,
        )
        self.assertEqual(second.applied, 0, second)
        self.assertFalse(Order.objects.filter(source_order_no=PEER_NO).exists())

    def test_a_peer_order_that_is_not_in_pending_review_has_no_submitted_at(self):
        """阴性对照：对端建单支落成 `IN_PROGRESS` ⇒ `submitted_at` 必须留空。

        （判别力：把 `mark_submitted` 换成「无条件写」，本用例红。）
        """
        apply_events(
            [self._downstream_event(
                status=OrderStatus.IN_PROGRESS, kind="order.assign",
            )],
            binding=self.upstream_binding,
        )
        order = Order.objects.get(source_order_no=PEER_NO)
        self.assertEqual(order.status, str(OrderStatus.UNASSIGNED))       # 接手类落成未分配
        self.assertIsNone(order.submitted_at)

    # ── 洞 ③：`apply_events` 的**未知 kind 支**（⭐ A2 改准：那扇门已关，改钉闸本身）──
    def test_an_unknown_kind_with_a_status_outside_the_set_is_rejected(self):
        """⭐⭐ 评审那条探针：`kind` 是个**未知值** + `status="PENDING_REVIEW"` ⇒ 必须记下交单时刻。

        ## 为什么这条必须有

        `apply_events` 的 `kind` **在本文件里没有白名单**：`order.update` 那一支有接受集闸，
        而「不是 `order.update` / 接手类 / 撤单」的 `else` 支**照抄** `raw_status`
        ⇒ 对端（两机版本偏斜时必然）能发一个未知 kind 把单写成「待验收」。
        本轮之前**这条支没有 `mark_submitted`** ⇒ 那些单按「交单时间」永远筛不出来
        （与批次 4 把验收时间接到零写入列上同类）。

        ## 实测读数（评审给的探针，修之前）

        `status='PENDING_REVIEW' submitted_at=None outcome=Outcome(applied=1, …)`
        ——**状态落库了、交单时间没有**。

        ## 判据

        ① 事件**真的被应用**（`applied == 1`，非空哨兵）；
        ② 库里的状态就是 `PENDING_REVIEW`；
        ③ `submitted_at` **等于事件的 `occurred_at`**（⛔ 不是只断非空）。
        """
        moment = self.T0 + timedelta(hours=1)
        outcome = apply_events(
            [self._downstream_event(
                status=OrderStatus.PENDING_REVIEW,
                kind="order.some.unknown.kind",
                occurred_at=moment,
            )],
            binding=self.upstream_binding,
        )
        # A2 / M-2 的正靶：未知 kind + 集外状态 ⇒ 硬跳过（`status_not_accepted_for_kind`）。
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(
            outcome.skipped_details[0]["code"], "status_not_accepted_for_kind"
        )
        self.assertFalse(Order.objects.filter(source_order_no=PEER_NO).exists())

    def test_an_unknown_kind_with_another_status_leaves_submitted_at_empty(self):
        """未知 kind 的**阴性对照**：`status="IN_PROGRESS"` ⇒ `submitted_at` 留空。

        （判别力：把那一支的 `mark_submitted` 换成「无条件写」，本用例红。）
        """
        apply_events(
            [self._downstream_event(
                status=OrderStatus.IN_PROGRESS, kind="order.some.unknown.kind",
            )],
            binding=self.upstream_binding,
        )
        order = Order.objects.get(source_order_no=PEER_NO)
        self.assertEqual(order.status, str(OrderStatus.IN_PROGRESS))      # 哨兵
        self.assertIsNone(order.submitted_at)

    def test_an_unknown_kind_on_an_existing_order_is_rejected_too(self):
        """⭐ **已有单**走未知 kind 支（评审那条洞的第二形态）⇒ A2 闸**同样拒**。

        路径：先用一条 `order.assign` 事件把单建出来（落成未分配）⇒ 再用未知 kind +
        `PENDING_REVIEW` 打同一张单 ⇒ 硬跳过：状态与交单时刻**一个字不动**。
        （原用例钉「这条支落待验收并留交单时刻」——A2 关门后改钉闸本身。）
        """
        apply_events(
            [self._downstream_event(status=OrderStatus.IN_PROGRESS, kind="order.assign")],
            binding=self.upstream_binding,
        )
        order = Order.objects.get(source_order_no=PEER_NO)
        self.assertIsNone(order.submitted_at)                             # 建单时不落
        moment = self.T0 + timedelta(hours=2)
        outcome = apply_events(
            [self._downstream_event(
                status=OrderStatus.PENDING_REVIEW,
                kind="order.some.unknown.kind",
                occurred_at=moment,
            )],
            binding=self.upstream_binding,
        )
        self.assertEqual(outcome.applied, 0, outcome)
        self.assertEqual(
            outcome.skipped_details[0]["code"], "status_not_accepted_for_kind"
        )
        row = self._row(order)
        self.assertEqual(row.status, str(OrderStatus.UNASSIGNED))   # 状态一个字不动
        self.assertIsNone(row.submitted_at)

    # ── 洞 ②：`apply_upstream_events`（回传支，接受集**含** `PENDING_REVIEW`）──
    def _downstream_binding_for(self, username):
        account = get_user_model().objects.create_user(
            username=username, password="pw-syn-tf-up"
        )
        binding = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-tf-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        return binding, account

    def _upstream_ready(self, username, number=UP_ORDER_NO):
        binding, account = self._downstream_binding_for(username)
        order = self._order(number, status=OrderStatus.IN_PROGRESS)
        Performance.objects.create(order=order, user=account, emp_type=EmpType.MAIN)
        return binding, order

    def _push(self, binding, order, status, *, occurred_at):
        return apply_upstream_events([{
            "kind": "order.update",
            "target_ref": order.order_no,
            "occurred_at": occurred_at.isoformat(),
            "payload": {"status": status},
        }], binding=binding)

    def test_an_upstream_push_into_pending_review_writes_submitted_at(self):
        """⭐ 回传支把状态写成「待验收」⇒ `submitted_at` **落库**，值是事件的时刻。

        ⚠️ 判别力：删掉共用落库中那句 `mark_submitted` ⇒ 本用例红。
        """
        binding, order = self._upstream_ready("syn-tf-up-1")
        moment = timezone.now() + timedelta(seconds=30)
        outcome = self._push(binding, order, OrderStatus.PENDING_REVIEW, occurred_at=moment)
        self.assertEqual(outcome.applied, 1, outcome)
        row = self._row(order)
        self.assertEqual(row.status, str(OrderStatus.PENDING_REVIEW))     # 哨兵
        self.assertEqual(row.submitted_at, moment)                        # 钉**值**

    def test_a_second_upstream_push_does_not_overwrite(self):
        """再回传一次（更晚的时刻）⇒ 值**不变**。"""
        binding, order = self._upstream_ready("syn-tf-up-2")
        first = timezone.now() + timedelta(seconds=30)
        self._push(binding, order, OrderStatus.PENDING_REVIEW, occurred_at=first)
        self.assertEqual(self._row(order).submitted_at, first)            # 哨兵
        self._push(
            binding, order, OrderStatus.PENDING_REVIEW,
            occurred_at=first + timedelta(minutes=10),
        )
        self.assertEqual(self._row(order).submitted_at, first)

    def test_an_upstream_push_that_is_not_pending_review_leaves_it_empty(self):
        """阴性对照：回传 `ISSUE` ⇒ `submitted_at` 留空（状态变了，但不是「交单」）。"""
        binding, order = self._upstream_ready("syn-tf-up-3")
        outcome = self._push(
            binding, order, OrderStatus.ISSUE,
            occurred_at=timezone.now() + timedelta(seconds=30),
        )
        self.assertEqual(outcome.applied, 1, outcome)
        row = self._row(order)
        self.assertEqual(row.status, str(OrderStatus.ISSUE))              # 哨兵
        self.assertIsNone(row.submitted_at)


# ── ③ 结构性网：**写入点粒度**的清单（对拍 / 穷举 / 不清空）─────────────────────
#
# ## 为什么是「写入点粒度」而不是「作用域粒度」（本网第二轮改准的原因）
#
# 第一版把 `("business/peer_inbound.py", "apply_events")` 记成**一个作用域、重数 5**
# ——于是「这 5 个落点里**哪几个**能写出 `PENDING_REVIEW`、各自有没有闸」网一个字不问。
# 实测后果：`apply_events` 的**未知 kind 支**（`else: order.status = raw_status`，
# 没有接受集闸）能把单直接写成「待验收」，而当时 **11 个模块 271 条用例全绿**。
# ⇒ 现在每个落点**单独一行**，各自登记「为什么它不可能写出待验收」或「它靠什么留下交单时刻」。
#
# ## 扫描面（**如实声明，⛔ 不写「全仓」**）
#
# 扫 `src/apps/**/*.py`（不含 `migrations/`）里的**两类**形态：
#   ① `order.status = <expr>`（本仓订单变量的名字恒为 `order`，实测枚举过）；
#   ② `Order.objects.<create|get_or_create|update|update_or_create|bulk_create|bulk_update>(… status=<expr>)`。
#
# ⛔ **不在扫描面内、且本网`不`声称覆盖的三种形态**（都用当场 `grep` 核过，见每条的理由）：
#   - `Order.objects.get_or_create(…, defaults=…)` —— `status` 藏在 `defaults` 里，
#     而本仓那一处（`seed_e2e_write_orders`）的 `defaults` 是**变量**（`fields`），
#     静态读不出来。⚠️ 该处只往**刚被重建的** `e2e_write` 夹具租户写，且状态取自
#     文件内的 `ORDER_SPECS` 常量（`UNASSIGNED` / `IN_PROGRESS`）⇒ 今天不可能写出待验收。
#   - `<obj>.save(update_fields=[…, "status", …])` —— 它写的是**内存里那个值**，
#     而那个值只能来自被 ① 扫到的赋值（或从库里读出来的原值）⇒ 不引入新的状态值。
#   - 把 queryset 存进变量再 `.update(status=…)`（如 `qs = Order.objects…; qs.update(…)`）——
#     本仓今天 **0 处**（`grep -rnE "\.update\(.*status=" src/apps` 的命中里，
#     接收者全都是 `<Model>.objects…` 或 `Order.objects…` 的字面链）。
#   ⚠️ 「今天 0 处」是**实测读数**、不是结构性保证：真要加一处，本网看不见。
#   ⇒ 这正是「作用域声明」的含义——⛔ 不要把它读成「全仓所有状态写入都在网里」。
_BLOCK_TYPES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
)

#: 建单/改状态的**方法名白名单**（②类落点的形状）。
_ORDER_WRITE_METHODS = (
    "create", "get_or_create", "update", "update_or_create",
    "bulk_create", "bulk_update",
)


def _walk_own(node):
    """`node` 子树里的全部节点，⛔ **不下钻进**嵌套的函数 / 类体。

    ⚠️ 少了这一层：一个类作用域会把它的每个方法里的落点也算一遍
    （实测过：`OrderViewSet` 会把它两个方法的 `create` 重复计成 2 处）。
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield child
        yield from _walk_own(child)


def _scopes(path):
    """按 `Class.method` / `outer.inner` / 顶层 `func` 的形状扫出 `{作用域名: 节点}`。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}

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


def _iter_app_files():
    """`apps/**` 的生产 `.py`（⛔ 不含迁移）。"""
    for path in sorted(_APPS.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        yield str(path.relative_to(_APPS)), path


def _called_names(node):
    """某个作用域里**被调用**的具名函数集合（`mark_published(...)` / `mod.mark_published(...)`）。"""
    names = set()
    for sub in _walk_own(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _is_none(node):
    return isinstance(node, ast.Constant) and node.value is None


# 这些调用读接单时刻或保存通知的去重键，均不写 Performance.claimed_at。
_QR_NOTICE_CLAIM_TIME_CALLS = {
    ("business/claim.py", "claim_order", "enqueue"),
    ("business/jidanpu_upstream.py", "promote_upstream_mirror", "enqueue"),
    ("business/peer_inbound.py", "_promote_claiming_mirror", "enqueue"),
    ("internal_chat/order_grab_qr_notices.py", "enqueue", "OrderGrabQrNotice.objects.get_or_create"),
    ("internal_chat/order_grab_qr_notices.py", "_current_worker", "Performance.objects.filter"),
}


def _non_null_claimed_at_scopes():
    """写了**非空** `claimed_at` 的作用域集合（`= None` 那一处是「清接手人」，不算）。"""
    scopes = set()
    for rel, path in _iter_app_files():
        for name, node in _scopes(path).items():
            for sub in _walk_own(node):
                if isinstance(sub, ast.Call):
                    for kw in sub.keywords:
                        if kw.arg == "claimed_at" and not _is_none(kw.value):
                            if (rel, name, ast.unparse(sub.func)) not in _QR_NOTICE_CLAIM_TIME_CALLS:
                                scopes.add((rel, name))
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr == "claimed_at"
                            and not _is_none(sub.value)
                        ):
                            scopes.add((rel, name))
    return scopes


def _marker_scopes(marker):
    """调了 `marker` 的作用域集合。"""
    scopes = set()
    for rel, path in _iter_app_files():
        for name, node in _scopes(path).items():
            if marker in _called_names(node):
                scopes.add((rel, name))
    return scopes


def _is_order_receiver(text):
    """`②` 类落点的接收者是不是**订单**表（`Order.objects…` / `Order.all_objects…`）。"""
    return text.startswith("Order.objects.") or text.startswith("Order.all_objects.")


def _tree_and_scopes(path):
    """**同一棵** AST 树 + `{作用域名: 节点}`。

    ⚠️ 必须同树：`_innermost_block` 靠**节点身份**在父子表里往上走，两次 `ast.parse`
    得到的节点不是同一批对象（实测：拿另一次 parse 的父子表去查 ⇒ 永远 `None`、
    于是「这一支里有没有标记」这条判据恒假 —— 正是它把 4 个 `needs_marker` 落点全判红）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{scope}.{child.name}" if scope else child.name
                found[name] = child
                walk(child, name)
            else:
                walk(child, scope)

    walk(tree, "")
    return tree, found


def _innermost_block(tree, node):
    """`node` 所在的那个**最内层块**（`if` / `with` / `for` / 函数体 …）。

    ⚠️ 这是「这个落点自己所在的那一支」的机械定义：用它来判「同一支里有没有 `mark_submitted`」
    ——不是「同一作用域里有没有」（后者正是第一版放走那个洞的粒度）。
    """
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    cursor = parents.get(node)
    while cursor is not None and not isinstance(cursor, _BLOCK_TYPES):
        cursor = parents.get(cursor)
    return cursor


def _order_status_write_sites():
    """**写入点粒度**的订单状态落点清单：`{键: (表达式文本, AST 节点, 所在的那一支)}`。

    键 = `(相对路径, 作用域名, 形态, 该作用域内的第几处)`；形态 ∈ `{"assign", *_ORDER_WRITE_METHODS}`。
    ⚠️ 「该作用域内的第几处」按**源码顺序**编号 ⇒ 在中间插一处新的、或把表达式换掉，都会让
    这张表变样（本网的判据之一就是它与登记表**恰好相等**）。
    ⚠️ 一并回「所在的那一支」：下面那条「这一支里有没有 `mark_submitted`」的判据要拿它定位
    **这个落点自己所在的那一支**（⛔ 不是「同作用域」）。
    """
    sites = {}
    for rel, path in _iter_app_files():
        tree, scopes = _tree_and_scopes(path)
        for name, scope in scopes.items():
            counters = collections.Counter()
            for sub in _walk_own(scope):
                recorded = None
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr == "status"
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "order"
                        ):
                            recorded = ("assign", ast.unparse(sub.value))
                elif isinstance(sub, ast.Call):
                    text = ast.unparse(sub.func)
                    if _is_order_receiver(text):
                        method = text.rsplit(".", 1)[-1]
                        if method in _ORDER_WRITE_METHODS:
                            for kw in sub.keywords:
                                if kw.arg == "status":
                                    recorded = (method, ast.unparse(kw.value))
                if recorded is not None:
                    kind, expr = recorded
                    counters[kind] += 1
                    key = (rel, name, kind, counters[kind])
                    sites[key] = (expr, sub, _innermost_block(tree, sub))
    return sites


def _block_calls_marker(block, marker="mark_submitted"):
    """`block` 这一支里（⛔ 不含嵌套函数 / 类体）有没有调 `marker`。"""
    if block is None:
        return False
    for sub in _walk_own(block):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute) and func.attr == marker:
                return True
            if isinstance(func, ast.Name) and func.id == marker:
                return True
    return False


#: ⭐⭐ **订单状态写入点登记表（写入点粒度）**。键与 `_order_status_write_sites()` 的键同形。
#: 每一格 = 那个落点写的是什么 + **它为什么不可能写出「待验收」**（或它靠什么留下交单时刻）。
#:
#: `verdict` 三档（每一档下面都有一条**会在实现被改坏时变红**的断言）：
#: - `"literal"`：写的是**字面量状态**（`OrderStatus.X` / 字符串）⇒ 断言它**不含** `PENDING_REVIEW`；
#: - `"gated"`：写的是变量，但**外面有一道具名接受集闸** ⇒ 断言那个集合**真的存在、非空、
#:   且不含 `PENDING_REVIEW`**（⛔ 不是靠注释声称）；
#: - `"needs_marker"`：写的是变量且**没有闸** ⇒ 断言**它自己所在那一支**里调了 `mark_submitted`
#:   （⛔ 不是「同作用域里有」——那正是放走那个洞的粒度）。
ORDER_STATUS_WRITE_SITES = {
    ("business/jidanpu_upstream.py", "_promote_accepted_pool_order", "assign", 1): {
        "expr": "OrderStatus.IN_PROGRESS",
        "verdict": "literal",
        "why": "上家确认抢单后的补录转正只进入代练中，由共用的 set_taker 写接单和首次发单时间。",
    },
    ("business/order_status_persistence.py", "persist_order_status", "assign", 1): {
        "expr": "status",
        "verdict": "needs_marker",
        "why": "本地流转和下家回传都可写入待验收，共用首次交单时间写入",
        "tests": (
            "tests.test_order_column_fields.SubmittedAtWritePathTests"
            "::test_submit_writes_submitted_at_to_the_database_row",
            "tests.test_order_column_fields.SubmittedAtWritePathTests"
            "::test_generic_transition_into_pending_review_also_writes_it",
            "tests.test_order_time_fields.PeerInboundSubmittedAtTests"
            "::test_an_upstream_push_into_pending_review_writes_submitted_at",
        ),
    },
    # ── 对端同步（绕过状态机的四条）────────────────────────────────────────
    ("business/peer_inbound.py", "apply_events", "assign", 1): {
        "expr": "raw_status",
        "verdict": "gated",
        "gate": ("apps.business.peer_sync", "DOWNSTREAM_ACCEPTED_STATUSES"),
        "why": "`order.update` 支：状态被下游接受集闸住（业务方 2026-09-26 裁定），"
               "该集合不含 `PENDING_REVIEW`。⭐ D-0364（2026-11-02，案 A）：赋值判据从"
               "裸的接受集收窄成 `inbound_status_written`（= 接受集 **且非** 池内豁免）——"
               "本地在 `PEER_POOL_LOCAL_STATUSES` 两池内态且入站 `IN_PROGRESS` 时状态"
               "**不写**（上家过期视图回放不许把回池单打回代练中；变异判据 ="
               "`tests.test_peer_p3_inbound_down` 的池内豁免用例，去掉判据即红）；"
               "本网口径不变：这把闸依旧**写不出待验收**",
    },
    ("business/peer_inbound.py", "apply_events", "assign", 2): {
        "expr": "OrderStatus.UNASSIGNED if not (promoted_hall or promoting_claim) else OrderStatus.IN_PROGRESS",
        "verdict": "literal",
        "why": "接手类事件（`order.assign` / `order.grab`）⇒ 落成**未分配**（业务方 2026-09-27 裁定）。"
               "⭐ D-0303（2026-10-25）例外支：该事件是 peer 镜像行转正的产物（`promoted_hall`）"
               "⇒ 挂抢单打手落**代练中**（业务方口径，peer P4「接手即未分配」的唯一例外）；"
               "⭐ D-0343（2026-10-30）例外支补齐：转正确认在途（`promoting_claim`）同落**代练中**"
               "——更新支此前漏挂这道闸（`_promote_claiming_mirror` 两个调用点各带一份，docstring 本就声明）；"
               "两支都是字面量、都不含「待验收」",
    },
    ("business/peer_inbound.py", "apply_events", "assign", 3): {
        "expr": "OrderStatus.REFUNDED",
        "verdict": "literal",
        "why": "撤单事件 ⇒ 落成**已退单**",
    },
    ("business/peer_inbound.py", "apply_events", "assign", 4): {
        "expr": "raw_status",
        "verdict": "gated",
        "gate": ("apps.business.peer_sync", "DOWNSTREAM_ACCEPTED_STATUSES"),
        "why": "⭐ A2 / M-2（2026-10-25 审计批，D-0305 裁定）：未知 kind 支照抄的 `raw_status` "
               "现在必须过**下家接受集**（上面 `apply_events` 的那道闸盖住本支）——"
               "集外（含 `PENDING_REVIEW`）整条硬跳过。⭐ 改准：本落点原为 `needs_marker`"
               "（批次 5 线 E 只补了交单时刻、把接受集挂账待裁）；那笔挂账已裁 ⇒ 能走到"
               "这一行的状态必在集内，`mark_submitted` 成了可证的死代码、已移除。"
               "⭐ D-0364（2026-11-02，案 A）：本支也套了**池内豁免**（与 assign,1 同一把"
               "`PEER_POOL_LOCAL_STATUSES` × `IN_PROGRESS` 判据，唯一出处共用）——"
               "豁免时状态不写、审计行 reason 写「状态保持」；本网口径不变：仍**写不出待验收**",
    },
    ("business/peer_inbound.py", "apply_events", "create", 1): {
        "expr": "new_status",
        "verdict": "gated",
        "gate": ("apps.business.peer_sync", "DOWNSTREAM_ACCEPTED_STATUSES"),
        "why": "⭐ A2 / M-2（2026-10-25 审计批，D-0305 裁定）：条件表达式的 else 支（首条 "
               "`order.update` 与未知 kind 照抄 `raw_status`）现在必须过**下家接受集**"
               "（上面 `apply_events` 的那道闸在建单之前跑）⇒ 建单支**写不出待验收**。"
               "⭐ D-0303（2026-10-25）：then 支的 `promoting_claim` 例外（claiming 镜像行"
               "的抢单转正 ⇒ 建**代练中**）不涉待验收。⭐ 改准（2026-10-26）：表达式收成"
               "局部变量 `new_status`、与隐藏单转正复用支**共用**同一份（`_apply_peer_order_fields`"
               " 那格的 `status` 就是它）；判据与闸一个字没变。"
               "（批次 5 线 E 的挂账「建单支没有接受集闸」已随 A2 裁定关闭）",
    },
    # ── 建单（字面量状态，不可能落在待验收）────────────────────────────────
    ("business/api/order_views.py", "OrderViewSet.copy", "create", 1): {
        "expr": "OrderStatus.UNASSIGNED",
        "verdict": "literal",
        "why": "复制订单 ⇒ 新单从未分配开始",
    },
    ("business/api/order_views.py", "OrderViewSet.split_local_order", "create", 1): {
        "expr": "OrderStatus.UNASSIGNED",
        "verdict": "literal",
        "why": "拆单 ⇒ 新单从未分配开始",
    },
    ("business/api/order_views.py", "OrderViewSet.split_priced_order", "create", 1): {
        "expr": "OrderStatus.UNASSIGNED",
        "verdict": "literal",
        "why": "并列拆单 ⇒ 新单从未分配开始",
    },
    # ⭐ D-0325：转正改成「复用隐藏单优先」后，`promote_upstream_mirror`
    #    的字段（含 `status`）收进**共用的一份** `promote_fields` 字典，新建支
    #    `Order.objects.create(**promote_fields)`、复用支 `.update(**promote_fields)`
    #    ——两支的 `status=` 都经 `**` 展开，本网声明的扫描面（`X.status = …` 与
    #    `Order.objects.<写方法>(status=…)`）**看不见 `**` 展开的字典** ⇒ 该落点不再
    #    进 built 集，本表相应撤销（登记表与 built 集**恰好相等**是本网的判据）。
    #    行为判据不丢：`tests.test_upstream_hall_shadow.PromoteReuseTests` 与
    #    `tests.test_jidanpu_upstream_hall` 的转正用例都断言库里 `IN_PROGRESS` + MAIN 行；
    #    字典里的值仍是字面量 `OrderStatus.IN_PROGRESS`（复用支与新建支共用同一份，
    #    那一格只剩一处、改坏会被上面两处行为用例红）。
    # ⚠️ D-0324：建单语句从 `record_assigned_order` 搬进它的建单核心
    #    `_create_pool_order`（复活重录与首次录池共用同一份投影）——表达式 / 判据一字未改。
    ("business/jidanpu_upstream.py", "_create_pool_order", "create", 1): {
        "expr": "OrderStatus.UNASSIGNED",
        "verdict": "literal",
        "why": "上家指派落单 ⇒ 从未分配开始（接手由随后那一步做）",
    },
    ("business/recycle.py", "restore_order", "assign", 1): {
        "expr": "OrderStatus.UNASSIGNED",
        "verdict": "literal",
        "why": "回收站恢复（业务方 2026-10-27 拍板）：删除时在厅（WAITING_GRAB 且非隐藏单）"
               "⇒ 回到「未分配」，等业务方手工重抛；⛔ 不走 apply_transition、⛔ 不排任何事件",
    },
    # ── 镜像卡派生的隐藏单（⭐ 业务方拍板；`Order.hall_card`）────────────
    ("business/hall_shadow.py", "ensure_for_card", "update", 1): {
        "expr": "OrderStatus.WAITING_GRAB",
        "verdict": "literal",
        "why": "卡 `gone` 后又回到大厅 ⇒ **同一张**隐藏单复活回待抢（条件更新只认 REFUNDED；"
               "⛔ 不新建第二张，1:1 由 `Order.hall_card` 的 OneToOne 唯一索引兜）",
    },
    ("business/hall_shadow.py", "ensure_for_card", "assign", 1): {
        "expr": "OrderStatus.WAITING_GRAB",
        "verdict": "literal",
        "why": "复活支更新成功后回写内存对象的 `status`——紧接着的 `queue_hall_launch` 里"
               "`expected_shape(order, binding)` 读的就是这个内存状态（不写就会按旧的"
               "`REFUNDED` 判成「对方不该有这张单」⇒ 复活不重新抛）。字面量、写不出待验收",
    },
    ("business/hall_shadow.py", "refund_for_card", "update", 1): {
        "expr": "OrderStatus.REFUNDED",
        "verdict": "literal",
        "why": "卡离开大厅（摘牌 / 关开关 / 解绑 / 卡死回收按证据下架）⇒ 隐藏单跟着退"
               "（条件更新只认 WAITING_GRAB；已退的幂等、其余状态不覆盖）",
    },
    # ⭐ 隐藏单转正复用的**唯一**字段应用口（`apply_events` 建单支在
    #    `link is None` 且卡有隐藏单时调它）。状态由调用方传**同一个** `new_status`
    #    （与新建支共用那一份表达式），能写出什么状态由 `apply_events` 的
    #    `DOWNSTREAM_ACCEPTED_STATUSES` 闸回答 ⇒ 登记成 `gated`（闸是同一个）。
    ("business/peer_inbound.py", "_apply_peer_order_fields", "assign", 1): {
        "expr": "status",
        "verdict": "gated",
        "gate": ("apps.business.peer_sync", "DOWNSTREAM_ACCEPTED_STATUSES"),
        "why": "隐藏单转正复用建单支的同一份字段 / 同一个 `new_status`（⭐ 2026-10-26）；"
               "闸在调用方 `apply_events` 建单之前（集外整条硬跳过）",
    },
    # ── 上家鸡蛋铺镜像卡派生的隐藏单（⭐ D-0325；`Order.upstream_hall_card`）
    #    ——三条与 `hall_shadow` 那组**同形同判**（模板照抄、判据独立维护）─────────
    ("business/upstream_hall_shadow.py", "ensure_for_card", "update", 1): {
        "expr": "OrderStatus.WAITING_GRAB",
        "verdict": "literal",
        "why": "卡 `gone` 后又回到大厅 / 软删后被再派生 ⇒ **同一张**隐藏单复活回待抢"
               "（条件更新只认 REFUNDED / 软删；⛔ 不新建第二张，1:1 由 "
               "`Order.upstream_hall_card` 的 OneToOne 唯一索引兜）",
    },
    ("business/upstream_hall_shadow.py", "ensure_for_card", "assign", 1): {
        "expr": "OrderStatus.WAITING_GRAB",
        "verdict": "literal",
        "why": "复活支条件更新成功后回写内存对象的 `status`——紧接着的 `queue_hall_launch`"
               "里 `expected_shape` 读的就是这个内存状态（不写就会按旧的 `REFUNDED` 判成"
               "「对方不该有这张单」⇒ 复活不重新抛）。字面量、写不出待验收",
    },
    ("business/upstream_hall_shadow.py", "refund_for_card", "update", 1): {
        "expr": "OrderStatus.REFUNDED",
        "verdict": "literal",
        "why": "上家镜像卡离开大厅（拉全轮消失 / 回环拦截转 gone / 僵尸卡下架 / 接单被拒）"
               "⇒ 隐藏单跟着退（条件更新只认 WAITING_GRAB；已退的幂等、其余状态不覆盖；"
               "转正支已清 `upstream_hall_card` ⇒ 不会连坐真单）",
    },
    ("business/upstream_hall_shadow.py", "refund_for_cards", "update", 1): {
        "expr": "OrderStatus.REFUNDED",
        "verdict": "literal",
        "why": "批量下架时先锁定与镜像卡关联的未软删待抢隐藏单，只按这些订单主键落已退单。",
    },
}

#: ⭐ **`published_at` 的写入点清单**（业务方口径：抛大厅 **或** 第一次接手，取先发生的）。
#: 键 = 调 `mark_published` 的作用域；值 = 为什么这里该写。
#: ⚠️ 「恰好等于」这条断言就是**完整**的判据（下面 `test_the_published_at_list_is_exactly_...`）
#: ——⛔ 不再单列一张「额外落点」常量假装它不是空集（那只是把同一件事说两遍）。
PUBLISHED_AT_WRITE_POINTS = {
    ("business/order_status_persistence.py", "persist_order_status"):
        "① 本地抛到大厅成功时由调用方显式开启",
    ("business/claim.py", "claim_order"):
        "② 第一次接手（`/claim` 的条件更新成功那一刻）",
    ("business/performance.py", "set_taker"):
        "② 第一次接手（`/assign` 与上家线同步指派都调它）",
    ("openapi/api/integration_orders.py", "IntegrationOrderActionView.post"):
        "① SDK 发到内部大厅。状态流转成功后登记第一次发单时间。",
}

#: ⭐ **写非空 `claimed_at` 的落点**（`claimed_at` = 「这个人何时接手」）。
#: 与 `PUBLISHED_AT_WRITE_POINTS` 的**关系**是本题要证明的那条：
#: **每一处写 `claimed_at` 的地方都必须同时写 `published_at`**（接手即发单）。
#: ⚠️ `performance.reset_taker_performance` 写 `claimed_at = None`（清接手人）**不在**本表里
#: ——它按定义不是「接手」，而且 `published_at` **不清空**（见模块 docstring 的口径 2）。
CLAIMED_AT_WRITE_POINTS: frozenset = frozenset({
    ("business/claim.py", "claim_order"),
    ("business/performance.py", "set_taker"),
})


def _sites_exist(dotted_names):
    """清单里点的用例名**真的存在**（⛔ 不是照着愿望填的字符串）。"""
    import importlib

    missing = []
    for dotted in dotted_names:
        target, _, test_name = dotted.partition("::")
        module_name, _, class_name = target.rpartition(".")
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, class_name)
        except (ImportError, AttributeError):
            missing.append(dotted)
            continue
        if not hasattr(cls, test_name):
            missing.append(dotted)
    return missing


class TimeFieldWritePathNetTests(TenantTestCase):
    """结构性网：写入点粒度清单 / 两张时间轴清单对拍 / 两列永不清空。"""

    # ── ④ `claimed_at` 与 `published_at` 清单对拍 ──────────────────────────
    def test_every_claimed_at_writer_also_marks_published(self):
        """⭐ **每一处写非空 `claimed_at` 的地方都调了 `mark_published`**（接手即发单）。

        判别力：把 `claim.py` 或 `performance.set_taker` 里那句 `mark_published` 删掉，
        本用例当场红（那张单的「发单时间」就永远空 ⇒ 按那一档筛不出来）。
        """
        missing = _non_null_claimed_at_scopes() - _marker_scopes("mark_published")
        self.assertEqual(
            missing, set(),
            f"这些地方写了 `claimed_at`（= 接手）却没写 `published_at`：{sorted(missing)}"
            "——接手即发单，「发单时间」那一档会永远筛不出这些单",
        )

    def test_the_claimed_at_list_is_exactly_the_registered_one(self):
        """`claimed_at` 的写入点集合与登记表**恰好一致**（新增一处必须回来登记）。"""
        self.assertEqual(_non_null_claimed_at_scopes(), set(CLAIMED_AT_WRITE_POINTS))

    def test_the_published_at_list_is_exactly_the_registered_one(self):
        """⭐ `mark_published` 的调用点集合与登记表**恰好一致**——这一条**就是**完整判据。

        ⚠️ 措辞改准（独立评审 P2-3）：第一版还单列了一张 `PUBLISHED_AT_EXTRA_WRITE_POINTS = frozenset()`
        并声称「⛔ 不是靠空集静默成立」，而**实现就是靠空集**（多出来的落点为空 ⇔ 恰好相等）。
        现在只留这一条**恰好相等**的断言：多一处、少一处都红，⛔ 不再用第二张常量把同一件事说两遍。
        """
        self.assertEqual(
            _marker_scopes("mark_published"), set(PUBLISHED_AT_WRITE_POINTS)
        )

    # ── ③ 进「待验收」的路径：**写入点粒度**穷举 ──────────────────────────
    def test_every_order_status_write_site_is_registered_with_its_expression(self):
        """⭐⭐ 每个订单状态落点的 `{键: 表达式}` 与登记表**恰好相等**（写入点粒度）。

        判别力（三条都实测过）：
        - 在某个作用域里**新增**一处状态写入 ⇒ 多出一个键 ⇒ 红；
        - 把某处的**字面量状态改成变量**（如 `OrderStatus.UNASSIGNED` → `raw_status`）
          ⇒ 表达式文本变了 ⇒ 红（⛔ 这正是评审那条探针的形状）；
        - 删掉一处 ⇒ 少一个键 ⇒ 红。
        """
        measured = {
            key: value[0] for key, value in _order_status_write_sites().items()
        }
        self.assertEqual(measured, {
            key: entry["expr"] for key, entry in ORDER_STATUS_WRITE_SITES.items()
        })

    def test_status_persistence_has_only_the_two_registered_call_sites(self):
        """覆盖源码中的直接调用及常规导入别名。"""
        module = "apps.business.order_status_persistence"
        function = "persist_order_status"
        callers = collections.Counter()
        for relative, path in _iter_app_files():
            tree, scopes = _tree_and_scopes(path)
            callees = {f"{module}.{function}"}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == module:
                            callees.add(f"{alias.asname or alias.name}.{function}")
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        alias_name = alias.asname or alias.name
                        if node.module in {module, "order_status_persistence"} and alias.name == function:
                            callees.add(alias_name)
                        elif node.module in {None, "apps.business"} and alias.name == "order_status_persistence":
                            callees.add(f"{alias_name}.{function}")
            for name, scope in scopes.items():
                callers.update(
                    (relative, name) for node in _walk_own(scope)
                    if isinstance(node, ast.Call) and ast.unparse(node.func) in callees
                )
        self.assertEqual(dict(callers), {
            ("business/order_status.py", "apply_transition"): 1,
            ("business/peer_inbound.py", "apply_upstream_events"): 1,
        })

    def test_every_literal_write_site_cannot_write_pending_review(self):
        """`verdict="literal"` 的落点：写的是**字面量**，断言它不含 `PENDING_REVIEW`。"""
        for key, entry in ORDER_STATUS_WRITE_SITES.items():
            if entry["verdict"] != "literal":
                continue
            with self.subTest(site=key):
                self.assertNotIn("PENDING_REVIEW", entry["expr"])
                self.assertNotIn("raw_status", entry["expr"])

    def test_every_gated_write_site_has_a_real_gate_that_excludes_pending_review(self):
        """⭐⭐ `verdict="gated"` 的落点：**真的去解析那道闸**，并断言它排除 `PENDING_REVIEW`。

        ⚠️ 这一条是本轮的关键：第一版只在注释里**声称**「`order.update` 支有接受集闸」，
        网一个字不问 ⇒ 那道闸被删掉/改宽了也照样绿。现在闸是**可执行**的：
        `importlib` 取那个集合，断言它**非空**且**不含** `PENDING_REVIEW`。
        """
        import importlib

        pending = str(OrderStatus.PENDING_REVIEW)
        for key, entry in ORDER_STATUS_WRITE_SITES.items():
            if entry["verdict"] != "gated":
                continue
            module_name, attr = entry["gate"]
            with self.subTest(site=key, gate=attr):
                gate = getattr(importlib.import_module(module_name), attr)
                self.assertTrue(gate, f"{module_name}.{attr} 是空集 —— 那道闸等于没有")
                self.assertNotIn(
                    pending, set(gate),
                    f"{key} 登记的闸 `{attr}` 里含 {pending} ⇒ 这一支其实能写出「待验收」"
                    "（要么换成 `needs_marker` 并补 `mark_submitted`，要么收窄那个集合）",
                )

    def test_every_ungated_write_site_marks_submitted_in_its_own_branch(self):
        """⭐⭐ `verdict="needs_marker"` 的落点：**它自己所在那一支**里必须调了 `mark_submitted`。

        ⚠️ 粒度是**这一支**（`_innermost_block`），⛔ 不是「同作用域里有一处就行」——
        后者正是第一版放走那个洞的形状（同作用域里另外两处调了标记，就把它一起遮住了）。
        判别力：把 `peer_inbound` 未知 kind 支那一句 `mark_submitted` 删掉 ⇒ 本用例红
        （实测过：删掉后这条与那条探针用例同时红，而**作用域粒度**的旧网当时全绿）。
        """
        sites = _order_status_write_sites()
        for key, entry in ORDER_STATUS_WRITE_SITES.items():
            if entry["verdict"] != "needs_marker":
                continue
            _expr, node, block = sites[key]
            with self.subTest(site=key, line=node.lineno):
                self.assertTrue(
                    _block_calls_marker(block),
                    f"{key[0]}:{node.lineno} 所在的那一支能把单写成「待验收」，"
                    "但那一支里没有 `mark_submitted` ⇒ 这些单按「交单时间」永远筛不出来",
                )

    def test_every_write_site_verdict_is_one_of_the_three_known_kinds(self):
        """登记表自身的形状检查：`verdict` 只能是那三档，且 `needs_marker` 必须登记用例。"""
        for key, entry in ORDER_STATUS_WRITE_SITES.items():
            with self.subTest(site=key):
                self.assertIn(entry["verdict"], {"literal", "gated", "needs_marker"})
                if entry["verdict"] == "gated":
                    self.assertIn("gate", entry)
                if entry["verdict"] == "needs_marker":
                    self.assertTrue(entry.get("tests"), f"{key} 没登记对应用例")
                    self.assertEqual(_sites_exist(entry["tests"]), [])

    def test_the_registered_tests_really_exist(self):
        """⭐ 两句清单里点的用例名**真的存在**（`importlib` 取，⛔ 不是字符串就完事）。"""
        names = [
            dotted
            for entry in ORDER_STATUS_WRITE_SITES.values()
            for dotted in entry.get("tests", ())
        ]
        self.assertTrue(names, "登记表里一条用例都没登记？")
        self.assertEqual(_sites_exist(names), [])

    # ── 两列永不被写成 `NULL` ───────────────────────────────────────────────
    def test_nothing_in_apps_ever_assigns_none_to_the_two_time_columns(self):
        """⭐ 结构性钉住「**不清空**」：全仓没有一处把这两列写成 `None`。

        ⚠️ 与 `completed_at` **有意不同**（那一列在「离开已完成」时会清）。
        判别力：在 `apply_transition` 里加一句 `order.published_at = None` ⇒ 当场红。
        """
        offenders = []
        for rel, path in _iter_app_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for sub in ast.walk(tree):
                if isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and target.attr in ("published_at", "submitted_at")
                            and _is_none(sub.value)
                        ):
                            offenders.append(f"{rel}:{sub.lineno} {ast.unparse(sub)}")
                if isinstance(sub, ast.Call):
                    for kw in sub.keywords:
                        if (
                            kw.arg in ("published_at", "submitted_at")
                            and _is_none(kw.value)
                        ):
                            offenders.append(f"{rel}:{sub.lineno} {ast.unparse(sub)}")
        self.assertEqual(
            offenders, [],
            "这两列是**筛选维度**，⛔ 不许清空（与 `completed_at` 有意不同）："
            f"{offenders}",
        )
