"""上家鸡蛋铺线的**调度接线**（计划 Task 13；spec §10 / §9.4 / R22）。

`jidanpu_sync` 从「每租户：租户级那条线 + 逐个代练员个人绑定」再扩一层：
**第三层 = 本线（上家鸡蛋铺）逐个启用绑定**，每个绑定的一轮顺序固定
`run_outbox` → `sync_hall` → `sync_my_orders`（后两段按**该绑定自己的**两个间隔判到点；
outbox **不设间隔闸**——已经排出去的动作要尽快变成真实动作，照反方向线的同一形状）。

这个文件只守**接线契约**本身（顺序 / 闸门 / 异常边界 / 幂等 / 退避 / 租约 / 汇总文案），
⛔ 不重复测三段各自的业务行为（那些在 `test_jidanpu_upstream_pull.py` /
`test_jidanpu_upstream_record.py` / `test_jidanpu_upstream_consume.py` 里）。

被钉住的契约：

1. **三层同进程共存、互不饿死**：一轮里租户级 → 代练员个人绑定 → 本线绑定**顺序固定**；
   每条线按**自己的闸门**判到点，一条线没到点 / 一条绑定坏了都**不阻塞**别的线、别的绑定。
2. **本层幂等**：同一张上家单重复轮**不重建订单 / 不重建链路**；周期轮**不排任何新 out 事件**
   （`rearm` 只属于**显式动作**那两个入口）。
3. **停用即停**（spec §8.4）：`enabled=False` 的绑定 ⇒ **一次上游都不打**、连建连都不建。
4. **失败退避且能到 `abandoned`**：退避窗内**一次都不打**；烧完额度的行**周期轮永远不 rearm**
   ——而**显式动作**（`queue_complete`）会给它重新发额度（F3 口径，反方向线同源）。
5. **中断不留永久租约**（spec §10 / 既有 `execution_lease` 先例）：本层被 `Ctrl-C`
   （`KeyboardInterrupt`）打断 ⇒ 进程**响亮退出**（⛔ 不被吞），事件上的执行租约**立刻还回去**。
6. ⭐ **R22**：间隔列存 `0`（配置写坏）⇒ 按**本线模型这一列自己的默认值**兜底
   （大厅 **30**、我的订单 **60**），⛔ 不是 10、⛔ 不抛 `FieldDoesNotExist`
   ——本线**独立实现**那个吃「模型 + 列名」的 helper，⛔ 不照抄 `jidanpu_hall._poll_seconds`。
7. ⭐ **健康列（Task 13 收口裁定 1 / 2 / 3）**：一轮里**至少一条读路到点、三段都没未预期异常、
   到点的大厅拉全、outbox 也没记下失败** ⇒ 在**整轮末尾**盖 `last_ok_at` 并清掉
   `last_error` / `last_error_at`（`jidanpu_upstream._stamp_binding_ok`，与 `run_outbox` 成功时
   的 `_stamp_health` **同一口径**）——**只跑读取、没有待发事件的绑定不许永远显示「从未成功」**；
   ⛔ **部分成功一律不盖**：拉不全 / 某一段抛异常 / 两条读路都未到点（什么都没做）/
   非 owner 的大厅（一次上游都没打）/ outbox 记了 `failed` · `abandoned` · `paused`；
   且 `last_error` 的写入**不许是空串**（空 `reason` ⇒ 写固定人话，裁定 3）。
   ⚠️ **判据打数据库行**（`AGENTS.md` §5-G）：本档全部断言 `JidanpuUpstreamBinding` 上的列值，
   ⛔ 不打 stdout 的人数（除了空 `reason` 那一档顺带钉一句兜底文案）。

⚠️ 判据打**数据库行**与**桩的调用集合**（`AGENTS.md` §5-G），⛔ 不打 logger。
⚠️ 桩打在**被查找的那个模块**上（E-0029）：三段都在 `apps.business.jidanpu_upstream` 里查，
   建连在 `apps.business.jidanpu` 里查，`tenant_context` / `Tenant.objects` 在命令模块里查。
⚠️ **零真实网络**：三段与建连全部打桩；两条「真跑」的用例也把 SDK 客户端换成替身。
⚠️ 夹具一律**合成值**（E-0077）：域名 `…test.invalid`、账号 `kefu-up-sched-*`、密码 canary。
"""
from __future__ import annotations

import io
from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu, jidanpu_member, jidanpu_upstream
from apps.business.jidanpu_upstream import (
    HallOutcome,
    MyOrdersOutcome,
    OutboxStats,
)
from apps.business.models import (
    Game,
    JidanpuMemberBinding,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
)

# ── 合成夹具值（E-0077）─────────────────────────────────────────────────────────

TENANT_SCHEMA = "tenant-up-sched"
UPSTREAM_BASE_URL = "https://jidanpu-upstream-sched.test.invalid"
UPSTREAM_PASSWORD = "pw-canary-sched"
MEMBER_BASE_URL = "https://jidanpu-member-sched.test.invalid"

#: 上家「我的订单」端点路径（**逐字**抄 spec §2.1；⛔ 不从模块常量派生）。
MY_ORDERS_PATH = "/OrderAjax/GetMyOrderList"

#: 本线两个间隔的**契约字面值**（spec §10 / T10 已定；⭐ D-0363 业务方把大厅
#: 这一档改定 **30**）；⛔ 不从模型常量派生，
#: 这样「有人把模型默认值改坏」时下面的 R22 用例**会红**（E-0024 / E-0065）。
HALL_DEFAULT_SECONDS = 30
MY_ORDERS_DEFAULT_SECONDS = 60

#: 固定的「现在」：闸门判据与注入的时钟都用它（⛔ 不用 `timezone.now()` 让断言随运行时刻漂）。
NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=dt_timezone.utc)


@contextmanager
def _noop_context(tenant):
    """把 `tenant_context` 换成「什么都不做」（只测接线逻辑，不切库）。"""
    yield


def _patch_tenants(module, tenants):
    """把 `Tenant.objects` 换成回给定租户的替身（照 `test_jidanpu_member_schedule` 的形状）。"""
    patcher = mock.patch.object(module.Tenant, "objects")
    objects = patcher.start()
    objects.exclude.return_value.order_by.return_value = list(tenants)
    return patcher, objects


def _open_pair(client):
    """`jidanpu._open_client_pair` 的替身：产出 `(client, http)`，⛔ 一次真实网络都不建。

    ⚠️ **2026-10-20 改准**（`docs/plans/2026-10-20-upstream-waf-risk-reduction.md` §2.1）：
    上家那条线的建连点从 `open_client_with_http`（**上下文管理器**，一轮一建一关）换成了
    `_open_client_pair`（**长期持有的一对**，由 `jidanpu.UpstreamClientPool` 留住跨轮复用）
    ⇒ 桩要打在**被查找的那个新名字**上（E-0029）。判据强度一字未降：桩没接上时下面那些
    用例会因为「读到的是真客户端」当场红。
    ⚠️ 绑定参数被有意忽略（连接由夹具给）。
    """
    return lambda binding: (client, mock.Mock())


class _StubClient:
    """`JidanpuClient` 的替身：按 `page` 从一份行里切页（照 `test_jidanpu_upstream_record`）。

    ⚠️ 必须把 `last_envelope` 写成 `code == 0`：被测代码的读端点成功码判据
    （`_require_read_success`）打的就是它（⛔ 不靠 SDK 宽松解包）。
    """

    def __init__(self, rows=None, *, per_page=20):
        self.rows = [dict(row) for row in (rows or [])]
        self.per_page = per_page
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}
        self.logins: list[str] = []
        #: ⭐ 池收尾（`UpstreamClientPool.release_all`）会关它 ⇒ 替身也得有 `close()`。
        self.closed = False

    def close(self):
        self.closed = True

    def ensure_login(self):
        self.logins.append(MY_ORDERS_PATH)

    def call(self, method, path, **kwargs):
        if not self.logins:
            self.ensure_login()
        self.calls.append((method, path, dict(kwargs.get("query") or {})))
        assert path == MY_ORDERS_PATH, f"未预期的端点：{method} {path}"
        page = int(kwargs["query"]["page"])
        start = (page - 1) * self.per_page
        page_rows = self.rows[start:start + self.per_page]
        self.last_envelope = {
            "code": 0, "msg": "success", "data": page_rows, "count": len(self.rows),
        }
        return [dict(row) for row in page_rows]


class _HandlerRecorder:
    """`_HANDLERS` 的替身：记下每一次消费，返回一个固定的结论（或抛给定异常）。"""

    def __init__(self, *, outcome="skipped", exc=None):
        self.outcome = outcome
        self.exc = exc
        self.calls: list[int] = []

    def __call__(self, event, **kwargs):
        self.calls.append(event.pk)
        if self.exc is not None:
            raise self.exc
        return self.outcome


