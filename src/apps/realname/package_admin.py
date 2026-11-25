"""实名资源包（平台密钥包）的**录入 / 列表**单一出处（spec §2.4）。

`manage.py realname_package`（`apps/platform/management/commands/realname_package.py`）
与租户侧端点 `GET/POST /api/accounts/realname-packages/`（`apps/accounts/views.py::
AccountViewSet.realname_packages`）**都调本模块**——⛔ 两份实现（同一套校验文案、
同一个掩码口径、同一处 `schema_context("public")`，漂移了就是两套行为）。

## 校验文案即契约

`add_package` 抛的 `PackageAdminError` 文案**逐字**来自命令的既有输出
（T2 实测落定的那份）：命令把它原样翻译成 `CommandError`（行为不变，
判据网 `tests/test_realname_package_command.py` 照旧绿），端点翻译成 200 + code 1000
（E-0010：领域层的预期失败翻成业务错误码）。⛔ 改任何一个字都要同时过两边的网。

## ⛔ secret 的零回显口径（本模块最贵的一条约束）

`key_secret` 是全平台最贵的字符串：**只写不读**。`add_package` 只收参数、不回显；
`list_packages()` 的行**根本没有** `key_secret` 这个键（不是「置空」，是键不存在）
——掩码只打在 `key_id` 上（`_mask_key_id`，前 4 位 + `***`）。判据在
`tests/test_realname_package_command.py`（命令）与 `tests/test_realname_package_admin_api.py`
（端点「任何字段不含 secret 原文，对照库里行断言」）。

## ⚠️ 为什么要显式 `schema_context("public")`

两张平台表只该由 `public` 那一份说了算（`apps/platform/models.py` 的「平台面唯一出处」
口径；机理与现场见命令模块 docstring 的同款长注释）。schema_context 放在**本模块**
（而不是两个调用方各包一层）：「写哪一份」这件事只允许有一处决定。
"""
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.platform.models import RealNamePackage, _mask_key_id


class PackageAdminError(Exception):
    """录入的**预期**失败（可翻译成 `CommandError` / 业务码 1000），⛔ 不是异常兜底。"""


def _duplicate_key_id_error(key_id) -> PackageAdminError:
    """重复 key_id 的**同一句**既有文案（命令 T2 实测落定的那份）。

    ⚠️ 预检分支与唯一约束的竞态分支**共用这一处**（⛔ 两份文案会漂移）——
    两支抛出的文案逐字相同，调用方（命令 / 端点）无需区分是哪一支。
    """
    return PackageAdminError(
        f"密钥 Id {_mask_key_id(key_id)} 已经录过了（本命令 ⛔ 不静默覆盖）："
        "要换密钥请另建一个包（额度各自独立），旧的置 is_active=False 停用。"
    )


def parse_expires_at(raw: str) -> datetime:
    """把 `expires_at` 解析成带时区的 `datetime`；空串 ⇒ 报错（⛔ 不静默当成「不过期」）。

    接受的形状（够用即可，⛔ 不引入新依赖）：
    `2027-10-10`（按**当前时区**的当天 `00:00`）、`2027-10-10 12:30[:45]`、ISO 8601 带时区。

    ⚠️ **空串必须响亮报错**：`--expires-at "$EXP"` 而那个变量没设是最常见的形态，静默按
    「不过期」处理会让一个**本该到期的包**永远被选包逻辑放行（`expires_at IS NULL` 是
    「永不过期」的语义，spec §6-7 ③）——那是**无声**地把额度无限期卖下去。
    """
    text = (raw or "").strip()
    if not text:
        raise PackageAdminError(
            "--expires-at 给的是空串：显式给了却没有值会被**静默**当成「不过期」"
            "（`expires_at IS NULL` 的语义就是永不过期），那会让一个本该到期的包一直被选包放行。"
            "要么给一个真实日期，要么整条参数都不给。"
        )
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        # 裸日期按**当前时区**解释（`settings.USE_TZ=True`，Django 不接受 naive datetime 落库）。
        return timezone.make_aware(naive) if timezone.is_naive(naive) else naive
    raise PackageAdminError(
        f"--expires-at 解析不了：{raw!r}。"
        "请用 2027-10-10 / '2027-10-10 12:30' / ISO 8601（带时区）这几种形状。"
    )


