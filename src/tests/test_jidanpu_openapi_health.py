"""健康列（`apps/business/jidanpu_openapi_health.py`，本线 **T12**）的判据。

> spec：`docs/specs/2026-10-08-jidanpu-official-api-binding-design.md`
> §9.4（健康列要能回答的三个问题）/ §9.5（后台也要认得出「权限被收回」）/ §4.2 的三列定位
> / §5.2 与 §19.2（部署冲突）/ §13.3-C8（`callback_watermark_at` ⛔ 不上页面）；任务书 T12。

## 这份文件守什么

1. ⭐ **§9.4 第 2 问的正面判据**：**`callback_last_at` 在往前、水位不动** ⇒ 结论必须是
   **「这台机器没人消费」**；**两个都在动** ⇒ **不是**（两条都钉，⛔ 不只测正例）。
2. ⭐ **低单量不报警**（§9.4 末段 / A19）：一条**很久以前**的回调 ⇒ ⛔ 结论里**不许**出现
   异常 / 报警那种话（「多久没回调」这件事本模块压根不看）。
3. **四种结论各自钉字段**（`text` / `bad`），含①式与「从没收到过」两档信息态。
4. ⭐ **§9.5 的三条**（权限被收回）：
   - ① `mode` **保持不动**（⛔ 不自动切回登录式 —— 那是改用户的配置）；
   - ② 结论进 `last_error`（人话，含 §9.3 那一档「去开通 / 或改用账号 + 密码」的骨架）；
   - ③ 那个必拒的接口**不再反复打**（**轮内**：一条记录只打一次；**轮与轮之间**：退避窗口内一次不打）。
5. **①③档必须分得开**（C9）：网络 / 5xx ⇒ ③，⛔ **不许**进退避（那会把一次抖动变成静默停摆）。

## 三条纪律

- ⛔ **零真实网络**：假上游复用 `tests.test_jidanpu_openapi._Upstream`（MockTransport），
  地址是 `.invalid` 顶级域 —— 桩没接上就永远解析不了。
- ⛔ **夹具一律合成值**（`AGENTS.md` §5-C / E-0077）：码是 22 位合成串、密钥 / client_id 都是
  `synthetic` / `canary`、时刻是写死的墙钟。
- ⛔ **断言打在数据库行 / 真请求记录上**（E-0009）：`binding.last_error`、`token_calls()` 的条数。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.core.management import call_command
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu_openapi, jidanpu_openapi_consume, jidanpu_openapi_health
from apps.business.management.commands import jidanpu_sync
from apps.business.models import (
    Game,
    JidanpuUpstreamBinding,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
)
from apps.callback import signature
from apps.callback import wsgi as callback_wsgi
#: ⭐ 复用 T2 那条线的假上游与合成凭据（⛔ 不另写一个假上游：两份桩必然漂）。
from tests.test_jidanpu_openapi import (
    BASE_URL,
    CLIENT_ID,
    ORDER_STATUS_PATH,
    SENTINEL_SECRET,
    TOKEN_PATH,
    _Upstream,
)
from tests.p5_auth_helpers import make_logged_in_client

# ── 合成夹具值 ────────────────────────────────────────────────────────────────

#: 22 位合成码（形状 = `[A-Za-z0-9_-]{22}`，照接收器的定长正则）。
CODE_MINE = "Ab3_-health_tests_A222"
assert len(CODE_MINE) == 22

#: 密钥文件的**首行注释**（接收器 spec §5.3 的固定文案）。
KEYS_HEADER = "# 团团通 · 鸡蛋铺官方 API 回调密钥（0600；由「保存绑定」自动维护；⛔ 人工改要重启接收器）"
SECRET_MINE = "tt-health-secret-0001"

#: 上家单号（远端）与我方单号（20 位）。
REMOTE_ID_A = "20261008000000000911"
REMOTE_ID_B = "20261008000000000912"
ORDER_NO_A = "20261008000000000811"
ORDER_NO_B = "20261008000000000812"

#: 写死的墙钟（⛔ 不用 `timezone.now()`：阈值判据的读数要能对着字面值断言）。
NOW = datetime(2026, 10, 8, 13, 0, 0, tzinfo=dt_timezone.utc)
#: 一条回调的接收时刻（比 `NOW` 早 3 小时 —— 「水位数小时不动」那一档）。
LONG_AGO = NOW - timedelta(hours=3)

#: 实测的**外层门槛**文案（spec §3.3 F12/F15）：`取 token` 这一步回它 ⇒ ①档。
OUTER_GATE_MSG = "您没有开放平台接入权限"
#: ⭐ A 档人话里必须出现的几个词（spec §9.3 逐字那一句的骨架）。
#: ⛔ 不整句相等 —— 那种断言会把「把对方原文带上」这条纪律一起钉死，而对方原文正是这一档的信息。
A_TIER_NEEDLES = ("还没开通", "开放平台", "账号 + 密码")

#: 我方租户 schema 名（`TenantTestCase` 建的那个；`--tenant` 闸门要用真值，⛔ 不猜）。
TENANT_SCHEMA = "test"


class _Clock:
    """可控单调钟（token 缓存与退避窗口都用 `time.monotonic`）。"""

    def __init__(self, now: float = 10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FrozenWallClock:
    """接收器 `_now_iso()` 的挂钟替身（水位要能对着**确定的值**断言）。"""

    value: datetime = LONG_AGO

    @classmethod
    def now(cls, tz=None):
        return cls.value


class _HealthCase(TenantTestCase):
    """临时事件 / 密钥文件 + 假上游 + 一行②式绑定（+ 两张我方订单与链路）。"""

    def setUp(self):
        super().setUp()
        jidanpu_openapi.reset_token_cache()
        jidanpu_openapi_consume.reset_process_state()
        self.addCleanup(jidanpu_openapi.reset_token_cache)
        self.addCleanup(jidanpu_openapi_consume.reset_process_state)
        #: ⚠️ 退避窗口用 `time.monotonic` ⇒ 整个用例期间换成**可控**钟：否则「窗口内 / 窗口外」
        #: 这两条判据会变成「跑得快不快」（那种读数是墙钟碰巧给的，不是判据）。
        self.clock = _Clock()
        monotonic = mock.patch.object(jidanpu_openapi.time, "monotonic", self.clock)
        monotonic.start()
        self.addCleanup(monotonic.stop)

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events = self.tmp / "shared" / "callback" / "events.jsonl"
        self.keys_file = self.tmp / "shared" / "callback-keys.env"
        self.keys_file.parent.mkdir(parents=True, exist_ok=True)
        self.keys_file.write_text(
            KEYS_HEADER + "\n" + f"{CODE_MINE}\t{SECRET_MINE}\n", encoding="utf-8",
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

        self.game = Game.objects.create(name="测试游戏甲")
        self.binding = self.make_binding()
        self.orders = {
            REMOTE_ID_A: self.make_order_and_link(ORDER_NO_A, REMOTE_ID_A),
            REMOTE_ID_B: self.make_order_and_link(ORDER_NO_B, REMOTE_ID_B),
        }

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def make_binding(self, *, mode="openapi", enabled=True, reconcile_seconds=600):
        username = "kefu-health-a"
        make_logged_in_client(
            username=username, groups=("客服",), nodes=("order.change_status",),
        )
        return JidanpuUpstreamBinding.objects.create(
            user=User.objects.get(username=username), jidanpu_base_url=BASE_URL,
            account="acc-health-a", password="pw-canary-health", mode=mode,
            client_id=CLIENT_ID, client_secret=SENTINEL_SECRET, callback_code=CODE_MINE,
            enabled=enabled, reconcile_seconds=reconcile_seconds,
        )

    def make_order_and_link(self, order_no, remote_id):
        order = Order.objects.create(
            game=self.game, order_no=order_no, status=OrderStatus.IN_PROGRESS,
            total_price=Decimal("100.00"),
        )
        JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=order, upstream_order_id=remote_id,
        )
        return order

    def post_callback(self, *, remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A,
                      received_at=LONG_AGO) -> str:
        """走**真接收器**打一条 POST（⛔ 不手搓一份记录形状）。"""
        params = {
            "orderNo": remote_id, "sourceOrderNo": source_order_no, "status": "问题单",
            "note": "", "action": "10", "timestamp": "1791500000",
        }
        body = dict(params)
        body[signature.SIGN_FIELD_NAME] = signature.compute_sign(params, SECRET_MINE)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": f"{callback_wsgi.CALLBACK_PATH_PREFIX}{CODE_MINE}",
            "QUERY_STRING": "", "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(raw)), "wsgi.input": BytesIO(raw),
            "REMOTE_ADDR": "127.0.0.1", "HTTP_X_REAL_IP": "203.0.113.9",
        }
        status: dict = {}

        def start_response(line, headers):
            status["line"] = line

        with mock.patch.object(callback_wsgi, "datetime", _FrozenWallClock):
            _FrozenWallClock.value = received_at
            callback_wsgi.application(environ, start_response)
        return status["line"]

    # ── 跑一轮（**真的**走调度层那一段）──────────────────────────────────────

    def run_round(self, *, now=NOW, binding=None):
        """跑一轮②式那一段（`jidanpu_sync._run_upstream_binding`）。

        ⚠️ 走**真的**那段代码，⛔ 不是把消费函数单独调一遍：§9.5 有一条判据是「本轮不再打那个
        必拒的接口」，而那只在**整行**的上下文里才成立（消费 + 兜底对账两段都要那一跳）。
        """
        from apps.business.management.commands.jidanpu_sync import Command

        command = Command(stdout=io.StringIO(), stderr=io.StringIO())
        tenant = mock.Mock(schema_name=TENANT_SCHEMA)
        return command._run_upstream_binding(tenant, binding or self.binding, now=now)

    # ── 读数 ────────────────────────────────────────────────────────────────

    def refresh(self):
        return JidanpuUpstreamBinding.objects.get(pk=self.binding.pk)

    def state(self, binding=None, *, now=NOW):
        return jidanpu_openapi_health.consumer_state(binding or self.refresh(), now=now)

    def order_status(self, remote_id) -> str:
        return Order.objects.get(pk=self.orders[remote_id].pk).status


# ══ ① §9.4 第 2 问：这台机器有没有人在消费（正反两条都要能红）══════════════════


class ConsumerVerdictTests(_HealthCase):
    """⭐ 判据 = 「`callback_last_at` 在动 **且** 水位长期不前进」⇒ 没人消费；两个都动 ⇒ 不是。"""

    def test_callback_alive_but_the_watermark_stuck_means_nobody_is_consuming(self):
        """⭐ **正面判据**（§9.4 第 2 问 / §5.2 那条部署冲突的可观测判据）。

        ⛔ 弄红它：把 `bad` 恒 `False`、或把文案换成中性话 ⇒ 第一条断言红；
        把两个时刻从人话里去掉 ⇒ 后两条断言红（「哪一条、停在什么时候」是这档唯一的读数）。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_last_at=NOW, callback_watermark_at=LONG_AGO,
        )

        state = self.state()

        self.assertTrue(state.bad, state)
        self.assertIn("没有任何东西在处理它", state.text)
        # D-0302：`_stamp` 出**项目时区**墙钟——UTC 13:00 / 10:00
        # 在北京墙钟上是 21:00 / 18:00（旧写法裸 strftime 出 UTC 钟点、健康页差 8 小时）。
        self.assertIn("2026-10-08 21:00", state.text)   # 最近一次收到（NOW，UTC 13:00）
        self.assertIn("2026-10-08 18:00", state.text)   # 水位（LONG_AGO，UTC 10:00）

    def test_both_moving_is_not_flagged(self):
        """⭐ **反向对照**（⛔ 只测正例等于没测）：两个时刻一起往前 ⇒ **不是**「没人消费」。"""
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_last_at=NOW, callback_watermark_at=NOW - timedelta(seconds=1),
        )

        state = self.state()

        self.assertFalse(state.bad, state)
        self.assertNotIn("没有任何东西在处理它", state.text)

    def test_a_stale_watermark_alone_is_not_enough(self):
        """⚠️ 合取的第一条承重：水位很久没动，但**回调只比它新一点点** ⇒ 不说「没人消费」。

        ⛔ 弄红它：把判据改成「只看 `now - watermark`」⇒ 这一条立刻红。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_last_at=LONG_AGO + timedelta(seconds=30), callback_watermark_at=LONG_AGO,
        )

        self.assertFalse(self.state().bad)

    def test_a_big_gap_that_just_appeared_is_not_enough(self):
        """⚠️ 合取的第二条承重：差很大、但**水位刚刚才动过**（正在追）⇒ 不判「没人消费」。

        ⛔ 弄红它：把 `now - watermark > window` 那半条去掉 ⇒ 这一条立刻红
        （页面上会挂在「正常追赶」那一格里亮红）。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_last_at=NOW, callback_watermark_at=NOW - timedelta(minutes=5),
        )

        self.assertFalse(self.state().bad)

    def test_a_slow_but_alive_consumer_stays_information_only(self):
        """⭐ **低单量不报警**（§9.4 末段 / A19）：回调是**两天前**的 ⇒ 差恒为 0 ⇒ 信息档。

        ⛔ 弄红它：加一句「`now - callback_last_at` 超过阈值就说异常」⇒ 这一条立刻红
        （那正是 spec 明禁的「多久没回调就报警」）。
        """
        received = NOW - timedelta(days=2)
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_last_at=received, callback_watermark_at=received,
        )

        state = self.state()

        self.assertFalse(state.bad, state)
        for banned in ("异常", "报警", "故障", "没人", "没有任何东西"):
            self.assertNotIn(banned, state.text)
        self.assertIn("有回调进来时都处理得了", state.text)

    def test_the_never_received_case_is_information_not_an_alarm(self):
        """一条回调都没来过 ⇒ **判不了**（⛔ 不说假话，也⛔ 不报警）。"""
        state = self.state()

        self.assertFalse(state.bad)
        self.assertEqual(state.text, jidanpu_openapi_health.NEVER_RECEIVED_TEXT)

    def test_a_password_mode_binding_has_no_callbacks_to_consume(self):
        """①式那一行**不适用**这一格 ⇒ 既⛔ 不报警、也⛔ 不说「还没收到过」。"""
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(mode="password")

        state = self.state()

        self.assertFalse(state.bad)
        self.assertIn("没有回调要消费", state.text)

    def test_the_window_follows_the_bindings_own_reconcile_interval(self):
        """窗口 = `max(900, 10 × 该行自己的 reconcile_seconds)`（⛔ 不写死秒数）。

        ⛔ 弄红它：把 `LAG_ROUNDS` 或 `LAG_WINDOW_FLOOR` 换个数 ⇒ 两条断言各红一条。
        """
        self.assertEqual(
            jidanpu_openapi_health.lag_window(self.binding), timedelta(seconds=6000),
        )
        self.binding.reconcile_seconds = 30
        # 小间隔 ⇒ **下限**兜住（30 × 10 = 300 < 900）；大间隔 ⇒ 轮数那一支生效。
        self.assertEqual(
            jidanpu_openapi_health.lag_window(self.binding), timedelta(seconds=900),
        )
        self.binding.reconcile_seconds = 1800
        self.assertEqual(
            jidanpu_openapi_health.lag_window(self.binding), timedelta(seconds=18000),
        )

    def test_a_zero_interval_falls_back_to_the_models_default(self):
        """`reconcile_seconds` 存 `0`（配置写坏）⇒ 按**模型列自己的默认值**算（E-0021 家族）。"""
        self.binding.reconcile_seconds = 0
        default = JidanpuUpstreamBinding._meta.get_field("reconcile_seconds").default
        self.assertEqual(
            jidanpu_openapi_health.lag_window(self.binding),
            timedelta(seconds=max(
                jidanpu_openapi_health.LAG_WINDOW_FLOOR,
                jidanpu_openapi_health.LAG_ROUNDS * default,
            )),
        )

    def test_a_naive_datetime_does_not_blow_up(self):
        """库里取回 naive 时刻 ⇒ ⛔ 不许抛 `TypeError`（那在一个纯读的视图里就是 500）。

        ⛔ 弄红它：去掉 `_as_utc` 的归一化 ⇒ 这一条以 `TypeError` 红。
        """
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            callback_last_at=datetime(2026, 10, 8, 12, 0, 0),        # naive
            callback_watermark_at=datetime(2026, 10, 8, 9, 0, 0),    # naive
        )

        self.assertTrue(self.state().bad)


