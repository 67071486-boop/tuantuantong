"""D-0303 团团通大厅镜像：**读面**（内部大厅 + 打手 API 大厅）与铺底命令（spec §7 / §2.3）。

判据（每条都能被单独弄红）：

① **键表恰好同形**：`serialize_peer_row` 的行键与鸡蛋铺镜像行**恰好同键**
   （`MERGED_ROW_KEYS`，前端一张表渲染四路行的前提；⛔ 不新增不删减键，spec §7）；
   takerapi 对外行 = **恰好 13 键**（11 白名单 + `origin` + `mirror_pk`，与鸡蛋铺镜像行同形）。
② **并列投影**：peer `listed` 行出现在两处读口（`origin="peer"` 可分辨）；
   游戏权限过滤与内部单同口径；**鸡蛋铺那一路不受影响**（正对照）。
③ **可见与可接同一道判据**（E-0288 同口径）：`gone` / `claiming` 行、归属绑定停用的行
   都不出现。
④ **铺底命令**：dry-run 零写入；`--execute` 每条在厅单 × 每个合格绑定各一行
   `hall.throw`；关推送的绑定不推。

⚠️ 夹具值全部合成值；断言打数据库行 / 响应载荷（E-0024）。
"""
import io
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    EmpType,
    Game,
    HallExitSnapshot,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
    PeerHallMirror,
    PeerSyncEvent,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_hall import serialize_peer_row
from apps.business.peer_sync import HALL_THROW_KIND, hash_token, new_token
from apps.core.datetime_display import local_iso
from tests.p5_auth_helpers import make_logged_in_client

UP_BASE = "https://peer-hall-read-up.test.invalid"

#: takerapi 对外镜像行的**恰好 13 键**（11 白名单 + 行身份两键；与
#: `tests/test_takerapi_hall_api.py` 的 HALL_ROW_KEYS ∪ {origin, mirror_pk} 同形，
#: ⛔ 不从那边 import 派生——那是自指恒真，照该文件的口径另写字面量）。
PEER_ROW_KEYS = {
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
    "origin",
    "mirror_pk",
}


def peer_payload(no, price="50.00"):
    return {
        "order_no": no,
        "status": "WAITING_GRAB",
        "total_price": "100.00",
        "hall_price": price,
        "remark": "syn-备注",
        "game_name": "读面夹具游戏",
        "server_id": None,
        "project_id": None,
        "project_text": "合成项目",
        "customer_phone": "",
        "customer_qq": "",
        "content": "syn-需求内容",
        "source_order_no": "",
    }


