"""开放平台对外 v2 的订单图片两条写端点：**取票**（`POST .../images/ticket/`：落一行 `pending`+回 `{key, upload_url, expires_in}`）与**认领**（`POST .../images/`：校验+落成 `ready`+回该项）（spec §6.1/§6.3/§13.10-3）。
承重：进度图走浏览器直传对象存储（预签名 PUT），两步制是业务方裁定——"第三方给 URL 我方去抓"会新增 SSRF 面与慢 URL 占 worker；`img_type` 按 `request._api_app.kind` 分叉：通用应用只收 `0`、锁定应用（`wecom_group_qr`）只收 `1`，缺省一律**响亮拒绝** ⛔ 不静默兜底（锁死不能变空话；分叉表见 spec §7.2）；认证层放行表（`apps/openapi/permissions.py`）是另一道强制点，本模块判据是第二道。
装配/幂等照写端点同源（继承 `ApiOrderWriteAssemblyMixin` + `@idempotent`，回放=上一次的快照、`upload_url` 时效不因回放续期）；同路径约束：GET 列表已占路由 ⇒ 写的一半必须是 mixin；`uploaded_by`=`request.auth.created_by`（代理上传，⛔ 不置空、⛔ 不造假用户）；操作日志刻意不记（`DELIBERATELY_NOT_LOGGED`），可追溯性靠幂等台账+行上外键。"""
from rest_framework import serializers
from rest_framework.views import APIView

from apps.business.api.screenshot_views import claim_screenshot, issue_screenshot_ticket
from apps.business.models import OrderScreenshot
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.openapi.api.order_write_views import ApiOrderWriteAssemblyMixin
from apps.openapi.credentials import OPENAPI_SPEC
from apps.openapi.idempotency import idempotent, mark_side_effect
#: ⭐ 用途枚举（`general` / `wecom_group_qr`）：`img_type` 的分叉判据取**这一份**定义，
#: ⛔ 不在这里抄字符串字面量（抄一份只会在改枚举时静默分叉）。
from apps.openapi.models import ApiAppKind
from apps.openapi.serializers import image_row

#: 取票成功 `data` 的键集（恰好这三个；接入文档逐字列出）。⛔ 不回内部主键 `screenshot_id`
#: （认领入口是对象名 `key`；对外给内部主键违反「不给主键」纪律）；⛔ 不回 `img_type`
#: （签出来的图种调用方必然已知——通用只收 `0`、锁定只收 `1`，回它是废话+多一次契约变更面）。
TICKET_DATA_KEYS = ("key", "upload_url", "expires_in")

#: 通用应用收 `img_type=1` 时的那句（对外契约一部分；"参数不合法"档 ⇒ `1000`）。
#: 只对 `kind=general` 生效（差异登记：对标 `UploadImg` 有 `ImgType=1`，我们对通用应用不做）；
#: 锁定应用的对照组是 `WECOM_QR_IMG_TYPE_MSG`。
IMAGE_IMG_TYPE_MSG = "本接口只支持订单进度图（img_type=0）"

#: 锁定应用取票没传 `img_type=1` 的那句（§7.2 逐字；"参数不合法" ⇒ `1000`，码非 0 ⇒ 幂等占位行回滚、键可重试）。
#: ⛔ 不许做成静默按 `1` 兜底：缺省/`0` 必须响亮拒绝（D6），否则"锁定"退化成"随便传什么都给群码"。
WECOM_QR_IMG_TYPE_MSG = "本应用只能上传群聊二维码（img_type=1）"


class OrderImageTicketSerializer(serializers.Serializer):
    """`POST .../images/ticket/` 的入参（唯一校验口）。`ext` 必填 ≤8，取值由领域函数按 `ALLOWED_EXTS` 判（表外 ⇒ `1000` 与内网同一句 ⛔ 不另造）。
    `img_type` 可选默认 `0`、放行哪一档按应用用途分叉（判据在视图）；⛔ 不写成 `ChoiceField`（文案会变 DRF 内置、且 `choices` 按字段写、表达不了"按应用分叉"）；用 `IntegerField`（`"abc"`/`true` 在这里挡成 `1000` ⛔ 不进领域函数比较，`"0"` 与 `0` 都收）。"""

    ext = serializers.CharField(
        max_length=8, error_messages={"required": "缺少 ext", "blank": "ext 不能为空"}
    )
    img_type = serializers.IntegerField(required=False, default=0)


