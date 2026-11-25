"""租户侧的实名认证数据：核验**流水** + **每租户一行的开关配置**。

设计基线：`docs/specs/2026-10-10-realname-verification-design.md` **§4.2**（本模块两张表的逐列口径）
与 **§5**（这些列分别在哪一步被填）。

## 这两张表住在哪一侧（⛔ 别与平台表混）

| | 表 | 住哪 | 谁写 |
|---|---|---|---|
| 批发层 | `RealNamePackage` / `RealNameGrant` | `public`（`apps/platform/models.py`） | 平台侧管理命令 + `apps/realname/quota.py` |
| 零售层 | 本模块两张表 | **每个租户自己的 schema**（`TENANT_APPS`） | 核验端点（`apps/realname/services.py`） |

⛔ 本模块**不许**出现任何第三方 HTTP 调用 —— 出站唯一站点是 `apps/realname/vendor.py`（spec §6-1）。

## ⚠️ 列宽与「落库前截断」的口径（本模块最容易变成 500 的地方）

厂商的 `message` **可能比我们这一列长**（它是厂商的自由文本，没有任何长度承诺）。厂商客户端
（T3）**刻意不截断** —— 截断是**落库**这一侧的责任：不截就是 `DataError`，
而 `DataError` 在核验端点会变成 **HTTP 500**（用户看到"服务器错误"，我们丢了一次核验的钱）。
⇒ 写流水前**必须**按列宽裁（`RealNameCertRecord.VENDOR_MESSAGE_MAX` 等常量在
`apps/realname/services.py` 的 `_truncate` 一处收口）。

## ⚠️ `package_pk` / `grant_pk` 是**裸整数**，⛔ 不加外键

它们指向 `public` 里的平台表，而本表在租户 schema 里 —— 跨 schema 的外键在本仓的
「一租户一库 + 库内按 schema 分面」形态下**建不出来**（引用不到另一侧的行）。
spec §4.2 明文要求裸 `IntegerField` + 一份 `package_remark` 快照
（对账时人不至于只看到一个裸 id）。⛔ 不许"顺手"补一个 `ForeignKey`：那会让迁移在租户 schema 上失败。
"""
from django.conf import settings
from django.db import models


class CertMethod(models.TextChoices):
    """认证方式（对标 `id5`：`1` ⇒ 基础认证，否则人脸加强）。

    ⚠️ 我们**只买到了基础认证**（二要素接口，没有人脸）⇒ 本批只会写 `BASIC`，
    `FACE` 这一支**不可达**（spec §13-2-1 如实登记）。⚠️ 但枚举要照对标留着：
    列表页的「认证方式」列按这两态渲染（绿徽章 / 橙徽章），删掉它前端就没得渲染。
    """

    BASIC = "basic", "基础认证"
    FACE = "face", "人脸加强"


class CertResult(models.TextChoices):
    """认证结果（对标 `id2`：`2` ⇒ 通过认证）。

    ⭐ **幂等短路读的就是这一列的这个值**（`PASS`）。⚠️ spec §5 第 ④ 步点名过：
    v0 草稿把判据字段写成 `matched`，而那个值在 choices 里**根本不存在**
    ⇒ 照抄会让短路整条**恒假**（每次都真调厂商、每次都扣钱）。
    """

    PASS = "pass", "实名通过"
    FAIL = "fail", "实名失败"


