"""团团通直派的两端业务逻辑。"""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from itertools import groupby

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, Q, Value, When
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from apps.business import peer_client, peer_direct_status, peer_dispatch, peer_inbound
from apps.business.commission import CommissionError, commission_percent_of
from apps.business.game_scope import accounts_with_game, games_of
from apps.business.models import (
    EmpType, Game, Order, OrderStatus, PeerDownstreamBinding, PeerOrderLink,
    PeerSyncEvent, PeerUpstreamBinding, Performance,
)
from apps.business.order_writes import WriteContext, assign_order, lock_order_row
from apps.business.penalty_amounts import PENALTY_AMOUNT_LOCKED, changes_approved_penalty_price
from apps.business.performance_amount import commission_amount
from apps.business.peer_sync import (
    DIRECT_TAKER_ASSIGN_KIND, HALL_TAKE_LEAVE_ASSIGNED, JIDANPU_DIRECT_PENDING_PLATFORM, PEER_DIRECT_PENDING_MESSAGE,
    effective_peer_ref, hash_token, order_payload,
)
from apps.business.roles import WORKER
from apps.business.upstream_game_rules import allowed_upstream_game
from apps.core.error_codes import ErrorCode
from apps.realname.services import claim_gate_blocks, get_setting

ASSIGNMENT_NODES = frozenset({"order.assign_taker"})
_ASSIGNABLE_STATUSES = frozenset({OrderStatus.UNASSIGNED, OrderStatus.WAITING_GRAB})
_SEARCH_LIMIT = 20
_UNSPECIFIED = object()


class DirectTakerError(Exception):
    def __init__(self, message, *, code=ErrorCode.BUSINESS):
        super().__init__(message)
        self.code = code


def _receiving_binding(binding):
    current = PeerUpstreamBinding.objects.select_for_update().filter(
        pk=binding.pk, enabled=True, deleted_at__isnull=True,
        token_in_hash=binding.token_in_hash, bound_by__is_active=True,
    ).first()
    if current is None:
        raise DirectTakerError("上家绑定已失效", code=ErrorCode.PEER_REJECTED)
    return current


def reveal_order_taker(binding, *, order_no):
    """旧协议的引用可确认为系统号时，原始来源号与引用不同。"""
    links = PeerOrderLink.objects.filter(
        Q(upstream_internal_order_no=order_no)
        | Q(upstream_internal_order_no="", peer_order_no=order_no),
        upstream_binding=binding,
        order__deleted_at__isnull=True,
    ).values_list("order_id", "upstream_internal_order_no", "origin_order_no")
    order_ids = [pk for pk, internal, origin in links if internal or origin.strip() not in ("", order_no)]
    if len(order_ids) != 1:
        return None
    name = Performance.objects.filter(
        order_id=order_ids[0], emp_type=EmpType.MAIN, deleted_at__isnull=True,
    ).values_list("user__first_name", flat=True).first()
    return {"account_name": name or None}


def _receiving_game(binding, game_name):
    if not binding.direct_taker_assignment_enabled:
        raise DirectTakerError("该工作室未授权上家直接指派打手")
    game = Game.objects.filter(name=game_name, is_active=True, deleted_at__isnull=True).first()
    if game is None or not allowed_upstream_game(binding, game.pk):
        raise DirectTakerError("该工作室未开放此游戏的直派")
    return game


def receiving_studio_available(binding, game_name):
    if game_name is None:
        return binding.direct_taker_assignment_enabled
    try:
        _receiving_game(binding, game_name)
    except DirectTakerError:
        return False
    return True


