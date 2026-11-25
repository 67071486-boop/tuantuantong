"""D-0303 大厅镜像 **P5**（`feat/peer-hall-port`）：转发跳数上限 + 关开关 / 解绑下架存量卡。

权威口径 = `docs/specs/2026-10-25-peer-hall-share-design.md` §3（环与深度：`hop` 上限 5，
超限丢弃并记 `peer.hall_hop_limit`）、§8 / §11-2（关开关时存量镜像**全部下架**）、
§11-3（解绑时镜像行**留档**、绑定行与卡行都不删）。

本文件钉住七块（每条都能被单独弄红）：

① **跳数**：源头（载荷里**没有** `hop` 键）= 0 ⇒ 下一跳 1 ⇒ … ⇒ `hop=5` 仍收；
   `hop=6` **不建卡、不建游戏、不转发**，只记一条台账；单跳（无 `hop` 键）行为逐字不变；
   坏形状的 `hop`（字符串 / 负数 / bool）按 0 兜底、⛔ 不 500。
② **关开关（PUT True → False）**：同一请求里把这份绑定名下 `listed` 的卡标 `gone`
   （`claiming` 不动）、下架行数进日志；读口不再出现；**再打开开关不会凭空复活**
   （只有上游重新抛才会复活）。
③ **解绑（DELETE）**：`listed` → `gone`；⭐ **改准（Z5 2026-10-25）**：
   `claiming` → **`listed`**（不是「不动」，也不是 `gone`）＋ 一条退回台账；
   绑定行软删仍在、卡行仍在。
④ **反例**：开关本来就关着时改百分比 / 开关保持开着再 PUT ⇒ **不触发**下架；
   解绑只下架**自己那份绑定**名下的卡（另一份绑定的卡一张不动）。
⑤ ⭐ **B1改价防环**：A↔B 互绑时一次改价沿环来回转发，跳数逐跳 +1、
   到上限就**只刷新本机价、不再转发**（出站条数 ≤ 上限），并留一条人话台账。
⑥ ⭐ **B2升级清理**：迁移 `0090` 把存量 `listed` 卡标 `gone`
   （`claiming` / `promoted` 不动、行不删）；反向是**有意的 no-op**。
⑦ ⭐ **B3下游撤卡**：关开关 / 解绑下架、以及 `claiming` 带着 take 证据
   收口成 `gone` 时，向**其它下家绑定**补发 `hall.take`（发起方只收 `claim_refused`）。
   真实 A→B→C 链的端到端判据在 `tests/test_peer_hall_chain.py::DownstreamCardWithdrawTests`。

⚠️ 判据钉**字面值**（`"peer.hall_hop_limit"` / `"listed"` / `"gone"` / 人话文案），
⛔ 不拿实现常量当期望（E-0024）。
⚠️ 夹具值全部合成值（`*.test.invalid` / `P5-` 前缀 / 合成单号），见 E-0077。
⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里。
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import (
    Game,
    PeerDownstreamBinding,
    PeerHallMirror,
    PeerSyncEvent,
    PeerUpstreamBinding,
)
from apps.business.peer_inbound import apply_events
from apps.business.peer_sync import (
    HALL_CLAIM_REFUSED_KIND,
    HALL_REPRICE_KIND,
    HALL_TAKE_KIND,
    HALL_THROW_KIND,
    hash_token,
    new_token,
)
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: 一条**我方为下家**的上家绑定地址（合成值）。
UP_BASE = "https://peer-hall-p5-up.test.invalid"
#: 大厅卡的号（沿链不变 = 源头单号）。
CARD_NO = "P5-CARD-0001"
#: 按**名字**对齐的游戏（R21：⛔ 不传对端 id）。
P5_GAME = "P5跳数夹具游戏"
#: `hop=6` 那条事件用的新游戏名：证明「超限丢弃 ⇒ 连游戏都不建」（零写入）。
NEVER_SEEN_GAME = "P5超限从未建过的游戏"

#: 关开关 / 解绑下架时写进 `last_error` 的两句**字面**人话（⛔ 不 import 常量当期望）。
UNLISTED_ON_SWITCH_OFF_TEXT = "上家大厅同步已关闭，这张卡已从大厅下架"
UNLISTED_ON_UNBIND_TEXT = "与上家的绑定已解除，这张卡已从大厅下架"
#: 超限台账的 kind（外部排障口径，逐字写死）。
HOP_LIMIT_KIND_TEXT = "peer.hall_hop_limit"
#: 页面绑定端点 + 节点门（裁决 R22；⛔ 不从视图 import 常量当期望）。
PAGE_URL = "/api/business/peer-bindings/"
PAGE_NODE = "system.bind_upstream_system"


def hall_payload(no, *, game_name=P5_GAME, price="50.00", hop=None):
    """一条 `order_payload` 形状的合成载荷（与 `test_peer_hall_inbound.py` 同款）。

    ⚠️ `hop=None`（默认）⇒ **键不出现**（单跳 / 老版本对端的真实形状）；
    要测「第 0 跳显式带键」就传 `hop=0`。
    """
    payload = {
        "order_no": no,
        "status": "WAITING_GRAB",
        "total_price": "100.00",
        "hall_price": price,
        "remark": "syn-备注",
        "game_name": game_name,
        "server_id": None,
        "project_id": None,
        "project_text": "syn-项目",
        "customer_phone": "",
        "customer_qq": "",
        "content": "syn-需求",
        "source_order_no": "",
    }
    if hop is not None:
        payload["hop"] = hop
    return payload


def hall_event(no, *, payload=None, kind=HALL_THROW_KIND):
    """一条大厅事件（worker 投出来的形状：kind / target_ref / payload / 事件级时刻）。"""
    return {
        "idempotency_key": f"syn-p5-{no}-{kind}",
        "kind": kind,
        "target_ref": no,
        "occurred_at": "2026-10-25T10:00:00+00:00",
        "payload": payload if payload is not None else hall_payload(no),
    }


def reprice_payload(no, *, hop=None, price="50.00"):
    """一条 `hall.reprice` 载荷（与 `hall_payload` 同形；`hop` 缺省 ⇒ 键不出现 = 源头改价）。"""
    payload = hall_payload(no, price=price)
    if hop is not None:
        payload["hop"] = hop
    return payload


class HallHopFixture(TenantTestCase):
    """下家视角夹具：一条上家绑定 + 一条**下家绑定**（用来观察「往下一跳转发」）。

    ⚠️ 下家绑定是承重的：没有它，`hop > 5` 的「**不转发**」就成了空断言
    （`enqueue_hall_mirror_event` 的目标集本来就是空的）。
    """

    def setUp(self):
        self.game = Game.objects.create(name=P5_GAME)
        self.operator = User.objects.create_user(
            username="p5-hop-op", password="pw-p5-hop"
        )
        # 下家绑定要一个**独立**账号（`account` 是 OneToOne）。
        self.downstream_account = User.objects.create_user(
            username="p5-hop-downstream", password="pw-p5-hop-down"
        )
        self.downstream = PeerDownstreamBinding.objects.create(
            account=self.downstream_account,
            peer_base_url="https://peer-p5-down.test.invalid",
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

    def _binding(self, base_url=UP_BASE, **over):
        data = {
            "peer_name": "测试上家P5",
            "peer_base_url": base_url,
            "peer_account": "p5-up-account",
            "token_in_hash": hash_token(new_token()),
            "token_out": new_token(),
            "local_base_url": "https://peer-p5-local.test.invalid",
            "bound_by": self.operator,
            "hall_sync_enabled": True,
        }
        data.update(over)
        return PeerUpstreamBinding.objects.create(**data)

    def _apply(self, binding, *events):
        return apply_events(list(events), binding=binding)

    def _mirror(self, binding, card_no=CARD_NO, *, status=PeerHallMirror.Status.LISTED,
                **over):
        """一条镜像行（合成值）；`over` 可覆盖状态 / 接单列 / 证据列。"""
        now = timezone.now()
        data = {
            "peer_upstream_binding": binding,
            "peer_base_url": binding.peer_base_url,
            "peer_order_no": card_no,
            "status": status,
            "game": self.game,
            "payload": hall_payload(card_no),
            "remote_hall_price": Decimal("50.00"),
            "first_seen_at": now,
            "last_seen_at": now,
        }
        data.update(over)
        return PeerHallMirror.objects.create(**data)

    def _pending_throw(self, card_no=CARD_NO):
        return PeerSyncEvent.objects.filter(
            binding_role="downstream",
            binding_id=str(self.downstream.pk),
            kind=HALL_THROW_KIND,
            target_ref=card_no,
            status=PeerSyncEvent.Status.PENDING,
        ).first()


class HallHopLimitTests(HallHopFixture):
    """① 跳数：源头 0 ⇒ 每跳 +1；`hop=6` 丢弃并记台账（⛔ 不 500、零写入）。"""

    def test_a_single_hop_payload_without_hop_lands_at_zero_and_forwards_one(self):
        """⭐ 单跳（载荷**没有** `hop` 键）行为逐字不变：卡在 0，转发出 1，不记台账。"""
        binding = self._binding()
        outcome = self._apply(binding, hall_event(CARD_NO))
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 0, outcome)
        row = PeerHallMirror.objects.get(peer_order_no=CARD_NO)
        self.assertEqual(row.hop, 0)
        forwarded = self._pending_throw()
        self.assertIsNotNone(forwarded, "有下家绑定时 throw 必须往下一跳转发")
        self.assertEqual(forwarded.payload["hop"], 1)
        # 单跳 ⇒ 没有超限台账（正对照：上面的 1 不是「靠丢弃得来的」）。
        self.assertFalse(
            PeerSyncEvent.objects.filter(kind=HOP_LIMIT_KIND_TEXT).exists()
        )

    def test_each_hop_adds_exactly_one_to_the_card_and_the_forwarded_payload(self):
        """链上 0 → 1 → … → 5 都收：卡上的 `hop` = 上游载荷那个值，转发时 +1。"""
        for expected in range(6):
            binding = self._binding(base_url=f"{UP_BASE}-{expected}")
            payload = hall_payload(CARD_NO, hop=expected)
            outcome = self._apply(binding, hall_event(CARD_NO, payload=payload))
            self.assertEqual(outcome.applied, 1, f"hop={expected}: {outcome}")
            row = PeerHallMirror.objects.get(
                peer_base_url=binding.peer_base_url, peer_order_no=CARD_NO
            )
            self.assertEqual(row.hop, expected)
            forwarded = self._pending_throw()
            self.assertIsNotNone(forwarded)
            self.assertEqual(forwarded.payload["hop"], expected + 1)
        self.assertFalse(
            PeerSyncEvent.objects.filter(kind=HOP_LIMIT_KIND_TEXT).exists()
        )

    def test_hop_five_is_the_last_one_that_lands(self):
        """边界：5 收（再转发是 6）、6 弃——判据是 `hop > 5`，⛔ 不是 `>=`。"""
        accepted = self._binding(base_url=f"{UP_BASE}-five")
        self._apply(
            accepted, hall_event(CARD_NO, payload=hall_payload(CARD_NO, hop=5))
        )
        self.assertEqual(
            PeerHallMirror.objects.get(
                peer_base_url=accepted.peer_base_url, peer_order_no=CARD_NO
            ).hop,
            5,
        )

        dropped = self._binding(base_url=f"{UP_BASE}-six")
        outcome = self._apply(
            dropped, hall_event(CARD_NO, payload=hall_payload(CARD_NO, hop=6))
        )
        # 「处理完了」而不是「跳过」：对端按 applied 记 done，⛔ 不重投一条注定失败的事件。
        self.assertEqual(outcome.applied, 1, outcome)
        self.assertEqual(outcome.skipped, 0, outcome)
        self.assertEqual(outcome.skipped_details, [])
        self.assertFalse(
            PeerHallMirror.objects.filter(
                peer_base_url=dropped.peer_base_url
            ).exists()
        )

    def test_hop_six_is_dropped_with_a_ledger_row_and_no_forward(self):
        """⭐ 超限：不建卡、**连游戏都不建**、不转发，只留一条 `peer.hall_hop_limit` 台账。"""
        binding = self._binding()
        events_before = PeerSyncEvent.objects.count()
        outcome = self._apply(
            binding,
            hall_event(
                CARD_NO,
                payload=hall_payload(CARD_NO, game_name=NEVER_SEEN_GAME, hop=6),
            ),
        )
        self.assertEqual(outcome.applied, 1, outcome)
        # ① 零业务写入：没有卡、没有游戏（`resolve_game` 没跑）、没有出站事件。
        self.assertFalse(PeerHallMirror.objects.exists())
        self.assertFalse(Game.objects.filter(name=NEVER_SEEN_GAME).exists())
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                direction="out", kind=HALL_THROW_KIND, target_ref=CARD_NO
            ).exists()
        )
        # ② 恰好一条台账（+1 行，不是 +2：没有出站事件）。
        self.assertEqual(PeerSyncEvent.objects.count(), events_before + 1)
        ledger = PeerSyncEvent.objects.get(kind=HOP_LIMIT_KIND_TEXT)
        self.assertEqual(ledger.direction, "in")
        self.assertEqual(ledger.status, "done")
        self.assertEqual(ledger.target_type, "hall")
        self.assertEqual(ledger.target_ref, CARD_NO)      # 源头单号
        self.assertEqual(ledger.binding_role, "upstream")
        self.assertEqual(ledger.binding_id, str(binding.pk))
        self.assertIn("5", ledger.reason)
        self.assertIn("跳", ledger.reason)
        self.assertEqual(ledger.payload["hop"], 6)
        self.assertEqual(ledger.payload["hop_limit"], 5)

    def test_a_malformed_hop_is_treated_as_zero_and_never_raises(self):
        """坏形状的 `hop`（字符串 / 负数 / bool）⇒ 按 0 收（⛔ 不 500、⛔ 不静默丢卡）。"""
        for index, bad in enumerate(("abc", -3, True)):
            with self.subTest(bad=bad):
                card_no = f"{CARD_NO}-BAD-{index}"
                binding = self._binding(base_url=f"{UP_BASE}-bad-{index}")
                outcome = self._apply(
                    binding,
                    hall_event(card_no, payload=hall_payload(card_no, hop=bad)),
                )
                self.assertEqual(outcome.applied, 1, f"{bad!r}: {outcome}")
                row = PeerHallMirror.objects.get(
                    peer_base_url=binding.peer_base_url, peer_order_no=card_no
                )
                self.assertEqual(row.hop, 0)
                self.assertFalse(
                    PeerSyncEvent.objects.filter(kind=HOP_LIMIT_KIND_TEXT).exists()
                )


class RepriceHopLoopTests(HallHopFixture):
    """⭐ B1（终审修复轮）：A↔B **互绑**时一次改价也不能沿环无限来回转发。

    模拟方式（单租户、零 HTTP）：本站同时是 A 的下家与 A 的上家（下游绑定的
    `peer_base_url` 指回同一个上家域名）。对端收到我们发出去的 `hall.reprice` 之后，
    按同一条分支做的唯一一件事就是「刷新它本地的价 + 把跳数 +1 再转发回来」⇒ 把**本站刚
    发出去的那一份载荷原样喂回来**，就是那条环的下一跳（价格按 100% 传递、不漂）。

    ⚠️ 判别力：把 `_apply_hall_event` reprice 支的跳数判据删掉（或改成拿**行上的** `hop`
    转发），本用例要么在 20 次内收敛不了（`self.fail`），要么出站条数远超 5 ⇒ 红。
    """

    def setUp(self):
        super().setUp()
        # 互绑的 B 那一侧：下游绑定指回同一个上家域名（纯语义标注；环靠下面手工喂回来模拟）。
        self.downstream.peer_base_url = UP_BASE
        self.downstream.save(update_fields=["peer_base_url"])

    def _reprice(self, binding, payload):
        return self._apply(
            binding, hall_event(CARD_NO, kind=HALL_REPRICE_KIND, payload=payload)
        )

    def _outbound_reprice(self):
        return (
            PeerSyncEvent.objects.filter(
                binding_role="downstream",
                binding_id=str(self.downstream.pk),
                kind=HALL_REPRICE_KIND,
                target_ref=CARD_NO,
            )
            .order_by("-pk")
            .first()
        )

    def test_one_price_change_in_a_mutual_binding_stops_at_the_hop_limit(self):
        binding = self._binding()
        self._mirror(binding)          # 卡已经在厅（对应源头那一跳的落卡）
        # 第 0 跳：对端 A 改自己的发单价（native 载荷**没有** hop 键）
        outcome = self._reprice(binding, reprice_payload(CARD_NO, price="51.00"))
        self.assertEqual(outcome.applied, 1, outcome)
        forwarded = self._outbound_reprice()
        self.assertIsNotNone(forwarded, "互绑时改价必须往下游转发（正对照）")
        self.assertEqual(forwarded.payload["hop"], 1)

        forwards = 1
        received = 1
        inbound = dict(forwarded.payload)
        for _ in range(20):
            before = inbound["hop"]
            outcome = self._reprice(binding, inbound)
            self.assertEqual(outcome.applied, 1, outcome)   # 超限那一档也是「处理完了」
            received += 1
            latest = self._outbound_reprice()
            next_hop = latest.payload["hop"]
            if next_hop == before:
                break
            self.assertEqual(
                next_hop, before + 1,
                "改价事件的跳数必须逐跳 +1（否则环不会被上限截断）",
            )
            forwards += 1
            inbound = dict(latest.payload)
        else:
            self.fail("互绑改价回环在 20 跳内没有收敛（B1 的防环失效）")

        # ① 有限、且**出站条数 ≤ 上限**（5）：第 6 跳会被对端按同一条判据丢弃 ⇒ 不发白工。
        self.assertEqual(forwards, 5)
        self.assertLessEqual(received, 6)
        # ② 本地镜像行**照常刷新价格**：截断的是转发，不是本机的价。
        row = PeerHallMirror.objects.get(
            peer_base_url=binding.peer_base_url, peer_order_no=CARD_NO
        )
        self.assertEqual(row.status, PeerHallMirror.Status.LISTED)
        self.assertEqual(row.remote_hall_price, Decimal(inbound["hall_price"]))
        # ③ 截断时留一条人话台账，且与 throw 的「丢弃」分开（这张卡**还在**）。
        ledger = PeerSyncEvent.objects.get(kind=HOP_LIMIT_KIND_TEXT)
        self.assertEqual(ledger.payload["hop"], 5)
        self.assertEqual(ledger.payload["hop_limit"], 5)
        self.assertIn("价", ledger.reason)
        self.assertIn("5", ledger.reason)
        self.assertEqual(PeerHallMirror.objects.count(), 1)


class LegacyCardCleanupMigrationTests(HallHopFixture):
    """⭐ B2（终审修复轮）：升级迁移把存量 `listed` 卡标 `gone`；`claiming` / `promoted` 不动。

    驱动方式：直接调迁移模块里的**前向函数**（`apps.get_model` 拿的就是真实模型，迁移只碰
    今天仍然存在的列）——`RunPython` 在真实迁移里调的是同一份函数、同一个 app registry。
    ⚠️ 迁移本身在测试库建库时已经跑过一次（空表 ⇒ 零行变更），不会与这里的夹具互相干扰。
    """

    #: 迁移写进 `last_error` 的字面人话（⛔ 不 import 迁移里的常量当期望，E-0024）。
    LEGACY_NOTE = "大厅共享升级：旧卡统一下架，等上家重新抛单"

    def _migration(self):
        # 模块名以数字开头 ⇒ 只能用 import_module（普通 import 语法写不出来）。
        from importlib import import_module

        return import_module(
            "apps.business.migrations.0090_unlist_legacy_peer_hall_cards"
        )

    def test_listed_cards_are_marked_gone_while_claiming_and_promoted_stay(self):
        from django.apps import apps as django_apps

        migration = self._migration()
        binding = self._binding()
        listed = self._mirror(binding, "P5-MIG-LISTED")
        claiming = self._mirror(
            binding, "P5-MIG-CLAIMING",
            status=PeerHallMirror.Status.CLAIMING,
            claiming_by=self.operator,
            claiming_at=timezone.now(),
        )
        promoted = self._mirror(binding, "P5-MIG-PROMOTED",
                                status=PeerHallMirror.Status.PROMOTED)

        migration.unlist_legacy_hall_cards(django_apps, None)

        listed.refresh_from_db()
        self.assertEqual(listed.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(listed.gone_at)
        self.assertEqual(listed.last_error, self.LEGACY_NOTE)
        for row in (claiming, promoted):
            row.refresh_from_db()
            self.assertNotEqual(row.status, PeerHallMirror.Status.GONE, row.peer_order_no)
            self.assertIsNone(row.gone_at, row.peer_order_no)
            self.assertEqual(row.last_error, "", row.peer_order_no)
        # ⛔ 不删行（留档）：三行都还在。
        self.assertEqual(PeerHallMirror.objects.count(), 3)

    def test_the_reverse_is_an_intentional_noop(self):
        """反向**不恢复**数据（理由写在迁移 docstring）：已 `gone` 的行照旧 `gone`。"""
        from django.apps import apps as django_apps

        migration = self._migration()
        binding = self._binding()
        row = self._mirror(binding, "P5-MIG-ROLLBACK")
        migration.unlist_legacy_hall_cards(django_apps, None)
        migration.keep_data_as_is(django_apps, None)      # 不抛、不改
        row.refresh_from_db()
        self.assertEqual(row.status, PeerHallMirror.Status.GONE)


class UnlistFixture(TenantTestCase):
    """页面端点夹具：一个持节点门的客服 client + 一条大厅读口 client + 绑定 / 卡片工厂。"""

    def setUp(self):
        cache.clear()
        self.game = Game.objects.create(name=P5_GAME)
        # ⚠️ `nodes` 默认空 ⇒ 不给节点就是 403（fail-closed），看起来像端点坏了。
        self.client = make_logged_in_client(
            username="p5-kefu", password="pw-p5-kefu",
            groups=("客服",), nodes=(PAGE_NODE,),
        )
        self.hall_client = make_logged_in_client(
            username="p5-hall-viewer", password="pw-p5-hall",
            groups=("代练员",), nodes=("grab_hall.take_order",),
        )
        self.user = User.objects.get(username="p5-kefu")

    def _binding(self, *, base_url="https://peer-p5-page-up.test.invalid", **over):
        data = {
            "peer_name": "测试上家P5页面",
            "peer_base_url": base_url,
            "peer_account": "p5-page-account",
            "password": "p5-page-password",
            "token_in_hash": hash_token(new_token()),
            "token_out": "TOKEN-OUT-P5",
            "local_base_url": "https://peer-p5-page-local.test.invalid",
            "bound_by": self.user,
        }
        data.update(over)
        return PeerUpstreamBinding.objects.create(**data)

    def _downstream(self, *, username="p5-page-downstream",
                    base_url="https://peer-p5-page-down.test.invalid"):
        """本租户的一条**下家**绑定（`account` 是 OneToOne ⇒ 每次一个新账号）。"""
        account = User.objects.create_user(
            username=username, password="pw-p5-page-down"
        )
        return PeerDownstreamBinding.objects.create(
            account=account,
            peer_base_url=base_url,
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

    def _mirror(self, binding, card_no, *, status=PeerHallMirror.Status.LISTED, **over):
        now = timezone.now()
        data = {
            "peer_upstream_binding": binding,
            "peer_base_url": binding.peer_base_url,
            "peer_order_no": card_no,
            "status": status,
            "game": self.game,
            "payload": hall_payload(card_no),
            "remote_hall_price": Decimal("50.00"),
            "first_seen_at": now,
            "last_seen_at": now,
        }
        data.update(over)
        return PeerHallMirror.objects.create(**data)

    def _put(self, pk, payload):
        return self.client.put(
            f"{PAGE_URL}{pk}/", json.dumps(payload),
            content_type="application/json",
        )

    def _delete(self, pk):
        return self.client.delete(f"{PAGE_URL}{pk}/")

    def _hall_card_nos(self):
        """本租户 `/api/business/hall/` 里团团通镜像行的展示号集合。"""
        resp = self.hall_client.get("/api/business/hall/")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertEqual(body["code"], 0, body)
        return {
            row["remote_order_view"]
            for row in body["data"]["items"]
            if row.get("origin") == "peer"
        }


class UnlistOnSwitchOffTests(UnlistFixture):
    """② 关开关（PUT True → False）同一事务下架 `listed` 卡；`claiming` 不动。"""

    def test_turning_the_switch_off_unlists_listed_cards_and_leaves_claiming_alone(self):
        binding = self._binding(hall_sync_enabled=True)
        first = self._mirror(binding, "P5-OFF-1")
        second = self._mirror(binding, "P5-OFF-2")
        claiming_at = timezone.now()
        claiming = self._mirror(
            binding, "P5-OFF-3", status=PeerHallMirror.Status.CLAIMING,
            claiming_by=self.user, claiming_at=claiming_at,
        )
        with self.assertLogs("apps.business.api.peer_views", level="INFO") as logs:
            resp = self._put(binding.pk, {"hall_sync_enabled": False})
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.json())
        # ⭐ 下架行数进日志（**两行**，不含 claiming 那一行）。
        self.assertTrue(
            any("count=2" in line for line in logs.output), logs.output
        )
        for row in (first, second):
            row.refresh_from_db()
            self.assertEqual(row.status, PeerHallMirror.Status.GONE)
            self.assertIsNotNone(row.gone_at)
            self.assertEqual(row.last_error, UNLISTED_ON_SWITCH_OFF_TEXT)
        claiming.refresh_from_db()
        self.assertEqual(claiming.status, PeerHallMirror.Status.CLAIMING)
        self.assertEqual(claiming.claiming_by_id, self.user.pk)
        self.assertEqual(claiming.claiming_at, claiming_at)
        self.assertIsNone(claiming.gone_at)

    def test_the_cards_disappear_from_the_hall_and_do_not_come_back_when_switched_on(self):
        """读口不再出现；再打开开关**不会凭空复活**（只有上游重新抛才会）。"""
        binding = self._binding(hall_sync_enabled=True)
        card = self._mirror(binding, "P5-OFF-READ-1")
        self.assertIn("P5-OFF-READ-1", self._hall_card_nos())   # 正对照：关之前看得见

        self.assertEqual(
            self._put(binding.pk, {"hall_sync_enabled": False}).json()["code"], 0
        )
        self.assertEqual(self._hall_card_nos(), set())

        # 再打开开关：行仍是 `gone` ⇒ 读口照旧空（⛔ 不是「一开关就复活」）。
        self.assertEqual(
            self._put(binding.pk, {"hall_sync_enabled": True}).json()["code"], 0
        )
        self.assertEqual(self._hall_card_nos(), set())

        # 上游重新抛一次 ⇒ 复活（D-0300 口径：gone 重新见到即复活）。
        # ⭐ A3 / M-3：复活闸吃事件时刻 ⇒ 这条「重新抛」必须带**晚于下架时刻**的
        # 新时刻（夹具原固定串此刻比 `gone_at = now` 旧，会被迟到闸正确地拦下）。
        binding.refresh_from_db()      # 请求里改过开关列，这份内存对象要跟上
        self.assertIs(binding.hall_sync_enabled, True)
        fresh = hall_event("P5-OFF-READ-1")
        fresh["occurred_at"] = (timezone.now() + timedelta(minutes=1)).isoformat()
        outcome = apply_events([fresh], binding=binding)
        self.assertEqual(outcome.applied, 1, outcome)
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(card.gone_at)
        self.assertIn("P5-OFF-READ-1", self._hall_card_nos())

    def test_changing_the_percent_while_the_switch_is_off_does_not_unlist(self):
        """反例：开关本来就关着，只改百分比 ⇒ 触发器（True → False）没发生，卡不动。"""
        binding = self._binding(hall_sync_enabled=False)
        card = self._mirror(binding, "P5-OFF-COUNTER-1")
        resp = self._put(binding.pk, {"hall_price_percent": 70})
        self.assertEqual(resp.json()["code"], 0, resp.json())
        binding.refresh_from_db()
        self.assertEqual(binding.hall_price_percent, 70)
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(card.gone_at)
        self.assertEqual(card.last_error, "")

    def test_putting_the_switch_on_again_while_it_is_on_does_not_unlist(self):
        """反例：`True → True`（只改百分比）⇒ 不触发下架。"""
        binding = self._binding(hall_sync_enabled=True)
        card = self._mirror(binding, "P5-OFF-COUNTER-2")
        resp = self._put(
            binding.pk, {"hall_sync_enabled": True, "hall_price_percent": 90}
        )
        self.assertEqual(resp.json()["code"], 0, resp.json())
        card.refresh_from_db()
        self.assertEqual(card.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(card.gone_at)

    def test_the_unlist_shares_the_switch_write_transaction(self):
        """⭐「同一事务」的判据：下架那一步抛错 ⇒ 开关那一列**也回滚**（不许半新半旧）。

        ⚠️ 补丁打在**定义处** `apps.business.peer_hall.unlist_binding_mirrors`：视图用的是
        `from apps.business import peer_hall` 拿到的**模块对象**，调用时按属性查找
        （E-0029：打在「被查找的那个模块」上）。
        """
        binding = self._binding(hall_sync_enabled=True)
        with mock.patch(
            "apps.business.peer_hall.unlist_binding_mirrors",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                self._put(binding.pk, {"hall_sync_enabled": False})
        binding.refresh_from_db()
        self.assertIs(binding.hall_sync_enabled, True)


class UnlistOnUnbindTests(UnlistFixture):
    """③ 解绑（DELETE）同一事务下架 `listed` 卡；绑定行软删仍在、卡行仍在。"""

    def test_delete_unlists_listed_cards_and_returns_claims_to_the_hall(self):
        """③ 解绑：`listed → gone`；⭐ Z5`claiming → listed`（改准，见 docstring）。

        ⚠️ **改准来由**：P5 原来钉「解绑时 `claiming` 不动」，
        但绑定一停，`claiming` 的两条出路（转正 / `hall.claim_refused`）都失去对端回音
        ⇒ 那张卡永久既接不了也看不见。现在解绑时把 `claiming` 退回 `listed`
        （⛔ **不是** `gone`：抢单没确认时上家那张单可能还在厅）。
        """
        binding = self._binding()
        listed = self._mirror(binding, "P5-UNBIND-1")
        claiming_at = timezone.now()
        claiming = self._mirror(
            binding, "P5-UNBIND-2", status=PeerHallMirror.Status.CLAIMING,
            claiming_by=self.user, claiming_at=claiming_at,
        )
        with mock.patch("apps.business.api.peer_views.unbind_upstream"):
            with self.assertLogs(
                "apps.business.api.peer_views", level="INFO"
            ) as logs:
                resp = self._delete(binding.pk)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.json()["code"], 0, resp.json())
        # 下架行数仍只数 `listed` 那一张（退出顺序：先下架、再退回）。
        self.assertTrue(
            any("count=1" in line for line in logs.output), logs.output
        )
        # ① 卡：listed → gone。
        listed.refresh_from_db()
        self.assertEqual(listed.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(listed.gone_at)
        self.assertEqual(listed.last_error, UNLISTED_ON_UNBIND_TEXT)
        # ② ⭐ Z5：claiming → listed（四个接单列清空 + 人话）。
        claiming.refresh_from_db()
        self.assertEqual(claiming.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(claiming.claiming_by_id)
        self.assertIsNone(claiming.claiming_at)
        self.assertIsNone(claiming.gone_at)
        self.assertEqual(claiming.last_error, "绑定停用，抢单未确认已退回大厅")
        # ③ 一条退回台账（人话 + 被退回的卡号）。
        ledger = PeerSyncEvent.objects.get(kind="hall.claim_release")
        self.assertEqual(ledger.reason, "绑定停用，抢单未确认已退回大厅")
        self.assertEqual(ledger.payload["released"], ["P5-UNBIND-2"])
        # ④ 两边**行都还在**：绑定软删（不是真删）、卡行一张不少（§11-3 留档）。
        binding.refresh_from_db()
        self.assertIsNotNone(binding.deleted_at)
        self.assertEqual(PeerUpstreamBinding.objects.count(), 1)
        self.assertEqual(PeerHallMirror.objects.count(), 2)
        # ⑤ 读口不再出现（绑定已解绑 ⇒ 可见与可接同一道判据）。
        self.assertEqual(self._hall_card_nos(), set())

    def test_delete_only_unlists_the_cards_of_that_binding(self):
        """反例（作用域）：解绑 A ⇒ 只下架 A 的卡；B 的卡一张不动。"""
        binding_a = self._binding(base_url="https://peer-p5-a.test.invalid")
        binding_b = self._binding(base_url="https://peer-p5-b.test.invalid")
        card_a = self._mirror(binding_a, "P5-UNBIND-A")
        card_b = self._mirror(binding_b, "P5-UNBIND-B")
        with mock.patch("apps.business.api.peer_views.unbind_upstream"):
            self.assertEqual(self._delete(binding_a.pk).json()["code"], 0)
        card_a.refresh_from_db()
        card_b.refresh_from_db()
        self.assertEqual(card_a.status, PeerHallMirror.Status.GONE)
        self.assertIsNotNone(card_a.gone_at)
        self.assertEqual(card_b.status, PeerHallMirror.Status.LISTED)
        self.assertIsNone(card_b.gone_at)
        self.assertEqual(card_b.last_error, "")

    def test_the_unlist_shares_the_soft_delete_transaction(self):
        """⭐「同一事务」的判据：下架抛错 ⇒ `deleted_at` **也回滚**（绑定仍算在绑）。"""
        binding = self._binding()
        with mock.patch(
            "apps.business.peer_hall.unlist_binding_mirrors",
            side_effect=RuntimeError("boom"),
        ):
            with mock.patch("apps.business.api.peer_views.unbind_upstream"):
                with self.assertRaises(RuntimeError):
                    self._delete(binding.pk)
        binding.refresh_from_db()
        self.assertIsNone(binding.deleted_at)


class DownstreamWithdrawTests(UnlistFixture):
    """⭐ B3（终审修复轮）：关开关 / 解绑下架时，**下家手里那张 `listed` 卡也要消失**。

    多跳链（A→B→C）上，C 的卡是 B 转发过去的；B 只把**本地**行标 `gone` 的话，C 那张卡
    既不在 B 的读面上、也不会再收到任何事件 ⇒ 永远接不了、也回不来的僵尸卡。
    ⇒ 每张真的翻成 `gone` 的卡都照 `hall.take` 的形状给下家绑定补发一条。
    """

    def test_switching_the_switch_off_takes_the_listed_cards_from_our_downstreams(self):
        binding = self._binding(hall_sync_enabled=True)
        self._mirror(binding, "P5-B3-TAKE-1")
        downstream = self._downstream()
        self.assertEqual(
            self._put(binding.pk, {"hall_sync_enabled": False}).json()["code"], 0
        )
        take = PeerSyncEvent.objects.filter(
            binding_role="downstream",
            binding_id=str(downstream.pk),
            kind=HALL_TAKE_KIND,
            target_ref="P5-B3-TAKE-1",
        ).first()
        self.assertIsNotNone(
            take, "下架必须给下家补一条 hall.take（否则多跳下游留僵尸卡）"
        )
        self.assertEqual(take.direction, "out")
        self.assertEqual(take.status, PeerSyncEvent.Status.PENDING)
        # 形状照 `hall.take` 现有的转发形状：卡号 = 源头单号（沿链不换号）。
        self.assertEqual(take.payload["order_no"], "P5-B3-TAKE-1")

    def test_claiming_cards_are_not_taken_from_downstreams(self):
        """反例（作用域）：`claiming` 的卡不在这次下架里 ⇒ 一条 take 都不发。"""
        binding = self._binding(hall_sync_enabled=True)
        self._mirror(
            binding, "P5-B3-TAKE-2",
            status=PeerHallMirror.Status.CLAIMING,
            claiming_by=self.user, claiming_at=timezone.now(),
        )
        downstream = self._downstream()
        self.assertEqual(
            self._put(binding.pk, {"hall_sync_enabled": False}).json()["code"], 0
        )
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                binding_role="downstream",
                binding_id=str(downstream.pk),
                kind=HALL_TAKE_KIND,
                target_ref="P5-B3-TAKE-2",
            ).exists()
        )

    def test_unbinding_takes_the_listed_cards_from_our_downstreams(self):
        binding = self._binding()
        self._mirror(binding, "P5-B3-TAKE-3")
        downstream = self._downstream(username="p5-page-downstream-unbind")
        with mock.patch("apps.business.api.peer_views.unbind_upstream"):
            self.assertEqual(self._delete(binding.pk).json()["code"], 0)
        self.assertTrue(
            PeerSyncEvent.objects.filter(
                binding_role="downstream",
                binding_id=str(downstream.pk),
                kind=HALL_TAKE_KIND,
                target_ref="P5-B3-TAKE-3",
            ).exists()
        )


class ClaimRefusedGoneTakeTests(UnlistFixture):
    """⭐ B3 的第二处：`claiming` 带着 take 证据收口成 `gone` 时，其它下家也要收 take。"""

    def test_the_other_downstreams_get_a_take_while_the_claimer_gets_the_refusal(self):
        binding = self._binding(hall_sync_enabled=True)
        initiator = self._downstream(username="p5-b3-claimer")
        other = self._downstream(username="p5-b3-other")
        row = self._mirror(
            binding, "P5-B3-CLAIM-1",
            status=PeerHallMirror.Status.CLAIMING,
            # `gone_at` = take 在 claiming 期间来过（M2 的证据列）⇒ refused 收口成 gone。
            gone_at=timezone.now(),
            claim_downstream=initiator,
            claim_id="claim-b3",
        )
        outcome = apply_events(
            [hall_event("P5-B3-CLAIM-1", kind=HALL_CLAIM_REFUSED_KIND)],
            binding=binding,
        )
        self.assertEqual(outcome.applied, 1, outcome)
        row.refresh_from_db()
        self.assertEqual(row.status, PeerHallMirror.Status.GONE)
        # 其它下家：那张 `listed` 卡必须消失。
        self.assertTrue(
            PeerSyncEvent.objects.filter(
                binding_role="downstream", binding_id=str(other.pk),
                kind=HALL_TAKE_KIND, target_ref="P5-B3-CLAIM-1",
            ).exists(),
            "多跳下游的 listed 卡没被撤（B3 第二处漏了）",
        )
        # 本次发起方：只收 `hall.claim_refused`（它的收口是那一条），⛔ 不收 take。
        self.assertTrue(
            PeerSyncEvent.objects.filter(
                binding_role="downstream", binding_id=str(initiator.pk),
                kind=HALL_CLAIM_REFUSED_KIND, target_ref="P5-B3-CLAIM-1",
            ).exists()
        )
        self.assertFalse(
            PeerSyncEvent.objects.filter(
                binding_role="downstream", binding_id=str(initiator.pk),
                kind=HALL_TAKE_KIND, target_ref="P5-B3-CLAIM-1",
            ).exists()
        )
        # 收口时四个接单列一起清（与既有 M2 口径一致）。
        self.assertIsNone(row.claiming_by_id)
        self.assertIsNone(row.claiming_at)
        self.assertIsNone(row.claim_downstream_id)
        self.assertEqual(row.claim_id, "")
