"""微信小程序「数据分析」的取数客户端（`datacube/getweanalysisappiddailyvisittrend`）。 ## 口径（业务方，计划 `docs/plans/2026-10-23-app-mp-stats.md` §7/§8） - 只给 `suqing` 这一个租户看；开关判断在 `apps/business/app_stats.py`，本模块不认识租户。 - 微信侧是 **T+1**：只能查到**昨天**及以前 ⇒ 窗口 = 最近 `days` 天、止于昨天。
- 端点一次只问一天（`begin_date == end_date`），默认 7 天 = **7 次**调用；token **每次现取**， ⛔ 不缓存、⛔ 不建表、⛔ 不挂定时任务（计划 §8）。 - 字段名用官方原名：`ref_date / session_cnt / visit_pv / visit_uv / visit_uv_new`。 ## 失败一律抛 `WechatStatsError`（⛔ 不用 0 冒充）调用方（`app_stats`）把它翻成 `available=false` + `reason`。三类解析分支都要能分辨：
1. **token 层**：网络失败 / 非 JSON / `errcode` 非 0 / 没有 `access_token`； 2. **响应层**：HTTP 非 200 / 非 JSON / 顶层不是对象 / 没有 `list`； 3. **数据行层**：`list` 为空、`ref_date` 不是那一天、缺 `session_cnt` 等字段、字段不是整数。 ⚠️ **不把 secret / 完整 URL 拼进异常文案**：`secret` 在 token 端点查询串里，异常消息会进接口响应（`reason`）⇒ 只报 errcode / HTTP 状态。日志同理（本模块不打 URL）。"""
import logging
import re
from datetime import date, timedelta

import httpx
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
DAILY_VISIT_TREND_URL = (
    "https://api.weixin.qq.com/datacube/getweanalysisappiddailyvisittrend"
)

#: 单次 HTTP 请求超时（秒）。7 天 = 7 次串行调用，最坏 70s；页面可以接受，⛔ 不加并发
#: （微信对同一 AppID 的 token 获取有频率限制，并发没有收益、只会把失败面放大）。
TIMEOUT_SECONDS = 10

#: 每一天必须有这 4 个数值字段（`ref_date` 单独校验格式）。缺任何一个都算解析失败。
REQUIRED_NUMERIC_FIELDS = ("session_cnt", "visit_pv", "visit_uv", "visit_uv_new")

_DATE_8 = re.compile(r"^\d{8}$")
_DATE_DASHED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class WechatStatsError(Exception):
    """微信数据**预期**失败（未配置 / 网络 / 微信拒绝 / 返回结构不对）。
    `str(exc)` 就是页面上那句 `reason`，所以文案要能给人看、且**不含凭据**。"""


def _config() -> tuple[str, str]:
    """读并校验平台级 `WX_MINI_APP_ID` / `WX_MINI_APP_SECRET`。"""
    app_id = (getattr(settings, "WX_MINI_APP_ID", "") or "").strip()
    secret = (getattr(settings, "WX_MINI_APP_SECRET", "") or "").strip()
    missing = [
        name
        for name, value in (("WX_MINI_APP_ID", app_id), ("WX_MINI_APP_SECRET", secret))
        if not value
    ]
    if missing:
        raise WechatStatsError(
            "小程序数据未配置，缺：" + "、".join(missing) + "（见 settings.WX_MINI_*）"
        )
    return app_id, secret


def _json_object(response: httpx.Response, what: str) -> dict:
    """HTTP 200 + JSON 对象；任何一层不对都抛 `WechatStatsError`（分支 2）。"""
    if response.status_code != 200:
        raise WechatStatsError(f"{what}请求失败（HTTP {response.status_code}）")
    try:
        body = response.json()
    except ValueError:
        raise WechatStatsError(f"{what}返回的不是 JSON") from None
    if not isinstance(body, dict):
        raise WechatStatsError(f"{what}返回结构不对")
    return body


def _errcode_is_failure(body: dict) -> int | None:
    """微信的成功响应**没有** errcode 字段；有且非 0 才是失败。返回 errcode 或 None。"""
    if "errcode" not in body:
        return None
    errcode = body.get("errcode")
    if str(errcode) == "0":
        return None
    return errcode


