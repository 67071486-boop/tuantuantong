"""Render the approved WxPusher business notification templates."""

import re
from dataclasses import dataclass
from datetime import datetime
from html import escape, unescape
from unicodedata import normalize

from django.utils import timezone
from django.utils.html import strip_tags


@dataclass(frozen=True)
class NoticeTemplate:
    title: str
    intro: str = ""
    reason_label: str = ""
    action: str = ""
    show_taker_price: bool = False


STAFF_KINDS = ("approval_assigned", "refund_assigned")
HALL_NEW_ORDER = "hall_new_order"
GROUP_MENTION = "group_mention"
GROUP_REPLY = "group_reply"
GROUP_MESSAGE = "group_message"
GROUP_CHAT_KINDS = {GROUP_MENTION, GROUP_REPLY, GROUP_MESSAGE}
DIRECT_MESSAGE = "direct_message"
CHAT_KINDS = GROUP_CHAT_KINDS | {DIRECT_MESSAGE}


NOTICE_TEMPLATES = {
    GROUP_MENTION: NoticeTemplate("订单群里有人 @ 你"),
    GROUP_REPLY: NoticeTemplate("订单群里有人回复你"),
    GROUP_MESSAGE: NoticeTemplate("订单群有新消息"),
    DIRECT_MESSAGE: NoticeTemplate("单聊有新消息"),
    "assigned": NoticeTemplate("收到新的订单指派", "工作室给你指派了一张新订单。", show_taker_price=True),
    "unassigned": NoticeTemplate("订单已撤销指派"),
    "review_rejected": NoticeTemplate(
        "订单提交验收不通过", reason_label="未通过原因", action="请按验收要求补充材料，再提交验收。",
    ),
    "completed": NoticeTemplate("订单已完成", "你的订单已完成。", show_taker_price=True),
    "paused": NoticeTemplate("订单已暂停", "这张订单已暂停，请先暂停操作。", reason_label="暂停原因"),
    "resumed": NoticeTemplate("订单已恢复", "这张订单已恢复，可以继续处理。"),
    "issue": NoticeTemplate(
        "订单被标为问题单", "这张订单已被标为问题单。", reason_label="问题说明", action="请先和客服确认处理方式。",
    ),
    "refunded": NoticeTemplate("订单已退单", "这张订单已退单，请停止处理。", reason_label="退单原因"),
    "settled": NoticeTemplate("你的订单报酬已结账"),
    "transfer_succeeded": NoticeTemplate("线上转账成功", "支付渠道已确认本次转账成功。"),
    "approval_assigned": NoticeTemplate("有一条罚款审批待你处理", "请到审批中心的「待我处理」查看。"),
    "refund_assigned": NoticeTemplate("有一条退款待你处理", "请到审批中心的「待我处理」查看。"),
    HALL_NEW_ORDER: NoticeTemplate("接单大厅有新订单", "符合你订阅条件的新单已进入接单大厅。", show_taker_price=True),
}
SETTLEMENT_KINDS = {"settled", "transfer_succeeded"}
ORDER_EVENTS = ("assigned", "unassigned", "review_rejected", "completed", "paused_resumed", "issue", "refunded")


def order_event_key(kind):
    return "paused_resumed" if kind in {"paused", "resumed"} else kind


_AMOUNT = re.compile(r"-?[0-9]+\.[0-9]{2}")
_SENSITIVE_ORDER_TEXT = re.compile(
    r"""
    账号|帐号|账户|密码|口令|验证码|密钥|手机|电话|联系|邮箱|微信|v[ \t.-]*信
    |QQ|WeChat|password|passwd|credential
    |(?<![A-Za-z0-9_])
      (?:
          account(?:[ \t]+(?:id|name))?
          |(?:discord|telegram|whatsapp)(?:[ \t]+(?:id|handle))?
          |(?:phone|telephone|mobile)(?:[ \t]+number)?
          |contact(?:[ \t]+(?:info|number))?
          |user(?:name|[ \t]+id)
          |steam[ \t]+id
      )[ \t]*[:：=]
    |(?<![A-Za-z0-9_])line[ \t]+(?:id|handle)\b
    |(?<![A-Za-z0-9_])(?:(?:discord|telegram)[ \t]+|line[ \t]*[:：=][ \t]*)@[A-Za-z0-9_]
    |(?P<standalone_contact>
      (?<!\d)(?:\+?86[-\s]?)?1[3-9](?:[-\s]?\d){9}(?!\d)
      |(?<!\d)(?:\([ \t]*0\d{2,3}[ \t]*\)[ \t]*|0\d{2,3}[- \t]+)\d{7,8}(?!\d)
      |[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}
    )
    """,
    re.I | re.X,
)


