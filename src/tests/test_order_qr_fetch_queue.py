"""马上来查码队列（`fetch_order_qr_queue` 每分钟定时补拉；D-0356）——候选形状 / 持久退避 /
两通道台账分口 / 配置闸 / 24h 窗口与上限 / 端到端落库。

## 测试纪律（沿用 `tests/test_order_qr_fetch.py` 的同族口径）

- ⛔ **不打真接口**：查码走 `httpx.MockTransport`（patch `order_qr_fetch._build_transport`，
  E-0029——桩打在被查找的模块上）；下载 patch `_download_image` 返回合成字节。
- ⛔ **不起线程**：本命令同步逐单调 `_run_fetch_careful`——它的 `finally` 会
  `connection.close()`，而 TestCase 的全部数据活在一个测试事务里，关连接 = 数据原地蒸发。
  故 setUp 里把 `order_qr_fetch.connection` 换成「close 变 no-op、其余转发」的替身；
  `_run_fetch_careful` 的其余机械件（顶层 catch / 回值哨兵）**全部真跑**。
- 夹具全部**合成值**（单号 `2026…`、密钥 `synthetic-*`、基址 `https://malailai.test`——
  RFC 2606 保留域，⛔ 不是对端真域名）。
- 断言一律打在**数据库行**（`refresh_from_db` / 重取）与**台账行**上，⛔ 不看内存对象。
"""
from __future__ import annotations

import io
import pathlib
import tempfile
from datetime import timedelta
from decimal import Decimal
from http import HTTPStatus
from unittest import mock

import httpx
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.audit.models import OperationLog
from apps.business import order_qr_fetch
from apps.business.management.commands import fetch_order_qr_queue as command_module
from apps.business.management.commands.fetch_order_qr_queue import (
    QUERY_INTERVAL_SECONDS,
    QUEUE_WINDOW_HOURS,
    qr_poll_due,
    queue_candidates,
)
from apps.business.models import (
    Game,
    JidanpuSyncEvent,
    Order,
    OrderQrFetchConfig,
    OrderScreenshot,
    OrderStatus,
)
from apps.core.storage import get_storage
from apps.platform.provisioning import provision_tenant

#: 合成密钥 / 基址（与既有查码测试同值：保留测试域，⛔ 不是对端真域名）。
SYNTHETIC_KEY = "synthetic-secret-key-123"
SYNTHETIC_BASE = "https://malailai.test"

SCHEMA_A = "oqq_a"
SCHEMA_B = "oqq_b"
DOMAIN_A = "oqq-a.test.local"
DOMAIN_B = "oqq-b.test.local"

#: 合成 JPEG（魔数 + 载荷；落库用例断言字节真的进了存储）。
JPEG = b"\xff\xd8\xff" + b"synthetic-jpeg-body"

#: 本文件统一的 20 位合成单号前缀（16 位 + ≤4 位后缀；位序含租户标记，两租户绝不互撞）。
NO_A = "2026103100000001"
NO_B = "2026103100000002"


def _not_found_envelope() -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "msg": "", "data": {"order_no": "x", "status": "group_not_found"}},
    )


def _generating_envelope() -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "msg": "", "data": {"order_no": "x", "status": "generating"}},
    )


def _ok_envelope(qr_url: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "msg": "", "data": {"order_no": "x", "status": "ok", "qr_url": qr_url}},
    )


class _NoCloseConnection:
    """`order_qr_fetch.connection` 的测试替身：只把 `close()` 变 no-op，其余转发真连接。

    存在理由见模块 docstring「不起线程」条：命令同步调 `_run_fetch_careful`，
    它的 finally 会关连接；TestCase 的事务经不起这一下（web 通道测试因此绕开
    `_run_fetch_careful` 直调 `_run_fetch`——本文件测的是命令全链，绕不开）。
    """

    def __init__(self, real):
        object.__setattr__(self, "_real", real)

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


