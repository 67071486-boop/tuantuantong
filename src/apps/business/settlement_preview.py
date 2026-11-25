"""结算预览（`GET /api/business/settlements/preview`）的取数与聚合——**只读**。 **它回答什么**：财务在「清算绩效」弹窗上、点「确定结算或修改」**之前**看到的那几格数字（所选订单笔数 / 预计要结总额 / 实际需结总额 / 结算对象 / 微信收款码）。对标物那颗按钮打开的是子页 `/Emp/SettlementEmp?ids=…`（服务端渲染），本模块是它的数据面。 ⛔ **本模块不写库、不写审计、不改 `updated_at`**：它只查、只算、只签名 URL。 ## 逐字段口径与出处（证据件见 `docs/plans/2026-10-03-settlement-emp-popup.md` §1.2） | 字段 | 口径 | 出处 | |---|---|---| | `count` | **命中行数**（查不到的 id 不计入；全查不到 ⇒ `0`） | 对标实测（`ids=999999999` ⇒ `0笔`） | | `temp_total` | `Σ temp_amount`（预计绩效） | 对标实测（「预计要结总额」） | | `bonus_total` | `Σ payable_net`（`bonus_amount − 扣点额 + Σ未结账调整记录`），**只**累加「这一趟真的会结掉」的行（P1-2） | **有意偏离**，见下 | | `settled_count` | 命中行里 `settle_status=已结账` 的**行数** | 我们的算式（对标子页没有这一格） | | `unsettled_count` | 命中行里 `settle_status=未结账` 的**行数** | 同上 | | `owners` | 按 `user` 聚合 `payable_net`（**同样只含可结的行**），**用户名升序**；每行 `{user_id, username, name, amount}`（`name` = 显示名，2026-10-04 追加） | 对标实测「结算对象 = 按人聚合的生效绩效」；顺序是我们取的确定性顺序 |
| `qr_codes` | 每个归属人 `StaffProfile.wechat_qr_key` 的短时效签名 URL | 对标实测（`GET /EmpAjax/GetEmpQrCode` 的 `{userName, qrCode, name}`） | | `errors` | `payable_net` 算不出来的行（按 `0.00` 计入）＋ **这次结不掉的行的原因**（逐 id） | 我们的取舍（不静默当 0 / 不静默多算） | ### ⚠️ `bonus_total` / `owners` 的口径（P1-2）两格都**只**累加 `settlement._resolve_batch` 判为「这一趟真的会结掉」的行（`SettleBatch.settleable_ids` = `rows` 腿 ∪ `adjustments` 腿 − 脏行）。⇒ 归属人被冻结、订单在池内、无归属人、已完全结清、留着结账痕迹的行**都不进钱**——预览的数字因此与 `/settlements/settle` 点下去的结果同口径（过去是按「可见行」求和，多算了永远结不掉的钱）。 ⚠️ `count` / `temp_total` / 两个计数**仍是所选行**的口径（对标那几格不筛结账状态）；「已完全结清 ⇒ 跳过」是**成功路径**（不结钱但不报错），它不进 `errors`，只不进金额。 ### ⚠️ `bonus_total` 的一处**有意偏离**（已登记在计划 §3 T2.1）对标实测的「实际需结总额」是 `Σ bonusAmount`（生效绩效），而**我方真正要结的钱**是 `payable_net`——`settlement.settle_performances` 第 4 步算的、写进 `settled_amount` 快照的就是它。有扣点额 / 未结账调整记录时两者不同：显示 `Σ bonus_amount` 会给出**一个与实际付款不一致的数**（财务照着它对账就是对不上）。**无扣点、无调整时两者恒等**，所以对标已
观测到的那几组读数在本模块下逐字复现。 ### ⚠️ `AmountError`（扣点越界等）的处理扣点额 > 预计绩效、或提成比例越界时 `payable_net` 会抛 `AmountError` （`settlement_amount.payable_net` 的 docstring 明写它会透传）。本端点是**逐行**调用，一个坏行不该把整个弹窗打不开，但**也不能把它静默当成 0**——那是一笔不报错的错账（E-0010 同族）。处置（照 `settlement.py::_row_nets` 的「不吞、不折算」精神取**读**侧的那一半）： - 该行的 `payable_net` **按 `0.00` 计入** `bonus_total` 与 `owners`； - 同时在响应的 `errors` 里给出一条**能定位到行**的记录（`{"performance_id": …, "code": …, "message": …}`，`code` / `message` 原样取异常的）。 ⇒ 财务在弹窗上看到的是「这行没算进去 + 为什么」，而不是「净额 0」这句假话，也不是整页 500。 ⚠️ **写路径不受本模块影响**：`/settlements/settle` 照旧把 `AmountError` 翻成 `SettleError`、 **整批拒绝**、一行不写。 ### ⚠️ 两处**我们自己定的**取舍（对标没有对应行，写在这里免得后来者以为是漏了） 1. **无归属人（`user` 为 NULL）的行**：并入 `owners` 里 `{"user_id": null, "username": null, "name": null}` 这一项（金额照算），**排在最后**。理由：跳过它会让 `owners` 的金额之和与 `bonus_total` 对不上，而弹窗上这两格是并排显示的——那才是更难解释的形态；`username: null` 这个形状本身就能让调用方识别出「这笔钱暂时没有主人」（前端可以据此提示）。对标**不存在**这种行（它的绩效行恒有归属人）。 2. **`qr_codes` 里 `name` 与 `owners` 里 `name`**：都取 `apps.accounts.display.user_display_name`（昵称优先、空串回落登录账号）。 `qr_codes.name` 是对标那两行名字的对齐（哪行是登录名、哪行是姓名我方数据里没有可对拍的字段 ⇒ 这一格是**我们的取舍**，不是对标实测）；`owners[].name` 是 2026-10-04 「显示人统一成昵称」线追加的**显示名键**（`username` 仍保留、是老键，前端按 `name ?? username` 回落）。两处的 `username` 都是登录账号（与 `settlement_views.SettlementPeopleView` 的 `username` 同义）。 ### 数据范围与结算列表**同一套**卡点：`scope_queryset(user, …, scope_filter=game_scope("order__game_id"))`，并显式排除已软删订单与已软删绩效行（跨关系 JOIN 不经过 `Order` 的默认 manager）。 ⇒ 跨租户 / 越出自己游戏范围的 id **不进** `count`，钱也不会被算出来。"""
from decimal import ROUND_HALF_UP, Decimal