class ReadFixture(TenantTestCase):
    """下家视角夹具：一条上家绑定 + 在挂 / 已摘 / 停绑三档镜像行 + 一路鸡蛋铺行。"""

    def setUp(self):
        self.game = Game.objects.create(name="读面夹具游戏")
        self.other_game = Game.objects.create(name="读面无权游戏")
        from django.contrib.auth import get_user_model

        operator = get_user_model().objects.create_user(
            username="syn-peer-hall-read-op", password="pw-syn"
        )
        self.up_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家戊",
            peer_base_url=UP_BASE,
            peer_account="syn-up-read",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-local4.test.invalid",
            bound_by=operator,
            # ⭐ P1：接收侧开关**默认关**（关着 = 没有这条功能）⇒ 本夹具要测「开着」的
            #    正常读面，必须显式打开；「关着 ⇒ 不投影」由专门的用例翻回去。
            hall_sync_enabled=True,
        )
        self.listed = self._mirror("UP-READ-1", price="50.00")
        self.gone = self._mirror("UP-READ-2", price="60.00")
        PeerHallMirror.objects.filter(pk=self.gone.pk).update(
            status=PeerHallMirror.Status.GONE,
        )
        self.claiming = self._mirror("UP-READ-3", price="70.00")
        PeerHallMirror.objects.filter(pk=self.claiming.pk).update(
            status=PeerHallMirror.Status.CLAIMING,
        )
        # 归属绑定停用的那一条（E-0288 同口径：僵尸行在读口就地消失）。
        self.disabled_binding = PeerUpstreamBinding.objects.create(
            peer_name="测试上家己",
            peer_base_url="https://peer-hall-read-disabled.test.invalid",
            peer_account="syn-up-read-disabled",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            local_base_url="https://peer-hall-local5.test.invalid",
            bound_by=operator,
            enabled=False,
        )
        self.zombie = PeerHallMirror.objects.create(
            peer_upstream_binding=self.disabled_binding,
            peer_base_url=self.disabled_binding.peer_base_url,
            peer_order_no="UP-READ-4",
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            payload=peer_payload("UP-READ-4", price="80.00"),
            remote_hall_price=Decimal("80.00"),
            first_seen_at=self._now(),
        )
        # ── 鸡蛋铺那一路的正对照（同一款游戏、同样在挂）──
        self.jdp_user = get_user_model().objects.create_user(
            username="syn-peer-hall-read-jdp", password="pw-syn"
        )
        self.jdp_binding = JidanpuUpstreamBinding.objects.create(
            user=self.jdp_user,
            jidanpu_base_url="https://jdp-read.test.invalid",
            account="syn-jdp-read",
            password="syn-pw",
            enabled=True,
        )
        self.jdp_row = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=self.jdp_binding.jidanpu_base_url,
            upstream_order_id="JDP-READ-1",
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game,
            remote_rec_fee=Decimal("45.00"),
            upstream_payload={"orderIdView": "VIEW-JDP-1"},
            owner_binding=self.jdp_binding,
            first_seen_at=self._now(),
        )

    @staticmethod
    def _now():
        from django.utils import timezone

        return timezone.now()

    def _mirror(self, no, *, price="50.00"):
        return PeerHallMirror.objects.create(
            peer_upstream_binding=self.up_binding,
            peer_base_url=UP_BASE,
            peer_order_no=no,
            status=PeerHallMirror.Status.LISTED,
            game=self.game,
            payload=peer_payload(no, price=price),
            remote_hall_price=Decimal(price),
            first_seen_at=self._now(),
        )

    def _tie_listed_cards(self):
        moment = self._now()
        JidanpuUpstreamHallOrder.objects.filter(pk=self.jdp_row.pk).update(first_seen_at=moment)
        PeerHallMirror.objects.filter(pk=self.listed.pk).update(first_seen_at=moment)
        for number in ("20261031000000110001", "20261031000000110002"):
            order = Order.objects.create(
                order_no=number, game=self.game, status=OrderStatus.WAITING_GRAB,
                hall_price=Decimal("42.00"),
            )
            Performance.objects.create(order=order, emp_type=EmpType.MAIN)
            Order.objects.filter(pk=order.pk).update(created_at=moment)
        return moment


