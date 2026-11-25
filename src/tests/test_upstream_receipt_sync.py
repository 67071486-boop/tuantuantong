"""上家收款同步（D-0359）的判据网（计划 §5 的 1–13 条 + §10 换源轮变异钉）。

## 判据落点纪律

- 断言打在**数据库行**（`Order.payment_status` / `receipt_sync_notfound_count` /
  `OperationLog` 行）与**真出口**（桩客户端收到的线上 query），⛔ 不打中间层。
- ①式桩位（E-0029，换源轮 2026-11-01）：`open_client_with_http` 打在**被查找的**
  `apps.business.jidanpu` 模块上（动态查找发生在调用时）；桩产出的 client 是**真
  `JidanpuClient`**（httpx.MockTransport，零网络）⇒ 派生视图（`unwrap="status"`）、
  登录握手、裸数组解包**全真跑**，`state["calls"]` 收的是**线上 query**
  （`request.url.params`）⇒ 全参数 / 窗口断言打在真出口上。
  另有一条「不接桩应当失败」对照（`test_the_jidanpu_channel_without_stubs_fails_loudly`）：
  桩不在 ⇒ 真建连打不可达域 ⇒ TransportError ⇒ error，两者分得开。
- peer 桩位照旧：`PeerQueryClient` / `_sleep` / `_monotonic` / `_now` 都打在
  `upstream_receipt_sync` 名字空间；`_income_view_client` 同（`_client_for` 按模块
  全局查找它）。
- 夹具一律**合成值**（单号 / 域名 / 密码全部形仿真不真）；探针证据写进
  `upstream_receipt_sync` 模块 docstring，这里只用合成化后的形状。

## 端点回归（§5 第 11 条）的落点

busy 码 1037 / 31 天校验 / 401-403 权限回归在 `tests/test_xgj_receipt_sync.py`
（端点级用例已换桩到编排器，保持原样绿）；本文件只钉「线程体 = 编排器」的接线。

## 变异钉（先红后绿留证）

每枚变异都**从原始文本出发**（替换前断言锚点恰好 1 处）、跑被钉用例看到红、按反向替换
还原（自证变异标记已不在文件中）后看到绿（§G 追加①②③）：

- **M1 判据放宽**（`jidanpu_row_settled` 体换成 `return True` =「有行就写」）：
  `JidanpuChannelRunTests.test_an_unsettled_probe_row_is_never_written` 红
  （`AssertionError: 'SETTLED' != PaymentStatus.UNSETTLED`）；还原后绿（Ran 1 | OK）。
- **M2 行匹配不回校验**（`_match_row` 的循环体换成「返回第一行」）：
  `JidanpuChannelRunTests.test_a_row_with_a_different_orderid_is_not_a_hit` 红
  （别人的单（settled=Y 但 orderId≠key）被当成这一单：
  `AssertionError: 'SETTLED' != PaymentStatus.UNSETTLED : 别人的单翻不得`）；还原后绿。
- **MP3 全过滤参数缺失**（`_jidanpu_income_query` 删 `"dateType": "10",` 一行）：
  `JidanpuChannelRunTests.test_the_query_carries_every_required_filter_param` 红
  （键表恰好集合断言：`AssertionError: Items in the second set but not the first:` ——
  `dateType` 缺席）；还原后绿。
- **MP4 形状守卫放宽**（`_query_jidanpu_one` 的 `if not isinstance(rows, list):`
  换成 `if False:`）：
  `JidanpuChannelRunTests.test_a_non_list_answer_is_an_error_and_never_written` 红
  （信封 dict 被当「查无」：首断言 `AssertionError: 1 != 3`——尝试次数塌成 1、
  放弃计数被烧）；还原后绿。
- **S1 会话壳识别被关掉**（`if isinstance(rows, dict) and _is_session_shell(rows):`
  换成 `if False and …`）：
  `JidanpuChannelRunTests.test_a_session_expired_shell_triggers_relogin_instead_of_burning_errors`
  红（`AssertionError: 1 != 2`——GetMyEmp 烧满 3 发、重登没发生）；还原后绿。
- **S3 旧形状复刻**（键空分支换成「首条 link 一锤定音：键空即计数并挡掉后续 link」）：
  `CandidateCollectionTests.test_a_first_empty_link_does_not_eclipse_a_later_keyed_link` 红
  （`AssertionError: Lists differ: [] != ['SECOND-KEY']`——带键 link 被键空 link 挡掉、
  可查的单被误计 skipped_no_link）；还原后绿。
- **M3 去掉放弃计数 +1**（`_bump_notfound` 体改成 `pass`）：
  `GiveupCounterTests.test_peer_notfound_increments_the_counter` 红
  （`1 != 0`：查无没计数）；还原后绿。（peer 侧代码本轮零改动，证据沿承上一轮。）
- **M4 去掉夜间阈值过滤**（`_nightly_order_filters` 删
  `receipt_sync_notfound_count__lt` 一行）：
  `GiveupCounterTests.test_threshold_exhaustion_removes_the_order_from_nightly` 红
  （`KeyError: receipt_sync_notfound_count__lt`）；还原后绿。（同上，沿承上一轮。）
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import connection
from django.test import Client, SimpleTestCase, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import upstream_receipt_sync, xgj_receipt_sync
from apps.business.models import (
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderSource,
    OrderStatus,
    PaymentStatus,
    PeerOrderLink,
    PeerUpstreamBinding,
    ReceiptSyncConfig,
)
from apps.core.error_codes import ErrorCode
from jidanpu.client import JidanpuClient
from jidanpu.config import JidanpuConfig
from tests.p5_auth_helpers import make_logged_in_client

DOMAIN = "tenant.test.com"
ENDPOINT_URL = "/api/business/orders/xgj-receipt-sync/"
#: 窗口日（显式给，⛔ 不依赖跑测试那天）。
DAY = date(2026, 11, 1)

#: 合成键值（形仿真不真：19 位对端单号 / 内部号 / 展示号）。
PEER_NO = "3200000000000000001"          # 19 位（对端来源单号域）
INTERNAL_ORDER_ID = "990123456789"       # ①式平台内部号（GetMyEmp 的 key / 行 orderId 域）
VIEW_ORDER_ID = "2200000000000000042"    # ①式展示号（orderIdView 域；换源后已不可查）

#: 换源轮探针的**合成化**样本行（真实值一律只在 `.tools/recon/`；这里只仿形状）：
#: `settingSta` 是**代码值**（Y 已结账 / N 未结账），`settingTime` = 上游结算时间。
SETTLED_ROW = {
    "orderId": INTERNAL_ORDER_ID,
    "settingSta": "Y",
    "settingTime": "10-31 14:04",
    "orderSta": "已完成",
    "netReceipts": "88.00",
    "gameName": "上家收款同步游戏",
}
UNSETTLED_ROW = {
    "orderId": INTERNAL_ORDER_ID,
    "settingSta": "N",
    "settingTime": "",
    "orderSta": "已完成",
    "netReceipts": "88.00",
    "gameName": "上家收款同步游戏",
}

#: GetMyEmp 的**全过滤参数**键表（缺任一 ⇒ 对端静默 0 行——全参数断言的权威副本）。
EXPECTED_INCOME_PARAM_KEYS = {
    "gameName", "stime", "etime", "dateType", "settingSta",
    "orderSta", "key", "key2", "page", "limit",
}

#: stime / etime 的线上格式（上游墙钟 Asia/Shanghai）。
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class _Base(TenantTestCase):
    """①式绑定（配来源）+ peer 绑定（https、密码已配、预检过）的完整夹具。"""

    def setUp(self):
        self.game = Game.objects.create(name="上家收款同步游戏")
        self.user = User.objects.create_user(username="receipt-owner", password="pw-1")
        #: ①式绑定配的来源（兜底路径只数「来源恰好等于它」的单）。
        self.source = OrderSource.objects.create(name="上家收款同步来源")
        #: 默认单挂的来源（**不是**绑定的来源 ⇒ 默认不进①式兜底候选，测试按需 opt-in）。
        self.other_source = OrderSource.objects.create(name="别的来源")
        self.jbinding = JidanpuUpstreamBinding.objects.create(
            user=self.user,
            jidanpu_base_url="https://jdp-upstream.test.invalid",
            account="jdp-account-1",
            password="jdp-password-1",
            enabled=True,
            mode=JidanpuUpstreamBinding.Mode.PASSWORD,
            order_source=self.source,
        )
        self.pbinding = PeerUpstreamBinding.objects.create(
            peer_name="对端工作室甲",
            peer_base_url="https://peer-upstream.test.invalid",
            peer_account="peer-account-1",
            token_in_hash="a" * 64,
            token_out="b" * 64,
            local_base_url=f"https://{DOMAIN}",
            bound_by=self.user,
            receipt_query_password="peer-query-password-1",
            receipt_query_password_set=True,
            receipt_query_precheck_ok=True,
        )
        self._seq = 0
        # ⛔ 节流不许真睡（限速另有专项用例）。
        sleep_patcher = mock.patch.object(upstream_receipt_sync, "_sleep")
        self.sleep_mock = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    # ── 夹具工具 ──────────────────────────────────────────────────────
    def _order(
        self,
        *,
        status=OrderStatus.COMPLETED,
        payment=PaymentStatus.UNSETTLED,
        completed_at=None,
        created_at=None,
        source_order_no=VIEW_ORDER_ID,
        source=None,
    ):
        self._seq += 1
        order = Order.objects.create(
            game=self.game,
            order_no=f"20261101{self._seq:012d}",
            status=status,
            payment_status=payment,
            order_source=source or self.other_source,
            source_order_no=source_order_no,
        )
        Order.objects.filter(pk=order.pk).update(
            completed_at=completed_at or timezone.now() - timedelta(days=2),
            created_at=created_at or timezone.now() - timedelta(days=3),
        )
        order.refresh_from_db()
        return order

    def _link(self, order, *, upstream_order_id=None, binding=None):
        # 默认 id 随序号唯一：(binding, upstream_order_id) 有唯一约束，撞了是夹具错。
        self._seq += 1
        return JidanpuUpstreamOrderLink.objects.create(
            binding=binding or self.jbinding,
            order=order,
            upstream_order_id=upstream_order_id or f"UP{self._seq:010d}",
        )

    def _peer_link(self, order, *, peer_order_no=None, origin_order_no=None, binding=None):
        self._seq += 1
        return PeerOrderLink.objects.create(
            order=order,
            upstream_binding=binding or self.pbinding,
            peer_order_no=peer_order_no or f"32{self._seq:017d}",
            origin_order_no=origin_order_no or f"32{self._seq:017d}",
        )

    def _run_nightly(self):
        return upstream_receipt_sync.run_upstream_receipt_sync(nightly=True)

    def _run_manual(self):
        return upstream_receipt_sync.run_upstream_receipt_sync(
            date_from=DAY, date_to=DAY,
        )

    def _jdp_stub(self, rows_by_key=None, *, payload=None, payload_by_key=None, on_call=None,
                  session_shells=0, session_shell_data=None):
        """①式通道桩：`open_client_with_http` 换成产出**真 JidanpuClient**
        （httpx.MockTransport，零网络）。

        - E-0029：桩打在**被查找的名字**上（`jidanpu.open_client_with_http` 是调用时
          动态查找）⇒ 通道拿到的 client 走**全部真代码**：派生视图（`unwrap="status"`）、
          登录握手（`/Login/Login` code=1 + sl-session cookie）、裸数组解包。
        - `state["calls"]` 收 GetMyEmp 的**线上 query**（`request.url.params`，字符串）
          ⇒ 全参数 / 窗口断言打在真出口上；`state["logins"]` 收登录次数
          （S1 会话壳用例的「真的重登了」判据）。
        - `payload` 非 None ⇒ GetMyEmp 一律回它（形状守卫用：dict 带 data list ⇒ 解包；dict 无 data list ⇒ error）；
          `payload_by_key` 按键给坏形状（一单失败不连坐用）；
          `on_call(params)` 在每次 GetMyEmp 落地时被调（扫中翻库的竞态用例用）；
          `session_shells` = 前 N 发 GetMyEmp 回 2xx「请先登录」JSON 壳
          （S1 会话过期第二形态用例；msg 照 vendored 词表）。
        """
        state: dict = {
            "calls": [],
            "logins": 0,
            "rows_by_key": rows_by_key or {},
            "payload": payload,
            "payload_by_key": payload_by_key or {},
            "session_shells": session_shells,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/Login/Login":
                state["logins"] += 1
                return httpx.Response(
                    200,
                    json={"code": 1, "msg": "", "data": ""},
                    headers=[("set-cookie", "sl-session=stub-session; Path=/")],
                )
            if request.url.path == upstream_receipt_sync.JIDANPU_MY_EMP_PATH:
                params = dict(request.url.params)
                state["calls"].append(params)
                if on_call is not None:
                    on_call(params)
                if state["session_shells"] > 0:
                    state["session_shells"] -= 1
                    return httpx.Response(
                        200, json={"code": 1001, "msg": "请先登录", "data": session_shell_data},
                    )
                key = params.get("key") or ""
                if key in state["payload_by_key"]:
                    return httpx.Response(200, json=state["payload_by_key"][key])
                if state["payload"] is not None:
                    return httpx.Response(200, json=state["payload"])
                return httpx.Response(
                    200, json=list(state["rows_by_key"].get(key, ()))
                )
            return httpx.Response(500, json={"code": 500, "msg": "stub 不认识这个端点"})

        http = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://jdp-upstream.test.invalid",
            follow_redirects=False,
        )
        self.addCleanup(http.close)
        client = JidanpuClient(
            JidanpuConfig(
                base_url="https://jdp-upstream.test.invalid",
                account="jdp-account-1",
                password="jdp-password-1",
            ),
            http_client=http,
        )
        # 建连处（`_open_client_pair`）同款：派生视图要从 client 拿共享 http 层。
        client._tt_http = http

        @contextmanager
        def fake_open(binding):
            yield (client, http)

        patcher = mock.patch.object(
            upstream_receipt_sync.jidanpu, "open_client_with_http", fake_open
        )
        return patcher, state

    def _query_keys(self, state):
        """线上调用序（GetMyEmp 的 key 参数列）。"""
        return [params.get("key") for params in state["calls"]]

    def _logs(self, order_no):
        return OperationLog.objects.filter(order_no=order_no)


# ═══════════ §5-1 候选网 ════════════════════════════════════════════════


class CandidateCollectionTests(_Base):
    """候选口径（①式 link 主路径 / 兜底计数跳过 / OPENAPI 排除 / 停用绑定 / 状态门）。"""

    def test_link_path_collects_completed_unsettled_orders(self):
        """①式主路径：link × 启用①式绑定 × COMPLETED+UNSETTLED ⇒ 收得上、按其键查询。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(self._query_keys(state), [INTERNAL_ORDER_ID])
        self.assertEqual(summary["jidanpu"]["scanned"], 1)

    def test_a_fallback_source_order_is_counted_as_skipped_no_link(self):
        """⭐ 换源轮：兜底路径单（无 link、来源单号域）键域已证伪 ⇒ `skipped_no_link`
        计数跳过——零上游调用、不烧放弃数、不翻状态（夜间摘要能解释这些单为何没被查）。"""
        order = self._order(source_order_no=VIEW_ORDER_ID, source=self.source)
        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(state["calls"], [], "键域已证伪 ⇒ 兜底单不得再被查询")
        self.assertEqual(summary["jidanpu"]["skipped_no_link"], 1)
        self.assertEqual(summary["jidanpu"]["scanned"], 0)
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(order.receipt_sync_notfound_count, 0, "查不了 ≠ 查无")

    def test_a_link_without_upstream_order_id_is_counted_skipped_no_link(self):
        """link 行存在但 `upstream_order_id` 空 ⇒ 换源后查不了（key2 已证伪）
        ⇒ `skipped_no_link` 计数跳过（旧形状退 source_order_no 兜底，已删）。"""
        order = self._order()
        # ⛔ 不走 `_link` 助手（它把空串 `or` 成默认键）——直接造「键空」的脏数据行。
        JidanpuUpstreamOrderLink.objects.create(
            binding=self.jbinding, order=order, upstream_order_id="",
        )
        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(state["calls"], [])
        self.assertEqual(summary["jidanpu"]["skipped_no_link"], 1)
        self.assertEqual(summary["jidanpu"]["scanned"], 0)
        order.refresh_from_db()
        self.assertEqual(order.receipt_sync_notfound_count, 0)

    def test_a_first_empty_link_does_not_eclipse_a_later_keyed_link(self):
        """⭐ S3（评审 2026-11-01）：同单两条 link——第一条键空、第二条带键 ⇒ 挑
        **带键**的照查、零 `skipped_no_link` 计数；⛔ 旧形状（首条 link 一锤定音）
        会把这张可查的单误计成键空并整单弃查（变异钉 S3 的承重用例）。"""
        second = JidanpuUpstreamBinding.objects.create(
            user=self.user,
            jidanpu_base_url="https://jdp-upstream-2.test.invalid",
            account="jdp-account-2",
            password="jdp-password-2",
            enabled=True,
            mode=JidanpuUpstreamBinding.Mode.PASSWORD,
            order_source=self.other_source,
        )
        order = self._order()
        # ⛔ 不走 `_link` 助手造空键（它会把空串 `or` 成默认键）——直接造「键空」行。
        JidanpuUpstreamOrderLink.objects.create(
            binding=self.jbinding, order=order, upstream_order_id="",
        )
        self._link(order, binding=second, upstream_order_id="SECOND-KEY")
        patcher, state = self._jdp_stub({"SECOND-KEY": [UNSETTLED_ROW]})
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(
            self._query_keys(state), ["SECOND-KEY"],
            "键空 link 不得挡掉同单后面带键的 link",
        )
        self.assertEqual(summary["jidanpu"]["skipped_no_link"], 0)
        self.assertEqual(summary["jidanpu"]["scanned"], 1)

    def test_fallback_needs_a_unique_source_and_a_non_null_binding_source(self):
        """兜底唯一性（S4）：两个启用绑定配同一来源 ⇒ 不兜底（连计数都不进——它们
        本来就不是兜底候选）；绑定来源为 NULL 的不算数；有 link 的单照走主路径。"""
        second = JidanpuUpstreamBinding.objects.create(
            user=self.user,
            jidanpu_base_url="https://jdp-upstream-2.test.invalid",
            account="jdp-account-2",
            password="jdp-password-2",
            enabled=True,
            mode=JidanpuUpstreamBinding.Mode.PASSWORD,
            order_source=self.source,
        )
        order = self._order(source=self.source)
        self._link(order, binding=second, upstream_order_id="SECOND-BIND-KEY")
        self._order(source=self.source)  # 想走兜底 ⇒ 来源不唯一 ⇒ 连 skipped_no_link 都不算
        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(self._query_keys(state), ["SECOND-BIND-KEY"])
        self.assertEqual(summary["jidanpu"]["skipped_no_link"], 0)
        self.assertEqual(summary["jidanpu"]["scanned"], 1)

    def test_openapi_mode_is_excluded_with_a_summary_count(self):
        """②式结构性排除（拍板①）：不收、不算错误、摘要给排除计数。"""
        JidanpuUpstreamBinding.objects.create(
            user=self.user,
            jidanpu_base_url="https://jdp-openapi.test.invalid",
            account="jdp-account-oa",
            password="jdp-password-oa",
            enabled=True,
            mode=JidanpuUpstreamBinding.Mode.OPENAPI,
        )
        order = self._order(source=self.source)
        self._link(order, binding=self.jbinding, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        # ②式绑定没有 link ⇒ 它名下本来无候选；排除计数必须报 1。
        self.assertEqual(summary["jidanpu"]["excluded_openapi_bindings"], 1)
        self.assertEqual(self._query_keys(state), [INTERNAL_ORDER_ID])

    def test_a_disabled_password_binding_still_syncs_receipts(self):
        """⭐ 拍板修订（2026-11-01）：停用的①式绑定（凭据完整）**仍参与收款同步**。

        `enabled` 开关只管订单拉取（`jidanpu_upstream` 侧零改动）；收款同步是只读动作
        （GetMyEmp 只查结算状态，不拉单 / 不建卡 / 不认领）⇒ 停用绑定名下候选照常
        入队、照常问结算状态（真单号：`link.upstream_order_id`）。
        ⭐⭐ 变异钉（先红后绿，§5-G 追加①②③）：把 `_jidanpu_credential_bindings` 的
        候选查询改回 `filter(enabled=True)` ⇒ 本用例红（停用绑定的候选重新消失：
        `calls==[]`、`scanned=0`；同态下「停用且凭据不全」用例也红——绑定根本到不了
        凭据闸、`excluded_no_credentials` 停在 0）。2026-11-01 实测：变异态
        `FAILED (failures=2)` = 本用例 + 凭据闸用例；还原后全绿。
        """
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        self.jbinding.enabled = False
        self.jbinding.save(update_fields=["enabled"])

        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(
            self._query_keys(state), [INTERNAL_ORDER_ID], "停用①式绑定的候选照常入队"
        )
        self.assertEqual(summary["jidanpu"]["scanned"], 1)

    def test_a_disabled_password_binding_without_credentials_is_still_excluded(self):
        """两闸之二：停用**且凭据不全**（密码空）⇒ 仍排除 + 计数。

        「enabled 不筛」只拆掉 enabled 那一道；mode 与凭据两闸原样（⛔ 不许连
        「凭据完整」一起放宽——拿空凭据去登录只会烧一次失败）。
        """
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        self.jbinding.enabled = False
        self.jbinding.password = ""
        self.jbinding.save(update_fields=["enabled", "password"])

        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(state["calls"], [], "凭据不全的停用绑定不得入队")
        self.assertEqual(summary["jidanpu"]["scanned"], 0)
        self.assertEqual(summary["jidanpu"]["excluded_no_credentials"], 1)

    def test_disabled_peer_binding_candidates_are_not_collected(self):
        """peer 通道候选条件**不动**：停用的 peer 绑定 ⇒ 候选整批不进队。

        同一次拍板只改了①式——「enabled 只管订单拉取」的修订**不含 peer**
        （peer 绑定有自己的 enabled / 凭据 / 预检闸，原样）。
        ⚠️ ①式绑定保持启用：它的候选照常入队，与 peer 的排除互不干扰。
        """
        self._peer_link(self._order())
        self.pbinding.enabled = False
        self.pbinding.save(update_fields=["enabled"])
        # ①式的对照候选（启用绑定名下）：与 peer 的排除互不干扰。
        self._link(self._order(), upstream_order_id=INTERNAL_ORDER_ID)

        patcher, state = self._jdp_stub()
        with patcher:
            with mock.patch.object(
                upstream_receipt_sync, "PeerQueryClient"
            ) as peer_factory:
                summary = self._run_nightly()

        peer_factory.assert_not_called()
        self.assertEqual(summary["peer"]["scanned"], 0)
        # ①式不受影响：启用绑定的候选照常入队、照常问。
        self.assertEqual(len(state["calls"]), 1)
        self.assertEqual(summary["jidanpu"]["scanned"], 1)

    def test_statuses_beyond_completed_unsettled_are_not_collected(self):
        """状态门：结算中不扫（拍板③）、已结款不扫、未完成不扫。"""
        self._link(self._order(payment=PaymentStatus.SETTLING))
        self._link(self._order(payment=PaymentStatus.SETTLED))
        self._link(self._order(status=OrderStatus.IN_PROGRESS))
        pending = self._order()
        self._link(pending, upstream_order_id=INTERNAL_ORDER_ID)
        self._peer_link(pending)  # peer 通道同一张 pending 单（两通道各自收各自的）

        patcher, state = self._jdp_stub()
        with patcher:
            with mock.patch.object(
                upstream_receipt_sync, "PeerQueryClient"
            ) as peer_factory:
                summary = self._run_nightly()

        self.assertEqual(self._query_keys(state), [INTERNAL_ORDER_ID])
        peer_factory.assert_called_once()
        self.assertEqual(summary["jidanpu"]["scanned"], 1)
        self.assertEqual(summary["peer"]["scanned"], 1)

    def test_peer_without_query_password_is_not_collected(self):
        """peer 候选闸：查询密码未配 ⇒ 整批不收 + 摘要计数（http 形态同判）。"""
        self._peer_link(self._order())
        PeerUpstreamBinding.objects.filter(pk=self.pbinding.pk).update(
            receipt_query_password_set=False
        )
        with mock.patch.object(upstream_receipt_sync, "PeerQueryClient") as peer_factory:
            summary = self._run_nightly()

        peer_factory.assert_not_called()
        self.assertEqual(summary["peer"]["excluded_no_password"], 1)

    def test_peer_http_binding_is_structurally_excluded(self):
        """http 形态的 peer 绑定结构性排除 + 摘要计数（Secure cookie 在 http 上不回传）。"""
        self._peer_link(self._order())
        PeerUpstreamBinding.objects.filter(pk=self.pbinding.pk).update(
            peer_base_url="http://peer-upstream.test.invalid"
        )
        with mock.patch.object(upstream_receipt_sync, "PeerQueryClient") as peer_factory:
            summary = self._run_nightly()

        peer_factory.assert_not_called()
        self.assertEqual(summary["peer"]["excluded_http_bindings"], 1)

    def test_a_binding_with_a_malformed_base_url_is_excluded_and_counted(self):
        """⭐ S3（评审 2026-11-01）：空串 / 裸域名（非 https 形态）的 peer 绑定在候选闸
        **排除并计数**——空串 `startswith("http://")` 为 False、原形状会把它放行，
        到了 httpx 才响 `InvalidURL`（⛔ 不在 `httpx.HTTPError` 族，通道边界接不住）
        ⇒ 在候选闸就挡住，别让它走异常路径。"""
        self._peer_link(self._order())
        PeerUpstreamBinding.objects.filter(pk=self.pbinding.pk).update(
            peer_base_url=""
        )
        with mock.patch.object(upstream_receipt_sync, "PeerQueryClient") as peer_factory:
            summary = self._run_nightly()

        peer_factory.assert_not_called()
        self.assertEqual(summary["peer"]["excluded_bad_base_url"], 1)

    def test_an_order_with_multiple_enabled_links_is_enqueued_once(self):
        """⭐ S2（评审 2026-11-01）：同单挂多个启用绑定的 link（罕见）⇒ 只入队一次
        ——不设防就是同一单被问两遍、留痕两行。"""
        second = JidanpuUpstreamBinding.objects.create(
            user=self.user,
            jidanpu_base_url="https://jdp-upstream-2.test.invalid",
            account="jdp-account-2",
            password="jdp-password-2",
            enabled=True,
            mode=JidanpuUpstreamBinding.Mode.PASSWORD,
            order_source=self.source,
        )
        order = self._order()
        self._link(order, binding=self.jbinding, upstream_order_id="DUP-A")
        self._link(order, binding=second, upstream_order_id="DUP-B")
        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(len(state["calls"]), 1, f"同单只该被问一遍：{state['calls']}")
        self.assertEqual(summary["jidanpu"]["scanned"], 1)


class NightlyWindowTests(_Base):
    """§5-2 夜间 90 天窗（窗外不收、窗内对照非空、手动不受窗限）。"""

    def test_outside_the_window_is_not_collected_with_an_inside_control(self):
        """窗外（91 天前完成）不收；同轮窗内对照（2 天前完成）收得上（E-0004 两路证据）。"""
        outside = self._order(completed_at=timezone.now() - timedelta(days=91))
        inside = self._order(completed_at=timezone.now() - timedelta(days=2))
        self._link(outside)
        self._link(inside, upstream_order_id="IN-WINDOW-ID")

        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(self._query_keys(state), ["IN-WINDOW-ID"], "窗外不该被查")
        self.assertEqual(summary["jidanpu"]["scanned"], 1)

    def test_the_manual_run_ignores_both_the_window_and_the_giveup_count(self):
        """手动跑 = 显式动作优先：完成时间在 90 天外、放弃计数烧穿、录单日在窗口里 ⇒ 照查。"""
        order = self._order(completed_at=timezone.now() - timedelta(days=91))
        # 手动口径按**录单日**窗（DAY）——把录单时间落进窗口（半开区间内的上午 9 点）。
        Order.objects.filter(pk=order.pk).update(
            created_at=timezone.make_aware(datetime(2026, 11, 1, 9, 0)),
            receipt_sync_notfound_count=99,
        )
        self._link(order)

        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_manual()

        self.assertEqual(len(state["calls"]), 1, "手动跑无视窗与放弃计数 ⇒ 照查")
        self.assertEqual(summary["jidanpu"]["scanned"], 1)
        self.assertEqual(summary["jidanpu"]["excluded_openapi_bindings"], 0)


# ═══════════ §5-3 放弃计数 ══════════════════════════════════════════════


class GiveupCounterTests(_Base):
    """对端查无的收敛（+1 / 阈值 / 查到不动 / 手动无视 / 未过预检不计数）。"""

    def _stub_peer_empty(self):
        """peer 客户端桩：`state["rows"]` 是 fetch 的回值（可先改后跑）；`state["clients"]`
        收构造出的客户端（工厂断言面：一个都不该建 = 候选没收）。"""
        state: dict = {"rows": [], "clients": []}

        def factory(**kwargs):
            client = mock.Mock()
            client.__enter__ = mock.Mock(return_value=client)
            client.__exit__ = mock.Mock(return_value=False)
            client.login = mock.Mock()
            client.fetch_orders = mock.Mock(return_value=state["rows"])
            client.close = mock.Mock()
            state["clients"].append(client)
            return client

        patcher = mock.patch.object(
            upstream_receipt_sync, "PeerQueryClient", side_effect=factory
        )
        return patcher, state

    def test_peer_notfound_increments_the_counter(self):
        """过预检的绑定对端查无 ⇒ `receipt_sync_notfound_count` +1（变异 M3 钉 +1 这一行）。"""
        order = self._order()
        self._peer_link(order, peer_order_no=PEER_NO)
        patcher, state = self._stub_peer_empty()
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.receipt_sync_notfound_count, 1)
        self.assertEqual(summary["peer"]["not_found"], 1)
        self.assertEqual(state["clients"][0].fetch_orders.call_args.kwargs, {"order_no": order.peer_links.first().peer_order_no})

    def test_threshold_exhaustion_removes_the_order_from_nightly(self):
        """计数 ≥ 3 ⇒ 夜间候选不再收（零上游调用）；变异 M4 钉阈值过滤这一行。"""
        order = self._order()
        self._peer_link(order)
        Order.objects.filter(pk=order.pk).update(receipt_sync_notfound_count=3)
        patcher, state = self._stub_peer_empty()
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(len(state["clients"]), 0, "烧穿的单不得再被查")
        self.assertEqual(summary["peer"]["scanned"], 0)

    def test_finding_a_row_does_not_move_the_counter(self):
        """查到行（未结）⇒ 计数一字不动（只有「查无」这一档收敛）。"""
        order = self._order()
        self._peer_link(order)
        patcher, state = self._stub_peer_empty()
        state["rows"] = [{"payment_status": PaymentStatus.UNSETTLED}]
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.receipt_sync_notfound_count, 0)
        self.assertEqual(summary["peer"]["not_settled"], 1)

    def test_unprechecked_binding_notfound_logs_but_does_not_count(self):
        """未过预检（M3）：查无只留痕、不烧放弃数（对端静默闸下「看不见」≠「没有」）。"""
        order = self._order()
        self._peer_link(order)
        PeerUpstreamBinding.objects.filter(pk=self.pbinding.pk).update(
            receipt_query_precheck_ok=False
        )
        patcher, state = self._stub_peer_empty()
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.receipt_sync_notfound_count, 0)
        self.assertEqual(summary["peer"]["not_found_uncounted"], 1)
        # 留痕照写：结局 not_found、detail 注明不计。
        log = self._logs(order.order_no).get(action=upstream_receipt_sync.QUERY_ACTION)
        self.assertIn("结局=not_found", log.reason)
        self.assertIn("不计数", log.reason)


