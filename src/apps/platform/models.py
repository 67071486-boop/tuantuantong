"""public schema 的平台数据。

铁律：本模块任何模型都不得承载租户业务数据（见 docs/ARCHITECTURE.md §2）。
租户业务实体一律放 apps/business。
"""
from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class Tenant(TenantMixin):
    """一个工作室（租户）。保存时自动创建并迁移其 schema。"""

    name = models.CharField("租户名称", max_length=100)
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    # 保存 Tenant 时自动建 schema 并执行 TENANT_APPS 的迁移。
    auto_create_schema = True
    # 删除 Tenant 行时不自动删 schema：误删数据不可接受，删除走人工流程。
    auto_drop_schema = False

    class Meta:
        verbose_name = "租户"
        verbose_name_plural = "租户"

    def __str__(self):
        return f"{self.name}({self.schema_name})"


class Domain(DomainMixin):
    """租户的独享域名。Host 解析即以此为依据。"""

    class Meta:
        verbose_name = "租户域名"
        verbose_name_plural = "租户域名"


class PlatformTenant(models.Model):
    """平台侧持有的**中央租户档案**：控制台的列表 / 开关 / 对账都以它为准。

    ⛔ **本模型已随平台侧一起迁出主仓（`D-0196`）⇒ 今天这里只剩一个「壳」。**
    真正在读写它的是独立项目 `ttt-platform`（`console.models.PlatformTenant`，`managed=False`，
    指向同一张表）。**为什么这个模型与它的迁移要留在主仓**：删掉模型会让
    `makemigrations --check` 想要一条 `DeleteModel`，而那条迁移一旦执行就会
    **把平台库里的真数据表 `platform_platformtenant` DROP 掉**（生产上就是各租户的名册）
    ⇒ 留壳保迁移历史。⛔ **不要在租户侧读写它**，也⛔ 不要在它上面加字段。

    ⛔ **纯 `models.Model`，永远不建 schema**（spec §3.2 的 C2）。这一条不是口味问题：
    django-tenants 的 `TenantMixin.save()` 对**已存在**的行也留着一条建 schema 的分支
    （`auto_create_schema` 为真且该 schema 不存在时），而 Django Admin 的修改表单正好会调
    `.save()` ⇒ 继承 `TenantMixin` 会让「在平台库里改一行档案」静默建出一个 schema、并把
    租户迁移跑进平台库。另一条路（把模型默认改成 `auto_create_schema = False`）也不行：
    `TenantTestCase` 靠那个开关建测试 schema，改默认会让一批测试文件一起红。
    ⇒ 结构上分开，比靠配置或约定可靠。

    **与 `Tenant` / `Domain` 的分工**（⛔ 别混用，两者不是一份东西）：

    - `Tenant` / `Domain` 是 django-tenants 的**路由行**：**每个库各有一份**，请求路径按 Host
      查它来解析租户；平台库那份只有 `public` 一行 + 平台域名。
    - `PlatformTenant`（本模型）是**平台库一份**的租户档案；它在**每个租户库的 `public` 里
      也有一张同名的空表**（`SHARED_APPS` 的模型在每个库都建），fail-closed：租户侧误读只会
      读到空。它**不参与** Host 路由，只为控制台与三方对账服务。

    ⛔ **不加指向 `Tenant` 的外键**：`Tenant` 行是「每个库各自的路由行」，不同库里的主键
    没有可比性（与「跨租户否定断言不能用主键」同源）⇒ 对账一律用 `schema_name` 这个**业务键**。
    """

    schema_name = models.CharField("schema 名", max_length=63, unique=True)
    name = models.CharField("租户名称", max_length=100)
    primary_domain = models.CharField("主域名", max_length=253, unique=True)
    is_active = models.BooleanField("是否启用", default=True)
    note = models.TextField("备注", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "中央租户档案"
        verbose_name_plural = "中央租户档案"

    def __str__(self):
        return f"{self.name}({self.schema_name})"


# ── 实名认证（批发层）────────────────────────────────────────────
# 对标页：「人员实名记录与配置」（`/User/RnameCertLogAndSetting`）。
# 设计基线：`docs/specs/2026-10-10-realname-verification-design.md` §4.1。
#
# ⚠️ **本节的模型属「平台方自己买来的东西」，⛔ 不是租户业务数据**，所以放 public schema；
# 租户的业务数据（核验流水 / 租户侧配置）在 `apps/realname`（T6），⛔ 别混。

def _mask_key_id(key_id: str) -> str:
    """把密钥 Id 打成可安全展示的掩码：前 4 位 + `***`（空值 ⇒ 直接回 `***`）。"""
    return f"{key_id[:4]}***" if key_id else "***"


class RealNamePackage(models.Model):
    """平台方购入的第三方实名认证接口「密钥包」：一包 = 一对密钥 + 一个定额。

    **为什么落库而不是放 `.env`**（spec §2-B2 业务方原话：「这种是一次性定额密钥，后面还会买新的密钥，
    密钥额度也不一样」）：一次性定额、可反复买、每包额度各不相同 ⇒ 一条环境变量表达不了
    「多条并存 + 各自余量」。⇒ 必须是一张表，`total_quota` / `used_quota` 各包各算。

    ⚠️ **`key_secret` 是全平台最贵的字符串**（spec §7）：拿它就能把我们买来的额度核光。
    因此它**只写不读**——⛔ 不进任何 API 响应 / 日志 / 审计文案 / 异常堆栈，
    `__str__` 也不带它（见本类 `__str__`）。

    ⚠️ **录入面是管理命令 `manage.py realname_package`**（`add` 录入 / `list` 看余量）：
    本仓**没装 `django.contrib.admin`**，平台控制台已独立成 `../ttt-platform`（D-0196），
    而那个项目不 import 本仓 `apps.*` ⇒ admin 的界面**两条路都不通**。
    ⇒ `list` 只回 `key_id` 前 4 位 + `***`，⛔ **绝不回显 `key_secret`**（判据在
    `src/tests/test_realname_package_command.py`）。
    """

    #: ⚠️ `unique=True`（评审收口）：`add_package` 的 `exists()` 预检挡不住
    #: **并发双录**（两个请求同时通过预检、先后 INSERT）⇒ 数据库层唯一约束兜底，
    #: 竞态落 `IntegrityError`，由 `add_package` 翻成同一句既有重复文案（apps/realname/package_admin.py）。
    key_id = models.CharField("密钥Id", max_length=64, unique=True)
    key_secret = models.CharField("密钥Key", max_length=128)
    remark = models.CharField("备注", max_length=120, blank=True, default="")
    total_quota = models.PositiveIntegerField("购入总次数")
    used_quota = models.PositiveIntegerField("已消耗", default=0)
    #: 云市场套餐有效期。空 = 不过期（选包时按「空 或 未到点」放行，spec §6-7 ③）。
    expires_at = models.DateTimeField("有效期至", null=True, blank=True)
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "实名密钥包"
        verbose_name_plural = "实名密钥包"
        ordering = ["id"]

    def __str__(self) -> str:
        # ⛔ `key_secret` 一律不进这里：`__str__` 会出现在 admin 的下拉 / 日志 / 报错文本里。
        return f"{self.remark or '（无备注）'}#{_mask_key_id(self.key_id)}"


class RealNameGrant(models.Model):
    """平台拨给某个租户的实名认证次数。

    「平台方是批发、租户是零售」（spec §2-B3）：平台包的余量（`RealNamePackage.total_quota - used_quota`）
    是「我们还能卖多少」，本模型的 `granted - used` 是「这个租户还能核几次」。
    ⚠️ **两层都要在**：只做一层会**卖超**——把还没买来的额度卖给租户。

    ⚠️ **`tenant` 必须唯一**（唯一约束见 `Meta.constraints`）：扣减走
    `RealNameGrant.objects.select_for_update().get(tenant=…)`，不唯一直接 `MultipleObjectsReturned`
    ⇒ 那是 500，而不是我们要的「余量不足」业务码（spec §6-7 ①）。
    ⇒ 一个租户**一行**，加额度是改这一行的 `granted`，⛔ 不是插一行新的。
    """

    tenant = models.ForeignKey(
        "platform.Tenant", on_delete=models.CASCADE, related_name="realname_grants"
    )
    granted = models.PositiveIntegerField("已拨次数")
    used = models.PositiveIntegerField("已用次数", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "实名额度"
        verbose_name_plural = "实名额度"
        ordering = ["id"]
        constraints = [
            # 名字是定死的（spec §4.1 / 计划 T1）：扣减侧按 `get(tenant=…)` 取行，
            # 靠这条约束保证「取到的一定只有一行」。
            models.UniqueConstraint(fields=["tenant"], name="uniq_realname_grant_tenant"),
        ]

    def __str__(self) -> str:
        return f"{self.tenant_id}:{self.used}/{self.granted}"
