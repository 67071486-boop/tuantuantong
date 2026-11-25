"""T5：打手侧**内部**的回调配置 / 推送台账 / 手工重投端点（第二期 spec
`docs/specs/2026-10-09-taker-callback-design.md` §7 / §8.4 / §9-4 / §9-5 / §9-6 / §9-8 / §9-9 / §9-10）。

## 本文件证什么（逐条都能单独弄红；每条的**弄红手段**写在用例自己的 docstring 里）

| 组 | 钉什么（spec 条号） |
|---|---|
| `CallbackUrlValidationTests` | ⭐ §9-8：`http://` / 私网 / 回环 / 带用户名密码 的地址 ⇒ **保存时** `1000` + **点名原因**（文案**逐字来自** `apps/openapi/outbound.py`，⛔ 不是本文件自己编的一句）；合法 https 公网地址 ⇒ **真入库**（正对照） |
| `CallbackConfigPatchTests` | §7 第一行：`PATCH` 能改 `callback_url` / `callback_state`，空串是**合法值**（= 清掉回调）；⛔ `taker` 仍不可改 |
| `DeliveryLedgerTests` | §7 第四行：台账行**恰好 13 键**（字面量）；分页（`page` / `page_size` / 上限）；`status` 筛生效；非法 `status` ⇒ `1000` |
| `DeliveryRetryTests` | ⭐ §7 第三行：`failed` ⇒ 变 `pending` **且 `next_attempt_at` 清空**（后者是承重的：不清 ⇒ 下一轮退避还没到，重投**静默不生效**）；`pending` / `done` ⇒ `1000` 点名、**那一行逐列一字未变** |
| `DeliveryIdentityScopingTests` | ⭐ §9-9：打手 A 看 / 重投 **B 的**应用台账 ⇒ 与"不存在"**同形**（同一句 `1000`）+ B 那一行逐列快照不变；A 看自己的 ⇒ 通（正对照） |
| `CallbackAuditTrailTests` | ⭐ §9-10：改回调地址 / 改回调开关 / 手工重投 ⇒ 各一行 `OperationLog`（断言**数据库行**，⛔ 不用 `assertLogs`）；且**不含任何凭据值** |
| `CallbackUnsubscribeTests` | ⭐ §9-6：`callback_state` 改成 `disabled` ⇒ 之后对该单做一次**真实状态变化** ⇒ `TakerApiDelivery` **零新行**；**已有的历史行不许被删** |
| `DeliveryMalformedInputTests` | 两条路由都带路径转换器 ⇒ 被 `test_no_route_500` 的显式路由腿跳过 ⇒ 这里用 **HTTP 级**断言补那张网的盲区（超长 / 非数字 pk、非法分页参数），一条都不许 500 |

## 夹具纪律

- 地址 / 应用名 / 凭据 / 单号一律**合成值**（`AGENTS.md` §5-C / E-0077）：公网那一档用
  **地址字面量** `93.184.216.34`（⛔ 不是任何真实第三方），私网 / 回环用文档保留段与回环字面量。
- 地址校验**只做 `getaddrinfo` / 地址字面量判断**（公网 / 私网字面量都**不解析 DNS**）
  ⇒ 本文件**一个真实 HTTP 都不发**（投递本身由 `tests/test_takerapi_dispatch_command.py` 那批负责）。
- 必须 `TenantTestCase`：`apps.takerapi` / `apps.business` / `apps.audit` 都在 `TENANT_APPS` 里。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business.models import EmpType, Game, Order, OrderStatus, Performance
from apps.business.order_writes import WriteContext, transition_order
from apps.openapi import outbound
from apps.openapi.models import ApiApp, OpenApiDelivery
from apps.rbac.choices import NodeKind
from apps.rbac.models import PermissionNode
from apps.rbac.seed import seed_catalog
from apps.takerapi.models import AppState, CallbackState, TakerApiDelivery, TakerApp
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

# ── 节点 code（**字面量**，⛔ 不从 `app_views` import：常量被改坏时派生断言不会红，E-0024）
SYSTEM_ADMIN_NODE = "system.pinduoduo_settings"
TAKER_IDENTITY_NODE = "system.allow_taker_open_api"

# ── 三条路径（与 `apps/takerapi/urls.py` 逐字一致；⚠️ 都带尾斜杠）─────────────
DETAIL_TEMPLATE = "/api/taker/apps/{pk}/"
DELIVERIES_TEMPLATE = "/api/taker/apps/{pk}/deliveries/"
RETRY_TEMPLATE = "/api/taker/apps/{pk}/deliveries/{delivery_pk}/retry/"

#: 取令牌端点（与 `apps/takerapi/urls.py` 逐字一致；⚠️ **不带**尾斜杠，那是 spec §2 的契约）。
#: ⚠️ 本文件只有 ④b（跨族对拍）/ ⑥（留痕）两处用到它（换一颗**真的**令牌来当"当次用到的凭据串"）。
TOKEN_PATH = "/api/taker/oauth/token"
GRANT_TYPE = "client_credentials"

# ── 业务码 / HTTP 码字面值（E-0024：写数字，让"码值被改掉"在用例里可见）──────
OK = 0
VALIDATION = 1000
UNAUTHENTICATED = 401
FORBIDDEN = 403
NOT_FOUND_ROUTE = 404
PAGE_OUT_OF_RANGE_CODE = 1002

# ── 内部页面端的分页口径（`apps/business/api/base.py::EnvelopePagination` 的既有取值；
#    ⚠️ 这里写**字面量**，⛔ 不从那个类派生：派生会让"上限被改小"这条静默绿）─────────
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 300

#: ⭐ 台账行**恰好**这 13 个键（字面量写在这里是**有意**的：⛔ 不从 `delivery_row_payload`
#: 或任何常量派生 —— 那样函数被改坏时本条不会红，E-0024）。
#: 与上家侧那张**逐字同集**（第二期 spec §7 第四行：「台账行 13 键照上家侧那张」）。
LEDGER_ROW_KEYS = {
    "id",
    "event",
    "event_text",
    "order_no",
    "source_order_no",
    "status",
    "status_text",
    "attempts",
    "reason",
    "response_status",
    "next_attempt_at",
    "created_at",
    "processed_at",
}

#: 行载荷**恰好**这 14 个键（第一期那 12 个 + T5 追加的回调配置两键；见 serializers 的 docstring）。
APP_ROW_KEYS = {
    "id", "name", "app_key", "app_secret", "taker", "taker_name",
    "state", "state_text", "ip_allowlist",
    "callback_url", "callback_state",
    "last_used_at", "secret_rotated_at", "created_at",
}

# ── 合成夹具值（⛔ 不是任何真实数据）──────────────────────────────────────────
ADMIN_USERNAME = "tkcb5-admin"
TAKER_A_USERNAME = "tkcb5-taker-a"
TAKER_B_USERNAME = "tkcb5-taker-b"
PASSWORD = "pw-tkcb5-0001"

APP_NAME = "回调端点夹具应用甲"
OTHER_APP_NAME = "回调端点夹具应用乙"
APP_KEY = "tkcb5key0000000001"
APP_SECRET = "tkcb5-secret-synthetic-000000000001"
OTHER_APP_KEY = "tkcb5key0000000002"
OTHER_APP_SECRET = "tkcb5-secret-synthetic-000000000002"

#: 一个**公网**地址字面量（只用于夹具；⛔ 不是任何真实第三方，也⛔ 不解析 DNS）。
PUBLIC_HOST = "93.184.216.34"
CALLBACK_URL = f"https://{PUBLIC_HOST}/taker/hook"
OTHER_CALLBACK_URL = f"https://{PUBLIC_HOST}/taker/hook2"

#: 必须被拒的三类地址（各取一个**字面量**，各自走 `outbound` 里一条不同的判据）：
#: ① 非 https（判据 1）；② 回环（判据 4）；③ URL 内带凭据（判据 2）。
HTTP_URL = f"http://{PUBLIC_HOST}/taker/hook"
LOOPBACK_URL = "https://127.0.0.1/taker/hook"
CREDENTIALED_URL = f"https://tkcb5-user:tkcb5-pass@{PUBLIC_HOST}/taker/hook"
#: 私网（判据 4 的同一条，另一档）：文档保留段的私网地址。
PRIVATE_URL = "https://10.11.12.13/taker/hook"

#: 台账 / 重投用的合成单号。
ORDER_NO = "20261009000000001201"
OTHER_ORDER_NO = "20261009000000001202"

# ── ⭐ T5b（跨族对拍用）上家侧那一族的合成夹具值（⛔ 不是任何真实数据）──────────
#: 对拍要**两族各一行应用**（各挂在自己那张台账上），凭据与名字都用合成值。
OPENAPI_APP_NAME = "回调端点对拍应用（上家侧）"
OPENAPI_APP_KEY = "tkcb5paritykey0001"
OPENAPI_APP_SECRET = "tkcb5-parity-secret-synthetic-0000000001"
#: 失败诊断文本（合成；长短与内容都只为本文件服务）。
REASON_TEXT = "对方回了 HTTP 503：合成回执"

#: 事件名（字面量；⛔ 不从 `TakerApiDelivery.Event` 取 —— 那样"事件名被改坏"这条不红）。
EVENT_STATUS = "order.status_changed"

#: 状态字面值（同上，写字面量）。
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

#: 留痕动作码（字面量；与 `apps/audit/actions.py` 的键逐字一致）。
ACTION_APP_UPDATE = "system.taker_api_app_update"
ACTION_DELIVERY_RETRY = "system.taker_api_delivery_retry"

#: ⚠️ **按库取诊断文本的 SQL 片段**（照 `tests/test_openapi_app_api.py::AppSecretNeverLoggedTests`
#: 的手法）：`OperationLog` 的文本列用 `CAST(col AS text) = %s` 做**逐列**全等比较
#: ——⛔ 不用 `icontains`（子串判据在"截断 / 拼接"下会给出假的保证）。
_LOG_TEXT_COLUMNS = (
    "actor", "target_name", "field_name", "from_value", "to_value",
    "reason", "order_no", "source_order_no", "summary",
)


def _row_snapshot(row):
    """一条台账行的**逐列快照**（断言"一字未变"必须用它，⛔ 不挑列：E-0064 同族）。"""
    return {
        field.name: getattr(row, field.attname)
        for field in row._meta.concrete_fields
    }


def _app_snapshot(app):
    """一行应用的**逐列快照**（同上）。"""
    return {
        field.name: getattr(app, field.attname)
        for field in TakerApp._meta.concrete_fields
    }


class _CallbackApiFixture(TenantTestCase):
    """一个租户、一个管理员（持门里那个既有节点）、两个打手（各持打手身份节点 + 各一套应用）。

    ⚠️ 两个打手**都**持 `system.allow_taker_open_api` ⇒ 他们**都能进**这组端点，
    差别只在"这一行绑的是谁" —— 那正是身份限权那条判据要拦的东西
    （只拦"谁持节点"那一层是拦不住的）。
    """

    def setUp(self):
        super().setUp()
        seed_catalog()
        self.domain_name = self.domain.domain
        self.admin = make_logged_in_client(
            username=ADMIN_USERNAME, groups=("客服",), nodes=(SYSTEM_ADMIN_NODE,)
        )
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        # 打手侧的节点：T4 已落目录，但这里 `get_or_create` 让本夹具在目录变动时仍可用
        # （判据读的是**节点表**，⛔ 不是目录常量；先例见 `tests/test_takerapi_app_api.py`）。
        PermissionNode.objects.get_or_create(
            code=TAKER_IDENTITY_NODE,
            defaults={
                "name": "允许打手使用开发平台应用",
                "note": "打手可进入「打手对接」页并为自己创建一套接入凭据",
                "url": "/open-platform/taker-apps",
                "kind": NodeKind.PAGE,
                "level": 1,
                "available": True,
            },
        )
        self.taker_a = make_logged_in_client(
            username=TAKER_A_USERNAME, groups=("代练员",), nodes=(TAKER_IDENTITY_NODE,)
        )
        self.taker_b = make_logged_in_client(
            username=TAKER_B_USERNAME, groups=("代练员",), nodes=(TAKER_IDENTITY_NODE,)
        )
        self.user_a = User.objects.get(username=TAKER_A_USERNAME)
        self.user_b = User.objects.get(username=TAKER_B_USERNAME)
        # 一款游戏 + 一张单（`transition_order` 那条真实状态变化要用到）。
        self.game = Game.objects.create(name="回调端点夹具游戏甲")

    # ── 造物 ───────────────────────────────────────────────────────────
    def make_app(self, *, name=APP_NAME, app_key=APP_KEY, app_secret=APP_SECRET,
                 taker=None, callback_url=CALLBACK_URL,
                 callback_state=CallbackState.ENABLED) -> TakerApp:
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            taker=taker or self.user_a,
            created_by=self.user_a,
            callback_url=callback_url,
            callback_state=callback_state,
        )

    def make_delivery(
        self,
        app,
        *,
        event=EVENT_STATUS,
        order_no=ORDER_NO,
        source_order_no="TKCB5-SRC-0001",
        payload=None,
        status=STATUS_FAILED,
        attempts=3,
        reason=REASON_TEXT,
        response_status=503,
        next_attempt_at=None,
        processed_at=None,
    ) -> TakerApiDelivery:
        """直接经 ORM 造一行台账（⛔ 不走入队：本文件测的是**读 / 重投**那一半）。

        ⚠️ `created_at` 是 `auto_now_add` ⇒ 要给它一个确定时刻必须建完再 `.update(...)`
        （`make_delivery(..., created_at=…)` 那种写法会被**静默忽略**）。
        """
        return TakerApiDelivery.objects.create(
            app=app,
            event=event,
            order_no=order_no,
            source_order_no=source_order_no,
            payload={} if payload is None else payload,
            status=status,
            attempts=attempts,
            reason=reason,
            response_status=response_status,
            next_attempt_at=next_attempt_at,
            processed_at=processed_at,
        )

    def make_order(self, n=1, *, status=OrderStatus.WAITING_GRAB) -> Order:
        """一张**待抢单**状态起的单（好让 `transition_order` 真的走一次状态机）。

        ⚠️ 单号**恰好 20 位**（`f"20261009000000{n:06d}"`：14 位前缀 + 6 位序号）——
        照 `tests/test_takerapi_callback_recipients.py::_no` 的形状。⛔ 位数不对会让入队
        那一跳静默不生效（症状是"零行"，看起来像收件人判据坏了）。
        """
        order = Order.objects.create(
            order_no=f"20261009000000{n:06d}",
            game=self.game,
            status=status,
            total_price="10.00",
            hall_price="8.00",
            sales_user=self.user_a,
        )
        Performance.objects.create(
            order=order,
            user=self.user_a,
            emp_type=EmpType.MAIN,
            claimed_at=timezone.now(),
        )
        return order

    def ctx(self) -> WriteContext:
        """`transition_order` 的操作上下文（节点集取那次迁移要的那几个；照 T3 夹具的口径）。"""
        return WriteContext(
            actor="回调端点夹具操作人",
            log_actor="回调端点夹具操作人",
            log_actor_id=self.user_a.pk,
            nodes=frozenset({
                "order.change_status", "order.assign_taker", "order.submit_payout_price",
                "order.complete", "grab_hall.take_order",
            }),
            user=self.user_a,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def patch_json(self, url, payload, *, client=None):
        return (client or self.admin).patch(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def post_json(self, url, payload=None, *, client=None):
        return (client or self.admin).post(
            url, data=json.dumps({} if payload is None else payload),
            content_type="application/json",
        )

    def get(self, url, *, client=None, data=None):
        return (client or self.admin).get(url, data or {})

    # ── 断言小工具 ─────────────────────────────────────────────────────
    @staticmethod
    def body(response):
        return json.loads(response.content)

    def issue_token(self, app) -> str:
        """走**真实的**取令牌端点换一颗令牌（⛔ 不手搓、⛔ 不自己签一个）。

        ⚠️ 令牌从"调用方真正看到的载荷"里取：成功体是**裸三字段**（⛔ 没有本仓信封），
        与 `tests/test_takerapi_app_api.py::issue_token` 同一条手法。
        ⚠️ 本文件**只**用它来当"当次用到的凭据串"（留痕 / 响应里不许出现它），
        ⛔ 不用它去调任何对外端点（那属 T6 的网）。
        """
        response = self.anonymous.post(
            TOKEN_PATH,
            {
                "grant_type": GRANT_TYPE,
                "client_id": app.app_key,
                "client_secret": app.app_secret,
            },
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        payload = json.loads(response.content)
        self.assertEqual(set(payload), {"access_token", "token_type", "expires_in"})
        return payload["access_token"]

    def assert_ok(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]

    def assert_business_failure(self, response, code, *, msg_fragment=None):
        """业务失败：**HTTP 200** + 指定业务码（状态码只表达传输 / 认证层）。

        ⚠️ `code` 是承重入参（⛔ 不是装饰）：只断 msg 会让"把 `1000` 写成 `1002`"活下来。
        """
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], code, payload)
        if msg_fragment is not None:
            self.assertIn(msg_fragment, payload["msg"], payload)
        return payload

    def logs_for(self, action):
        return OperationLog.objects.filter(action=action)

    def log_texts_containing(self, needle: str):
        """在 `OperationLog` 的**每一个文本列**上找 `needle`（逐列 `CAST(... AS text) = %s`）。

        ⚠️ 照 `tests/test_openapi_app_api.py::AppSecretNeverLoggedTests` 的手法：
        ⛔ 不用 `icontains`（子串判据在截断 / 拼接下给出假的保证），⛔ 也不只扫 `summary` 一列。
        """
        found = []
        with connection.cursor() as cursor:
            for column in _LOG_TEXT_COLUMNS:
                cursor.execute(
                    f"SELECT id, {column} FROM audit_operationlog "  # noqa: S608（列名是硬编码白名单）
                    f"WHERE CAST({column} AS text) = %s",
                    [needle],
                )
                found.extend(row[0] for row in cursor.fetchall())
        return found


# ── ① 地址校验（spec §9-8 / §8.4）──────────────────────────────────────────


class CallbackUrlValidationTests(_CallbackApiFixture):
    """⭐ 保存时的地址校验：**判据与文案都只有一份实现**（`apps/openapi/outbound.py`）。

    ⚠️ 本组每一条都用 `outbound` 里那个常量**拼期望文案**——那不是"从被测件派生"，
    而是**故意**把两处放在同一个输入上对拍：第二期 spec §8.4 要求「保存时」与「投递前」
    各校验一次、**同一句文案** ⇒ 打手侧若自己编了一句，本组当场红。
    """

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def _reject(self, url, expected_message):
        """`PATCH` 一个非法地址 ⇒ `1000` + **点名原因**，且**库里那一行一个字段都没变**。"""
        before = _app_snapshot(self.app)
        response = self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk),
                                   {"callback_url": url})
        payload = self.assert_business_failure(response, VALIDATION)
        self.assertIn(
            expected_message, payload["msg"],
            "拒收文案必须**逐字来自** apps/openapi/outbound.py（⛔ 不许在打手侧自己编一句）："
            f"{payload['msg']!r}",
        )
        self.app.refresh_from_db()
        self.assertEqual(
            _app_snapshot(self.app), before,
            "被拒的地址不许落库（连一个字都不许变）",
        )
        self.assertEqual(
            self.logs_for(ACTION_APP_UPDATE).count(), 0,
            "被拒的请求不许写留痕（否则日志里会有一条没发生过的改动）",
        )
        return payload

    def test_a_plain_http_url_is_rejected_by_name(self):
        """判据 1：协议只能 https。⚠️ 弄红它：把 `AppUpdateSerializer.callback_url` 的
        `validators=[...]` 去掉 ⇒ 本用例红成 `code=0`（地址**静默入库**）。"""
        self._reject(HTTP_URL, outbound.NOT_HTTPS_MSG)

    def test_a_loopback_url_is_rejected_by_name(self):
        """判据 4（回环）：`127.0.0.0/8`。⚠️ 弄红它：把 `_is_public_ip` 改成恒 `True` ⇒ 红。"""
        self._reject(LOOPBACK_URL, outbound.NON_PUBLIC_IP_MSG)

    def test_a_private_url_is_rejected_by_name(self):
        """判据 4（私网）：`10/8`。⚠️ 弄红它：同上（一条判据覆盖全部非公网档）。"""
        self._reject(PRIVATE_URL, outbound.NON_PUBLIC_IP_MSG)

    def test_a_url_with_credentials_is_rejected_by_name(self):
        """判据 2：URL 内不许带用户名 / 密码。⚠️ 弄红它：删掉 `parsed.username or parsed.password`
        那一句 ⇒ 本用例红（该地址会走到公网判据并通过 ⇒ 静默入库）。"""
        self._reject(CREDENTIALED_URL, outbound.CREDENTIALS_IN_URL_MSG)

    def test_a_legal_https_public_url_is_stored(self):
        """⭐ **正对照**（能失败）：合法的 https 公网地址 ⇒ **真入库**。

        ⚠️ 缺了它，"无差别全拒"的实现也能让上面四条绿——而那种实现会让**所有**打手
        都配不上回调（一条静默的功能性故障）。
        """
        data = self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk),
                            {"callback_url": OTHER_CALLBACK_URL})
        )
        self.app.refresh_from_db()
        self.assertEqual(self.app.callback_url, OTHER_CALLBACK_URL)
        self.assertEqual(data["callback_url"], OTHER_CALLBACK_URL)

    def test_the_stored_value_is_the_normalized_one(self):
        """归一化（去首尾空白）后的形态才是投递时用的形态 ⇒ 库里存的必须**就是**它。

        ⚠️ 弄红它：把 `validate_callback_url_value` 的 `return outbound.validate_callback_url(value)`
        改成 `return value` ⇒ 库里会存下带空白的原文（而投递时用归一化后的那份）。
        """
        padded = f"  {OTHER_CALLBACK_URL}  "
        data = self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk), {"callback_url": padded})
        )
        self.app.refresh_from_db()
        self.assertEqual(self.app.callback_url, OTHER_CALLBACK_URL)
        self.assertEqual(data["callback_url"], OTHER_CALLBACK_URL)

    def test_a_rejected_url_does_not_touch_a_neighbour_field(self):
        """反例②：被拒的那一次**连带**不许改同请求里的别的字段（校验先于任何写入）。"""
        before = _app_snapshot(self.app)
        self.assert_business_failure(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=self.app.pk),
                {"name": "不该被改的名字", "callback_url": HTTP_URL},
            ),
            VALIDATION,
        )
        self.app.refresh_from_db()
        self.assertEqual(_app_snapshot(self.app), before)


# ── ② 回调配置的 PATCH 面（spec §7 第一行）──────────────────────────────────


class CallbackConfigPatchTests(_CallbackApiFixture):
    """`callback_url` / `callback_state` 进了 `APP_UPDATE_WRITABLE_FIELDS`，且**逐字段留痕**。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def test_the_row_payload_carries_exactly_the_fourteen_keys(self):
        """⭐ 行载荷**恰好 14 键**（第一期 12 + T5 两键）。

        ⚠️ 第一期的 12 键断言在 `tests/test_takerapi_app_api.py` 里同一次改准
        （⛔ 不是放宽：那是**恰好集合**，加了两键就必须同一次改）。
        ⚠️ 弄红它：`app_row_payload` 里多回一个 `callback_state_text` ⇒ 本条红
        （第二期 spec §7 第四行逐字要求⛔ 不加它）。
        """
        data = self.assert_ok(self.get("/api/taker/apps/"))
        self.assertEqual(len(data), 1)
        self.assertEqual(set(data[0]), APP_ROW_KEYS, f"行载荷的键集变了：{sorted(data[0])}")
        self.assertNotIn("callback_state_text", data[0])

    def test_the_callback_state_can_be_switched_off_and_on(self):
        """`callback_state` 改得动（两个方向各一次）+ 中文 label 进留痕。"""
        first = self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk),
                            {"callback_state": "disabled"})
        )
        self.assertEqual(first["callback_state"], "disabled")
        self.app.refresh_from_db()
        self.assertEqual(self.app.callback_state, CallbackState.DISABLED)

        second = self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk),
                            {"callback_state": "enabled"})
        )
        self.assertEqual(second["callback_state"], "enabled")
        self.app.refresh_from_db()
        self.assertEqual(self.app.callback_state, CallbackState.ENABLED)

    def test_an_empty_callback_url_is_a_legal_value(self):
        """⭐ 空串 = **清掉回调**（不是"没传"）⇒ 必须真的清掉（E-0021 那条边界）。

        ⚠️ 弄红它：把视图里那条判据写成 `if data["callback_url"]:`（真值判断）
        ⇒ 空串被当成"没传"、`callback_url` 静默留着 —— 而打手以为已经关了回调。
        """
        data = self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk), {"callback_url": ""})
        )
        self.app.refresh_from_db()
        self.assertEqual(self.app.callback_url, "")
        self.assertEqual(data["callback_url"], "")

    def test_a_pure_no_op_writes_no_log(self):
        """重发同一个地址 ⇒ **不写库、不写留痕**（R26：no-op 不该在日志里留一条假话）。"""
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk),
                            {"callback_url": CALLBACK_URL})
        )
        self.assertEqual(self.logs_for(ACTION_APP_UPDATE).count(), 0)

    def test_the_taker_binding_is_still_not_writable(self):
        """⛔ `taker` 仍不可改（第一期的判据，本任务**没有**放松它）。"""
        before = _app_snapshot(self.app)
        payload = self.assert_business_failure(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk),
                            {"taker": self.user_b.pk}),
            VALIDATION,
        )
        self.assertIn("taker", payload["msg"])
        self.app.refresh_from_db()
        self.assertEqual(_app_snapshot(self.app), before)


