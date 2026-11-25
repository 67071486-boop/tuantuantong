"""入口闸：上家绑定「**走哪条路就跑哪几段**」（spec §2.1 / §10；2026-10-08 T8 收口裁定）。

> 现场（实测缺陷，T8 报告 §6）：`jidanpu_sync` 的逐绑定闸原本用**①式**的凭据判据
> （`jidanpu.credentials_of`：地址 + 账号 + 密码）⇒ ②式绑定必然回 `None` ⇒ **整行跳过** ⇒
> ⓪ 回调消费（T6）与 ⓪' 兜底对账（T8）**一段都不跑**，而且**静默**（页面无错、stdout 无字）。
> ⚠️ 而 **T11 界面新建出来的②式绑定 100% 是这个形态**：②式下账号那一格是隐藏的、
> 后端也不再要求它（`validate_account` 的 docstring 明写「②式不要求」）⇒
> **不修 = 官方API 这条路在生产上等于没接上。**

## 这份文件守什么

1. ⭐ **端到端（核心判据）**：造一个**从界面上真建出来**的②式绑定（`account=""` / `password=""`）
   + 一条该绑定的回调事件 + 一张「远端变了、我方是旧的」在途单 ⇒ 跑一轮 `jidanpu_sync`
   ⇒ **打数据库行**断言 **T6 与 T8 都真的跑了**（`payload.trigger` 分别是 `callback` 与 `reconcile`）。
2. **反向对照**：②式但凭据不全（`client_id` 空）⇒ **不许去建连**（⛔ 不许把「还没配好」
   报成「同步坏了」；台账零行、`last_error` 空）。
3. **①式回归**：①式的闸与行为**一个字不许变**（凭据齐 ⇒ 三条①式段照旧跑；凭据不齐 ⇒ 整行跳过；
   ⛔ 不许用 `client_id` 真值去反推这条行走哪条路）。

## 三条纪律（照 T6/T8 那两份）

- ⛔ **零真实网络**：假上游复用 T2/T6/T8 那一份（`tests.test_jidanpu_openapi._Upstream`）。
- ⛔ **夹具一律合成值**（E-0077），⭐ 而且**②式的夹具按界面的真实形态造**
  （`account=""` / `password=""`）——上一轮 T8 的夹具给了假密码，**那正是这个缺陷躲过全部用例的原因**。
- ⛔ **断言打在数据库行上**（E-0009）：订单状态、台账行、绑定列。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_openapi, jidanpu_upstream
from apps.business.jidanpu_upstream import HallOutcome, MyOrdersOutcome, OutboxStats
from apps.business.models import (
    Game,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
)
from apps.callback import signature
from apps.callback import wsgi as callback_wsgi
#: ⭐ 复用 T2/T6/T8 那一条线的假上游（⛔ 不另写一个：两份桩必然漂）。
from tests.p5_auth_helpers import make_logged_in_client
from tests.test_jidanpu_openapi import BASE_URL, CLIENT_ID, ORDER_STATUS_PATH, _Upstream

# ── 合成夹具值（⛔ 一个真值都没有）──────────────────────────────────────────────

CODE_UI = "Ui1_-entry_gate_test_1"  # 22 位（照接收器的定长正则）
CODE_PASSWORD = "Pw2_-entry_gate_test_2"
for _code in (CODE_UI, CODE_PASSWORD):
    assert len(_code) == 22, _code

SECRET_UI = "tt-entry-gate-secret-0001"
SECRET_PASSWORD = "tt-entry-gate-secret-0002"

REMOTE_ID = "20261008000000000801"
REMOTE_ID_B = "20261008000000000802"
ORDER_NO = "20261008000000000701"
ORDER_NO_B = "20261008000000000702"

NOW = datetime(2026, 10, 8, 14, 0, 0, tzinfo=dt_timezone.utc)
AUTHORITATIVE = "已暂停"


@contextmanager
def _noop_context(tenant):
    yield


class EntryGateCase(TenantTestCase):
    """一行**界面形态**的绑定 + 假上游 + 真事件文件（⛔ 不预置任何事件）。"""

    def setUp(self):
        super().setUp()
        jidanpu_openapi.reset_token_cache()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        from apps.business import jidanpu_openapi_consume, jidanpu_openapi_reconcile

        jidanpu_openapi_consume.reset_process_state()
        jidanpu_openapi_reconcile.reset_process_state()
        self.addCleanup(jidanpu_openapi_consume.reset_process_state)
        self.addCleanup(jidanpu_openapi_reconcile.reset_process_state)

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events = self.tmp / "shared" / "callback" / "events.jsonl"
        self.keys_file = self.tmp / "callback-keys.env"
        self.keys_file.write_text(
            jidanpu_openapi.CALLBACK_KEYS_FILE_HEADER + "\n"
            + f"{CODE_UI}\t{SECRET_UI}\n"
            + f"{CODE_PASSWORD}\t{SECRET_PASSWORD}\n",
            encoding="utf-8",
        )
        self.keys_file.chmod(0o600)
        env = mock.patch.dict(
            os.environ,
            {"CALLBACK_EVENTS_PATH": str(self.events), "CALLBACK_KEYS_FILE": str(self.keys_file)},
        )
        env.start()
        self.addCleanup(env.stop)

        self.upstream = _Upstream()
        http = mock.patch.object(jidanpu_openapi.httpx, "Client", self.upstream.factory)
        http.start()
        self.addCleanup(http.stop)
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 1, "msg": None,
            "data": {
                "status": AUTHORITATIVE, "creatTime": "2026-10-08 12:00:00", "operTime": None,
                "doneTime": None, "verifyTime": None,
            },
        })

        self.game = Game.objects.create(name="测试游戏甲")
        #: ⛔ ①式的三段（outbox / 大厅 / 我的订单）**一次都不该被调**（那要①式的登录凭据）
        #: ⇒ 桩一调就抛（比断言调用数更硬：漏调一处也看得见）。
        self.boom_calls: list[str] = []
        for name in ("run_outbox", "sync_hall", "sync_my_orders"):
            patcher = mock.patch.object(jidanpu_upstream, name, side_effect=self._boom(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        #: 登录式建连：②式下**不许进**（进了就是「拿①式的路去跑②式的行」）。
        connector = mock.patch.object(
            jidanpu_upstream.jidanpu, "open_client_with_http", side_effect=self._boom("建连")
        )
        connector.start()
        self.addCleanup(connector.stop)

    def _boom(self, name):
        def _call(*args, **kwargs):
            self.boom_calls.append(name)
            raise AssertionError(f"①式那一段（{name}）在②式绑定上被调用了")
        return _call

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def _user(self, name):
        """一个**持有 `order.change_status`** 的账号（写状态要这个节点，⛔ 别用裸 `create_user`：
        没有节点时 `apply_transition` 会以「缺少所需权限节点」拒绝，那会把判据红在**节点**上
        而不是闸上——本文件测的是闸）。"""
        username = f"entry-gate-{name}"
        make_logged_in_client(
            username=username, groups=("客服",), nodes=("order.change_status",),
        )
        return User.objects.get(username=username)

    def make_openapi_binding(self, *, name="ui", enabled=True, client_id=None,
                             account="", password="", secret=SECRET_UI, code=CODE_UI):
        """⭐ **界面形态**的②式绑定：`account` / `password` **都留空**（T11 的真实形态）。"""
        user = self._user(name)
        return JidanpuUpstreamBinding.objects.create(
            user=user, jidanpu_base_url=BASE_URL, account=account, password=password,
            mode=JidanpuUpstreamBinding.Mode.OPENAPI,
            client_id=CLIENT_ID if client_id is None else client_id,
            client_secret=secret, callback_code=code, enabled=enabled,
        )

    def make_password_binding(self, *, name="pw", password="pw-canary-synth", enabled=True):
        """①式的绑定（凭据齐 = 地址 / 账号 / 密码）。"""
        user = self._user(name)
        return JidanpuUpstreamBinding.objects.create(
            user=user, jidanpu_base_url=BASE_URL, account=f"acc-entry-gate-{name}",
            password=password, mode=JidanpuUpstreamBinding.Mode.PASSWORD, enabled=enabled,
        )

    def make_order(self, binding, *, order_no=ORDER_NO, remote_id=REMOTE_ID):
        order = Order.objects.create(
            game=self.game, order_no=order_no, status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("100.00"),
        )
        JidanpuUpstreamOrderLink.objects.create(
            binding=binding, order=order, upstream_order_id=remote_id,
        )
        return order

    def post_callback(self, *, order_no, code=CODE_UI, secret=SECRET_UI) -> str:
        """让**真接收器**落一条通过验签的 `action=10` 回调（⛔ 不是手搓 dict）。"""
        params = {
            "orderNo": order_no, "sourceOrderNo": ORDER_NO, "status": "问题单",
            "note": "", "action": "10", "timestamp": "1791500000",
        }
        body = dict(params)
        body[signature.SIGN_FIELD_NAME] = signature.compute_sign(params, secret)
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": f"{callback_wsgi.CALLBACK_PATH_PREFIX}{code}",
            "QUERY_STRING": "", "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)), "wsgi.input": BytesIO(raw),
            "REMOTE_ADDR": "127.0.0.1",
        }
        status: dict = {}
        callback_wsgi.application(environ, lambda line, headers: status.update(line=line))
        return status["line"]

    # ── 跑一轮 ──────────────────────────────────────────────────────────────

    def _run(self, *, now=NOW, tenants=None):
        from apps.business.management.commands import jidanpu_sync as module

        for name in (
            "_UPSTREAM_OPENAPI_RECONCILE_AT", "_UPSTREAM_HALL_AT",
            "_UPSTREAM_MY_ORDERS_AT",
        ):
            getattr(module, name).clear()
        out, err = io.StringIO(), io.StringIO()
        tenant = mock.Mock(schema_name="tenant-entry-gate")
        patcher = mock.patch.object(module.Tenant, "objects")
        objects = patcher.start()
        objects.exclude.return_value.order_by.return_value = (
            [tenant] if tenants is None else tenants
        )
        try:
            with mock.patch.object(module, "tenant_context", _noop_context), \
                 mock.patch.object(module.timezone, "now", return_value=now):
                call_command("jidanpu_sync", "--once", stdout=out, stderr=err)
        finally:
            patcher.stop()
        return out.getvalue(), err.getvalue()

    # ── 读数（⛔ 一律重新查库）──────────────────────────────────────────────

    def order_status(self, order) -> str:
        return Order.objects.get(pk=order.pk).status

    def notes(self, **filters):
        return JidanpuSyncEvent.objects.filter(**filters)

    def refresh(self, binding):
        return JidanpuUpstreamBinding.objects.get(pk=binding.pk)

    def api_paths(self) -> set[str]:
        return {row["path"] for row in self.upstream.api_calls()}


# ══ ① ⭐ 核心判据：界面形态的②式绑定 ⇒ T6 与 T8 **都真的跑了** ═══════════════════


class OpenApiUiShapedBindingTests(EntryGateCase):
    def test_a_ui_shaped_openapi_binding_really_runs_the_callback_and_reconcile_segments(self):
        """⭐ **这一条就是本次收口的核心**：`account` / `password` 都留空的②式绑定
        （= T11 界面建出来的真实形态）跑一轮 ⇒ **两条路都真的动了**。

        夹具（⛔ 刻意按界面的形态造）：
        - 绑定：`mode="openapi"`、`account=""`、`password=""`、`client_id` / `client_secret` 齐；
        - 订单 A：有**本绑定的真回调事件**（走真接收器）⇒ 该由 **T6 回调消费**改对；
        - 订单 B：**没有任何回调**（模拟「发版重启那段漏掉了」）⇒ 该由 **T8 兜底对账**改对。

        判据（⛔ 全部打在**数据库行**上）：
        ① 两张订单行都变成「已暂停」；
        ② 台账里**有**这两条结论，且两条各自的 `payload.trigger` 是 `callback` 与 `reconcile`
           ——⭐ 这一条就是「T6 跑了**且** T8 也跑了」的机械判据（⛔ 不看 stdout、⛔ 不看 logger）；
        ③ 只打了只读的 `GetOrderStatus`（⛔ 一次写接口都没打）；
        ④ ①式那三段（outbox / 大厅 / 我的订单 / 登录式建连）**一次都没被调**
           （夹具把它们换成「一调就抛」⇒ 跑了就不是断言红而是异常红）。

        ⛔ 弄红它（两个方向）：
        - 把闸改回①式的凭据判据（原来的缺陷）⇒ 整行跳过 ⇒ ①②两条都红
          （订单两张都不变、台账零行）；
        - 让②式照跑那三段 ⇒ ④那条以 `AssertionError` 的形式红。
        """
        binding = self.make_openapi_binding()
        with_callback = self.make_order(binding, order_no=ORDER_NO, remote_id=REMOTE_ID)
        without_callback = self.make_order(
            binding, order_no=ORDER_NO_B, remote_id=REMOTE_ID_B
        )
        self.assertEqual(
            self.post_callback(order_no=REMOTE_ID), "200 OK",
            "夹具坏了：真接收器没落盘",
        )

        out, err = self._run()

        # ① 两张单都真的被改对了
        self.assertEqual(self.order_status(with_callback), "PAUSED")
        self.assertEqual(self.order_status(without_callback), "PAUSED")
        # ② 两条路的结论都在台账里，且**各自的 trigger 打得出来**
        triggers = set(
            self.notes(kind="openapi.status_in", status="done")
            .values_list("payload__trigger", flat=True)
        )
        self.assertEqual(
            triggers, {"callback", "reconcile"},
            "T6（回调）与 T8（兜底对账）没有**都**真的跑起来",
        )
        by_order = {
            row.target_ref: row.payload.get("trigger")
            for row in self.notes(kind="openapi.status_in", status="done")
        }
        self.assertEqual(by_order[ORDER_NO], "callback", "有回调那一张不是回调驱动的")
        self.assertEqual(by_order[ORDER_NO_B], "reconcile", "没回调那一张没被兜底对账兜住")
        # ③ 只读端点
        self.assertEqual(self.api_paths(), {ORDER_STATUS_PATH})
        # ④ ①式那三段一次都没跑
        self.assertEqual(self.boom_calls, [], "②式绑定上跑了①式的段")
        self.assertNotIn("①式那一段", err)
        # 阳性对照：这一轮真的有话说（不是「什么都没跑所以没异常」）
        self.assertIn("对账", out)

    def test_the_round_records_last_run_and_ok_without_polluting_the_error_column(self):
        """②式的健康列：`last_run_at` / `last_ok_at` 不许永远为空，而**错误列不许被正常结论占住**。

        ⚠️ 为什么这两件事必须一起守（同一条用例）：
        ① 页面把 `last_run_at` / `last_ok_at` 摆在绑定列表上，运维判「这条线在不在跑」看的就是它；
           ②式跳过 outbox 之后没人写它们 ⇒ 不补就是**说假话**（每 3 秒都在跑，页面显示「从未运行」），
           而且一条恢复正常的绑定会**永远**挂着上一次的错误（`last_error` 只有 `_stamp_binding_ok` 会清）。
        ② 反过来，「写成功」与「一致 ⇒ 不写」都是**正常结论**（台账各有 done / skipped 行）
           ⇒ ⛔ 不许进 `last_error` —— 那会让**错误列**常年挂着「已回写为『已暂停』」（把成功说成错误）。

        ⛔ 弄红它（三条各自独立）：
        - 去掉②式那一支的 `_stamp_binding_run` / `_stamp_binding_ok` ⇒ 第 ①② 条断言红；
        - 让「写成功」也进 `findings` ⇒ 第一条断言里的 `last_error == ""` 红；
        - 让「一致 ⇒ 不写」也进 `findings` ⇒ **第二轮**那条 `last_error == ""` 红
          （第二轮全部已收敛 ⇒ 每张单的结论都是「一致 ⇒ 不写」）。
        """
        binding = self.make_openapi_binding()
        order = self.make_order(binding)

        self._run()  # 第一轮：远端变了 ⇒ 兜底对账把它改对（写成功）

        fresh = self.refresh(binding)
        self.assertEqual(fresh.last_run_at, NOW, "②式的 last_run_at 没人写")
        self.assertEqual(fresh.last_ok_at, NOW, "②式真的查过单了，却没盖成功")
        self.assertEqual(fresh.last_error, "", "写成功不该进错误列")
        self.assertEqual(self.order_status(order), "PAUSED")

        later = NOW.replace(minute=NOW.minute + 10)
        self._run(now=later)  # 第二轮：全都收敛了 ⇒ 每张单的结论都是「一致 ⇒ 不写」

        fresh = self.refresh(binding)
        self.assertEqual(fresh.last_ok_at, later, "这一轮真的查了单，却没盖成功")
        self.assertEqual(fresh.last_error, "", "「一致 ⇒ 不写」是正常结论，不该进错误列")


# ══ ② 反向对照：②式但凭据不全 ⇒ ⛔ 不许去建连 ═════════════════════════════════════


class OpenApiIncompleteCredentialsTests(EntryGateCase):
    def test_an_openapi_binding_without_client_id_is_skipped_without_any_noise(self):
        """②式但 `client_id` 空（凭据不全）⇒ **整行跳过**：零上游请求、订单不变、
        台账零行、`last_error` 空。

        ⚠️ 为什么要有「台账零行 + `last_error` 空」这两条：闸放错时最糟的形态不是「多打一次上游」，
        而是**把「还没配好」报成「同步坏了」**（每轮一条失败台账 + 页面一句错），
        用户看到的是「这个功能坏了」而不是「还差一格没填」。

        ⛔ 弄红它：把闸改成「只看 `mode`、不看凭据」⇒ 回调消费那一段会照跑，
        一试建连就抛 `ConfigError` ⇒ 台账里立刻多一条 `openapi.callback_query_failed`、
        `last_error` 也非空 ⇒ 本用例两条断言都红。

        ⚠️ 夹具里**有**本绑定的回调事件（否则「一段都没跑」是恒真的）——这是让判据能红的承重前提。
        """
        binding = self.make_openapi_binding(client_id="")
        order = self.make_order(binding)
        self.assertEqual(self.post_callback(order_no=REMOTE_ID), "200 OK")

        _out, err = self._run()

        self.assertEqual(self.upstream.api_calls(), [], "凭据不全却打了上游")
        self.assertEqual(self.order_status(order), "IN_PROGRESS")
        self.assertEqual(self.notes().count(), 0, "凭据不全却留了台账")
        self.assertEqual(self.refresh(binding).last_error, "", "把「还没配好」报成了「同步坏了」")
        self.assertEqual(self.boom_calls, [])
        self.assertNotIn("未预期失败", err)


# ══ ③ ①式回归：闸与行为一个字不许变 ═════════════════════════════════════════════


class PasswordModeRegressionTests(EntryGateCase):
    def test_a_password_binding_still_runs_the_three_login_segments(self):
        """①式（凭据齐）⇒ 三条①式段**照旧跑**，而②式那两段**不跑**（它们自己提前返回）。

        ⛔ 弄红它：把闸判反（①式也走②式的凭据判据 / ①式被跳过）⇒ 本用例红。
        """
        binding = self.make_password_binding()
        trace: list[str] = []

        def _segment(name, outcome):
            def _call(*args, **kwargs):
                trace.append(name)
                return outcome
            return _call

        with mock.patch.object(
            jidanpu_upstream, "run_outbox", side_effect=_segment("outbox", OutboxStats())
        ), mock.patch.object(
            jidanpu_upstream, "sync_hall", side_effect=_segment("hall", HallOutcome())
        ), mock.patch.object(
            jidanpu_upstream, "sync_my_orders",
            side_effect=_segment("orders", MyOrdersOutcome()),
        ), mock.patch.object(
            jidanpu_upstream.jidanpu, "open_client_with_http",
            side_effect=lambda _b: _stub_connection(),
        ):
            self._run()
            first_ok_at = self.refresh(binding).last_ok_at
            # ⭐ 第二趟在**对账闸内**（+1 分钟 < 600 秒）：①式那一行**一个字都不该多**
            #    ——⛔ 连「对账未到点」都不打（那一段在①式这一行上**不存在**，不是「没到点」）。
            #    ⚠️ 必须留在同一个 `with` 里：外面是 setUp 那三个「一调就抛」的桩。
            out2, _err2 = self._run(now=NOW.replace(minute=NOW.minute + 1))

        self.assertEqual(trace, ["outbox", "hall", "orders", "outbox", "hall", "orders"])
        self.assertEqual(self.api_paths(), set(), "①式绑定不该打官方API 的只读端点")
        # 阳性对照：①式那一轮真的走了健康列那一段（三段干净 ⇒ 盖 last_ok_at）
        self.assertEqual(first_ok_at, NOW)
        self.assertNotIn("对账", out2, "①式的汇总里混进了②式那一段")
        self.assertIn("大厅", out2)

    def test_a_password_binding_with_a_missing_password_is_still_skipped(self):
        """①式但**密码空** ⇒ 整行跳过（**行为一个字没变**）。

        ⚠️ 这一条是本次改动最容易踩坏的地方：新闸按 `mode` 分流，
        ①式那一支必须仍然是「地址 / 账号 / 密码」那一把判据。
        ⛔ 弄红它：把①式那一支也换成②式的判据（或干脆不筛）⇒ 三段会被调用 ⇒ 本用例红。
        """
        binding = self.make_password_binding(password="")
        trace: list[str] = []
        for name, outcome in (
            ("run_outbox", OutboxStats()), ("sync_hall", HallOutcome()),
            ("sync_my_orders", MyOrdersOutcome()),
        ):
            patcher = mock.patch.object(
                jidanpu_upstream, name,
                side_effect=lambda *a, _n=name, _o=outcome, **k: (trace.append(_n), _o)[1],
            )
            patcher.start()
            self.addCleanup(patcher.stop)

        self._run()

        self.assertEqual(trace, [], "①式凭据不全却跑了")
        self.assertEqual(self.boom_calls, [])

    def test_a_password_binding_ignores_the_openapi_columns_when_deciding(self):
        """⭐ 判「走哪条路」**只看 `mode`**（spec §2.1）：①式行即使 `client_id` /
        `client_secret` 都还留着（**切回①式之后它们不会被清**），也**必须照①式跑**。

        ⚠️ 夹具为什么要这么造（这是本条能红的关键）：只有让**两条判据给出不同的结论**，
        误用另一条判据才检得出来。这一行的形态是「①式凭据齐（地址/账号/密码都在）
        ＋ ②式的两格还留着」：
        - 正确的判据（看 `mode`）⇒ ①式 ⇒ 凭据齐 ⇒ **照跑三条①式段**；
        - 错用 `client_id` 真值反推（E-0021 家族）⇒ 被当成②式 ⇒
          `openapi_credentials_of` 里那道 `mode` 闸会回 `None` ⇒ **整行静默跳过**
          （用户看到的是「同步不动了」，而且没有任何错）。

        ⛔ 弄红它：把「走哪条路」改成看 `client_id` 非空 ⇒ `trace` 变空 ⇒ 本用例红。
        """
        binding = self.make_password_binding()
        JidanpuUpstreamBinding.objects.filter(pk=binding.pk).update(
            client_id=CLIENT_ID, client_secret="tt-entry-gate-secret-leftover",
        )
        trace: list[str] = []

        def _segment(name, outcome):
            def _call(*args, **kwargs):
                trace.append(name)
                return outcome
            return _call

        with mock.patch.object(
            jidanpu_upstream, "run_outbox", side_effect=_segment("outbox", OutboxStats())
        ), mock.patch.object(
            jidanpu_upstream, "sync_hall", side_effect=_segment("hall", HallOutcome())
        ), mock.patch.object(
            jidanpu_upstream, "sync_my_orders",
            side_effect=_segment("orders", MyOrdersOutcome()),
        ), mock.patch.object(
            jidanpu_upstream.jidanpu, "open_client_with_http",
            side_effect=lambda _b: _stub_connection(),
        ):
            _out, err = self._run()

        self.assertEqual(
            trace, ["outbox", "hall", "orders"],
            "①式的行被 `client_id` 反推成了②式 ⇒ 整行静默跳过",
        )
        self.assertEqual(self.api_paths(), set(), "①式行不该打官方API 的只读端点")
        self.assertNotIn("未预期失败", err)


@contextmanager
def _stub_connection():
    yield (mock.Mock(), mock.Mock())


__all__ = [
    "OpenApiIncompleteCredentialsTests",
    "OpenApiUiShapedBindingTests",
    "PasswordModeRegressionTests",
]
