"""订单截图的四个端点（spec §7 接口表 / §10 租户隔离判据）。 | 方法 | 路径 | 节点 | |---|---|---| | POST | `/api/business/orders/{order_no}/screenshots/ticket` | `order.upload_screenshot` | | POST | `/api/business/orders/{order_no}/screenshots` | `order.upload_screenshot` | | GET | `/api/business/orders/{order_no}/screenshots` | `order.view_screenshot` | | DELETE | `/api/business/orders/screenshots/{pk}` | `order.screenshot_manage` | **为什么每类操作一个视图**：本线的权限是**按动作分节点**的（签发/认领一个、看一个、删一个），而 `refund_views` 那种「一个视图两个方法共用一个节点」的形状在这里不成立——硬凑会让「谁能传」「谁能看」「谁能删」三件事在同一个 `get_permissions` 里分叉。 ⚠️ **节点 code 一律写成模块常量**：接线位点表（`apps/rbac/wiring.py`）的 T2 判据会解析锚点方法体、并向下解析**一层模块常量**来找 code（同款先例：`refund_views.REFUND_NODE`）。 ⚠️ **权限装配写在 `get_permissions()` 方法体里**（T2 判据只解析方法体，不解析类级 `permission_classes`）。 ⛔ 租户判据一律用 `apps.core.storage.key_belongs_to_tenant`（**归一化后**判首段）， **不是**字符串 `key.startswith(f"{tenant}/")`——Task 1 的独立评审**实测**后者可被 `demo/../victim/orders/x.jpg` 绕过（跨租户读写同时成立）。 ## 订单截图走共享前缀（业务裁定） - **`img_type=0`（订单截图）一律**落在 `shared-orders/<链上最初单号>/…`（`build_shared_order_key`）：同一张单不管被链上几个租户接过，图都是**同一套**（字节只存一份，各租户各持一行指着同一对象）。 origin 的口径**只有一处**：`apps.business.media_access.order_shared_origin`。 ⛔ **不按**「这张单有没有链身份」分叉——链头没有 `PeerOrderLink` 行，分叉会让同一张单的图一半在租户前缀、一半在共享前缀，且**静默**失败。 - **`img_type=1`（群聊二维码）自 D-0316起也走共享前缀**：它随订单在上下家之间同步（业务方原话「覆盖很正常，一个订单一般只有一个群二维码」）⇒ key 走 `build_shared_order_key(…, kind="qr")` = `shared-orders/<最初单号>/qr.<随机>.<ext>`。 ⚠️ 旧口径（R-P4-7「二维码是本租户自己的群邀请，留在本租户前缀、不同步」）**已废除**；链上相邻租户可读这件事业务方已确认接受（群邀请本来就是进群凭证）。 ⚠️ 覆盖淘汰仍然**只在本单内**：对端 `add` 会覆盖本租户自己那张，业务方明确接受。 - **未知 `img_type` 兜底到本租户前缀**（fail-closed：共享是安全边界）。 - 因此**签发 / 认领**两处的护栏都必须传 `shared_holder=order_screenshot_key_is_held` （护栏对共享前缀**没判据就一律拒**，fail-closed）。⛔ **用户媒体**那条路径（`apps/core/profile_views.py`） **不传**判据 ⇒ 头像 / 收款码对共享前缀仍是一律拒。
## 每单 20 张订单截图（业务方裁定） - 口径：某订单的 **`img_type=0` 行数**上限 20，含 `pending` 与 `ready`；**硬删的行不计** （`row.delete()` 之后它不在表里）。`img_type=1`（群聊二维码）**不受**这条约束。 - 两个强制点：**签发**（早反馈，省掉一次直传）+ **认领**（权威——`head` 拿得到真实大小/存在性，而签发时什么都还没有）。 - 拒绝形状：HTTP **200** + `ErrorCode.VALIDATION`（**1000**）+ `SCREENSHOT_LIMIT_MSG` 逐字文案（与既有「只支持 jpg / jpeg / png」同口径）。「记录不存在」才是 **1002**，⛔ 两者别混。 - **并发**：两个同时到达的请求可能**都**数到 19 ⇒ 计数与落行必须串行。做法是在 `transaction.atomic()` 里对**订单行**做 `select_for_update()`（两个强制点共用同一把锁），再数数、再落行。判据见 `tests/test_order_screenshots_api.py::OrderScreenshotLimitConcurrencyTests`。 - ⚠️ **两处锁防的是两件不同的事，⛔ 别混**： - **签发侧**（`ScreenshotTicketView.issue_ticket`）的锁**是承重的**：它把「计数 + **落行**」串起来，没有它两个并发签发会**都**数到 19、各落一行 ⇒ 21 张（判据 = `OrderScreenshotLimitConcurrencyTests`）。 - **认领侧**（`ScreenshotCollectionView.claim`）这把锁**不**防计数：认领**不改变行数** （同一行 `pending → ready`，是 `UPDATE` 不是 `INSERT`）⇒ 去掉它， `tests/test_order_screenshots_api.py` 的 **27/27 仍绿**（变异实测）。它真正在防的是 `_evict_previous_group_qr` 的 **read-then-delete 竞态**：两条并发认领 **群聊二维码**（`img_type=1`）时，两边都读不到「已就绪的二维码」⇒ 两张都留成 `ready` ⇒ 打破 D-0151 的「每单最多 1 张二维码」。判据见同文件的 `OrderScreenshotGroupQrConcurrencyTests`（有锁 1 行 / 去掉 `select_for_update()` 2 行）。 ### 锁的粒度与代价（⛔ 不是「反正是小概率」） - **锁什么**：`business_order` 里**那一行**（不是截图行、不是整张表、也不是「本租户」）。 ⛔ 不能锁截图行：计数为 0 时**没有行可锁**，两个请求会各拿一把空锁同时通过。 - **影响面**：同一订单的「签发 / 认领」互相串行——这正是要的（它们都在改这个订单的图片集合）。不同订单之间**不互相阻塞**；`GET`（列表 / zip）与 `DELETE`（硬删）**不取这把锁**，所以它们不会被上传挡住（代价：硬删与签发之间没有互斥，见下）。 - **持锁时长（只对签发侧成立）**：签发侧很短——一次 `COUNT` + 一次 `INSERT`，且 COS 的 **签名**调用（`presign_put`）被**移到事务之外**，不让它占着锁。 - ⚠️ **认领侧的持锁时长不含上面那条结论**（「COS 调用移到事务外」只对签发侧的 `presign_put` 成立）：认领的锁**内**有远端 I/O—— `storage.delete`（超限 / 类型不符时的补偿清理）、`storage.head`（拿真实大小）、 `sniff_image_type`（内部 `open_stream` 读头几个字节）、以及 `_evict_previous_group_qr`
里的删除。**已知代价**：一次认领会被这些远端调用的往返时间拖住，而它与签发共用同一把订单行锁 ⇒ **同一订单的签发票据会被一次认领的远端 I/O 挡在锁外**（生产 COS 下是若干次跨公网往返，不是「很短」）。**将来的改法**（暂不做：要重排补偿路径与失败分支，风险大于收益）：把 `head` / `sniff` 挪到事务之外（先无锁读一次，再进事务复检行状态与淘汰），让锁只包住「复检行 + 淘汰 + 落状态」这几步。 - **已知边界**（⛔ 不写「唯一」这类绝对句）：`ScreenshotDeleteView.destroy` 是硬删、走的不是这把锁 ⇒ 「删除」与「签发」并发时，最坏结果是删除先提交、签发随后仍按未删的计数落行（少一张或多一张的窗口），但它**不会**突破 20 的上限方向（上限只会更严，不会更松）。 ## ⭐⭐ T10a：签发 / 认领的判据与库写入已抽成**模块级领域函数** 上面每一段描述的判据，今天住在两个**模块级**函数里（⛔ 不再是视图方法体内）： | 函数 | 谁调它 | |---|---| | `issue_screenshot_ticket(...)` | 本模块的 `ScreenshotTicketView.issue_ticket`（内网）＋ 对外开放平台的**取票**端点（`apps/openapi/api/order_image_views.py`） | | `claim_screenshot(...)` | 本模块的 `ScreenshotCollectionView.claim`（内网）＋ 对外开放平台的**认领**端点（同一个文件） | **为什么抽**：对外开放平台 v2 的进度图是「取票 + 认领」两段（设计文档 §13.10-3 的业务方裁定），而那两段必须复用**同一份**判据（`AGENTS.md` §5-E：⛔ 不许在 `apps/openapi/**` 里抄第二份）。 **两个视图方法因此只剩「校验入参 → 调领域函数 → 包信封」**，行为逐字不变。 ⚠️ **为什么留在本模块、⛔ 没有另开一个领域文件**（如实登记这个取舍）：既有用例里有三处 **按模块属性打桩**的地方 —— `tests/test_order_screenshots_api.py` 的 `mock.patch("apps.business.api.screenshot_views._order_screenshot_count")`（并发用例的 **同步点**：没有它，那条并发用例会退化成"又测了一遍普通边界"）与 `tests/test_peer_p4_shared_media.py` 的两处 `mock.patch("apps.business.api.screenshot_views.enqueue_screenshot_event")` （故障注入：广播写不进去时认领必须回滚）。实现搬到别的模块会让这三个桩**静默失效** （E-0029：桩必须打在**被查找的那个模块**上）⇒ 那等于改准既有判据、并让一条并发判据失去判别力。先例同族：T4 抽结算筛法时 `settlement_rows` 也留在 `apps/business/api/settlement_views.py` 里、由开放平台的 `bills/` 端点 import。 ⚠️ 抽取后两条结构网按**作用域名**重数过（它们按 AST 收落点，键是"文件 + 作用域名"）： `tests/test_order_read_path_net.py` 的 `ORDER_READ_SITES` 两条改成新函数名、 `tests/test_openapi_event_sites_net.py` 的进度图写入点①改成新函数名（判据一个字没放宽）。
"""
import uuid
from dataclasses import dataclass

