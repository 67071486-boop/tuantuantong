"""T5：打手侧**对外**端点之一 —— **拉可接单列表**（`GET /api/taker/v1/hall/`）。

spec：`docs/specs/2026-10-08-taker-open-api-design.md` §5.1（行白名单）· §5.3-1（范围判据）·
§9 的 1（①）/ 2 / 5（①③）/ 8 / 9 / 10 条。

## 本文件证什么（逐条都能失败）

| # | 判据 | spec |
|---|---|---|
| 1 | ⭐ **范围收窄**：拿打手甲的令牌拉大厅 ⇒ 他**没有游戏权限**的那款游戏里的厅单**不出现**（①） | §9-1 / §9-5-① |
| 2 | ⭐ **正对照**：他有权限的那款游戏里**确实有一张厅单** ⇒ 那张单**必须出现** | §9-5-③ |
| 3 | ⭐ **"确实在厅"的第二个视角**：同一张单在**另一个打手**（有那款游戏权限）的大厅里**在** | §9-1 的前提 |
| 4 | ⭐ **跨凭据类型打真路由**：打手令牌打 `/api/open/v1/…` ⇒ **401**；上家令牌打本端点 ⇒ **401**；会话打本端点 ⇒ **401**；打手令牌打内部 `/api/taker/apps/` ⇒ **401** | §9-2 |
| 5 | ⭐ **P2 字段**：行键集合**恰好**那 11 个（字面量写在下面，⛔ 不从常量 import）＋显式点名"不许出现的键"一个都不在 | §9-8 |
| 6 | ⭐ **对外可搜的值 = 行上「订单号」键显示的那个值**（红队 2026-10-08 改准 + D-0338 改准）：① 字面量**恰好集合**断言；② **展示值搜得到**（有来源号的单=来源号、自录单=内置号）；③④⑤ 不可见字段的值（手机号 / QQ / 角色名 / **有来源号单的内置号**）⇒ 搜不出来 | §9-9 + 不变式「可搜的值 ⊆ 载荷里看得见的值」 |
| 7 | **分页**：`page` / `page_size` 非法 ⇒ `1000`（⛔ 不是 500）；`page_size` 超 100 ⇒ 夹到 100；`count` 是命中总数 | §5（统一口径） |
| 8 | **筛选真的接线**：`game` / `project_text` / 价格区间三条各真的收窄（否则"什么都返回"也能让上面几条绿） | §5 端点表第 1 行 |
| 9 | **大厅取数⛔ 不经过内部的"两路合并读口"**：那个合并函数**一次都没被调用** | §5.3-1 |

## ⚠️ 证明力边界（如实写清楚）

- 令牌**一律从 `/api/taker/oauth/token` 换**（走真路由 + 真 `Client`，⛔ 不手搓夹具），
  与 spec §9-2 的要求同款；
- 本文件**不**证明内部大厅那两路合并的行为（那是内部那条线的既有网的事）——
  它只证**本端点**没走那条路；
- 夹具一律**合成值**（`AGENTS.md` §5-C），来源 IP 取文档保留段（⛔ 不是任何真实客户端）。
"""
import json
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business import jidanpu_upstream
from apps.business.models import (
    EmpType,
    Game,
    GameServer,
    Order,
    OrderStatus,
    PeerHallMirror,
    PeerUpstreamBinding,
    Performance,
)
from apps.core.datetime_display import local_iso
from apps.openapi.models import ApiApp
from apps.takerapi.api import hall_views
from apps.takerapi.models import TakerApp
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

# ── 路径（与 `apps/takerapi/urls.py` 逐字一致；对外两条都**带尾斜杠**）──────────
HALL_PATH = "/api/taker/v1/hall/"
TOKEN_PATH = "/api/taker/oauth/token"
OPENAPI_TOKEN_PATH = "/api/open/oauth/token"
OPENAPI_ORDERS_PATH = "/api/open/v1/orders/"
INTERNAL_APPS_PATH = "/api/taker/apps/"

GRANT_TYPE = "client_credentials"

# ── 合成凭据（⛔ 与任何真实租户的 appKey / secret 无关）────────────────────────
TAKER_A_KEY = "takerhallkey000001"
TAKER_A_SECRET = "taker-hall-secret-000000000001"
TAKER_B_KEY = "takerhallkey000002"
TAKER_B_SECRET = "taker-hall-secret-000000000002"
OPENAPI_KEY = "openhallkey0000001"
OPENAPI_SECRET = "open-hall-secret-00000000001"

#: 文档保留段 `203.0.113.0/24` 的地址（⛔ 不是任何真实客户端）。
SOURCE_IP = "203.0.113.41"

# ── 业务码 / HTTP 码字面值（`E-0024`：写数字，让"码值被改掉"在用例里可见）──────
OK = 0
VALIDATION = 1000
UNAUTHENTICATED = 401

# ── ⭐ §5.1 的行白名单：**字面量**写在这里（⛔ 不从 `hall_views` 的任何常量派生
#    ——派生是自指恒真：被断言的集合跟着被测的常量一起变，`E-0024`）──────────────
HALL_ROW_KEYS = {
    "order_no",
    "source_order_no",
    "game",
    "game_name",
    "server",
    "project_text",
    "content",
    "time_requirement",
    "is_urgent",
    "hall_price",
    "created_at",
}

#: ⛔ 一行大厅行里**绝不许**出现的键（`E-0065`：拦截必须**显式枚举**，⛔ 不用"键名里含 customer"
#: 这类模糊判据）。三类来源逐条对应 §5.1 的"不给"：
#: ① 客户联系方式（P2：抢到之前不给）② 员工身份 / 我方内部痕迹 ③ 财务 ④ 镜像那一路的东西。
FORBIDDEN_ROW_KEYS = {
    # ① 客户联系方式
    "customer_phone",
    "customer_qq",
    "customer_ww",
    # ② 员工身份 / 我方内部痕迹
    "remark",
    "sales_agent",
    "created_by",
    "updated_by",
    "hall_publisher",
    "hall_publisher_name",
    # ③ 财务
    "net_received",
    "refund_fee",
    "total_price",
    "received_price",
    "platform_price",
    "fee",
    "payment_method",
    "payment_status",
    # ④ 镜像那一路（`search_hall` 里根本没有这些概念；写出来是为了钉住"没走合并读口"）
    "origin",
    "source_system",
    "remote_order_id",
    "remote_game_area",
    "mirror_pk",
}


