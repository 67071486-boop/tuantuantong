"""打手侧**内部**管理端点的载荷装配与入参校验（spec
`docs/specs/2026-10-08-taker-open-api-design.md` §3.1 / §6）。

本模块与 `apps/openapi/serializers.py` 的**应用管理那一半**同形（那是本线的先例），
但**只服务打手侧这一族**：载荷是给**租户自己**看的管理界面用的，⛔ 不是对外契约
（对外契约在 `docs/TAKER-API.md`，T7 落）。

## 三条冻结口径

1. ⛔ **入参白名单 fail-closed**（spec §6）：请求体里出现**表外**的键 ⇒ `1000` +
   **点名**那几个键、**一行都不落库**。判据是「**这类请求**不再静默」，不是「某个字段
   不再报错」——照 `apps/core/profile_views.py::MyProfileView._validate_only_editable` 的
   形状（那是本仓既有先例）。⚠️ 打手侧比上家侧**严**：上家侧那几个序列化器吃 DRF 的
   「忽略未知键」默认行为，本线**不吃**——打手是**外部**工作室，他们照文档拼错一个键名时
   「静默成功」比"报错"贵得多（他们会以为自己配好了）。
   ⚠️ **未知键**与**已知但本端点不可写的键**回的是**同一句**（`UNWRITABLE_KEYS_MSG`）：
   对调用方来说两者是同一件事（这个键不生效），⛔ 不开第二句文案。

2. ⭐ **`app_secret` 只出现在「行载荷」里**（`app_row_payload`）——列表 / 创建 / `PATCH` /
   轮换**四个出口共用一个装配函数**（⛔ 没有第二个出口，同形两份装配必然漂）。
   与上家侧 `D-0201` 的既有口径一致：**明文可随时获取**。
   ⛔ **没有放松的那一条**：它**不进任何留痕 / 操作日志 / 错误消息**
   （判据 = `tests/test_takerapi_app_api.py::TakerAppSecretNeverLoggedTests`）。

3. ⛔ **`app_key` / `app_secret` 不是入参**：由服务端生成，且**复用上家侧那一个生成口**
   （视图直接 import `apps.openapi.serializers.generate_app_key` / `generate_app_secret`，
   ⛔ **本模块不转发、不包装**——转发一层就会多一个可以各自演进的入口）——
   理由：`generate_app_secret` 复用的是 peer 通道那条既有原语
   （`apps.business.peer_sync.new_token`），另起一套会让两条通道的强度与字符集各自演进；
   而 spec §5.3 的定值（16 位 hex 的 key、43 字符的 secret）也只有一处出处。
   ⚠️ 那条纪律有一条**可证伪**的判据：`tests/test_takerapi_app_api.py::CredentialGenerationTests`
   断言视图里那两个名字**就是**上家侧那两个函数对象（`is` 判等）——谁在打手侧另起一套
   随机源，那条当场红。
"""
from rest_framework import serializers

from apps.accounts.display import user_display_name
from apps.core.datetime_display import local_iso
from apps.openapi.serializers import (
    APP_NAME_MAX_LENGTH,
    CALLBACK_URL_MAX_LENGTH,
    IP_ALLOWLIST_MAX_LENGTH,
    delivery_reason_text,
    validate_callback_url_value,
)
from apps.takerapi.models import AppState, CallbackState, TakerApiDelivery