from django.db import connection, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied

from apps.audit.actions import ACTION_LOG_TYPES
from apps.audit.log import record
from apps.business.api.screenshot_serializers import (
    ScreenshotClaimSerializer,
    ScreenshotTicketSerializer,
)
from apps.business import group_qr, jidanpu_member, order_qr_fetch
from apps.business.media_access import order_shared_origin, order_screenshot_key_is_held
from apps.business.models import Order, OrderScreenshot
from apps.business.peer_dispatch import enqueue_screenshot_event
from apps.core.datetime_display import local_iso
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.storage import (
    ALLOWED_EXTS,
    MAX_IMAGE_BYTES,
    append_thumbnail_query,
    build_order_key,
    build_shared_order_key,
    get_storage,
    key_belongs_to_tenant,
    sniff_image_type,
)
from apps.rbac.permissions import has_node

#: 上传（签发 + 认领）用同一个节点：对标把「上传订单图片」记成一个权限点（id 33）。
SCREENSHOT_UPLOAD_NODE = "order.upload_screenshot"
#: 查看（列表 / 单张读链接）。对标 id 32。
SCREENSHOT_VIEW_NODE = "order.view_screenshot"
#: 删除（对标 id 120「可以查看、删除上传的截图」）。
SCREENSHOT_MANAGE_NODE = "order.screenshot_manage"

#: 单个订单的**订单截图**（`img_type=0`）行数上限（业务方裁定）。 ⚠️ 计数含 `pending` 与 `ready` 两种状态（行还在就算占位）；**硬删**（`row.delete()`）的行不在表里 ⇒ 自然放过。
#: ⚠️ `img_type=1`（群聊二维码）**不受**它约束——那种图维持既有的「每单最多 1 张、认领新图时淘汰旧图」（`_evict_previous_group_qr`）。 ⚠️ 两个强制点（签发 + 认领）共用这一个数与这条文案，⛔ 别在别处再抄一份。
MAX_ORDER_SCREENSHOTS = 20
#: 超限的拒绝文案（契约逐字，前端按同一份契约提示；⛔ 标点与空格都不要动）。
SCREENSHOT_LIMIT_MSG = "单个订单最多上传20张截图，请先删除后再上传"

def _tenant_schema() -> str:
    """租户标识 = **租户 schema 名**（spec §6.2）。

    ⛔ 只来自 `connection.schema_name`（请求期已由 django-tenants 的中间件设好），
    **绝不接受任何来自请求参数的租户标识**（`AGENTS.md` §4）。
    """
    return connection.schema_name