class _HallFixture(TenantTestCase):
    """两个打手（各持**不同**的游戏权限）+ 若干张厅单 + 两个应用。"""

    def setUp(self):
        super().setUp()
        self.domain_name = self.domain.domain
        self.anonymous = Client(SERVER_NAME=self.domain_name)
        # 两款游戏：甲有 game_his 的权限、**没有** game_other 的权限（乙两款的权限都给，
        # 用来当"同一张单的第二个视角"）。
        self.game_his = Game.objects.create(name="大厅测试游戏甲")
        self.game_other = Game.objects.create(name="大厅测试游戏乙")
        self.server_his = GameServer.objects.create(game=self.game_his, name="测试区服甲")

        self.taker_a = self._make_taker("hall-taker-a", games=[self.game_his])
        self.taker_b = self._make_taker(
            "hall-taker-b", games=[self.game_his, self.game_other]
        )
        self.app_a = self._make_app("大厅测试应用甲", TAKER_A_KEY, TAKER_A_SECRET, self.taker_a)
        self.app_b = self._make_app("大厅测试应用乙", TAKER_B_KEY, TAKER_B_SECRET, self.taker_b)

        #: 他有权限那款游戏里的厅单 ⇒ **必须出现**（正对照）。
        self.order_his = self._order(
            self.game_his,
            "20261008000000000001",
            project_text="测试项目文本甲",
            price="88.00",
            role_name="测试角色甲",
        )
        #: 他**没有**权限那款游戏里的厅单 ⇒ **不许出现**（范围收窄的那一半）。
        self.order_other = self._order(
            self.game_other, "20261008000000000002", project_text="测试项目文本乙", price="66.00"
        )

    # ── 造物 ───────────────────────────────────────────────────────────
    def _make_taker(self, username, *, games, nodes=("grab_hall.take_order",)):
        """一个**普通打手账号** + 显式的游戏权限（⛔ 不走夹具默认的"全部游戏"）。

        ⭐ Z11：takerapi 的大厅读 / 接单都判
        `grab_hall.take_order` ⇒ 默认给上这颗节点（「合法打手」的正常形状）。
        ⚠️ 要测**无节点**那一档就显式传 `nodes=()`（opt-out，照 D-0136 的夹具口径）。
        `grant_nodes` 只挂**已有 Group** 的 Role ⇒ 先建/进组再授。
        """
        from django.contrib.auth.models import Group

        from tests.p5_auth_helpers import grant_nodes

        user = User.objects.create_user(username=username, password="pw-12345678")
        StaffProfile.objects.create(user=user).games.set(games)
        if nodes:
            group, _ = Group.objects.get_or_create(name="大厅节点")
            user.groups.add(group)
            grant_nodes(user, *nodes)
        return user

    def _make_app(self, name, app_key, app_secret, taker):
        return TakerApp.objects.create(
            name=name,
            app_key=app_key,
            app_secret=app_secret,
            taker=taker,
            created_by=taker,
        )

    def _order(
        self, game, order_no, *, status=OrderStatus.WAITING_GRAB, project_text="",
        price="88.00", role_name="", server=None, content="", time_requirement="",
        is_urgent=False, customer_phone="", customer_qq="",
    ):
        """建一对「订单 + **无主**的 MAIN 绩效行」（= 真的在厅）。

        ⚠️ 绩效行不是可选的：大厅的入选条件就是「待抢单 **且** MAIN 行无主」，
        少了它这张单根本不入厅（`selectors.search_hall` 的内连接）——
        而那样写的用例会**恒真**（`E-0004` 形态）。
        """
        order = Order.objects.create(
            game=game,
            order_no=order_no,
            status=status,
            hall_price=Decimal(price) if price is not None else None,
            project_text=project_text,
            game_role_name=role_name,
            server=server,
            content=content,
            time_requirement=time_requirement,
            is_urgent=is_urgent,
            customer_phone=customer_phone,
            customer_qq=customer_qq,
        )
        Performance.objects.create(
            order=order,
            emp_type=EmpType.MAIN,
            temp_amount=Decimal(price) if price is not None else Decimal("0.00"),
        )
        return order

    # ── 令牌：**只从取令牌端点拿**（spec §9-2 的要求）───────────────────
    def issue_token(self, app, *, path=TOKEN_PATH, key=None, secret=None) -> str:
        response = self.anonymous.post(
            path,
            {
                "grant_type": GRANT_TYPE,
                "client_id": app.app_key if key is None else key,
                "client_secret": app.app_secret if secret is None else secret,
            },
            REMOTE_ADDR=SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        body = json.loads(response.content)
        # ⚠️ 成功体是**裸三字段**（⛔ 没有本仓信封）：令牌从"调用方真正看到的载荷"里取。
        self.assertEqual(set(body), {"access_token", "token_type", "expires_in"})
        return body["access_token"]

    def make_api_app(self):
        """一行上家侧应用（跨凭据类型那一半要用它换一个上家侧令牌）。"""
        return ApiApp.objects.create(
            name="大厅测试上家应用甲",
            app_key=OPENAPI_KEY,
            app_secret=OPENAPI_SECRET,
            created_by=self.taker_a,
        )

    # ── 发请求 ─────────────────────────────────────────────────────────
    def hall(self, token, params=None, *, remote_addr=SOURCE_IP):
        return self.anonymous.get(
            HALL_PATH, params or {}, HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=remote_addr,
        )

    @staticmethod
    def body(response):
        return json.loads(response.content)

    def rows(self, token, params=None):
        """大厅响应里的行（同时把「成功」这件事钉住：HTTP 200 + `code=0`）。"""
        response = self.hall(token, params)
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = self.body(response)
        self.assertEqual(payload["code"], OK, payload)
        return payload["data"]["results"]

    @staticmethod
    def nos_of(rows):
        return sorted(row["order_no"] for row in rows)


class HallScopeTests(_HallFixture):
    """⭐ spec §9-1（①）与 §9-5（①③）：**范围收窄** + **正对照** + **第二个视角**。

    ⚠️ **为什么这三条必须一起写**（`E-0004` 形态的假绿）：只写"别人的单不出现"时，
    一个**恒空**的大厅也能让它绿；所以同时要有
    ① **正对照**（他有权限那款、且厅里确实有一张那样的单 ⇒ 必须出现）；
    ② **第二个视角**（同一张"不许出现"的单，在另一个有那款游戏权限的打手那里**在**）
    ——后者证明那张单**真的在厅**，而不是"它其实没进厅"这种与被测行为无关的原因。
    """

    def test_his_own_game_order_appears(self):
        """⭐ **正对照**：他有权限那款游戏的厅单**必须出现**（缺它，恒空的大厅也能让别条绿）。"""
        token = self.issue_token(self.app_a)
        rows = self.rows(token)
        self.assertEqual(self.nos_of(rows), [self.order_his.order_no])

    def test_an_order_outside_his_game_scope_does_not_appear(self):
        """⭐ 他**没有**权限那款游戏的厅单**不出现**（范围收窄的那一半）。"""
        token = self.issue_token(self.app_a)
        rows = self.rows(token)
        self.assertNotIn(self.order_other.order_no, self.nos_of(rows))
        # ⚠️ 不能只看"它不在"：先证他**确实看得到东西**（否则本断言在恒空大厅上照样绿）。
        self.assertIn(self.order_his.order_no, self.nos_of(rows))

    def test_the_other_order_really_is_in_the_hall_for_someone_else(self):
        """⭐ **第二个视角**：同一张单在**另一个有那款游戏权限的打手**的大厅里**在**。

        判别力：若那张单其实**没进厅**（例如夹具漏了 MAIN 绩效行、或它状态不是待抢单），
        上一条会以"与被测行为无关的原因"变绿；本用例把那个可能性排除掉。
        """
        token_b = self.issue_token(self.app_b)
        nos = self.nos_of(self.rows(token_b))
        self.assertEqual(nos, sorted([self.order_his.order_no, self.order_other.order_no]))
        self.assertIn(self.order_other.order_no, nos)

    def test_the_list_is_narrowed_by_his_account_not_by_an_anonymous_user(self):
        """⭐ **收窄的主体是绑定账号**（⛔ 不是 `request.user`）——这条判据专门钉那件事。

        机制：Bearer 认证成功时 `request.user` 是 `AnonymousUser()`，而 `games_of(匿名)` 是**空集**
        ⇒ 若实现拿 `request.user` 去收窄，大厅会**恒空**（而"别人的单不出现"照样绿）。
        判别力：这一条要求**自己的厅单必须出现**——恒空就红。
        ⚠️ 另外两条边界一起钉住：他把**别的应用**（同一个账号）拿来换令牌也看得到（范围来自**账号**、
        不是来自**应用**），以及**另一个账号**的令牌看不到（范围真的按账号分）。
        """
        token = self.issue_token(self.app_a)
        self.assertIn(self.order_his.order_no, self.nos_of(self.rows(token)))
        # 同一个账号的第二套凭据（新应用）：范围跟着**账号**走 ⇒ 看得到同样的东西。
        app_a2 = self._make_app("大厅测试应用甲二", "takerhallkey000003",
                                "taker-hall-secret-000000000003", self.taker_a)
        self.assertEqual(
            self.nos_of(self.rows(self.issue_token(app_a2))),
            [self.order_his.order_no],
        )
        # 另一个账号（少了 game_his 的权限）：看不到那张单（范围按账号分）。
        token_c = self.issue_token(
            self._make_app("大厅测试应用丙", "takerhallkey000004",
                           "taker-hall-secret-000000000004",
                           self._make_taker("hall-taker-c", games=[self.game_other]))
        )
        self.assertEqual(self.nos_of(self.rows(token_c)), [self.order_other.order_no])


class HallNeverMergesTheUpstreamBranchTests(_HallFixture):
    """⛔ **不许经过内部那个"合并读口"的装配**（spec §5.3-1；D-0296 改准）。

    ## 这条判据挡的是什么（比"键集对不对"更硬）

    D-0296起本端点**并入了镜像单**（业务方实测打手 API 拉不到镜像单），
    取数**复用**内部读口的同一把 `upstream_hall_queryset`（同源圈定 = 不自造第二套判据）。
    但内部读口的**装配三件**（`merge_hall_rows_two` 的两路归并、
    `serialize_upstream_row` / `serialize_upstream_hall_rows` 的全量投影——28+ 键、
    含 `remote_*` / 客户 / 财务键）**仍然一个都不能经过**：对外载荷只有 §5.1 的 11 键 +
    行身份（`origin` / `mirror_pk`），投影在本模块自己写（`hall_mirror_row_payload`）。
    ⇒ 本用例钉「那三件一次都没被调用」。

    ⚠️ **打桩打在"被查找的那个模块"上**（`E-0029`）：`apps.business.jidanpu_upstream`，
    ⛔ 不是本视图模块。桩会让那几个函数**抛异常**（而不是返回空）——这样"调用了但结果为空"
    与"根本没调用"不会混成同一种绿（返回空的话，一次误调用可能被后面的序列化吃掉）。
    """

    def test_the_internal_merge_and_full_projection_are_never_called(self):
        token = self.issue_token(self.app_a)

        def _boom(*args, **kwargs):
            raise AssertionError("本端点不许复用内部读口的归并 / 全量投影（spec §5.3-1）")

        with mock.patch.object(jidanpu_upstream, "merge_hall_rows_two", _boom), \
                mock.patch.object(jidanpu_upstream, "serialize_upstream_row", _boom), \
                mock.patch.object(jidanpu_upstream, "serialize_upstream_hall_rows", _boom):
            rows = self.rows(token)
        # 桩没接上的对照组：这一条同时证明"大厅确实回了行"（否则打桩把请求打挂也可能被读成绿）。
        self.assertEqual(self.nos_of(rows), [self.order_his.order_no])


class HallPayloadTests(_HallFixture):
    """⭐ spec §9-8：**P2 字段** —— 行键集合**恰好**那 10 个，且显式点名的禁键一个都不在。"""

    def test_the_source_order_no_filter_is_exact(self):
        """⭐ 2026-10-23：`?source_order_no=` 只回**逐字相等**的那一张（⛔ 不做子串）。

        ⭐ D-0338：行上 `order_no` 键 = **来源号优先展示号** ⇒ 有来源号的单
        这里回的展示号就是来源号本身（`nos_of` 收的是行上的 `order_no` 键）。
        """
        self.order_his.source_order_no = "SRC-HIS-001"
        self.order_his.save(update_fields=["source_order_no"])
        token = self.issue_token(self.app_a)
        rows = self.rows(token, {"source_order_no": "SRC-HIS-001"})
        self.assertEqual(self.nos_of(rows), ["SRC-HIS-001"])
        self.assertEqual(self.rows(token, {"source_order_no": "SRC-HIS"}), [])

    def test_native_row_order_no_is_source_first(self):
        """⭐ D-0338（2026-10-30，业务方「移动端大厅改成来源单号」）：原生行 `order_no`
        键 = **来源号非空取它**；`source_order_no` 键照旧给原值（对照/精确筛选用）。

        ⚠️ 这与**镜像行**的既有口径对齐（`hall_mirror_row_payload` / `hall_peer_row_payload`
        的 `order_no` 本来就是各自的展示号）——三路的「订单号」键从此都是"给打手看、
        拼接单 URL 用的那个号"。
        """
        self.order_his.source_order_no = "SRC-HIS-001"
        self.order_his.save(update_fields=["source_order_no"])
        row = self.rows(self.issue_token(self.app_a))[0]
        self.assertEqual(row["order_no"], "SRC-HIS-001")
        self.assertEqual(row["source_order_no"], "SRC-HIS-001")

    def test_native_row_order_no_falls_back_to_internal_when_source_empty(self):
        """⭐ D-0338 的**回落半边**：纯自录单（无来源号）⇒ `order_no` 键 = 内置单号（不空屏）。"""
        self.assertEqual(self.order_his.source_order_no, "")
        row = self.rows(self.issue_token(self.app_a))[0]
        self.assertEqual(row["order_no"], self.order_his.order_no)
        self.assertEqual(row["source_order_no"], "")

    def test_the_row_keys_are_exactly_the_whitelist(self):
        token = self.issue_token(self.app_a)
        rows = self.rows(token)
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        # ⚠️ 判据是**等号**（⛔ 不是"包含"）：多一个键就红。
        self.assertEqual(set(row), HALL_ROW_KEYS, sorted(row))
        # ⚠️ 再点一遍"不许有"：等号那条已经蕴含它，但这一条在**读失败信息**时能一眼看出
        #    泄漏的是哪一类（客户联系方式 / 员工身份 / 财务 / 镜像）。
        leaked = sorted(set(row) & FORBIDDEN_ROW_KEYS)
        self.assertEqual(leaked, [], f"大厅行泄漏了这些键：{leaked}")

    def test_the_payload_values_come_from_the_order(self):
        """⭐ 值也钉住（⛔ 不只是键集）：`game` 是 **id**、`game_name` 是名字、
        `hall_price` 是**两位小数字符串**、`created_at` 是 ISO 8601。"""
        self.order_his.content = "测试客户原文"
        self.order_his.time_requirement = "测试时间要求"
        self.order_his.is_urgent = True
        self.order_his.server = self.server_his
        self.order_his.save(
            update_fields=["content", "time_requirement", "is_urgent", "server"]
        )
        row = self.rows(self.issue_token(self.app_a))[0]
        self.assertEqual(row["order_no"], self.order_his.order_no)
        self.assertEqual(row["game"], self.game_his.pk)          # ⭐ id，⛔ 不是名字
        self.assertEqual(row["game_name"], "大厅测试游戏甲")      # 名字另有一个键
        self.assertEqual(row["server"], self.server_his.pk)
        self.assertEqual(row["project_text"], "测试项目文本甲")
        self.assertEqual(row["content"], "测试客户原文")
        self.assertEqual(row["time_requirement"], "测试时间要求")
        self.assertIs(row["is_urgent"], True)
        self.assertEqual(row["hall_price"], "88.00")
        # D-0302：`created_at` 是**项目时区**（`+08:00`，`local_iso`）——
        # TAKER-API 文档 §5.1 的示例本就是 `+08:00`；旧写法裸 `.isoformat()` 出 `+00:00` 与文档矛盾。
        self.assertEqual(row["created_at"], local_iso(self.order_his.created_at))
        self.assertTrue(row["created_at"].endswith("+08:00"))

    def test_hall_price_is_the_effective_taker_price_not_the_order_column(self):
        """⭐ `hall_price` 取**生效接单价**（绩效覆盖价优先），⛔ 不是 `Order.hall_price` 原值。

        判别力：夹具把订单列设成 `88.00`、把 MAIN 行的 `taker_price` 覆盖成 `123.45`
        ⇒ 实现若读 `order.hall_price`（或读错注解），这里会拿到 `"88.00"` 而红。
        ⚠️ 与内部大厅**同源**（同一个注解常量 `selectors.EFFECTIVE_TAKER_PRICE`），
        所以这条也顺带钉住"两条路的价不是两份判据"。
        """
        Performance.objects.filter(order=self.order_his).update(
            taker_price=Decimal("123.45")
        )
        row = self.rows(self.issue_token(self.app_a))[0]
        self.assertEqual(row["hall_price"], "123.45")
        self.assertEqual(self.order_his.hall_price, Decimal("88.00"))  # 订单列**没变**

    def test_a_null_price_row_is_reported_as_null(self):
        """没有接单价的厅单照样在厅（抢它会回 `1013`）⇒ 那一行如实回 `null`（⛔ 不是 `"0.00"`）。

        ⚠️ `D-0015` 之下 `null` 与 `0` 是两件事：把 NULL 渲染成 `"0.00"` 会让第三方
        以为"这单白干"，而 `check_grab_limits` 的判据恰恰是「没有接单价 ⇒ 不可抢」。
        """
        self._order(self.game_his, "20261008000000000003", price=None)
        rows = self.rows(self.issue_token(self.app_a))
        by_no = {row["order_no"]: row for row in rows}
        self.assertIsNone(by_no["20261008000000000003"]["hall_price"])
        self.assertEqual(by_no[self.order_his.order_no]["hall_price"], "88.00")


class HallKeywordTests(_HallFixture):
    """⭐ spec §9-9（红队 2026-10-08 改准）+ **D-0338（2026-10-30）改准**：对外 `keyword`
    的**匹配值** = 行上「订单号」键**显示的那个值**。

    ## 判据的来源是一条**不变式**（⛔ 不是"安全考虑"这类说法）

    **对外可搜的值必须 ⊆ 响应载荷里看得见的值**（§3.2 那 11 个键）。可搜而不可见 = 一条
    **可探测的信道**：拿一个值当 `keyword`，"命中 / 不命中"就能问出"这张单的某个字段
    是不是这个值"，而那个字段调用方**根本看不到**。

    ⚠️ **D-0338 改准的来由**：原生行的 `order_no` 键改成**来源号优先展示号**之后，
    "订单号键显示的值"在有来源号的单上 = **来源号**、在自录单上 = **内置单号**。
    于是旧钉「`source_order_no` 的值搜不出来」**作废**——它就是显示的订单号本身
    （起它本来就在载荷里、D-0301/D-0338 起它就是要给人看的号）；
    反倒必须**新增**「内置号在有来源号的单上搜不出来」（它在这条载荷里已经**不可见**，
    继续可搜就是红队定义的那条信道）。手机号 / QQ / 角色名三类**照旧搜不出来**。

    ## 判据缺一不可（缺了对照，"关键词功能整个坏掉"也能让别的绿）

    ① 字面量**恰好集合**断言（⛔ 不是 `issubset` / `toContain`）；
    ② **展示值搜得到**（有来源号的单用来源号搜到；自录单用内置号搜到——两个正对照）；
    ③④⑤ 三类**不可见**字段的值当 `keyword` ⇒ 那张单**搜不出来**（含"有来源号的单的内置号"）。

    ⚠️ ③④⑤ 用的是**同一张在厅的单**上的真值（⛔ 不是编出来的字符串）：实现若"照内部字段表
    搜"，这些条必红。⚠️ 而"它在厅"这件事由 ② 证明（同一张单搜得到）。
    """

    def setUp(self):
        super().setUp()
        self.order_his.customer_phone = "13300000001"
        self.order_his.customer_qq = "990000001"
        #: `source_order_no` = 淘宝 / 闲鱼那条外部单号（D-0338 起它就是行上显示的订单号）。
        self.order_his.source_order_no = "TB2026100888888"
        self.order_his.save(
            update_fields=["customer_phone", "customer_qq", "source_order_no"]
        )

    def test_the_searchable_field_set_is_exactly_the_display_no(self):
        """① ⭐ 字面量**恰好集合**断言（⛔ 不是 `issubset` / `toContain`）。

        ⚠️ 期望值**写字面量**（⛔ 不从 `hall_views` 派生任何东西）：派生是自指恒真。
        变异自证：往 `hall_views.HALL_KEYWORD_FIELDS` 里加**任意一个**键 ⇒ 本条当场红
        （断的是"恰好"，不是"至少包含"）。
        ⭐ D-0338：匹配面从 `order_no` 列改成 `display_no` **同值注解列**
        （= 行上「订单号」键显示的值；`NullIf` 包空串来源号、回落内置号）——
        "恰好一个匹配值"的形状不变，变的只是它对准的值。
        """
        self.assertEqual(hall_views.HALL_KEYWORD_FIELDS, {"display_no"})

    def test_the_displayed_source_no_is_searchable(self):
        """② ⭐ **正对照（D-0338 改准）**：有来源号的单，拿**来源号**（= 行上显示的订单号）
        搜**搜得到**，回的行 `order_no` 键也是它。

        ⚠️ 旧钉「来源号搜不出来」是 2026-10-08 的口径——当时载荷里**没有**这个键；
       键在、D-0338 起它就是展示的订单号 ⇒ 旧口径与"可见才可搜"不变式
        **自相矛盾**，本条是它的改准（不是放宽）。
        """
        token = self.issue_token(self.app_a)
        rows = self.rows(token, {"keyword": self.order_his.source_order_no})
        self.assertEqual(self.nos_of(rows), [self.order_his.source_order_no])

    def test_an_internal_order_no_of_a_source_bearing_row_is_not_searchable(self):
        """③ ⭐ **D-0338 新增的那条信道钉**：有来源号的单，**内置单号**在这条载荷里已经
        看不见 ⇒ 拿它当关键词**搜不出来**（否则就是红队定义的"可搜而不可见"信道）。
        """
        token = self.issue_token(self.app_a)
        self.assertEqual(self.rows(token, {"keyword": self.order_his.order_no}), [])

    def test_an_internal_order_no_of_a_self_entered_row_is_searchable(self):
        """②的回落半边：自录单（无来源号）显示的就是内置号 ⇒ 拿内置号**搜得到**。"""
        other = self._order(
            self.game_his, "20261008000000000003", project_text="自录对照单", price="12.00"
        )
        token = self.issue_token(self.app_a)
        rows = self.rows(token, {"keyword": other.order_no})
        self.assertEqual(self.nos_of(rows), [other.order_no])

    def test_a_customer_phone_is_not_searchable(self):
        token = self.issue_token(self.app_a)
        self.assertEqual(self.rows(token, {"keyword": "13300000001"}), [])

    def test_a_customer_qq_is_not_searchable(self):
        token = self.issue_token(self.app_a)
        self.assertEqual(self.rows(token, {"keyword": "990000001"}), [])

    def test_a_role_name_is_not_searchable(self):
        """⑤ ⭐ **红队抓到的信道之二**：角色名（`game_role_name`）的值当关键词 ⇒ 搜不出来。

        ⚠️ 角色名**不在** §3.2 的载荷里（载荷给的是 `project_text` / `content` 这些），
        与"不可见 ⇒ 不可搜"同一族；内部 `KEYWORD_FIELDS` 含它。
        """
        token = self.issue_token(self.app_a)
        self.assertEqual(self.rows(token, {"keyword": "测试角色甲"}), [])


class HallFilterTests(_HallFixture):
    """§5 端点表第 1 行那六个筛选：**真的接线**（否则"什么都返回"也能让别的判据绿）。"""

    def setUp(self):
        super().setUp()
        self.cheap = self._order(
            self.game_his, "20261008000000000011", project_text="便宜单", price="10.00"
        )
        self.pricey = self._order(
            self.game_his, "20261008000000000012", project_text="贵单", price="300.00"
        )
        # 前置哨兵：三条都**真的**在大厅里（否则"被筛掉"与"本来就不在"不可区分）。
        self.assertEqual(
            self.nos_of(self.rows(self.issue_token(self.app_a))),
            sorted([self.order_his.order_no, self.cheap.order_no, self.pricey.order_no]),
        )

    def test_the_game_filter_accepts_only_his_own_games(self):
        """`?game=` 传**没有权限**那款 ⇒ 空集（⛔ 不是"回退成不过滤"）。

        ⚠️ 这里传的是 `game_other` 的 id：它是**合法参数值**（形状对），
        只是不在他的范围里 ⇒ 必须回空集。若实现把"范围"与"筛选"判成两件互不相干的事
        （先按 `?game=` 取数、再按范围过滤时用 `OR`），这里就会漏出 `order_other`。
        """
        token = self.issue_token(self.app_a)
        self.assertEqual(self.rows(token, {"game": self.game_other.pk}), [])
        self.assertEqual(
            self.nos_of(self.rows(token, {"game": self.game_his.pk})),
            sorted([self.order_his.order_no, self.cheap.order_no, self.pricey.order_no]),
        )

    def test_the_price_range_filter_narrows_both_ends(self):
        token = self.issue_token(self.app_a)
        self.assertEqual(
            self.nos_of(self.rows(token, {"price_max": "100.00"})),
            sorted([self.cheap.order_no, self.order_his.order_no]),
        )
        self.assertEqual(
            self.nos_of(self.rows(token, {"price_min": "50.00"})),
            sorted([self.order_his.order_no, self.pricey.order_no]),
        )

    def test_the_project_text_filter_narrows(self):
        token = self.issue_token(self.app_a)
        self.assertEqual(
            self.nos_of(self.rows(token, {"project_text": "便宜"})), [self.cheap.order_no]
        )

    def test_the_server_filter_narrows(self):
        self.cheap.server = self.server_his
        self.cheap.save(update_fields=["server"])
        token = self.issue_token(self.app_a)
        self.assertEqual(
            self.nos_of(self.rows(token, {"server": self.server_his.pk})),
            [self.cheap.order_no],
        )


class HallPaginationAndParamsTests(_HallFixture):
    """分页与入参（spec §5 的统一口径；§9-10 的"非法入参 ⇒ `1000`，⛔ 不是 500"）。"""

    def setUp(self):
        super().setUp()
        self.extra = [
            self._order(self.game_his, f"2026100800000000002{i}", price="20.00")
            for i in range(1, 4)
        ]

    def test_page_window_is_returned_and_count_is_the_match_total(self):
        token = self.issue_token(self.app_a)
        response = self.hall(token, {"page": 1, "page_size": 2})
        self.assertEqual(response.status_code, 200, response.content[:300])
        data = self.body(response)["data"]
        # ⚠️ `data` 的键集也是契约（§5 统一口径）：恰好这四个。
        self.assertEqual(set(data), {"count", "results", "page", "page_size"})
        self.assertEqual(data["count"], 4)          # ⭐ 命中**总数**（⛔ 不是本页条数）
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 2)

    def test_a_second_page_returns_the_next_window(self):
        token = self.issue_token(self.app_a)
        first = self.body(self.hall(token, {"page": 1, "page_size": 2}))["data"]
        second = self.body(self.hall(token, {"page": 2, "page_size": 2}))["data"]
        self.assertEqual(len(first["results"]), 2)
        self.assertEqual(len(second["results"]), 2)
        # 两页不许重叠（顺序稳定是分页契约的一部分：`Order.Meta.ordering` 带 `-id` 兜底）。
        self.assertEqual(
            set(r["order_no"] for r in first["results"])
            & set(r["order_no"] for r in second["results"]),
            set(),
        )

    def test_the_default_page_size_is_20(self):
        token = self.issue_token(self.app_a)
        data = self.body(self.hall(token))["data"]
        self.assertEqual(data["page_size"], 20)
        self.assertEqual(data["count"], 4)

    def test_an_over_large_page_size_is_clamped_to_100(self):
        token = self.issue_token(self.app_a)
        data = self.body(self.hall(token, {"page_size": "1000"}))["data"]
        self.assertEqual(data["page_size"], 100)   # ⭐ 夹到上限（⛔ 不是报错）
        self.assertEqual(data["count"], 4)

    def test_illegal_pagination_params_are_1000_not_500(self):
        """`?page=abc` / `?page=0` / `?page=-1` / `?page_size=abc` / `?page_size=0` ⇒ `1000`。

        ⚠️ 判据是「**这一类**请求还会不会 500」（`E-0010`）：未校验的值直接进切片会抛
        `ValueError` ⇒ HTTP 500，而它不是业务结果。
        """
        token = self.issue_token(self.app_a)
        for params in (
            {"page": "abc"}, {"page": "0"}, {"page": "-1"},
            {"page_size": "abc"}, {"page_size": "0"}, {"page_size": "-5"},
        ):
            with self.subTest(params=params):
                response = self.hall(token, params)
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(self.body(response)["code"], VALIDATION)

    def test_illegal_filter_params_are_1000_not_500(self):
        """`?game=abc` / `?price_max=abc` / `?server=abc` ⇒ `1000`（⛔ 不是 500）。"""
        token = self.issue_token(self.app_a)
        for params in ({"game": "abc"}, {"price_max": "abc"}, {"server": "abc"}):
            with self.subTest(params=params):
                response = self.hall(token, params)
                self.assertEqual(response.status_code, 200, response.content[:300])
                self.assertEqual(self.body(response)["code"], VALIDATION)

    def test_an_out_of_range_page_is_an_empty_window_not_an_error(self):
        """页号超出范围 ⇒ 空 `results` + **真实** `count`（⛔ 不报错、也⛔ 不回 `1002`）。

        「这一页没有行」是分页的正常结果；本仓的 `1002` 口径留给"这条业务对象不存在"。
        """
        token = self.issue_token(self.app_a)
        data = self.body(self.hall(token, {"page": 9, "page_size": 2}))["data"]
        self.assertEqual(data["results"], [])
        self.assertEqual(data["count"], 4)