_HALL_ACCEPTANCE = r"联系(?:工作室|客服)验收(?:即可|就行)?"
_HALL_TASK_PHRASES = re.compile(rf"(?:手机游戏|{_HALL_ACCEPTANCE})(?=[ \t]*(?:[,;。!?\r\n]|$))")
_HALL_LOGIN_FIELD = re.compile(
    r"登录名|用户名|(?:(?:手机)?游戏|登录)[ \t]*(?:id|uid)\b|\blogin[ \t]+name\b"
    r"|(?<![A-Za-z0-9_])(?:id|uid)[ \t]*[:=]",
    re.I,
)
_HALL_BARE_CONTACT = re.compile(r"(?<![A-Za-z0-9_])(?:wxid_|@)[A-Za-z0-9_][A-Za-z0-9_.-]*", re.I)
_HALL_INVISIBLE = re.compile(r"[\u200b-\u200d\ufeff]")
_HALL_MATCH_CHARACTERS = str.maketrans({
    "碼": "码", "帳": "账", "賬": "账", "號": "号", "戶": "户", "錄": "录",
    "驗": "验", "證": "证", "鑰": "钥", "機": "机", "電": "电", "話": "话",
    "憑": "凭", "據": "据", "陸": "陆",
    "聯": "联", "繫": "系", "係": "系", "郵": "邮",
    "遊": "游", "戲": "戏", "務": "务", "練": "练", "內": "内", "後": "后",
    "場": "场", "對": "对", "兩": "两",
})
_HALL_NUMBER_WRAP = re.compile(r"(?<=\d)[ \t]*\r?\n[ \t]*(?=\d)")
_HALL_PRIVATE_TOKEN = re.compile(r"[!-~]+")
_HALL_PRIVATE_LABEL = re.compile(
    r"(?:(?:手机)?游戏)?(?:[账帐]密|[账帐][号户]密码|登[录入陆](?:密码|凭[证据]))"
    r"|微信号|手机号|联系方式|QQ号",
    re.I,
)
_HALL_CLAUSE_BREAK = re.compile(r"([，,；;。！？!?\r\n]+)")
_HALL_EXPLICIT_TASK = re.compile(
    rf"(?:(?:任务|代练内容)[ \t]*:[ \t]*)?"
    rf"(?:手机游戏|(?:完成后|打完后)?{_HALL_ACCEPTANCE}|"
    r"(?:打|完成)(?:排位|匹配|对局)[ \t]*[0-9一二三四五六七八九十百两]{1,3}[ \t]*(?:场|把|局))"
)


def _hall_match_text(value):
    return _HALL_INVISIBLE.sub("", normalize("NFKC", value)).translate(_HALL_MATCH_CHARACTERS)


def _hall_text_has_private_data(value):
    checked = _HALL_TASK_PHRASES.sub(" ", _hall_match_text(value))
    return any(pattern.search(checked) for pattern in (
        _SENSITIVE_ORDER_TEXT, _HALL_LOGIN_FIELD, _HALL_BARE_CONTACT, _HALL_PRIVATE_LABEL,
    ))


def _hall_standalone_contact(clause):
    if _HALL_BARE_CONTACT.fullmatch(clause):
        return True
    match = _SENSITIVE_ORDER_TEXT.fullmatch(clause)
    return bool(match and match.group("standalone_contact"))


def _hall_labeled_private_field(clause):
    field = re.split(r"([:=])", clause, maxsplit=1)
    if len(field) != 3:
        return False
    label, delimiter, value = field[0].strip(), field[1], field[2].strip()
    # A private label followed by prose can refer to another clause in the order.
    if not (_HALL_PRIVATE_TOKEN.fullmatch(value) or _hall_standalone_contact(value)):
        return False
    return bool(
        _HALL_PRIVATE_LABEL.fullmatch(label)
        or _SENSITIVE_ORDER_TEXT.fullmatch(label)
        or _SENSITIVE_ORDER_TEXT.fullmatch(label + delimiter)
        or _HALL_LOGIN_FIELD.fullmatch(label)
        or _HALL_LOGIN_FIELD.fullmatch(label + delimiter)
    )


