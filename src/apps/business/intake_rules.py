"""录单解析规则（D4 + D5）。 **这是数据，不是逻辑。** 引擎（`intake_parser.py`）不含任何字段知识；
以后做「录单识别设置」界面时，读写的就是这份规则，引擎不动。字段名尽量与 `Order` 的字段对齐；需要解析成 FK 的（游戏/区服/项目分类）
以 `_name` 结尾，由调用方负责把文本解析成对象。"""
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class Rule:
    """一条解析规则：从自由文本里抓某个字段。"""

    field: str
    patterns: tuple
    transform: Optional[Callable[[str], Any]] = None


def to_decimal(raw):
    """把抓到的文本转成 Decimal；转不了返回 None（表示这条没解析出来）。
    非有限值（`Infinity` / `NaN`）也是「转不了」：它们是合法的 `Decimal`，
    但不是金额——放进 `total_price` 会在写库时炸，而且不是业务错误码那种炸法。"""
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError):
        return None
    if not value.is_finite():
        return None
    return value


def _strip(raw):
    return raw.strip()


@dataclass(frozen=True)
class RecognitionItem:
    """一个**可配置**的识别项（对标 `System/autodiscernset` 的一行）。 `keywords` 是对标原文的默认值，**一个字都不许加**（spec §3-D3）；
    取值口径（`value_pattern` / `separator` / `label_tail` / `prefix` / `tight_value`
    / `loose_fallback`）是我们自己的实现细节 ——对标那套是「行内命中关键词 + 取分隔符右边的值」，读不出精确正则，属**推断级**（spec §13 #5）。"""

    code: str
    benchmark_id: int
    name: str
    field: str
    keywords: tuple
    value_pattern: str
    separator: str = r"[：:\s]*"
    left_boundary: bool = True
    label_tail: bool = True
    prefix: str = ""
    transform: Optional[Callable[[str], Any]] = None
    #: `value_pattern` **自带边界**（锚在数字 / ASCII 词字符上）⇒ 允许「紧贴」写法，即关键词后
    #: **没有**分隔符也能取到值：`价格100` / `电话13800138000`。这类项的第③备选保留
    #: `separator` 的零长语义（`[：:\s]*`）——它不会把标签文本吞进捕获组。 ⛔ 自由文本项**不能**开这个口子（见 `_pattern` 的说明）。
    tight_value: bool = False
    #: 这一项要不要额外编一条**无左边界**的兜底版本（`_pattern` 的「先严后松」）。
    #: `game_account` = `False`：`账号` ⊂ `子账号`，兜底版会把 `子账号：sub` 里的 `sub`
    #: 写进**代练员用来登录的**主账号字段（P2 教训；`AGENTS.md` §4）。
    loose_fallback: bool = True


#: 14 项，顺序 = 页面顺序 = 对标返回顺序去掉 4 个没有订单字段的项（spec §3-D2）。
RECOGNITION_ITEMS = (
    RecognitionItem("game_server_area", 16, "游戏区服", "server_name",
                    ("服务器", "区服", "平台", "大区", "b服", "官服", "卓还是", "果还"),
                    r"([^\s，,、/]+)"),
    RecognitionItem("server", 18, "服务器", "server_name",
                    ("游戏服", "服务器"), r"([^\s，,、/]+)"),
    # ⚠️ `prefix`：对标关键词是「账号」，而真实文本常写「游戏账号」——左边界会把「戏」当成非分隔字符而整条不命中。带上可选前缀「游戏」，就把今天 `RULES` 里那条
    # `(?:游戏)?账号` 的行为**逐字保留**（这是模式细节，不是关键词）。 ⛔ `loose_fallback=False`：`账号` ⊂ `子账号` ⇒ 这一项**不编**无左边界兜底版，
    # 否则 `子账号：sub` 单行文本会把 `sub` 同时写进主账号（P2 的承重左边界）。
    RecognitionItem("game_account", 2, "游戏账号", "game_account",
                    ("帐号", "战网", "账号", "通行证"), r"([^\s，,、/]+)",
                    separator=r"[：:\s]+", prefix=r"(?:游戏)?", loose_fallback=False),
    RecognitionItem("sub_account", 4, "子副账号", "game_sub_account",
                    ("子账号", "分账号", "子战网", "子帐号", "ID"), r"([^\s，,、/]+)",
                    separator=r"[：:\s]+"),
    RecognitionItem("role_name", 5, "角色名", "game_role_name",
                    ("角色", "名字", "名称", "昵称"), r"([^\s，,、/]+)"),
    RecognitionItem("current_info_1", 6, "当前信息1", "current_info_1",
                    ("装等", "装备", "等级", "当前", "排位", "铭文", "符文"), r"([^\n]+)"),
    RecognitionItem("current_info_2", 15, "当前信息2", "current_info_2",
                    ("盟约", "巅峰", "天赋", "英雄"), r"([^\n]+)"),
    RecognitionItem("project_text", 13, "代练内容", "project_text",
                    ("项目", "内容", "业务", "类型", "要求", "需求", "代", "任务", "标题"), r"([^\n]+)"),
    RecognitionItem("price", 7, "接单价格", "total_price",
                    ("价格", "接单价", "金额"), r"(-?\d+(?:\.\d{1,2})?)", transform=to_decimal,
                    tight_value=True),
    RecognitionItem("order_source", 14, "订单来源", "source",
                    ("来源", "店铺"), r"([^\s，,、/]+)"),
    RecognitionItem("source_order_no", 9, "来源单号", "source_order_no",
                    ("订单编号", "单号", "订单号", "定单编号"), r"([^\s，,、/]+)"),
    RecognitionItem("phone", 10, "联系电话", "customer_phone",
                    ("手机", "电话", "联系方式"), r"(\d{6,20})", tight_value=True),
    RecognitionItem("qq", 11, "QQ", "customer_qq",
                    ("qq", "微信"), r"([A-Za-z0-9_\-]{4,32})"),
    RecognitionItem("ww", 12, "旺旺", "customer_ww",
                    ("旺旺", "淘宝旺旺"), r"([^\s，,、/]+)"),
)