def _order_screenshot_count(order_id: int, *, exclude_pk: int | None = None) -> int:
    """该订单**现存的**订单截图行数（`img_type=0`，含 `pending` 与 `ready`）。 ⚠️ 计的是**行**、不是文件：所以硬删过的行不占位，而「签发了还没上传完」的 `pending` 行占位（那是**有意**的——它已经是一张被授权的图，24 小时清理任务会收掉没人认领的）。
    ⚠️ 本函数是**行锁的承重搭档**（两个强制点都在 `select_for_update` 之后调它）：并发用例的同步点就打在它身上（`tests/test_order_screenshots_api.py` 的 `OrderScreenshotLimitConcurrencyTests`），⛔ 别把它内联掉。
    """
    rows = OrderScreenshot.objects.filter(
        order_id=order_id, img_type=OrderScreenshot.IMG_ORDER
    )
    if exclude_pk is not None:
        rows = rows.exclude(pk=exclude_pk)
    return rows.count()


def _order_or_404(order_no: str) -> Order:
    """按**订单号**取单（全线对外的标识符；用主键等于换一套对外契约）。

    ⚠️ 走 `Order` 的默认 manager ⇒ 已软删单取不到（D-0144），这是**要的**：
    不许再往回收站里的单挂图。
    """
    return get_object_or_404(Order, order_no=order_no)


def _pending_row_or_error(
    order_no: str, screenshot_id: int | None, user, *, object_key: str | None = None,
    img_type: int | None = None,
) -> OrderScreenshot | None:
    """本次**由该用户**签发、还没认领的那一行（同时锁死「属于本单」与「属于签发人」）。 ⚠️ `uploaded_by=user` 是**评审修复**加的一道：只限「本租户 + 本单 + pending」时，同租户持上传节点的人可以**枚举 pk** 认领别人刚签发（甚至还没上传完）的行、并顺带覆盖群二维码（Task 3 评审 Important）。跨租户不成立（schema 隔离）。 ## ⭐ T10a：两个**选择器**，判据仍然只有一份 - 内网契约给的是 `screenshot_id`（签发那一跳的回执里有它）；
    - 对外的取票/认领契约**不给内部主键**，第三方手里只有对象名 ⇒ 它按 `object_key` 选。 ⛔ 两者**只是"选哪一行"的入口**，其余判据（本单 / 未软删 / pending / 同一上传人）逐字共用 ⇒ 不是第二份实现（`AGENTS.md` §5-E）。 ⚠️ **必须恰好给一个选择器**（两个都不给 = "这一单任意一条 pending 行"，那会让认领变成"随便挑一张"）⇒ 显式 `ValueError`（用例： `tests/test_openapi_order_images.py` 的那条直调用例），⛔ 不静默取任意行。
    """
    if screenshot_id is None and object_key is None:
        raise ValueError("必须给出 screenshot_id 或 object_key 之一")
    rows = OrderScreenshot.objects.filter(
        order__order_no=order_no,
        # ⚠️ D-0144：跨关系 JOIN 不过 `Order` 的默认 manager ⇒ 已软删单的图必须显式排除
        order__deleted_at__isnull=True,
        status=OrderScreenshot.STATUS_PENDING,
        uploaded_by=user,
    )
    if img_type is not None:
        rows = rows.filter(img_type=img_type)
    if screenshot_id is not None:
        rows = rows.filter(pk=screenshot_id)
    if object_key is not None:
        rows = rows.filter(object_key=object_key)
    # ⚠️ `.first()` 对**无序** queryset 会自己补 `ORDER BY pk` ⇒ 两个选择器下都确定
    #    （`object_key` 那一支若要按"最早那条"取，行为与内网那一支同形）。
    return rows.select_related("order").first()


@dataclass(frozen=True)
class ScreenshotOutcome:
    """签发 / 认领的**领域结果**（纯数据，⛔ 不是 DRF 的 `Response`）。 ⚠️ 两个调用方（内网视图 `ScreenshotTicketView` / `ScreenshotCollectionView` 与对外开放平台的取票 / 认领端点）各自包**自己的信封**，而判据与库写入只有下面那两份实现（`AGENTS.md` §5-E：同一个事实两套判据必然漂移）。 - `code` / `msg` / `data`：与 `apps.core.error_codes.ErrorCode` 同一套码； - `row`：认领成功时**那一行**（对外端点要用它装配"该图片项"载荷 —— 走
    `order_extra_views.image_row` 那一个共用出口，⛔ 不在开放平台里拼第二份）； - ⚠️ `writes_kept`：本结果**在库里留下了要保留的写入**（今天只有一种形状：拒收时把那一行**连同对象一起删掉**）。它的**唯一**消费者是对外那条路径 ——它套着幂等入口，而那个入口对"业务码 ≠ 0"的默认处置是**整笔回滚** （`apps/openapi/idempotency.py::_run`）。⇒ 那种情况下调用方必须 `mark_side_effect(request)`，否则**删除会被回滚、对象却已经没了**：
    留一条指着不存在对象的 `pending` 行（订单的张数还永久停在 21）。 ⚠️ 内网那条路径不看它（它不套幂等入口 ⇒ 正常 `return` 会提交那些删除，E-0022 的形状）。
    """

    code: int
    msg: str = ""
    data: dict | None = None
    row: OrderScreenshot | None = None
    writes_kept: bool = False

    @property
    def ok(self) -> bool:
        return int(self.code) == int(ErrorCode.OK)