def _eligible_takers(binding, game, *, prefer_name=None, **name_filter):
    matches = get_user_model().objects.filter(
        Q(groups__name=WORKER) | Q(groups__role__dashboard_type="worker"),
        is_active=True,
        staff_profile__deleted_at__isnull=True,
        **name_filter,
    ).exclude(pk=binding.bound_by_id)
    order_by = ["first_name", "pk"]
    if prefer_name is not None:
        order_by.insert(0, Case(When(first_name=prefer_name, then=Value(0)), default=Value(1)))
    matches = matches.order_by(*order_by)
    matches = accounts_with_game(matches, game).select_related("staff_profile").prefetch_related("groups")
    setting = get_setting()
    with closing(matches.iterator(chunk_size=100)) as users:
        for user in users:
            if not claim_gate_blocks(user=user, setting=setting, use_prefetched_user=True):
                yield user


def find_taker(binding, *, game_name, account_name):
    game = _receiving_game(binding, game_name)
    with closing(_eligible_takers(binding, game, first_name=account_name)) as eligible:
        taker = next(eligible, None)
        if taker is None:
            raise DirectTakerError("没有找到该账号名称对应的可接单打手")
        if next(eligible, None) is not None:
            raise DirectTakerError("该工作室有同名打手，请让下家确认账号名称后再指派")
    return taker, game


def _match_token(binding, taker, game):
    identities = json.dumps([
        binding.pk, binding.token_in_hash, taker.pk, taker.first_name, game.pk, game.name,
    ])
    return salted_hmac("peer-direct-taker-match", identities, algorithm="sha256").hexdigest()


def search_receiving_taker(binding, *, game_name, account_name):
    taker, game = find_taker(binding, game_name=game_name, account_name=account_name)
    return {
        "account_name": taker.first_name,
        "match_token": _match_token(binding, taker, game),
    }


def search_receiving_takers(binding, *, game_name, query):
    game = _receiving_game(binding, game_name)
    items = []
    with closing(_eligible_takers(binding, game, prefer_name=query, first_name__icontains=query)) as eligible:
        for name, matches in groupby(eligible, key=lambda user: user.first_name):
            taker = next(matches)
            if next(matches, None) is not None:
                continue
            items.append({"account_name": name, "match_token": _match_token(binding, taker, game)})
            if len(items) == _SEARCH_LIMIT:
                break
    return {"items": items}


def _receive_assignment(binding, *, target_ref, account_name, match_token, payload):
    if PeerOrderLink.objects.filter(
        Q(upstream_internal_order_no=payload["order_no"])
        | Q(peer_order_no__in={target_ref, payload["order_no"]}),
        upstream_binding=binding,
    ).exists():
        raise DirectTakerError("这笔订单已同步到下家，请联系下家核对接手人")
    taker, game = find_taker(binding, game_name=payload["game_name"], account_name=account_name)
    taker = get_user_model().objects.select_for_update().get(pk=taker.pk)
    if (
        not taker.is_active or taker.first_name != account_name
        or not constant_time_compare(match_token, _match_token(binding, taker, game))
    ):
        raise DirectTakerError("打手搜索结果已变化，请重新搜索")
    received = peer_inbound.apply_events([{
        "kind": "order.assign",
        "target_ref": target_ref,
        "occurred_at": timezone.now().isoformat(),
        "payload": payload,
    }], binding=binding, direct_taker_assignment=True)
    if received.applied != 1:
        raise DirectTakerError("订单未能录入下家系统")
    link = PeerOrderLink.objects.select_related("order__game").get(
        upstream_binding=binding, peer_order_no=target_ref, order__deleted_at__isnull=True,
    )
    order = link.order
    if order.status != OrderStatus.UNASSIGNED:
        raise DirectTakerError("下家订单已被接手")
    outcome = assign_order(
        order, taker=taker, dry_run=False,
        ctx=WriteContext(
            actor=binding.peer_name,
            log_actor=binding.peer_name,
            nodes=ASSIGNMENT_NODES,
            sync_personal_orders=False,
            sync_peer_orders=False,
        ),
    )
    if not outcome.ok:
        raise DirectTakerError("下家打手指派失败，请让下家核对接单设置")
    return {"status": "assigned", "account_name": account_name}