class QueueCommandTestBase(TenantTestCase):
    """两租户夹具（照 `tests/test_tenant_isolation.py` 的 `provision_tenant` 先例）+
    一份启用配置 + 同步对端桩（默认回 group_not_found）。"""

    extra_test_tenants = (
        {"schema_name": SCHEMA_A, "name": "查码队列租户A", "domain": DOMAIN_A},
        {"schema_name": SCHEMA_B, "name": "查码队列租户B", "domain": DOMAIN_B},
    )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # ⚠️ 建租户必须在 setUpClass：schema DDL 进不了 TestCase 的事务（会原地报错）。
        cls.tenant_a, _ = provision_tenant(
            schema_name=SCHEMA_A, name="查码队列租户A", domain=DOMAIN_A
        )
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="查码队列租户B", domain=DOMAIN_B
        )

    def setUp(self):
        # schema 上下文会跨用例残留：第一行先钉回本类自己的租户。
        connection.set_tenant(self.tenant)
        self._peer_calls: list[httpx.Request] = []
        self._patches: list[mock._patch] = []
        # 对端桩：默认 group_not_found（不落库的静默态）；子用例用 `_peer_*` 改写。
        self._apply_peer(self._peer_handler)
        # 命令同步调 `_run_fetch_careful` ⇒ 关连接要变 no-op（见模块 docstring）。
        self._patch(order_qr_fetch, "connection", new=_NoCloseConnection(connection))
        # 限速间隔在测试里置 0（几百候选的用例不能真睡）；间隔行为由
        # `test_paces_queries_with_interval` 单独钉。
        self._patch(command_module, "QUERY_INTERVAL_SECONDS", new=0)

    # ── 桩的机械件 ──────────────────────────────────────────────────

    def _patch(self, target, attr, **kwargs):
        patcher = mock.patch.object(target, attr, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._patches.append(patcher)

    def _apply_peer(self, handler):
        self._patch(
            order_qr_fetch,
            "_build_transport",
            return_value=httpx.MockTransport(handler),
        )

    def _peer_handler(self, request: httpx.Request) -> httpx.Response:
        self._peer_calls.append(request)
        return _not_found_envelope()

    def _peer_raw(self, response: httpx.Response) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self._peer_calls.append(request)
            return response

        self._apply_peer(handler)

    def _peer_ok(self, qr_url: str = "https://wework.example/qr.png") -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self._peer_calls.append(request)
            return _ok_envelope(qr_url)

        self._apply_peer(handler)

    # ── 夹具 ────────────────────────────────────────────────────────

    def _config(self, tenant_schema: str) -> OrderQrFetchConfig:
        return OrderQrFetchConfig.objects.create(
            tenant_schema=tenant_schema,
            base_url=SYNTHETIC_BASE,
            api_key=SYNTHETIC_KEY,
            enabled=True,
        )

    def _order(self, schema: str, order_no: str, **kw) -> Order:
        with tenant_context(self._tenant(schema)):
            game = Game.objects.create(name=f"队列测试游戏-{schema}")
            defaults = dict(
                game=game,
                order_no=order_no,
                status=OrderStatus.IN_PROGRESS,
                hall_price=Decimal("88.00"),
                source_order_no="1234567890123456789",
            )
            defaults.update(kw)
            return Order.objects.create(**defaults)

    def _tenant(self, schema: str):
        return self.tenant_a if schema == SCHEMA_A else self.tenant_b

    def _row(self, schema: str, order_pk: int) -> Order:
        with tenant_context(self._tenant(schema)):
            return Order.objects.get(pk=order_pk)

    def _run(self, *args) -> str:
        buf = io.StringIO()
        call_command("fetch_order_qr_queue", *args, stdout=buf)
        return buf.getvalue()

    def _ledger_rows(self, schema: str, order_no: str):
        with tenant_context(self._tenant(schema)):
            return list(
                OperationLog.objects.filter(
                    action="order.malailai_qr_fetch", target_name=order_no
                )
            )

    def _screenshots(self, schema: str, order_no: str):
        with tenant_context(self._tenant(schema)):
            return list(
                OrderScreenshot.objects.filter(order__order_no=order_no, img_type=1)
            )


class CandidateShapeTests(QueueCommandTestBase):
    """候选查询的 exclude 跨关系语义：有**任何**群码行（ready 或 pending）都跳过；
    只有 `img_type=0` 的截图不影响入选（计划 §一「钉用例」三条）。"""

    def setUp(self):
        super().setUp()
        with tenant_context(self.tenant_a):
            self.game = Game.objects.create(name="形状测试游戏")
            self.order = Order.objects.create(
                game=self.game, order_no=f"{NO_A}01", status=OrderStatus.IN_PROGRESS
            )

    def _in_candidates(self) -> bool:
        with tenant_context(self.tenant_a):
            now = timezone.now()
            return queue_candidates(now).filter(pk=self.order.pk).exists()

    def _add_screenshot(self, *, img_type: int, status: str) -> None:
        with tenant_context(self.tenant_a):
            uploader = order_qr_fetch.placeholder_malailai_user()
            OrderScreenshot.objects.create(
                order=self.order,
                img_type=img_type,
                object_key=f"synthetic/{self.order.order_no}-{img_type}-{status}.jpg",
                uploaded_by=uploader,
                status=status,
                size_bytes=len(JPEG),
                content_type="image/jpeg",
            )

    def test_an_order_without_any_screenshot_is_a_candidate(self):
        self.assertTrue(self._in_candidates())

    def test_a_ready_group_qr_row_excludes_the_order(self):
        self._add_screenshot(img_type=1, status="ready")
        self.assertFalse(self._in_candidates())

    def test_a_pending_group_qr_row_excludes_the_order_too(self):
        """孤儿 pending 残差的另一面：pending 行把单挡在队列外（签发凭证在途）。"""
        self._add_screenshot(img_type=1, status="pending")
        self.assertFalse(self._in_candidates())

    def test_an_order_screenshot_img_type_zero_does_not_exclude(self):
        self._add_screenshot(img_type=0, status="ready")
        self.assertTrue(self._in_candidates())


class SuppressBackoffTests(QueueCommandTestBase):
    """⭐ 2026-11-01 业务方口径：普通失败**不写**退避列（每分钟一轮扫描本身就是节流，
    下一轮自然重试）；**唯一例外是对端 429**（对端明确给「过 N 秒再来」⇒ 写列听对端的）。
    ok 也不写（ready 行自然排除）。"""

    def setUp(self):
        super().setUp()
        with tenant_context(self.tenant_a):
            self.config = self._config(SCHEMA_A)
        self.order = self._order(SCHEMA_A, f"{NO_A}11")

    def test_a_failed_fetch_writes_no_backoff_and_retries_next_round(self):
        # 对端默认桩回 not_found：第一轮消费 1、列保持空；第二轮它仍是候选、再打一次
        # ——「每分钟一轮扫描本身就是节流」的钉子。
        self._run("--tenant", SCHEMA_A)
        row = self._row(SCHEMA_A, self.order.pk)
        self.assertIsNone(
            row.qr_fetch_suppress_until,
            "普通失败（not_found）不写退避——节流靠每分钟一轮，不靠单单退避",
        )
        self._run("--tenant", SCHEMA_A)
        self.assertEqual(len(self._peer_calls), 2)

    def test_an_expired_429_backoff_lets_the_order_reenter_the_queue(self):
        self._peer_raw(
            httpx.Response(
                HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": "1800"},
                json={"code": 429, "msg": "slow down"},
            )
        )
        self._run("--tenant", SCHEMA_A)
        self.assertEqual(len(self._peer_calls), 1)
        # 退避期内第二轮：429 窗口未过 ⇒ 不再候选 ⇒ 对端零调用。
        self._run("--tenant", SCHEMA_A)
        self.assertEqual(len(self._peer_calls), 1)
        # 对端给的窗口过去 ⇒ 重新进候选、再打一次。
        with tenant_context(self.tenant_a):
            Order.objects.filter(pk=self.order.pk).update(
                qr_fetch_suppress_until=timezone.now() - timedelta(seconds=1)
            )
        self._run("--tenant", SCHEMA_A)
        self.assertEqual(len(self._peer_calls), 2)

    def test_rate_limited_without_retry_after_falls_back_to_sixty(self):
        """429 没带 `Retry-After` 头 ⇒ 至少等 60s（`COOLDOWN_SECONDS` 兜底）。"""
        self._peer_raw(
            httpx.Response(
                HTTPStatus.TOO_MANY_REQUESTS,
                json={"code": 429, "msg": "slow down"},
            )
        )
        before = timezone.now()
        self._run("--tenant", SCHEMA_A)
        row = self._row(SCHEMA_A, self.order.pk)
        after = timezone.now()
        self.assertIsNotNone(row.qr_fetch_suppress_until)
        self.assertGreaterEqual(row.qr_fetch_suppress_until, before + timedelta(seconds=59))
        self.assertLessEqual(row.qr_fetch_suppress_until, after + timedelta(seconds=61))

    def test_a_successful_fetch_does_not_write_the_backoff_column(self):
        self._peer_ok()
        with mock.patch.object(
            order_qr_fetch, "_download_image", return_value=JPEG
        ):
            out = self._run("--tenant", SCHEMA_A)
        self.assertIn(f"{SCHEMA_A}: 候选 1 / 消费 1 / ok 1", out)
        row = self._row(SCHEMA_A, self.order.pk)
        self.assertIsNone(
            row.qr_fetch_suppress_until,
            "ok 落了 ready 群码行 ⇒ 候选查询自然排除该单，退避列应保持空",
        )

    def test_rate_limited_backoff_takes_the_max_of_60_and_retry_after(self):
        """对端 429 + `Retry-After: 1800` ⇒ 列退避 ≈1800s（⛔ 不把对端给的窗口压回 60）。"""
        self._peer_raw(
            httpx.Response(
                HTTPStatus.TOO_MANY_REQUESTS,
                headers={"Retry-After": "1800"},
                json={"code": 429, "msg": "slow down"},
            )
        )
        before = timezone.now()
        self._run("--tenant", SCHEMA_A)
        row = self._row(SCHEMA_A, self.order.pk)
        after = timezone.now()
        self.assertIsNotNone(row.qr_fetch_suppress_until)
        self.assertGreaterEqual(row.qr_fetch_suppress_until, before + timedelta(seconds=1799))
        self.assertLessEqual(row.qr_fetch_suppress_until, after + timedelta(seconds=1800))


class UnexpectedExceptionTests(QueueCommandTestBase):
    """未预期异常：不写退避列（2026-11-01 口径）、退出码 1（CommandError）、
    其他租户不受影响（一炸一成）。"""

    def setUp(self):
        super().setUp()
        with tenant_context(self.tenant_a):
            self._config(SCHEMA_A)
        with tenant_context(self.tenant_b):
            self._config(SCHEMA_B)
        self.order_a = self._order(SCHEMA_A, f"{NO_A}21")
        self.order_b = self._order(SCHEMA_B, f"{NO_B}21")

    def _make_order(self, schema: str, order_no: str, *, hours_old: float) -> Order:
        """同租户第二张候选单（`_order` 的游戏名唯一、一事务只能建一次 ⇒ 这里自带
        独名游戏 + 显式 `created_at` 摆「老单优先」的位次）。"""
        with tenant_context(self._tenant(schema)):
            game = Game.objects.create(name=f"毒单测试游戏-{order_no}")
            order = Order.objects.create(
                game=game, order_no=order_no, status=OrderStatus.IN_PROGRESS
            )
            Order.objects.filter(pk=order.pk).update(
                created_at=timezone.now() - timedelta(hours=hours_old)
            )
        return order

    def test_a_poison_order_no_longer_interrupts_the_round(self):
        """M6（2026-11-01）：毒单（反复未预期异常的老单）**不再中断**本租户——
        两候选一炸一成：炸的进 failures 明细（`<单号>: unexpected`）且不再抛中断，
        毒单后面的候选仍被消费（对端真的被打）；本轮结束仍退出码 1（CommandError）。"""
        poison = self._make_order(SCHEMA_A, f"{NO_A}22", hours_old=3)
        good = self._make_order(SCHEMA_A, f"{NO_A}23", hours_old=1)
        real_run_fetch = order_qr_fetch._run_fetch

        def exploding(**kwargs):
            if kwargs.get("order_no") == poison.order_no:
                raise RuntimeError("synthetic-boom")
            return real_run_fetch(**kwargs)

        self._patch(order_qr_fetch, "_run_fetch", side_effect=exploding)
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "fetch_order_qr_queue", "--tenant", SCHEMA_A, stdout=out, stderr=err
            )
        text = out.getvalue()
        # 毒单：记账跳过（明细行 `: unexpected`）；摘要行照常打出 = 本轮没被中断
        # （3 张候选全部消费完——setUp 的 order_a + 毒单 + 好单）。
        self.assertIn(f"{SCHEMA_A}: 候选 3 / 消费 3 / ok 0", text)
        self.assertIn(f"  - {poison.order_no}: unexpected", text)
        # 毒单后面的候选仍被消费：对端被打到的是另外两张单（毒单没到对端就炸了）。
        self.assertEqual(len(self._peer_calls), 2)
        hit = "\n".join(str(call.url) for call in self._peer_calls)
        self.assertIn(f"order_no={good.order_no}", hit)
        self.assertIn(f"order_no={self.order_a.order_no}", hit)
        self.assertNotIn(f"order_no={poison.order_no}", hit)
        # 响亮信号保留：本轮出现过未预期失败 ⇒ stderr 点名 + 退出码 1。
        self.assertIn("未预期失败", err.getvalue())

    def test_an_exploding_tenant_fails_the_run_and_spares_the_other(self):
        real_run_fetch = order_qr_fetch._run_fetch

        def exploding(**kwargs):
            if kwargs.get("schema") == SCHEMA_A:
                raise RuntimeError("synthetic-boom")
            return real_run_fetch(**kwargs)

        self._patch(order_qr_fetch, "_run_fetch", side_effect=exploding)
        with self.assertRaises(CommandError):
            self._run()
        # A 的一单：异常不写退避列（下一轮自然重试）、没写台账。
        row_a = self._row(SCHEMA_A, self.order_a.pk)
        self.assertIsNone(
            row_a.qr_fetch_suppress_until,
            "未预期异常不写退避（2026-11-01：每分钟扫描本身就是节流）",
        )
        self.assertEqual(self._ledger_rows(SCHEMA_A, self.order_a.order_no), [])
        # B 的一单：照常拉成（对端默认桩回 not_found 也算「正常走完」——这里断言它
        # 真被打过一次、且没被 A 拖进失败）。
        self.assertEqual(len(self._peer_calls), 1)
        self.assertIn(f"order_no={self.order_b.order_no}", str(self._peer_calls[0].url))
        row_b = self._row(SCHEMA_B, self.order_b.pk)
        # not_found 不写退避（口径）；A 的炸不影响 B 的收尾。
        self.assertIsNone(row_b.qr_fetch_suppress_until)


