"""`JIDANPU_PROXY` 的取值与建连接线（2026-10-23，业务方选 B：只让鸡蛋铺走代理）。

⭐ **出口池线 T2）**：出口从「每次建连轮换」改成**粘性 + 连续拦截才切换 + 隔离期**——
`current_proxy()` 恒回同一个出口，`note_blocked()` 连攒 `JIDANPU_EGRESS_FAIL_THRESHOLD`
（默认 3）次才把当前出口隔离 `JIDANPU_EGRESS_QUARANTINE_SECONDS`（默认 900 秒）并推进到下一个
未隔离的出口，`note_ok()` 一次成功即把连续计数清零。旧那条「按建连次数轮换」的用例
（`next_proxy()`）随之删掉——**语义被替换了，用例跟着换**，⛔ 不是放松。
"""
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.business import jidanpu, jidanpu_proxy


@contextmanager
def _egress_env(proxies, **extra):
    """把出口相关的环境键全部**显式**设成给定值（⛔ 不受本机 `.env` 影响）。

    ⚠️ 空串 = 「这台机器没配这一项」⇒ 被测代码必须回落到**默认值**（阈值 3 / 隔离 900 秒）。
    """
    env = {
        "JIDANPU_PROXIES": proxies,
        "JIDANPU_PROXY": "",
        "JIDANPU_EGRESS_FAIL_THRESHOLD": "",
        "JIDANPU_EGRESS_QUARANTINE_SECONDS": "",
    }
    env.update(extra)
    with mock.patch.dict(os.environ, env):
        yield