class OrderImageClaimSerializer(serializers.Serializer):
    """`POST .../images/` 的入参（唯一校验口）。`key` 必填=取票回的对象名，长度界=`OrderScreenshot.object_key` 列宽 255（超长在这里 `1000`，⛔ 不让它撞 PostgreSQL `value too long` ⇒ 500，E-0010）。
    `original_name` 可选 ≤255（与内网同界）；`remark` 可选 ≤**50**（落列宽 50，⛔ 别放宽成 255：那会让「请求能填、库里静默截断」）。"""

    key = serializers.CharField(
        max_length=255, error_messages={"required": "缺少 key", "blank": "key 不能为空"}
    )
    original_name = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    remark = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )


class OrderImageTicketView(ApiOrderWriteAssemblyMixin, APIView):
    """`POST /api/open/v1/orders/<order_no>/images/ticket/` —— 取上传票：`{ext, img_type}` ⇒ `{key, upload_url, expires_in}`；幂等必带 `Idempotency-Key`；领域=`issue_screenshot_ticket`（内网签发同一个函数）。
    `img_type` 按 `request._api_app.kind` 分叉（分叉表见模块 docstring/spec §7.2；判据读 `request._api_app` ⛔ 不按 `app_key` 再查一次库）；`kind` 出处=`ApiAppKind` ⛔ 不抄字面量；`expires_in` 取 storage 层现成值 ⛔ 不自己编数字（编的会与真签出去的 URL 时效分叉）。
    副作用=一行 `pending`（24h 清理任务收没人认领的行）⇒ 拿到票≠传完；「20 张」名额按图种分：`img_type=0` 占、`img_type=1` 不占（受"每单最多 1 张、认领时淘汰旧图"）——领域层既有事实，本模块不重判；锁定应用反复取票攒 pending 是 §9-13 如实登记的代价，⛔ 不加新闸。"""

    def check_upload_order(self, order_no):
        pass

    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, order_no):
        serializer = OrderImageTicketSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        img_type = serializer.validated_data["img_type"]
        if request._api_app.kind == ApiAppKind.WECOM_GROUP_QR:
            # ⭐ 锁定应用只收 `1`（群聊二维码；§7.2/D6）；⛔ 缺省/`0` 一律响亮拒绝（不静默按 `1` 兜底）。
            # 业务参数不合法 ⇒ `1000`；码非 0 ⇒ 幂等占位行回滚 ⇒ 同一颗幂等键下次可重试
            # （⛔ 不会永久卡在"正在处理中"）。
            if img_type != OrderScreenshot.IMG_GROUP_QR:
                return api_response(
                    data=None, msg=WECOM_QR_IMG_TYPE_MSG, code=ErrorCode.VALIDATION
                )
        elif img_type != OrderScreenshot.IMG_ORDER:
            # ⭐ 通用应用：§13.10-3 差异登记一字不动（对标有 `ImgType=1`，我们对通用应用不做）。
            # ⛔ 分支顺序别动：`elif` 让"不认识的 kind"落进这一档（fail-closed：将来第三种用途默认只收 `0`，
            # ⛔ 不会静默拿到群码放行）。
            return api_response(
                data=None, msg=IMAGE_IMG_TYPE_MSG, code=ErrorCode.VALIDATION
            )
        # ⚠️ `uploaded_by` = **创建该应用的员工**（"代理上传"，理由见模块 docstring）。
        # ⛔ 不置空、⛔ 不造假用户：它是 NOT NULL + PROTECT 外键。
        self.check_upload_order(order_no)
        outcome = issue_screenshot_ticket(
            order_no=order_no,
            ext=serializer.validated_data["ext"],
            img_type=img_type,
            uploaded_by=request.auth.created_by,
        )
        if not outcome.ok:
            # 领域层可预期失败原样出去；"超过 20 张"只对 `img_type=0` 成立（群码档走不到它）。
            # 「订单取不到」是 `Http404` ⇒ 经本仓异常处理器变 `1002`+HTTP 200，与对外读面同一个出口
            # （⛔ 不在这里改成 404）。
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        # ⚠️ **按白名单挑键**（⛔ 不是 `pop("screenshot_id")`）：白名单让"将来领域结果
        # 多出一个内部字段"**不会**静默出现在对外契约上（多一个键就是一次契约变更）。
        return api_response({name: outcome.data[name] for name in TICKET_DATA_KEYS})


