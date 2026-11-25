"""录单校验。 **必填只有两项：游戏、代练/陪玩项目**（spec §5.2）。订单总价与客户联系方式**都不是必填**，因此订单可能在无价格、无联系方式的状态下存在—— 下游的列表、详情、报表都要容忍这种数据。
⚠️ **起，上面「订单总价不是必填」只对接口层成立**：接口层口径不变（`OrderSerializer.total_price` 仍 `required=False`，模型 `null=False default=Decimal("0.00")`
⇒ 请求省略该字段仍落 `0.00`）；但**录单页面**新增了「订单总价必填、默认 0」这道闸（见 `docs/DECISIONS.md` **D-0082**）。本 docstring 描述的是**接口层**，仍然成立。"""
import re

from apps.core.error_codes import ErrorCode

# 保持这个名字可被 import（不改签名），但取值只有一个来源。
ERROR_VALIDATION = int(ErrorCode.VALIDATION)

_WS = re.compile(r"[ \t\u3000]+")


class ValidationProblem(Exception):
    """业务校验失败。携带 `code` 供 API 层转成统一信封的业务错误码。"""

    def __init__(self, message, code=ERROR_VALIDATION):
        super().__init__(message)
        self.code = code


def normalize_project_text(text):
    """去首尾空白 + 把内部连续的空格/全角空格压成单个半角空格。
    只做最小归一：**不试图做同义词合并**——项目是自由文本（D9），
    过度归一反而会让客服看到的和库里存的不一致。"""
    if not text:
        return ""
    return _WS.sub(" ", text.strip())


def validate_required(*, game, project_text):
    """必填只有游戏与项目（D9：项目可以只填文本、不选 FK）。
    ⚠️ 这是**接口层**口径（起，录单**页面**另有「订单总价必填、默认 0」，
    见 `docs/DECISIONS.md` D-0082）。"""
    if game is None:
        raise ValidationProblem("游戏是必填项")
    if not normalize_project_text(project_text):
        raise ValidationProblem("代练/陪玩项目是必填项")


def validate_order_chain(*, game, server=None, category=None, project=None):
    """订单引用的区服、分类和常用项目都必须属于该游戏。"""
    if game is None:
        raise ValidationProblem("游戏是必填项")
    if server is not None and server.game_id != game.pk:
        raise ValidationProblem("所选区服不属于该游戏")
    if category is not None and category.game_id != game.pk:
        raise ValidationProblem("所选项目分类不属于该游戏")
    if project is not None:
        if category is None:
            raise ValidationProblem("选了项目就必须选项目分类")
        if project.category_id != category.pk and project.category.game_id != game.pk:
            raise ValidationProblem("所选项目不属于该游戏")
