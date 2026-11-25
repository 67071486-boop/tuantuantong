"""微信公众号 API 的 httpx 客户端（**唯一出站点**，D-0268：直接 httpx、⛔ 不引 wechatpy）。

## 打桩形态（照 `apps/realname/vendor.py` / `tests/test_realname_vendor.py` 先例）

`httpx.Client` 在构造函数里创建、`transport` 可注入 ⇒ 测试用 `httpx.MockTransport`
换掉 socket 那一层，URL / query / body 全部由**被测代码自己拼**，判据能钉"发出去的形状"。

## 成败判据（E-0087：调用不抛 ≠ 成功）

本模块**只**负责「HTTP 通不通、是不是 JSON」；**业务成败一律由调用方按回包判**
（`services.py` 的 `_judge_errcode` 一处收口）：

| 端点 | 成功回包 | 失败回包 |
|---|---|---|
| `cgi-bin/token` | `{"access_token":…, "expires_in":7200}`（**没有 errcode 键**） | `{"errcode":40013,"errmsg":…}` |
| `cgi-bin/menu/create` / `delete` / `message/template/send` | `{"errcode":0,"errmsg":"ok",…}` | `{"errcode":!=0,…}`（HTTP 仍 200） |
| `cgi-bin/menu/get` | `{"menu":…, "is_menu_open":1}`（**没有 errcode 键**） | `{"errcode":!=0,…}` |
| `sns/oauth2/access_token` | `{…, "openid":…}`（**没有 errcode 键**） | `{"errcode":40029,…}` |

⇒ 通用判据是「**回包里有 `errcode` 键且 ≠ 0 ⇒ 失败**」；写端点（menu/create、
menu/delete、template/send）**另有**「成功必须显式 `errcode == 0`」的更强判据
（计划 S1，在 `services.py` 落地并逐个配「失败信封必须被判失败」的用例）。
"""
from __future__ import annotations

import httpx

#: 微信 API 基址（公开文档常量；留成模块常量便于测试断言）。
API_BASE = "https://api.weixin.qq.com"
#: 单次 HTTP 超时（秒）。与 `apps/business/order_push.py::HTTP_TIMEOUT` 同量级：
#: 这是"订单状态变化不等一个外部 HTTP"的同一纪律。
DEFAULT_TIMEOUT = 10.0


class WechatMpApiError(Exception):
    """HTTP 层失败（非 2xx / 网络错 / 非 JSON）。**业务**失败不用它（调用方按 errcode 判）。"""


class WechatMpApiClient:
    """无状态的微信 API 客户端：access_token 由调用方随每次请求显式带上。

    ⛔ 客户端**不**缓存 token（库表持久化 + 行锁刷新在 `services.py`；
    malailai 的 MemoryStorage 教训：gunicorn 双 worker 各自缓存互踢烧配额）。
    ⛔ 实例**不**带租户凭据字段：appid/appsecret 每次调用传参 —— 租户上下文从连接来，
    别让一个长命对象替连接记住它。
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None, timeout: float = DEFAULT_TIMEOUT):
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def get(self, path: str, params: dict[str, str]) -> dict:
        return self._request("GET", path, params=params)

    def post_json(self, path: str, params: dict[str, str], payload: dict) -> dict:
        return self._request("POST", path, params=params, json_body=payload)

    def _request(self, method: str, path: str, *, params: dict[str, str], json_body: dict | None = None) -> dict:
        url = f"{API_BASE}{path}"
        try:
            response = self._client.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise WechatMpApiError(f"连接微信 API 失败：{exc.__class__.__name__}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise WechatMpApiError(f"微信 API 返回 HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise WechatMpApiError("微信 API 响应不是 JSON") from exc

    # ── 各端点（路径与参数名逐字照微信官方文档；⛔ 各端点不共用参数模板）──────────

    def fetch_access_token(self, *, appid: str, secret: str) -> dict:
        """`GET cgi-bin/token`：刷新接口调用凭据。成功回包**没有** errcode 键。"""
        return self.get(
            "/cgi-bin/token",
            {"grant_type": "client_credential", "appid": appid, "secret": secret},
        )

    def menu_create(self, *, access_token: str, menu: dict) -> dict:
        """`POST cgi-bin/menu/create`：写端点，成功 `errcode == 0`。"""
        return self.post_json(
            "/cgi-bin/menu/create", {"access_token": access_token}, menu
        )

    def menu_get(self, *, access_token: str) -> dict:
        """`GET cgi-bin/menu/get`：读端点，成功回包没有 errcode 键。"""
        return self.get("/cgi-bin/menu/get", {"access_token": access_token})

    def menu_delete(self, *, access_token: str) -> dict:
        """`GET cgi-bin/menu/delete`（⚠️ 官方这条**是 GET**，别照 create 抄成 POST）。

        成功 `errcode == 0`。malailai 的 `menu_delete` 同样用 GET（只读参考实现）。
        """
        return self.get("/cgi-bin/menu/delete", {"access_token": access_token})

    def send_template(self, *, access_token: str, payload: dict) -> dict:
        """`POST cgi-bin/message/template/send`：模板消息，成功 `errcode == 0`。"""
        return self.post_json(
            "/cgi-bin/message/template/send", {"access_token": access_token}, payload
        )

    def oauth_access_token(self, *, appid: str, secret: str, code: str) -> dict:
        """`GET sns/oauth2/access_token`：网页授权 code 换 openid（`snsapi_base`）。

        ⚠️ 它在 **`sns`** 路径下、参数与 `cgi-bin/token` **不同**（`grant_type=authorization_code`
        且必须带 `code`）——同名参数不等于同格式同语义（AGENTS.md §5-E）。
        成功回包含 `openid`、**没有** errcode 键。
        """
        return self.get(
            "/sns/oauth2/access_token",
            {
                "appid": appid,
                "secret": secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
