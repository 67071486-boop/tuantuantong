"""自助注册链接（spec `2026-10-16-self-registration-links-design.md` §7 判据 1–16）。

⚠️ 必须用 `TenantTestCase`：`apps.accounts` 只在 `TENANT_APPS` 里 ⇒ 普通 `TestCase`
的 search_path 里没有租户表（照 `tests/test_peer_binding_api.py` / `test_account_api.py`
的先例）。每条请求都要带租户域名：默认的 `testserver` 解析不到 `Domain` ⇒ 404。

⚠️ 期望值一律写字面副本（E-0024：从被测模块 import 常量来自比是自指断言——
常量被改坏时它跟着一起变、照样绿）；断言打在**数据库行**上（E-0009）。

弄红自证的逐条变异台账见提交说明与本文件底部注释（判据编号 ↔ 用例名对照表）。
"""
from datetime import timedelta
from unittest import mock
import threading

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_tenant_domain_model,
    get_tenant_model,
    tenant_context,
)

from apps.accounts.models import (
    SelfRegistrationConfig,
    SelfRegistrationInvite,
    StaffProfile,
)
from apps.audit.models import OperationLog
from apps.business.models import Game
from apps.business.peer_sync import hash_token
from apps.platform.provisioning import provision_tenant, seed_tenant
from tests.p5_auth_helpers import make_logged_in_client

User = get_user_model()

#: A 的域名 = `TenantTestCase` 自带租户的域名（与既有账号隔离测试同口径）。
DOMAIN = "tenant.test.com"

CONFIG_URL = "/api/accounts/self-registration/config/"
INVITES_URL = "/api/accounts/self-registration/invites/"
REGISTER_URL = "/api/accounts/self-registration/register/"

#: 外部契约值的**字面副本**（⛔ 不 import 视图常量自比，E-0024）。
LINK_INVALID_MSG = "链接已失效或已被使用，请联系人事重新获取"
MOBILE_TAKEN_MSG = "该手机号已注册，请直接登录或联系人事"
RATE_LIMITED_MSG = "尝试过于频繁，请稍后再试"
NODE_DENIED_MSG = "当前职位没有此操作所需的权限节点"
INITIAL_PASSWORD = "123456"
PREFIX_INVALID_MSG = "编号前缀只能是 1~5 个英文字母"
START_INVALID_MSG = "起始编号必须是 ≥ 1 的整数"
# ⭐ D-0253「作废」的业务文案（字面副本，逐字即外部契约）。
REVOKE_USED_MSG = "该链接已被使用，无法作废"
REVOKE_EXPIRED_MSG = "该链接已过期，无需作废"
REVOKE_NOT_FOUND_MSG = "链接不存在或已失效"
REVOKE_ACTION = "user.selfreg_revoke"

#: 注册成功的载荷都用这一族合成值（字面副本，断言对着它们打；E-0077：不是真值）。
MOBILE = "13800001234"
ALIPAY_ACCOUNT = "synth-alipay-001"
ALIPAY_NAME = "测试打手甲"


