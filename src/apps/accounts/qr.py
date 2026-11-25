"""支付链接 → QR 码 PNG（base64）——打手 App 充值扫码用。为什么后端出图（业务方批准新增 `qrcode` + `Pillow`）：App 是 uni-app x，`app/` 下**没有 npm、也没有 canvas**，纯前端二维码库得人肉移植成 UTS（风险高、视觉 / 纠错行为不可控）；后端多两个纯 Python 依赖就能把「一张 PNG」直接交给客户端 ⇒ 客户端只负责 `<image src="data:image/png;base64,…">`。出参契约（⛔ 只有一种写法）：`qr_png_base64()` 返回**裸 base64**、**不带** `data:image/png;base64,` 前缀，客户端自己拼——不要两种都支持，同一件事两套形状客户端必有一处漏判（前缀是固定串，放客户端比放服务端更不容易被中间层二次编码）。
画法口径（写死在常量里，改它就是改客户端看到的码）：纠错级别 **M**（扫码场景常规档，脏一点 / 歪一点仍能读）；白边 **2 个模块**（库默认 4，按业务方口径收窄）；目标边长**约 300 px**——`box_size` 由「模块数 + 白边」反算，边长落在 250–330 之间而不是恰好 300（模块数是整数、不能半分）。⚠️ 代价（如实）：同样 300px 目标，内容越长模块数越多 ⇒ 单模块越小、越难扫；支付链接长度是上游给的，本模块不截断（截断就是一张扫不出钱的码）。⛔ 不要把输入链接写进日志：`codeUrl` 里的 `payData` 是**可付款的凭证**（带签名 / 订单参数），本模块**不记任何日志**；调用方失败时也只记异常**类名**，⛔ 不记 `pay_data`、不记异常消息（可能带输入片段）。
"""
from __future__ import annotations

import base64
import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M

#: 纠错级别（`qrcode.constants.ERROR_CORRECT_M` = 15% 冗余）。
QR_ERROR_CORRECTION = ERROR_CORRECT_M

#: 白边宽度（单位 = 模块数；库默认 4，本仓口径 2）。
QR_BORDER_MODULES = 2

#: 目标边长（px）。实际边长 = `(模块数 + 2 * 白边) * box_size`，只能落在它附近。
QR_TARGET_PIXELS = 300


def qr_png_base64(text: str) -> str:
    """把 `text` 画成二维码 PNG，返回**裸 base64**（无 `data:` 前缀）。
    ⚠️ 传入空串 / 非字符串会由 `qrcode` 自己抛（`ValueError` / `TypeError`）⇒ 由调用方决定是否吞掉；本模块**不**做「空输入返回空图」这类兜底（那会生成一张扫不出东西的图）。
    """
    qr = qrcode.QRCode(
        version=None,  # 让库按内容自动选最小版本（固定版本会让长链接直接溢出错）
        error_correction=QR_ERROR_CORRECTION,
        box_size=1,  # 先占位；`make()` 之后知道模块数了再反算
        border=QR_BORDER_MODULES,
    )
    qr.add_data(text)
    qr.make(fit=True)
    # ⚠️ `make_image()` 在**调用时**读 `self.box_size`（源码 `qrcode/main.py` 的
    # `make_image`）⇒ 在这里改它是一件安全的事，不用重建 QRCode。
    qr.box_size = max(1, QR_TARGET_PIXELS // (qr.modules_count + 2 * QR_BORDER_MODULES))
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")