# ── ③ 推送台账（spec §7 第二 / 第四行）──────────────────────────────────────


class DeliveryLedgerTests(_CallbackApiFixture):
    """台账列表：13 键恰好、分页、`status` 筛。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def test_the_item_keys_are_exactly_the_contract_ones(self):
        """⭐ 键集**字面量恰好相等**（⛔ 不是 `assertIn`：多回一个键就是契约变更）。"""
        self.make_delivery(self.app)
        data = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk)))
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(
            set(data["items"][0]), LEDGER_ROW_KEYS,
            f"台账行的键集变了：{sorted(data['items'][0])}",
        )

    def test_the_payload_and_the_credentials_are_not_in_the_response(self):
        """⛔ 报文原文与⛔ 任何凭据都**不在**响应里（全响应文本扫，E-0065：只看顶层是假保证）。"""
        payload_marker = "报文标记-不该出现在台账里"
        self.make_delivery(self.app, payload={"note": payload_marker})
        response = self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk))
        self.assert_ok(response)
        text = response.content.decode("utf-8")
        for secret in (APP_SECRET, OTHER_APP_SECRET, APP_KEY):
            self.assertNotIn(secret, text, "凭据出现在了台账响应里")
        self.assertNotIn(payload_marker, text, "待投递的报文原文出现在了台账响应里")

    def test_the_order_is_newest_first_and_ties_break_by_id(self):
        """顺序 = `created_at` 倒序、同 `created_at` 再按 `id` 倒序（分页要求顺序**确定**）。

        ⚠️ 用**显式写入**的时刻造这个局面（`created_at` 是 `auto_now_add` ⇒ 建完再 `.update`），
        ⛔ 不指望插入速度。
        """
        now = timezone.now()
        oldest = self.make_delivery(self.app, order_no=ORDER_NO)
        tie_first = self.make_delivery(self.app, order_no=OTHER_ORDER_NO)
        tie_second = self.make_delivery(self.app, order_no="20261009000000001203")
        newest = self.make_delivery(self.app, order_no="20261009000000001204")
        TakerApiDelivery.objects.filter(pk=oldest.pk).update(
            created_at=now - timedelta(minutes=5)
        )
        for row in (tie_first, tie_second, newest):
            TakerApiDelivery.objects.filter(pk=row.pk).update(created_at=now)
        data = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk)))
        self.assertEqual(
            [row["id"] for row in data["items"]],
            [newest.pk, tie_second.pk, tie_first.pk, oldest.pk],
        )

    def test_the_item_moments_are_project_timezone_strings(self):
        """D-0302（2026-10-24）：台账行的三个时刻键是**项目时区**（+08:00）串。

        管理端推送台账页截串直显；旧写法 `_iso` 裸 `.isoformat()` 出 UTC 串、差 8 小时。
        夹具把 `next_attempt_at` / `created_at` 钉在 UTC 04:30 ⇒ 北京墙钟 12:30。
        """
        from datetime import datetime as dt
        from datetime import timezone as dt_tz

        fixed = dt(2026, 10, 24, 4, 30, tzinfo=dt_tz.utc)
        row = self.make_delivery(self.app, next_attempt_at=fixed)
        TakerApiDelivery.objects.filter(pk=row.pk).update(created_at=fixed)
        item = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk)))["items"][0]
        self.assertEqual(item["next_attempt_at"], "2026-10-24T12:30:00+08:00")
        self.assertEqual(item["created_at"], "2026-10-24T12:30:00+08:00")
        self.assertIsNone(item["processed_at"])

    def test_only_this_apps_rows_are_returned(self):
        """子资源必须属于父资源：另一个应用的行**不在**本应用的台账里。"""
        other = self.make_app(name=OTHER_APP_NAME, app_key=OTHER_APP_KEY,
                              app_secret=OTHER_APP_SECRET)
        mine = self.make_delivery(self.app)
        self.make_delivery(other, order_no=OTHER_ORDER_NO)
        data = self.assert_ok(self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk)))
        self.assertEqual([row["id"] for row in data["items"]], [mine.pk])

    def test_pagination_splits_the_rows_and_reports_the_totals(self):
        """分页：`page` / `page_size` 真的生效，`total` 是**筛后**的总数。"""
        for index in range(DEFAULT_PAGE_SIZE + 3):
            self.make_delivery(self.app, order_no=f"2026100900000001{index:04d}")
        first = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk), data={"page": 1})
        )
        self.assertEqual(len(first["items"]), DEFAULT_PAGE_SIZE)
        self.assertEqual(first["total"], DEFAULT_PAGE_SIZE + 3)
        self.assertEqual(first["page"], 1)
        self.assertEqual(first["page_size"], DEFAULT_PAGE_SIZE)

        # ⚠️ `page=2` 上只剩**总数 − 第一页**那么多行（这里 = 3）⇒ 断言写成算出来的那个数，
        #    ⛔ 不写死一个"看起来对"的小数字（写死会在总数变化时静默说谎）。
        remaining = (DEFAULT_PAGE_SIZE + 3) - DEFAULT_PAGE_SIZE
        second = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk), data={"page": 2})
        )
        self.assertEqual(len(second["items"]), remaining)
        self.assertEqual(second["page"], 2)
        self.assertEqual(second["total"], DEFAULT_PAGE_SIZE + 3)

        # `page_size` 真的生效：第二页取 5 条 ⇒ 恰好 5 条（总数足够）。
        third = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk),
                     data={"page": 2, "page_size": 5})
        )
        self.assertEqual(len(third["items"]), 5)
        self.assertEqual(third["page_size"], 5)
        self.assertEqual(third["total"], DEFAULT_PAGE_SIZE + 3)

    def test_the_page_size_upper_bound_is_three_hundred(self):
        """⭐ 上限照**共享分页基类**的既有取值（300）：超了**夹到 300**，⛔ 不是报错。

        ⚠️ 这里没有 201 行真的返回（只有 1 行数据）——判据打在**回显的 `page_size`** 上：
        夹取行为是可断言的（`EnvelopePagination.max_page_size`）。
        弄红它：把上限改成 100 ⇒ 本条红（回显 100）。
        """
        self.make_delivery(self.app)
        data = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk),
                     data={"page_size": MAX_PAGE_SIZE + 50})
        )
        self.assertEqual(data["page_size"], MAX_PAGE_SIZE)

    def test_the_status_filter_really_filters(self):
        """`?status=failed` 只回失败那一档（⛔ 不是"筛了但混着别的"）。"""
        failed = self.make_delivery(self.app, status=STATUS_FAILED)
        self.make_delivery(self.app, status=STATUS_DONE, order_no=OTHER_ORDER_NO)
        data = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk), data={"status": "failed"})
        )
        self.assertEqual([row["id"] for row in data["items"]], [failed.pk])
        self.assertEqual(
            {row["status"] for row in data["items"]}, {STATUS_FAILED},
        )
        self.assertEqual(data["total"], 1, "`total` 必须是**筛后**的总数")

    def test_an_illegal_status_is_a_business_error(self):
        """非法 `status` ⇒ `1000` + 点名那个字段（⛔ 不静默忽略：静默会让页面以为"筛了"）。

        ⚠️ 弄红它：把 `DeliveryStatusFilterSerializer` 换成 `serializers.CharField()`
        ⇒ 本用例红（非法值不再被拒，回 `code=0` + 全量列表）。
        """
        self.make_delivery(self.app)
        payload = self.assert_business_failure(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk), data={"status": "nope"}),
            VALIDATION,
        )
        # ⚠️ 文案里点名的是**中文列义 + 那个被拒的输入**（`状态筛选值不合法：nope`）——
        #    这正是本仓"文案带字段名"的口径；⛔ 不断言英文列名 `status`（那是裸列名，
        #    与 `FIELD_LABELS` 那条纪律相反）。
        self.assertIn("状态筛选", payload["msg"], "文案要点名是哪个筛选字段")
        self.assertIn("nope", payload["msg"], "文案要点名那个被拒的输入")

    def test_an_empty_status_means_no_filter(self):
        """`?status=`（空串）= **不筛**（DRF 对非必填字段的既有语义，⛔ 本端点不特判）。"""
        self.make_delivery(self.app, status=STATUS_FAILED)
        self.make_delivery(self.app, status=STATUS_DONE, order_no=OTHER_ORDER_NO)
        data = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.app.pk), data={"status": ""})
        )
        self.assertEqual(len(data["items"]), 2)

    def test_the_ledger_is_read_only(self):
        """⛔ 台账路径上不许长写方法：`POST` / `DELETE` 在这条路径上是 **405**。

        ⚠️ 与 `tests/test_operation_log_coverage.py::MUST_HAVE_NO_WRITE_METHOD` 那条
        结构钉子**互为对照**：那张表按 `get_resolver()` 的端点全集判，
        这里判**真实 HTTP 读数**（两处各自能红）。
        """
        self.make_delivery(self.app)
        url = DELIVERIES_TEMPLATE.format(pk=self.app.pk)
        self.assertEqual(self.post_json(url).status_code, 405)
        self.assertEqual(self.admin.delete(url).status_code, 405)


# ── ④ 手工重投（spec §7 第三行）────────────────────────────────────────────


class DeliveryRetryTests(_CallbackApiFixture):
    """⭐ **只有 `failed` 能重投**，且重投真的把那一行排回可投递状态。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def test_only_a_failed_row_can_be_retried(self):
        """`failed` ⇒ 变 `pending` + `attempts` 清零 + **`next_attempt_at` 清空**。

        ⭐ **`next_attempt_at` 那一条是承重的**（任务书点名的口径，与上家侧那条实现一致）：
        失败行带着一个**未来**的退避时刻；不清它 ⇒ 常驻投递进程下一轮按
        `(status, next_attempt_at)` 取数时**取不到**这一行 ⇒ 页面显示"重投成功"，
        而它**下次该投的时刻还是原来的退避点**（一次静默不生效）。
        ⚠️ 弄红它：把 `delivery.next_attempt_at = None` 那一行删掉 ⇒ 本用例红
        （`next_attempt_at` 仍是那个未来时刻）。
        ⚠️ `reason` / `response_status` / `processed_at` **刻意保留**（与上家侧逐字同一套口径）。
        """
        moment = timezone.now() + timedelta(hours=6)
        row = self.make_delivery(
            self.app, next_attempt_at=moment, processed_at=timezone.now(),
        )
        data = self.assert_ok(
            self.post_json(RETRY_TEMPLATE.format(pk=self.app.pk, delivery_pk=row.pk))
        )
        row.refresh_from_db()
        # ① 库里的终态（判据打在**数据库行**上，⛔ 不是只看响应）
        self.assertEqual(row.status, STATUS_PENDING)
        self.assertEqual(row.attempts, 0)
        self.assertIsNone(
            row.next_attempt_at,
            "重投没有清 `next_attempt_at` ⇒ 退避没到点，这一行**取不出来**（重投静默不生效）",
        )
        # ② 刻意保留的那三列
        self.assertEqual(row.reason, REASON_TEXT)
        self.assertEqual(row.response_status, 503)
        self.assertIsNotNone(row.processed_at)
        # ③ 响应 = 重排后的那一行（键集与列表 item 逐字相同）
        self.assertEqual(set(data), LEDGER_ROW_KEYS)
        self.assertEqual(data["id"], row.pk)
        self.assertEqual(data["status"], STATUS_PENDING)
        self.assertEqual(data["attempts"], 0)
        self.assertIsNone(data["next_attempt_at"])

    def test_a_pending_row_cannot_be_retried(self):
        """`pending` ⇒ `1000` + 点名，**那一行逐列一字未变**。

        ⚠️ 弄红它：把视图里那条 `status != FAILED` 的判据去掉 ⇒ 本用例红
        （待投递的行被重排、`attempts` 被清零 = 退避表被重置）。
        """
        row = self.make_delivery(self.app, status=STATUS_PENDING, attempts=2)
        before = _row_snapshot(row)
        self.assert_business_failure(
            self.post_json(RETRY_TEMPLATE.format(pk=self.app.pk, delivery_pk=row.pk)),
            VALIDATION,
            msg_fragment="只有投递失败的推送记录可以重投",
        )
        row.refresh_from_db()
        self.assertEqual(_row_snapshot(row), before, "被拒的重投改动了那一行")

    def test_a_done_row_cannot_be_retried(self):
        """⭐ `done` ⇒ `1000` 点名（这才是这条闸真正要挡的：**对方会收到两次**同一个事件）。

        ⚠️ 弄红它：同上（去掉那条判据）。
        """
        row = self.make_delivery(self.app, status=STATUS_DONE, attempts=1,
                                 next_attempt_at=None)
        before = _row_snapshot(row)
        self.assert_business_failure(
            self.post_json(RETRY_TEMPLATE.format(pk=self.app.pk, delivery_pk=row.pk)),
            VALIDATION,
            msg_fragment="只有投递失败的推送记录可以重投",
        )
        row.refresh_from_db()
        self.assertEqual(_row_snapshot(row), before)

    def test_a_delivery_of_another_app_is_not_found(self):
        """子资源不是装饰：拿 A 应用的 pk 去重投 **B 应用**那条行 ⇒ `1000`「推送记录不存在」。"""
        other = self.make_app(name=OTHER_APP_NAME, app_key=OTHER_APP_KEY,
                              app_secret=OTHER_APP_SECRET, taker=self.user_a)
        foreign = self.make_delivery(other, order_no=OTHER_ORDER_NO)
        before = _row_snapshot(foreign)
        self.assert_business_failure(
            self.post_json(RETRY_TEMPLATE.format(pk=self.app.pk, delivery_pk=foreign.pk)),
            VALIDATION,
            msg_fragment="推送记录不存在",
        )
        foreign.refresh_from_db()
        self.assertEqual(_row_snapshot(foreign), before)

    def test_a_missing_delivery_is_a_business_error_not_a_404(self):
        """不存在的 `<delivery_pk>` ⇒ `1000`（⛔ 不回 404：业务结果不用 HTTP 状态码表达）。"""
        self.assert_business_failure(
            self.post_json(RETRY_TEMPLATE.format(pk=self.app.pk, delivery_pk=99999999)),
            VALIDATION,
            msg_fragment="推送记录不存在",
        )

    def test_the_retry_endpoint_does_not_accept_a_body_it_should_ignore(self):
        """多余 body 既不 500、也不被误判成 `1000`（本端点没有入参）。"""
        row = self.make_delivery(self.app)
        self.assert_ok(
            self.post_json(
                RETRY_TEMPLATE.format(pk=self.app.pk, delivery_pk=row.pk),
                {"whatever": "ignored"},
            )
        )
        row.refresh_from_db()
        self.assertEqual(row.status, STATUS_PENDING)