class CrossCredentialTypeOnRealRoutesTests(_HallFixture):
    """⭐ spec §9-2：**跨凭据类型**（评审 T2b 第 6 条点名的"打真路由"版）。

    ## 为什么必须在**真路由**上再钉一遍

    `tests/test_takerapi_auth.py` 的 `CrossCredentialTypeTests` 用的是**探针视图**
    （装配与真端点逐字相同，但视图是自己写的）⇒ 它证的是**认证器**，证不了
    "**这条真端点有没有挂对这个认证器**"。本类四条都打**真实 URL**，
    所以任何一条对外端点挂错认证类（或内部端点装上了对外认证类）都会在这里红。

    ⚠️ **会话那一条是"反向"的承重判据**：三组不混的纪律里，最容易被"顺手统一"掉的就是
    "内部端点只认会话"（`apps/takerapi/api/app_views.py` 的装配**刻意不声明**
    `authentication_classes`）。所以两个方向都要有。
    """

    def test_a_taker_token_is_rejected_by_the_openapi_route(self):
        """打手令牌打**上家侧**真路由 ⇒ **401**（两族各查各的表 ⇒ 结构性 401）。"""
        token = self.issue_token(self.app_a)
        response = self.anonymous.get(
            OPENAPI_ORDERS_PATH, HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        payload = self.body(response)
        self.assertEqual(payload["code"], UNAUTHENTICATED)
        self.assertIsNone(payload["data"])

    def test_an_openapi_token_is_rejected_by_the_taker_hall_route(self):
        """上家侧令牌打**打手侧**真路由 ⇒ **401**。"""
        api_app = self.make_api_app()
        token = self.issue_token(api_app, path=OPENAPI_TOKEN_PATH)
        response = self.hall(token)
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        payload = self.body(response)
        self.assertEqual(payload["code"], UNAUTHENTICATED)
        self.assertEqual(payload["msg"], "应用凭据无效")

    def test_a_session_request_is_rejected_by_the_taker_hall_route(self):
        """⭐ **会话打对外端点 ⇒ 401**（对外面⛔ 一个字节的会话都不认）。"""
        session = make_logged_in_client(
            domain=self.domain_name, username="hall-session-user", games=None
        )
        response = session.get(HALL_PATH)
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        self.assertEqual(self.body(response)["code"], UNAUTHENTICATED)

    def test_a_taker_token_is_rejected_by_the_internal_apps_route(self):
        """⭐ 反向：打手令牌打**内部** `/api/taker/apps/` ⇒ **401**（内部只认会话）。

        ⚠️ 这条打在**真内部路由**上（T3 已有一条**同义**的判据，但它走的是同一组真路由；
        本行在此重复登记的理由是"三组不混"要**在这条对外端点的用例里也在场**——
        只看 T3 那份用例的人读不到本批新增的两条对外路由）。
        """
        token = self.issue_token(self.app_a)
        response = self.anonymous.get(
            INTERNAL_APPS_PATH, HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP
        )
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        self.assertEqual(self.body(response)["code"], UNAUTHENTICATED)

    def test_the_hall_route_is_401_without_any_credential(self):
        """⭐ 对照组（⛔ 缺了它，上面几条对"什么都拒"的实现也会绿）。"""
        response = self.anonymous.get(HALL_PATH, REMOTE_ADDR=SOURCE_IP)
        self.assertEqual(response.status_code, UNAUTHENTICATED, response.content[:200])
        # 而**有**合法令牌时它是 200（否则本类四条只是在描述"这里永远 401"）。
        rows = self.rows(self.issue_token(self.app_a))
        self.assertEqual(self.nos_of(rows), [self.order_his.order_no])


# ── D-0296 / T6：镜像单并入打手 API 的大厅 + 镜像接单端点 ──────────

MIRROR_CLAIM_PATH = "/api/taker/v1/hall/upstream/{pk}/claim/"

#: 镜像行的对外键集 = §5.1 的 11 键 + 行身份两个（字面量，⛔ 不从实现派生）。
MIRROR_ROW_KEYS = HALL_ROW_KEYS | {"origin", "mirror_pk"}

#: ⛔ 镜像行也**绝不许**出现的键（全量投影的 remote_* / 客户 / 财务 / 模拟单）。
MIRROR_FORBIDDEN_KEYS = {
    "source_system", "remote_order_id", "remote_order_view", "remote_game_area",
    "remote_game_server", "remote_hours_desc", "sim_pk", "is_preferred",
    "is_exited", "exit_kind",
    "customer_phone", "customer_qq", "customer_ww", "remark", "sales_agent",
    "created_by", "net_received", "refund_fee", "total_price",
    "payment_method", "payment_status", "taker_settled",
}


class HallMirrorFixture(_HallFixture):
    """在 `_HallFixture` 之上加：一份上家绑定 + 两款游戏的镜像行。"""

    def setUp(self):
        super().setUp()
        from apps.business.models import JidanpuUpstreamBinding, JidanpuUpstreamHallOrder

        self.binding_owner = User.objects.create_user(
            username="taker-hall-up-owner", password="pw-12345678"
        )
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.binding_owner,
            jidanpu_base_url="https://taker-mirror.test.invalid",
            account="acc-taker-mirror", password="pw-canary-taker-mirror",
            enabled=True,
        )
        self.mirror_his = self._mirror("20261024000000000901", game=self.game_his,
                                       first_seen_at=self.minutes_ago(30))
        # 乙游戏（甲没有权限）的镜像行：范围收窄与写侧 fail-closed 的夹具。
        self.mirror_other = self._mirror("20261024000000000902", game=self.game_other,
                                         first_seen_at=self.minutes_ago(20))
        self.mirror_fresh = self._mirror("20261024000000000903", game=self.game_his,
                                         first_seen_at=self.minutes_ago(5))

    @staticmethod
    def minutes_ago(minutes):
        from django.utils import timezone
        from datetime import timedelta

        return timezone.now() - timedelta(minutes=minutes)

    def _mirror(self, order_id, *, game, first_seen_at, rec_fee="88.00",
                status=None, view_no=None):
        from apps.business.models import JidanpuUpstreamHallOrder

        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=self.binding.jidanpu_base_url,
            upstream_order_id=order_id,
            owner_binding=self.binding,
            status=status or JidanpuUpstreamHallOrder.Status.LISTED,
            game=game, project_text="打手侧镜像合成项目",
            remote_rec_fee=Decimal(rec_fee),
            remote_game_area="安卓", remote_game_server="微信区",
            upstream_payload={
                "orderId": order_id, "orderIdView": view_no or f"V-{order_id}",
                "recFee": rec_fee,
            },
            first_seen_at=first_seen_at,
        )


