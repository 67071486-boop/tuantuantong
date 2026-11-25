"""订单截图打包下载（spec §7 / §11）。判据：`GET /api/business/orders/<order_no>/screenshots/zip`，节点 `order.download_screenshots_zip`。
⚠️ **不把整只 zip 攒在内存**（spec §7 的绑定条款）：`zipfile` 写进一个**不可 seek** 的 sink，生成器按块把字节交给 `StreamingHttpResponse`；⚠️ `_ZipSink` **故意不实现 `tell()`**——CPython 的 `zipfile` 在写不可 seek 的流时走 data descriptor 分支（这正是「边写边发」的前提），一旦实现 `tell()` 它会当成可 seek 并对整只 zip 做收尾寻址。
"""
import pathlib
import re
import zipfile
from urllib.parse import quote

from django.http import StreamingHttpResponse

from apps.business.api.screenshot_views import NodeGatedViewSet
from apps.business.models import OrderScreenshot
from apps.core.envelope import api_response
from apps.core.error_codes import ErrorCode
from apps.core.storage import get_storage

#: 打包下载的节点 code（对标 id 132）。⚠️ 模块常量：接线位点表的 T2 判据要能解析到它。
SCREENSHOT_ZIP_NODE = "order.download_screenshots_zip"


class _ZipSink:
    """把 `zipfile` 的输出按块交出来（⛔ 见模块 docstring：不实现 `tell()`）。"""

    def __init__(self):
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(data)
        return len(data)

    def flush(self) -> None:  # pragma: no cover - zipfile 只在关闭时调
        pass

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def _zip_chunks(rows, storage):
    """逐块产出 zip 字节。"""
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
        for index, row in enumerate(rows, 1):
            # 条目名：`001_<basename>`。⚠️ **必须取 basename**：`original_name` 是上传方给的，
            #    不洗净的话条目名可以是 `a/../../x`（zip-slip——解压方用不安全工具会写出目录）。
            #    `pathlib.Path(...).name` 同时把 `..` 与目录分隔符都吃掉。
            raw = row.original_name or pathlib.Path(row.object_key).name
            safe = pathlib.Path(raw).name or pathlib.Path(row.object_key).name
            zf.writestr(f"{index:03d}_{safe}", _read_all(storage, row.object_key))
            chunk = sink.drain()
            if chunk:
                yield chunk
    tail = sink.drain()
    if tail:
        yield tail


def _read_all(storage, key: str) -> bytes:
    with storage.open_stream(key) as fh:
        return fh.read()


def _zip_stem(order) -> str:
    """zip 落盘文件名的主体（⭐ D-0301）。取**展示单号**：`source_order_no` 非空取它、空回退 `order_no`（统一回退口径）。
    ⚠️ 来源号是**录入的自由文本**（最长 128、可含横杠 / 空格 / 路径分隔符等）⇒ **必须清洗**：只留 `\\w` / `.` / `-`，再截到 64 字符（原 20 位内置号不受影响）；清洗后为空（极端：来源号全是特殊字符）⇒ 回退内置号——**文件名永远非空**。⚠️ 文件名只含单号（不含租户名 / 人名）：给浏览器落盘用的，别顺手泄露业务信息。
    """
    raw = order.source_order_no or order.order_no
    cleaned = re.sub(r"[^\w.-]", "", raw)[:64]
    return cleaned or order.order_no


def _content_disposition(stem: str) -> str:
    """`Content-Disposition` 头的值（文件名 = `<stem>.zip`）。⚠️ **非 ASCII 文件名必须走 RFC 5987 的 `filename*`**：来源号是自由文本、可以是中文，直接塞 `filename="…中文…"` 会被 Django 整头 MIME 编码成 `=?utf-8?b?…?=`（浏览器不认，落盘名变成一串乱码）；纯 ASCII（内置号 / 西文来源号）照旧走 `filename="…"` 的老形状——与改前逐字同形，不惊动任何依赖它的下载方。
    """
    filename = f"{stem}.zip"
    if stem.isascii():
        return f'attachment; filename="{filename}"'
    return f"attachment; filename*=utf-8''{quote(filename)}"


class ScreenshotZipView(NodeGatedViewSet):
    node_code = SCREENSHOT_ZIP_NODE
    denied_message = "你没有打包下载订单截图的权限"

    def get_permissions(self):
        # ⚠️ **显式写在这里**（⛔ 不靠基类的 `self.node_code`）：接线位点表的 T2 判据要解析
        #    **本方法体**里的 `has_node(模块常量)`——只写在基类上它解析不到（实测会红）。
        from apps.rbac.permissions import has_node

        return [has_node(SCREENSHOT_ZIP_NODE)()]

    def download(self, request, order_no):
        rows = list(
            OrderScreenshot.objects.select_related("order").filter(
                order__order_no=order_no,
                # ⚠️ D-0144：跨关系 JOIN 不过 `Order` 的默认 manager ⇒ 软删单的图不打包
                order__deleted_at__isnull=True,
                status=OrderScreenshot.STATUS_READY,
            ).order_by("pk")
        )
        if not rows:
            return api_response(code=ErrorCode.VALIDATION, msg="该订单还没有截图")
        response = StreamingHttpResponse(
            _zip_chunks(rows, get_storage()), content_type="application/zip"
        )
        # ⭐ D-0301：文件名用展示单号（`_zip_stem` 已清洗；取数照旧按 URL 里的内置号——
        # 那是寻址键，⛔ 不变）。`select_related("order")` 让取来源号零额外查询。
        response["Content-Disposition"] = _content_disposition(_zip_stem(rows[0].order))
        return response
