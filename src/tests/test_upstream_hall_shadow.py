"""⭐ 上家鸡蛋铺镜像卡 → 打手下家鸡蛋铺抛单（D-0325，业务方 2026-10-29 拍板）的行为网。

业务口径一句话：上家镜像卡（`JidanpuUpstreamHallOrder`）挂在我方大厅期间，也要抛给所有
「启用 + `hall_sync_enabled`」的下家鸡蛋铺绑定（载体 = `Order.upstream_hall_card` 派生的
**隐藏单**，照 peer 线 `hall_shadow` 的模板）；价格照绑定配置走（`recFee × 上家% × throw%`）、
0 元 / 负价卡不抛 + 台账、绝不推回来源上家地址（硬闸）、转正同一张单接着用。

本文件钉十一组（每条**钉值**，⛔ 不是「有就行」；夹具一律合成值——全零段单号、`…test.invalid` 域名）：

⓪ **D-0329 常量水位（改准：总闸撤除、随发版自动生效）**：卡新于水位
   （`first_seen_at >= UPSTREAM_THROW_SINCE` = 第五车发版时刻）⇒ 照常派生 + 抛出；
   卡老于水位 ⇒ 不派生（存量 823 张不补抛的景）；回收路（`refund_for_card`）不受水位管；
   已派生的隐藏单不因任何闸被撤；
① **生命周期接线**：`sync_hall` 见 listed ⇒ 建隐藏单 + 排 `member.launch`；重复轮不重排；
   gone 复活 ⇒ 同一张复活并重抛；
② **价格口径三态**：隐藏单 `hall_price` = 镜像展示价（`upstream_hall_price` 同一份算法）、
   抛单价 = `hall_price × throw%`；上家改价传导（改 `hall_price` + 排 `member.price`）；
③ **0 元 / 负价不抛**：不建隐藏单、不排 launch、`upstream.hall_throw_no_price` 台账各一条
   （reason 分 0 元 / 负价两句）；已抛的卡改价成 0 ⇒ 退款 + 撤副本；
④ **硬闸**：绑定地址 == 来源上家地址 ⇒ 一条 launch 都不排；另一地址照排；同地址两绑定只推 id 最小；
⑤ **gone 撤副本**：拉全轮判消失（`_mark_gone`）⇒ 隐藏单 `REFUNDED` + 排 `member.withdraw`；
⑥ **转正接着用**：`promote_upstream_mirror` 复用同一张（`order_no` 不变、`upstream_hall_card`
   清空、⭐ D-0342 改准：`source_order_no` 改写为上家展示号——创建期仍内部号）+ MAIN 绩效行
   挂打手 + E-0283 清场（接手人 assign / 其余店 withdraw）；无隐藏单的卡照新建支（行为不变）；
⑦ **机器接手分支**：下家接走副本 ⇒ `claim_upstream_mirror(enforce_limits=False)` 被调、
   成功记台账；上游卡已被接走（`ClaimError`）⇒ 响亮拒绝、⛔ 不排退单；
⑧ **竞态两处置**：「上游 gone + 下家已接」——远端「未分配」⇒ 退单照发；远端「代练中」⇒
   `_consume_withdraw` 终态跳过保活单（`withdrawn_at` 不写）；
⑨ **慢路跳过**：`apply_remote_completion` 对未转正的隐藏单回 `SKIPPED` + 一次台账；
   隐藏单不进 `orders_of`（对账候选）与面向人的读面。
⑩ **S-06 逐行隔离**：行循环里单卡派生抛错 ⇒ 响亮跳过该卡、整轮照常走完。
⑪ **S-03 / D-0331 人工退款优先（业务方拍板）**：隐藏单被人工退款后，
   下一轮拉取**不得**静默复活它——复活只认「本引擎 `refund_for_card` 退的」（同事务
   台账标记 `upstream.hall_shadow_refunded`，payload 钉订单号 + 与订单行同一个
   `updated_at` 时刻）；人工退款 ⇒ 不复活 / 不补抛 / 不对价 + `upstream.hall_manual_refund`
   台账恰一条（幂等）；卡**不下架**（镜像生命周期属上家，D-0300 复活支会每轮翻回）；
   引擎退的（含价闸退后价回来）照旧复活；人工退款历经 gone 周期也不被老标记背书。
"""
from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils.dateparse import parse_datetime
from django.utils import timezone as dj_timezone

from django_tenants.test.cases import TenantTestCase

from apps.accounts.models import StaffProfile
from apps.business import jidanpu, jidanpu_member, jidanpu_upstream, upstream_hall_shadow
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    Order,
    OrderStatus,
    Performance,
    SystemSwitch,
)
from apps.business.order_no import generate_order_no
from tests.p5_auth_helpers import grant_nodes
from tests.test_jidanpu_upstream_consume import TAKEN_MESSAGE, _StubClient

UPSTREAM_URL = "https://jidanpu-uptest.test.invalid"
DOWNSTREAM_URL = "https://jidanpu-downtest.test.invalid"
DOWNSTREAM_URL_B = "https://jidanpu-downtest-b.test.invalid"
PASSWORD = "pw-canary-upt"
#: ⚠️ **故意**取在常量水位 `UPSTREAM_THROW_SINCE`（20:30 上海 = 12:30 UTC）
#: **之后**（22:00 上海）⇒ 夹具卡（`first_seen_at=NOW`）全部是「新卡」＝派生行为自身的
#: 稳态；「老卡」各用例自己把 `first_seen_at` 拨回水位之前去造（823 张景）。
NOW = datetime(2026, 10, 29, 14, 0, tzinfo=dt_timezone.utc)

#: 合成上家单号（镜像表幂等键；隐藏单 `source_order_no` 用的就是它，D-0325 拍板④）。
REMOTE_ID = "33160000000000001001"
#: 合成展示单号（与内部号不同——正是「不取展示号当回环键」的那组现场）。
REMOTE_VIEW = "VIEW-33160000000000001001"
REMOTE_ID_2 = "33160000000000001002"

NO_PRICE_KIND = upstream_hall_shadow.KIND_THROW_NO_PRICE


class _HallStub:
    """`JidanpuClient` 替身（照 `test_jidanpu_upstream_pull` 的 gameName 分流形态）。

    - 无游戏名全量路（不带 `gameName`）：第 1 页给 `rows`、第 2 页起**空页**（探页确认拉全）；
    - 逐游戏路（带 `gameName`）：默认回**同一份 `rows`**（合并按 `orderId` 去重 ⇒ 行为等价）；
      `per_game_rows` 可按游戏名覆盖——判消失的轮必须给**非空段**（YF-01：success-empty
      的游戏不供消失判定），段里放**别的卡**（被测卡本轮不在）。
    """

    def __init__(self, *, rows, per_game_rows=None):
        self.rows = [dict(row) for row in rows]
        self.per_game_rows = dict(per_game_rows or {})
        self.last_envelope: dict = {}

    def ensure_login(self):
        pass

    def call(self, method, path, **kwargs):
        query = dict(kwargs.get("query") or {})
        asked_game = str(query.get("gameName", "") or "")
        if asked_game:
            source = self.per_game_rows.get(asked_game, self.rows)
        else:
            source = self.rows
        page = int(query.get("page", 1) or 1)
        page_rows = source if page == 1 else []
        self.last_envelope = {
            "code": 0, "msg": "success", "data": page_rows, "count": len(source),
        }
        return [dict(row) for row in page_rows]


def hall_row(*, order_id=REMOTE_ID, view=REMOTE_VIEW, game_name="上家镜像夹具游戏",
             rec_fee="100.00"):
    return {
        "orderId": order_id,
        "orderIdView": view,
        "gameName": game_name,
        "gameArea": "安卓",
        "gameServer": "微信区",
        "project": "合成项目-上家",
        "orderRemark": "合成备注-上家",
        "roleName": "合成角色",
        "recFee": rec_fee,
        "createTime": "2026-10-29 11:46:54",
    }


def _grant_take_node(user):
    """给账号挂 `grab_hall.take_order`（机器接手的节点门照走，先例见 peer 镜像分支夹具）。"""
    worker_group, _ = Group.objects.get_or_create(name="代练员")
    user.groups.add(worker_group)
    grant_nodes(user, "grab_hall.take_order")


