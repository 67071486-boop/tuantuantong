"""订单截图四端点的接口测试（spec §7 / §10 / §11）。

⚠️ **租户层测试必须用 `TenantTestCase`**（本仓租户表只在租户 schema 里）。
⚠️ 跨租户的否定断言**用订单号指认**，⛔ 不用主键——两个租户库的主键序列各自从 1 开始，
`filter(pk=…)` 在对方库里命中的是它自己的行（`AGENTS.md` §5-G）。
⚠️ 断言打在**真实出口**上：库里那一行、磁盘上那个文件、以及审计表的行——⛔ 不打在 `layer.msg` 之类的中间层。
"""
from decimal import Decimal
import pathlib
import tempfile
import threading
import time
import uuid
from unittest import mock

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
)
from rest_framework.test import APIRequestFactory, force_authenticate

from apps.audit.models import OperationLog
from apps.business import group_qr
from apps.business.api import screenshot_views
from apps.business.models import Game, Order, OrderScreenshot, OrderStatus
from apps.business.roles import TENANT_ADMIN
from apps.core.storage import get_storage
from tests.p5_auth_helpers import make_logged_in_client

UPLOAD_NODE = "order.upload_screenshot"
VIEW_NODE = "order.view_screenshot"
MANAGE_NODE = "order.screenshot_manage"


def _client(username, group, *nodes):
    """⚠️ **每个 client 必须用不同的组**：夹具把节点挂在「该用户所属 Group 对应的 `Role`」上
    （`tests/p5_auth_helpers.py::_grant_nodes`），而 **`Role` 是组级共享的**——
    同一组里再建第二个用户会**继承**前一个用户挂上的节点 ⇒「无权限」用例会假失败（拿到 200）。
    同理：只给 `nodes` 不给 `groups` ⇒ 循环体一次都不进 ⇒ 一个节点都没挂上（症状是 403）。
    """
    return make_logged_in_client(username=username, groups=(group,), nodes=nodes)


class ScreenshotApiTestCase(TenantTestCase):
    def setUp(self):
        self.tenant = connection.schema_name
        self.game = Game.objects.create(name="原神")
        self.order = Order.objects.create(
            game=self.game,
            order_no="20260925000000000101",
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        self.client = _client("shot-admin", "系统管理员", UPLOAD_NODE, VIEW_NODE, MANAGE_NODE)
        self.url_ticket = f"/api/business/orders/{self.order.order_no}/screenshots/ticket"
        self.url_collection = f"/api/business/orders/{self.order.order_no}/screenshots"

    # ── 助手 ────────────────────────────────────────────────────────
    #: 一个**真的** JPEG 魔数开头的最小字节串（类型判据已改成魔数，假的会落库失败）
    JPEG = b"\xff\xd8\xff" + b"fake-jpeg-body"
    PNG = b"\x89PNG\r\n\x1a\n" + b"fake-png-body"

    def _ticket(self, ext="jpg", img_type=0):
        resp = self.client.post(self.url_ticket, {"ext": ext, "img_type": img_type}, format="json")
        return resp

    def _row(self, resp):
        return OrderScreenshot.objects.get(pk=resp.data["data"]["screenshot_id"])

    def _uploaded_row(self, img_type=0, body=None):
        row = self._row(self._ticket(img_type=img_type))
        get_storage().put_bytes(row.object_key, body or self.JPEG, content_type="image/jpeg")
        return row

    def _claim(self, row, **extra):
        payload = {"screenshot_id": row.pk}
        payload.update(extra)
        return self.client.post(self.url_collection, payload, format="json")


class TicketTests(ScreenshotApiTestCase):
    def test_ticket_refuses_disallowed_extension(self):
        resp = self._ticket(ext="gif")
        # 业务失败一律 200 + 业务 code（本仓口径），⛔ 不是 400
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(OrderScreenshot.objects.count(), 0)

    def test_ticket_creates_pending_row_and_returns_upload_url(self):
        resp = self._ticket()
        self.assertEqual(resp.data["code"], 0)
        row = self._row(resp)
        self.assertEqual(row.status, "pending")
        # ⚠️ 期望值写字面值，⛔ 不引用生产常量（自指断言改了常量也不会红）。
        # ⚠️ P4 **订单截图**（`img_type=0`）一律走共享前缀，origin 段 = 本单号
        #    （这张单没有链行）。⛔ 不许弱化成 `startswith("shared-orders/")`——那样丢了
        #    「按 origin 分段」这半条契约（同一张单的图必须落进同一段）。
        self.assertTrue(
            resp.data["data"]["key"].startswith(f"shared-orders/{self.order.order_no}/")
        )
        self.assertTrue(resp.data["data"]["key"].endswith(".jpg"))
        self.assertIn("/api/media/blob/", resp.data["data"]["upload_url"])

    def test_missing_order_returns_envelope_1002(self):
        resp = self.client.post(
            "/api/business/orders/20990101000000000001/screenshots/ticket",
            {"ext": "jpg"}, format="json",
        )
        # ⚠️ 本仓把「记录不存在」也收进**统一信封**：HTTP **200** + 业务码 **1002**，
        #    ⛔ 不是 HTTP 404（`apps/core/exceptions.py` 的 Http404 分支）。
        #    写成 404 会让这条用例假红——本行实测过一次。
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["code"], 1002)