# ═══════════ §5-4/§5-5 ①式判据与通道行为（换源轮：GetMyEmp / settingSta）═════════


class JidanpuJudgementTests(SimpleTestCase):
    """①式「已结账」判据（换源轮实测定案：`settingSta` 代码值 == "Y"；合成化探针样本）。"""

    def test_the_y_code_value_is_settled(self):
        """代码值 Y ⇒ 已结（strip / upper 归一容上游空白与小写形态）。"""
        self.assertTrue(upstream_receipt_sync.jidanpu_row_settled(SETTLED_ROW))
        self.assertTrue(
            upstream_receipt_sync.jidanpu_row_settled(
                {**SETTLED_ROW, "settingSta": " y "}
            )
        )

    def test_every_failing_side_of_the_predicate_is_not_settled(self):
        """N / 空 / 缺列 / 中文标签 / 其它串 / 非字典 ⇒ 未结（方向安全，不误翻状态）。"""
        for row in (
            {**SETTLED_ROW, "settingSta": "N"},
            {**SETTLED_ROW, "settingSta": ""},
            {**SETTLED_ROW, "settingSta": None},
            {**SETTLED_ROW, "settingSta": "已结账"},   # 前端词表的中文标签不是代码值
            {**SETTLED_ROW, "settingSta": "YES"},      # 非 "Y" 的串不收
            {**SETTLED_ROW, "settingSta": 5},          # 非字符串形态更不是
            {"orderId": INTERNAL_ORDER_ID},            # 缺列（旧判据的「互证缺一」形态已不存在）
            "不是字典",
            None,
        ):
            with self.subTest(row=row):
                self.assertFalse(upstream_receipt_sync.jidanpu_row_settled(row))


