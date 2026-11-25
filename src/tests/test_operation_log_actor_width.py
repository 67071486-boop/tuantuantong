"""Task M2：`OperationLog.actor` 必须装得下**完整的**认证态用户名（列宽 150）。

## 这条用例守的是什么（实测事实，不是推测）

`User.username` 的列宽是 **150**（Django 5.2 的 `AbstractUser`；本仓没有自定义用户模型），
而 `OperationLog.actor` 建表时是 **64**。`apps/audit/log.py::record()` 的 `_clip`
**按列宽截断**请求/业务来源的文本（模块 docstring 的防御性取向 ③）⇒ 走唯一写入出口时，
65–150 字的认证态用户名**不会 500，而是被静默截断**：日志行里留下的是
「另一个人的名字前缀」，`GET /api/audit/logs/?actor=<真名>` 也永远查不到这一行。

⚠️ **不要**把这条用例弱化成「不报错就行」：截断与完整**必须**由同一个断言分辨
（长名末尾带哨兵 `TAIL`，截到 64 时它必然消失）。`main` 已为同一类宽度不符付过一次代价
（`Order.updated_by` / `AuditLog.actor` 64 → 150），本文件是它在 `OperationLog` 上的回归网。

## 判据纪律

- 期望值写**字面值**（E-0024）：直接与测试自己造的那个用户名比对，不引生产常量。
- 断言打在**数据库行 / 响应体**上（E-0009），不打在中间层。
- **方向配对**：长名与普通名各一条；普通名那条是阳性对照——万一整个写路径坏了，
  只测长名的那条会以「actor 是空串」的形式红，归因会指向错的方向。
"""
from django_tenants.test.cases import TenantTestCase

from apps.audit.log import record
from apps.audit.models import LogType, OperationLog
from tests.p5_auth_helpers import make_logged_in_client

EVENTS_ENDPOINT = "/api/audit/events/"
LOGS_ENDPOINT = "/api/audit/logs/"

#: 20 位订单号（`Order.order_no` 与 `OperationLog.order_no` 的列宽都是 20）。
ORDER_NO = "20260922000000000201"

#: 70 字用户名（>64 且 ≤150）。末尾 `TAIL` 是哨兵：截断到 64 时它必然消失。
LONG_USERNAME = "m2-" + "u" * 63 + "TAIL"
assert len(LONG_USERNAME) == 70 and LONG_USERNAME.endswith("TAIL")

#: 阳性对照用的普通用户名（<64）。
NORMAL_USERNAME = "m2-normal-user"


class ActorWidthTests(TenantTestCase):
    """认证态用户名写进 `OperationLog.actor`：**完整**落库（方向配对）。"""

    def _report_and_get_row(self, *, username):
        """以真实登录态上报一条读操作，返回落库的那一行。

        走 `POST /api/audit/events/` 而不是直接调 `record()`：这条路径的 `actor`
        取自 `request.user.get_username()`（`apps/audit/api/views.py`），
        正是「认证态用户名进这些列」的真实形状。
        """
        client = make_logged_in_client(username=username)
        response = client.post(
            EVENTS_ENDPOINT,
            {"action": "order.open_edit", "order_no": ORDER_NO},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        return OperationLog.objects.get()

    def test_long_username_is_stored_in_full(self):
        row = self._report_and_get_row(username=LONG_USERNAME)

        # 字面值判据（E-0024）：等于测试自己造的那个 70 字用户名，一个字都不能少。
        self.assertEqual(len(row.actor), 70)
        self.assertEqual(row.actor, LONG_USERNAME)

    def test_normal_username_is_stored_in_full(self):
        """阳性对照：同一判据在普通用户名上成立（证明写路径本身是通的）。"""
        row = self._report_and_get_row(username=NORMAL_USERNAME)

        self.assertEqual(row.actor, NORMAL_USERNAME)

    def test_actor_filter_accepts_a_username_longer_than_64(self):
        """读侧同一条宽度：`GET /api/audit/logs/?actor=` 必须能按长名筛选。

        `/api/audit/logs/` 的查询参数里原先写死 `max_length=64`（比列宽早写死的一处副本）
        ⇒ 长名会被当场拒成 `code=1000`，用户「按操作人查」查不到自己刚写的行。
        """
        client = make_logged_in_client(username=LONG_USERNAME, is_superuser=True)
        reported = client.post(
            EVENTS_ENDPOINT,
            {"action": "order.open_edit", "order_no": ORDER_NO},
            content_type="application/json",
        )
        self.assertEqual(reported.status_code, 200, reported.content[:300])
        self.assertEqual(reported.json()["code"], 0, reported.json())

        response = client.get(LOGS_ENDPOINT, {"actor": LONG_USERNAME})

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body["code"], 0, body)
        self.assertEqual(body["data"]["total"], 1, body)
        self.assertEqual(body["data"]["items"][0]["actor"], LONG_USERNAME)


#: 100 字项目名（>64；真实来源 `Project.name` 可到 450）。末尾 `TAIL` 同上作哨兵。
LONG_TARGET_NAME = "项目" + "名" * 94 + "TAIL"
assert len(LONG_TARGET_NAME) == 100 and LONG_TARGET_NAME.endswith("TAIL")


class TargetNameWidthTests(TenantTestCase):
    """第二个「比来源窄」的列（M2 扫描的发现）：`target_name` 也要装得下完整名字。

    这一列按 R11 装「句子里的名字」，最宽来源是 `Project.name`（**450**，
    配置域 `ConfigAuditMixin` 把实例名整串写进它）；用户名（150）是第二个来源。
    原写 64 ⇒ `record()` 的 `_clip` 同样静默截断。这里用生产写出口 `record()` 直接落一行，
    断言**数据库行**里的名字一字不少。
    """

    def test_long_target_name_is_stored_in_full(self):
        created = record(
            log_type=LogType.SYSTEM,
            action="project.create",
            actor=NORMAL_USERNAME,
            # ⭐ 本用例只测 `target_name` 的宽度、没有认证态用户
            # ⇒ 显式声明机器来源（`record()` 对「没有操作人」fail-closed）。
            system=True,
            target_kind="project",
            target_id=1,
            target_name=LONG_TARGET_NAME,
        )

        row = OperationLog.objects.get(pk=created.pk)  # 判据打在**数据库行**上（E-0009）
        self.assertEqual(len(row.target_name), 100)
        self.assertEqual(row.target_name, LONG_TARGET_NAME)
