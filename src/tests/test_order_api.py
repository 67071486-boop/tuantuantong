from decimal import Decimal
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.utils import timezone

from django_tenants.test.cases import TenantTestCase

from django.contrib.auth import get_user_model

from apps.accounts.models import StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import (
    MONEY_FIELDS,
    AuditLog,
    EmpType,
    Game,
    Order,
    OrderRefund,
    OrderSource,
    OrderStatus,
    Performance,
    Project,
    ProjectCategory,
    SystemSwitch,
)
from tests.p5_auth_helpers import P2_TEST_USERNAME, grant_games, make_logged_in_client


#: 合成的「来源单号」（对标提示里的「淘宝单号」；⛔ 不是真实值，`E-0077`）。
SOURCE_ORDER_NO = "TB-合成-来源单号-0001"


class OrderApiTests(TenantTestCase):
    def setUp(self):
        # 必须用被 ALLOWED_HOSTS 放行的租户域名。订单端点没有声明 `permission_classes` → 受 fail-closed 默认权限类（`IsAuthenticated`，spec D11）保护，**先真实登录**（`tests/p5_auth_helpers.py`）。
        # ⚠️ `/transition/` **校验身份**（`nodes_of(request.user)`）：不传 `nodes` 的登录用户节点集合为空集 → 本类里 9 条真的迁状态的测试全部被判「非法迁移」。下面用 `nodes=` 显式声明这几条边要的节点门（抛单 / 撤回 / 退回未分配都含 `order.change_status`，见 `order_status.TRANSITIONS`）。
        # ⚠️ 订单端点逐 action 要节点 code（`OrderViewSet.ORDER_PERMISSIONS`）；`groups` 只建 Group、**不放行**，所以这里把本文件实际走到的门**显式声明**出来（顺带自文档：本文件覆盖了哪些端点）。漏声明会以 403 的形式红——这是刻意的 fail-closed，不是夹具坏了。
        self.client = make_logged_in_client(
            groups=("客服",),
            # ⚠️ 显式 opt-out（夹具默认翻面）：本类下面自己
            # `StaffProfile.objects.create(...).games.set([self.game])` ⇒ 夹具这一份必须
            # **不建**（`games=None`），否则两边都建同一 user 的档案会撞唯一约束。
            games=None,
            nodes=(
                "order.add_edit",             # 录单 / `/parse/`
                "order.list",                 # 检索列表
                "order.view_detail",          # 详情 / `/audit/`（留痕是详情的一部分）
                "order.change_status",        # `/transition/`
                # ⭐ 计划 ④：`/transition/` 的 **待抢单** 目标已改判独占 36
                #    ⇒ 本类里有 9 处迁 `/transition/` 到待抢单的用例（F5 发单人那几条等），
                #    夹具必须显式声明这条门（**声明**，不是放宽：缺了它拿到的是 403）。
                "order.send_to_grab_hall",
                "order.submit_payout_price",  # `/price/`（两个 code 任一）
                "order.edit_payout_price",
                "order.assign_taker",         # `/assign/`
            ),
        )
        self.base = "/api/business/orders"
        self.game = Game.objects.create(name="原神")
        self.cat = ProjectCategory.objects.create(game=self.game, name="深境螺旋")
        self.project = Project.objects.create(category=self.cat, name="满星")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。`get_queryset` 现在过 `game_scope("game_id")`、写侧过 `assert_game_allowed` ⇒ 登录账号必须持有本文件的游戏，否则录单/详情/改价一起 1000/1002。
        #    本文件各用例的**期望值一个字没改**。
        StaffProfile.objects.create(
            user=get_user_model().objects.get(username=P2_TEST_USERNAME)
        ).games.set([self.game])

    def _create(self, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "满星",
            "total_price": "199.50",
            "customer_phone": "13800138000",
        }
        payload.update(overrides)
        return self.client.post(self.base + "/", payload, content_type="application/json")

    def test_create_order_generates_order_no_and_default_status(self):
        body = self._create().json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(len(body["data"]["order_no"]), 20)
        self.assertEqual(body["data"]["status"], OrderStatus.UNASSIGNED)

    def _set_recorder_display_name(self, first_name: str):
        """给登录夹具用户设昵称（`first_name`），用来把「显示名」与「登录账号」区分开。"""
        user = get_user_model().objects.get(username=P2_TEST_USERNAME)
        user.first_name = first_name
        user.save(update_fields=["first_name"])
        return user

    def test_create_fills_sales_agent_with_recorder_display_name(self):
        """录单没带 `sales_agent` ⇒ 兜底成**录单人的显示名**（业务方裁定）。症状：订单列表「销售客服」列恒为空（该列渲染载荷的 `sales_agent`）。根因：对标那个控件是**运行期**注入的（`addorder.js` L1154-1166 往空的 `.cf` 里灌 radio），本仓录单页没实现它 ⇒ 请求体里根本没有这个键，而创建路径也没写过它。
        ⚠️ 期望值写**字面量**、不调 `user_display_name` 现算：现算会让「口径被改回 `username`」时**恒绿**（E-0024 族）。"""
        self._set_recorder_display_name("测试客服甲")
        body = self._create().json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.sales_agent, "测试客服甲")

    def test_create_fills_blank_sales_agent_with_recorder(self):
        """`sales_agent` 显式传**空串** ⇒ 与「没传」同档，仍兜底成录单人（空串 = 该列「没有值」）。
        录单不接受客户端自填的名字（见下一条），但空串不算「自填了一个名字」：服务端写进去的仍是归属人（缺 `sales_user` ⇒ 录单人）的显示名，不会造出「名字与归属人分叉」。名字闸在 `create_order`。"""
        self._set_recorder_display_name("测试客服甲")
        body = self._create(sales_agent="").json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.sales_agent, "测试客服甲")

    def test_create_rejects_a_client_supplied_sales_agent(self):
        """客户端显式给一个**别的**名字 ⇒ **业务码 1000、一行都不写**。新口径：`Order.sales_agent` 是 `Order.sales_user`（提成归属）的**派生快照** ⇒ 只有服务端按将要写入的归属人算出来的名字才算数；请求体里带一个别的名字 = 试图制造「名字是甲、提成归乙」⇒ **明确拒绝**（⛔ 不是静默忽略：静默忽略会让调用方以为名字被采纳了，`AGENTS.md` §5-E 的静默丢字段一族）。
        ⚠️ 这条**不是放宽判据**：仓库侧没有任何合法请求依赖「客户端自填名字」——录单页把这一格交给服务端（`order-addorder.ts` 的 `intakeSalesUserId` 那一支）。要提交**指定归属人**，用 `sales_user`（需换销售客服权限），名字由服务端从那个人算出。"""
        self._set_recorder_display_name("测试客服甲")
        before = Order.objects.count()
        body = self._create(sales_agent="手填的客服乙").json()
        self.assertEqual(body["code"], 1000)
        self.assertIn("归属人", body["msg"])
        self.assertEqual(Order.objects.count(), before)      # 被拒时一行都不写

    def test_create_accepts_a_sales_agent_that_matches_the_recorder(self):
        """阳性对照：请求体带的名字**与**服务端将要写入的那个（录单人显示名）**逐字相同** ⇒ 成功。
        判别力：把上面那道闸写成「只要带了 `sales_agent` 就拒」，本条立刻红（而前端整表单提交这类形状会因此被误拒）。"""
        self._set_recorder_display_name("测试客服甲")
        body = self._create(sales_agent="测试客服甲").json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.sales_agent, "测试客服甲")

    def test_overlong_recorder_display_name_is_clipped_not_500(self):
        """录单人显示名长于列宽 ⇒ **截断落库**，不是 500（`sales_agent` 是 `varchar(64)`）。
        ⚠️ 期望值写死 **64**：从 `Order._meta...max_length` 现算会让「截断被删掉」时**恒绿**（同款注释的先例：`tests/test_jidanpu_upstream_record.py`）。"""
        self._set_recorder_display_name("客" * 70)
        body = self._create().json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(len(order.sales_agent), 64)
        self.assertEqual(order.sales_agent, "客" * 64)

    def test_create_requires_only_game_and_project(self):
        """spec §5.2：价格与联系方式都不是必填；`total_price` 省略 → 落列默认 0。
        ⚠️ 这是**接口层**口径（录单**页面**另有「订单总价必填、默认 0」，见 `docs/DECISIONS.md` D-0082）。
        ⚠️ 不能走 `self._create(customer_phone="")`：`_create` 的默认 payload 里带着 `"total_price": "199.50"`（实测踩到，见 E-0047）。"""
        payload = {"game": self.game.id, "project_text": "满星", "customer_phone": ""}
        body = self.client.post(
            self.base + "/", payload, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(
            Order.objects.get(order_no=body["data"]["order_no"]).total_price,
            Decimal("0.00"),
        )

    def test_explicit_null_total_price_is_rejected(self):
        """非空字段上的显式 `null` 是调用方 bug → `1000`，不静默当 0（spec §12.2 拟定值，已实测）。"""
        body = self._create(total_price=None).json()
        self.assertEqual(body["code"], 1000)

    def test_missing_project_is_rejected(self):
        body = self._create(project_text="").json()
        self.assertEqual(body["code"], 1000)

    def test_project_text_length_limit_is_900(self):
        """`project_text` 上限 = **900**（业务方裁定；D-0218）。历史：本仓列宽原为 128，而录单页的 `#project` 是从对标证据件渲染的（原写 `maxlength="220"`）⇒ 用户能打 129~220 个字、提交却必被拒（实测 129 字回「请确保这个字段不能超过 128 个字符。」，业务码 1000）；D-0140 放宽到 300，D-0218 按业务方「提高 3 倍量」再放宽到 900。
        ⚠️ 900 是**字面量判据**：⛔ 不要从 `Order._meta` / 模型常量 import（自指断言在被断言的常量被改坏时不会红，见 AGENTS.md §5-G 与 E-0024）。"""
        ok = self._create(project_text="测" * 900).json()
        self.assertEqual(ok["code"], 0)
        self.assertEqual(len(ok["data"]["project_text"]), 900)

        too_long = self._create(project_text="测" * 901).json()
        self.assertEqual(too_long["code"], 1000)

    def test_server_from_another_game_is_rejected(self):
        """同链校验必须在 API 层生效，而不只是纯函数里正确。"""
        other = Game.objects.create(name="星穹铁道")
        from apps.business.models import GameServer

        foreign = GameServer.objects.create(game=other, name="国服")
        body = self._create(server=foreign.id).json()
        self.assertEqual(body["code"], 1000)

    def test_retrieve_by_order_no(self):
        created = self._create().json()["data"]
        body = self.client.get(f"{self.base}/{created['order_no']}/").json()
        self.assertEqual(body["data"]["order_no"], created["order_no"])

    def test_search_filters_by_status_and_project_text(self):
        self._create(project_text="满星")
        self._create(project_text="深渊")
        body = self.client.get(f"{self.base}/?project_text=深渊").json()
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["items"][0]["project_text"], "深渊")

    def test_search_by_keyword_matches_customer_phone(self):
        self._create(customer_phone="13800138000")
        self._create(customer_phone="13900139000")
        body = self.client.get(f"{self.base}/?keyword=1380013").json()
        self.assertEqual(body["data"]["total"], 1)

    def test_search_by_keyword_matches_source_order_no(self):
        """⭐ **业务方报过的缺陷**：管理订单页的「搜索1」**输来源单号搜不到**。界面对用户的提示**早就承诺**能搜（证据件 `public/replica/order-orderlist.html` 的 `#key1`：`title="可以输入系统单号、淘宝单号、角色名、电话"`），而后端的关键词字段表 `apps.business.selectors.KEYWORD_FIELDS` 里**没有** `source_order_no` ⇒ 填来源单号**一条都搜不出来**，而且是**静默**的（不报错、返回空集）。
        判据三件套：① 来源单号能搜到**那一条**；② 命中的是**来源单号**、不是碰巧撞上系统订单号（关键词是**系统单号里不可能出现的**串）；
        ③ 阴性对照：另一张单的来源单号不匹配 ⇒ 搜不到（否则「筛了」与「全返回」不可区分）。"""
        hit = self._create(source_order_no=SOURCE_ORDER_NO).json()["data"]
        self._create(source_order_no="TB-合成-别家单号")
        body = self.client.get(f"{self.base}/?keyword={SOURCE_ORDER_NO}").json()
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["items"][0]["order_no"], hit["order_no"])


    def test_malformed_game_param_returns_validation_error(self):
        """`?game=abc` 原先直接进 `filter(game_id=...)` → `ValueError` → HTML 500。
        违反本模块自己文档化的契约（业务结果一律 HTTP 200 + code）。"""
        response = self.client.get(f"{self.base}/?game=abc")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_zero_game_param_is_rejected_not_silently_ignored(self):
        """`?game=0` 不是「没传」：`IntegerField(min_value=1)` 在**入口**就判它非法。
        ⚠️ 这条钉的是**出口的实测语义**：控制方在 Ruling 39 的追加里要求「`?game=0` ⇒ 空集（不是忽略筛选返回全部）」，但 `0` 在序列化器那层就被拒了、**到不了选择器**，所以出口是 `code=1000`。选择器层的语义（非 `None` 的值 = 携带该条件）见 `tests/test_selectors.py`。两处都留痕——免得下一个人以为这个出口会返回空集。"""
        self._create()
        response = self.client.get(f"{self.base}/?game=0")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_malformed_date_param_returns_validation_error(self):
        response = self.client.get(f"{self.base}/?date_from=garbage")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_unknown_status_param_returns_validation_error(self):
        """**行为变更**：`?status=BOGUS` 以前静默返回空集，现在回 1000。
        静默空集让客户端分不清「没有匹配」与「参数写错」，而同一个端点的其它参数（游戏/日期）却是 500——三种行为，客户端无从适配。"""
        response = self.client.get(f"{self.base}/?status=BOGUS")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)

    def test_search_filters_by_status(self):
        """既有那条 `..._filters_by_status_and_project_text` **从不发送 `status`**——
        检索的 status 分支一直没有端点级覆盖。"""
        target = self._create(project_text="待抢的单").json()["data"]["order_no"]
        self._create(project_text="未分配的单")
        self.client.post(
            f"{self.base}/{target}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        )
        body = self.client.get(f"{self.base}/?status={OrderStatus.WAITING_GRAB}").json()
        self.assertEqual(body["data"]["total"], 1)
        self.assertEqual(body["data"]["items"][0]["order_no"], target)

    def test_search_filters_by_status_multi_value(self):
        """⭐ 多选实装：`?status=码1,码2` 筛**并集**（`status__in`）。
        一杀三断：① 两个码的单**都**出现；② 没选的那档**不**出现（否则「筛了」与「全返回」不可区分）；③ 非法 token 混进串里 ⇒ `1000` 且 msg 点名它（⛔ 不静默丢掉那个条件）。"""
        grabbed = self._create(project_text="待抢的单").json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{grabbed}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        )
        unassigned = self._create(project_text="未分配的单").json()["data"]["order_no"]

        body = self.client.get(
            f"{self.base}/?status={OrderStatus.WAITING_GRAB},{OrderStatus.UNASSIGNED}"
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            sorted(i["order_no"] for i in body["data"]["items"]),
            sorted([grabbed, unassigned]),
        )

        # 非法 token ⇒ 1000（点名），不是「丢掉那截继续筛」
        bad = self.client.get(
            f"{self.base}/?status={OrderStatus.WAITING_GRAB},BOGUS"
        ).json()
        self.assertEqual(bad["code"], 1000, bad)
        self.assertIn("BOGUS", bad["msg"])

    def test_search_filters_by_urgent_and_tag(self):
        """`is_urgent` 与 `tag` 在 spec §6 的检索列表里，之前整条漏掉。
        `?is_urgent=false` 必须能筛出「不加急」——判据是 `is not None` 而不是真值。"""
        urgent = self._create(is_urgent=True, tag="加急单").json()["data"]["order_no"]
        normal = self._create(is_urgent=False, tag="普通单").json()["data"]["order_no"]

        body = self.client.get(f"{self.base}/?is_urgent=false").json()
        self.assertEqual([i["order_no"] for i in body["data"]["items"]], [normal])

        body = self.client.get(f"{self.base}/?tag=加急").json()
        self.assertEqual([i["order_no"] for i in body["data"]["items"]], [urgent])

    def test_overlong_actor_returns_validation_error(self):
        """超长 `actor` 打在**仍读请求体 actor** 的写路径上都必须是 200 + 1000，而不是 HTML 500。`Order.updated_by` 与 `AuditLog.actor` 都是 varchar，而视图原先直接 `request.data.get("actor", "")`——这与第 3 轮修的 `reason` 是**逐字节相同的 bug**，只是当时只修了一处。⚠️ **上限本身提到 150**（Ruling 8；= `User.username` 的 `max_length`）——本用例的 `"长" * 200` 在 150 下**仍是非法值**，所以判据不变、一个字没改；改的只是它描述的那条线上限（`apps/business/models.py` 与 `ActorSerializer` 都对齐 150）。
        ⚠️ `/transition/` **从本测试里移出**（收紧后它不再读请求体的 `actor`，改取认证态用户名，见 `test_transition_actor_...`），所以「超长 actor 必须返回 1000」对那条路径不再有判据意义：它现在**成功**。原来的 4 条路径因此变 3 条——这不是覆盖缩水，是把那条路径的覆盖换成了它真正的语义。
        `create` 不在「必须 1000」那张表里：录单不再读请求体的 `actor`，`created_by` 取认证态用户名（`create_order`）。这不是把这条向量放掉——表格之后把它做成反向用例：超长 body actor 不再是非法入参，录单必须成功，且落库的是认证态用户名（正向"body actor 被忽略 + 与 `restrict_to_self_created` 的联动"另由 `test_rbac_endpoint_enforcement.py::test_create_records_the_authenticated_username_even_if_the_body_says_otherwise` 钉住）。到此"必须 1000"的路径数从 3 变 **2**（`partial_update` / `/price/`，入参来源仍是请求体）。"""
        no = self._create().json()["data"]["order_no"]
        # 阳性对照：成功的录单确实会写一行 `order.create` ⇒ 下面「没有新增」不是「表不可达」
        self.assertEqual(
            OperationLog.objects.filter(order_no=no, action="order.create").count(), 1
        )
        long_actor = "长" * 200
        paths = [
            (
                "partial_update",
                lambda: self.client.patch(
                    f"{self.base}/{no}/",
                    {"customer_phone": "13900139000", "actor": long_actor},
                    content_type="application/json",
                ),
            ),
            (
                "price",
                lambda: self.client.post(
                    f"{self.base}/{no}/price/",
                    {"total_price": "88.00", "actor": long_actor},
                    content_type="application/json",
                ),
            ),
        ]
        for name, call in paths:
            with self.subTest(path=name):
                response = call()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], 1000)

        # 两条路径都必须**没有改动**订单
        order = Order.objects.get(order_no=no)
        self.assertEqual(order.customer_phone, "13800138000")
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertEqual(order.total_price, Decimal("199.50"))
        # 新表仍**只有那条录单行**：两条失败路径一行都没加（`create` 那条是阳性对照）。
        self.assertEqual(
            list(
                OperationLog.objects.filter(order_no=no).values_list("action", flat=True)
            ),
            ["order.create"],
        )
        # 旧表 `AuditLog` 对**订单域**恒空：订单域的写入者迁到 `record()` 之后，剩下的写入点也已迁走、`apps/business/audit.py` 整个删除 ⇒ 旧表现在**全局**零新增行。
        # 这条**升级**成「恒空」钉点：它同时守住「订单写路径没有偷偷回写旧表」。
        self.assertFalse(AuditLog.objects.filter(order_no=no).exists())
        # create 那条也没有偷偷建出第二张单
        self.assertEqual(Order.objects.count(), 1)

        # ── `create` 的反向用例（Ruling 7）────────────────
        # 超长 body actor 对录单不再有判据意义 ⇒ 它必须**成功**，且库里记的是**认证态用户名**。⚠️ 值写字面量（`P2_TEST_USERNAME` 常量是夹具导出的，断言"落库值 == 常量"在常量被改坏时不会红——见 E-0024）。
        created = self._create(actor=long_actor)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["code"], 0)
        self.assertEqual(
            Order.objects.get(order_no=created.json()["data"]["order_no"]).created_by,
            "p2-api-user",
        )

    def test_transition_actor_is_the_authenticated_user_not_the_body(self):
        """`/transition/` 的留痕 `actor` 取**认证态用户名**，请求体的被忽略（spec §13.2 的收紧）。
        为什么必须这样：请求体里的身份是**可伪造**的，而留痕的全部价值就在于「谁改的」为真；收紧之前 `transition` 与 `/price/` 一样读 `self._actor(request)`，等于谁都能把名字写成别人。
        ⚠️ 与 `/price/` 的差别是**刻意的**：改价仍走 `ActorSerializer` 入参校验（那条路的请求体值仍是它记录的来源），只有状态迁移换成认证态。别把两处「统一」掉。"""
        no = self._create().json()["data"]["order_no"]
        body = self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.WAITING_GRAB, "actor": "长" * 200},
            content_type="application/json",
        ).json()
        # 超长的请求体 actor 现在不参与写库：迁移照常成功，不再是一条「入参非法」
        self.assertEqual(body["code"], 0)
        # 阳性对照：迁移**确实**发生了（否则下面那条断言在「迁移被拒」时也会通过）
        self.assertEqual(Order.objects.get(order_no=no).status, OrderStatus.WAITING_GRAB)
        log = OperationLog.objects.get(action="order.transition", order_no=no)
        self.assertEqual(log.actor, P2_TEST_USERNAME)

    def test_create_accepts_negative_price(self):
        """**金额可以为负**——负值表示退款 / 冲抵 / 调整（业务方确认，见 D-0015）。
        原用例断言 `-5.00` 被拒，它绿的唯一原因就是 spec §5.3 那句**未经业务方确认**的「金额必须 ≥ 0」；那条规则已作废，这条测试随之反转（同一模式见 E-0006）。
        断言取**库里的行**：防假通过的机制是这里**重新 `Order.objects.get(...)`**——它直接查出持久化后的值，而 Django 赋值时不做量化，只看写接口返回的内存对象会得到假通过。"""
        body = self._create(total_price="-5.00").json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=body["data"]["order_no"])
        self.assertEqual(order.total_price, Decimal("-5.00"))

    def test_price_change_accepts_negative_amount_and_records_it(self):
        """负的改价（退款 / 冲抵）必须**生效并留痕**——两者缺一不可（D-0015）。
        原用例断言 `-1.00` 被拒且无留痕行，它绿的唯一原因同样是那句已作废的 ≥ 0 规则。反转后**留痕更要紧**：一条负调整若改了金额却没有 `AuditLog` 行，比原先「改价被拒」糟糕得多——账实不符且事后查不到（spec §6 硬规则 2）。"""
        no = self._create().json()["data"]["order_no"]
        response = self.client.post(
            f"{self.base}/{no}/price/",
            {"total_price": "-1.00", "reason": "客户退款"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

        order = Order.objects.get(order_no=no)
        self.assertEqual(order.total_price, Decimal("-1.00"))

        # 留痕必须是**数据库里的行**，且如实记下负值与改前金额
        log = OperationLog.objects.get(action="order.change_taker_price", order_no=no)
        self.assertEqual(log.from_value, "199.50")
        self.assertEqual(log.to_value, "-1.00")
        self.assertEqual(log.reason, "客户退款")

    def test_order_detail_deduplicates_reads_and_keeps_complete_audit(self):
        order_no = self._create(source_order_no=SOURCE_ORDER_NO).json()["data"]["order_no"]
        detail_url = f"{self.base}/{order_no}/"
        self.assertEqual(self.client.get(detail_url).json()["code"], 0)
        self.assertEqual(self.client.get(detail_url).json()["code"], 0)
        audit_url = f"{self.base}/{order_no}/audit/"
        full = self.client.get(audit_url).json()["data"]
        reads = [row for row in full if row["action"] == "order.view_credentials"]
        self.assertEqual(len(reads), 1)
        self.assertIn(f"查看订单详情：{SOURCE_ORDER_NO}", reads[0]["summary"])
        visible = self.client.get(audit_url, {"scope": "business"}).json()["data"]
        self.assertEqual([row["action"] for row in visible], ["order.create"])

    def test_detail_rewords_legacy_sync_without_changing_a_note_or_raw_history(self):
        order_no = self._create(source_order_no=SOURCE_ORDER_NO).json()["data"]["order_no"]
        old = OperationLog.objects.create(
            log_type=10, action="order.jidanpu_member_auto_claim",
            order_no=order_no, actor="下家鸡蛋铺同步", target_name="本地接单账号",
            reason="uid=42 显示名=外部打手", summary=f"旧接单记录 {order_no}，远端 uid=42",
        )
        note = OperationLog.objects.create(
            log_type=10, action="order.note", order_no=order_no, actor=P2_TEST_USERNAME,
            reason=f"客户说过 {order_no}", summary=f"添加订单日志：客户说过 {order_no}",
        )
        hidden = OperationLog.objects.create(
            log_type=40, action="peer.outbound_sync",
            order_no=order_no, actor="系统", summary="已同步给下家",
        )
        url = f"{self.base}/{order_no}/audit/"
        normal = {row["id"]: row for row in self.client.get(url, {"scope": "business"}).json()["data"]}
        complete = {row["id"]: row for row in self.client.get(url).json()["data"]}
        self.assertNotIn(hidden.pk, normal)
        self.assertIn(hidden.pk, complete)
        self.assertIn(SOURCE_ORDER_NO, normal[old.pk]["summary"])
        self.assertNotIn("uid=", normal[old.pk]["summary"])
        self.assertIn(f"客户说过 {order_no}", normal[note.pk]["summary"])
        old.refresh_from_db()
        self.assertIn("uid=42", old.summary)

    def test_audit_endpoint_returns_transition_and_price_history(self):
        """spec §6：详情含**状态流转历史**。没有这个出口，`AuditLog` 在 P2 里只写不读——
        而留痕的价值恰恰在于事后能查。"""
        no = self._create().json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.WAITING_GRAB, "actor": "客服小张"},
            content_type="application/json",
        )
        self.client.post(
            f"{self.base}/{no}/price/",
            {"total_price": "88.00", "reason": "客户砍价", "actor": "客服小李"},
            content_type="application/json",
        )

        response = self.client.get(f"{self.base}/{no}/audit/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)

        by_action = {row["action"]: row for row in body["data"]}
        # 按**行**断言：恰好这三条动作（录单 + 迁移 + 改价）、不多不少
        self.assertEqual(
            sorted(by_action),
            ["order.change_taker_price", "order.create", "order.transition"],
        )

        status_row = by_action["order.transition"]
        # spec §5.1：`order.transition` 的前后值是**中文标签**（与 §9.1 的迁移同源）
        self.assertEqual(status_row["from"], "未分配")
        self.assertEqual(status_row["to"], "待抢单")
        # R20：**所有**新表留痕的操作人都取**认证态用户名**；请求体里的
        # "客服小张" / "客服小李" 都是可伪造的，一律被忽略、不得出现。
        self.assertEqual(status_row["actor"], P2_TEST_USERNAME)

        price_row = by_action["order.change_taker_price"]
        self.assertEqual(price_row["from"], "199.50")
        self.assertEqual(price_row["to"], "88.00")
        self.assertEqual(price_row["reason"], "客户砍价")
        self.assertEqual(price_row["actor"], P2_TEST_USERNAME)
        self.assertNotIn("客服小李", price_row["actor"])

    def test_audit_created_at_is_rendered_in_the_project_timezone(self):
        """E-0238：订单详情日志的 `created_at` 是**项目时区**（`+08:00`）的墙钟。
        此前这行是裸 `.isoformat()`（UTC）⇒ 消费端要么按偏移自救（order-orderview 的 `formatLogTime`）、要么显示差 8 小时；现在与 `/api/audit/*` 两个端点同一条口径（同一次留痕两个时钟 = 自相矛盾）。判据：UTC 02:00 ⇒ 载荷 10:00+08:00。"""
        no = self._create().json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.WAITING_GRAB, "actor": "客服小张"},
            content_type="application/json",
        )
        OperationLog.objects.filter(order_no=no, action="order.transition").update(
            created_at=datetime(2026, 9, 21, 2, 0, 0, tzinfo=dt_timezone.utc)
        )
        body = self.client.get(f"{self.base}/{no}/audit/").json()
        row = {r["action"]: r for r in body["data"]}["order.transition"]
        self.assertEqual(row["created_at"], "2026-09-21T10:00:00+08:00")

    def test_parse_returns_money_as_two_decimal_string(self):
        """D2 要求金额一律 Decimal 上线路——`parse` 是唯一一处把 float 放上金额线路的地方。订单端点回 `"199.50"`，`parse` 却回 `199.5`（JSON 数字）。
        ⚠️ 语料用**对标词表**里的 `价格`（不是一个字的"顺手"）：录单识别设置线起识别关键词来自 `RecognitionKeyword` 配置，默认值照抄对标原文，而旧语料里的 `总价` **不在**对标词表里 ⇒ 它按设计不再被识别（spec §3-D3，差异登记在 `tests/test_intake_parser.py::KeywordVocabularyChangeTests`）。
        判据（两位小数字符串）**一个字没改**。"""
        body = self.client.post(
            f"{self.base}/parse/",
            {"content": "价格：199.50"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        value = body["data"]["fields"]["total_price"]
        self.assertIsInstance(value, str)
        self.assertEqual(value, "199.50")

    def test_missing_order_returns_chinese_message(self):
        """404 分支原文是 DRF 的英文 detail（"No Order matches the given query."），
        而本模块其余部分的 `msg` 都是中文——它是给中文界面直接显示的。"""
        body = self.client.get(f"{self.base}/20260920000000000000/").json()
        self.assertEqual(body["code"], 1002)
        self.assertEqual(body["msg"], "记录不存在或已被删除")
        # 结构性兜底：不留任何 ASCII 字母（未来换文案时也挡得住英文 detail）
        self.assertFalse(
            [ch for ch in body["msg"] if ch.isascii() and ch.isalpha()], body["msg"]
        )

    def test_write_path_value_error_is_not_disguised_as_a_business_code(self):
        """`except ValueError` 会把**写库路径**冒出的 `ValueError` 也翻译成业务码 1002。
        那正是 P1 分层要避免的：未预期的错误必须暴露成 500，不能被伪装成业务结果。只有 `IllegalTransition`（非法状态迁移）才该走业务码分支。"""
        no = self._create().json()["data"]["order_no"]
        # ⚠️ 桩打在**被查找的那个模块**（E-0029）：A3 把 `transition` 的动作体搬进了
        #    `apps.business.order_writes`（唯一判据出处，spec §D5），`order_views.record`
        #    不再是这条路径上的查找点。期望值与用例语义**一个字未改**。
        with mock.patch(
            "apps.business.order_writes.record",
            side_effect=ValueError("这是写库路径的 ValueError，不是非法迁移"),
        ):
            with self.assertRaises(ValueError):
                self.client.post(
                    f"{self.base}/{no}/transition/",
                    {"target": OrderStatus.WAITING_GRAB, "actor": "客服小张"},
                    content_type="application/json",
                )
        # 事务回滚，状态未被改动
        self.assertEqual(Order.objects.get(order_no=no).status, OrderStatus.UNASSIGNED)

    def test_update_action_uses_the_money_read_only_serializer(self):
        """金额只读守卫原先**只挂在 `partial_update` 这个 action 名上**。
        今天 `PUT` 被 `http_method_names` 挡着（405）所以无害；若开放 PUT，守卫会因为 action 名不匹配而重新开口子——而 HTTP 层挡不住这条路。"""
        from apps.business.api.order_views import OrderViewSet
        from apps.business.api.serializers import OrderBalanceUpdateSerializer

        view = OrderViewSet()
        for action in ("partial_update", "update"):
            with self.subTest(action=action):
                view.action = action
                self.assertIs(view.get_serializer_class(), OrderBalanceUpdateSerializer)

    def test_patch_cannot_change_any_money_field(self):
        """6 个金额字段原先只有 `total_price` 被测试钉住——另外 5 个是**未被验证**的只读。
        用 `MONEY_FIELDS` 遍历，不手抄 6 个名字：以后加金额字段自动纳入。"""
        no = self._create().json()["data"]["order_no"]
        before = {field: getattr(Order.objects.get(order_no=no), field) for field in MONEY_FIELDS}

        payload = {field: "1.00" for field in MONEY_FIELDS}
        payload["total_price"] = str(before["total_price"])
        payload["customer_phone"] = "13900139000"
        response = self.client.patch(
            f"{self.base}/{no}/", payload, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)

        order = Order.objects.get(order_no=no)
        # 阳性对照：非金额字段确实被改到了，证明这次 PATCH 真的生效
        self.assertEqual(order.customer_phone, "13900139000")
        for field in MONEY_FIELDS:
            with self.subTest(field=field):
                self.assertEqual(getattr(order, field), before[field])

    def test_transition_unassigned_to_waiting_grab(self):
        no = self._create().json()["data"]["order_no"]
        body = self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["status"], OrderStatus.WAITING_GRAB)

    def test_illegal_transition_is_rejected_with_business_code(self):
        """白名单之外的迁移 ⇒ **HTTP 200 + 业务码**（不是 500、不是 403），订单状态不变。
        ⚠️ 靶子换过两次（D-0071、D-0147 / R8）：`UNASSIGNED → COMPLETED` 与 `UNASSIGNED → PENDING_REVIEW` 都已**合法**；R8 之下唯一仍非法的形状是**自环**，所以用 `UNASSIGNED → UNASSIGNED`。断言形状一个字没变。"""
        no = self._create().json()["data"]["order_no"]
        response = self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.UNASSIGNED},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["code"], 0)
        self.assertEqual(Order.objects.get(order_no=no).status, OrderStatus.UNASSIGNED)

    def test_change_price_is_a_separate_action_and_persists(self):
        no = self._create().json()["data"]["order_no"]
        body = self.client.post(
            f"{self.base}/{no}/price/",
            {"total_price": "88.00", "reason": "客户砍价"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        order = Order.objects.get(order_no=no)
        self.assertEqual(order.total_price, Decimal("88.00"))

    def test_price_change_is_recorded_with_old_new_reason_and_actor(self):
        """spec §6 硬规则 2：改价**必须留痕**——「不能只改字段」。
        断言的是**数据库里的那一行**，不是日志：日志断言会因为 `assertLogs` 自己把级别抬到 INFO 而假通过（见计划 Step 4b）。
        ⚠️ **R20**：留痕的 `actor` 取**认证态用户名**；请求体里那个 "客服小张" 是**可伪造**的，不得出现在留痕里（业务字段 `Order.updated_by` 仍取请求体值，那是 P2 契约，见 spec §5.9 的偏离登记）。"""
        no = self._create().json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{no}/price/",
            {"total_price": "88.00", "reason": "客户砍价", "actor": "客服小张"},
            content_type="application/json",
        )
        log = OperationLog.objects.get(action="order.change_taker_price", order_no=no)
        self.assertEqual(log.from_value, "199.50")   # 旧值
        self.assertEqual(log.to_value, "88.00")      # 新值
        self.assertEqual(log.reason, "客户砍价")      # 为什么改
        self.assertEqual(log.actor, P2_TEST_USERNAME)  # 谁改的（认证态）
        self.assertNotIn("客服小张", log.actor)        # 伪造的名字不得出现
        self.assertEqual(log.field_name, "total_price")   # 真实列名（M1）

    def test_transition_is_recorded(self):
        """spec §6 硬规则 2 的「迁移」那一半——迁移同样必须留痕。"""
        no = self._create().json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.WAITING_GRAB, "actor": "客服小张"},
            content_type="application/json",
        )
        log = OperationLog.objects.get(action="order.transition", order_no=no)
        # spec §5.1：前后值是**中文标签**（不是状态码）
        self.assertEqual(log.from_value, "未分配")
        self.assertEqual(log.to_value, "待抢单")
        self.assertEqual(log.field_name, "status")
        # （报告 §偏离 5）：actor 取**认证态用户名**——请求体里那个 "客服小张" 被忽略
        self.assertEqual(log.actor, P2_TEST_USERNAME)

    def test_rejected_transition_is_not_recorded(self):
        """被拒绝的迁移**没有发生**，不得留下「发生过」的假记录。实现者自己指出：brief 说「只在成功之后记」，却**没有任何测试钉住它**——把写入挪到 `try` 之前，全套测试照样绿。这条测试就是堵这个口子。
        ⚠️ 靶子换过两次（D-0071、D-0147 / R8）：`target=COMPLETED` 与 `UNASSIGNED → PENDING_REVIEW` 都已**合法** ⇒ 换用**自环**（R8 之下唯一仍非法的形状）。"""
        no = self._create().json()["data"]["order_no"]
        # 阳性对照：录单本身写了一行 ⇒ 下面的否定断言不是「看着一张空表」
        self.assertTrue(
            OperationLog.objects.filter(order_no=no, action="order.create").exists()
        )
        self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.UNASSIGNED},
            content_type="application/json",
        )
        self.assertFalse(
            OperationLog.objects.filter(order_no=no, action="order.transition").exists()
        )
        self.assertEqual(Order.objects.get(order_no=no).status, OrderStatus.UNASSIGNED)

    # ── ⭐ D-0337：「说明原因」──三个状态的问题单 / 暂停 / 退单要能记原因 ──

    def test_transition_with_reason_lands_in_log_summary_and_column(self):
        """带 `reason` 的流转：原因落留痕行的 `reason` 列 + summary 的「（原因：…）」尾段。
        业务方要求原因在订单日志 / 日志弹窗**自然可见**——那两个读面的行形状只有 `summary`（`OrderActionLogRowSerializer` 恰好 11 键、没有 `reason` 列）⇒ 判据必须打在 summary 文本上，只断 `reason` 列不算数。"""
        no = self._create().json()["data"]["order_no"]
        resp = self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.REFUNDED, "reason": "客户不要了"},
            content_type="application/json",
        )
        self.assertEqual(resp.json()["code"], 0)
        log = OperationLog.objects.get(action="order.transition", order_no=no)
        self.assertEqual(log.reason, "客户不要了")
        self.assertTrue(
            log.summary.endswith("（原因：客户不要了）"),
            f"summary 应带原因尾段，实际：{log.summary}",
        )
        self.assertIn("已退单", log.summary)

    def test_transition_without_reason_keeps_the_old_summary_verbatim(self):
        """回归钉：不带 `reason` ⇒ summary 与旧文案**逐字相同**、无「（原因：）」空壳。
        机器行（同步链路 / 批量不带原因）走的就是这一档；空括号尾巴是业务方一眼能看见的坏。"""
        no = self._create().json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        )
        log = OperationLog.objects.get(action="order.transition", order_no=no)
        self.assertEqual(log.reason, "")
        self.assertEqual(
            log.summary,
            f"{P2_TEST_USERNAME}将订单 {no} 的状态从未分配改为待抢单",
        )
        self.assertNotIn("原因", log.summary)

    def test_transition_reason_is_trimmed(self):
        """`reason` 首尾空白被剥掉（DRF `CharField` 默认 trim，先于 max_length）。"""
        no = self._create().json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.PAUSED, "reason": "  客户急退  "},
            content_type="application/json",
        )
        log = OperationLog.objects.get(action="order.transition", order_no=no)
        self.assertEqual(log.reason, "客户急退")

    def test_transition_reason_over_200_chars_is_rejected_and_writes_nothing(self):
        """201 字 ⇒ 业务码 1000（不是 500），且**一笔不写**（状态不动、无留痕行）。"""
        no = self._create().json()["data"]["order_no"]
        resp = self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.PAUSED, "reason": "退" * 201},
            content_type="application/json",
        )
        self.assertEqual(resp.json()["code"], 1000)
        self.assertIn("200", resp.json()["msg"])
        order = Order.objects.get(order_no=no)
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)
        self.assertFalse(
            OperationLog.objects.filter(order_no=no, action="order.transition").exists()
        )

    def test_transition_reason_of_exactly_200_chars_is_accepted(self):
        """边界钉：恰好 200 字**收**（上限本身不是 199 也不是 255）。"""
        no = self._create().json()["data"]["order_no"]
        resp = self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.PAUSED, "reason": "退" * 200},
            content_type="application/json",
        )
        self.assertEqual(resp.json()["code"], 0)
        log = OperationLog.objects.get(action="order.transition", order_no=no)
        self.assertEqual(len(log.reason), 200)

    def test_patch_cannot_change_total_price_or_partially_update_other_fields(self):
        """普通订单改价仍走 `/price/`；带金额的 PATCH 整体拒绝，不留半次编辑。"""
        no = self._create().json()["data"]["order_no"]
        response = self.client.patch(
            f"{self.base}/{no}/",
            {"total_price": "1.00", "customer_phone": "13900139000"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        order = Order.objects.get(order_no=no)
        self.assertEqual(order.customer_phone, "13800138000")
        self.assertEqual(order.total_price, Decimal("199.50"))

    def test_price_with_non_numeric_value_returns_validation_error(self):
        """非法金额必须是 HTTP 200 + 业务码，**不能是 500**。"""
        no = self._create().json()["data"]["order_no"]
        response = self.client.post(
            f"{self.base}/{no}/price/",
            {"total_price": "abc"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(Order.objects.get(order_no=no).total_price, Decimal("199.50"))

    def test_price_with_too_many_decimals_is_rejected(self):
        """精度超限要被**拒绝**，而不是被 numeric(12,2) 静默四舍五入——
        否则响应里回显的金额与库里的行不一致。"""
        no = self._create().json()["data"]["order_no"]
        response = self.client.post(
            f"{self.base}/{no}/price/",
            {"total_price": "1.999"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(Order.objects.get(order_no=no).total_price, Decimal("199.50"))

    def test_audit_failure_rolls_back_the_transition(self):
        """与改价同理：留痕写不进去时，状态必须**一起回滚**。
        没有这条，`transition` 就与 `/price/` 不对称——spec §6 的硬规则管的是「迁移**与**改价」，只护住一半等于没护。"""
        no = self._create().json()["data"]["order_no"]
        # ⚠️ 桩打在**被查找的那个模块**（E-0029）：A3 把 `transition` 的动作体搬进了
        #    `apps.business.order_writes`，`record` 在**那里**被查找。期望值未改。
        with mock.patch(
            "apps.business.order_writes.record",
            side_effect=RuntimeError("留痕写不进去"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"{self.base}/{no}/transition/",
                    {"target": OrderStatus.WAITING_GRAB, "actor": "客服小张"},
                    content_type="application/json",
                )
        self.assertEqual(Order.objects.get(order_no=no).status, OrderStatus.UNASSIGNED)
        self.assertFalse(
            OperationLog.objects.filter(order_no=no, action="order.transition").exists()
        )

    def test_overlong_reason_is_rejected_instead_of_500(self):
        """`reason` 无长度上限而列是 varchar(255)——超长会让改价**先落库再 500**，留痕丢失。
        那正是 spec §6 禁止的「只改字段」，而且比不修更糟：这类请求原先返回 200。所以必须在**入参层**挡住。"""
        no = self._create().json()["data"]["order_no"]
        response = self.client.post(
            f"{self.base}/{no}/price/",
            {"total_price": "88.00", "reason": "长" * 300},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 1000)
        self.assertEqual(Order.objects.get(order_no=no).total_price, Decimal("199.50"))
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=no, action="order.change_taker_price"
            ).exists()
        )

    def test_audit_failure_rolls_back_the_price_change(self):
        """留痕写不进去时，改价必须**一起回滚**。
        入参校验只能挡住我们**想得到**的失败；这条测试证明的是结构保证：不存在「改了字段但没有留痕」的中间状态。"""
        no = self._create().json()["data"]["order_no"]
        # ⚠️ 桩打在**被查找的那个模块**（E-0029）：A1 把改价动作体搬进了
        #    `apps.business.order_writes`（唯一判据出处，spec §D5），`order_views.record`
        #    不再是这条路径上的查找点。期望值与用例语义**一个字未改**。
        with mock.patch(
            "apps.business.order_writes.record",
            side_effect=RuntimeError("留痕写不进去"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    f"{self.base}/{no}/price/",
                    {"total_price": "88.00", "reason": "客户砍价"},
                    content_type="application/json",
                )
        self.assertEqual(Order.objects.get(order_no=no).total_price, Decimal("199.50"))
        self.assertFalse(
            OperationLog.objects.filter(
                order_no=no, action="order.change_taker_price"
            ).exists()
        )

    def test_parse_endpoint_returns_fields_without_creating_an_order(self):
        """解析只回字段、不建单——结果要允许人工修正后再提交（对标物行为）。
        这是录单页的第一屏主交互（spec §5.1）。"""
        body = self.client.post(
            f"{self.base}/parse/",
            {"content": "账号：player001\n角色名：旅行者\n价格：199.50"},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["fields"]["game_account"], "player001")
        self.assertEqual(body["data"]["fields"]["game_role_name"], "旅行者")
        self.assertIn("game_sub_account", body["data"]["unmatched"])
        # 关键：解析**不产生订单**（setUp 里没有建过任何订单，所以这里无歧义）
        self.assertFalse(Order.objects.exists())

    def test_physical_delete_is_not_exposed(self):
        """spec §6：取消走状态（REFUNDED），不做物理删除。"""
        no = self._create().json()["data"]["order_no"]
        response = self.client.delete(f"{self.base}/{no}/")
        self.assertIn(response.status_code, (404, 405))
        self.assertTrue(Order.objects.filter(order_no=no).exists())

    def test_detail_exposes_allowed_transitions(self):
        """详情要给出**当前状态可达**的目标，供前端在流转弹窗里只列可选项。⚠️ 判据与 `/transition/` **同源**（都用 `nodes_of(request.user)`，见 `serializers.py::get_allowed_transitions`）；本用例的夹具显式声明了 `order.change_status`，`UNASSIGNED` 的这几条可达边都含该节点，所以集合不变。
        ⚠️ **（计划 ④）**：`WAITING_GRAB` 那一项现在靠夹具里的 `order.send_to_grab_hall`(36) 撑着（该目标的端点门已改判 36、不再是 16）；只持 16 时它**不该**出现——反向判据在 `tests/test_rbac_throw_hall_gate.py` 的钉子⑥。
        ⚠️ **（D-0147 / R8）**：集合 +1 = `PENDING_REVIEW`（状态机整体放开后，`UNASSIGNED → PENDING_REVIEW` 的 target 节点集含 16）。`COMPLETED` 仍不在（要节点 17，本夹具不持）。期望值仍是**逐字全表**。"""
        no = self._create().json()["data"]["order_no"]

        body = self.client.get(f"{self.base}/{no}/").json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(
            body["data"]["allowed_transitions"],
            ["WAITING_GRAB", "IN_PROGRESS", "PENDING_REVIEW", "ISSUE", "PAUSED", "REFUNDED"],
        )

    def test_allowed_transitions_is_read_only(self):
        """客户端不能伪造它——它必须反映**服务端**算出的可达集合。"""
        response = self.client.post(
            self.base + "/",
            {
                "game": self.game.id,
                "project_text": "满星",
                "allowed_transitions": ["COMPLETED"],
            },
            content_type="application/json",
        )
        no = response.json()["data"]["order_no"]

        body = self.client.get(f"{self.base}/{no}/").json()

        self.assertNotIn("COMPLETED", body["data"]["allowed_transitions"])

    def test_allowed_transitions_follow_the_current_status(self):
        """换状态后集合要跟着变——证明它是**按当前状态**算的，不是常量。
        ⚠️ 这个集合**同时按调用方身份**算，换轴后判据是**节点**：客服持 `order.change_status`（16）⇒ 在 `WAITING_GRAB` 上**看得到** `IN_PROGRESS`（该边的节点集含 16），因为 `/transition/` 对他也接受这条边——两处同源。旧断言（不含 `IN_PROGRESS`）编码的是角色轴下「接单边只属于代练员」的旧事实，已随单轴化作废（spec A3 登记的放宽）。
        下面用**只持抢单节点的账号**作对照，钉住「同状态、不同节点 → 不同集合」——那个对照恰好是**空集**（理由见下）。"""
        no = self._create().json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.WAITING_GRAB},
            content_type="application/json",
        )

        allowed = self.client.get(f"{self.base}/{no}/").json()["data"][
            "allowed_transitions"
        ]

        # 顺序 = `OrderStatus.values` 的声明顺序（序列化器按它遍历），UNASSIGNED 在最前。
        # （D-0147 / R8）：集合 +1 = `PENDING_REVIEW`（该目标节点集含 16）。
        self.assertEqual(
            allowed,
            ["UNASSIGNED", "IN_PROGRESS", "PENDING_REVIEW", "ISSUE", "PAUSED", "REFUNDED"],
        )
        # 待抢单**可以**回到未分配：P3 刻意新增的「撤回抛单」边
        # （其节点集含 16 / 19 / 117，spec §4.1），持 16 的调用方能走。
        self.assertIn(OrderStatus.UNASSIGNED, allowed)

        # ：详情也要节点（`order.view_detail`）。`allowed_transitions` 的契约是「**通用 `/transition/`** 会接受的目标集合」
        # ⇒ 非 `COMPLETED` 目标还要求类级门 `order.change_status`（16）。只持 `order.view_detail` + `grab_hall.take_order`（50）的账号在状态机边上是通的
        # （`WAITING_GRAB → IN_PROGRESS` 的节点集含 50），但打通用端点必然 403（缺 16）⇒ 这里**恰好是空集**。那条边的专属入口是 `/claim/`，不是 `/transition/`（旧实现曾列出 `IN_PROGRESS`——列了却做不到）。
        taker = make_logged_in_client(
            username="taker_at_hall", groups=("代练员",),
            # ⚠️ 显式 opt-out（默认值翻面）：本用例下面自己建档案 + `games.set`。
            games=None,
            nodes=("order.view_detail", "grab_hall.take_order"),
        )
        # （按游戏权限收窄）：详情走 `get_object()` 的同一条 queryset，
        #    所以这个只读详情的代练员夹具也要有本单游戏的权限，否则读到 1002。
        StaffProfile.objects.create(
            user=get_user_model().objects.get(username="taker_at_hall")
        ).games.set([self.game])
        taker_allowed = taker.get(f"{self.base}/{no}/").json()["data"][
            "allowed_transitions"
        ]
        self.assertEqual(taker_allowed, [])

    def test_refunded_order_offers_every_reachable_target(self):
        """已退单**不再**是「可达目标为空」的终态，也不再只回池内两态（D-0147 / R8）。
        ⚠️ R8 把状态机整体放开后，`REFUNDED` 的出边铺到**除自环外的每个目标态**；这里登录的是客服（持 16 与 36、不持 17），所以 `COMPLETED` 仍不列出；⭐ **（计划 ④）**起「退单 → 待抢单」这一项靠 36（夹具已补），16 单独不够。
        期望值仍是**逐字全表**（顺序 = `OrderStatus.values`），不是放宽成包含。"""
        # ⚠️ **本用例不再需要「必须有价」**：毛收款回落档改成**订单总价 `total_price`**（`null=False default=0.00`）⇒ 无价单迁 `REFUNDED` 不再被 `1008` 拒（原「双空」分支已删除，D-0046 的内部冲突按 §5.3 的方向解开）。
        # 这里仍显式补一个 `received_price`，只是为了让这张单的退款金额非 0、更贴近生产形态——**不是在规避一道已经不存在的闸**。
        # 「无价（总价 0）单照常能退、毛收款记 0」由 `RefundedWithoutPriceTests` 单独钉住。
        no = self._create(received_price="199.50").json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.REFUNDED},
            content_type="application/json",
        )

        body = self.client.get(f"{self.base}/{no}/").json()

        self.assertEqual(body["data"]["status"], OrderStatus.REFUNDED)
        # 顺序 = `OrderStatus.values`；`COMPLETED` 不在（客服不持节点 17）。
        self.assertEqual(
            body["data"]["allowed_transitions"],
            [
                OrderStatus.UNASSIGNED,
                OrderStatus.WAITING_GRAB,
                OrderStatus.IN_PROGRESS,
                OrderStatus.PENDING_REVIEW,
                OrderStatus.ISSUE,
                OrderStatus.PAUSED,
            ],
        )
        # 阳性哨兵：那两条回池边仍在集合里
        self.assertIn(OrderStatus.UNASSIGNED, body["data"]["allowed_transitions"])
        self.assertIn(OrderStatus.WAITING_GRAB, body["data"]["allowed_transitions"])

    def test_refunded_can_transition_back_to_unassigned_and_writes_audit(self):
        """已退单 → 未分配：**真的能改成功**、状态落库、且写了一条留痕。
        业务方裁定第 3 条的正面证据（此前这条请求必然得到 `code=1002 非法状态迁移`，因为 `REFUNDED` 在边表里 0 出边）。
        三条断言缺一不可：码 0（请求被接受）、状态真的变了（不是「200 但没写」）、新表有一行 `order.transition`（前后值 `已退单 → 未分配`；spec §6 硬规则 2：迁移必须留痕）。"""
        # ⚠️ **本用例不再需要「必须有价」**：毛收款回落档改成**订单总价 `total_price`**（`null=False default=0.00`）⇒ 无价单迁 `REFUNDED` 不再被 `1008` 拒（原「双空」分支已删除，D-0046 的内部冲突按 §5.3 的方向解开）。
        # 这里仍显式补一个 `received_price`，只是为了让这张单的退款金额非 0、更贴近生产形态——**不是在规避一道已经不存在的闸**。
        # 「无价（总价 0）单照常能退、毛收款记 0」由 `RefundedWithoutPriceTests` 单独钉住。
        no = self._create(received_price="199.50").json()["data"]["order_no"]
        self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.REFUNDED},
            content_type="application/json",
        )

        body = self.client.post(
            f"{self.base}/{no}/transition/",
            {"target": OrderStatus.UNASSIGNED},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["status"], OrderStatus.UNASSIGNED)
        self.assertEqual(
            Order.objects.get(order_no=no).status, OrderStatus.UNASSIGNED
        )
        log = OperationLog.objects.filter(
            action="order.transition", order_no=no
        ).latest("id")
        # spec §5.1：前后值是**中文标签**
        self.assertEqual(log.from_value, "已退单")
        self.assertEqual(log.to_value, "未分配")

    def test_refunded_order_without_performance_row_recovers_to_the_pool(self):
        """已退单的**旧单**（一条 MAIN 绩效行都没有）恢复回池内必须成功，并把那条行补上。⚠️ 本条钉的是一个**实测过的 HTTP 500**（dev 租户单号 `20260920180229317002`）：业务方在管理订单页把一张**已退单**改成「未分配」，后端 `Performance.DoesNotExist`。根因不是「REFUNDED 来源」本身，而是 `_on_pool_entry` 假设「每张订单都有 MAIN 绩效行（录单即建绩效，spec §7.4）」——`0009_performance` 之前落库的旧单、以及不经 `OrderViewSet.create` 落库的单都**没有**那条行，且 `apps/business/migrations/` 里**没有**任何数据迁移回填过它们；`REFUNDED → {UNASSIGNED, WAITING_GRAB}` 两条出边让「已退单的旧单」第一次能走到这个钩子，于是踩空。**复现条件 = `REFUNDED` + 缺 MAIN 绩效行**（与来源态无关：`ISSUE/PAUSED → 池内` 打在同一张旧单上也会 500）。
        回滚修复（把 `_on_pool_entry` 改回 `main_performance_of`）本条立刻红，报 `Performance.DoesNotExist`（变异验证做过）。
        三条断言对应三件事：① 请求被接受（码 0，不是 500、不是业务码）；② **补行**而不是跳过（跳过只是把 500 挪到 `/assign`，并让 `/claim` 报一个假的「该订单已被别人接走」，见 `ensure_main_performance_of` 的 docstring）；③ 迁移留痕（spec §6 硬规则 2）。"""
        order = Order.objects.create(
            game=self.game,
            order_no="20260921000000000500",
            status=OrderStatus.REFUNDED,
            project_text="满星",
            # 接单价只在后面那条 `/assign` 里用得上。没有它会得到「该单没有接单价，请联系客服」
            # ——那是另一个**预期的业务码**（`_AmountError`），与本条要钉的 500 无关，
            # 但它会让本用例以一个错误的理由红。
            hall_price=Decimal("88.00"),
        )
        # 前置状态**就是复现条件本身**：这张单在库里一条绩效行都没有。
        self.assertFalse(Performance.objects.filter(order=order).exists())

        body = self.client.post(
            f"{self.base}/{order.order_no}/transition/",
            {"target": OrderStatus.UNASSIGNED},
            content_type="application/json",
        ).json()

        self.assertEqual(body["code"], 0)
        self.assertEqual(body["data"]["status"], OrderStatus.UNASSIGNED)
        self.assertEqual(
            Order.objects.get(order_no=order.order_no).status, OrderStatus.UNASSIGNED
        )
        perf = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertIsNone(perf.user_id)
        log = OperationLog.objects.filter(
            action="order.transition", order_no=order.order_no
        ).latest("id")
        self.assertEqual(log.from_value, "已退单")
        self.assertEqual(log.to_value, "未分配")

        # 补出来的那条行必须**真的能用**：恢复后的池内单要能指派（否则「补行」就白补了，
        # `/assign` 会走 `main_performance_of` 抛 500）。
        taker = get_user_model().objects.create_user(username="taker_pool_recover", password="x")
        # ⭐ D-0252：`assign_order` 现在判「目标打手的游戏范围」（无档案=无游戏=拒）
        #    ⇒ 本条的成功腿要打手持本单的游戏（闸的专钉在 tests/test_order_assign_game_gate.py）。
        grant_games(taker, self.game)
        assigned = self.client.post(
            f"{self.base}/{order.order_no}/assign/",
            {"user_id": taker.id},
            content_type="application/json",
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertEqual(assigned.json()["code"], 0, msg=assigned.json())
        perf.refresh_from_db()
        self.assertEqual(perf.user_id, taker.id)



class RefundedWithoutPriceTests(TenantTestCase):
    """**无价订单可以退单（毛收款 0）**——原「双空 ⇒ `1008`」已作废。⚠️ 口径：spec §5.3 说「客户在未分配阶段就取消，是最常见的情况」，而 §10 #14 曾要求「接单实收与接单价双空 ⇒ 拒绝退单」——两条**内部冲突**；后来毛收款回落档从 `hall_price` 改成**订单总价 `total_price`**，而后者是 `null=False default=0.00` ⇒ **「双空」这个状态不存在**，`refunds._gross_received` 的 1008 分支已删除 ⇒ 冲突按 §5.3 的方向解开：**无价（总价 0）订单照常能退**，只是毛收款是 `0.00`。
    断言（新语义，全是字面值）：HTTP 200 + `code 0`、订单真的到 `REFUNDED`、`refund_fee` 停在 `0.00`。⭐ **D-0181 改准后**：退单不建事件行 ⇒ `OrderRefund` 是 **0 条**（改准块见 `tests/test_refund_full.py` 的模块 docstring）。
    ⚠️ 夹具**显式建一张无价单**（不复用别处补过价的 `_create()`），并在发请求**之前**断言三处价确实是「没填」的状态（`total_price` 是模型默认 `0.00`）——否则「毛收款 0」可能只是撞了别的拒绝理由。⚠️ 建单走 `POST /orders/`：不传 `received_price` / `hall_price` / `total_price` ⇒ `total_price` 取默认 `0.00`（`total_price` 是 `required=False`，见 `api/serializers.py`；它**没有** NULL 态）。⚠️ **D-0142 之后**：`refunds._gross_received` **只读** `total_price`、`received_price` 不再参与 ⇒ 本类那张「三处价都没填」的单在两种口径下毛收款都是 `0.00`，所以本类的断言**不需要**随口径变更（它两个口径下都成立）。"""

    def setUp(self):
        # **本夹具声明它要过的门**：本用例走**两个**订单端点，各要一条节点 code（见 `OrderViewSet.ORDER_PERMISSIONS`）：
        # - `POST /orders/`（建无价单）→ `order.add_edit`；- `POST /orders/{no}/transition/`（迁 `REFUNDED`）→ `order.change_status`。
        # `groups=("客服",)` 只建 Group、**不放行任何节点**（节点由 `nodes=` 显式声明）——所以两条 code 都要显式声明。**这是「声明夹具需要的门」，不是放宽门**：门一个字节都没改；这条路径**不再有** `1008` 这道闸，退款钩子在毛收款 0 时照常完成。
        self.client = make_logged_in_client(
            groups=("客服",),
            # ⚠️ 显式 opt-out（默认值翻面）：本类下面自己建档案 + `games.set`。
            games=None,
            nodes=("order.add_edit", "order.change_status"),
        )
        self.base = "/api/business/orders"
        self.game = Game.objects.create(name="原神")
        # （按游戏权限收窄）：同理，夹具补齐本账号的游戏权限
        #    （否则录单被 `assert_game_allowed` 拒、迁状态拿不到 `get_object()`）。
        StaffProfile.objects.create(
            user=get_user_model().objects.get(username=P2_TEST_USERNAME)
        ).games.set([self.game])

    def test_a_priceless_order_can_be_refunded_for_zero(self):
        # 显式建无价单：不传任何价 —— 不是「补过价的 `_create()`」。
        body = self.client.post(
            self.base + "/",
            {
                "game": self.game.id,
                "project_text": "满星",
                "customer_phone": "13800138000",
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0)
        order_no = body["data"]["order_no"]
        order = Order.objects.get(order_no=order_no)
        # 前置：三处价确实是「没填」的状态（否则本条测的不是「毛收款 0」这条路径）。
        self.assertIsNone(order.received_price)
        self.assertIsNone(order.hall_price)
        self.assertEqual(order.total_price, Decimal("0.00"))   # 模型默认值，不是 NULL
        self.assertEqual(order.status, OrderStatus.UNASSIGNED)

        response = self.client.post(
            f"{self.base}/{order_no}/transition/",
            {"target": OrderStatus.REFUNDED},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)      # 业务码，不是 500
        body = response.json()
        self.assertEqual(body["code"], 0, body)          # 不再有 1008：毛收款 = 总价 = 0.00
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.REFUNDED)
        self.assertEqual(order.refund_fee, Decimal("0.00"))
        # ⭐ D-0181：退单不再建 `OrderRefund(kind=FULL)` 事件行。
        self.assertEqual(OrderRefund.objects.filter(order=order).count(), 0)

# ── 系统开关线（D-0223）：「禁止录入相同的来源单号」的手工路径用例 ──────
#: 首条开关的 key（与 `apps/business/system_switches.py` 的注册表同源）。
DUP_GUARD_SWITCH_KEY = "forbid_duplicate_source_order_no"
#: 本类专用的合成重复号（⛔ 不与上面 `SOURCE_ORDER_NO` 复用——那个已被搜索用例占着语义）。
DUP_GUARD_NO = "TB-合成-重复来源-0099"


class DuplicateSourceOrderNoGuardTests(TenantTestCase):
    """开关开着时：录单 / 手工编辑对**完全相同的来源单号**一律拦（D-0223）。路径豁免（开放平台 / peer 自动通道）与租户边界的用例在 `tests/test_system_switches.py`（那边自带各路夹具）。
    ⚠️ 开关的拨动直接写 `SystemSwitch` 行（不经 API）：本类证的是**闸**的行为，API 自己的用例在 `test_system_switches.py`。"""

    def setUp(self):
        # 与 `OrderApiTests` 同一份夹具形状（节点门 + 游戏 + 档案）。
        self.client = make_logged_in_client(
            groups=("客服",),
            games=None,
            nodes=(
                "order.add_edit",            # 录单 / 编辑
                "order.list",
                "order.view_detail",
            ),
        )
        self.base = "/api/business/orders"
        self.game = Game.objects.create(name="重复号守卫游戏")
        Project.objects.create(
            category=ProjectCategory.objects.create(game=self.game, name="守卫分类"),
            name="守卫项目",
        )
        StaffProfile.objects.create(
            user=get_user_model().objects.get(username=P2_TEST_USERNAME)
        ).games.set([self.game])

    def _switch_on(self):
        SystemSwitch.objects.create(key=DUP_GUARD_SWITCH_KEY, value="Y")

    def _create(self, source_order_no, **overrides):
        payload = {
            "game": self.game.id,
            "project_text": "守卫项目",
            "total_price": "10.00",
        }
        if source_order_no is not None:
            payload["source_order_no"] = source_order_no
        payload.update(overrides)
        return self.client.post(self.base + "/", payload, content_type="application/json")

    # ── 录单闸 ─────────────────────────────────────────────────────
    def test_switch_off_duplicate_still_creates(self):
        """开关关（默认）⇒ 既有行为一个字不变：重复号照建。"""
        first = self._create(DUP_GUARD_NO).json()
        second = self._create(DUP_GUARD_NO).json()
        self.assertEqual(first["code"], 0, first)
        self.assertEqual(second["code"], 0, second)
        self.assertEqual(Order.objects.filter(source_order_no=DUP_GUARD_NO).count(), 2)

    def test_switch_on_duplicate_is_rejected_writing_nothing(self):
        """开关开 + 完全相同的号 ⇒ 1000，且**订单与绩效行数都没涨**（被拒一行都不写）。"""
        self._switch_on()
        self.assertEqual(self._create(DUP_GUARD_NO).json()["code"], 0)
        orders_before = Order.objects.count()
        perf_before = Performance.objects.count()
        response = self._create(DUP_GUARD_NO)
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 1000, body)
        # ⭐ 文案必须带上撞的那个号（客服要能按它定位到已存在的那一单）。
        self.assertIn(DUP_GUARD_NO, body["msg"])
        self.assertIn("已存在", body["msg"])
        self.assertEqual(Order.objects.count(), orders_before, "被拒的请求一行都不该写")
        self.assertEqual(Performance.objects.count(), perf_before, "绩效与订单同生共死")

    def test_switch_on_duplicate_across_different_order_sources_is_still_rejected(self):
        """同号但两单 `order_source` 各指不同的字典行 ⇒ 仍拦：本线口径**不看订单来源**。
        ⚠️ 区分性用例：D-0223 有意偏离对标 SK32（对标是「同来源 + 同号」才拦）——若日后有人把闸「修」回对标口径（查询加 `order_source` 过滤），上面那批两单来源都为空的用例**一条都不会红**（`order_source IS NULL` 两边相等），只有本条能抓住（两单各指不同的 `OrderSource` 行）。"""
        self._switch_on()
        source_a = OrderSource.objects.create(name="来源甲")
        source_b = OrderSource.objects.create(name="来源乙")
        self.assertEqual(
            self._create(DUP_GUARD_NO, order_source=source_a.pk).json()["code"], 0
        )
        response = self._create(DUP_GUARD_NO, order_source=source_b.pk)
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 1000, body)
        self.assertIn(DUP_GUARD_NO, body["msg"])
        # 被拒一行不写：同号仍只有占号的那一行。
        self.assertEqual(Order.objects.filter(source_order_no=DUP_GUARD_NO).count(), 1)

    def test_switch_on_a_different_number_creates(self):
        self._switch_on()
        self._create(DUP_GUARD_NO)
        body = self._create("TB-合成-重复来源-0098").json()
        self.assertEqual(body["code"], 0, body)

    def test_switch_on_empty_number_creates(self):
        """空来源单号永远不拦（多单空号共存是既有合法形态，D-0223 第 5 条）。"""
        self._switch_on()
        self._create("")
        body = self._create("").json()
        self.assertEqual(body["code"], 0, body)

    def test_switch_on_recycled_order_does_not_occupy_the_number(self):
        """回收站不占号：唯一一行同号是**已软删**的单 ⇒ 删掉重录走得通（第 4 条）。"""
        self._switch_on()
        order_no = self._create(DUP_GUARD_NO).json()["data"]["order_no"]
        deleted = Order.objects.get(order_no=order_no)
        deleted.deleted_at = timezone.now()
        deleted.save(update_fields=["deleted_at"])
        body = self._create(DUP_GUARD_NO).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(
            Order.objects.filter(
                source_order_no=DUP_GUARD_NO, deleted_at__isnull=True
            ).count(),
            1,
        )

    # ── 编辑闸（手工 PATCH；开放平台改单共用 `update_order_fields` 但不走视图层闸）──
    def _patch(self, order_no, payload):
        return self.client.patch(
            f"{self.base}/{order_no}/", payload, content_type="application/json"
        )

    def test_switch_on_patch_to_a_used_number_is_rejected_and_writes_nothing(self):
        self._switch_on()
        holder = self._create(DUP_GUARD_NO).json()["data"]["order_no"]
        other = self._create("TB-合成-重复来源-0097").json()["data"]["order_no"]
        response = self._patch(other, {"source_order_no": DUP_GUARD_NO})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 1000, body)
        self.assertIn(DUP_GUARD_NO, body["msg"])
        order = Order.objects.get(order_no=other)
        self.assertEqual(order.source_order_no, "TB-合成-重复来源-0097", "该单未被改")
        self.assertIsNotNone(Order.objects.get(order_no=holder))

    def test_switch_on_patch_across_different_order_sources_is_still_rejected(self):
        """编辑闸同一条口径：撞的号在**另一个订单来源**的单上 ⇒ 仍拦（不看订单来源）。
        ⚠️ 区分性用例（与上面录单那条成对）：闸的查询若加 `order_source` 过滤（对标口径），上面那条「两单来源都为空」的编辑用例照样绿，只有本条会红。"""
        self._switch_on()
        source_a = OrderSource.objects.create(name="来源丙")
        source_b = OrderSource.objects.create(name="来源丁")
        self._create(DUP_GUARD_NO, order_source=source_a.pk)
        other = self._create(
            "TB-合成-重复来源-0093", order_source=source_b.pk
        ).json()["data"]["order_no"]
        response = self._patch(other, {"source_order_no": DUP_GUARD_NO})
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["code"], 1000, body)
        self.assertIn(DUP_GUARD_NO, body["msg"])
        order = Order.objects.get(order_no=other)
        self.assertEqual(order.source_order_no, "TB-合成-重复来源-0093", "该单未被改")

    def test_switch_on_patching_back_its_own_value_is_allowed(self):
        """`exclude(pk=order.pk)`：PATCH 回自己原值不被误拦（前端整表单提交会带原值）。"""
        self._switch_on()
        self._create(DUP_GUARD_NO)
        own = self._create("TB-合成-重复来源-0096").json()["data"]["order_no"]
        response = self._patch(own, {"source_order_no": "TB-合成-重复来源-0096"})
        self.assertEqual(response.json()["code"], 0, response.json())

    def test_switch_on_patch_to_a_fresh_number_is_allowed(self):
        self._switch_on()
        self._create(DUP_GUARD_NO)
        own = self._create("TB-合成-重复来源-0095").json()["data"]["order_no"]
        response = self._patch(own, {"source_order_no": "TB-合成-全新-0001"})
        self.assertEqual(response.json()["code"], 0, response.json())
        self.assertEqual(
            Order.objects.get(order_no=own).source_order_no, "TB-合成-全新-0001"
        )

    def test_switch_off_patch_to_a_used_number_is_allowed(self):
        """开关关 ⇒ 编辑路径既有行为不变（重复号照改）。"""
        self._create(DUP_GUARD_NO)
        own = self._create("TB-合成-重复来源-0094").json()["data"]["order_no"]
        response = self._patch(own, {"source_order_no": DUP_GUARD_NO})
        self.assertEqual(response.json()["code"], 0, response.json())
