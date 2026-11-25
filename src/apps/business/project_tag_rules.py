"""价格看板中心的「清洗 + 归类」规则与口径常量（**这是数据，不是逻辑**）。形态照 `intake_rules.py`：引擎（`project_tag_cleaning.py`）**不含任何取值知识**——
噪音词表、分隔符、括号对、项目类别、时间窗档位、样本量阈值全部住在本模块，以后做「控价中心」的设置界面时读写的就是这份数据，引擎不动。 ⚠️ **零 Django 依赖**：本模块只 import 标准库 ⇒ 任何地方（含纯单元测试）都能直接用。
⚠️ **单一出处**：噪音词表 / 分隔符 / 括号对**只在这里写一份**（设计稿 `docs/plans/2026-10-11-price-board-center.md` §2.2 ＋ 批次 1 计划 Task 2 的硬要求）， ⛔ 不要在清洗引擎、定时任务、管理命令里各抄一份。"""
import re
from dataclasses import dataclass
from typing import Tuple

# ── 切段 ────────────────────────────────────────────────────────────────────

#: 成对括号：(开, 闭)。⚠️ **括号里的内容会被摊平成独立的段**（见 `split_segments`），
#: ⛔ 不是被丢掉——实测真实文本里既有 `（不送货）` 这类纯噪音，也有 `【5部门升4】`
#: 这类**项目名本体**，连括号一起丢会把项目名也丢掉。
PAIRED_BRACKETS: Tuple[Tuple[str, str], ...] = (
    ("【", "】"),
    ("「", "」"),
    ("[", "]"),
    ("『", "』"),
    ("（", "）"),
    ("(", ")"),
)

#: 段分隔符（设计稿 §2.2 的口径）。⚠️ **`，` 与 `：` 不在其中**——它们是句内标点，
#: 拿它们切会把一句话切碎。因此「一段里既有模板词又有业务内容」是常态（见 `NOISE_PHRASES`）。
#: ⚠️ `\r` 不在其中：`\r\n` 会在归一那步被当空白去掉，不必单列。
SEGMENT_DELIMITERS: Tuple[str, ...] = ("/", "、", "\n", "|", ";", "；", "➕", "+")

#: 归一后把保留的段拼回一个字符串时用的连接符。 ⚠️ **它同时也必须是 `SEGMENT_DELIMITERS` 里的成员**：段里不可能含有分隔符
#: ⇒ 拼出来的串再洗一遍会得到**同一组段**（清洗是幂等的，这一点有测试钉住）。选 `+` 是因为**词典里的标准项目名就是这个形状**（`4.6主线+4.6地图全收集+4.6大活动`）
#: ⇒ `content_clean` 可以直接跟 `ProjectTagDict.name` 对照着看。
SEGMENT_JOIN = "+"

# ── 剥噪音 ──────────────────────────────────────────────────────────────────

#: 客服模板噪音词的**代码默认值**（实测「高频段自动发现 ＋ 人工补」的那批）。 ⚠️ 顺序即**报告顺序**：一段命中多个词时记的是**第一个**命中的那个（`不包前置任务不包主线` 排在 `不包主线` 前面，就是因为后者是前者的子串）。 ⚠️ 判据是**包含**（`词 in 段`），不是整段相等：`，`/`：` 都不是分隔符 ⇒ 模板常与业务内容落在同一段里。⚠️ **这条口径的代价已实测**：本仓 `.tools/recon/merged.json` 的 3001 条里有 **6 条（0.2%）**会因此被整段丢掉，
#: 而段里含业务内容（形状：`<业务内容>不包全图鉴<尾字>`）。这是按任务书原文「命中噪音词的段丢掉」取的**字面口径**，有测试钉住；要改成「整段相等才丢」只需换掉 `project_tag_cleaning.noise_hit` 的比较。 ⛔ **这里本该还有一条，被有意留空**：原始词表第 2 条含**对标租户的品牌名**，而那个名字在 `.tools/denylist.txt`（不进 git）的**真值黑名单**里，守卫 `src/frontend/src/config/__tests__/pii-guard.spec.ts` 的判据是「整个 `src/**`
#: 逐行包含黑名单整条」⇒ 原样写进来**必红**（且 worktree 里因为没有 `.tools/` 而**静默 skip**、看起来是绿的，属假绿）。按 `AGENTS.md` §4「不跨越租户边界 / 真值不进 `src/**`」的硬规则，**它不进代码**；它是**某个租户的客服话术**、不是通用规则，正确落点是**租户级配置**（与 `ProjectTagDict` 同一条路：默认值在代码、租户只存覆盖）。引擎已经收 `rules=` 形参（`clean_order_text(text, rules=…)`），调用方叠上租户那一份即可。
NOISE_PHRASES: Tuple[str, ...] = (
    "群码在订单截图",
    # ⛔ 此处原有一条含对标租户品牌名的模板噪音词，见上方说明——有意不进代码。
    "拉裙",
    "拉群找",
    "拉群",
    "上号截图",
    "留红留卡",
    "打手包损耗",
    "单子不许超时",
    "奖励归老板",
    "禁止打手卡",
    "禁止卡保底",
    "阶段任务均默认打够",
    "包宝箱前置",
    "具体内容看图",
    "进度看截图",
    "尽力打",
    "不包全图鉴",
    "含通行证",
    "不包前置任务不包主线",
    "不包主线",
    "官服",
    "包损",
    "不动老板",
    "包每日",
    "包日常",
)

