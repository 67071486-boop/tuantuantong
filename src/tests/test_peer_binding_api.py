"""上家侧握手端点（spec §5.1 / §5.2 / §8.2）+ 下家侧页面三端点（spec §8.4）。⚠️ 本文件同时承担 **HTTP 层**判据：401 / 会话被拒 / 跨租户。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里 ⇒ 普通 `TestCase` 的 search_path 里没有 `business_*` 表（照 `tests/test_peer_models.py` 的先例）。
⚠️ 每条请求都要带**租户域名**：默认的 `testserver` 在 `TenantMainMiddleware` 里解析不到 `Domain` ⇒ 404（本仓既有 HTTP 测试一律显式给 `SERVER_NAME`，见 `tests/p5_auth_helpers.py`）。"""
import json
import logging
import uuid
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import (
    Game,
    Order,
    OrderStatus,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerSyncEvent,
    PeerUpstreamBinding,
)
from apps.business.peer_client import PeerRejected, PeerUnreachable
from apps.business.peer_sync import hash_token, new_token
from apps.core.authentication import PeerTokenAuthentication
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: A 的域名 = `TenantTestCase` 自带租户的域名（与既有账号隔离测试同口径）。
PEER_DOMAIN_A = "tenant.test.com"
BIND_URL = "/api/business/peer-sync/bind/"
UNBIND_URL = "/api/business/peer-sync/unbind/"
#: URL 闸门失败文案的**外部契约值**——逐字写死。
#: ⛔ 不要改成 `peer_views.PEER_BIND_BAD_BASE_URL_MSG`：那是自指断言，常量被改成英文时不会红
#: （`AGENTS.md` §5-G / E-0024；同文件家族见 `test_peer_sync_pure.py` 的 `EXPECTED_PEER_ORDER_FIELDS`）。
EXPECTED_BAD_BASE_URL_MSG = "下家系统填的网址不对：只填 http/https 的域名（可带端口），且不能是内网地址"

#: `bind/` 失败限流的**外部契约值**（spec §9-S5 的 Q5；业务方裁定 10 / 60，决策 `D-0157`）。
#: ⚠️ 逐字写死、⛔ **不 import `PEER_BIND_MAX_FAILURES` 去自比**——那是自指断言：常量被改坏时它跟着一起变、照样绿（`AGENTS.md` §5-G / E-0024）。下面的用例拿**这两个数字**去比对真模块常量
#: ⇒ 值被改动而这里没跟着改时当场红（「把数改小」这个动作必须留下痕迹）。
EXPECTED_PEER_BIND_MAX_FAILURES = 10
EXPECTED_PEER_BIND_WINDOW_SECONDS = 60

#: 页面列表一行**恰好**的字面键名（`PeerUpstreamBindingSerializer.fields`）。
#: ⚠️ 逐字写死、⛔ **不从序列化器派生**：`set(PeerUpstreamBindingSerializer().fields)` 是自指断言，给序列化器删一个字段也照样绿（`AGENTS.md` §5-G / E-0024，先例 `test_order_source_api.py` 的 `CONTRACT_FIELDS`）。
#: `bound_by_name`（显示名）是页面新增的那一个；`uses_insecure_http` 是存量明文风险的标志（审计批新增）。
PEER_BINDING_CONTRACT_FIELDS = [
    "id", "peer_name", "peer_base_url", "peer_account",
    "password_set", "uses_insecure_http", "bound_by", "bound_by_name", "bound_at",
    "last_ok_at", "last_error", "last_error_at", "consecutive_failures",
    # ⭐ 大厅镜像 P1：接收侧开关 + 大厅价格百分比（编辑弹窗的回填出口）。
    "hall_sync_enabled", "hall_price_percent",
    # ⭐ D-0359：上家收款查询密码的「已配」布尔（⛔ 明文列
    #    `receipt_query_password` 本身永不进任何出参——下面有专门一条用例钉住）。
    "receipt_query_password_set",
]

#: 两个页面端点的**节点门**节点 code（**裁决 R22**）。⚠️ 逐字写死、⛔ 不从视图 import `PEER_BINDING_PAGE_NODE`：那是自指断言——常量被改成别的节点时不会红（E-0024）。
#: 这个值必须与 `src/frontend/src/config/menu.ts` / `config/routePermissions.ts` 上 `system-source-system` 声明的那个节点一致（`tests/test_rbac_wiring.py` 另有覆盖）。
PAGE_BINDING_NODE = "system.bind_upstream_system"


def _bind_payload(**over):
    data = {
        "account": "peer-account-a",
        "password": "peer-password-a",
        "peer_base_url": "https://peer-b.test.invalid",
        "token_out": new_token(),
    }
    data.update(over)
    return data


