"""上家鸡蛋铺线（客服个人绑定）：**大厅镜像**（spec §5.1 / §16.1；⭐ D-0366 后的取数形状）。

本文件检六件事：

① **逐游戏段查是唯一的取数路**（⭐ D-0366，2026-11-03，E-0314：业务方拍板「以后别用空
   游戏名查」——竹屿整厅口恒回 1 张 + 三家 `key1` 复核是在厅活卡盲查，旧「无游戏名全量路
   ＋ key1 逐单回查」两块基石在这类上家身上全部失真）：每轮对我方游戏字典的**每只游戏**
   带**完整 `game.name`** 分页拉一遍（GC 22：⛔ 简称/截断名会让上游**静默回 0 行**）；
   **主钉子** = 「一轮请求序列里不得出现无 `gameName` 的大厅 GET」（防回潮）；
② **自己按 `page` 翻全**（⛔ 不用 SDK `iter_all`：它拿默认 `page_size=100` 判「不满一页即尾页」
   ⇒ 第一页（20 行）就被当成尾页、静默少拿，spec B5 / §13.1-R3）；
③ **地址级去重**（A2：同一上家地址只有 `id` 最小的**启用**绑定打上游；⛔ 指派单那一路不走这个去重）；
④ ⭐ **游戏四级匹配**（A15 / §16.1：精确 → 归一化 → 子串 → **不中就不进大厅 + 人话台账**，⛔ 绝不硬塞）；
⑤ **消失判定的段查复核**（⭐ D-0366：候选 = `listed` 且不在本轮段行 `seen` 里且其游戏段本轮
   成功（**含 0 行成功**——YF-01 的 success-empty 排除随本决策删除）；复核 = 该游戏段**重新
   查一次**（新鲜请求），复核仍不见才落 `gone`；复核失败 ⇒ 跳过（宁漏标勿误标）。
   ⛔ D-0258 的 `key1` 逐单回查**删除**（E-0314 实证三家用它查在厅活卡也「查无」）；
⑥ **台账的幂等键含 `reason`** + **镜像投影的超宽文本按镜像列宽截断**（M-1）。

⚠️ 判据打**数据库行**与**桩的调用集合**（`AGENTS.md` §5-G），⛔ 不打 logger。
⚠️ **零真实网络**：`_StubClient` 打在 SDK 边界；只有「⛔ 别改回 `iter_all`」那一条用**真 SDK +
   `MockTransport`**（同样零网络）。
⚠️ **本轮拉不全 ⇒ ⛔ 不判「消失」**（spec §5.1 / §10）。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：域名 `…test.invalid`、账号 `acc-old` / `acc-new`、
   密码 `pw-canary-a`、单号 `20260928000000000101` 起（全零段，一眼是合成值）。游戏名用
   `重返未来：1999`（全角冒号，上家的完整名）与 `重返未来`（我方简称）这一对**形状**——它是
   spec §16.1 四级匹配的具名例子，⛔ 不是真实业务数据。
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError

from apps.business import jidanpu, jidanpu_upstream
from apps.business.api.serializers import HallListSerializer
from apps.business.jidanpu_common import REMOTE_ROW_BASE
from apps.business.models import (
    Game,
    HallExitSnapshot,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    Order,
    OrderStatus,
)
from apps.business.order_no import generate_order_no
from apps.business import upstream_hall_shadow

#: 端点路径**逐字**抄 spec §2.1（⛔ 不从被测常量派生）。
HALL_PATH = "/OrderAjax/GetInternalOrderGrabbingList"
#: 订单列表端点（会员线按单补核 / 对账在用；⛔ 不从被测常量派生）。
ORDER_LIST_PATH = "/OrderAjax/GetOrderList"

BASE_URL = "https://jidanpu-upstream-a.test.invalid"
BASE_URL_B = "https://jidanpu-upstream-b.test.invalid"
PASSWORD = "pw-canary-a"

#: 游戏名的一对**形状**（spec §16.1）：上家给全名，我方只有简称。
GAME_FULL = "重返未来：1999"
GAME_SHORT = "重返未来"
GAME_OTHER = "某款我们没有的游戏"

#: 大厅页每页固定 20 行（spec §2.1 **实测**，⛔ 不是契约 ⇒ 判据不写死它，这里只用来造夹具）。
PER_PAGE = 20

#: 归并新增的 7 个键（`HallListSerializer` 的 `Meta.fields` 末尾那一段，两路共用同一份）。
MERGE_ADDITION_KEYS = {
    "origin", "source_system", "remote_order_id", "remote_order_view",
    "remote_game_area", "remote_game_server", "remote_hours_desc",
}

#: 归并行的**共享键表**（`HallListSerializer.Meta.fields` 已含上面 7 个键，spec §4.6）。
MERGED_ROW_KEYS = set(HallListSerializer.Meta.fields) | MERGE_ADDITION_KEYS


def _http_factory(handler):
    """把 `jidanpu.httpx.Client(...)` 换成「真客户端 + `MockTransport`」⇒ **零网络**。"""
    real_client = httpx.Client

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    return factory


class _StubClient:
    """`JidanpuClient` 的替身（⭐ D-0366 后的形状：大厅**只**按 `gameName` 段查）。

    - `rows`：全部行的**真值表**；段查询按 `gameName` **现场过滤**（撤/加只改这一份
      ⇒ 复核那一跳天然看到新状态）；`per_game_rows` 可按游戏名**覆盖**整段
      （演「段与真值表不同形」的夹具；键必须与上游认的**完整名逐字相等**，不在表里的
      名字回 **0 行（count=0）**——上游对不认识的名字的实测语义）；
    - `total`：**只**给「不带 `gameName` 的翻页原语」用（`pull_all_pages` 的既有用例；
      ⛔ 同步轮不走这一形状——主钉子盯着它）；缺省 = `len(rows)`；
    - `per_game_totals`：按游戏名**覆盖**段壳里的 `count`（演「count 偏大 / 偏小」；
      缺省 = `len(段)`）；`count_missing=True` ⇒ 段壳里**没有** `count`（`int | None` 三态）；
    - `fail_game_names`：这些名字的段查询**从第一次起**就回失败业务壳（`code=7`）⇒
      `_require_read_success` 响亮抛 `ApiError`；
    - `fail_game_names_from_ask`：从第 **k** 次**请求该游戏段**起才失败（k 从 1 数）——
      演「本轮段查成功、复核那一跳失败」：复核是新请求，恰是第 2 次问；
    - `mutate_after_calls` / `withdraw_index`：第 N 次**取完大厅页**后「撤一行 + 加一行」
      （探针 `probes2/r3-upstream-pagination-shift.py` 的构造搬进测试，`.tools/recon` 不进 git）；
    - 订单列表端点那一跳保留（会员线按单补核 / 对账在用）：替身按 `key1` 过滤后切页；
      `order_list_error` 给一个异常 ⇒ 那一跳当失败。
    """

    def __init__(self, *, rows=None, total=None, per_page=PER_PAGE, count_missing=False,
                 account="acc-a", mutate_after_calls=frozenset(), withdraw_index=25,
                 order_list_error=None, per_game_rows=None, fail_game_names=frozenset(),
                 fail_game_names_from_ask=None, per_game_totals=None,
                 count_missing_from_ask=None):
        self.rows = [dict(row) for row in (rows or [])]
        self.total = len(self.rows) if total is None else total
        self.per_page = per_page
        self.count_missing = count_missing
        # ⭐ D-0366 评审补钉：从第 k 次问这个游戏段起，壳里**不再带 count**
        # （演「本轮段查成功、复核那一跳答了但判不了拉全」——`_mark_gone` 必须判 None ⇒ 跳过）。
        self.count_missing_from_ask = {
            str(name): int(k) for name, k in (count_missing_from_ask or {}).items()
        }
        self.account = account
        self.mutate_after_calls = frozenset(mutate_after_calls)
        self.withdraw_index = withdraw_index
        self.order_list_error = order_list_error
        self.per_game_rows = {
            str(name): [dict(row) for row in game_rows]
            for name, game_rows in (per_game_rows or {}).items()
        }
        self.per_game_totals = dict(per_game_totals or {})
        self.fail_game_names = frozenset(fail_game_names)
        self.fail_game_names_from_ask = {
            str(name): int(k)
            for name, k in (fail_game_names_from_ask or {}).items()
        }
        self.calls: list[tuple[str, str, dict]] = []
        self.logins: list[tuple[str, str]] = []
        self.last_envelope: dict = {}
        self.hall_calls = 0
        self.game_asks: dict[str, int] = {}

    def ensure_login(self):
        self.logins.append((self.account, HALL_PATH))

    def _withdraw_and_append(self):
        """探针的那一刀：撤掉 `withdraw_index` 那行 + 追加一张新单（净 count 不变）。"""
        withdrawn = self.rows[self.withdraw_index]
        game_name = str(withdrawn.get("gameName", "") or "")
        del self.rows[self.withdraw_index]
        # ⭐ 追加的新行**回被撤行的那一段**（gameName 沿用它）——探针的口径是
        # 「净 count 不变、行只挪位」，段世界里新行落回同段才不破坏段算术。
        self.rows.append({"orderId": "NEWROW", "gameName": game_name})

    def _segment(self, asked_game: str) -> list[dict]:
        if asked_game in self.per_game_rows:
            return list(self.per_game_rows[asked_game])
        return [
            row for row in self.rows
            if str(row.get("gameName", "") or "") == asked_game
        ]

    def call(self, method, path, **kwargs):
        # 真 SDK 的 `call()` **首次会惰性登录** ⇒ 桩照此记账，「谁打了上游」与「谁登录了」同源。
        if not self.logins:
            self.ensure_login()
        self.calls.append((method, path, dict(kwargs.get("query") or {})))
        if path == ORDER_LIST_PATH:
            if self.order_list_error is not None:
                raise self.order_list_error
            data = dict(kwargs.get("data") or {})
            key1 = str(data.get("key1", "") or "")
            hits = [
                row for row in self.rows
                if key1 and (
                    str(row.get("orderId", "")) == key1
                    or key1 in str(row.get("orderIdView", "") or "")
                )
            ]
            page = int(data.get("page", 1))
            start = (page - 1) * self.per_page
            page_rows = hits[start:start + self.per_page]
            self.last_envelope = {
                "code": 0, "msg": "success", "data": page_rows, "count": len(hits),
            }
            return [dict(row) for row in page_rows]
        assert path == HALL_PATH, f"未预期的端点：{method} {path}"
        self.hall_calls += 1
        query = dict(kwargs["query"])
        asked_game = str(query.get("gameName", "") or "")
        if asked_game:
            # ⭐「第 k 次问这个游戏」= `page=1` 的那一次开新问（翻页/探页是同一问的延续）。
            ask_no = self.game_asks.get(asked_game, 0)
            if int(query.get("page", "1")) == 1:
                ask_no += 1
                self.game_asks[asked_game] = ask_no
            floor = self.fail_game_names_from_ask.get(asked_game)
            if asked_game in self.fail_game_names or (
                floor is not None and ask_no >= floor
            ):
                self.last_envelope = {"code": 7, "msg": "没有权限", "data": []}
                return []
            source = self._segment(asked_game)
            source_total = self.per_game_totals.get(asked_game, len(source))
            if self.count_missing:
                source_total = None
            floor = self.count_missing_from_ask.get(asked_game)
            if floor is not None and ask_no >= floor:
                source_total = None
        else:
            # ⛔ 同步轮**不该**走进这一支（主钉子）；`pull_all_pages` 这个不带 gameName 的
            # 翻页原语的既有用例仍打这一形状 ⇒ 替身保留服份。
            source = list(self.rows)
            source_total = None if self.count_missing else self.total
        page = int(query["page"])
        start = (page - 1) * self.per_page
        page_rows = source[start:start + self.per_page]
        if self.hall_calls in self.mutate_after_calls:
            # 探针的时序：chunk 按**变更前**的列表切好，撤/加发生在**这一页取完之后**
            # （真值表变了 ⇒ 之后的调用（含复核）看到新状态）。
            self._withdraw_and_append()
        self.last_envelope = {"code": 0, "msg": "success", "data": page_rows}
        if source_total is not None:
            self.last_envelope["count"] = source_total
        return [dict(row) for row in page_rows]


class UpstreamHallPullTests(TenantTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=dt_timezone.utc)
        self.older_user = User.objects.create_user(username="up-old", password="pw-old")
        self.newer_user = User.objects.create_user(username="up-new", password="pw-new")
        self.older = JidanpuUpstreamBinding.objects.create(
            user=self.older_user, jidanpu_base_url=BASE_URL, account="acc-old",
            password=PASSWORD, enabled=True,
        )
        self.newer = JidanpuUpstreamBinding.objects.create(
            user=self.newer_user, jidanpu_base_url=BASE_URL, account="acc-new",
            password=PASSWORD, enabled=True,
        )
        #: 同步入口默认用的绑定 = 同地址最早的那一份（owner）。
        self.binding = self.older
        self.game_a = Game.objects.create(name="测试游戏甲")
        self.game_b = Game.objects.create(name="测试游戏乙")

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def hall_row(self, *, order_id="20260928000000000101", view=None, game_name="测试游戏甲",
                 rec_fee="88.00", **extra):
        row = {
            "orderId": order_id,
            "orderIdView": view if view is not None else f"VIEW-{order_id}",
            "gameName": game_name,
            "gameArea": "安卓",
            "gameServer": "微信区",
            "project": "合成项目",
            "orderRemark": "合成备注",
            "roleName": "合成角色",
            "recFee": rec_fee,
            "createTime": "2026-09-28 11:46:54",
        }
        row.update(extra)
        return row

    def scripted_pages(self, rows, **kwargs):
        return _StubClient(rows=rows, account=self.older.account, **kwargs)

    def make_mirror_row(self, *, order_id="20260928000000000201", status=None, game=None,
                        base_url=BASE_URL, first_seen_at=None, rec_fee=None,
                        upstream_payload=None):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=base_url,
            upstream_order_id=order_id,
            status=status or JidanpuUpstreamHallOrder.Status.LISTED,
            game=game,
            remote_rec_fee=rec_fee,
            upstream_payload=upstream_payload,
            owner_binding=self.older,
            first_seen_at=first_seen_at or self.now,
        )

    def make_mirror_rows(self, *, games):
        return [self.make_mirror_row(order_id=f"2026092800000000021{i}", game=game)
                for i, game in enumerate(games)]

    # ── Step 1：游戏四级匹配（⭐ 唯一出处 `match_game_by_upstream_name`）────────

    def test_level1_exact_name_beats_a_normalized_collision(self):
        """① 精确同名必须赢过「归一化后相等、但并非逐字相等」的那一只（与 ② 那条**互为对照**）。

        夹具刻意让归一化**塌成一格**：全角 `Ａ` 那只先建（pk 更小）、半角 `A` 那只后建，
        而上家给的串**逐字**等于半角那只 ⇒ ① 必须回半角那只。
        ⛔ 去掉 ① 会落到 ②：②在并列里取 `pk` 最小者 ⇒ **选错游戏**（这条用例红）。
        """
        full_width = Game.objects.create(name="测试游戏Ａ")   # pk 更小：归一化后与半角那只同形
        half_width = Game.objects.create(name="测试游戏A")
        self.assertLess(full_width.pk, half_width.pk)
        normalized = jidanpu_upstream._normalize_game_name("测试游戏A")
        self.assertEqual(normalized, jidanpu_upstream._normalize_game_name(full_width.name))
        chosen = jidanpu_upstream.match_game_by_upstream_name("测试游戏A")
        self.assertEqual(chosen.pk, half_width.pk)

    def test_level2_normalized_name_beats_a_longer_substring(self):
        """② 归一化相等必须赢过「只靠子串命中的更长名字」（与 ① 那条**互为对照**）。

        夹具：上家给**全角冒号 + 首尾空格**的完整名；我方**没有**逐字相等的那只（⇒ ① 不中），
        有归一化后相等的 `测试游戏:1999`（半角冒号）与一只更长的 `测试游戏:1999豪华版`。
        ⇒ ② 必须回归一化相等的那只。
        ⛔ 去掉 ② 会落到 ③：③在多个子串命中里取**名字最长**的那只 ⇒ **选错游戏**（这条用例红）。
        """
        normalized_twin = Game.objects.create(name="测试游戏:1999")
        longer = Game.objects.create(name="测试游戏:1999豪华版")
        chosen = jidanpu_upstream.match_game_by_upstream_name(" 测试游戏：1999 ")
        self.assertEqual(chosen.pk, normalized_twin.pk)
        self.assertNotEqual(chosen.pk, longer.pk)

    def test_level3_substring_name(self):
        """③ 一方是另一方的子串 ⇒ 对上（夹具：我方叫「重返未来」，上家给「重返未来：1999」）。"""
        game_short = Game.objects.create(name=GAME_SHORT)
        self.assertEqual(
            jidanpu_upstream.match_game_by_upstream_name(GAME_FULL).pk, game_short.pk,
        )

    def test_level4_unmatched_is_never_stuffed_into_a_game(self):
        """④ 都不中 ⇒ 该行 `skipped` + 人话台账（**四条断言，缺一不可**）。

        ⭐ D-0366 迁移注：行只能从「我方游戏字典的段」里来 ⇒ 无主游戏的本征行**永远
        不可见**；四级匹配保留作**防御**（段里混进别家游戏标签的行，实测上游不保证
        不会）。夹具把一张 `gameName=GAME_OTHER` 的行放进甲的段里演这个形态。
        """
        stub = self.scripted_pages([])
        stub.per_game_rows = {
            "测试游戏甲": [self.hall_row(order_id="20260928000000000451", game_name=GAME_OTHER)],
        }
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        row = JidanpuUpstreamHallOrder.objects.get()
        self.assertEqual(row.status, JidanpuUpstreamHallOrder.Status.SKIPPED)   # ①
        self.assertIsNone(row.game_id)                                          # ② 没被硬塞
        self.assertEqual(jidanpu_upstream.upstream_hall_rows(), [])             # ③ 不进大厅
        event = JidanpuSyncEvent.objects.get(kind="upstream.hall_skipped")      # ④ 人话台账
        self.assertIn("加别名", event.reason)

    def test_direct_level4_returns_none_and_never_stuff(self):
        """④ 的**纯函数面**：`match_game_by_upstream_name` 对不认识的直接回 `None`（⛔ 不硬塞）。"""
        self.assertIsNone(jidanpu_upstream.match_game_by_upstream_name(GAME_OTHER))

    def test_two_spellings_of_the_same_game_hit_the_same_row(self):
        """⭐ 防「分级写成只认第一条」：同一款游戏的两种写法都指向**同一只** `Game`。"""
        game_short = Game.objects.create(name=GAME_SHORT)
        self.assertEqual(
            jidanpu_upstream.match_game_by_upstream_name(GAME_FULL).pk,
            jidanpu_upstream.match_game_by_upstream_name(GAME_SHORT).pk,
        )
        self.assertEqual(
            jidanpu_upstream.match_game_by_upstream_name(GAME_FULL).pk, game_short.pk,
        )

    def test_multiple_substring_hits_take_the_longest_and_leave_a_note(self):
        """⭐ 子串那一级「命中多只」⇒ 取**名字最长**的那只 + 留一条台账（⛔ 不静默取第一条）。"""
        long_game = Game.objects.create(name="重返未来")
        Game.objects.create(name="未来")
        chosen = jidanpu_upstream.match_game_by_upstream_name("重返未来：1999")
        self.assertEqual(chosen.pk, long_game.pk)
        event = JidanpuSyncEvent.objects.get(kind="upstream.hall_game_ambiguous")
        self.assertIn("重返未来", event.reason)

    # ── Step 2：翻页真的拉全（spec §11-2 / B5 / R3）─────────────────────────

    def test_paging_walks_all_pages_even_though_every_page_is_20(self):
        """⭐ 桩给 `count=299`、每页恒 20 ⇒ 必须真的发出不同 `page`、拿回 299 行。

        ⚠️ 第 16 页是**探页**：前 15 页累计到 299 == `count` 就停，再探一页
        确认「没有下一页」⇒ 空页才判「拉全」。⛔ 探页的行使**不进**结果集。
        （这是 `pull_all_pages` 这个**翻页原语**自己的用例——它不带 `gameName`；
        ⛔ 同步轮不走这一形状，主钉子盯着。）
        """
        stub = _StubClient(rows=[{"orderId": f"S-{i}"} for i in range(299)], total=299)
        rows, complete = jidanpu_upstream.pull_all_pages(stub, path=HALL_PATH)
        self.assertTrue(complete)
        self.assertEqual(len(rows), 299)
        self.assertEqual(
            [query for _m, _p, query in stub.calls if "page" in query],
            [{"page": n} for n in range(1, 17)],
        )

    def test_iter_all_default_shape_would_stop_at_the_first_page(self):
        """⛔ 另一半：SDK 默认形状（`page_size=100`）只拿 20 行就停 —— 这条钉住「别改回 `iter_all`」。

        真 SDK + `MockTransport`（零网络）：上游每页恒 20 行、壳里 `count=299`。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/Login/Login":
                return httpx.Response(
                    200, json={"code": 1, "msg": "ok", "data": "ok"},
                    headers={"Set-Cookie": "sl-session=synth; Path=/"},
                )
            if path == HALL_PATH:
                page = request.url.params.get("page", "1")
                return httpx.Response(200, json={
                    "code": 0, "msg": "success", "count": 299,
                    "data": [{"orderId": f"S-{page}-{i}"} for i in range(PER_PAGE)],
                })
            raise AssertionError(f"未预期的请求：{request.method} {path}")

        with mock.patch.object(jidanpu.httpx, "Client", _http_factory(handler)):
            with jidanpu.open_client(self.older) as client:
                got = list(client.iter_all("GET", HALL_PATH))
        self.assertEqual(len(got), PER_PAGE)

    def test_count_missing_is_an_incomplete_round_not_a_complete_one(self):
        """⭐ `count is None` ⇒ 判不了拉全 ⇒ `complete=False` + 人话原因（⛔ 不比大小、⛔ 不当拉完）。"""
        stub = _StubClient(rows=[self.hall_row()], count_missing=True)
        rows, complete = jidanpu_upstream.pull_all_pages(stub, path=HALL_PATH)
        self.assertEqual(len(rows), 1)
        self.assertFalse(complete)

    def test_page_cap_stops_loud_and_never_marks_anything_gone(self):
        """⭐ 翻页撞上限（`HALL_MAX_PAGES`，评审 Minor ③-1：此前**零覆盖**）：

        压到 3 页 + 每页 1 行 + 段壳 `count` 远大于真实总量 ⇒ 恰好停在第 3 页、按**不完整**
        处理、reason 是**人话**且点明上限、本轮⛔ 不判任何行「消失」。
        """
        survivor = self.make_mirror_row(order_id="20260928000000000451", game=self.game_a)
        rows = [self.hall_row(order_id=f"2026092800000000046{i}") for i in range(5)]
        stub = self.scripted_pages(rows, per_page=1, per_game_totals={"测试游戏甲": 99})
        with mock.patch.object(jidanpu_upstream, "HALL_MAX_PAGES", 3):
            outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(outcome.complete)
        self.assertEqual(
            [query["page"] for _m, _p, query in stub.calls], [1, 2, 3]
        )
        self.assertEqual(outcome.gone, 0)
        self.assertIn("上限", outcome.reason)
        self.assertIn("3", outcome.reason)
        survivor.refresh_from_db()
        self.assertEqual(survivor.status, JidanpuUpstreamHallOrder.Status.LISTED)

    # ── ⭐ 主钉子：一轮请求序列里不得出现无 gameName 的大厅 GET（D-0366）──────

    def test_a_round_never_sends_a_game_name_less_hall_get(self):
        """⭐⭐ 主钉子（防回潮）：完整同步轮的**每一次**大厅 GET 都带 `gameName`，
        且带的名字与我方游戏字典的**完整 `game.name` 恰好集合相等**。

        - 传简称/截断名会让上游**静默返回 0 行**（GC 22）⇒ 判据打「值 ∈ 完整名集合」：
          任何简称/前缀/派生形式都不在集合里，一出现就红；
        - 无游戏名的整厅 GET 随 D-0366 停用（E-0314：竹屿整厅口恒回 1 张）⇒ 出现即红；
        - ⚠️ 同一游戏会出现**多次**（数据页 + 探页 + 复核都带同一个 `gameName`）——
          判据打「值 ∈ 完整名集合」，⛔ 不钉次数。
        """
        stub = self.scripted_pages([self.hall_row(game_name=GAME_SHORT)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        full_names = {self.game_a.name, self.game_b.name}
        asked = [
            query.get("gameName") for _m, _p, query in stub.calls if "gameName" in query
        ]
        self.assertTrue(asked, "逐游戏路一次都没问上游（网已失效）")
        self.assertEqual(set(asked), full_names)   # 恰好集合 = 完整名；简称不在集合里
        self.assertNotIn(GAME_SHORT, asked)
        no_name_calls = [query for _m, _p, query in stub.calls if "gameName" not in query]
        self.assertEqual(
            no_name_calls, [],
            "出现了无 gameName 的大厅 GET（D-0366 已停用空游戏名查询）",
        )

    # ── Step 2：地址级去重（A2 / spec §11-3）────────────────────────────────

    def test_only_the_owner_binding_talks_to_the_upstream(self):
        """⭐ 同地址两份启用绑定 ⇒ 只有 `id` 最小的那份打上游（第二份**一次请求都不发**）。"""
        stub = self.scripted_pages([self.hall_row()])
        jidanpu_upstream.sync_hall(binding=self.older, client=stub, now=self.now)
        calls_after_owner = len(stub.calls)
        outcome = jidanpu_upstream.sync_hall(binding=self.newer, client=stub, now=self.now)
        self.assertEqual([account for account, _p in stub.logins], [self.older.account])
        self.assertEqual(len(stub.calls), calls_after_owner)
        self.assertFalse(outcome.is_owner)
        self.assertTrue(outcome.reason)

    def test_dedupe_owner_is_the_oldest_enabled_binding_of_the_address(self):
        """`dedupe_owner(address)` = 同地址、**启用**、`id` 最小者；停掉它 ⇒ 轮到下一份。"""
        self.assertEqual(jidanpu_upstream.dedupe_owner(BASE_URL).pk, self.older.pk)
        # ⛔ 别的地址不参与（地址是幂等键的一半）。
        self.assertIsNone(jidanpu_upstream.dedupe_owner(BASE_URL_B))
        self.older.enabled = False
        self.older.save(update_fields=["enabled"])
        self.assertEqual(jidanpu_upstream.dedupe_owner(BASE_URL).pk, self.newer.pk)

    def test_enabled_bindings_only_lists_enabled_ones(self):
        self.newer.enabled = False
        self.newer.save(update_fields=["enabled"])
        self.assertEqual(
            list(jidanpu_upstream.enabled_bindings().values_list("pk", flat=True)),
            [self.older.pk],
        )

    def test_the_mirror_row_owner_follows_the_address_owner(self):
        """`owner_binding` = **当前**负责拉大厅的那一份；最早那份停用 ⇒ 轮给下一份（spec §8.4 / §11-31）。"""
        mirror = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id="20260928000000000801",
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game_a,
            first_seen_at=self.now,
        )
        jidanpu_upstream.sync_hall(
            binding=self.older,
            client=self.scripted_pages([self.hall_row(order_id=mirror.upstream_order_id)]),
            now=self.now,
        )
        mirror.refresh_from_db()
        self.assertEqual(mirror.owner_binding_id, self.older.pk)

        self.older.enabled = False
        self.older.save(update_fields=["enabled"])
        jidanpu_upstream.sync_hall(
            binding=self.newer,
            client=_StubClient(
                rows=[self.hall_row(order_id=mirror.upstream_order_id)],
                account=self.newer.account,
            ),
            now=self.now,
        )
        mirror.refresh_from_db()
        self.assertEqual(mirror.owner_binding_id, self.newer.pk)

    # ── Step 2：镜像行的幂等键与三态（spec §11-1 / §11-4）───────────────────

    def test_a_round_mirrors_each_row_once_and_a_second_round_does_not_duplicate(self):
        """⭐ spec §11-1：一行上家大厅行 ⇒ 恰好一行镜像行；重复跑一轮 ⇒ 不产生第二行。"""
        row = self.hall_row()
        jidanpu_upstream.sync_hall(
            binding=self.binding, client=self.scripted_pages([row]), now=self.now,
        )
        later = self.now.replace(hour=13)
        outcome = jidanpu_upstream.sync_hall(
            binding=self.binding, client=self.scripted_pages([row]), now=later,
        )
        self.assertEqual(JidanpuUpstreamHallOrder.objects.count(), 1)
        mirror = JidanpuUpstreamHallOrder.objects.get()
        self.assertEqual(mirror.game_id, self.game_a.pk)
        self.assertEqual(mirror.last_seen_at, later)
        self.assertEqual(outcome.listed, 1)

    def test_overwide_project_text_is_clipped_to_the_mirror_column_width(self):
        """⭐ 修复轮 M-1：镜像投影的 `project_text` 按**镜像列宽 300** 截断，且这一轮照常落行。

        上游给 301 字的项目（合成夹具）⇒ 不截断时 PostgreSQL 会以 `value too long` 拒掉
        整轮同步。⚠️ 期望里的 **300 / 301 是字面值**（模型契约），⛔ 不从 `_MIRROR_FIELD_MAX`
        派生——派生会让「截断被删掉 / 被抬宽」时**恒绿**（E-0024）。
        原样那一行完整留在 `upstream_payload`（spec §4.3）⇒ 截断**只影响投影**，不是丢数据。
        """
        row = self.hall_row(project="P" * 301)
        outcome = jidanpu_upstream.sync_hall(
            binding=self.binding, client=self.scripted_pages([row]), now=self.now,
        )
        self.assertEqual(outcome.listed, 1)
        mirror = JidanpuUpstreamHallOrder.objects.get()
        self.assertEqual(len(mirror.project_text), 300)
        self.assertEqual(mirror.project_text, "P" * 300)
        #: 原样行仍完整留在 `upstream_payload` ⇒ 截断只发生在投影列上。
        self.assertEqual(mirror.upstream_payload["project"], "P" * 301)

    def test_order_id_view_collision_does_not_merge_two_orders(self):
        """⭐ spec §11-4（B4）：两行 `orderIdView` 相同、`orderId` 不同 ⇒ 两行镜像、⛔ 不互相覆盖。"""
        rows = [
            self.hall_row(order_id="20260928114654544407", view="SAME-VIEW"),
            self.hall_row(order_id="202609281148072240", view="SAME-VIEW"),
        ]
        jidanpu_upstream.sync_hall(
            binding=self.binding, client=self.scripted_pages(rows), now=self.now,
        )
        self.assertEqual(JidanpuUpstreamHallOrder.objects.count(), 2)
        self.assertEqual(
            set(JidanpuUpstreamHallOrder.objects.values_list("upstream_order_id", flat=True)),
            {"20260928114654544407", "202609281148072240"},
        )

    def test_missing_order_id_row_is_skipped_with_a_reason(self):
        """缺 `orderId` 的行无法做幂等键 ⇒ ⛔ 不落镜像行 + 一条人话台账（D-0366：该行在甲段里）。"""
        row = self.hall_row(order_id="")
        row["gameName"] = "测试游戏甲"
        del row["orderId"]
        jidanpu_upstream.sync_hall(
            binding=self.binding,
            client=self.scripted_pages([row]),
            now=self.now,
        )
        self.assertEqual(JidanpuUpstreamHallOrder.objects.count(), 0)
        self.assertTrue(JidanpuSyncEvent.objects.get(kind="upstream.hall_skipped").reason)

    def test_missing_order_id_note_is_written_once_not_every_round(self):
        """⭐ 缺 `orderId` 的台账也走**幂等写口**（评审 Minor ③-2：此前用 `_record` 每轮一条）。

        同一句原因重复三轮 ⇒ 只留**一条**（那正是 `note_once` 的存在理由）。
        """
        row = self.hall_row(order_id="")
        row["gameName"] = "测试游戏甲"
        del row["orderId"]
        for _ in range(3):
            jidanpu_upstream.sync_hall(
                binding=self.binding, client=self.scripted_pages([row]), now=self.now,
            )
        events = JidanpuSyncEvent.objects.filter(kind="upstream.hall_skipped")
        self.assertEqual(events.count(), 1)
        self.assertIn("orderId", events.get().reason)
        self.assertEqual(events.get().target_ref, self.binding.jidanpu_base_url)

    def test_a_gone_row_seen_again_is_revived_on_sight(self):
        """⭐ D-0300（2026-10-24 业务方拍板）：`gone` 行重新见到 ⇒ **立即复活为 `listed`**。

        复活分支照 `rescue_gone_mirrors` 命令的同一套语义——清 `gone_at`、刷新投影、
        记 `upstream.hall_rescued` 台账；复活分支**不再**写「终态再现」的
        `upstream.hall_skipped` 台账（复活台账就是记录）。
        ⛔ `promoted` 那一支仍不复活（见下一条：复活会造成重复接单）。
        """
        row_id = "20260928000000000421"
        mirror = self.make_mirror_row(
            order_id=row_id, status=JidanpuUpstreamHallOrder.Status.GONE, game=self.game_a,
        )
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            gone_at=self.now - timedelta(hours=1), project_text="判 gone 前的旧投影",
        )
        stub = self.scripted_pages([
            self.hall_row(order_id=row_id, project="本轮刷新的投影", rec_fee="66.00"),
        ])
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertIsNone(mirror.gone_at)
        self.assertEqual(mirror.project_text, "本轮刷新的投影")   # 投影按本轮行内容刷新
        self.assertEqual(mirror.remote_rec_fee, Decimal("66.00"))
        self.assertEqual(mirror.last_seen_at, self.now)
        self.assertEqual(outcome.listed, 1)
        rescued = JidanpuSyncEvent.objects.filter(kind="upstream.hall_rescued")
        self.assertEqual(rescued.count(), 1)
        self.assertEqual(rescued.get().target_ref, f"{BASE_URL}/{row_id}")
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind="upstream.hall_skipped", target_ref=f"{BASE_URL}/{row_id}",
                reason=jidanpu_upstream.TERMINAL_REAPPEAR_REASON,
            ).exists(),
            "复活分支不该再写「终态再现」台账（upstream.hall_rescued 就是记录）",
        )

    def test_a_promoted_row_is_never_revived(self):
        """⛔ D-0300 保留的一支：`promoted` 仍终态不复活——只碰 `last_seen_at` + 台账。

        promoted 行已转成**我方正式订单**，复活会造成重复接单，这条口子不能开。
        """
        row_id = "20260928000000000431"
        mirror = self.make_mirror_row(
            order_id=row_id, status=JidanpuUpstreamHallOrder.Status.PROMOTED, game=self.game_a,
        )
        stub = self.scripted_pages([self.hall_row(order_id=row_id)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(mirror.last_seen_at, self.now)   # 只刷新「最近仍在大厅」
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(
                kind="upstream.hall_skipped", target_ref=f"{BASE_URL}/{row_id}",
                reason=jidanpu_upstream.TERMINAL_REAPPEAR_REASON,
            ).exists(),
            "promoted 再现仍只记「终态再现」台账",
        )
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_rescued").exists()
        )

    def test_terminal_reappear_adds_one_note_and_the_same_reason_never_adds_a_second(self):
        """⭐ 台账幂等键**含 `reason`**（评审修复轮 ①，两个方向都钉）：

        - **换了原因 ⇒ 新记一条**：同一张镜像单先因「游戏对不上」有一条 `upstream.hall_skipped`，
          之后它成终态又出现 ⇒ 必须**多出**一条「终态再现」（⛔ 不被前一句同 kind 的台账吞掉）；
        - **同一句原因重复 ⇒ 不新增**：再跑一轮同样的终态再现，行数**不变**。

        ⛔ 键里去掉 `reason` ⇒ 第一段就红（后一句被前一句静默吞掉，实测过 2→2）。
        ⭐ **D-0300夹具改准**：终态再现台账今天只剩 `promoted` 那一支会写
        （`gone` 重新见到即自动复活、走 `upstream.hall_rescued`）⇒ 夹具从 `gone` 换成
        `promoted`——本用例钉的「幂等键含 reason」性质不变。
        """
        mirror = self.make_mirror_row(
            order_id="20260928000000000411", status=JidanpuUpstreamHallOrder.Status.PROMOTED,
            game=self.game_a,
        )
        ref = f"{mirror.upstream_base_url}/{mirror.upstream_order_id}"
        #: 先在同一个 `target_ref` 上放一条**别的结论**（它曾经因游戏对不上被跳过）。
        jidanpu_upstream._record(
            direction="in", kind="upstream.hall_skipped", status="skipped",
            target_type="hall", target_ref=ref, reason="上家游戏「测试游戏丙」在我方对不上",
        )
        self.assertEqual(JidanpuSyncEvent.objects.filter(target_ref=ref).count(), 1)

        stub = self.scripted_pages([self.hall_row(order_id=mirror.upstream_order_id)])
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        events = JidanpuSyncEvent.objects.filter(
            kind="upstream.hall_skipped", target_ref=ref,
            reason=jidanpu_upstream.TERMINAL_REAPPEAR_REASON,
        )
        self.assertEqual(events.count(), 1)                       # 换了原因 ⇒ 新记一条
        self.assertEqual(JidanpuSyncEvent.objects.filter(target_ref=ref).count(), 2)

        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertEqual(events.count(), 1)                       # 同一句原因 ⇒ 不新增
        self.assertEqual(JidanpuSyncEvent.objects.filter(target_ref=ref).count(), 2)

    def test_a_claiming_row_is_not_pulled_back_by_a_pull_round(self):
        """⭐ `claiming`（正在接单 / 转正）的行：拉取轮只碰 `last_seen_at`，⛔ 不重算投影、不改状态。

        评审 Minor ③-5：这一支此前**零用例**——把 `_HALL_CLAIMING` 那一段删掉（落回 `listed`
        的刷新分支）**不会红**，而症状是「正在接单的单被拉取轮改写」。
        """
        mirror = self.make_mirror_row(
            order_id="20260928000000000471",
            status=JidanpuUpstreamHallOrder.Status.CLAIMING, game=self.game_a,
        )
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(project_text="占住时的项目")
        later = self.now.replace(hour=13)
        jidanpu_upstream.sync_hall(
            binding=self.binding,
            client=self.scripted_pages([
                self.hall_row(order_id=mirror.upstream_order_id, project="上游改过的项目"),
            ]),
            now=later,
        )
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        self.assertEqual(mirror.project_text, "占住时的项目")   # ⛔ 投影没被重算
        self.assertEqual(mirror.last_seen_at, later)            # ⚠️ 只刷新「最近仍在大厅」
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(
                kind="upstream.hall_skipped",
                target_ref=f"{mirror.upstream_base_url}/{mirror.upstream_order_id}",
            ).exists()
        )

    # ── Step 3：拉不全 ⇒ ⛔ 不判「消失」（spec §5.1 / §10）────────────────────

    def test_an_incomplete_round_does_not_mark_anything_gone(self):
        """⭐ 本轮**拉不全** ⇒ 断言行还留在 `listed`（⛔ 不许把「没看到」当「消失了」）。"""
        self.make_mirror_row(order_id="20260928000000000301", game=self.game_a)
        stub = _StubClient(
            rows=[self.hall_row(order_id="20260928000000000302")], count_missing=True,
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(outcome.complete)
        self.assertTrue(outcome.reason)
        self.assertEqual(outcome.gone, 0)
        self.assertEqual(
            JidanpuUpstreamHallOrder.objects.get(
                upstream_order_id="20260928000000000301"
            ).status,
            JidanpuUpstreamHallOrder.Status.LISTED,
        )

    def test_a_short_reported_count_probes_the_next_page_and_never_goes_gone(self):
        """⭐ 修复轮 ③（加固）：累计**到了**上家自报的 `count` 就停 ⇒ 再**探一页**确认。

        夹具：甲段真实 45 行、段壳 `count=40` ⇒ 第 2 页累计到 40 == `count` 就停；**探第 3 页**
        拿到 5 行 ⇒ `count` 偏小 ⇒ 本轮判**不完整**（⛔ 不判任何行「消失」）+ 人话原因。
        ⚠️ 甲段的请求页号序列**钉死为 `[1, 2, 3]`**（3 = 探页）——⛔ 不是「一路翻到底」。
        ⚠️ 那张 `listed` 行的单号**刻意落在夹具行之外**（否则行会被刷新、`gone` 断言恒真）。
        """
        survivor = self.make_mirror_row(order_id="20260928000000000701", game=self.game_a)
        rows = [
            self.hall_row(order_id=f"202609280000000008{i:02d}") for i in range(45)
        ]
        stub = self.scripted_pages(rows, per_game_totals={"测试游戏甲": 40})
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(outcome.complete)
        self.assertEqual(outcome.gone, 0)
        #: 人话原因要能看出「为什么这轮不算拉全」（⛔ 不是「未达上家自报的总数」那句
        #: ——那在本档是**假话**：我们恰恰达到了那个数）。
        self.assertIn("再探一页", outcome.reason)
        self.assertIn("消失", outcome.reason)
        survivor.refresh_from_db()
        self.assertEqual(survivor.status, JidanpuUpstreamHallOrder.Status.LISTED)
        #: ⭐ 甲段的页号序列逐字钉住（乙段 0 行、另有探页，不在本断言里）。
        self.assertEqual(
            [q["page"] for _m, _p, q in stub.calls if q.get("gameName") == "测试游戏甲"],
            [1, 2, 3],
        )

    def test_a_correct_reported_count_probes_and_then_still_marks_the_missing_row_gone(self):
        """⭐ 修复轮 ③（加固）的**阴性对照**：段自报总数**正确**、累计恰好等于 `count` ⇒ 探一页是
        **空页** ⇒ 照判「拉全」，该标 `gone` 的照样标（⛔ 探页不是「只要到数就不判消失」）。

        ⛔ 少了这一条，「探页」就退化成「消失判定永远不生效」——那是把保守做成了瘫痪。
        ⚠️ `survivor` 的单号**就在**夹具行里（本轮仍出现 ⇒ 不该被标 `gone`），`vanished` 的
        单号**不在**夹具行里（⇒ 段查复核也不见 ⇒ 必须被标 `gone`）——两个方向都有判据。
        """
        rows = [
            self.hall_row(order_id=f"202609280000000009{i:02d}") for i in range(40)
        ]
        survivor = self.make_mirror_row(
            order_id="20260928000000000905", game=self.game_a,
        )
        vanished = self.make_mirror_row(order_id="20260928000000000602", game=self.game_a)
        stub = self.scripted_pages(rows)
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.reason, "")
        self.assertEqual(outcome.gone, 1)
        vanished.refresh_from_db()
        self.assertEqual(vanished.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertEqual(vanished.gone_at, self.now)
        #: 探到的空页**不吞掉**已拉到的行 ⇒ 本轮出现的 `survivor` 照旧 `listed`、只刷新时刻。
        survivor.refresh_from_db()
        self.assertEqual(survivor.status, JidanpuUpstreamHallOrder.Status.LISTED)
        #: 甲段的页号序列：3 个数据页 + 1 个探页（本轮）+ 复核那一跳的 3 页 + 1 探页。
        self.assertEqual(
            [q["page"] for _m, _p, q in stub.calls if q.get("gameName") == "测试游戏甲"],
            [1, 2, 3, 1, 2, 3],
        )

    def test_a_page_that_overshoots_the_reported_count_also_probes_and_never_goes_gone(self):
        """⭐ 修复轮 ③（加固）：**累计超过** `count`（单页就把上报总数撑爆）同样是「可疑」。

        夹具：甲段真实 25 行、段壳 `count=10` ⇒ 第 1 页就给了 20 行 > 10 ⇒ 再探一页拿到 5 行
        ⇒ 判不完整、⛔ 不判消失。⚠️ 这条钉死「可疑」的成因是**我们因为 `count` 而停**（≥ count），
        ⛔ **不是**只认「累计恰好 `== count`」。
        """
        survivor = self.make_mirror_row(order_id="20260928000000000401", game=self.game_a)
        rows = [
            self.hall_row(order_id=f"202609280000000005{i:02d}") for i in range(25)
        ]
        stub = self.scripted_pages(rows, per_game_totals={"测试游戏甲": 10})
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(outcome.complete)
        self.assertEqual(outcome.gone, 0)
        self.assertIn("再探一页", outcome.reason)
        survivor.refresh_from_db()
        self.assertEqual(survivor.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(
            [query["page"] for _m, _p, query in stub.calls], [1, 2],
        )

    def test_a_complete_round_marks_the_missing_rows_gone_without_deleting_them(self):
        """拉全了、某 `listed` 行没出现 ⇒ 段查复核仍不见 ⇒ 标 `gone` + 台账（⛔ 不删行，留痕）。"""
        survivor = self.make_mirror_row(order_id="20260928000000000401", game=self.game_a)
        vanished = self.make_mirror_row(order_id="20260928000000000402", game=self.game_a)
        survivor_row = self.hall_row(order_id=survivor.upstream_order_id)
        stub = self.scripted_pages([survivor_row])
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.gone, 1)
        vanished.refresh_from_db()
        self.assertEqual(vanished.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertEqual(vanished.gone_at, self.now)
        self.assertEqual(JidanpuUpstreamHallOrder.objects.count(), 2)
        self.assertTrue(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").exists()
        )

    # ── Step 3.5：消失判定的段查复核（⭐ D-0366 换原语；E-0257 的窗口仍被兜住）──

    def test_a_withdraw_plus_add_shift_gets_caught_by_the_segment_recheck(self):
        """⭐ E-0257（R3-U1）主钉：段内「撤一行 + 加一行」分页窗口 ⇒ 在挂单**不**被误标 gone。

        构造照探针搬进测试：甲段 60 行（3 整页），第 2 页**取完后**上家撤掉索引 25 那行 +
        追加一张新单（净 count 不变）⇒ 原索引 40 那行前移进已拉过的第 2 页 ⇒ 它本轮不在
        甲段行里、后续页也不再出现，而翻页算术恰好闭合（`complete=True`）。
        ⭐ D-0366：候选在落终态前用**一跳新鲜段查复核**（同一段重查一次，撤/加只发生在
        第 2 次调用）⇒ 复核里它还在 ⇒ 不标、按既有非终态路径刷新。
        ⭐ D-0300 追注：误标了也有复活兜第二道；本用例钉的复核仍是**第一道闸**。
        """
        target_id = "20260928000000004040"
        rows = [self.hall_row(order_id=f"202609280000000040{i:02d}") for i in range(60)]
        #: 夹具刻意让 `last_seen_at` 落后一小时 ⇒ 「复核命中 ⇒ 走非终态刷新」那一半判据才钉得住。
        mirror = self.make_mirror_row(
            order_id=target_id, game=self.game_a, first_seen_at=self.now - timedelta(hours=1),
        )
        stub = self.scripted_pages(
            rows, mutate_after_calls={2}, withdraw_index=25,
        )

        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)

        #: 前提判据：窗口确实闭合了（否则这条测的不是那个洞）。
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.reason, "")
        #: 请求时序 = 轮拉取 3 数据页 + 1 探页（第 2 页取完后发生撤/加）＋ 复核一跳（4 次调用）。
        self.assertEqual(
            [q["page"] for _m, _p, q in stub.calls if q.get("gameName") == "测试游戏甲"],
            [1, 2, 3, 4, 1, 2, 3, 4],
        )
        self.assertEqual(outcome.gone, 0)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        #: 复核命中 ⇒ 走既有非终态路径：`last_seen_at` 刷新到本轮。
        self.assertEqual(mirror.last_seen_at, self.now)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").exists()
        )

    def test_a_failing_recheck_skips_the_candidate_instead_of_marking_it_gone(self):
        """⭐ 复核没答上来（`ApiError`）⇒ 本轮**跳过**该候选（宁漏标勿误标，D-0366 保留）。

        夹具：甲段本轮查询成功（第 1 次问）⇒ 候选成立；**复核**（第 2 次问）失败
        （`fail_game_names_from_ask={甲: 2}`）⇒ 跳过、不落终态、不留「已消失」台账。
        修前（key1 时代）那条「回查失败 ⇒ 跳过」的 fail-closed 口径原样保留。
        """
        vanished = self.make_mirror_row(order_id="20260928000000004102", game=self.game_a)
        stub = _StubClient(
            rows=[self.hall_row(order_id="20260928000000004101")],
            account=self.older.account,
            fail_game_names_from_ask={"测试游戏甲": 2},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.gone, 0)
        vanished.refresh_from_db()
        self.assertEqual(vanished.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertIsNone(vanished.gone_at)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").exists()
        )

    # ── Step 2：按游戏可筛（spec §11-29 / A15 第 4 条）──────────────────────

    def test_rows_are_filterable_by_game_like_the_other_two_streams(self):
        """⭐ `upstream_hall_rows(game_id=<甲>)` 只回甲那一款的行。"""
        self.make_mirror_rows(games=[self.game_a, self.game_b])
        rows = jidanpu_upstream.upstream_hall_rows(game_id=self.game_a.pk)
        self.assertEqual([row["game"] for _moment, row in rows], [self.game_a.pk])

    def test_rows_returned_by_the_read_port_carry_the_owner_and_the_listed_status(self):
        """读口只回 `listed` 行，且行里带 `game`（FK）与游戏名分组用的 FK id。"""
        listed = self.make_mirror_row(order_id="20260928000000000501", game=self.game_a)
        self.make_mirror_row(
            order_id="20260928000000000502", status=JidanpuUpstreamHallOrder.Status.GONE,
            game=self.game_a,
        )
        rows = jidanpu_upstream.upstream_hall_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], listed.first_seen_at)
        self.assertEqual(rows[0][1]["game"], self.game_a.pk)
        self.assertEqual(rows[0][1]["remote_order_id"], listed.upstream_order_id)

    def test_read_port_keyword_also_searches_the_remote_order_view(self):
        """⭐ 关键词也搜**展示单号**（`orderIdView`，评审 Minor ③-4：此前漏搜、P2 是搜的）。

        夹具让 `orderIdView` 与 `orderId` **不同** ⇒ 只有真的搜了原样行里那个键才能命中。
        """
        self.make_mirror_row(
            order_id="20260928000000000511", game=self.game_a,
            upstream_payload={"orderId": "20260928000000000511", "orderIdView": "VIEW-KEY-777"},
        )
        rows = jidanpu_upstream.upstream_hall_rows(keyword="KEY-777")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1]["remote_order_view"], "VIEW-KEY-777")

    # ── Step 4：两路归并（行形状恰好同键 + 并列次序；读口那一路照旧）──────────

    def test_the_two_streams_share_exactly_one_row_shape(self):
        """⭐ 两路的行键**恰好相等**（与 `HallListSerializer.Meta.fields` 对拍），`origin` 两档可分辨。

        ⛔ **2026-10-08 改准（原名 `test_three_streams_share_exactly_one_row_shape`）**：
        原第三路 = P2（租户级）镜像单，它随租户级鸡蛋铺线整条拆除 ⇒ 只剩原生 + 本线两路。
        ⚠️ 「恰好同键」这条判据的文字一个字没改，只把三档改成两档。
        """
        mirror = self.make_mirror_row(
            order_id="20260928000000000601", game=self.game_a, rec_fee="12.34",
        )
        row = jidanpu_upstream.serialize_upstream_row(mirror)
        self.assertEqual(set(row), MERGED_ROW_KEYS)
        # 原生那一路的行 = `REMOTE_ROW_BASE` ∪ 那 7 个合并键，且**恰好等于**本线的行键
        # （两路复用同一个基底 ⇒ 必然同键）。
        self.assertEqual(
            set(REMOTE_ROW_BASE) | MERGE_ADDITION_KEYS, MERGED_ROW_KEYS
        )
        self.assertEqual(row["origin"], "upstream")
        self.assertEqual(row["mirror_pk"], mirror.pk)
        self.assertEqual(row["game"], self.game_a.pk)
        self.assertEqual(row["remote_order_id"], mirror.upstream_order_id)
        self.assertEqual(row["hall_price"], "12.34")

    def test_merge_puts_native_then_upstream_on_a_tie(self):
        """并列时次序 = 原生 → 本线（⛔ 与构造顺序无关）。"""
        moment = self.now
        native = [(moment, {"origin": "native"})]
        upstream = [(moment, {"origin": "upstream"})]
        self.assertEqual(
            [row["origin"] for row in jidanpu_upstream.merge_hall_rows_two(
                native, upstream)],
            ["native", "upstream"],
        )
        self.assertEqual(
            [row["origin"] for row in jidanpu_upstream.merge_hall_rows_two(
                list(reversed(native)), list(reversed(upstream)))],
            ["native", "upstream"],
        )

    def test_merge_sorts_by_the_time_the_row_entered_the_hall(self):
        """两路统一按「进入大厅的时刻」倒序（原生 `created_at` / 本线 `first_seen_at` 同义）。"""
        early = self.now
        late = self.now + timedelta(minutes=5)
        merged = jidanpu_upstream.merge_hall_rows_two(
            [(early, {"origin": "native"})],
            [(early + timedelta(minutes=1), {"origin": "upstream"})],
        )
        self.assertEqual(
            [row["origin"] for row in merged], ["upstream", "native"],
        )
        # 正序输入也一样（⛔ 不依赖「构造顺序 + 稳定排序」）：
        merged_reversed = jidanpu_upstream.merge_hall_rows_two(
            [(late, {"origin": "native"})],
            [(early, {"origin": "upstream"})],
        )
        self.assertEqual(
            [row["origin"] for row in merged_reversed], ["native", "upstream"],
        )

    def test_the_two_sources_can_be_merged_through_the_real_read_port(self):
        """把两路的**真行**（本线走读口）喂进归并 ⇒ 两档 `origin` 都在、键集仍恰好一致。"""
        mirror = self.make_mirror_row(
            order_id="20260928000000000701", game=self.game_a, rec_fee="12.34",
        )
        base = jidanpu_upstream.serialize_upstream_row(mirror)
        upstream_rows = jidanpu_upstream.upstream_hall_rows()
        native = [(self.now, dict(base, origin="native"))]
        merged = jidanpu_upstream.merge_hall_rows_two(native, upstream_rows)
        self.assertEqual(
            {row["origin"] for row in merged}, {"native", "upstream"},
        )
        for row in merged:
            self.assertEqual(set(row), MERGED_ROW_KEYS)