#: 创建端点的**可写字段白名单**（spec §6 的入参：`name` / 打手账号 / 可选 `ip_allowlist`）。
#: ⚠️ 打手账号那个键叫 `taker`（与模型列同名）——`spec.app_model.taker` 的既有拼写。
#: ⭐ **T5追加两格**：`callback_url` / `callback_state`（第二期 spec
#: `2026-10-09-taker-callback-design.md` §7 的第一行「`PATCH` 允许改」）。
#: ⚠️ 为什么**创建**这一格也要加（任务书只点名了 `PATCH`）：上家侧那条端点从它的
#: T8 起就允许**建应用时一次填完**回调配置（`apps/openapi/serializers.py::AppCreateSerializer`
#: 的两个可选字段，注释写着「先建应用、再到列表里逐个改回调地址是两步」）⇒ 打手侧照**形状**
#: 对齐（本线的红线是「不抄第二份判据」，而列白名单是**形状**、判据仍在唯一那个校验函数里）。
#: ⛔ 两格都**可选**：缺席 ⇒ 用模型默认值（地址空、开关 `enabled`），⛔ **不改变**既有调用的语义。
APP_CREATE_WRITABLE_FIELDS = (
    "name",
    "taker",
    "ip_allowlist",
    "callback_url",
    "callback_state",
)

#: `PATCH` 的**可写字段白名单**：改名 / 停用启用 / 改 IP 白名单（spec §6：
#: 打手对自己那条应用能做「轮换密钥 / 停用 / 改 IP 白名单」，改名同属"改配置"这一档）。
#: ⛔ **`taker` 不在里面**：理由逐条写在 `AppUpdateSerializer` 的 docstring
#: （它是「这个应用能看什么」的唯一依据 ⇒ 改它 = 在**不换凭据**的前提下换掉整个权限面）。
#: ⭐ **T5追加两格**：`callback_url` / `callback_state`（第二期 spec §7 第一行）。
#: ⚠️ 它们与 `ip_allowlist` 同属"这个应用怎么对外说话"那一档的配置，⛔ 与 `taker` 那一档
#: （"这个应用是谁的"）是**两件事**——后者改了是**静默变权**，前者改了只是换个收件地址。
APP_UPDATE_WRITABLE_FIELDS = (
    "name",
    "state",
    "ip_allowlist",
    "callback_url",
    "callback_state",
)

#: 载荷里出现**不可写**的键时的那句话（**唯一**一处；`{}` 填点名的键清单）。
#: ⚠️ 措辞同时覆盖两种情形（**表外的键**与**本端点不可写的已知键**），理由见模块 docstring 第 1 条。
UNWRITABLE_KEYS_MSG = "这些字段不能通过本接口写入：{}"

#: 请求体**不是对象**时的那句话（数组 / 字符串 / 数字都会走到这里）。
#: ⚠️ 这条**不是防御性写法**：DRF 的 `JSONParser` 只 `json.load` 顶层、**不校验类型**，
#: 而下面的 `.keys()` 会抛 `AttributeError` ⇒ HTML 500（`E-0010`：非法输入一律 `1000`）。
BODY_MUST_BE_OBJECT_MSG = "请求体必须是对象"


def reject_unwritable_keys(data, allowed):
    """请求体里出现**任何**不在 `allowed` 里的键 ⇒ 一句可读的 `1000`（**点名**那些键）。

    ⚠️ 必须在**任何一次写入之前**调用（调用点在视图里紧挨着序列化器）：本函数只做校验、
    ⛔ 自己不开事务——「一行都不落库」这条判据靠的是"校验先于写入"这个顺序
    （先跑序列化器、再进 `transaction.atomic()`）。

    ⚠️ 报成**一句**而不是字段级 dict：错误信封只带 `_first_message` 那一条
    （`apps/core/exceptions.py`），字段级 dict 会让调用方**只看到第一个键**——
    而他真正需要的是"我把哪几个键拼错了"。
    """
    if not isinstance(data, dict):
        raise serializers.ValidationError(BODY_MUST_BE_OBJECT_MSG)
    extra = sorted(set(data.keys()) - set(allowed))
    if extra:
        raise serializers.ValidationError(UNWRITABLE_KEYS_MSG.format("、".join(extra)))