# ── ④b ⭐ 跨族对拍：重投闸只有一份实现（T5b，红队 Important）────────────────────


class RetryGateParityTests(_CallbackApiFixture):
    """⭐ 同样的三态（`failed` / `pending` / `done`）在两族的重投端点上给出**同一套语义**。

    ## 为什么需要它（红队 Important 的落点）

    "只有 `failed` 能重投"这道闸与那句文案，收口前是**两族视图里各一份逐字副本**
    （`apps/openapi/api/app_views.py` 与 `apps/takerapi/api/app_views.py`）⇒ 谁被单独放松
    都不会有症状（另一族的网照样绿）。收口后它只有一份实现
    （`apps/openapi/dispatch.py::prepare_retry`），本用例把两族**并排**驱动一次：
    该通的**都**通（那一行真被重排成"立刻可投"）、该拒的**都**拒（`1000` + 那一行逐列一字未变）。

    ## 判据落在哪（⛔ 不是"两边各调了一次就算对拍"）

    每族各生成一张**三态读数表**（`(业务码, 重排后的 status, 重排后的 attempts,
    重排后的 next_attempt_at, 那一行有没有被动过)`），然后**三条**一起断：
    ① 打手侧那张表 == 写死的期望表；② 上家侧那张表 == 同一张期望表；
    ③ 两张表**逐格相同**。
    ⚠️ 只断 ③ 是不够的：两族**同时**被放松时它也绿（那正是"共享件被改坏"的形状）⇒ ① ② 是承重的。
    ⚠️ 文案**不参与对拍**：它是两族各自的数据（spec §2.3 的纪律；今天恰好逐字相同）
    ⇒ 文案按**值**在本族那条腿上各断一次（`E-0024`：钉值不钉常量）。

    ## 弄红它（两条都实测过，读数见 T5b 报告）

    - 把 `prepare_retry` 的 `failed` 判据改成恒真 ⇒ ① ② 在 `pending` / `done` 两格红；
    - 让**某一族的视图**绕开共享件（自己写一份放松的判据）⇒ **那一族**的格子红、另一族仍绿
      ——这正是"某一族被单独放松"的形状。
    """

    #: 上家侧那一族的重投路径（与 `apps/openapi/urls.py` 逐字一致；⚠️ 带尾斜杠）。
    OPENAPI_RETRY_TEMPLATE = "/api/open/apps/{pk}/deliveries/{delivery_pk}/retry/"

    #: 三态在两张表里的**期望读数**（⭐ 写死，⛔ 不从被测件派生）：
    #: `failed` ⇒ 通（`pending` / `attempts=0` / 租约清空）；`pending` / `done` ⇒ 拒 + 一字未变。
    EXPECTED = {
        STATUS_FAILED: (OK, STATUS_PENDING, 0, None, True),
        STATUS_PENDING: (VALIDATION, STATUS_PENDING, 3, "2026-10-09T12:00:00+00:00", False),
        STATUS_DONE: (VALIDATION, STATUS_DONE, 1, None, False),
    }

    #: 那两档被拒时的**完整一句**人话（值写在这里，⛔ 不从 `TAKER_SPEC` / `OPENAPI_SPEC` 派生）。
    RETRY_ONLY_FAILED_TEXT = (
        "只有投递失败的推送记录可以重投（已投递成功的记录重投会让对方收到两次）"
    )

    #: 三态各自给台账行的初值（两族**同一套**，否则"逐格相同"这条判据没有意义）。
    #: ⚠️ `next_attempt_at` 取一个**固定时刻**（在该时刻的两族行上是同一个值 ⇒ 可逐格比）。
    LEASE = datetime(2026, 10, 9, 12, 0, tzinfo=dt_timezone.utc)

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        # ⭐ 上家侧那一族：同一个租户、同一个管理员账号管得着（节点门两族共用
        # `system.pinduoduo_settings`）⇒ 对拍只差"是哪一族的端点 / 哪一张台账"。
        self.openapi_app = ApiApp.objects.create(
            name=OPENAPI_APP_NAME,
            app_key=OPENAPI_APP_KEY,
            app_secret=OPENAPI_APP_SECRET,
            created_by=self.user_a,
        )

    def _taker_row(self, state, n):
        return self.make_delivery(
            self.app,
            order_no=f"20261009000001{n:04d}",
            status=state,
            attempts=3 if state in (STATUS_FAILED, STATUS_PENDING) else 1,
            next_attempt_at=None if state == STATUS_DONE else self.LEASE,
        )

    def _openapi_row(self, state, n):
        return OpenApiDelivery.objects.create(
            app=self.openapi_app,
            event=EVENT_STATUS,
            order_no=f"20261009000002{n:04d}",
            source_order_no="TKCB5-PARITY-0001",
            payload={},
            status=state,
            attempts=3 if state in (STATUS_FAILED, STATUS_PENDING) else 1,
            reason=REASON_TEXT,
            response_status=503,
            next_attempt_at=None if state == STATUS_DONE else self.LEASE,
        )

    def _reading(self, row):
        """一行台账的读数（重排后现取；`next_attempt_at` 归一成 ISO 串或 `None`）。"""
        lease = row.next_attempt_at
        return (
            row.status,
            row.attempts,
            None if lease is None else lease.isoformat(),
        )

    def _drive(self, *, url_for, row_for, label):
        """把三态各打一遍，回**读数表**；顺带在本族的腿上断"文案 == 那一句"。"""
        readings = {}
        for state in (STATUS_FAILED, STATUS_PENDING, STATUS_DONE):
            row = row_for(state, len(readings) + 1)
            before = _row_snapshot(row)
            response = self.post_json(url_for(row))
            body = self.body(response)
            row.refresh_from_db()
            readings[state] = (body["code"], *self._reading(row), _row_snapshot(row) != before)
            if state == STATUS_FAILED:
                continue
            # 被拒那两档：文案按**值**各断一次（两族今天逐字相同，但那是各自的数据）。
            with self.subTest(family=label, state=state):
                self.assertEqual(
                    body["msg"], self.RETRY_ONLY_FAILED_TEXT,
                    "被拒时那句话必须逐字是本族那一句（⛔ 不是另一族的、也不是新编的）",
                )
        return readings

    def test_both_families_agree_on_the_three_states(self):
        """⭐ 三态 × 两族：期望表逐格相同，且两族**逐格相同**。"""
        taker = self._drive(
            label="taker",
            url_for=lambda row: RETRY_TEMPLATE.format(pk=self.app.pk, delivery_pk=row.pk),
            row_for=self._taker_row,
        )
        openapi = self._drive(
            label="openapi",
            url_for=lambda row: self.OPENAPI_RETRY_TEMPLATE.format(
                pk=self.openapi_app.pk, delivery_pk=row.pk
            ),
            row_for=self._openapi_row,
        )
        self.assertEqual(
            taker, self.EXPECTED,
            "打手侧那一族的三态读数与期望不符（该通的通、该拒的拒）",
        )
        self.assertEqual(
            openapi, self.EXPECTED,
            "上家侧那一族的三态读数与期望不符（该通的通、该拒的拒）",
        )
        self.assertEqual(
            taker, openapi,
            "两族的重投闸给的不是同一套语义（某一族被单独放松 / 绕开了共享件）",
        )