class TailFastPathRetiredTests(TenantTestCase):
    """⭐ D-0366：大厅尾部快路整体退役（它的取数形状就是无游戏名 GET）。

    旧 `UpstreamHallTailFastPathTests` 的四条行为判据（冷启动探页 / 快路不判消失 /
    无 count 不做镜像 / 快路也去重）随快路**本身**消失：前两条的回归由「主钉子
    （无 gameName GET 零次）+ 本类的结构性钉」守住，后两条的判据（去重、count 三态）
    由全量路自己的用例继续承重。本类钉**退役本身**：函数、常量、缓存、调度锚、
    `sync_hall` 的 `tail_pages` 参数——一个都不许回来。
    """

    def test_the_module_has_no_tail_fast_path_left(self):
        from apps.business.management.commands import jidanpu_sync

        for name in (
            "HALL_TAIL_SECONDS", "HALL_TAIL_PAGES", "HALL_TAIL_NO_TAIL_REASON",
            "HALL_TAIL_PARTIAL_REASON", "_HALL_TAIL", "_hall_tail_key",
            "_fetch_hall_tail", "fetch_hall_page",
        ):
            self.assertFalse(
                hasattr(jidanpu_upstream, name),
                f"jidanpu_upstream.{name} 又回来了（尾部快路已随 D-0366 退役）",
            )
        self.assertNotIn(
            "tail_pages", inspect.signature(jidanpu_upstream.sync_hall).parameters,
            "sync_hall 又长出了 tail_pages 参数（D-0366 已删快路支）",
        )
        for name in ("fetch_hall_orders_page", "_hall_orders_params", "HallPage",
                     "HALL_ORDER_STATUS_GRABBING"):
            self.assertFalse(
                hasattr(jidanpu, name),
                f"jidanpu.{name} 又回来了（换源大厅路的死代码已随 D-0366 清除）",
            )
        self.assertFalse(
            hasattr(jidanpu_sync, "_UPSTREAM_HALL_TAIL_AT"),
            "调度侧的快路锚 _UPSTREAM_HALL_TAIL_AT 又回来了",
        )


