"""实名核验的**领域逻辑**（视图只管翻译异常与拼信封，⛔ 不在这里拼 HTTP）。

设计基线：`docs/specs/2026-10-10-realname-verification-design.md` **§5**（端到端流程，逐字照做）、
**§6-2b**（计费口径与每一档的证据等级）、**§6-7**（事务边界）。

## 顺序就是判据（⚠️ 本模块最容易写错的地方）

```
① 读本租户设置（每租户一行，get_or_create） ⇒ enabled 为假 ⇒ RealNameDisabled(1025)
② 本地解析身份证号（零成本、不出网） ⇒ 非法   ⇒ RealNameInvalidInput(1000)
③ 幂等短路：本租户内 (user, id_card_no, result="pass") 已有 ⇒ 直接返回那条，⛔ 不调厂商、不扣次数、不写新流水
                                              （⚠️ E-0252：命中时**补写** StaffProfile.id_card_no，
                                               见 verify_for_user 里那段的注释）
③b 按人限流（D-0251）：cache 桶按「租户 schema + 用户」+1 ⇒ 超额 ⇒ RealNameRateLimited(1029)。
   放在③之后、④之前 = 两条「零出网」路径（②/③）不计数；检查与递增都在厂商调用之前
④ with transaction.atomic():          ← 事务边界 = spec §6-7（判据以那一节为准）
     a. quota.lock(now=…)            ⇒ QuotaError(1026 / 1027) 直接往上抛
     b. 从 locked.package 取 key_id / key_secret
     c. vendor.verify(…)             ← **在事务内**（有意取舍，代价见下）
     d. 分档处置 → 扣几次 / 结果是什么（见 `_decide` 上方那张表）
     e. pass ⇒ 把号落进 StaffProfile.id_card_no（fail 时⛔ 不落，spec §14-P2）
     f. 写 RealNameCertRecord（任何一档都写）
⑤ 返回那条记录
```

## ⛔ 为什么没有任何 `set_rollback(True)`（这是有意的，别"补"回来）

spec §6-7 的 v0 草稿写的是「不可计费的那一档 `set_rollback(True)` 再 return」。**本实现不需要它**：
「不扣」这一档**压根不去改计数器**（`quota.charge()` 根本没被调用），所以没有什么要回滚的东西
—— 流水的写入与"扣不扣"是**两次独立的写**，而不是"先扣了再撤销"。
反过来，一旦这里出现裸 `return` 或 `set_rollback`，就会踩 `AGENTS.md` §5-E / **E-0022**：
`atomic()` 是**事务不是回滚点**，块内正常 `return` 会**提交**前面几笔写入
⇒ 留下"扣了次数却没有流水"或"流水写了、次数没扣"的半截状态。

## ⚠️ 为什么把出网调用放进事务里（代价必须认）

行锁会**跨越一次网络往返**（厂商平均 ~300ms，最坏到超时上限）⇒ 同一租户的并发核验会**排队**，
一个超时会把锁攥到超时为止。本场景可接受（子账号在个人页偶尔发起一次，不是高频接口）。
好处是「厂商失败就不扣次数」变成**结构性**的（事务回滚），不靠事后补偿代码
（补偿漏一次就**永久少记一次**，而且是**静默**的）。⛔ 别以为是漏了。

## ⚠️ 超时那一支的残差（spec §6-7 末段，如实登记）

我们超时回滚/保守记账，但厂商**可能已经记录了那一次**。这个差异**无法从我们这一侧消除**
（要厂商侧对账）⇒ ⛔ 不许在任何注释或文案里写"绝不漏计费"。
"""
from __future__ import annotations

from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import StaffProfile
from apps.business.roles import WORKER
from apps.realname import idcard, quota, rate_limit, vendor
from apps.realname.models import CertMethod, CertResult, RealNameCertRecord, RealNameSetting

#: 各列的列宽（**唯一出处**：`apps/realname/models.py`）。落库前按它裁，见模块 docstring。
VENDOR_MESSAGE_MAX = 191
VENDOR_RESULT_MAX = 8
VENDOR_REQUEST_ID_MAX = 64
PACKAGE_REMARK_MAX = 120
NAME_MAX = 64
ID_CARD_MAX = 18