class _UpstreamFixture(TenantTestCase):
    """共用夹具：一款游戏、一份上家绑定（85% 勾兑）、一份下家绑定（90% 抛价）。"""

    def setUp(self):
        super().setUp()
        self.game = Game.objects.create(name="上家镜像夹具游戏")
        self.up_user = get_user_model().objects.create_user(
            username="uht-up", password=PASSWORD,
        )
        self.up_binding = JidanpuUpstreamBinding.objects.create(
            user=self.up_user, jidanpu_base_url=UPSTREAM_URL, account="acc-uht-up",
            password=PASSWORD, enabled=True, hall_price_percent=85,
        )
        self.member_user = get_user_model().objects.create_user(
            username="uht-member", password=PASSWORD,
        )
        profile = StaffProfile.objects.create(user=self.member_user)
        profile.games.add(self.game)
        self.member_binding = JidanpuMemberBinding.objects.create(
            user=self.member_user, jidanpu_base_url=DOWNSTREAM_URL,
            account="acc-uht-member", password=PASSWORD, enabled=True,
            throw_price_percent=90,
        )
        # ⭐ D-0329 改准：总闸 `hall_upstream_throw` 已撤、随发版自动生效
        #    ⇒ 这里**不再造任何开关行**（造了也是 unknown key）。派生的唯一闸是常量水位
        #    `UPSTREAM_THROW_SINCE`；夹具卡 `first_seen_at=NOW` 在水位之后 ⇒ 本文件的
        #    既有用例全部演「新卡照常派生」的**稳态**（它们钉的是派生行为本身）；
        #    水位自己的四条判据在 `HallThrowAutoGateTests` 单独钉。

    # ── 夹具工厂 ────────────────────────────────────────────────────────────

    def _member_user(self, username, *, base_url, games=True, enabled=True,
                     throw_percent=90, same_url=False):
        user = get_user_model().objects.create_user(username=username, password=PASSWORD)
        binding = self._member_binding_for(
            user, base_url=base_url, games=games, enabled=enabled,
            throw_percent=throw_percent, same_url=same_url,
        )
        return user, binding

    def _member_binding_for(self, user, *, base_url, games=True, enabled=True,
                            throw_percent=90, same_url=False):
        profile, _created = StaffProfile.objects.get_or_create(user=user)
        if games:
            profile.games.add(self.game)
        return JidanpuMemberBinding.objects.create(
            user=user,
            jidanpu_base_url=base_url if not same_url else UPSTREAM_URL,
            account=f"acc-{user.username}", password=PASSWORD, enabled=enabled,
            throw_price_percent=throw_percent,
        )

    def _listed_mirror(self, *, remote_id=REMOTE_ID, rec_fee=Decimal("100.00"),
                       status=JidanpuUpstreamHallOrder.Status.LISTED):
        return JidanpuUpstreamHallOrder.objects.create(
            upstream_base_url=UPSTREAM_URL,
            upstream_order_id=remote_id,
            owner_binding=self.up_binding,
            status=status,
            game=self.game,
            remote_rec_fee=rec_fee,
            project_text="合成项目-上家",
            remote_remark="合成备注-上家",
            remote_role_name="合成角色",
            first_seen_at=NOW,
        )

    def _shadow_of(self, mirror):
        return Order.all_objects.filter(upstream_hall_card=mirror).first()

    def _launch_events(self, order):
        return JidanpuSyncEvent.objects.filter(
            kind="member.launch", target_ref=order.order_no, direction="out",
        )

    def _withdraw_events(self, order):
        return JidanpuSyncEvent.objects.filter(
            kind="member.withdraw", target_ref=order.order_no, direction="out",
        )

    def _link(self, binding, order, *, remote_id="JDP-33160000000000001001",
              in_hall=True):
        return JidanpuMemberOrderLink.objects.create(
            binding=binding, order=order, jidanpu_order_id=remote_id, in_hall=in_hall,
        )


# ══ ⓪ D-0329（业务方拍板）：常量水位自动生效 ═══════════════
#
# 原裁量是「租户开关 `hall_upstream_throw` + 开关行水位」；业务方当晚
# 拍板改准——「同步订单是一整个功能」⇒ 总闸撤除、随发版自动生效；下家「收不收」由
# 绑定自己的 `enabled` + `hall_sync_enabled` 管（HardGateTests 在钉）。存量不补抛的
# 原判据保留为常量水位 `UPSTREAM_THROW_SINCE`（第五车发版时刻）。四条判据各钉一景，
# 每条写明失败方向（弄红变异要弄红的就是它们）；夹具稳态（新卡照派生）由
# `_UpstreamFixture` 供给（NOW 在水位之后）。


class HallThrowAutoGateTests(_UpstreamFixture):
    """常量水位的四条判据（闸在 `upstream_hall_shadow.ensure_for_card` 入口）。"""

    def test_new_card_fresh_from_the_waterline_is_derived_and_thrown(self):
        """① 自动生效的正面：一行开关都没有 ⇒ 新卡（first_seen=NOW ≥ 水位）照样派生 + 抛出。

        「同步订单是一整个功能」——总闸撤了，功能随发版自动生效；判据先自证
        「确实没有任何开关行」（⭐ 不是「开关开着」的旧景——那个键已经不存在）。
        失败方向 = 把「自动生效」改回「等某个开关」⇒ 本用例红。
        """
        self.assertFalse(SystemSwitch.objects.exists())
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        self.assertIsNotNone(shadow)
        self.assertEqual(self._launch_events(shadow).count(), 1)

    def test_card_older_than_the_waterline_is_not_derived(self):
        """② 存量 823 张的景：卡的 `first_seen_at` 早于水位 ⇒ 不派生（自动生效也不补抛）。

        夹具卡先以「老卡」落地（first_seen 拨回 30 天前——存量卡没有一张是发版时刻
        之后才进厅的），再跑拉取轮：走的 listed 刷新支，隐藏单一张都不该建。
        失败方向 = 摘掉**水位判据** ⇒ 本用例红。
        """
        mirror = self._listed_mirror()
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            first_seen_at=NOW - timedelta(days=30),
        )
        stub = _HallStub(rows=[hall_row()])
        outcome = jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        self.assertEqual(outcome.listed, 1)
        self.assertIsNone(
            self._shadow_of(JidanpuUpstreamHallOrder.objects.get(pk=mirror.pk))
        )
        self.assertEqual(Order.all_objects.count(), 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind="member.launch", direction="out").count(),
            0,
        )

    def test_refund_path_ignores_the_waterline(self):
        """③ 回收路不受水位管：**老卡**（first_seen 在水位前）带着已有隐藏单，卡下架
        ⇒ 照常 `REFUNDED` + 撤下家副本。

        「老卡 + 已有单」不是空想：改准前带总闸的版本已在生产跑过、将来的补抛命令也会
        给老卡留隐藏单——所以本景先真派生（launch 在飞 = 撤单的推送证据），再把卡的
        `first_seen_at` 拨回水位前 30 天（⛔ 不是手工拼一张无证据的隐藏单：
        `queue_withdraw` 按**推送证据**找绑定，没有 launch 的影子单本来就没得撤）。
        一致性优先：闸只管「新建」，不管「回收」——别把「暂停新抛」写成「断链不回收」。
        失败方向 = 给 `refund_for_card`（或 gone 接线）也加水位判据 ⇒ 本用例红。
        """
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(
            JidanpuUpstreamHallOrder.objects.get(upstream_order_id=REMOTE_ID)
        )
        self.assertIsNotNone(shadow)
        # 卡「变老」：first_seen 拨回水位前 30 天——它带着的隐藏单与在飞的 launch 原样。
        JidanpuUpstreamHallOrder.objects.filter(
            upstream_order_id=REMOTE_ID,
        ).update(first_seen_at=NOW - timedelta(days=30))
        # 下一轮拉全没有这张卡（段里留一张**别的卡**——YF-01 非空段），回查也查不到
        # ⇒ gone ⇒ 隐藏单退款 + withdraw 恰一条（水位拦不着回收路）。
        gone_round = _HallStub(rows=[hall_row(order_id=REMOTE_ID_2, view="V-0002")])
        with mock.patch.object(jidanpu, "fetch_order_by_key1", return_value=None):
            jidanpu_upstream.sync_hall(
                binding=self.up_binding, client=gone_round, now=NOW,
            )
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        self.assertEqual(self._withdraw_events(shadow).count(), 1)

    def test_existing_shadow_is_not_touched_by_the_waterline_gate(self):
        """④ 残差钉住：老卡带着已派生的隐藏单仍在厅 ⇒ 水位闸对它只是「不再派生 /
        不再对价」，**不撤单、不排 withdraw**。

        失败方向 = 把闸写成「水位外 ⇒ 顺手撤已有单」⇒ 本用例红。
        """
        mirror = self._listed_mirror()
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            first_seen_at=NOW - timedelta(days=30),
        )
        shadow = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("85.00"),
            total_price=Decimal("100.00"), source_order_no=REMOTE_ID,
            upstream_hall_card=mirror,
        )
        # 再来一轮 listed 刷新（卡仍在厅、卡老于水位）⇒ 隐藏单原样保活：
        # 不撤、launch 不重排（本来就没有）。
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        self.assertEqual(self._withdraw_events(shadow).count(), 0)
        self.assertEqual(self._launch_events(shadow).count(), 0)


# ══ ① 生命周期接线 ════════════════════════════════════════════════════════