class ClaimTests(ScreenshotApiTestCase):

    def test_claim_marks_ready_with_real_size_and_type(self):
        row = self._uploaded_row()
        resp = self._claim(row, original_name="a.jpg", remark="第一张")
        self.assertEqual(resp.data["code"], 0)
        row.refresh_from_db()
        self.assertEqual(row.status, "ready")
        self.assertEqual(row.size_bytes, len(self.JPEG))
        self.assertEqual(row.remark, "第一张")
        self.assertIsNotNone(row.ready_at)

    def test_claim_refuses_when_object_not_uploaded_yet(self):
        row = self._row(self._ticket())
        resp = self._claim(row)
        self.assertEqual(resp.data["code"], 1000)
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")

    def test_claim_refuses_another_tenants_prefix(self):
        """护栏②：别的租户的对象名提交给本租户的落库接口 ⇒ 403，且那一行**原样不动**。"""
        row = self._uploaded_row()
        foreign = "someothertenant/orders/X/1.jpg"
        get_storage().put_bytes(foreign, b"x", content_type="image/jpeg")
        OrderScreenshot.objects.filter(pk=row.pk).update(object_key=foreign)
        resp = self._claim(row)
        self.assertEqual(resp.status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")

    def test_claim_refuses_dotdot_escaped_prefix(self):
        """护栏②（回归钉子）：`..` 逃逸的字符串形状**不许**过判据。"""
        row = self._uploaded_row()
        escaped = f"{self.tenant}/../someothertenant/orders/X/1.jpg"
        OrderScreenshot.objects.filter(pk=row.pk).update(object_key=escaped)
        resp = self._claim(row)
        self.assertEqual(resp.status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.status, "pending")

    def test_claim_refuses_over_size_limit_and_cleans_up(self):
        row = self._uploaded_row()
        with mock.patch("apps.business.api.screenshot_views.MAX_IMAGE_BYTES", 4):
            resp = self._claim(row)
        self.assertEqual(resp.data["code"], 1000)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNone(get_storage().head(row.object_key))

    def test_claim_refuses_a_zero_byte_object_and_cleans_up(self):
        """0 字节对象在**认领端明确拒收**（2026-09-25 复评 Minor-3）：与超限**同码同形状**
        （`code=1000`、先删对象再删行），文案与超限分开（0 字节不是「超过 18 MB」）。

        ⚠️ **为什么断言 `msg`**：不加这条判据时，0 字节对象仍会被**魔数嗅探**拒掉（读不到魔数）
        并同样清行清对象，但报「只支持 jpg / jpeg / png」⇒「被拒 + 不留行」那半不会因删掉
        `size <= 0` 变红。判据钉在两种拒绝的**区分点**（对外文案）上才算数。

        变异：删掉 `screenshot_views` 里 `if size <= 0:` 那四行 ⇒ 走嗅探分支 ⇒ `msg` 变成
        「只支持 jpg / jpeg / png」⇒ 本条以 msg 不等变红（已实测）。
        """
        row = self._row(self._ticket())
        get_storage().put_bytes(row.object_key, b"", content_type="image/jpeg")
        resp = self._claim(row)
        self.assertEqual(resp.data["code"], 1000, resp.data)
        self.assertEqual(resp.data["msg"], "图片无效或为空")
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNone(get_storage().head(row.object_key))


class GroupQrEvictionByteRetentionTests(ScreenshotApiTestCase):
    """群聊二维码每单最多一张（覆盖语义），但**共享前缀**的那份字节不许删（D-0316 / B1）。

    ⚠️ 为什么造行**手工**、⛔ 不走签发端点：签发端点产出的群码 key **现在恒为共享前缀**，
    而本用例还要钉住**升级前遗留的本租户前缀**那半边（共享 ⇒ 留字节；本租户 ⇒ 删字节，
    老行为不变）⇒ 直接建行、直接调领域内核 `group_qr.evict_previous_ready_group_qr`，
    不依赖认领那条链。

    ⚠️ 判据打在**存储的真实出口**（`get_storage().head(...)`）与**库里的行**上。
    """

    ORIGIN = "SYN-GQR-EVICT-1"

    def _manual_qr(self, name, *, shared, status=OrderScreenshot.STATUS_READY):
        """手工造一张群码行 + 真字节。

        ⚠️ 同单**不能同时存在两行 ready 群码**（部分唯一索引 `uniq_order_ready_group_qr`）
        ⇒ 需要两条时，第二条用 `pending` 造，这样才能不经过认领就调淘汰内核。
        """
        schema = self.tenant if isinstance(self.tenant, str) else self.tenant.schema_name
        prefix = f"shared-orders/{self.ORIGIN}" if shared else (
            f"{schema}/orders/{self.order.order_no}"
        )
        key = f"{prefix}/qr.{name}.jpg"
        row = OrderScreenshot.objects.create(
            order=self.order,
            object_key=key,
            uploaded_by=User.objects.get(username="shot-admin"),
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=status,
        )
        get_storage().put_bytes(key, self.JPEG, content_type="image/jpeg")
        return row, key

    def test_evicting_a_group_qr_keeps_the_shared_bytes(self):
        """共享前缀的旧群码：**行**被淘汰，**字节**留下（链上别的租户可能还在用）。"""
        old, old_key = self._manual_qr("aaaaaaaaaaaa", shared=True)
        new, _new_key = self._manual_qr(
            "bbbbbbbbbbbb", shared=True, status=OrderScreenshot.STATUS_PENDING
        )

        group_qr.evict_previous_ready_group_qr(self.order.pk, keep_pk=new.pk)

        self.assertFalse(OrderScreenshot.objects.filter(pk=old.pk).exists())
        self.assertIsNotNone(
            get_storage().head(old_key), "共享前缀的群码字节不许被覆盖动作删掉"
        )
        self.assertTrue(OrderScreenshot.objects.filter(pk=new.pk).exists())

    def test_evicting_a_group_qr_deletes_the_bytes_under_the_tenant_prefix(self):
        """本租户前缀的老群码：行与字节都删（本批**没有**改变这条老行为）。

        ⚠️ 字节删除排在**事务提交之后**（`transaction.on_commit`，评审 I2）⇒ 这里必须驱动
        提交回调才看得到删除；用 `captureOnCommitCallbacks(execute=True)`。
        """
        old, old_key = self._manual_qr("cccccccccccc", shared=False)
        new, _new_key = self._manual_qr(
            "dddddddddddd", shared=True, status=OrderScreenshot.STATUS_PENDING
        )

        with self.captureOnCommitCallbacks(execute=True):
            group_qr.evict_previous_ready_group_qr(self.order.pk, keep_pk=new.pk)

        self.assertFalse(OrderScreenshot.objects.filter(pk=old.pk).exists())
        self.assertIsNone(get_storage().head(old_key))

    def test_deferred_byte_deletion_does_not_run_when_the_batch_rolls_back(self):
        """⭐ 评审 I2：同一批后续步骤回滚 ⇒ 行回来、**字节也还在**（不会留坏图）。

        判据形态：淘汰之后在同一事务里抛异常（= 接收侧批事务后续失败）。如果把
        `storage.delete` 同步做在过去那种写法里，这里字节就没了、而行被回滚回来 ⇒ 坏图。
        """
        old, old_key = self._manual_qr("eeeeeeeeeeee", shared=False)

        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                group_qr.evict_previous_ready_group_qr(self.order.pk)
                raise RuntimeError("同批后续步骤失败")

        self.assertTrue(OrderScreenshot.objects.filter(pk=old.pk).exists())
        self.assertIsNotNone(get_storage().head(old_key), "回滚时不许删字节")

    def test_evicting_leaves_exactly_one_ready_group_qr(self):
        """覆盖语义本身不变：淘汰之后同单只剩传来那张 ready 群码。"""
        self._manual_qr("eeeeeeeeeeee", shared=True)
        new, _new_key = self._manual_qr(
            "ffffffffffff", shared=True, status=OrderScreenshot.STATUS_PENDING
        )

        group_qr.evict_previous_ready_group_qr(self.order.pk, keep_pk=new.pk)
        OrderScreenshot.objects.filter(pk=new.pk).update(
            status=OrderScreenshot.STATUS_READY
        )

        ready = OrderScreenshot.objects.filter(
            order=self.order, img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        )
        self.assertEqual(ready.count(), 1, list(ready))
        self.assertEqual(ready.get().pk, new.pk)

    def test_should_delete_object_bytes_matrix(self):
        """B1 的**决策点**单独钉一遍：⛔ 不许写成「共享前缀一律不删」。

        - `img_type=1` + 共享前缀 ⇒ **不删**；
        - `img_type=0` + 共享前缀 ⇒ **仍删**（规格 R1：任一家删则链上大家都没了）；
        - `img_type=1` + 本租户前缀 ⇒ 仍删（老群码字节只归本租户）。
        """
        shared_key = f"shared-orders/{self.ORIGIN}/x.jpg"
        # 升级前遗留的老群码形状：`<租户>/orders/<单号>/qr.<随机>.<ext>`（D-0316 不迁移）。
        local_key = f"{self.tenant}/orders/{self.order.order_no}/qr.aaaaaaaaaaaa.jpg"
        self.assertFalse(
            group_qr.should_delete_object_bytes(
                img_type=OrderScreenshot.IMG_GROUP_QR, object_key=shared_key
            )
        )
        self.assertTrue(
            group_qr.should_delete_object_bytes(
                img_type=OrderScreenshot.IMG_ORDER, object_key=shared_key
            )
        )
        self.assertTrue(
            group_qr.should_delete_object_bytes(
                img_type=OrderScreenshot.IMG_GROUP_QR, object_key=local_key
            )
        )


class OrderScreenshotLimitTests(ScreenshotApiTestCase):
    """业务方 2026-09-25 裁定：一个订单的**订单截图**（`img_type=0`）最多 **20 张**。

    ⚠️ 判据里的 20 是**字面量**（⛔ 不引用 `MAX_ORDER_SCREENSHOTS`）：两侧同源的话，
    把常量改坏这些用例仍绿——本仓反复踩过的「自指断言」。
    ⚠️ 计数的口径：`img_type=0` 的**行数**，含 `pending` 与 `ready` 两种状态；
    **硬删**（`row.delete()`）的行不计。
    ⚠️ 两处强制点：签发端点（早反馈）+ 认领端点（权威）。并发下靠**订单行锁**串行化
    ——判据见文件末尾的 `OrderScreenshotLimitConcurrencyTests`。
    """

    LIMIT = 20
    LIMIT_MSG = "单个订单最多上传20张截图，请先删除后再上传"

    @property
    def _uploader(self) -> User:
        return User.objects.get(username="shot-admin")

    def _order_screenshots(self):
        return OrderScreenshot.objects.filter(order=self.order, img_type=0)

    def _seed_ready(self, count: int) -> None:
        """用**真实端点**种 `count` 张已认领的订单截图（⛔ 不是直接 CREATE 行）。"""
        for index in range(count):
            row = self._uploaded_row()
            self.assertEqual(self._claim(row).data["code"], 0, f"第 {index + 1} 张没种上")
        self.assertEqual(self._order_screenshots().count(), count)

    def _pending_row_bypassing_ticket(self) -> OrderScreenshot:
        """绕过签发端点直接造一行 `pending`（模拟并发窗口里「两个请求都数到 19」的另一半）。"""
        key = f"{self.tenant}/orders/{self.order.order_no}/bypass-{uuid.uuid4().hex[:12]}.jpg"
        get_storage().put_bytes(key, self.JPEG, content_type="image/jpeg")
        return OrderScreenshot.objects.create(
            order=self.order, object_key=key, uploaded_by=self._uploader, img_type=0,
        )

    def _upload(self, row: OrderScreenshot) -> OrderScreenshot:
        """把对象真的写到存储层（签发只给凭证，⛔ 不落对象）。"""
        get_storage().put_bytes(row.object_key, self.JPEG, content_type="image/jpeg")
        return row

    def test_the_20th_is_accepted_and_the_21st_is_refused(self):
        self._seed_ready(19)

        twentieth = self._ticket()
        self.assertEqual(twentieth.data["code"], 0, twentieth.data)
        self.assertEqual(self._claim(self._upload(self._row(twentieth))).data["code"], 0)
        self.assertEqual(self._order_screenshots().count(), self.LIMIT)

        twenty_first = self._ticket()
        self.assertEqual(twenty_first.status_code, 200)          # 业务失败一律 200 + code
        self.assertEqual(twenty_first.data["code"], 1000)
        self.assertEqual(twenty_first.data["msg"], self.LIMIT_MSG)
        self.assertEqual(self._order_screenshots().count(), self.LIMIT)

    def test_the_claim_endpoint_is_the_authoritative_gate(self):
        """签发端点被绕过时（并发窗口 / 历史数据），**认领端点**必须再拦一次。"""
        self._seed_ready(self.LIMIT)
        extra = self._pending_row_bypassing_ticket()

        resp = self._claim(extra)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["code"], 1000)
        self.assertEqual(resp.data["msg"], self.LIMIT_MSG)
        # 被拒的那一行与它的对象都被清掉 ⇒ 计数回到 20，不会把订单永久卡在 21
        self.assertFalse(OrderScreenshot.objects.filter(pk=extra.pk).exists())
        self.assertIsNone(get_storage().head(extra.object_key))
        self.assertEqual(self._order_screenshots().count(), self.LIMIT)

    def test_group_qr_is_not_subject_to_the_limit(self):
        """`img_type=1`（群聊二维码）**不受**这 20 约束，它维持「每单最多 1 张」。"""
        self._seed_ready(self.LIMIT)
        # 自证夹具真的到了满额：此刻连**签发**订单截图都不给
        self.assertEqual(self._ticket().data["code"], 1000)

        qr_ticket = self._ticket(img_type=1)
        self.assertEqual(qr_ticket.data["code"], 0, qr_ticket.data)
        qr = self._row(qr_ticket)
        self.assertEqual(self._claim(self._upload(qr)).data["code"], 0)
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order=self.order, img_type=1, status=OrderScreenshot.STATUS_READY
            ).count(),
            1,
        )

    def test_a_hard_deleted_row_frees_a_slot(self):
        """硬删一张之后必须**可以**再传一张（计数把删掉的行放过）。"""
        self._seed_ready(self.LIMIT)
        self.assertEqual(self._ticket().data["code"], 1000)

        victim = self._order_screenshots().order_by("pk").first()
        deleted = self.client.delete(f"/api/business/orders/screenshots/{victim.pk}")
        self.assertEqual(deleted.data["code"], 0)

        again = self._ticket()
        self.assertEqual(again.data["code"], 0, again.data)
        self.assertEqual(self._claim(self._upload(self._row(again))).data["code"], 0)
        self.assertEqual(self._order_screenshots().count(), self.LIMIT)

    def test_a_12mb_order_screenshot_is_still_accepted(self):
        """两条线的上限不许混：订单截图在 10~18 MB 之间仍放行（10 MB 只管头像 / 收款码）。"""
        body = b"\xff\xd8\xff" + b"0" * (12 * 1024 * 1024 - 3)
        row = self._uploaded_row(body=body)
        resp = self._claim(row)
        self.assertEqual(resp.data["code"], 0, resp.data)
        row.refresh_from_db()
        self.assertEqual(row.status, OrderScreenshot.STATUS_READY)
        self.assertEqual(row.size_bytes, 12 * 1024 * 1024)