class SerializePeerRowTests(ReadFixture):
    """第 ① 层：行键表与取值。"""

    def test_row_keys_are_exactly_the_shared_hall_shape(self):
        """与鸡蛋铺镜像行**恰好同键**（判据 = 另一路的键常量，⛔ 不派生）。"""
        from tests.test_jidanpu_upstream_pull import MERGED_ROW_KEYS

        row = serialize_peer_row(PeerHallMirror.objects.get(pk=self.listed.pk))
        self.assertEqual(set(row), MERGED_ROW_KEYS)

    def test_row_values(self):
        row = serialize_peer_row(PeerHallMirror.objects.get(pk=self.listed.pk))
        self.assertEqual(row["origin"], "peer")
        self.assertEqual(row["remote_order_id"], "UP-READ-1")
        self.assertEqual(row["remote_order_view"], "UP-READ-1")
        self.assertEqual(row["mirror_pk"], self.listed.pk)
        self.assertEqual(row["game"], self.game.pk)
        self.assertEqual(row["project_text"], "合成项目")
        self.assertEqual(row["remark"], "syn-备注")
        # 价 = 上家发单价原样（v1 不打折）、两位小数字符串。
        self.assertEqual(row["hall_price"], "50.00")
        self.assertFalse(row["is_preferred"])
        self.assertEqual(row["status"], "WAITING_GRAB")
        # ⭐ B6 / L-12：`created_at` 是**项目时区**（`+08:00`，`local_iso`）——与原生行
        #    （DRF `DateTimeField`）和鸡蛋铺镜像行同一族；旧写法裸 datetime 被 DRF 渲染
        #    成 `+00:00`，同一份合并响应里两种偏移混排。
        self.assertEqual(row["created_at"], local_iso(self.listed.first_seen_at))
        self.assertTrue(row["created_at"].endswith("+08:00"))

    def test_no_price_row_keeps_none_and_is_not_preferred(self):
        """无价行：`hall_price=null`（⛔ 不伪装成 0.00）且非优选。"""
        PeerHallMirror.objects.filter(pk=self.listed.pk).update(remote_hall_price=None)
        row = serialize_peer_row(PeerHallMirror.objects.get(pk=self.listed.pk))
        self.assertIsNone(row["hall_price"])
        self.assertFalse(row["is_preferred"])

    def test_default_percent_is_hundred_and_keeps_the_upstream_price(self):
        """默认 100% ⇒ 展示价 = 上家发单价（与引入百分比之前**逐字同值**）。"""
        row = serialize_peer_row(PeerHallMirror.objects.get(pk=self.listed.pk))
        self.assertEqual(row["hall_price"], "50.00")

    def test_ninety_percent_scales_the_display_price(self):
        """⭐ P1：90% ⇒ 50.00 × 90% = **45.00**（读口投影按本绑定百分比算）。"""
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_price_percent=90
        )
        row = serialize_peer_row(PeerHallMirror.objects.get(pk=self.listed.pk))
        self.assertEqual(row["hall_price"], "45.00")

    def test_percent_rounds_half_up_to_two_decimals(self):
        """33.33 × 90% = 29.997 ⇒ **30.00**（`ROUND_HALF_UP`）。"""
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_price_percent=90
        )
        PeerHallMirror.objects.filter(pk=self.listed.pk).update(
            remote_hall_price=Decimal("33.33")
        )
        row = serialize_peer_row(PeerHallMirror.objects.get(pk=self.listed.pk))
        self.assertEqual(row["hall_price"], "30.00")

    def test_no_price_stays_null_even_with_a_percent(self):
        """⛔ 无价 ≠ 0 元：90% 之下 `NULL × 90%` 仍是 `NULL`（不补 0.00、不参与价格区间）。"""
        from apps.business.peer_hall import peer_hall_price_in_range

        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_price_percent=90
        )
        PeerHallMirror.objects.filter(pk=self.listed.pk).update(remote_hall_price=None)
        fresh = PeerHallMirror.objects.get(pk=self.listed.pk)
        row = serialize_peer_row(fresh)
        self.assertIsNone(row["hall_price"])
        # 区间筛选：没传区间 ⇒ 留在厅里；传了区间 ⇒ 它答不出这个问题 ⇒ 不命中。
        self.assertTrue(peer_hall_price_in_range(fresh))
        self.assertFalse(peer_hall_price_in_range(fresh, price_min=Decimal("0")))
        self.assertFalse(peer_hall_price_in_range(fresh, price_max=Decimal("999")))