def app_row_payload(app) -> dict:
    """一行打手侧应用的载荷（列表每一行 / 创建 / `PATCH` / 轮换——**唯一**装配出口）。

    ## 键集（**恰好**这 14 个，⛔ 不多不少）

    | 键 | 是什么 |
    |---|---|
    | `id` | 主键（页面拼 `PATCH` / 轮换的 URL 要用它） |
    | `name` | 应用名 |
    | `app_key` | 明文 AppKey（**按设计不是机密**：它要进限流桶的 `ident` 等排障面） |
    | `app_secret` | ⭐ **明文密钥**（`D-0201` 的既有口径：可随时获取；见模块 docstring 第 2 条） |
    | `taker` / `taker_name` | 绑定的打手账号（**主键 + 显示名**）：范围判据的唯一来源，页面要显示"这套凭据是谁的" |
    | `state` / `state_text` | 开关机器值（`enabled`/`disabled`）/ 中文名 |
    | `ip_allowlist` | 每行 / 逗号一个 IP 或网段；空串 = 不限制 |
    | ⭐ `callback_url` / `callback_state` | 回调地址（空串 = 没配回调）/ 回调开关机器值（`enabled`/`disabled`） |
    | `last_used_at` | 最近一次**验签成功**（`null` = 还没用过） |
    | `secret_rotated_at` | 轮换时刻（`null` = 从没轮换过） |
    | `created_at` | 创建时刻 |

    ⚠️ **`taker` 与 `taker_name` 两个都给**（与 `delivery_row_payload` 的 `event`/`event_text`
    同一条口径）：页面按**主键**决定行为（拉账号列表回填选择器）、按**显示名**显示，
    ⛔ 不许拿名字当判据（改个昵称就静默失效）。显示名取 `apps.accounts.display.user_display_name`
    ——那是本仓的**唯一**出处，⛔ 本模块不另写一份"昵称优先、回落账号"。

    ⭐ **T5追加的两键**（第二期 spec §7 第四行）：`callback_url` / `callback_state`
    —— 页面的「回调地址」输入框与「回调开关」要它们才渲染得出来，⛔ 缺了只能显示空，
    而"显示空"与"服务端没配"是两件事。

    ⛔⭐ **不加 `callback_state_text`**（照上家侧 `app_row_payload` 的口径）：上家侧那一格
    给的是**机器值** + 一句注明"页面要显示中文自己查一张两行的表"，而下面对 `state` 又**有**
    `state_text` —— 两者**不是**不一致：`state_text` 是**既有**关键字（第一期就在），
    `callback_state` 是本线**新开**的形状，新形状⛔ 不复制那个旧写法（否则同族两份必然漂）。

    ⛔ **刻意不在载荷里的**：`created_by`（"谁建的"与"这套凭据是谁的"是两件事，
    页面上没有这一栏；`taker` 已经把"它是谁的"说清了）。
    ⚠️ **`taker_name` 会读关联对象**（`app.taker.first_name`）⇒ 列表视图必须
    `select_related("taker")`，否则每个应用多一次查询（判据：列表的查询数不随行数增长，
    与上家侧列表那条同形）。`user_display_name` **自己一次库都不查**，所以预取一层就够。

    时间一律走 `_iso()`（`None` ⇒ `None`，⛔ 不写空串：空串是一个真的时间位置）。
    ⚠️ D-0302：本族载荷是**自有面**（管理端打手应用页 / 推送台账页）消费的
    ⇒ 出**项目时区** `+08:00` 串（页面截串直显）。机器契约例外不含本族（`docs/TAKER-API.md`
    的示例本就是 `+08:00`）。
    """
    return {
        "id": app.pk,
        "name": app.name,
        "app_key": app.app_key,
        # ⭐ 明文密钥（与上家侧 `D-0201` 同口径）：四个出口都走本函数 ⇒ "随时可取"。
        # ⛔ 仍然绝不进留痕 / 日志 / 错误消息。
        "app_secret": app.app_secret,
        "taker": app.taker_id,
        "taker_name": user_display_name(app.taker),
        "state": app.state,
        # ⚠️ 中文名取枚举自身（⛔ 不抄一份 `dict(AppState.choices)`）。
        "state_text": app.get_state_display(),
        "ip_allowlist": app.ip_allowlist,
        # ⭐ 第二期（T5）：回调配置两列。⚠️ `callback_state` 给**机器值**（与上家侧同款）；
        # 页面要显示中文自己查那张两行的表（⛔ 本函数不新开 `callback_state_text`，理由见 docstring）。
        "callback_url": app.callback_url,
        "callback_state": app.callback_state,
        "last_used_at": _iso(app.last_used_at),
        "secret_rotated_at": _iso(app.secret_rotated_at),
        "created_at": _iso(app.created_at),
    }


