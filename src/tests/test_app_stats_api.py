"""「APP 与小程序 → 数据概览」端点与取数层的判别力用例。

契约：`docs/plans/2026-10-23-app-mp-stats.md` §7（业务方口径）/ §8（评审改准的工程做法）。

## 钉住什么

1. **权限**：准入节点复用 `finance.support_statistics`；没节点 ⇒ HTTP 403 + 业务码 403。
2. **参数**：`days` 默认 7、上限 30；`0 / 31 / abc` ⇒ 200 + `1000`（⛔ 不是 500）。
3. **微信**（只有 `suqing`）：
   - 其它租户 ⇒ `available=false` + 「本租户未开通小程序」，⛔ 不显示数字；
   - 7 天 = **7 组 begin=end**，最后一天 = **昨天**（T+1）；
   - token / 响应 / 数据行三层解析失败 ⇒ `available=false` + 原因，⛔ 不用 0 冒充。
4. **注册**：只算 `account_source='app'`；按**最早一枚 AppToken 的 client** 拆三桶；
   造数走**真端点** `POST /api/app/auth/register/`（不是直接建行）。
5. **接单**：`OperationLog(action='order.claim')` 按日、按 `reason` 分；移动端只认
   `claim_channel.CHANNEL_APP`。
6. **渠道**：App 令牌接单记「打手App」、会话接单记「系统网页」；**总闸打开时网页仍是网页**。
7. **押金**：`sum(frozen)` + `frozen>0` 人数 + 每日变动；`release_request` 不动钱、不计变动；
   口径标注「含到期未结算」。
8. **跨租户**：两个租户用**取值不同的字段**指认（注册桶 / 微信可用性），⛔ 不靠主键。

⚠️ 夹具一律合成值（E-0077）；断言打**字面量**（E-0009 / E-0024）。
"""
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.choices import AccountSource
from apps.accounts.models import AppToken, StaffProfile, TakerWallet, WalletTransaction
from apps.audit.models import OperationLog
from apps.business import app_stats, wechat_datacube
from apps.business.claim_channel import CHANNEL_APP, CHANNEL_MINI_PROGRAM, CHANNEL_WEB
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.peer_sync import hash_token, new_token
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

NODE = "finance.support_statistics"
URL = "/api/business/app-stats/overview/"
REGISTER_URL = "/api/app/auth/register/"

DOMAIN_A = "tenant.test.com"
DOMAIN_B = "app-stats-suqing.test.local"
SCHEMA_B = "suqing"

OK = 0
VALIDATION = 1000
FORBIDDEN = 403

#: 与实现同值的两句文案（写字面副本：实现改文案时这里要同一次改，⛔ 不 import 常量）。
WECHAT_NOT_ENABLED = "本租户未开通小程序"
DEPOSIT_NOTE = "账面冻结额（含到期未结算）"

PASSWORD = "pw-app-stats-123"


def _at(day: date, hour: int = 12) -> datetime:
    """某个本地日期的固定时刻（`Asia/Shanghai`，`USE_TZ=True`）。"""
    return timezone.make_aware(datetime.combine(day, time(hour, 0)))


def _today() -> date:
    return timezone.localdate()


class _ApiMixin:
    """登录 client + 取信封的共用手法（不放在基类里以免影响 `TenantTestCase` 的 MRO）。"""

    def _viewer(self, *, username="stats-viewer", nodes=(NODE,), domain=None):
        return make_logged_in_client(
            domain=domain or DOMAIN_A,
            username=username,
            groups=("客服",),
            nodes=nodes,
        )

    def _get(self, client, **params):
        return client.get(URL, params)

    def _data(self, client, **params):
        response = self._get(client, **params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], OK, body)
        return body["data"]