#: 对标 18 项里**没有对应行**的两条（spec §3-D4）：照今天的模式**逐字保留**，不进页面。
BUILTIN_RULES = (
    Rule("game_name", (r"(?:游戏)[：:\s]+([^\s，,、/]+)",), _strip),
    Rule("received_price", (r"(?:接单实收|实收)[：:\s]*(-?\d+(?:\.\d{1,2})?)",), to_decimal),
)

#: 关键词串的分隔符：逗号（半角/全角）、顿号、空白（对标输入框提示语说的「逗号或空格」）。
_KEYWORD_SPLIT = re.compile(r"[,，、\s]+")


def split_keywords(raw):
    """把页面上的关键词串切成**有序去重**的元组。空串 ⇒ 空元组（= 这一项不再识别）。"""
    out = []
    for part in _KEYWORD_SPLIT.split(raw or ""):
        if part and part not in out:
            out.append(part)
    return tuple(out)


def _pattern(item, keywords, left_boundary):
    """一条识别项的取值正则。 - `(?i)`：对标的关键词里有小写 `qq` / `ID`，而文本里可能写 `QQ` / `id`； - 左边界 `(?:^|[\\s，,、;；])`：P2 的教训（否则 `子账号：sub` 会把 `sub` 当主账号）； - **标签尾巴** `[^：:\\n]{0,4}`：对标的关键词比我们原来的词短（`角色` vs `角色名`），没有这一段会把「角色名：旅行者」取成「名：旅行者」。 `label_tail=True` 时是**三条备选、顺序敏感**（照抄下面这版，别精简成两条——编排方实测过）： | 序 | 形状 | 保住的写法 |
    |---|---|---| | ① | `<head>[^：:\\n]{0,4}[：:]\\s*` | `角色名：旅行者`、`当前信息1：500` | | ② | `<head>[^：:\\n]{0,4}[：:\\s]\\s*` | `装备等级 500`（**没有②会把值取成「等级 500」**） | | ③ | `<head><separator>` | `电话13800138000`（**紧贴**、无分隔符；今天静态规则的行为） | ⚠️ ②是编排方用探针**实测**补上的一条：只有 ①+③ 时，`装备等级 500` 走③的 `[：:\\s]*`（零长也算）⇒ `([^\\n]+)` 把「等级 500」整段吃进去。
    ⚠️ ③的分隔符**分两类**（`tight_value`）： - `tight_value=True`（`price` / `phone`）⇒ 用 `item.separator`（零长也算）。它们的 `value_pattern` 锚在数字上，零长分隔符**不可能**把标签文本吃进捕获组。 - `tight_value=False`（其余项，值模式是自由文本）⇒ **至少一个** `[：:\\s]+`。否则③会变成「短关键词吞长标签」：`代` 是 `代练/陪玩项目` 的第一个字，零长分隔符下 `([^\\n]+)` 会把 `练/陪玩项目：满星满命` 整段当成 `project_text` （**静默写错权威值**）。代价如实登记在 spec §13 #5：自由文本的**紧贴**写法（`服务器X` / `项目X` / `QQabc12345`，关键词后一个分隔符都没有）不再识别。"""
    alt = "|".join(re.escape(k) for k in keywords)
    head = item.prefix + f"(?:{alt})"
    if left_boundary:
        head = r"(?:^|[\s，,、;；])" + head
    if item.label_tail:
        tight_sep = item.separator if item.tight_value else r"[：:\s]+"
        body = (
            f"(?:{head}[^：:\\n]{{0,4}}[：:]\\s*"
            f"|{head}[^：:\\n]{{0,4}}[：:\\s]\\s*"
            f"|{head}{tight_sep})"
        )
    else:
        body = head + item.separator
    return "(?i)" + body + item.value_pattern