class TakerApiHallReadTests(ReadFixture):
    """打手 API 大厅的三路并读（原生 ∪ 鸡蛋铺镜像 ∪ 团团通镜像）。"""

    def setUp(self):
        super().setUp()
        from apps.accounts.models import StaffProfile
        from apps.takerapi.models import TakerApp

        self.anonymous = Client(SERVER_NAME=self.domain.domain)
        self.taker = get_user_model().objects.create_user(
            username="syn-peer-hall-read-taker", password="pw-syn"
        )
        StaffProfile.objects.create(user=self.taker).games.set([self.game])
        # ⭐ Z11：takerapi 大厅读口判 `grab_hall.take_order` ⇒ 本夹具的
        #    「合法打手」必须持它（无节点那一档由 `test_takerapi_hall_api.py` 单独钉）。
        from django.contrib.auth.models import Group

        from tests.p5_auth_helpers import grant_nodes

        taker_group, _ = Group.objects.get_or_create(name="大厅节点")
        self.taker.groups.add(taker_group)
        grant_nodes(self.taker, "grab_hall.take_order")
        self.app = TakerApp.objects.create(
            name="读面打手应用",
            app_key="peerreadkey00000001",
            app_secret="peerreadsecret000001",
            taker=self.taker,
            created_by=self.taker,
        )
        token = self._token()
        self.hall = self._hall(token)

    def _token(self):
        response = self.anonymous.post(
            "/api/taker/oauth/token",
            {
                "grant_type": "client_credentials",
                "client_id": self.app.app_key,
                "client_secret": self.app.app_secret,
            },
            REMOTE_ADDR="203.0.113.51",
        )
        return response.json()["access_token"]

    def _hall(self, token, params=None):
        response = self.anonymous.get(
            "/api/taker/v1/hall/", params or {},
            HTTP_AUTHORIZATION=f"Bearer {token}", REMOTE_ADDR="203.0.113.51",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        return json.loads(response.content)

    def test_peer_listed_row_appears_with_exactly_thirteen_keys(self):
        rows = [
            row for row in self.hall["data"]["results"] if row.get("origin") == "peer"
        ]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), PEER_ROW_KEYS)
        self.assertEqual(row["order_no"], "UP-READ-1")
        self.assertEqual(row["hall_price"], "50.00")
        self.assertEqual(row["mirror_pk"], self.listed.pk)
        self.assertEqual(row["content"], "syn-需求内容")
        # ⭐ B6 / L-12：对外响应的时刻也是**项目时区**（`+08:00`，TAKER-API.md §5.1 契约）。
        self.assertEqual(row["created_at"], local_iso(self.listed.first_seen_at))
        self.assertTrue(row["created_at"].endswith("+08:00"))

    def test_equal_times_keep_source_and_row_order_across_page_boundary(self):
        self._tie_listed_cards()
        token = self._token()
        params = {"page_size": 3, "game": self.game.pk}
        first = self._hall(token, params)["data"]
        second = self._hall(token, {**params, "page": 2})["data"]
        self.assertEqual(first["count"], 4)
        self.assertEqual(second["count"], 4)
        self.assertEqual(
            [row["order_no"] for row in first["results"]],
            ["20261031000000110002", "20261031000000110001", "VIEW-JDP-1"],
        )
        self.assertEqual([row["order_no"] for row in second["results"]], ["UP-READ-1"])

    def test_only_listed_rows_of_enabled_bindings_appear(self):
        origins = {
            (row.get("origin"), row.get("order_no"))
            for row in self.hall["data"]["results"]
            if row.get("origin") in ("peer", "upstream")
        }
        # ⛔ gone / claiming / 停用绑定的行一个都不出现（可见与可接同一道判据）。
        peer_nos = {no for origin, no in origins if origin == "peer"}
        self.assertEqual(peer_nos, {"UP-READ-1"})
        # 鸡蛋铺那一路照旧（正对照：本线没有吃掉它）。
        self.assertIn(("upstream", "VIEW-JDP-1"), origins)

    def test_switched_off_binding_projects_no_peer_rows(self):
        """⭐ P1 第二处闸：开关关着 ⇒ takerapi 大厅**一行 peer 都不投影**（鸡蛋铺照旧）。"""
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_sync_enabled=False
        )
        payload = self._hall(self._token())
        self.assertEqual(
            [row for row in payload["data"]["results"] if row.get("origin") == "peer"],
            [],
        )
        # 正对照：鸡蛋铺那一路仍在（关的是**这一路**，不是整个大厅）。
        self.assertEqual(
            [
                row["order_no"]
                for row in payload["data"]["results"]
                if row.get("origin") == "upstream"
            ],
            ["VIEW-JDP-1"],
        )

    def test_ninety_percent_shows_the_scaled_price_in_the_taker_api(self):
        """Takerapi 那一路的 `hall_price` 也走 `display_price`（90% ⇒ 45.00）。"""
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_price_percent=90
        )
        payload = self._hall(self._token())
        peer = [
            row for row in payload["data"]["results"] if row.get("origin") == "peer"
        ]
        self.assertEqual([row["hall_price"] for row in peer], ["45.00"])

    def test_game_scope_narrows_the_peer_stream(self):
        """游戏权限与内部单同口径：无权游戏的 peer 行看不到。"""
        PeerHallMirror.objects.filter(pk=self.listed.pk).update(game=self.other_game)
        payload = self._hall(self._token())
        peer_rows = [
            row for row in payload["data"]["results"] if row.get("origin") == "peer"
        ]
        self.assertEqual(peer_rows, [])
        # 鸡蛋铺那一路照旧（它仍挂在有权游戏上）——收窄只吃游戏权限，不误伤别路。
        upstream_rows = [
            row for row in payload["data"]["results"] if row.get("origin") == "upstream"
        ]
        self.assertEqual([row["order_no"] for row in upstream_rows], ["VIEW-JDP-1"])

    def test_keyword_matches_the_visible_peer_order_no(self):
        payload = self._hall(self._token(), {"keyword": "UP-READ-1"})
        peer_rows = [
            row for row in payload["data"]["results"] if row.get("origin") == "peer"
        ]
        self.assertEqual(len(peer_rows), 1)
        payload = self._hall(self._token(), {"keyword": "UP-READ-2"})
        peer_rows = [
            row for row in payload["data"]["results"] if row.get("origin") == "peer"
        ]
        self.assertEqual(peer_rows, [])  # gone 行不可见 ⇒ 也不可搜



