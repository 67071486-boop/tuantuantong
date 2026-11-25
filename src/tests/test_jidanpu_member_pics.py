"""代练员线：**订单截图双向同步**（计划 `2026-10-05-member-order-pics`；A 段派单传图 + B 段拉图）。

被钉住的契约（业务方口径）：

- **A 段**（`member.assign` 的消费路径）：派单那一刻把本单 `ready` 的**订单截图 + 群聊二维码**
  （口径②「**全传**」）传到**下家那张单**上（`method=POST /OrderAjax/UploadOrderPic`，
  `orderId` = `link.jidanpu_order_id`、`imgType` 逐张 = `"0"` / `"1"`、
  `gameAccId` / `taskId` **来自下家上传页的隐藏域**——⛔ 不是硬编码）；
- ⛔ **口径差异**：上家线是「传图失败 ⇒ 不提交完成」，**本线是「传图失败不挡派单」**——
  事件照常 `done`、只留一条台账人话、⛔ 不写 `last_error`；
- **B 段**（状态回拉命中「已完成」）：先把下家的图**拉回来**存进我方订单截图
  （`OrderScreenshot`：两种图种 / `ready` / 确定性对象名 ⇒ 幂等），**然后无条件**写「待验收」；
- ⛔ **拉图任何失败都不挡「待验收」**（口径③）：`ViewPic` 报错 / 单张下载失败 / 单张落库失败
  都只留台账人话，**不写 `last_error`、不挡水位**；⚠️ 但「**没图**」（正常回空）与
  「**没拉成**」（异常）**不许混**——前者一条台账都不写；
- ⛔ **隐私**：图 URL / `accessid` / `signature` / 远程对象名 / 对端上传者名字**不进台账人话**、
  ⛔ 不进 `remark`。

## 本文件怎么钉 / 桩打在哪

- **A 段**走**真的** `consume_event`：夹具预置链路 ⇒ `_ensure_remote_order` 短路（⛔ 一次上游都不打），
  于是整条路上只有「传图三步」会出网，而那三步全部打在替身上：
  `_StubClient`（SDK 边界）/ `_StubHttp`（下家上传页那一跳）/ `post_to_oss`（OSS 直传那一跳，
  **打在 `jidanpu_upstream_pics`——被查找的模块**，E-0029）。
  这三个替身的形状**直接引** `tests/test_jidanpu_upstream_pics.py` 里那三份
  （⛔ 不另创一套假 client：换了形状的后果是「测试自己造了一个不存在的客户端」）；
- **B 段**走**真的** `pull_statuses` + **真 SDK + `httpx.MockTransport`**（零网络）：难点全在
  SDK 对这个未登记端点的**宽松规则**上（`code==0` + 载荷在 `msg` ⇒ 它判失败并抛 `ApiError`），
  用桩冒充验的是桩、不是那条规则（照 `RemoteHasPicRealSdkTests` 的同一条理由）。
  下载那一跳用 `jidanpu_upstream_pics.download_pic` 的替身（⛔ 真去 OSS 取图既慢又不可复现）；
  `download_pic` **自己**的形状（无 cookie / 非 200 / 0 字节）由
  `tests/test_jidanpu_upstream_pics.py::DownloadPicTests` 钉。
- ⚠️ 断言一律打在**数据库行 / 桩收到的调用载荷**上（⛔ 不打 logger，E-0009）；
  期望值一律**字面量**（⛔ 不拿被测常量当期望，E-0024）；夹具一律**合成值**（E-0077）、零真实网络。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.business import jidanpu, jidanpu_member, jidanpu_upstream_pics
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderScreenshot,
    OrderStatus,
    Performance,
    SettleStatus,
)
from apps.core.storage import get_storage
from tests.p5_auth_helpers import make_logged_in_client

# ⚠️ 替身形状**只此一处**：直接引传图那一网里那三份 + 端点字面量与合成值
#    （⛔ 不在这里凭印象另造一个假 client——造错了等于测一个不存在的客户端）。
from tests.test_jidanpu_upstream_pics import (
    GAME_ACC_SYNTH,
    LOGIN_PATH,
    OSS_HOST,
    TASK_SYNTH,
    UPLOAD_PIC_PATH,
    VIEW_PIC_PATH,
    _http_factory,
    _OssRecorder,
    _StubClient,
    _StubHttp,
    _view_pic_shell,
)

# ── 合成夹具值（E-0077）─────────────────────────────────────────────────────────

BASE_URL = "https://jidanpu-member-pics.test.invalid"
ACCOUNT = "member-pics-a"
ORDER_NO = "20261005000000000801"
JID = "JDP-00000000000000000801"
MACHINE_NODES = ("order.change_status", "order.change_status_completed", "grab_hall.take_order")

#: 台账 kind（**字面量**；本线刻意复用既有的两个，⛔ 不新增 kind ⇒ 不动前端那条恰好集合网）。
KIND_ASSIGN_LITERAL = "member.assign"
KIND_STATUS_IN_LITERAL = "member.status_in"

#: 图种字面量（⛔ 不从模型常量派生）。
IMG_ORDER_LITERAL = 0
IMG_GROUP_QR_LITERAL = 1

#: 两张合成图的源直链（带 canary：「⛔ 不进台账」那一条要搜字符串，spec §11-18 的同族）。
CANARY_URL = "https://oss-synth.invalid/pull-a.jpg?accessid=CANARY-ACCESSID&signature=CANARY-SIG"
#: 同形状的**合成**对象名（`<4位>-<19位>-<10位>.png`）。⛔ 真机那个串不进 `src/**`
#: （`AGENTS.md` §5-C / E-0077）；出处见 `.tools/recon/media-storage/Order__PicMange.html`。
CANARY_REMOTE_NAME = "9000-0000000000000000000-0000000000.png"

JPEG_BODY = b"\xff\xd8\xff" + b"\0" * 8
PNG_BODY = b"\x89PNG\r\n\x1a\n" + b"\0" * 8


def _remote_row(*, order_id=JID, order_sta="已完成"):
    """一行真实形状的订单列表回包（本线只承重 `orderId` 与 `orderSta`）。"""
    return {"orderId": order_id, "orderSta": order_sta, "isThrowOrder": "N"}


class _MemberPicsBase(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 10, 5, 10, 0, tzinfo=dt_timezone.utc)
        self.game = Game.objects.create(name="合成游戏甲")
        # 节点：状态回拉那条路要能写「待验收」（照慢路那一网的同一组）。
        make_logged_in_client(
            username=ACCOUNT, groups=("代练员",), nodes=MACHINE_NODES,
        )
        self.user = User.objects.get(username=ACCOUNT)
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account="acc-member-pics-a",
            account_name="传令兵甲",
            password="pw-member-pics-a",
            source_name="团团通系统",
            enabled=True,
        )
        self.order = Order.objects.create(
            game=self.game,
            order_no=ORDER_NO,
            status=OrderStatus.IN_PROGRESS,
            hall_price="12.34",
            total_price="50.00",
            project_text="合成项目甲",
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, user=self.user,
            temp_amount=Decimal("12.34"), settle_status=SettleStatus.UNSETTLED,
        )
        self.link = JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order, jidanpu_order_id=JID, in_hall=False,
        )
        #: ⭐ S1：A 段要读**下家上传页**那一跳 ⇒ 测试里**默认就给**一个
        #: http 替身，别让「注入了 client、忘了 http」这种测试自身的漏配去撞生产代码的边界
        #: （生产唯一调用点必然给 `real_http`；那条边界另有 `test_a_missing_http_is_loud...` 钉）。
        self.http = _StubHttp()
        lookup = mock.patch.object(jidanpu, "find_order_for_repair", side_effect=self._lookup_remote)
        lookup.start()
        self.addCleanup(lookup.stop)

    def _lookup_remote(self, client, *, remote_order_id, **_filters):
        return {
            "orderId": remote_order_id, "orderIdView": ORDER_NO, "orderSta": "未分配",
            "isThrowOrder": "Y" if self.link.in_hall else "N",
        }

    # ── 工具 ────────────────────────────────────────────────────────────────

    def _shot(self, *, img_type=IMG_ORDER_LITERAL, ext="jpg", key=None):
        key = key or f"shared-orders/{ORDER_NO}/shot-{img_type}-{ext}.{ext}"
        body = JPEG_BODY if ext in ("jpg", "jpeg") else PNG_BODY
        get_storage().put_bytes(key, body, content_type="image/jpeg")
        return OrderScreenshot.objects.create(
            order=self.order, img_type=img_type, object_key=key,
            status=OrderScreenshot.STATUS_READY, size_bytes=len(body),
            uploaded_by=self.user, remark="合成备注",
        )

    def _ledger(self, *, kind=KIND_ASSIGN_LITERAL, direction="in"):
        return JidanpuSyncEvent.objects.filter(
            kind=kind, direction=direction, target_ref=ORDER_NO
        )

    def _register_payloads(self, client):
        return [kw["data"] for _m, path, kw in client.calls if path == UPLOAD_PIC_PATH]


# ══ A 段：派单那一刻传图（我方 → 下家）════════════════════════════════════════════


class AssignPushScreenshotsTests(_MemberPicsBase):
    def _consume_assign(self, *, client, http="__default__"):
        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_ASSIGN,
        )
        if http == "__default__":
            http = self.http
        with mock.patch.object(
            jidanpu_upstream_pics, "post_to_oss", side_effect=_OssRecorder(),
        ) as _oss:
            outcome = jidanpu_member.consume_event(event, client=client, http=http)
        event.refresh_from_db()
        return outcome, event

    def test_it_pushes_every_ready_screenshot_of_both_kinds_to_the_downstream_order(self):
        """⭐ 口径②「全传」：订单截图 + 群聊二维码**都传**，`orderId` = 本线链路那一列。

        `gameAccId` / `taskId` 断的是**下家上传页里那些合成值**（`7` / `9`）——
        真机实测那两格是 `0` ⇒ 断言 `7` / `9` 才证明「读出来的，不是写死的」。
        """
        self._shot(img_type=IMG_ORDER_LITERAL)
        self._shot(img_type=IMG_GROUP_QR_LITERAL, ext="png",
                   key=f"{self.binding.user.username}/orders/{ORDER_NO}/qr.a.png")
        client = _StubClient()
        http = _StubHttp()

        outcome, event = self._consume_assign(client=client, http=http)

        self.assertEqual(outcome, "done")
        self.assertEqual(event.status, "done")
        payloads = self._register_payloads(client)
        self.assertEqual(len(payloads), 2)
        self.assertEqual([p["orderId"] for p in payloads], [JID, JID])
        # ⛔ 逐张带真实图种（对标上传页的 radio：0 = 订单截图、1 = 群聊二维码）。
        self.assertEqual([p["imgType"] for p in payloads], ["0", "1"])
        self.assertEqual([p["gameAccId"] for p in payloads], [GAME_ACC_SYNTH, GAME_ACC_SYNTH])
        self.assertEqual([p["taskId"] for p in payloads], [TASK_SYNTH, TASK_SYNTH])
        self.assertTrue(all(p["imgListStr"].startswith(f"{OSS_HOST}/") for p in payloads))
        # 上传页只读了一次（隐藏域那一跳）；读的是**下家**那台（地址来自绑定）。
        self.assertEqual([path for path, _kw in http.gets], ["/Order/UploadPic"])
        # ⭐ 成功**留一条 `done`**（否则「一张都没传」与「传了」在页面上长得一样）；
        # ⛔ 一条失败台账都没有。
        self.assertEqual([(n.status, n.direction) for n in self._ledger()], [("done", "in")])

    def test_without_any_screenshot_it_never_calls_a_picture_endpoint(self):
        """没有可传的图时，核对订单后直接结束。"""
        http = _StubHttp()

        outcome, event = self._consume_assign(client=object(), http=http)

        self.assertEqual(outcome, "done")
        self.assertEqual(event.status, "done")
        self.assertEqual(http.gets, [])
        self.assertEqual(self._ledger().count(), 0)

    def test_it_pushes_anyway_when_the_downstream_order_already_has_a_picture(self):
        """⭐⭐ 阻塞 1（2026-10-05 修复轮；**2026-10-24 / E-0282 改准为 per-pic 口径**）：
        下家那张单**已经有图**（含它家机器人自动挂的群二维码，**异键**）⇒ 我方那几张
        **照传**——对面已有的图不再整档挡住「我的图到他那儿」（口径②不回退）。

        判别读数：`ViewPic` **恰好一次**（E-0282 起 A 段走 `only_missing=True`，那次读取
        服务 per-pic 判缺——本用例里对面那张机器人群码是**异键**、我方两张都没有历史
        登记映射 ⇒ 两张都判缺、都传）。把 `only_missing` 拿掉（回到整档短路）⇒
        「register 调用数 = 2」那条红（真实后果 = A 段整体静默不生效）；把 `skip_if_remote_has_pic`
        改回默认 `True` ⇒ 同样红（短路先命中、一张都不传）。
        """
        self._shot(img_type=IMG_ORDER_LITERAL)
        self._shot(img_type=IMG_GROUP_QR_LITERAL, ext="png",
                   key=f"{ACCOUNT}/orders/{ORDER_NO}/qr.b.png")
        client = _StubClient(pic_envelope={
            "code": 0, "msg": {"title": "", "data": [
                {"pid": "0", "IsOpenView": "None",
                 "src": "https://oss-synth.invalid/theirs.jpg",
                 "alt": "【机器人01】上传于10-5 09:00，说明：群二维码"},
            ]},
        })

        outcome, event = self._consume_assign(client=client)

        self.assertEqual((outcome, event.status), ("done", "done"))
        payloads = self._register_payloads(client)
        self.assertEqual(len(payloads), 2)
        self.assertEqual([p["imgType"] for p in payloads], ["0", "1"])
        # ⭐ 恰好一次 ViewPic（L2 硬判据）：per-pic 判缺的取数口，不再多打第二次。
        self.assertEqual(
            [path for _m, path, _kw in client.calls if path == VIEW_PIC_PATH],
            [VIEW_PIC_PATH],
        )

    def test_a_successful_push_leaves_a_done_note_with_the_kind_composition(self):
        """⭐ 成功留痕（评审阻塞 1 第 3 条）：传上去 ≥1 张 ⇒ 一条 `done` 台账，带张数与图种构成。"""
        self._shot(img_type=IMG_ORDER_LITERAL)
        self._shot(img_type=IMG_GROUP_QR_LITERAL, ext="png",
                   key=f"{ACCOUNT}/orders/{ORDER_NO}/qr.c.png")

        _outcome, _event = self._consume_assign(client=_StubClient())

        note = self._ledger().get()
        self.assertEqual(note.status, "done")
        self.assertEqual(
            note.reason, "订单截图已传到下家（共 2 张：订单截图 1 张、群二维码 1 张）",
        )
        # ⛔ 人话里不许出现对象名 / URL / 桶名（隐私口径）。
        for canary in ("https", "shared-orders", "oss-", "qr.", "CANARY"):
            self.assertNotIn(canary, note.reason)

    def test_a_missing_http_is_loud_not_a_business_failure(self):
        """⭐ S1（评审建议 4）：`http=None` 是**接线漏了**（不是「传图失败」）
        ⇒ 响亮抛，⛔ 不许被口径①那个 `except Exception` 吞成一条台账。"""
        self._shot()
        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_ASSIGN,
        )

        with self.assertRaises(ValueError):
            jidanpu_member.consume_event(event, client=_StubClient(), http=None)

        self.assertEqual(self._ledger().count(), 0)

    def test_a_push_failure_does_not_block_the_assign(self):
        """⛔ 口径①：传图失败（取 OSS 签名被拒）⇒ **派单照常完成** + 一条台账人话。

        ⚠️ 与上家线相反的那一条：那边 `blocks_completion=True` ⇒ 一次提交都不发。
        """
        self._shot()
        client = _StubClient(
            policy_error=jidanpu_upstream_pics.ApiError("合成：取签名失败", code=0),
        )

        outcome, event = self._consume_assign(client=client, http=_StubHttp())

        self.assertEqual(outcome, "done")
        self.assertEqual(event.status, "done")
        note = self._ledger().get()
        self.assertEqual(note.status, "failed")
        self.assertIn("派单已成功", note.reason)
        self.assertIn("订单截图没传到下家", note.reason)
        # 台账人话⛔ 不带凭据 / 对象名（spec §11-18 的同一口径）。
        self.assertNotIn("CANARY", note.reason)
        # 「传图失败不挡派单」⇒ **不写** `last_error`（那是承重动作的失败面）。
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")

    def test_an_unexpected_push_error_also_does_not_block_the_assign(self):
        """⛔ 口径①的第二半：**预期之外**的异常（我们自己的编程错）同样不挡派单。

        判别力：把 `_push_order_screenshots_to_downstream` 里那个 `except Exception`
        去掉 ⇒ 事件变 `failed`（`run_outbox` 会退避重试一次**已经成功过的派单**）⇒ 本条红。
        """
        self._shot()
        with mock.patch.object(
            jidanpu_upstream_pics, "push_order_screenshots",
            side_effect=RuntimeError("合成：编程错"),
        ):
            outcome, event = self._consume_assign(client=_StubClient(), http=_StubHttp())

        self.assertEqual(outcome, "done")
        self.assertEqual(event.status, "done")
        note = self._ledger().get()
        self.assertEqual(note.status, "failed")
        self.assertIn("RuntimeError", note.reason)


# ══ B 段：下家已完成 ⇒ 先拉图、再无条件写「待验收」════════════════════════════════


class PullScreenshotsTests(_MemberPicsBase):
    """真 `pull_statuses` + 真 SDK（`MockTransport`）⇒ 契约、链路、状态一起钉。"""

    def _handler(self, view_pic):
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == LOGIN_PATH:
                return httpx.Response(
                    200, json={"code": 1, "msg": "ok", "data": "ok"},
                    headers={"Set-Cookie": "sl-session=synth-member; Path=/"},
                )
            if path == VIEW_PIC_PATH:
                return view_pic(request)
            raise AssertionError(f"未预期的请求：{request.method} {path}")

        return handler

    def _empty_download(self, _src):
        return JPEG_BODY

    def _pull(self, view_pic, *, download=None, row=None, has_row=True):
        rows = (_remote_row() if row is None else row,) if has_row else ()
        with (
            mock.patch.object(
                jidanpu, "fetch_completed_orders_page",
                return_value=jidanpu.OrderListPage(rows=rows, count=len(rows)),
            ),
            mock.patch.object(jidanpu.httpx, "Client", _http_factory(self._handler(view_pic))),
            mock.patch.object(
                jidanpu_upstream_pics, "download_pic",
                side_effect=download or self._empty_download,
            ),
        ):
            return jidanpu_member.pull_statuses(self.binding, now=self.now)

    def _rows(self):
        return list(OrderScreenshot.objects.filter(order=self.order).order_by("pk"))

    def test_it_skips_our_own_pushed_pictures_and_keeps_the_genuine_one(self):
        """⭐⭐ 回环断点（计划 Rev.2）：我们推给下家的订单截图 / 群码都**不拉回**，
        下家**真上传**的那张订单截图照收。

        ⚠️ 判别力：不传 `skip_remote_keys` ⇒ 推过的两张也被拉回（行数变 3）；
        不传 `img_types` ⇒ 群码与真图一起入库（图种断言红）。
        """
        pushed_shot_obj = "10001-20261005000000000801-11111-1792908117001.jpg"
        pushed_qr_obj = "10001-20261005000000000801-22222-1792908117002.png"
        genuine_obj = "10001-20261005000000000801-33333-1792908117003.jpg"
        genuine_src = f"https://oss-synth.invalid/{genuine_obj}"
        JidanpuSyncEvent.objects.create(
            direction="in", kind=KIND_ASSIGN_LITERAL, status="done",
            target_type="order", target_ref=ORDER_NO,
            reason="订单截图已传到下家（共 2 张：订单截图 1 张、群二维码 1 张）",
            payload={
                "member_binding_id": self.binding.pk,
                "pushed_pic_keys": {
                    f"shared-orders/{ORDER_NO}/pushed-shot.jpg": pushed_shot_obj,
                    f"{connection.schema_name}/orders/{ORDER_NO}/qr.pushed.png": pushed_qr_obj,
                },
            },
        )

        def view_pic(_request):
            return httpx.Response(200, json=_view_pic_shell([
                {"pid": "0", "IsOpenView": "None",
                 "src": f"https://oss-synth.invalid/{pushed_shot_obj}",
                 "alt": "【机器人01】上传于10-5 09:10，说明：订单截图"},
                {"pid": "1", "IsOpenView": "None",
                 "src": f"https://oss-synth.invalid/{pushed_qr_obj}",
                 "alt": "【机器人01】上传于10-5 09:11，说明：群二维码"},
                {"pid": "2", "IsOpenView": "None", "src": genuine_src,
                 "alt": "【下家客服】上传于10-5 09:12，说明：订单截图"},
            ]))

        downloaded: list[str] = []

        def download(src):
            downloaded.append(src)
            return JPEG_BODY

        applied = self._pull(view_pic, download=download)

        self.assertEqual(applied, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(
            [row.img_type for row in self._rows()], [IMG_ORDER_LITERAL],
        )
        # 判据打在**下载出口**上：两张我们推过的**一次都没下载**（⛔ 不是「下载了又丢掉」）。
        self.assertEqual(downloaded, [genuine_src])

    def test_pushed_remote_keys_unions_every_ledger_row(self):
        """`_pushed_remote_keys` = **多行 × 多值**的并集（推送台账的取数口径）。"""
        for tag in ("a", "b"):
            JidanpuSyncEvent.objects.create(
                direction="in", kind=KIND_ASSIGN_LITERAL, status="done",
                target_type="order", target_ref=ORDER_NO,
                reason="订单截图已传到下家（共 1 张：订单截图 1 张）",
                payload={
                    "member_binding_id": self.binding.pk,
                    "pushed_pic_keys": {f"shared-orders/{ORDER_NO}/{tag}.jpg": f"oss-{tag}.jpg"},
                },
            )
        self.assertEqual(
            jidanpu_member._pushed_remote_keys(self.order, self.binding),
            {"oss-a.jpg", "oss-b.jpg"},
        )

    def test_it_stores_the_order_screenshot_then_writes_pending_review(self):
        """⭐ 主路径：`code==0`（SDK 抛 `ApiError`，载荷在 `msg`）⇒ 订单截图入库、状态写「待验收」。

        ⚠️ 2026-10-25 改准（计划 Rev.2）：代练员线**拉回只收订单截图**——回包里的群码
        （本线自己推上去的那一类）按图种过滤掉，⛔ 不再入库（回环断点）。
        """
        def view_pic(_request):
            return httpx.Response(200, json=_view_pic_shell([
                {"pid": "0", "IsOpenView": "None", "src": CANARY_URL,
                 "alt": "【机器人01】上传于10-5 09:10，说明：订单截图"},
                {"pid": "1", "IsOpenView": "None", "src": "https://oss-synth.invalid/pull-qr.png",
                 "alt": "【机器人01】上传于10-5 09:11，说明：群二维码"},
            ]))

        applied = self._pull(view_pic, download=lambda src: PNG_BODY if "qr" in src else JPEG_BODY)

        self.assertEqual(applied, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        rows = self._rows()
        self.assertEqual([row.img_type for row in rows], [IMG_ORDER_LITERAL])
        self.assertEqual([row.status for row in rows], ["ready"])
        self.assertEqual([row.uploaded_by_id for row in rows], [self.user.pk])
        # ⛔ `alt` 里对端上传者的名字一个字都不进 `remark`。
        self.assertEqual([row.remark for row in rows], [""])
        # 订单截图走共享段（二维码不再回收 ⇒ 本单前缀那一支不必断言）。
        self.assertTrue(rows[0].object_key.startswith(f"shared-orders/{ORDER_NO}/"))
        # 台账：只有那一句「转到待验收」的 `done`——拉图成功**不另写**一行。
        notes = self._ledger(kind=KIND_STATUS_IN_LITERAL)
        self.assertEqual(
            [(n.status, "待验收" in n.reason) for n in notes], [("done", True)],
        )

    def test_the_lenient_positive_code_envelope_also_works(self):
        """另一种信封形态：`code>0` 正常返回 ⇒ 同样入库（⛔ 不许只认一种形态）。"""
        def view_pic(_request):
            return httpx.Response(200, json={
                "code": 1, "msg": {"start": 0, "title": None, "id": 0, "data": [
                    {"pid": "0", "IsOpenView": "None", "src": CANARY_URL,
                     "alt": "【机器人01】上传于10-5 09:10，说明：订单截图"},
                ]},
            })

        applied = self._pull(view_pic)

        self.assertEqual(applied, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(len(self._rows()), 1)

    def test_the_second_pull_does_not_store_a_second_row(self):
        """⭐ 幂等：同一张图第二轮**一张都不再存**（确定性对象名 + `object_key` 唯一）。"""
        shell = _view_pic_shell([
            {"pid": "0", "IsOpenView": "None", "src": CANARY_URL,
             "alt": "【机器人01】上传于10-5 09:10，说明：订单截图"},
        ])

        first = self._pull_screenshots_directly(shell)
        keys = [row.object_key for row in self._rows()]
        second = self._pull_screenshots_directly(shell)

        self.assertEqual(len(first.pulled), 1)
        self.assertEqual(second.pulled, ())
        self.assertEqual(list(second.existing), keys)
        self.assertEqual(len(self._rows()), 1)

    def _pull_screenshots_directly(self, shell):
        """直接调 B 段那个函数两次（第二次没有任何新行）。"""
        def view_pic(_request):
            return httpx.Response(200, json=shell)

        with (
            mock.patch.object(jidanpu.httpx, "Client", _http_factory(self._handler(view_pic))),
            mock.patch.object(
                jidanpu_upstream_pics, "download_pic", side_effect=self._empty_download,
            ),
        ):
            with jidanpu.open_client_with_http(self.binding) as (client, _http):
                return jidanpu_upstream_pics.pull_order_screenshots(
                    client, order=self.order, remote_order_id=JID,
                    uploaded_by=self.user, now=self.now,
                )

    def test_an_empty_pull_is_not_a_failure_and_still_writes_pending_review(self):
        """⛔ 口径③的另一半：**没图**（正常回空）⇒ 状态照写、**一条失败台账都不留**。

        判别力：把「没图」也当失败记一条 ⇒ 本条的台账条数断言红（那正是要避免的混淆）。
        """
        applied = self._pull(lambda _r: httpx.Response(200, json=_view_pic_shell([])))

        self.assertEqual(applied, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self._rows(), [])
        self.assertEqual(
            [n.status for n in self._ledger(kind=KIND_STATUS_IN_LITERAL)], ["done"],
        )

    def test_a_view_pic_failure_still_writes_pending_review(self):
        """⭐ 口径③：`ViewPic` 连不上 ⇒ **状态照写** + 一条失败台账；⛔ 不写 `last_error`、不挡水位。"""
        def view_pic(_request):
            raise httpx.ConnectError("合成：连不上")

        applied = self._pull(view_pic)

        self.assertEqual(applied, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(self._rows(), [])
        notes = list(self._ledger(kind=KIND_STATUS_IN_LITERAL).order_by("id"))
        self.assertEqual([n.status for n in notes], ["failed", "done"])
        self.assertIn("没拉回来", notes[0].reason)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")
        self.assertEqual(self.binding.status_pull_at, self.now)

    def test_the_pictures_are_stored_before_the_status_is_written(self):
        """⭐⭐ S4（评审建议 3）：**顺序钉子** —— 写「待验收」那一刻，图行**已经存在**。

        为什么单开一条：把「拉图」挪到状态回写**之后**，主路径那两条用例**照样绿**
        （它们只看终态），只有 3 条失败路径会因为台账行序反转而红
        ⇒ 没有这条，「顺序反了」这件事**检不出来**（独立评审实测）。
        """
        seen = {}
        real_transition = jidanpu_member.apply_transition

        def probe(order, *args, **kwargs):
            seen["rows_at_transition"] = OrderScreenshot.objects.filter(order=order).count()
            return real_transition(order, *args, **kwargs)

        def view_pic(_request):
            return httpx.Response(200, json=_view_pic_shell([
                {"pid": "0", "IsOpenView": "None", "src": CANARY_URL,
                 "alt": "【机器人01】上传于10-5 09:10，说明：订单截图"},
            ]))

        with mock.patch.object(jidanpu_member, "apply_transition", side_effect=probe):
            applied = self._pull(view_pic)

        self.assertEqual(applied, 1)
        # 写状态那一刻就该有 1 行；挪到写状态之后 ⇒ 这里是 0 ⇒ 红。
        self.assertEqual(seen.get("rows_at_transition"), 1)
        self.assertEqual(len(self._rows()), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)

    def test_an_unexpected_pull_error_still_writes_pending_review(self):
        """口径③的**最后一道**：B 段入口自己抛（编程错 / 存储后端）⇒ 状态照写。

        判别力：去掉 `_pull_remote_screenshots` 里那个 `except Exception` ⇒ 异常逃到
        `pull_statuses`（未预期 ⇒ 由调度层记 `unexpected`）⇒「待验收」根本写不下去 ⇒ 本条红。
        """
        with mock.patch.object(
            jidanpu_upstream_pics, "pull_order_screenshots",
            side_effect=RuntimeError("合成：编程错"),
        ):
            applied = self._pull(lambda _r: httpx.Response(200, json=_view_pic_shell([])))

        self.assertEqual(applied, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        notes = list(self._ledger(kind=KIND_STATUS_IN_LITERAL).order_by("id"))
        self.assertEqual([n.status for n in notes], ["failed", "done"])
        self.assertIn("RuntimeError", notes[0].reason)

    def test_one_bad_picture_does_not_block_the_others_or_the_status(self):
        """单张下载失败 ⇒ 只丢那一张（其余照存），状态照写。"""
        def view_pic(_request):
            return httpx.Response(200, json=_view_pic_shell([
                {"pid": "0", "IsOpenView": "None", "src": CANARY_URL,
                 "alt": "【机器人01】上传于10-5 09:10，说明：订单截图"},
                {"pid": "1", "IsOpenView": "None", "src": "https://oss-synth.invalid/pull-b.jpg",
                 "alt": "【机器人01】上传于10-5 09:11，说明：订单截图"},
            ]))

        def download(src):
            if src == CANARY_URL:
                raise httpx.ConnectError(f"合成：连不上 {src}")
            return JPEG_BODY

        applied = self._pull(view_pic, download=download)

        self.assertEqual(applied, 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PENDING_REVIEW)
        self.assertEqual(len(self._rows()), 1)
        notes = list(self._ledger(kind=KIND_STATUS_IN_LITERAL).order_by("id"))
        self.assertEqual([n.status for n in notes], ["failed", "done"])
        self.assertIn("第 1 张", notes[0].reason)

    def test_the_ledger_never_carries_the_source_url_or_the_remote_object_name(self):
        """⛔ 隐私（spec §6.3 / §11-18 的同一条）：图 URL（带签名）/ 远程对象名 / 对端人名不进人话。"""
        def view_pic(_request):
            return httpx.Response(200, json=_view_pic_shell([
                {"pid": "0", "IsOpenView": "None",
                 "src": f"https://oss-synth.invalid/{CANARY_REMOTE_NAME}",
                 "alt": "【机器人01】上传于10-5 09:10，说明：订单截图"},
            ]))

        def download(src):
            raise httpx.ConnectError(f"合成：连不上 {src}")

        self._pull(view_pic, download=download)

        notes = list(self._ledger(kind=KIND_STATUS_IN_LITERAL))
        # ⭐ 前置断言：**先证明这一轮真的留下了失败台账** —— 否则下面的
        # 「不含 canary」在「一条台账都没有」时**恒真**（把拉图那一跳整段删掉它照样绿）。
        failed = [n for n in notes if n.status == "failed"]
        self.assertEqual(len(failed), 1, [n.reason for n in notes])
        blob = "".join(n.reason for n in notes)
        for canary in ("https", "CANARY", CANARY_REMOTE_NAME, "accessid", "signature"):
            self.assertNotIn(canary, blob)

    def test_an_already_pending_review_order_is_not_pulled_again(self):
        """已经是「待验收」⇒ 那一跳根本不打（幂等短路在拉图**之前**）。"""
        Order.objects.filter(pk=self.order.pk).update(status=OrderStatus.PENDING_REVIEW)

        def view_pic(_request):
            raise AssertionError("不该为已经是待验收的单去打 ViewPic")

        applied = self._pull(view_pic)

        self.assertEqual(applied, 0)
        self.assertEqual(self._rows(), [])


class PicKindLiteralsTests(SimpleTestCase):
    """台账 kind **刻意复用既有的两个**（⛔ 不新增：前端那条 36 个的恰好集合网不属本线）。"""

    def test_the_two_kinds_are_the_existing_wire_values(self):
        self.assertEqual(jidanpu_member.KIND_ASSIGN, KIND_ASSIGN_LITERAL)
        self.assertEqual(jidanpu_member.KIND_STATUS_IN, KIND_STATUS_IN_LITERAL)

    def test_the_pushed_image_types_are_the_two_real_kinds(self):
        self.assertEqual(
            jidanpu_member.MEMBER_PUSH_IMG_TYPES, (IMG_ORDER_LITERAL, IMG_GROUP_QR_LITERAL),
        )

    def test_the_pulled_image_types_are_order_only(self):
        """⭐ 拉回侧**只收订单截图**（与推送侧「两种全传」不是同一个口径，⛔ 别合并）。"""
        self.assertEqual(jidanpu_member.MEMBER_PULL_IMG_TYPES, (IMG_ORDER_LITERAL,))


# ══ E-0282：`only_missing` per-pic 判重 + 推送映射 + 到图补推 ════════


def _view_pic_shell_of(keys):
    """ViewPic 业务壳（有图形态）：`src` 指向这些**远端对象键**（键提取按对象 key、丢域名）。"""
    return {
        "code": 0, "msg": {"title": "", "data": [
            {"pid": str(i), "IsOpenView": "None",
             "src": f"https://oss-synth.invalid/{key}",
             "alt": "【机器人01】上传于10-24 10:00，说明：订单截图"}
            for i, key in enumerate(keys)
        ]},
    }


def _synth_oss_key(*, tag=0):
    """远端 oss_key——**按生产真实算法构造**（M6）：`${sid}-${orderId}-${rand5}-${epoch}.${ext}`。"""
    return jidanpu_upstream_pics.build_object_key(
        sid="10001", remote_order_id=JID, ext="jpg",
        rand5=f"{12345 + tag}", epoch=1790000000000 + tag,
    )


def _inbound_key(order, *, tag):
    """本地入站键——**按生产真实算法构造**（M6）：sha256(源 URL 稳定身份) 的共享前缀键
    （与推送时随机现生成的 oss_key **两侧异形**，⛔ 不许桩上两侧同形造恒真判据）。"""
    return jidanpu_upstream_pics.build_inbound_object_key(
        order=order, img_type=IMG_ORDER_LITERAL, ext="jpg",
        src=f"https://oss-synth.invalid/upstream-shot-{tag}.jpg",
    )


class _OnlyMissingBase(_MemberPicsBase):
    """per-pic 判重那几张网的公共工具。"""

    def _ledger_with_mapping(self, mapping: dict):
        """一条「已传到下家」的成功台账行，payload 带推送映射（`_note_order_once` 落库的形状）。"""
        from apps.business.models import JidanpuSyncEvent

        return JidanpuSyncEvent.objects.create(
            direction="in", kind=KIND_ASSIGN_LITERAL, status="done",
            target_type="order", target_ref=ORDER_NO,
            reason="订单截图已传到下家（共 1 张：订单截图 1 张）",
            payload={
                "member_binding_id": self.binding.pk,
                PUSHED_KEYS: mapping,
            },
        )

    def _push(self, client, *, recorder=None):
        recorder = recorder or _OssRecorder()
        with mock.patch.object(
            jidanpu_upstream_pics, "post_to_oss", side_effect=recorder,
        ):
            return jidanpu_upstream_pics.push_order_screenshots(
                client, order=self.order, binding=self.binding, link=self.link,
                http=self.http, now=self.now, remote_order_id=JID,
                img_types=jidanpu_member.MEMBER_PUSH_IMG_TYPES,
                skip_if_remote_has_pic=False, only_missing=True,
                oss_post=recorder,
            )

    def _viewpic_calls(self, client):
        return [path for _m, path, _kw in client.calls if path == VIEW_PIC_PATH]


#: payload 里推送映射那一格的**字面量**（⛔ 不从被测常量派生，E-0024）。
PUSHED_KEYS = "pushed_pic_keys"

#: per-pic 判重跳过那句人话的**字面量**（同上）。
ALREADY_THERE_TEXT = "这张图对面那张单上已经有了（按历史推送登记判重），不重复传"


class OnlyMissingPushTests(_OnlyMissingBase):
    """⭐ B1（H1/M6/L2）：按**历史登记的 oss_key** 逐张判缺、恰好一次 ViewPic。"""

    def test_a_registered_oss_key_on_the_remote_skips_only_that_shot(self):
        """⭐ per-pic 判重（两侧异形键）：远端出现「历史登记过的 oss_key」⇒ **只**跳过那一张，
        其余照传。判别力：把判重改回「对面有任意图就整档不传」⇒ pushed 为空 ⇒ 红；
        把映射对照拿掉（恒不命中）⇒ 两张都传 ⇒ `pushed` 长度断言红（重复上传回退）。"""
        key_mapped = _inbound_key(self.order, tag=1)
        key_fresh = _inbound_key(self.order, tag=2)
        self._shot(key=key_mapped)
        self._shot(key=key_fresh)
        oss_registered = _synth_oss_key(tag=1)
        self._ledger_with_mapping({key_mapped: oss_registered})
        client = _StubClient(pic_envelope=_view_pic_shell_of([oss_registered]))

        outcome = self._push(client)

        # ⭐ 只有没登记过的那张传上去（per-pic，不是 per-order）。
        self.assertEqual([shot.object_key for shot in outcome.pushed], [key_fresh])
        self.assertFalse(outcome.failed)
        self.assertEqual(
            [(skip.object_key, skip.reason) for skip in outcome.skipped],
            [(key_mapped, ALREADY_THERE_TEXT)],
        )
        # 恰好一次 ViewPic（L2 硬判据；⛔ 不再调 remote_has_pic）。
        self.assertEqual(self._viewpic_calls(client), [VIEW_PIC_PATH])
        # 真上传的 register 恰好一笔，对象名是**推送时新生成**的（与本地键不同形）。
        payloads = self._register_payloads(client)
        self.assertEqual(len(payloads), 1)
        self.assertTrue(payloads[0]["imgListStr"].startswith(f"{OSS_HOST}/10001-{JID}-"))

    def test_a_machine_group_qr_on_the_remote_does_not_block_our_shots(self):
        """⭐ 口径②不回退：远端**只有**机器人群码（异键、无登记）⇒ 我方订单截图照传。"""
        key = _inbound_key(self.order, tag=3)
        self._shot(key=key)
        client = _StubClient(pic_envelope=_view_pic_shell_of([CANARY_REMOTE_NAME]))

        outcome = self._push(client)

        self.assertEqual([shot.object_key for shot in outcome.pushed], [key])
        self.assertEqual(outcome.skipped, ())
        self.assertEqual(self._viewpic_calls(client), [VIEW_PIC_PATH])

    def test_no_mapping_at_all_still_pushes_everything(self):
        """缺口期口径：映射行没有（本参数上线前推成功的图）⇒ 照传一次（可接受、不炸）。"""
        key = _inbound_key(self.order, tag=4)
        self._shot(key=key)
        client = _StubClient(pic_envelope=_view_pic_shell_of([_synth_oss_key(tag=9)]))

        outcome = self._push(client)

        self.assertEqual([shot.object_key for shot in outcome.pushed], [key])

    def test_everything_present_pushes_nothing_and_writes_no_ledger_row(self):
        """⭐ 全部已存在 ⇒ `pushed=()` 且无失败 ⇒ **一条台账都不写**、一次上传都不打
        （「对面已有」与「我方没传」在台账上分得开——前者零行、后者也零行但 reason 不同）。"""
        key = _inbound_key(self.order, tag=5)
        self._shot(key=key)
        oss_registered = _synth_oss_key(tag=5)
        self._ledger_with_mapping({key: oss_registered})
        client = _StubClient(pic_envelope=_view_pic_shell_of([oss_registered]))

        outcome = self._push(client)

        self.assertEqual(outcome.pushed, ())
        self.assertFalse(outcome.failed)
        self.assertTrue(outcome.already_had)
        self.assertEqual(self._register_payloads(client), [])
        self.assertEqual(self.http.gets, [], "一张都不缺 ⇒ 上传页那一跳也不打")
        self.assertEqual(self._viewpic_calls(client), [VIEW_PIC_PATH])

    def test_the_push_mapping_lands_in_the_ledger_payload(self):
        """⭐ B1 映射落库：消费 `member.assign` 推送成功 ⇒ 当次台账行 payload 带**完整**
        `{本地 object_key → 对面 oss_key}`；oss_key 取自 register 真实载荷（推送时随机现生成）。"""
        key_a = _inbound_key(self.order, tag=6)
        key_b = _inbound_key(self.order, tag=7)
        self._shot(key=key_a)
        self._shot(key=key_b)

        event = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_ASSIGN,
        )
        client = _StubClient()
        with mock.patch.object(
            jidanpu_upstream_pics, "post_to_oss", side_effect=_OssRecorder(),
        ):
            self.assertEqual(
                jidanpu_member.consume_event(event, client=client, http=self.http),
                "done",
            )

        payloads = self._register_payloads(client)
        note = self._ledger().get(status="done")
        expected = {
            key_a: payloads[0]["imgListStr"].rsplit("/", 1)[1],
            key_b: payloads[1]["imgListStr"].rsplit("/", 1)[1],
        }
        self.assertEqual(note.payload.get(PUSHED_KEYS), expected)
        # ⚠️ 夹具自证（防恒真）：两个 oss_key 都是真的推送键形状、且互不相同。
        self.assertNotEqual(expected[key_a], expected[key_b])
        for oss_key in expected.values():
            self.assertRegex(oss_key, rf"^10001-{JID}-\d{{5}}-\d{{13}}\.jpg$")

    def test_a_lost_mapping_row_degrades_to_one_more_push(self):
        """映射行丢失 ⇒ 退化为**重传一次**（可接受、不炸）——判据：远端键还在、但台账
        映射没了 ⇒ 那一张判不出 ⇒ 再传一次（不做「远端有同内容图就跳过」那种第二套判重）。"""
        key = _inbound_key(self.order, tag=8)
        self._shot(key=key)
        oss_registered = _synth_oss_key(tag=8)
        row = self._ledger_with_mapping({key: oss_registered})
        # 「映射行丢失」：把 payload 里那一格抹掉（行还在、映射没了）。
        from apps.business.models import JidanpuSyncEvent

        JidanpuSyncEvent.objects.filter(pk=row.pk).update(
            payload={"member_binding_id": row.payload["member_binding_id"]},
        )
        client = _StubClient(pic_envelope=_view_pic_shell_of([oss_registered]))

        outcome = self._push(client)

        self.assertEqual([shot.object_key for shot in outcome.pushed], [key])


class PicRePushHelperTests(_MemberPicsBase):
    """⭐ B2 helper：①式启用绑定 × 已建链 × 有图 ⇒ 排 `KIND_ASSIGN`（payload 带 repush）。"""

    def _repush_rows(self):
        return JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_ASSIGN_LITERAL, target_ref=ORDER_NO,
        )

    def test_ready_shots_of_both_kinds_queue_one_repush_event(self):
        """有图（订单截图 + 群码都算）⇒ 恰好一条 assign，payload 带 `"repush": true`。"""
        self._shot(img_type=IMG_ORDER_LITERAL)
        self._shot(img_type=IMG_GROUP_QR_LITERAL, ext="png",
                   key=f"{ACCOUNT}/orders/{ORDER_NO}/qr.repush.png")

        queued = jidanpu_member.queue_pic_repush_for_ready_screenshots(self.order)

        self.assertEqual(queued, 1)
        row = self._repush_rows().get()
        self.assertEqual(row.status, "pending")
        self.assertEqual(row.payload["member_binding_id"], self.binding.pk)
        self.assertIs(row.payload["repush"], True)
        # 再到一张（边沿触发）⇒ 在飞归并：还是同一条、不新增行。
        self._shot(key=_inbound_key(self.order, tag=11))
        queued_again = jidanpu_member.queue_pic_repush_for_ready_screenshots(self.order)
        self.assertEqual(queued_again, 1)
        self.assertEqual(self._repush_rows().count(), 1)

    def test_without_ready_shots_it_returns_zero_without_queueing(self):
        """无图 ⇒ 即刻返回 0、一行不排（「无候选即廉价返回」口径）。"""
        self.assertEqual(
            jidanpu_member.queue_pic_repush_for_ready_screenshots(self.order), 0,
        )
        self.assertEqual(self._repush_rows().count(), 0)

    def test_a_disabled_or_openapi_link_is_skipped_without_noise(self):
        """停用绑定 / ②式绑定 ⇒ 不排、也**不写** NOT_WIRED 噪声行（②式传图二期未接线）。"""
        from apps.business.models import JidanpuMemberBinding, JidanpuMemberOrderLink

        self._shot()
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        # ②式那份：mode 是唯一判据（⛔ 不看 client_id 真值）。
        openapi_user = User.objects.create_user(username="member-pics-openapi")
        openapi_binding = JidanpuMemberBinding.objects.create(
            user=openapi_user, jidanpu_base_url="https://jidanpu-openapi.test.invalid",
            account="acc-openapi", password="pw", mode="openapi",
            client_id="cid-synth", enabled=True,
        )
        JidanpuMemberOrderLink.objects.create(
            binding=openapi_binding, order=self.order, jidanpu_order_id="JDP-OPENAPI-1",
            in_hall=False,
        )

        queued = jidanpu_member.queue_pic_repush_for_ready_screenshots(self.order)

        self.assertEqual(queued, 0)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(target_ref=ORDER_NO).count(), 0,
            "不排队 ⇒ 一行都不写（含 NOT_WIRED 噪声行）",
        )

    def test_a_link_still_in_the_downstream_hall_is_skipped(self):
        """⭐ 修复轮 H1（2026-10-24，评审必修）：还挂在下家大厅（`in_hall=True`，抛出去还没
        人接）的链路 ⇒ **不排补推**——补推复用 `member.assign`，一张迟到截图把人家大厅里的
        副本撤下没有业务规则背书（「大厅档补图」口径在 E-0282 笔记里挂着待拍板）。
        判别力：把 `queue_pic_repush_for_ready_screenshots` 里的 `in_hall=False` 过滤拿掉
        ⇒ 本条红。"""
        from apps.business.models import JidanpuMemberOrderLink

        self._shot()
        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(in_hall=True)

        queued = jidanpu_member.queue_pic_repush_for_ready_screenshots(self.order)

        self.assertEqual(queued, 0)
        self.assertEqual(self._repush_rows().count(), 0)

    def test_a_withdrawn_link_is_skipped(self):
        """⭐ 修复轮 M2（同族收口）：已退成（`withdrawn_at` 非空）的链路 ⇒ 不补推——
        对方系统里那张单已经不在了，每张新图再排一条只会**烧额度**。
        判别力：把 `withdrawn_at__isnull=True` 过滤拿掉 ⇒ 本条红。"""
        from apps.business.models import JidanpuMemberOrderLink

        self._shot()
        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(
            withdrawn_at=self.now,
        )

        queued = jidanpu_member.queue_pic_repush_for_ready_screenshots(self.order)

        self.assertEqual(queued, 0)
        self.assertEqual(self._repush_rows().count(), 0)


class RepushDoesNotUnlistTests(_MemberPicsBase):
    """⭐ 修复轮 H1（2026-10-24，评审必修）的**消费侧护栏**：补推事件只传图、不带指派语义
    ⇒ 即便链路 `in_hall=True`，消费 `repush` 事件也**不排撤下**（`member.unlist`）。

    判别力：把 `_consume_assign` 里那个 `payload["repush"]` 护栏拿掉 ⇒ 第一段断言红
    （消费补推事件多出一条 unlist）。普通 assign 的撤下语义**原样保留**（对照段钉住）。
    """

    KIND_UNLIST_LITERAL = "member.unlist"

    def test_consuming_a_repush_event_on_an_in_hall_link_queues_no_unlist(self):
        from apps.business.models import JidanpuMemberOrderLink

        JidanpuMemberOrderLink.objects.filter(pk=self.link.pk).update(in_hall=True)
        self.link.refresh_from_db()
        # 直接造一条 payload 带 repush 的 assign（E-0282 的补推形态）并消费。
        repush = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_ASSIGN,
            payload={"repush": True},
        )
        # 链路已有（`ensure_remote` 走「自己已有链路」短路）且本单没有图 ⇒ 零上游。
        self.assertEqual(
            jidanpu_member.consume_event(repush, client=object(), http=None), "done",
        )
        # ⭐ 补推消费完：一条 unlist 都不许有（修复前这里会多出一条）。
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind=self.KIND_UNLIST_LITERAL, target_ref=ORDER_NO,
            ).count(),
            0,
        )

        # 对照（既有语义不改）：普通 assign（无 repush）对同一条在厅链路 ⇒ 照排撤下。
        plain = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_ASSIGN,
        )
        self.assertEqual(
            jidanpu_member.consume_event(plain, client=object(), http=None), "done",
        )
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind=self.KIND_UNLIST_LITERAL, target_ref=ORDER_NO,
            ).count(),
            1,
            "普通 assign 的撤下语义必须原样保留",
        )


class IncidentE0282RegressionTests(_MemberPicsBase):
    """⭐ E-0282 事故回归：先指派（0 图、静默跳过）→ 图后到（触发补推）→ 消费 ⇒ 图传到下家；
    再来一轮重复图 ⇒ per-pic 判重生效、**零重传**。"""

    def _store_a_late_pic(self, *, tag):
        """上家拉回一张迟到图（真实算法确定性键），回 `(对象键, PullOutcome)`。"""
        pics = jidanpu_upstream_pics.RemotePics(
            items=(
                jidanpu_upstream_pics.RemotePic(
                    src=f"https://oss-synth.invalid/late-{tag}.jpg",
                    alt="【机器人01】上传于10-24 02:10，说明：订单截图",
                ),
            ),
            rows=1,
        )
        return jidanpu_upstream_pics.store_remote_pics(
            pics, order=self.order, uploaded_by=self.user, now=self.now,
            download=lambda _src: JPEG_BODY,
        )

    def test_assign_before_pics_then_pics_arrive_then_consume_delivers_and_dedupes(self):
        # 指派当时没有图，核对原单后直接完成。
        first = jidanpu_member.queue_member_event(
            self.order, self.binding, kind=jidanpu_member.KIND_ASSIGN,
        )
        self.assertEqual(
            jidanpu_member.consume_event(first, client=object(), http=self.http), "done",
        )
        self.assertEqual(self._ledger().count(), 0)

        # ② 图后到（上家拉回）⇒ store_remote_pics 落库**并触发补推**（payload repush）。
        outcome = self._store_a_late_pic(tag=1)
        self.assertEqual(len(outcome.pulled), 1)
        stored_key = outcome.pulled[0].object_key
        repush = JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_ASSIGN_LITERAL, target_ref=ORDER_NO,
            status="pending",
        ).get()
        self.assertIs(repush.payload["repush"], True)

        # ③ 消费补推 ⇒ 图真的传到下家那张单（orderId = 本线链路远端单号）+ 映射落台账。
        client = _StubClient()
        with mock.patch.object(
            jidanpu_upstream_pics, "post_to_oss", side_effect=_OssRecorder(),
        ):
            self.assertEqual(
                jidanpu_member.consume_event(repush, client=client, http=self.http),
                "done",
            )
        payloads = self._register_payloads(client)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["orderId"], JID)
        pushed_oss = payloads[0]["imgListStr"].rsplit("/", 1)[1]
        note = self._ledger().get(status="done")
        self.assertEqual(note.payload.get(PUSHED_KEYS), {stored_key: pushed_oss})

        # ④ 再来一轮（重复到图 ⇒ 再排一条）⇒ 远端已有那张 ⇒ per-pic 判重：零重传、零台账。
        self._store_a_late_pic(tag=1)   # 同一张图：确定性键 ⇒ 幂等 existing，不再触发
        self.assertEqual(
            jidanpu_member.queue_pic_repush_for_ready_screenshots(self.order), 1,
        )
        again = JidanpuSyncEvent.objects.filter(
            direction="out", kind=KIND_ASSIGN_LITERAL, target_ref=ORDER_NO,
            status="pending",
        ).get()
        round_two = _StubClient(pic_envelope=_view_pic_shell_of([pushed_oss]))
        round_two_http = _StubHttp()   # ⚠️ 换一只干净的：self.http 记着第 ③ 轮那一跳
        notes_before = self._ledger().count()
        with mock.patch.object(
            jidanpu_upstream_pics, "post_to_oss", side_effect=_OssRecorder(),
        ):
            self.assertEqual(
                jidanpu_member.consume_event(
                    again, client=round_two, http=round_two_http,
                ),
                "done",
            )
        self.assertEqual(self._register_payloads(round_two), [], "判重生效 ⇒ 零重传")
        self.assertEqual(round_two_http.gets, [], "一张都不缺 ⇒ 上传页那一跳也不打")
        self.assertEqual(self._ledger().count(), notes_before, "零新台账行")


# ══════════════════════════════════════════════════════════════════════════════
# ⭐ **拉图每轮限流**（一轮要在预算内跑完、⛔ 不被图片拖住）
# ══════════════════════════════════════════════════════════════════════════════

#: ⭐ 一轮最多拉几张单的图（**字面量**：⛔ 不从被测常量派生，E-0024 / E-0065）。
PULL_PICS_PER_ROUND_LITERAL = 20
#: ⭐ 本类造多少张「刚完成」的单（> 上限，才验得出「超出的留到下一轮」）。
ROUND_ORDERS_LITERAL = 25


class PullPicsRoundBudgetTests(_MemberPicsBase):
    """⭐⭐ 拉图**每轮上限**：一轮最多拉 N 张单的图，超出的**留到下一轮**（⛔ 不许丢单、要留痕）。

    真机缺陷（kexing，2026-10-26 08:19 实测）：一轮里一次性命中几百张「刚完成」的单 ⇒ 逐张打
    `ViewPic` 把整轮拖到十几分钟，把下一轮的节奏挡住。三条判据：

    ① **只拉 N 张**（打在 `pull_order_screenshots` 这个**真实出口**的调用次数上，⛔ 不打 logger）；
    ② ⛔ **不许丢单**：那 25 张单的状态**全部**照样写成「待验收」——拉图限流不碰业务判据；
    ③ **超出的留到下一轮**：超出那几张各留一条**台账**（`skipped` + 人话），下一轮**真的**被补拉，
       补完就收口（⛔ 不许每轮重复打上游）。

    ⚠️ 桩打在**被查找的那个模块**上（E-0029）：`jidanpu_member._pull_remote_screenshots` 里是
    `jidanpu_upstream_pics.pull_order_screenshots(...)` ⇒ 打 `jidanpu_upstream_pics` 那一份。
    """

    def setUp(self):
        super().setUp()
        #: 额外 24 张「非终态」的单 + 链路（连同基类那张 = 25 张，超过上限 20）。
        self.remote_by_order_no = {ORDER_NO: JID}
        for index in range(ROUND_ORDERS_LITERAL - 1):
            order_no = f"202610050000000009{index:02d}"
            remote_id = f"JDP-000000000000000009{index:02d}"
            order = Order.objects.create(
                game=self.game, order_no=order_no, status=OrderStatus.IN_PROGRESS,
                hall_price="12.34", total_price="50.00", project_text="合成项目甲",
            )
            Performance.objects.create(
                order=order, emp_type=EmpType.MAIN, user=self.user,
                temp_amount=Decimal("12.34"), settle_status=SettleStatus.UNSETTLED,
            )
            JidanpuMemberOrderLink.objects.create(
                binding=self.binding, order=order, jidanpu_order_id=remote_id,
                in_hall=False,
            )
            self.remote_by_order_no[order_no] = remote_id

    # ── 工具 ────────────────────────────────────────────────────────────────

    def _rows(self):
        """25 行「已完成」的订单列表回包（本线只承重 `orderId` / `orderSta`）。"""
        return [
            {"orderId": remote_id, "orderSta": "已完成", "isThrowOrder": "N"}
            for remote_id in self.remote_by_order_no.values()
        ]

    def _round(self, *, rows, now, calls):
        """跑一轮**真** `pull_statuses`（上游与拉图两个出口都换成替身，零网络）。"""
        def _pull(_client, *, order, remote_order_id, uploaded_by, now, img_types=None,
                  skip_remote_keys=None):
            calls.append(remote_order_id)
            return jidanpu_upstream_pics.PullOutcome()

        with mock.patch.object(
                 jidanpu, "fetch_completed_orders_page",
                 return_value=jidanpu.OrderListPage(rows=list(rows), count=len(rows)),
             ), mock.patch.object(
                 jidanpu_upstream_pics, "pull_order_screenshots", side_effect=_pull,
             ):
            return jidanpu_member.pull_statuses(
                self.binding, now=now, client=mock.Mock(),
            )

    def _deferrals(self):
        """本轮「留到下一轮」的待办行（判据打在**数据库行**上，⛔ 不打日志）。"""
        return list(
            JidanpuSyncEvent.objects.filter(
                direction="in", kind=KIND_STATUS_IN_LITERAL,
                payload__member_binding_id=self.binding.pk,
                status="skipped",
            ).filter(reason__contains="留到下一轮").order_by("id")
        )

    # ── 判据 ────────────────────────────────────────────────────────────────

    def test_the_round_budget_is_twenty_orders(self):
        """⭐ 上限的**值**是 20（字面量断言：改小了性能退、改大了又被图片拖住）。"""
        self.assertEqual(jidanpu_member.PULL_PICS_PER_ROUND, 20)

    def test_a_round_pulls_only_the_budget_and_leaves_the_rest_to_the_next_round(self):
        """⭐⭐ 主判据：25 张刚完成 ⇒ 只拉 20 张、25 张单全部写「待验收」、5 张留台账；
        下一轮（没有新的完成）把剩下 5 张补拉回来，补完收口、再下一轮一张都不重复拉。
        """
        calls: list[str] = []
        applied = self._round(rows=self._rows(), now=self.now, calls=calls)

        # ② ⛔ 不许丢单：25 张的状态全部写成「待验收」（拉图限流**不碰**业务判据）。
        self.assertEqual(applied, ROUND_ORDERS_LITERAL, "拉图限流把单丢了（状态没写全）")
        self.assertEqual(
            Order.objects.filter(status=OrderStatus.PENDING_REVIEW).count(),
            ROUND_ORDERS_LITERAL,
        )
        # ① 只拉 N 张（判据打在真实出口的调用次数上，且不重复）。
        self.assertEqual(len(calls), PULL_PICS_PER_ROUND_LITERAL, "一轮拉超了名额")
        self.assertEqual(len(set(calls)), PULL_PICS_PER_ROUND_LITERAL)
        # ③ 超出的留痕（一条待办一句人话）。
        deferred = self._deferrals()
        self.assertEqual(
            len(deferred), ROUND_ORDERS_LITERAL - PULL_PICS_PER_ROUND_LITERAL,
            "超出名额的那几张没有留台账（要留痕）",
        )
        for row in deferred:
            self.assertIn("留到下一轮", row.reason)
            self.assertEqual(row.target_type, "order")
            self.assertIn(row.target_ref, self.remote_by_order_no)
        # 「名额用完」不是失败：不写 `last_error`、本轮没抛（⛔ 不许把限流报成红）。
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.last_error, "")

        # ③ 下一轮（没有新的完成）⇒ 把上一轮留下的那几张**真的**补拉回来。
        left_remote_ids = {
            self.remote_by_order_no[row.target_ref] for row in deferred
        }
        self.assertEqual(left_remote_ids - set(calls), left_remote_ids)
        calls.clear()
        applied_next = self._round(
            rows=[], now=self.now + timedelta(minutes=10), calls=calls,
        )
        self.assertEqual(applied_next, 0, "第二轮没有新的完成 ⇒ 写回 0 条")
        self.assertEqual(
            set(calls), left_remote_ids, "上一轮留下的图没有被补拉",
        )
        self.assertEqual(len(calls), len(left_remote_ids))
        # 补完就收口：待办行置 `done`（⛔ 不许下一轮再补一次）。
        self.assertEqual(self._deferrals(), [], "补完之后待办没有收口")

        # ③ 再下一轮：一张都不许重复拉（⛔ 重复打上游）。
        calls.clear()
        self._round(rows=[], now=self.now + timedelta(minutes=20), calls=calls)
        self.assertEqual(calls, [], "补过的图又被拉了一次")