class SelfRegistrationTestsBase(TenantTestCase):
    """公共基类：灌基线数据（Group / 目录 / admin）+ 三款游戏 + 两个人事客户端。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # `TenantTestCase` 不跑 `seed_tenant`（只建空 schema）：本线的写路径要
        # `Group.objects.get(name="代练员")`，目录与 Group 必须显式灌一次。
        seed_tenant(cls.tenant)
        cls.game_a = Game.objects.create(name="游戏甲")
        cls.game_b = Game.objects.create(name="游戏乙")
        cls.game_c = Game.objects.create(name="游戏丙")

    def setUp(self):
        # 失败限流计数在进程内缓存里：每个用例从零开始，不背上上一个用例的账。
        cache.clear()
        # 人事 = 持 `system_user.add_edit` 节点的普通账号（⭐ 经它的 Group → Role 挂节点）。
        self.hr = make_logged_in_client(
            username="selfreg-hr", groups=("人事",), nodes=("system_user.add_edit",)
        )
        self.public = Client(SERVER_NAME=DOMAIN)

    # ── 夹具助手 ──────────────────────────────────────────────────
    def save_config(self, games, forbid=False, number_prefix=None, number_start=None):
        body = dict(game_ids=[g.pk for g in games], forbid_orders_default=forbid)
        if number_prefix is not None:
            body["number_prefix"] = number_prefix
        if number_start is not None:
            body["number_start"] = number_start
        resp = self.hr.put(CONFIG_URL, data=body, content_type="application/json").json()
        self.assertEqual(resp["code"], 0, resp)
        return resp

    def create_invite(self, games, forbid=False):
        """按给定池生成一条链接，返回 `(明文 token, 库里的邀请行)`。

        ⚠️ 只重存**游戏池与开关**：编号两栏先 GET 现值再原样带回（PUT 缺席 = 重置，
        直发会把本用例配好的编号抹掉）——与桌面配置页「GET 回来什么就交回什么」同形。
        """
        current = self.hr.get(CONFIG_URL).json()["data"]
        body = self.hr.put(
            CONFIG_URL,
            data={
                "game_ids": [g.pk for g in games],
                "forbid_orders_default": forbid,
                "number_prefix": current.get("number_prefix", ""),
                "number_start": current.get("number_start"),
            },
            content_type="application/json",
        ).json()
        self.assertEqual(body["code"], 0, body)
        body = self.hr.post(INVITES_URL).json()
        self.assertEqual(body["code"], 0, body)
        token = body["data"]["token"]
        row = SelfRegistrationInvite.objects.get(token_hash=hash_token(token))
        return token, row

    @staticmethod
    def register_payload(
        token, game_ids, mobile=MOBILE, alipay_account=ALIPAY_ACCOUNT, alipay_name=ALIPAY_NAME
    ):
        return {
            "tk": token,
            "mobile": mobile,
            "alipay_account": alipay_account,
            "alipay_name": alipay_name,
            "game_ids": game_ids,
        }

    def register(self, token, game_ids, mobile=MOBILE, **kwargs):
        return self.public.post(
            REGISTER_URL,
            data=self.register_payload(token, game_ids, mobile=mobile, **kwargs),
            content_type="application/json",
        ).json()


class SelfRegistrationConfigTests(SelfRegistrationTestsBase):
    """判据 1（配置的读写回路与空池拒绝）+ 判据 3 的配置半（编号两栏的契约与校验）。"""

    def test_put_then_get_round_trips_games_and_switch(self):
        # E-0021：开关 `False` 也是合法值——先存一遍 False 再读，不能把 False 读成"没配"。
        self.save_config([self.game_a], forbid=False)
        body = self.hr.get(CONFIG_URL).json()
        self.assertEqual(body["data"]["game_ids"], [self.game_a.pk])
        self.assertIs(body["data"]["forbid_orders_default"], False)

        # 再存 True：布尔是**写进去的值**，不是"有没有这个键"。
        self.save_config([self.game_b, self.game_c], forbid=True)
        body = self.hr.get(CONFIG_URL).json()
        self.assertEqual(body["data"]["game_ids"], [self.game_b.pk, self.game_c.pk])
        self.assertIs(body["data"]["forbid_orders_default"], True)

    def test_number_fields_round_trip_and_absent_keys_reset_to_unconfigured(self):
        """编号两栏（spec §2.1）：PUT 带 ⇒ 原样存读；PUT 不带 ⇒ 按没配存（可选字段的语义）。"""
        self.save_config(
            [self.game_a], number_prefix="sq", number_start=20001
        )
        data = self.hr.get(CONFIG_URL).json()["data"]
        self.assertEqual(data["number_prefix"], "sq")
        self.assertEqual(data["number_start"], 20001)

        # 只带 game_ids 的旧形状调用（不提编号两键）⇒ 两列归位「没配」，
        # ⛔ 不是把旧值留在库里（缺省即重置，契约对前端才可预期）。
        self.save_config([self.game_a])
        data = self.hr.get(CONFIG_URL).json()["data"]
        self.assertEqual(data["number_prefix"], "")
        self.assertIsNone(data["number_start"])

    def test_put_rejects_invalid_number_prefix_and_non_positive_start(self):
        """判据 3 的配置半：非法前缀（数字 / 中文 / 超长）与 <1 的起始号响亮拒绝。"""
        for bad_payload, expected_msg in (
            ({"number_prefix": "sq1"}, PREFIX_INVALID_MSG),
            ({"number_prefix": "中文前缀"}, PREFIX_INVALID_MSG),
            ({"number_prefix": "toolong"}, PREFIX_INVALID_MSG),
            ({"number_start": 0}, START_INVALID_MSG),
            ({"number_start": -3}, START_INVALID_MSG),
        ):
            with self.subTest(payload=bad_payload):
                body = self.hr.put(
                    CONFIG_URL,
                    data={
                        "game_ids": [self.game_a.pk],
                        "forbid_orders_default": False,
                        **bad_payload,
                    },
                    content_type="application/json",
                ).json()
                self.assertEqual(body["code"], 1000, body)
                self.assertEqual(body["msg"], expected_msg)
        # 被拒的 PUT 一列都不许改（断在库行上，E-0009）：配置行没建过 ⇒ 依旧没有。
        self.assertFalse(SelfRegistrationConfig.objects.filter(pk=1).exists())

    def test_put_rejects_an_empty_game_pool_and_unknown_games(self):
        before = self.hr.get(CONFIG_URL).json()["data"]
        for bad_payload, expected_msg_part in (
            ({"game_ids": [], "forbid_orders_default": False}, "game_ids"),
            # 「游戏不存在：[999999]」与 `AccountWriteSerializer.validate_games` 同一份文案
            # （同域同语义，不另造第二句）；判据钉的是那个越界的 id 真被指出来。
            ({"game_ids": [self.game_a.pk, 999999], "forbid_orders_default": True}, "999999"),
        ):
            with self.subTest(payload=bad_payload):
                body = self.hr.put(
                    CONFIG_URL, data=bad_payload, content_type="application/json"
                ).json()
                self.assertEqual(body["code"], 1000, body)
                self.assertIn(expected_msg_part, body["msg"])
        # 被拒的 PUT 一行都不许改（断在数据库行上，E-0009）。
        after = self.hr.get(CONFIG_URL).json()["data"]
        self.assertEqual(after, before)


class SelfRegistrationInviteTests(SelfRegistrationTestsBase):
    """判据 2–5（令牌 / 快照 / 有效期 / 节点门与匿名）。"""

    def test_post_invites_returns_token_once_and_stores_only_the_hash(self):
        self.save_config([self.game_a])
        body = self.hr.post(INVITES_URL).json()
        self.assertEqual(body["code"], 0, body)
        token = body["data"]["token"]
        # `token_urlsafe(32)` 的长度就是 43（spec §7-2「43 字符级」的字面钉子）。
        self.assertEqual(len(token), 43)

        row = SelfRegistrationInvite.objects.get()
        # 库里存的是 sha256（算法复用 peer_sync.hash_token），不是明文。
        self.assertEqual(row.token_hash, hash_token(token))
        self.assertNotEqual(row.token_hash, token)
        # 全字段扫描：明文令牌不落库（ ⛔ 不写日志 / 不写别的列）。
        for field in SelfRegistrationInvite._meta.concrete_fields:
            value = getattr(row, field.name)
            self.assertNotIn(
                token, "" if value is None else str(value),
                f"明文令牌出现在列 {field.name} 里",
            )

    def test_invite_snapshots_the_config_at_generation_time(self):
        token1, row1 = self.create_invite([self.game_a], forbid=False)
        # 生成第二条**之前**改配置：第一条的池与开关必须停在生成那一刻。
        token2, row2 = self.create_invite([self.game_b, self.game_c], forbid=True)

        self.assertEqual(set(row1.games.values_list("pk", flat=True)), {self.game_a.pk})
        self.assertIs(row1.forbid_orders_default, False)
        self.assertEqual(
            set(row2.games.values_list("pk", flat=True)),
            {self.game_b.pk, self.game_c.pk},
        )
        self.assertIs(row2.forbid_orders_default, True)

    def test_invite_expires_seven_days_after_creation(self):
        _, row = self.create_invite([self.game_a])
        # auto_now_add 与 now+7d 之间有亚秒级间隙，容差 5 秒（不是"约等于 7 天"）。
        gap = row.expires_at - row.created_at
        self.assertLessEqual(abs(gap - timedelta(days=7)), timedelta(seconds=5))

    def test_invites_list_is_a_flat_array_with_the_contract_row_keys(self):
        """台账契约（2026-10-16 与前端线对齐）：`data` 就是行数组，不套分页信封。

        行字段**恰好** 8 个键、status 用三个英文字面值（E-0024：键名与取值都是
        字面副本，不从视图常量派生）。顺带钉「最新在前」与未消费行的空形态。
        """
        LIST_ROW_KEYS = {
            "id", "created_at", "created_by_name", "status", "expires_at",
            "consumed_at", "consumed_by_name", "chosen_game_names",
        }
        token1, row1 = self.create_invite([self.game_a])
        self.create_invite([self.game_b], forbid=True)
        # 消费第一条 ⇒ 台账里两条行各有各的状态。
        self.register(token1, [self.game_a.pk])

        body = self.hr.get(INVITES_URL).json()
        self.assertEqual(body["code"], 0, body)
        rows = body["data"]
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 2)
        self.assertEqual({key for row in rows for key in row}, LIST_ROW_KEYS)
        statuses = {row["status"] for row in rows}
        self.assertEqual(statuses, {"used", "unused"})
        # 最新在前：第一行是后生成的那条（未消费，勾选游戏为空表）。
        self.assertEqual(rows[0]["status"], "unused")
        self.assertEqual(rows[0]["chosen_game_names"], [])
        self.assertIsNone(rows[0]["consumed_at"])
        self.assertIsNone(rows[0]["consumed_by_name"])
        self.assertEqual(rows[0]["created_by_name"], "selfreg-hr")
        # 已用行：勾选游戏与注册人落值。
        used_row = next(row for row in rows if row["status"] == "used")
        self.assertEqual(used_row["chosen_game_names"], ["游戏甲"])
        self.assertIsNotNone(used_row["consumed_at"])
        self.assertEqual(used_row["consumed_by_name"], ALIPAY_NAME)

    def test_invite_list_time_fields_are_project_timezone_strings(self):
        """D-0302（2026-10-24）：台账行的三个时刻键是**项目时区**（+08:00）的 ISO 串。

        管理端自助注册页「切字符」直显；旧写法把裸 datetime 交给信封渲染成 UTC 串、
        整列差 8 小时。夹具把一条链接的三个时刻钉在固定值（UTC 04:30 ⇒ 北京 12:30）。
        """
        from django.utils.dateparse import parse_datetime

        _token, row = self.create_invite([self.game_a])
        fixed = parse_datetime("2026-10-24T04:30:00+00:00")
        SelfRegistrationInvite.objects.filter(pk=row.pk).update(
            created_at=fixed,
            expires_at=fixed + timedelta(days=7),
        )
        rows = self.hr.get(INVITES_URL).json()["data"]
        target = next(r for r in rows if r["id"] == row.pk)
        self.assertEqual(target["created_at"], "2026-10-24T12:30:00+08:00")
        self.assertEqual(target["expires_at"], "2026-10-31T12:30:00+08:00")
        self.assertIsNone(target["consumed_at"])  # 未消费 ⇒ null（形状不变）
        # 生成弹窗的 `expires_at` 同口径（POST 创建响应；POST 不收池——池来自配置快照）。
        gen = self.hr.post(INVITES_URL).json()
        self.assertEqual(gen["code"], 0, gen)
        self.assertTrue(gen["data"]["expires_at"].endswith("+08:00"), gen["data"])

    def test_pool_empty_rejects_link_generation(self):
        # 配置行存在但池为空 ⇒ 生成链接是业务错误，且一行都不落。
        # ⚠️ 池为空**进不来 PUT**（写端序列化器强制 game_ids ≥1，判据 1 已钉），
        # 这个状态只能经由别的路径出现（比如先配过再被旁路清掉）——所以这里
        # 直接把配置行的池清空来构造它，端点行为（生成时拒）不受影响。
        from apps.accounts.models import SelfRegistrationConfig

        SelfRegistrationConfig.objects.get_or_create(pk=1)
        SelfRegistrationConfig.objects.get(pk=1).games.clear()
        body = self.hr.post(INVITES_URL).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("游戏", body["msg"])
        self.assertEqual(SelfRegistrationInvite.objects.count(), 0)

    def test_config_and_invites_reject_missing_node_and_anonymous(self):
        # 无节点用户（登录了、但没挂 `system_user.add_edit`）⇒ 403 + 账号域那句拒人文案。
        # ⚠️ 不能与 `selfreg-hr` 同组：夹具把节点挂在 **Group 对应的 Role** 上，
        # 同组就等于同权（这正是本用例要证的反面）。用一颗任何用例都不发节点的组。
        no_node = make_logged_in_client(username="selfreg-no-node", groups=("访客",))
        for method, url, data in (
            (no_node.get, CONFIG_URL, None),
            (no_node.put, CONFIG_URL,
             {"game_ids": [self.game_a.pk], "forbid_orders_default": False}),
            (no_node.post, INVITES_URL, None),
        ):
            with self.subTest(url=url):
                if data is not None:
                    response = method(url, data=data, content_type="application/json")
                else:
                    response = method(url)
                self.assertEqual(response.status_code, 403, response.content[:200])
                self.assertEqual(response.json()["msg"], NODE_DENIED_MSG)
        # 匿名 ⇒ 401（`IsAuthenticated` 排在节点门之前）。
        for method, url in ((self.public.get, CONFIG_URL), (self.public.post, INVITES_URL)):
            with self.subTest(url=url):
                response = method(url)
                self.assertEqual(response.status_code, 401, response.content[:200])


class SelfRegistrationRevokeTests(SelfRegistrationTestsBase):
    """D-0253（2026-10-18）：链接台账的「作废」。

    口径（业务方「可以修复」）：① 作废 = **立即过期**——`expires_at` 置为
    当前时刻，**零迁移**（不新增列 / 状态值，「已作废」在台账上就是「已过期」的一种，
    `_invite_status` 的推导式一个字不改）；② 只对**未消费且未过期**的行可作废，
    已消费 / 已过期（含已作废）⇒ 明确业务文案，不 500、不二次改写；③ 权限走台账
    既有的 `_SelfRegNodeGateMixin` 同一节点门；④ 新写端点进
    `tests/test_operation_log_ledger.py` 的 INSTRUMENTED 钉子（同一次改）。
    """

    def revoke(self, pk):
        return self.hr.post(f"{INVITES_URL}{pk}/revoke/")

    def test_revoke_expires_the_link_at_once_and_both_public_endpoints_die(self):
        """作废成功：expires_at=当前时刻、公开 GET 回 valid=false、POST 注册被拒且零落库。"""
        token, row = self.create_invite([self.game_a])
        body = self.revoke(row.pk).json()
        self.assertEqual(body["code"], 0, body)

        row.refresh_from_db()
        # 作废 = expires_at 被置为「当前时刻」（容差同 invite TTL 钉子：亚秒级间隙）。
        self.assertLessEqual(row.expires_at, timezone.now())
        self.assertGreater(row.expires_at, timezone.now() - timedelta(seconds=5))
        # 未消费行才允许作废 ⇒ consumed 侧一列都不动。
        self.assertIsNone(row.consumed_at)
        self.assertIsNone(row.consumed_by)

        # 台账上这一行落地为「已过期」（推导式没改；前端照旧不画作废按钮）。
        listing = self.hr.get(INVITES_URL).json()["data"]
        self.assertEqual([r["id"] for r in listing], [row.pk])
        self.assertEqual(listing[0]["status"], "expired")

        # 公开 GET 校验接口回 valid=false（作废后，持有链接的人拿不到注册首屏）。
        self.assertIs(
            self.public.get(f"{REGISTER_URL}?tk={token}").json()["data"]["valid"], False
        )
        # 公开 POST 注册被拒（失效文案），一行都不落：没有新账号、链接没被消费。
        before_users = User.objects.count()
        reg = self.register(token, [self.game_a.pk])
        self.assertEqual(reg["code"], 1000, reg)
        self.assertEqual(reg["msg"], LINK_INVALID_MSG)
        self.assertEqual(User.objects.count(), before_users)
        row.refresh_from_db()
        self.assertIsNone(row.consumed_at)

        # 留痕：恰好一条 `user.selfreg_revoke`，actor = 点作废的人事、目标 = 那条链接行。
        log = OperationLog.objects.get(action=REVOKE_ACTION)
        self.assertEqual(log.actor_id, User.objects.get(username="selfreg-hr").pk)
        # `target_id` 是 CharField（对标「相关ID」列），落的是字符串形态。
        self.assertEqual(log.target_id, str(row.pk))

    def test_the_revoke_log_reason_stamps_created_at_in_the_project_timezone(self):
        """D-0302（2026-10-24）：作废留痕 reason 里的「生成于」时刻是**项目时区**墙钟。

        夹具把 `created_at` 钉在 UTC 2026-10-24 04:30 ⇒ reason 应为「2026-10-24 12:30」
        （北京墙钟）；旧写法裸 `strftime` 出 UTC 的 04:30、审计台账上差 8 小时。
        合成时刻（E-0077），秒级之后的部分由 `strftime("%Y-%m-%d %H:%M")` 截掉。
        """
        from django.utils.dateparse import parse_datetime

        token, row = self.create_invite([self.game_a])
        fixed = parse_datetime("2026-10-24T04:30:00+00:00")
        SelfRegistrationInvite.objects.filter(pk=row.pk).update(created_at=fixed)
        self.revoke(row.pk)
        log = OperationLog.objects.get(action=REVOKE_ACTION)
        self.assertEqual(log.reason, "2026-10-24 12:30")
        # ⛔ 不是 UTC 墙钟（防回退）。
        self.assertNotIn("04:30", log.reason)

    def test_only_an_unconsumed_unexpired_link_can_be_revoked(self):
        """已消费 / 已过期 / 重复作废 / 查无此行 ⇒ 明确业务文案，零写库、零留痕。"""
        # 已消费 ⇒ 「已被使用，无法作废」，expires_at / consumed_at 都不动。
        token, row = self.create_invite([self.game_a])
        self.register(token, [self.game_a.pk])
        row.refresh_from_db()
        expires_before = row.expires_at
        body = self.revoke(row.pk).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], REVOKE_USED_MSG)
        row.refresh_from_db()
        self.assertEqual(row.expires_at, expires_before)
        self.assertIsNotNone(row.consumed_at)
        self.assertEqual(OperationLog.objects.filter(action=REVOKE_ACTION).count(), 0)

        # 已过期 ⇒ 「已过期，无需作废」，expires_at 不被二次改写。
        _, expired_row = self.create_invite([self.game_b])
        stale = timezone.now() - timedelta(days=1)
        SelfRegistrationInvite.objects.filter(pk=expired_row.pk).update(expires_at=stale)
        body = self.revoke(expired_row.pk).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], REVOKE_EXPIRED_MSG)
        expired_row.refresh_from_db()
        self.assertEqual(expired_row.expires_at, stale)

        # 已作废 = 过期的一种 ⇒ 重复作废落在「无需作废」那一档（不 500、不二次改写）。
        _, fresh = self.create_invite([self.game_c])
        self.assertEqual(self.revoke(fresh.pk).json()["code"], 0)
        fresh.refresh_from_db()
        revoked_at = fresh.expires_at
        body = self.revoke(fresh.pk).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], REVOKE_EXPIRED_MSG)
        fresh.refresh_from_db()
        self.assertEqual(fresh.expires_at, revoked_at)
        # 成功的那一次恰好留一行痕（上面拒绝的三次都不留）。
        self.assertEqual(OperationLog.objects.filter(action=REVOKE_ACTION).count(), 1)

        # 查无此行（本租户 schema 里不存在）⇒ 业务 1000，不是 500。
        body = self.revoke(99999999).json()
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], REVOKE_NOT_FOUND_MSG)
        self.assertEqual(OperationLog.objects.filter(action=REVOKE_ACTION).count(), 1)

    def test_revoke_requires_login_and_the_same_node(self):
        """无节点 ⇒ 403 + 账号域那句拒人文案；匿名 ⇒ 401；被拒的两跳零写库、零留痕。"""
        _, row = self.create_invite([self.game_a])
        # ⚠️ 不能与 `selfreg-hr` 同组：节点挂在 **Group 对应的 Role** 上（同组 = 同权，
        # 正是本用例要证的反面）——照 `test_config_and_invites_reject_missing_node_and_anonymous`
        # 的同一写法，用一颗任何用例都不发节点的组。
        no_node = make_logged_in_client(username="selfreg-revoke-no-node", groups=("访客",))
        response = no_node.post(f"{INVITES_URL}{row.pk}/revoke/")
        self.assertEqual(response.status_code, 403, response.content[:200])
        self.assertEqual(response.json()["msg"], NODE_DENIED_MSG)
        response = self.public.post(f"{INVITES_URL}{row.pk}/revoke/")
        self.assertEqual(response.status_code, 401, response.content[:200])
        row.refresh_from_db()
        self.assertGreater(row.expires_at, timezone.now())
        self.assertEqual(OperationLog.objects.filter(action=REVOKE_ACTION).count(), 0)


class SelfRegisterPublicReadTests(SelfRegistrationTestsBase):
    """判据 6（公开首屏的有效 / 无效判定）。"""

    def test_valid_link_reports_tenant_name_pool_and_expiry(self):
        token, row = self.create_invite([self.game_a, self.game_c], forbid=False)
        body = self.public.get(f"{REGISTER_URL}?tk={token}").json()
        self.assertEqual(body["code"], 0, body)
        data = body["data"]
        self.assertIs(data["valid"], True)
        self.assertEqual(data["tenant_name"], self.tenant.name)
        self.assertEqual(
            data["games"], [{"id": self.game_a.pk, "name": "游戏甲"},
                            {"id": self.game_c.pk, "name": "游戏丙"}]
        )
        # D-0302：`expires_at` 出**项目时区**墙钟（`+08:00`，`local_iso`）。
        self.assertEqual(data["expires_at"][:19], timezone.localtime(row.expires_at).isoformat()[:19])

    def test_used_expired_or_bogus_tokens_report_invalid(self):
        token, row = self.create_invite([self.game_a])

        # 已用。
        SelfRegistrationInvite.objects.filter(pk=row.pk).update(
            consumed_at=timezone.now(), consumed_by=User.objects.get(username="admin")
        )
        self.assertIs(self.public.get(f"{REGISTER_URL}?tk={token}").json()["data"]["valid"], False)
        # 已过期。
        SelfRegistrationInvite.objects.filter(pk=row.pk).update(
            consumed_at=None, consumed_by=None, expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.assertIs(self.public.get(f"{REGISTER_URL}?tk={token}").json()["data"]["valid"], False)
        # 瞎编 / 缺参。
        self.assertIs(self.public.get(f"{REGISTER_URL}?tk=garbage").json()["data"]["valid"], False)
        self.assertIs(self.public.get(REGISTER_URL).json()["data"]["valid"], False)


class SelfRegisterFlowTests(SelfRegistrationTestsBase):
    """判据 7–12（注册主流程：成功行、挑子集、一次性、过期、占用、回滚）。"""

    def test_successful_register_writes_every_row_and_returns_initial_password(self):
        token, invite = self.create_invite([self.game_a, self.game_b, self.game_c], forbid=True)
        hr_pk = User.objects.get(username="selfreg-hr").pk
        before_users = User.objects.count()

        body = self.register(token, [self.game_a.pk, self.game_c.pk])
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["username"], MOBILE)
        self.assertEqual(body["data"]["initial_password"], INITIAL_PASSWORD)
        # 未配置编号 ⇒ 响应的「账号名称」= 支付宝姓名（spec §2.1 的回落形态）。
        self.assertEqual(body["data"]["display_name"], ALIPAY_NAME)

        user = User.objects.get(username=MOBILE)
        self.assertEqual(User.objects.count(), before_users + 1)
        # 字段逐列对账（期望值是字面副本，E-0024）。username 仍 = 手机号（⛔ 登录口径不动）。
        self.assertEqual(user.username, MOBILE)
        # 未配置编号 ⇒ 显示名回落支付宝姓名。
        self.assertEqual(user.first_name, ALIPAY_NAME)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password(INITIAL_PASSWORD))
        self.assertEqual(
            set(user.groups.values_list("name", flat=True)), {"代练员"}
        )
        profile = StaffProfile.objects.get(user=user)
        self.assertEqual(profile.mobile, MOBILE)
        self.assertEqual(profile.opened_by_id, hr_pk)
        # ⭐ 来源 = 链接自助注册（D-0277 的写入点判据；字面值、⛔ 不 import 枚举）。
        # 它与 App 注册**不同源**：网页登录口只拦 `app`，本档照常能登网页端。
        self.assertEqual(profile.account_source, "link")
        # 新人代练持单上限 = 3（业务方拍定；期望值写字面量、⛔ 不 import 常量，
        # 否则常量改了测试跟着绿，这条钉子就恒真了——E-0024）。
        self.assertEqual(profile.grab_limit_dl, 3)
        # 收款支付宝两栏落列（spec §2.1，判据 1 的对账腿）。
        self.assertEqual(profile.alipay_account, ALIPAY_ACCOUNT)
        self.assertEqual(profile.alipay_name, ALIPAY_NAME)
        # 开关 = 链接快照（本用例快照是 True，让它真正承重）。
        self.assertIs(profile.grab_forbid_self_service, True)
        self.assertEqual(
            set(profile.games.values_list("pk", flat=True)),
            {self.game_a.pk, self.game_c.pk},
        )

        invite.refresh_from_db()
        self.assertIsNotNone(invite.consumed_at)
        self.assertEqual(invite.consumed_by_id, user.pk)
        self.assertEqual(
            set(invite.chosen_games.values_list("pk", flat=True)),
            {self.game_a.pk, self.game_c.pk},
        )

        # 审计：一条 user.create + 逐游戏 user.game_permission_add（actor = 生成链接的人事）。
        created_rows = OperationLog.objects.filter(action="user.create", target_name=MOBILE)
        self.assertEqual(created_rows.count(), 1)
        self.assertEqual(created_rows.get().actor_id, hr_pk)
        game_rows = OperationLog.objects.filter(
            action="user.game_permission_add", target_name=MOBILE
        ).order_by("game_name")
        self.assertEqual(
            [r.game_name for r in game_rows], ["游戏丙", "游戏甲"]  # 按 game_name 升序
        )

    def test_register_accepts_a_subset_of_the_invite_pool(self):
        token, invite = self.create_invite([self.game_a, self.game_b, self.game_c])
        body = self.register(token, [self.game_a.pk, self.game_c.pk])
        self.assertEqual(body["code"], 0, body)
        user = User.objects.get(username=MOBILE)
        self.assertEqual(
            set(user.staff_profile.games.values_list("pk", flat=True)),
            {self.game_a.pk, self.game_c.pk},
        )
        invite.refresh_from_db()
        self.assertEqual(
            set(invite.chosen_games.values_list("pk", flat=True)),
            {self.game_a.pk, self.game_c.pk},
        )

    def test_empty_or_outside_pool_selections_are_rejected_without_consuming(self):
        token, invite = self.create_invite([self.game_a, self.game_b, self.game_c])
        outsider = Game.objects.create(name="池外的游戏丁")
        before_users = User.objects.count()
        for bad_game_ids in ([], [outsider.pk]):
            with self.subTest(game_ids=bad_game_ids):
                body = self.register(token, bad_game_ids)
                self.assertEqual(body["code"], 1000, body)
                if bad_game_ids:
                    self.assertIn("game_ids", body["msg"])
                invite.refresh_from_db()
                self.assertIsNone(invite.consumed_at, "被拒的请求不许消费链接")
        self.assertEqual(User.objects.count(), before_users)

    def test_a_used_link_cannot_register_a_second_account(self):
        token, invite = self.create_invite([self.game_a])
        self.assertEqual(self.register(token, [self.game_a.pk])["code"], 0)
        before_users = User.objects.count()

        body = self.register(token, [self.game_a.pk], mobile="13800005678")
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], LINK_INVALID_MSG)
        self.assertEqual(User.objects.count(), before_users)

    def test_an_expired_link_fails_post_and_get(self):
        token, invite = self.create_invite([self.game_a])
        SelfRegistrationInvite.objects.filter(pk=invite.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        before_users = User.objects.count()
        body = self.register(token, [self.game_a.pk])
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], LINK_INVALID_MSG)
        self.assertIs(self.public.get(f"{REGISTER_URL}?tk={token}").json()["data"]["valid"], False)
        self.assertEqual(User.objects.count(), before_users)

    def test_existing_mobile_is_rejected_without_consuming_and_can_retry(self):
        token, invite = self.create_invite([self.game_a])
        User.objects.create_user(username=MOBILE, password="pw-occupied")
        before_users = User.objects.count()

        body = self.register(token, [self.game_a.pk])
        self.assertEqual(body["code"], 1000, body)
        self.assertEqual(body["msg"], MOBILE_TAKEN_MSG)
        invite.refresh_from_db()
        self.assertIsNone(invite.consumed_at, "手机号被占用的请求不许消费链接")
        self.assertEqual(User.objects.count(), before_users)

        # 可换人重试：换一个手机号，同一条链接照常能用。
        retry = self.register(token, [self.game_a.pk], mobile="13800009876")
        self.assertEqual(retry["code"], 0, retry)
        invite.refresh_from_db()
        self.assertIsNotNone(invite.consumed_at)

    def test_mid_transaction_failure_rolls_everything_back(self):
        token, invite = self.create_invite([self.game_a, self.game_c])
        before_users = User.objects.count()
        before_logs = OperationLog.objects.count()

        client = Client(SERVER_NAME=DOMAIN, raise_request_exception=False)
        # 注入点：消费那次 `invite.save()`。判定全打在**终态**上（E-0022）：
        # 邀请行未消费、无用户行、无档案行、无审计行。
        with mock.patch.object(
            SelfRegistrationInvite, "save", side_effect=RuntimeError("boom")
        ):
            response = client.post(
                REGISTER_URL,
                data=self.register_payload(token, [self.game_a.pk, self.game_c.pk]),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 500)
        invite.refresh_from_db()
        self.assertIsNone(invite.consumed_at)
        self.assertIsNone(invite.consumed_by_id)
        self.assertFalse(User.objects.filter(username=MOBILE).exists())
        self.assertEqual(User.objects.count(), before_users)
        self.assertFalse(StaffProfile.objects.filter(user__username=MOBILE).exists())
        self.assertEqual(OperationLog.objects.count(), before_logs)


class SelfRegisterNumberingTests(SelfRegistrationTestsBase):
    """判据 1–3（编号配置后的注册落库 / 递进 / 自愈 / 回落与半配置）。

    断言一律打在**数据库行**上（E-0009）；「username=手机号」的登录口径在每条里都顺带钉住
    ——编号只动显示名（`first_name`），⛔ 不动登录账号。
    """

    def _register_and_fetch(self, token, game_ids, mobile=MOBILE):
        body = self.register(token, game_ids, mobile=mobile)
        self.assertEqual(body["code"], 0, body)
        return body, User.objects.get(username=mobile)

    def test_configured_numbering_writes_number_as_first_name_and_alipay_columns(self):
        """判据 1：配置（prefix=sq、start=20001）后注册 ⇒ 显示名 sq20001、支付宝两栏落列。"""
        self.save_config([self.game_a], number_prefix="sq", number_start=20001)
        token, _invite = self.create_invite([self.game_a])

        body, user = self._register_and_fetch(token, [self.game_a.pk], mobile="13800001201")
        # 显示名 = 前缀+起始号（不补零：20001 就是 5 位）；username 仍是手机号（登录口径不动）。
        self.assertEqual(body["data"]["display_name"], "sq20001")
        self.assertEqual(user.first_name, "sq20001")
        self.assertEqual(user.username, "13800001201")
        profile = StaffProfile.objects.get(user=user)
        self.assertEqual(profile.alipay_account, ALIPAY_ACCOUNT)
        self.assertEqual(profile.alipay_name, ALIPAY_NAME)
        self.assertEqual(profile.mobile, "13800001201")

    def test_second_registration_advances_and_raised_start_is_respected(self):
        """判据 2：连续注册递进（max+1）；把 start 改大 ⇒ 从新的 start 起步。"""
        self.save_config([self.game_a], number_prefix="sq", number_start=20001)
        token1, _ = self.create_invite([self.game_a])
        self._register_and_fetch(token1, [self.game_a.pk], mobile="13800001201")

        # 第二次注册（新链接）⇒ 同前缀现有最大号+1 = 20002。
        token2, _ = self.create_invite([self.game_a])
        _body, user2 = self._register_and_fetch(token2, [self.game_a.pk], mobile="13800001202")
        self.assertEqual(user2.first_name, "sq20002")

        # 把 start 改大（30000）再注册 ⇒ 取 max(30000, 20002+1) = 30000（start 下限语义）。
        self.save_config([self.game_a], number_prefix="sq", number_start=30000)
        token3, _ = self.create_invite([self.game_a])
        _body, user3 = self._register_and_fetch(token3, [self.game_a.pk], mobile="13800001203")
        self.assertEqual(user3.first_name, "sq30000")

    def test_display_name_edited_by_hand_self_heals_without_collision(self):
        """判据 2 的自愈腿：显示名被人手改成 sq00010（号已被占）⇒ 下一号 11，不重号。"""
        self.save_config([self.game_a], number_prefix="sq", number_start=10)
        # 旁路写入：有人把某账号的显示名手改成了「sq00010」（占掉 10 号这个值）。
        squatter = User.objects.create_user(username="13800009999", password="x")
        squatter.first_name = "sq00010"
        squatter.save(update_fields=["first_name"])

        token, _ = self.create_invite([self.game_a])
        _body, user = self._register_and_fetch(token, [self.game_a.pk])
        # max(10, 10+1) = 11 —— 既尊重 start 下限，又越过被占的 10（自愈）。
        self.assertEqual(user.first_name, "sq11")
        # 前提自证：库里确实存在那个被占的号（否则「没重号」是空库凑出来的恒真）。
        self.assertTrue(User.objects.filter(first_name="sq00010").exists())

    def test_non_digit_tail_rows_are_skipped_by_the_scan(self):
        """旁路写入的尾巴不是纯数字（如 `sq-备用`）⇒ 扫描跳过它，注册不炸、编号照走。"""
        self.save_config([self.game_a], number_prefix="sq", number_start=7)
        odd = User.objects.create_user(username="13800008888", password="x")
        odd.first_name = "sq-备用"
        odd.save(update_fields=["first_name"])

        token, _ = self.create_invite([self.game_a])
        _body, user = self._register_and_fetch(token, [self.game_a.pk])
        self.assertEqual(user.first_name, "sq7")

    def test_overlong_digit_tail_is_ignored_by_the_scan(self):
        """尾巴是**超长数字**（>8 位，2026-10-24 某租户现场 E-0285）⇒ 扫描忽略、编号不被抬飞。

        有人把显示名手改成 `sq` + 14 位数字（误贴的外部单号），`max` 若照收，
        下一个号就是万亿级——正常编号（起始号 + 递增）到不了那个量级。
        """
        self.save_config([self.game_a], number_prefix="sq", number_start=10)
        poison = User.objects.create_user(username="13800007777", password="x")
        poison.first_name = "sq01201955619161"
        poison.save(update_fields=["first_name"])

        token, _ = self.create_invite([self.game_a])
        _body, user = self._register_and_fetch(token, [self.game_a.pk])
        # max(10, 0+1) = 10 —— 14 位尾巴不进 max。
        self.assertEqual(user.first_name, "sq10")
        # 前提自证：毒源行确实在库里（否则「忽略」是空库凑出来的恒真）。
        self.assertTrue(User.objects.filter(first_name="sq01201955619161").exists())

    def test_unconfigured_or_half_configured_falls_back_to_alipay_name(self):
        """判据 3：prefix 空 / 只填一列（半配置）⇒ 显示名 = 支付宝姓名（两列都非空才生效）。"""
        cases = (
            # 完全没配（PUT 不带两键，行存在但两列空）。
            {},
            # 只填 prefix 不填 start。
            {"number_prefix": "sq"},
            # 只填 start 不填 prefix。
            {"number_start": 20001},
        )
        for i, number_fields in enumerate(cases):
            with self.subTest(case=number_fields):
                self.save_config([self.game_a], **number_fields)
                token, _ = self.create_invite([self.game_a])
                body = self.register(
                    token, [self.game_a.pk], mobile=f"1380000130{i}"
                )
                self.assertEqual(body["code"], 0, body)
                user = User.objects.get(username=f"1380000130{i}")
                self.assertEqual(user.first_name, ALIPAY_NAME)
                self.assertEqual(body["data"]["display_name"], ALIPAY_NAME)

    def test_missing_alipay_fields_are_rejected_and_token_not_consumed(self):
        """判据 4：缺 alipay_account / alipay_name ⇒ 拒且令牌未消费；姓名栏已不存在。"""
        token, invite = self.create_invite([self.game_a])
        for missing, field in (
            (("alipay_account",), "alipay_account"),
            (("alipay_name",), "alipay_name"),
        ):
            with self.subTest(field=field):
                payload = self.register_payload(token, [self.game_a.pk])
                for key in missing:
                    payload.pop(key)
                before_users = User.objects.count()
                body = self.public.post(
                    REGISTER_URL, data=payload, content_type="application/json"
                ).json()
                self.assertEqual(body["code"], 1000, body)
                self.assertIn(field, body["msg"])
                invite.refresh_from_db()
                self.assertIsNone(invite.consumed_at, "被拒的请求不许消费链接")
                self.assertEqual(User.objects.count(), before_users)
        # 空串同样拒（必填不是「键在就行」）。
        body = self.register(token, [self.game_a.pk], alipay_name="   ")
        self.assertEqual(body["code"], 1000, body)
        self.assertIn("alipay_name", body["msg"])
        invite.refresh_from_db()
        self.assertIsNone(invite.consumed_at)

    def test_request_display_name_key_no_longer_exists(self):
        """判据 4 的另一半：`display_name` 不再是注册载荷的一部分——传了也**不落库**，
        显示名由服务端算。"""
        token, _ = self.create_invite([self.game_a])
        payload = self.register_payload(token, [self.game_a.pk])
        payload["display_name"] = "请求里塞进来的姓名"
        body = self.public.post(
            REGISTER_URL, data=payload, content_type="application/json"
        ).json()
        self.assertEqual(body["code"], 0, body)
        user = User.objects.get(username=MOBILE)
        # 未配置编号 ⇒ 显示名 = 支付宝姓名，而不是请求塞的那串。
        self.assertEqual(user.first_name, ALIPAY_NAME)


class SelfRegisterRateLimitTests(SelfRegistrationTestsBase):
    """T5（spec §2.6）：失败限流**按链接分桶**——跨链接隔离 / 阈值按链接 / malformed 共享桶。

    ⚠️ 判据口径（业务方原话的判据化）：一条链接被瞎试到阈值，
    **同租户的另一条链接必须照常能用**——整租户一个桶的旧语义已删。
    """

    @override_settings(SELF_REGISTER_MAX_FAILURES=2)
    def test_failures_are_bucketed_per_link_and_do_not_leak_across_links(self):
        """判据 5a/5b：链接 A 灌到阈值 ⇒ A 的后续失败回固定文案；**链接 B 照常注册成功**。"""
        token_a, _ = self.create_invite([self.game_a])
        token_b, _ = self.create_invite([self.game_b])
        garbage_a = self.register_payload("no-such-token-A", [self.game_a.pk])
        garbage_b = self.register_payload("no-such-token-B", [self.game_b.pk])

        # A 的桶灌满（失败 1、2）：业务失败文案，还不是限流。
        for _ in range(2):
            body = self.public.post(
                REGISTER_URL, data=garbage_a, content_type="application/json"
            ).json()
            self.assertEqual(body["msg"], LINK_INVALID_MSG, body)
        # A 第 3 次（达到阈值后）⇒ 固定限流文案。
        body = self.public.post(
            REGISTER_URL, data=garbage_a, content_type="application/json"
        ).json()
        self.assertEqual(body["msg"], RATE_LIMITED_MSG, body)

        # ⭐ B 完全不受 A 拖累：照常注册成功（跨链接隔离，业务方原话的判据化）。
        body = self.register(token_b, [self.game_b.pk], mobile="13800001401")
        self.assertEqual(body["code"], 0, body)
        self.assertTrue(User.objects.filter(username="13800001401").exists())
        # B 自己的失败也从 0 起算（失败 1 ⇒ 还是业务文案，不是限流文案）。
        body = self.public.post(
            REGISTER_URL, data=garbage_b, content_type="application/json"
        ).json()
        self.assertEqual(body["msg"], LINK_INVALID_MSG, body)

    @override_settings(SELF_REGISTER_MAX_FAILURES=2)
    def test_malformed_bodies_share_one_bucket(self):
        """T5-5c：tk 缺失 / 非字符串 / 顶层非 dict ⇒ 共享字面桶 `…:malformed`，达到阈值后回固定文案。"""
        malformed_bodies = (
            [1, 2, 3],  # 顶层非 dict
            {"mobile": MOBILE},  # tk 缺失
            {"tk": 123, "mobile": MOBILE},  # tk 非字符串
            {"tk": ""},  # tk 空串
        )
        # 前两次：业务失败文案，且每一次都计入**同一个** malformed 桶。
        for i, payload in enumerate(malformed_bodies[:2]):
            body = self.public.post(
                REGISTER_URL, data=payload, content_type="application/json"
            ).json()
            self.assertEqual(body["code"], 1000, body)
            self.assertEqual(body["msg"], LINK_INVALID_MSG, body)
            self.assertEqual(cache.get("self-register-fail:malformed"), i + 1)

        # 第 3 次（达到阈值 2）⇒ 限流文案；桶键与出示的令牌无关——
        # malformed 请求根本分不出令牌，走的就是共享桶。
        for payload in malformed_bodies[2:]:
            body = self.public.post(
                REGISTER_URL, data=payload, content_type="application/json"
            ).json()
            self.assertEqual(body["msg"], RATE_LIMITED_MSG, body)

    @override_settings(SELF_REGISTER_MAX_FAILURES=2)
    def test_failures_count_into_the_presented_tokens_bucket_even_when_bogus(self):
        """T5-5d 前半：瞎编的令牌有自己的桶（按出示的哈希分，不管存在与否）。"""
        from django.core.cache import cache as dj_cache

        garbage = self.register_payload("no-such-token", [self.game_a.pk])
        self.public.post(REGISTER_URL, data=garbage, content_type="application/json").json()
        expected_key = f"self-register-fail:{hash_token('no-such-token')}"
        self.assertEqual(cache.get(expected_key), 1)
        # 整租户桶已删（T5 第 4 条）：schema 形状的键**不许**再出现。
        self.assertIsNone(cache.get(f"self-register-fail:{self.tenant.schema_name}"))

    def test_successful_register_never_counts_and_clears_nothing_else(self):
        """T5-5d 后半：成功注册不计数（成功前后，该链接桶的计数不变）。"""
        token, _ = self.create_invite([self.game_a])
        self.assertEqual(self.register(token, [self.game_a.pk])["code"], 0)
        # 成功路径上没有一次失败计数：桶键压根不存在（不是 0 也不是 1）。
        self.assertIsNone(cache.get(f"self-register-fail:{hash_token(token)}"))
        # 成功之后再失败一次：计数从 1 起算（成功没吃掉阈值配额，也没有基数残留）。
        garbage = self.register_payload("no-such-token-after-success", [self.game_a.pk])
        body = self.public.post(
            REGISTER_URL, data=garbage, content_type="application/json"
        ).json()
        self.assertEqual(body["msg"], LINK_INVALID_MSG, body)

    @override_settings(SELF_REGISTER_MAX_FAILURES=2)
    def test_exhausted_link_bucket_blocks_only_that_link(self):
        """T5-5b：阈值按链接生效——A 第 3 次失败被挡，同刻 B 的首次失败仍是业务文案。"""
        token_a, _ = self.create_invite([self.game_a])
        self.create_invite([self.game_b])
        garbage_a = self.register_payload("no-such-token-A", [self.game_a.pk])
        for _ in range(3):
            self.public.post(REGISTER_URL, data=garbage_a, content_type="application/json").json()
        body = self.public.post(
            REGISTER_URL, data=garbage_a, content_type="application/json"
        ).json()
        self.assertEqual(body["msg"], RATE_LIMITED_MSG, body)
        # B 的首次瞎试：业务文案（B 的桶是空的）。
        garbage_b = self.register_payload("no-such-token-B", [self.game_b.pk])
        body = self.public.post(
            REGISTER_URL, data=garbage_b, content_type="application/json"
        ).json()
        self.assertEqual(body["msg"], LINK_INVALID_MSG, body)
        # GET 首屏不计数照旧（T5 第 4 条）：B 的桶仍是空的。
        self.public.get(f"{REGISTER_URL}?tk=whatever")
        self.assertIsNone(cache.get(f"self-register-fail:{hash_token('whatever')}"))


class SelfRegisterCrossTenantTests(SelfRegistrationTestsBase):
    """判据 16（A 的令牌在 B 的域名下必须是死的；B 内用户数不变）。

    ⚠️ B 里**故意**留一条 B 自己的有效链接：要证的是「按哈希精确命中本租户的行」，
    不是「这个 schema 里碰巧有活行就放行」——否则这条判据在 B 空库时是恒真的。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant_b, _ = provision_tenant(
            schema_name="selfreg_b", name="自助注册租户 B", domain="selfreg-b.test.local"
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据；显式再调一次不依赖这一点。
        seed_tenant(cls.tenant_b)
        with tenant_context(cls.tenant_b):
            SelfRegistrationInvite.objects.create(
                token_hash=hash_token("b-native-token"),
                created_by=User.objects.get(username="admin"),
                expires_at=timezone.now() + timedelta(days=7),
            )

    def test_a_tenants_token_is_dead_on_tenant_b(self):
        token, _invite = self.create_invite([self.game_a])

        # 正向对照：同一条令牌在 A 自己的域名下是活的（否则「B 拒绝」可能只是令牌坏了）。
        self.assertIs(
            self.public.get(f"{REGISTER_URL}?tk={token}").json()["data"]["valid"], True
        )

        client_b = Client(SERVER_NAME="selfreg-b.test.local")
        get_body = client_b.get(f"{REGISTER_URL}?tk={token}").json()
        self.assertIs(get_body["data"]["valid"], False)

        with tenant_context(self.tenant_b):
            before_users = User.objects.count()
        post_body = client_b.post(
            REGISTER_URL,
            data=self.register_payload(token, [self.game_a.pk]),
            content_type="application/json",
        ).json()
        self.assertEqual(post_body["code"], 1000, post_body)
        self.assertEqual(post_body["msg"], LINK_INVALID_MSG)
        with tenant_context(self.tenant_b):
            self.assertEqual(User.objects.count(), before_users)


class SelfRegisterNumberingConcurrencyTests(TransactionTestCase):
    """判据 5（编号并发）：两条**真实连接**同时注册 ⇒ 编号不重复（锁配置行的语义）。

    ⚠️ 形态（如实登记，spec §2.1 给实现者二选一）：**真线程并发 + 终态断言**，
    与 `tests/test_realname_package_command.py` 的并发 grant、
    `tests/test_account_concurrency.py` 同一先例——`TenantTestCase` 的外层事务让两条线程
    看不到彼此，并发根本不发生 ⇒ 必须自己搭 schema 的建/拆并跑在 autocommit 上。

    ⚠️ **判别力**（本用例为什么要存在）：编号 = `max(start, 同前缀最大号+1)` 是一次
    「读-算-写」，不锁配置行时，两条线程都可能在对方提交**之前**扫库 ⇒ 同号。
    正确实现（`_generate_display_name` 里 `select_for_update()`）让后到的线程在行锁上
    排队、重新扫库，编号必然错开。弄红自证见本文件底部台账（摘掉锁后本用例红）。
    """

    DOMAIN = "tenant.test.com"
    BARRIER_TIMEOUT = 15.0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tenant = get_tenant_model()(schema_name="test")
        cls.tenant.save(verbosity=0)
        cls.domain = get_tenant_domain_model()(tenant=cls.tenant, domain=cls.DOMAIN)
        cls.domain.save()
        if cls.DOMAIN not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS += [cls.DOMAIN]
        connection.set_tenant(cls.tenant)
        seed_tenant(cls.tenant)
        Game.objects.create(name="并发游戏甲")

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        cls.domain.delete()
        cls.tenant.delete(force_drop=True)
        if cls.DOMAIN in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(cls.DOMAIN)
        super().tearDownClass()

    def setUp(self):
        # 前提自证：确实跑在**无外层事务**的形态上（否则并发没有判别力）。
        self.assertTrue(
            connection.get_autocommit(),
            "本用例没跑在 autocommit 上——检查基类是否被换成了 TenantTestCase",
        )
        connection.set_tenant(self.tenant)
        SelfRegistrationConfig.objects.get_or_create(pk=1)
        SelfRegistrationConfig.objects.filter(pk=1).update(
            number_prefix="sq", number_start=20001
        )
        User.objects.filter(first_name__startswith="sq").delete()
        cache.clear()

    def _make_invite(self, token_plain: str) -> None:
        """直插一条未消费链接（绕开「生成人必须存在」的审计路径，纯夹具）。

        ⚠️ 池必须挂上：注册的 `game_ids ⊆ 链接快照池` 判据只认**链接行上的**
        games——绕开 invites 端点直插的行不会自动带池。
        """
        invite = SelfRegistrationInvite.objects.create(
            token_hash=hash_token(token_plain),
            created_by=User.objects.get(username="admin"),
            expires_at=timezone.now() + timedelta(days=7),
        )
        invite.games.set(Game.objects.all())

    def _register_in_thread(self, token_plain, mobile, results, key) -> None:
        def worker() -> None:
            try:
                close_old_connections()
                connection.set_tenant(self.tenant)
                client = Client(SERVER_NAME=self.DOMAIN)
                response = client.post(
                    REGISTER_URL,
                    data={
                        "tk": token_plain,
                        "mobile": mobile,
                        "alipay_account": "synth-alipay-conc",
                        "alipay_name": "并发打手",
                        "game_ids": [Game.objects.get().pk],
                    },
                    content_type="application/json",
                )
                results[key] = response.json()
            except Exception as exc:  # noqa: BLE001 —— 结果要回主线程断言
                results[key] = {"code": "thread-error", "msg": f"{type(exc).__name__}: {exc}"}
            finally:
                connection.close()

        thread = threading.Thread(target=worker)
        thread.start()
        return thread

    def test_concurrent_registrations_get_distinct_numbers(self):
        """两条线程同时注册（各自的有效链接）⇒ 编号恰好是 {20001, 20002}，无重号。"""
        self._make_invite("conc-token-alpha")
        self._make_invite("conc-token-beta")
        results: dict = {}
        threads = []
        for key, (token_plain, mobile) in {
            "alpha": ("conc-token-alpha", "13800002001"),
            "beta": ("conc-token-beta", "13800002002"),
        }.items():
            threads.append(self._register_in_thread(token_plain, mobile, results, key))
        for t in threads:
            t.join(timeout=self.BARRIER_TIMEOUT)

        # 两条线程都真的跑完了（⛔ 挂起 / 异常不许伪装成「读数不对」）。
        self.assertFalse(any(t.is_alive() for t in threads), "并发注册挂住了")
        self.assertEqual(
            {key: body.get("code") for key, body in results.items()},
            {"alpha": 0, "beta": 0},
            results,
        )
        names = sorted(
            User.objects.filter(username__in=["13800002001", "13800002002"]).values_list(
                "first_name", flat=True
            )
        )
        # ⭐ 终态：恰好 sq20001 与 sq20002，各一个、无重号。
        self.assertEqual(names, ["sq20001", "sq20002"])


# ── 弄红自证台账（判据编号 ↔ 变异 ↔ 结果；对应模块 docstring 里「见提交说明与本文件
#    底部注释」的承诺）─────────────────────────────────────
# ── 本线（intake-realname-batch）新增弄红台账（还原一律快照写回逐字节比对，⛔ git checkout --；crashed 不算 killed）──
# 判据1 M18 编号生成整体短路（_generate_display_name 恒回 alipay_name）→ SelfRegisterNumberingTests 4条红✓｜
# 判据2 M19 _max_existing_number 改恒回 0 → 自愈+递进两条红✓｜判据3 M20 半配置判据删掉 start is None 支 → 回落用例红✓｜
# 判据4 M21 alipay_account 改 required=False → 缺字段用例红✓｜判据5 M22 摘掉配置行 select_for_update → 并发用例 3/3 红✓（无重号判据有判别力）｜
# 判据10 M23 profile 建行删 alipay 两列 → 回归网主用例红✓（载荷变了判据网同次改）｜
# T5（判据12）M24 限流键改回 schema 桶 → 跨链接隔离/阈值按链接/瞎令牌桶 3条红✓。
# 判据1 M1 PUT不写config.games.set→红✓｜判据2 M2 token_hash改存明文→红✓｜判据3 M3 生成不做games.set(pool)快照→红✓｜判据4 M4 TTL常量7→8→红✓｜判据5 M5 节点门换AllowAny→红✓(4子用例)｜判据6 M6 GET摘掉已用判定→红✓｜判据7/15 M7 注册不写opened_by=invite.created_by→红✓｜判据8 M8 摘掉⊆池校验→红✓(2子用例)｜判据9 M9 POST摘掉已用判定→红✓｜判据10 M10 POST摘掉已过期判定→红✓｜判据11 M11 摘掉手机号占用预检→红✓(落IntegrityError分支文案可判别)｜判据12 M12 消费改queryset.update()绕开被mock的save→注入不触发整体提交→红✓｜判据13 M13 限流阈值判据改恒假→红✓｜判据14 M14 views.create摘掉opened_by三行→红✓(同类既有3用例仍绿=判据17旁证)｜判据16 M16 GET令牌查无回退本schema任意行→红✓(B的诱饵链接被误放行,证判别力)｜操作日志网 M17 _RECORD_FORWARDERS摘除_record_self_registered→test_instrumented_endpoints_really_call_record红✓。全部断言失败(FAILED),无crashed冒充killed;还原一律快照文本写回逐字节比对,未用git checkout --。
