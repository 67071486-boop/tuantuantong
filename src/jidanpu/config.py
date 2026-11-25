"""SDK 配置对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from jidanpu.errors import ConfigError

UNWRAP_MODES = ("auto", "status", "envelope")

_DEFAULT_UA = "jidanpu-sdk/0.1.1"


@dataclass(frozen=True, slots=True)
class JidanpuConfig:
    """不可变的 SDK 配置。base_url 尾斜杠会被自动归一化。"""

    base_url: str
    account: str
    password: str = field(repr=False)
    timeout: float = 30.0
    max_retries: int = 2
    token_ttl: timedelta = timedelta(hours=12)
    token_store_path: Path | None = None
    unwrap: str = "auto"
    user_agent: str = _DEFAULT_UA

    def __post_init__(self) -> None:
        normalized = self.base_url.strip().rstrip("/")
        if not normalized:
            raise ConfigError("base_url 不能为空")
        if not normalized.startswith(("http://", "https://")):
            raise ConfigError(f"base_url 必须以 http:// 或 https:// 开头，当前为 {self.base_url!r}")
        if not self.account:
            raise ConfigError("account 不能为空")
        if not self.password:
            raise ConfigError("password 不能为空")
        if self.unwrap not in UNWRAP_MODES:
            raise ConfigError(f"unwrap 必须是 {UNWRAP_MODES} 之一，当前为 {self.unwrap!r}")
        if self.timeout <= 0:
            raise ConfigError("timeout 必须大于 0")
        if self.max_retries < 0:
            raise ConfigError("max_retries 不能为负")
        object.__setattr__(self, "base_url", normalized)

    @property
    def api_base(self) -> str:
        """接口根地址（不含尾斜杠）。"""
        return self.base_url