class WindowOrderAndLimitTests(QueueCommandTestBase):
    """24h 窗口（25h 前不进）+ 软删排除 + 老单优先 + 批/候选上限 + `--limit`。"""

    def setUp(self):
        super().setUp()
        with tenant_context(self.tenant_a):
            self._config(SCHEMA_A)
            self.game = Game.objects.create(name="窗口测试游戏")

    def _make(self, order_no: str, *, hours_old: float = 1.0, deleted: bool = False) -> Order:
        # ⚠️ 2026-11-03 分层间隔起，「老单优先 / 全量逐个问」那组窗口用例的单龄一律
        #    **向下对齐到 15 分钟的整数倍**（三个分档的到期块都以它为公约数）⇒ 每张
        #    都是块首、每轮必问——这组用例钉的是窗口 / 顺序 / 上限语义，⛔ 不让它被
        #    分层判定搅了（分层本身由 TieredIntervalTests 钉）。
        aligned_hours = (int(hours_old * 3600) // 900) * 900 / 3600
        with tenant_context(self.tenant_a):
            order = Order.objects.create(
                game=self.game, order_no=order_no, status=OrderStatus.IN_PROGRESS
            )
            changes: dict[str, object] = {
                "created_at": timezone.now() - timedelta(hours=aligned_hours)
            }
            if deleted:
                changes["deleted_at"] = timezone.now()
            # ⚠️ update 也必须在租户上下文里：上下文外跑 = 写到错误 schema、静默不命中。
            Order.objects.filter(pk=order.pk).update(**changes)
        return order

    def test_an_order_older_than_the_window_is_not_a_candidate(self):
        order = self._make(f"{NO_A}31", hours_old=QUEUE_WINDOW_HOURS + 1)
        out = self._run("--tenant", SCHEMA_A, "--dry-run")
        self.assertIn(f"{SCHEMA_A}: 候选 0", out)
        row = self._row(SCHEMA_A, order.pk)
        self.assertIsNone(row.qr_fetch_suppress_until)

    def test_a_soft_deleted_order_is_not_a_candidate(self):
        self._make(f"{NO_A}32", deleted=True)
        self.assertIn(f"{SCHEMA_A}: 候选 0", self._run("--tenant", SCHEMA_A, "--dry-run"))

    def test_oldest_first_and_limit_one_consumes_only_the_oldest(self):
        old = self._make(f"{NO_A}33", hours_old=20)
        new = self._make(f"{NO_A}34", hours_old=1)
        self._run("--tenant", SCHEMA_A, "--limit", "1")
        self.assertEqual(len(self._peer_calls), 1)
        self.assertIn(f"order_no={old.order_no}", str(self._peer_calls[0].url))
        # 没被消费的那张单没碰对端 ⇒ 也没写退避。
        self.assertIsNone(self._row(SCHEMA_A, new.pk).qr_fetch_suppress_until)

    def test_consumes_all_candidates_no_batch_cap(self):
        """⭐ 2026-11-01 业务方口径：每轮把候选**全部**问一遍（没有批上限——
        「后面的订单扫不到」的饿死洞由此堵死）；节流靠每单之间的间隔。"""
        count = 40
        for index in range(count):
            # 单龄 10h 起每张 +15 分钟（⭐ 分层间隔的块首步进）：都在 24h 窗内、
            # 每张每轮必问——本用例钉的是「无批上限」，⛔ 不许只处理前 30。
            self._make(f"{NO_A}4{index:02d}", hours_old=10 + index * 0.25)
        self._run("--tenant", SCHEMA_A)
        self.assertEqual(
            len(self._peer_calls), count,
            "候选 40 张 ⇒ 对端被打 40 次（全量逐个问，⛔ 不许只处理前 30）",
        )

    def test_paces_queries_with_interval(self):
        """相邻两单之间垫 `QUERY_INTERVAL_SECONDS`（首单前不垫）：3 张候选 ⇒ sleep 2 次、
        每次都是常量值（0.5s ⇒ 每台 ≤120 查询/min，压着对端限速线）。真睡会拖慢测试
        ⇒ sleep 换成计数桩。"""
        sleeps: list[float] = []
        for index in range(3):
            self._make(f"{NO_A}4{index:02d}", hours_old=10 + index * 0.25)
        self._patch(command_module, "QUERY_INTERVAL_SECONDS", new=QUERY_INTERVAL_SECONDS)
        with mock.patch.object(
            command_module.time, "sleep", side_effect=sleeps.append
        ):
            self._run("--tenant", SCHEMA_A)
        self.assertEqual(
            sleeps, [QUERY_INTERVAL_SECONDS] * 2,
            "3 张候选 ⇒ 只垫 2 次间隔（首单前不垫）、间隔=常量值",
        )

    def test_skipped_outcome_is_counted_not_failed(self):
        """消费前竞态结局（`skipped`：软删 / 人工传了码 / 配置刚被关）——单列回
        「跳过」计数，⛔ 不混进失败明细（S1，评审 2026-10-31）。"""
        self._make(f"{NO_A}36", hours_old=2)
        with mock.patch.object(
            order_qr_fetch, "_run_fetch_careful", return_value="skipped"
        ):
            out = self._run("--tenant", SCHEMA_A)
        self.assertIn("跳过 1", out)
        self.assertNotIn("  - ", out, "skipped 不是失败：失败明细应为空")

    def test_dry_run_reports_all_candidates(self):
        count = QUEUE_WINDOW_HOURS  # 任意 >0 的量；dry-run 报实际候选数、不切片
        for index in range(count):
            self._make(f"{NO_A}5{index:02d}", hours_old=10 + index / 100)
        out = self._run("--tenant", SCHEMA_A, "--dry-run")
        self.assertIn(f"{SCHEMA_A}: 候选 {count} ", out)

    def test_limit_below_one_is_rejected(self):
        with self.assertRaises(CommandError):
            self._run("--tenant", SCHEMA_A, "--limit", "0")


class ConfigGateTests(QueueCommandTestBase):
    """配置闸五档（无行 / 关 / 无密钥 / 无基址 / 401 闩）⇒ 整租户零出站零台账。"""

    def _order_in_a(self) -> Order:
        return self._order(SCHEMA_A, f"{NO_A}61")

    def _assert_zero_action(self, order: Order) -> None:
        self.assertEqual(self._peer_calls, [])
        self.assertEqual(self._ledger_rows(SCHEMA_A, order.order_no), [])
        row = self._row(SCHEMA_A, order.pk)
        self.assertIsNone(row.qr_fetch_suppress_until)

    def test_no_config_row_is_zero_action(self):
        order = self._order_in_a()
        self.assertIn("整租户零出站", self._run("--tenant", SCHEMA_A))
        self._assert_zero_action(order)

    def test_disabled_config_is_zero_action(self):
        with tenant_context(self.tenant_a):
            config = self._config(SCHEMA_A)
            config.enabled = False
            config.save()
        order = self._order_in_a()
        self._run("--tenant", SCHEMA_A)
        self._assert_zero_action(order)

    def test_blank_api_key_is_zero_action(self):
        with tenant_context(self.tenant_a):
            config = self._config(SCHEMA_A)
            config.api_key = ""
            config.save()
        order = self._order_in_a()
        self._run("--tenant", SCHEMA_A)
        self._assert_zero_action(order)

    def test_blank_base_url_is_zero_action(self):
        """F-1 同款：基址空 = 未配置——命令的整租户预判与 worker 复核同判据。"""
        with tenant_context(self.tenant_a):
            config = self._config(SCHEMA_A)
            config.base_url = ""
            config.save()
        order = self._order_in_a()
        self._run("--tenant", SCHEMA_A)
        self._assert_zero_action(order)

    def test_latched_auth_failure_is_zero_action(self):
        with tenant_context(self.tenant_a):
            config = self._config(SCHEMA_A)
            config.auth_failed_at = timezone.now()
            config.save()
        order = self._order_in_a()
        self._run("--tenant", SCHEMA_A)
        self._assert_zero_action(order)


class ChannelScopesTests(QueueCommandTestBase):
    """--dry-run 零出站零写入；--tenant 限定单租户。"""

    def test_dry_run_makes_zero_outbound_and_zero_writes(self):
        with tenant_context(self.tenant_a):
            self._config(SCHEMA_A)
        order = self._order(SCHEMA_A, f"{NO_A}71")
        out = self._run("--tenant", SCHEMA_A, "--dry-run")
        self.assertIn(f"{SCHEMA_A}: 候选 1", out)
        self.assertIn("dry-run", out)
        self.assertEqual(self._peer_calls, [])
        self.assertEqual(self._ledger_rows(SCHEMA_A, order.order_no), [])
        self.assertIsNone(self._row(SCHEMA_A, order.pk).qr_fetch_suppress_until)

    def test_tenant_flag_limits_the_loop_to_that_tenant(self):
        with tenant_context(self.tenant_a):
            self._config(SCHEMA_A)
        with tenant_context(self.tenant_b):
            self._config(SCHEMA_B)
        order_a = self._order(SCHEMA_A, f"{NO_A}72")
        order_b = self._order(SCHEMA_B, f"{NO_B}72")
        out = self._run("--tenant", SCHEMA_A)
        self.assertIn(SCHEMA_A, out)
        self.assertNotIn(f"{SCHEMA_B}:", out)
        self.assertEqual(len(self._peer_calls), 1)
        self.assertIn(f"order_no={order_a.order_no}", str(self._peer_calls[0].url))
        self.assertNotIn(f"order_no={order_b.order_no}", str(self._peer_calls[0].url))
        # B 的单原封不动（未进本轮循环 ⇒ 无退避、无台账）。
        self.assertIsNone(self._row(SCHEMA_B, order_b.pk).qr_fetch_suppress_until)
        self.assertEqual(self._ledger_rows(SCHEMA_B, order_b.order_no), [])


class TimerChannelLedgerTests(QueueCommandTestBase):
    """timer 通道（`record_failures=False`）：not_found / 对端 5xx 不落台账；
    ok 与 401 闩沿照落（计划 §三 T6 第 8 条）。"""

    def setUp(self):
        super().setUp()
        with tenant_context(self.tenant_a):
            self._config(SCHEMA_A)
        self.order = self._order(SCHEMA_A, f"{NO_A}81")

    def test_not_found_writes_no_ledger_row(self):
        self._run("--tenant", SCHEMA_A)
        self.assertEqual(len(self._peer_calls), 1)
        self.assertEqual(self._ledger_rows(SCHEMA_A, self.order.order_no), [])

    def test_peer_5xx_writes_no_ledger_row(self):
        self._peer_raw(httpx.Response(HTTPStatus.INTERNAL_SERVER_ERROR, json={}))
        self._run("--tenant", SCHEMA_A)
        self.assertEqual(self._ledger_rows(SCHEMA_A, self.order.order_no), [])
        # 普通失败也不写持久退避（每分钟一轮扫描本身就是节流）。
        self.assertIsNone(self._row(SCHEMA_A, self.order.pk).qr_fetch_suppress_until)

    def test_ok_still_writes_the_ledger_row(self):
        self._peer_ok()
        with mock.patch.object(order_qr_fetch, "_download_image", return_value=JPEG):
            self._run("--tenant", SCHEMA_A)
        rows = self._ledger_rows(SCHEMA_A, self.order.order_no)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].reason, "自动拉取了本单群二维码")

    def test_401_still_latches_and_writes_exactly_one_ledger_row(self):
        self._peer_raw(httpx.Response(HTTPStatus.UNAUTHORIZED, json={}))
        self._run("--tenant", SCHEMA_A)
        rows = self._ledger_rows(SCHEMA_A, self.order.order_no)
        self.assertEqual(len(rows), 1)
        with tenant_context(self.tenant_a):
            config = OrderQrFetchConfig.objects.get(tenant_schema=SCHEMA_A)
        self.assertIsNotNone(config.auth_failed_at)