#: 厂商 `data.result` 的「一致」取值（`1` 一致 / `2` 不一致 / `3` 库无，spec §3）。
RESULT_MATCH = "1"


class RealNameDisabled(Exception):
    """本租户**没有启用**实名认证（设置里的总开关为假）—— 调用方翻成 `1025`。

    ⚠️ 这是**预期**的业务拒绝（spec §5 第 ⓪ 步："只有启用了子账号那边才能发起实名认证"），
    不是异常路径的兜底。
    """


class RealNameInvalidInput(Exception):
    """身份证号**本地**就不合法（长度 / 字符 / 校验位 / 出生日期）—— 调用方翻成 `1000`。

    ⚠️ 这一档**故意先于出网**：厂商自己也会拦（实测 `code:400`），但先拦能省一次出网，
    而更重要的是一条**明确的本地判据**（用户看到的是"号码不对"，不是"余量不足"）。
    """


def _truncate(value: str, limit: int) -> str:
    """把厂商/用户给的值裁到列宽。⛔ 这不是"防御性写法"——它是**承重**的：

    厂商的 `message` 是自由文本、长度没有任何承诺；不裁的话 `DataError` 会让整个核验端点
    变成 **HTTP 500**（用户看到"服务器错误"，我们白花一次核验的钱）。
    判据：T6 用例里有"超长 `vendor_message` 照样 200 且流水落库"的形状
    （见 `tests/test_realname_verify_api.py` 的截断用例）。
    """
    text = "" if value is None else str(value)
    return text[:limit]


def get_or_create_setting() -> RealNameSetting:
    """取本租户那一行设置（没有就按**默认值**建一行）。

    ⚠️ **租户标识只来自连接上下文**（`connection.schema_name`），
    ⛔ 绝不接受任何请求参数传进来的租户标识（`AGENTS.md` §4 铁律）。
    ⚠️ 并发首访靠列上的唯一约束 + Django `get_or_create` 的 `IntegrityError` 重取兜住
    ⇒ 全表恰好一行（判据：`tests/test_realname_verify_api.py` 的并发用例）。
    """
    setting, _created = RealNameSetting.objects.get_or_create(
        tenant_schema=connection.schema_name
    )
    return setting


def get_setting() -> RealNameSetting | None:
    """**只读**地取本租户那一行设置；没有就是 `None`（调用方按默认值处理）。

    ⚠️ 与 `get_or_create_setting()` 的分工（同 `accounts/views.py::_realname_setting_row`
    的既有分工）：那个会**建**行（读回填 / 核验路径要它），本函数**只读**——
    接单闸在**抢单事务里**跑，⛔ 不能带「缺行就 INSERT」的副作用：实测（本线实装时
    `tests.test_claim_concurrency` 变红、干净 HEAD 对照绿的分辨实验）并发首抢会在
    `tenant_schema` 的唯一索引上互相等第一个人的未提交插入（冷启动竞态下生产同样
    会出现互相阻塞的窗口）。行不存在 ⇒ 按**默认值**处理（全 False），
    与 `get_or_create` 刚建出的默认行**可观察等价**。
    """
    return RealNameSetting.objects.filter(tenant_schema=connection.schema_name).first()


