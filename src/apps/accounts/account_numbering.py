"""账号编号（显示名）的生成规则。自助注册线与 App 开放注册线共用这一个模块，⛔ 不让两条注册路各写一份「取配置 → 算下一个号」的算法（那必然漂：一条补零、一条不补零）。
⚠️ **必须在注册事务内调用**：`generate_display_name` 对配置行 `select_for_update()`（行锁），同租户的并发注册在这里**排队**，编号才不会重号。
"""
from django.contrib.auth import get_user_model

from apps.accounts.models import SelfRegistrationConfig

User = get_user_model()


def generate_display_name(fallback: str) -> str:
    """算一次注册的**显示名**：配置了编号给编号，否则回 `fallback`。
    配置了编号（`number_prefix` 与 `number_start` **都**非空，E-0021：显式判空串 / `is not None`，⛔ 不用真值）⇒ `number = max(number_start, 同前缀现有最大号 + 1)`，显示名 = `f"{prefix}{number}"`（数字不补零——sq20001 就是 5 位，起始号几位就是几位）。同前缀现有号**扫库**得出（`first_name__startswith=prefix` + Python 侧过滤数字尾巴），「显示名被人手改成 sq00009」这种旁路写入也**自愈**（下一号从 10 之后接着走，不重号）。未配置 / 配置行不存在 ⇒ `fallback`（自助注册线是支付宝姓名；App 注册线传空串，显示名继续按 `display.user_display_name` 回落成登录账号）。
    """
    config = SelfRegistrationConfig.objects.select_for_update().filter(pk=1).first()
    if config is None:
        return fallback
    prefix = config.number_prefix or ""
    start = config.number_start
    if prefix == "" or start is None:
        # 两列都非空才算配置了编号（spec §2.1：只填一列 = 按未配置处理）。
        return fallback
    return f"{prefix}{max(start, max_existing_number(prefix) + 1)}"


def max_existing_number(prefix: str) -> int:
    """同前缀（`first_name` 以 `prefix` 开头）现有账号里的最大编号；没有 ⇒ 0。
    ⚠️ 尾巴不是纯数字的行（人手把显示名改成 `sq-备用` 之类）**跳过**，不炸：编号自愈语义只认数字尾巴；租户表量级小（spec §2.1 的取舍：扫描即可，⛔ 不为它建表达式索引）。⚠️ 尾巴**超过 8 位**的行同样跳过（现场实测，E-0285）：有人把显示名手改成 `sq` + 14 位数字（误贴的外部单号 / 账号），`int()` 之后把「下一个号」抬到万亿级；正常编号（起始号 + 递增）到不了那个量级 ⇒ 超长尾巴只可能是误贴的外部标识，不是这张表要自愈的对象。
    """
    max_no = 0
    rows = User.objects.filter(first_name__startswith=prefix).values_list(
        "first_name", flat=True
    )
    for name in rows:
        digits = name[len(prefix):]
        if not digits.isdigit() or len(digits) > 8:
            continue
        max_no = max(max_no, int(digits))
    return max_no
