"""`seed_e2e_write_orders` 的两道闸门 + 真库上的订单夹具判据（T2b）。

    **为什么这些判据必须有**：本命令是**写模式覆盖基线**的数据来源——它一旦静默少造一笔
    （或把「已指派」那一笔的接单人漏了），审计只会读到**少一支控件**、**不会红**
    （正是 spec §1 要抓的那类"静默少覆盖"）。所以上半把闸门钉在**不连库**的纯函数上，
    下半把「到底造出了什么」钉在**真库状态**（订单行 + MAIN 绩效行）上。

    ⚠️ 断言一律用 `assertRaisesMessage` 钉**判别内容**，⛔ 不用裸 `assertRaises`（`E-0024` 同族）。
    ⚠️ 期望值（单号 / 状态 / 接手人）在测试里**独立写死**，⛔ 不从 `ORDER_SPECS` 派生——
    拿命令自己的常量去断言命令自己 = 自指断言（`E-0024`），常量被改坏时用例不会红。
"""
from unittest import mock

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.business.models import EmpType, Order, OrderStatus, Performance
from apps.platform.management.commands import reset_e2e_write, seed_e2e_write_orders
from apps.platform.management.commands.reset_e2e_write import Command as ResetCommand
from apps.platform.management.commands.seed_e2e import _seed_tenant_fixtures
from apps.rbac.seed import seed_rbac
from apps.platform.management.commands.seed_e2e_write_orders import (
    ORDER_SPECS,
    WRITE_ORDER_SCHEMA,
    WRITE_ORDER_SCHEMAS,
    Command,
    _assert_write_order_schema,
)

#: 闸门文案里必须出现的关键片段（判据的判别内容；只写命令名会被消息里的命令名满足）。
GATE_MESSAGE = "只认一次性写租户 schema"
DEBUG_MESSAGE = "只在 DEBUG=true 下运行"

#: 夹具的**外部契约读数**：单号 / 状态 / 接手人。**独立写死**（⛔ 不从 `ORDER_SPECS` 派生）。
#: spec §7 要求「至少两种状态」⇒ 这里也把**四种状态**逐条钉住，少一种就红。
EXPECTED_FIXTURES = (
    ("77770000000000000001", "UNASSIGNED", None),
    ("77770000000000000002", "WAITING_GRAB", None),
    ("77770000000000000003", "IN_PROGRESS", "taker001"),
    ("77770000000000000004", "COMPLETED", "taker001"),
)


