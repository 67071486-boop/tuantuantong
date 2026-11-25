"""Task 5：内部抢单大厅列表（spec §5 / §4.1.2 / **D-0032**）。

大厅的入选条件是**一条不变量**，不是一组「筛选条件」：
`status = WAITING_GRAB` **且** 那条 MAIN 绩效行的 `user` 为空。
D-0032 之后后者由状态机保证（任何迁入池内状态的迁移都会回退绩效三元组），
所以这个条件一旦不成立，说明**不变量被破坏了**——大厅会静默少一张单。

⚠️ 因此本文件里有两种测试，不要混：
- **过滤器**测试（价格 / 关键词 / 状态）——证明筛选参数真的生效；
- **正确性条件**测试（无人接 / 孤儿单 / D-0032 端到端）——证明大厅里的单真的没人接。
"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business.api.serializers import HallSearchSerializer
from apps.business.models import EmpType, Game, GameServer, Order, OrderStatus, Performance
from apps.core.error_codes import ErrorCode
from tests.p5_auth_helpers import make_logged_in_client

# spec §5 的端点表写的是 `/api/business/hall`（全表都不带尾斜杠，与 P2 端点表同一写法）。
HALL_URL = "/api/business/hall/"

#: 本文件走到的订单端点门（`OrderViewSet.ORDER_PERMISSIONS`）。
#: `groups` 只建 Group、**不放行** ⇒ 每个真实登录的夹具都要显式声明它要过的门。
HALL_NODES = (
    "grab_hall.take_order",   # 大厅 / 抢单
    "order.add_edit",         # 录单
    "order.list",             # 列表
    "order.view_detail",      # 详情
    "order.change_status",    # `/transition/`
    # ⭐ `test_pool_entry_returns_the_order_to_the_hall`
    #    的「退回大厅」在 D2 之后是**两步产品动作**（先 `/clear-taker/` 再 `/throw-to-hall/`）
    #    ⇒ 夹具显式声明这两条门（**声明**，不是放宽任何判据：缺了它们拿到的是 403，
    #    与本用例要钉的「大厅可见性」无关）。
    "order.clear_taker",      # `/clear-taker/`
    # ⭐ 计划 ④：抛大厅改判**独占** 36 ⇒ 夹具从 20 换成 36
    #    （20 只管改发单价，不再开抛单这道门）。
    "order.send_to_grab_hall",  # `/throw-to-hall/`（独占闸）
)


class HallApiTests(TenantTestCase):
    def setUp(self):
        self.game = Game.objects.create(name="原神")
        # ⚠️ 必须走**真实登录**（fail-closed，spec D11）：`force_login` 的会话没有
        #    `identity_plane`，每个请求都会 401（见 tests/p5_auth_helpers.py）。
        #    双角色是为了下面的 D-0032 端到端用例能同时走 `/transition/`（需要客服）
        #    与看大厅（需要代练员，Ruling 35）。
        # ⚠️ **不要拿这个双角色夹具去测「客服不能看大厅」**——它同时持有代练员，
        #    闸门对它本来就该放行；越权必须另造只持客服的账号（见对应用例）。
        self.viewer = self._login("代练员", "客服")
        # （按游戏权限收窄）：**夹具补齐**，不是放宽判据。
        #    大厅列表过 `scope_queryset(..., game_scope("game_id"))` ⇒ 看大厅的账号
        #    必须持有本文件的游戏，否则大厅空集（下面各用例的期望值一个字没改）。
        #    本文件另有两处**用例内**的账号（`taker-only` / `test_hall_filters_by_game`
        #    里的第二款游戏）按同样的理由各自补权限。
        StaffProfile.objects.create(user=self.viewer).games.set([self.game])
        self.url = HALL_URL
        # 大厅该看到的：抛单了、还没人接
        self.in_hall = self._order("20260920000000000001", OrderStatus.WAITING_GRAB)
        # 不该看到的两种：没抛单 / 已被接走
        self.unassigned = self._order("20260920000000000002", OrderStatus.UNASSIGNED)
        self.claimed = self._order("20260920000000000003", OrderStatus.IN_PROGRESS)
        taker = User.objects.create_user(username="taker1")
        Performance.objects.filter(order=self.claimed).update(user=taker)

    def _login(self, *groups, username="hall-viewer"):
        """真实登录夹具（fail-closed，spec D11）——不用 `force_login`。

        ⚠️ `games=None`（本文件 setUps 的显式 opt-out，2026-09-23 夹具默认翻面）：下面马上
        自己 `StaffProfile.objects.create(...).games.set([self.game])` ⇒ 夹具这一份必须
        **不建**，否则两边都建同一 user 的档案会撞 `accounts_staffprofile_user_id_key`。
        """
        self.client = make_logged_in_client(
            username=username, groups=groups, games=None, nodes=HALL_NODES
        )
        return User.objects.get(username=username)

    def _order(
        self, no, status, price="88.00", role_name="", server=None, project_text="",
        source_order_no="",
    ):
        """建一对「订单 + 打绩效」。

        ⚠️ 绩效不是可选的：D-0032 之后**任何迁入池内状态**的路径都会读那条 MAIN 行，
        生产路径由录单保证它一定存在（spec §7.4）。夹具少了它，池内状态就是脏态。
        """
        order = Order.objects.create(
            game=self.game,
            order_no=no,
            status=status,
            hall_price=Decimal(price),
            game_role_name=role_name,
            server=server,
            source_order_no=source_order_no,
            project_text=project_text,
        )
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN, temp_amount=Decimal(price)
        )
        return order

    def _body(self, params=None):
        return self.client.get(self.url, params or {}).json()

    def _app_body(self, user, params=None):
        """以 **AppToken 令牌**（打手 App / 小程序那一面）拉大厅。

        ⭐ 2026-10-24 晚间规则：模拟单**只在这一面**可见（PC / H5 会话面永远没有）
        ⇒ 凡断言「模拟单行」的用例必须走这里，⛔ 不许用会话 `_body`。
        """
        from datetime import timedelta

        from django.utils import timezone as tz

        from apps.accounts.models import AppToken
        from apps.business.peer_sync import hash_token, new_token

        raw = new_token()
        AppToken.objects.create(
            user=user, key_hash=hash_token(raw), client="hall-app-sim",
            expires_at=tz.now() + timedelta(hours=1),
        )
        c = Client(SERVER_NAME="tenant.test.com")
        return c.get(self.url, params or {}, HTTP_AUTHORIZATION="Bearer " + raw).json()

    def _nos(self, params=None):
        return [row["order_no"] for row in self._body(params)["data"]["items"]]

    # —— 过滤器 ——
    def test_hall_lists_only_thrown_orders(self):
        """只有「待抢单」进大厅：`UNASSIGNED`（没抛单）与 `IN_PROGRESS`（已接走）都不进。

        判别力：去掉 `status=WAITING_GRAB` 那条筛选 → `unassigned`（绩效行 `user` 为空）
        会一起出现，本用例变红。
        """
        body = self._body()
        self.assertEqual(body["code"], 0)
        self.assertEqual(self._nos(), [self.in_hall.order_no])

    @override_settings(HALL_LIST_CACHE_TTL_S=0)
    def test_hall_only_loads_details_for_requested_page(self):
        from django.utils import timezone

        tied_at = timezone.now()
        newer = self._order("20260920000000000130", OrderStatus.WAITING_GRAB)
        newest = self._order("20260920000000000131", OrderStatus.WAITING_GRAB)
        Order.objects.filter(pk__in=[newer.pk, newest.pk]).update(created_at=tied_at)
        with patch.object(Order, "from_db", wraps=Order.from_db) as load_order:
            response = self.client.get(self.url, {"page": 2, "page_size": 1})
        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["total"], 3)
        self.assertEqual(payload["page"], 2)
        self.assertEqual([row["order_no"] for row in payload["items"]], [newer.order_no])
        self.assertEqual(load_order.call_count, 1)

    @override_settings(HALL_LIST_CACHE_TTL_S=0)
    def test_out_of_range_page_does_not_load_card_details(self):
        with patch.object(Order, "from_db", wraps=Order.from_db) as load_order:
            response = self.client.get(self.url, {"page": 2, "page_size": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], ErrorCode.BUSINESS)
        self.assertIsNone(response.json()["data"])
        self.assertEqual(load_order.call_count, 0)

    @override_settings(HALL_LIST_CACHE_TTL_S=0)
    def test_hall_recovers_a_coherent_page_when_a_selected_card_leaves(self):
        from apps.business.api.order_views import _HallSource

        first = self._order("20260920000000000132", OrderStatus.WAITING_GRAB)
        newest = self._order("20260920000000000133", OrderStatus.WAITING_GRAB)
        original = _HallSource.load

        def card_leaves_during_load(source, keys):
            if source.rows.model is Order:
                departing = newest if source.snapshot is None else first
                Order.objects.filter(pk=departing.pk).update(status=OrderStatus.IN_PROGRESS)
            return original(source, keys)

        with patch.object(_HallSource, "load", card_leaves_during_load):
            response = self.client.get(self.url, {"page": 1, "page_size": 1})
        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["total"], 2)
        self.assertEqual([row["order_no"] for row in payload["items"]], [first.order_no])

    def test_hall_native_row_order_no_stays_the_internal_claim_key(self):
        """⭐ D-0338（2026-10-30）**保护钉**：内部大厅（business hall）原生行的 `order_no`
        键 = **内置单号**，⛔ **不**跟 takerapi 那一路一起改成「来源号优先」。

        为什么必须钉住（勘察结论，别再改坏）：打手 App（`app/`，字段上的旧包）
        拿这一键拼**抢单 URL**（`POST /hall/<order_no>/claim/`，服务端 `get_object`
        只认内置号精确匹配）与**参考图 URL**（`orders/<order_no>/screenshots`，各取图口
        也只认内置号）——把这一键改成来源号，**线上旧包当场接不了单**。D-0301 的
        「来源号优先」在内部读口这一路是**客户端**展示分流（H5 `hallDisplay.ts` /
        App `orderNoText`，载荷的 `source_order_no` 键就在）；
        takerapi 那一路（对外第三方）才是 D-0338 改成来源号优先的那条路。
        """
        self.in_hall.source_order_no = "TB-合成-保护钉-来源号"
        self.in_hall.save(update_fields=["source_order_no"])
        rows = self._body()["data"]["items"]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        # 寻址键 = 内置号（App 旧包拿它抢单/取图）——⛔ 不是来源号。
        self.assertEqual(row["order_no"], self.in_hall.order_no)
        # 来源号照旧在自己的键上（客户端展示分流的取值出口，D-0301）。
        self.assertEqual(row["source_order_no"], "TB-合成-保护钉-来源号")

    def test_hall_filters_by_price_range(self):
        """价格区间是**两端**都收窄的；`hall_price` 是抛单价（spec §3.4 / §5）。"""
        cheap = self._order("20260920000000000004", OrderStatus.WAITING_GRAB, price="10.00")
        pricey = self._order("20260920000000000005", OrderStatus.WAITING_GRAB, price="300.00")
        # 前置哨兵：三条都**真的**在大厅里——否则「被价格筛掉」与「本来就不在大厅」
        # 不可区分（判别力判据 1）。
        self.assertEqual(
            set(self._nos()),
            {self.in_hall.order_no, cheap.order_no, pricey.order_no},
        )
        self.assertEqual(
            set(self._nos({"price_max": "100.00"})),
            {cheap.order_no, self.in_hall.order_no},
        )
        self.assertEqual(
            set(self._nos({"price_min": "50.00"})),
            {self.in_hall.order_no, pricey.order_no},
        )

    def test_hall_shows_and_filters_by_the_effective_taker_price(self):
        """覆盖价优先于默认价，且**展示与筛选同源**（Ruling 44 / spec §6.3）。

        构造「默认价 100.00 / 绩效覆盖价 300.00」的单：

        - **展示**：payload 的 `hall_price` 必须是 300.00（不是 100.00）；
        - **筛选**：`?price_min=200.00` 要能筛到它、`?price_max=200.00` 要筛不到它。

        判别力（两个方向各一个变异）：把 `search_hall` 的两个 filter 改回
        `hall_price` ⇒ 筛选取到默认价 100 ⇒ 下面两条筛选取向断言都变红；
        把 `HallListSerializer.hall_price` 的 `source=` 去掉 ⇒ 展示读到 100.00 ⇒ 变红。

        ⚠️ **今天的接口写不出这个状态**：`Performance.taker_price` 在 P3a 里
        **没有写入点**（P3b 的 `/adjust` 才会有），所以这里是**直接改库**构造。
        它钉的是「展示与筛选只能有一个价源」这条契约——契约的价值在写入点出现之后。
        """
        order = self._order("20260920000000000014", OrderStatus.WAITING_GRAB, price="100.00")
        Performance.objects.filter(order=order, emp_type=EmpType.MAIN).update(
            taker_price=Decimal("300.00")
        )
        rows = {row["order_no"]: row for row in self._body()["data"]["items"]}
        self.assertIn(order.order_no, rows)          # 前置哨兵：它真的在大厅里
        self.assertEqual(
            Decimal(rows[order.order_no]["hall_price"]), Decimal("300.00")
        )
        self.assertIn(order.order_no, self._nos({"price_min": "200.00"}))
        self.assertNotIn(order.order_no, self._nos({"price_max": "200.00"}))
        # 反向对照：**没有覆盖价**的单仍按自己的默认价说话（覆盖不是把默认价抹掉）
        plain = self._order("20260920000000000015", OrderStatus.WAITING_GRAB, price="100.00")
        self.assertIn(plain.order_no, self._nos({"price_max": "200.00"}))

    def test_hall_price_max_zero_is_not_treated_as_missing(self):
        """`?price_max=0.00` 必须**真的**按 0 收窄（Ruling 39）。

        `bool(Decimal("0.00")) is False` ⇒ 旧写法 `if price_max:` 把 0 当成「没传」，
        **静默**不过滤、连 300.00 的单一起返回。D-0015 之下金额可以为负，
        `0` 是**有意义的边界**而不是「空」。
        """
        pricey = self._order("20260920000000000011", OrderStatus.WAITING_GRAB, price="300.00")
        # 前置哨兵：它**真的**在大厅里——否则「被 0 筛掉」与「本来就不在大厅」不可区分
        self.assertIn(pricey.order_no, self._nos())
        self.assertEqual(self._nos({"price_max": "0.00"}), [])
        # 反向对照：上限放到 300.00 时它**必须**回来，否则上面的「排除」可能来自别的条件
        self.assertEqual(
            set(self._nos({"price_max": "300.00"})),
            {self.in_hall.order_no, pricey.order_no},
        )

    def test_hall_price_min_zero_excludes_negative_price(self):
        """`?price_min=0.00` 同理：必须排除**负价**单（D-0015 金额可为负）。"""
        negative = self._order("20260920000000000012", OrderStatus.WAITING_GRAB, price="-5.00")
        self.assertIn(negative.order_no, self._nos())          # 前置哨兵：负价单真的在大厅
        self.assertNotIn(negative.order_no, self._nos({"price_min": "0.00"}))
        self.assertIn(negative.order_no, self._nos({"price_min": "-10.00"}))

    def test_hall_filters_by_game(self):
        """大厅按游戏筛选：**另一个游戏**的池内单不入结果。

        判别力：把 `search_hall` 里的 game 过滤整段删掉 → 那张别的游戏的单会出现，
        本用例变红。不存在的 id（999999）⇒ **空集**，而不是「忽略筛选返回全部」——
        这正是 Ruling 39 追加里想要的语义（`0` 到不了这里：它在入口就被
        `IntegerField(min_value=1)` 判 `code=1000`，见 `tests/test_selectors.py`
        与 `test_order_api.py::test_zero_game_param_is_rejected_not_silently_ignored`）。
        """
        other_game = Game.objects.create(name="星穹铁道")
        # （按游戏权限收窄）：把第二款游戏**也勾给本账号**——
        #    本用例判的是「`?game=` 这个筛选参数生效」（判别力 = 删掉 search_hall 的 game
        #    过滤后 foreign 会出现）。不给它权限的话 foreign 会被**游戏可见范围**先滤掉，
        #    那条判别力就没了（测试会绿，但测的不再是它要测的东西）。
        self.viewer.staff_profile.games.add(other_game)
        foreign = Order.objects.create(
            game=other_game, order_no="20260920000000000014",
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("77.00"),
        )
        Performance.objects.create(
            order=foreign, emp_type=EmpType.MAIN, temp_amount=Decimal("77.00")
        )
        self.assertIn(foreign.order_no, self._nos())  # 前置哨兵：它真的在大厅里
        self.assertEqual(self._nos({"game": str(self.game.id)}), [self.in_hall.order_no])
        self.assertEqual(self._nos({"game": "999999"}), [])

    def test_hall_filters_by_project_text(self):
        """大厅按**项目名**（自由文本）模糊筛：`project_text__icontains`。

        判别力：删掉 `search_hall` 里的 `project_text` 过滤 → 本用例变红。
        （Task 5 复审 M2：这条筛选此前在大厅侧**零覆盖**——删掉不会红。）
        """
        hydro = self._order(
            "20260920000000000021", OrderStatus.WAITING_GRAB, project_text="水神瞳代肝"
        )
        abyss = self._order(
            "20260920000000000022", OrderStatus.WAITING_GRAB, project_text="深渊满星"
        )
        # 前置哨兵：两条都**真的**在大厅里——否则「被筛掉」与「本来就不在大厅」不可区分
        self.assertEqual(
            set(self._nos()), {self.in_hall.order_no, hydro.order_no, abyss.order_no}
        )
        self.assertEqual(self._nos({"project_text": "水神瞳"}), [hydro.order_no])
        # 反向对照：换另一条的关键词必须换回**另一条**——否则「命中」可能来自别的条件
        self.assertEqual(self._nos({"project_text": "深渊"}), [abyss.order_no])

    def test_hall_filters_by_server(self):
        """大厅按**区服**筛（`server_id`）。

        判别力：删掉 `search_hall` 里的 `server_id` 过滤 → 本用例变红。
        （Task 5 复审 M2：`server` 此前只有选择器层覆盖，大厅出口没有。）
        """
        server_a = GameServer.objects.create(game=self.game, name="国服")
        server_b = GameServer.objects.create(game=self.game, name="国际服")
        on_a = self._order("20260920000000000023", OrderStatus.WAITING_GRAB, server=server_a)
        on_b = self._order("20260920000000000024", OrderStatus.WAITING_GRAB, server=server_b)
        self.assertEqual(
            set(self._nos()), {self.in_hall.order_no, on_a.order_no, on_b.order_no}
        )
        self.assertEqual(self._nos({"server": str(server_a.id)}), [on_a.order_no])
        # 反向对照：换另一个区服必须换回**另一条**，否则「筛了」与「全都返回」不可区分
        self.assertEqual(self._nos({"server": str(server_b.id)}), [on_b.order_no])

    def test_hall_keyword_matches_order_no(self):
        """关键词命中的是**订单号**（`KEYWORD_FIELDS`）。

        ⚠️ 必须再造一条**不命中**的大厅单当哨兵：只有一条大厅单时，关键词筛选是不是
        生效都一样（`assertEqual(nos, [in_hall])` 恒真）——那是一条**不能失败**的断言
        （判别力判据 1 的同款失败形状）。
        """
        decoy = self._order("20260920000000000006", OrderStatus.WAITING_GRAB)
        self.assertEqual(set(self._nos()), {self.in_hall.order_no, decoy.order_no})
        self.assertEqual(self._nos({"keyword": self.in_hall.order_no}), [self.in_hall.order_no])

    def test_hall_keyword_matches_role_name(self):
        """关键词也命中**角色名**（对标物是「系统订单号 / 角色名 / 电话」）。"""
        decoy = self._order(
            "20260920000000000007", OrderStatus.WAITING_GRAB, role_name="钟离"
        )
        matched = self._order(
            "20260920000000000008", OrderStatus.WAITING_GRAB, role_name="雷电将军"
        )
        self.assertEqual(
            set(self._nos()), {self.in_hall.order_no, decoy.order_no, matched.order_no}
        )
        self.assertEqual(self._nos({"keyword": "雷电"}), [matched.order_no])

    def test_hall_keyword_also_matches_source_order_no(self):
        """⭐ 关键词也命中**来源单号**（对标 `#key1` 的 `title` 里的「淘宝单号」）。

        ⚠️ 这条同时是**「三个搜索口共用 `KEYWORD_FIELDS`」**的判据：本表是
        `search_orders` / `search_hall` / `search_recycle_bin` 的**同一个**出处
        ⇒ 管理订单页修好之后，大厅这一侧**不需要再改一行**就该能搜。
        ⛔ 若将来有人给某一页单独一份字段表，这条会与订单页那条**一起**提醒他。
        """
        decoy = self._order(
            "20260920000000000009", OrderStatus.WAITING_GRAB,
            source_order_no="TB-合成-大厅-别家",
        )
        matched = self._order(
            "20260920000000000010", OrderStatus.WAITING_GRAB,
            source_order_no="TB-合成-大厅-本家",
        )
        self.assertEqual(
            set(self._nos()), {self.in_hall.order_no, decoy.order_no, matched.order_no}
        )
        self.assertEqual(self._nos({"keyword": "TB-合成-大厅-本家"}), [matched.order_no])


    def test_hall_response_carries_the_taker_price(self):
        """大厅 payload 必须带**接单价** `hall_price`（Ruling 34）。

        代练员在大厅挑单，接单价是他唯一关心的数；而大厅又支持按价格区间筛 ⇒
        看不见价格就是「能筛却看不见」。
        ⚠️ 断**值**而不是断 key 存在——「`"hall_price" in row`」是恒真断言（判别力判据 4）。
        而且接单价刻意与客户总价取**不同的值**：若实现把 `total_price` 错当接单价暴露，
        这里读到的会是 999.00。
        """
        distinct = self._order("20260920000000000013", OrderStatus.WAITING_GRAB, price="120.00")
        Order.objects.filter(pk=distinct.pk).update(total_price=Decimal("999.00"))
        rows = {row["order_no"]: row for row in self._body()["data"]["items"]}
        self.assertIn(distinct.order_no, rows)  # 前置哨兵：它真的在大厅里
        self.assertEqual(Decimal(rows[distinct.order_no]["hall_price"]), Decimal("120.00"))
        # ⛔ Ruling 34 的「反向对照」（`/orders/` 列表**不含** `hall_price`）**已被
                #    spec §5 / C6（第二批列）作废**：管理订单页的「发单价」列要读它
        #    ⇒ `/orders/` 也带这个键，取值是 `Order.hall_price`（录单默认发单价），
        #    **不是**大厅那份生效接单价。所以这里改成断**新契约的值**（仍然与
        #    `total_price=999.00` 刻意不同 ⇒ 「把总价当发单价」照样会红），
        #    而不是断键不存在。
        order_rows = {
            row["order_no"]: row
            for row in self.client.get("/api/business/orders/").json()["data"]["items"]
        }
        self.assertIn(distinct.order_no, order_rows)  # 非空哨兵：这单真在列表里
        self.assertEqual(
            Decimal(order_rows[distinct.order_no]["hall_price"]), Decimal("120.00")
        )

    def test_hall_pagination_shape_follows_p2(self):
        """分页形状沿用 P2 的 `EnvelopePagination`：`data` 是 {items,total,page,page_size}。"""
        data = self._body()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 20)
        self.assertEqual([row["order_no"] for row in data["items"]], [self.in_hall.order_no])

    def test_hall_requires_authentication(self):
        """未认证 → 401（fail-closed 默认权限类，spec D11）。"""
        anonymous = Client(SERVER_NAME="tenant.test.com")
        response = anonymous.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_hall_is_visible_to_a_taker_only_account(self):
        """代练员能看大厅（200 + `code 0`）——**只持代练员**，不借 `setUp` 的双角色夹具。"""
        client = make_logged_in_client(
            username="taker-only", groups=("代练员",),
            # ⚠️ 显式 opt-out（默认值翻面）：本用例下面自己建档案 + `games.set`。
            games=None,
            nodes=("grab_hall.take_order",),
        )
        # （按游戏权限收窄）：本用例断言大厅里**看得到**那张池内单，
        #    所以这个账号也要有它的游戏权限（否则空集 ⇒ 红，但红的原因不是本用例要测的门）。
        StaffProfile.objects.create(
            user=User.objects.get(username="taker-only")
        ).games.set([self.game])
        response = client.get(self.url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 0)
        self.assertEqual(
            [row["order_no"] for row in body["data"]["items"]], [self.in_hall.order_no]
        )

    def test_hall_is_open_to_any_node_holder_not_only_takers(self):
        """**能不能看大厅只由节点回答**（2026-09-23 单轴化）。

        ⚠️ 本条替换了旧的 `test_hall_is_forbidden_to_a_service_only_account`
        ——它钉的是 `Role.TAKER` 职位闸门（Ruling 35），那道门已随单轴化删除。
        新口径：持 `grab_hall.take_order` 的账号——哪怕是「客服」这个内置职位、
        或任意自定义职位——都能看大厅（**这是本线的目标形态**）；
        不持该节点的账号才 403（类级节点门）。
        """
        holder = make_logged_in_client(
            username="cs-node-holder", groups=("客服",),
            # 本用例只断言端点准入（200），不关心游戏范围 ⇒ 显式「不建档案」，
            # 免得夹具顺手建一份与本用例无关的全量档案（默认值翻面）。
            games=None,
            nodes=("grab_hall.take_order",),
        )
        self.assertEqual(holder.get(self.url).status_code, 200)

        # 另起一个**不同组名**的账号，避免与前一个共用同一个 `Role`（那会把节点带过去）。
        without = make_logged_in_client(
            username="cs-no-node", groups=("无节点组",), games=None, nodes=(),
        )
        self.assertEqual(without.get(self.url).status_code, 403)

    def test_hall_search_serializer_declares_only_the_params_it_applies(self):
        """大厅的入参序列化器**只声明它真的会应用的字段**（Ruling 40②）。

        继承 `OrderSearchSerializer` 会把 `status`/`is_urgent`/`tag`/`date_from`/`date_to`
        一起带进来：它们被**校验通过**、然后被大厅**静默忽略**——客户端以为在筛，
        实际筛了个寂寞。共用字段仍只有一处定义（`OrderSearchBaseSerializer`）。
        ⭐ D-0296：`include_exited` / `preferred_only` 是本线**真的会应用**的
        两个开关（灰区 / 优选过滤）⇒ 它们进这个集合是**有意变更**（同一次改准本断言）。
        """
        self.assertEqual(
            set(HallSearchSerializer().fields),
            {
                "game", "server", "project_text", "keyword", "price_min", "price_max",
                "include_exited", "preferred_only",
            },
        )

    def test_hall_does_not_validate_params_it_never_applies(self):
        """上一条的**出口**对照：大厅不应用的参数与未知参数同待遇——忽略，不是报错。

        修前 `?status=BOGUS` 回 `code=1000`（被继承来的 `ChoiceField` 校验），
        修后回 `code=0`：大厅的参数集**就是**它会应用的那 6 个（与 `?tag=` 一致）。
        """
        response = self.client.get(self.url, {"status": "BOGUS"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

    def test_hall_never_500s_on_malformed_query_params(self):
        """请求来源的值进类型化的列/查询之前一律过序列化器（E-0010）。

        `?game=abc`（`IntegerField`）与 `?price_max=abc`（`DecimalField`）**直接**拿去
        `filter()` 会抛 `ValueError`/`ValidationError`——它们不在异常处理器的业务分支里，
        结果是 **HTML 500**，违反「业务结果一律 200 + code」。
        判据是「**这类请求**还会不会 500」，不是「这个字段」：所以三个参数一起断。
        """
        for params in ({"game": "abc"}, {"server": "abc"}, {"price_max": "abc"}):
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["code"], ErrorCode.VALIDATION)
        # 空串按「没传」处理（前端常送 `?game=`），不该被当成非法值
        response = self.client.get(self.url, {"game": "", "price_max": ""})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 0)

    # —— 正确性条件（大厅里的单必须真的没人接）——
    def test_claimed_order_leaves_the_hall(self):
        """⚠️ 这条是**正确性条件**，不是过滤器：大厅里的单必须真的没人接。

        起点刻意是**脏态**（不变量被破坏的形状）：订单在 `WAITING_GRAB`，
        绩效行却挂着接单人。D-0032 之下生产不可达（池内 ⇒ 无接单人），
        但**条件不成立时大厅必须少这张单**，否则抢单的条件更新会 `rowcount == 0`，
        与「已被别人接走」无法区分（spec §7.4）。
        它同时兜住「绩效行缺失」的脏数据——那种单不该出现在大厅。
        """
        taker = User.objects.create_user(username="taker-dirty")
        self.claimed.status = OrderStatus.WAITING_GRAB
        self.claimed.save(update_fields=["status"])
        Performance.objects.filter(order=self.claimed).update(user=taker)
        # 前置哨兵：脏态**真的**被造出来了（订单在池内 + 绩效行有人）
        self.claimed.refresh_from_db()
        self.assertEqual(self.claimed.status, OrderStatus.WAITING_GRAB)
        self.assertEqual(Performance.objects.get(order=self.claimed).user_id, taker.id)
        nos = self._nos()
        self.assertNotIn(self.claimed.order_no, nos)
        self.assertEqual(nos, [self.in_hall.order_no])

    def test_order_without_performance_never_shows_in_hall(self):
        """孤儿订单（没有绩效行）不得出现在大厅——否则抢单会 rowcount == 0，
        与「已被别人接走」无法区分（spec §7.4）。

        这是刻意保留的**响亮 500 的金丝雀**的反面：孤儿单不该存在，
        但真存在时大厅必须看不见它。
        """
        orphan = Order.objects.create(
            game=self.game, order_no="20260920000000000009",
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("50.00"),
        )
        # 前置哨兵：它**真的**是一条池内孤儿单（状态对 + 一条绩效行都没有）——
        # 否则「没出现在大厅」与「夹具根本没造成孤儿单」不可区分（判别力判据 1）。
        # 状态从**库里读回**，不是回读构造参数；判据打在**这条具体的单**上，不用 count()。
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, OrderStatus.WAITING_GRAB)
        self.assertFalse(Performance.objects.filter(order=orphan).exists())
        body = self._body()
        nos = [row["order_no"] for row in body["data"]["items"]]
        self.assertNotIn(orphan.order_no, nos)
        self.assertEqual(nos, [self.in_hall.order_no])

    def test_pool_entry_returns_the_order_to_the_hall(self):
        """**D-0032 的兑现**（端到端）：有人接的问题单退回「待抢单」后必须真的进大厅。

        决策记录：`.agents/notes/implemented/architecture/2026-09-20-pool-states-mean-no-taker.md`
        ——「未分配 / 待抢单」的语义就是**「没人接」**。

        判别力：原稿上半句「把 `performance.py`
        里 `WAITING_GRAB` 那条钩子注册去掉 ⇒ 本用例变红」**已失效**——D2 之后抛单那一步
        到达时接单人已被 `clear-taker` 那一步清掉，删掉那条注册本用例**照样绿**
        ，它的判别力**已移交** `tests/test_roles_and_intake_perf.py`
        的 `bonus_amount` 那半（删注册 ⇒ 那一列不回退 ⇒ 那条用例红）。
        原稿下半句的「大厅那侧放宽 `user__isnull` ⇒ 本用例不再有区分力」仍然成立。
        本用例今天真正承重的是**端到端可达**与那三列绩效回退：把 `clear-taker` 那一步的
        清人删掉 ⇒ 立刻红（下一步抛单会拿到 1002）。

        ⭐ D2 之后「**直接**把有接手人的单
        改成待抢单」已被拒（`/transition/` / `/throw-to-hall/` 都不在例外清单里，
        见 spec §4.1.2）⇒ 「退回大厅」这个动作的产品路径变成**两步**：
        ① `/clear-taker/`（例外①：显式清除接单人，落到 `UNASSIGNED`，绩效回退）
        ② `/throw-to-hall/`（此刻已无接手人 ⇒ 放行，落到 `WAITING_GRAB` 进大厅）。
        ⚠️ 如实登记：因此 ① 的那半判据（「有人接也照样回池」）不再由本用例钉——
        它由 `test_dispatch_api.TakerPresentPoolEntryGuardTests` 的例外① 用例钉；
        本用例钉的是**端到端可达**：清人之后这单**真的**出现在大厅里，
        而且「大厅要求 `user` 为空」这条判据仍然承重（把 `user__isnull` 放宽它不再变红，
        但把 `/clear-taker/` 的清人删掉它立刻红）。
        """
        order = self._order("20260920000000000010", OrderStatus.ISSUE)
        taker = User.objects.create_user(username="taker-issue")
        Performance.objects.filter(order=order).update(user=taker)
        # 前置哨兵：起点是「有人接的问题单」（非初始态），且此时**不在**大厅
        self.assertEqual(Performance.objects.get(order=order).user_id, taker.id)
        self.assertNotIn(order.order_no, self._nos())

        # ① 显式清除接单人（D2 例外①；本动作就是 `/clear-taker/` 的产品语义）
        cleared = self.client.post(
            f"/api/business/orders/{order.order_no}/clear-taker/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(cleared["code"], 0, cleared)
        self.assertNotIn(order.order_no, self._nos())          # 未分配 ≠ 待抢单，大厅仍看不见

        # ② 抛到大厅（此刻无接手人 ⇒ 放行）
        body = self.client.post(
            f"/api/business/orders/{order.order_no}/throw-to-hall/", {},
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(Order.objects.get(pk=order.pk).status, OrderStatus.WAITING_GRAB)
        # 三元组回退（spec §4.1.2）：不只是 user
        perf = Performance.objects.get(order=order)
        self.assertIsNone(perf.user_id)
        self.assertIsNone(perf.review_status)
        self.assertEqual(perf.bonus_amount, Decimal("0.00"))
        self.assertIn(order.order_no, self._nos())





    def test_hidden_orders_never_show_in_the_hall(self):
        """⭐ 评审 Important 7：隐藏单即使出现 MAIN 行也不许进大厅。

        防御性用例：生产上隐藏单没有 MAIN 行（大厅本来就 join 不上），但显式
        `exclude(hall_card__isnull=False)` 才是承重的那条 —— 去掉它本用例必红。
        """
        from tests.hall_shadow_helpers import make_hidden_order, make_mirror

        card = make_mirror(game=self.game, bound_by=self.viewer, no="SYN-SHADOW-HALL")
        hidden = make_hidden_order(
            game=self.game, card=card, order_no="20261026000000000031",
            with_main=True,
        )
        nos = self._nos()
        self.assertIn(self.in_hall.order_no, nos)          # 正对照
        self.assertNotIn(hidden.order_no, nos)


# ── D-0296：灰区 / 优选 / 模拟单 ─────────────────────────────────


def _sim_payload(no, game_id, price):
    """模拟卡的**全形状** payload（照生成侧 `hall_simulated._payload_for` 的键表）。"""
    from apps.business.jidanpu_common import REMOTE_ROW_BASE

    data = dict(REMOTE_ROW_BASE)
    data.update(
        {
            "order_no": no, "game": game_id, "status": "WAITING_GRAB",
            "hall_price": price, "origin": "simulated", "is_preferred": True,
            "customer_phone": "", "customer_ww": "", "game_role_name": "",
        }
    )
    return data


class HallMetaKeysTests(HallApiTests):
    """四类行（原生 / 镜像 / 模拟单 / 灰行）的**加法键**与「不传参数行为不变」。"""

    def _upstream(self, order_id, *, first_seen_at, rec_fee="66.00"):
        """一条镜像行（照 test_jidanpu_upstream_hall 的同款夹具）。"""
        from apps.business.models import JidanpuUpstreamBinding, JidanpuUpstreamHallOrder

        owner = User.objects.create_user(username="meta-up-owner")
        binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url="https://meta-hall.test.invalid",
            account="acc-meta", password="pw-canary-meta", enabled=True,
        )
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=binding.jidanpu_base_url, upstream_order_id=order_id,
            owner_binding=binding, status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game, project_text="镜像合成项目", remote_rec_fee=Decimal(rec_fee),
            upstream_payload={"orderId": order_id, "orderIdView": f"V-{order_id}",
                              "recFee": rec_fee},
            first_seen_at=first_seen_at,
        )

    def _sim(self, no, *, generated_at=None, price="150.00"):
        from django.utils import timezone as tz

        from apps.business.models import HallSimulatedOrder

        return HallSimulatedOrder.objects.create(
            game=self.game, display_order_no=no,
            project_text="模拟卡项目", price=Decimal(price),
            status=HallSimulatedOrder.Status.ACTIVE,
            generated_at=generated_at or tz.now(),
            payload=_sim_payload(no, self.game.pk, price),
        )

    def _snap(self, order, *, exit_kind="claimed", exited_at=None):
        """灰卡走**真实写点**（payload 与线上一字不差）。"""
        from apps.business import hall_snapshot

        return hall_snapshot.record_native_exit(order, exit_kind=exit_kind, at=exited_at)

    def test_rows_without_params_keep_the_old_shape_plus_the_new_keys(self):
        """不传参数：行数 / 顺序不变；行上**多** `is_preferred` / `is_exited` /
        `exit_kind` / `sim_pk` 四键（值 = 可接行的基线）。

        变异方向：把新键做成「只在开关打开时才出现」⇒ 本用例红（新键是纯加法、恒在）。
        """
        rows = self._body()["data"]["items"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for key in ("is_preferred", "is_exited", "exit_kind", "sim_pk"):
            self.assertIn(key, row)
        self.assertIs(row["is_exited"], False)
        self.assertIsNone(row["exit_kind"])
        self.assertIsNone(row["sim_pk"])
        # is_preferred = 原始价判（88 ≤ 200 ⇒ False；换一张 250 的 ⇒ True）。
        self.assertIs(row["is_preferred"], False)
        pricey = self._order("20260920000000000030", OrderStatus.WAITING_GRAB, price="250.00")
        rows = {r["order_no"]: r for r in self._body()["data"]["items"]}
        self.assertIs(rows[pricey.order_no]["is_preferred"], True)

    def test_all_four_row_kinds_carry_the_three_meta_keys(self):
        """三新键（is_preferred / is_exited / exit_kind）在原生 / 镜像 / 模拟单 / 灰行上都存在。

        ⚠️ 走 **AppToken 面**：模拟单只在移动端可见（晚间规则），会话面没有。
        """
        from django.utils import timezone as tz

        self._upstream("20260928000000003001", first_seen_at=tz.now())
        sim = self._sim("20260920000000000031")
        exited = self._order("20260920000000000032", OrderStatus.IN_PROGRESS)
        self._snap(exited)
        rows = self._app_body(self.viewer, {"include_exited": "1"})["data"]["items"]
        by_origin = {}
        for row in rows:
            by_origin.setdefault(row["origin"], row)
        self.assertEqual(
            set(by_origin), {"native", "upstream", "simulated"},
            f"四类行里少了哪一路：{sorted(by_origin)}",
        )
        # 灰行 = is_exited=True 的原生行（快照存的是原生在厅视图）。
        gray = [r for r in rows if r["is_exited"]]
        self.assertEqual(len(gray), 1)
        self.assertEqual(gray[0]["origin"], "native")
        self.assertEqual(gray[0]["exit_kind"], "claimed")
        for origin, row in by_origin.items():
            for key in ("is_preferred", "is_exited", "exit_kind"):
                self.assertIn(key, row, f"{origin} 行缺 {key}")
        # 模拟单行的身份键与恒优选。
        self.assertEqual(by_origin["simulated"]["sim_pk"], sim.pk)
        self.assertIs(by_origin["simulated"]["is_preferred"], True)

    def test_simulated_rows_sort_into_the_available_section_by_generated_at(self):
        """模拟单按 `generated_at` 与原生 / 镜像**归并倒序**（可接区一并排序）。⚠️ 走 AppToken 面。"""
        from datetime import timedelta

        from django.utils import timezone as tz

        base = tz.now()
        old_native = self._order("20260920000000000033", OrderStatus.WAITING_GRAB)
        Order.objects.filter(pk=old_native.pk).update(created_at=base - timedelta(minutes=30))
        self._sim("20260920000000000034", generated_at=base - timedelta(minutes=20))
        self._upstream("20260928000000003002", first_seen_at=base - timedelta(minutes=10))
        fresh_native = self._order("20260920000000000035", OrderStatus.WAITING_GRAB)
        Order.objects.filter(pk=fresh_native.pk).update(created_at=base)
        excluded = {self.in_hall.order_no}
        nos = [
            r["order_no"] for r in self._app_body(self.viewer)["data"]["items"]
            if r["order_no"] not in excluded and r["origin"] != "upstream"
        ]
        self.assertEqual(
            nos,
            [
                fresh_native.order_no,           # 最新
                "20260920000000000034",          # 模拟卡（generated_at 居中）
                old_native.order_no,             # 最老
            ],
        )


class HallSimulatedVisibilityTests(HallMetaKeysTests):
    """模拟单可见性（2026-10-24 业务方裁定两道；⭐ 2026-10-29 补第三道，D-0328）：

    ① **只在移动端**：模拟单只对 AppToken 令牌（打手 App / 小程序）的请求可见，
    PC / H5 会话面（`self.client`）**永远没有**——某租户现场：PC 内部抢单大厅
    不该出现演示卡；② **没开个人免押**：开了免押的老打手在 App 上也不见；
    ③ **当前没有任何冻结押金**：钱包里还冻着押金（`TakerWallet.frozen > 0`）
    的打手已经过了新人期，在 App 上也不见（业务方确认补上；D-0296
    当时漏登记）——本腿只收窄「交了押金但没开免押」的人群。
    真实订单的灰卡快照**不受**这三道影响（那是真实历史，两端都照在）。
    """

    def _viewer_quota(self, on: bool) -> None:
        from apps.accounts.models import StaffProfile

        StaffProfile.objects.filter(user=self.viewer).update(deposit_quota_enabled=on)

    def _viewer_wallet(self, *, frozen: str) -> None:
        from apps.accounts.models import TakerWallet

        TakerWallet.objects.create(user=self.viewer, frozen=Decimal(frozen))

    def _sims(self, rows: list) -> list:
        return [r for r in rows if r.get("origin") == "simulated"]

    def _fixture(self):
        from django.utils import timezone as tz

        from apps.business.models import HallSimulatedOrder

        sim_active = self._sim("SIM-VIS-01")
        sim_retired = self._sim("SIM-VIS-02")
        sim_retired.status = HallSimulatedOrder.Status.RETIRED
        sim_retired.retired_at = tz.now()
        sim_retired.save(update_fields=["status", "retired_at"])
        self._snap(self.in_hall, exit_kind="claimed")
        return sim_active, sim_retired

    def test_session_viewers_never_see_simulated_rows(self):
        """① PC / H5 会话面：**无论免押开关**都没有模拟单行（模拟单不上 PC）。"""
        sim_active, sim_retired = self._fixture()
        del sim_active, sim_retired
        self._viewer_quota(False)  # 免押关着也看不见——「只在移动端」是独立判据。
        rows = self._rows("?include_exited=1")
        self.assertEqual(self._sims(rows), [], "会话面（PC/H5）出现了模拟单")
        # 真实灰卡照在（对照自证：请求本身是通的、灰区不是被整段藏掉）。
        greys = [r for r in rows if r.get("is_exited")]
        self.assertTrue(
            any(r["order_no"] == self.in_hall.order_no for r in greys),
            "真实灰卡消失 ⇒ 请求本身坏了，这条用例失效",
        )

    def test_app_viewer_without_quota_sees_simulated_rows(self):
        """② App 面 + 免押关 ⇒ 在厅与灰区的模拟单都可见。"""
        from django.utils import timezone as tz

        from apps.business.models import HallSimulatedOrder

        sim_active, sim_retired = self._fixture()
        sim_retired = HallSimulatedOrder.objects.get(pk=sim_retired.pk)
        self._viewer_quota(False)
        rows = self._app_rows("?include_exited=1")
        sims = {r["order_no"] for r in self._sims(rows)}
        self.assertEqual(
            sims, {sim_active.display_order_no, sim_retired.display_order_no}
        )

    def test_app_viewer_with_quota_on_sees_no_simulated_rows(self):
        """③ App 面 + 免押开 ⇒ 模拟单消失（在厅 + 灰区），真实灰卡照在。"""
        from apps.business.models import HallSimulatedOrder

        self._fixture()
        self._viewer_quota(True)
        rows = self._app_rows("?include_exited=1")
        self.assertEqual(self._sims(rows), [], "免押查看者在 App 上还能看到模拟单")
        greys = [r for r in rows if r.get("is_exited")]
        self.assertTrue(
            any(r.get("sim_pk") is None and r["order_no"] == self.in_hall.order_no
                for r in greys),
            "真实订单的灰卡被误藏",
        )

    def test_app_viewer_with_frozen_deposit_sees_no_simulated_rows(self):
        """⭐ ④（D-0328 第三道）App 面 + 免押关 + **冻结押金 > 0** ⇒ 模拟单消失。

        在厅段与灰区 retired 段**同一道判据**（两段一起断）；真实灰卡照在（对照）。
        变异方向：把 `viewer_sees_simulated`（`apps/business/hall_simulated.py`，
        读/写两侧共用的那份判据）的 `TakerWallet.frozen` 那道删掉
        ⇒ 本条红（交了押金的查看者还能看到在厅与灰区的演示卡）。
        """
        from apps.business.models import HallSimulatedOrder

        sim_active, sim_retired = self._fixture()
        sim_retired = HallSimulatedOrder.objects.get(pk=sim_retired.pk)
        self._viewer_quota(False)
        self._viewer_wallet(frozen="50.00")
        rows = self._app_rows("?include_exited=1")
        self.assertEqual(
            self._sims(rows), [],
            "冻结押金 > 0 的查看者在 App 上还能看到模拟单（在厅/灰区都该没有）",
        )
        # 真实灰卡照在：证明请求本身是通的、消失的只是模拟单那一族。
        greys = [r for r in rows if r.get("is_exited")]
        self.assertTrue(
            any(r.get("sim_pk") is None and r["order_no"] == self.in_hall.order_no
                for r in greys),
            "真实订单的灰卡被误藏",
        )
        del sim_active, sim_retired

    def test_a_wallet_with_zero_frozen_keeps_the_rows_visible(self):
        """⭐ ④ 的 D-0015 边界：钱包行存在但 `frozen = 0` ⇒ **仍可见**。

        「交了押金」指的是**当下还冻着**：冻过又全解冻（frozen 归 0）的人回到
        「没有冻结押金」一族。没有钱包行（既有用例 ② 的形状）与 frozen=0 同判。
        """
        from apps.business.models import HallSimulatedOrder

        sim_active, sim_retired = self._fixture()
        sim_retired = HallSimulatedOrder.objects.get(pk=sim_retired.pk)
        self._viewer_quota(False)
        self._viewer_wallet(frozen="0.00")
        rows = self._app_rows("?include_exited=1")
        sims = {r["order_no"] for r in self._sims(rows)}
        self.assertEqual(
            sims, {sim_active.display_order_no, sim_retired.display_order_no}
        )

    def _app_rows(self, params: str) -> list:
        body = self._app_body(self.viewer, {"include_exited": "1"})
        return body["data"]["items"]

    def _rows(self, params: str) -> list:
        body = self.client.get(self.url + params).json()
        self.assertEqual(body["code"], 0, body)
        # ⚠️ 大厅分页信封的行键是 **`items`**（不是 `results`，见既有灰区用例）。
        return (body.get("data") or {}).get("items") or []


class HallExitedSectionTests(HallApiTests):
    """`include_exited=1`：灰区排在可接区之后、按离厅时刻倒序、只吃 game + scope。"""

    def _snap(self, order, *, exit_kind, exited_at):
        from apps.business import hall_snapshot

        return hall_snapshot.record_native_exit(order, exit_kind=exit_kind, at=exited_at)

    def test_exited_rows_come_after_available_sorted_by_exit_time_desc(self):
        """灰区**排在整个可接区之后**、按离厅时刻倒序（两张灰卡可分辨先后）。"""
        from apps.business.claim import claim_order
        from apps.business.recycle import soft_delete_order

        taker = User.objects.create_user(username="exited-taker")
        StaffProfile.objects.create(user=taker).games.set([self.game])
        first_gone = self._order("20260920000000000040", OrderStatus.WAITING_GRAB)
        claim_order(user_id=taker.pk, order_id=first_gone.pk, nodes=HALL_NODES)
        second_gone = self._order("20260920000000000041", OrderStatus.WAITING_GRAB)
        # 软删那条写点造第二张灰卡（时刻更晚）。
        soft_delete_order(order=second_gone, actor=taker, is_del_emp="N")
        # 一张仍在厅的单：可接区的哨兵。
        self._order("20260920000000000042", OrderStatus.WAITING_GRAB)
        rows = self._body({"include_exited": "1"})["data"]["items"]
        available = [r for r in rows if not r["is_exited"]]
        gray = [r for r in rows if r["is_exited"]]
        self.assertEqual(
            {r["order_no"] for r in available},
            {self.in_hall.order_no, "20260920000000000042"},
        )
        self.assertEqual(len(gray), 2)
        self.assertEqual(
            [r["order_no"] for r in gray],
            [second_gone.order_no, first_gone.order_no],
            "灰区没有按离厅时刻倒序",
        )
        self.assertEqual([r["exit_kind"] for r in gray], ["withdrawn", "claimed"])
        # 灰区在列表尾部（无论灰卡离厅多晚）。
        self.assertEqual(rows[-2:], gray)

    def test_gray_section_respects_the_game_scope_and_game_filter(self):
        """灰区只吃 `game` 筛选与游戏权限 scope（别的游戏的灰卡不出现）。"""
        from apps.business.models import HallExitSnapshot

        other_game = Game.objects.create(name="灰区别的游戏")
        foreign = Order.objects.create(
            game=other_game, order_no="20260920000000000043",
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("88.00"),
        )
        Performance.objects.create(order=foreign, emp_type=EmpType.MAIN)
        HallExitSnapshot.objects.create(
            order=foreign, mirror=None, source=HallExitSnapshot.Source.NATIVE,
            exit_kind=HallExitSnapshot.ExitKind.CLAIMED, game=other_game,
            payload={"order_no": foreign.order_no, "hall_price": "88.00"},
        )
        own_gone = self._order("20260920000000000044", OrderStatus.IN_PROGRESS)
        self._snap(own_gone, exit_kind="claimed",
                   exited_at=self._now())
        # 本账号没有 other_game 的权限 ⇒ 别的游戏的灰卡看不见。
        rows = self._body({"include_exited": "1"})["data"]["items"]
        gray = [r for r in rows if r["is_exited"]]
        self.assertEqual([r["order_no"] for r in gray], [own_gone.order_no])
        # game 筛选同样作用灰区：筛别的游戏 id ⇒ 灰区也空。
        rows = self._body({"include_exited": "1", "game": str(other_game.id)})["data"]["items"]
        self.assertEqual([r for r in rows if r["is_exited"]], [])

    @staticmethod
    def _now():
        from django.utils import timezone as tz

        return tz.now()

    def test_gray_rows_are_price_redacted_for_restricted_viewers(self):
        """灰行价格按**请求者**脱敏：持 `order.restrict_view_offer_price` 的账号读 `hall_price=None`。

        变异方向：把灰行装配处的脱敏删掉 ⇒ 本用例红（受限用户看到了原始价）。
        ⚠️ `is_preferred` 不受脱敏影响（判的是快照存的原始价）。
        ⚠️ 受限账号用**另一个组名**：节点挂的是「组 → 角色」，与前一个账号共用
        「代练员」会把 flag 带给对照账号（本文件既有的同一条教训）。
        """
        from apps.business.models import HallExitSnapshot

        gone = self._order("20260920000000000045", OrderStatus.IN_PROGRESS)
        HallExitSnapshot.objects.create(
            order=gone, mirror=None, source=HallExitSnapshot.Source.NATIVE,
            exit_kind=HallExitSnapshot.ExitKind.CLAIMED, game=gone.game,
            payload={
                "order_no": gone.order_no, "hall_price": "250.00",
                "status": "WAITING_GRAB", "taker_name": None, "origin": "native",
            },
        )
        restricted = make_logged_in_client(
            username="gray-restricted", groups=("受限灰卡组",), games=[self.game],
            nodes=HALL_NODES + ("order.restrict_view_offer_price",),
        )
        rows = restricted.get(
            self.url, {"include_exited": "1"}
        ).json()["data"]["items"]
        gray = [r for r in rows if r["is_exited"]]
        self.assertEqual(len(gray), 1)
        self.assertIsNone(gray[0]["hall_price"])
        self.assertIs(gray[0]["is_preferred"], True)  # 判价打在原始价上，脱敏不改判
        # 不受限的账号照常看到价（对照）。
        rows = self._body({"include_exited": "1"})["data"]["items"]
        gray = [r for r in rows if r["is_exited"]]
        self.assertEqual(Decimal(gray[0]["hall_price"]), Decimal("250.00"))


class HallPreferredFilterTests(HallApiTests):
    """`preferred_only=1`：只过滤可接区，灰区不受影响。"""

    def _sim(self, no, *, price, generated_at=None):
        from django.utils import timezone as tz

        from apps.business.models import HallSimulatedOrder

        return HallSimulatedOrder.objects.create(
            game=self.game, display_order_no=no,
            project_text="优选模拟卡", price=Decimal(price),
            status=HallSimulatedOrder.Status.ACTIVE,
            generated_at=generated_at or tz.now(),
            payload=_sim_payload(no, self.game.pk, price),
        )

    def _snap(self, order):
        from apps.business import hall_snapshot

        return hall_snapshot.record_native_exit(
            order, exit_kind=hall_snapshot.HallExitSnapshot.ExitKind.WITHDRAWN
        )

    def test_preferred_only_keeps_only_preferred_available_rows(self):
        """优选开关：可接区只留原始价 > 200 的行（原生 + 模拟单恒优选）。⚠️ 走 AppToken 面（模拟单只在移动端）。"""
        cheap = self._order("20260920000000000050", OrderStatus.WAITING_GRAB, price="100.00")
        pricey = self._order("20260920000000000051", OrderStatus.WAITING_GRAB, price="250.00")
        sim = self._sim("20260920000000000052", price="55.00")
        rows = self._app_body(self.viewer, {"preferred_only": "1", "game": str(self.game.pk)})["data"]["items"]
        nos = {r["order_no"] for r in rows}
        self.assertIn(pricey.order_no, nos)
        self.assertNotIn(cheap.order_no, nos, "非优选的可接行被留下了")
        self.assertIn(sim.display_order_no, nos, "模拟单恒优选，不该被滤掉")
        for row in rows:
            self.assertIs(row["is_preferred"], True)

    def test_preferred_only_does_not_touch_the_gray_section(self):
        """灰区不受优选开关影响（灰卡是历史事实：非优选的单也能进灰区）。"""
        gone_cheap = self._order("20260920000000000053", OrderStatus.IN_PROGRESS)
        self._snap(gone_cheap)
        self._order("20260920000000000054", OrderStatus.WAITING_GRAB, price="100.00")
        rows = self._body(
            {"preferred_only": "1", "include_exited": "1", "game": str(self.game.pk)}
        )["data"]["items"]
        gray = [r for r in rows if r["is_exited"]]
        self.assertEqual([r["order_no"] for r in gray], [gone_cheap.order_no])
        self.assertIs(gray[0]["is_preferred"], False)  # 灰行按自己 payload 的价判
        # 可接区那份只剩优选（对照：同一请求里的可接行全 True）。
        for row in rows:
            if not row["is_exited"]:
                self.assertIs(row["is_preferred"], True)


class HallPreferredSwitchTests(HallMetaKeysTests):
    """优选**租户开关** `hall_preferred_orders`（D-0328，2026-10-29）——**读口**侧。

    关（N）⇒ 本读口**所有行** `is_preferred` 恒 False（四路可接行 + 灰行）：
    优选角标消失、「只看优选」筛选自然筛空。四路各一条（原生 / 鸡蛋铺镜像 /
    团团通镜像 / 模拟单）+ 灰行一条 + 显式 Y 行的回归钉。
    ⚠️ 无覆盖行 = 默认 Y ⇒ 既有 `HallPreferredFilterTests` / `HallMetaKeysTests`
    全绿本身就是「开 ⇒ 行为逐字不变」的回归网。
    每条的变异方向写在 docstring 里。
    """

    SWITCH_KEY = "hall_preferred_orders"

    def _turn(self, value: str) -> None:
        from apps.business.models import SystemSwitch

        SystemSwitch.objects.create(key=self.SWITCH_KEY, value=value)

    def _peer_mirror(self, no, *, price="250.00"):
        """一张团团通镜像行（照 test_peer_hall_read.ReadFixture 的夹具形状）。"""
        from django.utils import timezone as tz

        from apps.business.models import PeerHallMirror, PeerUpstreamBinding
        from apps.business.peer_sync import hash_token, new_token
        from tests.test_peer_hall_read import peer_payload

        operator = User.objects.create_user(username="pref-switch-peer-op")
        binding = PeerUpstreamBinding.objects.create(
            peer_name="优选开关测试上家",
            peer_base_url="https://peer-preferred-switch.test.invalid",
            peer_account="pref-switch-peer",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-preferred-local.test.invalid",
            bound_by=operator,
            hall_sync_enabled=True,
        )
        return PeerHallMirror.objects.create(
            peer_upstream_binding=binding,
            peer_base_url=binding.peer_base_url,
            peer_order_no=no,
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            payload=peer_payload(no, price=price),
            remote_hall_price=Decimal(price),
            first_seen_at=tz.now(),
        )

    def test_tied_available_and_retired_cards_keep_legacy_order_across_pages(self):
        from apps.business.models import HallSimulatedOrder, PeerHallMirror

        moment = tz_now()
        Order.objects.filter(pk=self.in_hall.pk).update(created_at=moment)
        upstream = self._upstream("20260928000000003100", first_seen_at=moment)
        active = self._sim("TIED-ACTIVE-SIM", generated_at=moment)
        peer = self._peer_mirror("TIED-PEER")
        PeerHallMirror.objects.filter(pk=peer.pk).update(first_seen_at=moment)
        self._snap(self.in_hall, exited_at=moment)
        retired_legacy = self._sim("TIED-LEGACY-RETIRED", generated_at=moment)
        retired_recorded = self._sim("TIED-RCD-RETIRED", generated_at=moment)
        HallSimulatedOrder.objects.filter(pk=retired_legacy.pk).update(
            status=HallSimulatedOrder.Status.RETIRED,
        )
        HallSimulatedOrder.objects.filter(pk=retired_recorded.pk).update(
            status=HallSimulatedOrder.Status.RETIRED, retired_at=moment,
        )

        pages = [
            self._app_body(self.viewer, {"include_exited": "1", "page": page, "page_size": 2})["data"]
            for page in range(1, 5)
        ]
        self.assertEqual([page["total"] for page in pages], [7, 7, 7, 7])
        self.assertEqual(
            [
                [(item["origin"], item["is_exited"], item.get("remote_order_id") or item["order_no"])
                 for item in page["items"]]
                for page in pages
            ],
            [
                [("native", False, self.in_hall.order_no), ("upstream", False, upstream.upstream_order_id)],
                [("simulated", False, active.display_order_no), ("peer", False, peer.peer_order_no)],
                [("native", True, self.in_hall.order_no), ("simulated", True, retired_legacy.display_order_no)],
                [("simulated", True, retired_recorded.display_order_no)],
            ],
        )

    # ── 四路读口各一条（关 ⇒ 那一路恒 False）─────────────────────────

    def test_switch_off_natives_are_never_preferred(self):
        """原生路：开关关 ⇒ 抛单价 250 的在厅行 `is_preferred=False`。

        变异方向：把大厅读口的整批 strip 删掉 ⇒ 本条红（行上仍 True）。
        """
        pricey = self._order("20260920000000000060", OrderStatus.WAITING_GRAB, price="250.00")
        self._turn("N")
        rows = {r["order_no"]: r for r in self._body()["data"]["items"]}
        self.assertIs(rows[pricey.order_no]["is_preferred"], False)

    def test_switch_off_upstream_mirrors_are_never_preferred(self):
        """鸡蛋铺镜像路：开关关 ⇒ 勾兑展示价 250 的镜像行 `is_preferred=False`。"""
        from django.utils import timezone as tz

        self._upstream("20260928000000003010", first_seen_at=tz.now(), rec_fee="250.00")
        self._turn("N")
        rows = [r for r in self._body()["data"]["items"] if r["origin"] == "upstream"]
        self.assertEqual(len(rows), 1, "镜像行没读出来（夹具坏了）")
        self.assertIs(rows[0]["is_preferred"], False)

    def test_switch_off_peer_mirrors_are_never_preferred(self):
        """团团通镜像路：开关关 ⇒ 展示价 250 的 peer 行 `is_preferred=False`。"""
        self._peer_mirror("PREF-SWITCH-PEER-1", price="250.00")
        self._turn("N")
        rows = [r for r in self._body()["data"]["items"] if r["origin"] == "peer"]
        self.assertEqual(len(rows), 1, "peer 行没读出来（夹具坏了）")
        self.assertIs(rows[0]["is_preferred"], False)

    def test_switch_off_simulated_cards_are_never_preferred(self):
        """模拟单路：开关关 ⇒ 模拟卡 `is_preferred=False`（生成侧 payload 里的 True
        不改——读侧重申，与本读口同一道 strip；⚠️ 模拟单只在移动端可见，走 AppToken 面）。
        """
        from apps.business.models import HallSimulatedOrder

        sim = self._sim("PREF-SWITCH-SIM-1")
        sim_retired = self._sim("PREF-SWITCH-SIM-2")
        sim_retired.status = HallSimulatedOrder.Status.RETIRED
        sim_retired.retired_at = tz_now()
        sim_retired.save(update_fields=["status", "retired_at"])
        self._turn("N")
        rows = self._app_body(self.viewer, {"include_exited": "1"})["data"]["items"]
        sims = [r for r in rows if r.get("origin") == "simulated"]
        self.assertEqual(
            {r["order_no"] for r in sims},
            {sim.display_order_no, sim_retired.display_order_no},
            "模拟单在厅/灰区两段都该还在（开关只管优选标记）",
        )
        self.assertTrue(sims, "模拟单行没读出来（夹具坏了）")
        for row in sims:
            self.assertIs(row["is_preferred"], False)

    # ── 伴随断言 ───────────────────────────────────────────────────

    def test_switch_off_empties_the_preferred_only_filter(self):
        """开关关 ⇒ `preferred_only=1` 筛空（行全 False ⇒ 自然被滤掉），其余行照在。"""
        pricey = self._order("20260920000000000061", OrderStatus.WAITING_GRAB, price="250.00")
        cheap = self._order("20260920000000000062", OrderStatus.WAITING_GRAB, price="100.00")
        self._turn("N")
        rows = self._body({"preferred_only": "1"})["data"]["items"]
        self.assertEqual(rows, [], "开关关着「只看优选」还有行")
        nos = {r["order_no"] for r in self._body()["data"]["items"]}
        self.assertEqual(
            nos, {self.in_hall.order_no, pricey.order_no, cheap.order_no},
            "不带筛选的读口该照常出全部在厅行（含 setUp 那张 88 的）",
        )

    def test_switch_off_strips_the_gray_rows_too(self):
        """开关关 ⇒ 灰行（真实订单快照，原价 250）也 `is_preferred=False`。"""
        gone = self._order("20260920000000000063", OrderStatus.IN_PROGRESS, price="250.00")
        self._snap(gone, exit_kind="claimed")
        self._turn("N")
        rows = self._body({"include_exited": "1"})["data"]["items"]
        gray = [r for r in rows if r["is_exited"]]
        self.assertEqual(len(gray), 1, "灰卡没读出来（夹具坏了）")
        self.assertIs(gray[0]["is_preferred"], False)

    def test_explicit_on_row_keeps_the_behavior_unchanged(self):
        """显式登记 `Y` 行 ⇒ 与无覆盖行逐字同行为（回归钉；250 行 True + 筛选可用）。"""
        pricey = self._order("20260920000000000064", OrderStatus.WAITING_GRAB, price="250.00")
        self._turn("Y")
        rows = {r["order_no"]: r for r in self._body()["data"]["items"]}
        self.assertIs(rows[pricey.order_no]["is_preferred"], True)
        nos = {r["order_no"] for r in self._body({"preferred_only": "1"})["data"]["items"]}
        self.assertEqual(nos, {pricey.order_no})


def tz_now():
    from django.utils import timezone

    return timezone.now()