class HallMirrorRowTests(HallMirrorFixture):
    """镜像行出现在大厅里：键集 / 排序 / 值 / 范围。"""

    def test_listed_mirror_rows_appear_with_their_own_key_shape(self):
        """镜像行**出现在大厅**且键集恰好 11 + `origin` + `mirror_pk`（禁键一个不在）。"""
        token = self.issue_token(self.app_a)
        rows = self.rows(token)
        by_no = {r["order_no"]: r for r in rows}
        self.assertIn("V-20261024000000000901", by_no, "镜像行没进打手大厅")
        row = by_no["V-20261024000000000901"]
        self.assertEqual(set(row), MIRROR_ROW_KEYS, sorted(row))
        leaked = sorted(set(row) & MIRROR_FORBIDDEN_KEYS)
        self.assertEqual(leaked, [], f"镜像行泄漏了这些键：{leaked}")
        self.assertEqual(row["origin"], "upstream")
        self.assertEqual(row["mirror_pk"], self.mirror_his.pk)
        # 值映射：区服文案进 content、server=null、勾兑价两位小数。
        self.assertEqual(row["content"], "微信区")
        self.assertIsNone(row["server"])
        self.assertEqual(row["hall_price"], "88.00")
        self.assertEqual(row["game_name"], "大厅测试游戏甲")
        self.assertEqual(row["source_order_no"], "V-20261024000000000901")

    def test_mirror_rows_merge_with_native_by_their_hall_moments(self):
        """镜像按 `first_seen_at` 与原生 `created_at` **归并倒序**（同拍镜像在后）。"""
        token = self.issue_token(self.app_a)
        # 甲可见的原生单 created_at ≈ 夹具 setUp；镜像三张（5/20/30 分钟前）。
        merged = [r["order_no"] for r in self.rows(token)]
        nos = [n for n in merged if n.startswith("V-")]
        native_nos = [n for n in merged if not n.startswith("V-")]
        self.assertEqual(
            nos,
            [
                "V-20261024000000000903",     # 5 分钟前的镜像
                "V-20261024000000000901",     # 30 分钟前的镜像
            ],
            f"镜像之间没有按 first_seen_at 倒序：{merged}",
        )
        # 原生单（created_at = 本拍）排在最前（与 5 分钟前的镜像可分辨）。
        self.assertEqual(merged[0], self.order_his.order_no)
        self.assertEqual(native_nos, [self.order_his.order_no])

    def test_mirror_rows_respect_the_takers_game_scope(self):
        """范围收窄对镜像行生效：甲（无乙游戏权限）看不到乙游戏的镜像；乙看得到。"""
        token_a = self.issue_token(self.app_a)
        nos_a = [r["order_no"] for r in self.rows(token_a)]
        self.assertNotIn("V-20261024000000000902", nos_a)
        self.assertIn("V-20261024000000000901", nos_a)  # 正对照：甲看得到甲游戏的镜像
        token_b = self.issue_token(self.app_b)
        nos_b = [r["order_no"] for r in self.rows(token_b)]
        self.assertIn("V-20261024000000000902", nos_b)

    def test_keyword_matches_only_the_mirror_display_no(self):
        """`keyword` 对镜像行匹配**展示号**（与原生 `order_no` 同款 icontains）。"""
        token = self.issue_token(self.app_a)
        rows = self.rows(token, {"keyword": "V-20261024000000000901"})
        self.assertEqual(
            self.nos_of(rows), ["V-20261024000000000901"]
        )
        # 谁都不匹配的关键词 ⇒ 空（对照：搜索真的在筛，不是恒返回全部）。
        self.assertEqual(self.rows(token, {"keyword": "TK-NO-SUCH-DISPLAY"}), [])

    def test_the_source_order_no_filter_matches_the_mirror_display_no(self):
        """`source_order_no` 精确筛对镜像行匹配展示号。"""
        token = self.issue_token(self.app_a)
        rows = self.rows(token, {"source_order_no": "V-20261024000000000901"})
        self.assertEqual(self.nos_of(rows), ["V-20261024000000000901"])
        self.assertEqual(self.rows(token, {"source_order_no": "V-202610240000000009"}), [])

    def test_the_price_range_filter_works_on_the_blended_price(self):
        """价格区间对镜像行按**勾兑展示价**收窄（与内部读口同一判定）。"""
        token = self.issue_token(self.app_a)
        # 三张镜像都是 88.00；原生单 order_his 88.00、order_other 66.00（乙游戏，甲看不到）。
        rows = self.rows(token, {"price_min": "88.00"})
        self.assertEqual(
            [r["order_no"] for r in rows],
            [
                self.order_his.order_no,      # 原生（最新）
                "V-20261024000000000903",
                "V-20261024000000000901",
            ],
            f"价格下限筛选结果不对：{[r['order_no'] for r in rows]}",
        )
        self.assertEqual(self.rows(token, {"price_max": "10.00"}), [])
        self.assertEqual(self.rows(token, {"price_min": "10.00", "price_max": "50.00"}), [])

    def test_the_server_filter_hides_mirror_rows_like_the_internal_reader(self):
        """`?server=` ⇒ 镜像行整路消失（镜像是文本区服、没有 FK——内部读口同款口径）。"""
        token = self.issue_token(self.app_a)
        rows = self.rows(token, {"server": self.server_his.pk})
        self.assertEqual(
            [n for n in self.nos_of(rows) if n.startswith("V-")], [],
            "带区服筛选时镜像行不该出现",
        )

    def test_the_in_memory_pagination_counts_both_streams(self):
        """内存分页：`count` = 两路合计（镜像并入后 total 变了）。"""
        token = self.issue_token(self.app_a)
        data = self.body(self.hall(token, {"page": 1, "page_size": 2}))["data"]
        self.assertEqual(set(data), {"count", "results", "page", "page_size"})
        # 甲可见：原生 1（order_his）+ 镜像 2（甲游戏的两张）= 3。
        self.assertEqual(data["count"], 3)
        self.assertEqual(len(data["results"]), 2)
        second = self.body(self.hall(token, {"page": 2, "page_size": 2}))["data"]
        self.assertEqual(len(second["results"]), 1)