class _ScheduleTestCase(TenantTestCase):
    """一租户一库的真测试库 + 一个假租户对象（`tenant_context` 被换掉）。"""

    def setUp(self):
        from apps.business.management.commands import jidanpu_sync as module

        self.module = module
        # ⚠️ 各条路的间隔闸锚都是**进程内**状态 ⇒ 跨用例会串味 ⇒ 每条用例开头清空。
        # ⚠️ `getattr` 的兜底只为让**实现之前**的红是**行为红**（第三层没接上），
        #    ⛔ 不是「锚可以不存在」——下面的用例会把它们当真用。
        for name in (
            "_UPSTREAM_HALL_AT", "_UPSTREAM_MY_ORDERS_AT",
            #: ⭐ T8 加的**兜底对账**锚（与上面两个同族：进程内、跨用例会串味）。
            "_UPSTREAM_OPENAPI_RECONCILE_AT",
            #: ⭐ 加的**连续失败退避**两个字典（同族：进程内、跨用例会串味）——
            #: 上一个用例故意制造过读路失败，留下的窗口会让本用例「整轮跳过」而不自知。
            "_UPSTREAM_FAILURE_COUNT", "_UPSTREAM_PENALTY_UNTIL",
            "_MEMBER_FAST_AT", "_MEMBER_STATUS_PULL_AT", "_MEMBER_RECONCILE_AT",
        ):
            getattr(module, name, {}).clear()
        self.tenant = mock.Mock(schema_name=TENANT_SCHEMA)

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def _user(self, name):
        return User.objects.create_user(username=name, password="pw-canary-x")

    def _upstream(self, name, *, enabled=True, password=UPSTREAM_PASSWORD, **kwargs):
        # ⚠️ 账号按用例内的名字派生：`(jidanpu_base_url, account)` 上有唯一约束。
        return JidanpuUpstreamBinding.objects.create(
            user=self._user(f"kefu-up-sched-{name}"),
            jidanpu_base_url=UPSTREAM_BASE_URL, account=f"acc-up-sched-{name}",
            password=password, enabled=enabled, **kwargs,
        )

    def _member(self, name, *, enabled=True, password="pw-member-canary", **kwargs):
        return JidanpuMemberBinding.objects.create(
            user=self._user(f"kefu-up-mem-{name}"),
            jidanpu_base_url=MEMBER_BASE_URL, account=f"acc-up-mem-{name}",
            password=password, enabled=enabled, **kwargs,
        )

    def _key(self, binding):
        return (TENANT_SCHEMA, binding.pk)

    # ── 桩 ──────────────────────────────────────────────────────────────────

    @contextmanager
    def _patched_upstream(self, *, outbox=None, hall=None, orders=None):
        """三段各自的桩（每次调用都造**新** patcher，⛔ 不跨 `with` 复用）。"""
        with mock.patch.object(
            jidanpu_upstream, "run_outbox",
            return_value=OutboxStats() if outbox is None else outbox,
        ) as box, mock.patch.object(
            jidanpu_upstream, "sync_hall",
            return_value=HallOutcome() if hall is None else hall,
        ) as hall_stub, mock.patch.object(
            jidanpu_upstream, "sync_my_orders",
            return_value=MyOrdersOutcome() if orders is None else orders,
        ) as orders_stub:
            yield box, hall_stub, orders_stub

    # ── 跑一趟 ──────────────────────────────────────────────────────────────

    def _run(self, *, now=NOW, expect_error=False, stdout=None, stderr=None):
        """跑一趟 `--once`（租户遍历被替身接管），回 `(CommandError 或 None, out, err)`。

        ⚠️ `now` 是**注入的墙钟**：跨趟的判据（未到点 / 退避）靠把它往后推来验。
        """
        out = stdout if stdout is not None else io.StringIO()
        err = stderr if stderr is not None else io.StringIO()
        caught = None
        with mock.patch.object(self.module, "tenant_context", _noop_context), \
             mock.patch.object(self.module.timezone, "now", return_value=now):
            patcher, _ = _patch_tenants(self.module, [self.tenant])
            try:
                try:
                    call_command("jidanpu_sync", "--once", stdout=out, stderr=err)
                except CommandError as exc:
                    caught = exc
            finally:
                patcher.stop()
        if expect_error:
            self.assertIsNotNone(caught, "本趟应当以非零退出码收尾，却没有抛 CommandError")
        else:
            self.assertIsNone(caught, f"本趟不该非零退出：{caught}")
        return caught, out.getvalue(), err.getvalue()


# ── ⓪ 上家线**没有**「操作日志快路」这一档（业务方）─────────────────
#
# 业务方核定：上家侧那个鸡蛋铺账号是**打手权限**，**读不到对方的系统操作日志**
# （spec `docs/specs/2026-09-28-upstream-jidanpu-sync-design.md` §2.5 **实测**：
# `GET /SystemAjax/GetSysmteLogList` 对打手账号回**空列表 + code=0 的静默空**）
# ⇒ 下家线那条 `poll_log` 日志快路在本线上**不成立**，⛔ 不许挪进来。
# ⭐ 旧「大厅尾部快路」（`HALL_TAIL_SECONDS`，读抢单大厅列表）已随 D-0366 **整体退役**
# ——本线今天**没有任何快路**；判据两条不变。
#
# 判据两条：① 行为（一轮里那两个「读操作日志」的入口一次都不被调）；
# ② 结构（`_run_upstream_binding` 这一整行的源码里根本不许出现它们）。


class NoOperationLogFastPathTests(_ScheduleTestCase):
    def test_an_upstream_round_never_calls_the_operation_log_readers(self):
        """① 行为：一轮里 `jidanpu.fetch_system_logs` / `jidanpu_member.poll_log` 零调用。"""
        self._upstream("nolog", hall_poll_seconds=1, my_orders_poll_seconds=1)
        with self._patched_upstream(), \
             mock.patch.object(jidanpu, "fetch_system_logs") as system_logs, \
             mock.patch.object(jidanpu_member, "poll_log") as member_logs, \
             mock.patch.object(jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())):
            self._run()
        system_logs.assert_not_called()
        member_logs.assert_not_called()

    def test_the_upstream_row_source_contains_no_log_reader(self):
        """② 结构：`_run_upstream_binding` 与 `jidanpu_upstream` 的**代码**里不许出现日志读入口。

        ⚠️ 扫的是 **AST 去掉 docstring 之后的代码**（⛔ 不是文件文本）：注释 / 文档里
        解释「本行没有 `poll_log`」是**应该存在**的，那种自命中会把判据变成噪声
        （与 E-0011 的「搜主词」同族：要搜的是**被查找的名字**，不是它出现在哪句散文里）。
        判别力：谁把 `poll_log` / `fetch_system_logs` 接进这一行 ⇒ 本用例红
        （这条按业务方核定**永久**不成立，⛔ 不是「暂时没做」）。
        """
        import ast
        import inspect
        import textwrap

        def _strip_docstrings(node):
            for sub in ast.walk(node):
                if not isinstance(
                    sub, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                body = getattr(sub, "body", None)
                if not body:
                    continue
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                ):
                    sub.body = body[1:] or [ast.Pass()]

        def _code_without_docstrings(source):
            tree = ast.parse(textwrap.dedent(source))
            _strip_docstrings(tree)
            return ast.unparse(tree)

        # ⚠️ **2026-11-03 提取方式改纯 AST（合并树全量门禁实测，main 上即红）**：方法体内有
        #    顶格的多行字符串收尾（`"""` 在第 0 列）——任何「getsource/切片 + dedent + parse」
        #    的提取都会因公共缩进判不出来而 IndentationError。改在**语法树**上找节点、
        #    摘 docstring、直接 `ast.unparse`：完全不经源码文本的缩进形状；
        #    判据本体（去 docstring 后不许出现日志读入口）一字不动，判别力不变。
        fn = next(
            node
            for node in ast.walk(ast.parse(inspect.getsource(self.module)))
            if isinstance(node, ast.FunctionDef)
            and node.name == "_run_upstream_binding"
        )
        _strip_docstrings(fn)
        row_code = ast.unparse(fn)
        for banned in ("poll_log", "fetch_system_logs", "SYSTEM_LOG_PATH", "operation_log"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, row_code)
        module_code = _code_without_docstrings(inspect.getsource(jidanpu_upstream))
        for banned in ("fetch_system_logs", "SYSTEM_LOG_PATH"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, module_code)


# ── ① 三层共存、顺序固定、互不饿死 ────────────────────────────────────────────


