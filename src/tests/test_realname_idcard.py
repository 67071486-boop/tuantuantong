"""`apps/realname/idcard.py`（身份证号本地解析）的判据。

## 夹具一律合成值（`AGENTS.md` §5-C / E-0077）

⛔ 本文件里**没有任何真实身份证号**：下面每一个号都是按 GB 11643 的算法**自己算出来**的。

构造方法（要复算只需一张纸）：

1. 前 17 位挑成「6 位地区码 `110101` + 8 位生日 + 3 位顺序码」；
2. 每位乘权重 `W = [7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2]` 后**求和**；
3. 和**对 11 取模**，余数查表 `T = "10X98765432"`
   （余数 0→`1`、1→`0`、2→`X`、3→`9`、4→`8`、5→`7`、6→`6`、7→`5`、8→`4`、9→`3`、10→`2`）
   ⇒ 第 18 位校验位。

本文件用到的每个号（下表「加权和 / 余数 / 校验位」就是上面那三步的中间读数）：

| 常量 | 前 17 位 | 加权和 | 余数 | 校验位 | 完整号 | 生日 | 第 17 位 |
|---|---|---|---|---|---|---|---|
| `MALE_ID` | `11010119900307015` | 166 | 1 | `0` | `110101199003070150` | 19900307 | `5` 奇⇒男 |
| `FEMALE_ID` | `11010119950812024` | 213 | 4 | `8` | `110101199508120248` | 19950812 | `4` 偶⇒女 |
| `CHECK_X_ID` | `11010119900101039` | 134 | 2 | `X` | `11010119900101039X` | 19900101 | `9` 奇⇒男 |
| `LEAP_DAY_ID` | `11010120000229011` | 118 | 8 | `4` | `110101200002290114` | 20000229 | `1` 奇⇒男 |
| `NOT_LEAP_ID` | `11010119000229011` | 125 | 4 | `8` | `110101190002290118` | 19000229 | `1` 奇⇒男 |
| `BAD_DATE_ID` | `11010120260230015` | 121 | 0 | `1` | `110101202602300151` | 20260230 | `5` 奇⇒男 |
| `FUTURE_ID` | `11010120991231011` | 176 | 0 | `1` | `110101209912310111` | 20991231 | `1` 奇⇒男 |
| `BORN_TODAY_ID` | `11010120261010015` | 90 | 2 | `X` | `11010120261010015X` | 20261010 | `5` 奇⇒男 |

⚠️ 后四个（`NOT_LEAP_ID` / `BAD_DATE_ID` / `FUTURE_ID` / `BORN_TODAY_ID`）**校验位是算对的**，
故意的：它们要单独把「出生日期」与「不能是未来」两条判据钉出来，
校验位留对才排除掉「其实是校验位把它拦下来的」这条竞争解释。

## 判据风格（`AGENTS.md` §5-E / E-0021）

边界一律用 `assertIsNone` / `assertIsNotNone`（都是 `is` 判据），⛔ **不用真值**
（`assertFalse(info)`、`assertTrue(info)`）——`age` 合法值为 `0`（今天出生），
用真值判断会把「合法但为 0」误当成「解析失败」，而正面用例全绿、只有边界用例能抓到。
"""
from datetime import date
from unittest import TestCase

from apps.realname.idcard import IdCardInfo, parse

# —— 合成号（构造过程见模块 docstring 的表）——
MALE_ID = "110101199003070150"  # 生日 19900307，第 17 位 5（奇）⇒ 男
FEMALE_ID = "110101199508120248"  # 生日 19950812，第 17 位 4（偶）⇒ 女
CHECK_X_ID = "11010119900101039X"  # 校验位是 X（用来测末位小写 x），生日 19900101 ⇒ 男
LEAP_DAY_ID = "110101200002290114"  # 生日 20000229（2000 是闰年）⇒ 合法
NOT_LEAP_ID = "110101190002290118"  # 生日 19000229（1900 不是闰年）⇒ 非法
BAD_DATE_ID = "110101202602300151"  # 生日 20260230（2 月没有 30 号）⇒ 非法
FUTURE_ID = "110101209912310111"  # 生日 20991231（未来）⇒ 非法
BORN_TODAY_ID = "11010120261010015X"  # 生日 = 判断用的「今天」⇒ age 必须是 0

# 断言用的是固定的「今天」，⛔ 不读系统时钟——否则这些用例会随日历漂移。
TODAY = date(2026, 10, 10)