class HallMirrorClaimTests(HallMirrorFixture):
    """镜像接单端点：`POST /api/taker/v1/hall/upstream/<pk>/claim/`。"""

    def _claim(self, token, pk, *, key="mirror-claim-0001"):
        return self.anonymous.post(
            MIRROR_CLAIM_PATH.format(pk=pk),
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_claiming_a_listed_mirror_promotes_it(self):
        """接一张在挂镜像 ⇒ `code 0` + 成功三键；行转正、真的建了订单。"""
        from apps.business.models import JidanpuUpstreamHallOrder, Order

        stub = _MirrorStubClient()
        token = self.issue_token(self.app_a)
        with mock.patch.object(jidanpu_upstream.jidanpu, "claim_session",
                               lambda _b: _MirrorCtx(stub)):
            response = self._claim(token, self.mirror_his.pk)
        body = self.body(response)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(set(body["data"]), {"order_no", "status", "status_text"})
        self.mirror_his.refresh_from_db()
        self.assertEqual(
            self.mirror_his.status, JidanpuUpstreamHallOrder.Status.PROMOTED
        )
        # 转正建出的单：价是勾兑价（转正建单写它，见 promote_upstream_mirror）。
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.hall_price, Decimal("88.00"))

    def test_claiming_an_already_claimed_mirror_answers_1012(self):
        """对已不在挂（promoted）的镜像再点 ⇒ `1012`（与内部读口同款失败出口）。"""
        from apps.business.models import JidanpuUpstreamHallOrder

        self.mirror_his.status = JidanpuUpstreamHallOrder.Status.PROMOTED
        self.mirror_his.save(update_fields=["status"])
        token = self.issue_token(self.app_a)
        response = self._claim(token, self.mirror_his.pk, key="mirror-claim-0002")
        body = self.body(response)
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(body["code"], 1012, body)
        self.assertIsNone(body["data"])

    def test_a_mirror_outside_the_takers_game_scope_is_fail_closed(self):
        """写侧范围收窄：甲接乙游戏的镜像 ⇒ `1012` + **零改动**（与不存在同形）。"""
        from apps.business.models import JidanpuUpstreamHallOrder, Order

        before = Order.objects.count()  # 夹具里本来就有两张原生单
        token = self.issue_token(self.app_a)
        response = self._claim(token, self.mirror_other.pk, key="mirror-claim-0003")
        body = self.body(response)
        self.assertEqual(body["code"], 1012, body)
        self.mirror_other.refresh_from_db()
        self.assertEqual(
            self.mirror_other.status, JidanpuUpstreamHallOrder.Status.LISTED
        )
        self.assertEqual(Order.objects.count(), before)

    def test_the_preferred_gate_applies_to_the_mirror_claim(self):
        """⭐ 镜像优选闸（T4 接入②）对这条对外路径自动生效：完成 0 的个人打手被拒。"""
        from apps.business.models import Order

        # taker_a 默认不在「代练员」组 ⇒ 不是个人打手 ⇒ 闸豁免；本用例显式入组。
        from django.contrib.auth.models import Group

        grp, _ = Group.objects.get_or_create(name="代练员")
        self.taker_a.groups.add(grp)
        before = Order.objects.count()  # 夹具里本来就有两张原生单
        # recFee 250 ⇒ 展示价 250 ⇒ 优选。
        mirror = self._mirror("20261024000000000904", game=self.game_his,
                              first_seen_at=self.minutes_ago(1), rec_fee="250.00")
        token = self.issue_token(self.app_a)
        response = self._claim(token, mirror.pk, key="mirror-claim-0004")
        body = self.body(response)
        self.assertEqual(body["code"], 1013, body)
        self.assertEqual(
            body["msg"],
            "优选订单需要先完成过订单才能接：你还没有已完成的订单",
        )
        mirror.refresh_from_db()
        from apps.business.models import JidanpuUpstreamHallOrder

        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(Order.objects.count(), before)

    def test_a_disabled_bindings_stale_card_answers_1012_and_defuses_it(self):
        """⭐ 停用绑定的 listed 僵尸卡按 pk 打（**App 缓存旧大厅列表那条路**）⇒ `1012` + 行转 `gone`。

        2026-10-29 业务方报障修法一：修前这里回 `1015`「还没配置好鸡蛋铺同步账号，请联系
        管理员」且行永远滞留（报障 40 次被拒全打在停用绑定名下的滞留卡上）。修后按已下架
        口径回 `1012`，且**命中即下架**（listed → gone，点一颗拆一颗）；绑定恢复启用后由
        拉取轮的 gone 复活（D-0300）自然收敛——数据保留口径与 E-0288 一致。
        """
        from apps.business.models import (
            HallExitSnapshot,
            JidanpuUpstreamBinding,
            JidanpuUpstreamHallOrder,
        )

        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        token = self.issue_token(self.app_a)
        response = self._claim(token, self.mirror_his.pk, key="mirror-claim-stale-01")
        body = self.body(response)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], "该笔订单已被其他打手接取", body)
        self.assertIsNone(body["data"], body)
        # ⭐ DB 行终态（E-0009：断数据库行，不打中间层）。
        self.mirror_his.refresh_from_db()
        self.assertEqual(self.mirror_his.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertIsNotNone(self.mirror_his.gone_at, "命中即下架必须留 gone_at")
        # 一张 withdrawn 灰卡（照拉取侧 `_mark_gone` 的形态）。
        snaps = HallExitSnapshot.objects.filter(mirror=self.mirror_his)
        self.assertEqual(snaps.count(), 1, "命中即下架应恰好写一张离厅灰卡")
        self.assertEqual(snaps.first().exit_kind, HallExitSnapshot.ExitKind.WITHDRAWN)

    def test_a_recheck_defused_stale_card_keeps_the_gone_write_alive(self):
        """⭐ 点卡先探一跳的拆雷写入必须穿过 `@idempotent` 的失败回滚（停绑拆雷支同一条纪律）：
        滞卡＋复核答「上游已无」⇒ `1012` 且 gone 行在请求之后仍在库里——判据是
        `ClaimError.defused` ⇒ `mark_side_effect`，缺了这一棒幂等层会把下架回滚掉。"""
        from apps.business.models import HallExitSnapshot, JidanpuUpstreamHallOrder

        JidanpuUpstreamHallOrder.objects.filter(pk=self.mirror_his.pk).update(
            last_seen_at=self.minutes_ago(10)
        )
        stub = _MirrorStubClient(segment_rows=[
            {"orderId": "20261024000000009999", "gameName": "合成游戏", "recFee": "88.00"},
        ])
        token = self.issue_token(self.app_a)
        with mock.patch.object(jidanpu_upstream.jidanpu, "claim_session",
                               lambda _b: _MirrorCtx(stub)):
            response = self._claim(token, self.mirror_his.pk, key="mirror-claim-recheck-01")
        body = self.body(response)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], "该笔订单已被其他打手接取", body)
        self.assertIsNone(body["data"], body)
        # 段查真的问过上游、接单写端点一次没打（注定失败的写路径被换掉）。
        self.assertIn("/OrderAjax/GetInternalOrderGrabbingList", stub.paths)
        self.assertNotIn("/OrderAjax/RecOrderByInternal", stub.paths)
        # ⭐ 判据本体：拆雷写入在请求之后仍在库里（幂等层没有把它回滚掉）。
        self.mirror_his.refresh_from_db()
        self.assertEqual(self.mirror_his.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertIsNotNone(self.mirror_his.gone_at, "复核下架必须留 gone_at")
        snaps = HallExitSnapshot.objects.filter(mirror=self.mirror_his)
        self.assertEqual(snaps.count(), 1, "复核下架应恰好写一张离厅灰卡")
        self.assertEqual(snaps.first().exit_kind, HallExitSnapshot.ExitKind.WITHDRAWN)

    def test_the_event_cannot_queue_answers_1015_without_leaking_the_platform_name(self):
        """⭐ 2026-10-29（业务方指令）：1015 在 takerapi 出口**换装**——订单来源一个字不出。

        领域层那句常量是「还没配置好鸡蛋铺同步账号，请联系管理员」
        （`jidanpu_common.NOT_CONFIGURED_MESSAGE`，含「鸡蛋铺」）⇒ 对外口必须换成
        **带本租户工作室名**的句子；code 仍是 1015（⛔ 不动码值）。

        造数形态：`claim_upstream_mirror` 的「事件排不进」支（`queue_hall_claim` 回
        `None` ⇒ `set_rollback` + 1015）。**该支在请求路径上今天是防御性死角**——进到
        那里之前，`owner_binding` 为 None / 停用已被前一道闸按 1012 拆雷，同一行实例上
        缓存的 FK 不会再变 None ⇒ 只能把 `queue_hall_claim` 打桩成 `None` 确定性落进
        那支（`mock.patch` 打在**被查找的模块**上，E-0029）。桩接没接上的对照：没接上
        ⇒ 事件真排出去 ⇒ 走真上游（`.test.invalid`）响亮失败，本用例的 1015 断言当场红。
        """
        from apps.business.models import JidanpuUpstreamHallOrder
        from apps.platform.models import Tenant

        # 工作室名写进行（中间件每个请求都从库里现取租户 ⇒ 只改内存对象不够）。
        Tenant.objects.filter(pk=self.tenant.pk).update(name="打手厅测试工作室")
        self.tenant.name = "打手厅测试工作室"
        token = self.issue_token(self.app_a)
        with mock.patch.object(
            jidanpu_upstream, "queue_hall_claim", return_value=None
        ):
            response = self._claim(
                token, self.mirror_his.pk, key="mirror-claim-notcfg-0001"
            )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = self.body(response)
        self.assertEqual(body["code"], 1015, body)
        self.assertEqual(
            body["msg"], "还没配置好打手厅测试工作室的同步账号，请联系管理员", body
        )
        self.assertNotIn("鸡蛋铺", body["msg"])
        self.assertNotIn("上家", body["msg"])
        # 领域侧终态：那支走 `set_rollback` ⇒ 行回 `listed`、一个字没写。
        self.mirror_his.refresh_from_db()
        self.assertEqual(
            self.mirror_his.status, JidanpuUpstreamHallOrder.Status.LISTED
        )

    def test_the_route_requires_a_taker_token(self):
        """会话 / 匿名打这条对外端点 ⇒ 401（对外面只认 Bearer 令牌）。"""
        from tests.p5_auth_helpers import make_logged_in_client

        session = make_logged_in_client(
            domain=self.domain_name, username="mirror-session-user", games=None
        )
        self.assertEqual(session.post(MIRROR_CLAIM_PATH.format(pk=self.mirror_his.pk)).status_code, 401)
        anon = self.anonymous.post(
            MIRROR_CLAIM_PATH.format(pk=self.mirror_his.pk), REMOTE_ADDR=SOURCE_IP
        )
        self.assertEqual(anon.status_code, 401)


class HallClaimOldEndpointMirrorFallbackTests(HallMirrorFixture):
    """老接单端点的**镜像兜底**（2026-10-24 业务方裁定：第三方只适配一个端点）。

    `POST /api/taker/v1/hall/<order_no>/claim/` 的 `order_no` 就是拉单载荷里的 `order_no`，
    而镜像行那个键是**展示号** ⇒ 原生查不到时按展示号兜底到镜像接单（与
    `HallMirrorClaimView` 共用 `mirror_claim_response` 这一份主体）。判据六条：
    在挂镜像接得走（成功三键 + 行转正 + 真建单）、非在挂回 `1012`、不在游戏范围回
    `1002`（老端点的「取不到」同形出口，不泄露存在性）、**原生优先**（同号时原生赢、
    镜像不动）、未知单号仍是 `1002`（兜底不改变旧行为的对照组）。
    """

    OLD_CLAIM_PATH = "/api/taker/v1/hall/{order_no}/claim/"

    def _claim(self, token, order_no, *, key):
        return self.anonymous.post(
            self.OLD_CLAIM_PATH.format(order_no=order_no),
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_upstream_shadow_source_no_falls_through_to_mirror_resolution(self):
        """上家隐藏影子不截住单号；鸡蛋铺镜像仍由既有回退主体接住。"""
        from apps.business import upstream_hall_shadow
        from apps.core.envelope import api_response

        shadow = upstream_hall_shadow.ensure_for_card(self.mirror_his)
        self.assertIsNotNone(shadow)
        self.assertEqual(shadow.upstream_hall_card_id, self.mirror_his.pk)
        token = self.issue_token(self.app_a)
        response_stub = api_response(data=None, msg="合成拒绝", code=1012)
        with (
            mock.patch(
                "apps.takerapi.api.order_write_views.mirror_claim_response",
                return_value=response_stub,
            ) as mirror_claim,
            mock.patch(
                "apps.takerapi.api.order_write_views.peer_claim_response"
            ) as peer_claim,
        ):
            response = self._claim(
                token, self.mirror_his.upstream_order_id, key="old-jdp-shadow-fallback"
            )
        self.assertEqual(self.body(response)["code"], 1012)
        mirror_claim.assert_called_once_with(mock.ANY, mirror_pk=self.mirror_his.pk)
        peer_claim.assert_not_called()

    def test_claiming_a_listed_mirror_by_its_view_no_promotes_it(self):
        """老端点传**在挂镜像的展示号** ⇒ `code 0` + 成功三键；行转正、真建单。"""
        from apps.business.models import JidanpuUpstreamHallOrder, Order

        stub = _MirrorStubClient()
        token = self.issue_token(self.app_a)
        with mock.patch.object(jidanpu_upstream.jidanpu, "claim_session",
                               lambda _b: _MirrorCtx(stub)):
            response = self._claim(token, "V-20261024000000000901", key="old-mirror-0001")
        body = self.body(response)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(set(body["data"]), {"order_no", "status", "status_text"})
        # 转正建出的是**我们自己的新单**：20 位新单号 ≠ 展示号；价是勾兑价。
        self.assertNotEqual(body["data"]["order_no"], "V-20261024000000000901")
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.hall_price, Decimal("88.00"))
        self.mirror_his.refresh_from_db()
        self.assertEqual(self.mirror_his.status, JidanpuUpstreamHallOrder.Status.PROMOTED)

    def test_claiming_a_non_listed_mirror_by_view_no_answers_1012(self):
        """老端点传**已转正**镜像的展示号 ⇒ `1012`（与 pk 端点同款失败出口）。"""
        from apps.business.models import JidanpuUpstreamHallOrder

        self.mirror_his.status = JidanpuUpstreamHallOrder.Status.PROMOTED
        self.mirror_his.save(update_fields=["status"])
        token = self.issue_token(self.app_a)
        response = self._claim(token, "V-20261024000000000901", key="old-mirror-0002")
        body = self.body(response)
        self.assertEqual(body["code"], 1012, body)
        self.assertIsNone(body["data"])

    def test_mirror_outside_the_takers_games_stays_1002(self):
        """乙游戏的镜像（甲无权限）按展示号打老端点 ⇒ `1002`。

        ⚠️ 与 pk 端点的 `1012` **有意不同**：老端点的反枚举口径是「取不到 = `1002`
        同形」（`NOT_IN_HALL_MSG` 承重的那条）——兜底找不到也落同一个出口，
        ⛔ 不引入第二句「看得出来过镜像这层」的文案。
        """
        token = self.issue_token(self.app_a)
        response = self._claim(token, "V-20261024000000000902", key="old-mirror-0003")
        body = self.body(response)
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "该订单不存在或不在抢单大厅")

    def test_a_disabled_bindings_mirror_is_not_resolved_by_view_no(self):
        """⭐ 停用绑定的 listed 卡按展示号打老端点 ⇒ `1002` 同形出口（2026-10-29 修法二堵洞）。

        修前：`mirror_pk_for_view_no` 不过归属绑定的 enabled 闸 ⇒ 解析得到 pk ⇒ 领域层报
        `1015`「还没配置好鸡蛋铺同步账号」（报障里按单号兜底那条**确定性活洞**）。修后：
        解析口与内部读口（E-0288 的 `owner_binding__enabled=True`）同一道判据 ⇒ 解析不到
        ⇒ 落老端点「取不到 = `1002` 同形」的既有出口，与「不在游戏范围 / 未知单号」逐字
        同形（不泄露存在性）。⚠️ 任务书原期望「1012 族 + 行转 gone」；实测该端点「找不到
        镜像」的既有口径是 `1002`（上方两条既有用例钉着），按「先读现回什么、保持一致」
        以 `1002` 落——拆雷（行转 gone）发生在 **pk 端点**那条路（本文件
        `HallMirrorClaimTests` 与 `test_jidanpu_upstream_hall` 的新用例钉着），解析函数
        ⛔ 不写状态。
        """
        from apps.business.models import (
            JidanpuUpstreamBinding,
            JidanpuUpstreamHallOrder,
        )

        JidanpuUpstreamBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        # 夹具哨兵：行本身仍是 listed（停用绑定只动了绑定，没动行）。
        self.assertEqual(
            self.mirror_his.status, JidanpuUpstreamHallOrder.Status.LISTED
        )
        token = self.issue_token(self.app_a)
        response = self._claim(token, "V-20261024000000000901", key="old-mirror-stale-01")
        body = self.body(response)
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "该订单不存在或不在抢单大厅", body)
        self.assertIsNone(body["data"], body)
        # 对照自证：行没被动过（洞堵在解析口，不在写侧）。
        self.mirror_his.refresh_from_db()
        self.assertEqual(self.mirror_his.status, JidanpuUpstreamHallOrder.Status.LISTED)

    def test_unknown_order_no_stays_1002(self):
        """未知单号 ⇒ 仍是 `1002`（兜底不改变旧行为的对照组）。"""
        token = self.issue_token(self.app_a)
        response = self._claim(token, "20991231000000000099", key="old-mirror-0004")
        body = self.body(response)
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "该订单不存在或不在抢单大厅")

    def test_native_order_wins_when_numbers_collide(self):
        """同号冲突 ⇒ **原生优先**：接的是原生那张（转「代练中」）、镜像行原地不动。"""
        from apps.business.models import JidanpuUpstreamHallOrder
        from django.contrib.auth.models import Group
        from tests.p5_auth_helpers import grant_nodes

        # 原生接单的状态机边要 `grab_hall.take_order`：先进「代练员」组再授节点
        # （`grant_nodes` 挂在组对应的 Role 上；`test_takerapi_claim_api` 的同款成对写法）。
        grp, _ = Group.objects.get_or_create(name="代练员")
        self.taker_a.groups.add(grp)
        grant_nodes(self.taker_a, "grab_hall.take_order")
        shared = "NV000000000000000001"  # 恰 20 位：既是合法原生单号，也当展示号
        native = self._order(
            self.game_his, shared, project_text="原生优先对照单", price="66.00"
        )
        mirror = self._mirror(
            "20261024000000000905", game=self.game_his,
            first_seen_at=self.minutes_ago(2), view_no=shared,
        )
        token = self.issue_token(self.app_a)
        response = self._claim(token, shared, key="old-mirror-0005")
        body = self.body(response)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(body["data"]["order_no"], shared)
        native.refresh_from_db()
        self.assertEqual(native.status, OrderStatus.IN_PROGRESS)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)

    def test_mirror_view_no_fallback_is_idempotent_on_replay(self):
        """同一幂等键重放 ⇒ 拿到**同一次**的结果（镜像只转正一次、只建一张单）。"""
        from apps.business.models import JidanpuUpstreamHallOrder, Order

        stub = _MirrorStubClient()
        token = self.issue_token(self.app_a)
        with mock.patch.object(jidanpu_upstream.jidanpu, "claim_session",
                               lambda _b: _MirrorCtx(stub)):
            first = self._claim(token, "V-20261024000000000903", key="old-mirror-replay")
            second = self._claim(token, "V-20261024000000000903", key="old-mirror-replay")
        body_first, body_second = self.body(first), self.body(second)
        self.assertEqual(body_first["code"], OK, body_first)
        self.assertEqual(body_second["data"], body_first["data"])
        self.mirror_fresh.refresh_from_db()
        self.assertEqual(
            self.mirror_fresh.status, JidanpuUpstreamHallOrder.Status.PROMOTED
        )
        # 同一张展示号只落一张真单。
        self.assertEqual(
            Order.objects.filter(project_text="打手侧镜像合成项目").count(), 1
        )


