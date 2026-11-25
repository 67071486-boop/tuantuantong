"""上下游订单互通的端点（spec §8）。

    ⛔ 本文件里的**同步端点**（`peer-sync/*`）只装配 `PeerTokenAuthentication`；
    **页面端点**（`peer-bindings/*`）走全局默认的会话认证。两组**不要混**。
"""
from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.contrib.auth import authenticate
from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.peer_client import (
    HALL_PULL_REQUEST_ID_HEADER,
    PeerRejected,
    PeerUnreachable,
    bind_upstream,
    unbind_upstream,
    valid_hall_pull_request_id,
)
from apps.business.peer_inbound import SKIP_STALE, apply_events, apply_upstream_events
from apps.business.peer_sync import (
    HALL_PULL_KIND,
    PEER_EVENTS_MAX_BATCH,
    RECONCILE_MAX_ORDERS,
    decode_hall_cursor,
    effective_peer_ref,
    encode_hall_cursor,
    hall_pull_limit,
    hash_token,
    new_token,
    normalize_base_url,
    order_payload,
)
from apps.core.authentication import PeerTokenAuthentication
from apps.business.upstream_game_rules import upstream_price_profile_changed
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.rbac.permissions import has_node

logger = logging.getLogger(__name__)
hall_pull_trace_logger = logging.getLogger(f"{__name__}.hall_pull_trace")

#: 页面两个端点的**节点门**（**裁决 R22**）。复用菜单 `system-source-system` 上那个**已有**节点（同值出现在 `src/frontend/src/config/menu.ts` 与 `src/frontend/src/config/routePermissions.ts`），
#: ⛔ **不新增节点**（计划 D5）。菜单已按本节点控页面可见性 ⇒ 接上它**对合法用户零行为变化**，只是把"看得见菜单"与"调得动接口"对齐；代价是"被直接给接口但没给节点"的角色从 200 变 403 （正是想要的：没有这个节点的代练员不再能读绑定列表、更不能删绑定中断进单）。
PEER_BINDING_PAGE_NODE = "system.bind_upstream_system"

#: `bind/` 的失败限流（spec §9-S5）。✅ **已定值（业务方裁定；决策 `D-0157`）**： **每租户 10 次失败 / 60 秒**——下面两个常量就是那个值，⛔ 不再是"待确认的默认值"。 ⛔ 改数只改这里（别散落到别处）；⚠️ 值变了两条阈值用例（第 10 次锁死 / 第 11 次仍拦）
#: 与 `src/tests/test_peer_binding_api.py` 里那条字面值钉子会**一起红**——那是**有意的**。 ⚠️ 计数落在 Django 默认的 **LocMemCache** ⇒ 它是**进程内**的：多 gunicorn worker 各算一份 ⇒ 生产实际每租户上限 ≈ `PEER_BIND_MAX_FAILURES × worker 数`（本仓应用单元写的是 `--workers 2`
#: ⇒ **≈ 20 次/分钟/租户**，比上面这个数松两倍）。要严格按 10 执行得换共享存储（DB 计数或 Redis），属**新基础设施** ⇒ 本期有意不做（spec §14-R12：限流是"缓解"不是"控制项"）， ⛔ 不是待修的缺陷、也不是"把数改小"能解决的。
PEER_BIND_MAX_FAILURES = 10
PEER_BIND_WINDOW_SECONDS = 60
#: ⛔ 账号不存在与密码错**必须回同一句话**（spec §9-S5：不给账号枚举面）。
PEER_BIND_FAILED_MSG = "上家系统的账号或密码不对，或者那个系统暂时不可达"
#: URL 闸门失败时回给**外部调用方**的那一句。 ⚠️ 本端点**只回这一句**、且**不读任何异常文本**：URL 闸门（`normalize_base_url`）与 stdlib（`urlsplit()` / `SplitResult.port`）的原文一律不外传（spec §14-R7「不回显上游 / 框架原文」）。
#: ⚠️ 顺带：具体的拒绝理由（内网 / 组播 / 路径…）也不外传——本端点是**外部可达**的，把「这个地址被判成内网还是格式错」告诉对方等于给 SSRF 探测做反馈（spec §14-R7 同族）。
PEER_BIND_BAD_BASE_URL_MSG = "下家系统填的网址不对：只填 http/https 的域名（可带端口），且不能是内网地址"
#: ⭐ C2（Z22，审计批）：新绑定**拒绝 `http://`** 时回给外部调用方的那一句。
#: 握手口令、双向令牌、客资手机号/QQ 走 http = 全部明文过公网。存量绑定不打断
#: （页面序列化器只读 `uses_insecure_http` 展示风险）；本句只拦**新**握手。
PEER_BIND_INSECURE_HTTP_MSG = "绑定被拒绝：你声明的本方地址必须以 https:// 开头（明文 http 会把密码与令牌裸露在公网），改用 https 后重新握手"
#: ⭐ C2 同批：页面绑定弹窗拒 `http://` 的人话（页面端点可以安全回显细节）。
PEER_INSECURE_HTTP_MSG = "上家系统的网址必须以 https:// 开头：明文 http 会把握手密码与业务令牌裸露在公网上，请上家配好 HTTPS 证书后再绑"
#: ⭐ C7（L-15，审计批）：同一上家域名已有**另一条**未解绑绑定时的人话。
#: 镜像行的幂等键是 `(域名, 单号)` 不含绑定 ⇒ 同域名两条活绑定会互改写 / 接管对方的卡。
#: 文案指向旧绑定与两条出路（先解绑 / `replace=true` 顶替）。
PEER_BINDING_HOST_CONFLICT_MSG = (
    "这个上家域名已经有一条生效中的绑定（子账号 {account}）。"
    "同一个域名同时只能有一条活绑定：请先解绑旧的那条再绑，或改用「顶替旧绑定」直接换绑"
)
#: ⭐ B2 / M-7（审计批）：**同账号换了域名**再绑时，响应里带的旧绑定提示。上家「变死 / 换址」最常见的形状就是拿同一个子账号换个域名重绑——旧域名的绑定行照活，名下 `listed` 的镜像卡从此「可见 → 点了失败」（直到拉取连败判死收口，B2 的另一半）。
#: ⚠️ **只提示不阻断**：旧绑定可能是仍要用的另一条线，自动停用会误杀（M-7 给过两个选项，裁定取「提示」）。挂在**绑定 POST 的响应**上（页面人刚操作完、看得见的那一眼）；列表 / 详情两支的键集有「恰好集合」网钉着，⛔ 不动。
PEER_BINDING_STALE_HINT_MSG = (
    "提示：同一上家子账号「{account}」还有一条生效中的旧绑定指向 {base_url}。"
    "如果那个地址已废弃，请到列表里解绑旧绑定，否则旧域名下的镜像卡不会被收口"
)


def _bind_failure_key(request) -> str:
    """限流键。⚠️ 维度是**当前租户的 schema**，不引入任何请求参数里的租户标识（spec §9-S1）。取 `connection.schema_name`（与 `TenantBoundAuthentication` 复查会话时用的**同一个来源**）， ⛔ **不是** `request.get_host()`：后者**保留端口**，而 `TenantMainMiddleware` 解析租户时
    会 `request.get_host().split(":")[0]` 把端口丢掉 ⇒ `Host: <域名>:1111` 与 `…:2222` 是**同一个租户**，按 Host 分桶会让「10 次/分钟/租户」被任意端口后缀**乘**出来。
    """
    return f"peer-bind-fail:{connection.schema_name}"


def _count_failure(key: str) -> None:
    """失败计数（⭐ L-1，C3：`add + incr` 原子模式）。旧实现 `get` + `set` 在并发下会**丢计数**：两个请求同时读到 5、各写 6，「10 次 / 60 秒」被放宽成约一半。`cache.add` 只在键**不存在**时写入（原子），
    `cache.incr` 本身原子 ⇒ 竞争下一条都不丢。 ⚠️ 如实登记的口径变化：`incr` **不刷新 TTL** ⇒ 窗口从「最后一次失败起 60 秒」（滑动）变成「第一次失败起 60 秒」（固定）。对防爆破这只会更紧、不会更松。
    ⚠️ `incr` 打在刚过期的键上会抛 `ValueError`（Django 缓存 API 的既有行为）：「`add` 输了 → 键恰好过期」的窄窗里重新立一个，本次失败照样计 1。
    """
    cache.add(key, 0, PEER_BIND_WINDOW_SECONDS)
    try:
        cache.incr(key)
    except ValueError:
        cache.add(key, 1, PEER_BIND_WINDOW_SECONDS)