class JidanpuChannelRunTests(_Base):
    """①式通道逐单行为（判据接线 / 逐字回校验 / 全参数 / 窗口 / 形状守卫 / 单向闩 / 限速）。"""

    def test_a_settled_probe_row_flips_status_and_writes_two_logs(self):
        """判已结 ⇒ 翻 SETTLED + 两行日志（查询行 + change_payment_status 行）。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub({INTERNAL_ORDER_ID: [SETTLED_ROW]})
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(summary["jidanpu"]["settled"], 1)
        actions = list(
            self._logs(order.order_no).values_list("action", flat=True)
        )
        self.assertIn(upstream_receipt_sync.QUERY_ACTION, actions)
        self.assertIn("order.change_payment_status", actions)
        # 机器行 detail 带通道 / 绑定 / 结局 / 上游原始字段 JSON（换源轮新四件）。
        query_log = self._logs(order.order_no).get(action=upstream_receipt_sync.QUERY_ACTION)
        self.assertIsNone(query_log.actor_id, "机器行没有操作人")
        self.assertEqual(query_log.actor, "系统")
        self.assertIn("通道=jidanpu", query_log.reason)
        self.assertIn("结局=settled", query_log.reason)
        self.assertIn("settingSta", query_log.reason)
        self.assertIn("settingTime", query_log.reason)  # 上游结算时间进留痕（留档增强）

    def test_receipt_returning_after_rebinding_does_not_settle_old_order(self):
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        editor = Client(SERVER_NAME=DOMAIN)
        login = editor.post(
            "/api/auth/login", {"username": self.user.username, "password": "pw-1"},
            content_type="application/json",
        )
        self.assertEqual(login.json()["code"], 0, login.content)

        def switch_store(_params):
            response = editor.put(
                "/api/business/member/upstream-jidanpu-binding/",
                {"jidanpu_base_url": "https://new-receipt-store.test.invalid",
                 "account": "synthetic-new-receipt-account", "enabled": False},
                content_type="application/json",
            )
            self.assertEqual(response.json()["code"], 0, response.content)

        patcher, state = self._jdp_stub({INTERNAL_ORDER_ID: [SETTLED_ROW]}, on_call=switch_store)
        with patcher:
            summary = self._run_nightly()
            self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(summary["jidanpu"]["stale_skip"], 1)
        self.assertEqual(self._query_keys(state), [INTERNAL_ORDER_ID])
        self.assertFalse(self._logs(order.order_no).filter(action="order.change_payment_status").exists())

    def test_old_receipt_cannot_settle_a_new_completion_of_the_same_order(self):
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        next_completion = order.completed_at + timedelta(minutes=1)

        def complete_again(_params):
            Order.objects.filter(pk=order.pk).update(completed_at=next_completion)

        patcher, _ = self._jdp_stub({INTERNAL_ORDER_ID: [SETTLED_ROW]}, on_call=complete_again)
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.completed_at, next_completion)
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(summary["jidanpu"]["stale_skip"], 1)

    def test_the_query_carries_every_required_filter_param(self):
        """全参数断言（MP3 承重用例）：GetMyEmp 缺任一过滤参数 ⇒ 对端**静默 0 行**
        （2026-11-01 真机探针）⇒ 线上 query 的键表必须恰好这十个、值逐个钉死。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub()
        with patcher:
            self._run_nightly()

        self.assertEqual(len(state["calls"]), 1)
        params = state["calls"][0]
        self.assertEqual(
            set(params), EXPECTED_INCOME_PARAM_KEYS,
            f"过滤参数键表必须恰好十个（缺一 = 静默 0 行）：{sorted(params)}",
        )
        self.assertEqual(params["gameName"], "全部游戏")
        self.assertEqual(params["dateType"], "10")
        self.assertEqual(params["settingSta"], "")
        self.assertEqual(params["orderSta"], "")
        self.assertEqual(params["key"], INTERNAL_ORDER_ID)
        self.assertEqual(params["key2"], "")
        self.assertEqual(params["page"], "1")
        self.assertEqual(params["limit"], "50")

    def test_the_window_covers_the_order_creation_time(self):
        """窗口断言：`stime = created_at − 7d`、`etime = now + 1d`（服务端先按窗过滤
        再配 key ⇒ 窗必须罩住录单时间，否则命中单也静默 0 行）。"""
        created = timezone.make_aware(datetime(2026, 9, 1, 9, 0))
        now = timezone.make_aware(datetime(2026, 11, 1, 12, 0))
        order = self._order(created_at=created)
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub()
        with patcher, mock.patch.object(
            upstream_receipt_sync, "_now", return_value=now
        ):
            self._run_nightly()

        self.assertEqual(len(state["calls"]), 1)
        params = state["calls"][0]
        self.assertEqual(
            params["stime"],
            timezone.localtime(created - timedelta(days=7)).strftime(STAMP_FORMAT),
        )
        self.assertEqual(
            params["etime"],
            timezone.localtime(now + timedelta(days=1)).strftime(STAMP_FORMAT),
        )
        # 语义面（同格式字符串可直接比）：录单时间落在 [stime, etime]。
        self.assertLessEqual(params["stime"], "2026-09-01 09:00:00")
        self.assertGreaterEqual(params["etime"], "2026-09-01 09:00:00")

    def test_the_income_view_client_shares_the_session_and_unwraps_bare_arrays(self):
        """⭐ 换源轮接线形状：到达取数口的是 `_income_view_client` 的派生视图——
        仍是真 `JidanpuClient`、`unwrap="status"`、与建连产物**共享同一 auth**（不重登）；
        裸数组经它原样到达判据（⛔ 默认 auto 解包会把裸数组判「缺业务码」= 每单 error）。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub({INTERNAL_ORDER_ID: [SETTLED_ROW]})
        seen: list = []
        real_derive = upstream_receipt_sync._income_view_client

        def spy(client):
            view = real_derive(client)
            seen.append((client, view))
            return view

        with patcher, mock.patch.object(
            upstream_receipt_sync, "_income_view_client", side_effect=spy
        ):
            summary = self._run_nightly()

        self.assertTrue(seen, "派生视图至少被建一次")
        source, view = seen[0]
        self.assertIsInstance(view, JidanpuClient)
        self.assertEqual(view.config.unwrap, "status")
        self.assertIs(view.auth, source.auth, "共享同一会话（已登录不重登）")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED, "裸数组真的到了判据")
        self.assertEqual(summary["jidanpu"]["settled"], 1)

    def test_an_unsettled_probe_row_is_never_written(self):
        """判未结（settingSta=N）⇒ 零写（状态不变、无 change_payment_status 行）
        ——变异 M1「有行就写」的承重用例。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub({INTERNAL_ORDER_ID: [UNSETTLED_ROW]})
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(summary["jidanpu"]["not_settled"], 1)
        self.assertFalse(
            self._logs(order.order_no)
            .filter(action="order.change_payment_status")
            .exists()
        )
        # 查询行照写（留痕「问了、上家说没结」）。
        self.assertTrue(
            self._logs(order.order_no)
            .filter(action=upstream_receipt_sync.QUERY_ACTION)
            .exists()
        )

    def test_an_empty_settingsta_row_is_never_written(self):
        """行形状「空」（settingSta 缺省/空串）⇒ 未结、零写（N/空/未知都归这一档）。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        row = {**SETTLED_ROW, "settingSta": ""}
        patcher, state = self._jdp_stub({INTERNAL_ORDER_ID: [row]})
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(summary["jidanpu"]["not_settled"], 1)

    def test_a_row_with_a_different_orderid_is_not_a_hit(self):
        """逐字回校验（变异 M2 承重用例）：行 settled=Y 但 `orderId` ≠ 查询键 ⇒
        不命中 ⇒ not_found + 烧放弃数（⛔ 不拿「有行」当命中——服务端 key 的模糊面
        未实测穷尽，命中集里可能有别人的单）。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        row = {**SETTLED_ROW, "orderId": "别人的内部号"}
        patcher, state = self._jdp_stub({INTERNAL_ORDER_ID: [row]})
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED, "别人的单翻不得")
        self.assertEqual(summary["jidanpu"]["not_found"], 1)
        self.assertEqual(summary["jidanpu"]["settled"], 0)
        self.assertEqual(order.receipt_sync_notfound_count, 1)
        self.assertIn(
            "结局=not_found",
            self._logs(order.order_no).get(action=upstream_receipt_sync.QUERY_ACTION).reason,
        )

    def test_an_envelope_answer_is_unwrapped_and_matched(self):
        """⭐ 2026-11-01 生产实锤（换源轮盲区）：GetMyEmp 全参数形态回的是**标准信封**
        {code, data:[…]}（不是裸数组——裸数组只在部分参数组合下出现）⇒ 守卫解包
        `data` 当行列表、逐字命中 ⇒ settled。原 MP4 用例把信封当「上游改版」判 error，
        恰与生产真实形状相反，本轮反转。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        envelope = {
            "code": 0, "msg": "", "count": 1,
            "data": [{"orderId": INTERNAL_ORDER_ID, "settingSta": "Y"}],
        }
        patcher, state = self._jdp_stub(payload=envelope)
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(len(state["calls"]), 1, "信封形态一发命中，不重试")
        self.assertEqual(summary["jidanpu"]["settled"], 1)
        self.assertEqual(summary["jidanpu"]["error"], 0)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)

    def test_a_dict_without_data_list_is_still_an_error(self):
        """信封形状守卫的负半边：dict 但 `data` 不是 list（上游改版/形状漂移）⇒
        结局 error、⛔ 不当空数组（那会把「形状不对」说成「查无」并烧放弃数）、
        总尝试 ≤3。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        malformed = {"code": 0, "msg": "", "data": "not-a-list"}
        patcher, state = self._jdp_stub(payload=malformed)
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(len(state["calls"]), upstream_receipt_sync.NETWORK_MAX_ATTEMPTS)
        self.assertEqual(summary["jidanpu"]["error"], 1)
        self.assertEqual(summary["jidanpu"]["not_found"], 0)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(order.receipt_sync_notfound_count, 0, "error 不是查无、不烧放弃数")
        self.assertIn(
            "结局=error",
            self._logs(order.order_no).get(action=upstream_receipt_sync.QUERY_ACTION).reason,
        )

    def test_a_session_expired_shell_triggers_relogin_instead_of_burning_errors(self):
        """⭐ S1（评审 2026-11-01）：会话过期的第二形态（2xx JSON 壳「请先登录」）在
        `unwrap="status"` 下会当数据漏进形状守卫 ⇒ 守卫认出（提示词照 vendored
        transport 的既有常量，⛔ 不自编）、清共享会话 ⇒ 下一发尝试走既有重登路径
        重问（⛔ 不是 error×3 烧完；第一发登录是初建会话、第二发才是重登）。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub(
            {INTERNAL_ORDER_ID: [SETTLED_ROW]}, session_shells=1,
        )
        with patcher:
            summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(summary["jidanpu"]["settled"], 1)
        self.assertEqual(summary["jidanpu"]["error"], 0)
        # 恰好重登一次（登录共 2 发：初建会话 + 守卫清会话后的重登）。
        self.assertEqual(state["logins"], 2)
        # GetMyEmp 恰好两发（第一发吃壳、第二发拿到行）⇒ ⛔ 不得烧满 3 次尝试。
        self.assertEqual(len(state["calls"]), 2)

    def test_empty_list_in_login_failure_is_not_a_missing_order(self):
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub(
            {INTERNAL_ORDER_ID: [SETTLED_ROW]}, session_shells=1, session_shell_data=[],
        )

        with patcher:
            summary = self._run_nightly()["jidanpu"]

        order.refresh_from_db()
        self.assertEqual(state["logins"], 2)
        self.assertEqual(len(state["calls"]), 2)
        self.assertEqual(summary["not_found"], 0)
        self.assertEqual(order.receipt_sync_notfound_count, 0)
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)

    def test_failure_with_a_valid_order_row_does_not_settle_it(self):
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        patcher, state = self._jdp_stub(payload={
            "code": 1, "msg": "没有权限", "data": [SETTLED_ROW],
        })

        with patcher:
            summary = self._run_nightly()["jidanpu"]

        order.refresh_from_db()
        self.assertEqual(len(state["calls"]), upstream_receipt_sync.NETWORK_MAX_ATTEMPTS)
        self.assertEqual(summary["error"], 1)
        self.assertEqual(summary["not_found"], 0)
        self.assertEqual(order.receipt_sync_notfound_count, 0)
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)

    def test_failed_success_marker_rejects_both_matched_and_empty_results(self):
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        for rows in ([SETTLED_ROW], []):
            with self.subTest(has_row=bool(rows)):
                patcher, _ = self._jdp_stub(payload={"code": 0, "success": False, "data": rows})
                with patcher:
                    summary = self._run_nightly()["jidanpu"]
                order.refresh_from_db()
                self.assertEqual((summary["error"], summary["settled"], summary["not_found"]), (1, 0, 0))
                self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
                self.assertEqual(order.receipt_sync_notfound_count, 0)

    def test_an_error_on_one_order_does_not_stop_the_next(self):
        """E-0087 / §5-6：第一单回包形状漂移连烧 3 次尝试 ⇒ error 零写；第二单照跑。"""
        good = self._order()
        self._link(good, upstream_order_id="GOOD-ID")
        bad = self._order()
        self._link(bad, upstream_order_id="BAD-ID")

        patcher, state = self._jdp_stub(
            rows_by_key={"GOOD-ID": []},
            payload_by_key={"BAD-ID": {"code": 1000, "msg": "业务失败壳"}},
        )
        with patcher:
            summary = self._run_nightly()

        bad_calls = [c for c in state["calls"] if c["key"] == "BAD-ID"]
        self.assertEqual(len(bad_calls), upstream_receipt_sync.NETWORK_MAX_ATTEMPTS)
        self.assertEqual(summary["jidanpu"]["error"], 1)
        self.assertEqual(summary["jidanpu"]["scanned"], 2, "一张失败不连坐")
        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertEqual(good.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(bad.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(bad.receipt_sync_notfound_count, 0, "error 不是查无、不烧放弃数")
        self.assertEqual(good.receipt_sync_notfound_count, 1, "好单照走查无路径")

    def test_notfound_burns_the_counter_with_exactly_one_query(self):
        """换源轮：GetMyEmp 0 行 ⇒ not_found、烧放弃数、**恰好一发查询**——旧
        「已退单二次问法」已退役（GetMyEmp 无此问法，not_found 即 not_found）。"""
        order = self._order()
        self._link(order, upstream_order_id="MISSING-ID")
        patcher, state = self._jdp_stub()
        with patcher:
            summary = self._run_nightly()

        self.assertEqual(len(state["calls"]), 1, "⛔ 不得再补第二查（已退役）")
        self.assertEqual(summary["jidanpu"]["not_found"], 1)
        self.assertNotIn("refunded", summary["jidanpu"], "refunded 结局已随换源退役")
        order.refresh_from_db()
        self.assertEqual(order.receipt_sync_notfound_count, 1)
        self.assertIn(
            "结局=not_found",
            self._logs(order.order_no).get(action=upstream_receipt_sync.QUERY_ACTION).reason,
        )

    def test_a_mid_scan_status_flip_is_skipped_without_an_upstream_call(self):
        """写前复核（单向闩）：扫描中另一单被人工翻成 SETTLING ⇒ 轮到它时跳过、不查、不回退。"""
        settled_row = {
            "orderId": "FIRST-KEY", "settingSta": "Y",
            "settingTime": "10-31 14:04", "orderSta": "已完成",
        }
        first = self._order()
        self._link(first, upstream_order_id="FIRST-KEY")
        flipped = self._order()
        self._link(flipped, upstream_order_id="SECOND-KEY")

        def flip_second_during_first(params):
            if params["key"] == "FIRST-KEY":
                Order.objects.filter(pk=flipped.pk).update(
                    payment_status=PaymentStatus.SETTLING
                )

        patcher, state = self._jdp_stub({"FIRST-KEY": [settled_row]}, on_call=flip_second_during_first)
        with patcher:
            summary = self._run_nightly()

        # 只有第一张真的被查；第二张 fresh 重读发现已翻 ⇒ 跳过。
        self.assertEqual(self._query_keys(state), ["FIRST-KEY"])
        self.assertEqual(summary["jidanpu"]["stale_skip"], 1)
        self.assertEqual(summary["jidanpu"]["scanned"], 2)
        flipped.refresh_from_db()
        self.assertEqual(flipped.payment_status, PaymentStatus.SETTLING, "不回退人工标记")
        self.assertEqual(
            flipped.receipt_sync_notfound_count, 0, "跳过的单零上游 ⇒ 不烧放弃数"
        )

    def test_a_channel_crash_does_not_stop_the_other_channel(self):
        """⭐ M1-3 逐通道边界（评审 2026-11-01）：①式通道未预期异常（不是 `JidanpuError`
        ⇒ 收窄接不住）⇒ 编排器响应亮记录、该通道记档（`unexpected_error`）、
        **peer 照跑**——查码线「毒单中断整租户」同族教训（审计 M6）。"""
        peer_order = self._order()
        self._peer_link(peer_order)
        jdp_order = self._order()
        self._link(jdp_order, upstream_order_id=INTERNAL_ORDER_ID)  # ①式候选 ⇒ 通道会真的跑

        patcher, state = self._jdp_stub()
        with patcher, mock.patch.object(
            upstream_receipt_sync, "_income_view_client",
            side_effect=RuntimeError("未预期的形状变更"),
        ), _stub_peer_class():
            with mock.patch.object(_StubPeerClient, "login"), mock.patch.object(
                _StubPeerClient, "fetch_orders",
                return_value=[{"payment_status": PaymentStatus.UNSETTLED}],
            ):
                summary = self._run_nightly()

        self.assertTrue(summary["jidanpu"]["unexpected_error"])
        self.assertFalse(summary["peer"]["unexpected_error"])
        self.assertEqual(summary["peer"]["settled"], 0)
        self.assertEqual(summary["peer"]["not_settled"], 1, "另一通道照常跑了")
        peer_order.refresh_from_db()
        self.assertEqual(peer_order.payment_status, PaymentStatus.UNSETTLED)

    def test_candidates_are_globally_ordered_oldest_first_across_bindings(self):
        """⭐ S1（评审 2026-11-01）：「老单优先」是**跨绑定全局**的——⛔ 不是每个绑定
        各查各的：更老的那张（另一绑定的 link）必须排在较新单前面。"""
        second = JidanpuUpstreamBinding.objects.create(
            user=self.user,
            jidanpu_base_url="https://jdp-upstream-2.test.invalid",
            account="jdp-account-2",
            password="jdp-password-2",
            enabled=True,
            mode=JidanpuUpstreamBinding.Mode.PASSWORD,
            order_source=self.other_source,
        )
        newer = self._order(completed_at=timezone.now() - timedelta(days=5))
        self._link(newer, upstream_order_id="NEWER-VIA-A")
        older = self._order(completed_at=timezone.now() - timedelta(days=40))
        self._link(older, binding=second, upstream_order_id="OLDER-VIA-B")

        patcher, state = self._jdp_stub()
        with patcher:
            self._run_nightly()

        self.assertEqual(
            self._query_keys(state),
            ["OLDER-VIA-B", "NEWER-VIA-A"],
            f"更老的单必须先查（跨绑定全局老单优先）：{self._query_keys(state)}",
        )

    def test_queries_are_paced_by_the_configured_interval(self):
        """§5-8 限速：相邻两查询垫 `RECEIPT_SYNC_INTERVAL_SECONDS`（sleep 桩断言参数）。"""
        first = self._order()
        self._link(first, upstream_order_id="PACED-1")
        second = self._order()
        self._link(second, upstream_order_id="PACED-2")
        # 每单都命中行（未结）⇒ 每单只发一发 ⇒ 两单之间恰好一次 sleep。
        rows = {
            "PACED-1": [{**UNSETTLED_ROW, "orderId": "PACED-1"}],
            "PACED-2": [{**UNSETTLED_ROW, "orderId": "PACED-2"}],
        }

        patcher, state = self._jdp_stub(rows)
        with patcher, override_settings(
            RECEIPT_SYNC_INTERVAL_SECONDS=2.5,
        ), mock.patch.object(
            upstream_receipt_sync, "_monotonic", return_value=0.0,
        ) as clock, mock.patch.object(
            upstream_receipt_sync, "_sleep"
        ) as sleep_mock:
            self._run_nightly()

        sleep_mock.assert_called_once_with(2.5)
        self.assertGreaterEqual(clock.call_count, 2)

    def test_a_jidanpu_binding_deleted_mid_run_is_loudly_skipped(self):
        """竞态：候选收集后绑定被删（link 行随 CASCADE 也没了，但 jobs 已在内存）
        ⇒ 处理时重读绑定拿不到行 ⇒ bind_error 留痕、零上游、不烧放弃数。"""
        first = self._order()
        self._link(first, upstream_order_id="DEL-1")
        second = self._order()
        self._link(second, upstream_order_id="DEL-2")

        def delete_during_first(params):
            if params["key"] == "DEL-1":
                JidanpuUpstreamBinding.objects.filter(pk=self.jbinding.pk).delete()

        patcher, state = self._jdp_stub(on_call=delete_during_first)
        with patcher:
            summary = self._run_nightly()

        # DEL-1 被查了一发（每单恰好一发）；DEL-2（绑定已删）零上游。
        self.assertEqual(self._query_keys(state), ["DEL-1"])
        self.assertEqual(summary["jidanpu"]["bind_error"], 1)
        self.assertEqual(summary["jidanpu"]["queried"], 1)
        second.refresh_from_db()
        self.assertEqual(second.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(second.receipt_sync_notfound_count, 0)
        log = self._logs(second.order_no).get(action=upstream_receipt_sync.QUERY_ACTION)
        self.assertIn("结局=bind_error", log.reason)

    def test_the_jidanpu_channel_without_stubs_fails_loudly(self):
        """「不接桩应当失败」对照（E-0029）：真建连打不可达的 `.invalid` 域 ⇒
        TransportError（JidanpuError 族）⇒ 总尝试烧完 ⇒ error、零写。证明
        `_jdp_stub` 真的长在网络调用这条路上：桩在 ⇒ 通道绿；桩不在 ⇒ 响亮失败。"""
        order = self._order()
        self._link(order, upstream_order_id=INTERNAL_ORDER_ID)
        summary = self._run_nightly()  # ⛔ 刻意不接任何桩

        self.assertEqual(summary["jidanpu"]["error"], 1)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(order.receipt_sync_notfound_count, 0, "网络失败不是查无")


# ═══════════ §5-6/§5-7 peer 通道 ═══════════════════════════════════════


class _StubPeerClient:
    """peer 客户端的桩（行为面：login / fetch_orders / close）。"""

    instances: list["_StubPeerClient"] = []

    def __init__(self, *, base_url, username, password, timeout=10.0):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.login_calls = 0
        self.fetch_calls: list[str] = []
        self.login_error: Exception | None = None
        self.fetch_error: Exception | None = None
        self.rows_by_key: dict[str, list] = {}
        _StubPeerClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        pass

    def login(self):
        self.login_calls += 1
        if self.login_error is not None:
            raise self.login_error

    def fetch_orders(self, *, order_no):
        self.fetch_calls.append(order_no)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.rows_by_key.get(order_no, [])


def _stub_peer_class():
    """把模块里的 `PeerQueryClient` 换成桩类（E-0029：打在被查找的名字上）。"""
    _StubPeerClient.instances = []
    return mock.patch.object(upstream_receipt_sync, "PeerQueryClient", _StubPeerClient)


class PeerChannelRunTests(_Base):
    """peer 通道逐单行为（信封判失败 / ambiguous / bind_error 闩 / 预检竞态）。"""

    def test_a_settled_peer_row_flips_status(self):
        """对端行 `payment_status=SETTLED` ⇒ 翻已结款 + 查询行留痕。"""
        order = self._order()
        self._peer_link(order, peer_order_no=PEER_NO)
        with _stub_peer_class():
            with mock.patch.object(
                _StubPeerClient, "fetch_orders",
                return_value=[{"payment_status": PaymentStatus.SETTLED}],
            ):
                summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.SETTLED)
        self.assertEqual(summary["peer"]["settled"], 1)
        log = self._logs(order.order_no).get(action=upstream_receipt_sync.QUERY_ACTION)
        self.assertIn("通道=peer", log.reason)
        self.assertIn("结局=settled", log.reason)

    def test_an_unsettled_peer_row_writes_nothing(self):
        """对端行未结 ⇒ 零写、留痕（单向闩）。"""
        order = self._order()
        self._peer_link(order)
        with _stub_peer_class():
            with mock.patch.object(
                _StubPeerClient, "fetch_orders",
                return_value=[{"payment_status": PaymentStatus.UNSETTLED}],
            ):
                summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(summary["peer"]["not_settled"], 1)
        self.assertFalse(
            self._logs(order.order_no)
            .filter(action="order.change_payment_status")
            .exists()
        )

    def test_multi_row_answers_are_ambiguous_and_never_flip(self):
        """回包多于 1 行（对端同来源号多单）⇒ ambiguous、不翻状态、留痕行数。"""
        order = self._order()
        self._peer_link(order)
        with _stub_peer_class():
            with mock.patch.object(
                _StubPeerClient, "fetch_orders",
                return_value=[
                    {"payment_status": PaymentStatus.SETTLED},
                    {"payment_status": PaymentStatus.UNSETTLED},
                ],
            ):
                summary = self._run_nightly()

        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(summary["peer"]["ambiguous"], 1)
        log = self._logs(order.order_no).get(action=upstream_receipt_sync.QUERY_ACTION)
        self.assertIn("结局=ambiguous", log.reason)
        self.assertIn("行数=2", log.reason)
        self.assertEqual(order.receipt_sync_notfound_count, 0, "ambiguous 不是查无")

    def test_an_envelope_business_failure_is_an_error_and_never_written(self):
        """E-0087：信封业务码非 0 ⇒ `error`、零写；总尝试 ≤3 后跳过。"""
        order = self._order()
        self._peer_link(order)
        with _stub_peer_class():
            with mock.patch.object(
                _StubPeerClient, "fetch_orders",
                side_effect=upstream_receipt_sync.PeerQueryError("对端查单业务失败（code=1000）"),
            ) as fetch_mock, mock.patch.object(
                _StubPeerClient, "login"
            ):
                summary = self._run_nightly()

        self.assertEqual(fetch_mock.call_count, upstream_receipt_sync.NETWORK_MAX_ATTEMPTS)
        self.assertEqual(summary["peer"]["error"], 1)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(order.receipt_sync_notfound_count, 0)
        self.assertIn(
            "结局=error",
            self._logs(order.order_no).get(action=upstream_receipt_sync.QUERY_ACTION).reason,
        )

    def test_a_login_failure_latches_the_binding_for_the_rest_of_the_run(self):
        """§5-7 闩：登录失败 ⇒ 本单 bind_error + 该绑定剩余候选全跳过（零上游调用）。"""
        first = self._order()
        self._peer_link(first)
        second = self._order()
        self._peer_link(second)
        with _stub_peer_class():
            with mock.patch.object(
                _StubPeerClient, "login",
                side_effect=upstream_receipt_sync.PeerBindError(
                    "对端登录失败（凭据错 / 账号停用）", reason="credentials",
                ),
            ) as login_mock, mock.patch.object(
                _StubPeerClient, "fetch_orders"
            ) as fetch_mock:
                summary = self._run_nightly()

        self.assertEqual(login_mock.call_count, 1, "闩后不得再登录")
        fetch_mock.assert_not_called()
        self.assertEqual(summary["peer"]["bind_error"], 1)
        self.assertEqual(summary["peer"]["bind_latched_skip"], 1)
        for order in (first, second):
            order.refresh_from_db()
            self.assertEqual(order.payment_status, PaymentStatus.UNSETTLED)
        log = self._logs(first.order_no).get(action=upstream_receipt_sync.QUERY_ACTION)
        self.assertIn("结局=bind_error", log.reason)
        self.assertIn("credentials", log.reason)

    def test_an_orders_query_401_is_a_bind_error_and_latches(self):
        """查单 401/403 ⇒ bind_error + 闩（⛔ 不重试——不是瞬态）。"""
        order = self._order()
        self._peer_link(order)
        extra = self._order()
        self._peer_link(extra)
        with _stub_peer_class():
            with mock.patch.object(
                _StubPeerClient, "login"
            ), mock.patch.object(
                _StubPeerClient, "fetch_orders",
                side_effect=upstream_receipt_sync.PeerBindError(
                    "对端查单被拒（HTTP 403：会话失效或无权限）", reason="forbidden",
                ),
            ) as fetch_mock:
                summary = self._run_nightly()

        self.assertEqual(fetch_mock.call_count, 1, "401/403 ⛔ 重试")
        self.assertEqual(summary["peer"]["bind_error"], 1)
        self.assertEqual(summary["peer"]["bind_latched_skip"], 1)

    def test_a_rate_limited_login_is_diagnosed_as_1038_not_a_missing_prerequisite(self):
        """登录限流 1038 ⇒ bind_error 且 detail 带 rate_limited（别误诊成前置缺失）。"""
        order = self._order()
        self._peer_link(order)
        with _stub_peer_class():
            with mock.patch.object(
                _StubPeerClient, "login",
                side_effect=upstream_receipt_sync.PeerBindError(
                    "对端登录被限流（1038：该账号失败过于频繁，等 15 分钟自愈，勿连点）",
                    reason="rate_limited",
                ),
            ), mock.patch.object(_StubPeerClient, "fetch_orders"):
                self._run_nightly()

        log = self._logs(order.order_no).get(action=upstream_receipt_sync.QUERY_ACTION)
        self.assertIn("rate_limited", log.reason)
        self.assertIn("1038", log.reason)

    def test_a_binding_that_stops_qualifying_mid_run_is_skipped_as_precheck_failed(self):
        """竞态：候选收集后绑定失去候选资格（处理中密码布尔被清）⇒ 处理时重读绑定
        发现不达标 ⇒ precheck_failed 留痕、零上游、不烧放弃数。"""
        first = self._order()
        self._peer_link(first)
        second = self._order()
        self._peer_link(second)
        real_fetch = mock.Mock(return_value=[])

        def clear_during_first(*, order_no):
            PeerUpstreamBinding.objects.filter(pk=self.pbinding.pk).update(
                receipt_query_password_set=False
            )
            return real_fetch(order_no=order_no)

        with _stub_peer_class():
            with mock.patch.object(_StubPeerClient, "login"), mock.patch.object(
                _StubPeerClient, "fetch_orders", side_effect=clear_during_first
            ):
                summary = self._run_nightly()

        real_fetch.assert_called_once()
        self.assertEqual(summary["peer"]["precheck_failed"], 1)
        second.refresh_from_db()
        self.assertEqual(second.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(second.receipt_sync_notfound_count, 0)
        log = self._logs(second.order_no).get(action=upstream_receipt_sync.QUERY_ACTION)
        self.assertIn("结局=precheck_failed", log.reason)

    def test_the_client_gets_the_binding_credentials_and_https_base(self):
        """桩接线自证：客户端收到的就是绑定行的域名 / 子账号 / 查询密码（形状核对）。"""
        self._peer_link(self._order())
        with _stub_peer_class():
            with mock.patch.object(
                _StubPeerClient, "login"
            ), mock.patch.object(_StubPeerClient, "fetch_orders", return_value=[]):
                self._run_nightly()

        client = _StubPeerClient.instances[0]
        self.assertEqual(client.base_url, "https://peer-upstream.test.invalid")
        self.assertEqual(client.username, "peer-account-1")
        self.assertEqual(client.password, "peer-query-password-1")


# ═══════════ §5-10 peer 客户端本体（MockTransport，零真网络）═════════════


class PeerQueryClientUnitTests(SimpleTestCase):
    """登录流程与查单解析（响应判据按信封业务码，E-0087）。

    ⚠️ 用 `httpx.MockTransport` 喂真 httpx.Response——被测的是 `PeerQueryClient` 的
    判据与解析，不是传输层。另有一条「不接桩应当失败」对照（E-0029）证明通道真在网络
    调用上跑：`test_an_unreachable_peer_fails_loudly`。
    """

    def _client(self, handler) -> upstream_receipt_sync.PeerQueryClient:
        client = upstream_receipt_sync.PeerQueryClient(
            base_url="https://peer-upstream.test.invalid",
            username="peer-account-1",
            password="peer-query-password-1",
        )
        client._http = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://peer-upstream.test.invalid",
            follow_redirects=False,
        )
        self.addCleanup(client.close)
        return client

    def test_login_sends_csrf_header_and_credentials_and_ok_passes(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/csrf":
                return httpx.Response(
                    200, headers=[("set-cookie", "csrftoken=tok-123; Path=/")]
                )
            seen["header"] = request.headers.get("X-CSRFToken")
            seen["body"] = request.read().decode()
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {}})

        client = self._client(handler)
        client.login()
        self.assertEqual(seen["header"], "tok-123")
        self.assertIn("peer-account-1", seen["body"])
        self.assertIn("peer-query-password-1", seen["body"])

    def test_fetch_orders_parses_data_items(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["order_nos"], PEER_NO)
            return httpx.Response(
                200,
                json={
                    "code": 0, "msg": "",
                    "data": {"items": [{"payment_status": "SETTLED"}], "total": 1},
                },
            )

        client = self._client(handler)
        rows = client.fetch_orders(order_no=PEER_NO)
        self.assertEqual(rows, [{"payment_status": "SETTLED"}])

    def test_a_401_with_no_special_code_is_a_credential_bind_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/csrf":
                return httpx.Response(200, headers=[("set-cookie", "csrftoken=tok; Path=/")])
            return httpx.Response(401, json={"code": 1001, "msg": "未认证", "data": None})

        client = self._client(handler)
        with self.assertRaises(upstream_receipt_sync.PeerBindError) as ctx:
            client.login()
        self.assertEqual(ctx.exception.reason, "credentials")

    def test_1038_is_rate_limited_and_1034_is_app_account(self):
        for code, reason in (
            (ErrorCode.LOGIN_RATE_LIMITED, "rate_limited"),
            (ErrorCode.APP_ACCOUNT_WEB_LOGIN_FORBIDDEN, "app_account"),
        ):
            with self.subTest(code=code):
                def handler(request: httpx.Request, code=code) -> httpx.Response:
                    if request.url.path == "/api/auth/csrf":
                        return httpx.Response(
                            200, headers=[("set-cookie", "csrftoken=tok; Path=/")]
                        )
                    return httpx.Response(401, json={"code": int(code), "msg": "x", "data": None})

                client = self._client(handler)
                with self.assertRaises(upstream_receipt_sync.PeerBindError) as ctx:
                    client.login()
                self.assertEqual(ctx.exception.reason, reason)

    def test_a_403_is_forbidden(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/auth/csrf":
                return httpx.Response(200, headers=[("set-cookie", "csrftoken=tok; Path=/")])
            return httpx.Response(403, json={"code": 1002, "msg": "无权限", "data": None})

        client = self._client(handler)
        with self.assertRaises(upstream_receipt_sync.PeerBindError) as ctx:
            client.login()
        self.assertEqual(ctx.exception.reason, "forbidden")

    def test_orders_401_403_are_bind_errors_not_retries(self):
        for status in (401, 403):
            with self.subTest(status=status):
                def handler(request: httpx.Request, status=status) -> httpx.Response:
                    return httpx.Response(status, json={"code": 1001, "msg": "x", "data": None})

                client = self._client(handler)
                with self.assertRaises(upstream_receipt_sync.PeerBindError) as ctx:
                    client.fetch_orders(order_no=PEER_NO)
                self.assertEqual(ctx.exception.reason, "forbidden")

    def test_non_json_and_shape_drift_are_query_errors_not_silently_empty(self):
        for payload in ("not-json", {"code": 0}, {"code": 0, "data": None}):
            with self.subTest(payload=payload):
                def handler(request: httpx.Request, payload=payload) -> httpx.Response:
                    if payload == "not-json":
                        return httpx.Response(200, text="not-json")
                    return httpx.Response(200, json=payload)

                client = self._client(handler)
                with self.assertRaises(upstream_receipt_sync.PeerQueryError):
                    client.fetch_orders(order_no=PEER_NO)

    def test_a_nonzero_business_code_on_orders_is_a_query_error(self):
        """E-0087：200 + 业务码非 0 ⇒ 判失败（⛔ 不拿「没抛异常」当成功）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 1000, "msg": "校验失败", "data": None})

        client = self._client(handler)
        with self.assertRaises(upstream_receipt_sync.PeerQueryError):
            client.fetch_orders(order_no=PEER_NO)

    def test_an_unreachable_peer_fails_loudly(self):
        """「不接桩应当失败」对照（E-0029）：真客户端打不可达域名 ⇒ PeerQueryError。

        证明通道侧的桩（`PeerQueryClient`）确实长在网络调用这条路上：桩在 ⇒ 通道绿；
        桩不在 + 对端不可达 ⇒ 响亮失败，两者分得开。
        """
        client = upstream_receipt_sync.PeerQueryClient(
            base_url="https://receipt-sync-control.invalid",
            username="u", password="p", timeout=2.0,
        )
        with self.assertRaises(upstream_receipt_sync.PeerQueryError):
            client.login()