class HallClaimPeerFallbackTests(_HallFixture):
    """旧单号接单端点对团团通镜像的回退解析。"""

    CLAIM_PATH = "/api/taker/v1/hall/{order_no}/claim/"

    def setUp(self):
        super().setUp()
        from apps.business.peer_sync import hash_token, new_token

        self.peer_binding = PeerUpstreamBinding.objects.create(
            peer_name="合成测试上家",
            peer_base_url="https://peer-fallback.test.invalid",
            peer_account="syn-peer-fallback",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://taker-fallback.test.invalid",
            bound_by=self.taker_a,
            hall_sync_enabled=True,
        )

    def _peer_row(self, order_no, *, binding=None, game=None, status="listed"):
        from django.utils import timezone

        return PeerHallMirror.objects.create(
            peer_upstream_binding=binding or self.peer_binding,
            peer_base_url=(binding or self.peer_binding).peer_base_url,
            peer_order_no=order_no,
            status=status,
            game=game or self.game_his,
            payload={"order_no": order_no, "hall_price": "50.00"},
            remote_hall_price=Decimal("50.00"),
            first_seen_at=timezone.now(),
        )

    def _claim(self, token, order_no, key):
        return self.anonymous.post(
            self.CLAIM_PATH.format(order_no=order_no),
            HTTP_AUTHORIZATION=f"Bearer {token}",
            REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_old_order_no_path_reuses_peer_claim_body_even_for_nonlisted_rows(self):
        """唯一可见 peer 单号走旧路径；不筛状态，状态终裁仍由 peer 接单主体完成。"""
        row = self._peer_row("PEER-FALLBACK-0001", status=PeerHallMirror.Status.GONE)
        token = self.issue_token(self.app_a)
        from apps.core.envelope import api_response

        refusal = api_response(data=None, msg="测试拒绝", code=1012)
        with mock.patch(
            "apps.takerapi.api.order_write_views.peer_claim_response",
            return_value=refusal,
        ) as claim_body:
            response = self._claim(token, row.peer_order_no, "peer-fallback-old-0001")
        body = self.body(response)
        self.assertEqual(body["code"], 1012, body)
        self.assertEqual(body["msg"], "测试拒绝", body)
        claim_body.assert_called_once()
        self.assertEqual(claim_body.call_args.kwargs, {"mirror_pk": row.pk})

    def test_peer_shadow_source_no_falls_through_to_peer_mirror(self):
        """真实 `hall_card` 隐藏影子没有 MAIN，单号请求仍会落到 peer 接手主体。"""
        from apps.business import hall_shadow
        from apps.core.envelope import api_response

        row = self._peer_row("PEER-FALLBACK-SHADOW-01")
        shadow = hall_shadow.ensure_for_card(row)
        self.assertIsNotNone(shadow)
        self.assertEqual(shadow.hall_card_id, row.pk)
        self.assertEqual(shadow.source_order_no, row.peer_order_no)
        self.assertFalse(
            Performance.objects.filter(order=shadow, emp_type=EmpType.MAIN).exists()
        )
        token = self.issue_token(self.app_a)
        refusal = api_response(data=None, msg="测试拒绝", code=1012)
        with mock.patch(
            "apps.takerapi.api.order_write_views.peer_claim_response",
            return_value=refusal,
        ) as peer_claim:
            response = self._claim(token, row.peer_order_no, "peer-shadow-fallback-0001")
        body = self.body(response)
        self.assertEqual(body["code"], 1012, body)
        peer_claim.assert_called_once_with(mock.ANY, mirror_pk=row.pk)

    def test_duplicate_peer_order_no_fails_closed(self):
        """不同上家的相同单号不能猜一张接。"""
        from apps.business.peer_sync import hash_token, new_token

        self._peer_row("PEER-FALLBACK-DUP-01")
        binding = PeerUpstreamBinding.objects.create(
            peer_name="另一合成上家",
            peer_base_url="https://peer-fallback-other.test.invalid",
            peer_account="syn-peer-fallback-other",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://taker-fallback.test.invalid",
            bound_by=self.taker_a,
            hall_sync_enabled=True,
        )
        self._peer_row("PEER-FALLBACK-DUP-01", binding=binding)
        token = self.issue_token(self.app_a)
        with mock.patch("apps.takerapi.api.order_write_views.peer_claim_response") as claim_body:
            response = self._claim(token, "PEER-FALLBACK-DUP-01", "peer-fallback-dup-0001")
        body = self.body(response)
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "该订单不存在或不在抢单大厅", body)
        claim_body.assert_not_called()

    def test_native_order_keeps_priority_over_a_peer_order_number_collision(self):
        """旧路径解析到原生单时，后加的 peer 回退不能抢先命中。"""
        row = self._peer_row(self.order_his.order_no)
        token = self.issue_token(self.app_a)
        with mock.patch(
            "apps.takerapi.api.order_write_views.peer_claim_response"
        ) as claim_body:
            response = self._claim(token, self.order_his.order_no, "peer-native-prio-0001")
        body = self.body(response)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(body["data"]["order_no"], self.order_his.order_no)
        claim_body.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.status, PeerHallMirror.Status.LISTED)

    def test_hidden_peer_rows_are_not_resolved(self):
        """游戏范围、停用绑定和关闭大厅同步的卡都不能被旧路径解析。"""
        from apps.business.peer_sync import hash_token, new_token
        from django.utils import timezone

        self._peer_row("PEER-FALLBACK-NOGAME", game=self.game_other)
        disabled = PeerUpstreamBinding.objects.create(
            peer_name="停用合成上家",
            peer_base_url="https://peer-fallback-disabled.test.invalid",
            peer_account="syn-peer-fallback-disabled",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://taker-fallback.test.invalid",
            bound_by=self.taker_a,
            enabled=False,
            hall_sync_enabled=True,
        )
        self._peer_row("PEER-FALLBACK-DISABLED", binding=disabled)
        off = PeerUpstreamBinding.objects.create(
            peer_name="关闭同步合成上家",
            peer_base_url="https://peer-fallback-off.test.invalid",
            peer_account="syn-peer-fallback-off",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://taker-fallback.test.invalid",
            bound_by=self.taker_a,
            hall_sync_enabled=False,
        )
        self._peer_row("PEER-FALLBACK-OFF", binding=off)
        unbound = PeerUpstreamBinding.objects.create(
            peer_name="解绑合成上家",
            peer_base_url="https://peer-fallback-unbound.test.invalid",
            peer_account="syn-peer-fallback-unbound",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://taker-fallback.test.invalid",
            bound_by=self.taker_a,
            hall_sync_enabled=True,
            deleted_at=timezone.now(),
        )
        self._peer_row("PEER-FALLBACK-UNBOUND", binding=unbound)
        token = self.issue_token(self.app_a)
        for index, order_no in enumerate((
            "PEER-FALLBACK-NOGAME",
            "PEER-FALLBACK-DISABLED",
            "PEER-FALLBACK-OFF",
            "PEER-FALLBACK-UNBOUND",
        ), start=1):
            with self.subTest(order_no=order_no):
                with mock.patch("apps.takerapi.api.order_write_views.peer_claim_response") as claim_body:
                    response = self._claim(token, order_no, f"peer-fallback-hidden-{index}")
                body = self.body(response)
                self.assertEqual(body["code"], 1002, body)
                claim_body.assert_not_called()


