"""闲管家（闲鱼店铺管理平台）开放平台客户端。 **做三件事**：查「订单详情」、查「这家应用能操作的店铺」、以及「无需物流发货」（`logistics_dummy_send`，2026-10-03 自动发货线新增——原句写「只做两件事」，本线起不再成立，按本仓惯例改成如实口径而不是删掉原句）。 ## 已实测坐实的事实（spec §2.1，2026-09-22 无凭证探针，脚本 `.tools/sdk-inspect/probe_xgj*.py`） - 基址 `https://open.goofish.pro` 可达（官方 OpenAPI 的 `servers.url` 也写它，标注「新版正式环境」）； - **业务失败一律 HTTP 200 + `code != 0`**（与阿奇索同形，与本仓统一信封同形）； - **鉴权先于业务参数校验**：`appid` 无效时连 `order_no` 都不看（所以没有真实应用就探不到业务码）； - `100001` 是**复合码**——AppID 无效 / 签名错 / 时间戳越界（±1 小时实测被拒）/ 缺 AppID **四种都回它**，原文分别是「无效的AppID」「签名错误」「请求时间超出允许时间范围」「缺少AppID」
⇒ **判据只能是码本身，不要靠 `msg` 文本分支**； - `100004` = 请求参数错误（缺 `sign` / 缺 `timestamp`；官方文档的异常示例也是它）； - **未知路径回 HTTP 404 纯文本 `404 page not found`**（不是 JSON 信封） ⇒ 响应体不是 JSON 也必须翻译成本模块的异常，否则 `JSONDecodeError` 会逃逸成 500。 ## 真实凭据下的实测（spec §2.4，2026-09-22，**只读**联调 `tests/test_xgj_live.py`） - **签名实现正确**：真实 `app_key`/`app_secret` 调 `user/authorize/list` 成功（签名错只会回 `100001 签名错误`），实测返回一个账号下绑定的店铺列表。 - **`order/detail` 的 `data` 就是订单对象本身**（33 个顶层键，与 `order/list` 元素同构）。 - **上游带回官方文档里没有的字段**：订单里有 `xyb_seller_amount`、店铺列表里有 `seller_id`。 ⇒ 两个出口都**原样透传上游**（不裁剪、不改名）这个决策有了依据。
- **收货人信息只在「待发货」单里出现**：实测一张 `order_status=23`（已退款）的单， `receiver_name` / `receiver_mobile` / `prov_name` / `city_name` 等键**整个不在** （不是空串、也不是 null）⇒ 录单的 `customer_phone` 对这类单**必然为空**。代码里一律用 `detail.get(...) or ""`，不会因此 KeyError。 - `order/list` 的 `data = {list, count, page_no, page_size}`（与文档一致，实测 `count` 会给总数）。 ## 签名（spec §2.3；出处 = 厂商 SDK `xgj/client.py:20-42`） ``` body_md5 = md5(<body 的 JSON 字符串>)          # separators=(',',':')、ensure_ascii=False；空 body ⇒ '{}' sign     = md5(f"{app_key},{body_md5},{timestamp},{app_secret}") ``` ⚠️ **不传 `seller_id`**：官方文档对它的说明是「仅商务对接传入，自研/第三方ERP对接忽略即可」（spec §2.2）。所以签名串**恒为 4 段**，没有 SDK 里那个 `seller_id > 0` 的分支。 ## 两条刻意不照抄 SDK 的地方 1. SDK 的 `debug = True` 是**类属性默认值**⇒ 每次调用都 print 含 `sign` 的 URL。本模块默认**不打印**。 2. SDK 的本地校验失败返回 `json.dumps({"code": 202, ...})` **字符串**而不是抛异常 ⇒ 「我参数写错了」与「服务端返回了 202」走同一个通道。本模块一律抛 `XgjError`。 HTTP 用 stdlib `urllib`：本项目没有 `requests`/`httpx` 依赖，**不为此新增依赖**（AGENTS.md §4）。"""
from __future__ import annotations