def _public_receipt(receipt):
    return {
        key: receipt.meta[key] for key in ("operation_id", "status", "account_name", "message")
        if key in receipt.meta
    }


@transaction.atomic
def receive_assignment(binding, *, operation_id, target_ref, account_name, match_token, payload):
    binding = _receiving_binding(binding)
    event_key = {
        "binding_role": "upstream", "binding_id": str(binding.pk),
        "direction": "in", "kind": DIRECT_TAKER_ASSIGN_KIND,
        "idempotency_key": str(operation_id),
    }
    receipt = PeerSyncEvent.objects.filter(**event_key).first()
    if receipt is not None:
        if receipt.target_ref != target_ref or receipt.meta.get("requested_name") != account_name:
            raise DirectTakerError("直派请求号已被用于其他订单或账号", code=ErrorCode.PEER_REJECTED)
        return _public_receipt(receipt)
    try:
        with transaction.atomic():
            result = _receive_assignment(
                binding, target_ref=target_ref, account_name=account_name,
                match_token=match_token, payload=payload,
            )
    except DirectTakerError as exc:
        result = {"status": "rejected", "message": str(exc)}
    result["operation_id"] = str(operation_id)
    PeerSyncEvent.objects.create(
        **event_key, target_type="order", target_ref=target_ref,
        status=PeerSyncEvent.Status.DONE if result["status"] == "assigned" else PeerSyncEvent.Status.FAILED,
        reason=result.get("message", ""),
        meta={
            **result, "requested_name": account_name, "source_order_no": payload["order_no"],
            "binding_token_hash": binding.token_in_hash,
        },
        processed_at=timezone.now(),
    )
    return result


@transaction.atomic
def receive_assignment_receipt(binding, *, operation_id, target_ref, account_name, binding_token_hash):
    binding = _receiving_binding(binding)
    same_upstream_ids = PeerUpstreamBinding.objects.filter(
        peer_base_url=binding.peer_base_url, peer_account=binding.peer_account,
    ).values_list("pk", flat=True)
    receipt = PeerSyncEvent.objects.filter(
        binding_role="upstream", binding_id__in=[str(pk) for pk in same_upstream_ids],
        direction="in", kind=DIRECT_TAKER_ASSIGN_KIND, idempotency_key=str(operation_id),
        target_ref=target_ref, meta__requested_name=account_name,
        meta__binding_token_hash=binding_token_hash,
    ).first()
    if receipt is None:
        return None
    return _public_receipt(receipt)


def _downstream_bindings():
    return PeerDownstreamBinding.objects.filter(
        state=PeerDownstreamBinding.State.ACTIVE, account__is_active=True,
        peer_base_url__startswith="https://",
    ).select_related("account").order_by("pk")


def _fallback_studio_name(binding):
    return binding.account.first_name or binding.peer_base_url.removeprefix("https://")


def _studio_name_if_enabled(binding, game=None):
    try:
        params = {} if game is None else {"game_name": game.name}
        result = peer_client.direct_taker_request(binding, "availability", **params)
    except (peer_client.PeerUnreachable, peer_client.PeerRejected):
        return None
    if result is not None and result.get("enabled") is True:
        name = result.get("name")
        return name if isinstance(name, str) and name else _fallback_studio_name(binding)
    return None


def _current_binding(order):
    assignee_id = Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN, deleted_at__isnull=True,
    ).values_list("user_id", flat=True).first()
    if assignee_id is None:
        return None
    return _downstream_bindings().filter(account_id=assignee_id).first()


def _has_current_taker(order):
    return Performance.objects.filter(
        order=order, emp_type=EmpType.MAIN,
        deleted_at__isnull=True, user_id__isnull=False,
    ).exists()


def _ready_for_direct_assignment(order):
    return order.status in _ASSIGNABLE_STATUSES and not _has_current_taker(order)