# ═══════════ §5-8 锁互斥 / §5-11 端点接线 ═══════════════════════════════


@contextmanager
def _other_db_connection():
    """另一条到同一测试库的连接（照 `test_xgj_receipt_sync._other_db_connection` 形状）。"""
    import psycopg

    params = connection.get_connection_params()
    kwargs = {
        key: params[key]
        for key in ("dbname", "user", "password", "host", "port")
        if key in params
    }
    conn = psycopg.connect(**kwargs, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


class LockAndWiringTests(_Base):
    """锁 namespace 与端点线程体接线。"""

    def test_the_orchestrator_shares_the_xgj_lock_namespace(self):
        """§5-9：编排器与旧 `xgj_receipt_sync` 跑共用 namespace（同锁 ⇒ 互斥、不并发打上家）。"""
        self.assertIs(
            upstream_receipt_sync.xgj_receipt_sync.SYNC_LOCK_NAMESPACE,
            xgj_receipt_sync.SYNC_LOCK_NAMESPACE,
        )
        self.assertEqual(xgj_receipt_sync.SYNC_LOCK_NAMESPACE, 0x58474A52)

    def test_a_nightly_run_releases_the_lock(self):
        """跑完锁必须放掉（否则手动按钮永远 1037）。"""
        order = self._order()
        self._peer_link(order)
        with _stub_peer_class():
            with mock.patch.object(_StubPeerClient, "login"), mock.patch.object(
                _StubPeerClient, "fetch_orders",
                return_value=[{"payment_status": PaymentStatus.SETTLED}],
            ):
                summary = self._run_nightly()

        self.assertTrue(summary["ran"])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks "
                "WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            )
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_the_endpoint_thread_body_is_the_orchestrator(self):
        """§5-11 接线：端点起的是编排器线程体、旧 `start_xgj_receipt_sync` 不再被调。

        （busy 1037 / 31 天校验 / 401-403 权限回归原样住在 `tests/test_xgj_receipt_sync.py`，
        换桩后保持绿。）
        """
        client = make_logged_in_client(
            username="receipt-node-user", groups=("收款节点组",),
            nodes=("order.change_payment_status",),
        )
        with mock.patch.object(
            upstream_receipt_sync, "start_upstream_receipt_sync"
        ) as new_dispatch, mock.patch.object(
            xgj_receipt_sync, "start_xgj_receipt_sync"
        ) as old_dispatch:
            response = client.post(
                ENDPOINT_URL,
                {"date_from": DAY.isoformat(), "date_to": DAY.isoformat()},
                content_type="application/json",
            )

        self.assertEqual(response.json()["code"], ErrorCode.OK, response.content[:200])
        new_dispatch.assert_called_once()
        old_dispatch.assert_not_called()
        self.assertEqual(
            new_dispatch.call_args.kwargs,
            {"date_from": DAY, "date_to": DAY},
        )


# ═══════════ §5-12 租户闸 / §5-13 夜间命令 ══════════════════════════════


class NightlyCommandTests(_Base):
    """`sync_upstream_receipts`：租户闸 / 逐租户运行 / 摘要行。"""

    def _run_command(self):
        from io import StringIO

        out = StringIO()
        call_command("sync_upstream_receipts", stdout=out)
        return out.getvalue()

    def test_no_config_row_means_enabled(self):
        """读侧语义（S5）：无行 = get_or_create = 启用（⛔ 不留双解）。"""
        self.assertFalse(
            ReceiptSyncConfig.objects.filter(
                tenant_schema=connection.schema_name
            ).exists()
        )
        self.assertTrue(upstream_receipt_sync.receipt_sync_enabled())
        self.assertTrue(
            ReceiptSyncConfig.objects.filter(
                tenant_schema=connection.schema_name, enabled=True
            ).exists()
        )

    def test_a_disabled_tenant_gate_means_zero_upstream(self):
        """§5-12：`ReceiptSyncConfig.enabled=False` ⇒ 整租户零上游调用。"""
        self._peer_link(self._order())
        self._link(self._order())
        ReceiptSyncConfig.objects.create(tenant_schema=connection.schema_name, enabled=False)
        patcher, state = self._jdp_stub()
        with _stub_peer_class() as _:
            with mock.patch.object(_StubPeerClient, "login"), mock.patch.object(
                _StubPeerClient, "fetch_orders"
            ) as fetch_mock, patcher:
                output = self._run_command()

        fetch_mock.assert_not_called()
        self.assertEqual(state["calls"], [], "租户闸关闭 ⇒ 零上游调用")
        self.assertIn("租户闸关闭", output)

    def test_the_command_runs_the_tenant_and_prints_channel_summaries(self):
        """§5-13：枚举本机租户、逐租户跑两通道、结尾摘要行（候选/翻状态/查无/错误/排除）。"""
        peer_order = self._order()
        self._peer_link(peer_order)
        jdp_order = self._order(source_order_no=VIEW_ORDER_ID)
        self._link(jdp_order)

        patcher, state = self._jdp_stub()
        with _stub_peer_class():
            with mock.patch.object(_StubPeerClient, "login"), mock.patch.object(
                _StubPeerClient, "fetch_orders",
                return_value=[{"payment_status": PaymentStatus.UNSETTLED}],
            ), patcher:
                output = self._run_command()

        peer_order.refresh_from_db()
        self.assertEqual(peer_order.payment_status, PaymentStatus.UNSETTLED)
        self.assertEqual(len(state["calls"]), 1)
        self.assertIn("夜间上家收款完成", output)
        self.assertIn("①式[", output)
        self.assertIn("peer[", output)
        self.assertIn("scanned=1", output)
        self.assertIn("skipped_no_link=0", output)  # 换源轮新结局进摘要行
        self.assertIn("合计：", output)

    def test_a_channel_crash_still_fails_the_command_loudly(self):
        """⭐ M1-3 命令侧：通道级边界接住后**另一通道照跑**，但该租户这一轮
        **不算干净** ⇒ 命令仍以未预期失败收口（退出码 1）——语义不回退。"""
        self._peer_link(self._order())
        self._link(self._order(), upstream_order_id=INTERNAL_ORDER_ID)

        from django.core.management.base import CommandError

        patcher, state = self._jdp_stub()
        with _stub_peer_class():
            with mock.patch.object(_StubPeerClient, "login"), mock.patch.object(
                _StubPeerClient, "fetch_orders", return_value=[],
            ), patcher, mock.patch.object(
                upstream_receipt_sync, "_income_view_client",
                side_effect=RuntimeError("未预期的形状变更"),
            ):
                with self.assertRaises(CommandError):
                    self._run_command()

    def test_a_busy_tenant_is_skipped_with_a_line(self):
        """另一轮在跑（同 namespace 锁）⇒ 该租户本轮跳过、摘要照出。"""
        with _other_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_try_advisory_lock(%s, %s)",
                    [
                        xgj_receipt_sync.SYNC_LOCK_NAMESPACE,
                        xgj_receipt_sync.SYNC_LOCK_KEY,
                    ],
                )
                self.assertTrue(cursor.fetchone()[0], "夹具没拿到锁 ⇒ 用例是假绿")

            patcher, state = self._jdp_stub()
            with patcher, _stub_peer_class():
                output = self._run_command()

        self.assertEqual(state["calls"], [])
        self.assertIn("正在运行 ⇒ 本轮跳过", output)


