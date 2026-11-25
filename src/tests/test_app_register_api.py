"""打手 App 开放注册端点（`POST /api/app/auth/register/`，2026-10-20）。

## 钉住什么

1. **能自助建号**：`auth.User`（username = 手机号）+ `StaffProfile`（合作用户 / 手机号 /
   `grab_limit_dl=3` / 开户人 = 本人）+ 内置「代练员」Group，且 `set_password` 真的落库
   （拿同一个口令能走登录端点）；
2. **出参与登录同形**：`{token, expires_at, user}`，注册完拿返回的令牌**直接**能拉
   `/api/app/wallet/`（App「注册完直接进首页」的那条路）；
3. **业务码不 500**：手机号重复 `1001`、密码太弱 / 手机号非法 `1000`、超频 `1033`；
   被拒的每一次都**不落账号、不落审计、不发令牌**；
4. **防刷真的在拦**：`APP_REGISTER_MAX_ATTEMPTS` 调低后，第 N+1 次尝试被拒；
5. **跨租户**：A 域名注册出来的号只在 A 的 schema 里；B 域名的库查不到它，
   且 B 自己也能注册（**正向对照**，防止「B 查不到」由「B 那条路根本没接通」凑出来）；
   ⭐ 游戏权限也按 Host 的库判（A 的 game id 在 B 域名下不可用）；
6. **审计**：成功那一次一条 `user.create`（actor = 新账号本人、target = 手机号）；
   带游戏时**每个游戏**还有一条 `user.game_permission_add`；
7. ⭐ **游戏权限（`game_ids`）**：带 games 成功时**读库**断言 M2M 真的落上；
   ⭐ 2026-10-21 业务方改口径：不传 / 传 `[]` ⇒ **本租户全部未软删游戏**（App 注册页不再勾游戏）；
   不存在 / 已软删 / 别的租户的 id ⇒ 批量整体拒、账号一个字节都不写；
   不传（或传 `[]`）⇒ 零游戏，⛔ 不回落成全量；
8. ⭐ **注册页匿名游戏下拉**（`GET /api/app/auth/register/games/`）：匿名 200、
   只含本租户未软删游戏、两库同名不同 id 不串、键集**恰好** `{items: [{id, name}]}`、
   软删不出现、排序稳定、有独立的 IP 桶。

## ⚠️ 两条测试纪律

- **每条用例先 `cache.clear()`**：限流桶落在默认 `LocMemCache`（进程级），它**不会**
  跟 `TenantTestCase` 的事务一起回滚 ⇒ 不清会把上一条用例的计数带进下一条。
- **业务失败一律 HTTP 200 + 非 0 `code`**（本仓统一信封口径），所以断言打在 `body["code"]`
  上，⛔ 不是状态码；反过来说，`status_code != 200` 的失败会先被 `_body()` 的断言抓住。

## 跨租户那条用例的 schema 残留（实测，别把 `finally` 删掉）

`django_tenants` 的 `TenantMainMiddleware` 在请求进来时切 schema、**响应后不还原**
（`tests/test_account_isolation.py` 记着同一段）⇒ B 侧请求之后必须把连接显式钉回 A，
否则后面的 ORM 断言会静默去查 B 的库（症状看着像「A 的行没了」）。
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import tenant_context

from apps.accounts.models import AppToken, SelfRegistrationConfig, StaffProfile
from apps.audit.models import OperationLog
from apps.business.models import Game
from apps.business.peer_sync import hash_token
from apps.platform.provisioning import provision_tenant, seed_tenant

User = get_user_model()

REGISTER_URL = "/api/app/auth/register/"
REGISTER_GAMES_URL = "/api/app/auth/register/games/"
LOGIN_URL = "/api/app/auth/login/"
WALLET_URL = "/api/app/wallet/"

#: 租户 A（`TenantTestCase` 的默认租户）的域名，与其余账号测试同口径。
DOMAIN_A = "tenant.test.com"
#: 租户 B：域名刻意与 A 不同（`Domain` 有唯一约束，撞了 `provision_tenant` 会报错）。
DOMAIN_B = "app-reg-b.test.local"
SCHEMA_B = "app_reg_b"

#: 注册用的合成手机号 / 口令（⛔ 不是任何真实账号）。
MOBILE = "13800002222"
MOBILE_B = "13900003333"
PASSWORD = "pw-app-123456"

#: 业务码字面值。写数字而不是枚举，是为了让「码值被改掉」在测试里可见。
OK = 0
VALIDATION = 1000
CONFLICT = 1001
APP_REGISTER_RATE_LIMITED = 1033

#: 与实现逐字一致的两句外部文案（写字面副本：实现改文案时这里要同一次改，⛔ 不 import 常量）。
MOBILE_TAKEN_MSG = "该手机号已注册，请直接登录或找回密码"
RATE_LIMITED_MSG = "尝试过于频繁，请稍后再试"
PASSWORD_MSG = "密码长度需为 6–18 位"
GAME_SHAPE_MSG = "game_ids 必须是游戏 id 的整数数组，且每个 id 都 ≥ 1"
GAME_UNKNOWN_PREFIX = "游戏不存在、已删除或不属于当前工作室："


class AppRegisterTestCase(TenantTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # `TenantTestCase` 只建 schema 跑迁移，不灌基线数据；注册要 `Group.objects.get("代练员")`
        # ⇒ 必须把默认职位 / RBAC 种子灌进来（与 `test_account_isolation` 同一手法）。
        seed_tenant(cls.tenant)
        # 第二个租户**必须**在 `setUpClass` 建：schema DDL 不能在 `TestCase` 的事务里回滚
        # （与 `tests/test_account_isolation.py` 同一手法）。
        cls.tenant_b, _ = provision_tenant(
            schema_name=SCHEMA_B, name="App 注册隔离租户 B", domain=DOMAIN_B
        )
        # `provision_tenant` 只在**新建** schema 时灌基线数据；本类不依赖「B 是本次刚建的」。
        seed_tenant(cls.tenant_b)

    def setUp(self):
        # 第一行：把连接显式钉回租户 A（挡上一条用例里跨域名请求留下的 schema）。
        connection.set_tenant(self.tenant)
        cache.clear()
        self.domain = self.tenant.get_primary_domain().domain

    def tearDown(self):
        connection.set_tenant(self.tenant)
        cache.clear()
        super().tearDown()

    # ── 工具 ──────────────────────────────────────────────────────
    def _client(self, domain=None):
        return Client(SERVER_NAME=domain or self.domain)

    def _body(self, response):
        """业务响应一律 200（认证层失败才是 401/403），取信封。"""
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def _post(self, payload, *, domain=None):
        return self._client(domain).post(
            REGISTER_URL, payload, content_type="application/json"
        )

    def _register(self, *, mobile=MOBILE, password=PASSWORD, domain=None, **extra):
        return self._body(
            self._post({"mobile": mobile, "password": password, **extra}, domain=domain)
        )

    # ── 1. 成功建号 ────────────────────────────────────────────────
    def test_register_creates_the_account_and_returns_the_login_shape(self):
        body = self._register()
        self.assertEqual(body["code"], OK, body)
        # ⚠️ 出参与 `/api/app/auth/login/` **同一形状**（多一个键或少一个键都会让 App
        #    两处解析分开写）——这里钉的是键集本身。
        self.assertEqual(set(body["data"]), {"token", "expires_at", "user"})
        self.assertEqual(body["data"]["user"]["username"], MOBILE)
        self.assertEqual(body["data"]["user"]["roles"], ["代练员"])

        # 令牌明文只在响应里出现；库里只有哈希（与登录端点同一条纪律）。
        token = body["data"]["token"]
        self.assertTrue(token)
        row = AppToken.objects.get(user__username=MOBILE)
        self.assertEqual(row.key_hash, hash_token(token))

        user = User.objects.get(username=MOBILE)
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        # 口令真的落库了：拿注册时那个口令能走登录端点（⛔ 不是只看 `password` 非空）。
        login = self._body(
            self._client().post(
                LOGIN_URL,
                {"username": MOBILE, "password": PASSWORD},
                content_type="application/json",
            )
        )
        self.assertEqual(login["code"], OK, login)

        profile = StaffProfile.objects.get(user=user)
        self.assertEqual(profile.mobile, MOBILE)
        # `1` = `MemberType.PARTNER`「合作用户」（业务口径：新注册的是打手）。
        self.assertEqual(profile.member_type, 1)
        # D-0269 ⑥：新注册打手的代练持单上限 3 单/天。⚠️ 0 的语义是「不限制」，
        # 所以这里必须是**正数 3**，断言写死数字（E-0024：钉值不钉常量）。
        self.assertEqual(profile.grab_limit_dl, 3)
        # 开户人 = 注册者本人（开放注册里做这件事的就是他自己）。
        self.assertEqual(profile.opened_by_id, user.pk)
        self.assertEqual(list(user.groups.values_list("name", flat=True)), ["代练员"])
        # 本租户此时一款游戏都没有 ⇒ 不传 `game_ids` 也就是零游戏（默认全量的空集）。
        self.assertEqual(profile.games.count(), 0)

    def test_register_writes_exactly_one_audit_row(self):
        body = self._register()
        self.assertEqual(body["code"], OK, body)
        user = User.objects.get(username=MOBILE)

        rows = list(OperationLog.objects.filter(action="user.create"))
        self.assertEqual(len(rows), 1, [(r.action, r.summary) for r in OperationLog.objects.all()])
        row = rows[0]
        # `30` = `LogType.USER`（用户相关）；actor = 新账号本人、target = 手机号。
        self.assertEqual(row.log_type, 30)
        self.assertEqual(row.actor, MOBILE)
        self.assertEqual(row.actor_id, user.pk)
        self.assertEqual(row.target_kind, "user")
        self.assertEqual(row.target_id, str(user.pk))
        self.assertEqual(row.target_name, MOBILE)
        # 渲染出来的句子必须真的带上账号（缺 `target_name` 会渲染成空名字，而「行存在」
        # 的断言看不出来）。
        self.assertIn(MOBILE, row.summary)
        # 本租户没有游戏 ⇒ 默认全量是空集，一条游戏权限审计行都没有。
        self.assertFalse(
            OperationLog.objects.filter(action="user.game_permission_add").exists()
        )

    def test_register_normalizes_an_overseas_prefixed_mobile(self):
        """`+86` / 空格 / 连字符按 `core/sms.normalize_mobile` 归一 —— 建出来的是 11 位号。"""
        body = self._register(mobile=f"+86 {MOBILE[:3]}-{MOBILE[3:]}")
        self.assertEqual(body["code"], OK, body)
        self.assertTrue(User.objects.filter(username=MOBILE).exists())

    # ── 1b. 游戏权限（`game_ids`，业务方拍板「注册页让打手自己勾」）──────
    def test_register_with_games_grants_them_and_audits_each(self):
        """带 games 成功：**读库**断言 M2M 真的落上 + 每个游戏一行审计 + 出参里也带着。"""
        g1 = Game.objects.create(name="原神")
        g2 = Game.objects.create(name="英雄联盟")
        # 乱序 + 重复：入口会升序去重（`games.set()` 与审计行序因此稳定）。
        body = self._register(game_ids=[g2.pk, g1.pk, g2.pk])
        self.assertEqual(body["code"], OK, body)

        profile = StaffProfile.objects.get(user__username=MOBILE)
        self.assertEqual(
            sorted(profile.games.values_list("pk", flat=True)), sorted([g1.pk, g2.pk])
        )
        # 出参里的 `user.game_ids` 与库里一致（App 注册完就能按它渲染勾选状态）。
        self.assertEqual(body["data"]["user"]["game_ids"], sorted([g1.pk, g2.pk]))

        # 审计：一条建号 + 每个游戏一条「新增用户游戏权限」。
        self.assertEqual(OperationLog.objects.filter(action="user.create").count(), 1)
        game_rows = list(
            OperationLog.objects.filter(action="user.game_permission_add").order_by("id")
        )
        self.assertEqual([row.game_name for row in game_rows], ["原神", "英雄联盟"])
        for row in game_rows:
            self.assertEqual(row.log_type, 30)
            self.assertEqual(row.actor_id, profile.user_id)
            self.assertEqual(row.target_name, MOBILE)

    def test_register_without_games_gets_all_tenant_games(self):
        """⭐ 2026-10-21 改口径：不传 / 传 `[]` ⇒ **本租户全部未软删游戏**（App 注册页不再勾游戏）。

        判据钉在「值的集合」上：两款正常游戏都进、软删的那款**不进**；
        审计也按实际授予的游戏逐行落（与带 games 那条同一形状）。
        """
        g1 = Game.objects.create(name="原神")
        g2 = Game.objects.create(name="英雄联盟")
        Game.objects.create(name="已下架的游戏", deleted_at=timezone.now())

        body = self._register(game_ids=[])
        self.assertEqual(body["code"], OK, body)
        profile = StaffProfile.objects.get(user__username=MOBILE)
        self.assertEqual(
            sorted(profile.games.values_list("pk", flat=True)), sorted([g1.pk, g2.pk])
        )
        # 出参里的 `user.game_ids` 也必须是这两款（App 拿它渲染/判权限）。
        self.assertEqual(body["data"]["user"]["game_ids"], sorted([g1.pk, g2.pk]))
        self.assertEqual(
            list(
                OperationLog.objects.filter(action="user.game_permission_add")
                .order_by("id")
                .values_list("game_name", flat=True)
            ),
            ["原神", "英雄联盟"],
        )

    def test_register_assigns_the_tenant_account_number(self):
        """⭐ 2026-10-21：注册即按租户配置的编号生成显示名（App「我的」页不再显示手机号）。

        判据钉值：起始 20001 ⇒ `sq20001`；同前缀已有 `sq20006` ⇒ 下一个是 `sq20007`
        （自愈/递进与自助注册线同一个生成器，这里只钉 App 这条路接上了）。
        """
        SelfRegistrationConfig.objects.create(
            pk=1, number_prefix="sq", number_start=20001
        )
        body = self._register()
        self.assertEqual(body["code"], OK, body)
        user = User.objects.get(username=MOBILE)
        self.assertEqual(user.first_name, "sq20001")
        self.assertEqual(body["data"]["user"]["display_name"], "sq20001")

        # 递进：已有 `sq20006` ⇒ 下一次注册落到 20007。
        User.objects.create(username="13900009999", first_name="sq20006")
        body2 = self._register(mobile=MOBILE_B)
        self.assertEqual(body2["code"], OK, body2)
        self.assertEqual(User.objects.get(username=MOBILE_B).first_name, "sq20007")

    def test_register_without_the_field_also_gets_all_games(self):
        """不传键与传 `[]` 同义（两条入口都要覆盖，别只测其中一条）。"""
        g1 = Game.objects.create(name="原神")
        body = self._register()
        self.assertEqual(body["code"], OK, body)
        profile = StaffProfile.objects.get(user__username=MOBILE)
        self.assertEqual(list(profile.games.values_list("pk", flat=True)), [g1.pk])

    def test_a_batch_with_any_invalid_game_id_is_rejected_as_a_whole(self):
        """合法 + 不存在 + 已软删混在一起 ⇒ **整单拒**，账号 / 档案 / 令牌 / 审计一个都不留。"""
        valid = Game.objects.create(name="原神")
        deleted = Game.objects.create(name="已下架的游戏", deleted_at=timezone.now())
        missing = 987654

        body = self._register(game_ids=[valid.pk, deleted.pk, missing])
        self.assertEqual(body["code"], VALIDATION, body)
        # 说清是哪几个：点的那个（软删）与不存在的那个都列出来，合法的那个不列。
        self.assertEqual(
            body["msg"], f"{GAME_UNKNOWN_PREFIX}[{deleted.pk}, {missing}]", body["msg"]
        )

        # 回滚彻底：校验在写之前，所以是「一个字节都没写」（不是写一半再撤）。
        self.assertFalse(User.objects.filter(username=MOBILE).exists())
        self.assertFalse(StaffProfile.objects.exists())
        self.assertFalse(AppToken.objects.exists())
        self.assertFalse(OperationLog.objects.exists())

    def test_shape_invalid_game_ids_is_a_business_error_not_a_500(self):
        """不是数组 / 元素不是整数 / id < 1 ⇒ 1000 + 形状那一句，⛔ 不是 HTML 500。"""
        for bad in ("not-a-list", [0], [-1], ["abc"], [1.5], [None]):
            with self.subTest(game_ids=bad):
                body = self._register(game_ids=bad)
                self.assertEqual(body["code"], VALIDATION, body)
                self.assertEqual(body["msg"], GAME_SHAPE_MSG, body["msg"])
        self.assertFalse(User.objects.filter(username=MOBILE).exists())
        self.assertFalse(OperationLog.objects.exists())

    def test_a_soft_deleted_game_cannot_be_granted(self):
        """软删的游戏必须拒（`Game.objects` 不过滤软删，判据要显式写 `deleted_at__isnull=True`）。"""
        gone = Game.objects.create(name="已下架的游戏", deleted_at=timezone.now())
        body = self._register(game_ids=[gone.pk])
        self.assertEqual(body["code"], VALIDATION, body)
        self.assertIn(str(gone.pk), body["msg"])
        self.assertFalse(StaffProfile.objects.exists())

    # ── 1c. 注册页的匿名游戏列表（`GET .../register/games/`）─────────
    def _games(self, domain=None):
        """不带任何认证头 —— 这条端点必须匿名可用。"""
        return self._body(self._client(domain).get(REGISTER_GAMES_URL))

    def test_register_games_are_anonymous_and_scoped_to_the_host_tenant(self):
        """匿名 200；只含**本租户未软删**的游戏；两租户同名不同 id 也不串；键集钉死。

        ⚠️ 「同名不同 id」是针对**两个 schema 的主键各自从 1 开始**这个坑设计的：
        A 里先造两款、B 里先造一款填充，再各造一款**同名**的 ⇒ 同名行的 pk 不同
        （A=3 / B=2）⇒ 断言 id 就能分辨「返回的到底是哪个库的那一行」，而不是碰巧撞号。
        """
        shared = "同名游戏（两库各一款）"
        low = Game.objects.create(name="低排序", sort_order=0)       # A pk=1
        high = Game.objects.create(name="高排序", sort_order=5)      # A pk=2
        shared_a = Game.objects.create(name=shared, sort_order=1)    # A pk=3
        hidden = Game.objects.create(                                # A pk=4（软删）
            name="已下架的游戏", deleted_at=timezone.now()
        )
        with tenant_context(self.tenant_b):
            Game.objects.create(name="B 的填充游戏")                  # B pk=1
            shared_b = Game.objects.create(name=shared)              # B pk=2
        self.assertNotEqual(shared_a.pk, shared_b.pk, "两库的同名行没拿到不同 pk，本用例失去判别力")

        body = self._games()
        self.assertEqual(body["code"], OK, body)
        # ── 键集**恰好**：顶层只有 items，每行只有 id / name（⛔ 不是「至少包含」）──
        self.assertEqual(set(body["data"]), {"items"})
        items = body["data"]["items"]
        for item in items:
            self.assertEqual(set(item), {"id", "name"}, item)
        # 更宽一层：响应原文里连内部列名都不该出现（防将来有人把整行塞进 name 之外的地方）。
        raw = self._client().get(REGISTER_GAMES_URL).content.decode()
        for banned in ("sort_order", "is_active", "deleted_at", "deposit"):
            self.assertNotIn(banned, raw)

        # 只含 A 的**未软删**行，且按 `-sort_order, id`（high 5 → shared 1 → low 0）。
        self.assertEqual(
            [item["id"] for item in items],
            [high.pk, shared_a.pk, low.pk],
        )
        self.assertEqual(
            [item["name"] for item in items], ["高排序", shared, "低排序"]
        )
        ids = [item["id"] for item in items]
        self.assertNotIn(hidden.pk, ids, "软删的游戏出现在了注册页下拉里")
        self.assertNotIn(shared_b.pk, ids, "B 租户的同名游戏串进了 A 的响应")
        self.assertEqual([i["name"] for i in items].count(shared), 1)

    def test_register_games_excludes_soft_deleted(self):
        """软删的**不出现**（`Game.objects` 默认管理器不过滤软删，判据必须显式写）。"""
        visible = Game.objects.create(name="在架的游戏")
        Game.objects.create(name="已下架", deleted_at=timezone.now())
        items = self._games()["data"]["items"]
        self.assertEqual([item["id"] for item in items], [visible.pk])
        self.assertEqual([item["name"] for item in items], ["在架的游戏"])

    def test_register_games_order_matches_the_model_ordering(self):
        """排序 = `-sort_order, id`（与录单页 / 详情页下拉同一个 Meta ordering；id 兜底）。"""
        zero = Game.objects.create(name="A", sort_order=0)
        top = Game.objects.create(name="B", sort_order=9)
        tie1 = Game.objects.create(name="C", sort_order=3)
        tie2 = Game.objects.create(name="D", sort_order=3)
        ids = [item["id"] for item in self._games()["data"]["items"]]
        self.assertEqual(ids, [top.pk, tie1.pk, tie2.pk, zero.pk])

    @override_settings(APP_REGISTER_GAMES_MAX_REQUESTS=1)
    def test_register_games_has_its_own_ip_bucket(self):
        """读口也有闸，但**独立于**注册那个桶：拉列表被限了，注册照样能走。"""
        Game.objects.create(name="原神")
        self.assertEqual(self._games()["code"], OK)
        body = self._games()
        self.assertEqual(body["code"], APP_REGISTER_RATE_LIMITED, body)
        self.assertEqual(body["msg"], RATE_LIMITED_MSG)
        # 独立的桶：读口打满不影响写口（否则「多刷几次列表就注册不了」）。
        self.assertEqual(self._register()["code"], OK)

    # ── 2. 手机号重复 / 用户名重复（同一句、同一码）──────────────────
    def test_duplicate_mobile_is_a_business_conflict_and_changes_nothing(self):
        self.assertEqual(self._register()["code"], OK)
        before = (
            User.objects.filter(username=MOBILE).count(),
            StaffProfile.objects.count(),
            AppToken.objects.count(),
            OperationLog.objects.count(),
        )
        body = self._register(password="another-password-1")
        self.assertEqual(body["code"], CONFLICT, body)
        self.assertEqual(body["msg"], MOBILE_TAKEN_MSG)
        # 重复那一档**不回任何账号信息**（不给枚举者多一个可读字段）。
        self.assertIsNone(body["data"])
        after = (
            User.objects.filter(username=MOBILE).count(),
            StaffProfile.objects.count(),
            AppToken.objects.count(),
            OperationLog.objects.count(),
        )
        self.assertEqual(after, before)
        # 原口令仍然有效、新口令没被写进去（重复注册⛔ 不能改别人的密码）。
        user = User.objects.get(username=MOBILE)
        self.assertTrue(user.check_password(PASSWORD))
        self.assertFalse(user.check_password("another-password-1"))

    def test_a_manual_account_holding_the_mobile_gives_the_same_conflict(self):
        """**手工开的老账号**用户名不是手机号、只在档案里挂着这个号 ⇒ 同样是 `1001` 那一句。

        判据的意义：手机号重复与用户名重复**结构上是同一个答复**（不区分是哪一种撞了），
        而且只看 `username` 的实现会漏掉这一支（两个账号共用一个手机号会让短信登录
        「多命中 ⇒ 拒绝」，两个人都登不进去）。
        """
        old = User.objects.create_user(username="old-manual", password="pw-old-123")
        StaffProfile.objects.create(user=old, mobile=MOBILE)

        body = self._register()
        self.assertEqual(body["code"], CONFLICT, body)
        self.assertEqual(body["msg"], MOBILE_TAKEN_MSG)
        self.assertFalse(User.objects.filter(username=MOBILE).exists())

    # ── 3. 密码太弱 / 手机号非法 ──────────────────────────────────
    def test_too_short_password_is_rejected(self):
        body = self._register(password="12345")
        self.assertEqual(body["code"], VALIDATION, body)
        self.assertEqual(body["msg"], PASSWORD_MSG)
        self.assertFalse(User.objects.filter(username=MOBILE).exists())
        self.assertFalse(OperationLog.objects.exists())

    def test_too_long_password_is_rejected(self):
        body = self._register(password="p" * 19)
        self.assertEqual(body["code"], VALIDATION, body)
        self.assertEqual(body["msg"], PASSWORD_MSG)
        self.assertFalse(User.objects.filter(username=MOBILE).exists())

    def test_malformed_mobile_is_rejected(self):
        for mobile in ("12345", "1380000222a", "23800002222"):
            with self.subTest(mobile=mobile):
                body = self._register(mobile=mobile)
                self.assertEqual(body["code"], VALIDATION, body)
                self.assertIn("手机号格式不正确", body["msg"])
        self.assertFalse(User.objects.filter(username__startswith="1").exists())
        self.assertFalse(OperationLog.objects.exists())

    def test_a_non_object_body_is_a_business_error_not_a_500(self):
        """`data=[]` 时 `.get` 会 `AttributeError` ⇒ 500 —— 公开端点的请求体形状由调用方控制。"""
        response = self._client().post(
            REGISTER_URL, data="[]", content_type="application/json"
        )
        body = self._body(response)
        self.assertEqual(body["code"], VALIDATION, body)

    # ── 4. 限流 ──────────────────────────────────────────────────
    @override_settings(APP_REGISTER_MAX_ATTEMPTS=2)
    def test_the_same_ip_is_rate_limited(self):
        """阈值 2 ⇒ 前两次放行、第三次被拒（`count <= limit` 的边界语义，同 realname 先例）。

        ⚠️ 数的是**尝试**不是新建账号：被拒的那一次也进桶（枚举手机号正是要拦的形状）。
        """
        self.assertEqual(self._register(mobile=MOBILE)["code"], OK)
        self.assertEqual(self._register(mobile="13800004444")["code"], OK)

        body = self._register(mobile=MOBILE_B)
        self.assertEqual(body["code"], APP_REGISTER_RATE_LIMITED, body)
        self.assertEqual(body["msg"], RATE_LIMITED_MSG)
        self.assertFalse(User.objects.filter(username=MOBILE_B).exists())
        # 被拒的那一次不写审计（「建号 + 留痕」同生共死）。
        self.assertEqual(OperationLog.objects.count(), 2)

    @override_settings(APP_REGISTER_MAX_ATTEMPTS=1)
    def test_a_failed_validation_also_consumes_the_ip_bucket(self):
        """跑飞的重试（哪怕全是格式错）也要被闸住 —— 闸在**校验之前**。"""
        self.assertEqual(self._register(mobile="12345")["code"], VALIDATION)
        body = self._register(mobile=MOBILE)
        self.assertEqual(body["code"], APP_REGISTER_RATE_LIMITED, body)
        self.assertFalse(User.objects.filter(username=MOBILE).exists())

    # ── 5. 跨租户 ────────────────────────────────────────────────
    def test_register_is_confined_to_the_host_tenant(self):
        """A 域名注册出来的号只在 A 的库；B 的库查不到，而 B 自己注册得动（正向对照）。

        ⚠️ 请求体里**故意塞** `tenant` / `schema_name`：租户只由 Host 决定，
        这两个键必须被忽略（`AGENTS.md` §4：⛔ 不接受任何租户参数）。

        ⚠️ 游戏权限同样**按 Host 的库**解析：A 的 game id 在 B 域名下必须查不到（⇒ 1000），
        只有 B 自己的 game id 才挂得上。两条正向对照（A 的号带 A 的游戏 / B 的号带 B 的游戏）
        是判别力来源——没有它们，那两条否定判据可能由「games 这条路根本没接通」凑出来。
        """
        game_a = Game.objects.create(name="仅 A 的游戏")
        body = self._register(
            domain=DOMAIN_A,
            tenant=SCHEMA_B,
            schema_name=SCHEMA_B,
            game_ids=[game_a.pk],
        )
        self.assertEqual(body["code"], OK, body)
        self.assertTrue(User.objects.filter(username=MOBILE).exists(), "A 的号没落进 A 的库")
        self.assertEqual(
            list(
                StaffProfile.objects.get(user__username=MOBILE)
                .games.values_list("pk", flat=True)
            ),
            [game_a.pk],
        )

        with tenant_context(self.tenant_b):
            self.assertFalse(
                User.objects.filter(username=MOBILE).exists(),
                "A 域名注册出来的号出现在了 B 的库（或请求体里的租户参数被认了）",
            )

        # A 的 game id 打 B 域名：B 的库里没有这款游戏 ⇒ 1000，账号不建。
        body_cross = None
        try:
            body_cross = self._register(
                mobile=MOBILE_B, domain=DOMAIN_B, game_ids=[game_a.pk]
            )
        finally:
            # 跨域名请求之后连接停在 B 的 schema 上（见模块 docstring），必须钉回 A。
            connection.set_tenant(self.tenant)
        self.assertEqual(body_cross["code"], VALIDATION, body_cross)
        self.assertIn(str(game_a.pk), body_cross["msg"])
        with tenant_context(self.tenant_b):
            self.assertFalse(User.objects.filter(username=MOBILE_B).exists())

        # 正向对照：B 自己的 game id 在 B 域名下能挂上。
        with tenant_context(self.tenant_b):
            game_b_pk = Game.objects.create(name="仅 B 的游戏").pk
        body_b = None
        try:
            body_b = self._register(
                mobile=MOBILE_B, domain=DOMAIN_B, game_ids=[game_b_pk]
            )
        finally:
            connection.set_tenant(self.tenant)
        self.assertEqual(body_b["code"], OK, body_b)
        with tenant_context(self.tenant_b):
            self.assertTrue(User.objects.filter(username=MOBILE_B).exists(), "B 的号没落进 B 的库")
            self.assertEqual(
                list(
                    StaffProfile.objects.get(user__username=MOBILE_B)
                    .games.values_list("pk", flat=True)
                ),
                [game_b_pk],
            )
        with tenant_context(self.tenant):
            self.assertFalse(User.objects.filter(username=MOBILE_B).exists())

    # ── 6. 注册完直接能用 ─────────────────────────────────────────
    def test_the_token_returned_by_register_can_read_the_wallet(self):
        """「注册完直接进首页」的判据：不调登录、只用注册返回的那枚令牌拉钱包。"""
        body = self._register()
        self.assertEqual(body["code"], OK, body)
        token = body["data"]["token"]

        wallet = self._body(
            self._client().get(WALLET_URL, HTTP_AUTHORIZATION=f"Bearer {token}")
        )
        self.assertEqual(wallet["code"], OK, wallet)
        # 新账号的钱包是零（`get_wallet` 首次访问建行）；金额是**字符串**、两位小数。
        self.assertEqual(wallet["data"]["balance"], "0.00")
        self.assertEqual(wallet["data"]["frozen"], "0.00")
        # 本用例没传 `game_ids` ⇒ 零游戏 ⇒ 押金规则为空表（不是缺陷）。
        self.assertEqual(wallet["data"]["deposit_rules"], [])