class UpstreamLineWiringTests(_ScheduleTestCase):
    """第三层真的接上了，且接在**既有两条线之后**（顺序固定）。"""

    def test_the_third_layer_runs_after_the_existing_line(self):
        """一轮里的顺序 = 代练员个人绑定 → 本线绑定（spec §10 的入口表）。

        失败方向：把第三层插到个人绑定之前 / 整层忘了接 ⇒ 顺序断言或「本线 outbox 从未被调」
        ⇒ 本用例红。

        ⛔ **2026-10-08 改准（原名 `…_after_the_two_existing_lines`）**：原顺序三段 =
        租户级 → 代练员 → 本线；租户级那一段随租户级鸡蛋铺线整条拆除 ⇒ 只剩两段。
        ⚠️ **判据强度没降**：仍是**恰好等于**那个两元序列（`assertEqual`，⛔ 不是「包含」）。
        """
        self._member("order")
        self._upstream("order")
        trace: list[str] = []

        def _member_outbox(binding, **kwargs):
            trace.append("member")
            return jidanpu_member.OutboxStats()

        def _upstream_outbox(binding, **kwargs):
            trace.append("upstream")
            return OutboxStats()

        with mock.patch.object(jidanpu_member, "run_outbox", side_effect=_member_outbox), \
             mock.patch.object(jidanpu_member, "poll_log", return_value=0), \
             mock.patch.object(jidanpu_member, "pull_statuses", return_value=0), \
             mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ), \
             self._patched_upstream():
            # ⚠️ 本线 outbox 的顺序要点必须由 side_effect 记（不能用 return_value 的桩）
            with mock.patch.object(
                jidanpu_upstream, "run_outbox", side_effect=_upstream_outbox
            ):
                self._run()
        self.assertEqual(trace, ["member", "upstream"])

    def test_a_layer_that_is_not_due_does_not_block_the_other_lines(self):
        """⭐ 各线**各有各的闸门**：本线大厅未到点，另一条线与本线「我的订单」照跑。

        失败方向：把各线串成一个共用闸门（一处未到点就整轮 `return`）⇒
        代练员 outbox / 本线「我的订单」都不会被调 ⇒ 本用例红。

        ⛔ **2026-10-08 改准**：原判据含「租户级 `run_round` 也照跑」那一半，租户级那一段
        随租户级鸡蛋铺线整条拆除 ⇒ 那一半退场。⚠️ 留下的判据强度没降
        （`member_outbox.assert_called_once()` / `my_orders.assert_called_once()` /
        `hall_stub.assert_not_called()` 一个字没改）。
        """
        self._member("gate")
        binding = self._upstream(
            "gate", hall_poll_seconds=HALL_DEFAULT_SECONDS,
            my_orders_poll_seconds=MY_ORDERS_DEFAULT_SECONDS,
        )
        # 本线大厅：刚跑过（未到点）；「我的订单」：早就该跑（到点）。
        self.module._UPSTREAM_HALL_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        self.module._UPSTREAM_MY_ORDERS_AT[self._key(binding)] = NOW - timedelta(
            seconds=MY_ORDERS_DEFAULT_SECONDS + 1
        )
        with mock.patch.object(
                 jidanpu_member, "run_outbox", return_value=jidanpu_member.OutboxStats(),
             ) as member_outbox, \
             mock.patch.object(jidanpu_member, "poll_log", return_value=0), \
             mock.patch.object(jidanpu_member, "pull_statuses", return_value=0), \
             mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ), \
             self._patched_upstream() as (_box, hall_stub, my_orders):
            self._run()
        member_outbox.assert_called_once()
        my_orders.assert_called_once()
        hall_stub.assert_not_called()

    def test_each_binding_has_its_own_anchors(self):
        """⭐ 两个本线绑定：一个到点、一个刚跑过 ⇒ 只有到点的那个被打上游。

        失败方向：拿一个模块级布尔 / 单例锚判到点（第二个绑定被第一个的锚挡住）⇒ 本用例红。
        """
        due = self._upstream("anchor-due", hall_poll_seconds=300, my_orders_poll_seconds=60)
        fresh = self._upstream("anchor-fresh", hall_poll_seconds=300, my_orders_poll_seconds=60)
        self.module._UPSTREAM_MY_ORDERS_AT[self._key(fresh)] = NOW - timedelta(seconds=1)
        self.module._UPSTREAM_HALL_AT[self._key(fresh)] = NOW - timedelta(seconds=1)
        seen: list[int] = []

        def _orders(binding, **kwargs):
            seen.append(binding.pk)
            return MyOrdersOutcome()

        with mock.patch.object(jidanpu_upstream, "run_outbox", return_value=OutboxStats()), \
             mock.patch.object(jidanpu_upstream, "sync_hall", return_value=HallOutcome()), \
             mock.patch.object(jidanpu_upstream, "sync_my_orders", side_effect=_orders), \
             mock.patch.object(jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())):
            self._run()
        self.assertEqual(seen, [due.pk])

    def test_a_disabled_upstream_binding_never_touches_the_upstream(self):
        """⭐ spec §8.4：`enabled=False` ⇒ 三段一条都不调、⛔ 连建连都不建。

        失败方向：只按「绑定存在」遍历（不筛 `enabled`）⇒ `run_outbox` 被调 ⇒ 本用例红。
        """
        self._upstream("disabled", enabled=False)
        with self._patched_upstream() as (box, hall_stub, my_orders), \
             mock.patch.object(jidanpu, "_open_client_pair") as connect:
            self._run()
        box.assert_not_called()
        hall_stub.assert_not_called()
        my_orders.assert_not_called()
        connect.assert_not_called()

    def test_an_upstream_binding_with_incomplete_credentials_is_skipped(self):
        """凭据不全（密码为空）⇒ 整行跳过（照租户级那一段的凭据闸门）。

        失败方向：删掉那道闸 ⇒ 每轮都在建连处抛 `ConfigError`、被记成「同步坏了」⇒ 本用例红。
        """
        self._upstream("no-password", password="")
        with self._patched_upstream() as (box, hall_stub, my_orders):
            self._run()
        box.assert_not_called()
        hall_stub.assert_not_called()
        my_orders.assert_not_called()


# ── ② 间隔闸门（各有各的锚 / 各有各的间隔）＋ R22 兜底 ─────────────────────────