def available_studios(game=None):
    bindings = list(_downstream_bindings())
    if game is not None:
        bindings = [binding for binding in bindings if games_of(binding.account).filter(pk=game.pk).exists()]
    if not bindings:
        return []
    with ThreadPoolExecutor(max_workers=8) as workers:
        names = workers.map(lambda binding: _studio_name_if_enabled(binding, game), bindings)
        return [{"id": binding.pk, "name": name} for binding, name in zip(bindings, names) if name is not None]


def _pending_summary(pending):
    return {key: pending[key] for key in ("studio_id", "studio_name", "account_name", "price")}


def assignment_options(order, *, studio_game, can_assign, can_view_price, can_view_assignee):
    current = _current_binding(order) if can_view_assignee else None
    assignee = None
    lookup_failed = False
    history_unverified = False
    if current is not None:
        studio_name = _fallback_studio_name(current)
        try:
            remote = peer_client.direct_taker_request(
                current, "assignee", order_no=order.order_no,
            )
        except (peer_client.PeerUnreachable, peer_client.PeerRejected):
            lookup_failed = True
            assignee = {"studio_name": studio_name, "account_name": None}
        else:
            if remote is None:
                history_unverified = True
                assignee = {"studio_name": studio_name, "account_name": None}
            else:
                assignee = {
                    "studio_name": remote.get("studio_name") or studio_name,
                    "account_name": remote.get("account_name"),
                }

    pending = order.peer_direct_pending
    if pending and pending.get("platform") == JIDANPU_DIRECT_PENDING_PLATFORM:
        pending = None
    studios = []
    if pending is not None and can_assign:
        studios = [{"id": pending["studio_id"], "name": pending["studio_name"]}]
    elif order.peer_direct_pending is None and can_assign and studio_game is not None and _ready_for_direct_assignment(order):
        studios = available_studios(studio_game)
    return {
        "studios": studios, "assignee": assignee,
        "pending": _pending_summary(pending) if pending is not None and can_assign else None,
        "assignee_lookup_failed": lookup_failed,
        "assignee_history_unverified": history_unverified,
        "price": str(order.hall_price) if can_view_price and order.hall_price is not None else None,
    }


def _binding_for_studio(studio_id):
    binding = _downstream_bindings().filter(pk=studio_id).first()
    if binding is None:
        raise DirectTakerError("该下家工作室的绑定不可用")
    return binding


def search_taker(order, *, studio_id, account_name=None, query=None):
    if order.peer_direct_pending:
        raise DirectTakerError(PEER_DIRECT_PENDING_MESSAGE)
    if not _ready_for_direct_assignment(order):
        raise DirectTakerError("这笔订单已有接手安排")
    return search_taker_for_game(
        order.game, studio_id=studio_id, account_name=account_name, query=query,
        check_game=query is not None,
    )


def search_taker_for_game(game, *, studio_id, account_name=None, query=None, check_game=True):
    binding = _binding_for_studio(studio_id)
    if game is None:
        raise DirectTakerError("请先为订单设置游戏")
    if check_game and not games_of(binding.account).filter(pk=game.pk).exists():
        raise DirectTakerError("该工作室未开放此游戏的直派")
    action, params = ("search_matches", {"query": query}) if query is not None else (
        "search", {"account_name": account_name},
    )
    try:
        try:
            result = peer_client.direct_taker_request(binding, action, game_name=game.name, **params)
        except peer_client.PeerRejected as exc:
            if query is None or exc.code != ErrorCode.VALIDATION or "search_matches" not in str(exc):
                raise
            # Older receivers reject the new action in the action choice field.
            exact = peer_client.direct_taker_request(binding, "search", game_name=game.name, account_name=query)
            result = {"items": [_confirmed_exact_match(exact, query)]}
    except peer_client.PeerRejected as exc:
        raise DirectTakerError(str(exc)) from exc
    except peer_client.PeerUnreachable as exc:
        raise DirectTakerError("暂时无法搜索该下家工作室", code=ErrorCode.PEER_UNREACHABLE) from exc
    if query is not None:
        items = result.get("items") if result is not None else None
        if not isinstance(items, list) or len(items) > _SEARCH_LIMIT:
            raise DirectTakerError("下家工作室没有返回可确认的搜索结果")
        matches = []
        for item in items:
            if not isinstance(item, dict):
                raise DirectTakerError("下家工作室没有返回可确认的搜索结果")
            name, token = item.get("account_name"), item.get("match_token")
            if not isinstance(name, str) or not name.strip() or not isinstance(token, str) or not token:
                raise DirectTakerError("下家工作室没有返回可确认的搜索结果")
            matches.append({"account_name": name, "match_token": token})
        return {"items": matches}
    return _confirmed_exact_match(result, account_name)