from apps.accounts.display import user_display_name
from apps.accounts.models import StaffProfile
from apps.business.game_scope import game_scope
from apps.business.models import Performance, SettleStatus
from apps.business.performance_amount import AmountError, CENT
# ⭐ P1-2（计划 §3）：**结账侧第 2–3 步的同一份解析**——预览过去自己按「可见行」求和，
#    没套结账那三道闸（订单已完成 / 不在池内 / 归属人未冻结），弹窗数字与点下去的结果
#    对不上。现在两边共用 `_resolve_batch` / `_reject_reasons`，⛔ 不要在这里另写一份条件。
from apps.business.settlement import (
    _reject_reasons,
    _resolve_batch,
    unsettled_adjustments_prefetch,
)
from apps.business.settlement_amount import payable_net
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset

#: 加法与量化的起点（`Decimal` 加法是精确的，这里只是给初始值一个 2 位小数的形状）。
ZERO = Decimal("0.00")


def _money(value):
    """收口成 `CENT`（2 位小数、四舍五入）——与 `settlement_amount` 里的量化同口径。"""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def preview_rows(performance_ids, *, user):
    """这批 id 里**当前可见**的绩效行（口径与结算列表的底集同源）。 - 过 `scope_queryset` + `game_scope("order__game_id")`：别人的游戏 / 别人的租户的行
    在这里就落空（⇒ 不进 `count`、不算钱）； - 已软删单的绩效行与**单条软删**的绩效行同判（`order__deleted_at` / `deleted_at`）； - `prefetch_related(unsettled_adjustments_prefetch())` 是 `payable_net` 的入参契约
    （`instance.unsettled_adjustments`），筛法只有那一处定义； - `order_by("id")` 只为让遍历顺序确定（响应里 `owners` 的排序是另外显式排的）。 ⛔ 本函数是**读**：不写库、不写审计。"""
    queryset = (
        Performance.objects.select_related("order", "user")
        .prefetch_related(unsettled_adjustments_prefetch())
        .filter(
            id__in=performance_ids,
            order__deleted_at__isnull=True,
            deleted_at__isnull=True,
        )
        .order_by("id")
    )
    return list(
        scope_queryset(user, queryset, scope_filter=game_scope("order__game_id"))
    )


def _owner_sort_key(owner):
    """`owners` 的确定性顺序：用户名升序；**无归属人那一项排最后**（`None` 不可比）。"""
    return (owner["username"] is None, owner["username"] or "")


