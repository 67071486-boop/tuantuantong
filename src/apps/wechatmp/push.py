"""公众号模板消息推送渠道（计划 T5；v1 = **租户级全局开关**，不动 `OrderPushSetting`）。

## 形态（H2 订正后的 v1 口径）

模板消息**按收件人逐个发**（每个已绑定用户一次 `template/send`），与「一行规则一个
webhook」的 `OrderPushSetting` 表结构不同 ⇒ 本渠道不挂规则行：

- 开关 = `WechatMpConfig.enabled` **且** `template_id` 非空（租户级，全局生效）；
- 投递目标 = **该单的 `sales_user`**（销售提成归属人，`business/models.py` S5 钉字段）
  **与 MAIN 接手打手**（`Performance.emp_type=MAIN` 且 `user` 非空）中**已绑定公众号**
  （`WechatMpBinding.subscribed=True`）的人；逐规则粒度 = 后续需求。

## 线程禁查库（承重约束，`order_push.py:375-385` 明文）

django-tenants 的 schema 是**连接级**状态；投递线程是新连接、没有请求线程的租户 schema
⇒ **access_token、收件人 openid 清单、载荷全部在请求线程取好、作为普通值随线程参数带过去**
（`_send_template` 里只有一次 httpx POST，⛔ 不碰任何 ORM）。

## 失败口径（H2 订正引述）

照既有 webhook 通道口径：**不重试、不落库、只写 logger**（openid 只留前缀，同
`order_push` 只留 host 的凭据纪律）。要落库台账 = 新写入面，须按网登记流程另立（v1 不做）。
"""
from __future__ import annotations

import logging
import threading

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from apps.business.order_push import EVENT_HALL
from apps.wechatmp.client import WechatMpApiError
from apps.wechatmp.models import WechatMpBinding, WechatMpConfig
from apps.wechatmp.services import acquire_access_token, api_client, judge_body

logger = logging.getLogger(__name__)


def public_base_url() -> str:
    """模板消息跳转用的对外基址（生产必须配 `PEER_SYNC_LOCAL_BASE_URL`，
    见 `config/settings.py`；没配 ⇒ 空串，模板消息不带跳转 url）。"""
    return (django_settings.PEER_SYNC_LOCAL_BASE_URL or "").rstrip("/")


def _money(value) -> str:
    return "未设置" if value is None else f"¥{value:.2f}"


#: 模板 keyword 单值的**自截长度**（⭐ D-0301）：来源单号是自由文本、最长 128，
#: 而公众号模板 keyword 超长会被微信**截断/拒收**——我们在出站前自己截到 20 字符
#: （= 原口径内置号恰好 20 位 ⇒ 不缩短任何过去发得出去的值），超出部分宁可不出站。
KEYWORD_VALUE_MAX = 20


def _keyword2_value(order) -> str:
    """`keyword2`（订单号）的取值（⭐ D-0301）：来源单号非空取它、空回退内置号，
    再按 `KEYWORD_VALUE_MAX` 自截（理由见常量注释）。"""
    value = order.source_order_no or order.order_no
    return value[:KEYWORD_VALUE_MAX]


def build_template_payload(order, event: str, from_status: str | None, *, url: str) -> dict:
    """模板消息载荷（**纯函数**，测试钉值；v1 契约 = 旧版「订单动态」模板的
    `first / keyword1~3 / remark` 结构，设置页有同款说明）。"""
    if event == EVENT_HALL:
        header = "新单上架"
        keyword3 = f"发单价：{_money(order.hall_price)}"
    else:
        header = "订单动态"
        try:
            from apps.business.models import OrderStatus

            before = OrderStatus(from_status).label if from_status else "—"
            after = OrderStatus(order.status).label
        except ValueError:
            before, after = (from_status or "—"), order.status
        keyword3 = f"状态：{before} → {after}"
    payload = {
        "touser": "",  # 由投递侧按收件人填（载荷按人复制，别共享同一个 dict 再改）。
        "template_id": "",
        "data": {
            "first": {"value": header},
            "keyword1": {"value": order.game.name},
            "keyword2": {"value": _keyword2_value(order)},
            "keyword3": {"value": keyword3},
            "remark": {
                "value": timezone.localtime(timezone.now()).strftime("%Y-%m-%d %H:%M")
            },
        },
    }
    if url:
        payload["url"] = url
    return payload