class UpstreamPerGamePullTests(TenantTestCase):
    """⭐ D-0366：逐游戏段查是**全量路的本体**（D-0266 机制升格）。

    判据三条：
      ① 段查是我方游戏字典的**每一只**（query 带完整 `game.name` 逐字），行并入同一轮；
      ② 任一段查询失败/未判全 ⇒ **整轮 `complete=False`、`_mark_gone` 不跑**（H2
         fail-closed），但**已判全段拉到的行仍照常落镜像**（下一轮不再重复拉）；
      ③ 跳过态（D-0321）剔除的游戏在判完整的轮里被日志点名（其失败不计入完整性）。
    ⚠️ 夹具合成值：游戏名沿用 `重返未来：1999` 那对形状（头注），单号全零段。
    """

    def setUp(self):
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=dt_timezone.utc)
        self.user = User.objects.create_user(username="up-pergame", password="pw-pergame")
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=self.user, jidanpu_base_url=BASE_URL, account="acc-pergame",
            password=PASSWORD, enabled=True,
        )
        #: 我方游戏字典：**完整名**那只（段查 query 里带的正是它，逐字）。
        self.game_full = Game.objects.create(name=GAME_FULL)

    def _hall_row(self, *, order_id, game_name):
        return {
            "orderId": order_id,
            "orderIdView": f"VIEW-{order_id}",
            "gameName": game_name,
            "gameArea": "安卓", "gameServer": "微信区",
            "project": "合成项目", "orderRemark": "合成备注", "roleName": "合成角色",
            "recFee": "88.00", "createTime": "2026-09-28 11:46:54",
        }

    def test_game_rules_block_new_cards_but_keep_old_card_terms(self):
        old_game = Game.objects.create(name="测试保留游戏")
        Game.objects.create(name="测试未选游戏")
        old = self._hall_row(order_id="SYN-OLD", game_name=old_game.name)
        accepted = self._hall_row(order_id="SYN-NEW", game_name=self.game_full.name)
        not_selected = self._hall_row(order_id="SYN-BLOCKED", game_name=old_game.name)
        self.binding.game_rules = [{"game_id": old_game.pk, "hall_price_percent": 40}]
        self.binding.save(update_fields=["game_rules"])
        jidanpu_upstream.sync_hall(
            binding=self.binding, client=_StubClient(rows=[old]), now=self.now,
        )

        self.binding.game_rules = [{"game_id": self.game_full.pk, "hall_price_percent": 62}]
        self.binding.save(update_fields=["game_rules"])
        stub = _StubClient(rows=[old, accepted, not_selected], account=self.binding.account)
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        cards = {card.upstream_order_id: card for card in JidanpuUpstreamHallOrder.objects.all()}
        self.assertEqual(set(cards), {"SYN-OLD", "SYN-NEW"})
        self.assertEqual(cards["SYN-OLD"].game_id, old_game.pk)
        self.assertEqual(cards["SYN-OLD"].hall_price_percent_at_sync, 40)
        self.assertEqual(cards["SYN-NEW"].hall_price_percent_at_sync, 62)
        self.assertEqual(jidanpu_upstream.serialize_upstream_row(cards["SYN-OLD"])["hall_price"], "35.20")
        self.assertEqual(jidanpu_upstream.serialize_upstream_row(cards["SYN-NEW"])["hall_price"], "54.56")
        self.assertTrue(jidanpu_upstream.upstream_hall_price_in_range(
            cards["SYN-NEW"], price_max=Decimal("55.00"),
        ))

    def test_unmapped_cards_only_enter_hall_under_current_rule_when_first_matched(self):
        rows = [
            self._hall_row(order_id="SYN-RULE-SKIPPED", game_name=self.game_full.name),
            self._hall_row(order_id="SYN-RULE-GONE", game_name=self.game_full.name),
        ]
        for row, status in zip(rows, ("skipped", "gone"), strict=True):
            JidanpuUpstreamHallOrder.objects.create(
                owner_binding=self.binding, upstream_base_url=self.binding.jidanpu_base_url,
                upstream_order_id=row["orderId"], game=None, status=status,
                first_seen_at=self.now, last_seen_at=self.now,
            )
        self.binding.game_rules = [{"game_id": self.game_full.pk, "hall_price_percent": 27}]
        self.binding.save(update_fields=["game_rules"])
        jidanpu_upstream.sync_hall(
            binding=self.binding, client=_StubClient(rows=rows, account=self.binding.account),
            now=self.now + timedelta(minutes=1),
        )
        for card in JidanpuUpstreamHallOrder.objects.all():
            self.assertEqual(card.status, JidanpuUpstreamHallOrder.Status.LISTED)
            self.assertEqual(card.game_id, self.game_full.pk)
            self.assertEqual(card.hall_price_percent_at_sync, 27)

    def test_every_dictionary_game_is_pulled_per_game_with_full_names(self):
        """① 两只游戏 ⇒ 两个段都被查询（query 值 = 完整名逐字）；行落镜像。"""
        game_other = Game.objects.create(name="测试游戏丙")
        row_full = self._hall_row(order_id="20260928000000004551", game_name=GAME_FULL)
        row_other = self._hall_row(order_id="20260928000000004552", game_name="测试游戏丙")
        stub = _StubClient(rows=[row_full, row_other], account=self.binding.account)
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.pulled, 2)
        self.assertEqual(outcome.listed, 2)
        self.assertTrue(
            JidanpuUpstreamHallOrder.objects.filter(
                upstream_order_id="20260928000000004551",
                game=self.game_full, status=JidanpuUpstreamHallOrder.Status.LISTED,
            ).exists()
        )
        self.assertTrue(
            JidanpuUpstreamHallOrder.objects.filter(
                upstream_order_id="20260928000000004552",
                game=game_other, status=JidanpuUpstreamHallOrder.Status.LISTED,
            ).exists()
        )
        asked = {q["gameName"] for _m, _p, q in stub.calls if "gameName" in q}
        self.assertEqual(asked, {GAME_FULL, "测试游戏丙"})

    def test_a_failing_per_game_query_never_marks_anything_gone(self):
        """② 乙段查询失败 ⇒ 整轮 `complete=False` + 人话原因；甲段的行照常落库、谁都不判消失。"""
        at_risk = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id="20260928000000004599",
            status=JidanpuUpstreamHallOrder.Status.LISTED,
            game=self.game_full,
            owner_binding=self.binding,
            first_seen_at=self.now - timedelta(hours=1),
        )
        Game.objects.create(name="测试游戏乙")
        landed_row = self._hall_row(
            order_id="20260928000000004551", game_name=GAME_FULL,
        )
        stub = _StubClient(
            rows=[landed_row],
            account=self.binding.account,
            fail_game_names={"测试游戏乙"},
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        self.assertFalse(outcome.complete)
        self.assertIn("逐游戏", outcome.reason)
        self.assertIn("测试游戏乙", outcome.reason)
        self.assertEqual(outcome.gone, 0)
        at_risk.refresh_from_db()
        self.assertEqual(at_risk.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertIsNone(at_risk.gone_at)
        #: 已判全段（甲）拉到的行**仍照常落镜像**（下一轮不再重复拉——拉取与消失判定解耦）。
        self.assertTrue(
            JidanpuUpstreamHallOrder.objects.filter(
                upstream_order_id="20260928000000004551",
                status=JidanpuUpstreamHallOrder.Status.LISTED,
            ).exists(),
            "完整段的行没有落镜像（拉取被失败段连坐了）",
        )
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_gone").exists()
        )

    def test_a_zero_row_segment_is_evidence_and_skip_state_exits_are_named_in_the_log(self):
        """③ D-0321 跳过态剔除的游戏在判完整的轮里被日志点名（行为与 D-0366 前一致）。"""
        from unittest import mock as _mock

        skip_states = jidanpu_upstream._ZERO_ROW_GAME_SKIP.setdefault(
            jidanpu_upstream._hall_round_key(self.binding), {}
        )
        skip_states[self.game_full.pk] = jidanpu_upstream._ZeroRowGameSkip(
            consecutive_failures=3, skip=True,
            last_probe_at=self.now,
        )
        try:
            with self.assertLogs("apps.business.jidanpu_upstream", level="INFO") as cm:
                stub = _StubClient(account=self.binding.account)
                outcome = jidanpu_upstream.sync_hall(
                    binding=self.binding, client=stub,
                    now=self.now + jidanpu_upstream.ZERO_ROW_GAME_PROBE_EVERY // 2,
                )
            self.assertEqual(
                [q for _m, _p, q in stub.calls if q.get("gameName") == GAME_FULL],
                [],
                "未到探测点的跳过态游戏不该被查询",
            )
            self.assertTrue(outcome.complete)
            self.assertTrue(
                any("剔除" in r.getMessage() and GAME_FULL in r.getMessage()
                    for r in cm.records),
                "判完整的轮没有把剔除的跳过态游戏名写进日志",
            )
        finally:
            jidanpu_upstream._ZERO_ROW_GAME_SKIP.clear()


class SyncHallClaimingReclaimerTests(TenantTestCase):
    """⭐ D-0335 清道夫（「消掉抢单锁定」）：`sync_hall` 行循环对 `claiming` 残行的放回支。

    判据三条（⛔ 少一条就是误伤）：
    1. 行 `claiming` 且其 `upstream.hall_claim` 事件已**终态**（`done` / `skipped` /
       `abandoned`——按 `payload__mirror_id` 找，最新一条）；
    2. 链路（`JidanpuUpstreamOrderLink`）不存在——有链路说明已转正过，清道夫不碰；
    3. `failed` 且还有额度的事件**不碰**：那是后台轮正在重试的崩溃恢复路
       （`_consume_hall_claim` 的 `claimed` 断点续转正）。

    命中 ⇒ 回 `listed` + 清两接单列 + 删离厅灰卡 + `upstream.hall_claim_released` 台账
    + 隐藏单补抛（照复活两支同一纪律）。
    """

    def setUp(self):
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=dt_timezone.utc)
        owner = User.objects.create_user(username="up-reclaimer", password=PASSWORD)
        self.binding = JidanpuUpstreamBinding.objects.create(
            user=owner, jidanpu_base_url=BASE_URL, account="acc-reclaim",
            password=PASSWORD, enabled=True,
        )
        self.game = Game.objects.create(name="测试游戏甲")
        self.taker = User.objects.create_user(
            username="up-reclaim-taker", password=PASSWORD
        )

    def hall_row(self, order_id):
        """一份上家大厅行（同 `UpstreamHallPullTests.hall_row` 的合成形状）。"""
        return {
            "orderId": order_id,
            "orderIdView": f"VIEW-{order_id}",
            "gameName": "测试游戏甲",
            "gameArea": "安卓",
            "gameServer": "微信区",
            "project": "合成项目",
            "orderRemark": "合成备注",
            "roleName": "合成角色",
            "recFee": "88.00",
            "createTime": "2026-09-28 11:46:54",
        }

    def make_claiming_row(self, *, order_id="20260928000000004561", event_status="abandoned"):
        """一张 `claiming` 镜像行 + 它的 `hall_claim` 事件（按参数摆到指定状态）。"""
        row = JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=BASE_URL,
            upstream_order_id=order_id,
            status=JidanpuUpstreamHallOrder.Status.CLAIMING,
            game=self.game,
            owner_binding=self.binding,
            claiming_by_id=self.taker.pk,
            claiming_at=self.now,
            first_seen_at=self.now,
        )
        event = jidanpu_upstream.queue_hall_claim(row, taker_id=self.taker.pk)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(status=event_status)
        event.refresh_from_db()
        return row, event

    def test_a_claiming_row_with_a_settled_event_and_no_link_is_released(self):
        """事件 `abandoned`（烧完额度）+ 无链路 ⇒ 清道夫放回大厅（F6 残行的出口）。"""
        row, _event = self.make_claiming_row(event_status="abandoned")
        stub = _StubClient(
            rows=[self.hall_row(row.upstream_order_id)], account=self.binding.account
        )
        outcome = jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertIsNone(row.claiming_by_id)
        self.assertIsNone(row.claiming_at)
        self.assertIn("放回大厅", row.last_error)
        # 台账：新 kind（⛔ 不复用 rescued 的「gone 复活」语义），字面值钉死。
        ledger = JidanpuSyncEvent.objects.get(kind="upstream.hall_claim_released")
        self.assertEqual(ledger.direction, "in")
        self.assertEqual(ledger.status, "done")
        self.assertIn("放回大厅", ledger.reason)
        self.assertEqual(ledger.payload["mirror_id"], row.pk)
        self.assertEqual(outcome.listed, 1)

    def test_a_claiming_row_whose_event_still_has_budget_is_not_touched(self):
        """`failed` 且还有额度 ⇒ 不碰：那是后台轮正在重试的崩溃恢复路。"""
        row, _event = self.make_claiming_row(event_status="failed")
        JidanpuSyncEvent.objects.filter(kind=jidanpu_upstream.KIND_HALL_CLAIM).update(
            status="failed", attempts=1,
        )
        stub = _StubClient(
            rows=[self.hall_row(row.upstream_order_id)], account=self.binding.account
        )
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        self.assertEqual(row.claiming_by_id, self.taker.pk)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_claim_released").exists()
        )

    def test_a_claiming_row_with_a_link_is_not_touched(self):
        """有链路（已转正过 / 正常在途）⇒ 不碰——清道夫只捞「永远等不到收口」的残行。"""
        row, _event = self.make_claiming_row(event_status="abandoned")
        order = Order.objects.create(
            order_no=generate_order_no(),
            game=self.game, status=OrderStatus.IN_PROGRESS, order_type="BOOST",
            total_price=Decimal("88.00"),
        )
        JidanpuUpstreamOrderLink.objects.create(
            binding=self.binding, order=order,
            upstream_order_id=row.upstream_order_id, in_hall=False,
        )
        stub = _StubClient(
            rows=[self.hall_row(row.upstream_order_id)], account=self.binding.account
        )
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_claim_released").exists()
        )

    def test_a_claiming_row_with_a_pending_event_is_not_touched(self):
        """事件 `pending`（请求刚排下 / 后台还没跑）⇒ 不碰（最常见的在途形状）。"""
        row, _event = self.make_claiming_row(event_status="pending")
        stub = _StubClient(
            rows=[self.hall_row(row.upstream_order_id)], account=self.binding.account
        )
        jidanpu_upstream.sync_hall(binding=self.binding, client=stub, now=self.now)
        row.refresh_from_db()
        self.assertEqual(row.status, JidanpuUpstreamHallOrder.Status.CLAIMING)
        self.assertFalse(
            JidanpuSyncEvent.objects.filter(kind="upstream.hall_claim_released").exists()
        )