class SeedE2EWriteOrdersGateTests(SimpleTestCase):
    """两道闸门 + 白名单 + 合成单号的**外部契约值**。"""

    def test_refuses_when_debug_is_off(self):
        """⛔ 闸门一的存在理由：本命令会在租户库里造订单，绝不能在非 DEBUG 环境跑。"""
        with override_settings(DEBUG=False):
            with self.assertRaisesMessage(CommandError, DEBUG_MESSAGE):
                Command().handle(schema=WRITE_ORDER_SCHEMA)

    def test_accepts_the_write_schema(self):
        """正面：白名单里那个值**不抛**。"""
        _assert_write_order_schema("e2e_write")  # 不抛即通过

    def test_rejects_every_other_schema(self):
        """反面：只读租户 `e2e`、开发库、相近名字一律拒绝。

    ⛔ `e2e` 必须在内——它是**本命令最贵的误伤**：在只读租户上造单会改掉读模式的
    基线（那 98 笔订单与按它实测的下界）。⛔ `e2e_write_extra` 是前缀陷阱。
"""
        for schema in ("e2e", "tt_dev", "e2e_write_extra", "e2eX", "", "E2E_WRITE"):
            with self.subTest(schema=schema):
                with self.assertRaisesMessage(CommandError, GATE_MESSAGE):
                    _assert_write_order_schema(schema)

    def test_whitelist_is_the_literal_singleton(self):
        """白名单的**外部契约值**用字面量钉住（⛔ 不是"读常量再比常量"的自指断言）。"""
        self.assertEqual(set(WRITE_ORDER_SCHEMAS), {"e2e_write"})
        self.assertEqual(WRITE_ORDER_SCHEMA, "e2e_write")

    def test_order_numbers_are_synthetic_twenty_digit_constants(self):
        """合成单号必须是**确定的 20 位纯数字**、互不相同（幂等与详情路由都靠它）。

    ⛔ 不许随机 / 不许时间戳：那会让「重复执行结果确定」这条契约不成立。
"""
        numbers = [spec["order_no"] for spec in ORDER_SPECS]
        self.assertEqual(len(numbers), len(set(numbers)), "夹具单号有重复")
        for number in numbers:
            with self.subTest(order_no=number):
                self.assertRegex(number, r"^\d{20}$")
                # `7777` 前缀是**刻意的合成标记**：与真实生成器的 `YYYYMMDD…` 形状区分开。
                self.assertTrue(number.startswith("7777"))

    def test_fixture_covers_at_least_two_states(self):
        """spec §7 的硬要求：至少两种状态（否则订单页行内控件只有一支渲染得出来）。"""
        statuses = {str(spec["status"]) for spec in ORDER_SPECS}
        self.assertGreaterEqual(len(statuses), 2)
        self.assertIn(OrderStatus.UNASSIGNED, statuses)
        self.assertIn(OrderStatus.IN_PROGRESS, statuses)