# ══ ② §9.5：权限被收回时认得出（三条都要能红）══════════════════════════════════


class PermissionRevokedTests(_HealthCase):
    """spec §9.5：每一轮同步里碰到 A 档（没开通）时的三条。"""

    def setUp(self):
        super().setUp()
        #: 取 token 这一步回 A 档（实测原文）。
        self.upstream.token_reply(status=200, payload={"code": 0, "msg": OUTER_GATE_MSG})

    @staticmethod
    def _boom_request_token(*_args, **_kwargs):
        raise AssertionError(
            "退避窗口内又打了一次取 token —— spec §9.5 ② 明令「⛔ 不每轮反复打一个必拒的接口」"
        )

    def test_the_probe_classifies_this_exact_reply_as_tier_a(self):
        """前置守卫（E-0081 同族）：下面那三条判据打的**确实是①档**。

        ⚠️ 少了它，那三条可能打在③档上而**看起来一样绿**（把「我们这边坏了」说成「你没开通」）。
        ⛔ 弄红它：把 `classify_failure` 里取 token 那一支的 `any` 改成 `all`…
        （那两个词都在这一句里 ⇒ 仍 ①）；真正的红法是把 `OUTER_GATE_WORDS` 换成别的词。
        """
        with jidanpu_openapi.openapi_client(self.refresh()) as client:
            with self.assertRaises(jidanpu_openapi.OpenApiError) as caught:
                client.probe_token()
        self.assertIs(caught.exception.kind, jidanpu_openapi.FailureKind.CREDENTIALS)

    def test_a_network_failure_is_not_classified_as_tier_a(self):
        """反向对照：网络异常 ⇒ ③档（⛔ 不许说成「你没开通开放平台」，spec §9.1-C）。"""
        self.upstream.boom(TOKEN_PATH, httpx.ConnectError("synthetic transport failure"))
        with jidanpu_openapi.openapi_client(self.refresh()) as client:
            with self.assertRaises(jidanpu_openapi.OpenApiError) as caught:
                client.probe_token()
        self.assertIs(caught.exception.kind, jidanpu_openapi.FailureKind.OTHER)

    def test_the_mode_is_never_switched_back_automatically(self):
        """① `mode` **保持不动**（⛔ 不自动切回登录式 —— 那是**改用户的配置**）。"""
        self.post_callback()

        self.run_round()

        self.assertEqual(self.refresh().mode, "openapi")

    def test_the_conclusion_lands_in_last_error_as_plain_language(self):
        """② 结论进 `last_error`，而且是 §9.3 那一档的人话（含「去开通 / 或改用登录式」）。

        ⛔ 弄红它：把 `_failure_detail` 对①档那一支换成只留异常类型名 ⇒ 第一条断言红
        （用户看不懂 `OpenApiError`）。
        """
        self.post_callback()

        self.run_round()

        row = self.refresh()
        self.assertNotEqual(row.last_error, "")
        for needle in A_TIER_NEEDLES:
            self.assertIn(needle, row.last_error, row.last_error)
        self.assertNotEqual(row.last_error.strip(), "OpenApiError")
        # 水位⛔ 不前移（这一条没有任何结论 ⇒ 下一轮还要重试）。
        self.assertIsNone(row.callback_watermark_at)

    def test_the_rejected_call_is_not_repeated_within_the_round(self):
        """③（轮内）每轮对那个必拒的接口**只打一次**：两条本绑定的回调（两张单）也只打一次。

        ⛔ 弄红它：把「查不动 ⇒ 本轮停在这里」那条批处理口径去掉（改成每条都试一次）
        ⇒ `token_calls()` 变成 2、第一条断言红。
        """
        self.post_callback(remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A)
        self.post_callback(
            remote_id=REMOTE_ID_B, source_order_no=ORDER_NO_B,
            received_at=LONG_AGO + timedelta(seconds=1),
        )

        self.run_round()

        self.assertEqual(len(self.upstream.token_calls()), 1, self.upstream.token_calls())
        # 一张都没写成（查不动权威 ⇒ ⛔ 不拿回调里的值顶上）。
        self.assertEqual(self.order_status(REMOTE_ID_A), OrderStatus.IN_PROGRESS)
        self.assertEqual(self.order_status(REMOTE_ID_B), OrderStatus.IN_PROGRESS)

    def test_the_next_round_inside_the_window_does_not_hit_the_doomed_endpoint(self):
        """③（跨轮）⭐ **退避窗口**：下一轮**一次都不打**那个必拒的接口（spec §9.5 ②）。

        ⛔ 弄红它：把 `_token_for` 里那段「窗口内直接抛」去掉 ⇒ 第二轮又打一次取 token
        ⇒ 第二条断言红（=`token_calls()` 变 2）。

        ⚠️ 第二轮**必须**有一条新的本绑定回调，否则「没有新事件 ⇒ 本来就不打上游」会让这条判据
        变成恒真（那就不是在测退避了）。
        ⚠️ 第二轮**消费与兜底对账两段都要跳过**（它们都要那一跳才发得出请求）⇒ 用一个「一打就
        响亮失败」的替身把「两段都没打」变成一条能红的判据（只是数请求次数会漏掉「又打了但被我
        别的代码挡住」那种形态）。
        """
        self.post_callback(remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A)
        self.run_round()
        self.assertEqual(len(self.upstream.token_calls()), 1)

        self.post_callback(
            remote_id=REMOTE_ID_B, source_order_no=ORDER_NO_B, received_at=NOW,
        )
        with mock.patch.object(
            jidanpu_openapi, "_request_token", self._boom_request_token,
        ):
            self.run_round(now=NOW + timedelta(seconds=30))

        self.assertEqual(len(self.upstream.token_calls()), 1, self.upstream.token_calls())
        # ⛔ 窗口内也不是静默跳过：绑定 `last_error` 上有一句人话（含「等待窗口」）。
        self.assertIn("必被拒", self.refresh().last_error)

    def test_the_window_expires_and_the_endpoint_is_tried_again(self):
        """⚠️ 退避**不是**永久黑名单：窗口一过就真的再试一次（否则权限开回来也不恢复）。"""
        self.post_callback(remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A)
        self.run_round()
        self.assertEqual(len(self.upstream.token_calls()), 1)

        self.post_callback(
            remote_id=REMOTE_ID_B, source_order_no=ORDER_NO_B, received_at=NOW,
        )
        self.run_round(now=NOW + timedelta(seconds=30))
        self.assertEqual(len(self.upstream.token_calls()), 1, "窗口内不该再打")

        self.clock.advance(jidanpu_openapi.CREDENTIAL_BACKOFF_SECONDS + 1)
        self.run_round(now=NOW + timedelta(hours=1))

        self.assertEqual(len(self.upstream.token_calls()), 2, self.upstream.token_calls())

    def test_a_network_failure_never_enters_the_backoff(self):
        """⚠️ **①③必须分得开**（C9）：网络失败（③档）**不**进退避（下一轮照打）。

        ⛔ 弄红它：把 `note_credential_rejection` 里那条 `kind is not CREDENTIALS` 的守卫去掉
        ⇒ 一次网络抖动让这条路静默停摆十分钟：第三轮会带着 `token_blocked` 跳过整行
        ⇒ `token_calls()` 停在 2、第二条断言红。
        """
        self.upstream.boom(TOKEN_PATH, httpx.ConnectError("synthetic transport failure"))
        self.post_callback(remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A)
        self.run_round()
        self.assertEqual(len(self.upstream.token_calls()), 1)
        self.assertEqual(
            jidanpu_openapi.credential_backoff_reason(
                jidanpu_openapi.openapi_cache_key(self.refresh()),
            ),
            "",
            "③档（网络失败）被记进退避了",
        )

        self.upstream.errors.clear()
        self.post_callback(
            remote_id=REMOTE_ID_B, source_order_no=ORDER_NO_B, received_at=NOW,
        )
        self.run_round(now=NOW + timedelta(seconds=30))

        self.assertEqual(len(self.upstream.token_calls()), 2, "③档下一轮应当照打")

    def test_the_two_sections_of_one_round_share_the_same_verdict(self):
        """⭐ **一轮里只打一次**：消费那一段被判①档之后，**兜底对账那一段也不再打**。

        ⛔ 弄红它：把 `token_blocked` 那两个参数去掉（让两段各自去取 token）⇒ 这里红
        —— 实测一轮打**两次**（T12 的探针读数），而 spec §9.5 ③ 明令「⛔ 不每轮反复打一个
        必拒的接口」。
        ⚠️ **判据打在「谁调了取 token」上**（把那个函数换成「一调就响亮失败」的替身），
        ⛔ 不是只数请求次数：只数次数会漏掉「又打了、只是被别的代码挡住」那种形态。
        ⚠️ 这条与轮内那条（`test_the_rejected_call_is_not_repeated_within_the_round`）**不是
        同一件事**：那一条管「同一段里多条事件只打一次」，这一条管「同一轮的两段之间」。
        """
        self.post_callback(remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A)
        self.run_round()
        self.assertEqual(len(self.upstream.token_calls()), 1)

        # 第二轮：窗口内 ⇒ 两段**都不许**碰那个接口。
        self.post_callback(
            remote_id=REMOTE_ID_B, source_order_no=ORDER_NO_B, received_at=NOW,
        )
        with mock.patch.object(
            jidanpu_openapi, "_request_token", self._boom_request_token,
        ):
            self.run_round(now=NOW + timedelta(seconds=30))

    def test_the_test_connection_probe_bypasses_the_backoff(self):
        """⭐ 人主动点的「测试连接」**必须真的打上游**（⛔ 不许把窗口里的旧结论回给用户）。

        ⛔ 弄红它：把 `probe_token` 的 `allow_backoff=False` 去掉 ⇒ 这一条红
        （用户点「测试连接」会拿到一句十分钟前的旧结论）。
        """
        self.post_callback(remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A)
        self.run_round()
        calls_before = len(self.upstream.token_calls())
        self.assertEqual(calls_before, 1)

        with jidanpu_openapi.openapi_client(self.refresh()) as client:
            with self.assertRaises(jidanpu_openapi.OpenApiError):
                client.probe_token()

        self.assertEqual(len(self.upstream.token_calls()), calls_before + 1)

    def test_a_recovered_credential_clears_the_backoff(self):
        """窗口内对方把权限开回来 ⇒ 下一次真的取到 token 时**顺手摘掉**标记。

        ⛔ 弄红它：去掉 `_token_for` 成功那一支里的 `_credential_backoff.pop(...)` ⇒ 第一条断言红
        （标记会一直挂到窗口过完，页面上的「最后一次尝试」也不再更新）。
        """
        self.post_callback(remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A)
        self.run_round()
        key = jidanpu_openapi.openapi_cache_key(self.refresh())
        self.assertNotEqual(jidanpu_openapi.credential_backoff_reason(key), "")

        self.upstream.token_reply(status=200, payload=None)
        with jidanpu_openapi.openapi_client(self.refresh()) as client:
            client.probe_token()

        self.assertEqual(jidanpu_openapi.credential_backoff_reason(key), "")

    def test_the_backoff_key_carries_the_credential_dimension(self):
        """⚠️ 退避标记按**凭据**分（键含地址 / `client_id` / 密钥指纹，⛔ 不跨凭据串）。

        ⛔ 弄红它：把 `openapi_cache_key` 换成「只按绑定主键」⇒ 换了 `client_id` 之后键不变
        ⇒ 第一条断言红（旧凭据的结论会套到新凭据头上）。
        """
        key_before = jidanpu_openapi.openapi_cache_key(self.refresh())
        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(
            client_id=f"{CLIENT_ID}-other",
        )
        self.assertNotEqual(key_before, jidanpu_openapi.openapi_cache_key(self.refresh()))