# ── 归一 ────────────────────────────────────────────────────────────────────

#: 订单号这类**长数字串**的判定下界：`>= 15` 位就整串剔掉（`Order.order_no` 是 20 位）。
#: ⚠️ 取「15 位以上」＝含 15。下界定在 15 是因为项目正文里的版本号 / 数量都在 5 位以内，
#: 15 位往上只可能是订单号 / 流水号这类**标识**而不是业务内容。
ORDER_NO_MIN_DIGITS = 15

# ── 口径常量（Task 3 / Task 4 / Task 5 读这里，⛔ 不要各自再写一份）────────────

#: 项目类别（设计稿 §2.2）。⚠️ 取值照抄设计稿原文，⛔ 不加「其他」以外的兜底。
PROJECT_TAG_CATEGORIES: Tuple[str, ...] = (
    "包月托管",
    "地图收集",
    "任务代肝",
    "活动代肝",
    "段位代练",
    "材料代肝",
    "其他",
)

#: 上面那组类别的 `choices` 形状。⚠️ **值与 label 是同一个中文串**：
#: 类别的唯一出处是真实数据（`.tools/recon/merged.json` 的 `cat` 列），
#: 库层存的就是这几个中文串 ⇒ ⛔ 不要为了"整齐"另造一套英文码再翻译一次（那会多出一张映射表，而这张表没有任何消费方）。
PROJECT_TAG_CATEGORY_CHOICES: Tuple[Tuple[str, str], ...] = tuple(
    (name, name) for name in PROJECT_TAG_CATEGORIES
)

#: `ProjectTagStat.window_days` 的五档时间窗（业务方 定，设计稿 §8.1 ②）。
#: ⚠️ **不是单一默认值**——是用户可切的五档，每档一行统计。
STAT_WINDOW_DAYS: Tuple[int, ...] = (30, 60, 90, 180, 360)

#: 上面五档的 `choices` 形状（`(值, 中文 label)`）。⚠️ 只是**文档性**的：
#: choices 在库层**不构成约束**，写入侧仍要自己只传这五档（Task 4）。
STAT_WINDOW_CHOICES: Tuple[Tuple[int, str], ...] = tuple(
    (days, f"{days}天") for days in STAT_WINDOW_DAYS
)

#: 最小样本量阈值（业务方 定，设计稿 §8.1 ③）：**低于它就当没有**，
#: 写入侧不落行、读取侧显示「样本不足」。⛔ 照它报价比不给更糟（实测很多项目只有 1~2 单）。
MIN_SAMPLE_COUNT = 3


@dataclass(frozen=True)
class CleaningRules:
    """一套清洗规则。默认值 = 本模块的常量（单一出处）。收成 dataclass 而不是散着的模块全局，是为了**租户级覆盖**：
    调用方（定时任务 / 管理命令）可以 `dataclasses.replace(DEFAULT_CLEANING_RULES,
    noise_phrases=…)` 叠上租户自己的词表再传给引擎，引擎本身不动。"""

    noise_phrases: Tuple[str, ...] = NOISE_PHRASES
    brackets: Tuple[Tuple[str, str], ...] = PAIRED_BRACKETS
    delimiters: Tuple[str, ...] = SEGMENT_DELIMITERS
    segment_join: str = SEGMENT_JOIN
    order_no_min_digits: int = ORDER_NO_MIN_DIGITS


#: 默认规则。保留这个名字，调用方不必自己拼。
DEFAULT_CLEANING_RULES = CleaningRules()


def bracket_pattern(open_ch: str, close_ch: str):
    """把一对括号编成「取括号内内容」的模式。 ⛔ 不用 `.*` 而用否定字符类：`【A】【B】` 必须切成两段而不是一段
    （贪婪会从第一个 `【` 吃到最后一个 `】`）。"""
    inner = "[^" + re.escape(open_ch) + re.escape(close_ch) + "]*"
    return re.compile(re.escape(open_ch) + "(" + inner + ")" + re.escape(close_ch))


def delimiter_pattern(rules: CleaningRules = DEFAULT_CLEANING_RULES):
    """把分隔符列表编成一个字符类。"""
    return re.compile("[" + "".join(re.escape(d) for d in rules.delimiters) + "]")


def whitespace_pattern():
    """空白 ＋ 全角空格。 ⚠️ `\\u3000`（全角空格）本就在 Python `re` 的 `\\s` 里（str 模式是 Unicode 语义），
    这里显式写出来是**为了可读**、也为了以后有人把模式改成 `re.ASCII` 时不会静默失效。"""
    return re.compile(r"[\s\u3000]+")


def long_digits_pattern(rules: CleaningRules = DEFAULT_CLEANING_RULES):
    """订单号这类长数字串。"""
    return re.compile(r"\d{%d,}" % rules.order_no_min_digits)
