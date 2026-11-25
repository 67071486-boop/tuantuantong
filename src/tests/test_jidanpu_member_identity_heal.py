"""**下家线（代练员个人绑定）**的「同步账号显示名」按登录账号自愈（2026-09-29 本线）。

## 为什么（与租户级那条线同一根因，不同族）

`JidanpuMemberBinding.account_name`（下家账号显示名）**纯靠人手填**（唯一写入方是绑定页 PUT
白名单），全仓没有 `GetUserInfo` 回填、也没有每轮自愈。而它被快路的**回声抑制**
（`jidanpu_member._echo_by_name`）拿去判断「这行日志是不是我们自己刚写的动作」，慢路的
接手人比对（`jidanpu_reconcile._sync_account_name_of`）读的也是它 ⇒ 凭据账号与填进去的
显示名不一致时，**我们自己的日志行不被抑制** ⇒ 多走一次「回查远端 ⇒ 可能接手」那条路
（多打上游 + 可能误接手）。⚠️ 与主线的「本地退单 + 清绩效」**不同族**，但同一根因
（我方身份有两套来源，E-0166 / D-0170）。

## 本文件钉住的事

1. 一轮里（自愈）身份不一致 ⇒ 写回 `account_name` = **登录账号**的显示名，且**恰好一条**
   幂等痕迹（同一事实只一行；再跑不变多）；
2. 身份一致 ⇒ **不写库、零痕迹**（证明判据不是恒真）；
3. 显示名在两个名单里都查不到 ⇒ **不写、零痕迹**（E-0169：回落 / 空值不冒充真值）；
4. 上游异常 ⇒ **只跳过自愈**：整轮照常完成、显示名不变、零痕迹；
5. **回声抑制**（本线真正要保的行为）：用**查到的**登录显示名造一行「我们自己」的日志
   ⇒ 被抑制；换成别的名字 ⇒ 照旧不被抑制（反向对照，证明判据没被改死）；
6. **同轮生效**：同一轮里，自愈之后的那几路读到的是登录名（⛔ 不在自愈与各路之间
   `refresh_from_db()` —— 那正是 `E-0168` 那条假绿陷阱）；
7. **跨线不互相吞**（与 kind 复用直接相关）：租户级那条线的自愈痕迹用
   `target_ref = binding.account`，而下家线与它**可能同名**（本线两个 `account` 就是同一个
   测试串）⇒ 下家线必须用**可区分**的 `target_ref`，两条线的痕迹能**同时存在、各自一条**。

⚠️ 夹具一律**合成值**（E-0077）：uid 用 `9102` 这类显眼值、姓名用「测试同步账号甲」/
「测试下家旧名」；⛔ 不写任何真实账号 / 昵称。
⚠️ 桩打在**被查找的那个模块**上（E-0029）。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from types import SimpleNamespace
from unittest import mock

import httpx
from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from jidanpu.errors import ApiError, AuthError, UpstreamBlockedError

from apps.business import jidanpu, jidanpu_member, jidanpu_proxy
from apps.business.management.commands.jidanpu_sync import (
    Command,
    _MEMBER_CLAIM_NET_AT,
    _MEMBER_FAST_AT,
    _MEMBER_RECONCILE_AT,
    _MEMBER_STATUS_PULL_AT,
    _UPSTREAM_FAILURE_COUNT,
    _UPSTREAM_PENALTY_UNTIL,
)
from apps.business.models import (
    EmpType,
    Game,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
    Order,
    OrderStatus,
    Performance,
)
from tests.p5_auth_helpers import make_logged_in_client

# ── 合成夹具值（E-0077）─────────────────────────────────────────────────────────

BASE_URL = "https://jidanpu-member-heal.test.invalid"
ACCOUNT = "member-account-heal"          # 与租户级那条线的 account **故意同名**（跨线碰撞判据）
PASSWORD = "pw-canary-heal"
#: 绑定里那一格（**旧值**：自愈前写着的显示名）。
OLD_NAME = "测试下家旧名"
#: **登录账号**在鸡蛋铺的身份（合成值）：`GetUserInfo` 的 `Id` + 可指派名单里的 `userName`。
LOGIN_UID = 9102
LOGIN_NAME = "测试同步账号甲"
NOW = datetime(2026, 9, 29, 4, 0, 0, tzinfo=dt_timezone.utc)

#: 台账 kind 的**契约字面值**（E-0024：钉值不钉常量）——与租户级那条线**同一个** kind。
IDENTITY_HEAL_KIND_LITERAL = "binding.identity_healed"

# ── 快路回声判据的合成夹具（照 test_jidanpu_member_fastpath）─────────────────────

BINDING_USER = "member-heal"
OTHER_OPERATOR = "别人的操作人"
ORDER_NO = "20260929000000009101"
JID = "JDP-00000000000000009101"
REMOTE_TAKER_UID = 5101
REMOTE_TAKER_NAME = "打手甲"
REMARK_WEB = "【打手甲】通过接单大厅(内)自助接手此单(系统网页)，接手价：1.00"


@contextmanager
def _ctx(client):
    # ⚠️ `jidanpu_member._connection` 产出的是 `(sdk, http)` **一对**（真实形态里是两个对象）
    # ⇒ 桩也要交出一对，否则解包就 TypeError（看着像自愈坏了，其实是桩的形状不对）。
    yield client, client


class _IdentityClient:
    """假客户端：只答**登录身份那两个端点**（`GetUserInfo` / 可指派名单 / 人员页）。

    ⚠️ 比真实客户端窄是**有意**的：走到别处就说明自愈多调了端点（那是回归，不是噪音）。
    ⚠️ `assignable` / `staff` 可显式给空（造「显示名名单两处都没查到」那一档）：此时
    `resolve_login_identity` 会退到**人员页**（`USERS_PATH`）——那一步也要答得上。
    ⚠️ `login_payload` 可换掉 `GetUserInfo` 回包（缺省 = 大写 `Id` 的**老形态**）：
    造小写键 / `name=null` / `account` 有值等实测形状时用它。
    """

    def __init__(self, *, login_error=None, assignable=None, staff=None,
                 login_payload=None):
        self.login_error = login_error
        self.login_payload = login_payload
        self.assignable = (
            [{"id": LOGIN_UID, "userName": LOGIN_NAME, "isValid": "Y"}]
            if assignable is None
            else list(assignable)
        )
        self.staff = list(staff or [])
        self.calls: list[tuple[str, str]] = []
        self.last_envelope: dict = {}

    def call(self, method, path, **kwargs):
        self.calls.append((method, path))
        if path == jidanpu.LOGIN_USER_INFO_PATH:
            if self.login_error is not None:
                raise self.login_error
            self.last_envelope = {"code": 1, "msg": "success"}
            if self.login_payload is not None:
                return dict(self.login_payload)
            return {"Id": LOGIN_UID, "Account": ACCOUNT, "IsSuper": True}
        if path == jidanpu.ASSIGNABLE_USERS_PATH:
            self.last_envelope = {"code": 0, "msg": "success"}
            return [dict(row) for row in self.assignable]
        if path == jidanpu.USERS_PATH:
            self.last_envelope = {
                "code": 0, "msg": "success", "count": len(self.staff),
            }
            return [dict(row) for row in self.staff]
        raise AssertionError(f"未预期的端点：{method} {path}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _named_calls(client) -> list[str]:
    return [path for _method, path in client.calls]


class MemberBindingIdentityHealTests(TenantTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username=BINDING_USER, password="pw-user-canary")
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account=ACCOUNT,
            account_name=OLD_NAME,
            password=PASSWORD,
            source_name="团团通系统",
            enabled=True,
        )

    def _notes(self):
        return JidanpuSyncEvent.objects.filter(kind=IDENTITY_HEAL_KIND_LITERAL)

    # ── a) 对齐 + 幂等 ────────────────────────────────────────────────────

    def test_a_round_writes_the_login_name_back_and_keeps_one_note(self):
        """⭐ 判据（本线核心）：自愈后 `account_name` = **登录账号**的显示名，且**恰好一条**
        幂等痕迹；第二遍（已一致）不加行；第三遍（被改回旧值 ⇒ 自愈再写**同样**的新名）
        **仍然只有一行**。

        失败方向：删掉写回 ⇒ 显示名还是旧值、痕迹 0 行 ⇒ 红；去掉幂等键（每轮都写）⇒
        第三遍 2 行 ⇒ 红。
        """
        client = _IdentityClient()
        self.assertEqual(
            jidanpu_member.heal_binding_identity(self.binding, client=client), LOGIN_NAME
        )
        # 第一跳就是身份（⛔ 不多打别的端点）
        self.assertEqual(client.calls[0], ("GET", jidanpu.LOGIN_USER_INFO_PATH))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, LOGIN_NAME)
        notes = self._notes()
        self.assertEqual(notes.count(), 1)
        note = notes.get()
        # ⚠️ `target_type="binding"`：这条讲的是**绑定身份**（⛔ 不是一张订单）。
        self.assertEqual(note.target_type, "binding")
        self.assertEqual(note.payload["member_binding_id"], self.binding.pk)
        self.assertEqual(note.payload["old_name"], OLD_NAME)
        self.assertEqual(note.payload["new_name"], LOGIN_NAME)
        self.assertEqual(str(note.payload["login_uid"]), str(LOGIN_UID))
        # ⚠️ `reason` 是**稳定**的一句话（幂等键的一部分）：⛔ 不含新旧值。
        self.assertNotIn(OLD_NAME, note.reason)
        self.assertNotIn(LOGIN_NAME, note.reason)
        # ⚠️ `target_ref` 必须与租户级那条线**可区分**（⛔ 不只靠 account 串，见跨线判据）：
        #    本线用 `member:<主键>`（每个代练员一行 ⇒ 主键才是这一行的稳定标识）。
        self.assertEqual(note.target_ref, f"member:{self.binding.pk}")

        # 第二遍：已一致 ⇒ 不写、不加行、回 None
        self.assertIsNone(
            jidanpu_member.heal_binding_identity(self.binding, client=_IdentityClient())
        )
        self.assertEqual(self._notes().count(), 1)

        # 第三遍：有人把绑定**改回旧名**（shell / 管理端能做到）⇒ 自愈再写一次**同样**的新名
        # ⇒ 三元组幂等键让**同一个事实仍然只有一行**。
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(account_name=OLD_NAME)
        self.binding.refresh_from_db()
        self.assertEqual(
            jidanpu_member.heal_binding_identity(self.binding, client=_IdentityClient()),
            LOGIN_NAME,
        )
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, LOGIN_NAME)
        self.assertEqual(self._notes().count(), 1)

    # ── b) 一致 ⇒ 不写、零痕迹 ───────────────────────────────────────────

    def test_an_already_consistent_name_writes_nothing_and_leaves_no_note(self):
        """⭐ 对照（证明判据不是恒真）：显示名**一致** ⇒ 不写库、**零**痕迹。"""
        JidanpuMemberBinding.objects.filter(pk=self.binding.pk).update(
            account_name=LOGIN_NAME
        )
        self.binding.refresh_from_db()
        self.assertIsNone(
            jidanpu_member.heal_binding_identity(self.binding, client=_IdentityClient())
        )
        self.assertEqual(self._notes().count(), 0)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, LOGIN_NAME)

    # ── c) 名字查不到 ⇒ 不写、零痕迹（E-0169）─────────────────────────────

    def test_an_unresolvable_display_name_writes_nothing_and_leaves_no_note(self):
        """⭐ 判据：显示名的**全部来源**（回包 `name` → 两个名单 → 回包 `account`）都没查到
        ⇒ **不写**（⛔ 不落空 / 不回落旧名）、**零痕迹**（E-0169）。

        ⚠️ 旧显示名故意**非空**：这样「拿到了但名字没查到」若被写成空串 / 回落旧名，判据就会红
        （E-0169：回落值 / 空值⛔ 不冒充真值，也⛔ 不留「已写回」的假痕迹）。
        ⚠️ 回包故意 `account` 也是空的：`account` 是**最后一个**显示名来源（实测上游日志行
        就用它当操作人名），它有值就不是「全空」了。
        失败方向：让 heal 拿到空名也写库 / 留痕 ⇒ 红。
        """
        client = _IdentityClient(
            assignable=[], staff=[],
            login_payload={"Id": LOGIN_UID, "name": None, "account": ""},
        )
        self.assertIsNone(jidanpu_member.heal_binding_identity(self.binding, client=client))
        # 名单**真的都查了**（否则「没查到」是恒真的）
        self.assertEqual(
            _named_calls(client),
            [
                jidanpu.LOGIN_USER_INFO_PATH,
                jidanpu.ASSIGNABLE_USERS_PATH,
                jidanpu.USERS_PATH,
            ],
        )
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, OLD_NAME)   # ⛔ 不写
        self.assertEqual(self._notes().count(), 0)               # ⛔ 零痕迹

    # ── e) 实测回包形状（小写键 + name=null）⇒ 自愈灌上 account ─────────

    def test_heal_fills_the_account_name_for_the_measured_payload_shape(self):
        """⭐ 形状探针（E-0317 的另一半根因）：`GetUserInfo` 今天的实测回包是**小写键**
        且 `name=null`、只有 `account` 有值 ⇒ `heal_binding_identity` 解析出 `account`
        并写回 `account_name`（回声抑制恢复）；再跑一遍（已一致）⇒ 不写不留痕。"""
        payload = {
            "id": 9106, "account": "account-synth-net", "name": None,
            "email": None, "phone": "", "jdpUid": None, "isSuper": False,
        }
        client = _IdentityClient(login_payload=payload, assignable=[], staff=[])
        self.assertEqual(
            jidanpu_member.heal_binding_identity(self.binding, client=client),
            "account-synth-net",
        )
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, "account-synth-net")
        self.assertEqual(self._notes().count(), 1)
        # 幂等：已一致 ⇒ 不写、不加行
        self.assertIsNone(jidanpu_member.heal_binding_identity(
            self.binding,
            client=_IdentityClient(login_payload=payload, assignable=[], staff=[]),
        ))
        self.assertEqual(self._notes().count(), 1)

    # ── d) 上游异常 ⇒ 只跳过自愈 ─────────────────────────────────────────

    def test_an_upstream_failure_skips_the_heal_without_touching_anything(self):
        """⭐ 判据：身份那一跳抛 `ApiError` ⇒ 回 `None`、显示名**不变**、**零**痕迹
        （⛔ 不因为顺带刷新失败就写库 / 留一条假的「已自愈」）。"""
        client = _IdentityClient(login_error=ApiError("合成：登录身份读不到", code=0))
        self.assertIsNone(jidanpu_member.heal_binding_identity(self.binding, client=client))
        # 那一跳真的发出去了（否则上面的「跳过」是恒真的）
        self.assertEqual(client.calls[0], ("GET", jidanpu.LOGIN_USER_INFO_PATH))
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, OLD_NAME)
        self.assertEqual(self._notes().count(), 0)

    # ── g) 痕迹的 `target_ref` 与「账号串」**不是同一个值** ────────────────

    def test_the_member_note_is_kept_apart_from_an_account_shaped_ref(self):
        """⭐ 判据（与 kind 复用直接相关）：幂等键是 `(kind, target_ref, reason)` 三元组，
        而 `kind` 是**两条线共用**的一个 kind（`binding.identity_healed`，见上面那条字面值常量）
        ⇒ 本线的痕迹只能靠 `target_ref` 与别的写法**区分开**。本用例把这一条钉在**可达的形态**上：

        ① 本线写下的 `target_ref` 是 `member:<主键>` 形状，**⛔ 不是**绑定里那个账号串
           （拿账号串当 ref 会让「同一账号串出现在别处」时悄悄互吞——那条判据的原现场是
           **租户级那条线**，它已随 `docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`
           整条拆除，⛔ 今天**无法**再构造出第二条同名线 ⇒ 租户那一半按残余风险摘除，不在这里留桩）；
        ② 再跑一遍（已一致）⇒ 仍是**一条**（幂等，⛔ 不是每轮追加）。

        ⛔ **2026-10-08 追加（② 之后补回被削弱的那半，原文一句未删）**：原来还有一条
        「**两条台账痕迹可共存、不被幂等键 `(kind, target_ref, reason)` 吞掉**」的判据
        （当年是「租户级那条线一条 + 本线一条」，随租户级那半删除后**只剩「不是账号串」**）。
        ⇒ 在本线**可达的**等价形态上补回来：**本线的两条个人绑定各留一条**
        （③ 见下），⛔ 不用手写台账行充数（那种写法是自己造两行、判据会变成恒真）。
        ⚠️ 与当年那半的**唯一**差别：下家线自愈的 `reason` 是模块常量
        （`jidanpu_member.IDENTITY_HEAL_REASON`，两条绑定写出来**同一句话**）⇒ 这里能区分两条
        痕迹的是**幂等键的 `target_ref` 那条腿**（这也正是本用例要钉的那条腿）。

        ③ **两条下家线台账可共存**：再建一条绑定（合成夹具，⛔ 不是生产代码），它与
          `self.binding` 的 `account` 是**同一个串**（碰撞的前提，照被删用例的原现场）
          ⇒ 两条痕迹的 `target_ref` 仍是**两个**可区分的值、⛔ 都没有退化成账号串。

        ⚠️ ① 的判别力：把 `target_ref` 改回 `(binding.account or "").strip()` ⇒
        `target_ref == ACCOUNT` ⇒ 第一条断言红。
        ⚠️ ③ 的判别力（⭐ 交付前**自证过一次能失败**，两次输出都留档）：同一条变异
        （`target_ref` 改回账号串）会让第二条绑定的自愈被第一条的幂等键吞掉
        （`jidanpu_member._note_identity_heal_once` 的 `(kind, target_ref, reason)` 三元组命中）
        ⇒ 台账只剩 **1** 条、`refs` 收敛成 `{account}` ⇒ `len(refs) == 2` 与
        `account not in refs` 两条**同时**红（而变异前的正常实现下两条都绿）。
        """
        self.assertEqual(
            jidanpu_member.heal_binding_identity(self.binding, client=_IdentityClient()),
            LOGIN_NAME,
        )
        notes = self._notes()
        self.assertEqual(notes.count(), 1)

        note = notes.get()
        self.assertEqual(note.target_ref, f"member:{self.binding.pk}")
        # ⛔ 与「账号串」不是同一个值：那正是这条痕迹不会被同名的另一条线吞掉的原因。
        self.assertNotEqual(note.target_ref, self.binding.account)

        # 再跑一遍：已一致 ⇒ 仍是**一条**（不重复写）
        self.assertIsNone(
            jidanpu_member.heal_binding_identity(self.binding, client=_IdentityClient())
        )
        self.assertEqual(self._notes().count(), 1)

        # ── ③ **两条下家线台账可共存**（补回被削弱的判据，见 docstring 的追加段）────────
        # ⚠️ 第二条绑定是**合成夹具**：它与 `self.binding` 的 `account` **同一个串**
        #    （碰撞的前提），地址不同只是为了让 `(地址, 账号)` 那条唯一约束不挡它。
        peer = JidanpuMemberBinding.objects.create(
            user=User.objects.create_user(
                username="member-heal-peer", password="pw-peer-canary"
            ),
            jidanpu_base_url="https://jidanpu-member-heal-peer.test.invalid",
            account=ACCOUNT,
            account_name=OLD_NAME,
            password=PASSWORD,
            source_name="团团通系统",
            enabled=True,
        )
        # 碰撞的前提真的成立了（否则下面的 `refs` 判据是恒真的）
        self.assertEqual(peer.account, self.binding.account)
        self.assertEqual(
            jidanpu_member.heal_binding_identity(peer, client=_IdentityClient()),
            LOGIN_NAME,
        )
        notes = self._notes()
        self.assertEqual(notes.count(), 2)               # 两条**都**留下了（没被互相吞掉）
        refs = {note.target_ref for note in notes}
        self.assertEqual(len(refs), 2)                   # ⛔ 两个**可区分**的 ref
        self.assertIn(f"member:{self.binding.pk}", refs)
        self.assertNotIn(self.binding.account, refs)     # ⛔ 两条都不是账号串


def _log_time(moment) -> str:
    """测试侧独立的时间写法（⛔ 不从被测模块的格式常量派生）。"""
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M:%S")


def _row(*, create_time: str, join: str = JID, source: str = ORDER_NO,
         user: str = OTHER_OPERATOR, remark: str = REMARK_WEB) -> dict:
    return {
        "createTime": create_time,
        "joinOrderId": join,
        "soureOrderId": source,
        "userName": user,
        "remark": remark,
        "logType": 10,
        "gameName": "合成游戏甲",
        "gameRoleName": "合成角色甲",
    }


def _taken(remote_id: str = JID) -> list[dict]:
    return [{"userId": REMOTE_TAKER_UID, "userName": REMOTE_TAKER_NAME}]


class _Upstream:
    """`apps.business.jidanpu` 那三个被快路查找的出口的脚本替身（照 fastpath 用例）。"""

    def __init__(self, *, logs=None, ass=None, log_error=None, remote=None):
        self.logs = list(logs or [])
        self.ass = dict(ass or {})
        self.log_error = log_error
        self.remote = dict(remote or {})
        self.calls: list[tuple[str, dict]] = []

    def names(self) -> list[str]:
        return [name for name, _kwargs in self.calls]

    def fetch_system_logs(self, client, *, stime, log_type=10, etime=None):
        self.calls.append(("fetch_system_logs", {"stime": stime}))
        if isinstance(self.log_error, Exception):
            raise self.log_error
        return tuple(self.logs)

    def fetch_order_by_key1(self, client, *, key, expect_field, expect_value):
        self.calls.append(("fetch_order_by_key1", {"key": key}))
        if key in self.remote:
            return self.remote[key]
        return {"orderId": key, "orderSta": "待上号", "isThrowOrder": "N"}

    def fetch_ass_logs(self, client, *, remote_order_id):
        self.calls.append(("fetch_ass_logs", {"remote_order_id": remote_order_id}))
        got = self.ass.get(remote_order_id, [])
        if isinstance(got, Exception):
            raise got
        return tuple(got)


@contextmanager
def _stub(up: _Upstream):
    """把替身接到**被查找的模块** `apps.business.jidanpu` 上（E-0029）。"""
    with (
        mock.patch.object(jidanpu, "fetch_system_logs", side_effect=up.fetch_system_logs),
        mock.patch.object(jidanpu, "fetch_order_by_key1", side_effect=up.fetch_order_by_key1),
        mock.patch.object(jidanpu, "fetch_ass_logs", side_effect=up.fetch_ass_logs),
    ):
        yield up


class MemberEchoSuppressionAfterHealTests(TenantTestCase):
    """⭐ 本线真正要保的行为：**自愈把显示名变真**之后，我们自己的日志行才被抑制。"""

    def setUp(self):
        super().setUp()
        self.now = NOW
        self.game = Game.objects.create(name="合成游戏甲")
        self.user_client = make_logged_in_client(
            username=BINDING_USER, groups=("代练员",), nodes=("grab_hall.take_order",)
        )
        self.user = User.objects.get(username=BINDING_USER)
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account=ACCOUNT,
            account_name=OLD_NAME,
            password=PASSWORD,
            source_name="团团通系统",
            enabled=True,
        )
        self.order = Order.objects.create(
            game=self.game, order_no=ORDER_NO, status=OrderStatus.WAITING_GRAB,
            hall_price="12.34", total_price="50.00", project_text="合成项目甲",
        )
        Performance.objects.create(
            order=self.order, emp_type=EmpType.MAIN, temp_amount="12.34"
        )
        JidanpuMemberOrderLink.objects.create(
            binding=self.binding, order=self.order,
            jidanpu_order_id=JID, in_hall=True, last_out_at=None,
        )
        # 自愈（用假客户端，⛔ 不碰真网）：显示名落成**登录名**
        self.assertEqual(
            jidanpu_member.heal_binding_identity(self.binding, client=_IdentityClient()),
            LOGIN_NAME,
        )

    def test_a_row_from_the_healed_login_name_is_suppressed(self):
        """⭐ 判据：用**查到的**登录显示名造一行「我们自己」的日志 ⇒ 被抑制（⛔ 连回查都不做）。"""
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)),
                       user=LOGIN_NAME)],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 0)
        # ⚠️ 这是回声抑制的**代价方向**：⛔ 不白打一次上游。
        self.assertEqual(up.names(), ["fetch_system_logs"])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_the_same_row_from_another_name_is_still_not_suppressed(self):
        """⭐ 反向对照：把操作人换成**别的**名字 ⇒ 照旧回查并接手
        （证明回声判据没被改死、上一条不是「表不可达」的假绿）。"""
        up = _Upstream(
            logs=[_row(create_time=_log_time(self.now - timedelta(seconds=2)),
                       user=OTHER_OPERATOR)],
            ass={JID: _taken()},
        )
        with _stub(up):
            processed = jidanpu_member.poll_log(self.binding, now=self.now)
        self.assertEqual(processed, 1)
        self.assertIn("fetch_ass_logs", up.names())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)


    def test_reused_sdk_session_checks_identity_on_expiry_and_silent_relogin(self):
        requests = []
        now = 10
        relogin = False
        remote_name = LOGIN_NAME
        new_name = "合成更名后的同步账号"
        real_client = httpx.Client

        def respond(request):
            nonlocal relogin
            path = request.url.path
            requests.append(path)
            if path == "/Login/Login":
                return httpx.Response(
                    200, json={"code": 1, "data": "ok"},
                    headers={"Set-Cookie": "sl-session=synth-stable; Path=/"},
                )
            self.assertIn("sl-session=synth-stable", request.headers["cookie"])
            if path == jidanpu.LOGIN_USER_INFO_PATH:
                return httpx.Response(200, json={"code": 1, "data": {"id": LOGIN_UID, "name": remote_name}})
            if path == jidanpu.SYSTEM_LOG_PATH:
                if relogin:
                    relogin = False
                    return httpx.Response(302, headers={"Location": "/Login/Index"})
                row = _row(create_time=_log_time(NOW), user=remote_name)
                return httpx.Response(200, json={"code": 0, "data": [row], "count": 1})
            self.fail(f"unexpected request: {path}")

        def http_client(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)

        pool = jidanpu.UpstreamClientPool()
        self.addCleanup(pool.release_all)
        with (
            mock.patch.object(jidanpu, "member_pool", return_value=pool),
            mock.patch.object(jidanpu.httpx, "Client", side_effect=http_client),
            mock.patch.object(jidanpu_member, "time", monotonic=lambda: now),
        ):
            for now in (10, 13, 309):
                self.assertEqual(jidanpu_member.poll_log(self.binding, now=NOW, verify_identity=True), 0)
            self.assertEqual(requests.count(jidanpu.LOGIN_USER_INFO_PATH), 1)
            self.assertEqual(requests.count(jidanpu.SYSTEM_LOG_PATH), 3)
            now = 310
            jidanpu_member.poll_log(self.binding, now=NOW, verify_identity=True)
            self.assertEqual(requests.count(jidanpu.LOGIN_USER_INFO_PATH), 2)

            remote_name = new_name
            relogin = True
            now = 313
            self.assertEqual(jidanpu_member.poll_log(self.binding, now=NOW, verify_identity=True), 0)
            self.assertEqual(requests[-4:], [
                jidanpu.SYSTEM_LOG_PATH, "/Login/Login", jidanpu.SYSTEM_LOG_PATH, jidanpu.LOGIN_USER_INFO_PATH,
            ])
            self.binding.refresh_from_db()
            self.assertEqual(self.binding.account_name, new_name)
            self.binding.account = "synthetic-fastpath-rebind"
            self.binding.save(update_fields=["account", "updated_at"])
            jidanpu_member.poll_log(self.binding, now=NOW, verify_identity=True)
            self.assertEqual(requests[-3:], ["/Login/Login", jidanpu.LOGIN_USER_INFO_PATH, jidanpu.SYSTEM_LOG_PATH])
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.WAITING_GRAB)

    def test_identity_failure_does_not_reuse_expired_name_and_retries_next_round(self):
        client = SimpleNamespace(login_generation=0)
        row = _row(create_time=_log_time(NOW), user=LOGIN_NAME)
        with (
            mock.patch.object(jidanpu_member, "time") as clock,
            mock.patch.object(jidanpu, "resolve_login_identity") as identity,
            mock.patch.object(jidanpu, "fetch_system_logs", return_value=[row]),
            mock.patch.object(
                jidanpu, "fetch_order_by_key1",
                return_value={"orderId": JID, "orderSta": "代练中", "isThrowOrder": "N"},
            ) as order_check,
            mock.patch.object(jidanpu, "fetch_ass_logs", return_value=_taken()),
        ):
            clock.monotonic.return_value = 10
            identity.side_effect = [
                (str(LOGIN_UID), LOGIN_NAME), ApiError("synthetic failure"), (str(LOGIN_UID), LOGIN_NAME),
            ]
            jidanpu_member.poll_log(self.binding, now=NOW, client=client, verify_identity=True)
            order_check.assert_not_called()
            clock.monotonic.return_value = 310
            jidanpu_member.poll_log(self.binding, now=NOW, client=client, verify_identity=True)
            order_check.assert_called_once()
            self.order.refresh_from_db()
            self.assertEqual(self.order.status, OrderStatus.IN_PROGRESS)
            clock.monotonic.return_value = 313
            jidanpu_member.poll_log(self.binding, now=NOW, client=client, verify_identity=True)
            self.assertEqual(identity.call_count, 3)
            order_check.assert_called_once()

    def test_order_read_relogin_rechecks_name_before_the_next_echo(self):
        client = SimpleNamespace(login_generation=0)
        renamed = "合成重连后账号"
        rows = [
            _row(create_time=_log_time(NOW), user=OTHER_OPERATOR),
            _row(create_time=_log_time(NOW + timedelta(seconds=1)), user=renamed),
        ]

        def read_order(sdk, **_kwargs):
            sdk.login_generation += 1
            return {"isThrowOrder": "Y"}

        with (
            mock.patch.object(jidanpu, "resolve_login_identity", side_effect=[
                (str(LOGIN_UID), LOGIN_NAME), (str(LOGIN_UID), renamed),
            ]) as identity,
            mock.patch.object(jidanpu, "fetch_system_logs", return_value=rows),
            mock.patch.object(jidanpu, "fetch_order_by_key1", side_effect=read_order) as order_check,
        ):
            processed = jidanpu_member.poll_log(self.binding, now=NOW, client=client, verify_identity=True)
        self.assertEqual(processed, 1)
        self.assertEqual(identity.call_count, 2)
        order_check.assert_called_once()
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, renamed)

    def test_egress_block_during_order_check_is_counted_once_and_remains_a_retry(self):
        client = SimpleNamespace(login_generation=0, _tt_egress_outlet="http://synthetic.invalid:1111")
        cmd = Command(stdout=StringIO(), stderr=StringIO())
        tenant = SimpleNamespace(schema_name="synth-fastpath-egress")
        key = (tenant.schema_name, self.binding.pk)
        self.addCleanup(_MEMBER_FAST_AT.pop, key, None)
        self.addCleanup(_UPSTREAM_FAILURE_COUNT.pop, key, None)
        self.addCleanup(_UPSTREAM_PENALTY_UNTIL.pop, key, None)
        before = self.binding.log_cursor
        with (
            mock.patch.object(jidanpu, "member_session", side_effect=lambda _binding: _ctx(client)),
            mock.patch.object(jidanpu, "resolve_login_identity", return_value=(str(LOGIN_UID), LOGIN_NAME)),
            mock.patch.object(jidanpu, "fetch_system_logs", return_value=[_row(create_time=_log_time(NOW))]),
            mock.patch.object(jidanpu, "fetch_order_by_key1", side_effect=UpstreamBlockedError(status_code=468)),
            mock.patch.object(
                jidanpu_proxy, "note_blocked", return_value=jidanpu_proxy.BLOCK_COUNTED,
            ) as blocked,
            mock.patch.object(jidanpu_proxy, "note_ok") as ok,
        ):
            self.assertFalse(cmd._run_tenant_fastpath(tenant))
        blocked.assert_called_once_with("http://synthetic.invalid:1111")
        ok.assert_not_called()
        self.assertEqual(_UPSTREAM_FAILURE_COUNT[key], 1)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.log_cursor, before)
        self.assertIn("UpstreamBlockedError", self.binding.last_error)


    def test_identity_result_cannot_overwrite_a_binding_changed_during_lookup(self):
        def rebind(_client):
            fresh = JidanpuMemberBinding.objects.get(pk=self.binding.pk)
            fresh.account = "synthetic-fastpath-rebound"
            fresh.account_name = "新配置显示名"
            fresh.save(update_fields=["account", "account_name", "updated_at"])
            return str(LOGIN_UID), "过时的上游显示名"

        with (
            mock.patch.object(jidanpu, "resolve_login_identity", side_effect=rebind),
            mock.patch.object(jidanpu, "fetch_system_logs") as logs,
        ):
            client = SimpleNamespace(login_generation=0)
            for _ in range(2):
                result = jidanpu_member.poll_log(
                    self.binding, now=NOW, client=client, verify_identity=True,
                )
                self.assertEqual(result, 0)
        logs.assert_not_called()
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, "新配置显示名")

    def test_identity_auth_and_egress_failures_stamp_health_without_advancing_cursor(self):
        for failure in (AuthError("synthetic denied"), UpstreamBlockedError(status_code=468)):
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(jidanpu, "resolve_login_identity", side_effect=failure),
                mock.patch.object(jidanpu, "fetch_system_logs") as logs,
            ):
                before = self.binding.log_cursor
                with self.assertRaises(type(failure)):
                    jidanpu_member.poll_log(
                        self.binding, now=NOW, client=SimpleNamespace(login_generation=0), verify_identity=True,
                    )
                self.binding.refresh_from_db()
                self.assertIn(type(failure).__name__, self.binding.last_error)
                self.assertEqual(self.binding.log_cursor, before)
                logs.assert_not_called()


class MemberRoundSameObjectTests(TenantTestCase):
    """⭐（f）**同轮生效**：命令那一轮的 ⓪ 自愈之后，各路读到的必须是登录名。

    ⛔ 用例**不在**自愈与各路之间做任何 `refresh_from_db()`——`run_outbox` / `poll_log` /
    `pull_statuses` / `reconcile_and_queue` 拿到的是**同一个内存对象**；只在
    `heal_binding_identity` 里 `.update(...)`（只落库）时，它们这一轮读到的仍是旧名
    （`E-0168` 的假绿陷阱：手动刷一下就让缺陷不落在判据上）。
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username=BINDING_USER, password="pw-user-canary")
        self.binding = JidanpuMemberBinding.objects.create(
            user=self.user,
            jidanpu_base_url=BASE_URL,
            account=ACCOUNT,
            account_name=OLD_NAME,
            password=PASSWORD,
            source_name="团团通系统",
            enabled=True,
        )
        _MEMBER_FAST_AT.clear()
        _MEMBER_RECONCILE_AT.clear()
        _MEMBER_STATUS_PULL_AT.clear()
        _MEMBER_CLAIM_NET_AT.clear()
        self.addCleanup(_MEMBER_FAST_AT.clear)
        self.addCleanup(_MEMBER_RECONCILE_AT.clear)
        self.addCleanup(_MEMBER_STATUS_PULL_AT.clear)
        self.addCleanup(_MEMBER_CLAIM_NET_AT.clear)

    def _run(self, seen: dict, *, heal_client=None, heal_boom=None):
        """跑命令那一轮，把四条路读到的显示名收进 `seen`（四条路本身全桩掉）。"""
        def _outbox(binding, *, now, client=None):
            seen["outbox"] = binding.account_name
            return jidanpu_member.OutboxStats()

        def _fast(binding, *, now, client=None):
            seen["fast"] = binding.account_name
            return 0

        def _slow(binding, *, now, client=None):
            seen["slow"] = binding.account_name
            return 0

        def _reconcile(binding, *, now):
            seen["reconcile"] = binding.account_name
            return jidanpu_member.QueueStats()

        if heal_boom is not None:
            opener = mock.Mock(side_effect=heal_boom)
        else:
            opener = mock.Mock(
                side_effect=lambda binding: _ctx(heal_client or _IdentityClient())
            )

        cmd = Command(stdout=StringIO(), stderr=StringIO())
        with mock.patch.object(
            jidanpu, "member_session", opener
        ), mock.patch.object(
            jidanpu_member, "run_outbox", side_effect=_outbox
        ), mock.patch.object(
            jidanpu_member, "poll_log", side_effect=_fast
        ), mock.patch.object(
            jidanpu_member, "pull_statuses", side_effect=_slow
        ), mock.patch.object(
            jidanpu_member, "reconcile_and_queue", side_effect=_reconcile
        ), mock.patch.object(
            # ⑤ 兜底网与这里要看的「自愈→各路读到同一个名字」无关，且它走同一个
            # `member_session` 出口 ⇒ 桩掉（它的业务行为在 test_jidanpu_member_claim_net.py）。
            jidanpu_member, "claim_net", return_value=0
        ):
            unexpected = cmd._run_member_binding(
                SimpleNamespace(schema_name="heal-test"), self.binding, now=NOW
            )
        return unexpected

    def test_the_same_rounds_paths_read_the_healed_name(self):
        seen: dict[str, str] = {}
        unexpected = self._run(seen)
        self.assertFalse(unexpected)
        # 自愈真的发生了（否则四个读数相同可能只是「压根没自愈」）
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, LOGIN_NAME)
        # ⭐ 四条路读到的都是**登录名**（同一轮、同一内存对象，⛔ 无 refresh）
        self.assertEqual(seen.get("outbox"), LOGIN_NAME)
        self.assertEqual(seen.get("fast"), LOGIN_NAME)
        self.assertEqual(seen.get("slow"), LOGIN_NAME)
        self.assertEqual(seen.get("reconcile"), LOGIN_NAME)
        # 恰好一条幂等痕迹
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind=IDENTITY_HEAL_KIND_LITERAL
            ).count(),
            1,
        )

    def test_an_unexpected_heal_blowup_never_breaks_the_round(self):
        """⭐ 保底判据：自愈里冒出**非上游**异常（`member_session` 直接炸）
        ⇒ 仍然只跳过自愈、四条路照跑、本轮⛔ 不记成未预期失败、⛔ 零痕迹。"""
        seen: dict[str, str] = {}
        unexpected = self._run(seen, heal_boom=RuntimeError("合成：自愈自己坏了"))
        self.assertFalse(unexpected)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.account_name, OLD_NAME)
        self.assertEqual(seen.get("outbox"), OLD_NAME)   # 各路照跑（读到旧值）
        self.assertEqual(seen.get("fast"), OLD_NAME)
        self.assertEqual(
            JidanpuSyncEvent.objects.filter(
                kind=IDENTITY_HEAL_KIND_LITERAL
            ).count(),
            0,
        )