def _iso(value):
    """可空时间出口：没值 ⇒ `None`（⛔ 不写空串）。

    ⚠️ D-0302：走 `local_iso`（**项目时区** `+08:00`，`datetime_display.py`
    的收口出口）。与 `apps/openapi/serializers.py` 的同名函数**不是**"抄了一份"：那一族是
    上家机器契约（保持 UTC），本族是自有管理面 + TAKER-API 文档明示的 `+08:00` 示例。
    ⛔ 但**生成口**（随机源）与**列宽常量**是共享的——那两样抄第二份才是真的漂移
    （见模块 docstring 第 3 条）。
    """
    return local_iso(value)


class AppCreateSerializer(serializers.Serializer):
    """`POST /api/taker/apps/` 的入参（spec §6 的「能建」那一列）。

    ⚠️ 字段**全部 `required=False`**，**必填与否由身份决定**（那是视图的判据，见
    `TakerAppAdminView._resolve_taker`）：
    - 持 `system.pinduoduo_settings` 的调用方（系统管理员）建打手侧应用时**必须选**一个打手
      ⇒ 缺 `taker` ⇒ `1000` + 点名；
    - 持 `system.allow_taker_open_api` 的调用方（打手本人）**锁死为自己** ⇒ `taker`
      **不参与解析**（载荷里写了别人的 id 也**不会**被采纳，判据在
      `tests/test_takerapi_app_api.py::TakerIdentityScopingTests`）。

    ⇒ 序列化器这一层**只判形状**（是不是整数、名字长度、IP 白名单上限），
    "必填"这条**业务**判据留在视图里：写在这里会让两个身份分支各需要一个序列化器，
    而它们**共用同一份白名单**（那正是 `reject_unwritable_keys` 的入参）。

    ⚠️ `taker` 用 `IntegerField`（**不是** `PrimaryKeyRelatedField`）：后者会**当场查库**
    并在不存在时抛自己的英文文案，而本线要求"缺 / 非法 / 不是本租户的账号"回**同一句**点名
    人话（`TAKER_NOT_FOUND_MSG`）；查库那一步也在视图里（它还要按身份分支决定**要不要**查）。
    ⛔ 请求来源的值进类型化的列 / 查询之前一律过序列化器（`AGENTS.md` §5-E）：这里的
    `IntegerField` 就是那道界——非整数的 `taker` ⇒ `1000`，**不会**进 `filter(pk=…)`。
    """

    name = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=APP_NAME_MAX_LENGTH,
        error_messages={"required": "缺少参数：name", "blank": "应用名不能为空"},
    )
    taker = serializers.IntegerField(required=False)
    ip_allowlist = serializers.CharField(
        required=False, allow_blank=True, max_length=IP_ALLOWLIST_MAX_LENGTH
    )
    # ⭐ T5：回调配置两格（第二期 spec §7 后端面）。
    # ⚠️ `max_length` 与**模型列宽**（`TakerApp.callback_url` = 200）同源：本模块
    # ⛔ 不自己写一个数（两处各写一个数就会出现"页面让填、库里静默截断"）⇒ 取**共享**常量
    # `apps/openapi/serializers.CALLBACK_URL_MAX_LENGTH`（上家侧那个 200 与上家侧的列宽同源；
    # 打手侧这一列**逐字同宽**，见 `apps/takerapi/models.py::TakerApp.callback_url` 的注释）。
    callback_url = serializers.CharField(
        required=False, allow_blank=True, max_length=CALLBACK_URL_MAX_LENGTH,
        validators=[validate_callback_url_value],
    )
    callback_state = serializers.ChoiceField(choices=CallbackState.choices, required=False)