def issue_screenshot_ticket(
    *, order_no: str, ext: str, img_type: int, uploaded_by, presign: bool = True,
) -> ScreenshotOutcome:
    """**签发一张上传凭证**并把 `pending` 行落库（三段式的第一段；唯一的实现）。 ⚠️⭐ 开放平台 v2 的「取票」端点（`apps/openapi/api/order_image_views.py`）复用**同一份**判据与库写入（设计文档 §13.10-3：⛔ 不许在 `apps/openapi/**` 里抄第二份）⇒ 判据抽成这个**模块级函数**，两侧各调它一次；覆盖面 = 数量闸 / key 形状 / 行锁 / 落行字段 / 预签名，两侧必须同步改。 ⚠️ **为什么留在本模块、⛔ 没有另开文件**：既有用例有三处 **按模块属性**打桩的同步点 / 故障注入点 —— `tests/test_order_screenshots_api.py` 的 `mock.patch( "apps.business.api.screenshot_views._order_screenshot_count")`（并发用例的同步点）与 `tests/test_peer_p4_shared_media.py` 的两处
    `mock.patch("apps.business.api.screenshot_views.enqueue_screenshot_event")`。把实现搬去别的模块会让这三个桩**静默失效**（E-0029：桩必须打在**被查找的那个模块**上） ⇒ 那会改准既有判据、并把"绿的并发用例"退化成没测并发。先例同族：T4 抽结算筛法时 `settlement_rows` 也留在 `apps/business/api/settlement_views.py` 里，由开放平台 import。 ## 入参 / 出参 - `ext`：调用方给的后缀（本函数自己 `lower().lstrip(".")` 后按 `ALLOWED_EXTS` 校）； - `img_type`：`0`=订单截图 / `1`=群聊二维码 —— **两类都走共享前缀**（D-0316）； ⚠️ 两个取值都合法，**"对外只许 0"是对外契约的收窄**，那一条判据在 `apps/openapi/api/order_image_views.py` 里（⛔ 不写进这里：内网要 `1`）； - `uploaded_by`：`OrderScreenshot.uploaded_by`（**NOT NULL + PROTECT**）。内网传 `request.user`；对外路径没有"人"，传 `ApiApp.created_by`（"代理上传"，口径见 `peer_inbound._screenshot_uploader` 那个"非人类上传者"先例的同类处置）。
    ## 逐条承接的既有闸（⛔ 一条都不许丢） 1. 扩展名白名单 `ALLOWED_EXTS`（`core/storage.py:40`）⇒ 不在表里回 `1000`； 2. 数量闸 `MAX_ORDER_SCREENSHOTS`（只对 `img_type=0`；含 `pending`）； 3. **行锁** `Order.objects.select_for_update()`（锁**订单行**）：把"数数 + 落行"串起来，否则两条并发签发会**都**数到 19（判据 = `OrderScreenshotLimitConcurrencyTests`）； 4. key 口径：**两类图都走共享前缀**（D-0316：群码可跨租户同步）—— `0` / `1` 都走 `build_shared_order_key(order_shared_origin(order), …)`（`1` 带 `kind="qr"`）；**未知取值兜底到本租户前缀**（fail-closed：共享是安全边界）； 5. 预签名放**事务之外**（COS 那条路要调 SDK，别让它在锁里多待）。 ⚠️ 本函数**不写操作日志**（与内网那条端点同口径：`spec §9.2` 只给不可恢复的动作留痕；登记见 `tests/test_operation_log_ledger.py` 的 `DELIBERATELY_NOT_LOGGED`）。
    """
    ext = ext.lower().lstrip(".")
    if ext not in ALLOWED_EXTS:
        return ScreenshotOutcome(code=ErrorCode.VALIDATION, msg="只支持 jpg / jpeg / png")

    # ⚠️ **行锁**（`SELECT … FOR UPDATE` 锁**订单行**）把「数数 + 落行」串起来：
    #    没有它的话，两个同时到达的请求会**都**数到 19、都落一行 ⇒ 21 张。
    #    锁的粒度、代价与「为什么锁订单行而不是截图行」见模块 docstring 的「并发」段。
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), order_no=order_no)
        over_limit = (
            img_type == OrderScreenshot.IMG_ORDER
            and _order_screenshot_count(order.pk) >= MAX_ORDER_SCREENSHOTS
        )
        if not over_limit:
            random_part = uuid.uuid4().hex[:12]
            if img_type == OrderScreenshot.IMG_GROUP_QR:
                # ⭐ **D-0316（业务方拍板）**：群聊二维码与订单截图一样走**共享前缀**    —— 它随订单在上下家之间同步，字节只存一份（"一个订单一般只有一个群二维码"）。    ⚠️ 旧口径（R-P4-7「留在本租户前缀」）已废除：群码进共享段后，链上相邻租户
                #    可读，业务方已确认接受（群邀请本来就是进群凭证）。    ⚠️ 覆盖淘汰仍**只在本单内**（`_evict_previous_group_qr`）：对端 add 会覆盖    本租户自己那张，业务方明确接受（"覆盖很正常"）。
                key = build_shared_order_key(
                    order_shared_origin(order), ext,
                    random_part=random_part, kind="qr",
                )
            elif img_type == OrderScreenshot.IMG_ORDER:
                # ⭐ **每一张订单截图都走共享前缀**（裁决 R-P4-12），origin 由    `order_shared_origin` 统一口径解析（链身份 / 老行兜底 / 本单号）。    ⛔ **不许**按「这张单有没有链身份」分叉：链头**没有** `PeerOrderLink`
                #    行，分叉会让同一张单的图一半在本租户前缀、一半在共享前缀，且**静默**    失败（本地看不出任何异常）。
                key = build_shared_order_key(
                    order_shared_origin(order), ext, random_part=random_part
                )
            else:
                # ⭐ **显式的 `elif` + 这里兜底到本租户前缀**（M-b，fail-closed）：
                #    将来 `img_type` 长出第三种取值时，⛔ 不许它**静默**落进共享前缀
                #    （`shared-holder` 那条窄例外只该由 `img_type=0` 走）——共享是安全边界。
                key = build_order_key(
                    _tenant_schema(), order.order_no, ext, random_part=random_part
                )
            row = OrderScreenshot.objects.create(
                order=order,
                object_key=key,
                uploaded_by=uploaded_by,
                img_type=img_type,
            )
    if over_limit:
        # ⛔ 拒在**事务之外**：块里此刻没有任何写入（只有那次 `SELECT … FOR UPDATE`），
        #    所以这里的 `return` 不会提交半截写入（E-0022 的形状）。
        return ScreenshotOutcome(code=ErrorCode.VALIDATION, msg=SCREENSHOT_LIMIT_MSG)
    data = {"screenshot_id": row.pk, "key": key}
    # COS 签名留在事务之外，短时浏览器授权由本服务接收图片。
    if presign:
        signed = get_storage().presign_put(key)
        data.update(upload_url=signed.url, expires_in=signed.expires_in)
    return ScreenshotOutcome(code=ErrorCode.OK, data=data, row=row)


