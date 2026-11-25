"""微信公众号集成的领域层：配置读取 / access_token 行锁刷新 / 回包判成败 / 事件落位。

## 三条承重约束（都有出处，改前先读）

1. **token 行锁刷新（多 worker 安全）**：`select_for_update` + `expires_at` 双查，
   过期才真调微信；⛔ 不用进程内缓存（gunicorn 双 worker 各自缓存互踢烧配额 ——
   malailai 三套缓存并存的教训，D-0268）。
2. **写端点按 `errcode == 0` 判成败**（计划 S1 / E-0087：调用不抛 ≠ 成功）：
   `judge_body(body, write=True)`；读端点按「出现非零 errcode 才算失败」。
   `errcode in (40001, 42001)`（token 失效 / 过期）⇒ 强刷重试**一次**。
3. **回调事件处理只碰本 app 两张表**（计划 T2）：`apply_subscribe_event` 只有
   `WechatMpBinding` 一条 UPDATE；⛔ 不在回调线程查订单等租户业务表。
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Callable

from django.db import transaction
from django.utils import timezone

from apps.wechatmp.client import WechatMpApiClient, WechatMpApiError
from apps.wechatmp.models import WechatMpBinding, WechatMpConfig

logger = logging.getLogger(__name__)

#: token 过期时刻的安全余量（秒）：提前 5 分钟视为过期，避免"拿到就用时刚好过期"。
TOKEN_EXPIRY_MARGIN = 300
#: 余量兜底：微信回的 `expires_in` 异常地小时，至少给 60 秒生命期（否则每次都刷新）。
MIN_TOKEN_LIFETIME = 60
#: 微信"接口调用凭据失效 / 过期"两档：强刷一次重试（计划 S1）。
TOKEN_EXPIRED_ERRCODES = (40001, 42001)

#: 回调 body 的上限（字节）。微信事件包实际都在几 KB 内；超限说明不是微信发来的，
#: 按"解析失败"处理（200 空串，防重试风暴）。Django 全局那道 2.5 MiB 墙在这里太宽。
CALLBACK_BODY_MAX_BYTES = 64 * 1024

_CLIENT: WechatMpApiClient | None = None


def api_client() -> WechatMpApiClient:
    """模块级单例（无状态；transport 只在测试里换 —— 先例 `tests/test_realname_vendor.py`）。"""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = WechatMpApiClient()
    return _CLIENT


# ── 配置读取 ────────────────────────────────────────────────────────────────


def get_config() -> WechatMpConfig | None:
    """本租户的配置行（没有 ⇒ None）。租户上下文**只来自连接**（schema 路由）。"""
    return WechatMpConfig.objects.first()


def get_ready_config() -> WechatMpConfig | None:
    """回调 / 菜单动作的门槛：配置行存在**且** `is_ready()`（启用 + 三件凭据非空）。"""
    config = get_config()
    if config is None or not config.is_ready():
        return None
    return config


def ensure_config() -> WechatMpConfig:
    """读出配置行，没有就建默认行（保存配置端点用）。

    ⚠️ 单行唯一由 `tenant_schema` 上的 DB 约束兜底（S7）：并发的首次保存撞约束会抛
    `IntegrityError`，由调用方翻译成业务码（照 `wecom_qr_app_views` 的先例）。
    """
    config = get_config()
    if config is not None:
        return config
    from django.db import connection

    return WechatMpConfig.objects.create(tenant_schema=connection.schema_name)


# ── 回包判成败（S1 / E-0087 的一处收口）─────────────────────────────────────


def judge_body(body: object, *, write: bool) -> tuple[int | None, str]:
    """微信回包 → `(errcode, msg)`。成功 `(None, "")`；失败 `(errcode 或 None, 人话)`。

    - **写端点**（`write=True`：menu/create、menu/delete、template/send）：
      **必须有 `errcode` 且恰为 0** —— 缺 `errcode` 键的回包**不能**被当成成功
      （失败信封必须被判失败；含糊的信封也不许判成功）。
    - **读端点**（`write=False`：cgi-bin/token、menu/get、sns/oauth2/access_token）：
      官方成功回包**没有** `errcode` 键 ⇒ 缺键 = 成功、出现非零 = 失败。
    """
    if not isinstance(body, dict):
        return None, "微信 API 回包不是 JSON 对象"
    errcode = body.get("errcode")
    errmsg = str(body.get("errmsg") or "")
    if write:
        if errcode is None:
            return None, "微信接口回包缺少 errcode，无法确认成功"
        if errcode == 0:
            return None, ""
        return int(errcode), f"微信接口返回 errcode={errcode}：{errmsg}"
    if errcode is None or errcode == 0:
        return None, ""
    return int(errcode), f"微信接口返回 errcode={errcode}：{errmsg}"


def acquire_access_token(*, config: WechatMpConfig | None = None, force: bool = False) -> tuple[str, str]:
    """取一个可用的 access_token；返回 `(token, err)`。**必须在请求线程**调（查库）。

    行锁刷新（多 worker 安全）：`select_for_update` 锁住配置行 → `expires_at` 双查 →
    过期才真调微信 → 行锁内落库。`force=True`（40001/42001 重试那一次）跳过双查强刷。
    """
    with transaction.atomic():
        base = config if config is not None else get_config()
        if base is None:
            return "", "本租户还没配置公众号，请先到「微信公众号设置」页保存配置"
        row = WechatMpConfig.objects.select_for_update().get(pk=base.pk)
        if (
            not force
            and row.access_token
            and row.token_expires_at is not None
            and row.token_expires_at > timezone.now()
        ):
            return row.access_token, ""
        if not (row.appid and row.appsecret):
            return "", "本租户的公众号配置缺少 AppID / AppSecret"
        try:
            body = api_client().fetch_access_token(appid=row.appid, secret=row.appsecret)
        except WechatMpApiError as exc:
            return "", f"刷新微信 access_token 失败：{exc}"
        errcode, msg = judge_body(body, write=False)
        if errcode is not None:
            return "", msg
        token = str(body.get("access_token") or "")
        if not token:
            # 读端点判完 errcode 还要钉**值**本身：没有 access_token 的"成功"不是成功。
            return "", "微信回包缺少 access_token"
        try:
            lifetime = int(body.get("expires_in"))
        except (TypeError, ValueError):
            lifetime = 7200
        row.access_token = token
        row.token_expires_at = timezone.now() + timedelta(
            seconds=max(lifetime - TOKEN_EXPIRY_MARGIN, MIN_TOKEN_LIFETIME)
        )
        row.save(update_fields=["access_token", "token_expires_at"])
        return token, ""


def _call_with_token_retry(
    action: Callable[[str], dict], *, write: bool
) -> tuple[dict | None, str]:
    """「取 token → 调端点 → 判成败」的完整一轮；40001/42001 ⇒ 强刷重试**一次**（S1）。"""
    for attempt in range(2):
        token, err = acquire_access_token(force=attempt > 0)
        if err:
            return None, err
        try:
            body = action(token)
        except WechatMpApiError as exc:
            return None, str(exc)
        errcode, msg = judge_body(body, write=write)
        if errcode is None:
            return body, ""
        if errcode in TOKEN_EXPIRED_ERRCODES and attempt == 0:
            continue  # 第二轮 acquire_access_token(force=True) 强刷
        return None, msg
    return None, "微信接口在强刷 token 重试后仍然失败"


# ── 菜单动作（T3）───────────────────────────────────────────────────────────

#: 预设菜单（JSON 常量放代码；URL 由请求基址动态拼 —— 计划 T3）。
#: 「绑定账号」→ OAuth 绑定入口；「我的订单」→ m/ 打手页。
MENU_BUTTON_NAMES = ("绑定账号", "我的订单")


def build_preset_menu(base_url: str) -> dict:
    """预设菜单结构。`base_url` 不带尾斜杠（调用方负责）。"""
    return {
        "button": [
            {"type": "view", "name": MENU_BUTTON_NAMES[0], "url": f"{base_url}/api/wechatmp/bind-entry"},
            {"type": "view", "name": MENU_BUTTON_NAMES[1], "url": f"{base_url}/m/"},
        ]
    }


def sync_preset_menu(menu: dict) -> tuple[dict | None, str]:
    """`cgi-bin/menu/create`（写端点：`errcode == 0` 才算成功，S1）。"""
    return _call_with_token_retry(
        lambda token: api_client().menu_create(access_token=token, menu=menu), write=True
    )


def fetch_menu() -> tuple[dict | None, str]:
    """`cgi-bin/menu/get`（读端点）。"""
    return _call_with_token_retry(
        lambda token: api_client().menu_get(access_token=token), write=False
    )


def delete_menu() -> tuple[dict | None, str]:
    """`cgi-bin/menu/delete`（写端点）。"""
    return _call_with_token_retry(
        lambda token: api_client().menu_delete(access_token=token), write=True
    )


# ── OAuth（T4）──────────────────────────────────────────────────────────────


def exchange_openid(*, code: str) -> tuple[dict | None, str]:
    """网页授权 code 换 openid（读端点判据）。成功回包含 `openid`。

    ⚠️ **不取接口 access_token**：`sns/oauth2/access_token` 直接收 appid+secret+code
    （与 `cgi-bin/*` 那一族是**两套凭据**，⛔ 不共用参数模板）——套
    `_call_with_token_retry` 会白打一趟 `cgi-bin/token`，首跑实测就是这么暴露的。
    """
    config = get_ready_config()
    if config is None:
        return None, "本租户的公众号配置未启用或不完整"
    try:
        body = api_client().oauth_access_token(
            appid=config.appid, secret=config.appsecret, code=code
        )
    except WechatMpApiError as exc:
        return None, f"微信授权失败：{exc}"
    errcode, msg = judge_body(body, write=False)
    if errcode is not None:
        return None, msg
    openid = str(body.get("openid") or "")
    if not openid:
        # 同 acquire_access_token 的口径：判完信封还要钉**值**（没有 openid 的成功不是成功）。
        return None, "微信授权回包缺少 openid"
    return body, ""


# ── 回调事件（T2；只碰本 app 两张表）───────────────────────────────────────

_CDATA_TEMPLATE = r"<{tag}><!\[CDATA\[(?P<v>.*?)\]\]></{tag}>"
_PLAIN_TEMPLATE = r"<{tag}>(?P<v>.*?)</{tag}>"


def extract_xml_tag(xml_text: str, tag: str) -> str:
    """从微信 XML 里抽一个标签的文本（先 CDATA 后纯文本）。

    ⚠️ 刻意**不用** `xml.etree`：微信 XML 的格式是固定的少数几个标签，正则抽取天然
    免疫实体展开（billion laughs）那类 XML 攻击面；body 大小另有 64KB 闸。
    ⛔ 返回值**不做任何反转义**：我们只消费 openid / 事件名这类不含实体的值。
    """
    for pattern in (_CDATA_TEMPLATE, _PLAIN_TEMPLATE):
        match = re.search(pattern.format(tag=re.escape(tag)), xml_text, re.DOTALL)
        if match:
            return match.group("v").strip()
    return ""


def apply_subscribe_event(*, appid: str, openid: str, subscribed: bool) -> None:
    """`subscribe` / `unsubscribe` 事件：翻绑定行的 `subscribed` 位。

    ⚠️ 未绑定的微信用户关注 ⇒ **只记日志，不建行**（v1 口径，计划 T2）：
    没有团团通账号可挂，建了行也无处引用。
    """
    updated = WechatMpBinding.objects.filter(appid=appid, openid=openid).update(
        subscribed=subscribed
    )
    if not updated:
        logger.info(
            "微信公众号回调：%s（openid=%s…）但本租户没有绑定行，v1 不建行",
            "关注" if subscribed else "取消关注",
            openid[:8],
        )
