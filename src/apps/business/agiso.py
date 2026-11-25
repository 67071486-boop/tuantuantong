"""阿奇索（Agiso）开放平台客户端：只做「闲鱼订单查询」这一件事。 **已实测坐实的事实**（spec §3.2，2026-09-21 无凭证探针）： - 基址 `https://gw-api.agiso.com/aldsIdle`（平台段写 `alds` 会返回 `Error_Code:5 未知的URL`）；
- Header 必须是 `Authorization: Bearer <AccessToken>` + `ApiVersion: 1`，Body 是 `application/x-www-form-urlencoded`； - 公共参数 `timestamp`（秒级，服务端允许 ±10 分钟）与 `sign`；
- 上游错误码实测：`1` = `Invalid access token`；`5` = `未知的URL,请检查`；两个都带 `RequestId`。 - **鉴权先于业务参数校验**（无 token 时即使 tid/sign 乱填也只回 `Error_Code:1`）。 HTTP 用 stdlib `urllib`：本项目没有 `requests`/`httpx` 依赖，**不为此新增依赖**（AGENTS.md §4）。"""
from __future__ import annotations

import hashlib
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://gw-api.agiso.com/aldsIdle"

# 网络层异常必须**全部**翻译成 AgisoError（spec §8：网络异常 → 1017，不得 500）。
# ⚠️ 刻意不写 `except Exception`：那会把**编程错误**（TypeError / AttributeError 之类）
# 也伪装成上游故障，监控就此失明（AGENTS.md §5 与 E-0010）。只列**已知会从网络读写里长出来**的那几族（顺序：具体的在前）。
_NETWORK_ERRORS = (
    urllib.error.URLError,      # 含 HTTPError（下面单独先 catch，为的是能报出状态码）
    http.client.HTTPException,  # ⚠️ **不是** OSError：IncompleteRead / BadStatusLine 只在这里
    OSError,                    # ConnectionResetError / socket.timeout / 连接被拒等
    TimeoutError,
    UnicodeDecodeError,         # 响应体不是合法 UTF-8
    json.JSONDecodeError,
    # ⚠️ `ValueError`：`urllib.request.Request(...)` 对**缺 scheme 的基址**（平台配置写坏，如 `AGISO_API_BASE=no-scheme-here`）抛 `ValueError: unknown url type`
    # （实测 Python 3.13 的 `urllib/request.py:347`），而它既不是 `URLError` 也不是 `OSError` ⇒ 不在这里收干净就会逃逸到视图（只捕 `AgisoError`）＝ 500。
    # 它列进这张表而不是另开 `except Exception`：URL 解析失败与网络读写失败同属「环境/输入坏了」这一族，而编程错误（TypeError / AttributeError）仍要响亮地 500。
    ValueError,
)


class AgisoError(Exception):
    """上游返回的业务失败（`IsSuccess != true`）。"""

    def __init__(self, *, upstream_code: int, upstream_msg: str, request_id: str = "") -> None:
        super().__init__(f"阿奇索返回 {upstream_code}: {upstream_msg}")
        self.upstream_code = upstream_code
        self.upstream_msg = upstream_msg
        self.request_id = request_id


def sign_params(params: dict[str, object], secret: str) -> str:
    """签名：除 `sign` 外所有参数按**参数名 ASCII** 排序 → 拼「名+值」（无分隔符） → `md5(secret + 拼串 + secret)`（UTF-8）。官方文档给的向量见测试。
    ⚠️ 排序的是**参数名**，拼接时**不插分隔符**（`bar2foo1foo_bar3foobar4`）。
    ⚠️ 必须容忍「参数集变化」——不要写成固定字段列表（官方测试工具专门有这条用例）。"""
    items = sorted(
        ((k, v) for k, v in params.items() if k != "sign"), key=lambda kv: kv[0]
    )
    raw = "".join(f"{k}{v}" for k, v in items)
    return hashlib.md5(f"{secret}{raw}{secret}".encode("utf-8")).hexdigest()