class ValidIdCardTests(TestCase):
    """合法号：三项（性别 / 生日 / 周岁）都要对。"""

    def test_male_id_card_parses_gender_birthday_and_age(self):
        info = parse(MALE_ID, today=date(2026, 3, 7))

        self.assertIsNotNone(info, "合法的 18 位号码不应被判非法")
        assert info is not None
        self.assertIsInstance(info, IdCardInfo)
        self.assertEqual(info.gender, "男")
        self.assertEqual(info.birthday, "19900307")
        self.assertEqual(info.age, 36)  # 2026-03-07 生日当天 ⇒ 已满 36 岁

    def test_female_id_card_parses_gender_birthday_and_age(self):
        info = parse(FEMALE_ID, today=TODAY)

        self.assertIsNotNone(info, "合法的 18 位号码不应被判非法")
        assert info is not None
        self.assertEqual(info.gender, "女")
        self.assertEqual(info.birthday, "19950812")
        self.assertEqual(info.age, 31)  # 生日 8/12 已过 ⇒ 31 岁

    def test_lowercase_x_check_digit_is_accepted(self):
        """末位 `x` 小写必须与 `X` 等价（同样的号只改大小写）。"""
        upper = parse(CHECK_X_ID, today=TODAY)
        lower = parse(CHECK_X_ID[:-1] + "x", today=TODAY)

        self.assertIsNotNone(upper, "大写 X 的合法号不该被判非法（对照）")
        self.assertIsNotNone(lower, "末位小写 x 是合法写法，不该被判非法")
        assert lower is not None
        self.assertEqual(lower, upper, "同一个号的大小写两种写法必须解析出同样的结果")
        self.assertEqual(lower.gender, "男")
        self.assertEqual(lower.birthday, "19900101")
        self.assertEqual(lower.age, 36)

    def test_born_today_has_age_zero_and_is_not_none(self):
        """边界：今天出生 ⇒ `age == 0`，而 `parse` **不是** `None`（§5-E / E-0021）。

        这条是「用真值判成败」的照妖镜：把 `parse` 的结果或 `age` 当真值判断时，
        `age == 0` 会被当成「解析失败」，只有这条用例会红。
        """
        info = parse(BORN_TODAY_ID, today=TODAY)

        self.assertIsNotNone(info, "今天出生的号码是合法号，不该被判非法")
        assert info is not None
        self.assertEqual(info.age, 0)
        self.assertEqual(info.birthday, "20261010")
        self.assertEqual(info.gender, "男")


class CheckDigitTests(TestCase):
    """校验位：GB 11643 加权和 + 取模查表。"""

    def test_wrong_last_digit_returns_none(self):
        """把合法号的**最后一位**改掉 ⇒ 校验位必然不符（查表是双射）⇒ `None`。"""
        broken = MALE_ID[:-1] + ("1" if MALE_ID[-1] != "1" else "2")

        self.assertNotEqual(broken, MALE_ID, "变异自证：改完必须与原号不同")
        self.assertEqual(len(broken), 18, "变异自证：长度没被改坏")
        self.assertIsNone(parse(broken, today=TODAY))

    def test_wrong_middle_digit_returns_none(self):
        """改中间一位（生日 1990-03-07 → 1990-05-07）⇒ 加权和变了、校验位对不上 ⇒ `None`。

        ⚠️ 这里**只改中间一位、末位保持原样的 `0`**：该 17 位前辍的正确校验位是 `4`，
        所以它落在校验位这条判据上（不是被日期判据拦下的）。
        """
        broken = "11010119900507015" + MALE_ID[-1]

        self.assertIsNone(parse(broken, today=TODAY))


class LengthAndTypeTests(TestCase):
    """长度 / 类型：17 位 / 19 位 / 15 位旧号 / 空串 / `None`。"""

    def test_17_digits_returns_none(self):
        # 合法号砍掉末位：其余 17 位原样，用来证明拦住它的是**长度**而不是校验位。
        self.assertIsNone(parse(MALE_ID[:17], today=TODAY))

    def test_19_digits_returns_none(self):
        self.assertIsNone(parse(MALE_ID + "0", today=TODAY))

    def test_15_digit_old_format_returns_none(self):
        """⛔ 15 位旧号**不支持**，直接判非法（不是「兼容解析」）。"""
        self.assertIsNone(parse("110101900307015", today=TODAY))

    def test_empty_string_returns_none(self):
        self.assertIsNone(parse("", today=TODAY))

    def test_none_returns_none(self):
        """`None` 不能抛 `TypeError`——非法输入一律 `None`（调用方不当异常处理）。"""
        self.assertIsNone(parse(None, today=TODAY))  # type: ignore[arg-type]


class BirthDateTests(TestCase):
    """出生日期合法性（含闰年）与「不能是未来」。"""

    def test_invalid_birth_date_returns_none(self):
        """`20260230`：2 月没有 30 号 ⇒ `None`（该号校验位是算对的，见模块 docstring）。"""
        self.assertIsNone(parse(BAD_DATE_ID, today=TODAY))

    def test_leap_day_20000229_is_valid(self):
        """2000 是闰年 ⇒ `20000229` 合法。"""
        info = parse(LEAP_DAY_ID, today=TODAY)

        self.assertIsNotNone(info, "2000 是闰年，20000229 必须被接受")
        assert info is not None
        self.assertEqual(info.birthday, "20000229")
        self.assertEqual(info.age, 26)

    def test_1900_is_not_a_leap_year_so_19000229_is_invalid(self):
        """⚠️ 1900 不是闰年（百年不闰、400 年才闰）⇒ `19000229` 必须 `None`。

        这条很容易被「能被 4 整除就是闰年」的写法放过 ⇒ 它是闰年判据的照妖镜。
        """
        self.assertIsNone(parse(NOT_LEAP_ID, today=TODAY))

    def test_future_birth_date_returns_none(self):
        """生日不能在未来（相对传入的「今天」）。"""
        self.assertIsNone(parse(FUTURE_ID, today=TODAY))


class AgeBoundaryTests(TestCase):
    """周岁边界：生日当天 = 已满岁；前一天 = 少一岁。"""

    def test_age_on_birthday_is_full_years(self):
        info = parse(MALE_ID, today=date(2026, 3, 7))  # 正好生日当天

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.age, 36)

    def test_age_on_day_before_birthday_is_one_less(self):
        info = parse(MALE_ID, today=date(2026, 3, 6))  # 生日前一天

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.age, 35)
