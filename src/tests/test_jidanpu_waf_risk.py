"""上家鸡蛋铺对接的**风控降险**四条（spec `docs/plans/2026-10-20-upstream-waf-risk-reduction.md` §2 / §3）。

## 背景（实测，不是推断）

2026-10-20 某台租户机（`124.223.181.43`）对接的三家上家，**登录一律被对端 WAF 拦成
HTTP 468**（SafeLine 拦截页）；同一时刻从别的机器访问同一站点是正常的 ⇒ **按来源 IP 拦的**。
把这台机器惹毛的四条，各有一条用例钉住：

| # | 事实 | 本文件的用例 |
|---|---|---|
| 1 | 同步进程**每一轮都新建客户端** ⇒ cookie jar 销毁 ⇒ 每轮重新登录一次 | `UpstreamClientPoolTests` |
| 2 | 大厅快路 3 秒一拍 ⇒ 每个绑定约每 3–4 秒登录一次 | `WafRiskConstantTests` |
| 3 | 请求头 `User-Agent` 自报 `jidanpu-sdk/0.1.1` | `BrowserUserAgentTests` |
| 4 | 被封之后不退避，9 小时按原节奏白撞 1 万多次 | `UpstreamFailureBackoffTests` |

⚠️ **命令层的「退避窗口内整轮跳过」不在本文件**（那要驱动 `manage.py jidanpu_sync`）：
见 `tests/test_jidanpu_sync_command.py`。
⚠️ 夹具一律**合成值**（E-0077）：域名 `…waf-risk.test.invalid`、账号 `kefu-waf-*`、密码 canary；
⛔ 不引真实凭据、⛔ 不把上游回包全文写进断言。
⚠️ **零真实网络**：第 1 条用 `httpx.MockTransport` 当假上游（只认 `POST /Login/Login`，
其余请求一律当场失败），第 2 条不建连接。
⚠️ 桩打在**被查找的那个模块**上（E-0029）：建连在 `apps.business.jidanpu`，
退避的纯函数与常量在 `apps/business/management/commands/jidanpu_sync.py`。
"""
from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu, jidanpu_upstream
from apps.business.management.commands import jidanpu_sync
from apps.business.models import JidanpuUpstreamBinding

# ── 合成夹具值（E-0077）───────────────────────────────────────────────────────

BASE_URL = "https://jidanpu-waf-risk.test.invalid"
PASSWORD = "pw-canary-waf-risk"
#: 登录响应的合成会话值（⛔ 不是任何真站点的 cookie）。
SESSION_CANARY = "synthetic-session-canary"

#: 固定的「现在」：退避窗口的判据都按它算（⛔ 不用 `timezone.now()` 让断言随运行时刻漂）。
NOW = datetime(2026, 10, 20, 3, 15, 0, tzinfo=dt_timezone.utc)


class _UpstreamBindingMixin:
    """造一行**合成**的上家绑定（地址 / 账号 / 密码全是不存在的值）。"""

    def _binding(self, name="pool", *, enabled=True, **kwargs):
        user = User.objects.create_user(username=f"kefu-waf-{name}", password="pw-canary-x")
        return JidanpuUpstreamBinding.objects.create(
            user=user,
            jidanpu_base_url=BASE_URL,
            # ⚠️ `(jidanpu_base_url, account)` 上有唯一约束 ⇒ 账号按用例内的名字派生。
            account=f"acc-waf-{name}",
            password=PASSWORD,
            # ⚠️ 模型默认是 **False**（停用即停）⇒ 显式给 True，别让夹具悄悄造一行停用绑定。
            enabled=enabled,
            **kwargs,
        )


# ══ ① 会话复用：同一绑定跨轮只登录一次 ═════════════════════════════════════════