class ProxyUrlTests(SimpleTestCase):
    def setUp(self):
        jidanpu_proxy.reset_counter_for_tests()

    def test_unset_is_none(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JIDANPU_PROXY", None)
            self.assertIsNone(jidanpu_proxy.proxy_url())

    def test_blank_is_none(self):
        """空白 ≠ 代理：留一行空值就是「没配」，⛔ 不能把空串交给 httpx。"""
        with mock.patch.dict(os.environ, {"JIDANPU_PROXY": "   "}):
            self.assertIsNone(jidanpu_proxy.proxy_url())

    def test_value_is_used_verbatim(self):
        with mock.patch.dict(
            os.environ, {"JIDANPU_PROXY": "http://u:p@10.0.0.1:8080"}
        ):
            self.assertEqual(
                jidanpu_proxy.proxy_url(), "http://u:p@10.0.0.1:8080"
            )


class ClientProxyWiringTests(SimpleTestCase):
    def test_open_client_pair_passes_the_proxy_to_httpx(self):
        """建连那一跳真的把 `proxy` 传给了 `httpx.Client`（⛔ 不只是 helper 有值）。"""
        binding = SimpleNamespace(
            jidanpu_base_url="https://example.jidanpu.com",
            account="acct",
            password="pwd",
        )
        with mock.patch.dict(
            os.environ, {"JIDANPU_PROXY": "http://u:p@10.0.0.1:8080"}
        ), mock.patch.object(jidanpu.httpx, "Client") as client_cls, mock.patch.object(
            jidanpu, "_PooledJidanpuClient"
        ):
            jidanpu._open_client_pair(binding)
        self.assertEqual(
            client_cls.call_args.kwargs.get("proxy"), "http://u:p@10.0.0.1:8080"
        )


class EgressStickyTests(SimpleTestCase):
    """出口**粘性** / 连续拦截阈值 / 隔离期（出口池线 T2）。

    ⚠️ 断言打**真实出口**（`current_proxy()` 的返回值与 `current_label()`），
    ⛔ 不读进程内私有字典（那样「换了实现但行为没变」也会红，反过来则可能假绿）。
    """

    H1 = "http://u1:pw-one@h1.invalid:1"
    H2 = "http://u2:pw-two@h2.invalid:2"
    H3 = "http://u3:pw-three@h3.invalid:3"

    def setUp(self):
        jidanpu_proxy.reset_counter_for_tests()
        #: ⚠️ 出口状态是**进程内**的 ⇒ 用完必须清（否则隔离期会漏到后面的用例 / 别的模块上）。
        self.addCleanup(jidanpu_proxy.reset_counter_for_tests)

    def test_current_proxy_is_sticky_across_calls(self):
        """同一进程反复调 5 次 ⇒ **同一个**出口（⛔ 不再「每建一次连换一个」）。"""
        with _egress_env(f"{self.H1},{self.H2}"):
            got = [jidanpu_proxy.current_proxy() for _ in range(5)]
        self.assertEqual(got, [self.H1] * 5)

    def test_the_third_consecutive_block_switches_to_the_next_outlet(self):
        """连续拦截 **2** 次不切、第 **3** 次切到下一个（默认阈值 3，⛔ 不由本机 `.env` 决定）。"""
        with _egress_env(f"{self.H1},{self.H2}"):
            self.assertEqual(jidanpu_proxy.current_proxy(), self.H1)
            self.assertEqual(jidanpu_proxy.note_blocked(), "counted", "第 1 次拦截不该切")
            self.assertEqual(jidanpu_proxy.note_blocked(), "counted", "第 2 次拦截不该切")
            self.assertEqual(jidanpu_proxy.current_proxy(), self.H1)
            self.assertEqual(jidanpu_proxy.note_blocked(), "switched", "第 3 次拦截应当切换")
            self.assertEqual(jidanpu_proxy.current_proxy(), self.H2)

    def test_a_quarantined_outlet_is_not_returned_even_after_the_counter_resets(self):
        """切走之后，**隔离期内**即使计数被清零（`note_ok()`）也⛔ 不回上一个出口。

        三出口池走满一圈：h1 → h2 → h3 →（再切一次就该**回头**了）⇒ 隔离期内的 h1 / h2
        **都不可选** ⇒ 只剩「全隔离 ⇒ 直连」。⛔ **不许回到 h1**——那正是「刚被 WAF
        按 IP 封掉的出口马上又用上」的形态。

        失败方向：隔离截止时刻不生效（或被写成 0 秒）⇒ 最后那次切换**回到 h1** ⇒ 本用例红。
        """
        with _egress_env(f"{self.H1},{self.H2},{self.H3}"):
            for _ in range(3):
                jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_proxy(), self.H2)
            jidanpu_proxy.note_ok()  # 连续计数清零（⛔ 但隔离期不清）
            for _ in range(3):
                jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_proxy(), self.H3)
            for _ in range(3):
                jidanpu_proxy.note_blocked()  # 环形回头 ⇒ 只能落在「全隔离」
            self.assertIsNone(jidanpu_proxy.current_proxy())

    def test_all_outlets_quarantined_falls_back_to_direct_without_raising(self):
        """全部出口都在隔离期 ⇒ 回 `None`（= 直连 = 同步不停摆），⛔ **不抛异常**。"""
        with _egress_env(f"{self.H1},{self.H2}"):
            for _ in range(3):
                jidanpu_proxy.note_blocked()
            for _ in range(3):
                jidanpu_proxy.note_blocked()
            self.assertIsNone(jidanpu_proxy.current_proxy())
            # 全隔离期间再报拦截也不许抛（那一发已经打出去了，调用点没有第二个出口可试）；
            # ⭐ 这一发只**计数**（`counted`）——「第一次切到直连」已经报过了。
            self.assertEqual(jidanpu_proxy.note_blocked(), "counted")

    def test_note_blocked_reports_which_of_the_three_outcomes_happened(self):
        """返回值三分（评审 I2）：`counted`（只计数）/ `switched`（真换到池内另一个出口）/
        `exhausted`（全在隔离期 ⇒ 回落直连）。

        ⚠️ 承重：调用方要靠它决定日志写哪句话 —— 全隔离时写「已切换到下一个出口」是**假话**
        （那一刻没有「下一个出口」，只有直连）。
        """
        with _egress_env(f"{self.H1},{self.H2}"):
            self.assertEqual(jidanpu_proxy.note_blocked(), "counted")
            self.assertEqual(jidanpu_proxy.note_blocked(), "counted")
            self.assertEqual(jidanpu_proxy.note_blocked(), "switched")
            self.assertEqual(jidanpu_proxy.note_blocked(), "counted")
            self.assertEqual(jidanpu_proxy.note_blocked(), "counted")
            self.assertEqual(jidanpu_proxy.note_blocked(), "exhausted")
            # ⭐ **只有「从池内出口第一次切到直连」才报 exhausted**；
            #    已经回落直连之后再被拦 ⇒ `counted`（不丢会话、不刷告警，只计数）。
            self.assertEqual(jidanpu_proxy.note_blocked(), "counted")

    def test_note_ok_resets_the_consecutive_count(self):
        """拦截 2 次 + 成功 1 次 + 拦截 2 次 = **不切**（成功一次即复位，⛔ 不是累计）。"""
        with _egress_env(f"{self.H1},{self.H2}"):
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_ok()
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_proxy(), self.H1)

    def test_current_label_is_index_and_host_only(self):
        """出口标签只许 `<下标>:<host>`：⛔ 账号密码、⛔ 查询串都不得出现（日志会带出去）。"""
        with _egress_env("http://u:pw-must-not-leak@host.invalid:1/?token=s3cr3t"):
            label = jidanpu_proxy.current_label()
        self.assertEqual(label, "0:host.invalid")
        self.assertNotIn("p", label)
        self.assertNotIn("s3cr3t", label)
        self.assertNotIn("pw-must-not-leak", label)

    def test_a_single_direct_outlet_can_switch_to_none_without_raising(self):
        """池里只有 `direct` 时切到 `None` 也不抛（单出口机器不许因为「没得切」而炸）。"""
        with _egress_env("direct"):
            self.assertIsNone(jidanpu_proxy.current_proxy())
            for _ in range(4):
                jidanpu_proxy.note_blocked()  # 第 3 次起「切」到一个已隔离的池
            self.assertIsNone(jidanpu_proxy.current_proxy())
            self.assertTrue(jidanpu_proxy.current_label().endswith(":direct"))

    def test_blocking_a_non_current_outlet_only_quarantines_that_outlet(self):
        """**非当前出口**攒到阈值 ⇒ 只隔离它自己：⛔ 不切当前下标、⛔ 不回 `switched`（评审 ②）。

        现场：同一轮里 A 出口被切走之后，仍在用 A 客户端的那几段会继续报 468 ——
        那些 468 的归属是 A，⛔ 不是「当时的当前出口」。这里在出口池这一层把那条分支钉住：
        报一个**没在被用**的出口，它到阈值只把自己隔离掉，当前出口一个字都不动。
        """
        with _egress_env(f"{self.H1},{self.H2},{self.H3}"):
            for _ in range(3):
                jidanpu_proxy.note_blocked()  # 当前出口 h1 到阈值 ⇒ 切到 h2
            self.assertEqual(jidanpu_proxy.current_label(), "1:h2.invalid")
            # 报一个**没在被用**的出口 h3 三次（到阈值）
            outcomes = [jidanpu_proxy.note_blocked(self.H3) for _ in range(3)]
            self.assertEqual(
                outcomes, ["counted", "counted", "counted"],
                "非当前出口到阈值也⛔ 不许回 switched / exhausted",
            )
            self.assertEqual(
                jidanpu_proxy.current_label(), "1:h2.invalid",
                "当前出口被非当前出口的拦截推着动了",
            )
            # ⭐ 反证 h3 真的被隔离了：当前出口也到阈值时，**下一个可用出口只有 h3**
            #    ⇒ 若 h3 没被隔离，这里会 `switched` 到 h3；它被隔离了 ⇒ 只能回落直连。
            switched = [jidanpu_proxy.note_blocked() for _ in range(3)]
        self.assertEqual(switched[-1], "exhausted", f"h3 没被隔离？{switched}")
        self.assertIsNone(jidanpu_proxy.current_proxy())

    def test_blocking_an_outlet_that_is_not_in_the_pool_is_a_plain_counted_noop(self):
        """`outlet` 不在池里（运维改过配置）⇒ **不抛、只回 `counted`、什么都不动**（评审 ②）。

        语义（代码里的判据）：那种出口**无处可切** ⇒ 当一次没有下文的计数；
        ⛔ 不许把它算到当前出口头上（那会拿一个不相干的 468 把健康出口隔离掉）。
        """
        with _egress_env(f"{self.H1},{self.H2}"):
            before = jidanpu_proxy.current_label()
            self.assertEqual(
                jidanpu_proxy.note_blocked("http://gone:pw@old.invalid:9"), "counted"
            )
            self.assertEqual(jidanpu_proxy.current_label(), before)
            # ⛔ 也不许偷偷计入当前出口：再拦当前出口 2 次（共 2 次）仍不该切。
            jidanpu_proxy.note_blocked()
            jidanpu_proxy.note_blocked()
            self.assertEqual(jidanpu_proxy.current_label(), before)