class _ListLogHandler(logging.Handler):
    """把**真的流经 `logging` 的记录**收进列表（spec §9-S4 的"密码不进日志"判据用）。
    ⛔ **刻意不用 `assertLogs`**：它会自己把目标 logger 的级别抬到 INFO ⇒「生产一行都没写」的用例会**全绿**（本项目踩过，`AGENTS.md` §5-G / E-0009）。这里把记录收下来，断言打在**记录文本**上——
    并且另有一条**正向对照**证明这个 sink 抓得住带密码的记录（否则"没抓到"与"没写"分不开，判据恒真）。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def texts(self) -> list[str]:
        out = []
        for record in self.records:
            try:
                out.append(record.getMessage())
            except Exception:                     # pragma: no cover - 只为不让 sink 自己炸
                out.append(str(record.msg))
        return out


@contextmanager
def _capture_log_records():
    """在**根 logger** 上挂一个收集器（级别压到 DEBUG），退出时无条件还原。"""
    handler = _ListLogHandler()
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


class PeerBindEndpointTests(TenantTestCase):
    def setUp(self):
        cache.clear()
        self.client = Client(SERVER_NAME=PEER_DOMAIN_A)
        self.account = User.objects.create_user(
            username="peer-account-a", password="peer-password-a"
        )

    def _post(self, payload, **extra):
        return self.client.post(
            BIND_URL, json.dumps(payload),
            content_type="application/json", **extra,
        )

    def _post_on_port(self, port: str, payload):
        """换个 `Host` 端口打同一个租户域名。
        ⚠️ **坑（本用例第一版踩过、变异存活）**：`SERVER_PORT` 必须作为**请求的 extra**（`post(..., SERVER_PORT=…)`）传，⛔ 不能只传给 `Client(SERVER_PORT=…)` 的默认值——`RequestFactory.generic()` 在 `_base_environ` **之后**用 `SERVER_PORT="80"` 覆写它
        （实测：`Client(SERVER_PORT="1111")` 的请求里 `get_host()` 仍是 `tenant.test.com`）⇒ 那样写这条用例**恒真**。调用的用例里必须自证端口真的进了 `get_host()`。"""
        return self.client.post(
            BIND_URL, json.dumps(payload), content_type="application/json",
            SERVER_NAME=PEER_DOMAIN_A, SERVER_PORT=port,
        )

    def test_successful_bind_creates_a_row_and_returns_a_token(self):
        payload = _bind_payload()
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)
        handed_out = resp.json()["data"]["token_out"]
        self.assertTrue(handed_out)
        row = PeerDownstreamBinding.objects.get(account=self.account)
        self.assertEqual(row.peer_base_url, "https://peer-b.test.invalid")
        # ⚠️ 下家给我的那套令牌**只存哈希**（spec §9-S3 / R7）——明文一个字符都不落库。
        self.assertEqual(row.token_in_hash, hash_token(payload["token_out"]))
        self.assertNotEqual(row.token_in_hash, payload["token_out"])
        # ⚠️ 我回给下家的那套（`token_out`）反过来必须**明文**存：每次推单都要带它（S3）。
        self.assertEqual(row.token_out, handed_out)
        # 回的不是下家给我的那一套（否则等于把对方的令牌原样退回，握手没有发生）。
        self.assertNotEqual(handed_out, payload["token_out"])
        self.assertEqual(row.state, PeerDownstreamBinding.State.ACTIVE)

    def test_wrong_password_and_unknown_account_return_the_same_code_and_msg(self):
        """⛔ 不给账号枚举面（spec §9-S5）。"""
        wrong = self._post(_bind_payload(password="nope")).json()
        unknown = self._post(_bind_payload(account="no-such-account")).json()
        self.assertEqual(wrong["code"], unknown["code"])
        self.assertEqual(wrong["code"], 1018)
        self.assertEqual(wrong["msg"], unknown["msg"])
        # ⚠️ 上面那条相等断言在**两个空串**上也会通过 ⇒ 必须另钉「文案确实存在」
        # 。
        self.assertTrue(wrong["msg"])
        self.assertTrue(unknown["msg"])
        # 两次失败都不得留下半个绑定（判据打在库上，spec §9-S8）。
        self.assertFalse(PeerDownstreamBinding.objects.exists())

    def test_a_disabled_account_cannot_bind(self):
        """`bind/` 只接受**启用**的账号；停用账号回同一个 1018（spec §9-S8）。"""
        self.account.is_active = False
        self.account.save(update_fields=["is_active"])
        self.assertEqual(self._post(_bind_payload()).json()["code"], 1018)
        self.assertFalse(PeerDownstreamBinding.objects.exists())

    def test_private_base_url_is_rejected_as_a_business_error_not_a_500(self):
        """SSRF 闸门（spec §9-S9）：预期失败翻业务码，⛔ 不是 500（E-0010）。"""
        resp = self._post(_bind_payload(peer_base_url="http://127.0.0.1:8000"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 1018)
        # 被闸门挡下的请求不得落库（否则会留下一行指向内网的绑定）。
        self.assertFalse(PeerDownstreamBinding.objects.exists())

    def test_malformed_base_url_never_echoes_the_stdlib_text(self):
        """URL 闸门的失败文案必须是**自有中文**，⛔ 不许把 Python 的英文原文回给外部（spec §14-R7）。
        实测（本机 Python 3.13）三条原文，旧实现把它们**逐字**塞进 `msg`：`http://example.com:abc` ⇒ "Port could not be cast to integer value as 'abc'"；`http://example.com:99999` ⇒ "Port out of range 0-65535"；`http://[::1` ⇒ "Invalid IPv6 URL"。
        前两条由 `urlsplit(...).port` 抛、最后一条由 `urlsplit(...)` 自己抛。"""
        for bad, stdlib_phrasing in (
            ("http://example.com:abc", "Port could not be cast"),
            ("http://example.com:99999", "Port out of range"),
            ("http://[::1", "Invalid IPv6 URL"),
        ):
            with self.subTest(bad=bad):
                resp = self._post(_bind_payload(peer_base_url=bad))
                self.assertEqual(resp.status_code, 200, resp.content[:200])
                body = resp.json()
                self.assertEqual(body["code"], 1018)
                self.assertEqual(body["msg"], EXPECTED_BAD_BASE_URL_MSG)
                self.assertNotIn(stdlib_phrasing, body["msg"])
                self.assertNotIn("Port", body["msg"])      # 任何端口原文都不许漏
                self.assertFalse(PeerDownstreamBinding.objects.exists())

    def test_password_is_never_echoed(self):
        resp = self._post(_bind_payload())
        # ⚠️ **正向对照**（没有它时，一个"永远回 1018"的握手实现照样绿）：
        # 先证明这一次握手**真的成功、真的回了令牌**，再断言密码不在响应里。
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)
        self.assertTrue(resp.json()["data"]["token_out"])
        self.assertNotIn("peer-password-a", resp.content.decode())

    def test_rebinding_the_same_account_rotates_the_token_and_reactivates(self):
        """同一个账号再握手一次 = **重绑**（页面依赖的合同）。第二次必须：轮换 `token_out`、把 `state` 从 `revoked` 复位成 `active`、清空 `revoked_at`，且 ⛔ **不新增第二行**（`account` 是 `OneToOne`）。
        ⚠️ 相关事实（评审点名要报告的一条）：`bound_at` 是 `auto_now_add` ⇒ 重绑**不刷新**它。它只能读作「这一行第一次是什么时候建的」，
        ⛔ 不能读作「最近一次握手时间」（`tests/test_operation_log_ledger.py` 的留痕口径与它一致）。"""
        first = self._post(_bind_payload())
        self.assertEqual(first.status_code, 200, first.content[:200])
        first_token = first.json()["data"]["token_out"]
        row = PeerDownstreamBinding.objects.get(account=self.account)
        self.assertEqual(row.token_out, first_token)
        first_bound_at = row.bound_at

        # 人为把它打成「已解绑」，模拟页面上「解绑后重绑」那一条路。
        row.state = PeerDownstreamBinding.State.REVOKED
        row.revoked_at = timezone.now()
        row.save(update_fields=["state", "revoked_at"])

        payload = _bind_payload()
        second = self._post(payload)
        self.assertEqual(second.status_code, 200, second.content[:200])
        self.assertEqual(second.json()["code"], 0)
        second_token = second.json()["data"]["token_out"]

        # ⛔ 不新增第二行（`update_or_create` 的 update 支路真的被走到了）。
        self.assertEqual(PeerDownstreamBinding.objects.count(), 1)
        row.refresh_from_db()
        self.assertNotEqual(second_token, first_token)               # 令牌轮换
        self.assertEqual(row.token_out, second_token)
        self.assertEqual(row.state, PeerDownstreamBinding.State.ACTIVE)   # 复位
        self.assertIsNone(row.revoked_at)                                  # 清空
        # 下家第二次给的那套也换成了新的。
        self.assertEqual(row.token_in_hash, hash_token(payload["token_out"]))
        # ⚠️ 只是**如实登记**这条已实测事实、不是断言它是期望行为（见 docstring）：
        # 这是一条**已知残差**——若将来决定「重绑要刷新绑定时间」，得改模型 + 迁移，
        # 并**同一次**改掉本行与 `test_operation_log_ledger.py` 的注释，⛔ 不能只改一侧。
        self.assertEqual(row.bound_at, first_bound_at)

    def test_rebinding_resets_the_delivery_failure_counters(self):
        """⭐ 重绑 = 重新开始 ⇒ 复位**投递**失败台账。
        判别力：`bind/` 的 `update_or_create(defaults=…)` 里去掉那三列 ⇒ 本用例红（计数停在 10、`last_error` 还在、`last_error_at` 还非空）。
        后果是"重绑等于没用"：下一次投递失败立刻又触发自动停绑、并再写一条操作日志。"""
        first = self._post(_bind_payload())
        self.assertEqual(first.json()["code"], 0)
        row = PeerDownstreamBinding.objects.get(account=self.account)
        # 人为把它摆成"刚被自动停绑"的形状（计数 10 + 人话台账 + 已解除）。
        row.consecutive_failures = 10
        row.last_error = "连续失败 10 次已自动停止推送"
        row.last_error_at = timezone.now()
        row.state = PeerDownstreamBinding.State.REVOKED
        row.revoked_at = timezone.now()
        row.save(update_fields=[
            "consecutive_failures", "last_error", "last_error_at", "state", "revoked_at",
        ])

        second = self._post(_bind_payload())
        self.assertEqual(second.status_code, 200, second.content[:200])
        self.assertEqual(second.json()["code"], 0)

        row.refresh_from_db()
        self.assertEqual(row.state, PeerDownstreamBinding.State.ACTIVE)
        self.assertIsNone(row.revoked_at)
        self.assertEqual(row.consecutive_failures, 0)
        self.assertEqual(row.last_error, "")
        self.assertIsNone(row.last_error_at)

    def test_the_failure_bucket_is_the_tenant_schema_not_the_host_header_port(self):
        """限流键是**当前租户的 schema**，⛔ 不是 `request.get_host()`。
        ⚠️ `request.get_host()` **保留端口**，而 `TenantMainMiddleware` 解析租户时 `request.get_host().split(":")[0]` 把端口丢掉 ⇒ `Host: <域名>:1111` 与 `…:2222` 是**同一个租户**；
        旧实现按 Host 分桶 ⇒ 「10 次/分钟/租户」可以被任意端口后缀**乘**出来；本条在旧实现下会放行（⇒ 红），改成 `connection.schema_name` 后才拦得住。"""
        first = self._post_on_port("1111", _bind_payload(password="nope"))
        # ⚠️ **自证夹具**：端口必须真的进 `Host`，否则下面那条判据恒真（第一版就假绿过）。
        self.assertEqual(first.wsgi_request.get_host(), PEER_DOMAIN_A + ":1111")
        for _ in range(9):
            self._post_on_port("1111", _bind_payload(password="nope"))
        # 换端口 = 同一个租户：必须仍被拦住，而不是"另一个桶"。
        resp = self._post_on_port("2222", _bind_payload())
        self.assertEqual(resp.wsgi_request.get_host(), PEER_DOMAIN_A + ":2222")
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 1018)
        self.assertFalse(PeerDownstreamBinding.objects.exists())

    def test_nine_failures_still_allow_a_correct_password(self):
        """阈值是**第 10 次**，不是第 9 次：9 次失败之后正确口令仍必须放行。
        ⚠️ 这一条从**下界**钉死阈值：把判据挪到第 9 次（`>= 9`）它当场红。"""
        for _ in range(9):
            self._post(_bind_payload(password="nope"))
        resp = self._post(_bind_payload())
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)
        self.assertEqual(PeerDownstreamBinding.objects.count(), 1)

    def test_the_tenth_failure_flips_the_threshold(self):
        """第 10 次失败之后**当次即锁死**：紧接着的正确口令必须被拦。
        ⚠️ 既有那条「第 11 次」用例判不出 `>=` 与 `>`：第 11 次本来就带错口令、两种写法都回 1018 ⇒ 只有**这条**（10 次失败 + 正确口令）能把 `>` 弄红。"""
        for _ in range(10):
            self._post(_bind_payload(password="nope"))
        resp = self._post(_bind_payload())          # 口令正确
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 1018)
        self.assertFalse(PeerDownstreamBinding.objects.exists())

    def test_failure_counter_blocks_the_eleventh_attempt(self):
        """限流（spec §9-S5；参数见 Task 4 Step 0：10 次 / 分钟 / 租户）。"""
        for _ in range(10):
            self._post(_bind_payload(password="nope"))
        self.assertEqual(self._post(_bind_payload(password="nope")).json()["code"], 1018)
        # 第 11 次起即使密码对也不放行（窗口内）——判据同时落在库上：不得建行。
        self.assertEqual(self._post(_bind_payload()).json()["code"], 1018)
        self.assertFalse(PeerDownstreamBinding.objects.exists())

    def test_the_rate_limit_numbers_are_the_settled_ten_and_sixty(self):
        """限流**定值钉子**：模块常量必须**就是** 10 / 60（业务方裁定，决策 `D-0157`）。
        ⚠️ 判据打在**真模块常量**上、期望值来自本文件那两个**字面量**常量（⛔ 不是 import 自比）：把限流改小（例如 `= 5`）而没改这两个字面量 ⇒ 本用例红（`AGENTS.md` §5-G / E-0024）；⚠️ 另有一条**只读**含义：改小常量在生产上就是「放宽」——本用例让这个动作必须留下痕迹。
        ⚠️ 上面两条阈值用例（第 10 次锁死 / 第 11 次仍拦）**也**按 10 写死：常量一改它们一起红。"""
        from apps.business.api import peer_views

        self.assertEqual(
            peer_views.PEER_BIND_MAX_FAILURES, EXPECTED_PEER_BIND_MAX_FAILURES
        )
        self.assertEqual(
            peer_views.PEER_BIND_WINDOW_SECONDS, EXPECTED_PEER_BIND_WINDOW_SECONDS
        )

    def test_a_new_handshake_over_plain_http_is_rejected(self):
        """⭐ 新握手拒 `http://`——明文通道不收新绑定。
        判据：`1018` + 文案指向 https；一行绑定都不落（判据打库）；**计一次限流失败**（免认证端点上可无条件触发的动作必须进桶，否则白送一个免认证写缓存口）。"""
        from django.core.cache import cache as dj_cache

        resp = self._post(_bind_payload(peer_base_url="http://peer-b.test.invalid"))
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        body = resp.json()
        self.assertEqual(body["code"], 1018)
        self.assertIn("https", body["msg"])
        self.assertFalse(PeerDownstreamBinding.objects.exists())
        # 同一个租户桶里确实记了一次失败（键形状与 `_bind_failure_key` 一致——按 schema 分桶）。
        self.assertEqual(
            dj_cache.get(f"peer-bind-fail:{connection.schema_name}"), 1
        )

    def test_an_https_handshake_still_binds_after_the_http_gate(self):
        """正向对照：闸门只拦 `http://`，https 握手照常（否则上面那条可能恒真）。"""
        resp = self._post(_bind_payload())
        self.assertEqual(resp.json()["code"], 0, resp.json())
        self.assertEqual(PeerDownstreamBinding.objects.count(), 1)


class PeerBindRateLimiterAtomicityTests(TenantTestCase):
    """⭐ 失败计数 `add + incr` 的**原子性**。旧实现 `get` + `set` 在并发下丢计数：两个请求同时读到 5、各写 6 ⇒「10 次 / 60 秒」被放宽近半。
    判据 = N 个线程并发计数后**恰好** N（丢一条即红）。
    ⚠️ 只在**主线程**取键：`connection` 是 thread-local，从工作线程读 schema 会拿错（被测单元是计数器本身，不是键的推导）。"""

    def test_concurrent_failures_are_all_counted(self):
        import threading

        from apps.business.api import peer_views

        cache.clear()
        key = f"peer-bind-fail:{connection.schema_name}"
        threads = 16
        workers = [
            threading.Thread(target=peer_views._count_failure, args=(key,))
            for _ in range(threads)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(cache.get(key), threads)


class PeerAuthAtHttpLayerTests(TenantTestCase):
    """Task 3 欠的 HTTP 判据，这里打**真端点**。"""

    def test_no_token_is_401(self):
        resp = Client(SERVER_NAME=PEER_DOMAIN_A).post(
            UNBIND_URL, {}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 401, resp.content[:200])
        self.assertEqual(resp.json()["code"], 401)

    def test_session_cookie_is_rejected(self):
        """⚠️ 用**真登录**（不是 `force_login`）造会话：``force_login`` 产出的会话没有 `identity_plane`，被 `TenantBoundAuthentication` 按 fail-closed 拒掉（`tests/p5_auth_helpers.py` 的说明）⇒ 拿它测「会话被拒」没有判别力。
        正向对照：同一个 client 对自己的会话端点必须 200——否则这条 401 可能由「这个会话压根不成立」凑出来，与「同步端点不认会话」无关。"""
        client = make_logged_in_client(
            domain=PEER_DOMAIN_A, username="peer-account-a"
        )
        on_session_endpoint = client.get("/api/auth/me")
        self.assertEqual(on_session_endpoint.status_code, 200, on_session_endpoint.content[:200])
        resp = client.post(UNBIND_URL, {}, content_type="application/json")
        self.assertEqual(resp.status_code, 401, resp.content[:200])


class PeerUnbindEndpointTests(TenantTestCase):
    """`unbind/` 的两条真路径（spec §5.2）——含**承重判据**。
    ⚠️ 本类钉的是 `PeerTokenAuthentication` 里 `PeerUpstreamBinding` 的 `deleted_at__isnull=True` 那条过滤：它**承重**（已解绑的绑定其令牌必须立即失效），却**没有其他用例能弄红它**（没有用例建过 `deleted_at` 非空的绑定）。
    这里走真端点补上：解绑 → 同一个令牌再打一次必须 401。"""

    def setUp(self):
        cache.clear()
        self.client = Client(SERVER_NAME=PEER_DOMAIN_A)
        self.account = User.objects.create_user(username="peer-account-a", password="x")

    def _unbind(self, token):
        return self.client.post(
            UNBIND_URL, {}, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + token,
        )

    def test_the_downstream_binding_is_revoked_and_its_token_dies_immediately(self):
        """我是上家：下家给我的令牌命中 `PeerDownstreamBinding` ⇒ 置 `revoked`（§5.2 第 3 步）。"""
        raw = new_token()
        binding = PeerDownstreamBinding.objects.create(
            account=self.account,
            peer_base_url="https://peer-b.test.invalid",
            token_in_hash=hash_token(raw),
            token_out=new_token(),
        )
        first = self._unbind(raw)
        self.assertEqual(first.status_code, 200, first.content[:200])
        self.assertEqual(first.json()["code"], 0)
        binding.refresh_from_db()
        self.assertEqual(binding.state, PeerDownstreamBinding.State.REVOKED)
        self.assertIsNotNone(binding.revoked_at)
        # 解绑后同一个令牌**立即**失效（`state=ACTIVE` 那条过滤承重）。
        self.assertEqual(self._unbind(raw).status_code, 401)

    def test_the_upstream_binding_is_soft_deleted_and_its_token_dies_immediately(self):
        """命中 `PeerUpstreamBinding` 的令牌 ⇒ 走软删分支（§5.2 第 4 步的本地那一半）。
        ⚠️ 本条只测**认证层 + 分支**：`raw` 哈希进 `token_in_hash`（裁决 R15 的验证侧）。真实握手下下家用的是 `token_out`（`token_b`）、上家验的是它自己那张表——那一条端到端判据在 `PeerHandshakeCredentialTests`，⛔ 别拿本条充当「解绑令牌真的能验通」的证据。
        ⚠️ **承重点**：删掉 `PeerTokenAuthentication` 的 `deleted_at__isnull=True`，第二次调用会从 401 变成 200 ⇒ 本用例红。"""
        raw = new_token()
        binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(raw),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.account,
        )
        first = self._unbind(raw)
        self.assertEqual(first.status_code, 200, first.content[:200])
        self.assertEqual(first.json()["code"], 0)
        binding.refresh_from_db()
        self.assertIsNotNone(binding.deleted_at)
        # ⚠️ 这一条就是 承重判据。
        self.assertEqual(self._unbind(raw).status_code, 401)

    def test_the_unbind_notice_returns_unconfirmed_claims_to_the_hall(self):
        """⭐ 对端发来的解绑通知 ⇒ `claiming` 的卡退回 `listed` + 一条台账。
        与页面 DELETE 同一份口径（同一个 `peer_hall.release_claiming_mirrors`）；
        判别力：删掉退回那一支 ⇒ 卡停在 `claiming`，本用例红在状态与台账两处。"""
        from apps.business.peer_hall import (
            CLAIM_RELEASED_ON_UNBIND,
            RELEASED_CLAIM_LEDGER_KIND,
        )

        raw = new_token()
        binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(raw),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.account,
        )
        claimed_at = timezone.now()
        card = PeerHallMirror.objects.create(
            peer_upstream_binding=binding,
            peer_base_url=binding.peer_base_url,
            peer_order_no="SYN-Z5-UNBIND-CLAIM-1",
            status=PeerHallMirror.Status.CLAIMING,
            claiming_by=self.account,
            claiming_at=claimed_at,
            first_seen_at=claimed_at,
        )
        first = self._unbind(raw)
        self.assertEqual(first.status_code, 200, first.content[:200])
        self.assertEqual(first.json()["code"], 0)
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(card.claiming_by_id)
        self.assertIsNone(card.claiming_at)
        self.assertEqual(card.last_error, CLAIM_RELEASED_ON_UNBIND)
        ledger = PeerSyncEvent.objects.get(kind=RELEASED_CLAIM_LEDGER_KIND)
        self.assertEqual(ledger.reason, "绑定停用，抢单未确认已退回大厅")
        self.assertEqual(ledger.payload["released"], ["SYN-Z5-UNBIND-CLAIM-1"])

    def test_the_unbind_notice_also_unlists_listed_cards_and_forwards_take(self):
        """⭐ 解绑通知 ⇒ `listed` 存量卡同事务下架＋给下家补发 `hall.take`（与页面 DELETE 完全同一份口径）。
        病根：若只退 `claiming` 不下架存量卡，A→B→C 链上 B 收到 A 的解绑通知后，B 名下 `listed` 的卡既看得见又接不了、也不给 C 补 take（C 的卡永久滞留）。
        判别力：删掉 `unlist_binding_mirrors` 那一支 ⇒ 卡停在 `listed`、take 缺行，本用例红在状态与事件两处。"""
        from apps.business.peer_hall import UNLISTED_ON_UNBIND

        raw = new_token()
        binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(raw),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.account,
        )
        downstream = PeerDownstreamBinding.objects.create(
            account=self.account,
            peer_base_url="https://peer-c.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            state=PeerDownstreamBinding.State.ACTIVE,
            hall_push_enabled=True,
        )
        seen_at = timezone.now()
        card = PeerHallMirror.objects.create(
            peer_upstream_binding=binding,
            peer_base_url=binding.peer_base_url,
            peer_order_no="SYN-L9-UNBIND-LIST-1",
            status=PeerHallMirror.Status.LISTED,
            first_seen_at=seen_at,
        )

        first = self._unbind(raw)

        self.assertEqual(first.status_code, 200, first.content[:200])
        self.assertEqual(first.json()["code"], 0)
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.GONE)
        self.assertEqual(card.last_error, UNLISTED_ON_UNBIND)
        self.assertIsNotNone(card.gone_at)
        # 下家手里同款卡照旧补发 take（多跳状态跟随，`unlist_binding_mirrors` 内建）。
        takes = PeerSyncEvent.objects.filter(
            binding_role="downstream",
            binding_id=str(downstream.pk),
            direction="out",
            kind="hall.take",
            target_ref="SYN-L9-UNBIND-LIST-1",
        )
        self.assertEqual(takes.count(), 1)
        self.assertEqual(takes.get().status, PeerSyncEvent.Status.PENDING)


class PeerUnbindUnknownBindingTests(TenantTestCase):
    """第三张绑定表出现时，`unbind/` 不得把它**静默**当成上家绑定软删。
    ⚠️ 这条分支**今天不可达**（`PeerTokenAuthentication` 只挂 `PeerDownstreamBinding` / `PeerUpstreamBinding` 两张表），所以只能把认证层换成「命中一个认不出的对象」来驱动它；它正是「将来加第三张表、忘了同步改 `unbind/`」时的安全网 ⇒
    判据必须钉在「响亮 500 + 认不出的对象一行没动 + 真行没被误伤」上，⛔ 不是 200。"""

    def test_an_unknown_binding_type_fails_loudly_instead_of_being_soft_deleted(self):
        account = User.objects.create_user(username="peer-account-a", password="x")
        real_row = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-b.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )
        # 有 `deleted_at` 也有 `save()`：**旧写法**会在这里"成功"软删并回 200 ⇒ 本用例红。
        phantom = mock.MagicMock()

        def _fake_authenticate(self, request):
            request._peer_binding = phantom
            return account, None

        # `raise_request_exception=False`：未预期异常要变成 **500 响应**，而不是抛进用例
        # （抛进用例只能证明"炸了"，证明不了端点对外的答复是 500）。
        client = Client(SERVER_NAME=PEER_DOMAIN_A, raise_request_exception=False)
        with mock.patch.object(
            PeerTokenAuthentication, "authenticate", _fake_authenticate
        ):
            resp = client.post(
                UNBIND_URL, {}, content_type="application/json",
                HTTP_AUTHORIZATION="Bearer whatever",
            )

        # ⛔ 未预期 ⇒ 响亮失败（E-0010），不许伪装成业务结果、更不许伪装成成功。
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(phantom.save.called)          # 认不出的对象一行都不许动
        self.assertFalse(phantom.delete.called)
        real_row.refresh_from_db()
        self.assertEqual(real_row.state, PeerDownstreamBinding.State.ACTIVE)
        self.assertIsNone(real_row.revoked_at)


# ── 跨租户红线（spec §9-S6 / AGENTS.md §4）──────────────────────────────
#: B 的域名与 schema：刻意与 A 不同（`Domain` 上有唯一约束，撞了 `provision_tenant` 会报错）。
PEER_DOMAIN_B = "peer-iso-b.test.local"
PEER_SCHEMA_B = "peer_iso_b"


class PeerCrossTenantTokenTests(TenantTestCase):
    """A 的令牌打在 B 的域名上必须 401（spec §9-S6 / `AGENTS.md` §4）。
    ⚠️ **坑一**：第二个租户**必须在 `setUpClass` 里建**——用例内建 schema 会与外层事务相冲（Django 的 schema DDL 不能在 `TestCase` 的事务里回滚）。⚠️ **坑二**：跨域名的请求**会把连接留在那个租户的 schema 上**（`TenantMainMiddleware` 只在请求进来时 `set_tenant`、**不在响应结束还原**）⇒ 每次跨域名调用都要用 `try/finally` 把连接钉回本租户，`setUp` 第一行也要显式 `set_tenant(self.tenant)`（挡住上一条用例的残留）。**别把这两处还原删掉**。
    ⚠️ **本类抓不到**「手工拼 SQL / 把 schema 固定在别处」的查询路径——如实登记，⛔ 不许读成「跨租户已被完全证明」。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        cls.tenant_b, _ = provision_tenant(
            schema_name=PEER_SCHEMA_B, name="上下游隔离租户 B", domain=PEER_DOMAIN_B
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据；不变量「B 也有 admin 与默认 Group」
        # 不能依赖 schema 恰好是本次运行刚建的 ⇒ 显式再灌一次。
        seed_tenant(cls.tenant_b)

    def setUp(self):
        connection.set_tenant(self.tenant)          # ⚠️ 第一行，别删
        self.account = User.objects.create_user(
            username="peer-account-a", password="peer-password-a"
        )
        self.raw = new_token()
        self.binding = PeerDownstreamBinding.objects.create(
            account=self.account,
            peer_base_url="https://peer-b.test.invalid",
            token_in_hash=hash_token(self.raw),
            token_out=new_token(),
        )

    def _unbind_on(self, domain: str):
        """拿**本租户**的令牌打**指定域名**的 `unbind/`。⚠️ 调用后连接必须钉回来。"""
        try:
            return Client(SERVER_NAME=domain).post(
                UNBIND_URL, {},
                content_type="application/json",
                HTTP_AUTHORIZATION="Bearer " + self.raw,
            )
        finally:
            connection.set_tenant(self.tenant)

    def test_token_is_rejected_on_the_other_tenants_host(self):
        self.assertEqual(self._unbind_on(PEER_DOMAIN_B).status_code, 401)

    def test_the_row_in_this_tenant_is_untouched(self):
        """负向判据打在**数据库行**上（E-0009），⛔ 不看响应文案。"""
        self._unbind_on(PEER_DOMAIN_B)
        row = PeerDownstreamBinding.objects.get()
        self.assertEqual(row.state, PeerDownstreamBinding.State.ACTIVE)
        self.assertIsNone(row.revoked_at)
        self.assertEqual(row.peer_base_url, "https://peer-b.test.invalid")

    def test_the_same_token_works_on_this_tenants_own_host(self):
        """⚠️ **正向对照**（判别力从这里来）：没有它，「401」可能由
        「这个端点对谁都不工作」凑出来（与 `test_account_isolation.py` 的正向对照同一条纪律）。"""
        resp = self._unbind_on(PEER_DOMAIN_A)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)
        self.assertEqual(
            PeerDownstreamBinding.objects.get().state,
            PeerDownstreamBinding.State.REVOKED,
        )

    # ── spec §12.1 #2（红线）：请求参数里的租户标识**一律无效** ──────────────
    #: B 的 schema/主键塞进 query 与 body——「把请求指到别的租户」那条攻击面。
    TENANT_PARAMS = "?tenant=peer_iso_b&schema=peer_iso_b&tenant_id=1"
    TENANT_BODY = {"tenant": PEER_SCHEMA_B, "schema": PEER_SCHEMA_B, "tenant_id": 1}

    def _post_on_a(self, url, body, *, token=None):
        headers = (
            {"HTTP_AUTHORIZATION": "Bearer " + token} if token is not None else {}
        )
        return Client(SERVER_NAME=PEER_DOMAIN_A).post(
            url, json.dumps(body), content_type="application/json", **headers,
        )

    def _assert_b_has_nothing(self, model):
        """B 的 schema 里那个模型**一行都没有**（跨租户写入的判据打在**数据库行**上）。"""
        connection.set_tenant(self.tenant_b)
        try:
            self.assertEqual(model.objects.count(), 0)
        finally:
            connection.set_tenant(self.tenant)

    def test_bind_ignores_tenant_params_in_body_and_query(self):
        """spec §12.1 #2 / §9-S1：`bind/` 里的 `tenant` / `schema` / `tenant_id` 一律无效。
        判别力：本用例在 A 的域名上打 `bind/`，同时把 B 的 schema 塞进 query 与 body。若实现（或将来某个中间件）信了这些参数，落库就会跑到 B，或者认证到 B 里找不到的账号
        ⇒ 本用例的"回 0 + 落在 A + B 一行都没有"立刻红。"""
        body = dict(_bind_payload(
            peer_base_url="https://peer-after-tenant-params.test.invalid"
        ))
        body.update(self.TENANT_BODY)
        resp = self._post_on_a(BIND_URL + self.TENANT_PARAMS, body)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        # 写的是 **A** 里那一行（`update_or_create(account=…)` 的 update 支路）。
        row = PeerDownstreamBinding.objects.get(account=self.account)
        self.assertEqual(
            row.peer_base_url, "https://peer-after-tenant-params.test.invalid"
        )
        self._assert_b_has_nothing(PeerDownstreamBinding)

    def test_unbind_ignores_tenant_params_in_body_and_query(self):
        """spec §12.1 #2 / §9-S1：`unbind/` 同样不认租户参数（令牌在本租户命中才算）。"""
        resp = self._post_on_a(
            UNBIND_URL + self.TENANT_PARAMS, dict(self.TENANT_BODY), token=self.raw
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.state, PeerDownstreamBinding.State.REVOKED)
        self._assert_b_has_nothing(PeerDownstreamBinding)

    def test_inbound_events_ignore_tenant_params_in_body_and_query(self):
        """spec §12.1 #2 / §9-S1：收单端点也不认租户参数——单子必须落在**本租户**。"""
        raw_up = new_token()
        PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(raw_up),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.account,
        )
        game = Game.objects.create(name="隔离用例游戏")
        body = {
            "events": [{
                "idempotency_key": uuid.uuid4().hex,
                "kind": "order.assign",
                "target_ref": "ISO-1",
                "payload": {
                    "status": OrderStatus.IN_PROGRESS,
                    "game_name": game.name,
                },
            }],
            **self.TENANT_BODY,
        }
        resp = self._post_on_a(
            E2E_INBOUND_URL + self.TENANT_PARAMS, body, token=raw_up
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        self.assertEqual(resp.json()["data"]["applied"], 1, resp.content[:200])
        # 单子落在 A（本租户），B 里没有 Order。
        self.assertEqual(Order.objects.filter(source_order_no="ISO-1").count(), 1)
        self._assert_b_has_nothing(Order)


# ── 页面三端点（spec §8.4）──────────────────────────────────────
#: ⚠️ 三条路径都带尾斜杠（spec §8 的端点表；`AGENTS.md` 的项目约定）。
PAGE_URL = "/api/business/peer-bindings/"
#: 我方域名（`local_base_url`）由 `request.scheme` + `request.get_host()` 算出。⚠️ Django 测试 `Client` 的默认 scheme 是 `http` ⇒ 这个字面量**钉住**它，免得把 `https` 当默认值写进期望（那会让断言恒假，看起来像实现错）。
#: ⚠️ **必须配 `override_settings(PEER_SYNC_LOCAL_BASE_URL="")`**：`_local_base_url` 自裁决 R12 起是**配置优先**，本机 `.env` 若正确设了 `PEER_SYNC_LOCAL_BASE_URL=https://…`，请求推导这一支根本不会跑 ⇒ 这两条断言会在**代码正确**的机器上红。
#: 下面 `PeerBindingPageTests` 的类级 `override_settings` 把配置清空，专门把"请求推导"这一支钉住。
PAGE_LOCAL_BASE_URL = f"http://{PEER_DOMAIN_A}"


def _page_payload(**over):
    """页面弹窗那三个字段（对标 `homeCompany` / `homeUrl` / `homeAcc` + 密码）。"""
    data = {
        "peer_name": "测试工作室甲",
        "peer_base_url": "https://peer-a.test.invalid",
        "peer_account": "peer-account-a",
        "password": "peer-password-a",
    }
    data.update(over)
    return data


@override_settings(PEER_SYNC_LOCAL_BASE_URL="")
class PeerBindingPageTests(TenantTestCase):
    """页面三端点（spec §8.4）：`GET` / `POST` 集合 + `DELETE` 明细。
    ⚠️ **不用 `force_login`（实测会 401）**：`force_login` 产出的会话里没有 `identity_plane`，被 `TenantBoundAuthentication._assert_plane` 按 fail-closed 拒掉（`tests/p5_auth_helpers.py` 的说明）。页面端点走**全局默认的会话认证** ⇒ 必须用真登录夹具 `make_logged_in_client`；本文件 `PeerAuthAtHttpLayerTests` 的 `test_session_cookie_is_rejected` 就是「force_login 会话不成立」这条事实的对照。
    ⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里（文件头同款理由）。"""

    def setUp(self):
        cache.clear()
        # ⚠️ **必须显式给节点**（裁决 R22 之后这两个端点有节点门）：`make_logged_in_client`
        # 的 `nodes` 默认空集 ⇒ 不给就会 403（fail-closed），看起来像端点坏了。
        self.client = make_logged_in_client(
            domain=PEER_DOMAIN_A, username="kefu-a", password="pw-kefu-a",
            # ⚠️ 必须带一个 `groups`：`_grant_nodes` 是按"用户所在的组"挂节点的，
            # 一个组都没有时**节点挂不上去**（症状就是 403，看起来像判据写错）。
            groups=("客服",), nodes=(PAGE_BINDING_NODE,),
        )
        self.user = User.objects.get(username="kefu-a")

    def _post(self, payload):
        return self.client.post(
            PAGE_URL, json.dumps(payload), content_type="application/json"
        )

    def _detail(self, pk) -> str:
        return f"{PAGE_URL}{pk}/"

    def _make_row(self, **over):
        data = {
            "peer_name": "测试工作室甲",
            "peer_base_url": "https://peer-a.test.invalid",
            "peer_account": "peer-account-a",
            "password": "peer-password-a",
            "token_in_hash": hash_token(new_token()),
            "token_out": "TOKEN-OUT-EXISTING",
            "local_base_url": PAGE_LOCAL_BASE_URL,
            "bound_by": self.user,
        }
        data.update(over)
        return PeerUpstreamBinding.objects.create(**data)

    # ── GET 列表 ───────────────────────────────────────────────────────
    def test_list_is_empty_at_first(self):
        resp = self.client.get(PAGE_URL)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["data"], {"items": [], "total": 0})

    def test_list_returns_the_rows_and_hides_soft_deleted_ones(self):
        alive = self._make_row(peer_name="在绑的工作室")
        gone = self._make_row(peer_name="已解绑的工作室", deleted_at=timezone.now())

        data = self.client.get(PAGE_URL).json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual([item["id"] for item in data["items"]], [alive.pk])
        self.assertNotIn(gone.pk, [item["id"] for item in data["items"]])

    def test_list_payload_key_set_is_exactly_the_contract_literal_names(self):
        """出参键集**恰好**是 `PEER_BINDING_CONTRACT_FIELDS`（页面契约 + 最初两格）。
        ⚠️ 判别力来自**不派生**期望值：`sorted(item) == sorted(PEER_BINDING_CONTRACT_FIELDS)` 里右边是字面列表 ⇒ 加字段 / 删字段 / 改名（例如把 `bound_by_name` 写成 `operator_name`）都会红（E-0024）。"""
        self._make_row()
        item = self.client.get(PAGE_URL).json()["data"]["items"][0]
        self.assertEqual(sorted(item.keys()), sorted(PEER_BINDING_CONTRACT_FIELDS))

    def test_list_shows_the_operator_display_name_not_the_pk(self):
        """对标第 4 列「绑定操作人」显示的是**人**。
        ⛔ 旧实现把 `bound_by`（FK 主键）直接渲染成 `3`——本用例同时钉住 `bound_by`（id）**仍在**载荷里（别的消费方按 id 取），而页面那一列读的是 `bound_by_name`。"""
        self.user.first_name = "测试客服甲"
        self.user.save(update_fields=["first_name"])
        self._make_row()

        item = self.client.get(PAGE_URL).json()["data"]["items"][0]
        self.assertEqual(item["bound_by_name"], "测试客服甲")
        # 对照：主键仍在、且与显示名不同（否则上面那条在"名字恰好等于 pk 的字符串"上也成立）。
        self.assertEqual(item["bound_by"], self.user.pk)
        self.assertNotEqual(item["bound_by_name"], str(item["bound_by"]))

    def test_bound_by_name_falls_back_to_the_login_account_when_nickname_is_empty(self):
        """`first_name` 空 ⇒ 回落登录账号（全仓「怎么显示一个人」的既有口径）。"""
        self.user.first_name = ""
        self.user.save(update_fields=["first_name"])
        self._make_row()

        item = self.client.get(PAGE_URL).json()["data"]["items"][0]
        self.assertEqual(item["bound_by_name"], "kefu-a")

    def test_list_never_exposes_the_password_or_the_tokens(self):
        """spec §9-S4：`password` 与两个令牌**永不回显**；页面只读布尔 `password_set`。"""
        self._make_row(password="peer-password-a", token_out="TOKEN-OUT-EXISTING")
        body = self.client.get(PAGE_URL).content.decode()
        self.assertNotIn("peer-password-a", body)
        self.assertNotIn("TOKEN-OUT-EXISTING", body)
        self.assertNotIn("token_in_hash", body)
        self.assertNotIn("token_out", body)
        # ⚠️ 正向对照：列表**确实**回了这一行（否则上面五条 `assertNotIn` 在空列表上也成立）。
        self.assertIn("测试工作室甲", body)

    def test_list_never_exposes_the_receipt_query_password_either(self):
        """D-0359：`receipt_query_password` 与它的键名**永不回显**；只回布尔
        `receipt_query_password_set`（与握手密码同一套口径）。"""
        row = self._make_row()
        row.receipt_query_password = "receipt-query-secret-1"
        row.receipt_query_password_set = True
        row.save(update_fields=["receipt_query_password", "receipt_query_password_set"])
        body = self.client.get(PAGE_URL).content.decode()
        self.assertNotIn("receipt-query-secret-1", body)
        # 键名本身也不出现（出现键名 = 序列化器把明文列挂出去了）。
        self.assertNotIn("receipt_query_password\"", body)
        self.assertIn("receipt_query_password_set", body)

    # ── POST 绑定 ──────────────────────────────────────────────────────
    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_create_calls_upstream_and_stores_the_row(self, bind_upstream):
        resp = self._post(_page_payload())
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)
        # ⚠️ 桩打在**被查找的那个模块**上（`AGENTS.md` §5-E / E-0029），并钉住调用形状：
        #    只断言 `.called` 的话，把 `account` / `local_base_url` 传错也不会红。
        bind_upstream.assert_called_once()
        args, kwargs = bind_upstream.call_args
        self.assertEqual(args, ("https://peer-a.test.invalid",))
        self.assertEqual(kwargs["account"], "peer-account-a")
        self.assertEqual(kwargs["password"], "peer-password-a")
        self.assertEqual(kwargs["local_base_url"], PAGE_LOCAL_BASE_URL)
        row = PeerUpstreamBinding.objects.get()
        # `token_in_hash` 是上家**回给我的那套明文**（`token_a`）的哈希 —— 上家推单时带它、
        # 我只验证 ⇒ 存哈希（spec §6.1 / 裁决 R15）。
        self.assertEqual(row.token_in_hash, hash_token("TOKEN-UP"))
        self.assertNotEqual(row.token_in_hash, "TOKEN-UP")
        # `token_out` 是**我方生成、交给上家**的那套（`token_b`）⇒ 解绑要原样外发 ⇒ 明文存。
        self.assertEqual(row.token_out, kwargs["token_out"])
        self.assertNotEqual(row.token_out, hash_token(kwargs["token_out"]))
        self.assertEqual(row.bound_by_id, self.user.pk)
        self.assertEqual(row.peer_name, "测试工作室甲")
        self.assertEqual(row.peer_base_url, "https://peer-a.test.invalid")
        self.assertEqual(row.local_base_url, PAGE_LOCAL_BASE_URL)

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_create_does_not_echo_the_password_or_any_token(self, bind_upstream):
        resp = self._post(_page_payload())
        body = resp.content.decode()
        self.assertNotIn("peer-password-a", body)
        # `TOKEN-UP`（上家回的那套 `token_a`）与 `token_out`（我生成的那套 `token_b`）都不许回显。
        self.assertNotIn("TOKEN-UP", body)
        self.assertNotIn(bind_upstream.call_args.kwargs["token_out"], body)
        self.assertNotIn("token_in_hash", body)
        self.assertNotIn("token_out", body)
        # 正向对照：这一行**确实**落库了（否则上面几条在"什么都没建"上也成立）。
        self.assertEqual(PeerUpstreamBinding.objects.count(), 1)

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_binding_the_same_account_on_a_new_host_hints_the_stale_binding(self, _bind):
        """⭐ 同账号**换了域名**再绑 ⇒ 响应带旧绑定提示（只提示不阻断）。
        病根：上家换址 = 新绑定，旧域名的绑定行照活，名下 `listed` 的卡从此「可见 → 点了失败」。提示让人知道该去解绑旧的。"""
        old = self._make_row(peer_base_url="https://peer-old.test.invalid")
        resp = self._post(_page_payload(peer_base_url="https://peer-new.test.invalid"))
        self.assertEqual(resp.json()["code"], 0, resp.content[:300])
        hint = resp.json()["data"].get("stale_binding_hint")
        self.assertIn("https://peer-old.test.invalid", hint)
        self.assertIn("peer-account-a", hint)
        old.refresh_from_db()
        self.assertIsNone(old.deleted_at)   # ⛔ 只提示不阻断：旧行一个字节没动
        self.assertEqual(PeerUpstreamBinding.objects.count(), 2)

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_a_binding_without_an_old_same_account_row_has_no_hint(self, _bind):
        """没有同账号旧绑定 ⇒ 响应**不带**提示键（幂等重绑复用本行同样没有）。"""
        resp = self._post(_page_payload())
        self.assertEqual(resp.json()["code"], 0, resp.content[:300])
        self.assertNotIn("stale_binding_hint", resp.json()["data"])
        # 幂等重绑（同域名同账号）走「复用本行」那条路 ⇒ exclude(pk=row.pk) 之后无旧行。
        second = self._post(_page_payload()).json()["data"]
        self.assertNotIn("stale_binding_hint", second)

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_binding_the_same_upstream_and_account_twice_reuses_the_row(self, bind_upstream):
        """⭐ 裁决 R23：同一个 `(上家域名, 子账号)` 再绑一次 ⇒ **刷新那一行**，⛔ 不留幽灵行。
        判别力：旧实现（`objects.create`）跑完这里会是 **2 行**，第二行的令牌上家已不认（上家侧同一个握手是 `update_or_create(account=…)`，只记最新那一套）⇒ 页面上两行都显示"生效"，其中一行**永远收不到单**——正是验收清单点名当 bug 的形态。"""
        first = self._post(_page_payload())
        self.assertEqual(first.json()["code"], 0)
        row = PeerUpstreamBinding.objects.get()
        first_token_out = row.token_out
        first_bound_at = row.bound_at

        second = self._post(_page_payload(peer_name="改了名字的上家"))
        self.assertEqual(second.json()["code"], 0)
        self.assertEqual(PeerUpstreamBinding.objects.count(), 1)      # ⛔ 不是两行
        row.refresh_from_db()
        self.assertEqual(row.peer_name, "改了名字的上家")              # 配置刷新了
        self.assertNotEqual(row.token_out, first_token_out)           # 令牌轮换
        self.assertEqual(row.token_in_hash, hash_token("TOKEN-UP"))
        # ⚠️ 如实登记：`bound_at` 是 `auto_now_add`（本来就不可刷新），`bound_by` **有意不刷新**
        #    （见视图里那段注释）；两次绑定各留一条操作日志（第二行没有"幽灵"，但动作有留痕）。
        self.assertEqual(row.bound_at, first_bound_at)
        self.assertEqual(
            OperationLog.objects.filter(action="system.peer_binding_create").count(), 2
        )

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_rebinding_resets_the_inbound_failure_counters(self, bind_upstream):
        """⭐ 页面这条幂等重绑也要复位**入站**失败台账。
        那几个列由接收侧的硬跳喂起来（`_record_inbound_health`），语义是"对面推来的东西我处理不了" ⇒ 重绑 = 换了个对端重新开始，旧错误不该继续挂在页面上。
        判别力：视图里去掉那三行复位 ⇒ 本用例红（计数停在 10、`last_error` 还在）。"""
        self._make_row(
            consecutive_failures=10,
            last_error="上家推来的这批事件共 1 条，有 1 条没能接收；第一条原因：测试原因",
            last_error_at=timezone.now(),
        )

        resp = self._post(_page_payload())
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)

        row = PeerUpstreamBinding.objects.get()
        self.assertEqual(row.consecutive_failures, 0)
        self.assertEqual(row.last_error, "")
        self.assertIsNone(row.last_error_at)

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_a_soft_deleted_binding_is_not_reused(self, bind_upstream):
        """阴性对照：**已解绑**的行不复用（否则解绑等于没解绑、那行会"复活"）。"""
        self._make_row(deleted_at=timezone.now())
        self.assertEqual(self._post(_page_payload()).json()["code"], 0)
        self.assertEqual(PeerUpstreamBinding.objects.count(), 2)
        alive = PeerUpstreamBinding.objects.filter(deleted_at__isnull=True)
        self.assertEqual(alive.count(), 1)
        self.assertEqual(alive.get().peer_account, "peer-account-a")

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_a_different_account_on_the_same_upstream_is_rejected_by_default(self, _bind):
        """⭐ 同 host 双活**默认拒绝**（旧口径允许第二行，已废）。镜像行幂等键是 `(域名, 单号)`、不含绑定 ⇒ 同域名两条活绑定会互改写 / 接管对方的卡。
        拒绝文案必须指向旧绑定的子账号（人能照着去找），且旧绑定一个字节都不动。"""
        self._make_row()  # 同域名、账号 peer-account-a，活的
        resp = self._post(_page_payload(peer_account="peer-account-b"))
        body = resp.json()
        self.assertEqual(body["code"], 1001, body)
        self.assertIn("peer-account-a", body["msg"])
        # ⛔ 一个字节的写都不发生：没有新行、旧行原样（握手都没有——拦截在出站调用之前）。
        self.assertEqual(PeerUpstreamBinding.objects.count(), 1)
        self.assertIsNone(PeerUpstreamBinding.objects.get().deleted_at)
        _bind.assert_not_called()

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP-2")
    def test_replace_true_stops_the_old_binding_and_binds_the_new_one(self, bind_upstream):
        """⭐ 显式 `replace=true` ⇒ **原子停旧建新**（软删旧行 + 新行活 + 令牌轮换）。
        判据打库：旧行 `deleted_at` 落值、新行 `(域名, 新账号)` 唯一活行、新行的 `token_in_hash` 来自**这一次**握手的应答（令牌轮换，⛔ 不是沿用旧的）。"""
        old = self._make_row()
        resp = self._post(_page_payload(
            peer_account="peer-account-b", replace=True,
        ))
        self.assertEqual(resp.json()["code"], 0, resp.json())
        rows = PeerUpstreamBinding.objects.order_by("pk")
        self.assertEqual(rows.count(), 2)
        old.refresh_from_db()
        self.assertIsNotNone(old.deleted_at)                     # 旧的是停了，不是还在
        alive = rows.filter(deleted_at__isnull=True).get()
        self.assertEqual(alive.peer_account, "peer-account-b")
        self.assertEqual(alive.token_in_hash, hash_token("TOKEN-UP-2"))
        # 第二次握手真的对**新账号**发生了一次（停旧建新不是只改库）。
        self.assertEqual(bind_upstream.call_args.kwargs["account"], "peer-account-b")

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP-2")
    def test_replace_also_unlists_the_old_bindings_hall_cards(self, _bind):
        """⭐ C7 的「停旧」与页面 DELETE **同一份口径**：旧绑定名下的卡下架（listed → gone）。"""
        old = self._make_row()
        card = PeerHallMirror.objects.create(
            peer_upstream_binding=old,
            peer_base_url=old.peer_base_url,
            peer_order_no="SYN-C7-REPLACE-CARD-1",
            status=PeerHallMirror.Status.LISTED,
            first_seen_at=timezone.now(),
        )
        resp = self._post(_page_payload(peer_account="peer-account-b", replace=True))
        self.assertEqual(resp.json()["code"], 0, resp.json())
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.GONE)

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_a_soft_deleted_conflict_does_not_block_a_new_binding(self, _bind):
        """阴性对照：同 host 的**已解绑**行不算冲突（否则解绑之后永远绑不回来）。"""
        self._make_row(deleted_at=timezone.now())
        self.assertEqual(self._post(_page_payload()).json()["code"], 0)
        self.assertEqual(PeerUpstreamBinding.objects.count(), 2)

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_binding_over_plain_http_is_rejected_before_any_outbound_call(self, bind_upstream):
        """⭐ C2（Z22）：页面上新绑 `http://` ⇒ 拒绝 + 文案指向 https + ⛔ 不出站、不落库。"""
        resp = self._post(_page_payload(peer_base_url="http://peer-a.test.invalid"))
        body = resp.json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("https", body["msg"])
        bind_upstream.assert_not_called()
        self.assertEqual(PeerUpstreamBinding.objects.count(), 0)

    def test_uses_insecure_http_flags_only_legacy_http_rows(self):
        """⭐ C2：存量风险标志只读——`http://` 老行 `true`、https 行 `false`（存量不打断）。"""
        legacy = self._make_row(peer_base_url="http://legacy.test.invalid")
        secure = self._make_row(peer_base_url="https://secure.test.invalid")
        items = self.client.get(PAGE_URL).json()["data"]["items"]
        flags = {item["id"]: item["uses_insecure_http"] for item in items}
        self.assertTrue(flags[legacy.pk])
        self.assertFalse(flags[secure.pk])

    @mock.patch("apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP")
    def test_a_bound_row_keeps_no_plaintext_password(self, bind_upstream):
        """⭐ 绑定成功后**库里无明文**——列空、布尔列置位（验完即弃）。
        判别力：把视图里「验完即弃」那两行删掉（改回 `row.password = payload[...]`），第一条断言当场红。"""
        bind_upstream.return_value = "TOKEN-UP"
        resp = self._post(_page_payload())
        self.assertEqual(resp.json()["code"], 0, resp.json())
        row = PeerUpstreamBinding.objects.get()
        self.assertEqual(row.password, "")
        self.assertTrue(row.password_set)
        # ⛔ 握手确实收到了密码（不是序列化器漏了它——否则上面两条在"没绑成"上也成立）。
        self.assertEqual(bind_upstream.call_args.kwargs["password"], "peer-password-a")

    def test_password_set_reads_the_boolean_column_not_the_plaintext(self):
        """⭐ C1：`password_set` 读**布尔列**——明文列不再是判据（迁移后的存量形状两态都钉）。"""
        # 迁移后的存量行：明文已清空、布尔在位 ⇒ 显示 true。
        migrated = self._make_row(password="", password_set=True)
        # 「迁移没跑过」的残行（明文还在、布尔没置）⇒ 显示 false（如实暴露，不再装）。
        legacy = self._make_row(password="legacy-plaintext", password_set=False)
        items = {
            item["id"]: item["password_set"]
            for item in self.client.get(PAGE_URL).json()["data"]["items"]
        }
        self.assertTrue(items[migrated.pk])
        self.assertFalse(items[legacy.pk])

    def test_create_writes_one_operation_log_row(self):
        """页面写端点必须留痕（`INSTRUMENTED` 登记的另一半：埋点本身在真出口上）。"""
        with mock.patch(
            "apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP"
        ):
            self._post(_page_payload())
        row = PeerUpstreamBinding.objects.get()
        logs = OperationLog.objects.filter(action="system.peer_binding_create")
        self.assertEqual(logs.count(), 1)
        log = logs.get()
        self.assertEqual(log.actor, "kefu-a")
        self.assertEqual(log.target_kind, "peer_binding")
        self.assertEqual(log.target_id, str(row.pk))
        self.assertEqual(log.target_name, "测试工作室甲")
        # 40 = 系统相关（与同组 `system.*` 一致）；字面值，不从 `LogType` 派生（E-0024）。
        self.assertEqual(log.log_type, 40)
        self.assertEqual(log.summary, "kefu-a绑定了上家系统测试工作室甲")

    @mock.patch(
        "apps.business.api.peer_views.bind_upstream",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_unreachable_upstream_is_a_business_error_not_a_500(self, _):
        resp = self._post(_page_payload())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 1021)
        self.assertEqual(PeerUpstreamBinding.objects.count(), 0)
        self.assertEqual(OperationLog.objects.count(), 0)

    @mock.patch(
        "apps.business.api.peer_views.bind_upstream",
        side_effect=PeerRejected("账号或密码不对"),
    )
    def test_rejected_upstream_is_a_business_error_not_a_500(self, _):
        resp = self._post(_page_payload())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 1020)
        self.assertEqual(PeerUpstreamBinding.objects.count(), 0)

    def test_private_base_url_is_rejected_before_any_outbound_call(self):
        """SSRF 闸门（spec §9-S9）：预期失败翻业务码，且**一笔请求都不许发出去**。"""
        with mock.patch("apps.business.api.peer_views.bind_upstream") as bind_upstream:
            resp = self._post(_page_payload(peer_base_url="http://127.0.0.1:8000"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 1000)
        self.assertTrue(resp.json()["msg"])
        bind_upstream.assert_not_called()
        self.assertFalse(PeerUpstreamBinding.objects.exists())

    def test_malformed_base_url_never_echoes_the_stdlib_text(self):
        """URL 闸门抛的是**自有中文**（`BASE_URL_MALFORMED_MSG` 一族）。
        本端点会把 `str(exc)` 当 `msg` **回显**——这正是把 `urlsplit` / `.port` 的英文原文在闸门内翻掉的原因（spec §14-R7：不回显框架原文）。三条实测原文在这里一条都不许出现。"""
        for bad, stdlib_phrasing in (
            ("http://example.com:abc", "Port could not be cast"),
            ("http://example.com:99999", "Port out of range"),
            ("http://[::1", "Invalid IPv6 URL"),
        ):
            with self.subTest(bad=bad):
                with mock.patch(
                    "apps.business.api.peer_views.bind_upstream"
                ) as bind_upstream:
                    resp = self._post(_page_payload(peer_base_url=bad))
                body = resp.json()
                self.assertEqual(resp.status_code, 200, resp.content[:200])
                self.assertEqual(body["code"], 1000)
                self.assertTrue(body["msg"])
                self.assertNotIn(stdlib_phrasing, body["msg"])
                self.assertNotIn("Port", body["msg"])
                bind_upstream.assert_not_called()

    def test_a_missing_field_is_a_validation_error_not_a_500(self):
        payload = _page_payload()
        del payload["password"]
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 1000)
        self.assertFalse(PeerUpstreamBinding.objects.exists())

    # ── GET/PUT 明细：大厅镜像 P1 的两格 ─────────────────────────────────
    def _put(self, pk, payload):
        # ⚠️ PUT 走 `request()` 的 JSON 通道；这里直接用 Django test client 的
        # `put`（与 DELETE 同一条会话认证 + 节点门）。
        return self.client.put(
            self._detail(pk), json.dumps(payload), content_type="application/json"
        )

    def test_detail_get_returns_the_two_hall_keys_for_an_old_row(self):
        """老行（两列上线前建的）回 `false` / `100`，⛔ 不是 `null`——编辑弹窗靠它回填。"""
        row = self._make_row()
        resp = self.client.get(self._detail(row.pk))
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(sorted(body["data"].keys()), sorted([*PEER_BINDING_CONTRACT_FIELDS, "game_rules", "direct_taker_assignment_enabled"]))
        self.assertIs(body["data"]["hall_sync_enabled"], False)
        self.assertEqual(body["data"]["hall_price_percent"], 100)

    def test_put_updates_the_two_hall_keys(self):
        row = self._make_row()
        resp = self._put(row.pk, {"hall_sync_enabled": True, "hall_price_percent": 90})
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        self.assertIs(body["data"]["hall_sync_enabled"], True)
        self.assertEqual(body["data"]["hall_price_percent"], 90)
        row.refresh_from_db()
        self.assertIs(row.hall_sync_enabled, True)
        self.assertEqual(row.hall_price_percent, 90)

    def test_put_leaves_the_missing_key_alone(self):
        """两格各自独立：只给百分比 ⇒ 开关保持库里现值（⛔ 不许被打回默认）。"""
        row = self._make_row(hall_sync_enabled=True, hall_price_percent=80)
        resp = self._put(row.pk, {"hall_price_percent": 60})
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        row.refresh_from_db()
        self.assertIs(row.hall_sync_enabled, True)
        self.assertEqual(row.hall_price_percent, 60)

    def test_put_out_of_range_is_a_validation_error(self):
        """1–100 之外 ⇒ 业务码 `1000`（⛔ 不是 500），且**两列都不动**。"""
        row = self._make_row(hall_sync_enabled=True, hall_price_percent=90)
        for bad in (0, 101, -5, "abc", 90.5):
            with self.subTest(bad=bad):
                resp = self._put(row.pk, {"hall_price_percent": bad})
                body = resp.json()
                self.assertEqual(resp.status_code, 200, resp.content[:200])
                self.assertEqual(body["code"], 1000, body)
                row.refresh_from_db()
                self.assertEqual(row.hall_price_percent, 90)
                self.assertIs(row.hall_sync_enabled, True)

    def test_put_only_recognises_the_two_keys(self):
        """⛔ 请求体里别的键一律忽略（凭据 / 绑定关系 / 健康列一个都改不到）。"""
        row = self._make_row()
        resp = self._put(
            row.pk,
            {
                "peer_name": "被改名的上家",
                "peer_base_url": "https://evil.test.invalid",
                "peer_account": "evil-account",
                "password": "evil-password",
                "consecutive_failures": 99,
                "hall_sync_enabled": True,
            },
        )
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        row.refresh_from_db()
        self.assertEqual(row.peer_name, "测试工作室甲")
        self.assertEqual(row.peer_base_url, "https://peer-a.test.invalid")
        self.assertEqual(row.peer_account, "peer-account-a")
        self.assertEqual(row.password, "peer-password-a")
        self.assertEqual(row.consecutive_failures, 0)
        # 正向对照：认得的那一格**真的**写进去了（否则上面五条在「什么都没写」上也成立）。
        self.assertIs(row.hall_sync_enabled, True)

    # ── ⭐ D-0359：收款查询凭据与预检标记 ────────────────────
    def test_put_sets_the_receipt_query_password_and_reports_only_the_boolean(self):
        """非空 `receipt_query_password` ⇒ 落列 + `receipt_query_password_set=True`；
        响应只有布尔、明文与键名都不出现（write_only，照握手密码同一套口径）。"""
        row = self._make_row()
        resp = self._put(row.pk, {"receipt_query_password": "receipt-query-secret-2"})
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        body = resp.content.decode()
        self.assertNotIn("receipt-query-secret-2", body)
        self.assertNotIn("receipt_query_password\"", body)
        self.assertIs(body and resp.json()["data"]["receipt_query_password_set"], True)
        row.refresh_from_db()
        self.assertEqual(row.receipt_query_password, "receipt-query-secret-2")
        self.assertIs(row.receipt_query_password_set, True)

    def test_put_blank_and_missing_receipt_query_password_mean_keep(self):
        """「空串 = 不改 / 缺键 = 不改」（照 `_BLANK_MEANS_KEEP` 先例——一次没带值的 PUT
        不得静默抹掉配好的密码，E-0027 家族）。⛔ 没有清空入口（停用绑定才是关闭口）。"""
        row = self._make_row(receipt_query_password="keep-me-1", receipt_query_password_set=True)
        for payload in ({}, {"receipt_query_password": ""}):
            with self.subTest(payload=payload):
                resp = self._put(row.pk, payload)
                self.assertEqual(resp.json()["code"], 0, resp.content[:200])
                row.refresh_from_db()
                self.assertEqual(row.receipt_query_password, "keep-me-1")
                self.assertIs(row.receipt_query_password_set, True)

    def test_put_toggles_the_receipt_query_precheck_marker(self):
        """`receipt_query_precheck_ok` 显式给了才改（True/False 都可写）；
        缺键不动；它**不**顺手改密码布尔（两格各自独立）。"""
        row = self._make_row()
        resp = self._put(row.pk, {"receipt_query_precheck_ok": True})
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        row.refresh_from_db()
        self.assertIs(row.receipt_query_precheck_ok, True)
        self.assertIs(row.receipt_query_password_set, False)

        resp = self._put(row.pk, {"receipt_query_precheck_ok": False})
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        row.refresh_from_db()
        self.assertIs(row.receipt_query_precheck_ok, False)

        resp = self._put(row.pk, {"hall_sync_enabled": True})
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        row.refresh_from_db()
        self.assertIs(row.receipt_query_precheck_ok, False)

    def test_put_receipt_query_password_never_leaks_into_the_log(self):
        """留痕句只说「密码已更新」，⛔ 明文进操作日志（凭据红线，照握手密码同一档）。"""
        row = self._make_row()
        resp = self._put(row.pk, {"receipt_query_password": "receipt-query-secret-3"})
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        from apps.audit.models import OperationLog
        logs = "\n".join(
            row.summary + row.to_value
            for row in OperationLog.objects.filter(target_kind="peer_binding")
        )
        self.assertNotIn("receipt-query-secret-3", logs)

    # ── ⭐ N5（R3-M6）：改百分比沿隐藏单 / 下家传导 ─────────────────────
    def _make_listed_card(self, binding, no, *, status=PeerHallMirror.Status.LISTED):
        game, _ = Game.objects.get_or_create(name="N5 百分比传导游戏")
        return PeerHallMirror.objects.create(
            peer_upstream_binding=binding,
            peer_base_url=binding.peer_base_url,
            peer_order_no=no,
            status=status,
            game=game,
            payload={"game_name": game.name, "hall_price": "50.00"},
            remote_hall_price=Decimal("50.00"),
            first_seen_at=timezone.now(),
            last_seen_at=timezone.now(),
        )

    def test_percent_change_propagates_to_every_listed_hidden_order(self):
        """⭐ 改百分比 ⇒ 该绑定名下**全部 `listed` 卡**的隐藏单都更新、已转正的不动、`gone` 的没有单可动；每张卡各转发一条 `hall.reprice`。
        ⚠️ 触发条件是「**百分比真的变了**」：只把 `hall_sync_enabled` 再写一次（`:587-596` 那个 False→True 支会多写两列）⇒ 零隐藏单写入、零 reprice 事件。"""
        from apps.business import hall_shadow
        from apps.business.peer_sync import HALL_REPRICE_KIND

        row = self._make_row(hall_sync_enabled=True, hall_price_percent=90)
        PeerDownstreamBinding.objects.create(
            account=self.user,
            peer_base_url="https://peer-down-price.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            hall_push_enabled=True,
        )
        listed = self._make_listed_card(row, "N5-PCT-1")
        hidden = hall_shadow.ensure_for_card(listed, now=timezone.now())
        self.assertIsNotNone(hidden)
        self.assertEqual(hidden.hall_price, Decimal("45.00"))
        in_progress_card = self._make_listed_card(row, "N5-PCT-2")
        in_progress = hall_shadow.ensure_for_card(
            in_progress_card, now=timezone.now()
        )
        Order.objects.filter(pk=in_progress.pk).update(
            status=OrderStatus.IN_PROGRESS
        )
        gone_card = self._make_listed_card(
            row, "N5-PCT-3", status=PeerHallMirror.Status.GONE
        )
        self.assertIsNone(
            hall_shadow.ensure_for_card(gone_card, now=timezone.now())
        )
        old = timezone.now() - timedelta(days=1)
        Order.objects.filter(pk__in=(hidden.pk, in_progress.pk)).update(
            updated_at=old
        )

        resp = self._put(row.pk, {"hall_price_percent": 80})
        self.assertEqual(resp.json()["code"], 0, resp.content[:300])
        hidden.refresh_from_db()
        self.assertEqual(hidden.hall_price, Decimal("40.00"))
        self.assertGreater(hidden.updated_at, old)
        in_progress.refresh_from_db()
        self.assertEqual(in_progress.hall_price, Decimal("45.00"))
        self.assertEqual(in_progress.updated_at, old, "已转正的隐藏单被改了")
        self.assertFalse(
            Order.objects.filter(hall_card=gone_card).exists(),
            "gone 的卡不该有隐藏单可写",
        )
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind=HALL_REPRICE_KIND).count(),
            2,
            "每张 listed 卡都应转发一条 hall.reprice（已转正的单不额外写）",
        )

        # ⭐ 触发条件：这一次保存百分比没变（只提交同一个开关值）⇒ 零隐藏单写入、
        # 零 reprice 事件。若实现拿「保存了一次」当触发，这一半当场红。
        before_events = PeerSyncEvent.objects.filter(kind=HALL_REPRICE_KIND).count()
        before_updated = hidden.updated_at
        resp = self._put(row.pk, {"hall_sync_enabled": True})
        self.assertEqual(resp.json()["code"], 0, resp.content[:300])
        hidden.refresh_from_db()
        self.assertEqual(hidden.updated_at, before_updated)
        self.assertEqual(hidden.hall_price, Decimal("40.00"))
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind=HALL_REPRICE_KIND).count(),
            before_events,
            "百分比没变却凭空给下家转发 reprice",
        )

    def test_put_unknown_pk_is_a_business_error(self):
        resp = self._put(999999, {"hall_price_percent": 90})
        body = resp.json()
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "这个绑定不存在")

    def test_detail_get_unknown_pk_is_a_business_error(self):
        resp = self.client.get(self._detail(999999))
        body = resp.json()
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "这个绑定不存在")

    # ── DELETE 解绑 ────────────────────────────────────────────────────
    @mock.patch("apps.business.api.peer_views.unbind_upstream")
    def test_delete_notifies_upstream_and_soft_deletes_locally(self, unbind_upstream):
        row = self._make_row(token_out="TOKEN-OUT-EXISTING")
        resp = self.client.delete(self._detail(row.pk))
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)
        # ⚠️ 通知上家带的是**我方生成、交给上家的那套明文**（`token_out` = `token_b`）——
        # 上家按 `hash(token_b)` 验（spec §5.2 第 2 步 / 裁决 R15）。
        # ⛔ 带 `token_in_hash` 那一列（上家自己发的 `token_a`）上家验不了。
        unbind_upstream.assert_called_once_with(
            "https://peer-a.test.invalid", "TOKEN-OUT-EXISTING"
        )
        row.refresh_from_db()
        self.assertIsNotNone(row.deleted_at)          # 软删：留痕，不删行
        self.assertEqual(PeerUpstreamBinding.objects.count(), 1)

    @mock.patch("apps.business.api.peer_views.unbind_upstream")
    def test_delete_returns_unconfirmed_claims_to_the_hall(self, _unbind):
        """⭐ 解绑 ⇒ 这份绑定名下 `claiming` 的卡退回大厅。
        判据三点：① 状态回 `listed`（⚠️ ⛔ 不是 `gone`：抢单没确认时上家那张单可能还在厅，`gone` 会误杀真单）；② 四个接单列清空 + 一句人话；③ **一条**台账（人话 + 卡号）。
        判别力：删掉退回那一支 ⇒ 行停在 `claiming`、`claiming_by` 还在、台账不存在 ⇒ 本用例三处都会红。"""
        from apps.business.peer_hall import (
            CLAIM_RELEASED_ON_UNBIND,
            RELEASED_CLAIM_LEDGER_KIND,
        )

        row = self._make_row()
        claimed_at = timezone.now()
        card = PeerHallMirror.objects.create(
            peer_upstream_binding=row,
            peer_base_url=row.peer_base_url,
            peer_order_no="SYN-Z5-CLAIMING-1",
            status=PeerHallMirror.Status.CLAIMING,
            claiming_by=self.user,
            claiming_at=claimed_at,
            first_seen_at=claimed_at,
        )
        resp = self.client.delete(self._detail(row.pk))
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.json())
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(card.claiming_by_id)
        self.assertIsNone(card.claiming_at)
        self.assertEqual(card.last_error, CLAIM_RELEASED_ON_UNBIND)
        self.assertEqual(card.last_error, "绑定停用，抢单未确认已退回大厅")
        ledger = PeerSyncEvent.objects.get(kind=RELEASED_CLAIM_LEDGER_KIND)
        self.assertEqual(ledger.direction, "in")
        self.assertEqual(ledger.status, PeerSyncEvent.Status.DONE)
        self.assertEqual(ledger.binding_role, "upstream")
        self.assertEqual(ledger.binding_id, str(row.pk))
        self.assertEqual(ledger.reason, "绑定停用，抢单未确认已退回大厅")
        self.assertEqual(ledger.payload["released"], ["SYN-Z5-CLAIMING-1"])

    @mock.patch(
        "apps.business.api.peer_views.unbind_upstream",
        side_effect=PeerUnreachable("connection refused"),
    )
    def test_delete_succeeds_locally_even_when_the_notice_fails(self, _):
        """spec §5.2 第 5 条：⛔ 不允许「通知不到上家 ⇒ 解绑失败」（用户会卡住）。"""
        row = self._make_row()
        resp = self.client.delete(self._detail(row.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["code"], 0)
        row.refresh_from_db()
        self.assertIsNotNone(row.deleted_at)

    @mock.patch(
        "apps.business.api.peer_views.unbind_upstream",
        side_effect=PeerRejected("令牌无效"),
    )
    def test_delete_also_succeeds_locally_when_upstream_rejects(self, _):
        row = self._make_row()
        resp = self.client.delete(self._detail(row.pk))
        self.assertEqual(resp.json()["code"], 0)
        row.refresh_from_db()
        self.assertIsNotNone(row.deleted_at)

    def test_delete_writes_one_operation_log_row(self):
        row = self._make_row()
        with mock.patch("apps.business.api.peer_views.unbind_upstream"):
            self.client.delete(self._detail(row.pk))
        logs = OperationLog.objects.filter(action="system.peer_binding_delete")
        self.assertEqual(logs.count(), 1)
        log = logs.get()
        self.assertEqual(log.actor, "kefu-a")
        self.assertEqual(log.target_id, str(row.pk))
        self.assertEqual(log.target_name, "测试工作室甲")
        self.assertEqual(log.log_type, 40)
        self.assertEqual(log.summary, "kefu-a解除了与上家系统测试工作室甲的绑定")

    def test_delete_unknown_pk_is_a_business_error_not_a_500(self):
        resp = self.client.delete(self._detail(99999999))
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 1002)
        self.assertTrue(resp.json()["msg"])

    def test_delete_of_an_already_unbound_row_is_a_business_error(self):
        """软删过的行**不再是**可解绑的目标（⛔ 不能靠再删一次"成功"来掩盖）。"""
        row = self._make_row(deleted_at=timezone.now())
        with mock.patch("apps.business.api.peer_views.unbind_upstream") as unbind_upstream:
            resp = self.client.delete(self._detail(row.pk))
        self.assertEqual(resp.json()["code"], 1002)
        unbind_upstream.assert_not_called()