class UpstreamIntervalGateTests(_ScheduleTestCase):
    """大厅与「我的订单」各自按绑定自己的间隔判到点；存 `0` 按本列默认值兜底。"""

    def test_the_hall_and_my_orders_paths_wait_for_their_own_intervals(self):
        """`hall=300` / `my_orders=60`：两个闸门**互不代判**。

        第一趟：大厅锚只过去 1 秒（未到点）、「我的订单」锚过去 61 秒（到点）
        ⇒ 只打「我的订单」；第二趟反过来 ⇒ 只打大厅。
        失败方向：两个闸门共用一个锚 / 一个间隔 ⇒ 任何一趟的调用集合都会多出一个 ⇒ 本用例红。
        """
        binding = self._upstream(
            "gate", hall_poll_seconds=HALL_DEFAULT_SECONDS,
            my_orders_poll_seconds=MY_ORDERS_DEFAULT_SECONDS,
        )
        self.module._UPSTREAM_HALL_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        self.module._UPSTREAM_MY_ORDERS_AT[self._key(binding)] = NOW - timedelta(
            seconds=MY_ORDERS_DEFAULT_SECONDS + 1
        )
        with mock.patch.object(jidanpu_upstream, "run_outbox"), \
             mock.patch.object(jidanpu_upstream, "sync_hall") as hall_stub, \
             mock.patch.object(jidanpu_upstream, "sync_my_orders") as my_orders, \
             mock.patch.object(jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())):
            self._run()
        hall_stub.assert_not_called()
        my_orders.assert_called_once()
        # 第二趟：把两个锚对调（大厅该跑、「我的订单」刚跑过）
        self.module._UPSTREAM_HALL_AT[self._key(binding)] = NOW - timedelta(
            seconds=HALL_DEFAULT_SECONDS + 1
        )
        self.module._UPSTREAM_MY_ORDERS_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        with mock.patch.object(jidanpu_upstream, "run_outbox"), \
             mock.patch.object(jidanpu_upstream, "sync_hall") as hall_stub, \
             mock.patch.object(jidanpu_upstream, "sync_my_orders") as my_orders, \
             mock.patch.object(jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())):
            self._run()
        hall_stub.assert_called_once()
        my_orders.assert_not_called()

    def test_a_repeat_round_inside_the_interval_does_not_call_the_upstream_again(self):
        """同一间隔内再跑一趟 ⇒ **两条读路**一条都不再打；⚠️ outbox 每轮都跑（它不设闸）。

        失败方向：删掉两个读路闸门（每轮都跑）⇒ 第二趟两条读路的调用数变 2 ⇒ 本用例红。
        """
        self._upstream(
            "repeat", hall_poll_seconds=HALL_DEFAULT_SECONDS,
            my_orders_poll_seconds=MY_ORDERS_DEFAULT_SECONDS,
        )
        with self._patched_upstream() as (box, hall_stub, my_orders):
            self._run()
            self._run(now=NOW + timedelta(seconds=1))
        self.assertEqual(box.call_count, 2, "outbox 不设间隔闸 ⇒ 每轮都要跑")
        hall_stub.assert_called_once()
        my_orders.assert_called_once()

    def test_a_stored_zero_falls_back_to_this_lines_own_model_defaults(self):
        """⭐ R22：两个间隔列存 `0` ⇒ 按**本线模型列自己的默认值**兜底（30 / 60）。

        ⚠️ 这条是**实测逼出来的**：⛔ 照抄 `jidanpu_hall._poll_seconds` 会得到
        `hall=10`（大厅频率翻 3 倍）与 `my_orders` 直接抛 `FieldDoesNotExist`。
        失败方向：删掉 `or` 兜底 ⇒ 0 秒间隔 ⇒ 锚过去 1 秒也算到点 ⇒ 三段全被调 ⇒ 本用例红。
        ⚠️ 期望里的 **30 / 60 是字面值**（独立契约值，E-0024），⛔ 不从模型常量派生。
        ⭐ 大厅默认值从 300 缩到 **120**（业务方「大厅拉取频率提高」那一条；
        上家线**没有**操作日志快路 ⇒ 新单发现全靠拉取，见 `models.UPSTREAM_HALL_POLL_SECONDS`）。
        ⭐ D-0363）：再从 120 缩到 **30**（更快发现上家撤单、清僵尸；
        ×4 发数代价与 WAF 退路已登记，同见 `models.UPSTREAM_HALL_POLL_SECONDS`）。
        """
        self.assertEqual(
            self.module._poll_seconds(JidanpuUpstreamBinding, "hall_poll_seconds", 0),
            HALL_DEFAULT_SECONDS,
        )
        self.assertEqual(
            self.module._poll_seconds(JidanpuUpstreamBinding, "my_orders_poll_seconds", 0),
            MY_ORDERS_DEFAULT_SECONDS,
        )
        # 正常值照原样（⛔ 不是「一律回默认值」）
        self.assertEqual(
            self.module._poll_seconds(JidanpuUpstreamBinding, "hall_poll_seconds", 17), 17
        )
        binding = self._upstream("zero", hall_poll_seconds=0, my_orders_poll_seconds=0)
        self.module._UPSTREAM_HALL_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        self.module._UPSTREAM_MY_ORDERS_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        with self._patched_upstream() as (box, hall_stub, my_orders):
            self._run()
        box.assert_called_once()          # outbox 不设闸 ⇒ 每轮都要跑
        hall_stub.assert_not_called()
        my_orders.assert_not_called()


# ── ③ 异常边界（逐绑定、逐段）────────────────────────────────────────────────


class UpstreamExceptionBoundaryTests(_ScheduleTestCase):
    """一个绑定坏了不影响别的绑定与另外两条线；一段坏了不影响同一绑定的另一段。"""

    def test_one_broken_binding_does_not_stop_the_other_bindings_or_the_other_line(self):
        """⭐ 一个本线绑定炸了 ⇒ 另一个绑定与**另一条线**照跑，退出码非零。

        失败方向：把第三层包成一个大 `try`（一个绑定坏了整层退出）⇒ 第二个绑定的三段
        都不会被调 ⇒ 本用例红。

        ⛔ **2026-10-08 改准**：原判据里「**另外两条线**照跑」含租户级那一段，它随租户级
        鸡蛋铺线整条拆除 ⇒ 只剩「另一条线（代练员个人绑定）」那一半。⚠️ 强度没降：
        `member_outbox.assert_called_once()` 与两条读路的「好绑定在调用集合里」一个没改。
        """
        broken = self._upstream("broken")
        healthy = self._upstream("healthy")
        self._member("peer")
        seen: list[int] = []

        def _boom(binding, **kwargs):
            seen.append(binding.pk)
            if binding.pk == broken.pk:
                raise RuntimeError("boom")
            return OutboxStats()

        with mock.patch.object(
                 jidanpu_member, "run_outbox", return_value=jidanpu_member.OutboxStats(),
             ) as member_outbox, \
             mock.patch.object(jidanpu_member, "poll_log", return_value=0), \
             mock.patch.object(jidanpu_member, "pull_statuses", return_value=0), \
             mock.patch.object(
                 jidanpu_member, "reconcile_and_queue",
                 return_value=jidanpu_member.QueueStats(),
             ), \
             mock.patch.object(jidanpu_upstream, "run_outbox", side_effect=_boom), \
             mock.patch.object(jidanpu_upstream, "sync_hall", return_value=HallOutcome()) as hall2, \
             mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome()
             ) as orders2, \
             mock.patch.object(jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())):
            caught, out, err = self._run(expect_error=True)
        self.assertEqual(caught.returncode, 1)
        member_outbox.assert_called_once()
        # 好的那个绑定的三条段都照跑（⚠️ 坏的那个在 outbox 断了，但**逐段边界**让它的
        # 两条读路也照跑 ⇒ 两条读路各 2 次；判据钉「好的那个在调用集合里」）。
        self.assertIn(
            healthy.pk,
            {call.kwargs["binding"].pk for call in hall2.call_args_list},
        )
        self.assertIn(
            healthy.pk,
            {call.kwargs["binding"].pk for call in orders2.call_args_list},
        )
        # ⛔ 异常正文（可能带上游回包 / 凭据片段）一个字都不上行，只写异常类型名
        self.assertIn("RuntimeError", err)
        self.assertNotIn("boom", err)
        self.assertIn(f"#{broken.pk}", err)
        self.assertIn(f"上家绑定 #{healthy.pk}", out)
        # 坏的那个绑定**不再被同轮重试**（逐绑定边界 ⇒ 每轮一次）
        self.assertEqual(sorted(seen), sorted([broken.pk, healthy.pk]))

    def test_a_binding_that_blows_up_at_the_binding_level_does_not_stop_the_others(self):
        """⭐ 逐**绑定**边界本身可达：`_run_upstream_binding` 整行抛出 ⇒ 下一个绑定照跑。

        ⚠️ 为什么单独一条：三段各自的 `try` 会先接住**段内**异常 ⇒ 上面那条用例走不到
        逐绑定边界。这条把失败放在**绑定这一帧**（三段之外的代码：锚计算 / 人话汇总 …）
        ⇒ 逐绑定边界成为唯一接住它的地方。变异「`except Exception` → `except ValueError`」
        在这条用例上是 **KILLED**，在段内那条上是 SURVIVED（边界被段边界遮蔽）。
        失败方向：删掉逐绑定边界（或收窄它）⇒ 第一个绑定抛出后循环中断、第二个绑定不再被访问
        ⇒ `calls` 少一个 ⇒ 本用例红。
        """
        broken = self._upstream("binding-broken")
        healthy = self._upstream("binding-healthy")
        calls: list[int] = []

        def _run_binding(command, tenant, binding, *, now):
            calls.append(binding.pk)
            if binding.pk == broken.pk:
                raise RuntimeError("boom")
            return False

        with mock.patch.object(self.module.Command, "_run_upstream_binding", _run_binding):
            caught, _out, err = self._run(expect_error=True)
        self.assertEqual(caught.returncode, 1)
        self.assertEqual(sorted(calls), sorted([broken.pk, healthy.pk]))
        # ⛔ 异常正文（可能带上游回包 / 凭据片段）一个字都不上行，只写异常类型名
        self.assertIn("RuntimeError", err)
        self.assertNotIn("boom", err)

    def test_a_failing_hall_path_does_not_skip_the_my_orders_path(self):
        """⭐ 逐**段**边界：大厅抛未预期异常 ⇒ 同一绑定的「我的订单」照跑（反之亦然）。

        失败方向：把三段并成一个 `try`（大厅炸了跳过「我的订单」）⇒ `sync_my_orders`
        不被调 ⇒ 本用例红。
        """
        self._upstream("segment", hall_poll_seconds=300, my_orders_poll_seconds=60)
        with mock.patch.object(jidanpu_upstream, "run_outbox"), \
             mock.patch.object(
                 jidanpu_upstream, "sync_hall", side_effect=RuntimeError("boom")
             ), mock.patch.object(jidanpu_upstream, "sync_my_orders") as my_orders, \
             mock.patch.object(jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())):
            caught, _out, err = self._run(expect_error=True)
        self.assertEqual(caught.returncode, 1)
        my_orders.assert_called_once()
        self.assertIn("RuntimeError", err)

    def test_expected_outbox_failures_keep_the_exit_code_zero(self):
        """退出码契约的另一半：三段自己记账的**预期**失败 ⇒ 退出 0（那是每分钟都有的噪音）。

        失败方向：把「有失败计数」当未预期失败 ⇒ 本用例红。
        """
        self._upstream("expected")
        with self._patched_upstream(
            outbox=OutboxStats(failed=2, deferred=1),
            hall=HallOutcome(complete=False, reason="上家自报总数偏小，本轮未拉全"),
        ):
            _caught, out, _err = self._run()
        self.assertIn("上家绑定", out)