class AppUpdateSerializer(serializers.Serializer):
    """`PATCH /api/taker/apps/<pk>/` 的入参：**改名 / 停用启用 / 改 IP 白名单**
    ⭐ + T5：**改回调地址 / 回调开关**（第二期 spec §7 第一行）。

    ⚠️ 字段**全部 `required=False`**：缺席 = **保持库里的值**（PATCH 语义，与全仓口径一致）。
    ⛔ 调用方的判据必须是 `"字段" in validated_data`（**不是**真值判断）：
    空串 `ip_allowlist` 是**合法值**（空 = 不限制）⇒ 用真值判断会把"清空白名单"当成"没传"
    而**静默不生效**（`E-0021`）。⭐ 空串 `callback_url` 同理**合法**（清掉回调 ⇒ 不再产生投递）。

    ## ⛔⭐ 为什么 `taker` **不可改**（原句「你来定」的落点，判据在用例里）

    `taker` 是「这个应用能看什么、能做什么」的**唯一**依据（spec §3.1：范围从那一行算，
    本表**没有**第二份授权存储）。允许 `PATCH` 改它 ⇒ 在**不换凭据**的前提下换掉整个权限面：

    1. **对第三方是静默的**：外部工作室手里那把 `app_key` / `app_secret` 一个字都没变，
       而它背后能看到的游戏、能抢的单、能改的状态**整套换了人**。对方不会收到任何信号，
       也不会重新拉一次配置——这是本仓反复要挡的「静默变权」形状（`E-0027` / `E-0010` 同族）。
    2. **对打手调用方是一条提权路**：他若能改 `taker`，就能把自己那套凭据指到**别人**名下
       ⇒ 拿到别人名下的单（spec §6 那一栏写的是"打手⛔ 看不到、改不了**别人的**"）。
       身份限权（`_resolve_taker`）只在**创建**那一层拦，改的那一层不拦就等于没拦。
    3. **替代路径是明确的、而且更安全**：要换人 ⇒ **停用旧的 + 建一套新的**
       （旧凭据当场失效、新的当场生效，spec §6 的"秒级生效、无宽限期"）。
       这条路上"换人"是**看得见**的两个动作，留痕里也是两条。

    ⇒ 本端点的白名单里**没有** `taker`（`APP_UPDATE_WRITABLE_FIELDS`）⇒ 载荷里带它
    会被 `reject_unwritable_keys` 当场点名拒绝（`1000`），⛔ **不是静默忽略**。
    ⚠️ **可证伪的判据**（`tests/test_takerapi_app_api.py`）：① 带 `taker` 的 `PATCH`
    ⇒ `1000` 且点名 `taker`、库里那一行的 `taker_id` **一个字没变**；
    ② 对照：只带 `name` 的 `PATCH` ⇒ 真的改名（⛔ 不是"什么都拒"）。

    ⚠️ `state` 的取值走 `AppState`（打手侧**自己**那个枚举，⛔ 不是 `ApiAppState`：
    两族各自一份，值今天逐字相同、住在两个模型类里）。⭐ `callback_state` 同理走
    打手侧**自己**的 `CallbackState`（⛔ 不是 `apps.openapi.models.CallbackState`：
    今天两个枚举成员名与取值逐字相同，而它们住在**两个模型类**里 ⇒ 引擎按 `spec` 里的
    **字符串**比较、枚举各归各家，见 `apps/takerapi/models.py::CallbackState` 的 docstring）。

    ## ⭐ T5 追加的两格：校验**只有一份实现**

    `callback_url` 的判据（仅 https + 解析出的每个 IP 必须公网 + 解析不出就拒 + 不许带
    URL 内凭据）**逐条长在** `apps/openapi/outbound.py::validate_callback_url` 里，
    这里只是**转发**（`validate_callback_url_value`），**保存时**与**每次投递前**用的是
    **同一个函数**（第二期 spec §8.4）。⛔ **不许在这里再写一份 http / 内网判断**：
    两份判据必然漂移，而漂移的那一侧（投递时放松）是一条**真的 SSRF 通道**。
    """

    name = serializers.CharField(
        required=False,
        allow_blank=False,
        max_length=APP_NAME_MAX_LENGTH,
        error_messages={"blank": "应用名不能为空"},
    )
    state = serializers.ChoiceField(choices=AppState.choices, required=False)
    ip_allowlist = serializers.CharField(
        required=False, allow_blank=True, max_length=IP_ALLOWLIST_MAX_LENGTH
    )
    # ⭐ T5：回调地址（**仅 https + 公网 + 不许带凭据**，与投递前**同一个**校验函数）
    # 与回调开关（打手侧自己的枚举）。⚠️ 空串合法（= 清掉回调 ⇒ 不再产生投递）。
    callback_url = serializers.CharField(
        required=False, allow_blank=True, max_length=CALLBACK_URL_MAX_LENGTH,
        validators=[validate_callback_url_value],
    )
    callback_state = serializers.ChoiceField(choices=CallbackState.choices, required=False)


