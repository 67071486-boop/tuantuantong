"""实名认证配额出口：锁两层余量，再用 `F()` 扣减。

一次问清「本租户还能不能核、平台还有没有可用的包」，快照交给调用方；
厂商有结果后再 `charge()`。本模块不开事务、不出网、不写流水。
出站只在 `apps/realname/vendor.py`。

`RealNameGrant.used` / `RealNamePackage.used_quota` 只许这里的 `charge()` 写，
包括看起来只有一行的 `F()` 更新。结构网扫全仓，第二处赋值会红。
「唯一出口」靠纪律和这张网：租户角色对 public 表有写权限，不是连不上。

`1026`：本租户没额度行，或已用完。`1027`：平台侧没有可用密钥包。
`1025`（本租户没启用）归核验视图。两码必须分开，免得管理员去改没错的地方。
"""
from dataclasses import dataclass

from django.db import connection
from django.db.models import F, Q

from apps.core.error_codes import ErrorCode
from apps.platform.models import RealNameGrant, RealNamePackage, Tenant

#: 本模块会抛出的两个业务码（⚠️ `1025`「功能未启用」**不在**本模块——那是核验视图的开关）。
REALNAME_QUOTA_EXHAUSTED = ErrorCode.REALNAME_QUOTA_EXHAUSTED
REALNAME_NO_PACKAGE = ErrorCode.REALNAME_NO_PACKAGE


class QuotaError(Exception):
    """配额的**预期**失败（可翻译成业务码），⛔ 不是异常路径的兜底。

    调用方（核验视图）应当把它翻成 `HTTP 200 + code=<self.code>` ；⛔ 不许
    「except Exception」一并接住——未预期的异常仍要**响亮地 500**（`AGENTS.md` §5-E / E-0010：
    把未预期异常伪装成业务结果会让监控失明）。
    """

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = int(code)
        self.message = message


@dataclass(frozen=True)
class LockedQuota:
    """一次「锁住的两层余量」快照（`lock()` 的返回值）。

    ⚠️ `used` / `used_quota` 是**扣减前**的读数：`grant.used` 与 `package.used_quota` 在被
    `charge()` 扣减后就地变成 `F()` 表达式对象（那是个「还没算出来的值」）⇒ 想事后读「原来是多少」
    只能靠这里的快照，⛔ 不要拿扣减后的对象去猜（spec §5-⑨ 要回「剩余次数」给前端）。
    """

    package: RealNamePackage
    grant: RealNameGrant
    used: int
    used_quota: int


def _current_tenant() -> Tenant:
    """取本请求的租户行：**只从 `connection.schema_name` 取**（⛔ 不接受任何请求参数传租户）。

    `AGENTS.md` §4 的铁律：租户上下文只能来自认证态 / 连接态，绝不接受请求参数传入。
    `django-tenants` 已经把请求的 Host 解析成了连接上的 schema ⇒ 这里是那份上下文的唯一出处。
    """
    try:
        return Tenant.objects.get(schema_name=connection.schema_name)
    except Tenant.DoesNotExist as exc:
        # ⚠️ **响亮，不静默**：取不到租户行说明连接上下文与库里的租户表对不上（部署 / 测试环境
        # 配错），这一档按「本租户没有额度」处理并报 1026。⛔ 不许 auto-create 一个 Tenant 行，
        # 也⛔ 不许让它变成 500 之外的东西（这里就是明确的业务拒绝）。
        raise QuotaError(
            REALNAME_QUOTA_EXHAUSTED,
            "本租户实名认证余量不足（数据库里找不到当前租户的记录，请联系平台管理员核对部署）",
        ) from exc


