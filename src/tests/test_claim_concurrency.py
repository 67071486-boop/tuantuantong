"""Task 6：并发抢单（spec §7.3 / §7.4）。

⚠️ **为什么必须用 `TransactionTestCase` 而不是 `TenantTestCase`**：
`TenantTestCase` 继承 Django 的 `TestCase`，把每条测试包在一个事务里——
**其他线程看不到未提交的夹具**，于是最可能的结果是「全部请求都失败」。
如果断言写成「成功数 ≤ 1」，这种测试**恒绿**，并发保护根本没被验证
（P1 的教训：红线测试曾靠测试顺序假通过）。

⚠️ **为什么每个线程要 `schema_context`**：线程各有独立数据库连接
（`django.db.connections` 是 `thread_critical` 的 `Local`），新连接默认落在
**public** schema 上——那里没有 `business_order` / `business_performance`。
必须在线程内显式把 schema 切到租户上，并且**先 `connections.close_all()`**，
免得复用到别的 schema 上留下的连接。

⚠️ **`django_tenants` 没有 `TenantTransactionTestCase`**（读码：
`django_tenants/test/cases.py` 只有 `TenantTestCase` / `FastTenantTestCase` /
`SubfolderTenantTestCase`），所以夹具照 `TenantTestCase.setUpClass` 自己搭。

⚠️ **本文件证明什么、不证明什么**（如实写清楚，免得被当成「并发已经全对」）：
它证明的是「**条件更新**这条 `UPDATE ... WHERE user_id IS NULL` 在 N 个并发写者下
恰好一个赢」。它**不**证明端点上两笔写入的原子性
（那是 `test_claim.py::ClaimAtomicityTests` 的活），也**不**覆盖跨进程 / 多副本部署
（本地线程模型与真实部署的差异不在本用例的范围内）。

📌 **P3c Task 3 之后本文件有两条用例，它们证明的是两件不同的事，别互相外推**：
① `test_exactly_one_wins`：条件更新在 N 个并发写者下**恰好一个赢**；
② `test_a_committed_withdraw_is_not_overwritten_by_an_inflight_claim`：
**已经写进数据库的撤回**不会被在飞的抢单抹掉（`apply_transition` 的复读 + 行锁）。
⚠️ ②**不**证明「撤回永远赢」：反向顺序（抢单**先**提交、撤回到达）由 `/withdraw`
自己的入口闸门（只接受 `WAITING_GRAB`）拒绝 ⇒ 那种顺序下**先提交者赢**——
这是计划 §2 C1 的如实边界（业务方裁定），不是可以外推成
「撤回永远赢」的口径。两条都**不**覆盖跨进程 / 多副本部署。

⭐ **为什么还多插了一个同步点**（`_BARRIER_BEFORE_THE_WRITE`）——**实测逼出来的**：
只靠起跑线的 `threading.Barrier` + 8 个线程，本用例**抓不住**「先 SELECT 再 UPDATE」
这种非原子写法：实测把那 5 行改写成读-改-写后跑 5 次，**有 1 次是绿的**
（线程被调度成事实串行，没有重叠窗口）。**绿的并发测试等于没测并发**。
修法是在「即将执行那笔写」的**前一刻**再对齐一次：`check_grab_limits` 是
`claim_order` 里紧邻条件更新的那个调用（spec §7.2 要求限制校验在更新之前），
所以在本文件里把它包一层——**先真的调用它**（限制照旧生效，本用例不放过任何校验），
再让所有线程在栅栏上对齐，然后各自冲向那笔写。这样读-改-写的「读」阶段
必然**全部**先于任何一次「写」，竞争窗口是构造出来的而不是碰运气。
⚠️ 代价与边界：这个同步点依赖 `claim_order` **确实**会调用 `check_grab_limits`
（漏调 ⇒ 栅栏超时 ⇒ `BrokenBarrierError` ⇒ 用例**响亮变红**，不会静默通过）。
"""
import threading
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
)

from apps.business import claim
from apps.business.claim import claim_order
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.order_status import IllegalTransition, apply_transition
from apps.core.error_codes import ErrorCode

TENANT_SCHEMA = "test"
COMPETITORS = 8
# 栅栏超时：正常应远小于 1s（8 条本地连接），放 10s 只是为了在机器抖动时不误判
_BARRIER_TIMEOUT = 10


