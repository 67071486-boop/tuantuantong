"""身份证号本地解析（GB 11643 二代身份证，18 位）。

**为什么必须本地算**：厂商的「姓名 + 身份证号」二要素核验接口只回 `result` / `message`，
**不返回性别与生日**（实测，见 `docs/specs/2026-10-10-realname-verification-design.md` §4.2
的 `gender` / `birthday` 两列与 §5 的 ③）；而实名列表要显示「性别」「年龄」两列
⇒ 性别、生日、周岁只能从号码本身算出来。

**判据边界**：任何非法输入一律返回 `None`，⛔ **不抛异常**——调用方（核验端点）拿它当
「本地格式校验」用，非法 ⇒ 走业务错误码，不该变成 500。
⛔ **不支持 15 位旧号**（直接判非法）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# 前 17 位的加权因子（GB 11643-1999 附录 A）
_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
# 加权和对 11 取模后的余数 ⇒ 校验位（余数 0 查 '1'、1 查 '0'、2 查 'X' ……）
_CHECK_CODES = "10X98765432"

# 恰好 18 位：前 17 位数字，第 18 位数字或 X/x。
# ⚠️ 用显式 `[0-9]` 而不是 `\d`：Python 的 `\d` 与 `str.isdigit()` 都认非 ASCII 数字
# （如 `'٣'.isdigit()` 为 True、`'²'.isdigit()` 也为 True 但 `int('²')` 直接抛），
# 那会让「非法返回 None」出现漏网与 500 两条路。
_PATTERN = re.compile(r"[0-9]{17}[0-9Xx]")

_BIRTHDAY_SLICE = slice(6, 14)  # 第 7–14 位 = YYYYMMDD
_GENDER_INDEX = 16  # 第 17 位（倒数第 2 位），奇数为男、偶数为女


@dataclass(frozen=True)
class IdCardInfo:
    """一个合法身份证号解析出来的三件事。"""

    gender: str  # "男" / "女"
    birthday: str  # "YYYYMMDD"
    age: int  # 按传入的「今天」算出的周岁


def parse(id_card: str, *, today: date | None = None) -> IdCardInfo | None:
    """解析 18 位二代身份证号。

    非法（长度不对 / 字符不对 / 校验位不对 / 出生日期不是合法日期 / 出生日期在未来）
    一律返回 `None`，⛔ 不抛异常。

    `today` 只用于算周岁与「生日不能在未来」这条，为 `None` 时取 `date.today()`；
    它是**测试注入点**（年龄边界靠它才能被钉住，⛔ 不靠「跑测试当天的日期」巧合）。
    """
    if not isinstance(id_card, str) or _PATTERN.fullmatch(id_card) is None:
        return None

    body = id_card[:17]
    # 校验位：前 17 位加权求和 ⇒ 对 11 取模 ⇒ 查表；末位小写 `x` 按大写比对。
    total = sum(int(char) * weight for char, weight in zip(body, _WEIGHTS))
    if _CHECK_CODES[total % 11] != id_card[17].upper():
        return None

    birthday_text = id_card[_BIRTHDAY_SLICE]
    try:
        birthday = date(
            int(birthday_text[0:4]), int(birthday_text[4:6]), int(birthday_text[6:8])
        )
    except ValueError:
        # 含闰年判据：`date(2000, 2, 29)` 成立、`date(1900, 2, 29)` 抛（1900 不是闰年）。
        return None

    if today is None:
        today = date.today()
    if birthday > today:
        return None

    gender = "男" if int(id_card[_GENDER_INDEX]) % 2 == 1 else "女"
    # 周岁：当年生日还没到就减 1（`(月, 日)` 元组比较等价于「生日已过或正好今天」）。
    age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
    return IdCardInfo(gender=gender, birthday=birthday_text, age=age)