class LoginIdentityShapeTests(SimpleTestCase):
    """`resolve_login_identity` 的**回包形状**兼容（零 DB、零网络）。

    显示名来源序（顺序本身是判据）：①回包 `name`（非空直用）→ ②可指派名单（字段 `id`）
    → ③人员页（字段 `uid`）→ ④回包 `account`（`name` 为空的账号，上游自己的日志行就是
    拿它当操作人名渲染的——实测回声行 `userName == account`）；全空 ⇒ 空串（E-0169）。
    ⚠️ 键名大小写兼容：实测今日回包是小写 `id`/`name`/`account`，老形态是大写 `Id`。
    """

    #: 今天的实测回包形状（值全部改成合成值；键名与值域照实测）。
    MEASURED_SHAPE = {
        "id": 9107, "account": "account-synth-shape", "name": None,
        "email": None, "phone": "", "jdpUid": None, "isSuper": False,
    }

    def test_the_measured_lowercase_shape_with_a_name_uses_the_name_directly(self):
        """小写键 + `name` 有值 ⇒ 直用 `name`，且**名单一个都不查**（最省的那一档）。"""
        payload = dict(self.MEASURED_SHAPE, id=9108, name="测试同步账号乙")
        client = _IdentityClient(login_payload=payload)
        self.assertEqual(
            jidanpu.resolve_login_identity(client), ("9108", "测试同步账号乙")
        )
        self.assertEqual(_named_calls(client), [jidanpu.LOGIN_USER_INFO_PATH])

    def test_the_measured_lowercase_shape_falls_back_to_the_account(self):
        """⭐ 形状探针：小写键 + `name=null` + 名单都没查到 ⇒ 用 `account`（第 4 来源，
        ⛔ 不是编造回落）；两个名单**真的都查了**。"""
        client = _IdentityClient(
            login_payload=dict(self.MEASURED_SHAPE), assignable=[], staff=[],
        )
        self.assertEqual(
            jidanpu.resolve_login_identity(client),
            ("9107", "account-synth-shape"),
        )
        self.assertEqual(
            _named_calls(client),
            [
                jidanpu.LOGIN_USER_INFO_PATH,
                jidanpu.ASSIGNABLE_USERS_PATH,
                jidanpu.USERS_PATH,
            ],
        )

    def test_the_uppercase_legacy_payload_still_works(self):
        """大写 `Id` 老形态（既有夹具）⇒ 照旧工作：键名兼容不是「只认小写」。"""
        client = _IdentityClient()
        self.assertEqual(
            jidanpu.resolve_login_identity(client), (str(LOGIN_UID), LOGIN_NAME)
        )

    def test_an_int_id_is_accepted_and_a_bad_id_is_loud(self):
        """`id` 是正整数 int ⇒ 收（实测值域）；`0`/负数/非数字 ⇒ 照旧响亮抛 `ApiError`
        （⛔ 当场抛：不去打名单）。"""
        client = _IdentityClient(
            login_payload=dict(self.MEASURED_SHAPE, id=9109, name="测试同步账号甲")
        )
        self.assertEqual(
            jidanpu.resolve_login_identity(client), ("9109", "测试同步账号甲")
        )
        for bad in (0, -1, "0", "-1", "551.0", "uid=551", "不是数字", None, True, ""):
            with self.subTest(bad=bad):
                client = _IdentityClient(login_payload={"id": bad})
                with self.assertRaises(ApiError):
                    jidanpu.resolve_login_identity(client)
                self.assertEqual(
                    _named_calls(client), [jidanpu.LOGIN_USER_INFO_PATH]
                )

    def test_the_assignable_list_beats_the_account_fallback(self):
        """名单按 uid 命中且名字有值 ⇒ 仍用名单名（第 2 来源优先于 `account`）。"""
        payload = dict(
            self.MEASURED_SHAPE, id=LOGIN_UID,
            account="account-synth-shape", name=None,
        )
        client = _IdentityClient(login_payload=payload)
        self.assertEqual(
            jidanpu.resolve_login_identity(client), (str(LOGIN_UID), LOGIN_NAME)
        )
        # 名单命中后不再退人员页
        self.assertEqual(
            _named_calls(client),
            [jidanpu.LOGIN_USER_INFO_PATH, jidanpu.ASSIGNABLE_USERS_PATH],
        )
