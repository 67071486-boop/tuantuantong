"""通用录单解析引擎（D5）。引擎**不含任何字段知识**——它只做「按规则的顺序匹配、抽取、转换」。
字段是什么、长什么样，全在 `intake_rules.py` 的数据里。
解析结果**不直接建单**：它填进表单、允许人工修正（对标物也是这个行为）。"""
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from apps.business.intake_rules import RULES, compiled


@dataclass(frozen=True)
class ParseResult:
    """解析结果。 `unmatched` 是有意暴露的：调用方要能告诉客服「这几项没认出来，请手工填」，
    而不是静默留空。"""

    fields: Dict[str, Any] = field(default_factory=dict)
    matched: Tuple[str, ...] = ()
    unmatched: Tuple[str, ...] = ()


def parse_order_text(text, rules=RULES):
    """从一段自由文本里按规则抽出字段。对每条规则：按顺序试每个 pattern，命中即取第一个捕获组，转换后收下。
    转不出有效值（如金额不是数字）**算作未命中**，不用 None 污染结果。"""
    source = text or ""
    fields = {}
    matched = []
    unmatched = []

    for rule in rules:
        value = None
        for pattern in compiled(rule):
            found = pattern.search(source)
            if not found:
                continue
            raw = found.group(1)
            value = rule.transform(raw) if rule.transform else raw.strip()
            if value not in (None, ""):
                break
            value = None
        if value is None:
            unmatched.append(rule.field)
        else:
            fields[rule.field] = value
            matched.append(rule.field)

    return ParseResult(
        fields=fields, matched=tuple(matched), unmatched=tuple(unmatched)
    )
