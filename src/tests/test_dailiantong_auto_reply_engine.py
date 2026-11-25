"""按规则自动回复（T14，2026-10-27）：触发判据 / 优先级 / 幂等 / 部分失败 / 审计。

## 覆盖什么

| 组 | 判据 |
|---|---|
| 触发 | `Status=13` ⇒「申请验收后」；`Status=12`（接单后）**不在本引擎**（归 `dailiantong_qr`）；其它状态零调用 |
| 优先级 | 具体游戏规则优先于「全部游戏」；停用规则不算；别的游戏的规则不算 |
| 幂等 | 同一 `(push, rule)` 只发一次（库层唯一约束 + 第二轮 `already_sent`） |
| 图片 | 对象名形状 `tt/<单号>/ar-<规则 id>-<毫秒>.<ext>`；上传后**回读进度**自证 |
| 部分失败 | 图成功 / 文字失败 ⇒ 下一轮**不重复传图**，只补文字；三种失败都不写台账 |
| 审计 | 成功一条；失败原因变化才再写一条（同一原因不刷屏）；凭据零命中 |
| 触发点 | `dailiantong_sync --once` 在回读后调一次；先群码后自动回复；两者互不阻塞 |

## 不访问真实网络

代练通 API：服务层客户端构造点 `apps.business.dailiantong_auto_reply._open_client`
（它委托抛单模块的同一个构造点）换成假客户端；OSS 上传换掉
`apps.business.dailiantong_auto_reply.put_object`（⛔ 不重新实现签名）。
唯一一条「真 `put_object`」的用例用 `httpx.MockTransport` 收请求。

## 夹具纪律

凭据 / 单号 / STS token / 对象名全是合成值；`MEDIA_ROOT` 指到临时目录。
"""

from __future__ import annotations

import io
import os
import pathlib
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test import override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.audit.models import OperationLog
from apps.business import dailiantong_auto_reply as engine
from apps.business.dailiantong_client import (
    DailiantongError,
    ListPage,
    OssStsCredentials,
    OssUploadError,
    put_object,
)
from apps.business.models import (
    DailiantongAutoReplySent,
    DailiantongPush,
    Game,
    GameServer,
    Order,
    OrderStatus,
    PlatformAutoReplyRule,
    PlatformIntegration,
    PlatformPushSettings,
)
from apps.core.storage import get_storage

#: 合成凭据（⛔ 不是任何真实密钥）。
APP_ID = "synth-dlt-ar-app-0001"
APP_SECRET = "synth-dlt-ar-secret-7b21"
PAY_PASSWORD_MD5 = "0123456789abcdef0123456789abcdef"

#: 合成上游单号 / 本地单号。
SERIAL = "19000000000000000091"
ORDER_NO = "20261027000000000091"

#: 合成 STS 三件套（⛔ 不是任何真实凭证）。
STS_TOKEN = "synth-sts-token-ar-0001"
STS_AK_ID = "STS.synthArAkId0001"
STS_AK_SECRET = "synthStsArAccessKeySecret0001"

#: 一张合成 PNG（前 8 字节是 PNG 魔数，服务层据此判图种）。
IMAGE_BYTES = b"\x89PNG\r\n\x1a\nsynth-auto-reply-image-0001"

#: 埋点动作码（与 `apps/audit/actions.py` 同值；写死字面量，⛔ 不 import 常量偷懒）。
AUDIT_ACTION = "order.dailiantong_auto_reply"

#: 远端状态（`docs/DAILIANTONG-API.md` §5 的码表，写死字面量）。
REMOTE_IN_PROGRESS = 12
REMOTE_WAITING_REVIEW = 13
REMOTE_NOT_ACCEPTED = 11

#: 时机码（证据件 `system-platformautoreply.html:98-101`，写死字面量）。
TRIGGER_AFTER_ACCEPT = 0
TRIGGER_AFTER_REVIEW_REQUEST = 10

OSS_HOST = "dltfile01.oss-cn-hangzhou.aliyuncs.com"


def _sts() -> OssStsCredentials:
    return OssStsCredentials(
        security_token=STS_TOKEN,
        access_key_id=STS_AK_ID,
        access_key_secret=STS_AK_SECRET,
    )


class _TempMediaRootMixin:
    """把 `MEDIA_ROOT` 指到临时目录（同其余存储线用例的 mixin）。"""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)


