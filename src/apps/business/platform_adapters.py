"""交易平台适配层（多平台骨架，dl-F）。 ## 这个模块回答的问题 `PlatformIntegration.Platform` 里已经有 13 个平台 id，但**只有代练通（10）**真有客户端。在 dl-F 之前，抛单 / 状态回读 / 撤回 / 改价 / 补时 / 留言 / 自动回复 / 接手后发送这些调用点都直接 `platform=10` 或直接 import `dailiantong_*` —— 想接第二个平台，得把每条链路翻一遍，而且很容易漏掉其中一条、把代练通的凭据发到别的平台去。本模块把「按平台而异的能力」收在一个窄接口 `PlatformAdapter` 后面： - `get_adapter(platform_id)`：**已接入**的平台返回适配器；**未接入**的平台抛 `PlatformNotConnected`（中文「该交易平台尚未接入…」）——**零上游请求**；
- 调用点只认适配器，不再自己判断平台 id / 自己挑客户端。 ## 代练通适配器 = 纯转调（⛔ 不复制逻辑） `DailiantongAdapter` 的每个方法**只做一件事**：把 `platform` 参数补成 10，转调现有的 `dailiantong_push` / `dailiantong_actions` / `dailiantong_auto_reply` / `dailiantong_qr` 里的同名函数。业务判据 / 前置闸 / 上游调用 / 回读 / 审计**全部留在原模块**： - ⛔ 这里不判断订单状态、不算钱、不读凭据； - ⛔ 这里不写库（唯一的例外是 `latest_active_push` 的**只读查询**）。方法名与现有公开函数**逐字相同**（`withdraw_push` / `modify_push_price` / …）。这不是强迫症：`tests/test_operation_log_coverage.py` 按**调用的名字**跟埋点链
（`withdraw_push` 等已登记为跨模块转发名），改名会让四条平台动作端点报「声称已埋点、代码里找不到埋点」。 ## 未接入平台的行为（逐条） | 场景 | 行为 | |---|---| | `get_adapter(未接入 id)` | 抛 `PlatformNotConnected`（中文、可展示），**不构造任何上游客户端** | | 单笔动作（撤回 / 改价 / 补时 / 留言） | 该单**最新一条有效推送行**是未接入平台 ⇒ 中文错误、零上游请求 | | 批量动作（撤回 / 验收 / 同步） | 逐笔分派：未接入平台那一笔回中文错误，其它笔照常做（部分成功如实回） | | 抛单 / 预览 | 平台入参是未接入平台 ⇒ 中文错误、零上游请求（预览额外支持 `?platform=`） | | 同步命令轮询 | 未接入平台的行**跳过**（不计失败、不算「轮询成功」），汇总行报「未接入平台跳过 N」 | | 自动回复 / 接手后发送 | 未接入平台的行跳过，并把可读原因写进 `auto_reply_last_error` / `qr_last_error`（同值不重复写） | ## 注册表（显式，⛔ 不做自动发现） `_ADAPTERS` 是**显式登记**的：今天只有代练通。接入新平台时在这里（或该平台自己的模块里）调 `register_adapter()`。测试用同一个口子登记**假适配器**来验证「按行分派」， `unregister_adapter()` 负责还原（⛔ 不要靠 patch 私有字典）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from apps.business import dailiantong_actions as actions_module
from apps.business import dailiantong_auto_reply as auto_reply_module
from apps.business import dailiantong_push as push_module
from apps.business import dailiantong_qr as qr_module
from apps.business.dailiantong_push import DailiantongPushError
from apps.business.models import DailiantongPush, PlatformIntegration

#: 代练通平台 id（与 `PlatformIntegration.Platform.DAILIANTONG` 同源，⛔ 不另写一个 10）。
PLATFORM_DAILIANTONG = int(PlatformIntegration.Platform.DAILIANTONG)

#: 默认平台。**只用于「还没有任何推送行」的调用点**（例如首次预览 / 首抛）：
#: 那种情况下没有「行上的 platform」可看，只能落到本版唯一接入的平台。
#: ⚠️ 一旦有推送行，调用点必须用 `adapter_for_order()` 按**行上的 platform** 取适配器。
DEFAULT_PLATFORM = PLATFORM_DAILIANTONG


class PlatformNotConnected(DailiantongPushError):
    """该交易平台尚未接入（中文、可直接展示给业务方；⛔ 不是 500）。继承 `DailiantongPushError` 是**有意的**：所有平台动作的接口层已经统一 catch 它并翻
    `code=1002` 的中文信封（`dailiantong_action_views._service_failure`），批量动作也按它
    逐笔回失败原因 —— 未接入平台走同一条失败出口，⛔ 不需要给接口层再加一种异常类型。"""


@runtime_checkable
class PlatformAdapter(Protocol):
    """一个交易平台的**能力集合**（方法签名以现有代练通实现为准）。 ⚠️ 本协议只描述「调用点会用到什么」，⛔ 不描述「平台一定支持什么」：某个平台没有
    `send_accept_qr` 能力时，调用点先看 `supports_accept_send`，⛔ 不要靠
    `NotImplementedError` 兜底（那会变成 500）。"""

    platform_id: int
    platform_name: str
    #: 有没有「接手后发图 + 话术」这套能力（未接入平台按 False）。
    supports_accept_send: bool
    #: 有没有「按规则自动回复」这套能力（未接入平台按 False）。
    supports_auto_reply: bool

    def push_order(self, order, **kwargs) -> DailiantongPush: ...

    def preview_push(
        self, order, *, zone_server_id: str | None = None, memo: str | None = None
    ) -> dict: ...

    def refresh_push_status(self, push, *, client=None) -> DailiantongPush: ...

    def withdraw_push(
        self,
        order,
        *,
        operator,
        comment: str | None = None,
        pay_level_bal=None,
        rep_ensure_bal=None,
        platform: int | None = None,
        auto_arbitrate: bool = False,
    ): ...

    def modify_push_price(self, order, *, operator, price): ...

    def adjust_push_time(self, order, *, operator, hours): ...

    def add_push_message(self, order, *, operator, msg: str): ...

    def accept_push(self, order, *, operator): ...

    def sync_push(self, order, *, operator): ...

    def manual_status_denial(self, order) -> str | None: ...

    def platform_account_summary(self, *, force_refresh: bool = False) -> dict: ...

    def list_zones(self, query: str = "", *, force_refresh: bool = False) -> list[dict]: ...

    def list_zones_for_game(self, game_id: int) -> list[dict]: ...

    def default_zone_for_game(self, game_id: int) -> dict: ...

    def dispatch_for_push(self, push): ...

    def send_accept_qr(self, push): ...


class DailiantongAdapter:
    """代练通（平台 id=10）：现有实现的**纯转调**（⛔ 不复制任何业务逻辑）。"""

    platform_id = PLATFORM_DAILIANTONG
    platform_name = "代练通"
    supports_accept_send = True
    supports_auto_reply = True

    # ── 抛单 / 预览 ─────────────────────────────────────────────────
    def push_order(self, order, **kwargs) -> DailiantongPush:
        return push_module.push_order(
            order, platform=self.platform_id, **kwargs
        )

    def preview_push(
        self, order, *, zone_server_id: str | None = None, memo: str | None = None
    ) -> dict:
        return push_module.preview_push(
            order, zone_server_id=zone_server_id, memo=memo
        )

    # ── 状态回读 / 同步 ─────────────────────────────────────────────
    def refresh_push_status(self, push, *, client=None) -> DailiantongPush:
        return push_module.refresh_push_status(push, client=client)

    def sync_push(self, order, *, operator):
        return actions_module.sync_push(
            order, operator=operator, platform=self.platform_id
        )

    # ── 四个平台动作 ────────────────────────────────────────────────
    def withdraw_push(
        self,
        order,
        *,
        operator,
        comment: str | None = None,
        pay_level_bal=None,
        rep_ensure_bal=None,
        platform: int | None = None,
        auto_arbitrate: bool = False,
    ):
        return actions_module.withdraw_push(
            order,
            operator=operator,
            comment=comment,
            platform=self.platform_id if platform is None else platform,
            pay_level_bal=pay_level_bal,
            rep_ensure_bal=rep_ensure_bal,
            auto_arbitrate=auto_arbitrate,
        )

    def modify_push_price(self, order, *, operator, price):
        return actions_module.modify_push_price(
            order, operator=operator, price=price, platform=self.platform_id
        )

    def adjust_push_time(self, order, *, operator, hours):
        return actions_module.adjust_push_time(
            order, operator=operator, hours=hours, platform=self.platform_id
        )

    def add_push_message(self, order, *, operator, msg: str):
        return actions_module.add_push_message(
            order, operator=operator, msg=msg, platform=self.platform_id
        )

    def accept_push(self, order, *, operator):
        return actions_module.accept_push(
            order, operator=operator, platform=self.platform_id
        )

    def manual_status_denial(self, order) -> str | None:
        return actions_module.manual_status_denial(
            order, platform=self.platform_id
        )

    # ── 账号 / 区服（代练通专属能力，F1 的协议面）────────────────────
    def platform_account_summary(self, *, force_refresh: bool = False) -> dict:
        return push_module.platform_account_summary(force_refresh=force_refresh)

    def list_zones(self, query: str = "", *, force_refresh: bool = False) -> list[dict]:
        return push_module.list_zones(query, force_refresh=force_refresh)

    def list_zones_for_game(self, game_id: int) -> list[dict]:
        return push_module.list_zones_for_game(game_id)

    def default_zone_for_game(self, game_id: int) -> dict:
        return push_module.default_zone_for_game(game_id)

    # ── 接手后发送 / 自动回复 ───────────────────────────────────────
    def dispatch_for_push(self, push):
        return auto_reply_module.dispatch_for_push(push)

    def send_accept_qr(self, push):
        return qr_module.send_accept_qr(push)


#: 已接入平台的适配器（显式登记；键 = 平台 id）。今天只有代练通。
_ADAPTERS: dict[int, PlatformAdapter] = {
    PLATFORM_DAILIANTONG: DailiantongAdapter(),
}


def _as_platform_id(value) -> int:
    """把入参归一成 int 平台 id；归一不了 ⇒ 中文业务错误（⛔ 不是 ValueError 500）。"""
    if isinstance(value, bool):  # `bool` 是 `int` 的子类，⛔ 别让 True 变成 1
        raise PlatformNotConnected("该交易平台尚未接入（平台 id 不合法）。")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise PlatformNotConnected(
            f"该交易平台尚未接入（无法识别的平台 id：{value!r}）。"
        ) from exc


def register_adapter(adapter: PlatformAdapter, *, replace: bool = True) -> None:
    """登记一个平台适配器（接入新平台 / 测试用假适配器）。 ⚠️ `replace=False` 时重复登记会抛 `ValueError`：**已接入平台被悄悄换掉**是最难查的
    一类事故（调用点还在按老能力跑）⇒ 调用方必须显式说要覆盖。"""
    platform_id = _as_platform_id(adapter.platform_id)
    if not replace and platform_id in _ADAPTERS:
        raise ValueError(f"平台 {platform_id} 已经有适配器了（要覆盖请显式 replace=True）")
    _ADAPTERS[platform_id] = adapter


def unregister_adapter(platform_id) -> None:
    """注销一个平台适配器（测试还原用；⛔ 生产代码不要调它去「关掉」一个平台）。"""
    _ADAPTERS.pop(_as_platform_id(platform_id), None)


def adapter_or_none(platform_id):
    """未接入平台返回 `None`（调用方要跳过而不报错时用它，例如同步命令）。"""
    try:
        return get_adapter(platform_id)
    except PlatformNotConnected:
        return None


def get_adapter(platform_id) -> PlatformAdapter:
    """按平台 id 取适配器；**未接入平台抛中文错误、零上游请求**。"""
    platform_id = _as_platform_id(platform_id)
    adapter = _ADAPTERS.get(platform_id)
    if adapter is None:
        raise PlatformNotConnected(
            f"该交易平台尚未接入（平台 id={platform_id}），"
            "目前只有代练通（10）支持，请先在平台对接设置里核对平台。"
        )
    return adapter


def latest_active_push(order) -> DailiantongPush | None:
    """该单**最新一条有效推送行**（`pending` / `sent` / `unknown`，任意平台）。
    ⚠️ 与 `dailiantong_actions._latest_active_push` 的区别：那个按**指定平台**取，
    本函数**不筛平台**——调用点要的就是「这行到底是哪个平台的」，再按它取适配器。只读、不写库。"""
    return (
        DailiantongPush.objects.filter(
            order=order,
            state__in=DailiantongPush.ACTIVE_STATES,
        )
        .order_by("-id")
        .first()
    )


def adapter_for_order(order, *, default_platform: int = DEFAULT_PLATFORM):
    """按**推送行上的 platform** 取适配器（没有推送行时落到默认平台）。 ⚠️ 这是「单笔动作」的入口：撤回 / 改价 / 补时 / 留言 / 验收 / 同步 / 手改状态闸
    都先经过它。行上的平台未接入 ⇒ 抛 `PlatformNotConnected`（中文、零上游请求）。 ⚠️ 没有推送行时落到默认平台，是为了让「这张单还没抛过」这一类错误文案
    与改前**逐字一致**（由代练通服务层那句中文给出，⛔ 不在这里另编一句）。"""
    push = latest_active_push(order)
    platform_id = push.platform if push is not None else default_platform
    return get_adapter(platform_id)
