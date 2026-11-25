"""「我的资料 → 功能设置」里的**代练员个人绑定**端点（下游鸡蛋铺线 Task 3 / Task 8）。 | 路径 | 方法 | 干什么 | |---|---|---| | `/api/business/member/jidanpu-binding/` | GET | 读**本人那一行** + 健康状态（⛔ 不回显密码 / `client_secret`）+ ②式的回调地址 + 那句消费健康的人话 | | 同一路径 | PUT | 新建 / 覆盖**本人那一行**（写库 + 写 0600 回调密钥文件 + 写操作日志**同一个事务**） | | `/api/business/member/jidanpu-binding/test-connection/` | POST | **按对接方式分流**跑检测、把人话结论回给页面（⚠️ **只写一格引擎列**，且只在①式那一支：拿到登录显示名就把它落回 `account_name`，见下面那条；⛔ 不写 `checked_at` / `last_error`）。⚠️ **2026-09-30（D-0177）起读两个可选请求体键**：`password`（页面上此刻填的，只用于本次建连、⛔ 不落库、⛔ 不回显）与 `source_name`（下拉里此刻选中的那个）；两者**只对①式有意义** | | `/api/business/member/jidanpu-sync-now/` | POST | 对自己那一行跑一轮同步（spec §9.3 的「立即同步」）；⭐ **按对接方式分流**：①式跑状态回拉、②式跑**回调消费 + 兜底对账** | | `/api/business/member/jidanpu-push-now/` | POST | ⭐ **手动推送**（计划 §2）：「指派给我的订单」勾选的行 ⇒ 录单（`record`）或把我方状态推过去（`status`）；只作用于 `request.user` 那一行绑定、逐单按 `order_scope.claimed_perf_of` 判归属、推送走既有 outbox 引擎 | ## ⭐ 两种对接方式（`mode`）在**这一个入口**上怎么分流（官方 API 对接线，下家线的 HTTP 面） `mode` 是「用哪套凭证」的**唯一**判据（官方 API spec §2.1）：⛔ **不许**用 `client_id` 是不是非空去反推（E-0021 家族：切回①式之后 `client_id` 还在，那句会**静默走官方 API**）。形状与**上家线**（`jidanpu_upstream_views.py`，已上线）逐条同形，⛔ 不另发明一套： | 落点 | ①式（`password`，默认） | ②式（`openapi`） | |---|---|---| | 写（PUT）必填 | 地址 + 账号 + 密码（既有，一字未变） | 地址 + `client_id` + `client_secret`（`client_secret` 只写不读、空串 = 不改） | | 「凭据全不全」 | `jidanpu_member.credentials_missing`（既有） | `jidanpu_openapi.openapi_credentials_of`（⛔ 不在这里重写判空） | | 「测试连接」/ 启用前检测 | `jidanpu_member.detect_binding_with_login_identity`（**逐字不变**） | `jidanpu_openapi.probe_read_endpoints`（**逐条只读探测** + 一张表） | | 检测不过 | 不许启用（既有） | 不许启用，**只看 `token` + `GetOrderStatus` 那两跳**（spec §25.2；⚠️ 该节优先于 §11.4 末段）——⛔ 一行都不落 | | 「立即同步」 | `jidanpu_member.pull_statuses`（**逐字不变**） | `jidanpu_member_openapi.consume_member_callbacks` + `reconcile_member_statuses`（既有入口，⛔ 不写第二份） | | 保存时写 0600 密钥文件 | ⛔ 不写（`callback_key_of` 判据里就回 `None`） | ✅ 写（`jidanpu_openapi.ensure_callback_key`；写不进 ⇒ **整个保存回滚**） | ⚠️ ①式那一支的**行为、文案、状态码、键集合一个字都没变**（回归判据在
`tests/test_jidanpu_member_api.py` / `tests/test_jidanpu_member_status.py`）。 ## ⛔ `callback_watermark_at` 不上页面（spec §4.2 / §13.3-C8）那两列是**内部状态**（消费者的水位），判据已由后端算成**一句人话**（`member_consumer_state` ⇒ 响应里的 `consumer_state` 那一格）⇒ 它⛔ **不进序列化器、⛔ 不出现在任何响应里** （照本表 `status_pull_at` 的既有先例）。 ## 越权面 = 0（spec §8.5 / §9.2 的唯一入口）四条端点**只作用于 `request.user` 那一行**：查询一律 `filter(user=request.user)`，序列化器里**没有** `user` 字段，⛔ 不接受任何请求来源的用户 / 租户参数（`sync-now` 连请求体都不读）。代练员 A 的绑定，B 既读不到也改不到——判据打在**数据库行**上（`tests/test_jidanpu_member_api.py` / `tests/test_jidanpu_member_status.py`）。 ## ⛔ 几条硬约束 - **密码只写不读**：序列化器里 `write_only`，响应里连 `password` 这个**键**都不出现；操作日志的句子只说**站点地址**（去协议头），⛔ 不说账号、更不说密码（spec §8-S1）。 - **检测不过 ⇒ 不许启用**（spec §9.1 / A11）：`enabled=True` 时**先**同步跑一次 `jidanpu_member.detect_binding_with_login_identity`（检测 + 同一条连解析登录显示名），不过就**不落库**并把缺项逐条点名；过了才落库 + 写 `checked_at`。 - **`atomic()` 块内不许 `return`**（E-0022）：可预期的失败（检测不过 / 配置不全）一律在**进事务之前**返回——那时一行都还没写，不需要 `set_rollback`。 - **换绑处置**（spec §8.4）：改了 `account` 或 `jidanpu_base_url` ⇒ 把在飞的台账事件（`pending` / `failed` 且 `payload.member_binding_id` 指向这一行）收敛成 **`skipped`**
（作废终态：⛔ 不再重试、⛔ 不挡同键的新触发）+ 人话原因；本线的链路行**保留**（它是历史事实）， ⛔ 绝不拿新凭据去改旧单。⚠️ ⛔ **不许收敛成 `failed`**：那是「还会重试」的态（控制器裁定 7），「停用 → 再启用」会用**新凭据**重试旧事件，正是 §8.4 禁止的事。 - **测试连接只写「登录显示名」那一格**（2026-09-29 本线改准；原句写「只读、不写任何业务数据」，现已被实现推翻）：它探上游 + 把**登录账号的显示名**写回 `account_name`（与每轮 ⓪ 自愈同一把尺子， `D-0170`）；⛔ 不写 `checked_at` / `last_error`（那两列的写入方仍是 PUT 的启用那一刻与后续期的同步引擎）。 - **`sync-now` 内部走的是慢路那段代码本身**（`jidanpu_member.pull_statuses`，spec §9.3 明文要求 ⛔ 不写第二份）；它**会真的改我方订单状态**（由 `apply_transition` + 一条 `order.transition` 审计留痕）。 - ⚠️ **本线两张表与已上线那条租户级线零关系**（spec §4.1）：这里读写的只有 `JidanpuMemberBinding`，⛔ 不碰 `JidanpuBinding` / `JidanpuOrderLink`。 ⛔ **改准（上面那一条是历史原文）**：它点名的那两张**租户级**表已随租户级鸡蛋铺线整条拆除删除 ⇒「⛔ 不碰」今天没有对象了（历史说明）；本视图的读写面、权限门与断言一个字未改。
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.accounts.display import user_display_name
from apps.accounts.serializers import INT4_MAX
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import (
    jidanpu_assignment_lock,
    jidanpu_member,
    jidanpu_member_openapi,
    jidanpu_member_openapi_outbox,
    jidanpu_openapi,
)

#: 上游异常的「异常类型 → 人话 + 业务码」翻译**复用**既有实现（现住在中性宿主 `jidanpu_view_common`——⛔ 原宿主 `jidanpu_views` 已随租户级那条线整份删除）：
#: 它把 `CaptchaRequiredError` / `SessionExpiredError` 的判据排在同为 `AuthError` 子类的父类**前面**（顺序错了「请输验证码」会被说成「账号或密码错误」）——⛔ 不在这里再抄一份。
from apps.business.api.jidanpu_view_common import _upstream_error, _with_password
from apps.business.models import (
    SOURCE_NAME_DEFAULT,
    JidanpuMemberBinding,
    JidanpuMemberOrderLink,
    JidanpuSyncEvent,
)
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node
#: ⭐ 「他当前接手、未软删」的**既有唯一判据**（spec §5.3-2）：手动推送逐单要过它。
#: ⛔ 不在本模块另写一份 `Performance.objects.filter(...)`——那是第二套判据（E-0202 家族），
#: 而且会多出一个 `order__` join 读点（`tests/test_order_read_path_net.py` 要跟着登记）。
from apps.takerapi.api.order_scope import claimed_perf_of
from jidanpu.errors import JidanpuError

#: 操作日志的 `target_kind`（⛔ ≤16 字符，超了会被 `record()` 静默截断）。
#: ⚠️ 与租户级那条线的 `jidanpu_binding` **刻意不同名**：两张表零关系，日志里也要分得清。
JIDANPU_MEMBER_BINDING_TARGET_KIND = "jidanpu_member"

#: 换绑时给在飞事件写的人话原因（spec §8.4：⛔ 不静默丢弃，要有人能看懂发生了什么）。
REBINDED_EVENT_REASON = jidanpu_member.REBINDED_EVENT_REASON

#: 在飞（还会被重试 / 还会产出动作）的事件状态：只有这两档需要收敛。
#: ⚠️ 收敛的**终态**是 `skipped`（作废、不再重试），⛔ 不是 `failed`（那会继续重试）——
#: 控制器裁定 7；`skipped` 也不在 `run_outbox` 的取活集合与 `queue_member_event` 的去重集合里。
_IN_FLIGHT_STATUSES = (JidanpuSyncEvent.Status.PENDING, JidanpuSyncEvent.Status.FAILED)

#: 凭据不全时的人话（与租户级 `test-connection` 的「还没配置」同一口径）。
NOT_CONFIGURED_MESSAGE = "还没配置鸡蛋铺的地址 / 账号 / 密码，无法启用"

#: 「地址 + 账号」已被**别的**账号占用时的人话（业务方原话要点）。 ⚠️ 用 f-string 拼接（⛔ 不用 `str.format`）：占位的是**用户名**这种自由文本，    里面带 `{}` 时 `format` 会抛 `KeyError`（把一句提示变成 500）——照
#:    `jidanpu_member.source_missing_message` 的同一条口径。 ⚠️ 为什么要在这里先查再报而不是等数据库抛唯一约束：数据库那句是    「字段 jidanpu_base_url, account 必须能构成唯一集合。」——既不中文、也不说是谁占的，
#:    用户下一步该做什么（解除绑定）完全看不出来。 ⛔ **数据库那道唯一约束不放宽**：它仍是并发下的最后一道闸（两个人同时提交同一对值时，    唯一可能会撞 `IntegrityError`；那是并发，不是这里能收敛的形态）。
def binding_conflict_message(name: str) -> str:
    return f"该鸡蛋铺账号已被 {name} 绑定，如需换绑请先解除绑定"


#: PUT **更新**那一支能写的列：凭据 / 配置 / 开关 / `checked_at`，外加 `updated_at` （`auto_now` 只在它进 `update_fields` 时才会被刷新）。 ⛔ `input_order_kefu`（下家录入客服 uid）**不在**这份清单里：它是同步引擎**从上游读回来**的缓存列，不是用户配置 —— 放进来的话①请求体可以伪造它②整表覆盖会把并发写进去的值打回旧值。它只在**换绑**那一档被显式清空（见 `put` 里那个 `update_fields` 分支）。 ⛔ 四个**健康列**（`last_run_at` / `last_ok_at` / `last_error` / `last_error_at`）不在其中：
#: 它们是同步引擎的写入方，整行 `save()` 会把并发写进去的值覆盖回这次请求开始时的旧值（T3 M3 的 lost update）。⛔ 别把这份清单放宽回整行 `save()`。 ⭐ ②式那三格（`mode` / `client_id` / `client_secret`）**必须在**：漏了它们 ⇒ 序列化器校验通过、 **库里那一格却不会被写**（本仓最恨的那种静默形态；形状照上家线 `_PUT_WRITE_FIELDS`）。 ⚠️ `mode` 尤其承重：它是「用哪套凭证」的唯一判据 ⇒ ②→① / ①→② 切换时**必须**靠它落库。
_PUT_WRITE_FIELDS = (
    "jidanpu_base_url", "account", "account_name", "password", "source_name",
    "enabled", "fast_poll_seconds",
    #: ⭐ 状态回拉的间隔（拆节奏：与 `reconcile_seconds` 各管一段）。
    "status_pull_seconds", "reconcile_seconds",
    #: 抛单百分比（业务方）：大厅镜像那一档的抛单价 = 我方发单价 × 本值 / 100。
    "throw_price_percent",
    #: ⭐「同步大厅订单」开关（D-0299，业务方裁定）：只闸「抛大厅」那两条路，
    #: ⛔ 不影响 assign / status / withdraw / unlist / price / pics（缺省 True＝存量行为不变）。
    "hall_sync_enabled",
    #: ⭐ ②式（官方 OpenAPI）的三格（官方 API 对接线；spec §2.1 / §4.2）。
    "mode", "client_id", "client_secret",
    "checked_at", "updated_at",
)

#: 「空串 = 不修改」的三格（照 `password` 的既有口径，spec §11.2）。理由：这几格在页面上**不回显**（`password` / `client_secret` 只写不读），留空只能表示
#: 「不动它」；若把空串当成「清掉」，一次没带值的 PUT 就会**静默抹掉**用户配好的凭据（E-0027 家族：抹掉密钥之后回调**必然验签失败**，而页面与库里一切正常）。
_BLANK_MEANS_KEEP = frozenset({"password", "client_id", "client_secret"})

#: 凭据不全时的人话（**按方式分成两句**，spec §2.1：两套凭据各自独立）。
#: ①式缺的是地址 / 账号 / 密码；②式缺的是地址 / client_id / client_secret
#: ⇒ 说错一句会让用户去补一格他根本不需要的东西。
NOT_CONFIGURED_OPENAPI_MESSAGE = (
    "还没配置下家鸡蛋铺的地址 / client_id / client_secret，无法启用"
)

#: ②式必填的两句（spec §11.5 **逐字**，与上家线同一份文案）。
CLIENT_ID_REQUIRED_MESSAGE = "请填鸡蛋铺给你的 client_id"
CLIENT_SECRET_REQUIRED_MESSAGE = (
    "请填鸡蛋铺给你的 client_secret（只在保存时填一次，之后不会再显示）"
)


def _is_openapi(binding) -> bool:
    """这一行走不走②式。⚠️ 判据**只有** `mode`（spec §2.1），且判据的唯一出处是
    `jidanpu_openapi.is_openapi_binding`（⛔ 本模块不重写一遍，也⛔ 不用 `client_id` 真值反推）。
    """
    return jidanpu_openapi.is_openapi_binding(binding)


def _not_configured_message(binding) -> str:
    """「凭据不全」那一句人话（**按方式**说清缺哪一套，spec §2.1）。"""
    return NOT_CONFIGURED_OPENAPI_MESSAGE if _is_openapi(binding) else NOT_CONFIGURED_MESSAGE


def _member_binding_name(binding) -> str:
    """审计句子里显示的名字：**站点地址**（去协议头）。

    ⛔ 不用账号（它是登录名、属于凭据标识），⛔ 更不用密码（spec §8-S1 红线）。
    地址为空时回落到 `#<主键>`（配置校验要求它非空，这条只为「shell 写库」那种路径兜底）。
    """
    host = binding.jidanpu_base_url or ""
    for prefix in ("https://", "http://"):
        host = host.removeprefix(prefix)
    return host.rstrip("/") or f"#{binding.pk}"


class JidanpuMemberBindingSerializer(serializers.ModelSerializer):
    """代练员个人绑定（一人一份）。密码 / `client_secret` **只写不读**；留空 = 不改。 ⚠️ **没有 `user` 字段**是承重的：它不在 `fields` 里 ⇒ 请求体里塞 `user` 会被 DRF 静默忽略，行主只能由视图从 `request.user` 传进 `pending_instance()`。 ## ⭐ ②式（官方 OpenAPI）那几格（官方 API 对接线；spec §2.1 / §4.2 / §11.2 / §13.3-C3） | 字段 | 读 / 写 | 语义 | |---|---|---|
    | `mode` | 可读可写 | ⭐ **「用哪套凭证」的唯一判据**（`password` 默认 / `openapi`）；⛔ 不用别的列反推 | | `client_id` | 可读可写 | 身份、不是密钥（页面上要回显）⇒ 可读。**空串 = 不修改** | | `client_secret` | ⭐ **只写** | 响应里连**键**都不出现（只回 `client_secret_set` 布尔）；**空串 = 不修改** | | `callback_url` / `callback_url_reason` | 只读 | ②式的回调地址 + 算不出来时的**原因**（spec §11.3 / C10）。⛔ 由后端算 | | `consumer_state` | 只读 | ⭐ §9.4 第 2 问那句**人话** + `bad`（`{text, bad}` 两格，形状照上家线 `ROW_KEYS`）；⛔ `callback_watermark_at` 不上页面 | ⚠️ **②式的两格必填**判据在 `validate()`（spec §4.3 的不变式 + §11.5 的逐字文案），
    判的是「**这次请求之后**那一格是什么」而不是「这次带没带」——`client_secret` 只写不读，库里已有的那一把没法重输（spec §11.2 的「更新时允许空」）。 ⚠️ **两套凭据允许同时留着**（切来切去不用重输，spec §2.1）⇒ 切 `mode` ⛔ 不清另一套的值。
    """

    #: 密码的**只读替身**：只回答「配没配」，不回答「配的是什么」。
    password_set = serializers.SerializerMethodField()
    #: `client_secret` 的只读替身（与 `password_set` **同一口径**，spec §13.3-C3）。
    client_secret_set = serializers.SerializerMethodField()
    password = serializers.CharField(
        max_length=255, write_only=True, required=False, allow_blank=True,
        help_text="下家鸡蛋铺账号的密码（只写不读；留空 = 不改）",
    )
    #: 对接方式（spec §2.1）。⚠️ 用 `ChoiceField` 显式声明（而不是靠模型自动生成）：
    #: 值不合法时要给**中文**文案（DRF 默认那句英文会直接显示在页面上）。
    mode = serializers.ChoiceField(
        choices=JidanpuMemberBinding.Mode.choices, required=False,
        error_messages={"invalid_choice": "对接方式只能是「账号 + 密码」或「官方开放平台 API」"},
        help_text="用哪套凭据（唯一判据）：password = 账号 + 密码；openapi = 官方开放平台 API",
    )
    #: 官方 API 的 `client_id`（身份、可回显）。**空串 = 不修改**（见 `_BLANK_MEANS_KEEP`）。
    client_id = serializers.CharField(
        max_length=150, required=False, allow_blank=True,
        help_text="官方API 的 client_id（可读；留空 = 不改）",
    )
    #: ⭐ **只写不读**（spec §4.2 / §13.3-C3）：响应里连**键**都不出现。
    #: ⚠️ 这一列同时是**回调验签**用的那把密钥 ⇒ ⛔ 不进日志、⛔ 不进异常文本。
    client_secret = serializers.CharField(
        max_length=255, write_only=True, required=False, allow_blank=True,
        help_text="官方API 的 client_secret（只写不读；留空 = 不改）",
    )
    #: ⭐ ②式的回调地址（spec §11.3）：**由后端算好**回给页面 ⇒ ⛔ 前端不拼域名
    #: （域名拼接规则一变，前端那份必然漂移）。①式 ⇒ 空串（+ 原因）。
    callback_url = serializers.SerializerMethodField()
    #: 算不出地址时的**人话原因**（spec §13.3-C10）：⛔ 静默回空串 = 本仓最恨的静默形态。
    callback_url_reason = serializers.SerializerMethodField()
    #: ⭐ §9.4 第 2 问（「这台机器有没有人在消费这条绑定的回调」）的**一句人话** + `bad`。
    #: 判据的唯一出处是 `jidanpu_member_openapi.member_consumer_state`（本模块只搬运结论）。
    consumer_state = serializers.SerializerMethodField()
    #: 三个字符列显式声明 `allow_blank=True`，好让**本模块的**校验器（下面几个
    #: `validate_*`）给出点名到字段的中文文案；否则空串会被 DRF 的 `allow_blank=False`
    #: 挡在字段层，用户看到的是一句通用的「该字段不能为空」。
    jidanpu_base_url = serializers.CharField(max_length=255, allow_blank=True)
    account = serializers.CharField(max_length=150, allow_blank=True)
    source_name = serializers.CharField(max_length=64, required=False, allow_blank=True)
    #: 两个轮询间隔：显式声明的 `IntegerField` **不继承**模型字段的 `MaxValueValidator`
    #: ⇒ 漏了上界会让越界值落到 PostgreSQL `int4` 列上抛 `DataError` → 500（E-0010）。
    #: 下界 1：0 秒的轮询间隔没有意义（与模型默认值 3 / 180 同口径）。
    fast_poll_seconds = serializers.IntegerField(
        min_value=1, max_value=INT4_MAX, required=False,
    )
    #: ⭐ **状态回拉**的间隔（秒；拆节奏）。与 `reconcile_seconds` 同一条口径
    #: （显式声明 ⇒ 上下界必须自己给：模型字段的 `MaxValueValidator` 不会被继承，
    #: 越界值落到 PostgreSQL `int4` 上会 `DataError` → 500，E-0010）。
    status_pull_seconds = serializers.IntegerField(
        min_value=1, max_value=INT4_MAX, required=False,
    )
    reconcile_seconds = serializers.IntegerField(
        min_value=1, max_value=INT4_MAX, required=False,
    )
    #: 抛单百分比：**范围与模型校验器同值（1–100）**，序列化器这一层给的是**带字段名的中文文案**
    #: （DRF 默认文案不带字段语义，用户看不出来是哪一格错了）。
    throw_price_percent = serializers.IntegerField(
        min_value=1, max_value=100, required=False,
        error_messages={
            "min_value": "抛单百分比必须不小于 1（0 会把一张免费单真送进对方大厅）",
            "max_value": "抛单百分比不能大于 100",
            "invalid": "抛单百分比必须是 1–100 的整数",
        },
    )

    class Meta:
        model = JidanpuMemberBinding
        #: ⚠️ **不是** `__all__`：这张表的**内部状态列**（`callback_watermark_at` / `status_pull_at` / `log_cursor` 那几格）⛔ 不进序列化器、⛔ 不上页面
        #: （spec §4.2 / §13.3-C8，照 `status_pull_at` 的既有先例）； `callback_code` 也**不单独回**（地址里已经有它，spec §11.3：同一事实不在两处）。
        fields = [
            "id", "mode", "jidanpu_base_url", "account", "account_name", "password",
            "password_set", "client_id", "client_secret", "client_secret_set",
            "callback_url", "callback_url_reason", "consumer_state", "source_name",
            "enabled", "fast_poll_seconds", "status_pull_seconds", "reconcile_seconds",
            "throw_price_percent",
            "hall_sync_enabled",
            #: ⭐「同步大厅订单」开关（D-0299）：GET 回显、PUT 可写；**缺省 True**
            #: （模型默认 ＝ 存量行为不变；PUT 不带键 = 保持库里原值，DRF 对有默认值的列免必填）。
            #: ⭐ 从上游读回来的「录入客服 uid」缓存（只读；运维用它确认那一格到底填没填上）。
            "input_order_kefu",
            "last_run_at", "last_ok_at", "last_error", "last_error_at", "checked_at",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "account_name", "input_order_kefu", "last_run_at", "last_ok_at",
            "last_error", "last_error_at", "checked_at", "created_at", "updated_at",
        ]

    def get_password_set(self, obj) -> bool:
        return bool(obj.password)

    def get_client_secret_set(self, obj) -> bool:
        """只回答「配没配」（⛔ 不回答「配的是什么」，spec §13.3-C3）。"""
        return bool(obj.client_secret)

    def _address_of(self, obj):
        """这一行的 `CallbackAddress`（**一次请求只算一次**：`callback_url` 与
        `callback_url_reason` 两个键共用它，否则每渲染一次要多查一次域名表）。"""
        cached = getattr(self, "_address_cache", None)
        if cached is None or cached[0] is not obj:
            cached = (obj, jidanpu_openapi.callback_address(obj))
            self._address_cache = cached
        return cached[1]

    def get_callback_url(self, obj) -> str:
        """②式的回调地址（spec §11.3）：算不出来 ⇒ **空串**（原因在 `callback_url_reason`）。"""
        return self._address_of(obj).url

    def get_callback_url_reason(self, obj) -> str:
        """算不出地址时的**人话原因**（spec §13.3-C10）：⛔ 不给半截地址、⛔ 不静默回空串。"""
        address = self._address_of(obj)
        return "" if address.url else address.reason

    def get_consumer_state(self, obj) -> dict:
        """⭐ §9.4 第 2 问那句人话（`{text, bad}`）。 ⚠️ **只有两格**：`ConsumerState` 里那两个内部读数（`last_at` / `watermark_at`）
        ⛔ 一个都不进响应 —— 后者（= `callback_watermark_at`）是 spec §4.2 / §13.3-C8 明令不上页面的那一列（形状照上家线 `_jidanpu_row` 里 `consumer_state` 那一格）。
        """
        state = jidanpu_member_openapi.member_consumer_state(obj)
        return {"text": state.text, "bad": state.bad}

    def validate_jidanpu_base_url(self, value):
        """必须是 `http(s)://` 开头，并按 SDK 的口径归一化（`strip().rstrip("/")`）——
        不归一化的话，同一个地址会以两种形态进库，`(地址, 账号)` 唯一约束就形同虚设。
        """
        cleaned = (value or "").strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise serializers.ValidationError("鸡蛋铺地址必须以 http:// 或 https:// 开头")
        return cleaned

    def validate_account(self, value):
        """①式必须有账号；⭐ **②式不要求**（spec §2.1：两套凭据各自独立）。 ②式那一路**不登录**、用不到账号（Credential 面是地址 + `client_id` + `client_secret`）
        ⇒ 要求它非空会让「在界面上新建一条②式绑定」**永远被拒**（`account` 那一格在②式界面上是隐藏的，spec §11.2）。⚠️ 放宽**只对②式**，判据仍是 `mode`（⛔ 不看 `client_id`）。
        """
        cleaned = (value or "").strip()
        if not cleaned and not self._openapi_now():
            raise serializers.ValidationError("鸡蛋铺账号不能为空")
        return cleaned

    def validate_password(self, value):
        """首尾空白 / 换行是复制粘贴的常见形态，而带换行的密码只会得到一句
        「账号或密码错误」（看着像账号错了）⇒ 入口处 strip（与租户级那条同口径）。
        """
        return (value or "").strip()

    def validate_client_id(self, value):
        """与 `password` / `account` 同口径的 `strip`（复制粘贴带来的首尾空白很常见）。"""
        return (value or "").strip()

    def validate_client_secret(self, value):
        """`strip` 的理由同 `password`：带换行的密钥只会得到一句「签名对不上 / 凭据不对」。

        ⚠️ 这里**不判空**（空串 = 不改，见 `_BLANK_MEANS_KEEP`）；必填判据在 `validate()`。
        """
        return (value or "").strip()

    def validate_source_name(self, value):
        """空串 ⇒ 落到**来源名默认值的唯一出处**（`models` 的模块级常量）。 ⚠️ `detect_binding()` 读库时也用同一个常量做回落 ⇒ 这里写死字面值会造出第二个真相源（`jidanpu_member` 那边的注释已把这条口径写死）。
        ⭐ **改准**：这个常量原住在已删除的 `JidanpuBinding` 上，随那张表一起搬到 `models` 的**模块级**（同名、同值、同语义）⇒ 口径不变、仍是唯一出处。
        """
        return (value or "").strip() or SOURCE_NAME_DEFAULT

    def get_unique_together_validators(self):
        """**关掉 DRF 自动生成的那条唯一约束校验器**，改由下面的 `validate()` 自己报。 ⚠️ 为什么必须关：DRF 3.18 会替模型的 `UniqueConstraint(("jidanpu_base_url", "account"))` 自动装一条 `UniqueTogetherValidator`，它在 `validate()` **之前**跑、
        报的是 DRF 自己的 i18n 文案「字段 jidanpu_base_url, account 必须能构成唯一集合。」——既不说是谁占着这个账号，也不告诉用户下一步该干什么（业务方报的就是它）。 ⛔ 这不等于放宽约束：模型上那道 `uniq_jdp_member_account` 与数据库里的唯一索引
        一个字节都没动（并发下仍由它兜底）。 ⚠️ 本模型**只有**这一条唯一约束 ⇒ 直接回空列表；将来若加了别的约束，这里要按 `constraint.fields` 逐条放行，⛔ 别继续一句 `return []` 把它一起关掉。
        """
        return []

    def _mode_after(self) -> str:
        """**这次请求之后** `mode` 是什么（唯一判据的取值，spec §2.1）。 - 请求体里给了 `mode` ⇒ 就是它（值不合法时字段级校验已经先失败了，到不了这里）； - 没给 ⇒ 现有行那一格（⛔ **不是**模型默认值：老行不该因为一次没带 `mode` 的 PUT 被切回①式）；
        - 新建且没给 ⇒ 模型默认值（`password`）。 ⚠️ 读**原始载荷**（`initial_data`）而不是 `validated_data`：字段级校验器（`validate_account`）跑在 `validate()` **之前**，那时 `validated_data` 里还没有 `mode`。
        """
        raw = (self.initial_data or {}).get("mode")
        if raw:
            return str(raw)
        if self.instance is not None:
            return str(self.instance.mode)
        return str(JidanpuMemberBinding.Mode.PASSWORD)

    def _openapi_now(self) -> bool:
        """这次请求表达的是不是②式（`mode`，⛔ 不用 `client_id` 真值反推）。"""
        return self._mode_after() == jidanpu_openapi.OPENAPI_MODE

    def _value_after(self, field, value) -> str:
        """这一格**这次请求之后**的值（空串 = 不修改 ⇒ 现有行上那个值；新建 ⇒ 空串）。"""
        if value:
            return str(value)
        if self.instance is None:
            return ""
        return str(getattr(self.instance, field, "") or "")

    def _openapi_required_now(self) -> bool:
        """**这次请求**要不要强制「②式那两格非空」（spec §4.3 的不变式 + §13.3-C2 的判据）。触发条件 = **这次请求显式声明了②式**（只看请求体里的 `mode`；库里那一行是什么⛔ 不作判据，见下面那条 ⚠️）： - 新建：没带 `mode` ⇒ 默认①式 ⇒ 不强制；带了 `openapi` ⇒ 强制； - 更新：带了 `mode="openapi"` ⇒ 强制（含「本来就是②式、这次再确认一次」那一跳）。 ⚠️ **「没带 `mode`」的更新不判**（spec §4.3 的「为什么不落 DB」栏 + §13.3-C2）：
        ① `client_secret` **只写不读** ⇒ 它下不了 DB 约束；而本层判的是「**这次请求之后**那一格是什么」（`_value_after`：请求没带这一格时**回落到库里那一格**）⇒ 正常行**不会**被逼着「每次无关保存都重填密钥」； ② 判据挂在**用户表达方式的那一跳**上 ⇒ 「切到②式」一定被拦（⛔ 经这个端点造不出一行「②式但没有 client_id」）。 ⚠️ **代价（如实登记）**：ORM / shell 直写出来的遗留行（②式且 `client_id=""`）经本端点
        保存时**不会被这一层拦** —— 它由**另外两道**兜住：`_credentials_missing` （启用 / 检测前挡住并说「还没配置…client_id…」）与**启用前的那次探测**。
        """
        declared = str((self.initial_data or {}).get("mode") or "")
        # ⚠️ 到这里 `declared` 是空 ⇒ 只剩「**这次请求没有显式声明②式**」这一种情况 ⇒ 一律**不强制**（新建 ⇒ 模型默认 `password` = ①式；更新 ⇒ 有意不判，理由见上）。 ⛔ 这里原先还有半句 `self.instance is None and self._mode_after() == OPENAPI_MODE`：
        # 它走到这一行时 `declared` 已空 ⇒ `_mode_after()` 对**新建**只能回模型默认 `"password"` ⇒ 第二个合取项**恒假**、整个表达式**恒为 False**（「新建且没给 mode 的②式」那一档 **逻辑上不存在**，是**说假话的形状**，E-0063 家族）⇒ 已删，⛔ 别再补回来。
        return declared == jidanpu_openapi.OPENAPI_MODE

    def validate(self, attrs):
        """两条判据，**顺序承重**：先②式必填（§4.3 / §11.5），再①式的「地址 + 账号」占用。 ⚠️ ②式那两格的判据判「**这次请求之后**那一格是什么」而不是「这次带没带」： `client_id` / `client_secret` 都是「空串 = 不修改」⇒ 本来就是②式的那一行，改地址时不带这两格也必须过（否则「改地址」会变成「必须重填密钥」）。 ⚠️ 触发条件见 `_openapi_required_now`（只看 `mode`，⛔ 不用 `client_id` 非空去反推，E-0021 家族）。 ⚠️ ①式（`password`）**一个字节都不判**：`client_id` / `client_secret` 传空串照过（§13.3-C2 后半句）。 ## ⭐ 「地址 + 账号」这一条**只对①式**（业务方裁定） **为什么必须加这个分支**：②式那两格在界面上是隐藏的（`account` 是**空串**）⇒ 不加分支时，同一个域名上的第二条②式绑定会被这一条拒掉，而回的文案是「该鸡蛋铺账号已被 X 绑定…」
        —— **读起来像在说账号，实际是模式冲突**（业务方报的就是它）。 ⚠️ 判据与**模型那条部分唯一索引同一个谓词**（`mode="password"`，见 `JidanpuMemberBinding.Meta.constraints`）：两处不一致的话，要么库里报 IntegrityError、要么这一层放行了数据库又来拒（两边都不好看）。⛔ 这不是「放松约束」：①式那一支一个字没变。 ⚠️ ②式的唯一性**不靠账号**，靠「**一个域名一个回调码**」（`_ensure_callback_code`）：同一个域名的多个打手共用同一个回调地址（业务方 的口径）。「地址 + 账号」被别人占了 ⇒ **在序列化器层先查再报**（业务方）： ⚠️ **已停用**的绑定也算占用（照本线「地址级去重含已停用」的既有口径， `jidanpu_member._ensure_remote_order` 的地址级去重就是含停用的）⇒ ⛔ 不加
        `enabled=True` 过滤。 ⚠️ **自己保存自己**那一行不算冲突（`exclude(pk=self.instance.pk)`）：PUT 是覆盖写，页面总会把原来的地址与账号带回来，不排除自己就会「保存一次就自撞一次」。 ⚠️ 名字走 `apps.accounts.display.user_display_name`（显示名 / 用户名的**唯一出处**）： ⛔ 不在这里另写一份 `first_name or username`。
        """
        if self._openapi_required_now():
            if not self._value_after("client_id", attrs.get("client_id", "")):
                raise serializers.ValidationError({"client_id": CLIENT_ID_REQUIRED_MESSAGE})
            if not self._value_after("client_secret", attrs.get("client_secret", "")):
                raise serializers.ValidationError(
                    {"client_secret": CLIENT_SECRET_REQUIRED_MESSAGE}
                )
        # ⭐ ②式**不做**「地址 + 账号」占用判据（见 docstring）：那一档的账号是隐藏的空串，
        #    同一个域名下允许多条②式绑定（它们靠共用的回调码收自己那些单）。
        if self._mode_after() == jidanpu_openapi.OPENAPI_MODE:
            return attrs
        base_url = attrs.get(
            "jidanpu_base_url", getattr(self.instance, "jidanpu_base_url", "")
        )
        account = attrs.get("account", getattr(self.instance, "account", ""))
        #: ⚠️ `mode=password` 那半个谓词**必须**带上（与模型那条部分唯一索引一致）：
        #: 不带的话，一条②式行（`account` 里可能留着旧用户名）会把一次**正常的①式保存**拒掉，
        #: 而数据库那一层根本不会拒 —— 那就是「这一层比真相更严」（说假话的判据）。
        others = JidanpuMemberBinding.objects.filter(
            mode=JidanpuMemberBinding.Mode.PASSWORD,
            jidanpu_base_url=base_url, account=account,
        ).select_related("user")
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        occupant = others.first()
        if occupant is not None:
            raise serializers.ValidationError(
                binding_conflict_message(user_display_name(occupant.user))
            )
        return attrs

    def pending_instance(self, user):
        """按校验后的数据算出「落库后会是什么」的**未落库**实例（行主只来自 `user`）。 ⚠️ 为什么不用 DRF 的标准写入路径：`ModelSerializer.update()` 末尾自带 `instance.save()`，而 `save(commit=False)` 在本仓的 DRF 版本里**没有** `commit`
        参数（它必然落库）——本线要求「检测不过 ⇒ 一行都不落」⇒ 合并必须**只改内存**。这里逐字段 `setattr`，并单独处理 `password` 的「空串 = 不改」语义（页面上的密码框**回显不了**，留空只能表示「不动它」）；②式的 `client_id` /
        `client_secret` **同一条口径**（见 `_BLANK_MEANS_KEEP`）。 ⚠️ 行主由调用方（视图）从 `request.user` 传入：序列化器**没有** `user` 字段，请求体里塞 `user` 会被 DRF 静默忽略（越权面 = 0）。
        """
        if self.instance is None:
            return JidanpuMemberBinding(user=user, **self.validated_data)
        for field, value in self.validated_data.items():
            if field in _BLANK_MEANS_KEEP and not value:
                continue  # 空串 = 不改（旧值留在 instance 上）
            setattr(self.instance, field, value)
        return self.instance


class JidanpuMemberBindingView(APIView):
    """`/api/business/member/jidanpu-binding/`：本人那一行的单行资源。 ⚠️ 只定义 `get` / `put` ⇒ `POST` / `PATCH` / `DELETE` 由框架挡成 **405**
    （与租户级那条同形；`POST` 尤其不能出现在这条路径上——`test-connection` 住在它的子路径下，两个都接 POST 会让「POST 它到底干什么」变成路由层的歧义）。
    """

    def get(self, request):
        binding = JidanpuMemberBinding.objects.filter(user=request.user).first()
        data = None if binding is None else JidanpuMemberBindingSerializer(binding).data
        return api_response(data=data)

    def put(self, request):
        existing = JidanpuMemberBinding.objects.filter(user=request.user).first()
        if existing is not None and not jidanpu_assignment_lock.binding_change_available(existing.pk):
            return api_response(msg=jidanpu_assignment_lock.BINDING_BUSY, code=ErrorCode.BUSINESS)
        # ⚠️ 旧凭据要在 `pending_instance` **改内存之前**取下来：那之后 `existing` 与 `binding` 是同一个对象，再比就是自己跟自己比（换绑永远判不出来）。 ⭐ **`mode` 也算「凭据身份」的一部分**（官方 API spec §2.3-C2 明写「触发条件要加上
        # `mode` 变化」，与上家线同一口径）：①↔② 换方式 = 换了一整套凭据 ⇒ 在飞事件必须收敛（⛔ 不许拿新方式的凭据去重试旧事件）。
        previous_credentials = (
            None if existing is None
            else (existing.jidanpu_base_url, existing.account, existing.mode, existing.client_id if _is_openapi(existing) else "")
        )
        # ⚠️ 「本次是不是**翻真**」也要在 `pending_instance` 改内存之前取下来：那之后
        #    `existing` 与 `binding` 是同一个对象。只有翻真那一刻才补推（spec §5.2 / A4）。
        was_enabled = False if existing is None else existing.enabled
        # ⭐ 触发点⑨（D-0326 / E-0301）：「抛单百分比」的**旧值**同样要在    `pending_instance` 改内存之前取下来——那之后 `existing` 与 `binding` 是同一个
        #    对象，再比就是自己跟自己比（照上面 `previous_credentials` 的同一条注释）。    「**真的变了才排**」的判据在事务内那一处（触发点⑧的同一条家族纪律）。
        previous_throw_percent = (
            None if existing is None else existing.throw_price_percent
        )
        serializer = JidanpuMemberBindingSerializer(existing, data=request.data)
        serializer.is_valid(raise_exception=True)  # 失败 ⇒ 统一处理器包成 1000

        # 「落库后会是什么」——一个**未落库**的实例（空密码已按「不改」合并）。
        # 检测读它、⛔ 不读库、也⛔ 不落库：所以下面几条可预期失败的 `return`
        # 都在**进事务之前**（E-0022 的边界不在场）。
        binding = serializer.pending_instance(request.user)

        if binding.enabled:
            # ⚠️ 「凭据全不全」与「检测」**都按方式分流**（spec §2.1 / §11.4 末段：两个分支都适用）。
            # ⚠️ ①式那一支的调用序列、异常翻译与文案**一个字都没变**。
            if _credentials_missing(binding):
                return api_response(
                    data=None,
                    msg=_not_configured_message(binding),
                    code=ErrorCode.PLATFORM_NOT_CONFIGURED,
                )
            if _is_openapi(binding):
                # ⭐ ②式：**逐条只读探测**（spec §9.2 / §11.4 / §20.2），⛔ 一个写接口都不打。
                probe = jidanpu_openapi.probe_read_endpoints(binding)
                if not probe.gate_ok:
                    # ⭐ **这把闸只看两跳**（spec §25.2 的总指挥裁定，**优先于 §11.4 末段**的字面读法）：挡启用 = `token` 可取 **且** `GetOrderStatus` 可用（缺任何一跳，状态同步都不可能）。 ⛔ 另外三项（`GetGames` / `GetProject` / `GetOrderProcessImgs`）**不挡启用**：
                    # 它们是信息性的（前两项是**录单**那条路要用的、属后续阶段，进度图是展示），而**非闸行的**内层权限问题「不影响订单状态同步」（§20.2 / §9.1-B）⇒ 只缺 `GetGames` 权限的人**状态同步本来跑得起来**，⛔ 不该连启用都启用不了。
                    # ⚠️ 五项照样全探、照样进 `checks` 表（§20 的「逐条列出」没变）。 ⛔ 一行都不落，`enabled` 保持原值；回的是**逐条表**里那几句去重后的结论＋「该做什么」（与「测试连接」用的是**同一份**文案来源，⛔ 不另写一套）。
                    return api_response(
                        data=None,
                        msg="\n".join(probe.advice),
                        code=ErrorCode.PLATFORM_NOT_CONFIGURED,
                    )
                # ②式**没有**「登录显示名」这一跳（它压根不登录）⇒ 与停用档同形，不写 `account_name`。
                login_name = None
            else:
                try:
                    # ⚠️ 用**同一条连**多解析一跳「当前登录账号的显示名」（`GetUserInfo` +
                    #    两个名单）——⛔ 不另建一次连（那会多一次登录、多几条上游请求）。
                    report, login_name = jidanpu_member.detect_binding_with_login_identity(
                        binding
                    )
                except JidanpuError as exc:
                    code, message = _upstream_error(exc)
                    return api_response(data=None, msg=message, code=code)
                if not report.ok:
                    # 检测不过 ⇒ 不许启用（spec §9.1）：⛔ 一行都不落，`enabled` 保持原值。
                    return api_response(
                        data=None,
                        msg="；".join(report.failures),
                        code=ErrorCode.PLATFORM_NOT_CONFIGURED,
                    )
        else:
            # 停用档不建连、不解析身份（⛔ 不为「顺带刷新」去打上游）。
            login_name = None

        rebinding = previous_credentials is not None and previous_credentials != (
            binding.jidanpu_base_url, binding.account, binding.mode,
            binding.client_id if _is_openapi(binding) else "",
        )
        action = (
            "system.jidanpu_member_binding_create"
            if existing is None
            else "system.jidanpu_member_binding_update"
        )
        # ⚠️ 业务写入与留痕**同一个事务**：留痕写不进去就一起回滚。
        # ⛔ 块内**不许 `return`**（E-0022：`atomic()` 不是回滚点）。
        with transaction.atomic():
            if binding.pk is not None and not jidanpu_assignment_lock.lock_binding_change(binding.pk):
                return api_response(msg=jidanpu_assignment_lock.BINDING_BUSY, code=ErrorCode.BUSINESS)
            if rebinding:
                from apps.business import jidanpu_direct_queue

                if jidanpu_direct_queue.has_pending_binding(binding):
                    return api_response(msg="这家鸡蛋铺有待确认的指派，请先核对指派结果。", code=ErrorCode.BUSINESS)
            update_fields = _PUT_WRITE_FIELDS
            if rebinding:
                # 换了凭据 ⇒ 上一份凭据的检测结论不再成立（页面不能拿旧结论说「健康」）；
                # 若本次同时启用了，下面会立刻写成刚才这次检测的时刻。
                binding.checked_at = None
                # ⭐ 缓存的那个「下家录入客服 uid」也一起清掉：旧账号的 uid 对新账号是**另一个人**    （`AGENTS.md` §5-E：给一个字段加写入点，必须回查它的清理路径，E-0027）。
                #    ⚠️ 它**不在** `_PUT_WRITE_FIELDS` 里（那是用户可写的配置列）⇒ 只在**这一档**    临时把它加进 `update_fields`；其余时候一个字节都不写它（并发写不丢）。
                binding.input_order_kefu = ""
                update_fields = (*_PUT_WRITE_FIELDS, "input_order_kefu")
            if binding.enabled:
                binding.checked_at = timezone.now()
            # ⭐ 把**登录账号的显示名**落库：页面显示、快路回声抑制    （`_echo_by_name`）与对账接手人比对（`_sync_account_name_of`）都读这一格    ——身份错时我们自己的日志行不被抑制 ⇒ 多走「回查远端 ⇒ 可能接手」那条路。
            #    ⚠️ 写的是**引擎列的缓存**（P0-1 的口径），与同步引擎每轮的自愈    （`jidanpu_member.heal_binding_identity`）同一目的、同一值来源。    ⚠️ 在 `save()` **之前**赋值，让它随本次收窄写入（`account_name` 在
            #    `_PUT_WRITE_FIELDS` 里）一次落库；⛔ 不另开一次 `.update()`。    ⛔ 解析失败 / 名字没查到（`login_name is None`）⇒ **不写、不报错**，配置照落。
            if login_name:
                binding.account_name = login_name
            # ⚠️ **收窄写入**（T3 M3，转成 T5 的硬要求）：`enabled=True` 时上面那次检测与这次落库之间有网络往返，同步引擎完全可能在这段时间里写了 `last_*` 健康列 ⇒ 整行 `save()` 会把它们覆盖回旧值。⇒ 更新只写配置类列（`_PUT_WRITE_FIELDS`）。
            # ⚠️ **新建**那一支只能整行 `save()`：Django 的 `save(update_fields=…)` 在对象还没有主键时会抛 `ValueError: Cannot force an update in save() with no primary key.`
            # （5.2 实测）——新建时别的列都是刚设的，不存在 lost update。
            if binding._state.adding:
                binding.save()
            else:
                binding.save(update_fields=update_fields)
            # ⭐ ②式绑定保存时，把「码 → 该绑定的 client_secret」写进 `$R/shared/callback-keys.env`（0600，接收器按码取密钥的那份文件）。 ⚠️ 三条口径（判据的唯一出处是 `jidanpu_openapi.callback_key_of`，⛔ 别在这里再判一遍）：   ① **①式绑定 / 没有码 / 密钥为空 ⇒ 一个字节都不写**（写空值会让那条回调必然 401，      而页面与库里一切正常 —— 正是这条线最防的静默）；   ② `enabled=False` **照写**：地址要先给用户看，用户常常「先配好再启用」；
            #   ③ **只增不减**：解绑 / 换方式都不删旧行（删了会让还在用它的绑定断掉）。 ⚠️ 放在**事务内**且**不接异常**是有意的：写不进那份文件 ⇒ 整个保存回滚。    若吞掉它，库里会留下一行「代码说配好了、文件里没有」的绑定，而回调全部    401/503 且页面上看不出来（spec §5.3 第 2 条的代价栏写的就是这种形态）。
            jidanpu_openapi.ensure_callback_key(binding)
            if rebinding:
                JidanpuMemberOrderLink.objects.filter(binding=binding).update(retired_at=timezone.now())
                _converge_in_flight_events(binding, previous_store=previous_credentials[0])
            # ⭐ 触发点⑨（D-0326 / E-0301）：「抛单百分比」**真的变了** ⇒ 给**这一份绑定**    已有链路、在厅、本地待抢单的卡排 `member.price`（唯一实现在    `jidanpu_member.queue_percent_reprice`，⛔ 视图里不许内联第二份）。    ⚠️ 「值有没有变」的判据就在这一行：**旧值快照 ≠ 落库后的值**才排——    ⛔ 不是「这次请求带了这一格」（页面整表单提交，没改的那格也会带原值；    照触发点⑧「带了键就排 = 每存一次单就打一次上游」的同一条教训）。
            #    ⚠️ **换绑（`rebinding`）不触发**：那是「换了一整套凭据」，旧凭据名下的    在飞事件已在上一行收敛成 `skipped`、存量卡按 spec §8.4 归人工核对；    ⚠️ 顺序也是承重的：本句必须排在收敛**之后**——两档同一次叠加时，    新排的 `pending` 行按 `payload.member_binding_id` 也会被收敛命中。
            if (
                not rebinding
                and previous_throw_percent is not None
                and previous_throw_percent != binding.throw_price_percent
            ):
                jidanpu_member.queue_percent_reprice(binding)
            # ⭐ 下游鸡蛋铺线（spec §5.2 的「绑定那一刻的补推」/ A4；    已抽成**一个**函数）：`enabled` **翻真**那一刻，把    `reconcile()` 的差集一次算出来、逐张排台账（`to_launch` ⇒ `member.launch`；    **`to_record` ⇒ `member.assign`（只录单）**；`to_withdraw` ⇒ `member.withdraw`）    ——否则「绑定前就在他大厅里的单」与「已指派给他、本地却没有链路的单」都要等    下一轮对账（≤180 秒），而后者其实**永远不会**进场（X-1 抓出来的洞）。
            #    ⚠️ **唯一实现**在 `jidanpu_member.reconcile_and_queue` 里（⛔ 视图里不许内联    第二份）：它是「漏单」的最后一道兜底，定时循环调的是同一个函数。    ⚠️ **顺序是承重的**：必须排在 `_converge_in_flight_events` **之后**——    换绑时它会把这一行的在飞事件（`pending` / `failed`）收敛成 `skipped`，    补推若排在它前面，刚写的 `pending` 行会被同一笔请求立刻作废。    ⚠️ 只在**翻真**那一档跑（`enabled` 本来就是真的再保存一次不重复补推）。
            #    ⛔ 本行不调上游：补推只写台账，真投递在 `jidanpu_sync` 的 worker 里。    ⚠️ **返回值不许丢**（I-1）：同地址重叠被跳过的单由    `QueueStats.skipped` 带**人话原因**，它必须**出得去**（放进响应，页面可忽略）    ——本线不回退「逐单落台账」（那会每轮灌台账，spec §5.4 的收口改准）。
            queued = None
            if binding.enabled and not was_enabled:
                # 此事务已更新绑定和台账，避免反向获取指派先持有的订单锁。
                queued = jidanpu_member.reconcile_and_queue(
                    binding, now=timezone.now(), lock_launch_orders=False,
                )
            record(
                log_type=ACTION_LOG_TYPES[action],
                action=action,
                actor=request.user.get_username(),
                actor_id=request.user.pk,
                target_kind=JIDANPU_MEMBER_BINDING_TARGET_KIND,
                target_id=binding.pk,
                target_name=_member_binding_name(binding),
            )
        data = JidanpuMemberBindingSerializer(binding).data
        if queued is not None:
            # 补推那一跳的差集（计数 + 被跳过单的人话原因）。⚠️ 只在**真的跑了**那一档出现
            # ——「没跑」与「跑了但零动作」在这里必须分得开（`None` vs `{...0...}`）。
            data["reconcile"] = _reconcile_summary(queued)
        return api_response(data=data)


class JidanpuMemberTestConnectionSerializer(serializers.Serializer):
    """个人绑定的「测试连接」**可选入参**（2026-09-30 D-0177）：`password` 与 `source_name`。 ⚠️ **不接受** `jidanpu_base_url` / `account`（S3 红线）：那两样只从**自己那一行**取——地址决定「我们去打谁的服务器」，请求方能传它，这条端点就成了一个拿我们当跳板的通道。 ⭐ `password` 是业务方裁定放开的那一格（「密码用页面上此刻填的值，未保存也能查」）：只用于本次建连，⛔ 不落库（视图在**内存副本**上覆盖）、⛔ 不回显（`write_only`）。 ⭐ `source_name` 让「检查的就是下拉里此刻选中的那个」（与租户级 F4 同口径）。
    ⚠️ **两处空串语义相反，这里只走「回落」那一支**（评审必查项）： - **本序列化器（读路径）**：空串 ⇒ **回落库里那一行**（⛔ 不改动任何东西）； - **PUT 的 `validate_source_name`（写路径）**：空串 ⇒ **落回默认名「团团通系统」**。下拉把这两条都绕开了：页面**永远提交一个非空的名字**（见岛页的 `renderSourceOptions`）。
    """

    #: 与模型列宽同值（255）：显式声明的 `CharField` **不继承**模型列宽 ⇒ 漏了就是 500（E-0010）。
    password = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default="", write_only=True,
        help_text="页面上此刻填的密码（只用于本次检测；留空 = 用库里那一份）",
    )
    #: 同上（64，与 `JidanpuMemberBinding.source_name` 的列宽同值）。
    source_name = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )


class JidanpuMemberTestConnectionView(APIView):
    """`POST /api/business/member/jidanpu-binding/test-connection/`（spec §9.1 / §9.2）。 ⚠️ **只读上游、不改用户配置**（所以登记在 `operation_log_ledger.DELIBERATELY_NOT_LOGGED` 里）：地址 / 账号 / 密码 / 来源名 / 开关 / 间隔这些**用户填的**字段一个都不动，也不写 `checked_at` / `last_error`。 ⭐ **改准**：本段原写「**只读上游、不落库**」（原句在此保留）——本动作现在会写 **一格引擎列**：`account_name` 落成**登录账号**的显示名（业务方裁定「同步账号 = 我们登录的那个账号」；若显示名与凭据账号不一致，快路的回声抑制会认不出我们自己的日志行 ⇒ 多走「回查远端 ⇒ 可能接手」那条路）。⚠️ 它仍然不写健康列、仍然不记操作日志（改的是引擎写的缓存列，不是用户配置）；写法与同步引擎每轮的自愈（`jidanpu_member.heal_binding_identity`）同一口径、同一值来源。 ⚠️ **出参形状**：`{"ok": bool, "messages": [人话, …]}`，信封 `code` 恒为 0 （请求本身成功了；「检测没过」是一个**正常的结论**，与租户级 `test-connection` 用
    `messages` 数组表达配置类问题的口径一致）。⛔ 没绑定时才回业务失败码（没东西可测）。 ⭐ **改准（D-0177）**：上段那句形状**不再完整**（原句在此保留）——为页面那个「订单来源」下拉，出参**追加三个键**（纯加法，既有两个键名与取值一个不动）： `source_options`（对方来源字典**全量**，每项 `{name, label}`；`label` 是要保存的名字）、 `source_options_ok`（整份字典那一跳读到了没有）、`source_ok`（选中那个名字唯一命中了没有）。 ⚠️ **两个作用域的出参在这里刻意保持同形**（键名、每项的形状都一样）：页面共用一个渲染器、一套判据；⛔ 但**不是**把租户级那份富结果搬过来（`user_count` / `candidates` 那些**没有**，它们在个人这条线上根本不存在）。 ⚠️ **可选的请求体**（起，本端点**不再**「连请求体都不读」）： `password`（页面上此刻填的密码，⛔ 不落库、⛔ 不回显，只用于本次建连）与 `source_name`（下拉里此刻选中的来源名——「检查的就是你要保存的那个」，与租户级 F4 同口径）。两者空/缺省 ⇒ 回落到库里那一行（既有行为不变）。
    ⚠️ 这两个键**只对①式有意义**：②式那一支不登录、也不读来源字典 ⇒ 传了也只被忽略（⛔ 不会因此去打一次登录）。 ## ⭐ 按 `mode` 分流（官方 API 对接线；spec §11.4 / §20.2） | 方式 | 走什么 | 出参 | |---|---|---| | ①式（`password`） | `jidanpu_member.detect_binding_with_login_identity`（**逐字不变**） | `{"ok", "messages", "source_options", "source_options_ok", "source_ok"}`（**键集合一字未变**） | | ②式（`openapi`） | `jidanpu_openapi.probe_read_endpoints`：**逐条只读探测** | `{"ok", "messages", "actions", "checks"}`（逐条表，字段见 `EndpointProbe`） | ⚠️ 分流判据只有 `mode`（spec §2.1）⇒ ⛔ **不是**「`client_id` 非空」那种真值判据（E-0021 家族：切回①式之后 `client_id` 还在）。 ⚠️ ①式那一支**连键集合都没变**是有意的：既有的页与既有用例逐字依赖它（回归判据钉住了）。 ⚠️ ②式那一支**一个写接口都不打**（只有「取 token」那一发 POST，那是**取 token**、不改对方任何数据；其余全是 GET）；⛔ 也不落库、⛔ 不写 `account_name`。
    """

    def post(self, request):
        binding = JidanpuMemberBinding.objects.filter(user=request.user).first()
        if binding is None:
            return api_response(
                data=None,
                msg="还没配置下家鸡蛋铺的绑定，无法检测",
                code=ErrorCode.PLATFORM_NOT_CONFIGURED,
            )
        params = JidanpuMemberTestConnectionSerializer(data=request.data)
        params.is_valid(raise_exception=True)
        # ⚠️ `source_name` 空串 = **回落库里那一行**（⛔ 不是「落回默认值」——那件事只发生在
        #    PUT 的 `validate_source_name` 里；这一条是**读**路径，不许改语义）。
        source_name = (params.validated_data.get("source_name") or "").strip()
        # ⭐ 页面上此刻填的密码（D-0177）：只覆盖在**内存副本**上（⛔ 不落库、⛔ 不回显）。
        #    复用租户级那条线的同一实现（同一份判据，⛔ 不另抄一份）。
        target = _with_password(binding, params.validated_data.get("password"))
        if _credentials_missing(target):
            # 与 PUT 的启用路径同一句话：「缺项」说「还没配置」，⛔ 不说成「不合法」。
            # ⚠️ 按方式分成两句（②式缺的是 client_id / client_secret，不是账号密码）。
            return api_response(
                data=None,
                msg=_not_configured_message(target),
                code=ErrorCode.PLATFORM_NOT_CONFIGURED,
            )
        if _is_openapi(target):
            # ⭐ ②式：**逐条只读探测** + 一张表（spec §9.2 / §20.2），⛔ 不打写接口、⛔ 不落库。 ⚠️ 出参形状是 `{ok, messages, actions, checks}`（**没有** `source_options` 那三个键：    那条路要登录态，②式压根不登录）；它由 `ProbeReport.as_data()` 一处产出
            #    （⛔ 视图不自己拼那个 dict），与上家线②式那一支逐字同形。 ⚠️ 这一支**不写 `account_name`**（那是①式「登录显示名」的缓存，②式没有登录）。
            return api_response(data=jidanpu_openapi.probe_read_endpoints(target).as_data())
        try:
            # ⚠️ 用**同一条连**多解析一跳「当前登录账号的显示名」——⛔ 不另建一次连
            #    （那会多一次登录、还会破掉「点一次测试连接只建一次连」这条既有判据）。
            report, login_name = jidanpu_member.detect_binding_with_login_identity(
                target, source_name=source_name
            )
        except JidanpuError as exc:
            code, message = _upstream_error(exc)
            return api_response(data=None, msg=message, code=code)
        # ⭐ 把**登录账号的显示名**落库：页面上的「同步账号」显示名要显示    真的那个（详见类 docstring 的改准块）。    ⛔ 解析失败 / 名字没查到（`login_name is None`）⇒ **不写、不报错**，检测结论照旧返回。
        #    ⚠️ 写的是**引擎写的列**，与同步引擎每轮的自愈同一目的、同一值来源。    ⚠️ 写的是**原行**（`binding.pk`），⛔ 不是那份可能带临时密码的副本。
        if login_name:
            JidanpuMemberBinding.objects.filter(pk=binding.pk).update(
                account_name=login_name
            )
        return api_response(
            data={
                "ok": report.ok,
                "messages": list(report.failures),
                #: ⭐ 对方来源字典**全量**（页面下拉的唯一数据源，D-0177）。
                "source_options": [dict(row) for row in report.source_options],
                #: 「整份字典那一跳读到了没有」。⚠️ **`False` 时页面不许判「对方已无此来源」**
                #: ——登录没成也会走到 `False`（那时字典是空的，而「没读到」≠「没有」）。
                "source_options_ok": report.source_options_ok,
                #: 选中那个名字在字典里**唯一命中**了没有（与 `messages` 里那条同源）。
                "source_ok": report.source_ok,
            }
        )


def _reconcile_summary(stats) -> dict:
    """把 `reconcile_and_queue` 的返回值压成一份**出得去**的汇总（spec §5.4 的收口改准）。

    ⚠️ `skipped` 是**人话原因**清单（⛔ 不是光秃秃的计数，也不逐单落台账）：页面可以只显示
    `skipped_count`，但数据里必须带上原因，否则「同地址重叠被跳过」又变回静默。
    """
    return {
        "launch": stats.launch,
        "assign": stats.assign,
        "withdraw": stats.withdraw,
        # ⭐ 业务方：我方接手之后要把它从对方抢单大厅撤下（⛔ 不是退单）。
        "unlist": stats.unlist,
        "skipped_count": stats.skipped_count,
        "skipped": list(stats.skipped),
    }


def _credentials_missing(binding) -> bool:
    """凭据**不全**（**按 `mode` 分两支判**）⇒ 还没法启用（先挡在检测之前，给一句准确的话）。 - ①式（`mode="password"`）⇒ `jidanpu_member.credentials_missing`：地址 / 账号 / 密码（既有判据）； - ②式（`mode="openapi"`）⇒ `jidanpu_openapi.openapi_credentials_of`：地址 / client_id / client_secret。⚠️ ②式绑定在界面上的形态就是**账号与密码都为空** ⇒ 沿用①式那把判据会让它**永远启用不了**（说「还没配置账号密码」，而那两格②式根本不用）。 ⚠️ **判据的唯一出处是两个既有函数**（它们还会再 `strip` 一次，覆盖「历史数据 / 直接 shell
    写库」这类绕过序列化器的路径）⇒ ⛔ 不在这里重写一遍判空。 ⚠️ 分流的判据只有 `mode`（spec §2.1）⇒ ⛔ 不用 `client_id` 真值反推（E-0021 家族）。 ⚠️ 与调度层 `jidanpu_sync._member_credentials_missing` **同一口径**（那里是「还没法**同步**」的门，这里是「还没法**启用**」的门）：两处都按 `mode` 分流；⛔ 不 import 管理命令那一份（api 包不该依赖管理命令），⛔ 也不在那边重写本函数。 ⚠️ **本模块自己的文案口径**保留在这里（「缺项」说「还没配置」，「配了但不合法」说
    「地址或账号密码不合法」——两句混用会让用户去重填一个只是打错字的地址， `jidanpu_views._upstream_error` 的注释把这条区分写死了）。
    """
    if _is_openapi(binding):
        return jidanpu_openapi.openapi_credentials_of(binding) is None
    return jidanpu_member.credentials_missing(binding)


def _converge_in_flight_events(binding, *, previous_store=None) -> int:
    """把**在飞**的台账事件收敛成 `skipped`（作废终态）+ 人话原因（spec §8.4），回改动行数。 - 只收敛 `pending` / `failed`（`done` / `skipped` 是终态，不动）； - 只收敛 `payload.member_binding_id` 指向**这一行**的事件（别的绑定的事不归它管）； - 本线的**链路行**⛔ 不删（历史事实），也⛔ 不拿新凭据去改旧单——换绑之后旧单要人工核对，这正是那句 reason 要说的事。 ⚠️ **终态是 `skipped`，⛔ 不是 `failed`**（控制器裁定 7）：`failed` 是「还会重试」的态，
    绑定被重新启用后 `run_outbox` 会拿**新凭据**重试这些旧事件（§8.4 明令禁止），而且那条 `failed` 行还会挡住同键的新触发（去重集合含 `pending` / `failed`）。 `skipped` 是既有那条线里「作废、不再重试」的口径 ⇒ 两条毛病一起没了。 ⭐ **同时抹掉行上的状态指纹**（`jidanpu_member.REQUEUE_GUARD_FP_KEY`）：换绑是 **人的动作**，收敛出的 `skipped` 不是「状态没变 ⇒ 结局不变」的证据——留着它，终态停重排闸（D-0370）会把重新启用后的合法补推也拦死（「换绑收敛不挡新触发」
    的既有契约由这一抹承接）。 ⚠️ **两点残差（如实登记）**：① **量级无上界**——`_IN_FLIGHT_STATUSES` 含全部 `failed`，一个绑定名下的在飞行可以很多（逐行 UPDATE；正常运行时是在飞去重压着的个位数，病态积压下这一跳会变慢，而它跑在绑定 PUT 请求里）；② **中途崩溃 = 半收敛**——已改的行已作废、未改的还在飞，**可重跑自愈**（再保存一次绑定把剩下的收敛完），无半新半旧污染。逐行改 payload 是为了抹指纹（集合通常个位数）。
    """
    rows = list(
        JidanpuSyncEvent.objects.filter(
            status__in=_IN_FLIGHT_STATUSES,
            payload__member_binding_id=binding.pk,
        ).annotate(has_recording_evidence=jidanpu_member._recording_evidence())
    )
    for event in rows:
        payload = dict(event.payload or {})
        payload.pop(jidanpu_member.REQUEUE_GUARD_FP_KEY, None)
        if event.kind in (jidanpu_member.KIND_LAUNCH, jidanpu_member.KIND_ASSIGN) and event.has_recording_evidence:
            payload.setdefault("recording_store", previous_store or binding.jidanpu_base_url)
        JidanpuSyncEvent.objects.filter(pk=event.pk).update(
            status=JidanpuSyncEvent.Status.SKIPPED,
            reason=REBINDED_EVENT_REASON,
            processed_at=timezone.now(),
            payload=payload,
        )
    return len(rows)


# ══「立即同步」端点（spec §9.3）══════════════════════════════════════════════════ 位置：对标「指派给我的订单」那一排按钮里本来就有「发到我的管理系统」，形状有依据。作用：**只同步自己那一行**名下的单。⚠️ **按 `mode` 分成两条路**（②式的凭据与①式完全不同）：
#       · ①式 ⇒ 与 §6.2 的慢路**同一段代码**（`jidanpu_member.pull_statuses`），⛔ 不写第二份；       · ②式 ⇒ 与 §8.3 的常驻那一段**同一批入口**（回调消费 + 兜底对账，`jidanpu_member_openapi`         的两个入口），⛔ 不写第二份 ——「立即同步」在②式语义下就是「把回调消化掉 + 兜底补漏」。
# 频控：沿用 P2 spec §17.6 的形状（**同会话最小间隔 + 在途互斥 + 失败退避**），       ⛔ 数值只在这一处（控制器裁定 6）。

#: 同会话两次同步之间的最小间隔（秒）。P2 spec §17.6 的「同一会话最小间隔 = 2 秒」。
SYNC_NOW_MIN_INTERVAL_SECONDS = 2
#: 失败退避阶梯（秒）：第 1 次失败 5 秒、连续第 2 次 15 秒、第 3 次及以后 60 秒；
#: **一次成功即清零**（P2 spec §17.6 的「5 s → 15 s → 60 s（三次封顶）」）。
SYNC_NOW_BACKOFF_LADDER_SECONDS = (5, 15, 60)

SYNC_NOW_IN_FLIGHT_MESSAGE = "正在同步，请稍候"
SYNC_NOW_NOT_CONFIGURED_MESSAGE = "还没配置下家鸡蛋铺的绑定，无法同步"
SYNC_NOW_DISABLED_MESSAGE = "下家鸡蛋铺的同步还没启用，无法同步"
SYNC_NOW_TOO_SOON_MESSAGE = "同步太频繁，请 {seconds} 秒后再试"
SYNC_NOW_BACKOFF_MESSAGE = "上次同步失败，请 {seconds} 秒后再试"

#: ②式「立即同步」一轮**最多查几张单**（回调消费与兜底对账两段共用这一个上限）。 ⚠️ **这一跳是同步 web 请求**：用户在页面上点一下，这一跳没返回之前页面一直在等（gunicorn 的 worker 超时是 30 秒——打超了不是「慢」，是 502 + 一个被回收的 worker）
#: ⇒ 上限必须比常驻调度那两条路**小得多**：常驻那条撞上限只是「下一拍接着查」，而这一跳撞上限是**页面一直转圈**。 ⚠️ 量级照**上家线**那一个（`jidanpu_upstream_views.SYNC_NOW_OPENAPI_MAX_ORDERS` = 5）取：
#: 5 发查单（`GetOrderStatus`；token 那一发只打一次、且由模块级缓存持有）最坏也就几秒，而「点一下立即同步」本来该处理的正是「刚发生的那几条」。 ⚠️ 撞上限**只推迟、不丢**（两段各自的既有口径：回调水位 / 对账游标一格都不前移）⇒ 再点接着查。
SYNC_NOW_OPENAPI_MAX_ORDERS = 5

#: ②式本轮**有失败行**时回给页面的人话（⛔ 别谎报成功）。为什么必须有它：本线「立即同步」不写健康列（那是调度层的口径）⇒ ②式那一支没有 `last_error_at` 可看，若仍回 `code 0 + {"applied": 0}`，用户看到的是「成功但什么都没发生」；
#: 而闸门也因为「没失败」而不退避 ⇒ 连点能一直真打上游。 ⚠️ 措辞与上家线那一句**同一口径**（⛔ 不合并成一个跨线常量：两条线各自的文案要能各自演进）。
SYNC_NOW_OPENAPI_FAILED_ROWS_MESSAGE = "有 {count} 条回拉失败，请稍后重试"


@dataclass(frozen=True)
class _OpenApiSyncNowOutcome:
    """②式「立即同步」这一跳的读数（`applied` / `failed` 与①式那个 `int` 返回值同用途）。

    ⚠️ 只为了让**信封那一段只有一份**：视图里「有失败 ⇒ 业务码 + 人话」那几行对两条路
    逐字相同（⛔ 不为②式再写一份信封逻辑）。
    """

    applied: int = 0
    failed: int = 0


def _sync_now_openapi_round(binding, *, now) -> _OpenApiSyncNowOutcome:
    """②式「立即同步」的一轮 = **回调消费一轮 + 兜底对账一轮**（spec §9.3；顺序照 §8.3 第 ② 条）。 ⚠️ **为什么不能照①式那一跳做**：②式绑定**压根没有账号 / 密码**（界面新建出来就是这样， spec §11.2）⇒ 走 `jidanpu.open_client_with_http` 只会在建连处抛 `ConfigError`，用户点「立即同步」拿到的是「凭据不完整」，而②式真正的同步（下面这两段） **一次都不会跑** —— 与上家线栽过的那个坑同形（E-0202 / spec §27.1）。 ⚠️ 分流判据**只有 `mode`**（spec §2.1）⇒ ⛔ 不是「`client_id` 非空」那种真值判据
    （E-0021 家族：切回①式之后 `client_id` 还在）。 ⚠️ 两段都是**既有入口**（`jidanpu_member_openapi.consume_member_callbacks` / `reconcile_member_statuses`，调度层 `jidanpu_sync._run_member_openapi_binding` 调的就是这两个） ⇒ ⛔ 这里没有第二套「查 → 判 → 写」：判据的唯一出处仍是下家线自己的 `jidanpu_member.pullable_links` / `apply_remote_completion`。 ⚠️ 两段共用同一个**比常驻调度小得多**的上限（`SYNC_NOW_OPENAPI_MAX_ORDERS`）。
    ⚠️ 兜底对账那一跳的**闸门**（`reconcile_seconds`，默认 180）在调度层，⛔ 本函数不加闸（「点了立即同步」本来就该立刻跑一次；与上家线那一支同一取舍）。 ⚠️ 同 spec §9.5 ③：消费那一段的取 token 那一跳刚被拒 ⇒ **同一轮里**不再为它打第二次（那句话复用调度层的同一条常量：同一件事 ⛔ 不另写一句文案）。
    """
    #: ⚠️ 函数内 import：只为借那一句话，⛔ 不让 API 层在**模块级**依赖一个管理命令模块。
    from apps.business.management.commands.jidanpu_sync import TOKEN_BLOCKED_RECONCILE_REASON

    consume = jidanpu_member_openapi.consume_member_callbacks(
        binding, now=now, max_orders=SYNC_NOW_OPENAPI_MAX_ORDERS,
    )
    reconcile = jidanpu_member_openapi.reconcile_member_statuses(
        binding, now=now, max_orders=SYNC_NOW_OPENAPI_MAX_ORDERS,
        token_blocked=TOKEN_BLOCKED_RECONCILE_REASON if consume.token_rejected else "",
    )
    return _OpenApiSyncNowOutcome(
        applied=consume.applied + reconcile.applied,
        failed=consume.failed + reconcile.failed,
    )


@dataclass
class _SyncNowState:
    """一个会话的节流状态（在途 / 上次完成时刻 / 连败次数 / 退避到什么时候）。"""

    in_flight: bool = False
    finished_at: float | None = None
    failures: int = 0
    backoff_until: float | None = None


class _SyncNowGate:
    """同一会话（浏览器标签页）的节流闸门：最小间隔 + 在途互斥 + 失败退避。 ⚠️ **进程内**状态（`threading.Lock` + 一个字典）：本仓是**一租户一套部署**、这条同步进程按 RUNBOOK 只在**一台**上跑 ⇒ 进程内够用；⚠️ 代价如实登记：将来若真改成
    多进程 / 多实例，同一个会话可能各进程各有一份节流窗（退化成「每进程一次」）。 ⚠️ `acquire()` 成功之后**必须**在 `finally` 里 `release()`（在途标记靠它复位）。 ⚠️ `now` 是**为可测性**留的注入点（秒；缺省取 `clock()`）——⛔ 不是业务参数。
    """

    def __init__(self, *, min_interval=SYNC_NOW_MIN_INTERVAL_SECONDS,
                 backoff_ladder=SYNC_NOW_BACKOFF_LADDER_SECONDS, clock=time.monotonic):
        self.min_interval = float(min_interval)
        self.backoff_ladder = tuple(float(step) for step in backoff_ladder)
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, _SyncNowState] = {}

    def _moment(self, now):
        return self._clock() if now is None else float(now)

    def acquire(self, key: str, *, now=None) -> tuple[bool, str]:
        """尝试占用这个会话的同步窗；拒绝 ⇒ `(False, 人话)`（附上还要等几秒）。"""
        moment = self._moment(now)
        with self._lock:
            state = self._state.setdefault(key, _SyncNowState())
            if state.in_flight:
                return False, SYNC_NOW_IN_FLIGHT_MESSAGE
            if state.backoff_until is not None and moment < state.backoff_until:
                return False, SYNC_NOW_BACKOFF_MESSAGE.format(
                    seconds=max(1, math.ceil(state.backoff_until - moment))
                )
            if state.finished_at is not None:
                elapsed = moment - state.finished_at
                if elapsed < self.min_interval:
                    return False, SYNC_NOW_TOO_SOON_MESSAGE.format(
                        seconds=max(1, math.ceil(self.min_interval - elapsed))
                    )
            state.in_flight = True
            return True, ""

    def release(self, key: str, *, failed: bool, now=None) -> None:
        """收尾：复位在途标记、记下完成时刻；失败 ⇒ 按阶梯进入退避，成功 ⇒ 清零。"""
        moment = self._moment(now)
        with self._lock:
            state = self._state.setdefault(key, _SyncNowState())
            state.in_flight = False
            state.finished_at = moment
            if failed:
                state.failures += 1
                step = self.backoff_ladder[
                    min(state.failures, len(self.backoff_ladder)) - 1
                ]
                state.backoff_until = moment + step
            else:
                state.failures = 0
                state.backoff_until = None


#: 本进程的**唯一**闸门。⚠️ 测试会换一个实例（状态是进程内的，跨用例串味只会造成假红）。
_SYNC_NOW_GATE = _SyncNowGate()


def _sync_now_key(request) -> str:
    """节流键 = **同一会话**（P2 spec §17.6 的粒度）。

    ⚠️ 会话键拿不到时（理论上登录态一定有）回落到「按用户」——那会让同一个人的多标签页
    共用一个节流窗（P2 spec §17.6 已登记的那条退化口径）。
    """
    key = getattr(request.session, "session_key", None)
    if key:
        return f"session:{key}"
    return f"user:{request.user.pk}"


def _health_error_moment(binding_id):
    """该绑定的 `last_error_at`（**收窄读**：只用来判「这一轮有没有写下一条失败」）。"""
    return (
        JidanpuMemberBinding.objects.filter(pk=binding_id)
        .values_list("last_error_at", flat=True)
        .first()
    )


class JidanpuMemberSyncNowView(APIView):
    """`POST /api/business/member/jidanpu-sync-now/`（spec §9.3 的「立即同步」）。 ⛔ **一个字节的请求体都不读**：行主只来自 `request.user`（越权面 = 0）。 ⚠️ 频控的拒绝是**业务失败码**（HTTP 仍是 200）：它是正常的「等一下再点」，不是传输层错误。 ⚠️ 这一跳**会真的改我方订单状态**：每一张写回都由 `apply_transition` + 一条 `order.transition` 审计留痕（操作人 = 机器名）；端点自己那个 `POST` 的分类见
    `tests/test_operation_log_ledger.py` 的 `INSTRUMENTED`。 ## ⭐ 按 `mode` 分流（官方 API 对接线；spec §9.3） | 方式 | 这一跳跑什么 | |---|---| | ①式（`password`） | `jidanpu_member.pull_statuses` ——**逐字不变**（调度每轮调的就是同一个函数，⛔ 没有第二份实现会跟它漂移） | | ②式（`openapi`） | `_sync_now_openapi_round`：**回调消费一轮 + 兜底对账一轮**（spec §8.3 的顺序），⛔ 不建①式那套登录连接 |
    ⚠️ **为什么必须分**（与上家线栽过的那个坑**同形**，E-0202 / spec §27.1）：②式绑定**压根没有账号 / 密码**（界面上新建出来就是这样，spec §11.2）⇒ 照①式那一跳做只会在 `open_client_with_http` 的建连处抛 `ConfigError("…账号 / 密码不完整")` ⇒ 用户点「立即同步」拿到的是「凭据不完整」，而②式真正的同步（下面那两段）**一次都不会跑**。 ⚠️ 分流判据**只有 `mode`**（spec §2.1）⇒ ⛔ 不是「`client_id` 非空」那种真值判据（E-0021 家族：切回①式之后 `client_id` 还在）。
    """

    def post(self, request):
        binding = JidanpuMemberBinding.objects.filter(user=request.user).first()
        if binding is None:
            return api_response(
                data=None, msg=SYNC_NOW_NOT_CONFIGURED_MESSAGE,
                code=ErrorCode.PLATFORM_NOT_CONFIGURED,
            )
        if not binding.enabled:
            return api_response(
                data=None, msg=SYNC_NOW_DISABLED_MESSAGE,
                code=ErrorCode.PLATFORM_NOT_CONFIGURED,
            )
        key = _sync_now_key(request)
        allowed, message = _SYNC_NOW_GATE.acquire(key)
        if not allowed:
            return api_response(data=None, msg=message, code=ErrorCode.BUSINESS)
        failed = False
        openapi_failed = 0
        before = _health_error_moment(binding.pk)
        try:
            if _is_openapi(binding):
                # ⭐ ②式：**真的跑一轮**（回调消费 + 兜底对账）。⚠️ 这一支⛔ 绝不建①式的登录连接
                #    ——那条路会拿**空账号密码**去建连，回一句「凭据不完整」（E-0202 家族）。
                outcome = _sync_now_openapi_round(binding, now=timezone.now())
                applied = outcome.applied
                openapi_failed = outcome.failed
                #: 「这一轮失败了没有」在这里**只能**看本轮结果：本线「立即同步」不写健康列
                #: （那是调度层 `jidanpu_sync` 的事）⇒ 下面那条 `last_error_at` 判据对②式恒不成立。
                failed = outcome.failed > 0
            else:
                # ── ①式：既有行为**一字不变** ──
                applied = jidanpu_member.pull_statuses(binding, now=timezone.now())
        except Exception:
            # 未预期错误照旧响亮上行（⛔ 不伪装成业务结果，E-0010）；退避要**记下来**。
            failed = True
            raise
        finally:
            # 「这一轮失败了没有」= 抛了异常 **或** 本轮**刚刚写下**一条失败时刻（`pull_statuses` 的预期失败不抛、只写健康列，spec §10） ⇒ 两种都要退避，否则上游持续故障时页面可以连点把它刷爆。
            # ⚠️ **判据必须是 `after is not None and after != before`**：    **成功**那一轮会把 `last_error_at` **清成 `None`**，只比「值变了没有」    （`after != before`）会把「失败 → 成功」判成失败 ⇒
            #    ① 「一次成功即清零」失效；② 上游交替故障时计数**棘轮**递增 ⇒    退避**永久锁在 60 秒**（用户点「立即同步」越来越难成功，且看不出原因）。    ⚠️ ②式那一支一个字都不写这一列 ⇒ 下面这条对它恒不成立（判据在上面的本轮结果里）。
            after = _health_error_moment(binding.pk)
            if not failed and after is not None and after != before:
                failed = True
            _SYNC_NOW_GATE.release(key, failed=failed)
        if openapi_failed:
            # ⭐ ②式本轮**有失败行** ⇒ 业务失败码 + 一句人话（⛔ 不许 code 0 一片空白：    那是「成功但什么都没发生」，而用户唯一看得到的就是这一句）。
            # ⚠️ ①式那一支的返回形状**一个字都没变**（它既有那条 `last_error_at` 判据只管退避，    信封照旧只回 `{"applied": N}`）——⛔ 别顺手把①式也改成带 `failed` 的信封。
            return api_response(
                data={"applied": applied, "failed": openapi_failed},
                msg=SYNC_NOW_OPENAPI_FAILED_ROWS_MESSAGE.format(count=openapi_failed),
                code=ErrorCode.BUSINESS,
            )
        return api_response(data={"applied": applied})


# ══「手动推送到我的下家」（计划 `2026-10-23-taker-my-orders-push.md` §2）════════ 页面：「指派给我的订单」工具栏的两颗按钮（「发到我的管理系统」录单 / 「同步订单状态」推状态）。语义：**只推勾选的行、只推操作人自己那一条绑定**（业务口径；⛔ 不接受任何用户 / 租户参数）。
# ⛔ 不新增消费者、不写第二份同步逻辑：排事件与消费全部走既有的触发点 / outbox 引擎，    本视图只做「入参校验 → 逐单判归属 → 排事件 → 跑一轮 → 如实汇总」。

#: 手动推送端点的节点门：与「我的订单」列表**同一颗**（`MyOrdersViewSet.MY_ORDERS_PERMISSIONS`）。
#: ⛔ 不新增 RBAC 节点（D-0128 冻结）；`tests/test_rbac_wiring.py` 的位点表同批登记。
PUSH_NOW_NODE = "my_orders.send_to_my_system"

#: 一次最多推几单（计划 §2）：这是**同步 web 请求**，每单都要排台账 + 打上游，
#: 上限太大 = 页面一直转圈（gunicorn worker 超时 30 秒会被回收）。
PUSH_NOW_MAX_ORDERS = 50

#: 两个动作（字面值就是契约，⛔ 改名会静默换掉语义）。
PUSH_NOW_ACTION_RECORD = "record"
PUSH_NOW_ACTION_STATUS = "status"
PUSH_NOW_ACTIONS = (PUSH_NOW_ACTION_RECORD, PUSH_NOW_ACTION_STATUS)

#: 逐单结果的三种取值（**字面值**就是对外契约：前端按它分「成功 / 跳过 / 失败」）。
PUSH_NOW_RESULT_DONE = "done"
PUSH_NOW_RESULT_SKIPPED = "skipped"
PUSH_NOW_RESULT_FAILED = "failed"

#: 没有启用中的绑定时的人话（计划 §2 第 1 条；告诉用户**下一步去哪**）。
PUSH_NOW_NOT_BOUND_MESSAGE = (
    "还没绑定或没启用下家鸡蛋铺，请到「我的资料 → 功能设置」里绑定"
)
#: 不是他当前接手 / 已软删的单（与 `order_scope.claimed_perf_of` 的四句判据同源）。
PUSH_NOW_NOT_CLAIMED_REASON = "不是当前接手的单"
#: 事件仍在排队（退避窗内 / 这条线自己的开关关着）⇒ 这一轮没推出去的人话。
PUSH_NOW_PENDING_REASON = "本轮没能推出去（推送台账里还在排队，稍后会自动重试）"
#: 刚排完事件却读不到台账行——正常路径到不了，留一句人话而不是让页面拿到空白原因。
PUSH_NOW_NO_EVENT_REASON = "推送台账里找不到这一单的记录，请重试"


class JidanpuMemberPushNowSerializer(serializers.Serializer):
    """手动推送的入参：`{order_nos: [...], action: "record"|"status"}`（计划 §2）。 - `order_nos`：**1–50 条**（按入参长度卡上限），去重后**保持入参顺序**； - `action`：只有那两档。非法值 ⇒ 统一处理器包成 **1000**（⛔ 不是 500）。
    ⚠️ **不接受任何用户 / 租户参数**：行主（推哪一份绑定）只来自 `request.user`（越权面 = 0）。 ⚠️ 长度上限卡在**原始入参**上（⛔ 不是去重后）：页面一次勾 60 行就是 60 次上游往返，与「去重后只有 3 个不同单号」无关——限流要限**请求规模**。
    """

    order_nos = serializers.ListField(
        child=serializers.CharField(
            max_length=64,
            error_messages={"blank": "订单号不能是空串", "max_length": "订单号过长"},
        ),
        min_length=1,
        max_length=PUSH_NOW_MAX_ORDERS,
        error_messages={
            "required": "请给出要推送的订单号（order_nos）",
            "empty": "至少要选 1 笔订单",
            "min_length": "至少要选 1 笔订单",
            "not_a_list": "order_nos 必须是订单号数组",
            "max_length": f"一次最多推送 {PUSH_NOW_MAX_ORDERS} 笔订单",
        },
    )
    action = serializers.ChoiceField(
        choices=PUSH_NOW_ACTIONS,
        error_messages={
            "required": "请给出推送动作（action）",
            "invalid_choice": "推送动作只能是 record（录单）或 status（推状态）",
        },
    )

    def validate_order_nos(self, value):
        """**去重、保持入参顺序**（计划 §2）。 ⚠️ 为什么必须去重：同一单号在同一批里出现两次会排出两条事件（去重只挡**在飞**，同一事务里刚写的两行都是 pending ⇒ 第二条会被第一条挡住，于是它的结果读不到——
        逐单汇总就会错位）。去重后每个单号恰好一行结果。 ⚠️ 空串 / 纯空白 ⇒ 1000（那不是订单号）：⛔ 不静默丢掉，否则页面会以为推了 N 笔。
        """
        seen: set[str] = set()
        out: list[str] = []
        for raw in value:
            order_no = (raw or "").strip()
            if not order_no:
                raise serializers.ValidationError("订单号不能是空串")
            if order_no in seen:
                continue
            seen.add(order_no)
            out.append(order_no)
        if not out:
            raise serializers.ValidationError("至少要选 1 笔订单")
        return out


def _latest_push_events(binding, order_nos, kind, *, selected_penalty_events=None) -> dict:
    """一次取齐这条绑定下的结果，普通单取最新一行。
    罚款重试复用原录单事件，按本次所选事件回报，避免受更晚的旧对账结果影响。
    """
    rows = JidanpuSyncEvent.objects.filter(
        direction=JidanpuSyncEvent.Direction.OUT,
        kind=kind,
        target_ref__in=list(order_nos),
        payload__member_binding_id=binding.pk,
    ).order_by("created_at", "id")
    if selected_penalty_events:
        rows = rows.filter(
            Q(pk__in=selected_penalty_events.values()) | ~Q(target_ref__in=selected_penalty_events),
        )
    latest: dict = {}
    for row in rows:
        latest[row.target_ref] = row
    return latest


def _push_outcome(event, blocked_reason: str) -> tuple[str, str]:
    """一条台账行 → `(result, 人话原因)`；result ∈ `done` / `skipped` / `failed`。 - `done` ⇒ 这一单推出去了（原因空串）； - `skipped` / `failed` ⇒ 消费者写下的那句人话**原样**回给页面（⛔ 不改写成「操作失败」）；
    - 仍是 `pending` ⇒ 这一轮**没轮到它**（退避窗内 / 这条线自己的开关关着）⇒ 如实记 `failed` + 一句「为什么没推出去」（⛔ 不谎报成功，也⛔ 不谎称对方拒了）。
    """
    if event is None:
        # 正常路径到不了（排完事件就有一行，见调用处的注释）。留一句人话而不是让页面拿到空白原因。
        return PUSH_NOW_RESULT_FAILED, PUSH_NOW_NO_EVENT_REASON
    if event.status == JidanpuSyncEvent.Status.DONE:
        return PUSH_NOW_RESULT_DONE, ""
    if event.status == JidanpuSyncEvent.Status.SKIPPED:
        return PUSH_NOW_RESULT_SKIPPED, event.reason
    if event.status == JidanpuSyncEvent.Status.FAILED:
        return PUSH_NOW_RESULT_FAILED, event.reason
    return PUSH_NOW_RESULT_FAILED, blocked_reason or PUSH_NOW_PENDING_REASON


def _run_push_outbox(binding) -> "jidanpu_member.OutboxStats":
    """对**这一条绑定**跑一轮出向台账（⛔ 不新增消费逻辑，只按对接方式选**既有**入口）。 ⚠️ **为什么不能一律 `jidanpu_member.run_outbox(binding, …)`**：那个函数的 `line` 缺省是 **①式**（账号 + 密码登录），而②式绑定**压根没有账号 / 密码**（界面上新建出来就是这样）
    ⇒ 拿①式的差异面去跑它，只会在建连处得到「账号 / 密码不完整」，把一单都没推出去的事件记成失败（E-0202 / spec §27.1 是同一个坑）。②式有它自己的入口 `run_member_status_outbox`（它自带「这条线开没开」那道闸，⛔ 不许在这里绕过）。
    ⛔ 判据只有 `mode`（`jidanpu_openapi.is_openapi_binding`，spec §2.1）， ⛔ 不用 `client_id` 真值反推（E-0021 家族）。
    """
    if jidanpu_openapi.is_openapi_binding(binding):
        return jidanpu_member_openapi_outbox.run_member_status_outbox(
            binding, now=timezone.now()
        )
    return jidanpu_member.run_outbox(binding, now=timezone.now())


class JidanpuMemberPushNowView(APIView):
    """`POST /api/business/member/jidanpu-push-now/`（计划 §2）。 ## 干什么（一条请求 = 一批单的一轮推送） 1. **入参**：`{order_nos: [...], action: record|status}`，1–50 条、去重保序；非法 ⇒ `1000`。 2. **绑定**：`request.user` **启用中**的那一份（`jidanpu_member._enabled_binding_of`）；没有 ⇒ `1002` + 一句人话（告诉用户去哪绑）。 3. **逐单**：走 `api/order_scope.py::claimed_perf_of`（**既有判据**：他当前 MAIN 接手、未软删）确认归属；不是 ⇒ 该单 `skipped` + 「不是当前接手的单」，⛔ 不排任何事件。 - `record` ⇒ `jidanpu_member.queue_assign(order, taker_id=user.id)`（只录单；与客服指派 / 自己接单**同一条**触发点，⛔ 不在这里另写一份录单）。
    - `status` ⇒ `jidanpu_member.queue_status_out(order, manual=True, binding=本行)` （`manual` 只放宽消费者的推送集 + 防倒退；`binding=` 把范围收窄到**操作人自己**的绑定）。没有链路（从没录到我的下家）⇒ 该单 `skipped` + 消费者那句「没有链路」的人话。 4. 对绑定跑**一次** `_run_push_outbox`（⛔ 不一单一轮：每轮都要建连 + 写健康列）。 5. 汇总 `{requested, pushed, skipped, failed, items:[{order_no, result, reason}]}`， `result ∈ done|skipped|failed`，`items` 顺序 = 入参顺序。 ## 失败口径（计划 §2 末条：**推送失败不挡接口**） - **预期失败**（上游拒 / 网络 / 鉴权）在引擎里已经落成台账 `failed` + 退避重试 ⇒ 这里如实计入 `failed`，HTTP 仍是 200 + `code 0`（页面照常显示「已推送 N 笔，跳过 M 笔」）。
    - **建连都没成**（`JidanpuError`：登录失败 / 网络超时 / 地址不合法）⇒ 这一轮一单都没出去，把既有的异常翻译原样放进逐单 `failed`，同样**不抛 500**。 - `MemberOutboxUnexpectedFailures`（逐事件边界跑完才抛的那种）同理：每条事件自己的结论已经落在台账上 ⇒ 按台账如实汇总，⛔ 不让「某一单没推成」升级成 500。 - 其余未预期异常照旧**响亮 500**（E-0010：那是接线坏了，不是业务失败）。 ## 越权面 = 0 行主只来自 `request.user`（`_enabled_binding_of`），归属判据用 `request.user`； ⛔ 请求体里塞 `user` / `binding` / `taker_id` 一律被忽略（序列化器没有这些字段）。
    """

    permission_classes = [IsAuthenticated, has_node(PUSH_NOW_NODE)]

    def post(self, request):
        params = JidanpuMemberPushNowSerializer(data=request.data)
        params.is_valid(raise_exception=True)  # 非法 ⇒ 统一处理器包成 1000
        order_nos = params.validated_data["order_nos"]
        action = params.validated_data["action"]
        binding = jidanpu_member._enabled_binding_of(request.user.pk)
        if binding is None:
            return api_response(
                data=None,
                msg=PUSH_NOW_NOT_BOUND_MESSAGE,
                code=ErrorCode.BUSINESS,
            )
        kind = (
            jidanpu_member.KIND_ASSIGN
            if action == PUSH_NOW_ACTION_RECORD
            else jidanpu_member.KIND_STATUS_OUT
        )
        outcomes: dict[str, tuple[str, str]] = {}
        queued: list[str] = []
        selected_penalty_events: dict[str, int] = {}
        for order_no in order_nos:
            perf = claimed_perf_of(request.user, order_no)
            if perf is None:
                # 不存在 / 不是他接的 / 已软删 **同一个出口**（`claimed_perf_of` 的既有口径，
                # ⛔ 不在这里分三句文案：那会变成一条可探测的信道）。
                outcomes[order_no] = (
                    PUSH_NOW_RESULT_SKIPPED,
                    PUSH_NOW_NOT_CLAIMED_REASON,
                )
                continue
            order = perf.order
            if action == PUSH_NOW_ACTION_RECORD:
                penalty_event = jidanpu_member.queue_manual_penalty_record(order, binding=binding)
                if penalty_event is None:
                    jidanpu_member.queue_assign(order, taker_id=request.user.pk)
                else:
                    selected_penalty_events[order_no] = penalty_event.pk
            elif not jidanpu_member.queue_status_out(
                order, manual=True, binding=binding
            ):
                # 没有链路 = 这张单从没录到我的下家 ⇒ 没有可改的远端单（终态跳过）。
                outcomes[order_no] = (
                    PUSH_NOW_RESULT_SKIPPED,
                    jidanpu_member.STATUS_NO_LINK_REASON,
                )
                continue
            queued.append(order_no)

        blocked_reason = ""
        if queued:
            try:
                blocked_reason = _run_push_outbox(binding).blocked_reason
            except jidanpu_member.MemberOutboxUnexpectedFailures:
                # 逐事件边界已经跑完：每一单的结论（failed + 人话）都落在台账上
                # ⇒ 下面按台账如实汇总（⛔ 不把「某一单没推成」升级成 500，计划 §2 末条）。
                blocked_reason = ""
            except JidanpuError as exc:
                # 连建连都没成（登录失败 / 网络超时 / 地址不合法）⇒ 这一轮一单都没推出去。
                # ⛔ 不抛 500：把人话放进逐单 `failed`（⛔ 不在这里另写一套上游文案）。
                _code, message = _upstream_error(exc)
                for order_no in queued:
                    outcomes[order_no] = (PUSH_NOW_RESULT_FAILED, message)
                queued = []
            if queued:
                latest = _latest_push_events(
                    binding, queued, kind, selected_penalty_events=selected_penalty_events,
                )
                for order_no in queued:
                    outcomes[order_no] = _push_outcome(
                        latest.get(order_no), blocked_reason
                    )

        items = [
            {
                "order_no": order_no,
                "result": outcomes[order_no][0],
                "reason": outcomes[order_no][1],
            }
            for order_no in order_nos
        ]
        return api_response(
            data={
                "requested": len(order_nos),
                "pushed": sum(
                    1 for row in items if row["result"] == PUSH_NOW_RESULT_DONE
                ),
                "skipped": sum(
                    1 for row in items if row["result"] == PUSH_NOW_RESULT_SKIPPED
                ),
                "failed": sum(
                    1 for row in items if row["result"] == PUSH_NOW_RESULT_FAILED
                ),
                "items": items,
            }
        )