def _patterns_for(item, keywords):
    """一个字段的 pattern 序列：**先严后松**（顺序敏感）。 ① **有左边界**的版本（`_pattern` 的现状）——P2 的左边界与「子账号不吃主账号」靠它； ② **无左边界**的兜底版本——只在①**整条不命中**时才会被 `parse_order_text` 试到
    （引擎按 pattern 顺序试、先命中先赢）。为什么需要②：关键词常常处在**更长标签的中部**（`联系电话` 的 `电话`、`陪玩项目` 的 `项目`、`我的角色名` 的 `角色`、`代练/陪玩项目` 的 `项目`）——左边界让①整条不命中，
    而这些正是**录单表单自己的标签**（`src/frontend/src/views/orders/orderFormFields.ts`）。 ⛔ `game_account` 用 `loose_fallback=False` 显式**不编**②，见 `RecognitionItem` 的说明。"""
    if not item.left_boundary:
        return (_pattern(item, keywords, left_boundary=False),)
    if not item.loose_fallback:
        return (_pattern(item, keywords, left_boundary=True),)
    return (
        _pattern(item, keywords, left_boundary=True),
        _pattern(item, keywords, left_boundary=False),
    )


def build_rules(overrides=None):
    """把识别项 + 覆盖值编成规则元组（顺序 = 字段首次出现的顺序，末尾接 `BUILTIN_RULES`）。 ⚠️ **按字段合并**：`游戏区服` 与 `服务器` 都映射 `server_name` ⇒ 只产出一条规则；
    否则同一字段两条规则，`parse_order_text` 的 `unmatched` 会报一个**已经识别成功**的字段。 ⚠️ 每条规则的 `patterns` 是**先严后松**（无左边界的兜底版在后，见 `_patterns_for`）：
    引擎 `parse_order_text` 对每条规则**按 pattern 顺序试、先命中先赢**，所以兜底版只在严格版整条不命中时生效——`子账号：sub\\n账号：main` 里主账号仍由严格版取到 `main`。"""
    overrides = overrides or {}
    merged: dict = {}
    order = []
    for item in RECOGNITION_ITEMS:
        raw = overrides.get(item.code)
        keywords = split_keywords(
            ",".join(item.keywords) if raw is None else raw
        )
        if not keywords:
            continue
        slot = merged.setdefault(item.field, {"item": item, "keywords": []})
        if item.field not in order:
            order.append(item.field)
        slot["keywords"].extend(k for k in keywords if k not in slot["keywords"])
    rules = tuple(
        Rule(
            field=field,
            patterns=_patterns_for(merged[field]["item"], merged[field]["keywords"]),
            transform=merged[field]["item"].transform,
        )
        for field in order
    )
    return rules + BUILTIN_RULES


#: 默认配置编出来的规则。保留这个名字，既有导入点（引擎与其它调用方）不必改。
RULES = build_rules()

_PATTERN_CACHE = {}


def compiled(rule):
    """把规则的字符串模式编译成 Pattern 并缓存。缓存键是 **`(field, patterns)` 而不是 `field`**：字段名相同的两套规则
    （自定义规则、或以后「录单识别设置」里同字段多规则）必须各自编译，否则先编译的那套会被后者覆盖，出现「拿旧 pattern 解析新数据」的静默错配。"""
    key = (rule.field, tuple(rule.patterns))
    if key not in _PATTERN_CACHE:
        _PATTERN_CACHE[key] = tuple(re.compile(p) for p in rule.patterns)
    return _PATTERN_CACHE[key]