def _recipient_user_ids(order) -> set[int]:
    """收件人 = `sales_user` + MAIN 接手打手（S5 钉字段；去重）。**请求线程内**调（查库）。"""
    from apps.business.models import EmpType, Performance

    ids: set[int] = set()
    if order.sales_user_id is not None:
        ids.add(order.sales_user_id)
    ids.update(
        Performance.objects.filter(
            order_id=order.pk, emp_type=EmpType.MAIN, user__isnull=False
        ).values_list("user_id", flat=True)
    )
    return ids


def queue_wechatmp_push(order, event: str, from_status: str | None = None) -> int:
    """模板消息渠道的入口；**在 `order_push.queue_order_push` 的业务事务里**调。

    返回**排队**条数（0 = 未启用 / 无人绑定 —— 渠道不存在的常态，非错误；
    实际发送在事务提交后进行，token 取不到则 0 条）。
    """
    config = WechatMpConfig.objects.filter(enabled=True).first()
    if config is None or not config.template_id:
        return 0
    user_ids = _recipient_user_ids(order)
    if not user_ids:
        return 0
    bindings = list(
        WechatMpBinding.objects.filter(
            user_id__in=user_ids, appid=config.appid, subscribed=True
        )
    )
    if not bindings:
        return 0
    # ⭐ M1（独立评审）：`acquire_access_token` 有行锁 + 最长 10s 出网，
    # ⛔ 不在 `queue_order_push` 的业务事务里同步做——整段取/发挂 `on_commit`
    # （webhook 通道 `_deliver_later` 的同一条纪律）；提交前只算收件人。
    # 返回值 = **排队**条数（收件人数）；token 取不到时在提交后记一条 warning、按 0 条发送。
    base = public_base_url()

    def _dispatch() -> None:
        token, err = acquire_access_token(config=config)
        if err:
            logger.warning("公众号模板消息推送跳过（token 获取失败）：%s", err)
            return
        for binding in bindings:
            payload = build_template_payload(order, event, from_status, url=f"{base}/m/" if base else "")
            payload["touser"] = binding.openid
            payload["template_id"] = config.template_id
            _deliver_template_later(
                access_token=token,
                payload=payload,
                openid=binding.openid,
            )

    transaction.on_commit(_dispatch)
    return len(bindings)


def _send_template(*, access_token: str, payload: dict, openid: str) -> None:
    """线程入口。**只发一次 httpx、不碰数据库**（django-tenants schema 是连接级状态）。"""
    try:
        body = api_client().send_template(access_token=access_token, payload=payload)
    except WechatMpApiError as exc:
        logger.warning(
            "公众号模板消息发送失败 openid=%s…：%s", openid[:8], exc
        )
        return
    errcode, msg = judge_body(body, write=True)
    if errcode is not None:
        # 失败信封必须被判失败（S1）；v1 照 webhook 通道口径只记日志。
        logger.warning("公众号模板消息发送失败 openid=%s…：%s", openid[:8], msg)


def _deliver_template_later(*, access_token: str, payload: dict, openid: str) -> None:
    """提交之后再起线程（`transaction.on_commit` 由调用方保证在事务内）。"""

    def _start() -> None:
        threading.Thread(
            target=_send_template,
            kwargs={
                "access_token": access_token,
                "payload": payload,
                "openid": openid,
            },
            name="wechatmp-template-push",
            daemon=True,
        ).start()

    transaction.on_commit(_start)