class InternalHallReadTests(ReadFixture):
    """内部大厅读口的第四路。"""

    def setUp(self):
        super().setUp()
        self.api = make_logged_in_client(
            username="syn-peer-hall-read-web",
            groups=("打手",),
            nodes=("grab_hall.take_order",),
            games=[self.game],
        )

    def _hall(self, params=None):
        response = self.api.get("/api/business/hall/", params or {})
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return body

    def test_peer_listed_row_appears_beside_the_jidanpu_one(self):
        body = self._hall()
        rows = body["data"]["items"]
        peer_rows = [row for row in rows if row.get("origin") == "peer"]
        self.assertEqual(len(peer_rows), 1)
        self.assertEqual(peer_rows[0]["remote_order_id"], "UP-READ-1")
        # 键表恰好同形（内部行的同键网，四路一张表渲染的前提）。
        from tests.test_jidanpu_upstream_pull import MERGED_ROW_KEYS

        self.assertEqual(set(peer_rows[0]), MERGED_ROW_KEYS)
        # 鸡蛋铺那一路照旧（正对照）。
        upstream_rows = [row for row in rows if row.get("origin") == "upstream"]
        self.assertEqual(len(upstream_rows), 1)
        self.assertEqual(upstream_rows[0]["remote_order_id"], "JDP-READ-1")

    def test_equal_times_keep_source_order_and_gray_cards_follow_live_pages(self):
        moment = self._tie_listed_cards()
        HallExitSnapshot.objects.create(
            game=self.game, exited_at=moment, exit_kind="withdrawn",
            payload={"order_no": "20261031000000110003", "hall_price": "58.00"},
        )
        params = {"page_size": 2, "include_exited": "1"}
        first = self._hall(params)["data"]
        second = self._hall({**params, "page": 2})["data"]
        tail = self._hall({**params, "page": 3})["data"]
        self.assertEqual([page["total"] for page in (first, second, tail)], [5, 5, 5])
        self.assertEqual(
            [row["order_no"] for row in first["items"]],
            ["20261031000000110002", "20261031000000110001"],
        )
        self.assertEqual(
            [row["remote_order_id"] for row in second["items"]],
            ["JDP-READ-1", "UP-READ-1"],
        )
        self.assertEqual([row["order_no"] for row in tail["items"]], ["20261031000000110003"])
        self.assertTrue(tail["items"][0]["is_exited"])

    def test_preferred_price_range_uses_captured_and_legacy_mirror_prices(self):
        self._mirror("UP-READ-NULL")
        PeerHallMirror.objects.filter(peer_order_no="UP-READ-NULL").update(
            remote_hall_price=None,
        )
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(hall_price_percent=50)
        PeerHallMirror.objects.filter(pk=self.listed.pk).update(
            remote_hall_price=Decimal("300.01"), hall_price_percent_at_sync=80,
        )
        JidanpuUpstreamBinding.objects.filter(pk=self.jdp_binding.pk).update(hall_price_percent=80)
        JidanpuUpstreamHallOrder.objects.filter(pk=self.jdp_row.pk).update(
            remote_rec_fee=Decimal("300.01"),
        )
        # 300.01 × 80% rounds to 240.01. Peer uses its recorded ratio instead of live 50%.
        filters = {"price_min": "240.01", "price_max": "240.01", "preferred_only": "1", "page_size": 1}
        first = self._hall(filters)["data"]
        second = self._hall({**filters, "page": 2})["data"]
        self.assertEqual([first["total"], second["total"]], [2, 2])
        rows = first["items"] + second["items"]
        self.assertEqual({row["remote_order_id"] for row in rows}, {"JDP-READ-1", "UP-READ-1"})
        self.assertEqual({(row["hall_price"], row["is_preferred"]) for row in rows}, {("240.01", True)})

        self._mirror("UP-READ-ZERO", price="0.00")
        at_zero = self._hall({"price_min": "0", "price_max": "0", "page_size": 1})["data"]
        self.assertEqual(at_zero["total"], 1)
        self.assertEqual(
            [(row["remote_order_id"], row["hall_price"]) for row in at_zero["items"]],
            [("UP-READ-ZERO", "0.00")],
        )

    def test_switched_off_binding_projects_no_peer_rows(self):
        """⭐ P1 第二处闸：内部大厅读口同样一行不投影（与 takerapi 共用 `peer_hall_queryset`）。"""
        PeerUpstreamBinding.objects.filter(pk=self.up_binding.pk).update(
            hall_sync_enabled=False
        )
        rows = self._hall()["data"]["items"]
        self.assertEqual([row for row in rows if row.get("origin") == "peer"], [])
        # 正对照：鸡蛋铺那一路仍在。
        self.assertEqual(
            [
                row["remote_order_id"]
                for row in rows
                if row.get("origin") == "upstream"
            ],
            ["JDP-READ-1"],
        )

    def test_game_filter_narrows_the_peer_stream(self):
        body = self._hall({"game": self.other_game.pk})
        rows = body["data"]["items"]
        self.assertEqual(
            [row for row in rows if row.get("origin") in ("peer", "upstream")], []
        )

    def test_game_scope_narrows_for_restricted_viewer(self):
        """无权玩家连 peer 行也看不到（与内部单同一道卡点）。"""
        api = make_logged_in_client(
            username="syn-peer-hall-read-web2",
            groups=("打手",),
            nodes=("grab_hall.take_order",),
            games=[self.other_game],
        )
        response = api.get("/api/business/hall/")
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        rows = body["data"]["items"]
        self.assertEqual(
            [row for row in rows if row.get("origin") in ("peer", "upstream")], []
        )