class LifecycleWiringTests(_UpstreamFixture):

    def test_sync_hall_lists_a_card_and_launches_the_shadow_order(self):
        """新卡进厅 ⇒ 建隐藏单（待抢 / 价 = 展示价 / 来源号 = 上家内部号）+ 排一条 launch。"""
        stub = _HallStub(rows=[hall_row()])
        outcome = jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        self.assertEqual(outcome.listed, 1)
        mirror = JidanpuUpstreamHallOrder.objects.get(upstream_order_id=REMOTE_ID)
        shadow = self._shadow_of(mirror)
        self.assertIsNotNone(shadow)
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        # ⭐ 价 = 镜像展示价（100 × 85% = 85.00；下家看到 = 85 × 90% = 76.50，见②）。
        self.assertEqual(shadow.hall_price, Decimal("85.00"))
        # ⭐ 来源号 = 上家**内部**单号（D-0325 拍板④：回环键，⛔ 不取展示号）。
        self.assertEqual(shadow.source_order_no, REMOTE_ID)
        self.assertEqual(shadow.game, self.game)
        self.assertEqual(shadow.total_price, Decimal("100.00"))
        # 客户价不受百分比影响；隐藏单没有 MAIN 绩效行（转正才建）。
        self.assertFalse(Performance.objects.filter(order=shadow).exists())
        # launch 恰一条，落在这份启用绑定上。
        launches = self._launch_events(shadow)
        self.assertEqual(launches.count(), 1)
        self.assertEqual(launches.get().payload["member_binding_id"], self.member_binding.pk)

    def test_refresh_rounds_do_not_duplicate_the_shadow_or_launch(self):
        """同一张卡第二轮拉到 ⇒ 隐藏单仍一张、launch 不重排（幂等）。"""
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        self.assertEqual(JidanpuUpstreamHallOrder.objects.count(), 1)
        mirror = JidanpuUpstreamHallOrder.objects.get()
        shadow = self._shadow_of(mirror)
        self.assertEqual(self._launch_events(shadow).count(), 1)
        self.assertEqual(Order.all_objects.count(), 1)

    def test_revived_card_reuses_and_rethrows_the_same_shadow(self):
        """卡 gone 后又回到大厅（D-0300 复活）⇒ **同一张**隐藏单回待抢并重抛。"""
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        mirror = JidanpuUpstreamHallOrder.objects.get()
        shadow = self._shadow_of(mirror)
        order_no = shadow.order_no
        # 卡离厅：下一轮拉全里没有它（段里留一张**别的卡**——YF-01 的非空段要求），
        # 且回查（D-0258 逐单回查）也查不到 ⇒ gone。
        gone_round = _HallStub(rows=[hall_row(order_id=REMOTE_ID_2, view="V-0002")])
        with mock.patch.object(jidanpu, "fetch_order_by_key1", return_value=None):
            jidanpu_upstream.sync_hall(binding=self.up_binding, client=gone_round, now=NOW)
        mirror.refresh_from_db()
        shadow.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        # 卡又出现 ⇒ 复活同一张（order_no 不变）+ 重新排 launch。⚠️ 第一轮那条 launch
        # 仍是「在飞」（测试里没有消费者）⇒ 引擎按同键去重复用；把它标成 done（= 已消费）
        # 再看复活那一拍**新排**的一条——去重与重排两头都钉住。
        first_launch = self._launch_events(shadow).get()
        first_launch.status = "done"
        first_launch.save(update_fields=["status"])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        revived = self._shadow_of(mirror)
        self.assertEqual(revived.order_no, order_no)
        self.assertEqual(str(revived.status), str(OrderStatus.WAITING_GRAB))
        self.assertEqual(self._launch_events(revived).count(), 2)


# ══ ② 价格口径三态 + 改价传导 ═════════════════════════════════════════════


