"""②式（官方开放平台 API）那一套的**共享测试夹具**：零网络假上游 + 0600 密钥文件的临时目录。

## 为什么它单独住一个模块

这一块原先住在 `tests/test_jidanpu_upstream_api.py`（**上家线**的测试模块）里，消费方却是
**下家线**的 `tests/test_jidanpu_member_api.py`——它从那个**测试模块** import 下面这一整块
（`OPENAPI_*` / `OUTER_GATE_MSG` / `INNER_PERMISSION_MSG` / `OPENAPI_GAME_ROW` /
`WRITE_ENDPOINT_NAMES` / `_OpenApiUpstream` / `_patch_openapi_upstream` / `_temp_callback_keys_file`）。
上家线的②式**用例**已于 2026-10-09 撤除（业务方裁定上家不需要这种对接方式）⇒ 这批夹具在
上家这一侧**一个消费方都没有**，却**只服务下家**，于是：

- 谁去清理上家那半边（那个测试模块）谁就会让下家的测试**导入即失败**（`ImportError`）；
- 「测试模块 import 测试模块」把「谁都能删」变成「谁都不敢删」——同一件事被两条线**各记一半**。

⇒ 夹具搬到这个**中立模块**（照 `tests/xlsx_helpers.py` / `tests/p5_auth_helpers.py` /
`tests/order_rules_helpers.py` 的既有形态）：两条线谁都不再持有对方的夹具，谁清理自己那半边
都不会打断另一条线。⚠️ 理由与「两份桩必然漂」是同一条（下家线复用
`tests.test_jidanpu_openapi._Upstream` 也是这个口径）：**桩只许有一份**。

⚠️ 本模块**不是测试模块**（文件名不以 `test` 开头 ⇒ 不被测试发现），只放夹具，⛔ 里面没有用例。
⚠️ 夹具一律**合成值**（`AGENTS.md` §5-C / E-0077）：`…test.invalid` 域名、`*-synthetic-*` 的 id
与密钥；哨兵值只用来搜「有没有泄漏」。⛔ **零真实网络**（`httpx.MockTransport` 只换 socket 那一层）。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import httpx

from apps.business import jidanpu_openapi

#: ②式那一路的合成夹具（⛔ 一个真值都没有）。
OPENAPI_BASE_URL = "https://jidanpu-openapi-api.test.invalid"
OPENAPI_CLIENT_ID = "cid-synthetic-t11-0001"
#: ⭐ **哨兵密钥**：只允许出现在库里（响应 / 逐条表 / 文案里都不许有它）。
OPENAPI_SENTINEL_SECRET = "sentinel-secret-must-not-leak-t11-0001"
#: ⭐ 哨兵 token（上游「发回来」的那一个）：同样不许出现在任何响应里。
OPENAPI_SENTINEL_TOKEN = "sentinel-token-must-not-leak-t11-0002"
#: 实测的对方文案（与 `tests/test_jidanpu_openapi.py` 同源：spec §3.3 F12/F13/F15）。
OUTER_GATE_MSG = "您没有开放平台接入权限"
INNER_PERMISSION_MSG = "没有权限"
#: 一个形状正确的游戏行（`id` 就是 `GetProject` 要的 `gameId`）。
OPENAPI_GAME_ROW = {"id": "game-synthetic-t11-0001", "name": "测试游戏甲"}

#: 写接口的名字（spec §3.3 F14 列的那五个）⇒ ⛔ 探测里一个都不许出现。
WRITE_ENDPOINT_NAMES = (
    "CreateOrder", "ModifyOrder", "ChangeOrderStatus", "UploadImg", "ModifyOrderPrice",
)

#: 真 `httpx.Client`：模块级取好，这样替换 `jidanpu_openapi.httpx.Client` 之后仍能造出**真的**客户端。
_REAL_HTTPX_CLIENT = httpx.Client


class _OpenApiUpstream:
    """②式探测用的**零网络**假上游（真 `httpx.Client` + `MockTransport`，只换 socket 那一层）。

    ⚠️ 与 `tests/test_jidanpu_openapi.py` 的同类桩**同一个手法**（本仓既有做法）：
    请求构造、参数名、`Authorization` 头都是**真的**，桩只决定「对方回什么」。
    ⚠️ 记录每一次请求（含表单）⇒ 判据能钉「发了哪些请求、发的什么形状」。
    """

    def __init__(self):
        self.requests: list[dict] = []
        self.routes: dict[str, tuple[int, object]] = {}
        self.errors: dict[str, Exception] = {}

    def reply(self, path, payload, *, status=200) -> "_OpenApiUpstream":
        self.routes[path] = (status, payload)
        return self

    def boom(self, path, exc: Exception) -> "_OpenApiUpstream":
        self.errors[path] = exc
        return self

    def happy(self) -> "_OpenApiUpstream":
        """四项全通的默认回包（`GetGames` 必须显式给列表：默认体里 `data` 是 `null`）。"""
        return self.reply(jidanpu_openapi.GAMES_PATH, {"code": 1, "msg": None,
                                                      "data": [OPENAPI_GAME_ROW]})

    # ── httpx 接线 ────────────────────────────────────────────────────────────
    def factory(self, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(self.handler), **kwargs)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append({
            "method": request.method,
            "path": path,
            "params": dict(request.url.params),
            "authorization": request.headers.get("authorization"),
            "body": request.content.decode("utf-8") if request.content else "",
        })
        error = self.errors.get(path)
        if error is not None:
            raise error
        if path in self.routes:
            status, payload = self.routes[path]
        elif path == jidanpu_openapi.TOKEN_PATH:
            status, payload = 200, {"access_token": OPENAPI_SENTINEL_TOKEN,
                                    "token_type": "bearer", "expires_in": 3600}
        else:
            status, payload = 200, {"code": 1, "msg": None, "data": None}
        return httpx.Response(status, json=payload)

    # ── 读数 ──────────────────────────────────────────────────────────────────
    def paths(self) -> list[str]:
        return [row["path"] for row in self.requests]


def _patch_openapi_upstream(upstream):
    """把 `jidanpu_openapi.httpx.Client` 换成「走这个假上游的真客户端」（⛔ 零真实网络）。"""
    return mock.patch.object(jidanpu_openapi.httpx, "Client", upstream.factory)


def _temp_callback_keys_file(case) -> Path:
    """把 0600 密钥文件的路径指到一个**临时目录**（T3 的接线用例同款）。

    ⚠️ **必须指走**：默认路径是**生产形态**（`CALLBACK_ROOT=/home/ubuntu/projects/tuantuantong`
    ⇒ `$R/shared/callback-keys.env`）⇒ 不指走的话，任何一次**②式绑定**的保存都会去写那个
    本机不存在的目录并**响亮失败**（那是 T3 的设计：写不进 ⇒ 整个保存回滚，⛔ 不留半态）。
    ⇒ 本机跑②式的用例都得先把它指到临时目录，否则测的是「本机有没有那个目录」。
    """
    root = Path(tempfile.mkdtemp(prefix="tt-t11-keys-"))
    case.addCleanup(shutil.rmtree, root, ignore_errors=True)
    shared = root / "shared"
    shared.mkdir(mode=0o700)
    path = shared / "callback-keys.env"
    patcher = mock.patch.dict(os.environ, {"CALLBACK_KEYS_FILE": str(path)})
    patcher.start()
    case.addCleanup(patcher.stop)
    return path