# ── ⑤ 身份限权（spec §9-9）─────────────────────────────────────────────────


class DeliveryIdentityScopingTests(_CallbackApiFixture):
    """⭐ 打手只能看 / 重投**自己**应用的台账；别人的 ⇒ 与"不存在"**同形**。"""

    def setUp(self):
        super().setUp()
        self.mine = self.make_app()
        self.theirs = self.make_app(
            name=OTHER_APP_NAME, app_key=OTHER_APP_KEY, app_secret=OTHER_APP_SECRET,
            taker=self.user_b,
        )

    def test_a_taker_cannot_list_another_takers_ledger(self):
        """打手 A 看 B 的台账 ⇒ `1000` + 「应用不存在」（与"这个 pk 不存在"**同一句**）。

        ⚠️ 同形是**有意**的：回 403 等于告诉 A"这个 pk 存在、只是不是你的"
        ⇒ 一条可枚举应用主键的信道（理由逐条写在 `_get_app` 的 docstring 里）。
        ⚠️ **反例（钉住"回应不是空的"）**：B 那一行台账**一个字段都没变**（逐列快照）。
        ⚠️ 弄红它：把 `_get_app` 里的 `self._visible_apps(user)` 换成
        `TakerApp.objects`（去掉 `filter(taker=user)`）⇒ 本用例红成 `code=0` + B 的台账。
        """
        row = self.make_delivery(self.theirs, order_no=OTHER_ORDER_NO)
        before = _row_snapshot(row)
        self.assert_business_failure(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.theirs.pk), client=self.taker_a),
            VALIDATION,
            msg_fragment="应用不存在",
        )
        row.refresh_from_db()
        self.assertEqual(_row_snapshot(row), before)

    def test_a_taker_cannot_retry_another_takers_row(self):
        """打手 A 重投 B 那条行 ⇒ 与"不存在"同形，且**那一行逐列一字未变**（逐列快照）。"""
        row = self.make_delivery(self.theirs, order_no=OTHER_ORDER_NO)
        before = _row_snapshot(row)
        self.assert_business_failure(
            self.post_json(
                RETRY_TEMPLATE.format(pk=self.theirs.pk, delivery_pk=row.pk),
                client=self.taker_a,
            ),
            VALIDATION,
            msg_fragment="应用不存在",
        )
        row.refresh_from_db()
        self.assertEqual(
            _row_snapshot(row), before,
            "A 把 B 的那一行重排了（身份限权失效）",
        )

    def test_a_taker_cannot_patch_another_takers_callback_config(self):
        """打手 A 改 B 的回调地址 ⇒ 与"不存在"同形，且 B 那一行**逐列一字未变**。"""
        before = _app_snapshot(self.theirs)
        self.assert_business_failure(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=self.theirs.pk),
                {"callback_url": OTHER_CALLBACK_URL, "callback_state": "disabled"},
                client=self.taker_a,
            ),
            VALIDATION,
            msg_fragment="应用不存在",
        )
        self.theirs.refresh_from_db()
        self.assertEqual(_app_snapshot(self.theirs), before, "B 的那一行被 A 改动了")

    def test_a_taker_can_read_and_retry_their_own_ledger(self):
        """⭐ **正对照**（能失败）：A 看**自己**的台账 ⇒ 通；重投**自己**的失败行 ⇒ 通。

        ⚠️ 缺了它，"什么都拒"的实现也能让上面三条绿。
        """
        row = self.make_delivery(self.mine)
        data = self.assert_ok(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.mine.pk), client=self.taker_a)
        )
        self.assertEqual([item["id"] for item in data["items"]], [row.pk])
        self.assert_ok(
            self.post_json(
                RETRY_TEMPLATE.format(pk=self.mine.pk, delivery_pk=row.pk),
                client=self.taker_a,
            )
        )
        row.refresh_from_db()
        self.assertEqual(row.status, STATUS_PENDING)

    def test_an_account_without_the_node_is_forbidden(self):
        """登录但**没有**门里那两个节点 ⇒ **403**（⛔ 不是 `1000`、也⛔ 不是 401）。

        ⚠️⭐ **夹具纪律（实测评注，不是防御性写法）**：这个账号必须**一个 Group 都不建**
        （`groups=()`）。`make_logged_in_client(nodes=…)` 把节点挂到**该账号所有 Group**
        对应的 `Role` 上（`tests/p5_auth_helpers.py::_grant_nodes`）⇒ 若本文件的夹具已经
        给「代练员」那个 Role 挂了 `system.allow_taker_open_api`（打手 A / B 就是这么拿到的），
        再建一个同组账号**会白捡那个节点**、本用例会以"200 而不是 403"红
        （实测踩到过一次：那种红**看起来像节点门失效**，实际是夹具串了组）。
        """
        nobody = make_logged_in_client(username="tkcb5-nobody", groups=())
        row = self.make_delivery(self.mine)
        self.assertEqual(
            self.get(DELIVERIES_TEMPLATE.format(pk=self.mine.pk), client=nobody).status_code,
            FORBIDDEN,
        )
        self.assertEqual(
            self.post_json(
                RETRY_TEMPLATE.format(pk=self.mine.pk, delivery_pk=row.pk), client=nobody
            ).status_code,
            FORBIDDEN,
        )

    def test_an_anonymous_request_is_unauthenticated(self):
        """匿名 ⇒ **401**（两条端点各测一遍；判据打在**行为**上，⛔ 不读 `permission_classes`）。"""
        row = self.make_delivery(self.mine)
        self.assertEqual(
            self.anonymous.get(DELIVERIES_TEMPLATE.format(pk=self.mine.pk)).status_code,
            UNAUTHENTICATED,
        )
        self.assertEqual(
            self.anonymous.post(
                RETRY_TEMPLATE.format(pk=self.mine.pk, delivery_pk=row.pk)
            ).status_code,
            UNAUTHENTICATED,
        )