def _decide(vr: vendor.VendorResult) -> tuple[bool, str]:
    """把厂商结果翻成 `(这一档扣不扣次数, 落库的 result)`。⛔ **顺序即判据**。

    | 厂商这一档 | 扣不扣 | `result` | 证据等级（spec §6-2b 原文） |
    |---|---|---|---|
    | `ok` 且 `result_usable`（`code:200` + `result ∈ {1,2,3}`） | **扣** | `1` ⇒ pass，`2`/`3` ⇒ fail | **实测**（官方文档：查得即计一次，一致与不一致都算） |
    | `ok` 且 **不** `result_usable`（`code:200` 但拿不到可判定的结果码） | **扣** | fail | **实测定案**：已查得、已被计费，只是结果值形状变了 |
    | `error_kind == "invalid"`（`code:400`） | **不扣** | fail | ⚠️ **推断**（"本地就被拦、没进核验层 ⇒ 不计费"是推断，不是实测；spec §6-2b 要求发版前用真实余量对照实验回填） |
    | `error_kind ∈ {"vendor_error", "network"}`（`code:1000/9999` / 超时 / 非 JSON） | **扣** | fail | ⚠️ **无依据**，走 spec §6-2b 的**保守规则**：无法判定的失败档一律**照扣 1 次**（"宁可多扣租户一次，不可少记我们一笔"） |

    ⛔ **不许**把第二档并进 `vendor_error`：那会让它落进"不可计费"集合 ⇒
    **静默少记一次我们花钱买的次数**（spec §6-2b 第二行明文点名）。
    ⛔ **不许**因为"看起来不收"就静默不扣最后两档 —— 少记**没有任何人会察觉**，
    直到哪天对账对不上。
    """
    if vr.ok and vr.result_usable:
        # ⭐ 唯一的"可判定结果"档：`1` ⇒ pass，`2` / `3` ⇒ fail。
        charged = True
        result = CertResult.PASS if vr.vendor_result == RESULT_MATCH else CertResult.FAIL
    elif vr.ok:
        # 已查得、已被计费，只是结果码形状不对 ⇒ 照扣、落 fail（⛔ 不许当 pass、也不许谎报"没查得"）。
        charged = True
        result = CertResult.FAIL
    elif vr.error_kind == vendor.KIND_INVALID:
        # `code:400`（HTTP 200 上挂着的那个码）：本地校验就没过 ⇒ 不扣。
        # ⚠️ 证据等级 = **推断**（待 spec §6-2b 的余量对照实验回填），⛔ 不许写成"实测不收费"。
        charged = False
        result = CertResult.FAIL
    else:
        # `vendor_error`（`code:1000/9999`）/ `network`（超时 / 连不上 / 非 JSON）：
        # ⚠️ 证据等级 = **无依据** ⇒ 走保守规则**照扣**（spec §6-2b 末段：
        # "宁可多扣租户一次，不可少记我们一笔"）。⛔ 别把这一档并进上面的 `invalid`。
        charged = True
        result = CertResult.FAIL
    return charged, result