@override_settings(PEER_SYNC_LOCAL_BASE_URL="")
class PeerBindingPageRbacTests(TenantTestCase):
    """⭐ 裁决 R22：两个页面端点要有 `system.bind_upstream_system` 节点门。
    ⚠️ **为什么这是一条安全判据**：没有节点门时，落在此处的权限类只是全局默认的 `IsAuthenticated` ⇒ **任何登录用户**（例如一个没有任何系统节点的代练员）都能读到绑定列表（对方域名 / 子账号 / 操作人 / 上次错误），更能 **DELETE** 一条绑定——本地软删 **＋** 通知上家撤销 ⇒ **这个租户的进单被静默停掉**。
    ⚠️ 正向对照必须一起有：只钉"没节点 ⇒ 403"的话，把节点门写成 `permission_classes = []`（或写成"谁都拒"）也会绿（`AGENTS.md` §5-G）。"""

    def setUp(self):
        cache.clear()
        # ⚠️ 绑定的操作人（`bound_by`）**单独一个账号**：`make_logged_in_client` 内部是
        # `create_user(username=…)`（重名会 IntegrityError）⇒ 不能让夹具与它抢同一个名字。
        self.owner = User.objects.create_user(
            username="peer-page-owner", password="pw-peer-page-owner"
        )

    def _client(self, username, *, nodes):
        return make_logged_in_client(
            domain=PEER_DOMAIN_A, username=username, password=f"pw-{username}",
            # `groups` 是 `_grant_nodes` 的落点：没有组 ⇒ 节点挂不上（403）。
            groups=("客服",), nodes=nodes,
        )

    def _row(self, **over):
        data = {
            "peer_name": "测试工作室甲",
            "peer_base_url": "https://peer-a.test.invalid",
            "peer_account": "peer-account-a",
            "password": "peer-password-a",
            "token_in_hash": hash_token(new_token()),
            "token_out": "TOKEN-OUT-EXISTING",
            "local_base_url": PAGE_LOCAL_BASE_URL,
            "bound_by": self.owner,
        }
        data.update(over)
        return PeerUpstreamBinding.objects.create(**data)

    def test_a_user_without_the_node_cannot_use_any_of_the_three_page_endpoints(self):
        client = self._client("peer-page-outsider", nodes=())
        # 自证夹具真的登进来了（否则下面三条 403 可能由"压根没登录"凑出来）。
        self.assertEqual(client.get("/api/auth/me").status_code, 200)

        row = self._row()
        self.assertEqual(client.get(PAGE_URL).status_code, 403)
        with mock.patch(
            "apps.business.api.peer_views.bind_upstream", return_value="TOKEN-UP"
        ) as bind_upstream:
            resp = client.post(
                PAGE_URL,
                json.dumps(_page_payload(peer_account="peer-account-a")),
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 403, resp.content[:200])
        bind_upstream.assert_not_called()
        self.assertEqual(PeerUpstreamBinding.objects.count(), 1)     # ⛔ 没有新行
        with mock.patch(
            "apps.business.api.peer_views.unbind_upstream"
        ) as unbind_upstream:
            resp = client.delete(f"{PAGE_URL}{row.pk}/")
        self.assertEqual(resp.status_code, 403, resp.content[:200])
        unbind_upstream.assert_not_called()
        row.refresh_from_db()
        self.assertIsNone(row.deleted_at)          # ⛔ 绑定没被软删（进单不会被静默停掉）
        # ⭐ P1 的两支（读 / 改大厅两格）同一道门：没节点 ⇒ 403，且**一行不改**。
        self.assertEqual(client.get(f"{PAGE_URL}{row.pk}/").status_code, 403)
        resp = client.put(
            f"{PAGE_URL}{row.pk}/",
            json.dumps({"hall_sync_enabled": True, "hall_price_percent": 10}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403, resp.content[:200])
        row.refresh_from_db()
        self.assertIs(row.hall_sync_enabled, False)   # ⛔ 没被打开
        self.assertEqual(row.hall_price_percent, 100)  # ⛔ 没被改价

    def test_a_user_with_the_node_can_use_them(self):
        """正向对照：有节点的人 **GET（列表）与 DELETE（解绑）**都通（让上面那三条 403 里的两条有判别力）。
        ⚠️ **本条不覆盖 POST（绑定）**：那一路的正向对照在 `PeerBindEndpointTests` 里（⛔ 别把本 docstring 读成"三个端点都通"——它只跑 GET / DELETE）。"""
        client = self._client("peer-page-rbac", nodes=(PAGE_BINDING_NODE,))
        self.assertEqual(client.get("/api/auth/me").status_code, 200)
        self.assertEqual(client.get(PAGE_URL).status_code, 200)
        row = self._row(peer_account="peer-account-ok")
        # ⭐ P1：明细 GET 与 PUT 都要通（正向对照，否则上面那两条 403 在「端点根本不通」
        #    上也成立）。
        self.assertEqual(client.get(f"{PAGE_URL}{row.pk}/").status_code, 200)
        resp = client.put(
            f"{PAGE_URL}{row.pk}/",
            json.dumps({"hall_sync_enabled": True}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)
        row.refresh_from_db()
        self.assertIs(row.hall_sync_enabled, True)
        with mock.patch(
            "apps.business.api.peer_views.unbind_upstream"
        ):
            resp = client.delete(f"{PAGE_URL}{row.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)


class PeerPasswordNeverLoggedTests(TenantTestCase):
    """spec §9-S4 / §12.2 #5：握手用的密码**永不进日志**。断言打在**真的日志出口**上（⛔ 不是 mock 一个 logger）。
    做法：把一个收集型 `Handler` 挂到**根 logger**、级别压到 DEBUG，跑完两条真实路径后，在**记录文本**里找那个密码。⛔ 刻意不用 `assertLogs`——它会自己把目标 logger 的级别抬到 INFO，于是"生产一行都没写"的用例**全绿**（`AGENTS.md` §5-G / E-0009，本项目实测踩过）。
    ⚠️ 另有一条**正向对照**：同一个 sink 必须抓得住一条**故意**带密码的记录——没有它，"没抓到"与"没写"就分不开，判据恒真。"""

    def setUp(self):
        cache.clear()
        self.client = Client(SERVER_NAME=PEER_DOMAIN_A)
        self.account = User.objects.create_user(
            username="peer-account-a", password="peer-password-a"
        )

    def test_the_handshake_password_reaches_no_log_record(self):
        password = "peer-password-a"
        # ⚠️ 进来那一跳用的是**另一个**口令值：只断言 `password` 的话，"进来那一跳把口令打进
        # 日志"这种泄漏会因为值是另一个串而**溜过去**（本用例第一版就被自己的变异证伪过一次：
        # 往 `PeerBindView` 里加一条 `logger.warning(…, payload["password"])` 时它照样绿）。
        wrong_password = "peer-wrong-password-a"
        # 页面端点要节点门（裁决 R22）。
        page_client = make_logged_in_client(
            domain=PEER_DOMAIN_A, username="peer-log-kefu",
            password="pw-peer-log-kefu", groups=("客服",),
            nodes=(PAGE_BINDING_NODE,),
        )
        with _capture_log_records() as handler:
            # ①a **进来**的一跳（**成功**）：口令对 ⇒ 也走完整段 `payload` 处理与响应组装
            #    （只测失败那一跳会漏掉"成功路径顺手把有效载荷打进日志"这种泄漏——
            #     本用例第一版就是这样：变异加在 `cache.delete(key)` 后面时它照样绿）。
            ok = self.client.post(
                BIND_URL, json.dumps(_bind_payload(password=password)),
                content_type="application/json",
            )
            self.assertEqual(ok.json()["code"], 0, ok.content[:200])
            # ①b **进来**的一跳（口令错）：走 `authenticate` + 失败计数；外部可达的登录面。
            incoming = self.client.post(
                BIND_URL, json.dumps(_bind_payload(password=wrong_password)),
                content_type="application/json",
            )
            self.assertEqual(incoming.json()["code"], 1018)
            # ② **出去**的一跳：页面绑定 → 真 `bind_upstream` → httpx 抛
            #    （错误路径最可能"顺手把请求载荷打出来"）。桩打在**被查找的模块**上（E-0029）。
            with mock.patch(
                "apps.business.peer_client.httpx.stream",
                side_effect=httpx.ConnectError("connection refused"),
            ):
                outgoing = page_client.post(
                    PAGE_URL, json.dumps(_page_payload(password=password)),
                    content_type="application/json",
                )
            # 自证两条路**真的都跑到了**（否则"密码没进日志"可能只是因为什么都没发生）。
            self.assertEqual(outgoing.status_code, 200, outgoing.content[:200])
            self.assertEqual(outgoing.json()["code"], 1021, outgoing.content[:200])

        joined = "\n".join(handler.texts())
        # 两个口令值都要不在：只查一个 = 只守住一半的路径。
        for secret in (password, wrong_password):
            self.assertNotIn(secret, joined)

        # ⚠️ **正向对照**：同一个 sink 抓得住一条故意带密码的记录 ⇒ 上面那条能失败。
        with _capture_log_records() as canary:
            logging.getLogger("tests.peer-password-canary").warning(
                "canary（故意泄漏）%s", password
            )
        self.assertIn(password, "\n".join(canary.texts()))


# ── 端到端：真实握手产出的令牌必须真的能验通（裁决 R15 的**承重**判据）───────────
#: 下家租户的域名 / schema（刻意与 `PeerCrossTenantTokenTests` 的 B 不同）。
PEER_E2E_DOMAIN_DOWN = "peer-e2e-down.test.local"
PEER_E2E_SCHEMA_DOWN = "peer_e2e_down"
E2E_INBOUND_URL = "/api/business/peer-sync/inbound/events/"


#: ⭐ 握手声明的本方地址必须 **https**——C2 闸门会拒 `http://` 的新握手，而本类要的是「真实握手真的发生一次」。
#: ⚠️ ⛔ **不要**用类级 `@override_settings` 给它配：`django_tenants.TenantTestCase.setUpClass` **不调 `super().setUpClass()`**（django_tenants 源码实测），Django 的 `SimpleTestCase.setUpClass`（进入 `_overridden_settings` 的那一层）根本不跑 ⇒ TenantTestCase 上的类级 settings 装饰是**静默 no-op**（本仓既有两处 `""` 装饰正因本机 `.env` 也没配这个键才碰巧没露馅）。
#: ⇒ 在 `_real_handshake` 里用**上下文管理器**形态显式包住（两种形态的差别实测过）。
E2E_LOCAL_BASE_URL = "https://" + PEER_E2E_DOMAIN_DOWN


class PeerHandshakeCredentialTests(TenantTestCase):
    """真实握手 → 两个方向的**真端点**都必须 200（spec §5.1 / §5.2 / §9-S3；裁决 R15）。**为什么必须跨两个租户**：`PeerTokenAuthentication` 在**当前 schema** 里先扫 `PeerDownstreamBinding`、再扫 `PeerUpstreamBinding`。若把两张表放在同一个 schema 里，「另一个方向的令牌必须 401」这条**阴性对照**会被另一张表的**合法命中**顶掉（变成 200）⇒ 那次阴性对照恒假、证明不了任何东西；真实拓扑里两张表分属两个库，所以这里也分成两个租户。
    **它补的是哪条缺口**：既有的每条用例都按「代码当时比哪一列」造夹具（`token_in_hash=hash(随手一个 token)`），于是把两列对调也全绿。本类让握手**真的发生一次**，再拿两边**真实存下的明文**去打对方的真端点——那是「令牌到底能不能用」唯一的判据。
    ⚠️ 上家 = `TenantTestCase` 自带租户（`PEER_DOMAIN_A`）；下家 = 这里另开的租户。⚠️ 跨域名请求会把连接留在那个租户的 schema 上（`TenantMainMiddleware` 不还原）⇒ 每次跨域名调用都用 `try/finally` 钉回本租户（照 `PeerCrossTenantTokenTests` 的坑二）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        cls.tenant_down, _ = provision_tenant(
            schema_name=PEER_E2E_SCHEMA_DOWN,
            name="上下游端到端租户（下家）",
            domain=PEER_E2E_DOMAIN_DOWN,
        )
        seed_tenant(cls.tenant_down)

    def setUp(self):
        # ⚠️ 限流桶按 schema 记在 cache 里、**跨测试类不清**：`PeerBindEndpointTests` 可能把上家
        # 那个 schema 的桶灌满 ⇒ 不清的话本类的真握手会被 1018 拦掉（看起来像握手坏了）。
        cache.clear()
        connection.set_tenant(self.tenant)
        self.up_account = User.objects.create_user(
            username="peer-e2e-account-a", password="peer-e2e-pw"
        )
        # 下家侧：登录夹具**自己会建** `peer-e2e-kefu-b`（页面端点靠它当 `bound_by`）+
        # 一款游戏（接收端点要求事件里的**游戏名**在本租户解析得到，裁决 R21）。
        connection.set_tenant(self.tenant_down)
        try:
            self.game_down = Game.objects.create(name="上下游端到端游戏")
            self.client_down = make_logged_in_client(
                domain=PEER_E2E_DOMAIN_DOWN,
                username="peer-e2e-kefu-b",
                password="peer-e2e-pw-b",
                # ⚠️ 裁决 R22：页面端点要节点门（默认空集 ⇒ 403）。`groups` 是挂节点的落点
                # （`_grant_nodes` 按用户所在的组挂，没有组时节点挂不上）。
                groups=("客服",), nodes=(PAGE_BINDING_NODE,),
            )
        finally:
            connection.set_tenant(self.tenant)

    # ── 夹具 ───────────────────────────────────────────────────────────
    def _real_handshake(self) -> tuple[str, str]:
        """驱动下家页面端点：它真的生成 `token_b`，再真的 POST 到上家的 `bind/`。返回 `(token_b, token_a)`：
        - `token_b` = 下家 `PeerUpstreamBinding.token_out`（明文，解绑时要带）；
        - `token_a` = 上家握手响应里回的那套 = `PeerDownstreamBinding.token_out`（上家推单时要带）。"""
        captured: dict[str, str] = {}

        def _fake_bind(base_url, *, account, password, token_out, local_base_url, **kwargs):
            captured["token_b"] = token_out
            try:
                resp = Client(SERVER_NAME=PEER_DOMAIN_A).post(
                    BIND_URL,
                    json.dumps({
                        "account": account,
                        "password": password,
                        "peer_base_url": local_base_url,
                        "token_out": token_out,
                    }),
                    content_type="application/json",
                )
            finally:
                # ⚠️ 内层请求把连接切到了上家 schema ⇒ 必须切回下家，
                # 否则页面端点随后的 `PeerUpstreamBinding.objects.create` 会写进上家的库。
                connection.set_tenant(self.tenant_down)
            self.assertEqual(resp.status_code, 200, resp.content[:200])
            self.assertEqual(resp.json()["code"], 0, resp.content[:200])
            captured["token_a"] = resp.json()["data"]["token_out"]
            return captured["token_a"]

        with override_settings(PEER_SYNC_LOCAL_BASE_URL=E2E_LOCAL_BASE_URL):
            with mock.patch(
                "apps.business.api.peer_views.bind_upstream", side_effect=_fake_bind
            ):
                connection.set_tenant(self.tenant_down)
                try:
                    resp = self.client_down.post(
                        PAGE_URL,
                        json.dumps(_page_payload(
                            peer_account="peer-e2e-account-a", password="peer-e2e-pw",
                        )),
                        content_type="application/json",
                    )
                finally:
                    connection.set_tenant(self.tenant)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.content[:200])
        return captured["token_b"], captured["token_a"]

    def _post_on(self, domain, body, *, token=None):
        """拿 Bearer 令牌打某个域名的端点；退出时把连接钉回上家（本租户）。"""
        headers = {"HTTP_AUTHORIZATION": "Bearer " + token} if token else {}
        try:
            return Client(SERVER_NAME=domain).post(
                self._url_for(domain), json.dumps(body),
                content_type="application/json", **headers,
            )
        finally:
            connection.set_tenant(self.tenant)

    def _url_for(self, domain):
        return E2E_INBOUND_URL if domain == PEER_E2E_DOMAIN_DOWN else UNBIND_URL

    def _event(self, order_no="E2E-1"):
        return {
            "idempotency_key": uuid.uuid4().hex,
            "kind": "order.assign",
            "target_ref": order_no,
            # ⭐ 裁决 R21：按**名字**对齐游戏（⛔ 不传对端 id）。
            "payload": {"status": OrderStatus.IN_PROGRESS, "game_name": self.game_down.name},
        }

    def _upstream_row(self):
        connection.set_tenant(self.tenant)
        return PeerDownstreamBinding.objects.get(account=self.up_account)

    def _downstream_row(self):
        connection.set_tenant(self.tenant_down)
        try:
            return PeerUpstreamBinding.objects.get()
        finally:
            connection.set_tenant(self.tenant)

    # ── 判据 ───────────────────────────────────────────────────────────
    def test_the_handshake_stores_the_symmetric_token_pair(self):
        """先钉**方向**本身：每一侧「验谁、发谁」都必须与 R15 的表一致。
        ⚠️ 这一条是下面两条端点判据的**前提**：若两列被对调，下面两条会以 401 红在这里之后；本条则直接指出「存错了列」。"""
        token_b, token_a = self._real_handshake()
        self.assertTrue(token_b)
        self.assertTrue(token_a)
        self.assertNotEqual(token_a, token_b)

        up = self._upstream_row()             # 我是上家
        self.assertEqual(up.token_in_hash, hash_token(token_b))   # 验：下家给我的 token_b
        self.assertNotEqual(up.token_in_hash, token_b)            # 只存哈希
        self.assertEqual(up.token_out, token_a)                   # 发：我自己生成的 token_a（明文）

        down = self._downstream_row()         # 我是下家
        self.assertEqual(down.token_in_hash, hash_token(token_a))  # 验：上家推给我的 token_a
        self.assertNotEqual(down.token_in_hash, token_a)           # 只存哈希
        self.assertEqual(down.token_out, token_b)                  # 发：我生成的 token_b（明文）

    def test_the_upstreams_token_authenticates_on_the_downstream_inbound_endpoint(self):
        """(a) 上家推单带的是 `PeerDownstreamBinding.token_out`（`token_a`）⇒ 下家必须 200。
        ⚠️ 用的是**库里存下的**那一列（不是握手响应里顺手拿到的副本）：否则「存错列」这类缺陷会从本判据底下溜过去。"""
        self._real_handshake()
        token_a = self._upstream_row().token_out
        resp = self._post_on(PEER_E2E_DOMAIN_DOWN, {"events": [self._event()]}, token=token_a)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0)
        data = resp.json()["data"]
        self.assertEqual(data["applied"], 1)
        self.assertEqual(data["skipped"], 0)
        self.assertEqual(data["skipped_reasons"], [])
        # 落单真的发生（否则「200」可能由别的空路径凑出来）。
        connection.set_tenant(self.tenant_down)
        try:
            self.assertEqual(Order.objects.count(), 1)
        finally:
            connection.set_tenant(self.tenant)

    def test_the_downstreams_token_authenticates_on_the_upstream_unbind_endpoint(self):
        """(b) 下家页面解绑带上 `PeerUpstreamBinding.token_out`（`token_b`）⇒ 上家必须 200。
        ⚠️ 走**下家页面 DELETE**（`PeerBindingDetailView.delete` → `unbind_upstream`），而不是直接拿一个令牌去打上家：只有这样才能同时钉住「delete 选对了哪一列」。`unbind_upstream` 只在传输层被打桩成**进程内**的真请求（形态与 `_real_handshake` 同）。"""
        self._real_handshake()
        row = self._downstream_row()
        sent: dict[str, object] = {}

        def _fake_unbind(base_url, token, **kwargs):
            sent["base_url"] = base_url
            sent["token"] = token
            try:
                resp = Client(SERVER_NAME=PEER_DOMAIN_A).post(
                    UNBIND_URL, json.dumps({}), content_type="application/json",
                    HTTP_AUTHORIZATION="Bearer " + token,
                )
            finally:
                connection.set_tenant(self.tenant_down)
            sent["status"] = resp.status_code
            sent["code"] = resp.json().get("code")
            if resp.status_code != 200 or resp.json().get("code") != 0:
                # 照真 `unbind_upstream` 的语义抛（调用方会吞掉；本地照常解绑）。
                raise PeerRejected("对端拒绝了这次解绑")

        with mock.patch(
            "apps.business.api.peer_views.unbind_upstream", side_effect=_fake_unbind
        ):
            page = self.client_down.delete(f"{PAGE_URL}{row.pk}/")

        self.assertEqual(page.status_code, 200, page.content[:200])
        self.assertEqual(page.json()["code"], 0)
        self.assertEqual(sent["base_url"], "https://peer-a.test.invalid")
        # ⭐ 上家真端点回的是 200（不是 401）——这是「解绑令牌真的能验通」的判据。
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["code"], 0)
        up = self._upstream_row()
        self.assertEqual(up.state, PeerDownstreamBinding.State.REVOKED)
        self.assertIsNotNone(up.revoked_at)

    def test_neither_endpoint_accepts_the_other_directions_token(self):
        """阴性对照：**另一个方向**存下的令牌在本端点不通过认证（两个方向各一条）。⚠️ 它证明的**只有**这一件事——下家的 `token_out`（`token_b`）打下家自己的接收端点、上家的 `token_out`（`token_a`）打上家自己的解绑端点，都不认。⛔ 它**不是**让上面两条正向用例变得有意义的那条对照，也**判别不出列对调**：`token_matches` 对**第一个入参**做哈希（`peer_sync.token_matches` 的实现），而这里递进去的是库里存的**明文** ⇒ `hash(明文) != token_in_hash` 恒成立 ⇒ 三条列对调变异下，本用例**依旧是 401、照样全绿**；真正杀掉那些变异的是正向用例 (a)/(b)（它们拿的同样是库里存的明文，但打的是**该方向真正会收到的那一套**）。
        ⚠️ 两条都必须在**对应绑定仍是 active** 的状态下断言：上家那条若先被真解绑打成 `revoked`，401 就可能来自「行已失效」而不是「验错了列」（`AGENTS.md` §5-G）。
        所以这里**只**用另一个方向的令牌、不先做正向解绑。⚠️ 令牌一律取**库里存下的明文**（`token_out`），与正向用例同源。"""
        self._real_handshake()
        down_token_out = self._downstream_row().token_out   # 下家自己的那一套（token_b）
        up_token_out = self._upstream_row().token_out       # 上家自己的那一套（token_a）

        # 下家只验 `token_a` ⇒ 拿 `token_b` 打它的接收端点必须 401、且一单都不落。
        on_down = self._post_on(
            PEER_E2E_DOMAIN_DOWN, {"events": [self._event()]}, token=down_token_out
        )
        self.assertEqual(on_down.status_code, 401, on_down.content[:200])
        self.assertEqual(on_down.json()["code"], 401)
        connection.set_tenant(self.tenant_down)
        try:
            self.assertFalse(Order.objects.exists())
        finally:
            connection.set_tenant(self.tenant)

        # 上家此刻那一行仍是 active（上面的断言自证）⇒ 401 只能来自「验错了列」。
        self.assertEqual(
            self._upstream_row().state, PeerDownstreamBinding.State.ACTIVE
        )
        on_up = self._post_on(PEER_DOMAIN_A, {}, token=up_token_out)
        self.assertEqual(on_up.status_code, 401, on_up.content[:200])
        self.assertEqual(on_up.json()["code"], 401)
        self.assertEqual(
            self._upstream_row().state, PeerDownstreamBinding.State.ACTIVE
        )


# ── 下家侧四个健康列的**真写入者**（spec §16.7 第 1 条）──────────────
class PeerInboundHealthColumnTests(TenantTestCase):
    """⭐ 补齐缺口：绑定页那块红字提示读的四个列，从接收路径上**真有**写入者了。缺口是什么（`docs/specs/2026-09-25-upstream-downstream-order-sync-design.md` §16.7 实测）：页面差异表 #7 那块红字提示（裁决 R20）读的是**下家自己那条绑定**（`PeerUpstreamBinding`）的 `last_error` / `last_error_at` / `consecutive_failures` / `last_ok_at`；而最初版本里这四个列**没有任何写入者**——那时唯一的投递内核写的是**上家侧**的 `PeerDownstreamBinding`（该内核已随 spec §16.6 搬进 `apps/business/peer_worker.py`）⇒ 那块提示在早期不可能亮（页面用例只喂夹具，所以全绿）。
    判据打在哪：⛔ 不打在日志 / mock 上——每条用例都 `refresh_from_db()` 读**数据库里的那一行**（`AGENTS.md` §5-G / E-0009），且钉**字面值**（E-0024）；走的是**真端点**（Bearer 令牌 + `PeerTokenAuthentication` + 它那层 `transaction.atomic()`），⛔ 不是直接调 `receive_events`：只有这样才能同时证明这几笔写入没有被端点的整批事务吞掉 / 回滚掉。
    ⚠️ 夹具一律合成值（`测试工作室甲` / `peer-account-a` / `https://peer-a.test.invalid`）。"""

    def setUp(self):
        self.game = Game.objects.create(name="健康列夹具游戏")
        self.kefu = User.objects.create_user(
            username="peer-health-kefu", password="pw-peer-health"
        )
        self.raw = new_token()
        self.binding = PeerUpstreamBinding.objects.create(
            peer_name="测试工作室甲",
            peer_base_url="https://peer-a.test.invalid",
            peer_account="peer-account-a",
            token_in_hash=hash_token(self.raw),
            token_out=new_token(),
            local_base_url="https://peer-b.test.invalid",
            bound_by=self.kefu,
        )

    def _event(self, *, status=OrderStatus.IN_PROGRESS, occurred_at=None,
               order_no="A-1"):
        """一条 `order.assign` 事件；`occurred_at` 在**事件级**（与 `kind` 等并列）。
        ⚠️ `occurred_at=None` 表示**不带**这个键（早期事件形状），⛔ 不是带一个 `None` 值。"""
        event = {
            "idempotency_key": uuid.uuid4().hex,
            "kind": "order.assign",
            "target_ref": order_no,
            "payload": {"status": status, "game_name": self.game.name},
        }
        if occurred_at is not None:
            event["occurred_at"] = occurred_at
        return event

    def post_events(self, events):
        """拿上家给我的令牌打真端点（认证 → 整批事务 → 健康列写入，全在真实路径上）。"""
        return Client(SERVER_NAME=PEER_DOMAIN_A).post(
            E2E_INBOUND_URL, json.dumps({"events": events}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + self.raw,
        )

    def test_a_clean_batch_refreshes_last_ok_at_and_zeroes_the_counter(self):
        """干净批次（全部应用成功）⇒ `last_ok_at` 有值、`consecutive_failures` 清零。
        ⚠️ `consecutive_failures` 先人为设成 3：默认值本来就是 0 ⇒ 直接断言 0 是**恒真**的（E-0024 族：那种断言在"压根没写"的实现上也绿）。"""
        self.binding.consecutive_failures = 3
        self.binding.save(update_fields=["consecutive_failures"])

        resp = self.post_events(
            [self._event(occurred_at="2026-09-26T10:00:00+08:00")]
        )
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["data"]["applied"], 1, resp.content[:200])

        self.binding.refresh_from_db()
        self.assertIsNotNone(self.binding.last_ok_at)
        self.assertEqual(self.binding.consecutive_failures, 0)

    def test_a_hard_skip_records_an_error_and_does_not_refresh_last_ok_at(self):
        """硬跳（`code != stale`）⇒ 写失败原因 / 时刻、计数 +1，且 `last_ok_at` **不被刷新**。
        ⭐ 最后那条是承重的：`last_ok_at` 的语义是"最近一次**成功**收到"，硬跳批次一条都没收下 ⇒ 刷新它等于对页面上的人说假话（页面 #8 那块会显示一个假的"最近收到"）。"""
        old_ok = timezone.now() - timedelta(hours=1)
        self.binding.last_ok_at = old_ok
        self.binding.save(update_fields=["last_ok_at"])

        resp = self.post_events([self._event(status="NO_SUCH_STATUS")])
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        data = resp.json()["data"]
        self.assertEqual(data["applied"], 0, resp.content[:200])
        self.assertEqual(data["skipped"], 1, resp.content[:200])
        self.assertEqual(data["skipped_details"][0]["code"], "unknown_status")

        self.binding.refresh_from_db()
        # ⭐ 钉**字面**整句（E-0024）：计数与**第一条原因**都要在里面。
        self.assertEqual(
            self.binding.last_error,
            "上家推来的这批事件共 1 条，有 1 条没能接收；"
            "第一条原因：未知或缺失的订单状态：'NO_SUCH_STATUS'",
        )
        self.assertIsNotNone(self.binding.last_error_at)
        self.assertEqual(self.binding.consecutive_failures, 1)
        # ⭐ 承重判据：`last_ok_at` **逐字未动**。
        self.assertEqual(self.binding.last_ok_at, old_ok)

    def test_a_batch_of_only_stale_skips_is_still_a_clean_batch(self):
        """只有 `stale`（良性）⇒ 按**干净**批次处理：刷新 `last_ok_at`、计数清零。
        ⛔ `stale` 不是失败（spec §16.5）：它是"对端已有更新的"。把它算失败会让**重投**把 `consecutive_failures` 一路推上去、页面凭空亮红字（假报警）。"""
        # ① 先应用一条带时刻的事件 ⇒ 建立 `PeerOrderLink.last_applied_at`（干净的第一次照样是干净批次）。
        first = self.post_events(
            [self._event(occurred_at="2026-09-26T10:00:00+08:00")]
        )
        self.assertEqual(first.status_code, 200, first.content[:200])
        self.assertEqual(first.json()["data"]["applied"], 1, first.content[:200])

        # ② 把两个列人为打回"还没成功过 + 失败了 4 次"，让下一条 batch 的写入**可判别**。
        self.binding.last_ok_at = None
        self.binding.consecutive_failures = 4
        self.binding.save(update_fields=["last_ok_at", "consecutive_failures"])

        # ③ 同单号、**同一时刻**重投 ⇒ `stale`（良性跳过，spec §16.3 / Ruling 22）。
        replay = self.post_events(
            [self._event(occurred_at="2026-09-26T10:00:00+08:00")]
        )
        self.assertEqual(replay.status_code, 200, replay.content[:200])
        data = replay.json()["data"]
        self.assertEqual(data["applied"], 0, replay.content[:200])
        self.assertEqual(data["skipped"], 1, replay.content[:200])
        self.assertEqual(data["skipped_details"][0]["code"], "stale")

        self.binding.refresh_from_db()
        self.assertIsNotNone(self.binding.last_ok_at)
        self.assertEqual(self.binding.consecutive_failures, 0)

    def test_the_error_names_the_hard_skip_count_and_the_first_reason(self):
        """一批里有多条硬跳 ⇒ 计数写**这一批有几条**、原因取**第一条**；计数只 +1（不是逐条 +1）。"""
        self.binding.consecutive_failures = 2
        self.binding.save(update_fields=["consecutive_failures"])

        resp = self.post_events([
            self._event(status="NO_SUCH_STATUS"),
            # 第二条形状不对（`payload` 不是对象）⇒ 另一类硬跳 `bad_shape`。
            {
                "idempotency_key": uuid.uuid4().hex,
                "kind": "order.assign",
                "target_ref": "A-2",
                "payload": "不是对象",
            },
        ])
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        data = resp.json()["data"]
        self.assertEqual(data["applied"], 0, resp.content[:200])
        self.assertEqual(data["skipped"], 2, resp.content[:200])

        self.binding.refresh_from_db()
        self.assertEqual(
            self.binding.last_error,
            "上家推来的这批事件共 2 条，有 2 条没能接收；"
            "第一条原因：未知或缺失的订单状态：'NO_SUCH_STATUS'",
        )
        # ⭐ 计数是"这一批有硬跳就 +1"（2 + 1 = 3），⛔ 不是"每条硬跳 +1"（那会是 4）。
        self.assertEqual(self.binding.consecutive_failures, 3)
