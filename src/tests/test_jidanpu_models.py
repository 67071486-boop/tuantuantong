"""`Jidanpu*` 三张表的结构与默认值（spec §4.1–§4.4；第四张 `JidanpuUserLink` 已删，见下）。

⚠️ 本期**只建表**：同步引擎一行都不写（见计划「范围」）。所以本文件钉的是
「表真的在**租户 schema** 里」「默认值与唯一约束就是 spec 写的那几个」。
⚠️ 判据打**数据库行**（`AGENTS.md` §5-G）：用 `IntegrityError` / 字段值断言，不打 logger。
⚠️ **2026-10-08 改准**：本文件今天**没有**用 `IntegrityError` 的用例了（用它的那两组随租户级
那条线退场）⇒ 上句里的「`IntegrityError`」是历史原文；剩下的判据打的是**模型注册表**与
**库 introspection**（都是真实出口，⛔ 不是 logger）。

⛔ **「人员同步」残留已清**（业务方裁定「人员同步 不需要 删掉记录」；
spec `2026-09-25-jidanpu-hall-sync-design.md` §17.2 的追加裁定）：原来这里的
`JidanpuUserLinkModelTests`（三条唯一约束）随**整表删除**一起删掉了 ⇒ 改由
`JidanpuUserLinkRemovalTests` 钉**删除本身**（表没了、列没了）。⚠️ 那张表从来没有写入方，
所以「三条唯一约束」这条判据不是被弱化——它钉的对象今天不存在了。

⛔ **2026-10-08 改准**：本文件原还有两组**租户级**用例——`JidanpuBindingModelTests`
（默认值 / `account` 唯一 / `__str__`）与 `JidanpuOrderLinkModelTests`
（一单一链 / 一链一单 / 反向名 `jidanpu_link`）——它们测的 `JidanpuBinding` 与
`JidanpuOrderLink` 两张表已随**租户级鸡蛋铺线整条拆除**删除
（`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md` §2.2 / §10.4）⇒ 两组一并退场，
⛔ 不是放宽。本文件今天守着的是：**存活**的 `JidanpuSyncEvent` 表的结构与默认值，
以及 `JidanpuUserLink` 的删除本身。
"""
from __future__ import annotations

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection
from django_tenants.test.cases import TenantTestCase

from apps.business.models import JidanpuSyncEvent

#: 已随「人员同步」删掉的模型 / 表（判据的**期望对象**，⛔ 不是从被测代码派生出来的）。
REMOVED_CLASS = "JidanpuUserLink"
REMOVED_TABLE = "business_jidanpuuserlink"


class JidanpuUserLinkRemovalTests(TenantTestCase):
    """⛔ 「人员同步」残留已清：**表与列都不存在了**（业务方 2026-09-26 裁定）。

    两条判据各钉一半、都打**真实出口**：
    ① 模型 / 数据库**列**层面没有 `users_synced_at`（`_meta` + 库 introspection 两份读数）；
    ② 模型注册 / 数据库**表清单**层面没有 `JidanpuUserLink`。
    ⚠️ 光看 Python 侧（`_meta` / 模型注册）挡不住「迁移没跑、库里表还在」⇒ 两条都要库读数。
    ⚠️ 判据可红：把模型字段加回来 / 把 `DeleteModel` 操作从 `0042` 删掉（重建一个同名的）
    ⇒ 对应那半条红（E4 报告有红证）。

    ⛔ **2026-10-08 改准**：判据①（`users_synced_at` 那一列）随**租户级鸡蛋铺线整条拆除**
    一起退场——那一列挂在 `JidanpuBinding` 上，而那张表（连同模型）已删除
    （计划 §2.2 / §10.4）⇒ 今天无从判起，⛔ 不是放宽。
    ⚠️ 判据②（`JidanpuUserLink` 那个模型与表）**照旧承重**、一个字未改。
    """

    def test_the_user_link_model_and_table_are_gone(self):
        model_names = {model.__name__ for model in apps.get_app_config("business").get_models()}
        self.assertNotIn(REMOVED_CLASS, model_names)
        # 哨兵：模型注册非空（否则「不含」是在一个空集合上成立的）。
        # ⚠️ 哨兵不用 `JidanpuBinding`（它已随租户级那条线删除）
        # ⇒ 换成**存活**的 `JidanpuSyncEvent`（本文件下一组用例测的就是它）；哨兵的
        # 作用（证明这个集合非空）一字未改。
        self.assertIn("JidanpuSyncEvent", model_names)
        self.assertNotIn(REMOVED_TABLE, connection.introspection.table_names())