# ── ④ 汇总文案（大厅镜像 N / 记录 M / 跳过 K（人话）/ 跟退 J）─────────────────


class UpstreamRoundSummaryTests(_ScheduleTestCase):
    """每轮的人话汇总：四个数 + 拉不全的**人话原因**（⛔ 不许静默）。"""

    def test_the_round_summary_reports_the_four_numbers_and_the_hall_reason(self):
        """⭐ spec §10 / `HallOutcome` 的 docstring：四个数都要出现，且拉不全要说人话。

        失败方向：只报「大厅 N」不报拉不全的原因（静默）/ 漏掉跟退那一档 ⇒ 本用例红。
        """
        self._upstream("summary")
        reason = "上家自报总数偏小，本轮未拉全（探页仍有行），本轮不判消失"
        with self._patched_upstream(
            outbox=OutboxStats(done=1, deferred=2, skipped=3, abandoned=4),
            hall=HallOutcome(
                pulled=9, listed=5, skipped=2, gone=1, complete=False, reason=reason,
            ),
            orders=MyOrdersOutcome(pulled=6, recorded=3, skipped=2, followed=1),
        ):
            _caught, out, _err = self._run()
        self.assertIn("outbox 完成 1", out)
        self.assertIn("待重试 2", out)
        self.assertIn("跳过 3", out)
        self.assertIn("已放弃 4", out)
        self.assertIn("镜像 5", out)
        self.assertIn("记录 3", out)
        self.assertIn("跟退 1", out)
        self.assertIn(reason, out)

    def test_an_incomplete_hall_round_is_left_on_the_binding_as_last_error(self):
        """拉不全 ⇒ 除了 stdout，还要落到绑定 `last_error`（页面 / 告警看的是它）。

        失败方向：只 print 不落库 ⇒ 概览页与告警看不到「这轮没拉全」⇒ 本用例红。
        """
        binding = self._upstream("hall-reason")
        reason = "上家自报总数偏小，本轮未拉全（探页仍有行），本轮不判消失"
        with self._patched_upstream(hall=HallOutcome(complete=False, reason=reason)):
            self._run()
        self.assertEqual(
            JidanpuUpstreamBinding.objects.get(pk=binding.pk).last_error, reason
        )


# ── ⑤ 幂等（同一轮重复跑不重复建单 / 不重复推事件）────────────────────────────


class UpstreamIdempotencyTests(_ScheduleTestCase):
    """真跑 `sync_my_orders`（只把 SDK 客户端换成替身）：重复轮不产生第二张单。"""

    def _row(self, order_id="20260928000000000301"):
        return {
            "orderId": order_id,
            "orderIdView": f"VIEW-{order_id}",
            "orderSta": "待上号",
            "gameName": "测试游戏甲",
            "gameArea": "安卓",
            "gameServer": "微信区",
            "project": "合成项目",
            "orderContent": "合成需求",
            "orderRemark": "合成备注",
            "roleName": "合成角色",
            "gameAcc": "acc-synth",
            "zgameAcc": "sub-synth",
            "recFee": "88.00",
            "price": "120.00",
        }

    def test_two_due_rounds_create_exactly_one_order_and_one_link(self):
        """两次调度都真正读取了默认视图，且同一上家单只录入一次。"""
        Game.objects.create(name="测试游戏甲")
        binding = self._upstream("idem", hall_poll_seconds=300, my_orders_poll_seconds=60)
        client = _StubClient([self._row()])
        with mock.patch.object(jidanpu_upstream, "run_outbox"), \
             mock.patch.object(jidanpu_upstream, "sync_hall", return_value=HallOutcome()), \
             mock.patch.object(jidanpu, "_open_client_pair", new=_open_pair(client)):
            self._run()
            self._run(now=NOW + timedelta(seconds=MY_ORDERS_DEFAULT_SECONDS + 1))
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(JidanpuUpstreamOrderLink.objects.count(), 1)
        link = JidanpuUpstreamOrderLink.objects.get()
        self.assertEqual(link.upstream_order_id, "20260928000000000301")
        self.assertEqual(link.binding_id, binding.pk)
        windows = [
            query["etime"] for _, _, query in client.calls
            if query["page"] == 1 and "orderSta" not in query and "key1" not in query
        ]
        self.assertEqual(len(set(windows)), 2, "两轮应各读取一次默认视图")

    def test_periodic_rounds_never_queue_a_new_out_event(self):
        """⭐ 周期轮**自己不排事件**：两轮之后一条本线 out 事件都没有。

        `rearm` 只属于**显式动作**（`queue_hall_claim` / `queue_complete`，见 `_queue_out_event`）；
        周期轮若顺手重排，就会把「烧完额度的行」变成**无限重试**（spec §10 的失败退避口径）。
        失败方向：周期轮里加一句 `queue_*`（带不带 `rearm`）⇒ 本用例红。
        """
        Game.objects.create(name="测试游戏甲")
        self._upstream("noqueue", hall_poll_seconds=300, my_orders_poll_seconds=60)
        client = _StubClient([self._row(order_id="20260928000000000302")])
        with mock.patch.object(jidanpu_upstream, "run_outbox"), \
             mock.patch.object(jidanpu_upstream, "sync_hall", return_value=HallOutcome()), \
             mock.patch.object(jidanpu, "_open_client_pair", new=_open_pair(client)):
            self._run()
            self._run(now=NOW + timedelta(seconds=MY_ORDERS_DEFAULT_SECONDS + 1))
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                direction="out", kind__in=jidanpu_upstream.OUTBOX_KINDS,
            ).count(),
            0,
        )


# ── ⑥ 失败退避 + 到 abandoned + 周期轮不 rearm ────────────────────────────────