class SeedE2EWriteOrdersOnRealTenantTests(TenantTestCase):
    """真库判据：跑**生产路径**（`handle()`）后，订单夹具必须真的可用。

    测试租户的 schema 名取字面量 `e2e_write` ⇒ `handle()` 的白名单闸门放行，
    于是这里连「闸门真的接在 `handle()` 上」一起钉住。
"""

    #: 与测试里独立写死的那份读数的状态集合（见模块常量注释）。
    EXPECTED = EXPECTED_FIXTURES

    @classmethod
    def get_test_schema_name(cls):
        return "e2e_write"

    def setUp(self):
        """先灌 RBAC 目录，否则自定义角色按 code 取不到 `order.list`。

    `_seed_tenant_fixtures` 会建自定义角色，并按 code 取 `PermissionNode`。
    生产路径由 `provision_tenant_db → seed_tenant → seed_rbac` 保证；
    `TenantTestCase` 只建 schema、不灌种子，所以这里显式补。这不是放宽判据。
"""
        super().setUp()
        seed_rbac()

    def _seed_config_and_accounts(self):
        """先摆 `seed_e2e` 的那套配置 / 账号（本命令只补订单，⛔ 不建它们）。"""
        _seed_tenant_fixtures(self.tenant)

    def _run(self):
        with override_settings(DEBUG=True):
            Command().handle(schema="e2e_write")

    def test_creates_the_deterministic_order_fixtures(self):
        """正常路径：4 笔夹具订单的 单号 / 状态 / 接手人 与契约**逐条**相等。"""
        self._seed_config_and_accounts()
        self._run()

        actual = []
        for spec in self.EXPECTED:
            order_no = spec[0]
            order = Order.objects.filter(order_no=order_no).first()
            self.assertIsNotNone(order, f"{order_no} 没被造出来")
            main = Performance.objects.filter(
                order=order, emp_type=EmpType.MAIN
            ).first()
            self.assertIsNotNone(main, f"{order_no} 没有 MAIN 绩效行（行内控件点不开）")
            actual.append(
                (
                    order.order_no,
                    str(order.status),
                    main.user.username if main.user_id is not None else None,
                )
            )
        self.assertEqual(actual, list(self.EXPECTED))

    def test_covering_states_and_assignment_branches(self):
        """覆盖口径：**至少两态**，且「未指派」与「已指派给 taker001」**两支同时在**。

    ⚠️ 只断「订单存在」不够：把「已指派」那一笔的 `set_taker` 删掉时订单还在、
    状态也还在，但订单页行内少一整支控件 ⇒ 本用例必须红。
"""
        self._seed_config_and_accounts()
        self._run()

        statuses = set(Order.objects.values_list("status", flat=True))
        self.assertGreaterEqual(len(statuses), 2, f"只造出 {sorted(statuses)} 一种状态")
        assigned = Performance.objects.filter(
            emp_type=EmpType.MAIN, order__status=OrderStatus.IN_PROGRESS
        )
        self.assertEqual([p.user.username for p in assigned], ["taker001"])
        pool = Order.objects.filter(status=OrderStatus.UNASSIGNED)
        self.assertTrue(pool.exists())
        for order in pool:
            main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
            self.assertIsNone(
                main.user_id, "池内状态却挂着接单人（D-0032：池内 ⇒ 无接单人）"
            )

    def test_is_idempotent_on_rerun(self):
        """幂等：连跑两次后订单**不翻倍**，且读数与第一次相同。

    ⚠️ 这条正是「⛔ 不用 `generate_order_no()`」的判据：换成随机 / 时间戳单号时，
    第二次会多出 4 笔新单 ⇒ 本用例红。
"""
        self._seed_config_and_accounts()
        self._run()
        first = set(Order.objects.values_list("order_no", flat=True))
        self._run()
        second = set(Order.objects.values_list("order_no", flat=True))
        self.assertEqual(first, second)
        self.assertEqual(len(second), len(self.EXPECTED))

    def test_refuses_without_seed_e2e_config(self):
        """缺 `seed_e2e` 的配置 ⇒ **响亮**拒绝，⛔ 不静默造一笔挂空配置的订单。"""
        with override_settings(DEBUG=True):
            with self.assertRaisesMessage(CommandError, "先跑 `seed_e2e"):
                Command().handle(schema="e2e_write")

    def test_gate_is_wired_into_handle(self):
        """闸门二真的接在 `handle()` 上：只读租户 schema ⇒ 在**闸门**就被拒。

    文案锚点是闸门那一句 ⇒ 若闸门被删掉，本用例会拿到"schema 'e2e' 不存在"而红。
"""
        with override_settings(DEBUG=True):
            with self.assertRaisesMessage(CommandError, GATE_MESSAGE):
                Command().handle(schema="e2e")


class ResetE2EWriteOrdersWiringTests(SimpleTestCase):
    """`reset_e2e_write` 真的**在 `seed_e2e` 之后**调了订单夹具命令（T2b 的接线判据）。

    ⛔ 不连库、不起进程：顶掉四个真实出口（DROP 原语 / 供给子命令 / 两条灌数据命令），
    并用 `os.stat` 的桩过掉凭据文件 0600 的收尾复核。
"""

    def test_seed_write_orders_runs_after_seed_e2e(self):
        calls = []

        def _record(name):
            return lambda self: calls.append(name)

        with mock.patch.object(reset_e2e_write, "drop_tenant_database"), mock.patch.object(
            reset_e2e_write, "call_command"
        ), mock.patch.object(
            ResetCommand, "_run_seed_e2e", _record("seed_e2e")
        ), mock.patch.object(
            ResetCommand, "_run_seed_write_orders", _record("seed_e2e_write_orders")
        ), mock.patch.object(
            reset_e2e_write.os, "stat"
        ) as stat:
            # `st_mode` 取**目录 + 0600**（`0o040600`）：`handle()` 只读 `& 0o777`，
            # 而 `Path.mkdir(exist_ok=True)` 会拿同一个 `os.stat` 判 `is_dir()`
            # ⇒ 只给 `0o100600` 会让它把 `.tools/e2e` 当成"不是目录"、进而抛 FileExistsError。
            stat.return_value.st_mode = 0o040600
            with override_settings(DEBUG=True):
                ResetCommand().handle()

        self.assertEqual(calls, ["seed_e2e", "seed_e2e_write_orders"])


