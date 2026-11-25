"""实名认证第三方接口（腾讯云市场「姓名 + 身份证号」二要素核验）的客户端 —— 本线**唯一**出站点。

## 一、本模块的纪律：唯一出站点

第三方实名接口**只准在这一个模块里调**（照 `apps.openapi.outbound` 与 `apps.business.peer_client`
的既有纪律）。别处不准 `httpx.post` 到这个域名 —— 判成败的口径、超时、密钥的用法一旦散开就会各自漂移，
而**漂移是静默的**：没有任何一条用例会红，只有线上会偶尔把失败当成成功。

⚠️ **如实登记（现场 grep 全仓）**：本模块是**约定上的**唯一出站点 ——
**截至 尚无结构网钉住它**（全仓没有任何一条测试会因为"别处又写了第二个客户端"而变红）。
T5 会补一条结构网，但那条钉的是**配额扣减不得绕开 `quota.py`**，**不是钉这里**。
⇒ 这条纪律今天由本段注释 + 评审在管；要一条真的网得另写。

## 二、实测契约（⛔ 照抄，别猜；出处 `docs/RESEARCH-REALNAME-VENDOR.md` 与设计基线 §3）

| 项 | 值 |
|---|---|
| 端点 | `POST https://ap-shanghai.cloudmarket-apigw.com/service-olvzprl7/id_name/check` |
| 方法 | **只能 POST**（GET 会 404 —— 不是"最好用 POST"，是 GET 根本不通） |
| 参数 | `name` / `idcard` 放 **URL query**；body 必须是**空表单**（`data=""` + `application/x-www-form-urlencoded`） |
| 鉴权 | `Authorization` = **一段 JSON 字符串**：`{"id": "<key_id>", "x-date": "<GMT>", "signature": "<base64>"}` |
| 签名 | `base64(HMAC-SHA1(key=key_secret, msg="x-date: " + x_date))` —— ⛔ **只签 `x-date: <时间>` 这一行**，不签 method / path / body |
| 时间 | `%a, %d %b %Y %H:%M:%S GMT`（UTC，如 `Sat, 10 Oct 2026 01:59:55 GMT`） |
| 返回 | JSON：`{"msg": …, "requestid": …, "code": 200 / 400 / 1000 / 9999, "data": {…}}` |
| ⭐ `data.result` 的形状 | **JSON 数字（int）**：`1` 一致 / `2` 不一致 / `3` 库无 —— **在生产机用库里密钥真调实测**，原始回包 `{"msg":"成功","requestid":"<厂商流水号，此处占位>","code":200,"data":{"result":2,"message":"核验不一致"}}`（`result` 是 **int `2`**，不是字符串）。⚠️ 曾按**字符串**假设写过判据 ⇒ 线上「姓名与证件号完全一致也永远通不过实名」。⛔ **别再按字符串假设写判据**；字符串 `"1"/"2"/"3"` 只作为**向后兼容**（厂商将来改回字符串时仍认） |

## 三、⛔ 判成败**只看响应体里的 `code`**，⛔ **绝不许看 HTTP 状态码**

实测：`code:400`（参数错）挂在 **HTTP 200** 上 —— 看状态码会把"这个号本地校验就没过"
读成"核验成功"，而那一档**不消耗次数**，账面上却多记一次（`docs/specs/2026-10-10-realname-verification-design.md` §3）。
出处：`AGENTS.md` §5-E（第三方写接口不许拿松散解包判成败）；同类先例见 `apps/business/peer_client.py`
的 `_post_envelope`（读码时为上游文件第 44–60 行那一带，它开头就写着"不读 HTTP 状态码"，
理由是"本仓的失败也是 200 + 业务码"）。

⚠️ 本模块唯一读到 HTTP 状态码的地方是**"对方回的不是 JSON"那句人话里的排障线索** ——
它**不参与**任何判断（别把那条线索读成判据）。

## 四、失败一律**返回**，不抛（抛出去会变成一个 500）

超时 / DNS / 连不上 / 响应不是 JSON / 码不在册 —— 全部返回 `error_kind` 标好的 `VendorResult`，
由调用方（核验端点）决定"这一档扣不扣次数"并**照常写流水**（设计基线 §5 第 ⑦ 步）。
抛异常的话，那一次核验既不落流水、也不给用户任何答复，只剩一个 500（`AGENTS.md` §5-E / E-0010）。

## 五、密钥纪律（`key_secret` 是**全平台最贵的字符串**）

- ⛔ **不进**异常文本、日志、`__repr__`（本模块不写任何日志）；
- ⛔ 网络失败的人话里**不带** `str(exc)`：httpx 的异常文本里有**完整 URL**，而 URL 的 query
  里有姓名与身份证号 ⇒ 那会把 PII 灌进 `vendor_message`（要落库的列）和将来的日志；
- ⛔ 签名值本身也不外传：它是"已知明文 + 已知答案"的旁证，拿到就能离线试密钥
  （同族口径见 `apps/callback/signature.py` 的「不许做的三件事」）。

## 六、计费口径（调用方按 `ok` 判）

`ok=True` ⇔ `code == 200`（**"查得"是唯一可计费的档**：一致、不一致都算一次）。
`error_kind` 只区分"这一档是什么失败"，⛔ 不用它判扣不扣 —— 那是 T6 的口径（设计基线 §6-2b）。

⚠️ `ok=True` **不保证**有结果码：`code:200` 但 `data` 里没有合法的 `result`（缺失 / 未登记的 int 如 `9` /
浮点 `2.0` / 布尔 `true`/`false` / 未登记的字符串 / list / dict）时，
`ok` 照样是 `True`（我们确实被计费了，⛔ 不许谎报成失败），但 `result_usable=False`、`vendor_result=""`
⇒ 调用方必须落 `result="fail"` 且**照常算已消耗**（见 `VendorResult` 的字段说明）。

⚠️ **现行口径（实测）**：上面那句「缺失 / **数字** / 未登记值」曾把「是数字」整体当成
不可用。实测厂商正常档回的**就是 int `1/2/3`** ⇒ 那种写法会让**每一次真实核验**都落进这一档
（姓名与证件号完全一致也判 fail，而且照扣费）。⛔ 正确的边界是「**int 且 ∈ {1,2,3}** 可用」，
**bool 单独排除**（`True == 1`，见 `_from_response` 里的逐条判据）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from email.utils import formatdate

import httpx

#: 厂商端点。⛔ **只能 POST**（实测 GET 一律 404）。
VENDOR_URL = "https://ap-shanghai.cloudmarket-apigw.com/service-olvzprl7/id_name/check"

#: 默认超时（秒）。⛔ **必须显式交给 httpx**，不许靠 httpx 自己的默认值（它默认 5 秒、且随版本变，
#: 而"超时"决定了我们这一档算不算失败）。先例见 `apps/business/jidanpu.py` 的 `_new_http_client`
#: （读码时为上游文件第 644–662 行那一段：把 `config.timeout` 显式递进去，本行同款理由）。
DEFAULT_TIMEOUT_SECONDS = 10.0

#: 厂商业务码（⛔ 判成败只看它，见模块 docstring 第三节）。取值出处：设计基线 §3 的实测表。
CODE_OK = 200
CODE_INVALID = 400
CODE_VENDOR_ERRORS = (1000, 9999)

#: `error_kind` 的四个取值（与设计基线 §6 / 计划 T3 的接口逐字一致）。
KIND_OK = "ok"
KIND_INVALID = "invalid"
KIND_VENDOR_ERROR = "vendor_error"
KIND_NETWORK = "network"

#: ⭐ 唯一算得上**可判定结果码**的三个取值（spec §3：`data.result` 1=一致 / 2=不一致 / 3=库无）。
#: ⛔ 它是**白名单**（只有这三个）而不是黑名单：厂商将来加一个新值（如 `"4"`）时，
#: 我们要的是"拿不到可判定的结果"（`result_usable=False`）而不是"照它当已知结果用"。
#: ⚠️ 厂商**实测**回的是 **int**（`data.result` 是 JSON 数字）⇒ 字符串这一份是**给向后兼容**留的，
#: 归一化时 int 与 str 两种形状都按这份白名单认（见 `_from_response`）。
USABLE_RESULT_CODES = ("1", "2", "3")
_USABLE_RESULT_CODES = frozenset(USABLE_RESULT_CODES)
#: 同一份白名单的**整数**形态（⛔ 从上面那份派生，别手抄一份 —— 手抄会漂移）。
#: ⚠️ 用它做成员判断时**必须先排除 `bool`**：`isinstance(True, int)` 为真、且 `True in {1, 2, 3}` 也为真
#: ⇒ 漏了那一步，JSON 的 `true` 会被读成"一致"（`_from_response` 的判据顺序就是为它定的）。
_USABLE_RESULT_CODES_INT = frozenset(int(code) for code in USABLE_RESULT_CODES)


class VendorConfigError(ValueError):
    """调用参数不合法（**编程错误**，⛔ 不是厂商的失败档）。

    ⚠️ 只用于"压根不该发出去"的调用（凭据是空串）。消息里⛔ **不带**凭据本身，也⛔ **不 `raise … from e`**
    —— `from e` 会把带签名头的请求详情挂在异常链上（设计基线 §6-3）。
    """


@dataclass(frozen=True)
class VendorResult:
    """一次核验的结果（调用方按它写流水与扣减）。

    - `ok`：是否**查得**（`code == 200`，**唯一可计费**的档）；⭐ **成败判据只有它**；
    - `result_usable`：⭐ **不是成败判据**（成败判据是上面的 `ok`）—— 它只回答"这一档拿到了
      **可判定的结果码**吗"。`True` ⇔ `ok` 且 `result` 是 `1`/`2`/`3`（**实测厂商回的是 int**，
      也认字符串 `"1"/"2"/"3"`）；
      `False` ⇔ 查得了（已计费）但结果码缺失 / 形状不对（未登记的 int 如 `9`、浮点 `2.0`、
      布尔 `true`/`false`、未登记的值、`data` 里压根没有）。
      调用方（核验端点）拿到 `False` 时：**照 `ok` 扣次数**（我们确实被计费了），
      但**因为没有可判定的结果码，只能落 `result="fail"`**（⛔ 不许当成 pass，也⛔ 不许谎报成"没查得"）；
    - `vendor_code`：厂商 `code` 原值（形状不对、没拿到码时是 `0`，`ok` 一定为 `False`）；
    - `vendor_result`：`"1"` 一致 / `"2"` 不一致 / `"3"` 库无；其它档一律**归一化成空串**（见 `result_usable`）；
    - `vendor_message`：厂商的结果描述（优先 `data.message`，缺了退到顶层 `msg`）；
    - `vendor_request_id`：厂商 `requestid` —— 对账与工单的唯一凭据。⚠️ 参数错时厂商回**空字符串**（实测）；
    - `error_kind`：`"ok"` / `"invalid"` / `"vendor_error"` / `"network"`。
    """

    ok: bool
    result_usable: bool
    vendor_code: int
    vendor_result: str
    vendor_message: str
    vendor_request_id: str
    error_kind: str

    def __repr__(self) -> str:
        """显式列出允许外传的字段（⛔ 不用 dataclass 自动生成的那一份）。

        本类今天**没有**密钥字段，覆写看起来是多余的 —— 它是**承重的预防**：
        自动 repr 会把**将来新增的任何字段**原样印进日志 / 异常 / 测试失败信息，
        而这个类最可能新增的恰恰是"带签名的请求详情"那一类（为排障加的）。
        ⇒ 把 repr 钉在一张**显式白名单**上：新增字段不会**默认**外泄（设计基线 §6-3）。
        """
        return (
            f"VendorResult(ok={self.ok!r}, result_usable={self.result_usable!r}, "
            f"vendor_code={self.vendor_code!r}, "
            f"vendor_result={self.vendor_result!r}, vendor_message={self.vendor_message!r}, "
            f"vendor_request_id={self.vendor_request_id!r}, error_kind={self.error_kind!r})"
        )


def _gmt_now() -> str:
    """当前的 GMT 时间串（形如 `Sat, 10 Oct 2026 01:59:55 GMT`）。

    ⚠️ 用 `email.utils.formatdate(usegmt=True)`，⛔ **不用** `strftime("%a, %d %b %Y %H:%M:%S GMT")`：
    后者的 `%a` / `%b` 受**进程 locale** 影响（一个 `LC_ALL=zh_CN.UTF-8` 的机器会印出中文星期），
    而厂商是按字面串比时间的 ⇒ 那会变成"某些机器上签名偶尔不对"这种最难查的形状。
    两者的输出逐字相同（本模块的用例把格式钉住了）。
    """
    return formatdate(timeval=None, localtime=False, usegmt=True)


def _signature(key_secret: str, x_date: str) -> str:
    """按契约算签名：`base64(HMAC-SHA1(key=key_secret, msg="x-date: " + x_date))`。

    ⛔ **只签 `x-date: <时间>` 这一行** —— 不签 method、不签 path、不签 body、不签整段 Authorization JSON。
    这是实测契约（官方示例代码），不是"标准 API 网关那一套"（那种是 `hmac id=…, algorithm=…, headers=…`）：
    签多了**不会更安全**，只会让厂商那一侧算不出同一个值。

    ⚠️ 返回值（签名）⛔ **不许**外传（日志 / 异常 / 响应）：它是"已知明文 + 已知答案"的旁证。
    """
    message = f"x-date: {x_date}".encode("utf-8")
    digest = hmac.new(key_secret.encode("utf-8"), message, hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _headers(key_id: str, key_secret: str) -> dict[str, str]:
    """拼这一次请求的三个头（签名时间与 `Authorization` 里的 `x-date` **必须同一个串**）。"""
    x_date = _gmt_now()
    authorization = json.dumps(
        {"id": key_id, "x-date": x_date, "signature": _signature(key_secret, x_date)},
        separators=(",", ":"),
    )
    return {
        "Authorization": authorization,
        # ⚠️ 这个头**必须自己写**：实测 httpx 0.28.1 在 `data=""`（空表单）时**不会**自动补
        # `Content-Type`（只有 `data` 是 dict 时才会）⇒ 少了它，body 就不是契约要求的"空表单"了。
        "Content-Type": "application/x-www-form-urlencoded",
        # 契约里"可带"的 request-id（官方示例带的是小写 uuid1）。
        "request-id": str(uuid.uuid1()),
    }


def _kind_of(code: int) -> str:
    """把**响应体里的** `code` 映射成 `error_kind`（⛔ 与 HTTP 状态码无关）。

    ⚠️ **未登记的非 200 码一律归 `vendor_error`**（保守方向）：它们是"收到了信封、但码不在册"。
    归成 `network` 会让调用方读成"没查得、不扣费"，归成 `ok` 更糟 —— 那是把失败静默当成功
    （`AGENTS.md` §5-E / E-0087：调用不抛异常 ≠ 成功）。
    """
    if code == CODE_OK:
        return KIND_OK
    if code == CODE_INVALID:
        return KIND_INVALID
    return KIND_VENDOR_ERROR


def _network_result(message: str) -> VendorResult:
    """网络层失败的结果（`vendor_code=0`、`vendor_result=""`、`requestid=""` —— 我们什么都没拿到）。

    `result_usable=False`：连信封都没拿到，自然没有可判定的结果码。
    """
    return VendorResult(
        ok=False,
        result_usable=False,
        vendor_code=0,
        vendor_result="",
        vendor_message=message,
        vendor_request_id="",
        error_kind=KIND_NETWORK,
    )


def _from_response(response: httpx.Response) -> VendorResult:
    """解厂商的响应体。

    ⛔ **本函数不拿 HTTP 状态码判任何事**（唯一用到它的地方是下面那句"不是 JSON"的排障线索）：
    实测 `code:400` 就挂在 **HTTP 200** 上，拿状态码判会把参数错读成成功。
    出处：`AGENTS.md` §5-E；同类先例见 `apps/business/peer_client.py` 的 `_post_envelope`
    （读码时为上游文件第 44–60 行那一带）。
    """
    try:
        body = response.json()
    except ValueError:
        # ⛔ 不许把 `json.JSONDecodeError` 漏出去：它会在核验端点变成一个 500，
        # 而"对方回了个 HTML 错误页"是**可预期**的现象（网关 / WAF / 停机页都会这么干）。
        return _network_result(f"厂商回的响应不是 JSON（HTTP {response.status_code}，仅作排障线索）")
    if not isinstance(body, dict):
        # 契约的回包永远是一个信封对象；回了裸列表 / 裸字符串说明形状变了，⛔ 不静默当空值。
        return _network_result("厂商回的 JSON 不是一个信封对象（形状与契约不符）")

    code = body.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        # 连"哪一档"都判不出来 ⇒ 形状不对。⚠️ 不许用 `0` 之类的默认码假装读到了。
        return _network_result("厂商响应里没有可判的整数 code（形状与契约不符）")

    data = body.get("data")
    data = data if isinstance(data, dict) else {}
    vendor_result = data.get("result")
    # ⭐ 结果码**归一化**（spec §5 第 ⑦ 步的口径）。⭐ **实测契约**：厂商正常档回的 `data.result` 是
    # **JSON 数字（int）** `1`/`2`/`3`（在生产机用库里密钥真调，原始回包
    # `{"code":200,…,"data":{"result":2,"message":"核验不一致"}}`）—— ⛔ 不是字符串。
    # ⚠️ **R1 当年在这里写错过**（留作教训，⛔ 别再退回去）：R1 担心 `str(1) == "1"` 会把数字 `1`
    # 静默读成"一致"，于是要求 `isinstance(vendor_result, str)` ⇒ 结果是**真实响应的 `result_usable`
    # 永远是 `False`** ⇒ 上层照 `ok=True & result_usable=False` 落 `result="fail"` 并照扣
    # ⇒ **姓名与证件号完全一致的用户也永远通不过实名**。R1 担心的**方向**是对的（`bool` 是 `int` 的
    # 子类、`True == 1`！），但结论错了：正确做法是**接受 int 1/2/3 同时把 `bool` 单独排除**，
    # 而不是拒绝所有 int（`AGENTS.md` §5-E / E-0087「宽松转换」那条管的是"别猜着转换"，不包含"拒绝实测形状"）。
    #
    # 逐条判据（⛔ **顺序即判据，`bool` 必须在 `int` 之前**）：
    # ① `code != 200`              ⇒ 不可用、`vendor_result=""`；
    # ② `result` 是 **bool**（`true`/`false`）⇒ 不可用、`vendor_result=""`
    # （⚠️ 必须先判它：`isinstance(True, int)` 为真、且 `True in {1, 2, 3}` 为真 ⇒ 反过来写会把
    # JSON 的 `true` 读成"一致"，而形状不对时的唯一正确处置是 fail-closed）；
    # ③ `result` 是 **int** 且 ∈ `{1, 2, 3}` ⇒ **可用**，归一化成 `str(result)`；
    # ④ `result` 是 **str** 且 ∈ `{"1","2","3"}` ⇒ **可用**（厂商将来改回字符串时的向后兼容）；
    # ⑤ 其余（缺 `result` / 其它 int（如 `9`）/ 其它 str / 小数（`2.0` 是 float 不是 int）/
    # list / dict）⇒ 不可用、`vendor_result=""`。
    #
    # ⚠️ **归一化成字符串这条是给下游的契约**：`services._decide` 比的是 `vr.vendor_result == "1"`
    # （`RESULT_MATCH`）⇒ int 那一支必须 `str()` 出来，⛔ 别去改下游、也⛔ 别把 int 原样留在
    # `vendor_result` 里。`ok` 仍是 `True`（"查得"是事实，⛔ 不许谎报成失败）。
    if code != CODE_OK:
        # ① 非 200 档连"查得"都不是 ⇒ 没有可判定的结果码。
        usable = False
        vendor_result = ""
    elif isinstance(vendor_result, bool):
        # ② ⚠️ **必须在 int 之前**（`bool` 是 `int` 的子类）⇒ 这是本判据的杀手位，
        # 测试 `…json_true_result_is_unusable` 就是为它写的（删掉本支它必须红）。
        usable = False
        vendor_result = ""
    elif isinstance(vendor_result, int) and vendor_result in _USABLE_RESULT_CODES_INT:
        # ③ 实测形状：int 1/2/3 ⇒ 可用；归一化成字符串交给下游。
        usable = True
        vendor_result = str(vendor_result)
    elif isinstance(vendor_result, str) and vendor_result in _USABLE_RESULT_CODES:
        # ④ 向后兼容：厂商将来改回字符串也认（⛔ 不是"只认字符串"）。
        usable = True
    else:
        # ⑤ 其余一律不可用（⛔ 不猜着 `int()`/`str()` 转换：`2.0` 与 `2` 是两种形状）。
        usable = False
        vendor_result = ""
    # 查得时结果描述在 `data.message`；参数错 / 服务异常时 `data` 是空的，人话在顶层 `msg`。
    message = data.get("message") or body.get("msg") or ""
    kind = _kind_of(code)
    return VendorResult(
        ok=(kind == KIND_OK),
        result_usable=usable,
        vendor_code=code,
        vendor_result=vendor_result,
        vendor_message="" if message is None else str(message),
        # ⚠️ 参数错时厂商回**空字符串**（实测）⇒ 这里不许拿它当"一定有值"。
        vendor_request_id=str(body.get("requestid") or ""),
        error_kind=kind,
    )


def verify(
    name: str,
    id_card: str,
    *,
    key_id: str,
    key_secret: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> VendorResult:
    """查一次二要素（姓名 + 身份证号）；⛔ 不抛网络异常（见模块 docstring 第四节）。

    唯一会抛的是**编程错误**：凭据是空串。那说明调用方压根没把密钥取出来，
    真发出去只会拿到一个语焉不详的 `code:400`，把"我们的配置漏了"伪装成"这个号有问题"。
    异常里⛔ 不带 `key_secret`、⛔ 不 `raise … from e`（设计基线 §6-3）。
    """
    if not key_id or not key_secret:
        raise VendorConfigError("实名认证厂商凭据未配置（key_id / key_secret 都不能是空串）")

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                VENDOR_URL,
                params={"name": name, "idcard": id_card},  # 契约：这两个参数放 **query**
                data="",  # 契约：body 是**空表单**（配套的 Content-Type 在 `_headers` 里）
                headers=_headers(key_id, key_secret),
            )
    except httpx.TimeoutException:
        # ⚠️ 人话里**不带** `str(exc)`：httpx 的异常文本含完整 URL，而 query 里有姓名与身份证号。
        return _network_result(f"厂商接口超时（{timeout:g} 秒内没有拿到响应）")
    except httpx.HTTPError:
        # 连不上 / DNS 解析不了 / 传输层被掐 —— 都没拿到信封，一律"没查得"。
        return _network_result("连不上厂商接口（DNS 解析 / 连接 / 传输层失败）")
    return _from_response(response)