class UpstreamOutboxBackoffTests(_ScheduleTestCase):
    """退避窗 / `abandoned` / `rearm` 三条口径在**调度这一层**的读数。"""

    def _event(self, binding, *, attempts, processed_at, target_ref="sched|burned"):
        return JidanpuSyncEvent.objects.create(
            direction="out", kind=jidanpu_upstream.KIND_COMPLETE, status="failed",
            target_type="order", target_ref=target_ref, attempts=attempts,
            processed_at=processed_at, payload={"upstream_binding_id": binding.pk},
        )

    def test_a_failed_event_waits_out_its_backoff_window(self):
        """⭐ 退避窗内**一次都不打**；窗过了才真的消费（`2^attempts × 60` 秒）。

        失败方向：删掉 `_backoff_elapsed`（每轮都消费）⇒ 第一趟就会调处理器 ⇒ 本用例红。
        """
        binding = self._upstream("backoff")
        event = self._event(binding, attempts=1, processed_at=NOW - timedelta(seconds=1))
        recorder = _HandlerRecorder()
        with mock.patch.dict(jidanpu_upstream._HANDLERS, {
                 jidanpu_upstream.KIND_COMPLETE: recorder,
             }), mock.patch.object(
                 jidanpu_upstream, "sync_hall", return_value=HallOutcome()
             ), mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome()
             ), mock.patch.object(
                 jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())
             ):
            _caught, out, _err = self._run()
            self.assertEqual(recorder.calls, [])
            self.assertIn("待重试 1", out)
            event.refresh_from_db()
            self.assertEqual(event.attempts, 1)
            self.assertEqual(event.status, "failed")
            # 退避窗 = 2^1 × 60 = 120 秒 ⇒ 现在到点了
            self._run(now=NOW + timedelta(seconds=120))
        self.assertEqual(recorder.calls, [event.pk])

    def test_a_burned_event_stays_abandoned_across_periodic_rounds_but_an_explicit_action_rearms_it(self):
        """⭐⭐ spec §10 + F3（`D-0162` 第 ④ 条 / `E-0153`）：周期轮**永不 rearm**，
        只有**显式动作**（`queue_complete`）才给烧完额度的行重新发额度。

        失败方向：
        ① 周期轮里加一句带 `rearm=True` 的重排 ⇒ 第 ③ 步 `attempts` 被复位 ⇒ 本用例红；
        ② 把 `queue_complete` 的 `rearm=True` 改成默认值 ⇒ 第 ④ 步不复位 ⇒ 本用例红。
        """
        Game.objects.create(name="测试游戏甲")
        binding = self._upstream("burned", hall_poll_seconds=300, my_orders_poll_seconds=60)
        client = _StubClient([{
            "orderId": "20260928000000000303", "orderIdView": "VIEW-303",
            "orderSta": "待上号", "gameName": "测试游戏甲", "recFee": "88.00",
            "price": "120.00",
        }])
        # ① 先真的录一张单（拿到订单 + 链路），再让显式动作排一条事件。
        with mock.patch.object(jidanpu_upstream, "run_outbox", return_value=OutboxStats()), \
             mock.patch.object(jidanpu_upstream, "sync_hall", return_value=HallOutcome()), \
             mock.patch.object(jidanpu, "_open_client_pair", new=_open_pair(client)):
            self._run()
        order = Order.objects.get()
        self.assertEqual(jidanpu_upstream.queue_complete(order), 1)
        event = JidanpuSyncEvent.objects.get(
            direction="out", kind=jidanpu_upstream.KIND_COMPLETE
        )
        # ② 烧完额度（模拟反复失败到上限）
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            attempts=jidanpu_upstream.OUTBOX_MAX_ATTEMPTS, status="failed",
            reason="合成：已烧完额度", processed_at=NOW,
        )
        recorder = _HandlerRecorder()
        # ③ 周期轮连跑三趟（每趟把钟往后推过一切退避窗）⇒ 处理器一次都不许被调
        with mock.patch.dict(jidanpu_upstream._HANDLERS, {
                 jidanpu_upstream.KIND_COMPLETE: recorder,
             }), mock.patch.object(
                 jidanpu_upstream, "sync_hall", return_value=HallOutcome()
             ), mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome()
             ), mock.patch.object(
                 jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())
             ):
            for step in range(3):
                _caught, out, _err = self._run(
                    now=NOW + timedelta(seconds=3600 * (step + 1))
                )
            self.assertEqual(recorder.calls, [], "周期轮把烧完额度的事件又打了一次上游")
            self.assertIn("已放弃 1", out)
            event.refresh_from_db()
            self.assertEqual(event.attempts, jidanpu_upstream.OUTBOX_MAX_ATTEMPTS)
            self.assertEqual(event.status, "failed")
        # ④ 显式动作（我方订单进「已完成」那一次）⇒ 重新发额度
        self.assertEqual(jidanpu_upstream.queue_complete(order), 1)
        event.refresh_from_db()
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.status, "pending")
        self.assertIsNone(event.processed_at)
        # ⑤ 发完额度之后，周期轮才真的打上游
        with mock.patch.dict(jidanpu_upstream._HANDLERS, {
                 jidanpu_upstream.KIND_COMPLETE: recorder,
             }), mock.patch.object(
                 jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())
             ):
            self._run(now=NOW + timedelta(seconds=7200))
        self.assertEqual(recorder.calls, [event.pk])


# ── ⑦ 中断不留永久租约 ───────────────────────────────────────────────────────


class UpstreamLeaseTests(_ScheduleTestCase):
    """常驻进程被 `Ctrl-C` 打断 ⇒ 事件上的执行租约**立刻还回去**（⛔ 不空等一个租约时长）。"""

    def test_a_keyboard_interrupt_leaves_no_permanent_lease(self):
        """⭐ 中断**不被吞**（`except Exception` 不接 `BaseException`），且租约被 `finally` 还回。

        失败方向：
        ① 把逐段边界写成 `except BaseException`（吞掉 `Ctrl-C`）⇒ `assertRaises` 红；
        ② 把 `execution_lease` 的 `finally` 去掉 ⇒ `lease_owner` 非空 ⇒ 本用例红
           （那条事件要空等到租约到期才轮到别人）。
        """
        binding = self._upstream("lease")
        event = JidanpuSyncEvent.objects.create(
            direction="out", kind=jidanpu_upstream.KIND_COMPLETE, status="pending",
            target_type="order", target_ref="sched|lease",
            payload={"upstream_binding_id": binding.pk},
        )
        interrupt = _HandlerRecorder(exc=KeyboardInterrupt())
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(jidanpu_upstream._HANDLERS, {
                 jidanpu_upstream.KIND_COMPLETE: interrupt,
             }), mock.patch.object(
                 jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())
             ), mock.patch.object(
                 self.module, "tenant_context", _noop_context
             ), mock.patch.object(
                 self.module.timezone, "now", return_value=NOW
             ):
            patcher, _ = _patch_tenants(self.module, [self.tenant])
            try:
                with self.assertRaises(KeyboardInterrupt):
                    call_command("jidanpu_sync", "--once", stdout=out, stderr=err)
            finally:
                patcher.stop()
        event.refresh_from_db()
        self.assertEqual(
            event.lease_owner, None, "Ctrl-C 之后事件上还挂着租约（会空等一个租约时长）"
        )
        self.assertEqual(event.lease_expires_at, None)
        self.assertEqual(event.status, "pending")
        # 下一趟（正常消费）能立刻接手 ⇒ 没有永久租约
        done = _HandlerRecorder(outcome="done")
        with mock.patch.dict(jidanpu_upstream._HANDLERS, {
                 jidanpu_upstream.KIND_COMPLETE: done,
             }), mock.patch.object(
                 jidanpu_upstream, "sync_hall", return_value=HallOutcome()
             ), mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome()
             ), mock.patch.object(
                 jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())
             ):
            self._run(now=NOW + timedelta(seconds=1))
        self.assertEqual(done.calls, [event.pk])
        event.refresh_from_db()
        # ⚠️ 桩处理器只回结论、**不写终态**（真处理器才写）⇒ 这里只钉「它被消费到了」
        #    与「租约仍然为空」；终态由 `test_jidanpu_upstream_consume` 那一族守。
        self.assertEqual(event.lease_owner, None)
        self.assertEqual(event.lease_expires_at, None)


# ── ⑧ 健康列─────────────────────────────────────
#    跨线同口径：`jidanpu_member._stamp_poll_health`（快路 / 回拉成功即盖 `last_ok_at` +
#    清旧错）与 `jidanpu_hall._stamp_round_result`（整轮成功即盖）都**已经**这么干了，
#    本线此前只让 `run_outbox` 在**有待发事件**时盖 ⇒ 「只跑读取」的绑定永远「从未成功」。
#    ⛔ 本文件不动那两条线（差异只在交付说明里登记）。