class PoolStaleTests(SimpleTestCase):
    """出口换了 ⇒ **整池**作废（修复轮 2 / 评审 ①②）。"""

    def setUp(self):
        from apps.business import jidanpu

        jidanpu._SHARED_POOL = None
        jidanpu._SHARED_MEMBER_POOL = None
        super().setUp()

    def test_mark_all_stale_refreshes_every_binding_and_only_closes_on_acquire(self):
        """`mark_all_stale()` ⇒ **每一条**绑定下次 `acquire` 都换新；且**标记时不关**。

        ⚠️ 两条承重（评审 ①②）：① 出口是**进程级**的 ⇒ 只丢「报拦截那一条」会让其余绑定继续
        从旧 IP 发请求、而它们的 468 被记到**新**出口账上（把健康出口误隔离）；
        ② 调用点可能在**段中间**（同一轮后面的段还要用这个客户端）⇒ 标记时关掉会让后续段
        撞 `RuntimeError: client has been closed`，被记成假故障。
        """
        from apps.business import jidanpu

        bindings = [self._binding(1), self._binding(2)]
        made, closed = [], []

        def fake_open(binding):
            made.append((binding.pk, object()))
            return made[-1][1], object()

        def fake_close(client, http):
            closed.append(client)

        with mock.patch.object(jidanpu, "_open_client_pair", side_effect=fake_open), \
             mock.patch.object(jidanpu, "close_client_pair", side_effect=fake_close), \
             mock.patch.object(jidanpu, "_schema_name", return_value="t"):
            pool = jidanpu.UpstreamClientPool()
            firsts = [pool.acquire(binding)[0] for binding in bindings]
            pool.mark_all_stale()
            # ⭐ 标记**不当场关**：段中间还要用
            self.assertEqual(closed, [])
            seconds = [pool.acquire(binding)[0] for binding in bindings]
        self.assertEqual(len(made), 4, "两条绑定都应该重建")
        self.assertIsNot(firsts[0], seconds[0])
        self.assertIsNot(firsts[1], seconds[1])
        self.assertEqual(sorted(closed, key=id), sorted(firsts, key=id), "旧的两份都该关掉")
        # ⚠️ 新一代**不会**在下一轮又被判作废（否则每轮重建 = 每轮重新登录 = WAF 的老路）
        with mock.patch.object(jidanpu, "_open_client_pair", side_effect=fake_open), \
             mock.patch.object(jidanpu, "close_client_pair", side_effect=fake_close), \
             mock.patch.object(jidanpu, "_schema_name", return_value="t"):
            self.assertIs(pool.acquire(bindings[0])[0], seconds[0])

    @staticmethod
    def _binding(pk):
        return SimpleNamespace(
            jidanpu_base_url="https://pool-stale.test.invalid",
            account=f"acc-pool-{pk}",
            password="pw-canary-pool",
            pk=pk,
        )


