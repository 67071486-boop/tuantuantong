"""权限点的三种形态（spec §4.2 / D4）。

⚠️ **纯常量模块**：不 import 任何模型，也不反向 import `apps.core`——它被模型、
序列化器、判据与业务代码共同引用，一旦这里 import 了模型就会出现循环。
"""


class NodeKind:
    """用普通类而不是 `TextChoices`：本模块要能被**不依赖 Django** 的地方引用。"""

    PAGE = "page"
    API = "api"
    FLAG = "flag"

    CHOICES = (
        (PAGE, "页面"),
        (API, "接口"),
        (FLAG, "语义开关"),
    )
