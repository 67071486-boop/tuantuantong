"""录单识别设置的**读取层**（spec §5 / §6）。 ⚠️ `intake_rules.py` 保持零 Django 依赖（它是纯数据 + 纯函数，引擎与页面共用）；
DB 读取一律收在本模块，读出来作为普通 dict 交给 `build_rules`。 ⚠️ **租户边界**（`AGENTS.md` §4）：这里所有查询都在**当前连接 / 租户 schema** 上跑，
**不接受任何 tenant 参数**——租户上下文只能来自认证态 / 域名，绝不从请求参数传入。"""
from apps.business.intake_rules import RECOGNITION_ITEMS, build_rules
from apps.business.models import RecognitionKeyword


def recognition_overrides():
    """`item_code -> keywords` 的覆盖表（**只含被改过的行**）。"""
    return dict(RecognitionKeyword.objects.values_list("item_code", "keywords"))


def recognition_rows():
    """页面要的 14 行：**默认值 ∪ 覆盖值**（空串是合法覆盖，不是"没改"⇒ 判据用 `in`）。"""
    overrides = recognition_overrides()
    return [
        {
            "item_code": item.code,
            "name": item.name,
            "keywords": overrides[item.code]
            if item.code in overrides
            else ",".join(item.keywords),
        }
        for item in RECOGNITION_ITEMS
    ]


def rules_for_tenant():
    """当前租户的解析规则（解析端点用它喂引擎）。"""
    return build_rules(recognition_overrides())