def _confirmed_exact_match(result, account_name):
    if (
        result is None or result.get("account_name") != account_name
        or not isinstance(result.get("match_token"), str) or not result["match_token"]
    ):
        raise DirectTakerError("下家工作室没有返回可确认的打手")
    return {"account_name": account_name, "match_token": result["match_token"]}


def _outgoing_context(user):
    return WriteContext(
        actor=user.get_username(), log_actor=user.get_username(), log_actor_id=user.pk,
        user=user, nodes=ASSIGNMENT_NODES, notify_worker=False,
        sync_personal_orders=False, sync_peer_orders=False,
    )


def _previous_assignment(order, *, studio_id, account_name, requested_price=_UNSPECIFIED):
    binding = _current_binding(order)
    if binding is None or binding.pk != studio_id:
        return None
    claim_stamp = peer_direct_status.current_claim_stamp(order, binding.account_id)
    if claim_stamp is None:
        return None
    event = PeerSyncEvent.objects.filter(
        binding_role="downstream", binding_id=str(studio_id), direction="out",
        kind=DIRECT_TAKER_ASSIGN_KIND, target_ref=order.order_no, status=PeerSyncEvent.Status.DONE,
        meta__source_claim_stamp=claim_stamp,
    ).first()
    if event is None or event.meta.get("account_name") != account_name:
        return None
    if requested_price is not _UNSPECIFIED:
        confirmed_price = event.meta.get("confirmed_price")
        if confirmed_price is None or Decimal(confirmed_price) != requested_price:
            raise DirectTakerError("这笔订单已完成直派，本次发单价未生效")
    return {key: event.meta[key] for key in ("studio_name", "account_name")}


def _check_expected_price(expected_price, price):
    if expected_price is not _UNSPECIFIED and expected_price != price:
        raise DirectTakerError("发单价已变化，请核对最新价格后再指派")


