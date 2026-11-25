"""对标物「新增系统子账户」三个枚举（spec §3.2，取值均为**实测**）。⚠️ 与 `business.roles` 的「职位」不是一回事：职位是 Group（`userRole`），这里是人物属性。
"""
from django.db import models


class BusinessType(models.IntegerChoices):
    """业务类型（对标物 `bussType`）。"""

    BOTH = 0, "代练+陪玩"
    BOOSTING = 1, "专做代练"
    COMPANION = 2, "专做陪玩"


class MemberType(models.IntegerChoices):
    """成员类型（对标物 `userType`）。⚠️ 对标物自己的 tooltip 原文是「该选项并无实际作用」——照抄字段，但**不接任何行为**。
    """

    INTERNAL = 0, "内部成员"
    PARTNER = 1, "合作用户"


class CommissionMode(models.IntegerChoices):
    """提成模式（对标物 `autoBrokerageType`）。"""

    FIXED = 10, "固定金额"
    ORDER_PERCENT = 20, "订单价百分比"
    PROFIT_PERCENT = 30, "利润的百分比"


class AccountSource(models.TextChoices):
    """**账号来源**：这个账号是从哪条路被创建进本租户的（D-0277）。⚠️ 它既不是「职位」（Group）也不是「成员类型」（内部/合作）：只回答一件事——**是不是打手 App 自己注册的**。今天唯一的消费方是网页控制台登录口（`apps/core/auth_views.py::LoginView`）：`APP` ⇒ 禁止登录网页端、请用 App 登录；其余取值一律放行（业务方口径：「网页端/后台建的账号不受限制」）；⛔ 不要顺手把 `LINK` / `IMPORT` 也拦掉——那是另一档业务判断，没有拍过板。
    - `CONSOLE`：网页端 / 后台新增（`accounts/views.py::_persist_account`），**也是默认值**——存量账号与「没有档案行」的账号（平台管理员、`seed_tenant` 的 `admin`、`seed_e2e` 的开发账号）都按它算 ⇒ 改动前能登网页端的人，改动后照样能登；`APP`：打手 App 开放注册（`POST /api/app/auth/register/`）；`LINK`：链接自助注册（人事发的一次性注册链接，`accounts/selfreg_views.py::SelfRegisterPublicView`；开户人是发链接的人事，不是本人）；`IMPORT`：**预留给导入**——⚠️ 如实登记：今天**没有任何写入点**写这个值，列出来是为了取值表完整、将来接导入时不必再改列定义，⛔ 别因为「表里没有 import」就去删它。
    """

    CONSOLE = "console", "网页端/后台新增"
    APP = "app", "打手 App 注册"
    LINK = "link", "链接自助注册"
    IMPORT = "import", "导入"