class PeerBindView(APIView):
    """`POST /api/business/peer-sync/bind/`：**唯一**一个用账号密码的端点（spec §5.1）。

    ⚠️ 它是本期**唯一**的外部可达登录面（spec §14-R7）⇒ 限流 + 错误码合并都在这里。
"""

    #: ⚠️ 这是本线**唯一**一个不是 `IsAuthenticated` 的端点：握手时还没有令牌可验（它正是来取令牌的）⇒ 显式 `AllowAny`。 ⛔ **`permission_classes` 不要**写成空列表：那要靠"空 = 放行"这条隐式约定，
    #: 而本仓的全局默认是 `IsAuthenticated`（裁决 R5）。 ⚠️ `authentication_classes` 反过来**必须**是空列表：它是本线唯一"不认任何认证凭据" 的端点（空列表在 DRF 里就是匿名），与上面的 `AllowAny` 配套。
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        from apps.business.api.serializers import PeerBindRequestSerializer
        from apps.business.models import PeerDownstreamBinding

        key = _bind_failure_key(request)
        if (cache.get(key) or 0) >= PEER_BIND_MAX_FAILURES:
            return api_response(data=None, msg=PEER_BIND_FAILED_MSG,
                                code=ErrorCode.PEER_BIND_FAILED)
        serializer = PeerBindRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        try:
            peer_base_url = normalize_base_url(payload["peer_base_url"])
        except ValueError:
            # ⛔ **不读 `str(exc)`**：闸门现在自己抛中文（`BASE_URL_MALFORMED_MSG` 等），
            # 但外部调用方的 `msg` 一律用这句固定文案——理由见 `PEER_BIND_BAD_BASE_URL_MSG` 的注释。
            _count_failure(key)
            return api_response(data=None, msg=PEER_BIND_BAD_BASE_URL_MSG,
                                code=ErrorCode.PEER_BIND_FAILED)

        # ⭐ C2（Z22，审计批）：新握手拒绝 `http://`——下家声明的是它自己收推送的地址，http 意味着之后双向令牌与客资字段全部明文过公网。 ⚠️ 与 URL 闸门同一条纪律：这不算「账号密码错」，但属于握手失败 ⇒ 同样计入
        # 限流桶（它是免认证端点上可无条件触发的动作，不计数就白送一个免认证写缓存口）。存量绑定不受影响：本检查只在**握手时刻**跑。
        if peer_base_url.startswith("http://"):
            _count_failure(key)
            return api_response(data=None, msg=PEER_BIND_INSECURE_HTTP_MSG,
                                code=ErrorCode.PEER_BIND_FAILED)

        user = authenticate(
            request, username=payload["account"], password=payload["password"]
        )
        if user is None or not user.is_active:
            _count_failure(key)
            return api_response(data=None, msg=PEER_BIND_FAILED_MSG,
                                code=ErrorCode.PEER_BIND_FAILED)

        token_up = new_token()
        with transaction.atomic():
            binding, created = PeerDownstreamBinding.objects.update_or_create(
                account=user,
                defaults={
                    "peer_base_url": peer_base_url,
                    "token_in_hash": hash_token(payload["token_out"]),
                    "token_out": token_up,
                    "state": PeerDownstreamBinding.State.ACTIVE,
                    "revoked_at": None,
                    # ⭐ （Important 1）：**重绑 = 重新开始** ⇒ 复位投递失败台账。
                    # ⛔ 不复位的话，一条被自动停绑（计数 10）的绑定重新绑好之后，
                    # 下一次投递失败会**立刻又被停**、并再写一条操作日志（"重绑等于没用"）。
                    "consecutive_failures": 0,
                    "last_error": "",
                    "last_error_at": None,
                },
            )
            if created:
                from apps.internal_chat.peer_chat import register_binding

                register_binding("downstream", binding)
        cache.delete(key)
        # ⚠️ `token_up` **只在这里回一次**（spec §5.1 第 4 步）：之后永不回显。
        return api_response(data={"token_out": token_up})