def claim_screenshot(
    *,
    order_no: str,
    uploaded_by,
    screenshot_id: int | None = None,
    object_key: str | None = None,
    original_name: str = "",
    remark: str = "",
    img_type: int | None = None,
) -> ScreenshotOutcome:
    """**认领**：把刚直传上去的对象落成 `ready`（三段式的第三段；唯一的实现）。 ⚠️⭐ 与 `issue_screenshot_ticket` 同源同取舍（⛔ 不另开文件：三处按模块属性打的桩必须继续生效）。 ## 逐条承接的既有闸与副作用（⛔ 一条都不许丢） 1. **行锁**（与签发端点**同一把**：锁订单行）。⚠️ 它在这里**不**防计数（认领是 `UPDATE`、行数不变；变异实测：去掉它「20 张上限」的判据照样全绿），它防的是 `_evict_previous_group_qr` 的 read-then-delete 竞态（判据 = `OrderScreenshotGroupQrConcurrencyTests`）。代价：锁**内**有远端 I/O（见模块 docstring）； 2. 行选择：本单 + 未软删 + `pending` + **同一上传人**（`uploaded_by=uploaded_by`）——选择器二选一，见 `_pending_row_or_error`； 3. 数量闸（**除本行之外**已有 20 行 ⇒ 拒）：拒时把**那一行连同对象一起删掉**； 4. 租户护栏 `key_belongs_to_tenant(..., shared_holder=order_screenshot_key_is_held)` （共享前缀是唯一窄例外，不传判据一律拒）⇒ 不通过抛 `PermissionDenied`（403）； 5. `storage.head(...)` 拿**真实**大小（⛔ 不采信客户端）⇒ 拿不到 = `1000`「文件还没上传成功」；
    `size <= 0` ⇒ 删对象 + 删行 + `1000`「图片无效或为空」； `size > MAX_IMAGE_BYTES`（18 MiB）⇒ 删对象 + 删行 + `1000`「图片超过 18 MB」； 6. 类型判据 = **魔数**（`sniff_image_type`，⛔ 不看 `Content-Type`、也不信按扩展名推的值） ⇒ 认不出时删对象 + 删行 + `1000`「只支持 jpg / jpeg / png」； 7. `_evict_previous_group_qr(row)`（群聊二维码每单最多一张）； 8. 落库字段：`size_bytes` / `content_type`（**嗅探出来的**类型）/ `original_name` / `remark` / `status=ready` / `ready_at`； 9. **入队广播**（**两类图都播**，且**同一个事务内**的最后一句）： `enqueue_screenshot_event` 对 `0` / `1` **都**发（⭐ D-0316：群码随订单同步）；开放平台的 `notify_order_image_added` 与打手回调**只对 `img_type=0`**——那是"订单进度图" 的语义（两个当事人，⛔ 不能省掉一句，⛔ 也不能顺手把群码接进来）。 ## ⚠️⭐ 四条"拒收即清理"的分支都带 `writes_kept=True`（T10a 补的一处**必要**标记）上面第 3 / 5 / 6 条里那四档（超限 / 0 字节 / 超 18 MiB / 魔数认不出）都**删行 + 删对象**。 ⛔ 那两笔删除是**要保留的业务写入**、不是补偿噪音：留着那一行会让订单的张数永久停在 21 （列表只出 `ready` ⇒ 用户看不到它、也再也传不进新图，只能等 24 小时清理任务）。
    ⇒ 这四档把 `ScreenshotOutcome.writes_kept` 置真，让**套着幂等入口**的对外路径调 `mark_side_effect(request)`；否则 `_run` 会把它们整笔回滚，而 `storage.delete` **不是** 事务性的 ⇒ 留下一条指着**已不存在对象**的 `pending` 行（重试时的答复还会从「图片无效或为空」变成「文件还没上传成功」——一次静默的答复漂移）。 ⚠️ 内网那条路径不看这个标记（它不套幂等入口，正常 `return` 会提交那些删除）。 ⚠️ 判据：`tests/test_openapi_order_images.py` 的三条拒绝用例断言**那一行真的被删掉**。 ## 留痕口径（按现状办，⛔ 不是本函数新增的行为）认领**不写** `OperationLog`（`spec §9.2` 只给不可恢复的动作留痕；内网那两个端点登记在 `tests/test_operation_log_ledger.py` 的 `DELIBERATELY_NOT_LOGGED`）。对外路径沿用同一条口径 ⇒ 「这是哪个应用触发的」这条事实**不进操作日志**，由 `uploaded_by` （= `ApiApp.created_by`，即创建该应用的那个员工）承载—— ⚠️ **已知边界**：同一个员工建的多个应用在这条路上**区分不开**（本路径没有别的留痕面）。要把应用名写进留痕，得先新增一个动作码（六处登记的**扩大改动面**动作）——按 `AGENTS.md` §5-G 的升级规则，那要先升档确认，⛔ 不顺手加。
    """
    storage = get_storage()
    # ⚠️ **行锁**（与签发端点**同一把**：锁的都是订单行）。详细的理由与代价见上面 docstring
    #    第 1 条与模块 docstring 的「锁的粒度与代价」。
    with transaction.atomic():
        # ⚠️ 订单不存在 / 已软删时 `locked_order is None`：这时**保持既有行为**
        #    （下面取不到行 ⇒ `1000`「没有待认领的上传记录」），⛔ 不在这里改判成 `1002`。
        locked_order = (
            Order.objects.select_for_update().filter(order_no=order_no).first()
        )
        row = _pending_row_or_error(
            order_no, screenshot_id, uploaded_by, object_key=object_key, img_type=img_type
        )
        if row is None:
            return ScreenshotOutcome(
                code=ErrorCode.VALIDATION, msg="没有待认领的上传记录"
            )

        # ⚠️ 上限判据：**除本行之外**已有 20 行 ⇒ 认领它就会变成 21 ⇒ 拒。
        #    ⛔ 只对 `img_type=0` 生效：群聊二维码不受这 20 约束（见 `MAX_ORDER_SCREENSHOTS`）。
        if (
            row.img_type == OrderScreenshot.IMG_ORDER
            and locked_order is not None
            and _order_screenshot_count(locked_order.pk, exclude_pk=row.pk)
            >= MAX_ORDER_SCREENSHOTS
        ):
            # 与下面两条拒绝同款：这是一次**终态**拒绝，把行与对象都清掉——
            # 留着它会让订单的计数停在 21、用户既看不到这一行（列表只出 ready）
            # 又再也传不进新图（只能等 24 小时清理）。
            storage.delete(row.object_key)
            row.delete()
            return ScreenshotOutcome(
                code=ErrorCode.VALIDATION, msg=SCREENSHOT_LIMIT_MSG, writes_kept=True
            )

        # 护栏②：对象名必须属于本租户（**归一化**判据，见模块 docstring）。 ⚠️ 订单截图现在落在**共享前缀** `shared-orders/<origin>/…` 下，而那条
        #    前缀的首段不是租户名 ⇒ 必须把「本租户确实持有这张单」的判据交给护栏；    ⛔ 不传判据时护栏对共享前缀**一律拒**（fail-closed），那会让所有共享截图认领失败。
        if not key_belongs_to_tenant(
            row.object_key, _tenant_schema(),
            shared_holder=order_screenshot_key_is_held,
        ):
            raise PermissionDenied("对象名不属于当前租户")

        # 护栏②的加强：`head` 拿**真实**大小（⛔ 不采信前端上报的任何值）
        meta = storage.head(row.object_key)
        if meta is None:
            return ScreenshotOutcome(
                code=ErrorCode.VALIDATION, msg="文件还没上传成功"
            )
        # ⚠️ 只取**大小**；`head()` 给的内容类型**故意丢弃**——它客户端可控 / 本机按扩展名推，
        #    不可信（真正的判据在下面的魔数嗅探）。复评指出过这里曾是「死绑定」。
        size, _header_content_type = meta
        # ⚠️ **0 字节对象一律拒收**（Minor-3）：与 `media_blob_views` 模块    docstring 的「空图片永远不合法」同一口径，码与清理路径**照抄**下面那条超限分支    （同一个 `ErrorCode.VALIDATION`、同样删对象 + 删行）。文案与超限分开：0 字节不是
        #    「超过 18 MB」。⚠️ 它**先于**下面的魔数嗅探（嗅探也能让 0 字节落不了库，但会报    「只支持 jpg / jpeg / png」——对空文件是误导）。
        if size <= 0:
            storage.delete(row.object_key)
            row.delete()
            return ScreenshotOutcome(
                code=ErrorCode.VALIDATION, msg="图片无效或为空", writes_kept=True
            )
        if size > MAX_IMAGE_BYTES:
            storage.delete(row.object_key)
            row.delete()
            return ScreenshotOutcome(
                code=ErrorCode.VALIDATION, msg="图片超过 18 MB", writes_kept=True
            )

        # ⭐ 类型判据 = **魔数**（读对象头几个字节），⛔ 不看客户端给的 `Content-Type`、
        #   也不信本机实现按扩展名推出来的那个值。
        sniffed = sniff_image_type(storage, row.object_key)
        if sniffed is None:
            storage.delete(row.object_key)
            row.delete()
            return ScreenshotOutcome(
                code=ErrorCode.VALIDATION, msg="只支持 jpg / jpeg / png",
                writes_kept=True,
            )

        _evict_previous_group_qr(row)
        row.size_bytes = size
        # 记录**嗅探出来的**类型（比 `Content-Type` 可信；COS 侧常见 `application/octet-stream`）
        row.content_type = sniffed
        row.original_name = original_name
        row.remark = remark
        row.status = OrderScreenshot.STATUS_READY
        row.ready_at = timezone.now()
        row.save(update_fields=["size_bytes", "content_type", "original_name",
                                "remark", "status", "ready_at"])
        # ⭐ E-0282 / B2：图变 ready ⇒ 给该单已建链的①式 member 绑定排一条    `member.assign` 补推（消费侧只补缺的那几张图，见 `queue_pic_repush_for_ready_screenshots`）。    ⭐ **M3：挂在 `row.save()` 之后、下面那个 `img_type == IMG_ORDER` 闸之外**——
        #    闸内那些入队全是**订单截图档**的广播（peer / 开放平台 / 打手回调，各自的理由    都写在闸内）；member 口径是「全传」（含群聊二维码）⇒ 照抄进闸会把**群码**的    补推静默漏掉。位置仍在**同一个 `transaction.atomic()` 块内**：图行 ready 与
        #    补推行同生共死（照「写事件与业务动作同一个事务」那条纪律）；    上面每一条拒绝分支都在 save 之前 `return` ⇒ 一条都不排。
        jidanpu_member.queue_pic_repush_for_ready_screenshots(row.order)
        # ⭐ （裁决 R-P4-17 / Minor 1）：**认领成功之后**才广播，而且这一笔入队    是**同一个 `transaction.atomic()` 里的最后一句**——与"这一行真的 ready"同生共死    （`peer_dispatch` 的纪律：写事件与业务动作同一个事务）。放在块外会让    "提交成功、入队失败"变成一次**静默丢广播**：行是 ready，对端永远不知道，也没有    任何东西会重试。⛔ 上面每一条拒绝分支都在 `with` 块里 `return` ⇒ 一条都不发。    ⛔ 不在签发那一半发：那里行还是 `pending`、对象可能永远不上传。
        #    ⭐ **D-0316 改准**：群聊二维码**也广播**（它随订单在上下家之间同步，    业务方：「之间同步就行，覆盖很正常」）——见下面那个 `elif` 支。⚠️ 它与订单截图    **共用** peer 那条广播，但 **⛔ 不进**开放平台 / 打手回调那两句（它们的语义是    "订单进度图"，群码没有；开放平台推群码仍由锁定应用那条独立路径负责）。
        if row.img_type == OrderScreenshot.IMG_ORDER:
            enqueue_screenshot_event(
                row.order,
                kind="order.screenshot.add",
                object_key=row.object_key,
                img_type=row.img_type,
                remark=row.remark,
            )
            # ⭐ 开放平台 v2（T8，设计文档 §7.2.1 裁决 3）：进度图变 `ready` 的**三个**写入点    之一（内部认领）。⚠️ 上面那句是 **peer 链路**的广播，与开放平台那条推送    通道**不是同一个当事人** ⇒ 必须各接一句（⛔ 不能拿"已经广播过了"当省掉）。    ⚠️ 位置仍在**同一个 `transaction.atomic()` 块内**、`row.save()` 之后：    "这一行真的 ready"与推送行同生共死（放块外就是"提交成功、入队失败"的静默丢推送）。    ⚠️ 同一处 `if row.img_type == IMG_ORDER` 之内：开放平台 / 打手回调的语义是
            #    「订单进度图」，⛔ 不是「群聊二维码是本租户自己的群邀请」——那句旧理由已随    D-0316 废除（群码现在也走共享前缀并 peer 广播）；这里管的是**回调**要不要发，    群码那两句仍不发（与 peer 广播**不是**同一件事，⛔ 别顺手放开）。    锁定应用可以上传群码，但这里仍不推 `order.image_added`（推的是「新增了一张进度图」）。    群码另走会员图推送，别把「这里不推」读成「群码不外流」。    ⚠️⭐ **`spec=OPENAPI_SPEC` 是显式的**（回调线 T1b）：投递引擎的公开入口从 T1b
            #    起**不再有**"默认回上家侧"那一格（漏传 = 当场 `TypeError`）⇒ 本处（上家侧那一族    的写入点）显式声明自己是哪一族（理由见 `dispatch` 模块 docstring 第 4 条）。
            from apps.openapi import dispatch as openapi_dispatch
            from apps.openapi.credentials import OPENAPI_SPEC
            from apps.takerapi.api import callback_recipients as taker_callback

            # ⚠️ 两族共用**同一个**时刻对象（同一次动作、两个当事人）。
            moment = timezone.now()
            openapi_dispatch.notify_order_image_added(
                row.order,
                spec=OPENAPI_SPEC,
                object_key=row.object_key,
                img_type=row.img_type,
                remark=row.remark,
                occurred_at=moment,
            )
            # ⭐⭐ 打手侧回调线（T3，spec §4.3 那张表的第 4 行）：**紧挨着**上面那句，    同一个 `atomic()` 块、同一个 `moment`。⛔ 不能省——这张单的当前接手人    （我们这儿的打手）与上家应用是**两个当事人**，各接一句。
            #    ⚠️ 位置同样在那个 `if row.img_type == IMG_ORDER` **之内**、`row.save()` 之后：    ① 群码没有「订单进度图」这个打手侧语义（⛔ 不是"群码不外流"，见 D-0316）；    ② 放块外就是"行已 ready、入队失败"的**静默丢推送**。
            taker_callback.notify_order_image_added_for_taker(
                row.order,
                object_key=row.object_key,
                img_type=row.img_type,
                remark=row.remark,
                occurred_at=moment,
            )
        elif row.img_type == OrderScreenshot.IMG_GROUP_QR:
            # ⭐ D-0316：**群码认领也要广播 peer**（上行 + 下行各一条，    `enqueue_screenshot_event` 本来两个方向都发）——下家/上家都能看到这张群码，    接收侧按"后到覆盖"只留最新一张。
            #    ⛔ **只调 peer 这一句**：开放平台推送与打手回调是"订单进度图"的语义    （第三方没有群码那件事，见上面那段注释），⛔ 别顺手接进来。    ⚠️ 与订单截图同一个事务（`with transaction.atomic()` 内、`row.save()` 之后）。
            enqueue_screenshot_event(
                row.order,
                kind="order.screenshot.add",
                object_key=row.object_key,
                img_type=row.img_type,
                remark=row.remark,
            )
    return ScreenshotOutcome(code=ErrorCode.OK, data={"id": row.pk}, row=row)