class SnapshotCommandTests(ReadFixture):
    """铺底命令 `push_peer_hall_snapshot`（dry-run 默认）。

    ⚠️ 命令推的是**本租户**的在厅单、收件人是**下游绑定**（`PeerDownstreamBinding`，
    我方为上家那一张表）——读面夹具里那条 `PeerUpstreamBinding` 是下家视角的表，
    与命令无关。
    """

    def setUp(self):
        super().setUp()
        from apps.business.models import PeerDownstreamBinding

        operator = get_user_model().objects.create_user(
            username="syn-peer-hall-read-snap", password="pw-syn"
        )
        self.down_binding = PeerDownstreamBinding.objects.create(
            account=operator,
            peer_base_url="https://peer-hall-read-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

    def _make_native_listed_order(self, no):
        """上家自己的一张在厅单（命令推的是**本租户**的 WAITING_GRAB，不是镜像行）。"""
        from apps.business.models import Order, OrderStatus

        return Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
        )

    def test_dry_run_writes_nothing(self):
        """dry-run：打印候选、一个字不落库。"""
        from django.core.management import call_command

        self._make_native_listed_order("20261025000000060001")
        out = io.StringIO()
        call_command(
            "push_peer_hall_snapshot", schema=connection.schema_name, stdout=out
        )
        self.assertIn("dry-run", out.getvalue())
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_execute_enqueues_throw_for_each_listed_order(self):
        from django.core.management import call_command

        order = self._make_native_listed_order("20261025000000060002")
        out = io.StringIO()
        call_command(
            "push_peer_hall_snapshot", schema=connection.schema_name,
            execute=True, stdout=out,
        )
        rows = PeerSyncEvent.objects.filter(kind=HALL_THROW_KIND)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().target_ref, order.order_no)
        self.assertEqual(rows.get().binding_id, str(self.down_binding.pk))
        self.assertIn("已排 1 条", out.getvalue())

    def test_muted_binding_is_a_loud_error_when_named(self):
        """显式点名一个关推送的绑定：响亮报错（开关绕不过，spec §5；⛔ 不是静默 0 条）。"""
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from django.core.management.base import CommandError

        from apps.business.models import PeerDownstreamBinding

        account = get_user_model().objects.create_user(
            username="syn-peer-hall-read-muted", password="pw-syn"
        )
        muted = PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url="https://peer-hall-read-muted.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
            hall_push_enabled=False,
        )
        self._make_native_listed_order("20261025000000060003")
        with self.assertRaises(CommandError) as caught:
            call_command(
                "push_peer_hall_snapshot", schema=connection.schema_name,
                execute=True, binding=muted.pk, stdout=io.StringIO(),
            )
        self.assertIn("未开启大厅推送", str(caught.exception))
        self.assertEqual(PeerSyncEvent.objects.count(), 0)

    def test_execute_is_idempotent_on_rerun(self):
        """重跑无害：已投递 / 待投的 throw 不会重复排（pending 去重）。"""
        from django.core.management import call_command

        self._make_native_listed_order("20261025000000060004")
        call_command(
            "push_peer_hall_snapshot", schema=connection.schema_name,
            execute=True, stdout=io.StringIO(),
        )
        call_command(
            "push_peer_hall_snapshot", schema=connection.schema_name,
            execute=True, stdout=io.StringIO(),
        )
        self.assertEqual(
            PeerSyncEvent.objects.filter(kind=HALL_THROW_KIND).count(), 1
        )



