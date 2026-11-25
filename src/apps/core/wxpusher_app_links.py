"""代代通安卓通知使用的固定入口。"""

import re

DAIDAITONG_ORIGIN = "https://suqing.suqing.chat"
DAIDAITONG_HOME_SCHEME = "daidaitong://suqing.suqing.chat/m/wxpusher/open"
_ORDER_SCHEME_PREFIX = "daidaitong://suqing.suqing.chat/m/orders/"
_ORDER_SCHEME = re.compile(re.escape(_ORDER_SCHEME_PREFIX) + r"[0-9]{20}")
_HALL_SCHEME_PREFIX = "daidaitong://suqing.suqing.chat/m/hall?source="
_HALL_SCHEME = re.compile(re.escape(_HALL_SCHEME_PREFIX) + r"(?:local&ref=[0-9]{20}|(?:upstream|peer)&ref=[1-9][0-9]{0,14})")


def is_daidaitong_app_link(url: str) -> bool:
    return url == DAIDAITONG_HOME_SCHEME or bool(_ORDER_SCHEME.fullmatch(url) or _HALL_SCHEME.fullmatch(url))