@transaction.atomic
def _stage_assignment(order, *, user, binding, studio_name, account_name, match_token, expected_price, price):
    from apps.business.models import DailiantongPush

    order = lock_order_row(order)
    if order is None or not games_of(user).filter(pk=order.game_id).exists():
        raise DirectTakerError("订单不存在或没有该游戏权限")
    if order.peer_direct_pending is not None:
        pending = order.peer_direct_pending
        if pending.get("platform") == JIDANPU_DIRECT_PENDING_PLATFORM or (
            binding.pk != pending["studio_id"] or account_name != pending["account_name"]
        ):
            raise DirectTakerError(PEER_DIRECT_PENDING_MESSAGE)
        fixed_price = Decimal(pending["price"])
        if changes_approved_penalty_price(order, fixed_price):
            raise DirectTakerError(PENALTY_AMOUNT_LOCKED)
        _check_expected_price(expected_price, fixed_price)
        if price is not None and price != fixed_price:
            raise DirectTakerError(PEER_DIRECT_PENDING_MESSAGE)
        return order, pending
    if not match_token:
        raise DirectTakerError("请先搜索并确认下家打手")
    if not _ready_for_direct_assignment(order):
        raise DirectTakerError("这笔订单已有接手安排")
    pending_platform_push = DailiantongPush.objects.filter(
        order=order, state__in=[
            DailiantongPush.State.PENDING, DailiantongPush.State.UNKNOWN, DailiantongPush.State.SENT,
        ],
    ).exists()
    if pending_platform_push:
        raise DirectTakerError("订单已发往其他平台，请先撤回后再指派")
    _check_expected_price(expected_price, order.hall_price)
    chosen_price = price if price is not None else order.hall_price
    if chosen_price is None:
        raise DirectTakerError("请先设置订单发单价")
    if changes_approved_penalty_price(order, chosen_price):
        raise DirectTakerError(PENALTY_AMOUNT_LOCKED)
    outcome = assign_order(order, taker=binding.account, ctx=_outgoing_context(user), dry_run=True)
    if not outcome.ok:
        raise DirectTakerError(outcome.msg)
    try:
        commission_amount(chosen_price, commission_percent_of(binding.account))
    except CommissionError as exc:
        raise DirectTakerError(exc.message, code=exc.code) from exc
    target_ref = effective_peer_ref(order)
    if len(target_ref) > PeerOrderLink._meta.get_field("peer_order_no").max_length:
        raise DirectTakerError("来源单号超出直连订单支持的长度")
    payload = order_payload(order, origin_order_no=peer_dispatch._origin_order_no(order))
    payload["status"] = OrderStatus.UNASSIGNED
    payload["hall_price"] = str(chosen_price)
    pending = {
        "studio_id": binding.pk, "studio_name": studio_name, "account_name": account_name,
        "match_token": match_token,
        "price": str(chosen_price), "operation_id": str(uuid.uuid4()),
        "binding_base_url": binding.peer_base_url,
        "binding_token_hash": hash_token(binding.token_out),
        "target_ref": target_ref, "payload": payload, "has_price_override": price is not None,
    }
    order.peer_direct_pending = pending
    order.save(update_fields=["peer_direct_pending"])
    return order, pending


def _send_assignment(binding, pending):
    original_base_url = pending.get("binding_base_url")
    token_changed = hash_token(binding.token_out) != pending["binding_token_hash"]
    if (original_base_url is None and token_changed) or (
        original_base_url is not None and binding.peer_base_url != original_base_url
    ):
        raise DirectTakerError(
            "指派待确认期间下家绑定已变更，请联系下家核对接手结果",
            code=ErrorCode.PEER_UNREACHABLE,
        )
    request = {
        "target_ref": pending["target_ref"],
        "operation_id": pending["operation_id"],
        "account_name": pending["account_name"],
    }
    if token_changed:
        action = "receipt"
        request["binding_token_hash"] = pending["binding_token_hash"]
    else:
        action = "assign"
        request.update(payload=pending["payload"], match_token=pending["match_token"])
    try:
        result = peer_client.direct_taker_request(binding, action, **request)
    except (peer_client.PeerUnreachable, peer_client.PeerRejected) as exc:
        raise DirectTakerError(PEER_DIRECT_PENDING_MESSAGE, code=ErrorCode.PEER_UNREACHABLE) from exc
    if (
        result is None or result.get("operation_id") != pending["operation_id"]
        or result.get("status") not in ("assigned", "rejected")
        or (result["status"] == "assigned" and result.get("account_name") != pending["account_name"])
    ):
        raise DirectTakerError(PEER_DIRECT_PENDING_MESSAGE, code=ErrorCode.PEER_UNREACHABLE)
    return result