import hashlib
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://open.goofish.pro"

#: 上游**凭据或时钟**类失败的码（实测：AppID 无效 / 签名错 / 时间戳越界 / 缺 AppID 都回它）。
#: 视图据此把它映射成 `ErrorCode.PLATFORM_TOKEN_INVALID`（`1016`），其余失败一律 `1017`
#: ——判据是「**等于这个码**」，不枚举其它码（spec §7 / D10）。
AUTH_ERROR_CODE = 100001

#: 网络层异常必须**全部**翻译成 `XgjError`（spec §7：网络异常 → `1017`，不得 500）。 ⚠️ 刻意不写 `except Exception`：那会把**编程错误**（TypeError / AttributeError 之类）
#: 也伪装成上游故障，监控就此失明（AGENTS.md §5 与 E-0010）。顺序上具体的在前；`ValueError` 单列的理由见 `agiso.py` 同名字典的注释（`urllib.request.Request(...)` 对缺 scheme 的基址抛 `ValueError`，而它既不是 `URLError`
#: 也不是 `OSError`）；`json.JSONDecodeError` 覆盖「404 纯文本」那条实测路径。
_NETWORK_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    OSError,
    TimeoutError,
    UnicodeDecodeError,
    json.JSONDecodeError,
    ValueError,
)


class XgjError(Exception):
    """上游返回的业务失败，或任何我们无法把它变成正常结果的失败。"""

    def __init__(self, *, upstream_code: int, upstream_msg: str) -> None:
        super().__init__(f"闲管家返回 {upstream_code}: {upstream_msg}")
        self.upstream_code = upstream_code
        self.upstream_msg = upstream_msg


def sign(*, app_key: str, body_json: str, timestamp: int, app_secret: str) -> str:
    """签名（spec §2.3）。拼串是**逗号分隔的四段**，与厂商 SDK 逐字一致。 ⚠️ `body_json` 必须是**实际发出去的那个字符串**：body 的 MD5 参与签名，
    序列化参数（分隔符 / `ensure_ascii`）换一个就全部签名失败。所以拼 body 的地方
    **只有 `json_body()`**，不要在各调用点自己 `json.dumps`。"""
    body_md5 = hashlib.md5(body_json.encode("utf-8")).hexdigest()
    raw = f"{app_key},{body_md5},{timestamp},{app_secret}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def json_body(body: dict) -> str:
    """请求体 → JSON 字符串。**空字典 ⇒ `'{}'`**（与厂商 SDK `get_json_body` 一致）。
    ⚠️ `separators=(",", ":")`（不留空格）与 `ensure_ascii=False` 是**契约的一部分**：
    它们决定了上面那个 MD5。改这里等于改签名算法。"""
    if body:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return "{}"