class UpstreamHealthStampTests(_ScheduleTestCase):
    """一轮**读路完整成功**才盖 `last_ok_at`；部分成功（拉不全 / 抛异常 / 什么都没做 /
    outbox 记了失败 · 停用）一律不盖，而且**不许**清掉这一轮刚写下的 `last_error`。"""

    def _row(self, binding):
        return JidanpuUpstreamBinding.objects.get(pk=binding.pk)

    def _old(self):
        return NOW - timedelta(seconds=999)

    def test_orders_result_without_any_read_does_not_stamp_success(self):
        binding = self._upstream("orders-not-read")
        old = self._old()
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(
            last_ok_at=old, last_error="上一轮留下的旧错", last_error_at=old,
        )
        with self._patched_upstream(
            hall=HallOutcome(complete=True, is_owner=False), orders=MyOrdersOutcome(read=False),
        ):
            self._run()
        row = self._row(binding)
        self.assertEqual(row.last_ok_at, old)
        self.assertEqual(row.last_error, "上一轮留下的旧错")

    def test_a_fully_successful_read_round_stamps_last_ok_at_and_clears_the_old_error(self):
        """⭐ 裁定 1：完整成功的一轮（大厅 + 我的订单都到点、拉全）⇒ 盖 `last_ok_at`
        并清掉旧的 `last_error` / `last_error_at`。

        场景 = 「**只跑读取、没有待发事件**」的绑定（outbox 空转、两条读路都成功）——
        它此前永远显示「从未成功」，而运维判「这条线在不在跑」看的正是 `last_ok_at`。
        失败方向：删掉 `_stamp_binding_ok` 那次收窄写入 ⇒ `last_ok_at` 停在旧值、
        旧错还在 ⇒ 本用例红。
        """
        binding = self._upstream("ok-stamp")
        old = self._old()
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(
            last_ok_at=old, last_error="上一轮留下的旧错", last_error_at=old,
        )
        with self._patched_upstream(
            hall=HallOutcome(complete=True), orders=MyOrdersOutcome(pulled=1)
        ):
            self._run()
        row = self._row(binding)
        self.assertEqual(row.last_ok_at, NOW)
        self.assertEqual(row.last_error, "")
        self.assertIsNone(row.last_error_at)

    def test_a_failed_round_then_a_successful_round_moves_last_ok_at_and_clears_last_error(self):
        """⭐ 裁定 2：先失败（拉不全）写进 `last_error` ⇒ 再跑一轮完整成功
        ⇒ `last_error` 变空、`last_ok_at` **前进**。

        失败方向：成功分支只盖 `last_ok_at` 而**不清旧错** ⇒ 页面上会一直挂着一条
        早就好了的错 ⇒ 本用例红（两个字段都要跟着动，⛔ 不是只看一个）。
        """
        binding = self._upstream(
            "fail-then-ok", hall_poll_seconds=HALL_DEFAULT_SECONDS,
            my_orders_poll_seconds=MY_ORDERS_DEFAULT_SECONDS,
        )
        reason = "上家自报总数偏小，本轮未拉全（探页仍有行），本轮不判消失"
        with self._patched_upstream(hall=HallOutcome(complete=False, reason=reason)):
            self._run()
        failed = self._row(binding)
        self.assertEqual(failed.last_error, reason)
        self.assertEqual(failed.last_error_at, NOW)
        self.assertIsNone(failed.last_ok_at)
        later = NOW + timedelta(seconds=HALL_DEFAULT_SECONDS)
        with self._patched_upstream(hall=HallOutcome(complete=True)):
            self._run(now=later)
        recovered = self._row(binding)
        self.assertEqual(recovered.last_ok_at, later)      # 前进了
        self.assertEqual(recovered.last_error, "")         # 旧错被清掉
        self.assertIsNone(recovered.last_error_at)

    def test_an_incomplete_hall_round_never_stamps_last_ok_at(self):
        """裁定 1 的边界①：**拉不全** ⇒ ⛔ 不盖 `last_ok_at`，且那条 reason **留在** `last_error`。

        ⚠️ 这是裁定 1 点名要守住的那一档：拉不全刚在**同一轮**写过 `last_error`，
        再盖成功会把它清掉 ⇒ 等于把「没拉全」说成健康。
        ⚠️ **本条钉的是「读路那一次盖章」**（`_stamp_binding_ok`）——本用例把 `run_outbox`
        **桩掉**了 ⇒ `/outbox` 段没有任何成功事件 ⇒ 那一处独立盖章（`_stamp_health`）也不动
        `last_ok_at`。⚠️ **别把本条读成「拉不全一律不盖 `last_ok_at`」**：本线**有两处独立盖章**，
        outbox 段本轮真成功过时它自己会盖 —— 组合见下面
        `test_an_incomplete_hall_round_still_advances_last_ok_at_when_the_outbox_segment_succeeded`。
        失败方向：把 `hall.complete` 从判据里删掉（只看到点 + 没抛异常）⇒ 本用例红。
        """
        binding = self._upstream("hall-incomplete")
        old = self._old()
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(last_ok_at=old)
        reason = "翻页达到上限 15 页仍未拉全，未判「消失」"
        with self._patched_upstream(hall=HallOutcome(complete=False, reason=reason)):
            self._run()
        row = self._row(binding)
        self.assertEqual(row.last_ok_at, old)          # ⛔ 不许推进
        self.assertEqual(row.last_error, reason)
        self.assertEqual(row.last_error_at, NOW)

    def test_an_incomplete_hall_round_still_advances_last_ok_at_when_the_outbox_segment_succeeded(self):
        """上一条只管**读路那一次盖章**。
        盖章有两处，任一成立都推进 `last_ok_at`：① **outbox 段**（`_stamp_health`：本轮有事件成功且无失败）
        ② **整轮读路满四档**（`_stamp_binding_ok`）。
        一轮以「大厅拉不全」收尾、而 outbox 段本轮**真的成功过**时，`last_ok_at` **照样前进**，
        同时 `last_error` / `last_error_at` **仍被写**（第二条告警规则兜底，⛔ 不是静默）。

        ⚠️ **为什么必须真跑 `run_outbox`**：本文件其余 health 用例都把它桩掉
        （`_patched_upstream`）⇒ 那些只能证明「读路那次盖章被拦住」，**抓不到这一档**。
        失败方向（两条各自可红）：① 把 `_stamp_health` 的成功分支改成「只有整轮读路也成功
        才盖」⇒ `last_ok_at` 停在旧值 ⇒ 红；② 删掉 `_stamp_binding_error` 那次写入 ⇒
        `last_error` 变空 ⇒ 红。
        """
        binding = self._upstream("hall-incomplete-outbox-ok")
        old = self._old()
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(
            last_ok_at=old, last_error="上一轮留下的旧错", last_error_at=old,
        )
        event = JidanpuSyncEvent.objects.create(
            direction="out", kind=jidanpu_upstream.KIND_COMPLETE, status="pending",
            target_type="order", target_ref="sched|health-outbox-ok-incomplete-hall",
            payload={"upstream_binding_id": binding.pk},
        )
        recorder = _HandlerRecorder(outcome="done")
        reason = "翻页达到上限 15 页仍未拉全，未判「消失」"
        with mock.patch.dict(jidanpu_upstream._HANDLERS, {
                 jidanpu_upstream.KIND_COMPLETE: recorder,
             }), mock.patch.object(
                 jidanpu_upstream, "sync_hall",
                 return_value=HallOutcome(complete=False, reason=reason),
             ), mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome()
             ), mock.patch.object(
                 jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())
             ):
            self._run()
        # 桩接上了（outbox 段**真的**跑了，⛔ 不是空转）——少了这条，下面的读数就不指向这一档。
        self.assertEqual(recorder.calls, [event.pk])
        row = self._row(binding)
        self.assertEqual(row.last_ok_at, NOW)          # ⭐ 前进（outbox 段那一处独立盖章）
        self.assertNotEqual(row.last_ok_at, old)
        self.assertEqual(row.last_error, reason)       # 拉不全仍被写下（第二条规则兜底）
        self.assertEqual(row.last_error_at, NOW)

    def test_a_read_segment_that_raises_never_stamps_last_ok_at(self):
        """裁定 1 的边界②：**某一段抛异常**（这里让「我的订单」抛、大厅成功）⇒ ⛔ 不盖，
        也不许清掉上一轮的旧错。

        ⚠️ 为什么让**后一段**抛：大厅成功 + 「我的订单」抛 ⇒ 另外三档判据（到点 / 拉全 /
        outbox 干净）**全为真**，「没有未预期异常」这一档才是唯一拦住它的那条 ⇒ 这条用例
        才能把那一档单独钉住（若让大厅抛，别的档也会拦住，变异读数会说不清是谁拦的）。
        失败方向：把 `not unexpected` 从判据里删掉 ⇒ 本用例红。
        """
        binding = self._upstream("orders-boom")
        old = self._old()
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(
            last_ok_at=old, last_error="上一轮的旧错", last_error_at=old,
        )
        with mock.patch.object(
            jidanpu_upstream, "run_outbox", return_value=OutboxStats()
        ), mock.patch.object(
            jidanpu_upstream, "sync_hall", return_value=HallOutcome(complete=True)
        ), mock.patch.object(
            jidanpu_upstream, "sync_my_orders", side_effect=RuntimeError("boom")
        ), mock.patch.object(
            jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())
        ):
            caught, _out, err = self._run(expect_error=True)
        self.assertEqual(caught.returncode, 1)
        self.assertIn("RuntimeError", err)
        row = self._row(binding)
        self.assertEqual(row.last_ok_at, old)              # ⛔ 不许推进
        self.assertEqual(row.last_error, "上一轮的旧错")   # ⛔ 不许清
        self.assertEqual(row.last_error_at, old)

    def test_a_round_where_no_read_path_is_due_never_stamps_last_ok_at(self):
        """裁定 1 的边界③：两条读路**都未到点**（这一轮什么都没做）⇒ ⛔ 不盖。

        失败方向：成功判据只看「没抛异常」（不看有没有真的跑）⇒ 每拍都盖成功
        ⇒ 对运维说假话 ⇒ 本用例红。
        """
        binding = self._upstream(
            "nothing-due", hall_poll_seconds=HALL_DEFAULT_SECONDS,
            my_orders_poll_seconds=MY_ORDERS_DEFAULT_SECONDS,
        )
        old = self._old()
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(last_ok_at=old)
        self.module._UPSTREAM_HALL_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        self.module._UPSTREAM_MY_ORDERS_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        with self._patched_upstream():
            self._run()
        self.assertEqual(self._row(binding).last_ok_at, old)

    def test_a_non_owner_hall_round_alone_never_stamps_last_ok_at(self):
        """裁定 1 的边界③（另一档）：这一轮**只有大厅到点**、而大厅回**非 owner**
        （A2：「一次上游都没打」）⇒ 这一轮实际零上游动作 ⇒ ⛔ 不盖。

        失败方向：把 `hall.is_owner` 从「真的跑了一条读路」的判据里删掉 ⇒ 本用例红。
        """
        binding = self._upstream(
            "non-owner", hall_poll_seconds=HALL_DEFAULT_SECONDS,
            my_orders_poll_seconds=MY_ORDERS_DEFAULT_SECONDS,
        )
        old = self._old()
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(last_ok_at=old)
        self.module._UPSTREAM_HALL_AT[self._key(binding)] = NOW - timedelta(
            seconds=HALL_DEFAULT_SECONDS + 1
        )
        self.module._UPSTREAM_MY_ORDERS_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        with self._patched_upstream(
            hall=HallOutcome(is_owner=False, reason=jidanpu_upstream.NOT_OWNER_REASON)
        ) as (_box, hall_stub, my_orders):
            self._run()
        hall_stub.assert_called_once()
        my_orders.assert_not_called()
        self.assertEqual(self._row(binding).last_ok_at, old)

    def test_an_outbox_failure_recorded_this_round_is_not_cleared_by_a_successful_read(self):
        """裁定 1 的边界④：outbox 这一轮记了 **`failed`** ⇒ 读路成功也**不许**清那条错。

        ⚠️ 真跑 `run_outbox`（桩处理器只回结论），让 `_stamp_health` 真的把失败原因写进
        `last_error`，再看读路那一轮有没有把它清掉。
        失败方向：成功判据只看 `unexpected`（不看 outbox 计数）⇒ 刚写下的「仍失败」被清掉
        ⇒ 本用例红。
        """
        binding = self._upstream("outbox-failed")
        event = JidanpuSyncEvent.objects.create(
            direction="out", kind=jidanpu_upstream.KIND_COMPLETE, status="pending",
            target_type="order", target_ref="sched|health-failed",
            payload={"upstream_binding_id": binding.pk},
        )
        recorder = _HandlerRecorder(outcome="failed")
        with mock.patch.dict(jidanpu_upstream._HANDLERS, {
                 jidanpu_upstream.KIND_COMPLETE: recorder,
             }), mock.patch.object(
                 jidanpu_upstream, "sync_hall", return_value=HallOutcome(complete=True)
             ), mock.patch.object(
                 jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome()
             ), mock.patch.object(
                 jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())
             ):
            self._run()
        self.assertEqual(recorder.calls, [event.pk])
        row = self._row(binding)
        self.assertIn("仍失败", row.last_error)
        self.assertEqual(row.last_error_at, NOW)
        self.assertIsNone(row.last_ok_at)

    def test_a_burned_outbox_event_also_keeps_last_error_despite_a_successful_read(self):
        """裁定 1 的边界④（`abandoned` 那一档）：烧完额度的事件让 `_stamp_health` 写下
        「需人工核对」⇒ 读路成功也**不许**把它清掉。

        ⚠️ 与上一条**分开**：`abandoned` 与 `failed` 是两个计数（`_consume_all` 里烧完额度
        的行 `continue`、不计 `failed`）⇒ 只钉一条不能证明另一条也在判据里。
        失败方向：把 `stats.abandoned` 从判据里删掉 ⇒ 本用例红。
        """
        binding = self._upstream("outbox-burned")
        JidanpuSyncEvent.objects.create(
            direction="out", kind=jidanpu_upstream.KIND_COMPLETE, status="failed",
            target_type="order", target_ref="sched|health-burned",
            attempts=jidanpu_upstream.OUTBOX_MAX_ATTEMPTS, processed_at=NOW,
            payload={"upstream_binding_id": binding.pk},
        )
        with mock.patch.object(
            jidanpu_upstream, "sync_hall", return_value=HallOutcome(complete=True)
        ), mock.patch.object(
            jidanpu_upstream, "sync_my_orders", return_value=MyOrdersOutcome()
        ), mock.patch.object(
            jidanpu, "_open_client_pair", new=_open_pair(mock.Mock())
        ):
            _caught, out, _err = self._run()
        self.assertIn("已放弃 1", out)
        row = self._row(binding)
        self.assertIn("不再自动重试", row.last_error)
        self.assertIsNone(row.last_ok_at)

    def test_a_paused_outbox_round_never_stamps_last_ok_at(self):
        """裁定 1 的边界③：outbox 回 **`paused`**（遍历那一刻还启用、跑到这一段已被停用）
        ⇒ ⛔ 不盖——停用的绑定在页面上不许显示「健康」。

        ⚠️ 这条模拟的是**停用与遍历之间那道缝**（`enabled_bindings()` 的快照挡不住它）。
        失败方向：把 `stats.paused` 从判据里删掉 ⇒ 本用例红。
        """
        binding = self._upstream("paused-stamp")
        old = self._old()
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(last_ok_at=old)
        with self._patched_upstream(outbox=OutboxStats(paused=True)):
            self._run()
        self.assertEqual(self._row(binding).last_ok_at, old)

    def test_a_disabled_binding_never_gets_a_health_stamp(self):
        """裁定 1 的边界③（最外那一档）：`enabled=False` ⇒ 整行跳过 ⇒ 连 `last_ok_at` 都不许动。

        失败方向：遍历不筛 `enabled`（把停用的行也交给 `_run_upstream_binding`）⇒
        桩三段照跑、收窄写入盖上成功 ⇒ 本用例红。
        """
        binding = self._upstream("disabled-stamp", enabled=False)
        with self._patched_upstream():
            self._run()
        row = self._row(binding)
        self.assertIsNone(row.last_ok_at)
        self.assertEqual(row.last_error, "")

    def test_an_empty_hall_reason_never_writes_an_empty_last_error(self):
        """⭐ 裁定 3：`hall.reason` 是**空串** ⇒ ⛔ 不许盖出一个**空的失败原因**。

        `last_error=""` 配一个**新的** `last_error_at` 与「从没失败过」长得一模一样
        ⇒ 按「没有可用原因」写固定人话顶替（依据：同口径先例 `_round_failure_reason` 与
        `jidanpu_member._stamp_poll_health` 的 `reason or ROUND_UNKNOWN_FAILURE_REASON`）。
        ⚠️ stdout 也照同一句兜底（⛔ 不出现空的「（不完整：）」）。
        失败方向：删掉 `_stamp_binding_error` 的空串守卫 ⇒ `last_error == ""` ⇒ 本用例红。
        """
        binding = self._upstream("empty-reason")
        with self._patched_upstream(hall=HallOutcome(complete=False, reason="")):
            _caught, out, _err = self._run()
        row = self._row(binding)
        self.assertEqual(row.last_error, jidanpu_upstream.ROUND_UNKNOWN_FAILURE_REASON)
        self.assertNotEqual(row.last_error, "")
        self.assertEqual(row.last_error_at, NOW)
        self.assertIn(jidanpu_upstream.ROUND_UNKNOWN_FAILURE_REASON, out)