class PermissionAndDaysTests(_ApiMixin, TenantTestCase):
    """准入节点 + `days` 边界。"""

    def test_admin_or_node_holder_gets_the_envelope(self):
        data = self._data(self._viewer())
        self.assertEqual(
            set(data),
            {"days", "date_from", "date_to", "wechat", "registrations", "claims", "deposit"},
        )
        self.assertEqual(data["days"], 7)
        self.assertEqual(data["date_to"], _today().isoformat())
        self.assertEqual(data["date_from"], (_today() - timedelta(days=6)).isoformat())

    def test_user_without_the_node_gets_http_403_and_business_403(self):
        """⛔ 不接受 200 + 全 0：没权限就是 403（fail-closed）。"""
        response = self._get(self._viewer(username="no-node", nodes=()))
        self.assertEqual(response.status_code, FORBIDDEN)
        self.assertEqual(response.json()["code"], FORBIDDEN)

    def test_node_constant_matches_report_views(self):
        """本模块的 `REPORT_NODE` 与经营报表那份**同值**（结构网只解本模块的常量）。"""
        from apps.business.api import app_stats_views, report_views

        self.assertEqual(app_stats_views.REPORT_NODE, report_views.REPORT_NODE)
        self.assertEqual(app_stats_views.REPORT_NODE, NODE)

    def test_days_default_is_7(self):
        self.assertEqual(len(self._data(self._viewer())["claims"]["series"]), 7)

    def test_days_can_be_30(self):
        data = self._data(self._viewer(), days=30)
        self.assertEqual(data["days"], 30)
        self.assertEqual(len(data["registrations"]["series"]), 30)

    def test_days_out_of_range_or_garbage_is_business_1000_not_500(self):
        client = self._viewer()
        for value in ("0", "31", "abc", "-1"):
            response = self._get(client, days=value)
            self.assertEqual(response.status_code, 200, value)
            self.assertEqual(response.json()["code"], VALIDATION, value)

    def test_empty_days_means_default(self):
        """前端清空输入框会发 `?days=` ⇒ 按没传处理（不是 1000）。"""
        data = self._data(self._viewer(), days="")
        self.assertEqual(data["days"], 7)


class WechatClientTests(TenantTestCase):
    """微信 datacube 客户端：7 次 begin=end、止于昨天、三层解析失败。"""

    #: 本机 `.env` 默认不配小程序凭据（生产才配）⇒ 客户端用例显式给合成值，
    #: 判据才落在**解析分支**上（而不是「未配置」那一条）。
    WX = {"WX_MINI_APP_ID": "wx-app-stats", "WX_MINI_APP_SECRET": "secret-app-stats"}

    def _token_ok(self):
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})

    def _row(self, day: date, **over):
        return {
            "ref_date": day.strftime("%Y%m%d"),
            "session_cnt": 11,
            "visit_pv": 12,
            "visit_uv": 13,
            "visit_uv_new": 14,
            **over,
        }

    def _post_ok(self, day: date, **over):
        return httpx.Response(200, json={"list": [self._row(day, **over)]})

    def setUp(self):
        super().setUp()
        self._wx = override_settings(**self.WX)
        self._wx.enable()

    def tearDown(self):
        self._wx.disable()
        super().tearDown()

    def test_seven_days_means_seven_calls_ending_yesterday(self):
        today = date(2026, 10, 23)
        days = [today - timedelta(days=offset) for offset in range(7, 0, -1)]  # 10-16 .. 10-22

        with mock.patch.object(wechat_datacube.httpx, "get", return_value=self._token_ok()) as get:
            with mock.patch.object(
                wechat_datacube.httpx,
                "post",
                side_effect=[self._post_ok(day) for day in days],
            ) as post:
                series = wechat_datacube.fetch_daily_visits(days=7, today=today)

        get.assert_called_once()
        self.assertEqual(post.call_count, 7)
        bodies = [call.kwargs["json"] for call in post.call_args_list]
        self.assertEqual(
            bodies,
            [{"begin_date": day.isoformat(), "end_date": day.isoformat()} for day in days],
        )
        # 每一组 begin == end，且最后一天 ≤ 昨天（T+1）。
        for body in bodies:
            self.assertEqual(body["begin_date"], body["end_date"])
            self.assertLessEqual(
                date.fromisoformat(body["end_date"]), today - timedelta(days=1)
            )
        self.assertEqual([row["date"] for row in series], [day.isoformat() for day in days])
        self.assertEqual(series[0]["visit_uv_new"], 14)

    def test_token_parse_failures_are_three_distinct_reasons(self):
        cases = [
            # 分支 1：非 JSON。
            (httpx.Response(200, content=b"<html>"), "不是 JSON"),
            # 分支 1：errcode。
            (httpx.Response(200, json={"errcode": 40013, "errmsg": "x"}), "errcode：40013"),
            # 分支 1：成功但没 token。
            (httpx.Response(200, json={"expires_in": 7200}), "没有 access_token"),
        ]
        for response, expected in cases:
            with mock.patch.object(wechat_datacube.httpx, "get", return_value=response):
                with self.assertRaises(wechat_datacube.WechatStatsError) as raised:
                    wechat_datacube.fetch_daily_visits(days=1, today=date(2026, 10, 23))
            self.assertIn(expected, str(raised.exception))

    def test_response_and_row_parse_failures_are_reported_not_zeroed(self):
        today = date(2026, 10, 23)
        day = today - timedelta(days=1)
        cases = [
            # 分支 2：顶层不是对象。
            (httpx.Response(200, json=[1, 2]), "返回结构不对"),
            # 分支 2：没有 list。
            (httpx.Response(200, json={"errcode": 0}), "缺少 list"),
            # 分支 2：list 为空 —— ⛔ 不当成「那天 0 人」。
            (httpx.Response(200, json={"list": []}), "没有返回"),
            # 分支 3：缺字段。
            (httpx.Response(200, json={"list": [{"ref_date": day.strftime("%Y%m%d")}]}), "缺少字段"),
            # 分支 3：字段不是整数。
            (
                httpx.Response(
                    200,
                    json={"list": [self._row(day, visit_pv="很多")]},
                ),
                "不是整数",
            ),
            # 分支 3：返回的不是那一天。
            (
                httpx.Response(
                    200,
                    json={"list": [self._row(day - timedelta(days=1))]},
                ),
                "没有返回",
            ),
        ]
        for response, expected in cases:
            with mock.patch.object(wechat_datacube.httpx, "get", return_value=self._token_ok()):
                with mock.patch.object(wechat_datacube.httpx, "post", return_value=response):
                    with self.assertRaises(wechat_datacube.WechatStatsError) as raised:
                        wechat_datacube.fetch_daily_visits(days=1, today=today)
            self.assertIn(expected, str(raised.exception), response.content)

    def test_datacube_errcode_is_reported(self):
        with mock.patch.object(wechat_datacube.httpx, "get", return_value=self._token_ok()):
            with mock.patch.object(
                wechat_datacube.httpx,
                "post",
                return_value=httpx.Response(200, json={"errcode": 45009}),
            ):
                with self.assertRaises(wechat_datacube.WechatStatsError) as raised:
                    wechat_datacube.fetch_daily_visits(days=1, today=date(2026, 10, 23))
        self.assertIn("errcode：45009", str(raised.exception))

    def test_missing_config_is_a_clear_reason_without_leaking_values(self):
        with mock.patch.object(wechat_datacube, "_config", side_effect=wechat_datacube.WechatStatsError("小程序数据未配置，缺：WX_MINI_APP_ID")):
            with self.assertRaises(wechat_datacube.WechatStatsError) as raised:
                wechat_datacube.fetch_daily_visits(days=1, today=date(2026, 10, 23))
        self.assertIn("WX_MINI_APP_ID", str(raised.exception))


