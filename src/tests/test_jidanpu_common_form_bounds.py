"""①式录单表单的**出向字节边界**（2026-10-25 审计批 D1，M-9 的①式半边）。

> 审计：`.tools/recon/code-audit/2026-10-25-sync-events.md` 的 **M-9**——
> 「①式录单表单 project/remark 不裁字节——E-0242 只修了②式半边」。

## 修的是什么

`Order.project_text` 列宽 900（D-0218）之后，**250 个中文**（750 字节）的入向长单在
**①式 `launch` / `assign`** 那一发表单里原样出去 ⇒ 超过对方 ≈732 字节的实测上限
（E-0242）⇒ 被对方拒 → 退避 → `abandoned`（E-0242 当天 6 张卡单同形，随时可复发）。
修法：把②式的剪刀（`_clip_project` / `PROJECT_TEXT_OUTBOUND_MAX_BYTES`）抽到
`jidanpu_common` 共用（**单一出处**），①式 `build_add_order_form` 对 `project` 与
`remark` **两格都裁**；②式行为一个字不改（仍裁、同一常量）。

## 判据钉在哪

- `build_add_order_form` 是**纯函数** ⇒ 它的返回值就是真实出口（`AGENTS.md` §5-G 的
  「真实出口」对纯函数就是返回值；写面的端到端由 `tests/test_jidanpu_member_consume.py`
  的 `test_the_launch_payload_comes_from_the_shared_builders` 钉住——表单基底来自本函数）。
- 250 中文字 ⇒ 裁到 233 字（699 字节）：**字面量**，⛔ 不从常量派生（E-0024）。
- 「两条线同一把剪刀」用 **`is`**（同一个函数对象）钉，⛔ 不是值相等——值相等挡不住
  「谁复制了一份」。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.business import jidanpu_common, jidanpu_member_openapi_outbox

#: 250 个中文字（750 字节 > 700 上限）——验收口径的原话数字。
LONG_PROJECT = "长" * 250
#: 裁完 233 个汉字 = **699 字节** ≤ 700；第 234 个字的残字节被边界安全丢弃。
CLIPPED_PROJECT = "长" * 233


def _order(**overrides) -> SimpleNamespace:
    """录单表单要读的**全部格子**的合成夹具（E-0077；`build_add_order_form` 不碰库）。"""
    fields = dict(
        order_no="20261025000000D1",
        game_id=321,
        project_text="合成项目甲",
        total_price="100.00",
        content="合成内容",
        customer_phone="13800000000",
        customer_qq="123456",
        customer_ww="合成旺旺",
        game_account="合成账号",
        game_role_name="合成角色",
        remark="合成备注",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class StyleOneFormClipsTextTests(SimpleTestCase):
    """①式 `build_add_order_form` 的 `project` / `remark` 两格在出向边界裁字节。"""

    def test_a_250_chinese_char_project_stays_within_the_byte_limit(self):
        """⭐ 验收判据：250 中文字的单，①式出向那一格 **≤700 字节**（裁到 233 字）。"""
        form = jidanpu_common.build_add_order_form(_order(project_text=LONG_PROJECT))
        self.assertEqual(len(form["project"].encode("utf-8")), 699)
        self.assertLessEqual(len(form["project"].encode("utf-8")), 700)
        self.assertEqual(form["project"], CLIPPED_PROJECT)

    def test_the_remark_cell_is_clipped_by_the_same_scissors(self):
        """⭐ `remark` 同格同刀：超宽备注裁到同一上限（M-9 点名的第二格）。"""
        form = jidanpu_common.build_add_order_form(_order(remark=LONG_PROJECT))
        self.assertEqual(form["remark"], CLIPPED_PROJECT)
        self.assertEqual(len(form["remark"].encode("utf-8")), 699)

    def test_short_values_pass_through_untouched(self):
        """⛔ 短值一个字节都不动（裁剪不许顺手 strip / 归一化——匹配键语义）。"""
        form = jidanpu_common.build_add_order_form(_order())
        self.assertEqual(form["project"], "合成项目甲")
        self.assertEqual(form["remark"], "合成备注")

    def test_the_builder_reads_the_shared_constant(self):
        """判别力：把公共常量压到 8 字节 ⇒ ①式那一格跟着变（证明用的是同一把剪刀）。"""
        with mock.patch.object(jidanpu_common, "PROJECT_TEXT_OUTBOUND_MAX_BYTES", 8):
            form = jidanpu_common.build_add_order_form(_order(project_text="长名" * 10))
        #: 8 字节 =「长名」两字，第三字残字节被丢（与②式端到端用例同一个字面期望）。
        self.assertEqual(form["project"], "长名")


class SingleSourceTests(SimpleTestCase):
    """「单一出处」的结构判据：②式 outbox 只是**别名再暴露**，⛔ 不是第二份。"""

    def test_the_outbox_names_are_the_same_objects_as_common(self):
        self.assertIs(
            jidanpu_member_openapi_outbox._clip_project, jidanpu_common._clip_project
        )
        self.assertIs(
            jidanpu_member_openapi_outbox.PROJECT_TEXT_OUTBOUND_MAX_BYTES,
            jidanpu_common.PROJECT_TEXT_OUTBOUND_MAX_BYTES,
        )

    def test_the_shared_limit_is_still_seven_hundred_bytes(self):
        """钉值（字面量）：两条线共用的上限就是 700 字节（E-0242 实测口径）。"""
        self.assertEqual(jidanpu_common.PROJECT_TEXT_OUTBOUND_MAX_BYTES, 700)