class PeerUnbindView(APIView):
    """`POST /api/business/peer-sync/unbind/`（Bearer；spec §5.2）。 ⚠️ 命中的那一行由 `PeerTokenAuthentication` 挂在 `request._peer_binding` 上（ 的产物），本视图 ⛔ 不再按令牌查一次库。两个方向共用这一个端点（两个方向各自的绑定表不同）： - 我是**上家** ⇒ 命中 `PeerDownstreamBinding` ⇒ 置 `revoked`（留痕、不删行，§5.2 第 3 步）； - 我是**下家** ⇒ 命中 `PeerUpstreamBinding` ⇒ 软删（`deleted_at`，§5.2 第 4 步）。
    ⭐ Z5（审计批次二）：同一个事务里把这份绑定名下 `claiming` 的镜像卡退回 `listed`（清四个接单列 + 一条台账），理由见 `peer_hall.release_claiming_mirrors`。 ⭐ B4 / L-9：同一个事务里**先**把 `listed` 的存量卡下架（`unlist_binding_mirrors`，自带给下家补发 `hall.take` 的转发），与页面 DELETE **完全同一份口径**（先下架、后退回，顺序承重）。 ⚠️ 两个分支都用**显式 `isinstance`** 认表：⛔ 不用「命中上家那张表，否则当上家绑定软删」
    这种排他写法——将来加第三张绑定表时，它会被**静默**当成 `PeerUpstreamBinding` 软删（评审 finding 7）。认不出的类型干脆响亮地 500（见 `post` 的 `else` 分支）。
    """

    authentication_classes = [PeerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.business.models import PeerDownstreamBinding, PeerUpstreamBinding

        binding = getattr(request, "_peer_binding", None)
        if binding is None:
            return api_response(data=None, msg="没有可解除的绑定",
                                code=ErrorCode.PEER_NOT_BOUND)
        if isinstance(binding, PeerDownstreamBinding):
            binding.state = PeerDownstreamBinding.State.REVOKED
            binding.revoked_at = timezone.now()
            binding.save(update_fields=["state", "revoked_at"])
        elif isinstance(binding, PeerUpstreamBinding):
            # ⭐ Z5：软删与「抢单未确认的卡退回大厅」**同一个事务**（与页面 DELETE 同一份口径、同一句人话）。绑定一停，`claiming` 那两条出路（转正 / refused）都不会再有回音 ⇒ 当场退回 `listed` + 一条台账。
            # ⭐ B4 / L-9：同一事务里**先**把 `listed` 的存量卡下架（`unlist_binding_mirrors`）、**再**退回 `claiming` 的卡——解绑通知与页面上点解绑**是同一个事实**（「这条绑定停了」），两支必须同一份口径；漏了下架这步，A→B→C 链上 B 收到 A 的解绑通知后，B 名下 `listed` 的卡既看得见又接不了，还**不给下家 C 补发 take**（C 的卡永远滞留——L-9 的现场；`unlist_binding_mirrors` 内部自带 take 转发，补调它就一并接上了）。
            # ⚠️ 顺序承重（页面 DELETE 同一条理由）：先下架、后退回——反过来刚退回 `listed` 的行会被一起标成 `gone`（白退）。
            from apps.business import peer_hall

            with transaction.atomic():
                binding.deleted_at = timezone.now()
                binding.save(update_fields=["deleted_at"])
                unlisted = peer_hall.unlist_binding_mirrors(
                    binding, note=peer_hall.UNLISTED_ON_UNBIND
                )
                released = peer_hall.release_claiming_mirrors(
                    binding, note=peer_hall.CLAIM_RELEASED_ON_UNBIND
                )
                if unlisted or released:
                    logger.info(
                        "大厅镜像：解绑通知下架存量镜像卡 %s 张、退回抢单未确认 %s 张"
                        " binding=%s",
                        unlisted, released, binding.pk,
                    )
        else:
            # ⛔ 认不出的绑定类型 ⇒ **响亮地 500**，既不当成功、也不当业务失败（`AGENTS.md` §5-E / E-0010：未预期的错误伪装成业务结果会让监控失明）。旧写法（排他分支）更糟：它会把 `deleted_at` 写到任何非 `PeerDownstreamBinding`
            # 的对象上、然后回 200 —— 调用方以为解绑成功，实际一行绑定都没动。 ⚠️ 今天**不可达**（`PeerTokenAuthentication` 只挂那两张表）；将来加第三张表而没同步改这里时，这条会立刻以 500 + 日志暴露，而不是静默走错分支。
            raise TypeError(f"未知的绑定类型：{type(binding).__name__}")
        return api_response(data=None)


# ── 页面侧（spec §8.4）：会话认证，走全局默认（⛔ 不装配 `PeerTokenAuthentication`）──
def _local_base_url(request) -> str:
    """我方域名（spec §6.1 的 `local_base_url` / **裁决 R12**）：**配置优先、请求推导兜底**。 ⚠️ **为什么不能只从 `request.scheme` 算**：生产里 nginx 终止 TLS、gunicorn 侧说 http，而本仓**没有** `SECURE_PROXY_SSL_HEADER` ⇒ `request.scheme` 恒为 `"http"`。这个值会在握手时
    交给上家、成为它后来推送用的地址 ⇒ 打到 nginx 的 80 → 301，而 `httpx` **默认不跟随重定向** ⇒ **下行推送永远失败**（spec §14-R13）。⇒ 生产必须显式配 `PEER_SYNC_LOCAL_BASE_URL=https://<租户域名>`；留空只对本地开发成立（那时请求推导出来的地址是可达的）。
    ⚠️ 它只是握手时交给上家的"我是谁"，只用于**核对**（上家推单时对不对得上）， ⛔ 不参与任何租户判定——租户上下文只来自 Host（`AGENTS.md` §4）。
    """
    configured = getattr(settings, "PEER_SYNC_LOCAL_BASE_URL", "")
    if configured:
        return normalize_base_url(configured)
    return normalize_base_url(f"{request.scheme}://{request.get_host()}")


class PeerBindingListView(APIView):
    """`GET` / `POST /api/business/peer-bindings/`（spec §8.4）。会话认证 + 节点门（裁决 R22）。 ⚠️ 与上面两个同步端点**刻意不同**：本类不声明 `authentication_classes` ⇒ 走全局默认的 `TenantBoundAuthentication`（人坐在页面上操作）。
    ⚠️ `permission_classes` **显式**声明节点门（`PEER_BINDING_PAGE_NODE`）——⛔ 不能靠全局默认的 `IsAuthenticated`：那条只要求"登录了"，任何登录用户（例如没有系统节点的代练员）都能**删**绑定（本地软删 + 通知上家撤销 ⇒ 这个租户的进单被静默停掉）。
    """

    permission_classes = [has_node(PEER_BINDING_PAGE_NODE)]

    def get(self, request):
        from apps.business.api.serializers import PeerUpstreamBindingSerializer
        from apps.business.models import PeerUpstreamBinding

        # 软删（解绑）过的行不再出现在列表里，但**不删行**（留痕，spec §5.2 第 4 步）。
        # ⚠️ `select_related("bound_by")`：`bound_by_name`（显示名）要读 FK ⇒ 不加就是一页
        # N 行 N 条查询（与 `hall_publisher_name` 同一条纪律）。
        rows = PeerUpstreamBinding.objects.filter(
            deleted_at__isnull=True
        ).select_related("bound_by")
        return api_response(data={
            "items": PeerUpstreamBindingSerializer(rows, many=True).data,
            "total": rows.count(),
        })

    def post(self, request):
        from apps.business.api.serializers import (
            PeerBindCreateSerializer,
            PeerUpstreamBindingSerializer,
        )
        from apps.business.models import PeerUpstreamBinding

        serializer = PeerBindCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        local_base_url = _local_base_url(request)

        try:
            base_url = normalize_base_url(payload["peer_base_url"])
        except ValueError as exc:
            # 闸门抛的是**自有中文**（ 在闸门内把 stdlib 英文原文翻掉了）
            # ⇒ 这里可以安全地回显给页面上的人（spec §14-R7）。
            return api_response(data=None, msg=str(exc), code=ErrorCode.VALIDATION)

        # ⭐ C2（Z22，审计批）：**新绑定**拒 `http://`——握手密码、双向令牌、
        # 客资手机号/QQ 在 http 上全是明文（三条链路唯一「凭据明文过公网」的活口）。
        # 存量绑定不打断（列表只读 `uses_insecure_http` 展示风险）；本检查只在绑定时跑。
        if base_url.startswith("http://"):
            return api_response(data=None, msg=PEER_INSECURE_HTTP_MSG,
                                code=ErrorCode.VALIDATION)

        # ⭐ C7（L-15，审计批）：**同 host 双活闸**。同一 `peer_base_url` 下已有**另一条**未解绑（软删之外）的绑定 ⇒ 默认拒绝；显式 `replace=true` ⇒ 在下面同一个事务里**原子停旧建新**。
        # ⭐ **豁免（承重）**：同 `(域名, 子账号)` 的幂等重绑（停绑恢复路径）**不经过本闸**——`exclude(peer_account=…)` 把本账号自己的行摘掉，重绑照旧刷新原行（裁决 R23 的既有测试钉着它，⛔ 不打断）。
        # 为什么承重：镜像行幂等键是 `(域名, 单号)`、**不含**绑定标识 ⇒ 同域名两条活绑定会互改写 / 接管对方的大厅卡（审计 L-15 / 旧账 L16）。
        host_conflicts = list(
            PeerUpstreamBinding.objects.filter(
                peer_base_url=base_url, deleted_at__isnull=True,
            )
            .exclude(peer_account=payload["peer_account"])
            .order_by("pk")
        )
        if host_conflicts and not payload.get("replace", False):
            return api_response(
                data=None,
                msg=PEER_BINDING_HOST_CONFLICT_MSG.format(
                    account=host_conflicts[0].peer_account,
                ),
                code=ErrorCode.CONFLICT,
            )

        # ⚠️ 令牌两列按**裁决 R15**（与上家侧完全对称）：
        # `token_out` 是**我方生成、交给上家**的那一套（`token_b`）⇒ 解绑时要原样外发 ⇒ **明文存**；
        # `token_in_hash` 存的是**上家回给我的那套**（`token_a`）的哈希 ⇒ 只用来验证上家的推送。
        token_out = new_token()
        try:
            peer_token = bind_upstream(
                base_url,
                account=payload["peer_account"],
                password=payload["password"],
                token_out=token_out,
                local_base_url=local_base_url,
            )
        except PeerUnreachable as exc:
            # 连不上 ⇒ `1021`；⛔ **什么都不落库**（失败不许留下半行绑定）。
            return api_response(data=None, msg=str(exc), code=ErrorCode.PEER_UNREACHABLE)
        except PeerRejected as exc:
            return api_response(data=None, msg=str(exc), code=ErrorCode.PEER_REJECTED)

        with transaction.atomic():
            # ⭐ C7：`replace=true` ⇒ **同一个事务里**把同域名的旧绑定**按页面 DELETE 同一份口径**停掉（软删 + 下架镜像卡 + 退回抢单未确认的卡）。 ⚠️ 顺序承重：停旧的收尾（下架 → 退回）必须**先于**新建行——与 DELETE
            # 视图里「下架之后才退回」同一条理由（刚退回 `listed` 的行不许被一起标 gone）。握手已成功 ⇒ 停旧失败会让整个事务回滚、本次绑定整体失败（原子，⛔ 不留半态）。
            if host_conflicts and payload.get("replace", False):
                from apps.business import peer_hall

                for old in host_conflicts:
                    old.deleted_at = timezone.now()
                    old.save(update_fields=["deleted_at"])
                    unlisted = peer_hall.unlist_binding_mirrors(
                        old, note=peer_hall.UNLISTED_ON_UNBIND
                    )
                    released = peer_hall.release_claiming_mirrors(
                        old, note=peer_hall.CLAIM_RELEASED_ON_UNBIND
                    )
                    logger.info(
                        "上下游绑定：replace 顶替旧绑定 binding=%s 下架镜像卡 %s 张、"
                        "退回抢单未确认 %s 张",
                        old.pk, unlisted, released,
                    )
            # ⭐ **裁决 R23：按 `(peer_base_url, peer_account)` 在未软删行里幂等**。上家侧同一个握手本来就是 `update_or_create(account=…)` 幂等的，而本处旧实现是 `objects.create` ⇒ 同一个上家、同一个子账号再绑一次会留下**第二行**，其令牌
            # 上家已不认（它只记最新的那一套）：页面上两行都显示"生效"，其中一行**永远收不到单** ——正是验收清单点名当 bug 的那种"幽灵绑定"。 ⚠️ 只在**未软删**行里找：已经解绑过的行不再复用（否则解绑等于没解绑，
            # 页面上那行会"复活"）⇒ 那时新建一行（软删行留着，spec §5.2 第 4 步）。
            row = PeerUpstreamBinding.objects.filter(
                peer_base_url=base_url,
                peer_account=payload["peer_account"],
                deleted_at__isnull=True,
            ).first()
            created = row is None
            if created:
                row = PeerUpstreamBinding(
                    peer_base_url=base_url,
                    peer_account=payload["peer_account"],
                    # `bound_by` 只在**新建**时落：见下面"有意不刷新"的说明。
                    bound_by=request.user,
                )
            # 刷新的是这次握手真正换掉的那些（名字 / 密码布尔 / 两套令牌 / 我方域名）。 ⚠️ `bound_by` 与 `bound_at` **有意不刷新**：它们读作"这一行第一次是谁、什么时候建的"（`bound_at` 是 `auto_now_add`，本来就不可写）。刷新 `bound_by` 还会**静默
            # 改掉已落单的销售提成归属口径**——`receive_events` 拿 `binding.bound_by` 当同步单的 `sales_user`（spec §10.4）；`bound_at` 的同一口径登记在 `tests/test_peer_binding_api.py::test_rebinding_the_same_account_rotates_the_token_and_reactivates`。
            row.peer_name = payload["peer_name"]
            # ⭐ C1（M-1，审计批）：**验完即弃**——握手密码只进这一次 `bind_upstream` 调用，⛔ 不再落列；页面「要不要重填」改读布尔
            # `password_set`（迁移 0095 已把存量明文回填成布尔并清空）。顺手把列写成空串：让「库里无明文」在任何一行上都成立（含改造前的残行）。
            row.password = ""
            row.password_set = True
            row.token_out = token_out
            row.token_in_hash = hash_token(peer_token)
            row.local_base_url = local_base_url
            # ⭐ （Important 1）：重绑 = 重新开始 ⇒ 同时复位**入站**失败台账（这几个列由接收侧的硬跳喂起来，语义是"对面推来的东西我处理不了"，
            # 见 `_record_inbound_health`）。⛔ 不复位的话，重新握手之后那块红字提示会继续挂着一个已经换掉的对端的旧错误。
            row.consecutive_failures = 0
            row.last_error = ""
            row.last_error_at = None
            row.save()
            if created:
                from apps.internal_chat.peer_chat import register_binding

                register_binding("upstream", row)
            record(
                log_type=ACTION_LOG_TYPES["system.peer_binding_create"],
                action="system.peer_binding_create",
                actor=request.user.get_username(), actor_id=request.user.pk,
                target_kind="peer_binding", target_id=str(row.pk),
                target_name=row.peer_name,
            )
        # ⚠️ 回的是列表行（**不含**密码与两个令牌，spec §9-S4） ⭐ B2 / M-7：同账号**换了域名**的旧绑定还在 ⇒ 响应里带一句提示（只提示不阻断）。同 `(域名, 子账号)` 的幂等重绑走的是「复用本行」那条路（上面 R23 的 filter），
        # 到不了这里还有旧行 ⇒ 能命中的只剩「同账号、不同域名」——正是 M-7 的现场。 ⚠️ 键只加在**本端点的响应**上：`stale_binding_hint` 不进序列化器（GET 列表 / 详情的键集恰好网钉着 `PEER_BINDING_CONTRACT_FIELDS`，⛔ 不动那份契约）。
        data = PeerUpstreamBindingSerializer(row).data
        stale_same_account = list(
            PeerUpstreamBinding.objects.filter(
                peer_account=payload["peer_account"], deleted_at__isnull=True,
            )
            .exclude(pk=row.pk)
            .order_by("pk")
        )
        if stale_same_account:
            data["stale_binding_hint"] = PEER_BINDING_STALE_HINT_MSG.format(
                account=stale_same_account[0].peer_account,
                base_url=stale_same_account[0].peer_base_url,
            )
            logger.warning(
                "上下游绑定：同账号存在旧域名绑定 binding=%s 旧绑定=%s",
                row.pk, [old.pk for old in stale_same_account],
            )
        return api_response(data=data)


#: 取不到行（不存在 / 已被并发解绑）时的人话（**外部契约值**：DELETE 的既有原话）。
#: 行级 `GET` / `PUT` 与它**逐字同句**——同一页里「这条绑定没了」只有一种说法（⛔ 不另造）。
PEER_BINDING_MISSING_MESSAGE = "这个绑定不存在"


class PeerBindingDetailView(APIView):
    """`/api/business/peer-bindings/<pk>/` 的三支：读 / 改大厅两格 / 解绑。 | 方法 | 干什么 | 出处 | |---|---|---| | `GET` | 回**这一行**（`PeerUpstreamBindingSerializer`，含 `hall_sync_enabled` / `hall_price_percent`）—— 绑定页编辑弹窗的**回填出口** | P1 | | `PUT` | **只改**大厅那两格 + 收款查询凭据两格（`PeerBindingSettingsSerializer`；1–100 之外 ⇒ `1000`；密码 write_only 非空才设、预检标记显式给才改——D-0359） | P1 / D-0359 | | `DELETE` | 通知上家 + 本地软删（spec §5.2） | |
    ⭐ **P5 增量**：`PUT` 把 `hall_sync_enabled` 从 `True` 改成 `False`、以及 `DELETE` 软删绑定——两支都**在各自的 `atomic()` 里**把这份绑定名下 `status=listed` 的镜像卡标 `gone`（`claiming` 不动），各记一行下架行数日志。绑定行与镜像行都保留（软删 / `PROTECT`），口径见 `peer_hall.unlist_binding_mirrors`。 ## 节点门：三支**同一个**（**裁决 R22**）没有 `system.bind_upstream_system` 的人一律 403。三条理由各自对应一支：
    删绑定会同时停掉这个租户的进单；`PUT` 打开大厅同步 = **把另一家租户的大厅卡片摆到我方打手面前**（默认关，业务方拍板）；`GET` 会回对端域名 / 子账号 / 上次错误——与列表页看到的是同一批事实。
    """

    permission_classes = [has_node(PEER_BINDING_PAGE_NODE)]

    def _row_or_none(self, pk, *, for_update=False):
        """取一条**未解绑**的绑定（`select_related("bound_by")`：`bound_by_name` 要读 FK）。"""
        from apps.business.models import PeerUpstreamBinding

        rows = PeerUpstreamBinding.objects.filter(pk=pk, deleted_at__isnull=True).select_related("bound_by")
        if for_update:
            rows = rows.select_for_update(of=("self",))
        return rows.first()

    def get(self, request, pk):
        """读一条（含大厅两格）——编辑弹窗的回填出口。"""
        from apps.business.api.serializers import PeerUpstreamBindingDetailSerializer

        row = self._row_or_none(pk)
        if row is None:
            return api_response(
                data=None, msg=PEER_BINDING_MISSING_MESSAGE, code=ErrorCode.BUSINESS
            )
        return api_response(data=PeerUpstreamBindingDetailSerializer(row).data)

    @transaction.atomic
    def put(self, request, pk):
        """保存上家设置，冻结存量镜像价格后再启用新规则。"""
        from apps.business import hall_shadow, peer_hall, peer_inbound
        from apps.business.api.serializers import (
            PeerBindingSettingsSerializer,
            PeerUpstreamBindingDetailSerializer,
        )
        from apps.business.models import PeerHallMirror

        row = self._row_or_none(pk, for_update=True)
        if row is None:
            return api_response(
                data=None, msg=PEER_BINDING_MISSING_MESSAGE, code=ErrorCode.BUSINESS
            )
        serializer = PeerBindingSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)  # 失败 ⇒ 统一处理器包成 1000
        wanted = serializer.validated_data
        was_hall_sync_enabled = row.hall_sync_enabled
        was_hall_price_percent = row.hall_price_percent
        row.hall_sync_enabled = wanted.get("hall_sync_enabled", row.hall_sync_enabled)
        row.hall_price_percent = wanted.get("hall_price_percent", row.hall_price_percent)
        old_game_rules = row.game_rules
        row.game_rules = wanted.get("game_rules", old_game_rules)
        rules_changed = upstream_price_profile_changed(old_game_rules, row.game_rules)
        percent_changed = (
            "hall_price_percent" in wanted
            and row.hall_price_percent != was_hall_price_percent
        )
        # ⭐ D-0359：收款查询凭据与预检标记（两格各自独立；缺键 = 不改）。 - 密码：DRF CharField 默认 trim 空白 ⇒ 非空串 = 设置（同时置布尔）；
        # 缺键 / 空串 = 不改（`_BLANK_MEANS_KEEP` 先例，⛔ 没有清空入口）。 - 预检：显式给了才改（True/False 都可写——预检结论变化时可撤）。
        new_password = wanted.get("receipt_query_password")
        password_changed = bool(new_password)
        new_precheck = wanted.get("receipt_query_precheck_ok")
        precheck_changed = new_precheck is not None and new_precheck != row.receipt_query_precheck_ok
        if password_changed:
            row.receipt_query_password = new_password
            row.receipt_query_password_set = True
        if precheck_changed:
            row.receipt_query_precheck_ok = new_precheck
        update_fields = ["hall_sync_enabled", "hall_price_percent"]
        if "game_rules" in wanted:
            update_fields.append("game_rules")
        if "direct_taker_assignment_enabled" in wanted:
            row.direct_taker_assignment_enabled = wanted["direct_taker_assignment_enabled"]
            update_fields.append("direct_taker_assignment_enabled")
        if password_changed:
            update_fields += ["receipt_query_password", "receipt_query_password_set"]
        if precheck_changed:
            update_fields += ["receipt_query_precheck_ok"]
        if not was_hall_sync_enabled and row.hall_sync_enabled:
            # ⭐ 计划 `2026-10-25-peer-hall-pull` §5：**flip False→True 的持久标记**。只在这一刻置位（True→True 不刷新）⇒ worker 欠一次「从开关打开起算的完整全量」，跑完才清。`next_at = now` 让它是**立刻**可拉（不必等 5 分钟节拍）。
            # ⚠️ 两列**不进** `PeerUpstreamBindingSerializer` / `PEER_BINDING_CONTRACT_FIELDS` （它们是内部调度标记，不是页面字段）。
            marker_at = timezone.now()
            row.hall_pull_requested_at = marker_at
            row.hall_pull_next_at = marker_at
            update_fields += ["hall_pull_requested_at", "hall_pull_next_at"]
        if rules_changed or (old_game_rules is not None and percent_changed):
            PeerHallMirror.objects.filter(
                peer_upstream_binding=row, hall_price_percent_at_sync__isnull=True,
            ).exclude(status=PeerHallMirror.Status.PROMOTED).update(
                hall_price_percent_at_sync=was_hall_price_percent or 100,
            )
        row.save(update_fields=update_fields)
        # ⚠️ `to_value` 是**纯值**说明（R10：标签由模板正文提供，见
        # `apps/audit/actions.py::SYSTEM_PEER_BINDING_UPDATE`）。
        record(
            log_type=ACTION_LOG_TYPES["system.peer_binding_update"],
            action="system.peer_binding_update",
            actor=request.user.get_username(), actor_id=request.user.pk,
            target_kind="peer_binding", target_id=str(row.pk),
            target_name=row.peer_name,
            to_value=(
                f"同步上家抢单大厅={'开启' if row.hall_sync_enabled else '关闭'}，"
                f"价格百分比={row.hall_price_percent}"
                + ("，已更新按游戏规则" if rules_changed else "")
                + ("，收款查询密码已更新" if password_changed else "")
                + (
                    f"，收款查询预检标记={'通过' if row.receipt_query_precheck_ok else '未通过'}"
                    if precheck_changed else ""
                )
            ),
        )
        # 未使用自定义规则的旧卡仍随全局比例更新；已经冻结的卡保持原价。
        if percent_changed and not rules_changed and row.game_rules is None:
            now = timezone.now()
            cards = (
                PeerHallMirror.objects.filter(
                    peer_upstream_binding=row,
                    status=PeerHallMirror.Status.LISTED,
                    hall_price_percent_at_sync__isnull=True,
                )
                .select_related("peer_upstream_binding", "game")
            )
            for card in cards:
                hall_shadow.sync_hidden_order_price(card, now=now)
                peer_inbound.forward_hall_reprice(
                    card, hop=card.hop, now=now
                )
        # ⭐ P5：**开 → 关** ⇒ 同一事务里把名下在挂的卡下架（`claiming` 不动：它有自己的收口 / 5 分钟回收；`promoted` / `gone` 不碰）。⚠️ 开关关着时读口 / 接单口
        # 本来就被 P1 的 `peer_hall_queryset` / `claim_peer_mirror` 挡住，这一步是 **清数据**（防再打开时挂着一批过期卡），⛔ 不是给读口补闸。
        if was_hall_sync_enabled and not row.hall_sync_enabled:
            unlisted = peer_hall.unlist_binding_mirrors(
                row, note=peer_hall.UNLISTED_ON_SWITCH_OFF
            )
            logger.info(
                "大厅镜像：关闭接收开关，下架存量镜像卡 binding=%s count=%s",
                row.pk, unlisted,
            )
        return api_response(data=PeerUpstreamBindingDetailSerializer(row).data)

    def delete(self, request, pk):
        """解绑（软删上家绑定）+ **同一事务**下架这份绑定名下在挂的镜像卡（⭐ P5）。 ⚠️ 绑定行与镜像行**都保留**：`row.deleted_at` = 软删（留痕）； `PeerHallMirror.peer_upstream_binding` 是 `PROTECT` ⇒ 镜像行不可能被真删，且 share-design §11-3 要求解绑时镜像行留档（只标 `gone` + `gone_at`， `payload` / 单号 / 价格原样）。 ⚠️ 只动 `listed`（`claiming` 不动：它有自己的收口 / 5 分钟回收）。
        ⭐ Z5（审计批次二）**改准**：`claiming` 不再「什么都不做」——解绑会让它那两条出路都失去回音 ⇒ 同一事务里退回 `listed`（`peer_hall.release_claiming_mirrors`，顺序在下架**之后**，否则刚退回的行会被一起标成 `gone`）。 ⚠️ 下架行数**只进日志、不进响应体**：本端点既有契约回 `data=None`（前端只判 code），加一个键等于给一个没人消费的字段；而且与 PUT 同一口径（见那里的说明）。
        """
        from apps.business import peer_hall
        from apps.business.models import PeerUpstreamBinding

        row = PeerUpstreamBinding.objects.filter(
            pk=pk, deleted_at__isnull=True
        ).first()
        if row is None:
            # ⚠️ 不存在的 pk 与"已经解绑过的行"**同一个出口**（`1002`）：
            # 再删一次不算成功（⛔ 不许用"幂等成功"掩盖"这行早就不在"）。
            return api_response(
                data=None, msg=PEER_BINDING_MISSING_MESSAGE, code=ErrorCode.BUSINESS
            )
        # ⚠️ **通知失败不能让解绑失败**（spec §5.2 第 5 条）：本地照样解绑；通知丢了之后，上家会经自动停绑在投递连续失败到阈值时自己停绑
        # （`apps/business/peer_worker.py::_record_failure`：连败到 `AUTO_REVOKE_AFTER_FAILURES` ⇒ 置 `revoked` + 写一条操作日志，spec §16.6 第 4 条）。
        # ⚠️ **窗口仍在**：解绑那一刻本侧已解绑、上家仍在 `ACTIVE`，要等它把失败数攒够；停绑只停投递、绑定行与账号都还在（spec §9-S8 / §14-R3 的边界收窄但不消失）。
        try:
            # ⚠️ 带的是 `token_out`（= 握手时我方生成并交给上家的 `token_b`，明文）——
            # 上家按 `PeerDownstreamBinding.token_in_hash = hash(token_b)` 验（spec §5.2 第 2 步 / R15）。
            unbind_upstream(row.peer_base_url, row.token_out)
        except (PeerUnreachable, PeerRejected) as exc:
            logger.warning("上下游解绑通知失败（本地仍然解绑）：%s", exc)
        with transaction.atomic():
            row.deleted_at = timezone.now()
            row.save(update_fields=["deleted_at"])
            record(
                log_type=ACTION_LOG_TYPES["system.peer_binding_delete"],
                action="system.peer_binding_delete",
                actor=request.user.get_username(), actor_id=request.user.pk,
                target_kind="peer_binding", target_id=str(row.pk),
                target_name=row.peer_name,
            )
            # ⭐ P5：解绑 ⇒ 同一事务里把这份绑定名下在挂的卡下架（业务方裁定：
            # 「解绑时存量镜像全部下架，镜像行留档」）。⛔ 不碰别的绑定的卡（判据是 FK）。
            unlisted = peer_hall.unlist_binding_mirrors(
                row, note=peer_hall.UNLISTED_ON_UNBIND
            )
            logger.info(
                "大厅镜像：解绑下架存量镜像卡 binding=%s count=%s",
                row.pk, unlisted,
            )
            # ⭐ Z5：`claiming` 的卡**不归上一步管**（它在等对端回音，而绑定一停那个回音永远不来）⇒ 当场退回 `listed`。
            # ⚠️ 顺序**承重**：必须在 `unlist_binding_mirrors` **之后**——反过来刚退回 `listed` 的行会被下一步的 `listed → gone` 一起标成 `gone`（白退）。
            released = peer_hall.release_claiming_mirrors(
                row, note=peer_hall.CLAIM_RELEASED_ON_UNBIND
            )
            if released:
                logger.info(
                    "大厅镜像：解绑退回抢单未确认的镜像卡 binding=%s count=%s",
                    row.pk, released,
                )
        return api_response(data=None)