class PriceChainTests(_UpstreamFixture):

    def test_downstream_sees_recfee_times_upstream_percent_times_throw_percent(self):
        """下家看到的抛单价 = recFee × 上家勾兑% × 下家 throw%（×0.765 现场同源）。"""
        stub = _HallStub(rows=[hall_row(rec_fee="100.00")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        self.assertEqual(shadow.hall_price, Decimal("85.00"))
        self.assertEqual(
            jidanpu_member._throw_fee(shadow, self.member_binding), Decimal("76.50")
        )

    def test_upstream_reprice_flows_to_the_shadow_and_queues_price_update(self):
        """上家把 25 改成 70 ⇒ 隐藏单价改 21.25 → 59.50（70×85%）+ 给**已有链路**排
        `member.price`（引擎口径：改价只发给已录到对方系统的店——先造链路模拟 launch 已消费）。"""
        stub = _HallStub(rows=[hall_row(rec_fee="25.00")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        self.assertEqual(shadow.hall_price, Decimal("21.25"))
        # 模拟首轮 launch 已消费：链路行落地（改价那一拍只找有链路的绑定）。
        self._link(self.member_binding, shadow, remote_id="JDP-33160000000000001001")
        stub2 = _HallStub(rows=[hall_row(rec_fee="70.00")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub2, now=NOW)
        shadow.refresh_from_db()
        self.assertEqual(shadow.hall_price, Decimal("59.50"))
        prices = JidanpuSyncEvent.objects.filter(
            kind="member.price", target_ref=shadow.order_no, direction="out",
        )
        self.assertEqual(prices.count(), 1)
        self.assertEqual(prices.get().payload["member_binding_id"], self.member_binding.pk)

    def test_same_price_round_is_a_noop(self):
        """价没变的那一轮 ⇒ 不排 `member.price`（不刷台账）。"""
        stub = _HallStub(rows=[hall_row(rec_fee="25.00")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind="member.price", target_ref=shadow.order_no,
            ).count(),
            0,
        )


# ══ ③ 0 元 / 负价不抛 ═════════════════════════════════════════════════════


class NoPriceGateTests(_UpstreamFixture):

    def test_zero_fee_card_is_not_thrown_and_leaves_one_ledger_row(self):
        """`recFee` 缺失（展示价 0.00）⇒ 不建隐藏单、不排 launch、台账一句「0 元」。"""
        stub = _HallStub(rows=[hall_row(rec_fee="")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        self.assertTrue(JidanpuUpstreamHallOrder.objects.filter(
            upstream_order_id=REMOTE_ID,
        ).exists())
        self.assertEqual(Order.all_objects.count(), 0)
        self.assertEqual(JidanpuSyncEvent.objects.filter(
            kind="member.launch", direction="out",
        ).count(), 0)
        rows = JidanpuSyncEvent.objects.filter(kind=NO_PRICE_KIND)
        self.assertEqual(rows.count(), 1)
        self.assertIn("0", rows.get().reason)

    def test_negative_fee_card_is_not_thrown_and_names_the_negative(self):
        stub = _HallStub(rows=[hall_row(rec_fee="-5.00")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        self.assertEqual(Order.all_objects.count(), 0)
        rows = JidanpuSyncEvent.objects.filter(kind=NO_PRICE_KIND)
        self.assertEqual(rows.count(), 1)
        self.assertIn("负", rows.get().reason)

    def test_repeated_rounds_do_not_duplicate_the_no_price_ledger(self):
        stub = _HallStub(rows=[hall_row(rec_fee="")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind=NO_PRICE_KIND).count(), 1
        )

    def test_repriced_to_zero_refunds_the_shadow_and_withdraws_copies(self):
        """已抛出的卡改价成 0 ⇒ 隐藏单 `REFUNDED` + 排 `member.withdraw`（下家不留错价卡）。"""
        stub = _HallStub(rows=[hall_row(rec_fee="25.00")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        self.assertEqual(self._launch_events(shadow).count(), 1)
        stub2 = _HallStub(rows=[hall_row(rec_fee="0.00")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub2, now=NOW)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        self.assertEqual(self._withdraw_events(shadow).count(), 1)
        self.assertGreaterEqual(
            JidanpuSyncEvent.objects.filter(kind=NO_PRICE_KIND).count(), 1
        )


# ══ ④ 硬闸：绝不推回来源上家地址 ══════════════════════════════════════════


class HardGateTests(_UpstreamFixture):

    def test_binding_at_the_upstream_address_gets_no_launch(self):
        """下家绑定地址 == 该卡来源上家地址（租户 S-chuyin 现场）⇒ 一条 launch 都不排。"""
        _, same_store = self._member_user("uht-same", base_url=UPSTREAM_URL)
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        launches = self._launch_events(shadow)
        # 只有另一地址那份收到；同地址那份（id 更小、本该赢去重）一条都没有。
        self.assertEqual(launches.count(), 1)
        self.assertEqual(launches.get().payload["member_binding_id"], self.member_binding.pk)
        self.assertNotEqual(launches.get().payload["member_binding_id"], same_store.pk)

    def test_two_bindings_at_the_same_other_address_dedupe_to_the_earliest(self):
        """另一个地址上两份绑定 ⇒ 只推 id 最小那份（同地址去重照旧）。"""
        self._member_user("uht-b1", base_url=DOWNSTREAM_URL_B)
        self._member_user("uht-b2", base_url=DOWNSTREAM_URL_B)
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        launches = self._launch_events(shadow)
        self.assertEqual(launches.count(), 2)  # DOWNSTREAM_URL 一条 + DOWNSTREAM_URL_B 一条
        bound_ids = {row.payload["member_binding_id"] for row in launches}
        b_ids = list(
            JidanpuMemberBinding.objects.filter(
                jidanpu_base_url=DOWNSTREAM_URL_B,
            ).values_list("pk", flat=True)
        )
        self.assertEqual(min(b_ids) in bound_ids, True)
        self.assertEqual(max(b_ids) in bound_ids, False)

    def test_hall_sync_disabled_binding_gets_no_launch(self):
        """`hall_sync_enabled=False` ⇒ 不收 launch（D-0299 既有口径对本线同样成立）。"""
        self.member_binding.hall_sync_enabled = False
        self.member_binding.save(update_fields=["hall_sync_enabled"])
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        mirror = JidanpuUpstreamHallOrder.objects.get()
        # 隐藏单照建（卡在我方大厅里），只是这份关了开关的绑定不收 launch。
        self.assertIsNotNone(self._shadow_of(mirror))
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind="member.launch", direction="out",
            ).count(),
            0,
        )


# ══ ⑤ gone 撤副本 ═════════════════════════════════════════════════════════


class GoneRefundTests(_UpstreamFixture):

    def test_mark_gone_refunds_the_shadow_and_queues_withdraw(self):
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get(upstream_order_id=REMOTE_ID))
        # 下一轮拉全没这张卡（段里留一张别的卡——YF-01 非空段），回查也查不到 ⇒ gone。
        gone_round = _HallStub(rows=[hall_row(order_id=REMOTE_ID_2, view="V-0002")])
        with mock.patch.object(jidanpu, "fetch_order_by_key1", return_value=None):
            outcome = jidanpu_upstream.sync_hall(
                binding=self.up_binding, client=gone_round, now=NOW,
            )
        self.assertEqual(outcome.gone, 1)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        # `upstream_hall_card` 不清（退掉的派生单仍属隐藏面）。
        self.assertIsNotNone(shadow.upstream_hall_card_id)
        self.assertEqual(self._withdraw_events(shadow).count(), 1)

    def test_refund_of_a_promoted_shadow_is_a_noop(self):
        """转正后的单（列已清）不会被 gone 钩子翻状态（数据被手改也⛔ 不覆盖）。"""
        mirror = self._listed_mirror()
        order = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.IN_PROGRESS, hall_price=Decimal("85.00"),
            total_price=Decimal("100.00"),
        )
        # 不挂 upstream_hall_card（转正已清）⇒ refund_for_card 找不到隐藏单。
        self.assertEqual(upstream_hall_shadow.refund_for_card(mirror, now=NOW), 0)

    def test_outbox_taken_refusal_refunds_the_shadow_and_queues_withdraw(self):
        """⭐ 评审 B-1（2026-10-29）：`claiming → gone` 的 **outbox 重试路**也必须退隐藏单。

        `_consume_hall_claim` 的「已被接走」拒单支与 `_claim_refused`（请求路）是同一次接单的
        两条并行执行路：请求路认不出拒绝（`_retry`）或进程在打上游前死掉 ⇒ `run_outbox` 稍后
        重试、上家回「已被接走」⇒ 走的就是这一支（可达性同
        `test_jidanpu_upstream_consume.py::test_a_refused_claim_converges_the_mirror_to_gone`）。
        漏挂钩子的后果：隐藏单停 `WAITING_GRAB`、`member.withdraw` 不排队 ⇒ 下家大厅留永动机副本。

        四幕（照评审探针形状）：claiming 卡 → outbox 路吃到 gone 拒单 ⇒ 镜像 gone +
        隐藏单 `REFUNDED` + withdraw 恰一条。失败方向 = 摘掉 `_consume_hall_claim` 里的
        `refund_for_card` ⇒ 本用例红。
        """
        taker = get_user_model().objects.create_user(
            username="uht-taker-b1", password=PASSWORD,
        )
        mirror = self._listed_mirror()
        shadow = upstream_hall_shadow.ensure_for_card(mirror, now=NOW)
        self.assertIsNotNone(shadow)
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        # 抛出去了 ⇒ launch 恰一条（下家副本真实在飞）。
        self.assertEqual(self._launch_events(shadow).count(), 1)
        # 打手接卡：listed → claiming，事件入 outbox（本用例演的是重试路——请求路没打上游）。
        event = jidanpu_upstream.queue_hall_claim(mirror, taker_id=taker.pk)
        self.assertIsNotNone(event)
        JidanpuUpstreamHallOrder.objects.filter(pk=mirror.pk).update(
            status=JidanpuUpstreamHallOrder.Status.CLAIMING,
            claiming_by=taker, claiming_at=NOW,
        )
        # outbox 重试路消费 `upstream.hall_claim`：上家回「已被接走」（合成文案，⛔ 非实测原文）。
        client = _StubClient(claim_code=0, claim_msg=TAKEN_MESSAGE, claim_data="")
        self.assertEqual(jidanpu_upstream.consume_event(event, client=client), "done")
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        # —— 钩子这一拍：隐藏单退款 + 撤下家副本（withdraw 恰一条，⛔ 不是 0）——
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        self.assertEqual(self._withdraw_events(shadow).count(), 1)


# ══ ⑥ 转正接着用 ══════════════════════════════════════════════════════════


class PromoteReuseTests(_UpstreamFixture):

    def _taker(self):
        taker = get_user_model().objects.create_user(
            username="uht-taker", password=PASSWORD,
        )
        profile, _ = StaffProfile.objects.get_or_create(user=taker)
        profile.games.add(self.game)
        _grant_take_node(taker)
        return taker

    def _claiming_mirror_with_shadow(self, *, taker):
        mirror = self._listed_mirror()
        # ⭐ D-0342：转正改写要读镜像报文里的 `orderIdView` ⇒ 夹具带上它
        #    （拉取投影实写这格：`sync_hall` 落行存的是远端整行，形状照
        #    `test_jidanpu_mirror_claim_member_assign.py::_mirror`）。
        mirror.upstream_payload = {
            "orderId": REMOTE_ID, "orderIdView": REMOTE_VIEW,
        }
        mirror.status = JidanpuUpstreamHallOrder.Status.CLAIMING
        mirror.claiming_by = taker
        mirror.claiming_at = NOW
        mirror.save(
            update_fields=["status", "claiming_by", "claiming_at", "upstream_payload"]
        )
        shadow = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("85.00"),
            total_price=Decimal("100.00"), source_order_no=REMOTE_ID,
            upstream_hall_card=mirror,
        )
        return mirror, shadow

    def test_promotion_reuses_the_shadow_order_in_place(self):
        """转正 = **同一张**：`order_no` 不变、`IN_PROGRESS`、MAIN 行挂打手、列清空、
        ⭐ D-0342（改准）：`source_order_no` 改写为上家**展示号**（镜像行保留
        内部号↔展示号映射）；与新建支同值 ⇒ 两支转正后同口径（D-0272 全端口径）。"""
        taker = self._taker()
        self._member_binding_for(taker, base_url="https://taker-down.test.invalid")
        mirror, shadow = self._claiming_mirror_with_shadow(taker=taker)
        order_no = shadow.order_no

        order = jidanpu_upstream.promote_upstream_mirror(
            mirror, binding=self.up_binding, detail={"orderContent": "合成内容"},
            user_id=taker.pk, now=NOW,
        )
        self.assertEqual(order.pk, shadow.pk)
        self.assertEqual(order.order_no, order_no)
        self.assertEqual(str(order.status), str(OrderStatus.IN_PROGRESS))
        self.assertIsNone(order.upstream_hall_card_id)
        # ⭐ 来源号**改写为展示号**（创建期仍是内部号——由 `_projection_fields` 钉）。
        self.assertEqual(order.source_order_no, REMOTE_VIEW)
        main = Performance.objects.get(order=order, emp_type=EmpType.MAIN)
        self.assertEqual(main.user_id, taker.pk)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.PROMOTED)
        self.assertEqual(mirror.promoted_order_id, order.pk)
        # 内部号↔展示号映射留在 promoted 镜像行上（审计可回溯）。
        self.assertEqual(mirror.upstream_order_id, REMOTE_ID)
        self.assertEqual(mirror.upstream_payload["orderIdView"], REMOTE_VIEW)
        # 金额与新建支同形：客户价照抄 recFee、发单价 = 勾兑价。
        self.assertEqual(order.total_price, Decimal("100.00"))
        self.assertEqual(order.hall_price, Decimal("85.00"))
        self.assertEqual(order.sales_user_id, self.up_binding.user_id)

    def test_promotion_without_payload_view_keeps_the_internal_source(self):
        """镜像报文没有 `orderIdView`（空报文兜底档）⇒ 不碰来源号列
        （⛔ 不写空串进非空列；回落内部号的旧行为保留）。"""
        taker = self._taker()
        self._member_binding_for(taker, base_url="https://taker-down.test.invalid")
        mirror, shadow = self._claiming_mirror_with_shadow(taker=taker)
        mirror.upstream_payload = {}
        mirror.save(update_fields=["upstream_payload"])

        order = jidanpu_upstream.promote_upstream_mirror(
            mirror, binding=self.up_binding, detail={}, user_id=taker.pk, now=NOW,
        )
        self.assertEqual(order.pk, shadow.pk)
        self.assertEqual(order.source_order_no, REMOTE_ID)

    def test_promotion_queues_assign_for_taker_and_withdraw_for_other_stores(self):
        """E-0283 清场照走：接手人自己的绑定收 `member.assign`、其余收到过副本的店退单。"""
        taker = self._taker()
        taker_binding = self._member_binding_for(
            taker, base_url="https://taker-down.test.invalid",
        )
        mirror, shadow = self._claiming_mirror_with_shadow(taker=taker)
        # 另一家店收到过副本（链路 = 推送证据）。
        _, other_binding = self._member_user("uht-other", base_url=DOWNSTREAM_URL_B)
        self._link(other_binding, shadow, remote_id="JDP-OTHER-00000000000001")

        order = jidanpu_upstream.promote_upstream_mirror(
            mirror, binding=self.up_binding, detail={}, user_id=taker.pk, now=NOW,
        )
        assigns = JidanpuSyncEvent.objects.filter(
            kind="member.assign", target_ref=order.order_no, direction="out",
        )
        self.assertEqual(assigns.count(), 1)
        self.assertEqual(assigns.get().payload["member_binding_id"], taker_binding.pk)
        withdraws = JidanpuSyncEvent.objects.filter(
            kind="member.withdraw", target_ref=order.order_no, direction="out",
        )
        self.assertEqual(withdraws.count(), 1)
        self.assertEqual(withdraws.get().payload["member_binding_id"], other_binding.pk)

    def test_promotion_without_shadow_keeps_the_create_path(self):
        """没有隐藏单的卡（0 元卡直接接）⇒ 走新建支：来源号用展示号（既有行为不变）。"""
        taker = self._taker()
        mirror = self._listed_mirror()
        mirror.upstream_payload = {"orderIdView": REMOTE_VIEW}
        mirror.status = JidanpuUpstreamHallOrder.Status.CLAIMING
        mirror.claiming_by = taker
        mirror.save(update_fields=["status", "claiming_by", "upstream_payload"])
        order = jidanpu_upstream.promote_upstream_mirror(
            mirror, binding=self.up_binding, detail={}, user_id=taker.pk, now=NOW,
        )
        self.assertEqual(order.source_order_no, REMOTE_VIEW)


# ══ ⑦ 机器接手分支 ════════════════════════════════════════════════════════


class MachineClaimBranchTests(_UpstreamFixture):

    def setUp(self):
        super().setUp()
        _grant_take_node(self.member_user)
        self.mirror = self._listed_mirror()
        self.shadow = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("85.00"),
            total_price=Decimal("100.00"), source_order_no=REMOTE_ID,
            upstream_hall_card=self.mirror,
        )

    def test_machine_claim_routes_to_claim_upstream_mirror_without_per_user_gates(self):
        """下家接走副本 ⇒ 走 `claim_upstream_mirror(enforce_limits=False,
        enforce_deposit=False)`（裁定 8），成功记审计 + `member.remote_taken` 台账。"""
        with mock.patch.object(
            jidanpu_upstream, "claim_upstream_mirror", return_value=self.shadow,
        ) as spy:
            outcome = jidanpu_member.machine_claim_order(
                binding=self.member_binding, order=self.shadow,
                remote_uid=707, remote_name="合成打手", remote_order_id="JDP-M-0001",
                source_note="uid=707 显示名=合成打手",
            )
        self.assertTrue(outcome.claimed)
        self.assertIsNone(outcome.claim_error)
        self.assertIsNone(outcome.denial)
        kwargs = spy.call_args.kwargs
        self.assertEqual(kwargs["user_id"], self.member_user.pk)
        self.assertEqual(kwargs["mirror_id"], self.mirror.pk)
        self.assertFalse(kwargs["enforce_limits"])
        self.assertFalse(kwargs["enforce_deposit"])
        taken = JidanpuSyncEvent.objects.filter(
            kind=jidanpu_member.KIND_REMOTE_TAKEN, target_ref=self.shadow.order_no,
        )
        self.assertEqual(taken.count(), 1)
        self.assertIn("上家镜像", taken.get().reason)

    def test_refused_claim_ends_loud_without_queuing_withdraw(self):
        """上游卡已被别人接走（`ClaimError ALREADY_TAKEN`）⇒ 响亮拒绝，⛔ 不排退单
        （E-0236 口径：谁都没接手就退对方副本 = 事故形状）。"""
        from apps.business.claim import ClaimError
        from apps.core.error_codes import ErrorCode

        with mock.patch.object(
            jidanpu_upstream, "claim_upstream_mirror",
            side_effect=ClaimError("已被接走", ErrorCode.ORDER_ALREADY_TAKEN),
        ):
            outcome = jidanpu_member.machine_claim_order(
                binding=self.member_binding, order=self.shadow,
                remote_uid=707, remote_name="合成打手", remote_order_id="JDP-M-0001",
                source_note="uid=707",
            )
        self.assertFalse(outcome.claimed)
        self.assertIsNotNone(outcome.claim_error)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind="member.withdraw", direction="out",
            ).count(),
            0,
        )

    def test_transport_error_is_translated_not_raised(self):
        """上游传输层异常翻成可预期失败（⛔ 不裸逃打挂整轮快路）。"""
        from jidanpu.errors import ApiError

        with mock.patch.object(
            jidanpu_upstream, "claim_upstream_mirror",
            side_effect=ApiError("传输失败"),
        ):
            outcome = jidanpu_member.machine_claim_order(
                binding=self.member_binding, order=self.shadow,
                remote_uid=707, remote_name="x", remote_order_id="JDP-M-0001",
                source_note="uid=707",
            )
        self.assertFalse(outcome.claimed)
        self.assertIsNotNone(outcome.claim_error)

    def test_audit_to_value_reads_the_promoted_price_not_the_stale_instance(self):
        """以实际转正动作生成接单日志，价格取转正瞬间。"""
        from apps.audit.models import OperationLog

        def _promote_and_overwrite_price(*args, **kwargs):
            self.mirror.status = JidanpuUpstreamHallOrder.Status.CLAIMING
            self.mirror.claiming_by = self.member_user
            self.mirror.remote_rec_fee = Decimal("75.00")
            self.mirror.save(update_fields=["status", "claiming_by", "remote_rec_fee"])
            return jidanpu_upstream.promote_upstream_mirror(
                self.mirror, binding=self.up_binding, detail={}, user_id=self.member_user.pk, now=NOW,
                claim_payload={
                    "claim_downstream": kwargs["downstream"], "claim_sync_actor": kwargs["sync_actor"],
                },
            )

        with mock.patch.object(
            jidanpu_upstream, "claim_upstream_mirror",
            side_effect=_promote_and_overwrite_price,
        ):
            outcome = jidanpu_member.machine_claim_order(
                binding=self.member_binding, order=self.shadow,
                remote_uid=707, remote_name="合成打手",
                remote_order_id="JDP-M-0001",
                source_note="uid=707 显示名=合成打手",
            )
        self.assertTrue(outcome.claimed)
        row = OperationLog.objects.get(
            action="order.claim",
            order_no=self.shadow.order_no,
        )
        self.assertEqual(row.to_value, "63.75")
        self.assertIn("已同步到下家", row.summary)


# ══ ⑧ 竞态两处置：上游 gone + 下家已接 ═══════════════════════════════════


class WithdrawRaceGateTests(_UpstreamFixture):
    """`_consume_withdraw` 的写前回读闸（D-0325 拍板⑦，E-0236 同形）。"""

    REMOTE_JID = "JDP-33160000000000001001"

    def _shadow_with_link(self, *, in_hall=True):
        mirror = self._listed_mirror()
        mirror.status = JidanpuUpstreamHallOrder.Status.GONE
        mirror.save(update_fields=["status"])
        shadow = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.REFUNDED, hall_price=Decimal("85.00"),
            total_price=Decimal("100.00"), source_order_no=REMOTE_ID,
            upstream_hall_card=mirror,
        )
        self._link(self.member_binding, shadow, remote_id=self.REMOTE_JID, in_hall=in_hall)
        event = jidanpu_member.queue_member_event(
            shadow, self.member_binding, kind=jidanpu_member.KIND_WITHDRAW,
        )
        return shadow, event

    def _consume(self, event, *, read_rows):
        """接桩跑一次消费：`fetch_order_by_key1` 按序回 `read_rows`（耗尽后回 None）。"""
        reads = list(read_rows)

        def fake_fetch(client, *, key, expect_field, expect_value):
            return reads.pop(0) if reads else None

        def fake_refund(client, **kwargs):
            calls.append(kwargs.get("remote_order_id"))

        calls: list[str] = []
        with (
            mock.patch.object(jidanpu, "fetch_order_by_key1", side_effect=fake_fetch),
            mock.patch.object(jidanpu, "set_order_status", side_effect=fake_refund),
        ):
            verdict = jidanpu_member.consume_event(event, client=object())
        return verdict, calls

    def test_unassigned_remote_still_gets_refunded(self):
        """竞态（a）：远端「未分配」（没人接）⇒ 照旧退单。"""
        shadow, event = self._shadow_with_link()
        verdict, refund_calls = self._consume(event, read_rows=[
            {"orderId": self.REMOTE_JID, "orderSta": jidanpu.ORDER_UNASSIGNED_LABEL},
            {"orderId": self.REMOTE_JID, "orderSta": jidanpu.ORDER_REFUNDED_LABEL},
        ])
        self.assertEqual(verdict, "done")
        self.assertEqual(refund_calls, [self.REMOTE_JID])
        link = JidanpuMemberOrderLink.objects.get(
            binding=self.member_binding, order=shadow,
        )
        self.assertIsNotNone(link.withdrawn_at)

    def test_in_progress_remote_is_protected_not_refunded(self):
        """竞态（b）：远端「代练中」（下家打手在做）⇒ 终态跳过保活单，`withdrawn_at` 不写。"""
        shadow, event = self._shadow_with_link()
        verdict, refund_calls = self._consume(event, read_rows=[
            {"orderId": self.REMOTE_JID, "orderSta": jidanpu.ORDER_IN_PROGRESS_LABEL},
        ])
        self.assertEqual(verdict, "skipped")
        self.assertEqual(refund_calls, [])  # 一次写都没打
        event.refresh_from_db()
        self.assertIn("保护对方活单", event.reason)
        self.assertIn(jidanpu.ORDER_IN_PROGRESS_LABEL, event.reason)
        link = JidanpuMemberOrderLink.objects.get(
            binding=self.member_binding, order=shadow,
        )
        self.assertIsNone(link.withdrawn_at)

    def test_unreadable_remote_falls_back_to_the_existing_flow(self):
        """回读读不到（①式口径问题）⇒ 不是「有人在做」的证据 ⇒ 照旧走退单流。"""
        shadow, event = self._shadow_with_link()
        verdict, refund_calls = self._consume(event, read_rows=[
            None,  # 竞态闸那一次读：读不到
            {"orderId": self.REMOTE_JID, "orderSta": jidanpu.ORDER_REFUNDED_LABEL},
        ])
        self.assertEqual(verdict, "done")
        self.assertEqual(refund_calls, [self.REMOTE_JID])

    def test_plain_orders_do_not_pay_the_extra_read(self):
        """普通单（非隐藏单）不为这道闸多打一次上游（既有行为一个字节不变）。"""
        order = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.REFUNDED, hall_price=Decimal("10.00"),
            total_price=Decimal("20.00"),
        )
        self._link(self.member_binding, order, remote_id=self.REMOTE_JID)
        event = jidanpu_member.queue_member_event(
            order, self.member_binding, kind=jidanpu_member.KIND_WITHDRAW,
        )
        verdict, refund_calls = self._consume(event, read_rows=[
            # ⭐ 只喂**两次**读（写后回读那一次）：普通单没有写前那一次 ⇒ 喂三次会耗不尽、
            #    喂两次恰好走完 = 「没有多打一次」的行为判据。
            {"orderId": self.REMOTE_JID, "orderSta": jidanpu.ORDER_REFUNDED_LABEL},
        ])
        self.assertEqual(verdict, "done")


# ══ ⑨ 慢路跳过 + 读面排除 ═════════════════════════════════════════════════


class SlowPathSkipTests(_UpstreamFixture):

    def test_remote_completion_on_a_hidden_shadow_is_skipped_with_one_ledger(self):
        """下家直接完成 ⇒ `SKIPPED`（⛔ 不写畸形待验收）+ 一次台账；重复轮不重复记。"""
        mirror = self._listed_mirror()
        shadow = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("85.00"),
            total_price=Decimal("100.00"), source_order_no=REMOTE_ID,
            upstream_hall_card=mirror,
        )
        link = self._link(self.member_binding, shadow, in_hall=True)

        def apply():
            return jidanpu_member.apply_remote_completion(
                binding=self.member_binding, order=shadow, link=link,
                remote_status="已完成", nodes=frozenset(), now=NOW,
                client=None,
            )

        outcome = apply()
        self.assertEqual(outcome.verdict, jidanpu_member.REMOTE_DONE_SKIPPED)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        rows = JidanpuSyncEvent.objects.filter(
            kind=jidanpu_member.KIND_STATUS_IN, target_ref=shadow.order_no,
        )
        self.assertEqual(rows.count(), 1)
        self.assertIn("隐藏单", rows.get().reason)
        # 第二轮（同一条链路再次命中「已完成」）⇒ 不重复记台账。
        self.assertEqual(apply().verdict, jidanpu_member.REMOTE_DONE_SKIPPED)
        self.assertEqual(rows.count(), 1)