class UpstreamClientPoolTests(_UpstreamBindingMixin, TenantTestCase):
    """`jidanpu.UpstreamClientPool`：同一绑定**跨轮复用同一个 client**（= 只登录一次）。

    ## 这条用例为什么必须存在

    改之前是 `with jidanpu.open_client_with_http(binding)`：**每一轮**建一个客户端、出块就关
    ⇒ cookie jar 跟着销毁 ⇒ 下一轮又 `POST /Login/Login` 一次。大厅快路 3 秒一拍 ⇒
    每个绑定约每 3–4 秒登录一次（某台租户机实测两分钟 88 次）⇒ 对端按 IP 封。

    ⚠️ **桩的接法**（E-0029）：`jidanpu.httpx.Client` 被换成「带 `MockTransport` 的真客户端」
    ⇒ 假上游是**被查找的那一层**（SDK 用的那个 httpx 客户端），⛔ 不是打在 SDK 内部某个函数上。
    ⚠️ **桩没接上时本类用例会红**：域名是 `…test.invalid`（不存在的域），请求只会抛
    `TransportError`，而下面每一条断言都要求「登录请求被假上游数到」——所以「桩接上了」
    本身就是判据的一部分（`self.assertEqual(logins, 1)` 不可能在真网络下成立）。
    """

    def setUp(self):
        super().setUp()
        #: 假上游收到的登录请求数（每次 `POST /Login/Login` +1）。
        self.logins = 0
        self._pool = jidanpu.UpstreamClientPool()
        # ⚠️ 池必须每条用例收尾（否则打开的 httpx 客户端会跨用例泄漏）。
        self.addCleanup(self._pool.release_all)

    # ── 假上游 ──────────────────────────────────────────────────────────────

    def _handler(self, request: httpx.Request) -> httpx.Response:
        """假上游：**只认登录**（其余请求一律当失败——本类不该发别的请求）。"""
        if request.method == "POST" and request.url.path == "/Login/Login":
            self.logins += 1
            return httpx.Response(
                200,
                json={"code": 1, "msg": "success", "data": None},
                headers={"Set-Cookie": f"sl-session={SESSION_CANARY}; Path=/"},
            )
        raise AssertionError(f"未预期的请求：{request.method} {request.url}")

    def _patched_http(self):
        """把 `jidanpu.httpx.Client` 换成「真客户端 + `MockTransport`」（⛔ 零真实网络）。"""
        real_client = httpx.Client
        transport = httpx.MockTransport(self._handler)

        def _client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        return mock.patch.object(jidanpu.httpx, "Client", side_effect=_client)

    # ── 判据 ────────────────────────────────────────────────────────────────

    def test_the_same_binding_is_logged_in_only_once(self):
        """同一绑定连着 `acquire` 两次 ⇒ **同一个 client 对象**、登录**只发 1 次**。

        失败方向（两种都能让它红）：
        ① 池退化成「每次 `acquire` 都新建一对」⇒ 两个 client 不是同一个对象、登录 2 次；
        ② 池的键漏了某个维度（例如只按文本账号拼一个跨绑定的共用键）⇒ 两条用例里
           另一个绑定的 `acquire` 会命中同一个 client（见下一条）。
        """
        binding = self._binding("reuse")
        with self._patched_http():
            first = self._pool.acquire(binding)
            first[0].ensure_login()
            second = self._pool.acquire(binding)
            second[0].ensure_login()
        self.assertIs(first[0], second[0], "同一绑定必须复用同一个 client（= 同一个 cookie jar）")
        self.assertEqual(self.logins, 1, "跨轮复用 ⇒ 整个进程只登录一次")

    def test_a_different_binding_gets_its_own_client_and_logs_in_again(self):
        """换一个绑定 ⇒ 另一个 client、再登一次（⛔ 不共用别人的会话）。

        失败方向：池键只按基址（漏了 pk / 账号）⇒ 两个绑定共用同一个 client ⇒ 登录仍只 1 次
        ⇒ 本用例红。
        """
        one = self._binding("one")
        two = self._binding("two")
        with self._patched_http():
            first = self._pool.acquire(one)
            first[0].ensure_login()
            other = self._pool.acquire(two)
            other[0].ensure_login()
        self.assertIsNot(first[0], other[0], "两个绑定不许共用客户端")
        self.assertEqual(self.logins, 2, "每个上游身份各登一次")

    def test_changing_the_password_replaces_and_closes_the_old_client(self):
        """密码被改 ⇒ 池里换一个新 client，**旧的那个被关掉**（⛔ 泄漏连接池）。

        失败方向：键里不含密码（改了密码还复用旧会话）⇒ 两次是同一个对象 ⇒ 本用例红；
        或者旧 client 没关 ⇒ `is_closed` 仍是假 ⇒ 本用例红。
        """
        binding = self._binding("rotate")
        with self._patched_http():
            first = self._pool.acquire(binding)
            first[0].ensure_login()
            # ⚠️ 只改**内存里这一行**（不落库）：池的键读的就是绑定对象上的值。
            binding.password = f"{PASSWORD}-rotated"
            second = self._pool.acquire(binding)
        self.assertIsNot(first[0], second[0], "密码变了就是另一个上游身份")
        self.assertTrue(first[1].is_closed, "被替换下来的旧客户端必须关掉（否则泄漏连接池）")

    def test_release_all_closes_every_client_and_can_be_called_twice(self):
        """`release_all()`：关掉池里所有客户端，且**幂等**（`--once` 那一趟可能一个都没建）。

        失败方向：`release_all` 不关客户端 ⇒ `is_closed` 是假 ⇒ 本用例红；
        或者第二次调用抛（键已清空时不做判空）⇒ 本用例红。
        """
        one = self._binding("release-1")
        two = self._binding("release-2")
        with self._patched_http():
            first = self._pool.acquire(one)
            second = self._pool.acquire(two)
            self._pool.release_all()
            self.assertTrue(first[1].is_closed)
            self.assertTrue(second[1].is_closed)
            self._pool.release_all()  # 幂等：池空时什么都不做

    def test_the_same_pk_in_another_tenant_gets_its_own_client(self):
        """⭐ 池键里**租户 schema 那一维**承重：同一个绑定对象、两次「不同租户」⇒ 两个 client。

        为什么必须有这条（评审 I-2，实测：把 schema 从键里去掉，全仓 54 条用例仍绿）：
        `binding.pk` 是**租户库内**的自增 ⇒ 两个租户撞 pk 是必然的；键里少这一维就会把
        A 租户的上游会话交给 B 租户用（`AGENTS.md` §4 的租户边界）。

        ⚠️ **换的是 `connection.schema_name`，⛔ 不是 patch `jidanpu._schema_name`**：
        池键读的就是 `_schema_name()` 的返回值，而那个函数只是 `connection.schema_name` 的薄封装。
        去打薄封装等于把被测的那一行**整段换掉**——变异「把 `_schema_name()` 改成返回常量」
        会被这层 patch 掩盖成绿的（第一版就是这么写的，实测没杀死）。打在 `connection` 上，
        「`_schema_name` 真的去读当前租户」也一并被钉住。

        ⚠️ **两条客户端都要活着（⛔ 不关旧的那个）**：常驻进程是**轮流**服务所有租户的
        （`_run_once` 挨个 `tenant_context`）⇒ 换成 B 就把 A 关掉，下一拍 A 又得重新登录
        ——那正是本条要治的病（每轮重登）。所以「关旧的」只在这一行的**凭据**变了时发生
        （见 `test_changing_the_password_replaces_and_closes_the_old_client`），⛔ 不在租户维度上发生。

        失败方向：把 `_schema_name()` 换成常量（`""` / 固定串）⇒ 第二次 `acquire` 命中第一次的
        条目 ⇒ 两个 client 是同一个 ⇒ 本用例红（已实测，见本条 docstring 上一条）。
        """
        binding = self._binding("tenant-dim")
        with self._patched_http():
            with mock.patch.object(connection, "schema_name", "tenant-a"):
                first = self._pool.acquire(binding)
                same_tenant_again = self._pool.acquire(binding)
            with mock.patch.object(connection, "schema_name", "tenant-b"):
                other = self._pool.acquire(binding)
        self.assertIs(first[0], same_tenant_again[0], "同一租户内必须复用（⛔ 别把复用关掉）")
        self.assertIsNot(
            first[0], other[0], "换了租户就是另一个会话（⛔ 不许把 A 租户的会话给 B 用）"
        )
        self.assertFalse(
            first[1].is_closed,
            "A 租户那一份必须留着：常驻进程下一拍还要用它（关掉 = 每轮重登，正是本线要治的病）",
        )
        self.assertFalse(other[1].is_closed)