def lock(*, now) -> LockedQuota:
    """在**调用方已开启的事务里**锁住本租户额度行与一个可用密钥包行。

    两层任一不足 ⇒ `raise QuotaError`（`1026` 或 `1027`，中文人话）。⛔ 本函数自己**不开事务**
    （事务由调用方管，见 spec §6-7 的顺序：锁 → 出网 → 扣减 → 写流水，全在一个 `atomic()` 里）。

    ⚠️ **锁序是判据的一部分**（所有调用方一律「先额度行、后包行」，死锁要靠它避免）：
    额度行是**每租户一行**、包行是**多租户共用**的，两把锁的顺序反过来就是成环的经典条件。

    ⚠️ **本函数在事务里，行锁会跨越一次网络往返**（调用方紧接着要出网调厂商）：同一租户的并发核验
    会**排队**。**这是有意的取舍**（spec §6-7「为什么把出网调用放进事务里」）——好处是「厂商失败
    就不扣次数」变成**结构性的**（事务回滚），不靠事后的补偿代码。⛔ 别以为是漏了。
    """
    tenant = _current_tenant()

    # ① 锁本租户的额度行。⚠️ 行不存在 ⇒ 1026，⛔ **不 auto-create 一个 `granted=0` 的行**
    #    ——那会让「没买过」与「买过用完」在数据上混淆（spec §6-7 ①），也让运营再也看不出
    #    这个租户到底买没买过（两档都会显示成 0/0）。
    try:
        grant = RealNameGrant.objects.select_for_update().get(tenant=tenant)
    except RealNameGrant.DoesNotExist as exc:
        raise QuotaError(
            REALNAME_QUOTA_EXHAUSTED,
            "本租户实名认证余量不足（尚未购买实名认证套餐包，请联系客服购买）",
        ) from exc

    # ② **整数比较**，⛔ 不是真值判断：`granted=0` 是合法且有意义的边界（`AGENTS.md` §5-E / E-0021）。
    #    写成 `if grant.granted and grant.used >= grant.granted` 会让「一次都没买」的租户**放行**。
    if grant.used >= grant.granted:
        raise QuotaError(
            REALNAME_QUOTA_EXHAUSTED,
            f"本租户实名认证余量不足（已用 {grant.used} 次、共 {grant.granted} 次，"
            "请联系客服购买）",
        )

    # ③ 选包：启用 + 还有余量 + （不过期 或 未到点），按 `id` 最小的先用。
    #    ⚠️ `expires_at is None` 是「买了没有有效期的包」⇒ 必须放行，所以条件是「空 **或** 未到点」。
    candidate = (
        RealNamePackage.objects.filter(
            is_active=True,
            used_quota__lt=F("total_quota"),
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .order_by("id")
        .first()
    )
    if candidate is None:
        raise QuotaError(
            REALNAME_NO_PACKAGE,
            "平台侧暂无可用的实名认证密钥包（套餐已用尽或已过期，请联系平台管理员补充）",
        )

    # ④ **必须再锁住那一行**，并在锁内**复查**（spec §6-7 ③④）：
    #    - 为什么必须锁：包是**多租户共用**的，不锁的话两个租户会同时选中同一个包、一起把余量用穿
    #      （实测：去掉它，用例 9/10 双双红）；
    #    - 为什么锁内还要复查：③ 的过滤与这里的行锁之间那条缝里，别的租户可能已经把这个包用尽，
    #      而 `get(pk=…)` 取行**不再过滤余量** ⇒ 不复查就会把「已经用完的包」记上一次消耗。
    #    ⚠️⚠️ **如实登记（T5 实测，⛔ 别把这段读成「已被判据覆盖」）**：这条复查**目前没有判据
    #      守得住** —— 把它删掉，`src/tests/test_realname_quota.py` 的 16 条用例**照样全绿**。
    #      机理：`select_for_update()` 在 READ COMMITTED 下**锁住之后会重新求值 WHERE**
    #      （PostgreSQL 语义），而后到的连接上一步「重新过滤」时就已经跳过那个用尽的包了
    #      ⇒ 「过了过滤、锁上时却已用尽」那条缝在**单库**形态里到不了。
    #      ⇒ 它今天是**结构化要求**（spec 明文要求），⛔ 不是「被判据证明过的承重逻辑」。
    #      要真覆盖它得造出那条交错（或改成别的实现形态），那是另一次设计。
    package = RealNamePackage.objects.select_for_update().get(pk=candidate.pk)
    if package.used_quota >= package.total_quota:
        raise QuotaError(
            REALNAME_NO_PACKAGE,
            "平台侧暂无可用的实名认证密钥包（并发核验把最后一包用尽了，请重试或联系平台管理员）",
        )

    return LockedQuota(
        package=package,
        grant=grant,
        # 快照取在扣减**之前**（`charge()` 会把这两个属性就地变成 `F()` 表达式）。
        used=grant.used,
        used_quota=package.used_quota,
    )


def charge(locked: LockedQuota) -> None:
    """把 `locked` 记着的那一次消耗记上：`grant.used` / `package.used_quota` 各 +1。

    用 `F()` 加一，不用读-改-写：并发下读改写会丢更新（两条都读到 0、各写 1）。

    ⚠️ 只 `save(update_fields=[...])`这两列：本函数只关心那两列，别的一律不动
    （`RealNamePackage.updated_at` 是 `auto_now`，Django 会**自己**把它带进这条 `UPDATE`，
    不需要也不该由我们手写进 `update_fields`）。
    """
    locked.grant.used = F("used") + 1
    locked.grant.save(update_fields=["used"])

    locked.package.used_quota = F("used_quota") + 1
    locked.package.save(update_fields=["used_quota"])