# ═════════════════════════════════════════════════════════════════════════════
# 推送台账（`TakerApiDelivery`）—— 两个内部端点（T5）的载荷与筛选
# ═════════════════════════════════════════════════════════════════════════════


def delivery_row_payload(delivery) -> dict:
    """打手侧推送台账**一行**的载荷（列表每一行 / 重投响应）——本函数的键集就是那份契约。

    ## 键集（**恰好**这 13 个，⛔ 不多不少）

    与上家侧 `apps/openapi/serializers.py::delivery_row_payload` 的键集**逐字相同**
    （`id` / `event` / `event_text` / `order_no` / `source_order_no` / `status` /
    `status_text` / `attempts` / `reason` / `response_status` / `next_attempt_at` /
    `created_at` / `processed_at`）—— 第二期 spec §7 第四行逐字要求「台账行 13 键照上家侧那张」。
    ⚠️ 两个载荷函数**各有一份**（⛔ 不合并成一个）：两条通道的台账行是**两张表**
    （`TakerApiDelivery` ↔ `OpenApiDelivery`），合并会让"一族的载荷改了、另一族没改"
    这件事只在**其中一族的页面上**看得见（而本仓的形状纪律是"两族各自演进、共享件才共享"）。

    ## 逐键语义（与上家侧同款，⛔ 不重复造词）

    | 键 | 是什么 |
    |---|---|
    | `id` | 台账行主键（重投按钮要拿它拼 URL） |
    | `event` / `event_text` | 事件**机器值**（`order.status_changed` 等四项）/ 中文名 |
    | `order_no` / `source_order_no` | 我方单号 / 对端的来源单号（可为空串） |
    | `status` / `status_text` | 状态**机器值**（`pending`/`done`/`failed`）/ 中文名 |
    | `attempts` | 已尝试投递次数 |
    | `reason` | **上一次失败的诊断文本**（过共享的 `delivery_reason_text`，只给页面看） |
    | `response_status` | 对方最近一次的 HTTP 回执码（`null` = 还没拿到回执） |
    | `next_attempt_at` | 下一次可投递时刻（`null` = 立刻可投 / 已终态） |
    | `created_at` / `processed_at` | 入队时刻 / 上一次落终态的时刻（`null` = 还没落过） |

    ⚠️ `event`/`status` 给机器值、`*_text` 给中文，**两个都要**：页面按机器值决定按钮可不可点
    与筛选（⛔ 不许拿中文当判据 —— 改一句文案就会静默失效），显示用中文（页面不做映射表）。

    ## 两处**共享**的上家侧实现（⛔ 不是"在打手侧抄了第二份判据"）

    1. `reason` 的**展示截断**走 `apps/openapi/serializers.py::delivery_reason_text`
       （页面口径 200 字符 + `…` 标记的**唯一**出处）。**直接 import 那一个函数**，
       ⛔ 不在本模块再写一遍 `[:200]` —— 那样"页面看到多长"会变成两份可以各自演进的数；
       而"库里的原文一个字都不动"这条纪律也要求只有**一个**展示出口的实现。
       ⚠️ 这是本任务书「⛔ 不许在 `apps/takerapi/**` 抄第二份」直接管到的地方：
       判据（截断行为）在那里、这里只是调用。
    2. 时间出口走本模块的 `_iso()`（与上家侧那个同名函数**不是**抄：两族各自的载荷装配
       各有一份，且都只有三行；见 `_iso` 自己的 docstring）。

    ## ⛔ 刻意**不在**载荷里的两样（各有理由，⛔ 别"顺手带上"）

    1. **`payload`**（待投递的报文原文）：里面装的是**订单业务数据** ⇒ 带上它等于把订单数据
       复制一份到管理页的响应里（这一页要回答的只是"推没推成"）。
    2. **任何凭据类字段**（`app_secret` / `app_key` / 签名）：本行是**台账**、与凭据无关；
       打手侧密钥的展示出口今天只有一个（`app_row_payload`），⛔ 本函数不开第二个
       （判据 = `tests/test_takerapi_callback_api.py` 里逐条递归扫键 + 全响应文本扫密钥）。
    """
    return {
        "id": delivery.pk,
        "event": delivery.event,
        # 中文名取枚举自身（⛔ 不抄一份 `dict(Event.choices)`）
        "event_text": delivery.get_event_display(),
        "order_no": delivery.order_no,
        "source_order_no": delivery.source_order_no,
        "status": delivery.status,
        "status_text": delivery.get_status_display(),
        "attempts": delivery.attempts,
        "reason": delivery_reason_text(delivery.reason),
        "response_status": delivery.response_status,
        "next_attempt_at": _iso(delivery.next_attempt_at),
        "created_at": _iso(delivery.created_at),
        "processed_at": _iso(delivery.processed_at),
    }


