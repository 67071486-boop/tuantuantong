"""给「人看的」时间列一个**唯一的时区换算出口**（E-0238 家族收口）。**为什么需要它（实测根因）**：`USE_TZ = True` 下 Django 从库里读出的 `DateTimeField` 是 **UTC** 的 aware datetime ⇒ 裸 `.isoformat()` 出的是 `…+00:00` 串，本仓的 ISO 时间串因此有**两族口径**（走 DRF `DateTimeField` 的端点按**项目时区** `Asia/Shanghai` 序列化 ⇒ `+08:00`；模型直读 `.isoformat()` 的端点是 UTC ⇒ `+00:00`）。桌面前端的助手有的按「串已是 +08:00」裸切片（`feedback.ts::formatTime`）、有的按偏移换算（`order-orderview.ts::formatLogTime`）、有的按浏览器时区渲染（`order-screenshots-manage.ts::formatShotTime`）——同一份事实三种口径，反馈列表的提交时间因此**整整差 8 小时**（业务方报过）。
⚠️ **消费方事实（D-0302）**：打手 App（`app/utils/format.uts`）与 H5 有**按串截断直显**的消费方（截 `MM-DD HH:mm` 当地墙钟用、不做时区换算）——自有端点的手拼时刻一旦回退成 UTC 串，这些页面立刻整体差 8 小时；管理端网页同样有大量「切字符」格式化器依赖同一条口径 ⇒ 自有三端消费的端点，手拼时刻一律走本模块。口径：**显示端点一律在后端行拼装处换算到项目时区**（与 DRF `DateTimeField` 同一条出口语义，`rest_framework/fields.py::DateTimeField.default_timezone` = `timezone.get_current_timezone()`），调用方拿到的永远是 `+08:00` 串、前端无论切片还是换算都对；⛔ **机读契约不进这里**（对外开放平台 / takerapi / peer 同步的 `isoformat` 是外部约定，动它们要先问业务方，见该函数 docstring）。
"""


from django.utils import timezone


def local_iso(value):
    """aware datetime → **项目时区**（`Asia/Shanghai`）的 ISO-8601 串；`None` 原样返回。

    ⚠️ 只收 aware（`USE_TZ=True` 下模型列出来的都是 aware）；naive 会 `ValueError` 响亮——
    那是调用方拿错了值，不静默。
    """
    if value is None:
        return None
    return timezone.localtime(value).isoformat()
