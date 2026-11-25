"""提前结算申请的**状态口径与状态写入**（spec §3.4 / §8.1）。 **为什么单独成模块**：`PerformanceSettleRequest.state` 有四个写入方 —— 申请接口（`apps/accounts/app_income_views.py`）、结账（`settlement.py`）、撤销结账（`performance_writes.py`）、订单退款（`refunds.py`）。它们分住在 App 面与领域两侧，
如果「哪些状态占额度」「哪些状态算作废」各写一份，两边必然漂移（E-0021 同族）。本模块只做**读口径 + 条件更新**：不碰金额、不写审计、**不开事务**—— 调用方（结账 / 撤销 / 退款钩子）都在自己的 `transaction.atomic()` 里调它。 ## 两条作废路径的**口径不同**（2026-10-23 业务方拍板，别合并成一条） | 触发点 | 作废哪些状态 | 为什么 |
|---|---|---| | **撤销结账**（`performance_writes.revoke_settlements`） | `pending` **和** `settled` | 结算被撤销 ⇒ 那次「已结」**不成立**，申请不能挡住重申（业务方原话「撤销结账要能重申」） | | **订单退款**（`refunds.on_order_refunded`） | **只有** `pending` | 已 `settled` 的申请意味着钱已按那次结账打过，退款是另一条回冲链，不改「当时申请被满足过」这个事实 |"""
from apps.business.models import PerformanceSettleRequest

#: **占额度**的状态（spec §8.1 改准：`pending` / `settled` 两条）。
#: ⚠️ `cancelled` **不占**（作废之后可以重新申请）⇒ 计额度一律用这个集合，
#: ⛔ 不要在任何一处写「按 user 全量 count」。
QUOTA_STATES = (
    PerformanceSettleRequest.State.PENDING,
    PerformanceSettleRequest.State.SETTLED,
)

#: **撤销结账**时要作废的状态集合（`pending` + `settled`）：结算被撤销 ⇒ 那次「已结」
#: 不成立。⚠️ 与退款的集合**不同**（退款只作废 `pending`），见模块 docstring 的表格。
REVOKE_CANCELLED_STATES = (
    PerformanceSettleRequest.State.PENDING,
    PerformanceSettleRequest.State.SETTLED,
)


def used_early_settle_count(user):
    """这个账号**已占用**的提前结算额度（只数 `pending` / `settled`）。
    ⚠️ 口径唯一出处 = 上面的 `QUOTA_STATES`：summary 的 `early_settlement.used` 与
    申请时的额度判定都调本函数（两处各写一个 `filter(state=...)` 必然漂）。"""
    return PerformanceSettleRequest.objects.filter(
        user=user, state__in=QUOTA_STATES
    ).count()


def _cancel(performance_ids, states):
    """把命中 `states` 的申请置 `cancelled`（并清 `settled_at`）；返回受影响行数。 ⚠️ **清 `settled_at`**：那一列记的是「财务结账那一刻」。申请作废之后，它**没有被任何一次有效的结账满足过** ⇒ 留着一个旧时刻就是假话（撤销结账那一档尤其明显：
    那次结账已经被撤掉了）。⛔ 不要为了「留痕」保留它——留痕在绩效行的审计里。 ⚠️ `performance_ids` 为空 ⇒ `__in=[]` 编译成空结果、`update()` 返回 0 且**一条 SQL 都不发**（Django 的 `EmptyResultSet`，见 `lookups.py::In.process_rhs`）⇒ 不需要前置 `if`。
    ⚠️ **幂等**由「只命中目标状态」这条谓词保证：作废过的行已经是 `cancelled`，同一单重复退款 / 重复撤销再跑一次命中 0 行，**不会重复扣额度**。"""
    return PerformanceSettleRequest.objects.filter(
        performance_id__in=list(performance_ids),
        state__in=states,
    ).update(
        state=PerformanceSettleRequest.State.CANCELLED,
        settled_at=None,
    )


def cancel_pending_settle_requests(performance_ids):
    """**退款**路径：把这些绩效行上处于 `pending` 的申请置 `cancelled`；返回受影响行数。
    ⚠️ **只改 `pending`**：`settled` 的不动（钱已按那次结账打了，退款是另一条回冲链）。
    这条口径与「撤销结账」不同（那边连 `settled` 一起作废），逐条理由见模块 docstring 的表。"""
    return _cancel(performance_ids, (PerformanceSettleRequest.State.PENDING,))


def cancel_settle_requests_for_revoke(performance_ids):
    """**撤销结账**路径：把这些绩效行上 `pending` **和** `settled` 的申请都置 `cancelled`。
    ⚠️ 为什么连 `settled` 一起作废（业务方口径）：结算被撤销 ⇒ 那次「已结」 **不成立**，申请不能挡住重申；作废之后额度按 `QUOTA_STATES` 自然回落、打手可以重新申请。
    ⚠️ 幂等：已是 `cancelled` 的行不在命中集合里（再跑一次 0 行）。"""
    return _cancel(performance_ids, REVOKE_CANCELLED_STATES)