class PeerLoginOriginHeaderTests(SimpleTestCase):
    """login 的真 HTTP 层必须带 Origin/Referer 缺省头。

    2026-11-01 生产对照实锤（curl 假密码对照）：对端 Django CSRF 中间件对 HTTPS POST
    缺 `Origin` ⇒ 403「CSRF 校验失败」；带 Origin 后假密码正常进认证（401）。浏览器
    永远带 Origin 所以网页端无感；既有桩全打在 `PeerQueryClient` 整体上，HTTP 层从未
    真跑过 ⇒ 429 条分支网没红、运行时预检首日坐实。轻量档 `fix/peer-login-origin`。
    """

    def test_the_login_post_carries_origin_and_referer_default_headers(self):
        seen = {}

        def handler(request):
            seen.setdefault(request.url.path, dict(request.headers))
            if request.url.path == upstream_receipt_sync.PEER_CSRF_PATH:
                return httpx.Response(
                    200,
                    headers={"set-cookie": "csrftoken=stub-token; Path=/"},
                    json={"code": 1000, "data": None},
                )
            return httpx.Response(
                401,
                json={"code": 401, "msg": "账号或密码错误", "data": None},
            )

        real_client = httpx.Client

        def factory(*args, **kwargs):
            return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        with mock.patch.object(
            upstream_receipt_sync.httpx, "Client", side_effect=factory
        ):
            client = upstream_receipt_sync.PeerQueryClient(
                base_url="https://peer.invalid", username="u", password="p"
            )
            with self.assertRaises(upstream_receipt_sync.PeerBindError):
                client.login()

        origin = "https://peer.invalid"
        self.assertEqual(
            seen[upstream_receipt_sync.PEER_LOGIN_PATH].get("origin"), origin
        )
        self.assertEqual(
            seen[upstream_receipt_sync.PEER_LOGIN_PATH].get("referer"), origin + "/"
        )
        self.assertEqual(
            seen[upstream_receipt_sync.PEER_CSRF_PATH].get("origin"), origin
        )