# ══ ② 浏览器级 User-Agent（⛔ 不自报 SDK）══════════════════════════════════════


class BrowserUserAgentTests(_UpstreamBindingMixin, TenantTestCase):
    """两处 `JidanpuConfig(...)` 构造点都要带 `BROWSER_USER_AGENT`，⛔ 不带 SDK 自报串。

    ⚠️ 判据打在**真实出口**上（`AGENTS.md` §5-G）：除了「配置对象收到了什么」，
    还看**真的 httpx 客户端上的那个头**（`open_client_with_http` 那一支）。
    """

    def test_open_client_passes_the_browser_user_agent_to_the_sdk_config(self):
        """`open_client` 那条（后台 worker / 拉取那一线）的 `JidanpuConfig.user_agent`。

        失败方向：把 `user_agent=` 去掉（SDK 回落成 `jidanpu-sdk/0.1.1`）⇒ 本用例红。
        """
        binding = self._binding("ua-open-client")
        seen: list[dict] = []
        real_config = jidanpu.JidanpuConfig

        def _spy(**kwargs):
            seen.append(kwargs)
            return real_config(**kwargs)

        with mock.patch.object(jidanpu, "JidanpuConfig", side_effect=_spy):
            with jidanpu.open_client(binding):
                pass
        self.assertEqual(len(seen), 1, "建连只该构造一份配置")
        self.assertEqual(seen[0].get("user_agent"), jidanpu.BROWSER_USER_AGENT)
        self.assertNotIn("sdk", str(seen[0].get("user_agent")).lower())

    def test_the_http_client_library_layer_sends_the_browser_user_agent(self):
        """`open_client_with_http` 那一支：**真的 httpx 客户端**上的 `User-Agent` 头。

        ⚠️ 这一条单独钉的理由（评审改准机制）：**走 SDK `Transport` 的请求不需要它**
        ——`Transport.request` 每一发都 `setdefault("User-Agent", config.user_agent)`
        （`src/jidanpu/transport.py:457`），所以那条路上自报什么由 `config.user_agent` 决定
        （改之前是 `jidanpu-sdk/0.1.1`）。这个**客户端级**的头承重的是**绕开 `Transport` 的裸
        `http` 调用**：`probe_pages` 的 `http.get(...)`、上家「上传页取隐藏域」那一发——
        不给它，那两发就发 `python-httpx/x.y`（核对时发现的既有缺口）。

        失败方向：把 `headers={"User-Agent": config.user_agent}` 从那个 `httpx.Client` 上删掉
        ⇒ 头变成 `python-httpx/…`、且含 `sdk` 那一半的判据也一起红。
        """
        binding = self._binding("ua-with-http")
        with jidanpu.open_client_with_http(binding) as (client, http):
            agent = http.headers["user-agent"]
            self.assertEqual(agent, jidanpu.BROWSER_USER_AGENT)
            self.assertNotIn("sdk", agent.lower())
            self.assertIn("Mozilla/5.0", agent)
            # ⭐ 这条头承重的正是**裸调用**那一面：拿它直接打一发，头必须真的到线上。
            #    ⚠️ 不打真网络：`probe_pages` 要的是状态码，这里直接看客户端将要发的头。
            self.assertEqual(
                http.build_request("GET", "/OrderAjax/GetOrderList").headers["user-agent"],
                jidanpu.BROWSER_USER_AGENT,
            )
            self.assertIsNotNone(client)

    def test_the_constant_itself_is_browser_shaped(self):
        """常量本身：像浏览器、⛔ 不含 `sdk`（钉住「别哪天被改回自报串」）。"""
        self.assertIn("Mozilla/5.0", jidanpu.BROWSER_USER_AGENT)
        self.assertNotIn("sdk", jidanpu.BROWSER_USER_AGENT.lower())