class JidanpuSyncEventModelTests(TenantTestCase):
    def test_defaults_and_indexes_match_the_spec(self):
        event = JidanpuSyncEvent.objects.create(
            direction="out",
            kind="order.create",
            target_type="order",
            target_ref="20260924000000000901",
        )
        self.assertEqual(event.status, "pending")
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.reason, "")
        self.assertIsNone(event.payload)
        self.assertIsNone(event.processed_at)
        self.assertIsNotNone(event.created_at)
        # 索引要钉**名字 → 列**的映射：只钉名字的话，把某个索引的列换掉照样绿。
        self.assertEqual(
            {index.name: list(index.fields) for index in JidanpuSyncEvent._meta.indexes},
            {
                "jdp_event_status_idx": ["status", "created_at"],
                "jdp_event_target_idx": ["target_type", "target_ref", "created_at"],
            },
        )

    def test_enum_values_match_the_spec(self):
        """两个枚举的成员**逐个钉死**（spec §4.4）。

        ⚠️ 光有下面那条「写进库再读回来」的往返**证明不了这一步**：`objects.create()` 不跑
        `full_clean()`、`choices` 也不产生数据库检查约束 ⇒ 删掉一个成员，往返用例照样绿。
        """
        self.assertEqual(list(JidanpuSyncEvent.Direction.values), ["out", "in"])
        self.assertEqual(
            list(JidanpuSyncEvent.Status.values),
            ["pending", "done", "failed", "skipped"],
        )

    def test_unknown_choice_is_rejected_by_full_clean(self):
        """`choices` 是**承重**的：非法取值必须在 `full_clean()` 处被拒。

        对照（同一条用例内）：合法取值**不抛**——否则「它抛了」可能只是别的原因抛的。
        """
        base = {"kind": "order.create", "target_type": "order", "target_ref": "x"}
        JidanpuSyncEvent(direction="out", status="pending", **base).full_clean()
        with self.assertRaises(ValidationError) as direction_error:
            JidanpuSyncEvent(direction="sideways", **base).full_clean()
        self.assertIn("direction", direction_error.exception.message_dict)
        with self.assertRaises(ValidationError) as status_error:
            JidanpuSyncEvent(direction="out", status="bogus", **base).full_clean()
        self.assertIn("status", status_error.exception.message_dict)

    def test_direction_and_status_accept_the_spec_values(self):
        """⚠️ 样例 `kind` 用**在位**的取值（`hall.remote_taken`），⛔ 不用死 kind `user.sync`。

        原文这两处（本文件与 `test_jidanpu_sync_lists.py`）拿 `user.sync` 当「通用样例 kind」
        用；随「人员同步」残留清理（业务方裁定）改成在位取值—样例里再留着它，
        下一个读者会以为那是个真会出现的 kind（`tests/test_jidanpu_money.py` 早就钉着它已死）。
        """
        for direction in ("in", "out"):
            for status in ("pending", "done", "failed", "skipped"):
                with self.subTest(direction=direction, status=status):
                    event = JidanpuSyncEvent.objects.create(
                        direction=direction,
                        kind="hall.remote_taken",
                        target_type="order",
                        target_ref="20260924000000000901",
                        status=status,
                    )
                    self.assertEqual(event.status, status)

    def test_payload_round_trips_as_json(self):
        event = JidanpuSyncEvent.objects.create(
            direction="in",
            kind="order.link",
            target_type="order",
            target_ref="20260924000000000901",
            payload={"jidanpu_order_id": "JDP-1"},
        )
        # 从库里重读：断言的是**落盘后的值**，不是内存对象上的那个 dict。
        event.refresh_from_db()
        self.assertEqual(event.payload, {"jidanpu_order_id": "JDP-1"})