@transaction.atomic
def _finish_assignment(order, *, user, binding, pending, result):
    order = lock_order_row(order)
    if order is None:
        raise DirectTakerError(PEER_DIRECT_PENDING_MESSAGE)
    account_name = pending["account_name"]
    current = order.peer_direct_pending
    if current is None or current["operation_id"] != pending["operation_id"]:
        previous = _previous_assignment(order, studio_id=binding.pk, account_name=account_name)
        if previous is not None:
            return previous
        raise DirectTakerError(result.get("message") or PEER_DIRECT_PENDING_MESSAGE)

    if result["status"] == "assigned":
        current_binding = _downstream_bindings().select_for_update(of=("self",)).filter(pk=binding.pk).first()
        if current_binding is None or current_binding.peer_base_url != current["binding_base_url"]:
            raise DirectTakerError(PEER_DIRECT_PENDING_MESSAGE)
        binding = current_binding
    order.peer_direct_pending = None
    order.save(update_fields=["peer_direct_pending"])
    if result["status"] == "rejected":
        return None

    locked_price = Decimal(pending["price"])
    price = locked_price if pending["has_price_override"] or order.hall_price != locked_price else None
    was_in_hall = order.status == OrderStatus.WAITING_GRAB
    outcome = assign_order(order, taker=binding.account, ctx=_outgoing_context(user), price=price, dry_run=False)
    if not outcome.ok:
        raise DirectTakerError(f"{PEER_DIRECT_PENDING_MESSAGE}。上家暂未完成保存：{outcome.msg}")
    if was_in_hall:
        from apps.business import jidanpu_member

        # 直派本身已在下家录单，只补各大厅的撤牌事件。
        peer_dispatch.notify_hall_membership(
            order, from_status=OrderStatus.WAITING_GRAB, target=order.status,
            hall_publisher=None, leave_reason=HALL_TAKE_LEAVE_ASSIGNED,
        )
        jidanpu_member.queue_withdraw(order, hall_only=True, summarize=True)
    peer_dispatch._enqueue_existing_order_screenshots(order)
    order.refresh_from_db(fields=["updated_at"])
    response = {"studio_name": pending["studio_name"], "account_name": account_name}
    PeerSyncEvent.objects.create(
        binding_role="downstream", binding_id=str(binding.pk), direction="out",
        kind=DIRECT_TAKER_ASSIGN_KIND, target_type="order", target_ref=order.order_no,
        idempotency_key=pending["operation_id"], status=PeerSyncEvent.Status.DONE,
        meta={
            **response, "confirmed_price": pending["price"], "direct_target_ref": pending["target_ref"],
            "direct_peer_base_url": binding.peer_base_url, "status_anchor_at": order.updated_at.isoformat(),
            "source_claim_stamp": peer_direct_status.current_claim_stamp(order, binding.account_id),
        },
        processed_at=timezone.now(),
    )
    if "downstream_status" in current:
        peer_inbound.apply_upstream_events([current["downstream_status"]], binding=binding)
    return response


def assign_to_studio(order, *, user, studio_id, account_name, match_token=None, expected_price=_UNSPECIFIED, price=None):
    previous = _previous_assignment(
        order, studio_id=studio_id, account_name=account_name,
        requested_price=price if price is not None else expected_price,
    )
    if previous is not None:
        return previous
    pending = order.peer_direct_pending
    if pending is not None and (
        pending.get("platform") == JIDANPU_DIRECT_PENDING_PLATFORM
        or studio_id != pending["studio_id"] or account_name != pending["account_name"]
    ):
        raise DirectTakerError(PEER_DIRECT_PENDING_MESSAGE)
    binding = _binding_for_studio(studio_id)
    studio_name = pending["studio_name"] if pending is not None else _studio_name_if_enabled(binding)
    if studio_name is None:
        raise DirectTakerError("该工作室未开放直派或暂时无法连接，请刷新后重试")
    order, pending = _stage_assignment(
        order, user=user, binding=binding, studio_name=studio_name,
        account_name=account_name, match_token=match_token, expected_price=expected_price, price=price,
    )
    result = _send_assignment(binding, pending)
    response = _finish_assignment(order, user=user, binding=binding, pending=pending, result=result)
    if response is None:
        raise DirectTakerError(result.get("message") or "下家拒绝指派")
    return response
