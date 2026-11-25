"""部分失败契约：接收侧的 `skipped_details` → 发送侧的 `done` / `failed` 分流。

spec §16.5：接收侧按**机器码** `code`（`stale` / `bad_shape` / `unknown_status` /
`unknown_order`）报告每一条被跳过的事件；发送侧据此决定这次投递算成功还是失败。

⚠️ 三条判据各自的判别力（⛔ 不是"跑通就行"）：

① **按 `code` 分流、⛔ 不按 `reason` 文本**：`reason` 是对端给人看的文案、随时会改，
   `code` 才是双方约定的契约。`test_classification_uses_the_code_not_the_reason_text`
   用**同一段文本**配两个不同的 `code`，期望两种结果 —— 任何"文本里含某个词就分流"
   的实现都会在那里红（`AGENTS.md` §5-E / E-0011 同族）。
② **只认本事件的 `target_ref`**：一批里可能有别人的明细，⛔ 不能把别人的失败算到自己头上。
③ **兼容旧响应**：没有 `skipped_details`（或它不是 list、或响应根本不是 dict）⇒ 成功。
   最后一种同时守住 P1 那四条投递用例（它们把 `push_events` 打成裸 `MagicMock`——
   `isinstance(MagicMock(), dict)` 为假，裸 mock 不是 dict）。

⚠️ **Task 8（Ruling 10）改了硬跳的落点**：硬跳过不再让事件直接 `failed`——失败是**可重投**的
（spec §16.6 第 3 条：`pending` + 退避）⇒ 本文件的硬跳用例一律断言
`pending` + `now + 60`；`failed` 终态由 `tests/test_peer_worker.py` 钉住
。
⚠️ `mock.patch` 与调用点随投递内核从 `apps.business.api.peer_views` 搬到
`apps.business.peer_worker`（Ruling 6：⛔ 不在 `peer_views` 留 shim；E-0029：桩打在**被查找的
那个模块**上）。

⚠️ 必须用 `TenantTestCase`：`apps.business` 只在 `TENANT_APPS` 里 ⇒ 普通 `TestCase`
的 search_path 里没有 `business_*` 表（照 `tests/test_peer_order_push.py` 的先例）。
"""
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from apps.business.models import PeerDownstreamBinding, PeerSyncEvent
from apps.business.peer_client import PeerRejected, push_events
from apps.business.peer_sync import hash_token, new_token
from apps.business.peer_worker import deliver_one

PEER_BASE_URL = "https://peer-down.test.invalid"


def _detail(target_ref="A-1", code="stale", reason="事件不比本地已应用的新"):
    """接收侧 `skipped_details` 里的一条明细（形状见 `peer_inbound.Outcome`）。"""
    return {"target_ref": target_ref, "code": code, "reason": reason}


class PushEventsReturnsDataTests(SimpleTestCase):
    """`push_events` 把信封里的业务数据 `data` 交给调用方（⛔ 不是回 `None`）。

    判别力：改回 `return None`（P1 的形状）⇒ 下面两条都红；发送侧也就永远看不到
    `skipped_details`（本任务的整个前提）。
    """

    def _push(self, envelope):
        # ⭐ C5：出站改走流式读（`httpx.stream`）⇒ 假件换共用那一份（`_peer_stream_fakes`）。
        from tests._peer_stream_fakes import patch_peer_stream, stream_response

        with patch_peer_stream(stream_response(envelope)):
            return push_events(PEER_BASE_URL, "TOKEN-IN", [{"k": 1}])

    def test_the_business_data_is_returned(self):
        data = {
            "applied": 0,
            "skipped": 1,
            "skipped_reasons": [{"target_ref": "A-1", "reason": "事件不比本地已应用的新"}],
            "skipped_details": [_detail()],
        }
        self.assertEqual(self._push({"code": 0, "data": data}), data)

    def test_a_missing_or_non_object_data_is_an_empty_dict_not_a_crash(self):
        """`data` 缺失 / 为 null / 是数组 ⇒ `{}`（⛔ 不抛：`_post_envelope` 已为真失败抛过）。"""
        for envelope in ({"code": 0}, {"code": 0, "data": None}, {"code": 0, "data": [1]}):
            with self.subTest(envelope=envelope):
                self.assertEqual(self._push(envelope), {})

    def test_a_non_zero_code_still_raises(self):
        """分流只吃**成功信封**里的 `data`：业务码非 0 仍是 `PeerRejected`（⛔ 不被吞掉）。"""
        with self.assertRaises(PeerRejected):
            self._push({"code": 1020, "msg": "令牌无效"})