# ══ ③ 连续失败的指数退避（纯函数）══════════════════════════════════════════════


class UpstreamFailureBackoffTests(SimpleTestCase):
    """退避窗口：30 / 60 / 120 / 240 / 300（封顶），一次干净 ⇒ 清零。

    ⚠️ **直接测纯函数**（spec §3 第 3 条）：⛔ 不去驱动整个命令循环——那会把
    「退避算得对不对」和「调度接线对不对」搅成一条用例（后者在
    `tests/test_jidanpu_sync_command.py`）。
    """

    def setUp(self):
        super().setUp()
        self.key = ("tenant-waf-risk", 1)
        # ⚠️ 进程内状态 ⇒ 用例之间会串味 ⇒ 两头都清。
        jidanpu_sync._UPSTREAM_FAILURE_COUNT.clear()
        jidanpu_sync._UPSTREAM_PENALTY_UNTIL.clear()
        self.addCleanup(jidanpu_sync._UPSTREAM_FAILURE_COUNT.clear)
        self.addCleanup(jidanpu_sync._UPSTREAM_PENALTY_UNTIL.clear)

    def test_the_window_doubles_from_thirty_seconds_and_caps_at_five_minutes(self):
        """`基数 × 2^(n-1)`，封顶 300 秒：30 / 60 / 120 / 240 / 300 / 300。

        失败方向：把指数写成线性（30/60/90…）或漏了封顶（第 5 次变 480）⇒ 本用例红。
        """
        self.assertEqual(jidanpu_sync.UPSTREAM_FAILURE_BACKOFF_BASE_SECONDS, 30)
        self.assertEqual(jidanpu_sync.UPSTREAM_FAILURE_BACKOFF_MAX_SECONDS, 300)
        got = [
            jidanpu_sync._upstream_backoff_seconds(count) for count in range(1, 7)
        ]
        self.assertEqual(got, [30, 60, 120, 240, 300, 300])

    def test_three_consecutive_failures_write_thirty_sixty_and_one_twenty_seconds(self):
        """连续 3 次失败 ⇒ `penalty_until` 依次是 `now+30` / `now+60` / `now+120`。

        失败方向：把窗口写成固定值（每次都 30 秒）⇒ 第 2、3 次断言红；
        或者计数不累加（每次都被重置成 1）⇒ 同样红。
        """
        untils = [
            jidanpu_sync._note_upstream_failure(self.key, now=NOW)
            for _ in range(3)
        ]
        self.assertEqual(
            [(until - NOW).total_seconds() for until in untils], [30, 60, 120]
        )
        self.assertEqual(jidanpu_sync._UPSTREAM_FAILURE_COUNT[self.key], 3)
        self.assertEqual(jidanpu_sync._UPSTREAM_PENALTY_UNTIL[self.key], untils[-1])

    def test_a_clean_round_clears_the_count_and_the_window(self):
        """一次干净 ⇒ 计数与窗口都被清掉（⛔ 不留下一个已经不带计数的窗口）。

        失败方向：只清计数不清窗口 ⇒ `penalty_until` 残留 ⇒ 本用例红；
        反过来只清窗口不清计数 ⇒ 下次失败直接跳到 60 秒 ⇒ 也红。
        """
        jidanpu_sync._note_upstream_failure(self.key, now=NOW)
        jidanpu_sync._note_upstream_failure(self.key, now=NOW)
        self.assertIn(self.key, jidanpu_sync._UPSTREAM_PENALTY_UNTIL)
        jidanpu_sync._clear_upstream_failure(self.key)
        self.assertNotIn(self.key, jidanpu_sync._UPSTREAM_FAILURE_COUNT)
        self.assertNotIn(self.key, jidanpu_sync._UPSTREAM_PENALTY_UNTIL)

    def test_the_counter_is_per_binding(self):
        """计数按 `(租户 schema, 绑定 pk)` 分开（⛔ 一个绑定的退避别拖累另一个）。

        失败方向：把两个字典写成模块级标量 / 单一键 ⇒ 另一把键读到的也是 1 ⇒ 本用例红。
        """
        other = ("tenant-waf-risk", 2)
        jidanpu_sync._note_upstream_failure(self.key, now=NOW)
        jidanpu_sync._note_upstream_failure(self.key, now=NOW)
        jidanpu_sync._note_upstream_failure(other, now=NOW)
        self.assertEqual(jidanpu_sync._UPSTREAM_FAILURE_COUNT[self.key], 2)
        self.assertEqual(jidanpu_sync._UPSTREAM_FAILURE_COUNT[other], 1)
        self.assertEqual(
            (
                jidanpu_sync._UPSTREAM_PENALTY_UNTIL[other] - NOW
            ).total_seconds(),
            30,
        )