class EndToEndLandingTests(QueueCommandTestBase):
    """端到端：对端回 ok ⇒ 群码落库（字节真进存储）+ 台账一行 + 退出码 0。"""

    def test_ok_lands_the_group_qr_bytes_with_one_ledger_row_and_exit_zero(self):
        with tenant_context(self.tenant_a):
            self._config(SCHEMA_A)
        order = self._order(SCHEMA_A, f"{NO_A}91")
        self._peer_ok()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage_override = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        storage_override.enable()
        self.addCleanup(storage_override.disable)
        download = mock.patch.object(order_qr_fetch, "_download_image", return_value=JPEG)
        download.start()
        self.addCleanup(download.stop)
        out = self._run("--tenant", SCHEMA_A)  # 不抛 = 退出码 0
        self.assertIn(f"{SCHEMA_A}: 候选 1 / 消费 1 / ok 1", out)
        screenshots = self._screenshots(SCHEMA_A, order.order_no)
        self.assertEqual(len(screenshots), 1)
        row = screenshots[0]
        self.assertEqual(row.status, "ready")
        stored = get_storage().head(row.object_key)
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], len(JPEG))
        self.assertEqual(row.size_bytes, len(JPEG))
        self.assertEqual(row.content_type, "image/jpeg")
        ledger = self._ledger_rows(SCHEMA_A, order.order_no)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0].action, "order.malailai_qr_fetch")
        # 成功落库的单下一轮不再候选（查到即出队）。
        out2 = self._run("--tenant", SCHEMA_A, "--dry-run")
        self.assertIn(f"{SCHEMA_A}: 候选 0", out2)

    def test_landing_failure_is_land_failed_not_ok_and_warns(self):
        """H1（2026-11-01）：对端回 ok 但**落库失败**（下载炸）⇒ 摘要**不**计 ok、
        failures 明细 `<单号>: land_failed`、失败走 `logger.warning`（本项目没配
        LOGGING，INFO 到不了 journald ⇒ 旧形态「每轮 ok N 全绿零信号」由此堵死）；
        不是 unexpected（退出码 0）、不写退避（下一轮自然重试）。"""
        with tenant_context(self.tenant_a):
            self._config(SCHEMA_A)
        order = self._order(SCHEMA_A, f"{NO_A}92")

        def broken_download(url: str) -> bytes:
            raise order_qr_fetch.QrFetchError("下载群码失败（HTTP 404）")

        self._peer_ok()
        download = mock.patch.object(
            order_qr_fetch, "_download_image", side_effect=broken_download
        )
        download.start()
        self.addCleanup(download.stop)
        with mock.patch.object(order_qr_fetch.logger, "warning") as warn_spy:
            out = self._run("--tenant", SCHEMA_A)  # 不抛 = 退出码 0（落库失败 ≠ unexpected）
        self.assertIn(f"{SCHEMA_A}: 候选 1 / 消费 1 / ok 0", out)
        self.assertIn(f"  - {order.order_no}: land_failed", out)
        self.assertGreaterEqual(
            warn_spy.call_count, 1, "落库失败必须至少一条 WARNING（INFO 到不了 journald）"
        )
        # 没落成截图行；timer 通道失败不进台账；不写退避（下一轮自然重试）。
        self.assertEqual(self._screenshots(SCHEMA_A, order.order_no), [])
        self.assertEqual(self._ledger_rows(SCHEMA_A, order.order_no), [])
        self.assertIsNone(self._row(SCHEMA_A, order.pk).qr_fetch_suppress_until)