def _post_json(*, url: str, body: str, headers: dict[str, str], timeout: float) -> object:
    """发一个 JSON 请求并把响应体解析成 Python 对象。 **单独一个函数**是为了测试能把桩打在它身上（E-0029：桩要打在「被查找的那个模块」上）。
    ⚠️ `Request(...)` 的构造**在 try 里**：基址缺 scheme 时它抛 `ValueError`
    （见 `_NETWORK_ERRORS` 的注释）——留在 try 外就是一个 500 的逃逸口。"""
    try:
        request = urllib.request.Request(
            url, data=body.encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise XgjError(upstream_code=-1, upstream_msg=f"HTTP {exc.code}") from exc
    except _NETWORK_ERRORS as exc:
        raise XgjError(upstream_code=-1, upstream_msg=f"网络异常：{exc}") from exc


def _upstream_code(payload: dict) -> tuple[int, str | None]:
    """`code` → `(整数码, 非数字时的原文)`；**非数字也不得逃逸**。 ⚠️ 直接 `int(payload["code"])` 在码是 `"abc"` 时抛 `ValueError`，而调用方只
    `except XgjError` ⇒ 500。转不成整数就按「未知上游码 `-1`」处理，并把原文交回去写进 msg。 ⚠️ 判据是**转换失败本身**（第二个元素非 `None`），**不是** `code == -1`：
    `-1` 是能转成整数的合法取值，拿它当「非数字」的判据会让 msg 与上游实回内容矛盾（`agiso._upstream_code` 的同一条教训）。"""
    raw = payload.get("code")
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return -1, repr(raw)


def call(
    path: str,
    body: dict,
    *,
    app_key: str,
    app_secret: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 10.0,
) -> dict:
    """调一个 `/api/open/...` 端点并返回信封里的 `data`（**必然是一个 dict**）。 **契约**：成功时必然返回 `dict`；任何失败——网络层异常、非 JSON 响应、`code != 0`、
    `data` 不是 dict——一律抛 `XgjError`，不返回别的类型、也不抛别的异常类型（调用方只 `except XgjError`，否则就是 spec §7 禁止的 500）。 ⚠️ `data == {}` 在**失败**回包里是常态（实测），但**成功**时不会：
    空 `data` 对 `order_detail` 意味着「没这单」，静默兜成 `{}` 会让录单页显示空价格而没人知道为什么 ——所以 `order_detail` / `shop_list` 各自在下面**显式拒绝**空 data。"""
    timestamp = int(time.time())
    body_json = json_body(body)
    query = urllib.parse.urlencode(
        {
            "appid": app_key,
            "timestamp": timestamp,
            "sign": sign(
                app_key=app_key, body_json=body_json, timestamp=timestamp, app_secret=app_secret
            ),
        }
    )
    payload = _post_json(
        url=f"{base_url.rstrip('/')}{path}?{query}",
        body=body_json,
        # 与厂商 SDK 同形；`sdk_version` 是**我们的**标识，不是闲管家的版本号。
        headers={
            "Content-Type": "application/json;charset=utf-8",
            "sdk_version": "tuantuantong-xgj-v1",
        },
        timeout=timeout,
    )
    # 形状收口：上游回 `[]` / `"ok"` / `123` 这类**非 dict 的 JSON** 时，下面的 `.get(...)`
    # 会抛 `AttributeError` 逃逸。与截断响应、非数字 code 同一个坑 ⇒ 一并翻译成 XgjError。
    if not isinstance(payload, dict):
        raise XgjError(
            upstream_code=-1, upstream_msg=f"上游响应不是 JSON 对象：{type(payload).__name__}"
        )
    if "code" not in payload:
        raise XgjError(upstream_code=-1, upstream_msg="上游响应缺少 code 字段")
    code, non_numeric_repr = _upstream_code(payload)
    if code != 0:
        msg = str(payload.get("msg") or "")
        if non_numeric_repr is not None:
            note = f"上游 code 非数字：{non_numeric_repr}"
            msg = f"{msg}（{note}）" if msg else note
        raise XgjError(upstream_code=code, upstream_msg=msg)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise XgjError(
            upstream_code=-1, upstream_msg=f"上游响应 data 不是 JSON 对象：{type(data).__name__}"
        )
    return data


def logistics_dummy_send(
    order_no: str,
    *,
    app_key: str,
    app_secret: str,
    send_way: int,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 10.0,
) -> dict:
    """「无需物流发货」（`POST /api/open/trade/logistics/dummy/send`）→ 信封里的 `data`。业务参数**只有两个**（spec §3.2，依据是业务方 提供的官方文档： query 是 `appid/timestamp/sign`，JSON 体是 `{order_no, send_way}`）： `order_no` = 闲鱼那一侧的订单号（本仓取 `Order.source_order_no`，D2）；
    `send_way` = `1`（仅更新订单状态）/ `2`（同时发内容）——本仓固定 `1`（D6）。 ⚠️ **成功时上游回的 `data` 就是 `{}`**（spec §3.2 的文档口径）⇒ 本函数**只**转发 `call()`，⛔ **不要**学 `order_detail` / `shop_list` 加「空 data = 失败」那一句：
    那两处的空 data 各自有别的含义（没这单 / 没给 list），本端点的空 data 是**正常的成功**。 `call()` 本身只要求 `data` 是 dict（`{}` 放行），所以直接用 `call()` 是对的。 ⚠️ **本端点没有实测过**（spec §2 的证据等级是「业务方提供的官方文档」）⇒ 判据仍是 `call()` 那条既有契约：`code != 0` 抛 `XgjError`。若上游实际用的是别的成功码，症状会是「每次发货都记失败、`remark` 是上游原文」——那是**可见**的失败，不是静默错误。"""
    return call(
        "/api/open/trade/logistics/dummy/send",
        {"order_no": order_no, "send_way": send_way},
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url,
        timeout=timeout,
    )


def order_detail(
    order_no: str,
    *,
    app_key: str,
    app_secret: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 10.0,
) -> dict:
    """「订单详情」（`POST /api/open/order/detail`，唯一业务参数 `order_no`）→ 订单对象。 **`data` 的形状已实测闭合（spec §11-T1，2026-09-22 真实凭据）**： `data` 就是**订单对象本身**（实测 33 个顶层键，与 `order/list` 的元素同构），
    **不是** `{list: [...]}` 包装。所以这里只认一种形状。 ⚠️ 本函数曾经"两种形状都收"（对象 / `list[0]`），那是 T1 未实测时的临时兜底。实测之后**删掉了 `list[0]` 那一支**：保留一个从未被观察到的分支，等于把「上游换了包装」这件事从**响亮的失败**降级成**可能取错对象**（本项目 E-0011 的同族纪律：
    没有依据的分支比不写更糟）。真出现第二种形状时，这里的 `XgjError` 会当场报出来。 ⚠️ **`data == {}` 是「没这单」**（或凭据无权看它）⇒ 抛 `XgjError`，**不**静默兜成 `{}`：「上游没给」与「上游给了一个空的」是两件事，静默替换会让录单页显示空价格而没人知道为什么。"""
    data = call(
        "/api/open/order/detail",
        {"order_no": order_no},
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url,
        timeout=timeout,
    )
    if not data.get("order_no"):
        raise XgjError(
            upstream_code=-1,
            upstream_msg=f"上游响应里没有订单对象（data 的键：{sorted(data)[:8]}）",
        )
    return data


def shop_list(
    *,
    app_key: str,
    app_secret: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 10.0,
) -> list[dict]:
    """「查询闲鱼店铺」（`POST /api/open/user/authorize/list`）→ `data.list` 的**逐行原样**。页面用它做「测试连接」：业务方 R4 的原话是「该应用**仅能调用当前闲管家账号绑定的店铺**」，
    所以「这个应用到底能操作哪些店」必须**看得见**，而不是等录单失败才知道密钥配错了。 ⚠️ **空列表是合法结果**（应用有效但一个店铺都没授权）——与「上游没给 list」不同：
    后者是 `XgjError`。别把两者合并成一个空列表。 ⚠️ 逐行**必须是 dict**：混进非 dict 就抛错，**不静默丢弃** （静默丢行会让页面少显示一家店，而没人知道少了）。"""
    data = call(
        "/api/open/user/authorize/list",
        {},
        app_key=app_key,
        app_secret=app_secret,
        base_url=base_url,
        timeout=timeout,
    )
    rows = data.get("list")
    if not isinstance(rows, list):
        raise XgjError(
            upstream_code=-1, upstream_msg=f"上游响应 data.list 不是数组：{type(rows).__name__}"
        )
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise XgjError(
                upstream_code=-1,
                upstream_msg=f"上游 data.list[{index}] 不是 JSON 对象：{type(row).__name__}",
            )
    return rows