class HallClaimBySourceNoTests(_HallFixture):
    """⭐ D-0338（2026-10-30）：原生行的 `order_no` 键 = **来源号优先展示号** ⇒ 第三方
    「按原样拼」回来的抢单地址可能是**来源号** ⇒ 老接单端点必须认得它。

    ## 判据顺序（承重）

    ① **内置号精确命中优先**（既有行为一字不变——自录单 / 老第三方缓存的内置号照常接）；
    ② 内置号查不到 ⇒ 按**来源号**找，且**恰好一张**才认（来源号列**不唯一**：
    两张在厅单同号 = 脏数据 ⇒ fail-closed 落 `1002` 同形出口，⛔ 不挑一张、
    ⛔ 不把「撞号」泄露成另一种答复）；
    ③ 游戏范围收窄与内置号那一路**同一道** `scope_queryset`（越范围 = 取不到 = `1002`）；
    ④ 镜像兜底在最后，次序不变（同号时原生赢的既有钉在上一类）。
    """

    CLAIM_PATH = "/api/taker/v1/hall/{order_no}/claim/"

    def setUp(self):
        super().setUp()
        # 原生接单的状态机边要 `grab_hall.take_order`：先进「代练员」组再授节点
        # （照 `test_native_order_wins_when_numbers_collide` 的同款成对写法）。
        from django.contrib.auth.models import Group

        from tests.p5_auth_helpers import grant_nodes

        grp, _ = Group.objects.get_or_create(name="代练员")
        self.taker_a.groups.add(grp)
        grant_nodes(self.taker_a, "grab_hall.take_order")

    def _claim(self, token, order_no, *, key):
        return self.anonymous.post(
            self.CLAIM_PATH.format(order_no=order_no),
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def _sourced_order(self, order_no, source, *, game=None):
        order = self._order(game or self.game_his, order_no, project_text="来源号抢单对照")
        order.source_order_no = source
        order.save(update_fields=["source_order_no"])
        return order

    def test_claiming_by_source_order_no_claims_the_native_order(self):
        """⭐ 有来源号的单，按**来源号**拼抢单 URL ⇒ `code 0`、接的就是这张原生单。"""
        order = self._sourced_order("20261030000000000011", "DD-20261030-0001")
        token = self.issue_token(self.app_a)
        response = self._claim(token, "DD-20261030-0001", key="src-claim-0001")
        body = self.body(response)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body["code"], OK, body)
        # 成功三键的 `order_no` 是**转正后的内置号**（§4.2 既有契约，一字不变）。
        self.assertEqual(body["data"]["order_no"], order.order_no)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.IN_PROGRESS)

    def test_claiming_by_internal_order_no_still_works(self):
        """精确优先半边：同一张单拿**内置号**打老地址 ⇒ 照常接（既有行为一字不变）。"""
        order = self._sourced_order("20261030000000000012", "DD-20261030-0002")
        token = self.issue_token(self.app_a)
        response = self._claim(token, order.order_no, key="src-claim-0002")
        body = self.body(response)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(body["data"]["order_no"], order.order_no)

    def test_a_duplicate_source_number_is_fail_closed_1002(self):
        """⭐ 来源号列**不唯一**：两张在厅单同号 ⇒ `1002` 同形出口（fail-closed，⛔ 不挑一张）。"""
        self._sourced_order("20261030000000000021", "DUP-SRC-0001")
        self._sourced_order("20261030000000000022", "DUP-SRC-0001")
        token = self.issue_token(self.app_a)
        response = self._claim(token, "DUP-SRC-0001", key="src-claim-0003")
        body = self.body(response)
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "该订单不存在或不在抢单大厅", body)
        # 两张都原封不动（⛔ 不许"接走其中一张"）。
        self.assertEqual(
            Order.objects.filter(
                source_order_no="DUP-SRC-0001", status=OrderStatus.WAITING_GRAB
            ).count(),
            2,
        )

    def test_a_source_number_out_of_game_scope_stays_1002(self):
        """范围收窄与内置号那一路同一道：乙游戏的单按来源号打 ⇒ `1002` 同形（不泄露存在性）。"""
        self._sourced_order("20261030000000000031", "DD-20261030-0003", game=self.game_other)
        token = self.issue_token(self.app_a)
        response = self._claim(token, "DD-20261030-0003", key="src-claim-0004")
        body = self.body(response)
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "该订单不存在或不在抢单大厅", body)