class ListTests(ScreenshotApiTestCase):
    def test_list_returns_ready_rows_only_with_a_signed_url(self):
        pending = self._row(self._ticket())
        ready = self._row(self._ticket("png"))
        get_storage().put_bytes(ready.object_key, self.PNG, content_type="image/png")
        self.client.post(self.url_collection, {"screenshot_id": ready.pk}, format="json")

        resp = self.client.get(self.url_collection)
        self.assertEqual(resp.data["code"], 0)
        items = resp.data["data"]["items"]
        self.assertEqual([i["id"] for i in items], [ready.pk])
        self.assertIn("/api/media/blob/", items[0]["url"])
        self.assertNotIn(pending.pk, [i["id"] for i in items])

    def test_preview_uses_thumbnail_for_all_order_images_and_original_for_qr(self):
        small = self._row(self._ticket("png"))
        boundary = self._row(self._ticket("png"))
        large = self._row(self._ticket("png"))
        unknown = self._row(self._ticket("png"))
        qr = self._row(self._ticket("png", img_type=1))
        sizes = {small.pk: 100, boundary.pk: 16384, large.pk: 16385,
                 unknown.pk: 100, qr.pk: 16385}
        for row in (small, boundary, large, unknown, qr):
            body = self.PNG + b"x" * (sizes[row.pk] - len(self.PNG))
            get_storage().put_bytes(row.object_key, body, content_type="image/png")
            self.assertEqual(self._claim(row).data["code"], 0)
        unknown.size_bytes = 0
        unknown.save(update_fields=["size_bytes"])
        items = self.client.get(self.url_collection).data["data"]["items"]
        by_id = {item["id"]: item for item in items}
        for row in (small, boundary, large, unknown):
            self.assertEqual(
                by_id[row.pk]["thumbnail_url"],
                by_id[row.pk]["url"] + "?imageMogr2/thumbnail/300x/format/jpg/rquality/80",
            )
        self.assertEqual(by_id[qr.pk]["thumbnail_url"], by_id[qr.pk]["url"])

    def test_created_at_is_rendered_in_the_project_timezone(self):
        """E-0238：截图列表的 `created_at` 是**项目时区**（`+08:00`）的墙钟，不是 UTC。

        与反馈列表同一根因（显示端点裸 `.isoformat()`）：桌面截图管理页此前靠
        浏览器时区自救，东八区以外整点平移。判据：UTC 02:00 ⇒ 载荷 10:00+08:00。
        """
        from datetime import datetime, timezone as dt_timezone

        ready = self._row(self._ticket("png"))
        get_storage().put_bytes(ready.object_key, self.PNG, content_type="image/png")
        self.client.post(self.url_collection, {"screenshot_id": ready.pk}, format="json")
        OrderScreenshot.objects.filter(pk=ready.pk).update(
            created_at=datetime(2026, 10, 12, 2, 0, 0, tzinfo=dt_timezone.utc)
        )
        resp = self.client.get(self.url_collection)
        items = resp.data["data"]["items"]
        self.assertEqual(items[0]["created_at"], "2026-10-12T10:00:00+08:00")