class ConcurrentClaimTests(TransactionTestCase):
    """N 个人同时抢同一单：**恰好 1 个成功**（spec §7.3 的硬要求）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command(
            "migrate_schemas", schema_name="public", interactive=False, verbosity=0
        )
        cls.tenant = get_tenant_model()(schema_name=TENANT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(
            tenant=cls.tenant, domain="tenant.test.com"
        )
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_exactly_one_wins(self):
        game = Game.objects.create(name="原神")
        order = Order.objects.create(
            game=game,
            order_no="20260920000000000001",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        users = [
            User.objects.create_user(username=f"taker{i}") for i in range(COMPETITORS)
        ]
        order_id, user_ids = order.id, [u.id for u in users]

        barrier = threading.Barrier(COMPETITORS)
        results = {}

        # ⚠️ 同步点：**先真的做限制校验**，再在栅栏上对齐——所有线程此刻都还没碰那笔写。
        #    它不改变被测行为（`check_grab_limits` 照常执行、照常可能抛 `ClaimError`），
        #    只是把「读-改-写」的读阶段**全部**排到写阶段之前（见模块 docstring 实测理由）。
        real_check_grab_limits = claim.check_grab_limits

        def _check_limits_then_align(**kwargs):
            real_check_grab_limits(**kwargs)
            barrier.wait(timeout=_BARRIER_TIMEOUT)

        def _try(index):
            # 每个线程一条独立连接：先关掉本线程可能残留的连接，再切 schema
            connections.close_all()
            with schema_context(TENANT_SCHEMA):
                try:
                    # `roles` 必填（Ruling 47）：本用例的 8 个竞争者都是代练员
                    # ⚠️ 事务边界与**生产**同形：`order_views.claim`
                    #    用 `transaction.atomic()` 包住 `claim_order`，而
                    #    `apply_transition` 的 `select_for_update` **要求调用方在事务里**
                    #    （autocommit 下 Django 抛 `TransactionManagementError`）。
                    #    这不是放宽断言——三条断言（恰好 1 个赢 / 其余全是
                    #    `ORDER_ALREADY_TAKEN` / DB 里的归属人）一条都没动。
                    with transaction.atomic():
                        claim_order(
                            user_id=user_ids[index],
                            order_id=order_id,
                            nodes={"grab_hall.take_order"},
                        )
                    results[index] = "ok"
                except Exception as exc:          # noqa: BLE001 —— 分类要落到结果里
                    results[index] = getattr(exc, "code", repr(exc))
                finally:
                    connections.close_all()

        threads = [
            threading.Thread(target=_try, args=(i,)) for i in range(COMPETITORS)
        ]
        with mock.patch(
            "apps.business.claim.check_grab_limits",
            side_effect=_check_limits_then_align,
        ):
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        # ⓪ 哨兵：所有线程都跑完了、都留下了结果。
        #    没有这一条时，「3 个线程死在启动阶段」会让下面的断言在**样本变少**的情况下
        #    依然成立（判别力判据 1 的同族形状：起点/样本必须先被证明）。
        self.assertFalse(
            [t.name for t in threads if t.is_alive()], "有线程没在 30 秒内退出"
        )
        self.assertEqual(
            len(results), COMPETITORS, f"有线程没留下结果（死在 try 之外）: {results}"
        )

        # ① 恰好一个成功（spec §7.4：断言「恰好 1 个成功」而不是「没有报错」）
        winners = [i for i, r in results.items() if r == "ok"]
        self.assertEqual(len(winners), 1, f"结果: {results}")

        # ② 其余**全部**是「已被接走」，不是别的错——否则说明这条用例测错了东西
        losers = [r for r in results.values() if r != "ok"]
        self.assertEqual(
            set(losers), {ErrorCode.ORDER_ALREADY_TAKEN},
            f"失败原因必须统一是 ORDER_ALREADY_TAKEN: {results}",
        )

        # ③ 数据库里的归属就是那个唯一成功者（不是「状态对了但没写人」）
        perf = Performance.objects.get(order_id=order_id, emp_type=EmpType.MAIN)
        self.assertEqual(perf.user_id, user_ids[winners[0]])
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_a_committed_withdraw_is_not_overwritten_by_an_inflight_claim(self):
        """**已提交的撤回不会被在飞的抢单覆盖**（P3c Task 3 的承重用例；spec §16.4 #16）。

        构造（两段栅栏，顺序是**造出来**的，不是碰运气）：
        A（代练员）走 `claim_order`；B（客服）走**真实撤回路径**
        （`apply_transition(order, UNASSIGNED, nodes={"order.change_status"})`，即 `WAITING_GRAB → UNASSIGNED`
        这条唯一写入口上的边）。A 在 `claim_order` 内已经读完订单（手上是 `WAITING_GRAB`）、
        且**尚未写状态**时落在第一段栅栏；B 这时提交撤回；B 提交后过第二段栅栏，
        才放 A 去写那笔状态。

        判据：① 终态必须是 `UNASSIGNED`（**撤回赢**）；② A 拿到的是**业务失败**
        （`IllegalTransition`），不是静默成功；③ A 那半截绩效条件更新**没有留下脏行**
        （`user_id` 仍为空——回滚的证据）。

        ⚠️ **为什么 B 直接调 `apply_transition` 而不是走 HTTP `/withdraw`**：本用例要钉的是
        **唯一漏斗**（`apply_transition`）上的复读 + 行锁，直接调它才是**最少间接层**的真实
        业务路径（它本来就是唯一写状态入口）；走 HTTP 还要为 B 线程再建一套登录凭据，
        多出来的只是「端点闸门 + 留痕」这两层与本判据无关的东西。
        `nodes={"order.change_status"}` 与目标态都是 `TRANSITIONS[(WAITING_GRAB, UNASSIGNED)]` 的真实边，
        **不是**手写的裸 `UPDATE`。

        ⚠️ **为什么 A 要包一层 `transaction.atomic()`**：`apply_transition` 的
        `select_for_update` **要求调用方在事务里**——autocommit 下 Django 会抛
        `TransactionManagementError`（响亮失败）。生产端点的包法与这里同形
        （`order_views.claim` 用 `transaction.atomic()` 包住 `claim_order`），
        所以这不是为测试放宽，而是**更贴近生产**。

        ⚠️ **本用例证明什么、不证明什么**（如实）：
        它证明「**已经提交的撤回**不会被随后提交的抢单覆盖」。
        它**不**证明「撤回永远赢」——反向顺序（抢单**先**提交、撤回到达）会被 `/withdraw`
        自己的入口闸门（只接受 `WAITING_GRAB`）拒绝 ⇒ 那种顺序下**先提交者赢**
        （计划 §2 C1 的边界）。
        ⚠️ **本用例对行锁那一半零判别力**（独立评审实测：把 `apply_transition` 里那次复读的
        `select_for_update()` 换回普通 `get()`，本用例 **6/6 仍绿**）——原因是
        **结构决定论**，不是「窗口小」：第二段 `Barrier(2)`（`withdraw_committed`）把 B 的
        **提交**严格排在 A 的**复读**之前，READ COMMITTED 下那次无锁 SELECT **必然**读到
        `UNASSIGNED` ⇒ 加不加锁**每次都会抛** ⇒ 必绿。
        所以它只钉住**复读比对**这一半；行锁那一半**没有**被本用例机械钉住，
        必要性由**读码论证**：`SELECT … FOR UPDATE` 才是真正的串行化点，没有它，
        A 的复读与 A 的写入之间仍可插入 B 的提交
        （读-改-写的经典窗口；Step 5 变异 ② 的如实登记）。
        """
        game = Game.objects.create(name="原神")
        order = Order.objects.create(
            game=game,
            order_no="20260920000000000002",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        taker = User.objects.create_user(username="inflight-taker")
        order_id, taker_id = order.id, taker.id

        # 两段栅栏（同模块 `_BARRIER_BEFORE_THE_WRITE` 的理由）：
        #   ① `read_done`：A 已读完订单、即将写状态 ⇄ B 可以开始撤回；
        #   ② `withdraw_committed`：B 已提交撤回 ⇄ 才放 A 去写。
        # 只靠起跑线对齐抓不住「读 → 撤回 → 写」这个顺序（线程可能被调度成事实串行）。
        read_done = threading.Barrier(2)
        withdraw_committed = threading.Barrier(2)
        claim_result = {}
        withdraw_result = {}

        real_check_grab_limits = claim.check_grab_limits

        def _check_limits_then_two_barriers(**kwargs):
            real_check_grab_limits(**kwargs)
            read_done.wait(timeout=_BARRIER_TIMEOUT)
            withdraw_committed.wait(timeout=_BARRIER_TIMEOUT)

        def _inflight_claim():
            connections.close_all()
            with schema_context(TENANT_SCHEMA):
                try:
                    # 事务边界与生产同形：`select_for_update` 必须在事务里（否则响亮抛
                    # `TransactionManagementError`）。异常冒出去 ⇒ 这一层 atomic 回滚，
                    # 于是「绩效条件更新写了一半」不会留下脏行（判据 ③）。
                    with transaction.atomic():
                        claim_order(
                            user_id=taker_id, order_id=order_id, nodes={"grab_hall.take_order"}
                        )
                    claim_result["outcome"] = "ok"
                except Exception as exc:          # noqa: BLE001 —— 分类要落到结果里
                    claim_result["outcome"] = getattr(exc, "code", type(exc).__name__)
                    claim_result["exc"] = exc
                finally:
                    connections.close_all()

        def _withdraw():
            connections.close_all()
            with schema_context(TENANT_SCHEMA):
                try:
                    read_done.wait(timeout=_BARRIER_TIMEOUT)
                    with transaction.atomic():
                        # 线程内自己现取行：跨线程传 ORM 对象会把 A 的连接/schema 带进来。
                        fresh = Order.objects.get(pk=order_id)
                        apply_transition(
                            fresh, OrderStatus.UNASSIGNED, nodes={"order.change_status"}
                        )
                    withdraw_result["outcome"] = "ok"
                except Exception as exc:          # noqa: BLE001
                    withdraw_result["outcome"] = repr(exc)
                    withdraw_result["exc"] = exc
                finally:
                    # ⚠️ 无条件放行 A：B 失败时若不放行，A 会以栅栏超时收场，
                    #    把「B 失败」伪装成栅栏故障（下面那条断言才是真正的判据）。
                    withdraw_committed.wait(timeout=_BARRIER_TIMEOUT)
                    connections.close_all()

        with mock.patch(
            "apps.business.claim.check_grab_limits",
            side_effect=_check_limits_then_two_barriers,
        ):
            threads = [
                threading.Thread(target=_inflight_claim),
                threading.Thread(target=_withdraw),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        # ⓪ 哨兵：两条线程都跑完、都留下了结果（否则下面的断言在样本缺失时也成立）
        self.assertFalse(
            [t.name for t in threads if t.is_alive()], "有线程没在 30 秒内退出"
        )
        self.assertIn("outcome", claim_result, f"抢单线程没留下结果: {claim_result}")
        self.assertEqual(
            withdraw_result.get("outcome"), "ok", f"撤回线程本身失败: {withdraw_result}"
        )

        # ① 撤回赢：终态是 `UNASSIGNED`（不是 `IN_PROGRESS`）
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        # ② A 拿到**业务失败**，不是静默成功
        self.assertNotEqual(claim_result["outcome"], "ok", f"{claim_result}")
        self.assertIsInstance(claim_result.get("exc"), IllegalTransition)
        # ②b `/claim` 的失败必须**可区分**且消息说真话（计划 Interfaces 的「可区分」要求）：
        #     `IllegalTransition` 由视图翻成 `ErrorCode.BUSINESS` + `str(exc)`（与 `/withdraw`
        #     的失败形态一致），而 `ORDER_ALREADY_TAKEN` 是**另一条**码。这里钉住异常消息里
        #     写的是**数据库里的真实状态**（`UNASSIGNED`），不是调用方内存里那份陈旧的
        #     `WAITING_GRAB` —— 否则用户会看到「非法状态迁移：WAITING_GRAB → IN_PROGRESS」，
        #     与事实不符。
        self.assertIn("该单当前状态已是 UNASSIGNED", str(claim_result["exc"]))
        # ③ 脏行判据：A 的绩效条件更新被回滚，库里没有「绩效行有人、订单却在池内」
        self.assertIsNone(
            Performance.objects.get(order_id=order_id, emp_type=EmpType.MAIN).user_id
        )