def _post_form(*, url: str, params: dict[str, object], headers: dict[str, str], timeout: float) -> dict:
    """发一个 form 请求并回 JSON。**单独一个函数**是为了测试能把桩打在它身上（E-0029）。"""
    body = urllib.parse.urlencode(params).encode("utf-8")
    try:
        # ⚠️ `Request(...)` 的构造**必须在 try 里**：基址缺 scheme 时它抛 `ValueError`
        # （见 `_NETWORK_ERRORS` 的注释）——留在 try 外就是一个 500 的逃逸口。
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 上游 5xx/4xx：按上游失败处理，不当成我们的 500
        raise AgisoError(upstream_code=-1, upstream_msg=f"HTTP {exc.code}") from exc
    except _NETWORK_ERRORS as exc:
        raise AgisoError(upstream_code=-1, upstream_msg=f"网络异常：{exc}") from exc


def _upstream_code(payload: dict) -> tuple[int, str | None]:
    """`Error_Code` → `(整数码, 非数字时的原文)`；**非数字也不得逃逸**。 ⚠️ 直接 `int(payload.get("Error_Code") or 0)` 在码是 `"abc"`（或任何非数字）时抛 `ValueError`，
    而 Task 4 只 `except AgisoError` ⇒ 会 500。转不成整数就按「未知上游码 -1」处理，并把原文交回去写进 msg —— 让人看得见上游到底回了什么。 ⚠️ 判据是**转换失败本身**（第二个元素非 `None`），**不是** `code == -1`：
    `-1`（或字符串 `"-1"`）是**能**转成整数的合法上游码，拿 `-1` 当「非数字」的判据会让 msg 写成「上游 Error_Code 非数字：-1」，与上游实回内容直接矛盾。"""
    raw = payload.get("Error_Code")
    try:
        return int(raw or 0), None
    except (TypeError, ValueError):
        return -1, repr(raw)


def order_detail(
    tid: str,
    access_token: str,
    *,
    app_id: str,
    app_secret: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 10.0,
) -> dict:
    """闲鱼「订单查询」（`POST /Order/Detail`，唯一业务参数 `tid`）。 **契约**：成功时**必然**返回一个 `dict`（上游 `Data` 原样）；任何失败—— 网络层异常、上游 `IsSuccess != true`、响应体不是 JSON 对象、`Data` 不是 JSON 对象——
    一律抛 `AgisoError`，**不返回**别的类型、也不抛别的异常类型（Task 4 只 `except AgisoError`，否则就是 spec §8 禁止的 500）。 ⚠️ `Data` 缺失或非 dict ⇒ `AgisoError`，**不**静默兜成 `{}`：
    「上游没给」与「上游给了一个空的」是两件事，静默替换会让录单页显示空价格而没人知道为什么。 ⚠️ `Data == {}` 是**合法**的（「给了但为空」），原样返回。"""
    params: dict[str, object] = {"tid": tid, "timestamp": int(time.time())}
    params["sign"] = sign_params(params, app_secret)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "ApiVersion": "1",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    payload = _post_form(
        url=f"{base_url.rstrip('/')}/Order/Detail", params=params, headers=headers, timeout=timeout
    )
    # 形状收口：上游回 `[]` / `"ok"` / `123` 这类**非 dict 的 JSON** 时，下面的 `.get(...)`
    # 会抛 `AttributeError` 逃逸。与截断响应、非数字 Error_Code 同一个坑 ⇒ 一并翻译成 AgisoError。
    if not isinstance(payload, dict):
        raise AgisoError(
            upstream_code=-1,
            upstream_msg=f"上游响应不是 JSON 对象：{type(payload).__name__}",
        )
    if not payload.get("IsSuccess"):
        code, non_numeric_repr = _upstream_code(payload)
        msg = str(payload.get("Error_Msg") or "")
        if non_numeric_repr is not None:
            # 上游给的码转不成整数：按未知码 -1 处理，但把原文留在 msg 里让人看得见。
            note = f"上游 Error_Code 非数字：{non_numeric_repr}"
            msg = f"{msg}（{note}）" if msg else note
        raise AgisoError(
            upstream_code=code,
            upstream_msg=msg,
            request_id=str(payload.get("RequestId") or ""),
        )
    # 成功路径同样要守契约：`IsSuccess=true` 但 `Data` 不是 dict 时，
    # `_to_form_fields` 会直接 `data.get("payment")` ⇒ `AttributeError` ⇒ 500。
    # **不要**退回 `or {}` —— 那会把「上游没给」伪装成「上游给空」（两个方向都是静默的）。
    data = payload.get("Data")
    if not isinstance(data, dict):
        raise AgisoError(
            upstream_code=-1,
            upstream_msg=f"上游响应 Data 不是 JSON 对象：{type(data).__name__}",
        )
    return data