class DeliverEventPartialFailureTests(TenantTestCase):
    """`deliver_one` 在**没抛异常**的推送之后，按响应业务数据决定成败。"""

    def setUp(self):
        self.now = timezone.now()
        self.account = get_user_model().objects.create_user(
            username="peer-account-a", password="pw-a"
        )
        self.downstream = PeerDownstreamBinding.objects.create(
            account=self.account,
            peer_base_url=PEER_BASE_URL,
            token_in_hash=hash_token(new_token()),
            token_out=new_token(),
        )

    def _new_event(self, target_ref="A-1"):
        return PeerSyncEvent.objects.create(
            binding_role="downstream", binding_id=str(self.downstream.pk),
            direction="out", kind="order.assign", target_type="order",
            target_ref=target_ref, payload={"status": "IN_PROGRESS"},
            idempotency_key=uuid.uuid4().hex,
        )

    def _deliver(self, data, *, event=None):
        """投一次并回**从库里重读的**事件与绑定（判据打在真实出口上，E-0009）。"""
        event = event or self._new_event()
        with mock.patch(
            "apps.business.peer_worker.push_events", return_value=data
        ) as push:
            deliver_one(event.pk, now=self.now)
        event.refresh_from_db()
        self.downstream.refresh_from_db()
        return event, push

    # ── `stale`：对端已有更新的 ⇒ 良性，算成功（spec §16.5）────────────────────
    def test_a_stale_skip_marks_the_event_done(self):
        """`code == "stale"` ⇒ `done`、刷新 `last_ok_at`、清零连续失败；⛔ 不写失败原因。"""
        self.downstream.consecutive_failures = 3
        self.downstream.last_error = "上一次的旧失败"
        self.downstream.save(update_fields=["consecutive_failures", "last_error"])

        event, _ = self._deliver({"skipped": 1, "skipped_details": [_detail(code="stale")]})

        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
        self.assertIsNotNone(event.processed_at)
        # `stale` 是良性 ⇒ 事件上⛔ 不留失败原因（留了会让台账看起来"失败过"）。
        self.assertEqual(event.reason, "")
        self.assertEqual(self.downstream.consecutive_failures, 0)
        self.assertIsNotNone(self.downstream.last_ok_at)

    def test_a_stale_skip_keeps_the_meta_at_the_top_level_of_the_outgoing_event(self):
        """⭐ 跨进程钉子：`meta` 展开在事件对象**顶层**、且 `skipped_details` 分流没把它顶掉。

        ⚠️ 接收侧从 `raw.get("occurred_at")` 读（⛔ 不读 `payload`）⇒ 发送侧少了这几个键，
        同单更新与撤单落库在真环境里永远走不到，而"两层各自绿"看不出接线断了。
        这里特意走**被跳过**的那条路（本任务新碰的代码块）：`stale` 也要带上它们。
        """
        event = self._new_event()
        event.meta = {
            "occurred_at": "2026-09-26T10:00:00+08:00",
            "withdraw_reason": "taker_removed",
            "actor_name": "测试客服甲",
        }
        event.save(update_fields=["meta"])

        event, push = self._deliver(
            {"skipped": 1, "skipped_details": [_detail(code="stale")]}, event=event
        )

        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
        sent = push.call_args.args[2][0]
        self.assertEqual(sent["occurred_at"], "2026-09-26T10:00:00+08:00")
        self.assertEqual(sent["withdraw_reason"], "taker_removed")
        self.assertEqual(sent["actor_name"], "测试客服甲")
        # 事件级字段 ⛔ 不进 `payload`；`payload` 也没被展开的 meta 顶掉。
        for key in ("occurred_at", "withdraw_reason", "actor_name"):
            self.assertNotIn(key, sent["payload"])
        self.assertEqual(sent["payload"], {"status": "IN_PROGRESS"})

    # ── 其余 `code`：如实记失败（可重投）并把对端给的原因带回来 ─────────────────
    def test_an_unknown_status_skip_is_retried_with_the_peers_reason(self):
        """`unknown_status` ⇒ `pending` + `now + 60` + 对端原因 + 绑定的失败台账。

        ⚠️ Task 8（spec §16.6 第 3 条）：失败**可重投** ⇒ 落 `pending` + 退避；
        终态 `failed` 只在**第 5 次**失败时出现（修复轮 Minor 4；
        `tests/test_peer_worker.py` 钉住）。
        ⭐ PW-01：硬跳是**业务类**失败 ⇒ ⛔ **不计入**自动停绑计数
        （对端能回 `skipped_details` = 链路是通的；计数只留给「失联」）。
        """
        peer_reason = "未知或缺失的订单状态：'NOT_A_STATUS'"
        # 预置非零再断言「逐字未动」（E-0024：默认 0 上断言 0 是恒真的）。
        self.downstream.consecutive_failures = 2
        self.downstream.save(update_fields=["consecutive_failures"])

        event, _ = self._deliver({
            "skipped": 1,
            "skipped_details": [_detail(code="unknown_status", reason=peer_reason)],
        })

        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        # 对端的原因**原样**进 `reason`（⛔ 不是我们编一句"投递失败"）。
        self.assertEqual(event.reason, peer_reason)
        self.assertIsNone(event.processed_at)
        self.assertEqual(event.attempts, 1)
        self.assertEqual(event.next_attempt_at, self.now + timedelta(seconds=60))
        # 失败台账照旧写两列（原因 / 时刻）；计数**不**动（PW-01：业务类不计入）。
        self.assertEqual(self.downstream.consecutive_failures, 2)
        self.assertEqual(self.downstream.last_error, peer_reason)
        self.assertEqual(self.downstream.last_error_at, self.now)
        self.assertIsNone(self.downstream.last_ok_at)

    def test_an_unknown_order_skip_is_retried(self):
        """`unknown_order`（撤单指向本地没有的单）⇒ 记失败（可重投），⛔ 不当良性吞掉。"""
        event, _ = self._deliver({
            "skipped": 1,
            "skipped_details": [
                _detail(code="unknown_order", reason="撤单指向一张本地没有的订单")
            ],
        })
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(event.reason, "撤单指向一张本地没有的订单")

    def test_a_bad_shape_skip_is_retried(self):
        """`bad_shape` 也是硬跳过（四个码里剩下的那个）⇒ 记失败。"""
        event, _ = self._deliver({
            "skipped": 1,
            "skipped_details": [_detail(code="bad_shape", reason="payload 不是对象")],
        })
        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(event.reason, "payload 不是对象")

    def test_the_peers_reason_is_clipped_to_the_event_column(self):
        """对端原因长于 `reason` 列宽 ⇒ 截断（⛔ 否则 PostgreSQL 报 `value too long`）。

        ⚠️ 列宽从**模型元数据**取（⛔ 不抄 255），并先自证夹具真的超过列宽。
        """
        width = PeerSyncEvent._meta.get_field("reason").max_length
        long_reason = "对端跳过原因：" + "长" * (width + 40)
        self.assertGreater(len(long_reason), width)     # 夹具自证：真的超过列宽

        event, _ = self._deliver({
            "skipped": 1,
            "skipped_details": [_detail(code="unknown_status", reason=long_reason)],
        })

        self.assertEqual(event.status, PeerSyncEvent.Status.PENDING)
        self.assertEqual(len(event.reason), width)
        self.assertEqual(event.reason, long_reason[:width])

    def test_classification_uses_the_code_not_the_reason_text(self):
        """⭐ 核心判据：**同一段文案**配两个 `code` ⇒ 两种结果（按文本分流必红）。

        接收侧的 `reason` 是给人读的中文、随时会改；`code` 才是契约（spec §16.5）。
        判别力：把实现改成"文本里含某个词（如「更旧」）就良性"，本用例里
        `unknown_status` 那一格会被判成 `done` ⇒ 红。
        """
        shared_text = "事件比本地已应用的更旧（这段文案不足以说明分流口径）"
        for code, expected in (
            ("stale", PeerSyncEvent.Status.DONE),
            ("unknown_status", PeerSyncEvent.Status.PENDING),
        ):
            with self.subTest(code=code):
                event, _ = self._deliver({
                    "skipped": 1,
                    "skipped_details": [_detail(code=code, reason=shared_text)],
                })
                self.assertEqual(event.status, expected)

    # ── 兼容 / 边界：一律算成功 ────────────────────────────────────────────────
    def test_a_response_without_skipped_details_is_a_success(self):
        """响应里没有 `skipped_details`（P1 形状）⇒ 成功，⛔ 不因为"没看到明细"就记失败。"""
        event, _ = self._deliver({
            "applied": 0,
            "skipped": 1,
            "skipped_reasons": [{"target_ref": "A-1", "reason": "旧形状的跳过"}],
        })
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)

    def test_a_skip_for_another_target_ref_does_not_fail_this_event(self):
        """明细里是**别人**的 `target_ref` ⇒ 与本事件无关，算成功（⛔ 不张冠李戴）。"""
        event, _ = self._deliver({
            "skipped": 1,
            "skipped_details": [
                _detail(target_ref="B-9", code="unknown_status", reason="别人那条")
            ],
        })
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)

    def test_a_non_list_skipped_details_is_a_success(self):
        """`skipped_details` 不是 list（对端形状漂了）⇒ 成功，⛔ 不去猜、更不 500。"""
        event, _ = self._deliver({
            "skipped": 1,
            "skipped_details": _detail(code="unknown_status"),
        })
        self.assertEqual(event.status, PeerSyncEvent.Status.DONE)

    def test_a_non_dict_response_is_a_success(self):
        """响应不是 dict（含 P1 用例里那个裸 `MagicMock`）⇒ 成功。

        ⚠️ `None` 那一格是判别力所在：不先判 `isinstance(data, dict)` 的实现在它上面
        `AttributeError`（本用例 `ERROR`），而不是让状态断言红。
        """
        for data in (mock.MagicMock(), None, ["nope"], "nope"):
            with self.subTest(data=type(data).__name__):
                event, _ = self._deliver(data)
                self.assertEqual(event.status, PeerSyncEvent.Status.DONE)
