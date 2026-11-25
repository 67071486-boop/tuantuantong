"""「我的资料 → 功能设置」里的**上家鸡蛋铺绑定**端点 + **大厅镜像单接单**端点（spec §6.1 / §9.1 / §9.2 / §9.3）。 | 路径 | 方法 | 干什么 | |---|---|---| | `/api/business/member/upstream-jidanpu-binding/` | GET | 读**本人那一行** + 健康状态（⛔ 不回显密码） | | 同一路径 | PUT | 新建 / 覆盖**本人那一行**（写库与写操作日志同一个事务） | | `/api/business/member/upstream-jidanpu-binding/test-connection/` | POST | 跑四项检测、把人话结论回给页面（⛔ 不落库） | | `/api/business/member/upstream-jidanpu-sync-now/` | POST | 对本人名下的链路跑一轮状态 / 内容回拉（spec §9.3） | | `/api/business/hall/upstream/<pk>/claim/` | POST | 在我方大厅接一张**本线的镜像单**（spec §6.1；Task 11） | ⛔ **起（D-0256 / E-0255）**：绑定本身已是**租户多行**资源，「绑定上家系统设置」页的写入口已改走**行级端点**（`POST` / `PUT /api/business/upstream-bindings/jidanpu/[<pk>/]`，住在 `upstream_binding_views.py`，与本模块共用下面那份检测闸与落库+留痕实现）。上面前两条**为兼容保留**、语义一字不改（仍只作用「本人那一行」）； `test-connection` / `sync-now` 的行定位仍是 `filter(user=request.user).first()` （多行时取的是「本人名下未排序的第一行」——页面已无这两条的消费方，残差登记在 D-0256）。 ## ⛔ 这里**只有一种**对接方式：①式（账号 + 密码）——②式（官方开放平台 API）**已作废** > **2026-10-09 业务方裁定**（原话：「绑定上家这边我不需要啊 谁绑定上家是去用别人上家的 api 密钥的？」）： > 要绑上家，用的是**别人家**的鸡蛋铺账号 —— 你手里**不可能有别人的 API 密钥** ⇒ > 「②式」这种对接方式**只对「自己那套」有意义**（下家线），**上家这一侧不存在**这种用法。 ⇒ 本轮撤掉了上家这一侧的 ②式写入口与两条分流： | 落点 | 现在的样子 | |---|---| | PUT 的写字段 | ⛔ **不再接受** `mode` / `client_id` / `client_secret`（序列化器里没有这三个字段 ⇒ 塞进来一律被 DRF **静默忽略**，⛔ 不报错、⛔ 不改库） | | 响应 | ⛔ **不再回** `client_secret_set` / `callback_url` / `callback_url_reason` |
| PUT 的落库 | ⛔ **不再写** 0600 回调密钥文件（`jidanpu_openapi.ensure_callback_key` 那一步已撤） | | 「测试连接」 | **只有** `jidanpu_upstream.detect_binding` 那一支（按 `mode` 分流的那一支已撤） | | 「立即同步」 | **只有** `jidanpu_upstream.pull_statuses` 那一支（②式的「回调消费 + 兜底对账」那支已撤） | | 凭据判空 | **只有** `jidanpu.credentials_of`（地址 / 账号 / 密码） | ⚠️ 撤除**不丢任何真实数据**（已核实：两台生产机上②式上家绑定**一条都没有**）。 ⚠️ 模型的 9 列（`mode` / `client_id` / `client_secret` / `callback_*`）**没删**（删列要动共享引擎，风险更大、另开一轮）；`apps/business/jidanpu_openapi.py` 与共享引擎**整块保留**（下家线在用）。 ⚠️ ①式的**行为、文案、状态码一个字都没变**（回归判据在 `tests/test_jidanpu_upstream_api.py`）。 ## 越权面 = 0（spec §8.3）前四条端点**只作用于 `request.user` 那一行**：查询一律 `filter(user=request.user)`，序列化器里**没有** `user` 字段，⛔ 不接受任何请求来源的用户 / 租户参数（`sync-now` 连请求体都不读）⇒ 客服 A 的绑定，B 既读不到也改不到。接单那一条同口径：行主（`user_id`）由视图从 `request.user` 算出，⛔ **一个字节的请求体都不读** （塞 `user` / `sales_user` / 绑定 id 一律无效）；**游戏权限**这道数据卡点在视图层先过一次（照 P2 `remote_claim` 的同一道门，⛔ 不新造第二套）。 ⚠️ 反方向线的 `_SyncNowGate` / `_sync_now_key` 是**同一份实现**、跨线复用（⛔ 不写第二份），但**闸门实例是各自的**（两条线的「立即同步」不该互相挡）。 ## ⛔ 与另外两条鸡蛋铺线零关系（spec §4.1）本模块只读写 `JidanpuUpstream*` 三张表：⛔ 不碰租户级的 `JidanpuBinding` / `RemoteHallOrder`、⛔ 不碰反方向的 `JidanpuMemberBinding`（三套表的字段形状刻意不同，混进来就是串线）。⚠️ 接单端点**复用** P2 的两句用户文案与租约机制，但**入口是自建的** （spec B2：⛔ 不复用 `jidanpu_hall.claim_remote`——它绑死租户级单行绑定 + `assign_order`）。 ⛔ **改准（上面这一段是历史原文）**：它点名的**租户级**那两张表（`JidanpuBinding` / `RemoteHallOrder`）以及 `jidanpu_hall.claim_remote` 都已随租户级鸡蛋铺线
整条拆除删除 ⇒ 今天真正要防的是**本线 ↔ 下家代练员线**串线（`JidanpuMemberBinding` 一族）； ⚠️ 本模块的读写面与三条硬约束**一个字未改**。 ## 三条硬约束 - **密码只写不读**：序列化器里 `write_only`，响应里连 `password` 这个**键**都不出现（只回 `password_set` 布尔）；操作日志的句子只说**站点地址**（去协议头），⛔ 不说账号 / 密码。 - **检测不过 ⇒ 不许启用**（spec §9.2 / A17 同族）：`enabled=True` 时**先**同步跑一次 `jidanpu_upstream.detect_binding`，不过就**一行都不落**并把缺项逐条点名。 - **`atomic()` 块内不许 `return`**（E-0022）：可预期的失败（检测不过 / 配置不全）一律在**进事务之前**返回——那时一行都还没写。 ## 三档（停用 / 换绑 / 删号；spec §8.4） - **换绑**（改了 `account` 或 `jidanpu_base_url`）⇒ 把在飞的**本线 outbox 事件** （`pending` / `failed`，`payload.upstream_binding_id` 指向这一行）收敛成 **`skipped`** （作废终态：⛔ 不再重试、⛔ 不挡同键的新触发）+ 人话原因；⚠️ ⛔ **不许收敛成 `failed`**：那是「还会重试」的态 ⇒「停用 → 再启用」会拿**新凭据**去重试旧事件，正是 §8.4 禁止的事。 ⚠️ **只碰 outbox**（`direction="out"` + `OUTBOX_KINDS`）：`direction="in"` 的台账行哪怕状态列也叫 `failed`，也是**审计**而不是待办 —— 改它就是把历史改成假话。 ⛔ 链路行**不删**（历史事实），也⛔ 不拿新凭据去改旧单。 - **停用**（`enabled=False`）⇒ 只是把开关关掉：⛔ 不收敛在飞事件（凭据没变，再启用该继续）、 ⛔ **不发明任何「转给谁」的提示 / 告警**（业务方对 A17 的答复是「不用管」，spec §16.3）。镜像行的 `owner_binding` 由**下一轮** `sync_hall` 按 A2 的地址级去重轮到下一份启用绑定。 - **删号** ⇒ `on_delete` 三档刻意不同（绑定 `CASCADE` / 链路随绑定 `CASCADE` / 镜像行 `SET_NULL` / 台账无 FK）⇒ **台账与镜像行活着**。端点不需要为它写任何代码。
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.views import APIView

from apps.accounts.serializers import INT4_MAX
from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business import jidanpu, jidanpu_common, jidanpu_upstream

#: 上游异常的「异常类型 → 人话 + 业务码」翻译**复用**既有实现（现住在中性宿主 `jidanpu_view_common`——⛔ 原宿主 `jidanpu_views` 已随租户级那条线整份删除；判据顺序
#: 写死在里面：`CaptchaRequiredError` / `SessionExpiredError` 都排在 `AuthError` 之前）——⛔ 不再抄一份。
from apps.business.api.jidanpu_view_common import _upstream_error
#: 「立即同步」的会话级节流**复用**反方向线的**同一份实现**（spec §9.3 明文「沿用形状」；
#: ⛔ 不写第二份）。⚠️ 只复用实现、**不复用实例**：下面自建 `_SYNC_NOW_GATE`。
from apps.business.api.jidanpu_member_views import _SyncNowGate, _sync_now_key
#: 接单端点的**响应行**（转正出来的那张我方订单）与另两条接单入口**同一个序列化器**。
from apps.business.api.serializers import OrderSerializer, UpstreamGameRulesSerializer
from apps.business.claim import ClaimError
from apps.business.claim_channel import channel_for
from apps.business.commission import CommissionError
from apps.business.deposit_gate import deposit_enforced_for
from apps.business.game_scope import game_scope
from apps.business.models import (
    JidanpuSyncEvent,
    JidanpuUpstreamBinding,
    JidanpuUpstreamHallOrder,
    JidanpuUpstreamOrderLink,
    OrderSource,
)
from apps.business.performance_amount import AmountError
from apps.business.upstream_game_rules import upstream_price_profile_changed
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.scoping import scope_queryset
from apps.rbac.permissions import has_node
from jidanpu.errors import JidanpuError

#: 操作日志的 `target_kind`（⛔ ≤16 字符，超了会被 `record()` 静默截断）。
#: ⚠️ 与另两条鸡蛋铺线**刻意不同名**：三张表零关系，日志里也要分得清。
JIDANPU_UPSTREAM_BINDING_TARGET_KIND = "jidanpu_upstream"

#: 在飞（还会被重试 / 还会产出动作）的事件状态：只有这两档需要收敛。
#: ⚠️ 收敛的**终态**是 `skipped`（作废、不再重试），⛔ 不是 `failed`（那会继续重试）——
#: `skipped` 不在 `run_outbox` 的取活集合与 `_queue_out_event` 的去重集合里。
_IN_FLIGHT_STATUSES = (JidanpuSyncEvent.Status.PENDING, JidanpuSyncEvent.Status.FAILED)

#: 凭据不全时的人话（与两条既有鸡蛋铺线的「还没配置」同一口径）。
NOT_CONFIGURED_MESSAGE = "还没配置上家鸡蛋铺的地址 / 账号 / 密码，无法启用"

#: PUT **更新**那一支能写的列：凭据 / 配置 / 开关 / `checked_at`，外加 `updated_at` （`auto_now` 只在它进 `update_fields` 时才会被刷新）。 ⛔ 四个**健康列**（`last_run_at` / `last_ok_at` / `last_error` / `last_error_at`）不在其中：它们是同步引擎的写入方，整行 `save()` 会把并发写进去的值覆盖回这次请求开始时的旧值。 ⛔ 别把这份清单放宽回整行 `save()`。 ⛔ ②式那三格（`mode` / `client_id` / `client_secret`）已撤掉（见模块 docstring）。
#: ⭐ `order_source`（D-0261，审阅 A）：**必须**在这份清单里——它是行级 PUT 更新分支「能落库的列清单」，漏了它就是「校验通过、内存有值、落库被静默丢弃」（E-0027 家族）。 ⭐ `name`同理：人填的「上家系统名称」是**可回显**的可见格（编辑弹窗整表单重发）⇒ 必须进收窄写入清单，漏了它 = 改名永远不落库（同 E-0027 家族）。
_PUT_WRITE_FIELDS = (
    "jidanpu_base_url", "account", "account_name", "name", "password", "enabled",
    "hall_price_percent", "hall_poll_seconds", "my_orders_poll_seconds",
    "order_source",
    "checked_at", "updated_at",
)

#: 「空串 = 不修改」的格（照 `password` 的既有口径）。
#: 理由：这一格在页面上**不回显**，留空只能表示「不动它」；
#: 若把空串当成「清掉」，一次没带值的 PUT 就会**静默抹掉**用户配好的密码（E-0027 家族）。
_BLANK_MEANS_KEEP = frozenset({"password"})


def _upstream_binding_name(binding) -> str:
    """审计句子里显示的名字：**站点地址**（去协议头）。

    ⛔ 不用账号（它是登录名、属于凭据标识），⛔ 更不用密码（spec §8.3 的红线）。
    地址为空时回落到 `#<主键>`（配置校验要求它非空，这条只为「shell 写库」那种路径兜底）。
    """
    host = binding.jidanpu_base_url or ""
    for prefix in ("https://", "http://"):
        host = host.removeprefix(prefix)
    return host.rstrip("/") or f"#{binding.pk}"


def _credentials_missing(binding) -> bool:
    """地址 / 账号 / 密码**任一为空** ⇒ 还没法启用（先挡在检测之前，给一句准确的话）。

    ⚠️ **判据的唯一出处**是 `jidanpu.credentials_of`（它还会再 `strip` 一次，
    覆盖「历史数据 / 直接 shell 写库」这类绕过序列化器的路径）。
    """
    return jidanpu.credentials_of(binding) is None


#: 上家绑定的**凭据域**（D-0261 审阅 B 的闸条件用它；与 §8.4 换绑判据的同一组列；
#: ⛔ 不含 `password`——它走 `_BLANK_MEANS_KEEP` 的「空串 = 不改」语义，单独判）。
#: ⚠️ 顺序与行级 PUT 抓下的 `previous_credentials` 快照 `(地址, 账号)` 一一对应。
UPSTREAM_BINDING_CREDENTIAL_FIELDS = ("jidanpu_base_url", "account")


def _row_put_needs_detection(validated_data, *, previous_credentials, was_enabled: bool,
                             now_enabled: bool) -> bool:
    """**行级编辑**的检测闸条件（D-0261 审阅 B 的裁定 + 执行期实况修订）。 - **审阅 B 的原口径**：`validated_data 与凭据域 {jidanpu_base_url, account, password} 相交 或 enabled 翻真`。 - ⚠️ **执行期实况修订（按「实况优先 + 汇报主线」处理）**：「相交」按**键在 validated_data** 判会让免检测分支**永不可达**——`jidanpu_base_url` / `account` 是序列化器**必填格**，任何能通过校验的 PUT 都带着它们（先红实测：
    `1000 该字段是必填项`）⇒ 那条免检测路是死代码。故这里把「相交」落成 **「凭据域被真改写」**：地址 / 账号的新值与旧值不同，或密码**非空**（空串 = 「不改」的既有哨兵，`_BLANK_MEANS_KEEP`）。页面弹窗「只改来源」的保存（原值重发地址 / 账号 + 密码留空）因此真的免检测，开放点 3 的意图成立。 - **enabled 翻真必须单列**：只判「凭据被改写」会漏放「停用行 + 改来源 + 启用」的组合——启用必须过检测（spec §9.2）。
    - ⚠️ **两个前置快照必须在 `pending_instance()` 改内存之前抓**（与 `previous_credentials` 同一处口径）：那之后 `row` 与合并结果**是同一个对象**，「旧值」就再也读不到了（执行期实测踩过：合并后读 `row.enabled` 恒为新值，「停用行启用」的翻真判不出来）。 ⚠️ 只服务**行级编辑**（`upstream_binding_views.py` 的 PUT）；member PUT 与行级创建照旧「`enabled` 即检测」，不走本函数。
    """
    old_base_url, old_account = previous_credentials
    rewrote_credentials = (
        ("jidanpu_base_url" in validated_data
         and validated_data["jidanpu_base_url"] != old_base_url)
        or ("account" in validated_data and validated_data["account"] != old_account)
        or bool(validated_data.get("password"))
    )
    return now_enabled and (rewrote_credentials or not was_enabled)


class JidanpuUpstreamBindingSerializer(serializers.ModelSerializer):
    """上家鸡蛋铺个人绑定（一人一份）。密码**只写不读**；留空 = 不改。 ⚠️ **没有 `user` 字段**是承重的：它不在 `fields` 里 ⇒ 请求体里塞 `user` 会被 DRF 静默忽略，行主只能由视图从 `request.user` 传进 `pending_instance()`。 ⚠️ `account_name`（上家账号显示名）是**只读**的：它的写入方是后续期的同步引擎 / 人工运维， ⛔ 不是这一屏（与反方向线的同名列同口径）。 ⭐ `name`（上家系统名称，2026-10-29 业务方指令）是**人填的**：同一租户绑多家上家鸡蛋铺时
    用来区分（选填；空串 = 清除——它是**可回显**的可见格，与「不回显 ⇒ 空串 = 不改」的 `password` 不同一套语义）。⚠️ 与只读的 `account_name` 是两回事，见模型那一列的注释。 ## ⛔ ②式那几格已撤掉（见模块 docstring 的裁定块） `mode` / `client_id` / `client_secret` 三格**不再进 `fields`** ⇒ 请求体里塞它们会被 DRF **静默忽略**（⛔ 不报错、⛔ 不改库，口径见 `tests/test_jidanpu_upstream_api.py` 的那条用例）； `client_secret_set` / `callback_url` / `callback_url_reason` 三个只读键也**不再回**。
    """

    #: 密码的**只读替身**：只回答「配没配」，不回答「配的是什么」。
    password_set = serializers.SerializerMethodField()
    password = serializers.CharField(
        max_length=255, write_only=True, required=False, allow_blank=True,
        help_text="上家鸡蛋铺账号的密码（只写不读；留空 = 不改）",
    )
    #: 两个字符列显式声明 `allow_blank=True`，好让**本模块的**校验器给出点名到字段的中文文案。
    jidanpu_base_url = serializers.CharField(max_length=255, allow_blank=True)
    account = serializers.CharField(max_length=150, allow_blank=True)
    #: 上家价的勾兑百分比（业务方）：我方**发单价** = 上家 `recFee` × 本值 / 100。范围 **1–100**：⛔ 0 等于把一张**免费单**发进我方大厅（与 `LAUNCH_NO_PRICE_REASON` 同族）， ⛔ >100 等于我方倒贴（发单价高过客户价）⇒ 两头都在入口挡掉。
    #: ⚠️ 显式声明的 `IntegerField` **不继承**模型字段的校验器 ⇒ 漏了范围会让越界值落到 PostgreSQL `smallint` 列上抛 `DataError` → 500（E-0010）；模型层那对校验器（`full_clean()` 才跑）是**第二道**，⛔ 不能当入口用。空白态 `0` 的兜底（回落 100）在**读侧**
    #: `jidanpu_upstream.upstream_hall_price` 里，⛔ 不是这里的入口语义（同两个轮询间隔的口径）。
    hall_price_percent = serializers.IntegerField(
        min_value=1, max_value=100, required=False,
        error_messages={
            "min_value": "发单价百分比不能小于 1（0 等于把一张免费单发进大厅）",
            "max_value": "发单价百分比不能大于 100（超过 100 等于我方倒贴）",
            "invalid": "发单价百分比必须是 1–100 的整数",
        },
    )
    #: 两个轮询间隔（spec §10 / T10 已定 300 / 60）：下界 1 —— **0 秒没有意义**， ⛔ 别把它读成「用默认值」（0 = 配置写坏，是闸门那一侧的兜底，不是这里的入口语义）。显式声明的 `IntegerField` **不继承**模型字段的校验器 ⇒ 漏了上界会让越界值落到
    #: PostgreSQL `int4` 列上抛 `DataError` → 500（E-0010）。 ⚠️ 防误读（D-0363）：上面那句「已定 300 / 60」是对冻结 spec 的过去时引用——默认值现由 `UPSTREAM_HALL_POLL_SECONDS` 管辖，已改定 30，见 models.py；本处只管入口校验。
    game_rules = UpstreamGameRulesSerializer(required=False, allow_null=True)
    hall_poll_seconds = serializers.IntegerField(
        min_value=1, max_value=INT4_MAX, required=False,
        error_messages={
            "min_value": "上家大厅拉取间隔必须不小于 1 秒（0 秒没有意义）",
            "max_value": "上家大厅拉取间隔超出上限",
            "invalid": "上家大厅拉取间隔必须是整数秒",
        },
    )
    my_orders_poll_seconds = serializers.IntegerField(
        min_value=1, max_value=INT4_MAX, required=False,
        error_messages={
            "min_value": "「我的订单」拉取间隔必须不小于 1 秒（0 秒没有意义）",
            "max_value": "「我的订单」拉取间隔超出上限",
            "invalid": "「我的订单」拉取间隔必须是整数秒",
        },
    )
    #: ⭐ D-0261：订单来源的**读形状** = `{id, 名字}`（审阅 F：GET 用方法字段，不是裸主键
    #: ——弹窗下拉要的是「字典项名」）；未配置 ⇒ `null`。
    order_source = serializers.SerializerMethodField()
    #: ⭐ D-0261：订单来源的**写入口**（审阅 F：DRF 对模型 FK 默认收 `order_source` 键；显式声明成 `order_source_id` 才与「字典项主键」的语义对齐）。 - `required=False`：省略 = 不改（`pending_instance` 只合并进来的键）；
    #: - `allow_null=True`：`null` = **清除**（计划 §4 的口径）； - `write_only=True`：响应里只回上面的 `{id, 名字}`，⛔ 不再重复回一个裸主键。
    order_source_id = serializers.PrimaryKeyRelatedField(
        source="order_source", queryset=OrderSource.objects.all(),
        required=False, allow_null=True, write_only=True,
        error_messages={
            "does_not_exist": "这个订单来源不存在（可能已被删除）",
            "incorrect_type": "订单来源必须是字典项的主键（整数）",
        },
    )

    class Meta:
        model = JidanpuUpstreamBinding
        #: ⚠️ **不是** `__all__`：这张表的健康列（`callback_watermark_at` 那类**内部状态**） ⛔ 不进序列化器、⛔ 不上页面（spec §13.3-C8，照 `status_pull_at` 的既有先例）； `callback_code` 也**不单独回**。
        #: ⛔ ②式那六格（`mode` / `client_id` / `client_secret` / `client_secret_set` / `callback_url` / `callback_url_reason`）已撤掉（见类 docstring）。
        fields = [
            "id", "jidanpu_base_url", "account", "account_name", "name", "password",
            "password_set", "enabled", "hall_price_percent", "game_rules",
            "hall_poll_seconds", "my_orders_poll_seconds",
            "order_source", "order_source_id",
            "last_run_at", "last_ok_at", "last_error", "last_error_at", "checked_at",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "account_name", "last_run_at", "last_ok_at", "last_error",
            "last_error_at", "checked_at", "created_at", "updated_at",
        ]

    def get_password_set(self, obj) -> bool:
        return bool(obj.password)

    def get_order_source(self, obj) -> dict | None:
        """⭐ D-0261（审阅 F）：`{id, 名字}`（弹窗回填两样都要）；未配置 ⇒ `null`。"""
        source = obj.order_source
        if source is None:
            return None
        return {"id": source.pk, "name": source.name}

    def validate_jidanpu_base_url(self, value):
        """必须是 `http(s)://` 开头，并按 SDK 的口径归一化（`strip().rstrip("/")`）——
        不归一化的话，同一个地址会以两种形态进库，`(地址, 账号)` 唯一约束就形同虚设。
        """
        cleaned = (value or "").strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise serializers.ValidationError("鸡蛋铺地址必须以 http:// 或 https:// 开头")
        return cleaned

    def validate_account(self, value):
        cleaned = (value or "").strip()
        if not cleaned:
            raise serializers.ValidationError("鸡蛋铺账号不能为空")
        return cleaned

    def validate_password(self, value):
        """首尾空白 / 换行是复制粘贴的常见形态，而带换行的密码只会得到一句
        「账号或密码错误」（看着像账号错了）⇒ 入口处 strip（与另两条鸡蛋铺线同口径）。
        """
        return (value or "").strip()

    def pending_instance(self, user):
        """按校验后的数据算出「落库后会是什么」的**未落库**实例（行主只来自 `user`）。 ⚠️ 为什么不用 DRF 的标准写入路径：`ModelSerializer.update()` 末尾自带 `instance.save()`，而 `save(commit=False)` 在本仓的 DRF 版本里**没有** `commit`
        参数（它必然落库）——本线要求「检测不过 ⇒ 一行都不落」⇒ 合并必须**只改内存**。 ⚠️ 密码的「空串 = 不改」语义：页面上的密码框**回显不了**，留空只能表示「不动它」。 ⚠️ 行主由调用方（视图）从 `request.user` 传入：序列化器**没有** `user` 字段。
        """
        if self.instance is None:
            return JidanpuUpstreamBinding(user=user, **self.validated_data)
        for field, value in self.validated_data.items():
            if field in _BLANK_MEANS_KEEP and not value:
                continue  # 空串 = 不改（旧值留在 instance 上）
            setattr(self.instance, field, value)
        return self.instance


class MemberJidanpuUpstreamBindingSerializer(JidanpuUpstreamBindingSerializer):
    """member **兼容端点**专用的收窄序列化器（D-0261 审阅 D 的显式动作）。 member GET/PUT 与行级三支**共用**基类 ⇒ 「订单来源」两格加进基类后，兼容路径会默认带上（GET 回它、PUT 收它）——而兼容路径的口径是「**语义一字不改**」（D-0256、计划 §4
    「既有 member PUT 兼容路径不带此字段」）⇒ 这里**显式剔除**，⛔ 不是「页面不发就行」： DRF 只对**必填**的声明字段要求出现在 `Meta.fields` 里（两格都 `read_only` / `required=False`）⇒ 子类收窄 `fields` 即可整格摘掉，读与写两个方向同时消失。
    ⭐：`name`（上家系统名称）**同样剔除**（照 D-0261 对「订单来源」的同一处置）：它是行级世界的新格（「绑定上家系统设置」弹窗），兼容路径的形状照旧一字不改。
    """

    class Meta(JidanpuUpstreamBindingSerializer.Meta):
        fields = [
            name for name in JidanpuUpstreamBindingSerializer.Meta.fields
            if name not in ("order_source", "order_source_id", "name", "game_rules")
        ]


def _detect_gate_response(binding):
    """**启用前的检测闸**（`enabled=True` 时先同步跑一次绑定检测）。 ⭐ 本函数是「检测不过 ⇒ 不许启用」这一判据的**唯一出处**（spec §9.2）：既有按用户的 member PUT 与行级创建/编辑（D-0256）共用它，⛔ 不抄第二份。
    失败 ⇒ 返回**业务失败响应**（凭据不全 / 上游异常翻译 / 缺项点名）；通过 ⇒ `None`。 ⚠️ 检测读的是「落库后会是什么」的**未落库**实例：它读内存、⛔ 不落库 ⇒ 下面几条可预期失败都发生在**进事务之前**（E-0022 的边界不在场）。
    """
    if _credentials_missing(binding):
        return api_response(
            data=None,
            msg=NOT_CONFIGURED_MESSAGE,
            code=ErrorCode.PLATFORM_NOT_CONFIGURED,
        )
    # ⚠️ ①式的调用序列、异常翻译与文案**一个字都没变**（②式那一支已撤掉）。
    try:
        report = jidanpu_upstream.detect_binding(binding)
    except JidanpuError as exc:
        code, message = _upstream_error(exc)
        return api_response(data=None, msg=message, code=code)
    if not report.ok:
        # 检测不过 ⇒ 不许启用（spec §9.2）：⛔ 一行都不落，`enabled` 保持原值。
        return api_response(
            data=None,
            msg="；".join(report.problems),
            code=ErrorCode.PLATFORM_NOT_CONFIGURED,
        )
    return None


def _persist_binding_with_audit(*, binding, action, actor, actor_id, detection_ran: bool,
                                price_fields=()):
    """保存绑定时一起隔离旧关联，并在同一事务留痕。"""
    rebinding = disabled = False
    with transaction.atomic():
        if detection_ran:
            binding.checked_at = timezone.now()
        if binding._state.adding:
            binding.save()
        else:
            previous = JidanpuUpstreamBinding.objects.select_for_update().get(pk=binding.pk)
            if previous.identity_revision != binding.identity_revision:
                raise serializers.ValidationError("上家店铺或账号已被其他操作更换，请刷新后再保存")
            rebinding = (previous.jidanpu_base_url, previous.account) != (
                binding.jidanpu_base_url, binding.account,
            )
            disabled = previous.enabled and not binding.enabled
            update_fields = _PUT_WRITE_FIELDS
            if rebinding:
                binding.identity_revision = previous.identity_revision + 1
                binding.my_orders_progress = None
                update_fields = (*update_fields, "identity_revision", "my_orders_progress")
                if not detection_ran:
                    binding.checked_at = None
            if "hall_price_percent" not in price_fields:
                update_fields = tuple(field for field in update_fields if field != "hall_price_percent")
            if price_fields:
                if "hall_price_percent" not in price_fields:
                    binding.hall_price_percent = previous.hall_price_percent
                if "game_rules" not in price_fields:
                    binding.game_rules = previous.game_rules
                else:
                    update_fields = (*update_fields, "game_rules")
                if upstream_price_profile_changed(previous.game_rules, binding.game_rules) or (
                    previous.game_rules is not None
                    and previous.hall_price_percent != binding.hall_price_percent
                ):
                    JidanpuUpstreamHallOrder.objects.filter(
                        owner_binding=previous, hall_price_percent_at_sync__isnull=True,
                        game_id__isnull=False,
                    ).exclude(status=JidanpuUpstreamHallOrder.Status.PROMOTED).update(
                        hall_price_percent_at_sync=previous.hall_price_percent or 100,
                    )
            binding.save(update_fields=update_fields)
        if rebinding:
            JidanpuUpstreamOrderLink.objects.filter(binding=binding).update(retired_at=timezone.now())
            _converge_in_flight_events(binding)
        if rebinding or disabled:
            # 停用/换绑生效点（僵尸窗治理⑤）：必须在 save 之后跑——接管判据读的是
            # 本次落库后的 enabled / 地址（同事务同连接，读得到刚写的值）。
            jidanpu_upstream.unlist_orphan_hall_rows(binding)
        record(
            log_type=ACTION_LOG_TYPES[action],
            action=action,
            actor=actor,
            actor_id=actor_id,
            target_kind=JIDANPU_UPSTREAM_BINDING_TARGET_KIND,
            target_id=binding.pk,
            target_name=_upstream_binding_name(binding),
        )
    return rebinding


class JidanpuUpstreamBindingView(APIView):
    """`/api/business/member/upstream-jidanpu-binding/`：本人那一行的单行资源。 ⛔ **起本端点为兼容保留（D-0256）**：绑定已是**租户多行**资源（E-0255），「绑定上家系统设置」页已改走行级端点（`POST` / `PUT /api/business/upstream-bindings/jidanpu/[<pk>/]`）；本端点**语义一字不改**（仍是「本人那一行」的单行资源）地活着，页面不再消费它。 ⚠️ 只定义 `get` / `put` ⇒ `POST` / `PATCH` / `DELETE` 由框架挡成 **405**
    （与两条既有鸡蛋铺线同形；`POST` 尤其不能出现在这条路径上——`test-connection` 住在它的子路径下，两个都接 POST 会让「POST 它到底干什么」变成路由层的歧义）。 ⚠️ 权限走**默认**（`IsAuthenticated`）：本线**不新增 RBAC 节点**（本人自助， spec §9.3 的「待实现评审」口径）。
    """

    def get(self, request):
        binding = JidanpuUpstreamBinding.objects.filter(user=request.user).first()
        # ⭐ D-0261（审阅 D）：兼容路径用**收窄**序列化器——响应里没有「订单来源」两格。
        data = None if binding is None else MemberJidanpuUpstreamBindingSerializer(binding).data
        return api_response(data=data)

    def put(self, request):
        existing = JidanpuUpstreamBinding.objects.filter(user=request.user).first()
        # ⭐ D-0261（审阅 D）：兼容路径用**收窄**序列化器——请求体里塞 `order_source` /
        # `order_source_id` 一律被静默忽略（判据钉 ⑤），语义与引入这两格之前一字不差。
        serializer = MemberJidanpuUpstreamBindingSerializer(existing, data=request.data)
        serializer.is_valid(raise_exception=True)  # 失败 ⇒ 统一处理器包成 1000

        # 「落库后会是什么」——一个**未落库**的实例（空密码已按「不改」合并）。
        binding = serializer.pending_instance(request.user)

        if binding.enabled:
            gate = _detect_gate_response(binding)
            if gate is not None:
                return gate

        action = (
            "system.jidanpu_upstream_binding_create"
            if existing is None
            else "system.jidanpu_upstream_binding_update"
        )
        _persist_binding_with_audit(
            binding=binding,
            action=action,
            actor=request.user.get_username(),
            actor_id=request.user.pk,
            # member 路径照旧「enabled 即检测」⇒ 盖戳条件与 D-0261 之前一字不差。
            detection_ran=binding.enabled,
            price_fields=serializer.validated_data.keys() & {"hall_price_percent"},
        )
        return api_response(data=MemberJidanpuUpstreamBindingSerializer(binding).data)


def _converge_in_flight_events(binding) -> int:
    """作废旧身份的发送待办与排队中的打手拉图请求，保留历史台账。"""
    return JidanpuSyncEvent.objects.filter(
        Q(direction="out", kind__in=jidanpu_upstream.OUTBOX_KINDS, status__in=_IN_FLIGHT_STATUSES)
        | Q(
            direction="in", kind=jidanpu_upstream.KIND_PULL_PICS_IN,
            payload__trigger=jidanpu_upstream.PULL_PICS_TRIGGER_APP,
            status=JidanpuSyncEvent.Status.PENDING,
        ),
        payload__upstream_binding_id=binding.pk,
    ).update(
        status=JidanpuSyncEvent.Status.SKIPPED,
        reason=jidanpu_upstream.REBINDED_EVENT_REASON,
        processed_at=timezone.now(),
    )


class JidanpuUpstreamTestConnectionView(APIView):
    """`POST /api/business/member/upstream-jidanpu-binding/test-connection/`（spec §9.2）。 ⚠️ **只读上游、不落库**（登记在 `operation_log_ledger.DELIBERATELY_NOT_LOGGED` 里）：它不改配置，也不写 `checked_at` / `last_error`——那两个列的写入方是 PUT 的启用那一刻与后续期的同步引擎。 ⚠️ **一个字节的请求体都不读**（页面那一侧也是空体 POST）：凭据只从本人那一行取。 ## ⛔ 只有①式那一支（②式按 `mode` 分流的那一支已撤掉）
    | 方式 | 走什么 | 出参 | |---|---|---| | ①式（`password`，**现在唯一的方式**） | `jidanpu_upstream.detect_binding` | `{"ok": bool, "messages": [人话, …]}` | | ~~②式（`openapi`）~~ | ⛔ 已作废（见模块 docstring 的裁定块） | —— | **出参形状**：`{"ok": bool, "messages": [人话, …]}`，信封 `code` 恒为 0 （请求本身成功了；「检测没过」是一个**正常的结论**）。⛔ 没绑定 / 凭据不全时才回业务失败码。
    """

    def post(self, request):
        binding = JidanpuUpstreamBinding.objects.filter(user=request.user).first()
        if binding is None:
            return api_response(
                data=None,
                msg="还没配置上家鸡蛋铺的绑定，无法检测",
                code=ErrorCode.PLATFORM_NOT_CONFIGURED,
            )
        if _credentials_missing(binding):
            # 与 PUT 的启用路径同一句话：「缺项」说「还没配置」，⛔ 不说成「不合法」。
            return api_response(
                data=None,
                msg=NOT_CONFIGURED_MESSAGE,
                code=ErrorCode.PLATFORM_NOT_CONFIGURED,
            )
        # ── ①式：既有行为**一字不变** ──
        try:
            report = jidanpu_upstream.detect_binding(binding)
        except JidanpuError as exc:
            code, message = _upstream_error(exc)
            return api_response(data=None, msg=message, code=code)
        return api_response(data={"ok": report.ok, "messages": list(report.problems)})


# ══「立即同步」端点（spec §9.3）══════════════════════════════════════════════════ 作用：**只同步自己那一行**名下的单。⚠️ 只有①式那一支（②式的「回调消费 + 兜底对账」       那一支已随②式撤掉，见模块 docstring 的裁定块）：
#       · 与 §5.3 的慢路**同一段代码**（`jidanpu_upstream.pull_statuses`），⛔ 不写第二份。频控：**复用**反方向线那只闸门（同会话最小间隔 + 在途互斥 + 失败退避），       ⚠️ 但**实例各自一个**：两条线的「立即同步」不该互相挡。

SYNC_NOW_NOT_CONFIGURED_MESSAGE = "还没配置上家鸡蛋铺的绑定，无法同步"
SYNC_NOW_DISABLED_MESSAGE = "上家鸡蛋铺的同步还没启用，无法同步"

#: 本轮**有失败行**时回给页面的人话（⛔ 别谎报成功）。为什么必须有它：本线「立即同步」**不写健康列**（已裁定口径）⇒ 这类**不抛异常**的预期失败（远端一行都没有 / `orderSta` 为空 / 状态机拒绝）旧实现回的是 `code 0 + {"applied": 0}`，
#: 用户看到的是「成功但什么都没发生」；而闸门也因为「没失败」而不退避 ⇒ 连点能一直真打上游。判据落在**本轮结果**（`PullRoundOutcome.failed`）上：⛔ 不靠台账里有没有 `failed` 行（`note_once` 按原因去重，第二轮回写不出新行 ⇒ 退避阶梯只生效一次）。
#: ⚠️ 措辞用「回拉失败」而不是「未能拉取」：`failed` 里也含「上家给了认不出的状态」那一档（它按兜底写了状态、但确实留了一条 `failed` 台账）——「回拉失败」对两档都成立。
SYNC_NOW_FAILED_ROWS_MESSAGE = "有 {count} 条回拉失败，请稍后重试"

#: 本进程的**唯一**闸门（⚠️ 与 `jidanpu_member_views._SYNC_NOW_GATE` **不是**同一个实例）。
#: ⚠️ 测试会换一个实例（状态是进程内的，跨用例串味只会造成假红）。
_SYNC_NOW_GATE = _SyncNowGate()


class JidanpuUpstreamSyncNowView(APIView):
    """`POST /api/business/member/upstream-jidanpu-sync-now/`（spec §9.3 的「立即同步」）。 ⛔ **一个字节的请求体都不读**：行主只来自 `request.user`（越权面 = 0）。 ⚠️ **与慢路同一段代码**：调的是 `jidanpu_upstream.pull_statuses` （Task 13 的调度每轮调的是同一个函数）⇒ ⛔ 没有第二份实现会跟它漂移。 ⛔ ②式那一支（「回调消费一轮 + 兜底对账一轮」）已撤掉（见模块 docstring）。 ⚠️ 频控的拒绝是**业务失败码**（HTTP 仍是 200）：它是正常的「等一下再点」。
    ⚠️ 这一跳**会真的改我方订单状态**：每一张写回都由 `apply_transition` + 一条 `order.transition` 审计留痕（操作人 = 机器名）。 ⚠️ **失败了要说出来**：本线不写健康列（已裁定口径）⇒ 「这一轮失败了没有」只能看 `PullRoundOutcome.failed`（**不抛异常**的预期失败也在里面）。有失败 ⇒ ① 让闸门按阶梯退避、 ② 信封回业务失败码 + 「有 N 条未能拉取」（⛔ 不许 code 0 一片空白：那是「成功但什么都没发生」）。
    """

    def post(self, request):
        binding = JidanpuUpstreamBinding.objects.filter(user=request.user).first()
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
        try:
            # ── ①式：既有行为**一字不变** ──
            with jidanpu.open_client_with_http(binding) as (client, _http):
                outcome = jidanpu_upstream.pull_statuses(
                    binding=binding, client=client, now=timezone.now(), probe_clock=timezone.now,
                )
            #: 「这一轮失败了没有」的**唯一**判据在本轮结果里（⛔ 不是异常、⛔ 不是健康列）：
            #: 远端一行都没有 / 认不出的 token 这类失败**不抛异常**，只看 `applied` 看不出来。
            failed = outcome.failed > 0
        except JidanpuError as exc:
            # 预期内的上游失败（读不到 / 拉不全 / 配置不合法）⇒ 翻译成人话 + 业务码；
            # 退避要**记下来**（否则上游持续故障时页面可以连点把它刷爆）。
            failed = True
            code, message = _upstream_error(exc)
            return api_response(data=None, msg=message, code=code)
        except Exception:
            # 未预期错误照旧响亮上行（⛔ 不伪装成业务结果，E-0010）；退避也要记。
            failed = True
            raise
        finally:
            _SYNC_NOW_GATE.release(key, failed=failed)
        if outcome.failed:
            return api_response(
                data={"applied": outcome.applied, "failed": outcome.failed},
                msg=SYNC_NOW_FAILED_ROWS_MESSAGE.format(count=outcome.failed),
                code=ErrorCode.BUSINESS,
            )
        return api_response(data={"applied": outcome.applied})


# ══ 大厅镜像单接单端点（spec §6.1 / §11-19 / §11-29）═════════════════════ 路径：`POST /api/business/hall/upstream/<pk>/claim/`（计划的 Files 逐字）。 ⚠️ 与 P2 的 `hall/remote/<source_system>/<remote_order_id>/claim/` **同族但不同源**：
#    那条走租户级单行绑定 + `assign_order`（spec B2 明确**不复用**）；本线走自建的三张表。 ⚠️ 权限：与原生接单 / P2 镜像接单**同一个**节点门 `grab_hall.take_order`（spec §6.1 末段）。


class JidanpuUpstreamHallClaimView(APIView):
    """在我方大厅接一张**本线**的上家镜像单（spec §6.1 的 ①–⑥，领域层在 `jidanpu_upstream.claim_upstream_mirror`）。 ## 越权面 = 0（spec §8.3 / §11-19） - **行主 / 归属只来自认证态与事件**：`user_id` 取 `request.user`；归属取镜像行 `owner_binding`（= 排队那一刻的**事件绑定**，spec §15.5 的改准块）。 ⛔ **一个字节的请求体都不读** ⇒ 塞 `user` / `sales_user` / `taker_id` / 绑定 id **一律被忽略**（判据见 `tests/test_jidanpu_upstream_hall.py`）。 - **游戏权限**这道数据卡点**在写侧也必须有**（照 P2 `remote_claim` 的修复波 1/C1）：读侧的大厅列表用 `scope_queryset(..., game_scope("game_id"))` 收窄，写侧漏了它就会
    **越权接单 + 越权计酬**（转正会建出该游戏的真订单并写 MAIN 绩效）。 ⚠️ 收窄必须在**调领域层之前**做：领域层按 `pk` 取行、不认权限。 ⚠️ 取不到行时的返回口径与「这一行不存在」**逐字同形**（fail-closed、不泄露存在性）——⛔ 不新造业务码、⛔ 不写「你没有该游戏的权限」这种会泄露存在性的文案。 ## 重复 / 并发认领（spec §6.1 的分支表） ⛔ **绝不允许双开一张单**：本地预占（`listed → claiming`）是**条件更新**，事件与它 **同事务**；只有**赢下事件执行租约**的那一条请求路径才打上游。重复点 / 第二个打手 / 另有一个执行者在跑，都由领域层回**明确业务失败**（`1002` 已被接走 / 正在处理中）， ⛔ 不会静默成功、⛔ 也不会把别人的订单当成本次成功交回来。
    ## 失败口径 - 领域层的**预期**业务失败（`ClaimError`）⇒ 原样把 `msg` + 业务码交回去； - 提成 / 金额不可算（`CommissionError` / `AmountError`）这一档发生在「上游占住已成功、我方落库失败」**之后** ⇒ ⛔ 不能翻成「操作失败 / 已被别人接走」那类说假话的码（与 P2 `remote_claim` **同一处置**：复用固定文案 + `1002`，镜像行留在 `claiming`、事件留在队列里由退避 / 补偿重试）； - **未预期**的错误（含上游传输层异常）照旧**响亮上行**（⛔ 不伪装成业务结果，E-0010）。
    """

    permission_classes = [has_node("grab_hall.take_order")]

    def post(self, request, pk):
        visible = scope_queryset(
            request.user,
            JidanpuUpstreamHallOrder.objects.filter(pk=pk),
            scope_filter=game_scope("game_id"),
        ).first()
        if visible is None:
            return api_response(
                data=None,
                msg=jidanpu_common.ALREADY_TAKEN_MESSAGE,
                code=ErrorCode.ORDER_ALREADY_TAKEN,
            )
        # ⚠️ 押金闸（业务方：上家单与自家单一样）：判据与原生单同源
        # （`deposit_enforced_for` = 总闸 **或** App 专属闸 + App 令牌），冻结在领域层、
        # 与本地预占同一个事务；余额不足时在打上游之前拒。
        enforce_deposit = deposit_enforced_for(request)
        try:
            order = jidanpu_upstream.claim_upstream_mirror(
                user_id=request.user.id,
                mirror_id=pk,
                actor=request.user.get_username() or "",
                actor_id=request.user.pk,
                enforce_deposit=enforce_deposit,
                channel=channel_for(request),
            )
        except ClaimError as exc:
            return api_response(data=None, msg=str(exc), code=exc.code)
        except (CommissionError, AmountError):
            # ⚠️ **不读异常的 message / code**：这一档的口径是固定文案 + `1002`（见类 docstring）。
            return api_response(
                data=None,
                msg=jidanpu_common.RESERVED_PROMOTE_PENDING_MESSAGE,
                code=ErrorCode.BUSINESS,
            )
        return api_response(data=OrderSerializer(order, context={"request": request}).data)