class _PoolResetMixin:
    def setUp(self):
        from apps.business import jidanpu

        jidanpu._SHARED_POOL = None
        jidanpu._SHARED_MEMBER_POOL = None
        super().setUp()


class PooledSessionTests(_PoolResetMixin, SimpleTestCase):
    def test_invalidation_closes_both_parts_of_the_pooled_session(self):
        from apps.business import jidanpu

        binding = SimpleNamespace(
            jidanpu_base_url="https://example.jidanpu.com", account="acct", password="pwd", pk=7,
        )
        client, http = mock.Mock(), mock.Mock()
        with (
            mock.patch.object(jidanpu, "_open_client_pair", return_value=(client, http)),
            mock.patch.object(jidanpu, "_schema_name", return_value="tenant"),
        ):
            pool = jidanpu.UpstreamClientPool()
            pool.acquire(binding)
            pool.invalidate(binding)

        client.close.assert_called_once_with()
        http.close.assert_called_once_with()

    def test_acquire_reuses_the_pair_until_invalidated(self):
        """同一个绑定两次 `acquire` 拿到**同一个对象**（= 不重新登录）；invalidate 之后才重建。"""
        from apps.business import jidanpu

        binding = SimpleNamespace(
            jidanpu_base_url="https://example.jidanpu.com",
            account="acct",
            password="pwd",
            pk=7,
        )
        made = []

        def fake_open(_binding):
            made.append(object())
            return (made[-1], object())

        with mock.patch.object(jidanpu, "_open_client_pair", side_effect=fake_open), \
             mock.patch.object(jidanpu, "close_client_pair"), \
             mock.patch.object(jidanpu, "_schema_name", return_value="t"):
            pool = jidanpu.UpstreamClientPool()
            first, _ = pool.acquire(binding)
            second, _ = pool.acquire(binding)
            self.assertIs(first, second)
            self.assertEqual(len(made), 1)
            pool.invalidate(binding)
            third, _ = pool.acquire(binding)
            self.assertIsNot(first, third)
            self.assertEqual(len(made), 2)