class DeliveryStatusFilterSerializer(serializers.Serializer):
    """推送台账列表的 `status` 筛选入参（打手侧这一族的**唯一**校验口）。

    ⚠️ **请求来源的值一律过序列化器**（`AGENTS.md` §5-E / E-0010）：`?status=…` 直接进
    `filter(status=…)` 今天不会炸，但那是"碰巧"（列的比较行为），不是判据。这里把它钉成
    **枚举**：非法值 ⇒ `1000` + 一句带字段名的人话（⛔ 不是静默忽略 —— 静默忽略会让页面
    以为"筛了"，而列表里其实混着别的状态）。

    ⚠️ `required=False`：**不传 = 不筛**。
    ⚠️⭐ **空串（`?status=`）也等于"不筛"**（**实测**，⛔ 不是本端点的选择）：DRF 的
    `Field.get_value` 对 HTML 形状的输入（`QueryDict` 就是）有一支
    `ret == '' and not self.required ⇒ empty`（那个 `empty` 是"缺席"哨兵）⇒ **全仓**每一个
    非必填字段上的空串都是这个语义。⛔ 本端点**不特判**它 —— 特判会让"空串"这一种输入
    在这里与其余每一个可选字段上分叉（先例 = `apps/openapi/serializers.py` 的同一个类）。
    """

    status = serializers.ChoiceField(
        # ⚠️ 取**打手侧自己**那张表的 `Status`（⛔ 不是 `OpenApiDelivery.Status`：
        # 两族各一张台账表、各一份枚举，今天取值逐字相同而住在两个模型类里）。
        choices=TakerApiDelivery.Status.choices,
        required=False,
        error_messages={"invalid_choice": "状态筛选值不合法：{input}"},
    )