class DeleteTests(ScreenshotApiTestCase):
    #: 操作人的**昵称**（合成值）——与登录账号 `shot-admin` **刻意不同**，
    #: 好让「正文用的是昵称还是账号」这条判据有判别力。
    OPERATOR_NICKNAME = "测试昵称甲"

    def test_delete_removes_row_object_and_writes_an_audit_row(self):
        row = self._row(self._ticket())
        get_storage().put_bytes(row.object_key, self.JPEG, content_type="image/jpeg")
        self.client.post(self.url_collection, {"screenshot_id": row.pk}, format="json")
        key = row.object_key
        operator = User.objects.get(username="shot-admin")
        operator.first_name = self.OPERATOR_NICKNAME
        operator.save(update_fields=["first_name"])

        resp = self.client.delete(f"/api/business/orders/screenshots/{row.pk}")
        self.assertEqual(resp.data["code"], 0)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNone(get_storage().head(key))
        logs = OperationLog.objects.filter(
            action="order.delete_screenshot", order_no=self.order.order_no
        )
        # ⭐ 判据是**逐字**正文（MUST-FIX A）+
        # **操作人主键**——修前这一刻写的是 `actor=request.user`（User 对象）且**没有**
        # `actor_id` ⇒ 走机器分支 ⇒ 实测 `summary='删除订单截图：<单号>'`（**一个人名都没有**），
        # 而「有一行」这条判据**照样绿**（这就是它曾被漏掉的原因）。
        self.assertEqual(logs.count(), 1)
        log = logs.get()
        self.assertEqual(
            log.summary,
            f"【{self.OPERATOR_NICKNAME}】删除订单截图：{self.order.order_no}",
        )
        # 两个方向都钉：正文里是**昵称**、且**不是**登录账号。
        self.assertNotIn("shot-admin", log.summary)
        # 人为行的主键 —— 修前这里是 `None`（`record()` 当时会把漏传静默当成机器行）。
        self.assertEqual(log.actor_id, operator.pk)
        self.assertEqual(log.actor, "shot-admin")
        self.assertEqual(log.actor_name, self.OPERATOR_NICKNAME)


