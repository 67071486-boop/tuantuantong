"""模型基类。

刻意使用 extra="allow"：鸡蛋铺接口会漂移，多出字段不应该炸掉调用方。

repr 也必须默认安全：pydantic 原生的 __repr__ 会原样打印所有字段值，任何模型只要
带 token / password 字段就会把凭据写进日志。脱敏放在基类，未来新增模型无需自觉。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.fields import FieldInfo

from jidanpu.errors import SENSITIVE_KEYS

T = TypeVar("T", bound="JdpModel")

_MASK = "***"


def _is_sensitive(name: str, field: FieldInfo | None) -> bool:
    """字段名或别名命中 SENSITIVE_KEYS 即为敏感字段（大小写不敏感，同 errors.redact）。

    额外把下划线当作连字符再比一次：pydantic 字段名不能含 `-`，所以 `x_token`
    这类头部字段名只有归一化后才能和 `x-token` 对上。
    """
    lowered = name.lower()
    if lowered in SENSITIVE_KEYS or lowered.replace("_", "-") in SENSITIVE_KEYS:
        return True
    if field is None:
        return False
    aliases = (field.alias, field.validation_alias, field.serialization_alias)
    return any(isinstance(alias, str) and alias.lower() in SENSITIVE_KEYS for alias in aliases)


class JdpModel(BaseModel):
    """所有鸡蛋铺数据模型的基类。"""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        str_strip_whitespace=False,
    )

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        """把敏感字段的值换成 ***；__repr__、__str__、rich 都走这个钩子。"""
        fields = type(self).model_fields
        for name, value in super().__repr_args__():
            if name is not None and _is_sensitive(name, fields.get(name)):
                yield name, _MASK
            else:
                yield name, value

    @classmethod
    def from_api(cls: type[T], data: Any) -> T | list[T] | Any:
        """把接口返回的原始数据转成模型。

        dict → 单个模型；list → 逐项转模型；其他 → 原样返回。
        """
        if isinstance(data, dict):
            return cls.model_validate(data)
        if isinstance(data, list):
            return [cls.model_validate(item) for item in data if isinstance(item, dict)]
        return data