class HallTailRetirementTests(_ScheduleTestCase):
    """⭐ D-0366（2026-11-03）：大厅**快路**整体退役——调度里不再有快路这一拍。

    旧三条判据（到点跑 tail / 全量让位 / 坏了响亮不失败）随机制一并删除；本类改钉
    **退役**本身（防回潮）：
    ① 调度模块不再有快路锚 `_UPSTREAM_HALL_TAIL_AT`；
    ② 全量未到点的拍**不调** `sync_hall`（旧形状里快路会顶上，回潮即红）。
    `HALL_TAIL_*` 常量与 `_fetch_hall_tail` 的缺席钉在 `test_jidanpu_upstream_pull`
    的退役网（同批 D-0366）。
    """

    def test_the_tail_anchor_state_is_gone_from_the_scheduler_module(self):
        self.assertFalse(
            hasattr(self.module, "_UPSTREAM_HALL_TAIL_AT"),
            "快路锚已随 D-0366 退役：调度模块不应再有 _UPSTREAM_HALL_TAIL_AT",
        )

    def test_no_hall_pull_happens_when_the_full_sweep_is_not_due(self):
        binding = self._upstream("tail-retired", hall_poll_seconds=HALL_DEFAULT_SECONDS)
        # 全量刚跑过（未到点）⇒ 旧行为里这一拍会跑快路；退役后**什么都不该拉**。
        self.module._UPSTREAM_HALL_AT[self._key(binding)] = NOW - timedelta(seconds=1)
        with self._patched_upstream() as (_box, hall_stub, _orders):
            self._run()
        hall_stub.assert_not_called()