def _access_token() -> str:
    """现取一个 access_token（分支 1：网络 / 非 JSON / errcode / 缺 token）。"""
    app_id, secret = _config()
    try:
        response = httpx.get(
            TOKEN_URL,
            params={
                "grant_type": "client_credential",
                "appid": app_id,
                "secret": secret,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        # ⛔ 不把异常消息拼进来：URL 里有 secret。
        raise WechatStatsError("微信 access_token 请求失败（网络不通），请稍后重试") from None

    body = _json_object(response, "微信 access_token")
    errcode = _errcode_is_failure(body)
    if errcode is not None:
        logger.warning("小程序 access_token 失败：errcode=%s", errcode)
        raise WechatStatsError(f"微信 access_token 获取失败（errcode：{errcode}）")

    token = str(body.get("access_token") or "").strip()
    if not token:
        raise WechatStatsError("微信 access_token 获取失败（响应里没有 access_token）")
    return token


def _normalize_ref_date(raw) -> date:
    """`ref_date` → `date`。微信历史格式是 `20261022`，也接受 `2026-10-22`。"""
    text = str(raw or "").strip()
    if _DATE_8.match(text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    elif not _DATE_DASHED.match(text):
        raise WechatStatsError("微信数据返回的 ref_date 格式不对")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise WechatStatsError("微信数据返回的 ref_date 不是合法日期") from None


def _numeric(row: dict, key: str) -> int:
    """取一个数值字段（分支 3）：**必须存在**且能转成整数。"""
    if key not in row:
        raise WechatStatsError(f"微信数据缺少字段：{key}")
    value = row.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise WechatStatsError(f"微信数据字段不是整数：{key}") from None


def _one_day(token: str, day: date) -> dict:
    """问一天的访问趋势，返回归一化后的那一行。"""
    try:
        response = httpx.post(
            DAILY_VISIT_TREND_URL,
            params={"access_token": token},
            json={
                "begin_date": day.isoformat(),
                "end_date": day.isoformat(),
            },
            timeout=TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        raise WechatStatsError(f"微信数据接口请求失败（{day.isoformat()}，网络不通）") from None

    body = _json_object(response, "微信数据接口")
    errcode = _errcode_is_failure(body)
    if errcode is not None:
        logger.warning("小程序 datacube 失败：day=%s errcode=%s", day, errcode)
        raise WechatStatsError(
            f"微信数据接口拒绝（{day.isoformat()}，errcode：{errcode}）"
        )

    rows = body.get("list")
    if not isinstance(rows, list):
        raise WechatStatsError("微信数据接口返回结构不对（缺少 list）")
    if not rows:
        # ⚠️ 空 list **不当作 0**：这一天到底是真的没人访问、还是接口漏了，从回包分不出来。
        raise WechatStatsError(f"微信数据接口没有返回 {day.isoformat()} 的数据")

    matched = None
    for row in rows:
        if not isinstance(row, dict):
            raise WechatStatsError("微信数据接口返回的行不是对象")
        if _normalize_ref_date(row.get("ref_date")) == day:
            matched = row
            break
    if matched is None:
        raise WechatStatsError(f"微信数据接口没有返回 {day.isoformat()} 的那一行")

    return {
        # `date` 是页面用的 ISO 日期；`ref_date` 保留官方字段名（同一个值），
        # 免得读接口的人再去猜「ref_date 被改名了没有」。
        "date": day.isoformat(),
        "ref_date": day.isoformat(),
        **{key: _numeric(matched, key) for key in REQUIRED_NUMERIC_FIELDS},
    }


def fetch_daily_visits(*, days: int, today: date | None = None) -> list[dict]:
    """最近 `days` 天（止于**昨天**）的访问趋势，按日期升序。返回 `[{date, session_cnt, visit_pv, visit_uv, visit_uv_new}, …]`，长度恰好 `days`。
    任何一天失败 ⇒ 整块抛 `WechatStatsError`（调用方回 `available=false`）——
    半块数据比没有数据更容易被误读成「那天真的没人」。"""
    if days < 1:
        raise ValueError("days 必须 ≥ 1")
    end = (today or timezone.localdate()) - timedelta(days=1)
    start = end - timedelta(days=days - 1)

    token = _access_token()
    series = []
    day = start
    while day <= end:
        series.append(_one_day(token, day))
        day += timedelta(days=1)
    return series