def verify_for_user(*, user, name: str, id_card_no: str) -> RealNameCertRecord:
    """给 `user` 核验一次「姓名 + 身份证号」，返回**落库的那条流水**。

    抛出的都是**预期**失败（调用方逐个翻成业务码，⛔ 不许 `except Exception` 一并接）：

    - `RealNameDisabled` ⇒ `1025`（本租户没启用）
    - `RealNameInvalidInput` ⇒ `1000`（号码本地就不合法）
    - `rate_limit.RealNameRateLimited` ⇒ `1029`（同一用户提交过于频繁，D-0251）
    - `quota.QuotaError` ⇒ `1026` / `1027`（本租户没余量 / 平台没可用包）

    未预期的异常（数据库炸了、厂商客户端抛了）**不在这里接** ⇒ 让它响亮地 500
    （`AGENTS.md` §5-E / E-0010：把未预期异常伪装成业务结果会让监控失明）。

    ⚠️ 幂等短路**只认 `result="pass"`** 的记录（spec §5 第 ④ 步 / §14-P3）：
    同一号码重复提交 ⇒ 直接返回那条旧记录，不调厂商、不扣次数、不写新流水
    （防"用户连点五次按钮扣五次钱"）。⚠️ 失败的号码**不短路**：允许用户改对之后重交。
    """
    applied_at = timezone.now()

    # ① 总开关（对标 `CertSwicth`）。⛔ `enabled` 用真值判断**是对的**：它是真 bool 列，
    # 属 `AGENTS.md` §5-E 明写的那个例外（"读层已把值归一化成真 bool 的限制开关"）。
    setting = get_or_create_setting()
    if not setting.enabled:
        raise RealNameDisabled("实名认证功能未启用，请联系管理员")

    # ② 本地格式校验 + 解析性别/生日/年龄（厂商这条接口不回这两个值，只能本地算）。
    # ⚠️ `today` 显式传 `timezone.localdate()`：不传的话 `idcard.parse` 会退回
    # `date.today()`（进程本地时区），而本项目 `USE_TZ=True` / `TIME_ZONE=Asia/Shanghai`
    # ⇒ 两台机器时区不同就会算出**差一天**的周岁（跨生日当天的核验最容易踩）。
    info = idcard.parse(id_card_no, today=timezone.localdate())
    if info is None:
        raise RealNameInvalidInput("身份证号格式不正确，请核对后重新提交")

    # ③ 幂等短路（⚠️ 判据字段是 `result`、取值 `"pass"` —— ⛔ 不是 v0 草稿写的 `matched`，
    # 那个值在 choices 里不存在，照抄会让这一整条恒假）。
    existing = (
        RealNameCertRecord.objects.filter(
            user=user, id_card_no=id_card_no, result=CertResult.PASS
        )
        .order_by("-id")
        .first()
    )
    if existing is not None:
        # ⭐ E-0252：短路的「零出网、零额度、零新流水」口径**不变**，但档案补写不能省——
        # `reset_realname` 只清档案、**保留** pass 流水（口径明文不动流水）⇒ 短路命中的
        # 瞬间档案可能是空的：用户重验同一个明明过审的号，接口回「已通过」，
        # D-0245 接单闸却继续拦（卡死）。这里把「pass 那次本来就会写的那一列」补回去：
        # 幂等（档案已是同一个号就一行都不写）、零出网、零额度。
        # ⚠️ 无档案行**不建档**（照任务书口径）：建档是 pass 主路径（下面第 e 步）的语义，
        # 短路只负责「把清掉/丢掉的号写回**既有**行」。
        profile = StaffProfile.objects.filter(user=user).first()
        # ⭐ E-0260：补写**仅限档案为空**——档案已有号（哪怕与本次提交不同，比如管理员
        # 手填过）一律不动：那是有主的真值，不能被旧验短路的号无痕迹地盖掉
        # （留痕只记「提交过」，盖掉就是静默丢管理操作）。
        # E-0252 的死锁修复照旧：档案为空（reset 只清档案、留流水）⇒ 照样补回这一列。
        if profile is not None and not (profile.id_card_no or "").strip():
            profile.id_card_no = id_card_no
            # ⚠️ 与下面第 e 步同款：只写这一列，不顺手把整行再写一遍。
            profile.save(update_fields=["id_card_no"])
        return existing

    # ③b 按人限流（D-0251）：**检查 + 递增都在厂商调用之前**。
    # 放在③短路之后 = 本地格式失败（②）与 PASS 短路（③）两条「零出网」路径天然不计数；
    # 被限流的那一次也零出网、零扣减、零流水（判据网：`tests/test_realname_rate_limit.py`）。
    # ⚠️ 有意取舍（如实登记）：1026/1027（余量/包）拒掉的提交**也计数**——递增点在进入
    # 配额事务**之前**，被限流的人不必先排队等配额行锁才被告知「太频繁」，而「拿着错误
    # 信息反复提交」本身就是这一档要拦的形状；不锁定，窗口滚过自动恢复。
    rate_limit.register_attempt(user=user)

    # ④ 事务：锁两层余量 → 出网 → 扣减 → 落流水（spec §6-7 的顺序，⛔ 不许调换）。
    with transaction.atomic():
        # a. 锁本租户额度行 + 一个可用包行。⚠️ 行不存在 / 余量为 0 / 没有可用包
        # 一律是 `QuotaError`（1026 / 1027）⇒ 直接往上抛，本函数不接（那是调用方的活）。
        locked = quota.lock(now=applied_at)

        # b. 密钥**从这一行包取**（B2：密钥可多条并存、各带自己的额度）。
        # ⛔ 本函数不 `repr` / 不打印 / 不写日志：`key_secret` 是全平台最贵的字符串（spec §6-3）。
        key_id = locked.package.key_id
        key_secret = locked.package.key_secret

        # c. 出网（唯一出站点 = `apps/realname/vendor.py`）。⚠️ 在事务里 ⇒ 行锁跨一次网络往返（代价见模块 docstring）。
        vr = vendor.verify(name, id_card_no, key_id=key_id, key_secret=key_secret)
        certified_at = timezone.now()

        # d. 分档处置（判据与证据等级全在 `_decide` 的 docstring 里）。⛔ 这一档**只决定扣不扣**，
        # 不决定"写不写流水"——流水下面**无条件**写（spec §5 第 ⑦ 步 / 评审 Blocking #2）。
        charged, result = _decide(vr)
        if charged:
            quota.charge(locked)

        # e. `pass` ⇒ 把**已核验的号码**落进档案（spec §5 第 ⑧ 步）。
        # ⛔ `fail` 时**不落**（spec §14-P2 业务方口径："没核过就不该落库"）。
        # （`StaffProfile` 用模块顶部的 import；E-0252 的短路补写也在函数前半用到它，
        # 函数内再 import 一份会让整个函数作用域把它当局部变量 ⇒ 前半句 UnboundLocalError。）
        if result == CertResult.PASS:
            profile, _created = StaffProfile.objects.get_or_create(user=user)
            profile.id_card_no = id_card_no
            # ⚠️ 只写这一列（`update_fields`）：`get_or_create` 刚建的档案走这一次 `save`
            # 已经够了，而**不是**顺手把整行再写一遍（那会覆盖并发改动的其它列）。
            profile.save(update_fields=["id_card_no"])

        # f. 写流水（**任何一档都要写**；`fail` 也要写，否则用户看不到"我提交过"）。
        record = RealNameCertRecord.objects.create(
            user=user,
            user_name_snapshot=_truncate(user.get_username(), NAME_MAX),
            name=_truncate(name, NAME_MAX),
            id_card_no=_truncate(id_card_no, ID_CARD_MAX),
            gender=info.gender,
            birthday=info.birthday,
            age=info.age,
            # 我们只买到了基础认证（二要素）⇒ 本批恒为 `basic`（spec §13-2-1）。
            cert_method=CertMethod.BASIC,
            result=result,
            vendor_code=vr.vendor_code,
            vendor_result=_truncate(vr.vendor_result, VENDOR_RESULT_MAX),
            # ⚠️ **落库前裁到列宽**：厂商原文可能超长，不裁就是 `DataError` ⇒ 500。
            vendor_message=_truncate(vr.vendor_message, VENDOR_MESSAGE_MAX),
            vendor_request_id=_truncate(vr.vendor_request_id, VENDOR_REQUEST_ID_MAX),
            applied_at=applied_at,
            certified_at=certified_at,
            # 二要素没有认证图片 ⇒ 恒空，列照渲染（spec §13-2-4）。
            cert_image="",
            package_pk=locked.package.pk,
            grant_pk=locked.grant.pk,
            package_remark=_truncate(locked.package.remark, PACKAGE_REMARK_MAX),
        )

    return record