def add_package(
    *,
    key_id,
    key_secret,
    total_quota,
    expires_at,
    remark,
) -> RealNamePackage:
    """录入一个密钥包（校验**逐字**同命令 T2 的既有实现；`schema_context("public")` 内写）。

    参数即原始输入：`key_id` / `key_secret` / `remark` 是 str（可为空白）、
    `total_quota` 是 int 或 None、`expires_at` 是 str 或 None（内部解析）。
    校验顺序与失败文案**与命令逐字一致**（判据网照旧绿的承重点）。
    返回建好的 `RealNamePackage` 行。
    """
    # ⚠️ 用 `> 0` 判，⛔ 不用真值：`0` 是**合法入参**（不是"没给"），必须响亮拒绝而不是
    # 静默当成默认值——`--total-quota 0` 会产生一个「永远选不中」的包，看起来录进去了。
    if total_quota is None or total_quota <= 0:
        raise PackageAdminError(
            f"--total-quota 必须 ≥ 1，给的是 {total_quota!r}："
            "0 或负数会录进一个永远选不中的包（总额 0），运营事后看不出来为什么余额一直是 0。"
        )
    key_id = (key_id or "").strip()
    if not key_id:
        raise PackageAdminError("--key-id 不能是空白")
    key_secret = (key_secret or "").strip()
    if not key_secret:
        raise PackageAdminError(
            "--key-secret 不能是空白：密钥是这张表存在的理由（B2：密钥不存 .env、只存这里），"
            "录一个空的等于这个包永远核不了。"
        )
    expires_at = parse_expires_at(expires_at) if expires_at is not None else None

    with schema_context(get_public_schema_name()):
        if RealNamePackage.objects.filter(key_id=key_id).exists():
            raise _duplicate_key_id_error(key_id)
        try:
            # ⚠️ INSERT 包在自己的 `atomic()` 里、下面的 `except` 在它**外面**
            # （先例 = `apps/accounts/views.py::create`）：IntegrityError 穿出内层
            # atomic 时由 savepoint 回滚把连接清干净。不包的话，`Model.save` 外面那层
            # `mark_for_rollback_on_error`（Django `Model.save` 外层的标准行为，
            # 见 django/db/models/base.py 与 transaction.py 附近实现）会把还开着的**外层**事务标记待回滚——
            # TestCase 的用例事务正是这样的外层，后续查询全部 `TransactionManagementError`。
            with transaction.atomic():
                return RealNamePackage.objects.create(
                    key_id=key_id,
                    key_secret=key_secret,
                    remark=(remark or "").strip(),
                    total_quota=total_quota,
                    expires_at=expires_at,
                )
        except IntegrityError:
            # 并发双录兜底（评审收口）：`exists()` 预检（无锁读）与 INSERT 之间
            # 有竞态窗口——两个并发 add 同时通过预检，后到的 INSERT 撞上 `key_id`
            # 的唯一约束（platform 迁移 0004）⇒ 翻译成**同一句**既有重复文案
            # （⛔ 不是未预期 500；secret 不在 IntegrityError 文本里，`raise … from None`
            # 让两支的异常形状逐字一致）。
            raise _duplicate_key_id_error(key_id) from None


def package_payload(pkg: RealNamePackage) -> dict:
    """一行的**掩码出口形状**（`list_packages` 与端点的创建响应共用，⛔ 两个形状）。

    ⛔ 键集**不含** `key_secret`（不是置空——置空仍是一次「密钥进了响应体」的走私）；
    `key_id` 掩码后出。
    """
    return {
        "id": pkg.pk,
        "key_id": _mask_key_id(pkg.key_id),
        "total_quota": pkg.total_quota,
        "used_quota": pkg.used_quota,
        "expires_at": pkg.expires_at,
        "is_active": pkg.is_active,
        "remark": pkg.remark,
        "created_at": pkg.created_at,
    }


def list_packages() -> list[dict]:
    """全部密钥包的**掩码行**（`schema_context("public")` 内读；按 id 升序）。

    ⛔ 行里**没有** `key_secret` 这个键；`key_id` 掩码后出。`expires_at` / `created_at`
    是 `datetime`（信封的 JSON 渲染器负责 ISO 化），命令侧自己按需要格式化。
    """
    with schema_context(get_public_schema_name()):
        rows = RealNamePackage.objects.order_by("id")
        return [package_payload(pkg) for pkg in rows]
