"""QQ 群消息的限频常量与滑动窗口（纯逻辑，可单测）。

## 限频口径（出处与校准状态）

官方文档（QQ 机器人开放平台，2026-09 版）给的群维度限频：

- **Bot 维度**：已认证 60 条/分钟、未认证 30 条/分钟（哪一档看 `binding.auth_verified`）；
- **单群维度**：20 条/分钟；
- **单群每日**：1000 条。

⚠️ 上面是**文档值**，本仓还没拿真机校准过（与 jidanpu_openapi 的限频同一条登记：
「限频的确切数值没有实测」）——**发版前必须真机校准一次**；校准后改这里的常量，
调用方（worker.sender）与测试（`tests/test_qqbot_ratelimit.py`）都只认这份常量。
"""
from __future__ import annotations

import time
from collections import deque

#: Bot 维度限频（条/分钟）：已认证 / 未认证。出处见模块 docstring。
BOT_QPM_VERIFIED = 60
BOT_QPM_UNVERIFIED = 30
#: 单群维度限频（条/分钟）。
GROUP_QPM = 20
#: 单群每日上限（条/天）。
GROUP_DAILY_LIMIT = 1000


class SlidingWindow:
    """固定速率的滑动窗口（1 分钟窗口；可注入时钟，纯内存、不碰 DB）。

    用法（`worker.GroupSender`）：

    - 先 `wait_time()`：>0 说明窗口已满、要等这么多秒——**没有**消耗名额；
    - 真要发时 `acquire()`：占一个名额并回 0（理论上调用方先看过 `wait_time`）。

    ⚠️ 名额在 `acquire()` 那一刻才消耗——先 peek 后占位，是为了「bot 窗口满」时
    不白白烧掉群窗口的名额（顺序见 sender 的四步判定）。
    """

    def __init__(self, limit: int, *, period: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.period = period
        self._clock = clock
        self._hits: deque[float] = deque()

    def _prune(self, now: float) -> None:
        while self._hits and now - self._hits[0] >= self.period:
            self._hits.popleft()

    def wait_time(self) -> float:
        """窗口满 ⇒ 还要等的秒数（>0，不消耗名额）；没满 ⇒ 0。"""
        now = self._clock()
        self._prune(now)
        if len(self._hits) < self.limit:
            return 0.0
        return max(0.0, self._hits[0] + self.period - now)

    def acquire(self) -> float:
        """占一个名额；窗口满时**不占**并回还要等的秒数（>0）。"""
        wait = self.wait_time()
        if wait > 0.0:
            return wait
        self._hits.append(self._clock())
        return 0.0