def _qr_codes(owner_rows):
    """`owners` 里**有收款码**的那些人的签名 URL（一次查完档案，⛔ 不逐人查）。 - 只取 `user` 非空的人（对标 `/EmpAjax/GetEmpQrCode` 的 `data: []` 就是这个形态）； - 空 `wechat_qr_key` = 没上传过 ⇒ 这个人不出现在结果里； - **签名失败**（存储层异常）⇒ 这个人也不出现，**不让整个弹窗 500**——与 `api/serializers.py::PerformanceListSerializer.get_wechat_qr_url` 的处置同口径（那一处也是「签名失败回 `None`」）。代价如实登记：存储出故障时收款码静默少一张。
    ⚠️ **`except Exception` 的范围与代价（行为不变）**：那个 `except` **只包 `get_storage().presign_get(key).url` 这一句**（`get_storage()` 也在里面），捕到的是**存储层的任何**异常——包括**未预期**的那些（例如 `CosStorage.__init__` 的 `import qcloud_cos` 失败、`settings.STORAGE_BACKEND` 写错、SDK 抛出的非 `CosServiceError` 异常）。代价就是 m-3 点名的那句：**未预期错误会被静默**—— 弹窗上只是「这个人没有收款码」，没有红、没有日志。
    **为什么不收窄到具体异常类型**（读码依据）： `apps/core/storage.py` 的 `Storage` 是**可插拔**的基类（`presign_get` 是 `raise NotImplementedError` 的抽象口），今天两个实现抛的东西完全不同： `LocalStorage.presign_get` 只拼一个 f-string（**不碰 `_path`**，实际抛不出）； `CosStorage.presign_get` 走 SDK 的 `client.get_presigned_url(...)`（可抛 `CosServiceError` / 参数错 / 网络错）。把这里写死成今天的某几个类型 ⇒ **换一个存储后端就退化成「一张码算不出来 = 整个弹窗 500」**（那正是本条要避免的）。 ⇒ 与 `api/serializers.py` 里 `PerformanceListSerializer.get_wechat_qr_url` 那一处同款先例（它也是「签名失败回 `None`」的宽捕），**保留宽捕**、把代价写在这里。 ⚠️ 这条宽捕**只在这个只读的展示支路**成立：写路径（上传收款码）遇到同类故障是 **响亮**的，不会跟着静默。"""
    from apps.core.storage import get_storage

    user_ids = [row["user_id"] for row in owner_rows if row["user_id"] is not None]
    if not user_ids:
        return []
    profiles = {
        profile.user_id: profile
        for profile in StaffProfile.objects.select_related("user")
        .filter(user_id__in=user_ids)
        .exclude(wechat_qr_key="")
    }
    codes = []
    for row in owner_rows:
        profile = profiles.get(row["user_id"])
        if profile is None:
            continue
        try:
            url = get_storage().presign_get(profile.wechat_qr_key).url
        except Exception:  # noqa: BLE001 — 只包**这一句**；见 docstring 的代价段
            continue
        codes.append(
            {
                "user_id": row["user_id"],
                "username": row["username"],
                "name": user_display_name(profile.user),
                "url": url,
            }
        )
    return codes


