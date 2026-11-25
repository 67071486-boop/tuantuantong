"""镜像卡 / 隐藏单的**测试夹具小工具**（一律合成值，⛔ 零真实网络）。

为什么单独一个模块：`Order.hall_card`（镜像卡派生的隐藏单）在**多条读面**的用例里都要造
「一张上家镜像卡 + 一张隐藏单」。各文件各抄一份建绑定的代码既啰嗦、又会漂（改列名时漏一处）。
⚠️ 本模块**不是测试模块**（文件名不以 `test` 开头 ⇒ 不被测试发现），只放夹具，⛔ 没有用例。
⚠️ 与 `tests/p5_auth_helpers.py` / `tests/_openapi_fixtures.py` 同一个既有形态。
"""
from __future__ import annotations

from decimal import Decimal

from apps.business.models import (
    EmpType,
    Order,
    OrderStatus,
    PeerHallMirror,
    PeerUpstreamBinding,
    Performance,
)
from apps.business.peer_sync import hash_token, new_token


def make_mirror(
    *, game, bound_by, no="SYN-SHADOW-CARD",
    base_url="https://syn-shadow-up.test.invalid",
    price="50.00", status=PeerHallMirror.Status.LISTED,
):
    """一张上家镜像卡（下家视角）：归属绑定 + `listed` 行。"""
    binding = PeerUpstreamBinding.objects.create(
        peer_name="隐藏单夹具上家",
        peer_base_url=base_url,
        peer_account="syn-shadow-up",
        token_in_hash=hash_token(new_token()),
        token_out=new_token(),
        local_base_url="https://syn-shadow-local.test.invalid",
        bound_by=bound_by,
        hall_sync_enabled=True,
    )
    return PeerHallMirror.objects.create(
        peer_upstream_binding=binding,
        peer_base_url=base_url,
        peer_order_no=no,
        status=status,
        game=game,
        remote_hall_price=Decimal(price) if price is not None else None,
        first_seen_at=_now(),
    )


def make_hidden_order(
    *, game, card, order_no, status=OrderStatus.WAITING_GRAB,
    price="50.00", total="100.00", with_main=False, main_user=None,
):
    """镜像卡派生的隐藏单（`hall_card` 非空）。

    ⚠️ 生产上隐藏单**没有 MAIN 绩效行**；`with_main=True` 只在「显式排除」的**防御性**
    用例里用（证明即使出现 MAIN 行也不许从那个读面露出来）。
    """
    order = Order.objects.create(
        game=game,
        order_no=order_no,
        status=status,
        hall_price=Decimal(price) if price is not None else None,
        total_price=Decimal(total),
        source_order_no=card.peer_order_no,
        hall_card=card,
    )
    if with_main:
        Performance.objects.create(
            order=order, emp_type=EmpType.MAIN,
            temp_amount=Decimal(price) if price is not None else None,
            user=main_user,
        )
    return order


def _now():
    from django.utils import timezone

    return timezone.now()