def _public_hall_tasks(value):
    if not _hall_text_has_private_data(value):
        return value

    parts = _HALL_CLAUSE_BREAK.split(_HALL_NUMBER_WRAP.sub(" ", value))
    public = []
    previous = -2
    allow_task = True
    for index in range(0, len(parts), 2):
        original = parts[index].strip()
        if not original:
            continue
        clause = _hall_match_text(original)
        if _HALL_EXPLICIT_TASK.fullmatch(clause):
            if allow_task:
                if public:
                    public.append(parts[index - 1] if index == previous + 2 else "，")
                public.append(original)
                previous = index
            parts[index] = ""
            continue
        labeled = _hall_labeled_private_field(clause)
        if labeled or _hall_standalone_contact(clause):
            parts[index] = ""
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        allow_task = labeled and separator in ("。", "；", ";")

    # Any remaining private hint could refer to a clause we planned to publish.
    if _hall_text_has_private_data("".join(parts)):
        return ""
    return "".join(public)


def safe_notice_order_text(value, *, sensitive_fallback, limit, recover_public_tasks=False):
    value = value.strip()
    if value and recover_public_tasks:
        value = _public_hall_tasks(value)
        if not value:
            return sensitive_fallback
    elif _SENSITIVE_ORDER_TEXT.search(normalize("NFKC", value)):
        return sensitive_fallback
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _text(details, key):
    value = details.get(key)
    return value.strip() if isinstance(value, str) else ""


def _amount(details):
    value = _text(details, "amount")
    return f"¥{value}" if _AMOUNT.fullmatch(value) else ""


def _time(details):
    try:
        value = datetime.fromisoformat(_text(details, "occurred_at"))
    except ValueError:
        return ""
    if timezone.is_naive(value):
        return ""
    local = timezone.localtime(value)
    return f"{local.month} 月 {local.day} 日 {local:%H:%M}"


def _fields(pairs):
    return [f"<strong>{label}</strong>　{escape(value)}" for label, value in pairs if value]


def _settlement_lines(kind, template, details):
    amount = _amount(details)
    occurred_at = _time(details)
    if kind == "transfer_succeeded":
        return [template.intro, *_fields((
            ("转账金额", amount), ("转账方式", "支付宝"), ("转账时间", occurred_at),
        ))]

    count = details.get("count")
    count_text = f" <strong>{count} 笔订单</strong>" if type(count) is int and count > 0 else ""
    amount_text = f"，本次结账金额 <strong>{amount}</strong>" if amount else ""
    return [
        f"工作室已为你结账{count_text}{amount_text}。",
        *_fields((("结账时间", occurred_at),)),
    ]


def notice_display_order_no(order_no, details):
    return _text(details, "display_order_no") or order_no


def _notice_lines(kind, order_no, details):
    template = NOTICE_TEMPLATES[kind]
    if kind == DIRECT_MESSAGE:
        return _fields((("发件人", _text(details, "sender")),))
    if kind in GROUP_CHAT_KINDS:
        return _fields((
            ("订单号", notice_display_order_no(order_no, details)),
            ("发件人", _text(details, "sender")),
        ))
    if kind in STAFF_KINDS:
        return [template.intro]
    if kind in SETTLEMENT_KINDS:
        return _settlement_lines(kind, template, details)
    demand = _text(details, "demand") if kind != HALL_NEW_ORDER else ""
    project = _text(details, "project") or ("" if demand else "未填写")
    return [
        template.intro,
        *_fields((
            ("订单号", notice_display_order_no(order_no, details)),
            ("游戏", _text(details, "game_name") if kind == HALL_NEW_ORDER else ""),
            ("区服", _text(details, "server_name") if kind == HALL_NEW_ORDER else ""),
            ("代练内容", project),
            ("需求描述", demand),
            ("接单金额", _amount(details) if template.show_taker_price else ""),
            ("说明", "已撤销指派" if kind == "unassigned" else ""),
            (template.reason_label, _text(details, "reason") if template.reason_label else ""),
        )),
        template.action,
    ]


_LINK_LABELS = {
    HALL_NEW_ORDER: "前往抢单",
    DIRECT_MESSAGE: "打开单聊",
}


def render_notice(kind, order_no, details, url, *, link_label=None, unread_count=None):
    if link_label is None:
        link_label = "打开群聊" if kind in GROUP_CHAT_KINDS else _LINK_LABELS.get(kind, "请前往查看")
    lines = _notice_lines(kind, order_no, details)
    if unread_count is not None:
        lines.insert(1, f"<strong>未读消息</strong>　{unread_count} 条")
    lines.append(f'<a href="{escape(url)}">{escape(link_label)}</a>')
    return NOTICE_TEMPLATES[kind].title, "".join(f"<p>{line}</p>" for line in lines if line)


def notice_text_lines(kind, order_no, details):
    return [unescape(strip_tags(line)) for line in _notice_lines(kind, order_no, details) if line]
