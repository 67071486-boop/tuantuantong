"""鸡蛋铺同步的共用文案与翻页上限。本模块只剩这些常量。下家个人绑定（`jidanpu_member._fetch_completed_rows`）复用同一条「拉全」判据和同一批人话文案。
改文案等于改下家线的失败提示。`CLOSED_STATUSES` 同时给开放平台对账用。"""
from __future__ import annotations

#: `CLOSED_STATUSES` 用订单状态枚举。
from apps.business.models import OrderStatus

#: 一轮里**逐档状态**最多翻多少页：翻满还没到底 ⇒ 判「拉不全」⇒ 本轮不动作。
#: ⚠️ 下家线（`jidanpu_member._fetch_completed_rows`）复用同一个上限值（20）。
MAX_PAGES = 20

#: 人话文案（⛔ 不含凭据、⛔ 不回显上游自由文本 / 整行）。
#: ⚠️ 措辞里带上「负数 / 类型不对」：取数层把**不可用**的 `count`（缺失 / 负数 / 布尔）
#: 一律折成 `None` ⇒ 这一句要如实覆盖那三种形状，⛔ 别只说「没有给出」（那是假话）。
NO_COUNT_REASON = "{label}：上游没有给出**可用**的总行数（缺失 / 负数 / 类型不对）⇒ 本轮无法判定是否拉全"
SHORT_PAGE_REASON = "{label}：本页只回 {got} 行，累计未达总数 {count}"
PAGE_CAP_REASON = "{label}：翻页达到上限（{pages} 页）仍未到底，本轮按拉不全处理"

#: 我方算「已闭合」的状态（决定工作集里哪些闭合单要按「最近 N 天」筛）。
#: ⚠️ 这是一张**枚举**：`ISSUE`（问题单）**不在**里面——plan §5.2 把远端
#: `WTD` / `CXZ` / `ZCZ` / `YZC` / `YCZ` 都算非终态，而它们映射到我方 `ISSUE`。
CLOSED_STATUSES = frozenset({OrderStatus.COMPLETED, OrderStatus.REFUNDED})
