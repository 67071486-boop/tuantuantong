"""⭐ **结构网**：`CredentialSpec` 的「闸开关格」**必须没有默认值**（2026-10-09 修复轮起）。

## 这条网拦的是什么（⛔ 不是风格检查）

`CredentialSpec`（`apps/openapi/credentials.py`）的每一格都由**构造它的那份 spec** 显式声明；
而**带闸语义**的格子原先/险些带默认值，它们的**唯一**消费者都是某道闸的**第一行**：

| 格子 | 消费者（闸） | 危险形状（默认值 ⇒ fail-open） |
|---|---|---|
| `locked_app_kind` | `apps/openapi/permissions.py::enforce_app_endpoint_scope` 第一行 `if … is None: return`（端点白名单闸） | 新 spec 漏写 ⇒ 整族闸静默不存在，现有测试全绿 |
| `bound_user_field`（D-0333） | Bearer 引擎第 5.5 步与取令牌流程的第一行 `if … is None`（「绑定账号必须可用」闸，M3 半边收口） | 同上：有绑定账号的一族漏写 ⇒ 停用打手的令牌静默照常可用 |

⇒ 「这一族有没有这道闸」**由这一格决定**。挂默认值时的失败形态是 **fail-open**：
将来谁新写一份 `CredentialSpec`（第三族）而**漏写**这一格 ⇒ 那道闸**静默不存在** ——
没有异常、没有日志、**现有测试全绿**（没有任何用例会因为"少了一道闸"而红）。
⇒ 去掉默认值以后，"漏写"这件事变成**构造期 `TypeError`**
（`apps/openapi/credentials.py` 的类 docstring：漏填一个 ⇒ 构造期响亮报错）。

## 为什么判据只能是一条**结构网**（⛔ 不是断言某个值）

值断言挡不住这条 fail-open：`OPENAPI_SPEC.locked_app_kind == "wecom_group_qr"` 与
`TAKER_SPEC.locked_app_kind is None` 两条**今天都绿**，而默认值还在那儿、洞也还在
（`bound_user_field` 同理）。要拦的是「**这个 dataclass 允不允许漏写**」这件事本身
⇒ 判据打在 `dataclasses.fields(...)` 上。

## ⭐ 这条网**能失败**（变异实测：把默认值加回去 ⇒ 两条用例都红）

**变异手法**（忠实地还原"改之前那一版"）：把声明从类体中段**挪回末尾**并加回默认值 ——

    locked_app_kind: str | None            # 现在的位置（前面全是**无**默认值的字段）
      ⇒（移到 `bucket_unique_fields` 之后）
    locked_app_kind: str | None = None     # 改之前那一版

⚠️ **先试过一次"就地加 `= None`"，那条读数不产出结论**：`dataclass` 在**导入期**就抛
`TypeError: non-default argument 'invalid_credentials_msg' follows default argument
'locked_app_kind'` ⇒ 整轮是**收集期崩溃**、一条用例都没跑到（`AGENTS.md` §5-G：
**crashed 不算 killed**）。⇒ 变异必须**连同位置一起**还原才算忠实。

**实测读数**（raw，`manage.py test tests.test_credential_spec_locked_kind_required -v 1`）：

    FF.
    FAIL: test_a_spec_that_omits_locked_app_kind_raises_at_construction
      ... with self.assertRaises(TypeError):
      AssertionError: TypeError not raised
    FAIL: test_locked_app_kind_field_has_no_default
      ... self.assertIs(field.default, dataclasses.MISSING, ...)
      AssertionError: None is not <dataclasses._MISSING_TYPE object at 0x1074b01a0> :
        locked_app_kind 又有默认值了 ⇒ 「新 spec 漏写这一格」会重新变成 fail-open
        （整族凭据的端点白名单那道闸会静默不存在）。
    Ran 3 tests in 0.000s
    FAILED (failures=2)

⚠️ 纪律（`AGENTS.md` §5-G）：`KILLED` 只由**目标用例自己**的 `FAIL:` / `ERROR:` 行判定
（上面两条正是）；变异前后各自证过"文本 = 原始 / = 变异后"（两个锚点各命中**恰好 1 次**）；
跑测试前无条件清过 `__pycache__`；还原写进 `try/finally`（⛔ **不用** `git checkout --`：
本文件与 `credentials.py` 当时都还有**未提交的真实改动**），收尾 `git status --porcelain`
复核只列本修复轮那几处。
"""
from __future__ import annotations

import dataclasses

from django.test import SimpleTestCase

from apps.openapi.credentials import OPENAPI_SPEC, CredentialSpec
from apps.takerapi.credentials import TAKER_SPEC

#: 钉**值**（⛔ 不钉 `ApiAppKind.WECOM_GROUP_QR` 这个常量名）：枚举改值时这条要红
#: （`AGENTS.md` §5-G / E-0024：自指断言在被断言的常量被改坏时**不会红**）。
OPENAPI_LOCKED_KIND_VALUE = "wecom_group_qr"