# ══ ④ 降频：两个常量的**契约字面值** ═════════════════════════════════════════════


class WafRiskConstantTests(SimpleTestCase):
    """WAF 风险相关的**契约字面值**（spec §2.4；D-0366 改准）。

    ⚠️ **断言钉字面值，⛔ 不从模块常量派生**（`AGENTS.md` §5-G / E-0024）：
    派生写法（`assertEqual(X, jidanpu_upstream.X)`）恒真，改坏了也不会红。
    """

    def test_the_hall_tail_fast_path_is_retired(self):
        """⭐ D-0366：大厅快路（5 秒尾部）**整体退役**——它是对上家发数的大头，
        取数形状（无游戏名 GET）已随「别用空游戏名查」一并停用。

        ⛔ 防回潮：任何把 `HALL_TAIL_SECONDS` / `HALL_TAIL_PAGES` 加回模块的改动，
        本用例红。降频的现行口径 = 大厅轮询间隔列（默认 30 秒，D-0363）。
        """
        self.assertFalse(hasattr(jidanpu_upstream, "HALL_TAIL_SECONDS"))
        self.assertFalse(hasattr(jidanpu_upstream, "HALL_TAIL_PAGES"))

    def test_the_per_round_single_order_check_cap_is_five(self):
        """在途按单补核每轮上限 20 → 5（代价：轮转一圈变慢 `⌈在途/5⌉` 轮）。"""
        self.assertEqual(jidanpu_upstream.MY_ORDERS_LINK_CHECK_MAX, 5)