class ReadFaceExclusionTests(_UpstreamFixture):

    def test_shadow_is_hidden_from_read_faces_and_reconcile(self):
        from apps.business import selectors

        mirror = self._listed_mirror()
        shadow = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("85.00"),
            total_price=Decimal("100.00"), source_order_no=REMOTE_ID,
            upstream_hall_card=mirror,
        )
        normal = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("10.00"),
            total_price=Decimal("20.00"),
        )
        # 列表 / 大厅 / 回收站。
        self.assertNotIn(shadow.pk, selectors.search_orders().values_list("pk", flat=True))
        self.assertIn(normal.pk, selectors.search_orders().values_list("pk", flat=True))
        self.assertNotIn(shadow.pk, selectors.search_hall().values_list("pk", flat=True))
        self.assertNotIn(
            shadow.pk, selectors.search_recycle_bin().values_list("pk", flat=True)
        )
        # 详情按单号仍可打开（隐藏 ≠ 不存在）——这一面由
        # `test_order_read_path_net` 的 `HALL_SHADOW_MUST_NOT_EXCLUDE_SITES` 与
        # `HallShadowDetailActionWhitelistTests` 两张网钉住（要 request 上下文，本文件不重演）。
        # 对账候选（orders_of）不含隐藏单 ⇒ 绑定后启用不补推（D-0325 拍板⑧）。
        self.assertNotIn(
            shadow.pk,
            list(jidanpu_member.orders_of(self.member_binding).values_list("pk", flat=True)),
        )
        # 链路那一支也不把它加回对账集合。
        result = jidanpu_member.reconcile(self.member_binding, now=NOW)
        self.assertNotIn(shadow.pk, [row.order.pk for row in result.to_launch])

    def test_restore_does_not_flip_a_shadow_back_to_unassigned(self):
        """回收站守卫：恢复隐藏单不把它翻成「未分配」真单（状态归镜像卡生命周期）。"""
        from apps.business.recycle import restore_order

        mirror = self._listed_mirror()
        shadow = Order.objects.create(
            order_no=generate_order_no(), game=self.game,
            status=OrderStatus.WAITING_GRAB, hall_price=Decimal("85.00"),
            total_price=Decimal("100.00"), source_order_no=REMOTE_ID,
            upstream_hall_card=mirror, deleted_at=NOW, deleted_by="夹具",
        )
        restore_order(order=shadow, actor=self.up_user)
        shadow.refresh_from_db()
        self.assertIsNone(shadow.deleted_at)
        # ⭐ 仍是 WAITING_GRAB（没有被翻成 UNASSIGNED——那会把一张隐藏卡变成真单）。
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))