class HallNodeGateTests(HallMirrorFixture):
    """⭐ Z11：没有 `grab_hall.take_order` ⇒ 读口空 / 接单 1002。

    改准来由：本模块此前只装 Bearer 认证、**不判节点** ⇒ 一个只持 App 凭据、账号却没勾
    「允许自助接单」的租户照样能拉单并接走（转正建真单 + 写 MAIN 绩效）。
    ⚠️ 正向对照与失败两侧都要有：`app_a`（持节点）**看得到也接得走**，无节点账号
    **连读都是空列表**——缺了正对照，一个「恒空的大厅」也能让本类绿（`E-0004` 形态）。
    """

    NONODE_KEY = "hallnonodekey00001"
    NONODE_SECRET = "hall-nonode-secret-00001"

    def setUp(self):
        super().setUp()
        self.nobody = self._make_taker(
            "hall-taker-nonode", games=[self.game_his], nodes=()
        )
        self.app_nonode = self._make_app(
            "大厅无节点应用", self.NONODE_KEY, self.NONODE_SECRET, self.nobody
        )

    def _claim(self, token, pk, *, key):
        return self.anonymous.post(
            MIRROR_CLAIM_PATH.format(pk=pk),
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_the_hall_is_empty_without_the_node(self):
        """读口：无节点 ⇒ `count=0` / `results=[]`（与「本租户没有单」同形）。

        正对照：持节点那个打手的大厅**非空** ⇒ 这些单真的在厅，空列表不是「本来就没单」。
        """
        self.assertIn(
            "V-20261024000000000901",
            self.nos_of(self.rows(self.issue_token(self.app_a))),
        )
        response = self.hall(self.issue_token(self.app_nonode))
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = self.body(response)
        self.assertEqual(body["code"], OK, body)
        self.assertEqual(body["data"]["results"], [], body)
        self.assertEqual(body["data"]["count"], 0, body)

    def test_the_mirror_claim_is_1002_without_the_node(self):
        """写侧（鸡蛋铺镜像 pk 端点）：`1002` ＋ 与「不存在」同形文案，行零改动。"""
        from apps.business.models import JidanpuUpstreamHallOrder

        token = self.issue_token(self.app_nonode)
        response = self._claim(token, self.mirror_his.pk, key="nonode-mirror-0001")
        body = self.body(response)
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "该订单不存在或不在抢单大厅", body)
        self.assertIsNone(body["data"], body)
        self.mirror_his.refresh_from_db()
        self.assertEqual(
            self.mirror_his.status, JidanpuUpstreamHallOrder.Status.LISTED
        )

    def test_the_old_endpoint_mirror_fallback_is_1002_without_the_node(self):
        """写侧（老端点按展示号兜底）：同一条判据也在，⛔ 不能绕开 pk 端点。"""
        from apps.business.models import JidanpuUpstreamHallOrder

        token = self.issue_token(self.app_nonode)
        response = self.anonymous.post(
            "/api/taker/v1/hall/V-20261024000000000901/claim/",
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR=SOURCE_IP,
            HTTP_IDEMPOTENCY_KEY="nonode-old-mirror-0001",
        )
        body = self.body(response)
        self.assertEqual(body["code"], 1002, body)
        self.assertEqual(body["msg"], "该订单不存在或不在抢单大厅", body)
        self.mirror_his.refresh_from_db()
        self.assertEqual(
            self.mirror_his.status, JidanpuUpstreamHallOrder.Status.LISTED
        )


class _MirrorStubClient:
    """SDK 边界替身（写端点成功码必须 > 0；照 test_jidanpu_upstream_hall 的同款）。

    ⭐ `segment_rows`：段查复核（`GetInternalOrderGrabbingList`）的段真值表——给了才应答
    该端点（缺省保持旧形状：段查=未预期端点，供「不该有复核查询」的用例自证）。"""

    def __init__(self, *, segment_rows=None):
        self.segment_rows = [dict(row) for row in (segment_rows or [])]
        self.calls: list[tuple[str, str, dict]] = []
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        self.calls.append((method, path, dict(kwargs.get("query") or {})))
        if path == "/OrderAjax/GetInternalOrderGrabbingList":
            page = int(kwargs["query"]["page"])
            page_rows = self.segment_rows[(page - 1) * 2:page * 2]
            self.last_envelope = {
                "code": 0, "msg": "success", "data": page_rows,
                "count": len(self.segment_rows),
            }
            return [dict(row) for row in page_rows]
        if path == "/OrderAjax/RecOrderByInternal":
            self.last_envelope = {"code": 1, "msg": "接单成功", "data": ""}
            return ""
        raise AssertionError(f"未预期的端点：{method} {path}")

    @property
    def paths(self) -> list[str]:
        return [path for _method, path, _kwargs in self.calls]


class _MirrorCtx:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        return self.client

    def __exit__(self, *_exc):
        return False