class SeedE2EWriteOrdersSharesTheTenantRunnerTests(SimpleTestCase):
    """两条灌数据命令**共用同一个子进程运行器**（⛔ 不各写一份 argv / 错误翻译）。

    判据打在**行为**上：`_run_tenant_command` 的 argv 里命令名必须是参数给出的那个、
    且**一定带 `--schema e2e_write`**；两条命令各产出一条 argv。
"""

    def test_both_commands_go_through_one_runner(self):
        seen = []

        def _fake_run(argv, **kwargs):
            seen.append((argv, kwargs["env"].get("POSTGRES_DB")))
            result = mock.Mock()
            result.stdout = ""
            return result

        credentials = {
            "POSTGRES_DB": "tt_e2e_write",
            "POSTGRES_USER": "tt_e2e_write",
            "POSTGRES_PASSWORD": "x",
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": "5432",
        }
        with mock.patch.object(
            reset_e2e_write, "_read_credentials", return_value=credentials
        ), mock.patch.object(reset_e2e_write.subprocess, "run", _fake_run):
            command = ResetCommand()
            command._run_seed_e2e()
            command._run_seed_write_orders()

        commands = [argv[2] for argv, _ in seen]
        self.assertEqual(commands, ["seed_e2e", "seed_e2e_write_orders"])
        for argv, database in seen:
            with self.subTest(command=argv[2]):
                self.assertEqual(argv[-2:], ["--schema", "e2e_write"])
                self.assertEqual(database, "tt_e2e_write")

    def test_seed_write_orders_uses_the_shared_runner(self):
        """结构性判据：`_run_seed_write_orders` 必须**委托**给共用运行器，不自起子进程。

    做法：把共用运行器换成记录器 ⇒ 它被调用即证明委托；⛔ 不看源码文本。
"""
        calls = []
        with mock.patch.object(
            ResetCommand,
            "_run_tenant_command",
            lambda self, name: calls.append(name),
        ):
            ResetCommand()._run_seed_write_orders()
        self.assertEqual(calls, ["seed_e2e_write_orders"])


class SeedE2EWriteOrdersGuardWiringTests(TenantTestCase):
    """`_assert_fixture_orders_are_usable` **承重**的那一条：把「已指派」那一笔改坏 ⇒ 命令必须拒绝。

    构造方式：让 `set_taker` 变成 no-op（`apps/business/performance.py` 里**被本命令查找**
    的那一处，`E-0029`）⇒ 库里那两笔「已指派」的 `user` 为空，正是「漏掉指派」在库里的后果。
    ⛔ 断言的是**效果**（响亮拒绝 + 事务回滚），不是"守卫被调用过"。
    删掉 `_upsert_fixture_orders` 末尾那行守卫调用 ⇒ 本用例必红。
"""

    @classmethod
    def get_test_schema_name(cls):
        return "e2e_write"

    def setUp(self):
        """同 `SeedE2EWriteOrdersOnRealTenantTests.setUp`：`_seed_tenant_fixtures` 需要 RBAC 目录。"""
        super().setUp()
        seed_rbac()

    def test_refuses_when_assignment_silently_missing(self):
        _seed_tenant_fixtures(self.tenant)
        with mock.patch.object(
            seed_e2e_write_orders, "set_taker", lambda performance, user: performance
        ):
            with override_settings(DEBUG=True):
                with self.assertRaisesMessage(CommandError, "接手人是 None"):
                    Command().handle(schema="e2e_write")

        # 拒绝时整段写入必须回滚（`transaction.atomic()`）：一笔夹具订单都不留。
        self.assertFalse(
            Order.objects.filter(order_no__startswith="7777").exists(),
            "守卫拒绝时应当整体回滚，不留半套订单",
        )