class OrderImageClaimMixin(ApiOrderWriteAssemblyMixin):
    """`POST /api/open/v1/orders/<order_no>/images/` 的处理器（**认领**）。必须是 mixin（框架事实）：GET 列表已挂 `order_extra_views.OrderImagesView`、同路径第二条路由走不到（先例 `OrderStatusChangeMixin`/`OrderUpdateMixin`）；本 mixin 自带装配（宿主与本 mixin 装配值相同，无漂移）。
    入参 `{key, original_name, remark}`；成功=data=该图片项（与 GET 列表项同一装配出口 `image_row`）；领域=`claim_screenshot`（内网认领同一个函数）；逐条失败档（键不在/403 租户外/没传上来/0 字节/超 18 MiB/魔数/超 20 张/单不存在——全部来自共用实现 ⛔ 本模块不重判，完整表见 spec §9.2 与用例）。
    ⚠️ "同一上传人"闸只到员工层：同一员工建的另一个应用也能认领（如实登记边界）；⛔ 别的员工（含别的租户）认领不到。成功连带：行落 `ready`+两笔入队**按图种分开**（`img_type=0`：peer 广播+开放平台推送；`img_type=1`：只 peer 广播，D-0316），同一事务里要么都成要么都不成。"""

    def check_upload_order(self, order_no):
        pass

    @idempotent(spec=OPENAPI_SPEC)
    def post(self, request, order_no):
        serializer = OrderImageClaimSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.check_upload_order(order_no)
        outcome = claim_screenshot(
            order_no=order_no,
            # ⚠️ 对外按**对象名**选那一行（第三方手里只有 `key`；⛔ 我们没有把行号给出去）。
            object_key=serializer.validated_data["key"],
            original_name=serializer.validated_data["original_name"],
            remark=serializer.validated_data["remark"],
            uploaded_by=request.auth.created_by,
        )
        if not outcome.ok:
            # ⭐⭐ 拒收即清理四档（超限/0 字节/超 18 MiB/魔数认不出）必须声明「副作用已落库」（`writes_kept=True` ⇒ `mark_side_effect`）：
            # 它们删行+删对象，幂等入口对「码 ≠ 0」默认回滚，而 `storage.delete` 非事务 ⇒ 不声明就留下指向已删对象的行。口径照 `OrderCreateMixin.post`：
            # 判据="业务写入已落库且这次不打算回滚" ⛔ 不是"失败就标记"——不写库的失败档不声明、占位行照旧回滚（键可重试）。
            if outcome.writes_kept:
                mark_side_effect(request)
            return api_response(data=None, msg=outcome.msg, code=outcome.code)
        # ⚠️ 载荷走**列表项那一个出口**（`serializers.image_row`，从列表端点抽出来的
        # **同一段**代码）⇒ 「认领回来的那一项」与「列表里那一项」逐键同形（⛔ 不拼第二份）。
        # ⚠️ 它内部会预签一个读链接（生产 COS 整点对齐 1–2 小时），并读一次 `uploaded_by`（显示名要用它）。
        return api_response(image_row(outcome.row))


#: 两条端点的路径模板（供用例 import；⛔ 不放进 `urls.py`——那里只管路由。
#: 先例 = `order_write_views.STATUS_PATH_TEMPLATE`）。
TICKET_PATH_TEMPLATE = "/api/open/v1/orders/{order_no}/images/ticket/"
CLAIM_PATH_TEMPLATE = "/api/open/v1/orders/{order_no}/images/"