class TieredIntervalTests(QueueCommandTestBase):
    """⭐ 2026-11-03 分层轮询间隔（CPU 底噪治理）：候选按**单龄**分档——<1h 每 1 分钟、
    1~6h 每 5 分钟、6~24h 每 15 分钟；判定 `qr_poll_due` 是纯函数（按 `created_at`
    对齐块），「老单优先」顺序与 24h 窗语义不变；本轮没到期的单进「分层等 N」计数、
    ⛔ 不出站（判据打在桩的请求数上）。"""

    def setUp(self):
        super().setUp()
        with tenant_context(self.tenant_a):
            self._config(SCHEMA_A)

    def _aged(self, order_no: str, *, hours: float) -> Order:
        # ⚠️ 单龄**刻意不对齐块首**（本类钉的就是分层判定）；hours 由用例自己算 mod。
        with tenant_context(self.tenant_a):
            game, _ = Game.objects.get_or_create(name="分层间隔测试游戏")
            order = Order.objects.create(
                game=game, order_no=order_no, status=OrderStatus.IN_PROGRESS
            )
            Order.objects.filter(pk=order.pk).update(
                created_at=timezone.now() - timedelta(hours=hours)
            )
        return order

    def test_fresh_order_is_polled_every_round(self):
        """<1h 的单：每轮必问（间隔 ≤ 触发周期）。"""
        self._aged(f"{NO_A}11", hours=0.2)
        self._run("--tenant", SCHEMA_A)
        self.assertEqual(len(self._peer_calls), 1)

    def test_mid_and_old_orders_wait_out_their_blocks(self):
        """1~6h 单在「单龄 mod 5 分钟」非块首 ⇒ 本轮分层等、零出站；6h+ 同理 15 分钟档。"""
        # 单龄 2h + 7 分钟：mod 5min = 7min > 1min 触发周期 ⇒ 不问（下一块才问）。
        order_mid = self._aged(f"{NO_A}12", hours=2 + 7 / 60)
        self._run("--tenant", SCHEMA_A)
        self.assertEqual(self._peer_calls, [], "中期单非块首 ⇒ 本轮分层等、零出站")
        with tenant_context(self.tenant_a):
            row = Order.objects.get(pk=order_mid.pk)
        self.assertFalse(qr_poll_due(row, timezone.now()))
        # 老单档：单龄 9h+3min ⇒ mod 15min = 3min ≥ 1min 触发周期 ⇒ 等（零出站）。
        order_old = self._aged(f"{NO_A}13", hours=9 + 3 / 60)
        self._run("--tenant", SCHEMA_A)
        with tenant_context(self.tenant_a):
            row_old = Order.objects.get(pk=order_old.pk)
        self.assertFalse(qr_poll_due(row_old, timezone.now()))

    def test_block_head_orders_are_due_and_summary_counts_tier_wait(self):
        """块首（age mod 间隔 < 触发周期）⇒ 到期；摘要行报「分层等 N」。"""
        # 单龄 2h 整（mod 5min = 0 < 60s）⇒ 到期。
        self._aged(f"{NO_A}14", hours=2)
        # 单龄 2h+7min ⇒ 等。
        self._aged(f"{NO_A}15", hours=2 + 7 / 60)
        out = self._run("--tenant", SCHEMA_A)
        self.assertEqual(len(self._peer_calls), 1, "块首那一张照问；另一张等")
        self.assertIn("分层等 1", out)

    def test_poll_due_is_pure_and_restart_safe(self):
        """纯函数钉：同一 (单龄) 判定恒同——进程重启不漂（timer 每分钟新起进程的形态）。"""
        now = timezone.now()
        with tenant_context(self.tenant_a):
            game, _ = Game.objects.get_or_create(name="分层间隔测试游戏")
            order = Order.objects.create(
                game=game, order_no=f"{NO_A}16", status=OrderStatus.IN_PROGRESS
            )
            Order.objects.filter(pk=order.pk).update(
                created_at=now - timedelta(hours=2)
            )
            row = Order.objects.get(pk=order.pk)
        a = qr_poll_due(row, now)
        b = qr_poll_due(row, now)
        self.assertEqual(a, b)