class NodeGateTests(ScreenshotApiTestCase):
    """四个/三类节点**各一条拒**（放由上面的用例覆盖）。"""

    def test_upload_node_is_required_for_ticket(self):
        client = _client("shot-nobody", "客服", VIEW_NODE)
        resp = client.post(self.url_ticket, {"ext": "jpg"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_view_node_is_required_for_list(self):
        client = _client("shot-viewer-none", "财务", UPLOAD_NODE)
        resp = client.get(self.url_collection)
        self.assertEqual(resp.status_code, 403)

    def test_manage_node_is_required_for_delete(self):
        row = self._row(self._ticket())
        client = _client("shot-no-manage", "代练员", UPLOAD_NODE, VIEW_NODE)
        resp = client.delete(f"/api/business/orders/screenshots/{row.pk}")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(OrderScreenshot.objects.filter(pk=row.pk).exists())

    def test_anonymous_is_401(self):
        from django.test import Client

        resp = Client(SERVER_NAME="tenant.test.com").get(self.url_collection)
        self.assertEqual(resp.status_code, 401)


class RealBoundaryTests(ScreenshotApiTestCase):
    """spec §11 要求「拿**真实**边界值打接口」——⛔ 不是把上限 mock 成 4。

    这两条会真的写 18 MB 到 `MEDIA_ROOT`（`.tools/media`，不进 git）。慢一点，但只有这样
    「把 `MAX_IMAGE_BYTES` 改成 1 MB」这类改坏才会红。
    """

    #: ⚠️ **字面量**，⛔ 不用 `MAX_IMAGE_BYTES`：两侧同源的话，把常量改成 1 MB 这两条仍绿
    #: （复评指出的自指夹具）。
    EIGHTEEN_MIB = 18 * 1024 * 1024

    def test_exactly_18mb_is_accepted(self):
        body = b"\xff\xd8\xff" + b"0" * (self.EIGHTEEN_MIB - 3)
        row = self._uploaded_row(body=body)
        self.assertEqual(self._claim(row).data["code"], 0)
        row.refresh_from_db()
        self.assertEqual(row.status, "ready")
        self.assertEqual(row.size_bytes, self.EIGHTEEN_MIB)

    def test_one_byte_over_18mb_is_rejected(self):
        body = b"\xff\xd8\xff" + b"0" * (self.EIGHTEEN_MIB - 2)   # 18 MB + 1
        row = self._uploaded_row(body=body)
        resp = self._claim(row)
        self.assertEqual(resp.data["code"], 1000)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNone(get_storage().head(row.object_key))


class ImageTypeSniffTests(ScreenshotApiTestCase):
    """类型判据 = **魔数**（⛔ 不看 `Content-Type`、也不信按扩展名推出来的值）。"""

    def test_non_image_bytes_named_jpg_are_rejected(self):
        row = self._uploaded_row(body=b"GIF89a-not-an-image")   # 扩展名是 .jpg，字节不是图片
        resp = self._claim(row)
        self.assertEqual(resp.data["code"], 1000)
        self.assertFalse(OrderScreenshot.objects.filter(pk=row.pk).exists())
        self.assertIsNone(get_storage().head(row.object_key))

    def test_png_magic_is_accepted_and_recorded(self):
        row = self._uploaded_row(body=self.PNG)
        self.assertEqual(self._claim(row).data["code"], 0)
        row.refresh_from_db()
        # 记录的是**嗅探出来**的类型，不是客户端给的 Content-Type
        self.assertEqual(row.content_type, "image/png")


class IssuerScopeTests(ScreenshotApiTestCase):
    """护栏③：认领只认**自己签发**的那一行（⛔ 不是同租户谁都能认领）。"""

    def test_cannot_claim_a_row_issued_by_someone_else(self):
        other = _client("shot-other", "审核人", UPLOAD_NODE, VIEW_NODE)
        resp = other.post(self.url_ticket, {"ext": "jpg"}, format="json")
        foreign_row = OrderScreenshot.objects.get(pk=resp.data["data"]["screenshot_id"])
        get_storage().put_bytes(foreign_row.object_key, self.JPEG, content_type="image/jpeg")

        # 主 client（同租户、持上传节点）拿这个 pk 去认领 ⇒ 必须拒，且那一行仍 pending
        resp = self._claim(foreign_row)
        self.assertEqual(resp.data["code"], 1000)
        foreign_row.refresh_from_db()
        self.assertEqual(foreign_row.status, "pending")


class ObjectNameShapeTests(ScreenshotApiTestCase):
    def test_group_qr_key_uses_the_qr_prefix(self):
        """⭐ D-0316：群码除 `qr.` 那一段之外，**前缀也是共享段**（随订单同步）。"""
        resp = self._ticket(img_type=1)
        key = resp.data["data"]["key"]
        self.assertRegex(key, r"/qr\.[0-9a-f]{12}\.jpg$")
        self.assertTrue(
            key.startswith(f"shared-orders/{self.order.order_no}/qr."), key
        )


class OrderScreenshotLimitConcurrencyTests(TransactionTestCase):
    """两个**同时**到达的第 20 张签发请求：行锁必须把「数数 + 落行」串起来。

    ⚠️ **为什么必须 `TransactionTestCase` + 每线程 `schema_context`**：见
    `tests/test_claim_concurrency.py` 的模块 docstring（`TestCase` 把每条测试包在事务里，
    其他线程看不到未提交夹具；线程各有独立连接，默认落在 `public` schema）。
    `django_tenants` 没有 `TenantTransactionTestCase`（读码：`django_tenants/test/cases.py`
    只有 `TenantTestCase` / `FastTenantTestCase` / `SubfolderTenantTestCase`），
    所以租户夹具照 `TenantTestCase.setUpClass` 自己搭。

    ⚠️ **同步点为什么打在 `_order_screenshot_count` 上**：它是「**已经拿到订单行锁**、
    还没落行」那一刻的承重调用（实现里锁在它之前）。把先到的那条在这里把临界区撑开 ~1 秒，
    后到者才有**必然**的重叠窗口。⛔ 不这么做的话，两条请求会被调度成事实串行，
    用例会静默退化成「又测了一遍普通边界」——**绿的并发测试等于没测并发**
    （同族实测见 `test_claim_concurrency.py` 的模块 docstring）。
    ⚠️ 这个同步点依赖实现**确实**先加锁再数数：把锁删掉 ⇒ 两条都数到 19 ⇒ 21 行 / 两个成功
    ⇒ 本用例红；把锁挪到数数之后 ⇒ 后到者仍能在窗口里数到 19 ⇒ 同样红。
    ⚠️ **本用例证明什么**：① 两个并发签发里**恰好一个**成功、另一个拿到 1000 + 契约 msg；
    ② 库里 `img_type=0` 的行数**不超过 20**。它**不**覆盖跨进程 / 多副本部署。
    """

    TENANT_SCHEMA = "test"
    LIMIT_MSG = "单个订单最多上传20张截图，请先删除后再上传"
    #: 先到者把临界区撑开的时长；正常应远小于它（后到者要等这一次提交）
    _WINDOW_SECONDS = 1.0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command(
            "migrate_schemas", schema_name="public", interactive=False, verbosity=0
        )
        cls.tenant = get_tenant_model()(schema_name=cls.TENANT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(
            tenant=cls.tenant, domain="tenant.test.com"
        )
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def test_only_one_of_two_concurrent_tickets_gets_through(self):
        game = Game.objects.create(name="原神")
        order = Order.objects.create(
            game=game,
            order_no="20260925000000000901",
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        # 持「系统管理员」职位 ⇒ 节点判据短路为全目录（不另造 RBAC 目录，本用例测的是锁）
        group, _ = Group.objects.get_or_create(name=TENANT_ADMIN)
        uploader = User.objects.create_user(username="limit-racer", password="pw-limit-racer")
        uploader.groups.add(group)
        for index in range(19):
            OrderScreenshot.objects.create(
                order=order,
                object_key=f"{self.TENANT_SCHEMA}/orders/{order.order_no}/seed-{index}.jpg",
                uploaded_by=uploader,
                img_type=OrderScreenshot.IMG_ORDER,
                status=OrderScreenshot.STATUS_READY,
            )
        order_pk, order_no, user_pk = order.pk, order.order_no, uploader.pk

        factory = APIRequestFactory()
        # ⚠️ `ScreenshotTicketView` 是 `GenericViewSet` ⇒ `as_view()` 必须给动作映射
        #    （⛔ 不给会抛 `TypeError: The actions argument must be provided`）
        view = screenshot_views.ScreenshotTicketView.as_view({"post": "issue_ticket"})
        real_count = screenshot_views._order_screenshot_count
        first_counted = threading.Event()
        counting_gate = threading.Lock()
        state = {"first": True}
        results: dict[str, dict] = {}

        def _count_with_a_wide_critical_section(order_id, *args, **kwargs):
            value = real_count(order_id, *args, **kwargs)
            with counting_gate:
                am_first = state["first"]
                state["first"] = False
            if am_first:
                # 此刻本线程**已持有订单行锁**（实现里锁在数数之前）、且还没落行
                first_counted.set()
                time.sleep(self._WINDOW_SECONDS)
            return value

        def _request_ticket(tag: str) -> None:
            connections.close_all()
            with schema_context(self.TENANT_SCHEMA):
                try:
                    request = factory.post(
                        f"/api/business/orders/{order_no}/screenshots/ticket",
                        {"ext": "jpg", "img_type": 0}, format="json",
                    )
                    force_authenticate(request, user=User.objects.get(pk=user_pk))
                    results[tag] = view(request, order_no=order_no).data
                except Exception as exc:      # noqa: BLE001 —— 分类要落到结果里
                    results[tag] = {"error": repr(exc)}
                finally:
                    connections.close_all()

        def _second() -> None:
            # ⚠️ 后到者必须**等先到者真进了临界区**才发请求（否则会被调度成事实串行）
            first_counted.wait(timeout=10)
            _request_ticket("second")

        with mock.patch(
            "apps.business.api.screenshot_views._order_screenshot_count",
            side_effect=_count_with_a_wide_critical_section,
        ):
            threads = [
                threading.Thread(target=_request_ticket, args=("first",)),
                threading.Thread(target=_second),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        # ⓪ 哨兵：两条线程都跑完、都留下了结果（样本缺失时下面的断言也会成立）
        self.assertFalse([t.name for t in threads if t.is_alive()], "有线程没在 30 秒内退出")
        self.assertEqual(set(results), {"first", "second"}, f"有线程没留下结果: {results}")

        # ① 恰好一个成功（没有锁时两个都会成功 ⇒ 这条会红）
        codes = {tag: payload.get("code") for tag, payload in results.items()}
        winners = [tag for tag, code in codes.items() if code == 0]
        self.assertEqual(len(winners), 1, f"必须恰好一个成功: {results}")

        # ② 输的那个拿到的是 1000 + 契约文案（不是 500、不是静默成功）
        loser = next(tag for tag in codes if tag != winners[0])
        self.assertEqual(codes[loser], 1000, f"{results}")
        self.assertEqual(results[loser].get("msg"), self.LIMIT_MSG, f"{results}")

        # ③ 真实出口：库里最多 20 行（没有锁时这里是 21）
        self.assertEqual(
            OrderScreenshot.objects.filter(
                order_id=order_pk, img_type=OrderScreenshot.IMG_ORDER
            ).count(),
            20,
        )


class OrderScreenshotGroupQrConcurrencyTests(TransactionTestCase):
    """两条**同时**到达的群聊二维码认领：行锁把「读旧 ready 行 + 淘汰 + 落新 ready」串起来。

    ## 这把锁防的是「二维码淘汰的 read-then-delete」，**不是**计数

    认领**不改变行数**（同一行 `pending → ready`，是 `UPDATE` 不是 `INSERT`）⇒ 它跟「每单 20 张」
    的计数**无关**（评审实测：去掉认领侧的 `select_for_update()`，`tests.test_order_screenshots_api`
    的 27 条仍全绿）。它真正防的是 `_evict_previous_group_qr` 的 read-then-delete 竞态：
    两条并发认领（各认领自己签发的 `img_type=1` 行）时，两边**都读不到**对方的「已就绪二维码」
    （都还没提交）⇒ 两边都把自己的行落成 `ready` ⇒ 每单 2 张二维码，打破 D-0151 的
    「每单最多 1 张二维码」。本用例就是这条的判据（评审的临时探针：有锁 1 行 / 无锁 2 行）。

    ## 同步点怎么造（⛔ 不靠 sleep 碰运气）

    两段栅栏，都是**结构性**的、不是时间窗口：

    1. **起跑线**（`start_line`，2 个线程）：两条线程都先完成连接/schema 准备再对齐
       ⇒ 保证「两条都在飞」是构造出来的（同 `test_claim_concurrency.py` 起的起跑线）；
    2. **淘汰栅栏**（`rendezvous`，2 个线程）：把 `_evict_previous_group_qr` 包一层——
       **先真的调用它**（淘汰照常生效，本用例不放宽任何行为），再在栅栏上对齐，
       然后才回到调用方去 `row.save(READY)`。
       ⇒「读旧 ready 行」这一段**必然**全部先于任何一次「落 ready」。

    于是两种实现下的终态都是**结构性决定**的：

    - **有 `select_for_update()`**：只有一个线程能进临界区，另一个堵在订单行锁上
      ⇒ 栅栏**必然超时**（`BrokenBarrierError`，本用例显式接住）⇒ 先到者淘汰后提交；
      后到者拿到锁后再淘汰它 ⇒ 终态 **1 行 ready**；
    - **去掉它**：两条都进得来 ⇒ 栅栏**必然放行** ⇒ 各自落 ready ⇒ 终态 **2 行 ready** ⇒ 本用例红。

    ⚠️ 「栅栏超时」在有锁那一侧是**必然事件**（另一条线程根本到不了栅栏），不是「机器慢」的兜底；
    反过来说，**无锁那一侧**只在「另一条线程 2 秒内还没走到淘汰栅栏」时才会假绿——起跑线栅栏
    已把这种可能压到「本地两条线程各走几步 DB 却超过 2 秒」，实测 6 次均如期红（见任务报告）。
    ⚠️ **本用例证明什么、不证明什么**：证明本进程内两条线程 + 两条独立连接下「每单最多 1 张
    二维码」成立；⛔ **不**覆盖跨进程 / 多副本部署（那是另一回事，同 `OrderScreenshotLimitConcurrencyTests`）。
    """

    TENANT_SCHEMA = "test"
    #: 起跑线超时（宽松：只用来把「线程没起来」变成响亮失败，不承载判据）
    _START_LINE_SECONDS = 10.0
    #: 淘汰栅栏超时：**有锁时必然超时**（另一条线程被锁挡在栅栏之外）；无锁时两线程毫秒级到达
    _RENDEZVOUS_SECONDS = 2.0
    #: 真的 JPEG 魔数开头（认领的类型判据是魔数，假的会在 `sniff_image_type` 那步退出）
    JPEG = b"\xff\xd8\xff" + b"fake-jpeg-body"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command(
            "migrate_schemas", schema_name="public", interactive=False, verbosity=0
        )
        cls.tenant = get_tenant_model()(schema_name=cls.TENANT_SCHEMA)
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(
            tenant=cls.tenant, domain="tenant.test.com"
        )
        cls.domain.save()
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        # ⛔ 别往真实 `.tools/media` 写：本用例要真的落两个对象（`head` / `sniff` / 淘汰删除
        #    都走存储层），临时根目录用完即删（同 `test_media_storage._TempMediaRootMixin`）。
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = override_settings(MEDIA_ROOT=pathlib.Path(tmp.name))
        patcher.enable()
        self.addCleanup(patcher.disable)

    def test_two_concurrent_qr_claims_leave_exactly_one_ready_row(self):
        game = Game.objects.create(name="原神")
        order = Order.objects.create(
            game=game,
            order_no="20260925000000000902",
            status=OrderStatus.IN_PROGRESS,
            hall_price=Decimal("88.00"),
        )
        # 持「系统管理员」职位 ⇒ 节点判据短路为全目录（本用例测的是锁，不另造 RBAC 目录）
        group, _ = Group.objects.get_or_create(name=TENANT_ADMIN)
        uploader = User.objects.create_user(username="qr-racer", password="pw-qr-racer")
        uploader.groups.add(group)

        # 顺序签发两张**同一个用户**的 pending 二维码，并把两个对象真的写进存储
        # （认领时 `uploaded_by` 必须对上；对象不在 ⇒ 在 `head` 那步就以 1000 退出、测不到淘汰）。
        storage = get_storage()
        pending = []
        for index in range(2):
            row = OrderScreenshot.objects.create(
                order=order,
                object_key=(
                    f"{self.TENANT_SCHEMA}/orders/{order.order_no}/qr.seed{index}.jpg"
                ),
                uploaded_by=uploader,
                img_type=OrderScreenshot.IMG_GROUP_QR,
                status=OrderScreenshot.STATUS_PENDING,
            )
            storage.put_bytes(row.object_key, self.JPEG, content_type="image/jpeg")
            pending.append(row)
        order_pk, order_no = order.pk, order.order_no
        row_pks = [row.pk for row in pending]
        user_pk = uploader.pk

        factory = APIRequestFactory()
        # ⚠️ `ScreenshotCollectionView` 是 `GenericViewSet` ⇒ `as_view()` 必须给动作映射
        view = screenshot_views.ScreenshotCollectionView.as_view({"post": "claim"})
        real_evict = screenshot_views._evict_previous_group_qr
        start_line = threading.Barrier(2)
        rendezvous = threading.Barrier(2)
        #: 栅栏结局（`released` / `timeout`）：只为让失败信息说清当时走的哪条路
        rendezvous_outcomes: list[str] = []
        results: dict[str, dict] = {}

        def _evict_then_rendezvous(row):
            real_evict(row)                     # 淘汰照常发生（⛔ 不用 mock 掉它）
            try:
                rendezvous.wait(timeout=self._RENDEZVOUS_SECONDS)
                rendezvous_outcomes.append("released")
            except threading.BrokenBarrierError:
                rendezvous_outcomes.append("timeout")

        def _request_claim(tag: str, row_pk: int) -> None:
            connections.close_all()
            with schema_context(self.TENANT_SCHEMA):
                try:
                    start_line.wait(timeout=self._START_LINE_SECONDS)
                    request = factory.post(
                        f"/api/business/orders/{order_no}/screenshots",
                        {"screenshot_id": row_pk}, format="json",
                    )
                    force_authenticate(request, user=User.objects.get(pk=user_pk))
                    results[tag] = view(request, order_no=order_no).data
                except Exception as exc:      # noqa: BLE001 —— 分类要落到结果里
                    results[tag] = {"error": repr(exc)}
                finally:
                    connections.close_all()

        with mock.patch(
            "apps.business.api.screenshot_views._evict_previous_group_qr",
            side_effect=_evict_then_rendezvous,
        ):
            threads = [
                threading.Thread(target=_request_claim, args=("first", row_pks[0])),
                threading.Thread(target=_request_claim, args=("second", row_pks[1])),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        # ⓪ 哨兵：两条线程都跑完、都留下了结果、栅栏被两次走到（样本缺失时下面的断言也会成立）
        self.assertFalse([t.name for t in threads if t.is_alive()], "有线程没在 30 秒内退出")
        self.assertEqual(set(results), {"first", "second"}, f"有线程没留下结果: {results}")
        self.assertEqual(len(rendezvous_outcomes), 2, f"淘汰栅栏被走到的次数不对: {rendezvous_outcomes}")

        # ① 两条认领都成功（淘汰是**覆盖**语义，不是拒绝；失败信息带上栅栏结局便于归因）
        codes = {tag: payload.get("code") for tag, payload in results.items()}
        self.assertEqual(
            set(codes.values()), {0}, f"两条认领都该成功: {results}；栅栏={rendezvous_outcomes}"
        )

        # ② 真实出口：库里**恰好 1 行** ready 的群聊二维码
        #    —— 去掉认领侧的 `select_for_update()` 时这里是 **2**（本用例的红点）
        ready = OrderScreenshot.objects.filter(
            order_id=order_pk,
            img_type=OrderScreenshot.IMG_GROUP_QR,
            status=OrderScreenshot.STATUS_READY,
        ).count()
        self.assertEqual(
            ready, 1, f"「每单最多 1 张二维码」被打破：{ready} 行；栅栏={rendezvous_outcomes}"
        )