def latest_pass_record(user) -> RealNameCertRecord | None:
    """该用户最近一条 `pass` 流水（没实名过就是 `None`）。

    用途：`GET /api/auth/realname/` 的 `verified_at`（**认证时间**取自流水，不另建列）。
    ⚠️ 与 `StaffProfile.id_card_no` 的关系如实登记：管理员可以**手填**身份证号
    （`apps/accounts/views.py`），那种情况下档案非空而本函数回 `None`
    ⇒ 「已实名」的判据仍以档案为准（`is_real_name`），`verified_at` 只作展示（spec §10.3 / §14-P9 的残差）。
    """
    return (
        RealNameCertRecord.objects.filter(user=user, result=CertResult.PASS)
        .order_by("-id")
        .first()
    )


# ── 「未实名不允许接单」闸（D-0245；spec `docs/specs/2026-10-16-realname-claim-gate-design.md` §2）──
# ⚠️ 判定的**唯一出处**在本文件：接单侧（`apps/business/claim.py::check_grab_limits`）与
# 指派侧（`apps/business/order_writes.py::assign_order`）都只调下面这两个函数，
# ⛔ 不许在任何一侧把「开关 + 代练员 + 未实名」这组判据再抄第二份（E-0021 的形状）。
# ⚠️ 机器自动接手（`jidanpu_member.machine_claim_order`，裁定 8 `enforce_limits=False`）
# **不进** `check_grab_limits` ⇒ 天然不受本闸影响——上游同步不能卡死，这是业务方拍板的边界
# （spec §5），⛔ 不许把闸塞进机器路径。