class SnapshotHiddenOrderTests(ReadFixture):
    """⭐ 独立评审阻断 2：铺底命令按 `status=WAITING_GRAB` 取单 ⇒ **隐藏单不能再抛一次**。

    隐藏单**不是**本租户的原生在厅单（它只是把镜像卡同步给下家鸡蛋铺的载体）；铺底若把它
    当原生单一并抛给 `PeerDownstreamBinding`，下家会收到两张同内容的卡（原生 throw 已经
    在卡落厅时抛过一次）。
    """

    def setUp(self):
        super().setUp()
        from apps.business.models import PeerDownstreamBinding

        operator = get_user_model().objects.create_user(
            username="syn-shadow-snap-op", password="pw-syn"
        )
        self.down_binding = PeerDownstreamBinding.objects.create(
            account=operator,
            peer_base_url="https://syn-shadow-snap-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

    def _make_native_listed_order(self, no):
        from apps.business.models import Order, OrderStatus

        return Order.objects.create(
            game=self.game,
            order_no=no,
            status=OrderStatus.WAITING_GRAB,
            total_price=Decimal("100.00"),
            hall_price=Decimal("50.00"),
        )

    def test_hidden_orders_are_not_pushed_again(self):
        from django.core.management import call_command

        from apps.business import hall_shadow

        hidden = hall_shadow.ensure_for_card(self.listed)
        self.assertIsNotNone(hidden)
        native = self._make_native_listed_order("20261025000000060005")
        out = io.StringIO()
        call_command(
            "push_peer_hall_snapshot", schema=connection.schema_name,
            execute=True, stdout=out,
        )
        rows = PeerSyncEvent.objects.filter(kind=HALL_THROW_KIND)
        self.assertEqual(rows.count(), 1, rows.values_list("target_ref", flat=True))
        self.assertEqual(rows.get().target_ref, native.order_no)
        self.assertNotIn(hidden.order_no, out.getvalue())
