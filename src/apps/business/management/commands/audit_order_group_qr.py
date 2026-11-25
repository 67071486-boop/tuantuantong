"""审计 / 去重「每单多张 `ready` 群二维码」——**默认 dry-run**（计划 Rev.2 Task 5）。 ## 什么时候用 `0092_orderscreenshot_uniq_order_ready_group_qr` 会加一条**部分唯一索引** （`img_type=1 AND status='ready'` ⇒ 每单最多一张）。加索引前必须先把历史重复行清掉，否则迁移本身会失败。⛔ 但**对象存储里的字节不由迁移删**：行删了、对象还在 = 永久孤儿。 ⇒ 上线顺序（每台租户机、**迁前**）： manage.py audit_order_group_qr --schema=<schema>                              # 先看
manage.py audit_order_group_qr --schema=<schema> --execute --delete-objects   # 再清然后才 migrate_schemas --tenant --schema=<schema> ## 口径 - **只碰 `img_type=1 AND status='ready'`**（群二维码）；⛔ `img_type=0` 的订单截图 **一行都不动**（121 单 / 1,534 张超标存量是业务方 明确决定不动的残差）； - 每单保留 **`max(id)`**（最新那一张），其余行删掉；重复执行幂等（第二轮命中 0）；
- `--delete-objects`：**行先删**，事务**提交后**（`transaction.on_commit`）才删对象 —— 顺序反了就会出现「行没了、对象还在、谁也找不到」； ⚠️ **只删本租户前缀的字节**（D-0316）：共享前缀 `shared-orders/…` 的群码字节链上别的租户可能还在用 ⇒ 行照删、字节不删（判据唯一出处 `group_qr.should_delete_object_bytes`）； - `--limit N`：本轮最多处理 N 个**重复订单**（默认 20）；大租户分批跑； - `--schema`：只处理这一个租户；不给则遍历**全部非 public 租户**（business 是 TENANT_APP， public 下根本没有 `business_orderscreenshot` ⇒ 必须逐租户进 `tenant_context`，照 `purge_pending_screenshots` 的同一套遍历）。"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction
from django_tenants.utils import (
    get_public_schema_name,
    get_tenant_model,
    tenant_context,
)

from apps.business import group_qr
from apps.business.models import OrderScreenshot
from apps.core.storage import get_storage


class Command(BaseCommand):
    help = "审计/去重每单多张 ready 群二维码（默认 dry-run；只处理 img_type=1）"

    def add_arguments(self, parser):
        parser.add_argument("--schema", default="", help="只处理这一个租户 schema（默认：全部）")
        parser.add_argument("--execute", action="store_true", help="真的删行（默认只 dry-run）")
        parser.add_argument(
            "--delete-objects", action="store_true",
            help="行删除提交后再删对象（必须在迁移前跑，否则对象成永久孤儿）",
        )
        parser.add_argument(
            "--limit", type=int, default=20, help="本轮最多处理 N 个重复订单（默认 20）",
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1:
            # ⛔ 0 / 负数会让「一轮什么都没干」看起来像「清干净了」。
            raise CommandError("--limit 必须 ≥ 1")

        tenants = get_tenant_model().objects.exclude(
            schema_name=get_public_schema_name()
        )
        only = options["schema"]
        if only:
            tenants = tenants.filter(schema_name=only)
            if not tenants.exists():
                raise CommandError(f"没有这个 schema：{only}")

        for tenant in tenants.order_by("schema_name"):
            # ⚠️ 必须逐个进 tenant_context：租户表只在各自 schema 里（见模块 docstring）。
            with tenant_context(tenant):
                self._run_for_tenant(tenant.schema_name, options)

    def _run_for_tenant(self, schema_name: str, options) -> None:
        rows = list(
            OrderScreenshot.objects.filter(
                img_type=OrderScreenshot.IMG_GROUP_QR,
                status=OrderScreenshot.STATUS_READY,
            )
            .values("order_id")
            .annotate(n=models.Count("id"))
            .filter(n__gt=1)
            .order_by("order_id")[: options["limit"]]
        )
        extra = sum(row["n"] - 1 for row in rows)
        self.stdout.write(
            "%s %s orders=%d extra_rows=%d" % (
                "execute" if options["execute"] else "dry-run",
                schema_name, len(rows), extra,
            )
        )
        if not options["execute"] or not rows:
            return

        deleted = 0
        with transaction.atomic():
            for row in rows:
                rows_of_order = list(
                    OrderScreenshot.objects.filter(
                        order_id=row["order_id"],
                        img_type=OrderScreenshot.IMG_GROUP_QR,
                        status=OrderScreenshot.STATUS_READY,
                    ).order_by("-id").values_list("id", "object_key")
                )
                drop = rows_of_order[1:]          # 保留 max(id)
                if not drop:
                    continue
                OrderScreenshot.objects.filter(
                    pk__in=[pk for pk, _key in drop],
                ).delete()
                deleted += len(drop)
                if options["delete_objects"]:
                    # ⚠️ 先删行、**提交后**再删对象：顺序反了 = 行删失败时对象已没了。 ⭐ D-0316（评审 B1）：**只删本租户前缀**的字节 —— 共享前缀的群码字节
                    # 链上别的租户可能还在用（行照删、字节不删）。⛔ 别写成"群码一律不删"：本租户前缀的老群码仍删（本批没有改变那条老行为）。
                    keys = [
                        key for _pk, key in drop
                        if group_qr.should_delete_object_bytes(
                            img_type=OrderScreenshot.IMG_GROUP_QR, object_key=key
                        )
                    ]
                    transaction.on_commit(lambda keys=keys: self._delete_objects(keys))
        self.stdout.write(f"{schema_name} deleted={deleted}")

    def _delete_objects(self, keys: list[str]) -> None:
        storage = get_storage()
        for key in keys:
            try:
                storage.delete(key)
            except Exception as exc:  # noqa: BLE001 —— 对象删失败不该让这一轮整体炸
                # 行已经删了 ⇒ 这次失败只会留下孤儿；如实报出来，交给运维按 key 清。
                self.stderr.write(f"删对象失败：{key}（{type(exc).__name__}）")
