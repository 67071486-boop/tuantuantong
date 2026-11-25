"""SDK 主入口。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from jidanpu.auth import (
    LOGIN_PATH,
    LOGIN_SUCCESS_CODE,
    Authenticator,
    SessionAuth,
    TokenStore,
    _Secret,
)
from jidanpu.config import JidanpuConfig
from jidanpu.errors import AuthError, ConfigError, SessionExpiredError
from jidanpu.models.base import JdpModel
from jidanpu.registry import Endpoint, Registry, load_registry
from jidanpu.transport import Transport

logger = logging.getLogger(__name__)

#: 包内清单目录：构建 wheel 时由 `pyproject.toml` 的 `force-include`
#: 把仓库根的 `spec/` 映射到这里，所以**装好的包**里清单就在包自己内部。
_PACKAGED_SPEC_DIR = Path(__file__).resolve().parent / "spec"

#: 仓库检出布局下的清单位置（`<repo>/spec/endpoints.json`）。
#: 只在源码树里有效，装进 site-packages 后这个路径没有意义。
_REPO_SPEC_PATH = Path(__file__).resolve().parents[2] / "spec" / "endpoints.json"


def resolve_spec_path(packaged_dir: Path, repo_path: Path) -> Path:
    """选定默认端点清单路径：包内优先，包内没有才回退仓库检出布局。
    「装好的包」才是分发形态（那个布局下 `parents[2]` 指向 site-packages 上级，永远找不到清单）；
    回退分支只为源码树内直接运行服务。"""
    packaged = packaged_dir / "endpoints.json"
    if packaged.is_file():
        return packaged
    return repo_path


DEFAULT_SPEC_PATH = resolve_spec_path(_PACKAGED_SPEC_DIR, _REPO_SPEC_PATH)

#: 登录端点：不得触发 `ensure_login()` 预登录，也不参与会话失效重登（否则自指）。
_LOGIN_PATHS = frozenset({LOGIN_PATH})


class JidanpuClient:
    """鸡蛋铺商户后台客户端。真实 API 用 cookie 会话：`SessionAuth` 发一次 `POST /Login/Login` 表单，
    凭据（`sl-session`）留在 `Transport` 的 cookie jar 里，后续请求自动带上。
    用法：`client.ensure_login()` 后 `client.call("POST", "/OrderAjax/GetOrderList", query={"page": 1})`。"""

    def __init__(
        self,
        config: JidanpuConfig,
        *,
        registry: Registry | None = None,
        spec_path: Path | None = None,
        auth: Authenticator | None = None,
        token_store: TokenStore | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._config = config

        if registry is not None:
            self._registry = registry
        else:
            self._registry = load_registry(spec_path or DEFAULT_SPEC_PATH)

        #: 会话标记（`auth.SESSION_MARKER`）：只表示「已登录」，不是凭据。
        #: 真正的凭据是 `Transport` cookie jar 里的会话 cookie，客户端不碰，
        #: 也**不**把它当 token 发出去（真实 API 无 `x-token`，spec §0.2）。
        self._token: _Secret | None = None

        self._transport = Transport(
            config,
            client=http_client,
        )

        self._auth = auth or SessionAuth(
            config,
            self._transport,
            store=token_store,
        )
        self._transport.on_unauthorized = self._on_unauthorized

    # ── 属性 ────────────────────────────────────────────────
    @property
    def config(self) -> JidanpuConfig:
        return self._config

    @property
    def registry(self) -> Registry:
        return self._registry

    @property
    def auth(self) -> Authenticator:
        return self._auth

    @property
    def last_envelope(self) -> dict[str, Any] | None:
        """最近一次请求业务壳的脱敏副本；还没发过请求时为 `None`。
        `call()` 成功时只交出壳里的 `data`，分页要用的 `count`/`total` 得从这里读
        （`client.last_envelope["count"]`）；服务端回显的凭据在存入前已抹掉（见 `Transport`）。"""
        return self._transport.last_envelope

    # ── 认证 ────────────────────────────────────────────────
    def ensure_login(self) -> None:
        """确保会话已建立。
        `auth.get_token()` 返回不透明会话标记；会话 cookie 由 `Transport` 的 jar 持有。
        这里只记「已登录」，不回流给传输层当 `x-token`（真实 API 无该头，认证完全靠 jar）。"""
        self._token = _Secret(self._auth.get_token())

    def _on_unauthorized(self) -> None:
        """401/403：清会话 → 重登一次。重登必须在这里完成：Transport 只会在
        `on_unauthorized()` 返回后递归重试一次（`auth_retry=False`），届时 jar 里已是新会话 cookie。
        不会死锁：`invalidate()` 先释放登录锁，锁内登录请求走 `inject_token=False`，不回调本方法、不参与重登。"""
        self._token = None
        self._auth.invalidate()
        self.ensure_login()

    # ── 调用 ────────────────────────────────────────────────
    def call(
        self,
        method: str,
        path: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
        success_rule: str | None = None,
        retry_on_sent: bool | None = None,
        retry_on_rejection: bool = True,
    ) -> Any:
        """调用任意端点（未登记的也可调用，记 warning）：表单接口用 `data=`、JSON 用 `body=`，`Referer` 默认取清单。
        成功判定：显式 `success_rule=` → 清单声明 → 按 `risk`（write→positive_code，其余→lenient）；壳里带 `success` 字段时由它说了算（`transport._envelope_succeeded`）。
        `retry_on_sent` 按端点幂等性解析（⛔ 不是 HTTP 方法；写端点不重试=防重复下单）；`retry_on_rejection=False` 禁止限流或登录失效后的原请求重发。成功返回 `data`，壳无 `data` 键时返回整壳（载荷在 `msg` 则从 `result["msg"]` 取）。"""
        filled = _fill_path(path, path_params, method=method)
        endpoint = self._registry.match(method, filled)

        if endpoint is None:
            logger.warning(
                "端点未登记在 spec 中：%s %s。将按宽松模式调用。", method.upper(), filled
            )
        else:
            _validate_required(
                endpoint, path_params=path_params, query=query, body=body, data=data
            )

        is_login = filled in _LOGIN_PATHS
        if not is_login:
            self.ensure_login()

        call_referer = referer or (endpoint.referer if endpoint else "")
        # 登录的成功码是 1（与业务接口相反）且必须走**严格**模式 —— 宽松模式对
        # `code=2`（需要验证码）也判成功，登录用它就破了登录闸门。
        # 业务调用传 `None`（宽松）：真实成功码有 0/1/200 三种，钉死任一个都会误判。
        expect_code = LOGIN_SUCCESS_CODE if is_login else None
        # 登录路径一律退回 `lenient`：登录走严格 `expect_code`，不该被任何码规则接管
        # （`_envelope_succeeded` 里严格模式排在 `positive_code` 之前，这里显式退回只是让
        # 「登录不看端点规则」不依赖函数内部顺序）。非登录路径走「显式 → 清单声明 → `risk` 默认」三步。
        effective_rule = (
            "lenient" if is_login else success_rule or _endpoint_success_rule(endpoint)
        )
        # ⭐ Z21（C8）：「请求已发出」型错误的重试标记，按端点幂等性解析（见 docstring）。
        # 登录同样保持可重试：它只是重建会话，重放无副作用。
        if retry_on_sent is None:
            retry_on_sent = not (endpoint is not None and endpoint.risk == "write")

        def _send() -> Any:
            return self._transport.request(
                method,
                filled,
                query=query,
                body=body,
                data=data,
                files=files,
                headers=headers,
                referer=call_referer,
                expect_code=expect_code,
                success_rule=effective_rule,
                retry_on_sent=bool(retry_on_sent),
                auth_retry=retry_on_rejection,
                retry_rate_limit=retry_on_rejection,
                # 登录端点自身不是「可认证请求」：401/403 时 Transport 会回调
                # `on_unauthorized` → 用配置凭据静默重登 → 重试本次请求，对登录端点
                # 就是自指（与 auth.py 发登录请求时一致，见 `inject_token=False`）。
                inject_token=not is_login,
            )

        try:
            return _send()
        except AuthError as exc:
            if is_login:
                raise
            if not retry_on_rejection:
                # 让下一次请求重新登录，保留当前写入的原始回执。
                self._token = None
                self._auth.invalidate()
                raise
            if not isinstance(exc, SessionExpiredError):
                raise
            self._on_unauthorized()
            return _send()

    def call_endpoint(
        self,
        operation_id: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        data: dict[str, Any] | None = None,
        files: Any = None,
    ) -> Any:
        """按 operation_id 调用端点。"""
        endpoint = self._registry.by_operation_id(operation_id)
        if endpoint is None:
            raise ConfigError(f"清单中没有 operation_id={operation_id!r} 的端点")
        return self.call(
            endpoint.method,
            endpoint.path,
            path_params=path_params,
            query=query,
            body=body,
            data=data,
            files=files,
        )

    def find_endpoints(self, pattern: str) -> list[Endpoint]:
        """检索端点（按 path / name / tags / operation_id）。"""
        return self._registry.find(pattern)

    def model_for(self, path: str) -> type[JdpModel] | None:
        """该端点的实测响应模型；没有实测形状时返回 `None`——只有 36 个端点有实测形状
        （`spec/response_shapes.json`），没测到就是没测到，不拿权威索引的残缺快照冒充。
        模型所有字段可选（样本不足以断言必填；`JdpModel` 是 `extra="allow"`，未记录键原样保留）。"""
        from jidanpu.models.generated import MODEL_FOR_PATH

        return MODEL_FOR_PATH.get(path)

    # ── 分页 ────────────────────────────────────────────────
    def iter_all(
        self,
        method: str,
        path: str,
        *,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        data: dict[str, Any] | None = None,
        page_param: str = "page",
        limit_param: str = "limit",
        page_size: int = 100,
        start_page: int = 1,
        max_pages: int = 1000,
    ) -> Iterator[Any]:
        """逐页遍历列表接口，把每页的 `data` 摊平成一个迭代器。停止条件（任一满足）：页不满、空页、或达到壳里的 `count`。
        不会静默截断：翻满 `max_pages` 仍满页且服务端没给可用 `count` 时抛 `ConfigError`（显式放大 `max_pages`），不悄悄交半份数据。
        只用于只读列表：`risk == "write"` 的端点发请求前直接拒（分页遍历写端点是 bug）；参数与端点校验立即做，不拖到第一次 `next()`。"""
        if page_size < 1:
            raise ConfigError(f"page_size 必须 >= 1，收到 {page_size}")
        if start_page < 1:
            raise ConfigError(f"start_page 必须 >= 1，收到 {start_page}")
        if max_pages < 1:
            raise ConfigError(f"max_pages 必须 >= 1，收到 {max_pages}")

        filled = _fill_path(path, path_params, method=method)
        endpoint = self._registry.match(method, filled)
        if endpoint is not None and endpoint.risk == "write":
            raise ConfigError(
                f"{endpoint.label}（{endpoint.name}）在清单里是 risk='write'，"
                f"iter_all 只用于只读列表接口"
            )

        return self._iter_pages(
            method,
            filled,
            path_params=path_params,
            query=query,
            body=body,
            data=data,
            page_param=page_param,
            limit_param=limit_param,
            page_size=page_size,
            start_page=start_page,
            max_pages=max_pages,
        )

    def _iter_pages(
        self,
        method: str,
        filled: str,
        *,
        path_params: dict[str, Any] | None,
        query: dict[str, Any] | None,
        body: Any,
        data: dict[str, Any] | None,
        page_param: str,
        limit_param: str,
        page_size: int,
        start_page: int,
        max_pages: int,
    ) -> Iterator[Any]:
        page = start_page
        fetched = 0
        total: int | None = None

        for _ in range(max_pages):
            page_query: dict[str, Any] = dict(query or {})
            page_query[page_param] = page
            page_query[limit_param] = page_size

            payload = self.call(
                method,
                filled,
                path_params=path_params,
                query=page_query,
                body=body,
                data=data,
            )
            if not isinstance(payload, list):
                raise ConfigError(
                    f"{method.upper()} {filled} 的 data 不是列表（是 "
                    f"{type(payload).__name__}），iter_all 只能遍历列表接口"
                )

            count = (self.last_envelope or {}).get("count")
            # bool 是 int 的子类，但 `count: true` 不是条数。
            if isinstance(count, int) and not isinstance(count, bool):
                total = count

            if not payload:  # 空页即结束
                return
            yield from payload
            fetched += len(payload)

            if len(payload) < page_size:  # 不满页 = 最后一页
                return
            if total is not None and fetched >= total:
                return
            page += 1

        raise ConfigError(
            f"{method.upper()} {filled} 翻了 {max_pages} 页仍未结束：每页都是满页，"
            f"且业务壳里没有可用的 count（已取 {fetched} 条）。请确认该接口是否真的"
            f"分页，或用 max_pages= 显式放大上限"
        )

    # ── 生命周期 ────────────────────────────────────────────
    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> JidanpuClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _endpoint_success_rule(endpoint: Endpoint | None) -> str:
    """端点没有显式声明成功语义时按 `risk` 取默认（顺序由 `call()` 负责：显式 → 清单声明 → 这里）。
    write→positive_code：写接口成功时 `code` 是业务值，`code==0` 一律失败（留宽松启发式=失败壳被判成功的 fail-open）；其余/未登记→lenient（读接口成功码实测 0/1/200，宽松才判得住）。
    ⚠️ positive_code 只管壳里没有 `success` 字段的那一半（优先级见 `transport._envelope_succeeded`）；risk 标错的后果是响亮报错，可用 `success_rule="lenient"` 覆盖。"""
    if endpoint is not None and endpoint.success_rule:
        return endpoint.success_rule
    if endpoint is not None and endpoint.risk == "write":
        return "positive_code"
    return "lenient"


def _fill_path(path: str, path_params: dict[str, Any] | None, *, method: str) -> str:
    import re

    names = re.findall(r"\{([^}]+)\}", path)
    if not names:
        return path

    provided = path_params or {}
    missing = [n for n in names if n not in provided]
    if missing:
        raise ConfigError(
            f"{method.upper()} {path} 缺少路径参数：{', '.join(missing)}"
            f"（应通过 path_params 传入）"
        )
    filled = path
    for name in names:
        filled = filled.replace("{" + name + "}", str(provided[name]))
    return filled


def _required_names(endpoint: Endpoint) -> tuple[str, ...]:
    """严格必填的参数名：只有所有出现处都 required=true 才算——鸡蛋铺自己的文档就不自洽
    （同一参数在 query 标 required、body 标 false；「验证码」类参数实测服务端并不校验），否则交给调用方决定。
    位置不强制：同名参数在 query 或 body 任一处提供即可（OR 语义）。"""
    by_name: dict[str, list[bool]] = {}
    for param in endpoint.params:
        if param.location in ("path", "query", "body"):
            by_name.setdefault(param.name, []).append(param.required)
    return tuple(name for name, flags in by_name.items() if all(flags))


def _validate_required(
    endpoint: Endpoint,
    *,
    path_params: dict[str, Any] | None,
    query: dict[str, Any] | None,
    body: Any,
    data: dict[str, Any] | None = None,
) -> None:
    """本地必填校验。`data`（form 编码）与 `query`/`body` 一样算「已提供」。"""
    provided = set(path_params or {}) | set(query or {}) | set(data or {})
    if isinstance(body, dict):
        provided |= set(body)

    problems = [f"{n}(path)" for n in endpoint.path_params if n not in provided]
    problems += [
        f"{n}(query 或 body)" for n in _required_names(endpoint) if n not in provided
    ]

    if problems:
        raise ConfigError(
            f"{endpoint.label}（{endpoint.name}）缺少必填参数：{', '.join(sorted(problems))}"
        )