class NodeGatedViewSet(viewsets.GenericViewSet):
    """三个节点视图的公共装配（`refund_views` 同款：方法体里写 `has_node`）。 ⚠️ `permission_denied` **必须转调 `super()`**：DRF 在抛 `PermissionDenied` 之前先判
    「有没有认证成功」——未认证时它抛 `NotAuthenticated`（401）。自己直接 `raise` 会把未认证也变成 403，破坏「未认证 401 / 已认证无权限 403」的分层。
    """

    node_code = ""

    def get_permissions(self):
        return [has_node(self.node_code)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(request, message=self.denied_message, code=code)


class ScreenshotTicketView(NodeGatedViewSet):
    """签发一张「只能写这一个对象」的上传凭证，并先落一条 `pending` 行（spec §6.3）。 ⚠️ **先落 `pending` 行**不是多此一举：直传是「先传文件、后落库」，用户传完就关页面会留孤儿对象，
    而本线定了**永久保留、没有 90 天兜底**（A3）⇒ 必须有一条「谁在什么时候被授权写过」的记录， 24 小时清理任务靠它（`purge_pending_screenshots`）。
    """

    node_code = SCREENSHOT_UPLOAD_NODE

    def get_permissions(self):
        # ⚠️ **显式写在这里**（⛔ 不靠基类的 `self.node_code`）：接线位点表的 T2 判据要解析
        #    **本方法体**里的 `has_node(模块常量)`——只写在基类上它解析不到（实测会红）。
        return [has_node(SCREENSHOT_UPLOAD_NODE)()]
    denied_message = "你没有上传订单图片的权限"

    def issue_ticket(self, request, order_no):
        """签发（内网入口）：**唯一的实现**在模块级 `issue_screenshot_ticket`。 ⚠️⭐ T10a：判据与库写入已抽成模块级领域函数，对外开放平台的
        「取票」端点调的是**同一个函数**（设计文档 §13.10-3）。本方法只剩「校验入参 → 调领域函数 → 包信封」，**行为逐字不变**。
        """
        ser = ScreenshotTicketSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        outcome = issue_screenshot_ticket(
            order_no=order_no,
            ext=ser.validated_data["ext"],
            img_type=ser.validated_data["img_type"],
            uploaded_by=request.user,
        )
        if not outcome.ok:
            return api_response(code=outcome.code, msg=outcome.msg)
        return api_response(outcome.data)


class ScreenshotCollectionView(viewsets.GenericViewSet):
    """`orders/<order_no>/screenshots` 这一条路径上的两个方法。 | 方法 | 语义 | 节点 | |---|---|---| | POST | 认领刚直传上去的对象（落成 `ready`） | `order.upload_screenshot` | | GET | 该订单的图 + 每张一个短时效读链接 | `order.view_screenshot` | **为什么两个方法住同一个视图**：spec §7 的端点表把它们写成**同一条路径**；
    拆成两个视图会让 `urls.py` 出现两条字面相同的 `path()`，而 Django **只命中第一条** ⇒ 第二条是**死路由**（本行自检时踩到）。`RefundView` 是同款先例。 ⚠️ 节点按**方法**分派，且两条 `has_node(...)` 都写在方法体里、参数是**模块常量** ——接线位点表（`apps/rbac/wiring.py`）的 T2 判据要能解析到它们（⛔ 别改成 `self.node_code`）。
    """

    denied_message = "你没有订单截图相关的权限"

    def get_permissions(self):
        if self.request.method == "POST":
            return [has_node(SCREENSHOT_UPLOAD_NODE)()]
        return [has_node(SCREENSHOT_VIEW_NODE)()]

    def permission_denied(self, request, message=None, code=None):
        super().permission_denied(request, message=self.denied_message, code=code)

    def claim(self, request, order_no):
        """认领（内网入口）：**唯一的实现**在模块级 `claim_screenshot`。 ⚠️⭐ T10a：判据与库写入（行锁 / 数量闸 / 租户护栏 / 真实大小 / 0 字节 / 18 MiB / 魔数 / 淘汰 / 入队广播）已抽成模块级领域函数，
        对外开放平台的「认领」端点调的是**同一个函数**（设计文档 §13.10-3，逐条闸的清单在那个函数的 docstring 里）。本方法只剩「校验入参 → 调领域函数 → 包信封」，**行为逐字不变**。
        """
        ser = ScreenshotClaimSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        outcome = claim_screenshot(
            order_no=order_no,
            screenshot_id=ser.validated_data["screenshot_id"],
            original_name=ser.validated_data["original_name"],
            remark=ser.validated_data["remark"],
            uploaded_by=request.user,
        )
        if not outcome.ok:
            return api_response(code=outcome.code, msg=outcome.msg)
        return api_response(outcome.data)

    def list_items(self, request, order_no):
        """该订单的图（**不分页**：单订单的图被上传上限约束住，分页只给前端加无用状态）。"""
        order = _order_or_404(order_no)
        rows = (
            OrderScreenshot.objects.filter(
                order__order_no=order_no,
                # ⚠️ D-0144：同上，软删单的图不进列表
                order__deleted_at__isnull=True,
                status=OrderScreenshot.STATUS_READY,
            )
            .order_by("pk")
        )
        storage = get_storage()
        items = []
        for row in rows:
            url = storage.presign_get(row.object_key).url
            items.append({
                "id": row.pk,
                "img_type": row.img_type,
                "url": url,
                "thumbnail_url": (
                    append_thumbnail_query(url)
                    if row.img_type == OrderScreenshot.IMG_ORDER
                    else url
                ),
                "original_name": row.original_name,
                "size_bytes": row.size_bytes,
                "remark": row.remark,
                "uploaded_by": row.uploaded_by_id,
                "created_at": local_iso(row.created_at),
            })
        # ⭐ D-0348：列表里没有群聊二维码（客服端看群码的读口空着）时，
        #    在这个自然触发点异步拉一次「马上来」查码（不阻塞本响应；未配置 ⇒ 零动作）。
        if not any(item["img_type"] == OrderScreenshot.IMG_GROUP_QR for item in items):
            order_qr_fetch.maybe_enqueue_fetch(order)
        return api_response({"items": items, "total": len(items)})


class ScreenshotDeleteView(NodeGatedViewSet):
    """删一张图（行 + 对象，**共享前缀的群码只删行**），并**记一条操作日志**（spec §9.2）。"""

    node_code = SCREENSHOT_MANAGE_NODE

    def get_permissions(self):
        # ⚠️ **显式写在这里**（⛔ 不靠基类的 `self.node_code`）：接线位点表的 T2 判据要解析
        #    **本方法体**里的 `has_node(模块常量)`——只写在基类上它解析不到（实测会红）。
        return [has_node(SCREENSHOT_MANAGE_NODE)()]
    denied_message = "你没有订单截图管理的权限"

    def destroy(self, request, pk):
        row = get_object_or_404(OrderScreenshot, pk=pk)
        delete_order_screenshot(row, actor=request.user.get_username(), actor_id=request.user.pk)
        return api_response(None)


def delete_order_screenshot(row, *, actor, actor_id=None, after_commit=False):
    order, key, image_type = row.order, row.object_key, row.img_type
    delete_bytes = group_qr.should_delete_object_bytes(img_type=image_type, object_key=key)
    if delete_bytes and not after_commit:
        get_storage().delete(key)
    with transaction.atomic():
        if image_type == OrderScreenshot.IMG_ORDER:
            enqueue_screenshot_event(order, kind="order.screenshot.delete", object_key=key)
        image_id = row.pk
        row.delete()
        record(
            log_type=ACTION_LOG_TYPES["order.delete_screenshot"], action="order.delete_screenshot",
            actor=actor, actor_id=actor_id, system=actor_id is None,
            target_kind="order_screenshot", target_id=str(image_id), order_no=order.order_no,
        )
        if delete_bytes and after_commit:
            transaction.on_commit(lambda: get_storage().delete(key), robust=True)


def _evict_previous_group_qr(row: OrderScreenshot) -> None:
    """群聊二维码**每单最多一张**：认领新的那张时，把同订单旧的二维码行淘汰掉（覆盖语义）。 ⭐ D-0316：内核（含「字节删不删」的决策）已统一到 `apps.business.group_qr`； **本函数保留名字与签名**（有测试按模块属性打桩：`tests/test_order_screenshots_api.py`
    的并发 QR 认领用例），函数体只做委托。 ⚠️ **只删 `ready` 的旧行**（Task 3 评审 Minor）：把「还没上传完」的 `pending` 行也删掉的话，那个对象之后才传上去就**没有行**能被 24 小时清理任务找到——在 A3（永久保留）下变成**永久孤儿**。
    `pending` 行留给 Task 7 的清理任务按超时处理。
    """
    if row.img_type != OrderScreenshot.IMG_GROUP_QR:
        return
    group_qr.evict_previous_ready_group_qr(row.order_id, keep_pk=row.pk)