# ══ ⑨ S-06（第五轮审计）：行循环对派生钩子的**逐行隔离** ═════════════════════


class SyncHallRowLoopIsolationTests(_UpstreamFixture):
    """`sync_hall` 的行循环里**单卡派生失败不许拖垮整轮**（S-06，第五轮审计）。

    现场（读码）：四处 `ensure_for_card` 裸调在行循环内，同族的辅助写（灰卡快照
    清理）都有 `try/except logger.exception` + 「响亮留痕不回滚」，唯独这条派生钩子
    没有——某张卡的数据形态让派生路径确定性抛错时，该绑定的大厅轮每轮在同一张卡上
    中断：后面的行不处理、`_mark_gone` 跑不到、`_stamp_binding_ok` 不落 ⇒ 消失判定
    停摆、幽灵卡堆积（与 R3-M2「回调消费卡死口」同形状）。修法 = 四处调用点套与
    快照清理同款的逐行隔离（响亮、不回滚、不中断轮）。

    ⚠️ 坏卡注入走**被查找的那个模块**（`upstream_hall_shadow` 的属性，E-0029）：
    `sync_hall` 是懒导入后按属性调用 ⇒ `mock.patch.object(upstream_hall_shadow, …)`。
    """

    BAD_ID = REMOTE_ID_2  # 行序在前的那张「坏卡」
    GOOD_ID = REMOTE_ID   # 行序在后的「好卡」（要证它没有被连坐）

    def _two_rows(self):
        return _HallStub(rows=[
            hall_row(order_id=self.BAD_ID, view=f"VIEW-{self.BAD_ID}", rec_fee="66.00"),
            hall_row(order_id=self.GOOD_ID, view=f"VIEW-{self.GOOD_ID}", rec_fee="100.00"),
        ])

    def test_one_bad_card_does_not_abort_the_round(self):
        """第一张卡派生抛错 ⇒ 只跳过它：好卡照常建镜像 + 派生、轮判 complete 正常返回。

        变异方向：摘掉 `jidanpu_upstream.py` 里四处 `ensure_for_card` 调用点**任意一处**
        的 `try/except`（行序 = 坏卡先到，命中的就是「新卡建镜像」那一处）⇒ `sync_hall`
        把 `RuntimeError` 直接抛出本函数，本条以 error 红。
        """
        stub = self._two_rows()
        real = upstream_hall_shadow.ensure_for_card
        attempted: list[str] = []

        def flaky(mirror, *, now=None):
            attempted.append(mirror.upstream_order_id)
            if mirror.upstream_order_id == self.BAD_ID:
                raise RuntimeError("注入：坏卡的派生路径确定性抛错")
            return real(mirror, now=now)

        with mock.patch.object(upstream_hall_shadow, "ensure_for_card", side_effect=flaky):
            with self.assertLogs("apps.business.jidanpu_upstream", level="ERROR") as logs:
                outcome = jidanpu_upstream.sync_hall(
                    binding=self.up_binding, client=stub, now=NOW,
                )
        # 轮**没有**被中断：完整返回、两张卡都进 listed、消失判定照常跑完（complete）。
        self.assertTrue(outcome.complete)
        self.assertEqual(outcome.pulled, 2)
        self.assertEqual(outcome.listed, 2)
        # 两张镜像行都落了库（好卡没有被坏卡连坐——它在坏卡**之后**被处理）。
        self.assertTrue(
            JidanpuUpstreamHallOrder.objects.filter(
                upstream_order_id=self.BAD_ID,
                status=JidanpuUpstreamHallOrder.Status.LISTED,
            ).exists(),
            "坏卡自己的镜像行应已落库（隔离只跳过派生，不回滚行）",
        )
        self.assertTrue(
            JidanpuUpstreamHallOrder.objects.filter(
                upstream_order_id=self.GOOD_ID,
                status=JidanpuUpstreamHallOrder.Status.LISTED,
            ).exists(),
            "好卡的镜像行消失 ⇒ 整轮被坏卡拖垮了",
        )
        # 好卡的派生**照常被尝试**（真函数跑通：建出隐藏单 + 排 launch）。
        self.assertEqual(attempted, [self.BAD_ID, self.GOOD_ID])
        good_mirror = JidanpuUpstreamHallOrder.objects.get(upstream_order_id=self.GOOD_ID)
        self.assertIsNotNone(self._shadow_of(good_mirror))
        # 坏卡只被跳过、没有隐藏单。
        bad_mirror = JidanpuUpstreamHallOrder.objects.get(upstream_order_id=self.BAD_ID)
        self.assertIsNone(self._shadow_of(bad_mirror))
        # 响亮留痕：ERROR 恰一行，点名坏卡（⛔ 不许静默吞）。
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn(self.BAD_ID, logs.output[0])