# ══ ③ 接进命令：窗口内整行跳过，而且**不刷**「上一次尝试的时刻」════════════════


class CommandWiringTests(_HealthCase):
    def test_the_whole_binding_is_skipped_inside_the_window(self):
        """窗口内那一行**整行跳过**（照「停用 / 凭据不全」的既有口径：不打印噪音）。

        判据打在**终态**上：跑第二趟 `--once` 之后，绑定上的 `last_error` 仍是第一轮那句人话
        （⛔ 没被改成别的、也⛔ 没被清掉），而那个必拒的接口一次都没再打。
        """
        self.upstream.token_reply(status=200, payload={"code": 0, "msg": OUTER_GATE_MSG})
        self.post_callback(remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A)
        call_command("jidanpu_sync", "--once", "--tenant", TENANT_SCHEMA)
        first_error = self.refresh().last_error
        self.assertNotEqual(first_error, "")
        self.assertEqual(len(self.upstream.token_calls()), 1)

        self.post_callback(
            remote_id=REMOTE_ID_B, source_order_no=ORDER_NO_B, received_at=NOW,
        )
        call_command("jidanpu_sync", "--once", "--tenant", TENANT_SCHEMA)

        self.assertEqual(self.refresh().last_error, first_error)
        self.assertEqual(len(self.upstream.token_calls()), 1, self.upstream.token_calls())


