"""鸡蛋铺（JiDanPu）商户后台 HTTP 接口 Python SDK。"""

from jidanpu.auth import SessionAuth
from jidanpu.client import DEFAULT_SPEC_PATH, JidanpuClient
from jidanpu.config import JidanpuConfig
from jidanpu.errors import (
    ApiError,
    AuthError,
    CaptchaRequiredError,
    ConfigError,
    JidanpuError,
    JidanpuTimeoutError,
    RateLimitError,
    ServerError,
    SessionExpiredError,
    TransportError,
    UpstreamBlockedError,
)
from jidanpu.models import JdpModel
from jidanpu.registry import Endpoint, Param, Registry, load_registry

__version__ = "0.1.4"

__all__ = [
    "__version__",
    "JidanpuClient",
    "JidanpuConfig",
    "JdpModel",
    "Registry",
    "Endpoint",
    "Param",
    "load_registry",
    "DEFAULT_SPEC_PATH",
    "SessionAuth",
    "JidanpuError",
    "ConfigError",
    "TransportError",
    "JidanpuTimeoutError",
    "AuthError",
    "SessionExpiredError",
    "CaptchaRequiredError",
    "RateLimitError",
    "ApiError",
    "ServerError",
    "UpstreamBlockedError",
]