class NoLedgerFamilyRowsTests(QueueCommandTestBase):
    """⭐ 2026-11-03 台账简化：查码任何轮次都 ⛔ 不在鸡蛋铺台账留行
    （旧 `qr.fetch_started` / `qr.fetch_gaveup` 两 kind 停写；查到行的判据在
    `tests.test_audit_write_side_ledger.QrLedgerSimplifiedTests`）。"""

    def _jdp_rows(self, schema: str, order_no: str):
        with tenant_context(self._tenant(schema)):
            return list(
                JidanpuSyncEvent.objects.filter(target_ref=order_no).order_by("id")
            )

    def test_rounds_leave_no_jdp_ledger_rows_even_when_aged_out(self):
        """not_found 轮 + 老出窗（旧「放弃扫描」的输入形态）⇒ 零台账行、幂等。"""
        with tenant_context(self.tenant_a):
            self._config(SCHEMA_A)
        order = self._order(SCHEMA_A, f"{NO_A}81")
        out = self._run("--tenant", SCHEMA_A)
        self.assertIn(f"{SCHEMA_A}: 候选 1 / 消费 1 / ok 0", out)
        self.assertEqual(self._jdp_rows(SCHEMA_A, order.order_no), [])
        # 老出窗再跑一轮（旧实现会在这里补「放弃」行）⇒ 仍零行。
        with tenant_context(self.tenant_a):
            Order.objects.filter(pk=order.pk).update(
                created_at=timezone.now() - timedelta(hours=25)
            )
        out2 = self._run("--tenant", SCHEMA_A)
        self.assertEqual(self._jdp_rows(SCHEMA_A, order.order_no), [])
        self.assertNotIn("放弃", out2)