# ══ ⑪ S-03 / D-0331（业务方拍板）：人工退款优先 ══════════════════


class ManualRefundPriorityTests(_UpstreamFixture):
    """隐藏单被人工退款后，下一轮拉取不得静默撤销该退款（S-03，第五轮审计）。

    现场（审计原文）：隐藏单被人手动改 `REFUNDED` 后，镜像卡仍 `listed` 时下一轮
    listed 刷新的复活支会把同一张单翻回 `WAITING_GRAB` + 重新抛——退款在一个拉取轮
    内被静默作废。修法（D-0331）：复活只认「本引擎 `refund_for_card` 退的」（同事务
    台账标记，payload 钉订单号 + 与订单行同一个 `updated_at` 时刻）；人工退的走
    「人工退款优先」支：不复活 / 不补抛 / 不对价 + 台账恰一条，**卡不下架**（镜像
    生命周期属上家，本地置 gone 会被 D-0300 复活支每轮翻回 listed 打摆；下游副本由
    人工退款自身的出站漏斗 `member.status_out` 撤）。

    ⚠️ 「人工退款」夹具按审计原文的库面状态造（`update(status=REFUNDED,
    updated_at=…)`）：人工口的完整漏斗（`apply_transition` 的钱钩子 / 出站事件）是
    订单域既有行为、不在本组判据面内——本组钉的是**拉取轮读到的状态**与它对该状态
    的处置。判据全部打在数据库行上（状态 / 台账行数 / payload 值）。
    """

    T_REFUND = NOW + timedelta(hours=1)

    def _manual_refund(self, shadow):
        """人工把隐藏单改成 REFUNDED（审计 S-03 的原文动作），时刻钉在拉取轮之后。"""
        Order.objects.filter(pk=shadow.pk).update(
            status=OrderStatus.REFUNDED, updated_at=self.T_REFUND,
        )
        shadow.refresh_from_db()

    def _engine_marks(self, shadow):
        return JidanpuSyncEvent.objects.filter(
            kind=upstream_hall_shadow.KIND_SHADOW_REFUNDED,
            payload__order_no=shadow.order_no,
        )

    def _manual_rows(self):
        return JidanpuSyncEvent.objects.filter(
            kind=upstream_hall_shadow.KIND_MANUAL_REFUND
        )

    def test_manually_refunded_shadow_is_not_revived_by_the_next_listed_round(self):
        """① 核心：人工退款 + 卡 listed ⇒ 下一轮不复活单、不重抛、不对价，台账恰一条。

        对价「不发生」用真值钉：把隐藏单 `hall_price` 拨成 1.00、把镜像 `recFee` 改成
        200（展示价 170 ≠ 1.00）——若复活支或对价支还在跑，`hall_price` 必被写成
        170 且排 `member.price`。失败方向 = 摘掉 `ensure_for_card` 入口的人工退款
        优先支 ⇒ 单被翻回 `WAITING_GRAB`、价被改写，本条红。
        """
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        self.assertEqual(self._launch_events(shadow).count(), 1)
        self._manual_refund(shadow)
        Order.objects.filter(pk=shadow.pk).update(hall_price=Decimal("1.00"))
        # 下一轮：卡仍在厅、上家把价改成了 200 ⇒ 展示价 170。
        stub2 = _HallStub(rows=[hall_row(rec_fee="200.00")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub2, now=NOW)
        shadow.refresh_from_db()
        # ⛔ 不复活：退款态原样。
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        # ⛔ 不重抛：launch 还是当初那一条。
        self.assertEqual(self._launch_events(shadow).count(), 1)
        # ⛔ 不对价：价没被 170 覆盖、`member.price` 一条都没排。
        self.assertEqual(shadow.hall_price, Decimal("1.00"))
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind="member.price", target_ref=shadow.order_no, direction="out",
            ).count(),
            0,
        )
        # ⛔ 卡不下架：镜像生命周期属上家（D-0300 复活支会把本地 gone 每轮翻回）。
        mirror = JidanpuUpstreamHallOrder.objects.get()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        # 台账恰一条（人话点名人工退款与恢复路径）；再过一轮仍是恰一条（幂等）。
        rows = self._manual_rows()
        self.assertEqual(rows.count(), 1)
        self.assertIn("人工退款", rows.get().reason)
        self.assertIn("重新指派", rows.get().reason)
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub2, now=NOW)
        self.assertEqual(self._manual_rows().count(), 1)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))

    def test_manually_refunded_shadow_survives_a_gone_cycle(self):
        """② 人工退款历经 gone 周期也不复活：gone 不写标记（翻不动已退的单）、
        卡回厅后复活支认不出引擎凭证 ⇒ 人工优先支接住。

        这一景是「按调用点区分」的修法防不住的缝：复活由 D-0300 复活支的 ensure
        触发，与「引擎退于 gone 期」的合法复活完全同形——只有退款凭证本身能分。
        失败方向 = 复活判据退回「REFUNDED 即复活」⇒ 卡回厅那一拍单被翻回
        `WAITING_GRAB`，本条红。
        """
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        self._manual_refund(shadow)
        # 卡离厅（拉全轮没它 + 回查查不到）⇒ gone；引擎对已退的单翻不动、
        # ⛔ 也不许留下冒名的引擎标记（rowcount=0 就不写）。
        gone_round = _HallStub(rows=[hall_row(order_id=REMOTE_ID_2, view="V-0002")])
        with mock.patch.object(jidanpu, "fetch_order_by_key1", return_value=None):
            jidanpu_upstream.sync_hall(
                binding=self.up_binding, client=gone_round, now=NOW,
            )
        mirror = JidanpuUpstreamHallOrder.objects.get(upstream_order_id=REMOTE_ID)
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.GONE)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        self.assertEqual(self._engine_marks(shadow).count(), 0)
        # 卡又回厅（D-0300 复活）⇒ 复活支必须认不出「引擎退的」⇒ 人工退款原样。
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        mirror.refresh_from_db()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        self.assertEqual(self._launch_events(shadow).count(), 1)
        self.assertEqual(self._engine_marks(shadow).count(), 0)
        self.assertEqual(self._manual_rows().count(), 1)

    def test_engine_refund_leaves_a_marker_and_card_return_still_revives(self):
        """③ 引擎退款回归钉：`refund_for_card` 同事务留标记（payload 钉单号 + 订单行
        同一个 `updated_at` 时刻）；卡回厅照旧复活同一张 + 重抛。

        失败方向 = 摘掉 `refund_for_card` 里的标记写口 ⇒ 复活支认不出引擎凭证、把
        引擎退的单也当人工退的拦下（`test_revived_card_reuses_and_rethrows_the_same_shadow`
        同拍红），本条的标记断言直接红。
        """
        stub = _HallStub(rows=[hall_row()])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        gone_round = _HallStub(rows=[hall_row(order_id=REMOTE_ID_2, view="V-0002")])
        with mock.patch.object(jidanpu, "fetch_order_by_key1", return_value=None):
            jidanpu_upstream.sync_hall(
                binding=self.up_binding, client=gone_round, now=NOW,
            )
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        self.assertEqual(self._withdraw_events(shadow).count(), 1)
        # 标记恰一条：钉同一张单、时刻与订单行 `updated_at` **同一个值**（判据读法见
        # `_engine_refund_alive`；按时刻相等对拍，aware datetime 比瞬间不比表示）。
        marks = self._engine_marks(shadow)
        self.assertEqual(marks.count(), 1)
        mark = marks.get()
        self.assertEqual(mark.payload["order_no"], shadow.order_no)
        self.assertEqual(
            parse_datetime(mark.payload["order_updated_at"]), shadow.updated_at,
        )
        # 卡回厅 ⇒ 照旧复活同一张并重抛（既有合法流不被本支误伤）。
        first_launch = self._launch_events(shadow).get()
        first_launch.status = "done"
        first_launch.save(update_fields=["status"])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        self.assertEqual(self._launch_events(shadow).count(), 2)
        # 复活不是退款：标记仍一条、人工台账零行。
        self.assertEqual(self._engine_marks(shadow).count(), 1)
        self.assertEqual(self._manual_rows().count(), 0)

    def test_second_engine_cycle_gets_a_fresh_marker(self):
        """④ 同一张卡第二个 gone 周期拿到**新**标记（判据为什么⛔ 不用 note_once）。

        四元组幂等只会在第一周期留一条标记；若判据靠「标记行存在」，第二周期退款后
        卡回厅时读到的还是第一周期的老时刻——与第二周期订单行的 `updated_at` 对不上
        ⇒ 引擎自己的退款会被误判成人工的。失败方向 = 把 `_mark_engine_refunded` 改成
        `note_once` 形态（同键去重）⇒ 第二周期复活那一拍红。
        """
        stub = _HallStub(rows=[hall_row()])
        gone_round = _HallStub(rows=[hall_row(order_id=REMOTE_ID_2, view="V-0002")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        later = NOW + timedelta(hours=2)
        # 周期一：退（NOW）→ 回（复活）。
        with mock.patch.object(jidanpu, "fetch_order_by_key1", return_value=None):
            jidanpu_upstream.sync_hall(
                binding=self.up_binding, client=gone_round, now=NOW,
            )
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        self.assertEqual(self._engine_marks(shadow).count(), 1)
        # 周期二：再退（now=later ⇒ 订单行与标记都钉 later）→ 再回 ⇒ 必须照样复活。
        with mock.patch.object(jidanpu, "fetch_order_by_key1", return_value=None):
            jidanpu_upstream.sync_hall(
                binding=self.up_binding, client=gone_round, now=later,
            )
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        self.assertEqual(shadow.updated_at, later)
        self.assertEqual(self._engine_marks(shadow).count(), 2)
        latest = self._engine_marks(shadow).order_by("-created_at", "-id").first()
        self.assertEqual(
            parse_datetime(latest.payload["order_updated_at"]), later,
        )
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=later)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        self.assertEqual(self._manual_rows().count(), 0)

    def test_human_refund_after_an_engine_cycle_is_still_respected(self):
        """⑤ 老周期的标记不给后来的人工退款背书（判据「时刻相等」半边的承重景）。

        时序：引擎退（标记钉 NOW）→ 卡回厅复活（`updated_at` 刷成回厅轮）→ **人工**
        退款（`updated_at`=T_REFUND）→ 下一轮。此时标记行**存在**但时刻对不上
        ⇒ 必须走人工优先支。失败方向 = 判据退回「标记行存在即可复活」⇒ 本条红。
        """
        stub = _HallStub(rows=[hall_row()])
        gone_round = _HallStub(rows=[hall_row(order_id=REMOTE_ID_2, view="V-0002")])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        with mock.patch.object(jidanpu, "fetch_order_by_key1", return_value=None):
            jidanpu_upstream.sync_hall(
                binding=self.up_binding, client=gone_round, now=NOW,
            )
        # 第一条 launch 仍「在飞」会被复活那一拍的在飞去重归并 ⇒ 先标 done 再看新排的。
        first_launch = self._launch_events(shadow).get()
        first_launch.status = "done"
        first_launch.save(update_fields=["status"])
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        self.assertEqual(self._launch_events(shadow).count(), 2)
        self._manual_refund(shadow)
        jidanpu_upstream.sync_hall(binding=self.up_binding, client=stub, now=NOW)
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        self.assertEqual(self._launch_events(shadow).count(), 2)
        self.assertEqual(self._engine_marks(shadow).count(), 1)
        self.assertEqual(self._manual_rows().count(), 1)

    def test_price_gate_refund_is_engine_refund_and_recovers_with_the_price(self):
        """⑥ 价闸的退款也是引擎退的：留标记；上家把价修回来 ⇒ 照旧复活（既有语义
        不被本支误伤），且⛔ 不落人工台账（它不是人工退的）。

        价闸是引擎三个退款出口里**唯一**卡还 listed 的（另两个卡已 gone）——它证明
        「引擎退 ⇔ 卡曾 gone」不成立、判据只能钉在退款凭证上。失败方向 = 判据改成
        「标记 + 卡曾 gone」之类形态 ⇒ 价闸恢复那一拍被误拦，本条红。
        """
        jidanpu_upstream.sync_hall(
            binding=self.up_binding, client=_HallStub(rows=[hall_row(rec_fee="25.00")]),
            now=NOW,
        )
        shadow = self._shadow_of(JidanpuUpstreamHallOrder.objects.get())
        self.assertEqual(self._launch_events(shadow).count(), 1)
        # 改价成 0 ⇒ 价闸退款（卡留在厅）+ 标记。
        jidanpu_upstream.sync_hall(
            binding=self.up_binding, client=_HallStub(rows=[hall_row(rec_fee="0.00")]),
            now=NOW,
        )
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.REFUNDED))
        mirror = JidanpuUpstreamHallOrder.objects.get()
        self.assertEqual(mirror.status, JidanpuUpstreamHallOrder.Status.LISTED)
        self.assertEqual(self._engine_marks(shadow).count(), 1)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(kind=NO_PRICE_KIND).count(), 1,
        )
        # 价修回来 ⇒ 引擎退的照旧复活（不是人工退款，台账不许落人工那一条）。
        jidanpu_upstream.sync_hall(
            binding=self.up_binding, client=_HallStub(rows=[hall_row(rec_fee="25.00")]),
            now=NOW,
        )
        shadow.refresh_from_db()
        self.assertEqual(str(shadow.status), str(OrderStatus.WAITING_GRAB))
        self.assertEqual(self._manual_rows().count(), 0)