# ── ⑥ 留痕（spec §9-10）────────────────────────────────────────────────────


class CallbackAuditTrailTests(_CallbackApiFixture):
    """改回调地址 / 改回调开关 / 手工重投 ⇒ **各一行** `OperationLog`，且**不含凭据值**。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def test_changing_the_callback_url_writes_one_log_row(self):
        """一行，且 `field_name` 是**真实列名**、中文由标签表派生（⛔ 句子里不出现裸列名）。"""
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk),
                            {"callback_url": OTHER_CALLBACK_URL})
        )
        logs = list(self.logs_for(ACTION_APP_UPDATE))
        self.assertEqual(len(logs), 1, [row.summary for row in logs])
        log = logs[0]
        self.assertEqual(log.field_name, "callback_url")
        self.assertEqual(log.to_value, OTHER_CALLBACK_URL)
        self.assertEqual(log.from_value, CALLBACK_URL)
        self.assertEqual(log.target_kind, "taker_app")
        self.assertEqual(str(log.target_id), str(self.app.pk))
        self.assertEqual(log.target_name, APP_NAME)
        self.assertIn("回调地址", log.summary, "中文标签必须由 `FIELD_LABELS` 派生")
        self.assertNotIn("callback_url", log.summary, "句子里不许出现裸列名")

    def test_changing_the_callback_state_writes_one_log_row_with_chinese_labels(self):
        """开关那一条的两个值都是**中文 label**（与 `state` 那条同款）。"""
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk),
                            {"callback_state": "disabled"})
        )
        logs = list(self.logs_for(ACTION_APP_UPDATE))
        self.assertEqual(len(logs), 1, [row.summary for row in logs])
        log = logs[0]
        self.assertEqual(log.field_name, "callback_state")
        self.assertEqual(log.from_value, "启用")
        self.assertEqual(log.to_value, "停用")
        self.assertIn("回调开关", log.summary)

    def test_changing_both_fields_writes_two_rows(self):
        """⭐ **每个真正变化的字段各一行**（⛔ 不是把两个字段挤进一句）。"""
        self.assert_ok(
            self.patch_json(
                DETAIL_TEMPLATE.format(pk=self.app.pk),
                {"callback_url": OTHER_CALLBACK_URL, "callback_state": "disabled"},
            )
        )
        logs = list(self.logs_for(ACTION_APP_UPDATE))
        self.assertEqual(len(logs), 2, [row.summary for row in logs])
        self.assertEqual(
            sorted(row.field_name for row in logs), ["callback_state", "callback_url"]
        )

    def test_a_manual_retry_writes_exactly_one_log_row(self):
        """重投 ⇒ 恰好一行：动作码 / 目标 / 事件中文名 / 单号逐列断言。"""
        row = self.make_delivery(self.app, next_attempt_at=timezone.now() + timedelta(hours=1))
        self.assert_ok(
            self.post_json(RETRY_TEMPLATE.format(pk=self.app.pk, delivery_pk=row.pk))
        )
        logs = list(self.logs_for(ACTION_DELIVERY_RETRY))
        self.assertEqual(len(logs), 1, [entry.summary for entry in logs])
        log = logs[0]
        self.assertEqual(log.log_type, 40, "动作码的分类列应当是 `LogType.SYSTEM`（40）")
        self.assertEqual(log.target_kind, "taker_app")
        self.assertEqual(str(log.target_id), str(self.app.pk))
        self.assertEqual(log.target_name, APP_NAME)
        self.assertEqual(log.order_no, ORDER_NO)
        self.assertEqual(log.to_value, "订单状态变化")
        # ⛔ `reason` 不进留痕（它可能装着对方响应体片段）
        self.assertNotIn(REASON_TEXT, log.summary)

    def test_the_audit_rows_never_carry_a_credential_value(self):
        """⭐ 三条留痕**逐列**扫凭据值（`CAST(col AS text) = 值` 全等，⛔ 不用 `icontains`）。

        ⚠️ 照 `tests/test_openapi_app_api.py::AppSecretNeverLoggedTests` 的手法：
        `app_secret` 一个字都不许进任何文本列——判据是"这一列**等于**那个值"，
        ⛔ 不是"这一列里**含**某个词"（子串判据在截断 / 拼接下给出假保证）。

        ⭐ **T5b 追加的第二条腿（红队 Minor）**：按列**全等**那条抓不到"**密钥被拼进句子**"
        （例如 `to_value` 变成 `"轮换密钥 " + secret`：整列 ≠ 那个值，却**含**它）
        ⇒ 再**逐列 `not in`** 扫一遍，扫的是**当次真正用到**的两样东西：
        ① 本应用的 `app_secret`（换令牌用的就是它）；② **当次换到的那颗令牌串**。
        ⚠️ 两条判据各自能红、覆盖面**不同**：全等那条管"整列就是那个值"，`not in` 这条管
        "那个值出现在这一列文本的任意位置"（含被拼进句子的形状）。
        ⛔ 这里**不是**拿模糊匹配当唯一判据 —— `not in` 是逐列的**精确子串**判定
        （没有通配位、不做大小写折叠、不跨列拼接）。
        ⚠️ **响应那一半的边界（如实说破）**：`PATCH` 的响应体**按设计就带明文 `app_secret`**
        （`D-0201` 的口径："明文可随时获取"）⇒ ⛔ 不拿它当判据（那是自相矛盾，失败响应那一半
        另有 `test_no_secret_ever_reaches_a_failure_response` 在管）。所以响应这一侧只断
        **两件站得住的事**：① 三跳响应里都**不许**出现那颗令牌串；② **重投**的响应是
        **台账行载荷**（13 键、按设计不含凭据）⇒ 它里面也不许出现密钥。
        """
        row = self.make_delivery(self.app)
        # ⚠️ 令牌**先换**再打那三跳：它是"当次用到"的那颗（换它用的就是 `APP_SECRET`）。
        token = self.issue_token(self.app)
        self.assertTrue(token, "对照：令牌真的换到了（空串会让下面那条 `not in` 恒真）")
        patched = self.patch_json(
            DETAIL_TEMPLATE.format(pk=self.app.pk),
            {"callback_url": OTHER_CALLBACK_URL, "callback_state": "disabled"},
        )
        self.assert_ok(patched)
        retried = self.post_json(
            RETRY_TEMPLATE.format(pk=self.app.pk, delivery_pk=row.pk)
        )
        self.assert_ok(retried)
        self.assertEqual(
            OperationLog.objects.filter(
                action__in=(ACTION_APP_UPDATE, ACTION_DELIVERY_RETRY)
            ).count(),
            3,
            "三跳各一行留痕（改地址 / 改开关 / 重投）",
        )
        for secret in (APP_SECRET, OTHER_APP_SECRET):
            with self.subTest(secret=secret):
                self.assertEqual(
                    self.log_texts_containing(secret), [],
                    "凭据值出现在了操作日志的某个文本列里",
                )

        # ── ⭐ 第二条腿：**逐列 `not in`**（当次用到的密钥 + 当次换到的令牌）──────────
        needles = (APP_SECRET, OTHER_APP_SECRET, token)
        log_rows = OperationLog.objects.filter(
            action__in=(ACTION_APP_UPDATE, ACTION_DELIVERY_RETRY)
        )
        self.assertEqual(len(log_rows), 3, "对照：这一轮真的落了三条留痕")
        for log_row in log_rows:
            for column, value in self._text_columns(log_row):
                for needle in needles:
                    self.assertNotIn(
                        needle, value,
                        f"留痕 {log_row.action} 的 `{column}` 列里**含**凭据值"
                        "（按列全等那条抓不到这种拼进句子的形状）",
                    )

        # 响应侧：令牌串三跳都不许出现；重投那条是台账行载荷 ⇒ 密钥也不许出现。
        for name, response in (("patch", patched), ("retry", retried)):
            text = response.content.decode("utf-8", "replace")
            self.assertNotIn(token, text, f"{name} 响应体里出现了令牌串")
        self.assertNotIn(
            APP_SECRET, retried.content.decode("utf-8", "replace"),
            "重投的响应是台账行载荷（按设计不含凭据）⇒ 密钥不许出现",
        )

    @staticmethod
    def _text_columns(row):
        """一条留痕行的**每一个字符串列**（逐列扫，⛔ 不挑列：`E-0064` 同族）。"""
        for field in row._meta.concrete_fields:
            value = getattr(row, field.attname, None)
            if isinstance(value, str):
                yield field.name, value


# ── ⑦ 取消订阅后不再产生新行（spec §9-6）───────────────────────────────────


class CallbackUnsubscribeTests(_CallbackApiFixture):
    """⭐ 把 `callback_state` 改成 `disabled` ⇒ 之后对该单做一次状态变化 ⇒ **零新行**。"""

    def setUp(self):
        super().setUp()
        # ⚠️ 本组**必须**先有那一条应用：⛔ 不加这一句则"取消订阅之前"那一跳也会是零行
        #    （没有可用收件人），于是"零新行"那条判据**恒真** —— 实测踩到过
        #    （症状：`0 != 1`，看起来像入队坏了）。
        self.app = self.make_app()

    def test_disabling_the_callback_stops_new_rows_but_keeps_history(self):
        """⭐ 判据打在**表的行数**上（⛔ 不是"接口没报错"）。

        ⚠️ 驱动的是**真实业务路径**（`transition_order`，与
        `tests/test_takerapi_callback_recipients.py::RealPathCoverageTests` 同一条）——
        ⛔ 不是直接调 `notify_*` 包装（那只证明包装能跑，证明不了"入队点被停用挡在门外"）。
        ⚠️ 同时钉住**历史行不许被删**：取消订阅只是"之后不再产生"，⛔ 不是清理。
        ⚠️ 弄红它：把收件人判据里的 `callback_state=…` 去掉 ⇒ 本用例红（多一行）。
        """
        order = self.make_order(11)
        # 取消订阅**之前**先制造一条历史行（真实状态变化 ⇒ 真入队）。
        outcome = transition_order(
            order, target=OrderStatus.IN_PROGRESS, ctx=self.ctx(), dry_run=False
        )
        self.assertTrue(outcome.ok, outcome)
        history = list(TakerApiDelivery.objects.order_by("id"))
        self.assertEqual(len(history), 1, [row.payload for row in history])

        # 取消订阅（经**端点**改，走真实写入路径）。
        self.assert_ok(
            self.patch_json(DETAIL_TEMPLATE.format(pk=self.app.pk),
                            {"callback_state": "disabled"})
        )
        self.app.refresh_from_db()
        self.assertEqual(self.app.callback_state, CallbackState.DISABLED)

        # 之后对该单再做一次状态变化 ⇒ **零新行**。
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)
        outcome = transition_order(
            order, target=OrderStatus.PAUSED, ctx=self.ctx(), dry_run=False
        )
        self.assertTrue(outcome.ok, outcome)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PAUSED, "状态变化本身要真的发生")
        self.assertEqual(
            TakerApiDelivery.objects.count(), 1,
            "取消订阅之后仍产生了新行（三条『不投递』判据里那条没兜住）",
        )
        # 历史行**一个字段都没变**（⛔ 不是"被删了"）。
        self.assertEqual(
            [_row_snapshot(row) for row in TakerApiDelivery.objects.order_by("id")],
            [_row_snapshot(row) for row in history],
            "取消订阅把已有的历史行改动 / 删除了",
        )

    def test_re_enabling_the_callback_resumes_new_rows(self):
        """**正对照**（能失败）：开着的时候同一条路上产生新行。

        ⚠️ 缺了它，上面那条"零新行"会被"这条路径压根不入队"骗过（那种情况下它恒绿）。
        """
        order = self.make_order(12)
        outcome = transition_order(
            order, target=OrderStatus.IN_PROGRESS, ctx=self.ctx(), dry_run=False
        )
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(TakerApiDelivery.objects.count(), 1)
        order.refresh_from_db()
        outcome = transition_order(
            order, target=OrderStatus.PAUSED, ctx=self.ctx(), dry_run=False
        )
        self.assertTrue(outcome.ok, outcome)
        self.assertEqual(
            TakerApiDelivery.objects.count(), 2,
            "同一条路上第二次状态变化没有入队 ⇒ 上面那条'零新行'失去判别力",
        )


# ── ⑧ 畸形入参（补 `test_no_route_500` 显式路由腿的盲区）────────────────────


class DeliveryMalformedInputTests(_CallbackApiFixture):
    """⭐ 两条路由**都带路径转换器** ⇒ 被 `test_no_route_500` 的显式路由腿跳过 ⇒ 这里补上。

    判据 = **一条都不许 500**，且每一档的期望码**逐条写出**（⛔ 不是"跑过就算"）。
    """

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.row = self.make_delivery(self.app)

    def test_a_huge_pk_is_a_business_error_not_a_500(self):
        """天文数字 pk ⇒ `1000`「应用不存在」（psycopg 按 `numeric` 送 ⇒ PostgreSQL 比出 0 行）。

        ⚠️ 与上家侧那条同款：**刻意没有"pk 上界"守卫**（去掉它没有用例会红 ⇒ 按
        `AGENTS.md` §5-E 不留那种"防御性写法写成承重逻辑"），但行为被本用例钉住。
        """
        huge = "9" * 40
        self.assert_business_failure(
            self.get(f"/api/taker/apps/{huge}/deliveries/"),
            VALIDATION,
            msg_fragment="应用不存在",
        )
        self.assert_business_failure(
            self.post_json(f"/api/taker/apps/{huge}/deliveries/{self.row.pk}/retry/"),
            VALIDATION,
            msg_fragment="应用不存在",
        )

    def test_a_huge_delivery_pk_is_a_business_error_not_a_500(self):
        """天文数字 `delivery_pk` ⇒ `1000`「推送记录不存在」。"""
        self.assert_business_failure(
            self.post_json(
                f"/api/taker/apps/{self.app.pk}/deliveries/{'9' * 40}/retry/"
            ),
            VALIDATION,
            msg_fragment="推送记录不存在",
        )

    def test_a_non_numeric_pk_does_not_match_the_route(self):
        """非数字 pk ⇒ `<int:pk>` **不匹配** ⇒ 404（⛔ 不是 500，也⛔ 不是 `1000`）。"""
        self.assertEqual(
            self.get("/api/taker/apps/abc/deliveries/").status_code, NOT_FOUND_ROUTE
        )
        self.assertEqual(
            self.post_json("/api/taker/apps/abc/deliveries/1/retry/").status_code,
            NOT_FOUND_ROUTE,
        )

    def test_malformed_pagination_parameters(self):
        """`page` / `page_size` 的畸形值 ⇒ 分页器自己的既有行为（越界页 `1002`），⛔ 不许 500。

        ⚠️ 与 `/api/business/` 下每一个分页列表**逐字同款**（本类不特判：那会让同一种输入
        在不同列表上给出不同的码）。
        """
        url = DELIVERIES_TEMPLATE.format(pk=self.app.pk)
        self.assert_business_failure(self.get(url, data={"page": 0}), PAGE_OUT_OF_RANGE_CODE)
        self.assert_business_failure(self.get(url, data={"page": 999}), PAGE_OUT_OF_RANGE_CODE)
        # 非数字的 `page` / `page_size`：DRF 的既有读数（`1002`），⛔ 不是 500。
        for params in ({"page": "abc"}, {"page_size": "abc"}):
            with self.subTest(params=params):
                response = self.get(url, data=params)
                self.assertNotEqual(response.status_code, 500)
                self.assertIn(
                    self.body(response)["code"], (OK, PAGE_OUT_OF_RANGE_CODE),
                    self.body(response),
                )
