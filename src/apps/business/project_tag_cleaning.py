"""价格看板中心的**清洗引擎**（纯函数、零 Django 依赖）。三步：**切段 → 剥噪音 → 归一**。形态照 `intake_parser.py`：引擎**不含任何取值知识**——噪音词、分隔符、括号对全部住在 `project_tag_rules.py` 的数据里（单一出处）。规则用 `rules=` 形参传进来，默认取
`DEFAULT_CLEANING_RULES` ⇒ 租户级覆盖不必改引擎。 ⚠️ **「洗成空」是合法结果，而且必须可观测**：实测约 12% 的订单正文整段都是客服模板，剥完噪音什么都不剩。设计稿 §2.1 明写这类订单**照样要落行**
（`OrderTagging.status=PENDING`）——不落，则「多少单没归上类」这个最重要的健康指标就看不见了。因此 `CleanResult.is_empty` 是给调用方看的**观测点**： ⛔ 调用方不许因为 `cleaned == ""` 就静默跳过这一单。"""
from dataclasses import dataclass
from typing import Optional, Tuple

from apps.business.project_tag_rules import (
    DEFAULT_CLEANING_RULES,
    CleaningRules,
    bracket_pattern,
    delimiter_pattern,
    long_digits_pattern,
    whitespace_pattern,
)

#: 全角数字 → 半角（设计稿 §3 的「全角归一」）。
#: ⚠️ 只转**数字**：全角字母 / 全角标点不在口径里，⛔ 不要顺手加。
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


@dataclass(frozen=True)
class CleanResult:
    """一次清洗的**全部**读数（一个字段都不许丢，理由见模块 docstring）。 - `cleaned`：归一后把保留的段拼起来的字符串。**可以是空串**（合法结果）。
    - `kept`：保留下来的段（归一后、顺序不变）。调用方要按段匹配词典时用它。 - `noise_dropped`：命中噪音词被丢掉的段（**原样**，便于体检「词表是不是吃多了」）。
    - `empty_dropped`：没命中噪音、但归一后变成空串的段（典型：整段就是一个订单号）。 - `is_empty`：`cleaned` 是不是空串 —— **观测点**，⛔ 不是「这单不用处理」的意思。"""

    cleaned: str
    kept: Tuple[str, ...]
    noise_dropped: Tuple[str, ...]
    empty_dropped: Tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return self.cleaned == ""


def split_segments(source, rules: CleaningRules = DEFAULT_CLEANING_RULES) -> Tuple[str, ...]:
    """切段：先把成对括号里的内容摊平进文本，再按分隔符切。 ⚠️ 括号内容**也是段**（不是被丢掉）：实测真实文本里既有 `（不送货）` 这类纯噪音，
    也有 `【5部门升4】` 这类**项目名本体** ⇒ 连括号一起丢会把项目名也丢掉。 ⚠️ 摊平时用的占位符就是**第一个分隔符**，所以括号内外的段顺序不会乱
    （`【A】B` ⇒ `A`、`B`，而不是 `B`、`A`）。"""
    flattened = source or ""
    placeholder = rules.delimiters[0]
    for open_ch, close_ch in rules.brackets:
        pattern = bracket_pattern(open_ch, close_ch)
        flattened = pattern.sub(
            lambda found: placeholder + found.group(1) + placeholder, flattened
        )
    return tuple(delimiter_pattern(rules).split(flattened))


def noise_hit(segment: str, rules: CleaningRules = DEFAULT_CLEANING_RULES) -> Optional[str]:
    """这一段命中了哪条噪音词（没有则 `None`）。判据是**包含**，不是整段相等 —— 口径、代价与它的依据写在
    `project_tag_rules.NOISE_PHRASES` 上方。返回命中的那一条本身，
    是为了让 `CleanResult.noise_dropped` 能回答「是哪个词吃掉的」。"""
    for phrase in rules.noise_phrases:
        if phrase in segment:
            return phrase
    return None


def normalize_segment(text: str, rules: CleaningRules = DEFAULT_CLEANING_RULES) -> str:
    """归一：全角数字转半角 → 剔掉 15 位以上的长数字串（订单号）→ 去空白与全角空格。
    ⚠️ 三步的**顺序是承重的**：先转全角数字，否则全角写法的订单号躲过 `\\d{15,}`；
    先剔长数字串，否则去空白之后不好判断那串数字是否连续。"""
    out = text.translate(_FULLWIDTH_DIGITS)
    out = long_digits_pattern(rules).sub("", out)
    out = whitespace_pattern().sub("", out)
    return out


def clean_order_text(source, rules: CleaningRules = DEFAULT_CLEANING_RULES) -> CleanResult:
    """把一段自由文本洗成「段列表 + 拼回来的串」。 ⛔ 空输入、纯噪音、纯订单号都返回**空串 + `is_empty=True`**，不抛异常、也不返回 `None`
    —— 调用方靠 `is_empty` 判断，⛔ 不靠 `if result`。"""
    kept = []
    noise_dropped = []
    empty_dropped = []

    for raw in split_segments(source, rules):
        segment = raw.strip()
        if not segment:
            continue
        if noise_hit(segment, rules) is not None:
            noise_dropped.append(segment)
            continue
        normalized = normalize_segment(segment, rules)
        if not normalized:
            empty_dropped.append(segment)
            continue
        kept.append(normalized)

    return CleanResult(
        cleaned=rules.segment_join.join(kept),
        kept=tuple(kept),
        noise_dropped=tuple(noise_dropped),
        empty_dropped=tuple(empty_dropped),
    )