# ══ ④ ⭐ 健康列：什么都没做成的那一轮⛔ 不许盖 `last_ok_at`════


class DidSomethingHealthTests(_HealthCase):
    """`did_something` 的判据必须是「**真的处理成了**」，⛔ 不是「有东西可处理 / 打过上游」。

    ⚠️ 原判据是 `reconcile.queried > 0 or consume.mine > 0`，两半都能在「什么都没做成」
    的那一轮成立：

    - `consume.mine > 0` = **有待处理的事件**（第一条就 `_RETRY`：查不动、水位一格不动、
      订单一个字没改 ⇒ 什么都没做成）；
    - `reconcile.queried > 0` = **真打出去了**（第一发就失败 ⇒ `stopped=True`、
      `applied=skipped=0` ⇒ 同样什么都没做成）。

    ⇒ 收紧成「有结论的那两档计数」（`applied` 写了 / `skipped` 有结论但不必写）
    —— 照裁定 1「什么都没做不许盖成功」的同一条口径。
    """

    def setUp(self):
        super().setUp()
        #: 对账锚是**进程内**的（键 = 租户 schema + 绑定 id）⇒ 不清掉的话，上一个用例留下的锚
        #: 会让这一轮「对账未到点」，而本用例第二幕要钉的正是「消费那一段真的处理成了」。
        jidanpu_sync._UPSTREAM_OPENAPI_RECONCILE_AT.clear()

    def test_a_round_where_every_query_retries_does_not_stamp_last_ok(self):
        """⭐ **全 `_RETRY` 的一轮不盖 `last_ok_at`**（本地有事件、上游一发都没问成）。

        两个读数各自承重（缺一个就会变成恒真/假绿）：
        ① 这一轮**真的试过了**（`token_calls()` 非空、水位仍是 `None`、订单一个字没改）
           —— 否则「没盖成功」可能只是「压根没跑」；
        ② 紧接着**上游恢复**的那一轮**盖上了**（阳性对照）—— 否则前一条在「健康列永远不盖」
           的实现下也绿，那是另一种假绿（把这一列变成死列）。

        ⛔ 弄红它（两处，任一都能弄红这一条）：把 `did_something` 写回
        `reconcile.queried > 0`（第一发就失败也成立）或 `consume.mine > 0`（有事件就成立）。
        """
        self.post_callback(remote_id=REMOTE_ID_A, source_order_no=ORDER_NO_A)
        #: ③档（网络失败）：⛔ 不进跨轮退避窗口 ⇒ 这一轮消费与对账**各自真打了一次**查询（都失败）。
        self.upstream.boom(ORDER_STATUS_PATH, httpx.ConnectError("合成：连不上"))

        self.run_round()

        fresh = self.refresh()
        self.assertTrue(self.upstream.token_calls(), "这一轮一次上游都没打 ⇒ 判据落在空转上")
        self.assertIsNone(
            fresh.callback_watermark_at, "水位动了 ⇒ 这一轮其实处理成了（夹具不对）",
        )
        self.assertEqual(self.order_status(REMOTE_ID_A), OrderStatus.IN_PROGRESS)
        self.assertIsNone(fresh.last_ok_at, "什么都没做成，却盖了「上一轮成功」")

        # ── 第二幕（阳性对照）：上游恢复 ⇒ 同一件事这一轮**真的处理成了** ⇒ 健康列必须盖上 ──
        self.upstream.errors.pop(ORDER_STATUS_PATH, None)
        self.upstream.reply(ORDER_STATUS_PATH, payload={
            "code": 1, "msg": None,
            "data": {
                "status": "已暂停", "creatTime": "2026-10-08 12:00:00", "operTime": None,
                "doneTime": None, "verifyTime": None,
            },
        })
        later = NOW + timedelta(seconds=60)
        self.run_round(now=later)

        self.assertEqual(self.order_status(REMOTE_ID_A), "PAUSED", "恢复之后这一轮没写成")
        self.assertEqual(
            self.refresh().last_ok_at, later,
            "真的处理成了却没盖成功（健康列成了死列）",
        )


__all__ = [
    "CommandWiringTests",
    "ConsumerVerdictTests",
    "DidSomethingHealthTests",
    "PermissionRevokedTests",
]