def build_settlement_preview(performance_ids, *, user):
    """结算预览的响应体 `data`（逐字段口径见模块 docstring）。 ⚠️ **入参先 `sorted(set(...))`**：重复 id 只算一次（`count` 是**行数**），与 `settlement.settle_performances` 第 1 步的去重同口径。 ⭐ **P1-2（计划 §3）：钱只算「真的会结掉」的行**。可结集合由 `settlement._resolve_batch(..., lock=False)` 给出（结账侧第 2–3 步的同一份实现，
    ⛔ 这里不复制条件）： - `rows` 腿 ∪ `adjustments` 腿（坏账出口）− 脏行（`already_traced`）= `settleable_ids`； - `count` / `temp_total` / 两个计数仍是**所选行**的口径（对标那几格不筛结账状态）； - `bonus_total` 与 `owners[].amount` **只**累加 `settleable_ids` 里的行——被冻结的
    归属人、池内订单、无归属人、已完全结清、留着结账痕迹的脏行都**不进金额**； - 那些行**为什么**没进金额仍写进 `errors`（前端不渲染它，只供接口层排查；计划 §3 明写本批只钉数字）。 ⚠️ 「已完全结清 ⇒ 跳过」是**成功路径**（`/settle` 返回 0、只是不结钱），所以它不进 `errors`；进 `errors` 的是「让整批 1006」的那几档（冻结 / 池内 / 无归属人 / 脏行）。 ⚠️ 只给**命中了的行**写原因：查不到的 id 过去就不进 `count`、也不报错（`test_an_all_miss_reports_zero` 钉着那个响应形状），给它们补「不存在或不可见」只会让打错 id 的请求多出一堆噪音。"""
    ids = sorted(set(performance_ids))
    rows = preview_rows(ids, user=user)
    # ⚠️ 只读：`lock=False`（预览端⛔ 不许拿 `select_for_update`）。
    batch = _resolve_batch(ids, user=user, lock=False)
    settleable = batch.settleable_ids

    temp_total = ZERO
    bonus_total = ZERO
    settled_count = 0
    unsettled_count = 0
    owners = {}
    errors = []
    for performance in rows:
        temp_total += Decimal(performance.temp_amount)
        # ⚠️ 两态各数各的（不用「count − settled」推）：`settle_status` 是带 choices 的列，
        #    但存量脏值不该被静默算进某一态（E-0024 同族：断言/统计不靠减法猜）。
        if performance.settle_status == SettleStatus.SETTLED:
            settled_count += 1
        elif performance.settle_status == SettleStatus.UNSETTLED:
            unsettled_count += 1
        try:
            net = payable_net(performance, performance.unsettled_adjustments)
        except AmountError as exc:
            # 预期失败：这一行按 0.00 计入，**同时**如实列出（见模块 docstring 的误差段）。
            net = ZERO
            errors.append(
                {
                    "performance_id": performance.id,
                    "code": exc.code,
                    "message": exc.message,
                }
            )
        if performance.id not in settleable:
            # ⭐ P1-2：这一行**这次结不掉**（不合格 / 脏行 / 已完全结清）⇒ 不进金额。
            #    原因在下面统一写进 `errors`（已完全结清那档按「成功但空操作」处理、
            #    ⛔ 不进 errors，见 docstring）。
            continue
        bonus_total += net
        owner = owners.get(performance.user_id)
        if owner is None:
            # ⭐（「显示人统一成昵称」线）：**追加** `name`（显示名键），与同文件 `_qr_codes` 那一份**同键名、同口径**（§2.4-C）。
            # ⚠️ `username` 是**加法前的既有键**，原样保留（前端按 `name ?? username` 回落）；
            # ⛔ 不改金额、不改排序（排序仍按 `username`，见 `_owner_sort_key`）。
            owner = {
                "user_id": performance.user_id,
                "username": (
                    performance.user.get_username()
                    if performance.user_id is not None
                    else None
                ),
                "name": (
                    user_display_name(performance.user)
                    if performance.user_id is not None
                    else None
                ),
                "amount": ZERO,
            }
            owners[performance.user_id] = owner
        owner["amount"] += net

    # 原因：结账侧**同一份**判据（`_reject_reasons`），只换成逐 id 的形状。
    # ⚠️ **只给「命中了的行」写原因**：查不到的 id 过去就不进 `count`（也不报错），
    # 给每个打错的 id 补一条「不存在或不可见」只是噪音；四类「算不了钱」的现场（无归属人 / 冻结 / 池内 / 脏行）都是**可见行**，照样会被写出来。
    visible_ids = {performance.id for performance in rows}
    unqualified_visible = [
        perf_id for perf_id in batch.unqualified if perf_id in visible_ids
    ]
    why_not = (
        dict(_reject_reasons(unqualified_visible, user=user))
        if unqualified_visible
        else {}
    )
    for perf_id in batch.already_traced:
        why_not[perf_id] = "已留有结账痕迹（settled_at / settled_amount），本次结不了"
    for perf_id, reason in why_not.items():
        errors.append(
            {
                "performance_id": perf_id,
                "code": ErrorCode.SETTLE_STATE_INVALID,
                "message": reason,
            }
        )

    owner_rows = sorted(owners.values(), key=_owner_sort_key)
    for owner in owner_rows:
        owner["amount"] = str(_money(owner["amount"]))

    return {
        "count": len(rows),
        "temp_total": str(_money(temp_total)),
        "bonus_total": str(_money(bonus_total)),
        "settled_count": settled_count,
        "unsettled_count": unsettled_count,
        "owners": owner_rows,
        "qr_codes": _qr_codes(owner_rows),
        "errors": errors,
    }