# ── 同步侧（spec §7.2 / §8.3 / §10.4；）────────────────────────────────
class PeerInboundEventsView(APIView):
    """`POST /api/business/peer-sync/inbound/events/`（Bearer；spec §8.3）。 ⚠️ 本端点是**机器对机器**的：只装 `PeerTokenAuthentication`，会话 cookie 一律 401 （spec §9-S2）⇒ 它登记在 `test_no_route_500.TOKEN_ONLY_ROUTES` 里。
    ⚠️ 命中的绑定行由认证类挂在 `request._peer_binding` 上（ 的产物），本视图 ⛔ 不再按令牌查一次库。
    """

    authentication_classes = [PeerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.business.models import PeerDownstreamBinding, PeerUpstreamBinding

        binding = getattr(request, "_peer_binding", None)
        # ⚠️ 令牌是**分方向**的：认证类两张绑定表都扫（`PeerTokenAuthentication`），所以手里拿着「另一个方向绑定令牌」的对端也能通过认证、走到这里 ⇒ 本视图**按类型分流**（P3 之前是"只认上家绑定、其余一律 1020"）。
        # ⚠️ 非 dict 的顶层请求体（例如裸 JSON 数组）**必须与「events 不是数组」同一出口**：旧实现把它归一成 `{}` ⇒ `events=[]` ⇒ 回 `{"applied":0,"skipped":0}` + 业务码 0，发送方据此把这次推送标成 done，而**实际一条都没落**（E-0010 同族：请求来源的值
        # 进类型化操作之前一律先归一，⛔ 不许归一成"什么都不用做"）。
        if not isinstance(request.data, dict):
            return api_response(data=None, msg="请求体必须是一个 JSON 对象",
                                code=ErrorCode.VALIDATION)
        events = request.data.get("events") or []
        if not isinstance(events, list):
            return api_response(data=None, msg="events 必须是数组",
                                code=ErrorCode.VALIDATION)
        # ⭐ C4（L-2，审计批）：**批量硬上限**——持令牌的对端不许用一份超长 `events` 逼我们跑一个巨型事务（整批一个事务 ⇒ 一条塞几十万条小事件就能把投递轮拖死）。上限 = `max(2 × 发送端单批, 100)`；发送端现状是
        # **一次 HTTP 只投一条**（`peer_worker.deliver_one` 的既有形状，常量出处见 `peer_sync.PEER_EVENTS_MAX_BATCH`）。正常批量（≤100）完全不受影响。
        if len(events) > PEER_EVENTS_MAX_BATCH:
            return api_response(
                data=None,
                msg=f"events 一次最多 {PEER_EVENTS_MAX_BATCH} 条，请分批投递",
                code=ErrorCode.VALIDATION,
            )
        if isinstance(binding, PeerUpstreamBinding):
            # ── P1/P2 的本职：**下家**收上家推来的单（建单 / 更新 / 撤单）── 旧实现把别的绑定直接交给 `receive_events`，实测（带事件时）在 `PeerOrderLink.objects.filter(upstream_binding=…)` 上抛
            # `ValueError: Cannot query "下游绑定:…": Must be "PeerUpstreamBinding" instance.` → 500。整批一个事务：任何一条落单抛异常 ⇒ 这一批全部回滚（上家会把它记成 failed）。
            with transaction.atomic():
                result = receive_events(binding, events)
        elif isinstance(binding, PeerDownstreamBinding):
            # ⭐ P3：**上家**收下家回传的**状态**（只改状态，⛔ 不建单、⛔ 不碰价格）。 ⛔ **本支有意不调 `_record_inbound_health`**：`PeerDownstreamBinding` 的那四列（`last_ok_at` / `last_error` / `last_error_at` / `consecutive_failures`）按本仓已写明的分工是**发送方的账**——"我推给下家的东西成没成"（`receive_events` 的 docstring 原话：⛔ 不碰上家侧的 `PeerDownstreamBinding`），而 `apps/business/peer_worker.py::_record_failure` 正拿 `consecutive_failures`
            # 数到 10 去**自动停绑**。把"下家推来的东西我处理不了"也累加到同一个计数器上，会让一个推得挺正常的下家因为自己回传了几条废事件而把**我推给它的通道**停掉——两本账混成一本。⇒ 本支**只回 `skipped_details`**（下家作为发送方看得见；它自己的台账会记 `failed` + 原因），上家侧不入站健康列（ 残差⑥）。
            with transaction.atomic():
                outcome = apply_upstream_events(events, binding=binding)
            result = _outcome_to_dict(outcome)
        else:
            # ⛔ 别把这一支删掉：既不是 `PeerUpstreamBinding` 也不是 `PeerDownstreamBinding`
            # 的绑定类型必须**响亮失败**（`PeerUnbindView` finding 7 的同族防线），
            # ⛔ 不许退化成 500、⛔ 也不许静默塞进上面任一支。
            return api_response(
                data=None,
                msg="无法识别这条令牌对应的绑定类型",
                code=ErrorCode.PEER_REJECTED,
            )
        return api_response(data=result)


class PeerReconcileView(APIView):
    """`POST /api/business/peer-sync/reconcile/`（Bearer；spec §16.8 / P3 ）。 ⚠️ 本端点服务的是**被问的一方**：上家问下家「我推给你的这些单，你应用到哪了」。令牌命中的那一行由 `PeerTokenAuthentication` 挂在 `request._peer_binding` 上。两个方向共用这一个端点（两张绑定表），按**显式 `isinstance`** 分流： - 命中 `PeerUpstreamBinding`（我是**下家**、正被上家问）⇒ 按 `(upstream_binding=binding, peer_order_no=ref)` 查 `PeerOrderLink`，如实回报**这张单最后一次被本地决定的时刻** = `max(last_applied_at, local_action_at)`（见下方循环里的理由）。没见过的单号 / 两个时刻都为空回 `null`（=「我没应用过」）。这正是重排要用的那个水位。 ⭐ **起的两个键**（计划 `2026-10-25-peer-source-identity` §4）：请求可多带
    `order_nos = {<来源单号>: <发送方内置号>}`——老链路的 `peer_order_no` 记的是**当年那个内置号**，而升级后的发送方问的是**来源单号** ⇒ 收方**任一对上即命中**（过渡期兼容）。老发送方不带这个键 ⇒ 行为逐字不变；⛔ 不做存量反推（老行不改写）。 - 命中 `PeerDownstreamBinding`（我是**上家**、正被下家问）⇒ **一律回 `null`**。 ⚠️ **本期对账只覆盖「上家问下家」这一个方向**（ 残差）：「下家问上家」需要上家侧另一套水位来源，而上家侧**没有** `PeerOrderLink`（它的冲突裁决用 `order.updated_at` 作代理，见 `peer_inbound.apply_upstream_events`）⇒ 这里如实回"我没记"，⛔ 不假装知道。代价：下家若拿 `null` 当"上家没收到"，会对每条自己发过的回传都重投一遍——本期**没有** 下家侧的对账发起方（`run_reconcile` 只问下游），所以这条今天不可达； 做那个方向时
    必须同时定"上家的水位从哪来"。请求体非 JSON 对象 / `orders` 非数组 / 单号不是字符串 / `order_nos` 不是 `{字符串: 字符串}` 对象 ⇒ `1000`（⛔ 不许静默当成空请求，那会让发起方以为"对端全都没有"而重投一整批）。⚠️ 「`orders` 非数组」**包含** `{}` / `0` / `false` / `""` 这些 falsy 值（见下面 `orders is None` 那一段）；只有**缺键 / 显式 `null`** 才回空映射 `{"applied": {}}`。单号按 `RECONCILE_MAX_ORDERS` **切片**（超出的有意不理：对端不该能用一份超长清单逼我们跑无界查询）。
    """

    authentication_classes = [PeerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from apps.business.models import (
            PeerDownstreamBinding,
            PeerOrderLink,
            PeerUpstreamBinding,
        )

        # ⚠️ 非 dict 的顶层请求体（例如裸 JSON 数组）与「orders 不是数组」**同一出口**：归一成 `{}` 会让发起方收到 `{"applied": {}}` + 业务码 0，据此以为"对端全都没有"
        # 而重投一整批（E-0010 同族：请求来源的值进类型化操作之前一律先归一，⛔ 不许归一成 "什么都不用做"）。
        if not isinstance(request.data, dict):
            return api_response(data=None, msg="请求体必须是一个 JSON 对象",
                                code=ErrorCode.VALIDATION)
        orders = request.data.get("orders")
        # ⚠️ 只有**缺键 / 显式 `null`** 才算"没问任何单号"；⛔ **不许**写成 `... or []`——那会把 `{}` / `0` / `false` / `""` 这类 **falsy 非数组**也归一成空请求，回
        # `{"applied": {}}` + 业务码 0，而按对账语义"对端回 `null`/没这个号 = 我没有"，发起方会据此把**整个窗口**的候选当成落后而全部重投（旧对端发 `{"orders": {}}` 即触发）。
        if orders is None:
            orders = []
        if not isinstance(orders, list):
            return api_response(data=None, msg="orders 必须是数组",
                                code=ErrorCode.VALIDATION)
        refs = orders[:RECONCILE_MAX_ORDERS]
        # 非字符串单号进不了 JSON 对象的键（列表/字典当键会抛 `TypeError` ⇒ 500）⇒ 过闸门。
        if not all(isinstance(ref, str) for ref in refs):
            return api_response(data=None, msg="orders 里必须都是字符串单号",
                                code=ErrorCode.VALIDATION)
        # ⭐（计划 `2026-10-25-peer-source-identity` §4）：对账的**第二个键**。新口径下发送方问的是**来源单号**，而老链路那一列 `peer_order_no` 记的是当年那个**内置号** ⇒ 发送方把每条候选的内置号一并带来（`{来源号: 内置号}`），
        # 收方**任一对上即命中**（过渡期兼容，⛔ 不做存量反推）。 ⚠️ 可选键：老发送方不带它 ⇒ 行为逐字不变（只按 `orders` 里的原样键查）。
        order_nos = request.data.get("order_nos")
        if order_nos is not None and (
            not isinstance(order_nos, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in order_nos.items()
            )
        ):
            # ⛔ 不许静默忽略（E-0010 同族）：这个键的形状不对说明发送方的实现坏了，
            # 当作空请求会让它每一轮都把整个窗口当缺口重投。
            return api_response(
                data=None, msg="order_nos 必须是 {来源单号: 内置单号} 对象",
                code=ErrorCode.VALIDATION,
            )

        binding = getattr(request, "_peer_binding", None)
        if isinstance(binding, PeerUpstreamBinding):
            # ⚠️ 一次 `peer_order_no__in=…` 拿全（两把键的并集，且 refs 已被上限切过
            # ⇒ 查询有界），⛔ 不要逐号查一次（对端给 200 个号就是 200 条查询）。
            lookup_keys = list(refs)
            for ref in refs:
                legacy = (order_nos or {}).get(ref)
                if isinstance(legacy, str) and legacy and legacy not in lookup_keys:
                    lookup_keys.append(legacy)
            links = {
                link.peer_order_no: link
                for link in PeerOrderLink.objects.filter(
                    upstream_binding=binding, peer_order_no__in=lookup_keys
                )
            }
            applied = {}
            for ref in refs:
                # ⭐ 两把键：先原样键（新口径 / 老发送方的内置号），再兜第二把键。
                link = links.get(ref)
                if link is None:
                    legacy = (order_nos or {}).get(ref)
                    if isinstance(legacy, str) and legacy:
                        link = links.get(legacy)
                # 回的是**这张单最后一次被本地决定**的时刻：`last_applied_at` = "我应用了上家哪一刻的事件"，`local_action_at` = "我这边最后一次本地动作"（ 的冲突裁决用它，见 `peer_inbound`）。取两者**较晚**的——只回 `last_applied_at` 会让"本地有意保留
                # 了更新改动、因而把上家事件良性跳过"的单永远显示落后（跳过**不推进** `last_applied_at`），上家于是每轮都把它当缺口重投。两者都为空 ⇒ `null`。
                moments = []
                if link is not None:
                    moments = [
                        moment
                        for moment in (link.last_applied_at, link.local_action_at)
                        if moment is not None
                    ]
                applied[ref] = max(moments).isoformat() if moments else None
        elif isinstance(binding, PeerDownstreamBinding):
            applied = {ref: None for ref in refs}
        else:
            # ⛔ 认不出的绑定类型 ⇒ 响亮地按业务失败回（同 `PeerInboundEventsView` 的防线），
            # ⛔ 不许退化成 500、⛔ 也不许静默恒回 `null`（那等于对任何未知令牌都说"我没收到"）。
            return api_response(
                data=None,
                msg="无法识别这条令牌对应的绑定类型",
                code=ErrorCode.PEER_REJECTED,
            )
        return api_response(data={"applied": applied})


def _write_pull_evidence(binding, refs) -> None:
    """给**真正放进响应**的每一张卡记一条 `hall.pull` 证据（Z20 的第二类来源）。计划 `2026-10-25-peer-hall-pull` §2 的六条承重口径： 1. **先过闸、只对真正返回的行写**（调用方在拿到 `refs` 之后才调本函数）； 2. `direction="in" / kind="hall.pull" / binding_role="downstream" / binding_id=<请求方绑定 pk> / target_type="hall" / target_ref=<卡号> / status="done" / reason=""`——与抢单判据的第二个 `exists()` **逐字对应**；
    3. **按卡去重**：`bulk_create(ignore_conflicts=True)` + 部分唯一索引 `uniq_peer_event_hall_pull`（5 分钟一轮的慢路不该长出重复行）； 4. 每条都带 `idempotency_key`（模型必填列；随机值，本行不是 outbox）； 5. 与响应**同一事务**（调用方 `transaction.atomic()`；写失败 ⇒ 整个端点失败，否则「看见了却没证据」= 点一次拒一次的僵尸）； 6. ⛔ **绝不写**汇总行 / 整厅行：`target_ref` 永远是**单张卡**的号。
    """
    from apps.business.models import PeerSyncEvent

    unique_refs = list(dict.fromkeys(ref for ref in refs if ref))
    if not unique_refs:
        return
    PeerSyncEvent.objects.bulk_create(
        [
            PeerSyncEvent(
                binding_role="downstream",
                binding_id=str(binding.pk),
                direction="in",
                kind=HALL_PULL_KIND,
                target_type="hall",
                target_ref=ref,
                status=PeerSyncEvent.Status.DONE,
                reason="",
                idempotency_key=uuid.uuid4().hex,
            )
            for ref in unique_refs
        ],
        ignore_conflicts=True,
    )


class PeerHallListView(APIView):
    """`POST /api/business/peer-sync/hall/list/`：上家按绑定令牌返回**共享给这一家**的在厅卡。 ## 鉴权与类型闸（承重） - 只认 Bearer；命中的绑定由认证类挂在 `request._peer_binding` 上（⛔ `request.auth` 恒为 `None`，照 `PeerInboundEventsView` / `PeerReconcileView` 的既有写法）； - ⛔ **必须** `isinstance(binding, PeerDownstreamBinding)`：两张表共用一套令牌校验，上游令牌也能通过认证 ⇒ 不做类型闸，**下家**能拿自己的令牌拉走本租户的厅；其它类型 ⇒ 响亮业务码 `1020`（照三支分流），⛔ 不 500。 ## 可见性三闸 + 三态（计划 §1.2） 1. 请求方绑定 `state=active`（认证已保证，这里仍显式判）； 2. 本方向 `hall_push_enabled=True`； 3. 数据源 = 原生 `WAITING_GRAB`（真的可抢：MAIN 行 + 未接手）∪ 镜像 `listed` （归属绑定未解绑 / 启用 / 接收开关开着，照 `peer_hall_queryset`）。
    | 情况 | 响应 | 下家怎么读 | |---|---|---| | 闸 2 关着 / 没有共享关系 | `shared=false` | **不判 gone** | | 共享、真空厅 | `shared=true, rows=[], count=0` | 可判 gone（还要过 10 分钟陈旧闸） | | 网络 / 业务码 / 形状失败 | 无响应 | 整轮作废，不判 gone | ## 分页与载荷 - keyset 游标 `(进厅时刻, 来源档位, pk)`：原生 `Order.created_at` + rank 0，镜像 `PeerHallMirror.first_seen_at` + rank 1；两表各查一次再归并； - `cursor` 是**不透明串**（只校验长度 / 字符集，⛔ 不当整数解析）； `limit` 上限 200、缺省 100，非整数 / 负数 / bool ⇒ `1000`； - 载荷按来源用现成函数：原生 `peer_sync.order_payload`（白名单，无 `total_price`）、镜像 `peer_dispatch.mirror_hall_payload`（本层展示价 + 行 hop + 1）；
    ⛔ 不直接回 `PeerHallMirror.payload`（会漏上家成本价、抹掉中间层加价）； - `complete` = 本页没有 `next_cursor`（⛔ 不拿 `count` 判，会抖）。 ⚠️ 权限（`PeerTokenAuthentication` + `IsAuthenticated`）与页面会话端点**刻意不同**：本端点登记在 `test_no_route_500.TOKEN_ONLY_ROUTES` 里；它是机器对机器、无操作日志（`DELIBERATELY_NOT_LOGGED`，理由同 `bind/` / `reconcile/`）。
    """

    authentication_classes = [PeerTokenAuthentication]
    permission_classes = [IsAuthenticated]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        request_id = request.headers.get(HALL_PULL_REQUEST_ID_HEADER)
        if getattr(request, "_peer_binding", None) is not None and valid_hall_pull_request_id(request_id):
            hall_pull_trace_logger.info(
                "大厅拉取应答 request_id=%s phase=response_prepared status=%s",
                request_id, response.status_code,
            )
        return response

    def post(self, request):
        from django.db import transaction
        from django.db.models import Q

        from apps.business import peer_dispatch
        from apps.business.models import (
            EmpType,
            Order,
            OrderStatus,
            PeerDownstreamBinding,
            PeerHallMirror,
        )

        binding = getattr(request, "_peer_binding", None)
        if not isinstance(binding, PeerDownstreamBinding):
            # ⛔ 别删这一支：上游令牌也能过认证（两张表共用一套校验），不做类型闸
            # ⇒ 下家把自己的令牌发过来就能拉走本租户的厅。
            return api_response(
                data=None,
                msg="无法识别这条令牌对应的绑定类型",
                code=ErrorCode.PEER_REJECTED,
            )
        if (
            binding.state != PeerDownstreamBinding.State.ACTIVE
            or not binding.hall_push_enabled
        ):
            # 闸 1 / 闸 2：**不共享**不是「空厅」——下家据此**不判 gone**。
            return api_response(
                data={
                    "shared": False, "rows": [], "next_cursor": None,
                    "count": 0, "complete": False,
                }
            )

        raw = request.data
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            return api_response(
                data=None, msg="请求体必须是一个 JSON 对象", code=ErrorCode.VALIDATION
            )
        try:
            limit = hall_pull_limit(raw.get("limit"))
            cursor_value = raw.get("cursor")
            cursor = (
                None if cursor_value is None else decode_hall_cursor(cursor_value)
            )
        except ValueError as exc:
            return api_response(data=None, msg=str(exc), code=ErrorCode.VALIDATION)

        native_qs = Order.objects.filter(
            status=OrderStatus.WAITING_GRAB,
            # 与大厅读口 `search_hall` 同一条「真的可抢」判据：有 MAIN 行、还没人接。
            # ⛔ 不放宽成裸 `WAITING_GRAB`：那样会把**抢不动的卡**共享出去（下家点一次拒一次）。
            performances__emp_type=EmpType.MAIN,
            performances__user__isnull=True,
        # ⭐ ：隐藏单是本租户**镜像卡**的同步载体，不是原生在厅单
        # ⇒ 不共享给下家（共享它会把同一张卡重复抛一次）。
        ).exclude(hall_card__isnull=False).exclude(
            upstream_hall_card__isnull=False
        ).select_related("game")
        mirror_qs = PeerHallMirror.objects.filter(
            status=PeerHallMirror.Status.LISTED,
            peer_upstream_binding__deleted_at__isnull=True,
            peer_upstream_binding__enabled=True,
            peer_upstream_binding__hall_sync_enabled=True,
        ).select_related("game", "peer_upstream_binding")

        # `count` = 共享给这一家的**总数**（三闸之后、游标之前；计划 §1.2 的 `count` 口径）。
        count = native_qs.count() + mirror_qs.count()

        page_limit = limit + 1          # 多取一行判「还有没有下一页」（⛔ 不靠 count 判）
        if cursor is not None:
            moment, rank, pk = cursor
            if rank == 0:
                native_qs = native_qs.filter(
                    Q(created_at__gt=moment)
                    | Q(created_at=moment, pk__gt=pk)
                )
            else:
                native_qs = native_qs.filter(created_at__gt=moment)
            if rank == 0:
                mirror_qs = mirror_qs.filter(first_seen_at__gte=moment)
            else:
                mirror_qs = mirror_qs.filter(
                    Q(first_seen_at__gt=moment)
                    | Q(first_seen_at=moment, pk__gt=pk)
                )
        native_rows = list(native_qs.order_by("created_at", "pk")[:page_limit])
        mirror_rows = list(mirror_qs.order_by("first_seen_at", "pk")[:page_limit])
        merged = [
            (order.created_at, 0, order.pk, order) for order in native_rows
        ] + [
            (row.first_seen_at, 1, row.pk, row) for row in mirror_rows
        ]
        merged.sort(key=lambda item: (item[0], item[1], item[2]))
        page_items = merged[:limit]
        next_cursor = None
        if len(merged) > limit:
            moment, rank, pk, _obj = page_items[-1]
            next_cursor = encode_hall_cursor(moment, rank, pk)

        rows: list[dict] = []
        for _moment, rank, _pk, obj in page_items:
            if rank == 0:
                ref = effective_peer_ref(obj)
                if not ref:
                    continue
                payload = order_payload(obj)
            else:
                ref = obj.peer_order_no
                payload = peer_dispatch.mirror_hall_payload(obj)
            rows.append({"target_ref": ref, "payload": payload})

        data = {
            "shared": True,
            "rows": rows,
            "next_cursor": next_cursor,
            "count": count,
            "complete": next_cursor is None,
        }
        # ⭐ Z20 证据与响应**同一个事务**：写失败 ⇒ 整个端点失败（计划 §2 的三审补条）。
        # ⚠️ 先过闸、只对 `rows` 里真正要回的行写（上面对 `ref` 的空值过滤也算闸的一部分）。
        with transaction.atomic():
            _write_pull_evidence(binding, [row["target_ref"] for row in rows])
            response = api_response(data=data)
        return response


def _inbound_failure_text(total: int, hard_count: int, first_reason: str) -> str:
    """下家侧 `last_error` 的**人话**整句（spec §16.7 第 1 条）。 ⚠️ 只写**两条计数 + 第一条原因**：一批里可能有几十条明细，全塞进 `last_error`
    （`TextField`）既排不了障，也会把页面上那一行撑爆。逐条明细在响应的 `skipped_details` 里，**发送侧**看得见（spec §16.5）。
    """
    return (
        f"上家推来的这批事件共 {total} 条，有 {hard_count} 条没能接收；"
        f"第一条原因：{first_reason}"
    )


def _record_inbound_health(binding, outcome) -> None:
    """把一批事件的处置结果写进**下家自己那条绑定**的健康列（spec §16.7 第 1 条）。 ⭐ 补的是 P1 缺口：绑定页那块红字提示（裁决 R20）读的就是这四个列（`last_ok_at` / `last_error` / `last_error_at` / `consecutive_failures`），而 P1 里它们**没有任何写入者**——那时唯一的写入点（投递内核）写的是**上家侧**的 `PeerDownstreamBinding`（该内核已随 spec §16.6 搬进 `apps/business/peer_worker.py`）。这里给它们真数据（下家侧的 `PeerUpstreamBinding`）。
    ⚠️ **只有 `stale` 算干净**（spec §16.5）：它是"对端已有更新的"的良性跳过。把它算失败会让**重投**把 `consecutive_failures` 一路推上去、页面凭空亮红字（正是 A19 不许的假报警）。 ⚠️ 硬跳分支**有意不刷新 `last_ok_at`**：它的语义是"最近一次**成功**收到"，硬跳批次一条都没收下 ⇒ 刷新它等于对页面上的人说假话（页面 #8 那块会显示假的"最近收到"）。 ⚠️ 两条分支都用**显式 `update_fields`**：⛔ 不能整行 `save()`——那会把这批事件期间
    由别处刷新的列（例如握手换掉的令牌）**静默写回旧值**。 ⚠️ 这些是**普通列写**，批的事务边界不在本函数：调用方（接收端点）那个 `transaction.atomic()` 照旧管着整批（spec §16.7 的"不做额外事务"）。
    """
    hard = [detail for detail in outcome.skipped_details if detail["code"] != SKIP_STALE]
    if not hard:
        binding.last_ok_at = timezone.now()
        binding.consecutive_failures = 0
        binding.save(update_fields=["last_ok_at", "consecutive_failures"])
        return
    binding.last_error = _inbound_failure_text(
        outcome.applied + outcome.skipped, len(hard), hard[0]["reason"]
    )
    binding.last_error_at = timezone.now()
    binding.consecutive_failures += 1
    binding.save(
        update_fields=["consecutive_failures", "last_error", "last_error_at"]
    )


def _outcome_to_dict(outcome) -> dict:
    """把 `Outcome` 组装成响应字典（**两个方向共用**，⛔ 不复制两份组装逻辑）。 ⚠️ **返回形状**是 `{"applied": int, "skipped": int, "skipped_reasons": [{"target_ref", "reason"}], "skipped_details": [{"target_ref", "code", "reason"}]}`：⛔ 计数之外多出的键
    不是装饰——只加计数等于**静默拒收**（spec §10.2「留原因」）。`skipped_reasons` 是 P1 的人读形状（对账 / 排障直接可见），`skipped_details` 是 P2 新增的**机器契约**（发送侧按 `code` 分流，spec §16.5）；两者由同一份明细派生，逐条一一对应。
    ⭐ **D-0347**：上行 `hall.claim` 有判决时多一个 `"claim_replies"` 键（同步抢单的响应车，见 `peer_inbound.Outcome.claim_replies`）；**空 ⇒ 键不出现**（老下家的响应逐字节不变）。
    """
    result: dict = {
        "applied": outcome.applied,
        "skipped": outcome.skipped,
        "skipped_reasons": [
            {"target_ref": detail["target_ref"], "reason": detail["reason"]}
            for detail in outcome.skipped_details
        ],
        "skipped_details": outcome.skipped_details,
    }
    if outcome.claim_replies:
        result["claim_replies"] = list(outcome.claim_replies)
    return result


def receive_events(binding, events) -> dict:
    """把一批事件落到本地（spec §10.4 / §16.2 / §16.3）。 ⚠️ 落库规则已整体搬进 `apps/business/peer_inbound.py::apply_events`（那里有全部判据与代价说明）；本函数是 P1 以来的**函数入口**（`tests/test_peer_order_push.py` 直接调它） ⇒ 名字与位置保留，这里只把 `Outcome` 组装成响应字典（复用 `_outcome_to_dict`）。 ⚠️ **本函数还会写绑定自己的健康列**（spec §16.7 第 1 条）：写的是**下家侧** `PeerUpstreamBinding` 那一行（页面差异表 #7 的红字提示读它）。⛔ **不碰上家侧的**
    `PeerDownstreamBinding`——那一份是发送方的账（`apps/business/peer_worker.py` 写）；上行支（`apply_upstream_events`）同理**一个字都不写它**。 ⚠️ 批的事务边界在**调用方**（接收端点那个 `transaction.atomic()`）；本函数只做 "落库 + 记健康"。
    """
    outcome = apply_events(events, binding=binding)
    _record_inbound_health(binding, outcome)
    return _outcome_to_dict(outcome)


# ══ 大厅**团团通镜像单**接单端点（D-0303，spec §4.1 / §7）═══════════════════════ 路径：`POST /api/business/hall/peer/<pk>/claim/`。 ⭐ D-0347 起（业务方拍板「能抢就是抢到 抢不到就是抢不到」）本端点是**同步抢单**：
# 领域层 `peer_hall.claim_peer_mirror_sync` 在请求内把 `hall.claim` 直推对端（≤3 秒）——受理 ⇒ 当场转正成交（`status="promoted"`）；回绝 ⇒ 当场 1012；给不出结论 ⇒ 当场放回大厅 + 1017。⛔ 不再有「接单中」的成功响应：成功 = 已成交。机器路径与
# 多跳转发照旧走 outbox（`queue_claim=True` 缺省），不受本改动影响。


class PeerHallClaimView(APIView):
    """在我方大厅接一张**团团通上家**的镜像单（spec §4.1）。 ## 越权面 = 0（照 `JidanpuUpstreamHallClaimView` 同一条判据） - `user_id` 取 `request.user`；⛔ **一个字节的请求体都不读**； - **游戏权限**这道数据卡点在写侧也必须有（读侧大厅列表用 `scope_queryset(..., game_scope("game_id"))` 收窄，写侧漏了它就会**越权接单 + 越权计酬**——转正会建出该游戏的真订单并写 MAIN 绩效）。收窄必须在**调领域层之前**
    做：领域层按 `pk` 取行、不认权限。取不到行时的返回口径与「这一行不存在」**逐字同形** （fail-closed、不泄露存在性）。 ## 失败口径 - 领域层的**预期**业务失败（`ClaimError`：已被接走 / 对端未响应 / 过不了闸）⇒ 原样把 `msg` + 业务码交回去； - **成功 = 已成交**（⭐ D-0347 同步直推：响应车带 `order.assign` 回来、当场转正），
    响应带行状态（`status="promoted"`）； - **未预期**的错误照旧**响亮上行**（⛔ 不伪装成业务结果，E-0010）。
    """

    permission_classes = [has_node("grab_hall.take_order")]

    def post(self, request, pk):
        from apps.business import peer_hall
        from apps.business.claim import ClaimError
        from apps.business.models import PeerHallMirror
        from apps.core.scoping import scope_queryset
        from apps.business.game_scope import game_scope

        visible = scope_queryset(
            request.user,
            PeerHallMirror.objects.filter(pk=pk),
            scope_filter=game_scope("game_id"),
        ).first()
        if visible is None:
            from apps.business.jidanpu_common import ALREADY_TAKEN_MESSAGE

            return api_response(
                data=None,
                msg=ALREADY_TAKEN_MESSAGE,
                code=ErrorCode.ORDER_ALREADY_TAKEN,
            )
        # ⭐ 押金闸（热修）：**必须**显式传这条请求自己的判据——判据唯一出处 `deposit_gate.deposit_enforced_for`（总闸 或 App 专属闸），
        # ⛔ 不传就会落回「只读租户总闸」的老口径 ⇒ App 端接镜像单**不冻结押金** （某租户真机：无押金打手接走了上家卡）。与上家鸡蛋铺镜像单那条同一道判据。
        from apps.business.deposit_gate import deposit_enforced_for

        try:
            row = peer_hall.claim_peer_mirror_sync(
                user_id=request.user.id, mirror_id=pk,
                enforce_deposit=deposit_enforced_for(request),
            )
        except ClaimError as exc:
            return api_response(data=None, msg=str(exc), code=exc.code)
        return api_response(
            data={
                "mirror_pk": row.pk,
                "peer_order_no": row.peer_order_no,
                "status": row.status,
                "status_text": row.get_status_display(),
            }
        )
