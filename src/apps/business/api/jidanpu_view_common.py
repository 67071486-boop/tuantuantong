"""两条**个人绑定线**视图共用的两个 helper（下家 `jidanpu_member` / 上家 `jidanpu_upstream`）。⚠️ 本模块是**纯搬家**：这两个函数**逐字**来自 `apps/business/api/jidanpu_views.py`（该模块随**租户级**鸡蛋铺线一起删除，处置见 `docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md` §2.3 第 11 条 / §10.1）——⛔ **不改名、不改签名、不改文案、不改行为**（判据：旧文件里这两个名字已无定义、本模块里各恰 1 处）。
⚠️ 为什么不能就地留着：`jidanpu_views.py` 的其余内容（租户级那 4 个视图与序列化器一族）**整份要删**，而这两个 helper 与它们没有共享状态 ⇒ 只能换个中性宿主；为什么不能各自抄一份：`_upstream_error` 的判据**顺序是承重的**（`CaptchaRequiredError` / `SessionExpiredError` 都是 `AuthError` 的子类，必须排在它**前面**，否则「请输验证码」会被说成「账号或密码错误」）⇒ 抄两份的那天就会分叉。
"""
from __future__ import annotations

from copy import copy

from apps.core.error_codes import ErrorCode
from jidanpu.errors import (
    AuthError,
    CaptchaRequiredError,
    ConfigError,
    JidanpuError,
    ServerError,
    SessionExpiredError,
    TransportError,
)


def _with_password(binding, password):
    """给这一行绑定的**内存副本**换上页面上此刻填的密码（D-0177）；空串 / 没有行 ⇒ 原样回。⭐ 为什么：业务方要求「密码用页面上此刻填的值」（**未保存也能查**）——直接把请求里的密码写进那一行再查，会把「点一下测试连接」变成一次**落库**（用户只是想知道能不能连上），所以只在**一份副本**上覆盖，原行一个字节都不动。
    三条边界（逐条是判据，见 `tests/test_jidanpu_source_picker.py`）：① ⛔ **不落库**（`copy.copy` 出来的对象**不 save**，调用方也拿不到它去 save）；② ⛔ **不回显**（只喂给 `inspect_binding*`，那里只把它交给 SDK 建连）；③ ⛔ **换不了目标**（`base_url` / `account` **原样**从那一行带过来 ⇒ 打不到别的鸡蛋铺，「不许当跳板」红线的判据是**地址**）。
    """
    typed = (password or "").strip()
    if not typed or binding is None:
        return binding
    target = copy(binding)
    target.password = typed
    return target


def _upstream_error(exc: JidanpuError) -> tuple[int, str]:
    """SDK 异常 →（业务码，**人话**）。错误码不新开（spec §6 末段）。⛔ **不回显上游原文**：上游的 `msg` 是第三方文本，它回显我们发过去的东西是完全可能的、我们**无法枚举**它不会 ⇒ 一律用固定文案（与 `xgj_views.test_connection` 同一取向）；⛔ 也**不记日志**：失败只回业务码 + 固定文案，凭据没有任何出口（spec §8-S1）。⚠️ 判据顺序承重：`CaptchaRequiredError` / `SessionExpiredError` 都是 `AuthError` 的子类，必须排在它**前面**，否则「请输验证码」会被说成「账号或密码错误」。
    ⚠️ **`ConfigError` 是一个类型、两种含义，这里只管第二种**：① 「**绑定不完整**」（地址 / 账号 / 密码有空）由 `JidanpuTestConnectionView.post` 在**调 SDK 之前**用 `credentials_of(binding) is None` 挡掉、回「还没配置…」，它**到不了**本函数；② 「**SDK 拒绝了配置值**」（如 `base_url` 少 `http(s)://`、端点清单里找不到，抛点见 SDK 的 `config.py` / `client.py` / `registry.py`）——这一支文案必须说**地址 / 值不合法**、⛔ 不能说「还没配置」：租户明明配了、只是打错了字，说成「没配」会让他去重填一遍而不是去改那个错字；两种含义在响应上必须可判别，由 `tests/test_jidanpu_test_connection.py` 钉住。
    ⚠️ **本函数不做第二个成功/失败判据**：上游成败由 SDK 抛不抛异常决定（spec §5.7 / D15），这里只做「异常类型 → 人话」的翻译。
    """
    if isinstance(exc, CaptchaRequiredError):
        # spec §12-1 的头号风险：鸡蛋铺若打开二步验证，这条桥就断了。必须说出来。
        return (
            ErrorCode.PLATFORM_TOKEN_INVALID,
            "鸡蛋铺要求输入登录验证码，本功能无法自动处理——请到鸡蛋铺后台关闭登录验证",
        )
    if isinstance(exc, SessionExpiredError):
        return ErrorCode.PLATFORM_TOKEN_INVALID, "登录状态已失效，请再点一次「测试连接」"
    if isinstance(exc, AuthError):
        return ErrorCode.PLATFORM_TOKEN_INVALID, "登录失败：账号或密码错误，或该账号已被停用"
    if isinstance(exc, ServerError):
        return ErrorCode.PLATFORM_QUERY_FAILED, "鸡蛋铺服务端返回错误，请稍后再试"
    if isinstance(exc, TransportError):
        return ErrorCode.PLATFORM_QUERY_FAILED, "连接鸡蛋铺失败：网络超时或地址不可达"
    if isinstance(exc, ConfigError):
        return ErrorCode.PLATFORM_NOT_CONFIGURED, "鸡蛋铺地址或账号密码不合法"
    return ErrorCode.PLATFORM_QUERY_FAILED, "调用鸡蛋铺失败"