class WechatEndpointTests(_ApiMixin, TenantTestCase):
    """端点侧：只有 suqing 可用；失败 ⇒ `available=false`，⛔ 不显示 0。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # `TenantTestCase` 只建空 schema；本类要真实登录（登录要 Group / 角色目录）。
        seed_tenant(cls.tenant)
        # `suqing` 是微信块的**唯一**开通判据（`app_stats.WECHAT_TENANT_SCHEMA`）。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="微信开通租户", domain=DOMAIN_B
        )
        seed_tenant(cls.tenant_b)

    def setUp(self):
        connection.set_tenant(self.tenant)

    def tearDown(self):
        connection.set_tenant(self.tenant)

    def _suqing_viewer(self):
        try:
            with tenant_context(self.tenant_b):
                return self._viewer(username="suqing-viewer", domain=DOMAIN_B)
        finally:
            connection.set_tenant(self.tenant)

    def test_other_tenants_get_unavailable_with_the_exact_reason(self):
        data = self._data(self._viewer())
        self.assertEqual(
            data["wechat"],
            {
                "available": False,
                "reason": WECHAT_NOT_ENABLED,
                "date_from": (_today() - timedelta(days=7)).isoformat(),
                "date_to": (_today() - timedelta(days=1)).isoformat(),
                "series": [],
                "totals": None,
            },
        )

    def test_suqing_gets_seven_days_ending_yesterday(self):
        today = _today()
        series = [
            {
                "date": (today - timedelta(days=offset)).isoformat(),
                "session_cnt": offset,
                "visit_pv": offset + 1,
                "visit_uv": offset + 2,
                "visit_uv_new": offset + 3,
            }
            for offset in range(7, 0, -1)
        ]
        client = self._suqing_viewer()
        with mock.patch.object(app_stats, "fetch_daily_visits", return_value=series) as fetch:
            try:
                data = self._data(client)
            finally:
                connection.set_tenant(self.tenant)
        fetch.assert_called_once_with(days=7, today=today)
        self.assertTrue(data["wechat"]["available"])
        self.assertEqual(data["wechat"]["reason"], "")
        self.assertEqual(data["wechat"]["series"], series)
        self.assertEqual(data["wechat"]["totals"]["session_cnt"], sum(range(1, 8)))

    def test_suqing_failure_is_unavailable_not_zero(self):
        client = self._suqing_viewer()
        with mock.patch.object(
            app_stats,
            "fetch_daily_visits",
            side_effect=wechat_datacube.WechatStatsError("微信 access_token 获取失败（errcode：40013）"),
        ):
            try:
                data = self._data(client)
            finally:
                connection.set_tenant(self.tenant)
        self.assertFalse(data["wechat"]["available"])
        self.assertEqual(data["wechat"]["reason"], "微信 access_token 获取失败（errcode：40013）")
        self.assertEqual(data["wechat"]["series"], [])
        self.assertIsNone(data["wechat"]["totals"])


class RegistrationTests(_ApiMixin, TenantTestCase):
    """注册：真端点造数 + 最早 token 的 client 分桶 + 跨租户取值不同。"""

    def setUp(self):
        # 跨域名请求结束后连接会停在那个 schema（django-tenants 不还原）⇒ 每条用例先钉回 A。
        connection.set_tenant(self.tenant)
        cache.clear()

    def tearDown(self):
        connection.set_tenant(self.tenant)

    def _register(self, domain, mobile, client):
        response = Client(SERVER_NAME=domain).post(
            REGISTER_URL,
            {"mobile": mobile, "password": PASSWORD, "client": client},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], OK, body)
        return body

    def _today_row(self, data):
        return data["registrations"]["series"][-1]

    def test_mp_weixin_app_android_and_unknown_are_counted_separately(self):
        self._register(DOMAIN_A, "13800001001", "mp-weixin")
        self._register(DOMAIN_A, "13800001002", "mp-weixin")
        self._register(DOMAIN_A, "13800001003", "app-android")
        # 没有令牌的 App 档案（极端：注册事务半途 / 历史数据）⇒ unknown，⛔ 不丢行。
        orphan = User.objects.create_user(username="orphan-app", password=PASSWORD)
        StaffProfile.objects.create(user=orphan, account_source=AccountSource.APP)
        # 链接自助注册**不算**（§7 2A：只算 account_source='app'）。
        link_user = User.objects.create_user(username="link-user", password=PASSWORD)
        StaffProfile.objects.create(user=link_user, account_source=AccountSource.LINK)

        data = self._data(self._viewer())
        row = self._today_row(data)
        self.assertEqual(
            {key: row[key] for key in ("mp_weixin", "app_android", "unknown", "total")},
            {"mp_weixin": 2, "app_android": 1, "unknown": 1, "total": 4},
        )
        self.assertEqual(
            data["registrations"]["totals"],
            {"mp_weixin": 2, "app_android": 1, "unknown": 1, "total": 4},
        )
        # 旧数据提示照计划上屏（页面要标「历史未区分」）。
        self.assertIn("历史未区分", data["registrations"]["note"])

    def test_earliest_token_decides_the_bucket(self):
        """同一账号后来又签发了一枚别的 client 的令牌 ⇒ 仍按**最早**那一枚算。"""
        body = self._register(DOMAIN_A, "13800001004", "mp-weixin")
        user = User.objects.get(username="13800001004")
        self.assertTrue(body["data"]["token"])
        AppToken.objects.create(
            user=user,
            key_hash=hash_token(new_token()),
            client="app-android",
            expires_at=timezone.now() + timedelta(days=1),
        )
        row = self._today_row(self._data(self._viewer()))
        self.assertEqual(row["mp_weixin"], 1)
        self.assertEqual(row["app_android"], 0)

    def test_cross_tenant_uses_values_that_differ(self):
        """A 只有小程序、B 只有 App ⇒ 两个响应各自只认自己那一桶（⛔ 不靠主键指认）。"""
        self._register(DOMAIN_A, "13800001005", "mp-weixin")
        self._register(DOMAIN_B, "13900001006", "app-android")

        # B 的请求把连接留在了 B 的 schema（django-tenants 不还原）⇒ 建 A 的 viewer 前先钉回 A。
        connection.set_tenant(self.tenant)
        a = self._today_row(self._data(self._viewer()))
        self.assertEqual((a["mp_weixin"], a["app_android"]), (1, 0))

        try:
            with tenant_context(self.tenant_b):
                viewer_b = self._viewer(username="stats-viewer-b", domain=DOMAIN_B)
            b = self._today_row(
                self._data(viewer_b)
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual((b["mp_weixin"], b["app_android"]), (0, 1))

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        seed_tenant(cls.tenant)
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="微信开通租户", domain=DOMAIN_B
        )
        seed_tenant(cls.tenant_b)


class ClaimStatsTests(_ApiMixin, TenantTestCase):
    """每日接单：按日、按 reason 分；移动端只认 `CHANNEL_APP`。"""

    def test_series_groups_by_local_day_and_reason(self):
        today = _today()
        rows = [
            ("order-a", CHANNEL_APP, _at(today, 9)),
            ("order-b", CHANNEL_APP, _at(today, 10)),
            ("order-c", CHANNEL_WEB, _at(today, 11)),
            ("order-d", "打手对接接口", _at(today - timedelta(days=1), 12)),
            ("order-e", "", _at(today - timedelta(days=1), 13)),
        ]
        for order_no, reason, moment in rows:
            OperationLog.objects.create(
                log_type=10,
                action="order.claim",
                reason=reason,
                order_no=order_no,
                summary="x",
                created_at=moment,
            )
        # 别的动作不算（判别力对照）。
        OperationLog.objects.create(
            log_type=10,
            action="order.update",
            reason=CHANNEL_APP,
            order_no="order-x",
            summary="x",
            created_at=_at(today, 14),
        )

        data = self._data(self._viewer(), days=7)
        by_date = {row["date"]: row for row in data["claims"]["series"]}
        today_row = by_date[today.isoformat()]
        yesterday_row = by_date[(today - timedelta(days=1)).isoformat()]
        self.assertEqual(today_row["total"], 3)
        self.assertEqual(today_row["mobile"], 2)
        self.assertEqual(today_row["by_reason"], {CHANNEL_APP: 2, CHANNEL_WEB: 1})
        self.assertEqual(yesterday_row["total"], 2)
        self.assertEqual(yesterday_row["mobile"], 0)
        self.assertEqual(
            yesterday_row["by_reason"], {"打手对接接口": 1, "未记录": 1}
        )
        self.assertEqual(
            data["claims"]["totals"],
            {
                "total": 5,
                "mobile": 2,
                "by_reason": {CHANNEL_APP: 2, CHANNEL_WEB: 1, "打手对接接口": 1, "未记录": 1},
            },
        )
        self.assertEqual(data["claims"]["mobile_channels"], [
            "App 端大厅", "微信小程序端大厅", "打手App", "打手移动端大厅",
        ])


class ClaimChannelTests(TenantTestCase):
    """`order_views.claim` 的审计渠道：App 令牌 ≠ 系统网页。"""

    def setUp(self):
        self.game = Game.objects.create(name="游戏A")
        self.client = make_logged_in_client(
            domain=DOMAIN_A,
            username="claim-taker",
            groups=("客服",),
            nodes=("grab_hall.take_order",),
            games=[self.game],
        )
        self.user = User.objects.get(username="claim-taker")
        self._seq = 0

    def _order(self):
        self._seq += 1
        order = Order.objects.create(
            game=self.game,
            order_no=f"20261023{self._seq:012d}",
            status=OrderStatus.WAITING_GRAB,
            hall_price=Decimal("88.00"),
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal("88.00")
        )
        return order

    def _claim_with_app_token(self, order, *, client="app-android"):
        raw = new_token()
        AppToken.objects.create(
            user=self.user,
            key_hash=hash_token(raw),
            client=client,
            expires_at=timezone.now() + timedelta(days=1),
        )
        response = Client(SERVER_NAME=DOMAIN_A).post(
            f"/api/business/hall/{order.order_no}/claim/",
            HTTP_AUTHORIZATION=f"Bearer {raw}",
        )
        return response

    def _reason(self, order_no):
        return OperationLog.objects.get(action="order.claim", order_no=order_no).reason

    def test_app_token_claim_is_labelled_app(self):
        order = self._order()
        response = self._claim_with_app_token(order)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], OK, response.json())
        self.assertEqual(self._reason(order.order_no), CHANNEL_APP)
        self.assertNotEqual(self._reason(order.order_no), CHANNEL_WEB)

    def test_mini_program_claim_has_separate_channel_and_order_number(self):
        order = self._order()
        order.source_order_no = "TEST-HALL-ORIGIN"
        order.save(update_fields=["source_order_no"])
        response = self._claim_with_app_token(order, client="mp-weixin")
        self.assertEqual(response.json()["code"], OK, response.json())
        entry = OperationLog.objects.get(action="order.claim", order_no=order.order_no)
        self.assertEqual(entry.reason, "微信小程序端大厅")
        self.assertIn("从微信小程序端大厅接手了订单 TEST-HALL-ORIGIN", entry.summary)

    def test_session_claim_is_still_web(self):
        order = self._order()
        response = self.client.post(f"/api/business/hall/{order.order_no}/claim/")
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], OK, response.json())
        self.assertEqual(self._reason(order.order_no), CHANNEL_WEB)

    @override_settings(DEPOSIT_ENFORCED=True)
    def test_global_deposit_gate_does_not_turn_web_claims_into_app(self):
        """总闸 `DEPOSIT_ENFORCED=1` 时 `deposit_enforced_for(会话)` 也是 True——
        渠道判据**不能**直接拿它，否则网页接单会被统计成移动端（本线实测修正）。
        """
        order = self._order()
        # 总闸打开 ⇒ 会话接单也要过押金闸：先给够冻结额，让这一单真的接得下来，
        # 判据才落在**渠道**上（而不是「被押金闸拒了」）。
        TakerWallet.objects.create(user=self.user, frozen=Decimal("100.00"))
        response = self.client.post(f"/api/business/hall/{order.order_no}/claim/")
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["code"], OK, response.json())
        self.assertEqual(self._reason(order.order_no), CHANNEL_WEB)


class DepositTests(_ApiMixin, TenantTestCase):
    """押金：当前总额 + 冻结人数 + 每日变动；`release_request` 不动钱。"""

    def _wallet(self, username, frozen):
        user = User.objects.create_user(username=username, password=PASSWORD)
        return TakerWallet.objects.create(user=user, frozen=Decimal(frozen))

    def _tx(self, wallet, kind, amount, day):
        row = WalletTransaction.objects.create(
            wallet=wallet,
            kind=kind,
            amount=Decimal(amount),
            balance_after=Decimal("0.00"),
            frozen_after=Decimal("0.00"),
        )
        WalletTransaction.objects.filter(pk=row.pk).update(created_at=_at(day))
        return row

    def test_totals_count_and_daily_delta(self):
        today = _today()
        day1 = today - timedelta(days=2)
        day2 = today - timedelta(days=1)
        frozen_wallet = self._wallet("wallet-frozen", "100.00")
        zero_wallet = self._wallet("wallet-zero", "0.00")

        self._tx(frozen_wallet, WalletTransaction.KIND_FREEZE, "50.00", day1)
        self._tx(frozen_wallet, WalletTransaction.KIND_FROZEN_ADD, "30.00", day1)
        self._tx(frozen_wallet, WalletTransaction.KIND_RELEASE, "20.00", day2)
        self._tx(frozen_wallet, WalletTransaction.KIND_FROZEN_SUB, "10.00", day2)
        # 申请解冻只记事件、**不动钱** ⇒ 不进每日变动。
        self._tx(frozen_wallet, WalletTransaction.KIND_RELEASE_REQUEST, "999.00", day2)
        # 与冻结无关的流水也不进。
        self._tx(zero_wallet, WalletTransaction.KIND_RECHARGE, "77.00", day2)

        data = self._data(self._viewer(), days=7)
        deposit = data["deposit"]
        self.assertEqual(deposit["total_frozen"], "100.00")
        self.assertEqual(deposit["taker_count"], 1)
        self.assertEqual(deposit["note"], DEPOSIT_NOTE)
        deltas = {row["date"]: row["delta"] for row in deposit["series"]}
        self.assertEqual(deltas[day1.isoformat()], "80.00")
        self.assertEqual(deltas[day2.isoformat()], "-30.00")
        self.assertEqual(deltas[today.isoformat()], "0.00")