class _FakeAutoReplyClient:
    """假代练通客户端：实现本线用到的五跳，记录调用顺序。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.sts_error: Exception | None = None
        self.message_error: Exception | None = None
        self.progress_error: Exception | None = None
        self.progress_list_error: Exception | None = None
        #: 进度列表（`progress_add` 成功时按「上游会存成 URL」的形态追加一行）。
        self.progress_rows: list[dict] = []
        #: 留言列表（`message_add` 成功时追加；发送前回读靠它判断「话术已发」）。
        self.message_rows: list[dict] = []
        #: 回读里 `Img` 的形态；改成不相干的值即可模拟「假成功 / 回读没出现」。
        self.progress_img_template = "http://{host}/{img}"
        #: `order_detail` 回给 `refresh_push_status()` 的远端状态。
        self.detail_status = REMOTE_IN_PROGRESS
        self.detail_cancel_status = 10
        self.closed = False

    def _record(self, action: str, **fields) -> None:
        self.calls.append((action, fields))

    @property
    def actions(self) -> list[str]:
        return [action for action, _fields in self.calls]

    def get_sts(self):
        self._record("GetSTS")
        if self.sts_error is not None:
            raise self.sts_error
        return _sts()

    def message_add(self, serial_no, msg):
        self._record("message_add", serial_no=serial_no, msg=msg)
        if self.message_error is not None:
            raise self.message_error
        self.message_rows.append({"Msg": msg, "SerialNo": serial_no})
        return "发送成功."

    def message_list(self, serial_no, *, max_count=None):
        self._record("message_list", serial_no=serial_no, max_count=max_count)
        return ListPage(rows=tuple(self.message_rows))

    def progress_add(self, serial_no, msg, *, img=None):
        self._record("progress_add", serial_no=serial_no, msg=msg, img=img)
        if self.progress_error is not None:
            raise self.progress_error
        self.progress_rows.append(
            {
                "Msg": msg,
                "Img": self.progress_img_template.format(host=OSS_HOST, img=img or ""),
            }
        )
        return "添加完成."

    def progress_list(self, serial_no, *, max_count=None):
        self._record("progress_list", serial_no=serial_no, max_count=max_count)
        if self.progress_list_error is not None:
            raise self.progress_list_error
        return ListPage(rows=tuple(self.progress_rows))

    def order_detail(self, serial_no):
        self._record("order_detail", serial_no=serial_no)
        return {"Status": self.detail_status, "CancelStatus": self.detail_cancel_status}

    def close(self):
        self.closed = True


class _AutoReplyTestBase(_TempMediaRootMixin, TenantTestCase):
    """租户 A：一个启用且默认的代练通配置 + 一条已到 `Status=12` 的推送行。"""

    def setUp(self):
        connection.set_tenant(self.tenant)
        self.operator = get_user_model().objects.create_user(
            username="dlt-ar-operator", password="synth-pw-0001", first_name="回复员"
        )
        self.game = Game.objects.create(name="王者荣耀")
        self.other_game = Game.objects.create(name="和平精英")
        self.server = GameServer.objects.create(game=self.game, name="安卓QQ区")
        self.integration = PlatformIntegration.objects.create(
            platform=10,
            account="USR-SYNTH-AR-01",
            app_id=APP_ID,
            app_secret=APP_SECRET,
            pay_password_md5=PAY_PASSWORD_MD5,
            is_enabled=True,
            is_default=True,
            contact_phone="",
            contact_qq="",
        )
        self.order = Order.objects.create(
            game=self.game,
            server=self.server,
            order_no=ORDER_NO,
            status=OrderStatus.WAITING_GRAB,
            project_text="星耀上王者",
            total_price=Decimal("120.00"),
        )
        self.push = DailiantongPush.objects.create(
            order=self.order,
            platform=10,
            state=DailiantongPush.State.SENT,
            serial_no=SERIAL,
            price=Decimal("5.00"),
            ensure1=Decimal("0.00"),
            ensure2=Decimal("0.00"),
            time_limit_hours=24,
            ex_order_no=ORDER_NO,
            remote_status=REMOTE_WAITING_REVIEW,
            remote_cancel_status=10,
            attempts=1,
        )
        self.fake = _FakeAutoReplyClient()
        self.uploaded: list[dict] = []

    # ── 夹具 ────────────────────────────────────────────────────────
    def _rule(
        self,
        *,
        trigger: int = TRIGGER_AFTER_REVIEW_REQUEST,
        game=None,
        content: str = "合成话术：您好，已接单。",
        image_key: str = "",
        is_enabled: bool = True,
        sort_order: int = 0,
    ) -> PlatformAutoReplyRule:
        """建一条规则。`game=None` = 全部游戏（⚠️ 与「不传」不同义，这里显式写）。"""
        return PlatformAutoReplyRule.objects.create(
            platform=10,
            game=game,
            trigger=trigger,
            content=content,
            image_key=image_key,
            is_enabled=is_enabled,
            sort_order=sort_order,
        )

    def _rule_image(self, suffix: str = "a") -> str:
        key = f"{self.tenant.schema_name}/auto-reply/rule-{suffix}.png"
        get_storage().put_bytes(key, IMAGE_BYTES, content_type="image/png")
        return key

    def _set_remote_status(self, status: int) -> None:
        self.push.remote_status = status
        self.push.save(update_fields=["remote_status", "updated_at"])

    def _dispatch(self, push=None):
        """调引擎；客户端与 OSS 上传都换成假件（⛔ 真实网络）。"""

        def fake_put_object(sts, object_key, data, content_type, **kwargs):
            self.uploaded.append(
                {
                    "sts": sts,
                    "object_key": object_key,
                    "data": data,
                    "content_type": content_type,
                }
            )

        with mock.patch.object(
            engine, "_open_client", side_effect=lambda _integration: self.fake
        ), mock.patch.object(engine, "put_object", side_effect=fake_put_object):
            return engine.dispatch_for_push(push or self.push)

    def _reload(self) -> DailiantongPush:
        self.push.refresh_from_db()
        return self.push

    def _sent_rows(self):
        return list(DailiantongAutoReplySent.objects.order_by("id"))

    def _audit_rows(self):
        return list(OperationLog.objects.filter(action=AUDIT_ACTION).order_by("id"))


class TriggerTests(_AutoReplyTestBase):
    def test_status_12_is_not_handled_here(self):
        """⭐ B1：`Status=12`（接单后）由 `dailiantong_qr` 的合并入口负责，本引擎零调用。"""
        PlatformAutoReplyRule.objects.create(
            platform=10,
            game=self.game,
            trigger=TRIGGER_AFTER_ACCEPT,
            content="接单后话术",
        )
        self._set_remote_status(REMOTE_IN_PROGRESS)

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self._sent_rows(), [])
        self.assertEqual(self._audit_rows(), [])
        self.assertIn("申请验收后", self._reload().auto_reply_last_error)

    def test_status_13_uses_the_after_review_request_rule(self):
        rule = self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            content="申请验收后话术",
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        self.assertEqual(outcome.trigger, TRIGGER_AFTER_REVIEW_REQUEST)
        messages = [fields["msg"] for action, fields in self.fake.calls if action == "message_add"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("申请验收后话术"))
        self.assertIn("系统自动回复", messages[0])
        rows = self._sent_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].rule_id, rule.pk)
        self.assertEqual(rows[0].push_id, self.push.pk)
        self.assertIsNotNone(rows[0].sent_at)

    def test_any_other_remote_status_sends_nothing(self):
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        self._set_remote_status(REMOTE_NOT_ACCEPTED)

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self._sent_rows(), [])
        self.assertEqual(self._audit_rows(), [])

    def test_cancelling_order_sends_nothing_even_at_status_12(self):
        """`Status=12 + CancelStatus=11`（协商撤销中）⇒ 一条都不发、零上游调用。"""
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        self.push.remote_cancel_status = 11
        self.push.save(update_fields=["remote_cancel_status", "updated_at"])

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self._sent_rows(), [])
        self.assertEqual(self._audit_rows(), [])

    def test_cancel_status_thirteen_also_does_not_send(self):
        """裁决口径 =「不是空、也不是 10（未撤销）就不发」⇒ 13（撤销已取消）也不发。"""
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        self.push.remote_cancel_status = 13
        self.push.save(update_fields=["remote_cancel_status", "updated_at"])

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertEqual(self.fake.calls, [])

    def test_no_rule_at_all_sends_nothing(self):
        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self._sent_rows(), [])
        self.assertEqual(self._audit_rows(), [])


class RuleSelectionTests(_AutoReplyTestBase):
    def test_specific_game_rule_wins_over_all_games_rule(self):
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=None, content="全部游戏话术"
        )
        specific = self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game, content="本游戏话术"
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        messages = [fields["msg"] for action, fields in self.fake.calls if action == "message_add"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("本游戏话术"))
        self.assertIn("系统自动回复", messages[0])
        self.assertEqual(self._sent_rows()[0].rule_id, specific.pk)

    def test_specific_game_rule_beats_a_lower_sort_order_all_games_rule(self):
        """`sort_order` 不能把「具体游戏」挤掉：优先级判据是游戏，排序只在同优先级内用。

        ⚠️ B2 起同一组合只允许一条 ⇒ 「同优先级多条」在库层已不可能；这里改用
        「全部游戏那条 sort_order 更小」来证明**仍然**选具体游戏那条。
        """
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=None,
            content="全部游戏话术",
            sort_order=0,
        )
        specific = self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            content="本游戏话术",
            sort_order=99,
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        messages = [fields["msg"] for action, fields in self.fake.calls if action == "message_add"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("本游戏话术"))
        self.assertEqual(self._sent_rows()[0].rule_id, specific.pk)

    def test_all_games_rule_is_used_when_there_is_no_specific_rule(self):
        all_games = self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=None, content="全部游戏话术"
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        messages = [fields["msg"] for action, fields in self.fake.calls if action == "message_add"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("全部游戏话术"))
        self.assertIn("系统自动回复", messages[0])
        self.assertEqual(self._sent_rows()[0].rule_id, all_games.pk)

    def test_rule_for_another_game_does_not_match(self):
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.other_game, content="别的游戏话术"
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self._sent_rows(), [])

    def test_disabled_rule_is_not_sent(self):
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            is_enabled=False,
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self._sent_rows(), [])

    def test_rule_for_another_platform_does_not_match(self):
        rule = self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        rule.platform = 11
        rule.save(update_fields=["platform", "updated_at"])

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertEqual(self.fake.calls, [])


class IdempotencyTests(_AutoReplyTestBase):
    def test_second_round_does_not_send_again(self):
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)

        first = self._dispatch()
        second = self._dispatch()

        self.assertEqual(first.status, engine.STATUS_SENT)
        self.assertEqual(second.status, engine.STATUS_ALREADY_SENT)
        messages = [c for c in self.fake.calls if c[0] == "message_add"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(len(self._sent_rows()), 1)
        self.assertEqual(len(self._audit_rows()), 1)

    def test_database_rejects_the_same_push_and_rule_twice(self):
        rule = self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        DailiantongAutoReplySent.objects.create(push=self.push, rule=rule)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                DailiantongAutoReplySent.objects.create(push=self.push, rule=rule)

    def test_only_the_review_rule_is_sent_and_the_accept_rule_belongs_to_the_qr_entry(self):
        """一条推送同时配了两档规则：本引擎只发「申请验收后」那条（B1 合并入口）。"""
        accept_rule = self._rule(
            trigger=TRIGGER_AFTER_ACCEPT, game=None, content="接单后话术"
        )
        review_rule = self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=None,
            content="申请验收后话术",
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        self.assertEqual({row.rule_id for row in self._sent_rows()}, {review_rule.pk})
        self.assertNotIn(accept_rule.pk, {row.rule_id for row in self._sent_rows()})
        messages = [fields["msg"] for action, fields in self.fake.calls if action == "message_add"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("申请验收后话术"))
        self.assertIn("系统自动回复", messages[0])


class ClaimAndReadbackTests(_AutoReplyTestBase):
    """⭐ 审计修复（2026-10-27）：发送前占位 + 回读判已发（P1-2）。"""

    def _ledger(self, rule):
        return DailiantongAutoReplySent.objects.get(push=self.push, rule=rule)

    def test_message_already_in_readback_is_not_sent_again(self):
        """上一轮文字成功后、写台账前进程被杀 ⇒ 本轮回读命中，⛔ 不重发。"""
        rule = self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game, content="已发话术")
        self.fake.message_rows = [
            {
                "Msg": engine.build_message_text(
                    "已发话术", order_no=ORDER_NO, rule_id=rule.pk
                ),
                "SerialNo": SERIAL,
            }
        ]

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        self.assertNotIn("message_add", self.fake.actions)
        self.assertEqual(self._ledger(rule).state, DailiantongAutoReplySent.State.SENT)
        self.assertEqual(len(self.uploaded), 0)

    def test_hand_message_with_the_same_short_text_is_not_counted_as_ours(self):
        """⭐ I-A：规则配「好的」，打手历史留言也是「好的」⇒ 仍必须发（靠系统标记区分）。"""
        rule = self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game, content="好的")
        self.fake.message_rows = [{"Msg": "好的", "SerialNo": SERIAL}]

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        messages = [fields["msg"] for action, fields in self.fake.calls if action == "message_add"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("好的"))
        self.assertIn(f"订单 {ORDER_NO}", messages[0])
        self.assertIn(f"规则 {rule.pk}", messages[0])
        self.assertLessEqual(len(messages[0]), engine.AUTO_REPLY_MESSAGE_MAX_LENGTH)

    def test_outgoing_text_has_the_system_marker_and_the_order_no(self):
        rule = self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game, content="话术")

        self._dispatch()

        message = [fields["msg"] for action, fields in self.fake.calls if action == "message_add"][0]
        self.assertTrue(message.startswith("话术"))
        self.assertEqual(
            message,
            f"话术（系统自动回复·订单 {ORDER_NO}·规则 {rule.pk}）",
        )

    def test_success_after_a_skip_clears_the_skip_reason(self):
        """⭐ Minor：先「无可用规则」写下的跳过原因，成功一次后必须清掉。"""
        first = self._dispatch()
        self.assertEqual(first.status, engine.STATUS_SKIPPED)
        self.assertIn("没有可用的自动回复规则", self._reload().auto_reply_last_error)

        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        second = self._dispatch()

        self.assertEqual(second.status, engine.STATUS_SENT)
        self.assertEqual(self._reload().auto_reply_last_error, "")

    def test_fresh_pending_claim_blocks_a_second_process(self):
        rule = self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        DailiantongAutoReplySent.objects.create(
            push=self.push,
            rule=rule,
            state=DailiantongAutoReplySent.State.PENDING,
            claim_token="other-process-token",
            sent_at=timezone.now(),
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertNotIn("message_add", self.fake.actions)
        self.assertEqual(
            self._ledger(rule).state, DailiantongAutoReplySent.State.PENDING
        )

    def test_stale_pending_claim_is_reclaimed_after_a_crash(self):
        rule = self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        DailiantongAutoReplySent.objects.create(
            push=self.push,
            rule=rule,
            state=DailiantongAutoReplySent.State.PENDING,
            claim_token="dead-process-token",
            sent_at=timezone.now()
            - timedelta(seconds=engine.AUTO_REPLY_CLAIM_STALE_SECONDS + 1),
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        self.assertIn("message_add", self.fake.actions)
        self.assertEqual(
            self._ledger(rule).state, DailiantongAutoReplySent.State.SENT
        )

    def test_ledger_placeholder_exists_before_the_upstream_send(self):
        rule = self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        seen = {}
        original = self.fake.message_add

        def observing(serial_no, msg):
            row = DailiantongAutoReplySent.objects.get(push=self.push, rule=rule)
            seen["state"] = row.state
            seen["token"] = row.claim_token
            return original(serial_no, msg)

        self.fake.message_add = observing
        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        self.assertEqual(seen["state"], DailiantongAutoReplySent.State.PENDING)
        self.assertTrue(seen["token"])

    def test_no_rule_skip_reason_is_visible_in_the_error_column(self):
        """⭐ P3-16：「没有可用的自动回复规则」不再只写 debug。"""
        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SKIPPED)
        self.assertIn("没有可用的自动回复规则", self._reload().auto_reply_last_error)


class ImageTests(_AutoReplyTestBase):
    def test_image_is_uploaded_with_the_expected_object_key_and_verified(self):
        image_key = self._rule_image()
        rule = self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            content="带图话术",
            image_key=image_key,
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_SENT)
        self.assertTrue(outcome.with_image)
        self.assertEqual(len(self.uploaded), 1)
        upload = self.uploaded[0]
        self.assertEqual(upload["sts"], _sts())
        self.assertEqual(upload["data"], IMAGE_BYTES)
        self.assertEqual(upload["content_type"], "image/png")
        self.assertRegex(
            upload["object_key"],
            rf"^tt/{self.tenant.schema_name}/{ORDER_NO}/ar-{rule.pk}-\d{{13}}\.png$",
        )
        # 上传 → 发进度 → 回读 → 文字（⛔ 不是先发文字再传图）。
        self.assertEqual(
            self.fake.actions,
            [
                "progress_list",
                "message_list",
                "GetSTS",
                "progress_add",
                "progress_list",
                "message_add",
            ],
        )
        progress = self.fake.calls[3][1]
        self.assertEqual(progress["img"], upload["object_key"])
        self.assertEqual(progress["serial_no"], SERIAL)
        self.assertIsNotNone(self._sent_rows()[0].sent_at)

    def test_missing_image_in_storage_fails_without_upstream_calls(self):
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            image_key=f"{self.tenant.schema_name}/auto-reply/missing.png",
        )

        outcome = self._dispatch()

        self.assertEqual(outcome.status, engine.STATUS_FAILED)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.uploaded, [])
        self.assertEqual(self._sent_rows(), [])
        self.assertIn("读取自动回复图片失败", self._reload().auto_reply_last_error)

    def test_text_success_after_image_failure_does_not_duplicate_the_image(self):
        """部分成功语义：图成功、文字失败 ⇒ 下一轮只补文字（⛔ 不再传图）。"""
        image_key = self._rule_image()
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            content="部分成功话术",
            image_key=image_key,
        )
        self.fake.message_error = DailiantongError(
            code="-1", message="订单状态异常", action="LevelOrderMessageAdd"
        )

        first = self._dispatch()

        self.assertEqual(first.status, engine.STATUS_FAILED)
        self.assertEqual(self._sent_rows(), [])
        self.assertEqual(len(self.uploaded), 1)
        progress_adds = [c for c in self.fake.calls if c[0] == "progress_add"]
        self.assertEqual(len(progress_adds), 1)

        # 下一轮：话术恢复 ⇒ 先回读发现图已在进度里，只补文字。
        self.fake.message_error = None
        calls_before = len(self.fake.calls)
        second = self._dispatch()

        self.assertEqual(second.status, engine.STATUS_SENT)
        self.assertEqual(len(self.uploaded), 1)  # ⛔ 没有第二张图
        progress_adds = [c for c in self.fake.calls if c[0] == "progress_add"]
        self.assertEqual(len(progress_adds), 1)
        new_actions = [c[0] for c in self.fake.calls[calls_before:]]
        self.assertEqual(
            new_actions, ["progress_list", "message_list", "message_add"]
        )
        self.assertIn("图片此前已发", self._audit_rows()[-1].summary)

    def test_image_upload_failure_blocks_text_and_retries(self):
        image_key = self._rule_image()
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            content="带图话术",
            image_key=image_key,
        )

        def failing_put_object(*_args, **_kwargs):
            raise OssUploadError(
                "上传到代练通 OSS 失败（HTTP 403，Code=AccessDenied）"
            )

        with mock.patch.object(
            engine, "_open_client", side_effect=lambda _integration: self.fake
        ), mock.patch.object(engine, "put_object", side_effect=failing_put_object):
            first = engine.dispatch_for_push(self.push)

        self.assertEqual(first.status, engine.STATUS_FAILED)
        self.assertIn("403", first.reason)
        self.assertEqual(self._sent_rows(), [])
        # 上传没成 ⇒ 文字一次都不许发（⛔ 刷屏）。
        self.assertNotIn("message_add", self.fake.actions)
        self.assertIn("403", self._reload().auto_reply_last_error)

        # 下一轮上传恢复 ⇒ 整段重试并成功（此时没有重复的可见内容：上一轮什么都没发出去）。
        second = self._dispatch()

        self.assertEqual(second.status, engine.STATUS_SENT)
        self.assertEqual(len(self._sent_rows()), 1)
        self.assertEqual(len(self.uploaded), 1)
        self.assertIn("message_add", self.fake.actions)

    def test_progress_readback_missing_is_a_failure_and_retries(self):
        image_key = self._rule_image()
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            content="带图话术",
            image_key=image_key,
        )
        # 上游「假成功」：进度加了，但回读里 Img 不是我们这次的对象名。
        self.fake.progress_img_template = "http://example.invalid/unrelated.png"

        first = self._dispatch()

        self.assertEqual(first.status, engine.STATUS_FAILED)
        self.assertIn("没看到", first.reason)
        self.assertEqual(self._sent_rows(), [])
        self.assertNotIn("message_add", self.fake.actions)
        self.assertIn("没看到", self._reload().auto_reply_last_error)

        # 下一轮回读正常 ⇒ 重新传图并核验通过。
        self.fake.progress_img_template = "http://{host}/{img}"
        second = self._dispatch()

        self.assertEqual(second.status, engine.STATUS_SENT)
        self.assertEqual(len(self._sent_rows()), 1)
        self.assertEqual(len(self.uploaded), 2)

    def test_progress_readback_call_failure_is_a_failure_and_retries(self):
        image_key = self._rule_image()
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            image_key=image_key,
        )
        self.fake.progress_list_error = DailiantongError(
            code="-1", message="订单异常", action="LevelOrderProgressList"
        )

        first = self._dispatch()

        self.assertEqual(first.status, engine.STATUS_FAILED)
        self.assertEqual(self._sent_rows(), [])
        self.assertNotIn("message_add", self.fake.actions)

        self.fake.progress_list_error = None
        second = self._dispatch()

        self.assertEqual(second.status, engine.STATUS_SENT)

    def test_progress_readback_asks_for_a_full_page(self):
        image_key = self._rule_image()
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            image_key=image_key,
        )

        self._dispatch()

        reads = [fields for action, fields in self.fake.calls if action == "progress_list"]
        self.assertEqual(len(reads), 2)
        # 钉字面值（100），⛔ 不钉模块常量。
        for fields in reads:
            self.assertEqual(fields["max_count"], 100)


class TextFailureTests(_AutoReplyTestBase):
    def test_text_failure_records_a_readable_reason_and_retries_next_round(self):
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game, content="话术"
        )
        self.fake.message_error = DailiantongError(
            code="-1", message="订单状态异常", action="LevelOrderMessageAdd"
        )

        first = self._dispatch()

        self.assertEqual(first.status, engine.STATUS_FAILED)
        self.assertEqual(self._sent_rows(), [])
        push = self._reload()
        self.assertIn("订单状态异常", push.last_error)
        self.assertIn("订单状态异常", push.auto_reply_last_error)
        self.assertIn("申请验收后", push.auto_reply_last_error)

        self.fake.message_error = None
        second = self._dispatch()

        self.assertEqual(second.status, engine.STATUS_SENT)
        self.assertEqual(len(self._sent_rows()), 1)
        self.assertEqual(
            len([c for c in self.fake.calls if c[0] == "message_add"]), 2
        )
        push = self._reload()
        self.assertEqual(push.auto_reply_last_error, "")
        self.assertEqual(push.last_error, "")


class AuditTests(_AutoReplyTestBase):
    def test_success_writes_exactly_one_audit_row(self):
        rule = self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game, content="话术"
        )

        self._dispatch()

        rows = self._audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].log_type, 10)
        self.assertEqual(rows[0].order_no, ORDER_NO)
        self.assertEqual(rows[0].target_kind, engine.AUDIT_TARGET_KIND)
        self.assertEqual(rows[0].target_id, str(self.push.pk))
        self.assertIn("申请验收后自动回复已发送", rows[0].summary)
        # ⛔ 规则正文不进审计（用户自由文本）。
        self.assertNotIn(rule.content, rows[0].summary)

    def test_failure_reason_change_writes_a_second_audit_only_when_it_changes(self):
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        self.fake.message_error = DailiantongError(
            code="-1", message="订单状态异常", action="LevelOrderMessageAdd"
        )

        self._dispatch()  # 第一次失败 ⇒ 审计 1 条
        self._dispatch()  # 同一原因 ⇒ 不刷屏
        self.assertEqual(len(self._audit_rows()), 1)

        self.fake.message_error = DailiantongError(
            code="-1", message="系统繁忙", action="LevelOrderMessageAdd"
        )
        self._dispatch()  # 原因变了 ⇒ 第二条
        rows = self._audit_rows()
        self.assertEqual(len(rows), 2)
        self.assertIn("系统繁忙", rows[1].summary)

    def test_success_is_audited_after_a_previous_failure(self):
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        self.fake.message_error = DailiantongError(
            code="-1", message="订单状态异常", action="LevelOrderMessageAdd"
        )
        self._dispatch()
        self.fake.message_error = None

        self._dispatch()

        rows = self._audit_rows()
        self.assertEqual(len(rows), 2)
        self.assertIn("已发送", rows[-1].summary)
        self.assertNotIn("失败", rows[-1].summary)

    def test_credentials_never_leak_into_audit_logs_or_error_columns(self):
        """端到端：真 `put_object` 遇 403，上游错误 XML 里回显 STS 三件套。"""
        image_key = self._rule_image()
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            image_key=image_key,
        )
        leaky_body = (
            "<Error><Code>AccessDenied</Code>"
            f"<Message>{STS_TOKEN} {STS_AK_ID} {STS_AK_SECRET}</Message></Error>"
        )

        def leaky_oss_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text=leaky_body)

        def real_put_with_fake_transport(sts, object_key, data, content_type, **kwargs):
            return put_object(
                sts,
                object_key,
                data,
                content_type,
                transport=httpx.MockTransport(leaky_oss_handler),
            )

        with self.assertLogs("apps.business.dailiantong_auto_reply", level="DEBUG") as captured, \
                mock.patch.object(
                    engine, "_open_client", side_effect=lambda _integration: self.fake
                ), mock.patch.object(
                    engine, "put_object", side_effect=real_put_with_fake_transport
                ):
            outcome = engine.dispatch_for_push(self.push)

        self.assertEqual(outcome.status, engine.STATUS_FAILED)
        push = self._reload()
        self.assertEqual(self._sent_rows(), [])

        audit_text = " ".join(
            "".join(
                str(getattr(row, column) or "")
                for column in (
                    "summary",
                    "actor",
                    "actor_name",
                    "target_name",
                    "order_no",
                    "from_value",
                    "to_value",
                    "reason",
                )
            )
            for row in self._audit_rows()
        )
        log_text = "\n".join(record.getMessage() for record in captured.records)
        haystack = "\n".join(
            [
                audit_text,
                log_text,
                push.last_error,
                push.auto_reply_last_error,
                outcome.reason,
            ]
        )
        for secret in (
            STS_TOKEN,
            STS_AK_ID,
            STS_AK_SECRET,
            APP_SECRET,
            PAY_PASSWORD_MD5,
        ):
            self.assertNotIn(secret, haystack)


# ── 触发点（`dailiantong_sync --once`）──────────────────────────────────


class _SyncTriggerBase(_AutoReplyTestBase):
    """回调根指到临时目录；上游详情由假客户端回。"""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp(prefix="dlt-ar-sync-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        env_patcher = mock.patch.dict(
            os.environ, {"DAILIANTONG_CALLBACK_ROOT": self.tmpdir}, clear=False
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def _enable_qr_switch(self) -> None:
        """打开「平台接手后自动发群二维码」，让触发点用例真的走到 QR 那一段。"""
        row = PlatformPushSettings.get_solo()
        row.auto_send_group_qr_after_accept = 1
        row.save(
            update_fields=["auto_send_group_qr_after_accept", "updated_at"]
        )

    def _run_sync_raw(self) -> tuple[str, str]:
        """跑一轮，**真**走 `refresh_push_status` + 真引擎（客户端 / 上传是假件）。"""

        def fake_put_object(sts, object_key, data, content_type, **_kwargs):
            self.uploaded.append(
                {
                    "sts": sts,
                    "object_key": object_key,
                    "data": data,
                    "content_type": content_type,
                }
            )

        out, err = io.StringIO(), io.StringIO()
        with mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=lambda _integration: self.fake,
        ), mock.patch.object(
            engine, "_open_client", side_effect=lambda _integration: self.fake
        ), mock.patch.object(
            engine, "put_object", side_effect=fake_put_object
        ):
            call_command("dailiantong_sync", "--once", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def _run_sync_mocked(self, qr_mock, dispatch_mock) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch(
            "apps.business.dailiantong_push._open_client",
            side_effect=lambda _integration: self.fake,
        ), mock.patch(
            "apps.business.management.commands.dailiantong_sync.send_accept_qr",
            qr_mock,
        ), mock.patch(
            "apps.business.management.commands.dailiantong_sync.dispatch_for_push",
            dispatch_mock,
        ):
            call_command("dailiantong_sync", "--once", stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()


class SyncTriggerTests(_SyncTriggerBase):
    def test_sync_refreshes_then_sends_the_matching_rule(self):
        """端到端：回读把 11 改成 13，自动回复用的是**回读后**的状态。"""
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game, content="端到端话术"
        )
        self.push.remote_status = REMOTE_NOT_ACCEPTED
        self.push.save(update_fields=["remote_status", "updated_at"])
        self.fake.detail_status = REMOTE_WAITING_REVIEW

        out, err = self._run_sync_raw()

        self.assertEqual(err, "")
        self.assertEqual(self._reload().remote_status, REMOTE_WAITING_REVIEW)
        messages = [fields["msg"] for action, fields in self.fake.calls if action == "message_add"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("端到端话术"))
        self.assertIn("系统自动回复", messages[0])
        self.assertEqual(len(self._sent_rows()), 1)
        self.assertIn("自动回复 1", out)

    def test_status_13_is_dispatched_too(self):
        self._rule(
            trigger=TRIGGER_AFTER_REVIEW_REQUEST,
            game=self.game,
            content="验收后话术",
        )
        self.push.remote_status = REMOTE_WAITING_REVIEW
        self.push.save(update_fields=["remote_status", "updated_at"])
        self.fake.detail_status = REMOTE_WAITING_REVIEW

        out, _err = self._run_sync_raw()

        messages = [fields["msg"] for action, fields in self.fake.calls if action == "message_add"]
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0].startswith("验收后话术"))
        self.assertIn("系统自动回复", messages[0])
        self.assertIn("自动回复 1", out)

    def test_qr_runs_before_auto_reply_and_they_do_not_block_each_other(self):
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        self._enable_qr_switch()
        self.fake.detail_status = REMOTE_WAITING_REVIEW
        order: list[str] = []

        def qr_mock(_push):
            order.append("qr")
            return mock.MagicMock(sent=True, failed=False)

        def dispatch_mock(_push):
            order.append("auto_reply")
            return mock.MagicMock(sent=True, failed=False)

        out, _err = self._run_sync_mocked(qr_mock, dispatch_mock)

        self.assertEqual(order, ["qr", "auto_reply"])
        self.assertIn("接手后 1", out)
        self.assertIn("自动回复 1", out)

    def test_qr_failure_does_not_block_auto_reply(self):
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        self._enable_qr_switch()
        self.fake.detail_status = REMOTE_WAITING_REVIEW
        qr_mock = mock.MagicMock(side_effect=RuntimeError("qr boom"))
        dispatch_mock = mock.MagicMock(
            return_value=mock.MagicMock(sent=True, failed=False)
        )

        out, err = self._run_sync_mocked(qr_mock, dispatch_mock)

        self.assertEqual(dispatch_mock.call_count, 1)
        self.assertIn("自动回复 1", out)
        self.assertNotIn("boom", err)

    def test_auto_reply_failure_does_not_break_the_round(self):
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        self.fake.detail_status = REMOTE_WAITING_REVIEW
        qr_mock = mock.MagicMock()
        dispatch_mock = mock.MagicMock(side_effect=RuntimeError("auto boom"))

        out, err = self._run_sync_mocked(qr_mock, dispatch_mock)

        self.assertEqual(dispatch_mock.call_count, 1)
        self.assertIn("轮询 1", out)
        self.assertNotIn("auto boom", err)

    def test_terminal_inactive_push_is_not_dispatched(self):
        """`is_active=False`（远端到终态）的行不再触发自动回复。"""
        self._rule(trigger=TRIGGER_AFTER_REVIEW_REQUEST, game=self.game)
        self.fake.detail_status = REMOTE_WAITING_REVIEW
        self.push.is_active = False
        self.push.save(update_fields=["is_active", "updated_at"])
        dispatch_mock = mock.MagicMock(
            return_value=mock.MagicMock(sent=True, failed=False)
        )

        self._run_sync_mocked(mock.MagicMock(), dispatch_mock)

        self.assertEqual(dispatch_mock.call_count, 0)