class NotFoundLoggingTests(QueueCommandTestBase):
    """⭐ 案 C（D-0364）：对端 404 折成 `not_found` 静默档后，队列通道的 journald 行
    打 **detail**（「对端系统里没有这张单（host）」，按这句话可辨可数）；200 态的
    `not_found` / `generating` 没有 detail ⇒ 回落 kind（旧行为不丢形）。

    ⚠️ 台账**两通道都零行**：404 不是失败（web `_dispatch` 在 `_record_ledger` 之前
    return；队列另有 `record_failures` 闸——本文件走的就是队列通道）。

    ⚠️ **变异自证（必须能红）**：把 `_dispatch` 的日志从 `result.detail or result.kind`
    改回 `result.kind` ⇒ 第一条的「对端系统里没有这张单」断言红。
    """

    def setUp(self):
        super().setUp()
        with tenant_context(self.tenant_a):
            self.config = self._config(SCHEMA_A)
        self.order = self._order(SCHEMA_A, f"{NO_A}21")

    def test_the_404_not_found_line_carries_the_detail(self):
        self._peer_raw(httpx.Response(404))
        with self.assertLogs("apps.business.order_qr_fetch", level="INFO") as captured:
            self._run("--tenant", SCHEMA_A)
        joined = "\n".join(captured.output)
        self.assertIn("对端系统里没有这张单", joined)
        self.assertIn("malailai.test", joined)
        # 静默档：零台账行。
        self.assertEqual(self._ledger_rows(SCHEMA_A, self.order.order_no), [])

    def test_a_detail_less_not_found_still_names_the_kind(self):
        # 默认桩 = 200 + `group_not_found`（detail 为空）⇒ 行回落 kind，仍可辨。
        with self.assertLogs("apps.business.order_qr_fetch", level="INFO") as captured:
            self._run("--tenant", SCHEMA_A)
        joined = "\n".join(captured.output)
        self.assertIn("not_found", joined)
        self.assertNotIn("对端系统里没有这张单", joined)