class RealNameCertRecord(models.Model):
    """一次核验的流水（对标「人员实名记录与配置」页的那张列表）。

    ⚠️ **每一档都要落一行**（spec §5 第 ⑦ 步 / 评审 Blocking #2）：「不扣次数」与
    「不写记录」是**两件事** —— 厂商参数错、超时、服务异常这三档都不（或按保守规则）扣次数，
    但**流水一律要写**。缺了流水，用户会看到"提交了但列表里什么都没有"，
    我们也丢掉了 `vendor_message` 这个唯一的对账线索。

    ⚠️ **PII**：`id_card_no` 是明文（spec §7 如实登记：租户库里的身份证号本来就是明文，
    本线不引入新的泄露面，也不解决它）⇒ 本表的读面（列表端点）必须脱敏展示。
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="realname_records",
        verbose_name="系统用户",
    )
    #: 提交那一刻的**登录账号快照**（对标列表第一列「系统用户名称」）。
    #: ⛔ 不靠 `user__username` 现取：改过账号名之后，历史记录不该跟着变（spec §4.2）。
    user_name_snapshot = models.CharField("系统用户名称", max_length=64)
    name = models.CharField("真实姓名", max_length=64)
    id_card_no = models.CharField("身份证号", max_length=18)
    #: 性别 / 生日由**本地**从号码解析（厂商这条接口不回这两个值，实测）。
    gender = models.CharField("性别", max_length=4, blank=True, default="")
    birthday = models.CharField("出生日期", max_length=8, blank=True, default="")
    #: 提交当日算出的周岁。⚠️ `null` 是合法值（解析不出来时留空），⛔ 不用 `0` 冒充"没有"。
    age = models.PositiveSmallIntegerField("年龄", null=True, blank=True)

    cert_method = models.CharField(
        "认证方式", max_length=16, choices=CertMethod.choices, default=CertMethod.BASIC
    )
    result = models.CharField("认证结果", max_length=8, choices=CertResult.choices)

    # ── 厂商原始回包（对账与工单的唯一凭据）────────────────────────────
    vendor_code = models.IntegerField("厂商返回码", default=0)
    vendor_result = models.CharField("厂商结果原值", max_length=8, blank=True, default="")
    #: ⚠️ **落库前必须裁到 191**（厂商原文长度没有承诺，超了就是 `DataError` ⇒ 500）。
    vendor_message = models.CharField(
        "厂商结果描述", max_length=191, blank=True, default=""
    )
    #: ⚠️ 参数错（`code:400`）时厂商回**空串**（实测）⇒ 用 `blank/default=""` 接住，⛔ 不用 `null`。
    vendor_request_id = models.CharField(
        "厂商请求号", max_length=64, blank=True, default=""
    )

    #: 申请时间（对标 `str4`）= 进入核验函数那一刻。
    applied_at = models.DateTimeField("申请时间")
    #: 认证时间（对标 `str5`）= 拿到厂商结果那一刻；没拿到就是 `None`。
    certified_at = models.DateTimeField("认证时间", null=True, blank=True)
    #: 认证图片（对标 `str6`）。⚠️ 基础认证（二要素）**没有图** ⇒ 本批**恒空**，列照渲染。
    cert_image = models.CharField("认证图片", max_length=255, blank=True, default="")

    # ── 跨 schema 的**裸引用**（⛔ 不加外键，理由见模块 docstring）──────────
    package_pk = models.IntegerField("密钥包主键", null=True, blank=True)
    grant_pk = models.IntegerField("额度行主键", null=True, blank=True)
    #: 包备注**快照**：对账时不至于只看到一个裸 id（spec §4.2）。
    package_remark = models.CharField("密钥包备注", max_length=120, blank=True, default="")

    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "实名认证记录"
        verbose_name_plural = "实名认证记录"
        # 列表按倒序（对标页也是最新的在前），⛔ 不按 `created_at` 排：
        # 同一秒内的两条会拿到不稳定的顺序，分页时会跳行。
        ordering = ["-id"]

    def __str__(self) -> str:
        # ⛔ **不含身份证号**：`__str__` 会出现在日志 / 报错文本 / 关联对象的下拉里。
        return f"{self.user_name_snapshot}:{self.name}:{self.result}"


class RealNameSetting(models.Model):
    """本租户的实名认证开关与年龄门槛（对标「实名认证相关设置」弹窗的 4 项）。

    **每租户一行**（spec §4.2）。⭐ **唯一性靠数据库约束，⛔ 不靠 `get_or_create` 的运气**：
    没有约束时，两个并发的首访会各建一行（`get_or_create` 的 `get` 与 `create` 之间有缝），
    此后「读哪一行」看运气 —— 管理员在弹窗里改的那一行可能**不是**核验时读的那一行
    （症状：开关打开了、核验仍报 1025）。判据见 `tests/test_realname_verify_api.py` 的并发用例。

    ⚠️ **默认值是判据**（对标证据件的 `checked` 位置，`AGENTS.md` §5-C）：
    三个布尔**全 `False`**、两个年龄**全 `0`**。⛔ 不许图方便默认 `True` —— 那等于
    **默默打开**一个会拦接单 / 拦结算的开关（`block_claim_without_cert` / `require_cert_for_settlement`）。
    """

    #: 租户标识 = 租户 schema 名。⚠️ **只来自 `connection.schema_name`**，
    #: ⛔ 绝不接受请求参数传入的租户标识（`AGENTS.md` §4 铁律）。
    tenant_schema = models.CharField("租户标识", max_length=64, unique=True)

    # ── 对标那 4 项（文案与默认值逐条照证据件，spec §4.2 的表）──────────────
    #: 对标 `CertSwicth`：是否启用实名认证功能。**默认不启用** ⇒ 未启用时核验端点明确拒绝（1025）。
    enabled = models.BooleanField("启用实名认证", default=False)
    #: 对标 `CertPassRecOrder`：是否不允许未实名的子账号接单。**默认允许**。
    #: 消费方（D-0245）：接单闸 `business/claim.py::check_grab_limits`
    #: 经 `services.ensure_can_claim` + 指派闸 `business/order_writes.py::assign_order`，
    #: 判定单一出处 `services.claim_gate_blocks`；机器自动接手不消费（裁定 8）。
    block_claim_without_cert = models.BooleanField("未实名不允许接单", default=False)
    #: 对标 `CertOnineSett`：结算打款是否要求已实名。**默认不要求**。
    require_cert_for_settlement = models.BooleanField("结算要求已实名", default=False)
    #: 对标 `CertMinAge` / `CertMaxAge`：`0` = **不限制**（⚠️ 不是"门槛是 0 岁"）。
    #: 判据用 `!= 0` / `is not None`，⛔ 不用真值判断（`AGENTS.md` §5-E / E-0021）。
    min_age = models.PositiveSmallIntegerField("最小年龄", default=0)
    max_age = models.PositiveSmallIntegerField("最大年龄", default=0)

    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "实名认证设置"
        verbose_name_plural = "实名认证设置"
        # ⚠️ 唯一性由列上的 `unique=True` 提供（Django 生成名为
        # `<表名>_tenant_schema_key` 的唯一约束）。计划 T1 提过的具名写法
        # `UniqueConstraint(fields=["tenant_schema"], name="uniq_realname_setting_schema")`
        # 与它**等价**（都是数据库级唯一），二选一即可 ⇒ 本实现取任务书给定的列级 `unique=True`。
        # ⛔ 别把它删掉"简化一下"：删了之后并发首访会留下两行，且**没有任何异常**。

    def __str__(self) -> str:
        state = "启用" if self.enabled else "未启用"
        return f"{self.tenant_schema}:{state}"