class RealNameClaimBlocked(Exception):
    """「未实名不允许接单」开启时，未实名的代练员尝试接单（或被指派接单）。

    ⚠️ 与本模块其余异常同一口径：这是**预期**的业务拒绝，不是异常路径的兜底。
    ⚠️ 本异常**不携带**用户可见文案：两个消费方各翻各的错误形状、各用各的文案
    （抢单侧 → `ClaimError("请先完成实名认证后再接单", …)`，指派侧 →
    `WriteOutcome` 失败 + 「该打手未完成实名认证，不能指派接单」，spec §2.3）。
    """


def is_real_name(profile) -> bool:
    """「已实名」判据的**单一出处**（spec §2.1）：档案存在**且** `id_card_no` **strip 后**非空。

    ⚠️ E-0252：判空必须**去掉空白后**判——`bool(" ")` 为真，档案上躺着一个纯空格
    （历史脏数据 / 任何旁路写入）就会被判「已实名」。写入侧同一次收口
    （`apps/accounts/serializers.py::AccountWriteSerializer.validate_id_card_no`：
    手填 strip、空白归空），两侧同一口径；`or ""` 挡的是 `id_card_no=None` 的形状。
    ⚠️ 这条语义一字未改地收编两处既有内联（`apps/realname/views.py` 的实名状态端点与
    `apps/core/profile_views.py` 的资料 payload）——「管理员手填的号码也算已实名」
    仍是既有口径（实名线 spec §10.3），本函数只是把它收成一份；
    ⛔ 不许在任何调用方再写第二份内联（AST 结构网钉着：`tests/test_realname_claim_gate.py`）。
    ⚠️ `profile` 允许传 `None`（查无档案）：`bool(None and …)` 恒为 `False`，调用方不必先判空。
    ⚠️ 重置实名（`accounts/views.py::reset_realname`）就是清空 `id_card_no` ⇒ 本判据随之变假。
    """
    return bool(profile and (profile.id_card_no or "").strip())


_UNREAD_CLAIM_SETTING = object()


def claim_gate_blocks(*, user, setting=_UNREAD_CLAIM_SETTING, use_prefetched_user=False) -> bool:
    """只读检查接单资格。默认查库，避免沿用重置实名前后的旧档案。
    批量筛选可传 use_prefetched_user=True，前提是档案和角色均由本次列表查询预加载。
    """
    if user is None:
        return False
    if setting is _UNREAD_CLAIM_SETTING:
        setting = get_setting()
    if setting is None or not setting.block_claim_without_cert:
        return False
    is_worker = (
        any(group.name == WORKER for group in user.groups.all()) if use_prefetched_user
        else user.groups.filter(name=WORKER).exists()
    )
    if not is_worker:
        return False
    profile = (
        getattr(user, "staff_profile", None) if use_prefetched_user
        else StaffProfile.objects.filter(user=user).first()
    )
    return not is_real_name(profile)


def ensure_can_claim(*, user) -> None:
    """接单闸（spec §2.2）：合格 ⇒ 正常返回；被拦 ⇒ `RealNameClaimBlocked`。

    接单侧唯一调用方是 `apps/business/claim.py::check_grab_limits`（它把本异常翻成
    `ClaimError`）。判定与指派侧同源（`claim_gate_blocks`）——本函数只是
    「布尔判定 → 异常」的翻译，⛔ 不许让两个写路径各写一份判定。
    """
    if claim_gate_blocks(user=user):
        raise RealNameClaimBlocked()