class LockedAppKindHasNoDefaultTests(SimpleTestCase):
    """`locked_app_kind` 必填 ⇒ 漏写 = 构造期 `TypeError`（⛔ 不是静默关闸）。

    ⚠️ 本类**不碰数据库**（`SimpleTestCase`）：它只读 `dataclasses` 的元数据与两份现成 spec。
    """

    def test_locked_app_kind_field_has_no_default(self) -> None:
        """⭐ 这条就是那条网：`default` 与 `default_factory` **两个**都必须是 `MISSING`。"""
        field = {f.name: f for f in dataclasses.fields(CredentialSpec)}["locked_app_kind"]
        # 自证"拿到的是真东西"（⛔ 不是拿一个空对象在断言）：这一份 `fields()` 确实来自
        # `CredentialSpec` 自己（三个模型字段是它的固定成员），而这一格确实叫这个名字。
        names = {f.name for f in dataclasses.fields(CredentialSpec)}
        self.assertTrue({"app_model", "idem_model", "bucket_model"} <= names)
        self.assertEqual(field.name, "locked_app_kind")
        self.assertIs(
            field.default,
            dataclasses.MISSING,
            "locked_app_kind 又有默认值了 ⇒ 「新 spec 漏写这一格」会重新变成 fail-open"
            "（整族凭据的端点白名单那道闸会静默不存在）。",
        )
        self.assertIs(
            field.default_factory,
            dataclasses.MISSING,
            "locked_app_kind 的 default_factory 不是 MISSING ⇒ 同上（漏写仍然不报错）。",
        )

    def test_a_spec_that_omits_locked_app_kind_raises_at_construction(self) -> None:
        """漏写那一格 ⇒ **构造期** `TypeError`（这一条钉的是那条 fail-open 的实际后果）。

        ⚠️ 构造用的 kwargs **从现存那份 spec 现取**（⛔ 不在这里抄一遍字段清单：抄一份就会漏掉
        将来新加的字段，而"漏掉的那个字段"本身可能就是新的静默默认值）。
        """
        kwargs = {
            f.name: getattr(OPENAPI_SPEC, f.name)
            for f in dataclasses.fields(CredentialSpec)
            if f.name != "locked_app_kind"
        }
        # 自证"这一份 kwargs 确实**恰好少一格**"（否则下面的 `assertRaises` 可能因为别的原因红）。
        self.assertNotIn("locked_app_kind", kwargs)
        self.assertEqual(len(kwargs), len(dataclasses.fields(CredentialSpec)) - 1)
        with self.assertRaises(TypeError):
            CredentialSpec(**kwargs)

    def test_the_two_live_specs_still_declare_it_explicitly(self) -> None:
        """⛔ **不改行为**的对照：两族今天各自写的值**一字不变**。

        ⚠️ 这一条**不是**那条网（它今天与昨天都绿）——它守的是"改成必填"这一次的手脚干净：
        上家侧仍是 `wecom_group_qr`、打手侧仍是 `None`。
        """
        self.assertEqual(OPENAPI_SPEC.locked_app_kind, OPENAPI_LOCKED_KIND_VALUE)
        self.assertIsNone(TAKER_SPEC.locked_app_kind)


#: 钉**值**（E-0024）：打手侧那一格必须指到 `TakerApp` 上那一条**必填** FK 的列名。
TAKER_BOUND_USER_FIELD_VALUE = "taker"


class BoundUserFieldHasNoDefaultTests(SimpleTestCase):
    """`bound_user_field` 必填（D-0333）⇒ 漏写 = 构造期 `TypeError`（⛔ 不是静默关闸）。

    三条与上面 `locked_app_kind` 那一类**同款**（网能红的证明同那一段，不重复抄）：
    「绑定账号必须可用」这道闸的两个消费者（Bearer 引擎第 5.5 步 / 取令牌流程）第一行都是
    `if spec.bound_user_field is None: …（早退）` ⇒ 默认值 `None` 会让
    「有绑定账号的一族漏写这一格」重新变成 fail-open（停用打手的令牌静默照常可用）。
    ⚠️ 本类**不碰数据库**（`SimpleTestCase`）。
    """

    def test_bound_user_field_has_no_default(self) -> None:
        """⭐ 那条网：`default` 与 `default_factory` **两个**都必须是 `MISSING`。"""
        field = {f.name: f for f in dataclasses.fields(CredentialSpec)}["bound_user_field"]
        # 自证"拿到的是真东西"（⛔ 不是拿一个空对象在断言）：这一份 `fields()` 确实来自
        # `CredentialSpec` 自己（三个模型字段是它的固定成员），而这一格确实叫这个名字。
        names = {f.name for f in dataclasses.fields(CredentialSpec)}
        self.assertTrue({"app_model", "idem_model", "bucket_model"} <= names)
        self.assertEqual(field.name, "bound_user_field")
        self.assertIs(
            field.default,
            dataclasses.MISSING,
            "bound_user_field 又有默认值了 ⇒ 「有绑定账号的新 spec 漏写这一格」会重新变成"
            " fail-open（停用打手的对外令牌静默照常可用，D-0333 收的口重新敞开）。",
        )
        self.assertIs(
            field.default_factory,
            dataclasses.MISSING,
            "bound_user_field 的 default_factory 不是 MISSING ⇒ 同上（漏写仍然不报错）。",
        )

    def test_a_spec_that_omits_bound_user_field_raises_at_construction(self) -> None:
        """漏写那一格 ⇒ **构造期** `TypeError`（那条 fail-open 的实际后果）。

        ⚠️ 构造用的 kwargs **从现存那份 spec 现取**（⛔ 不抄字段清单，理由同上那一类）。
        """
        kwargs = {
            f.name: getattr(OPENAPI_SPEC, f.name)
            for f in dataclasses.fields(CredentialSpec)
            if f.name != "bound_user_field"
        }
        self.assertNotIn("bound_user_field", kwargs)
        self.assertEqual(len(kwargs), len(dataclasses.fields(CredentialSpec)) - 1)
        with self.assertRaises(TypeError):
            CredentialSpec(**kwargs)

    def test_the_two_live_specs_still_declare_it_explicitly(self) -> None:
        """⛔ **不改行为**的对照：两族今天各自写的值一字不变（上家 `None` / 打手 `"taker"`）。"""
        self.assertIsNone(OPENAPI_SPEC.bound_user_field)
        self.assertEqual(TAKER_SPEC.bound_user_field, TAKER_BOUND_USER_FIELD_VALUE)