class MemberSessionTests(_PoolResetMixin, SimpleTestCase):
    def _binding(self):
        return SimpleNamespace(
            jidanpu_base_url="https://member.jidanpu.com",
            account="acct",
            password="pwd",
            pk=11,
        )

    def test_member_session_reuses_one_pair_across_rounds(self):
        """下家①式：两轮取到的**是同一对客户端**（= 没有重新登录）。"""
        from apps.business import jidanpu

        made = []

        def fake_open(_binding):
            made.append(object())
            return (made[-1], object())

        with mock.patch.object(jidanpu, "_open_client_pair", side_effect=fake_open), \
             mock.patch.object(jidanpu, "close_client_pair"), \
             mock.patch.object(jidanpu, "_schema_name", return_value="t"):
            with jidanpu.member_session(self._binding()) as first:
                pass
            with jidanpu.member_session(self._binding()) as second:
                pass
        self.assertIs(first[0], second[0])
        self.assertEqual(len(made), 1)

    def test_auth_error_invalidates_the_member_pair(self):
        """`AuthError` ⇒ 丢掉这一份，下一次换新的（并重新走建连）。"""
        from apps.business import jidanpu
        from jidanpu.errors import AuthError

        made = []

        def fake_open(_binding):
            made.append(object())
            return (made[-1], object())

        with mock.patch.object(jidanpu, "_open_client_pair", side_effect=fake_open), \
             mock.patch.object(jidanpu, "close_client_pair"), \
             mock.patch.object(jidanpu, "_schema_name", return_value="t"):
            with self.assertRaises(AuthError):
                with jidanpu.member_session(self._binding()):
                    raise AuthError("登录失败")
            with jidanpu.member_session(self._binding()) as after:
                pass
        self.assertEqual(len(made), 2)
        self.assertIs(after[0], made[-1])

    def test_member_pool_and_upstream_pool_are_different_objects(self):
        from apps.business import jidanpu

        self.assertIsNot(jidanpu.member_pool(), jidanpu.shared_pool())


class SharedEgressStateTests(SimpleTestCase):
    """⭐ 2026-10-26：**跨进程共享出口决策**（同一台机的多个常驻单元共用一份状态）。

    场景：同步拆成上家 / 快路 / 回拉 / outbox 多个单元后，各进程若各自选出口，一个单元切走了、
    其余仍挂在被封 IP 上。设了 `JIDANPU_EGRESS_STATE_FILE` ⇒ 所有单元读写同一份状态。
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self._dir = tempfile.mkdtemp()
        self._state = f"{self._dir}/egress.json"
        self._env = mock.patch.dict(
            os.environ,
            {
                "JIDANPU_PROXIES": "http://u:p@h1.invalid:1,http://u:p@h2.invalid:2",
                "JIDANPU_EGRESS_STATE_FILE": self._state,
            },
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        jidanpu_proxy.reset_counter_for_tests()
        self.addCleanup(jidanpu_proxy.reset_counter_for_tests)

    def test_a_switch_in_one_process_is_visible_to_another(self):
        for _ in range(3):
            jidanpu_proxy.note_blocked()
        self.assertEqual(jidanpu_proxy.current_label(), "1:h2.invalid")
        # 模拟另一个进程：内存清空后首次调用必须**从文件读回** H2（而不是回到下标 0）。
        jidanpu_proxy._restore({})
        self.assertEqual(jidanpu_proxy.current_label(), "1:h2.invalid")

    def test_quarantine_survives_a_fresh_process(self):
        for _ in range(3):
            jidanpu_proxy.note_blocked()      # H1 到阈值 ⇒ 隔离、切 H2
        for _ in range(3):
            jidanpu_proxy.note_blocked()      # H2 到阈值 ⇒ 全隔离、回落直连
        self.assertEqual(jidanpu_proxy.current_label(), "-:direct")
        jidanpu_proxy._restore({})            # 新进程
        self.assertEqual(jidanpu_proxy.current_label(), "-:direct")
        self.assertEqual(jidanpu_proxy.note_blocked(), "counted")  # 直连上再挨一发只计数

    def test_the_state_file_carries_no_userinfo_and_is_private(self):
        """状态文件 ⛔ 不含 `user:pass@`，且权限必须是 0600（与同目录 `.env` 同口径）。

        失败方向（改前实测都成立）：键是**完整出口 URL** ⇒ `shared/egress-state.json`
        里明文躺着代理账号 / 密码；`open(..., "w")` 不 `chmod` ⇒ 落盘是 `-rw-r--r--`
        （同目录的 `shared/.env` 是 0600）⇒ 凡能读这台机共享目录的人都拿到出口凭据。
        """
        for _ in range(2):
            jidanpu_proxy.note_blocked()      # 触发一次落盘（含 blocked 两个键）
        text = Path(self._state).read_text(encoding="utf-8")
        self.assertNotIn("u:p@", text, "状态文件里出现了明文 userinfo（账号 / 密码）")
        self.assertNotIn("h1.invalid:1", text, "状态文件里出现了完整出口 URL")
        self.assertEqual(
            os.stat(self._state).st_mode & 0o777, 0o600,
            "状态文件必须是 0600（与同目录 .env 同口径）",
        )
