"""鸡蛋铺回调 `sign` 的验签（第一版，按第三方文档原文口径实现）。

## 算法（对标文档原文）

> 签名由以上参数和授权秘钥（clientSecret）按照键自然排序进行 md5 加密，**空值不参与签名**

⇒ 待签串 = 把参数与 `clientSecret` 拼成 `k=v&k=v…`、**按 key 升序**、**空值不参与**，
再做 **MD5（小写 hex）**。

文档附了一个**完整算例**（这是我们唯一的权威对拍物，⛔ 不是自指断言）：

```
待签串 = action=10&clientSecret=<密钥>&orderNo=2025062409091188820&sourceOrderNo=54556512310200&status=已撤销&timestamp=1750859828
期望 sign = ce15d9cf983fb68da7bc1e5887487ea2
```

⚠️ 那个算例里的 `note` 是**空串**且**不出现**在待签串里 —— 这正是"空值不参与"的判据
（把它也算进去会得到另一个值）。算例的原始文本（含示例密钥）**不进仓库**，只在不进 git 的
`.tools/recon/` 下；对拍用例从那里读夹具，读不到就 `skip`（⛔ 不把密钥抄进 `src/**`）。

## 参与签名的是**报文里出现的所有顶层字段**（`sign` 自己除外；空值不参与）

这正是文档的口径：「签名由以上参数和授权秘钥（clientSecret）按照键自然排序进行 md5 加密」——
"以上参数"指的是**这条通知实际带的参数**，⛔ 不是某一份清单。

### ⭐ 现行口径（被**真机数据**证伪后定的）
原实现用一张**显式白名单**（`SIGNED_FIELDS` = 只算文档字段表里列出的那几个），理由是"未登记字段不参与 = fail-closed"。
**第一次接真回调就把这条证伪了**（`kexing` 上 236 条真实回调，总指挥当场统计）：
- `action=30`（订单有新截图）会带 **`imgUrl` / `name`** —— 文档的字段表里**没有**这两个，
  但**鸡蛋铺把它们算进了签名** ⇒ 那 **39 条全部** `sign_mismatch`；
- 而"把所有顶层标量字段都算进去"的**诊断**在**同样这 39 条上全部通过**
  ⇒ **100% 是漏算字段**，不是篡改、不是密钥错、也不是值类型问题。

原取舍的**两条前提都被证伪**，原文已在上面保留、逐条对照：
1. 它说"误判成签名不对的代价 = **少一条提醒**" —— 实际是**一整类通知（新截图）全部作废**，
   而"打手传了截图"恰恰是要人跟进的事（spec §3）。
2. 它暗示白名单**更安全** —— 恰恰相反：签名必须覆盖**发送方实际签的那一组字段**，
   少算只会**误拒真报文**；⛔ 它**挡不住任何伪造**（伪造者没有密钥，字段多算少算都过不了）。
   ⇒ **"算得比对方少"不是 fail-closed，是假阴性。**

### 白名单的残余用途：**只作诊断对照**
`SIGNED_FIELDS` 保留，但**不再是主判据**；主判失败时会顺手按它再算一次，
记进 `sign_ok_declared_fields_only`（**只取证、不参与状态码**）：
它 **True 而主判 False** ⇒ "对得上文档那一组、但对不上实际那一组"，
是"双方对'哪些字段参与'的理解不一致"的直接证据（本次 39 条就是这个形状）。

## ⛔ 本模块不许做的事

- 不读数据库、不读 settings、不发任何网络请求（纯函数 + `hashlib`）。
- **不返回也不记录待签串与算出来的签名**：待签串里含密钥，算出来的签名是一个
  "已知答案"式的旁证（拿到它就能离线试密钥）⇒ `verify_sign` 只回"过没过 + 为什么 + 用了哪些 key"。

## 证据等级（⛔ 别把下面两类混着读）

| 项 | 等级 | 出处 |
|---|---|---|
| key 升序 / **空值不参与** / MD5 小写 hex | **文档明写** | 文档正文 + 完整算例（判据见下） |
| 非字符串值怎么拼（`amount` 是数字时） | ⚠️ **推断**（文档没写） | `_scalar_text`（含**重新评估的触发条件**） |
| **参与主判的字段 = 报文里出现的所有顶层字段** | **文档明写**（"以上参数"）+ **真机数据佐证** | 本节上面那段（原白名单口径被 236 条真回调证伪） |
| `SIGNED_FIELDS`（文档字段表）**只作诊断对照** | **我们自定** | `sign_ok_declared_fields_only` 取证 |
| 收到值的大小写 / 首尾空白容忍 | **我们自定**（有意的单侧归一化） | `_normalise_provided_sign` |
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Mapping, NamedTuple

#: `sign` 自己**不参与**签名（它是签名结果）。
SIGN_FIELD_NAME = "sign"

#: 密钥在待签串里的 key 名（文档原文写的就是 `clientSecret`）。
SECRET_FIELD_NAME = "clientSecret"

#: 文档字段表里列出的那几个字段。⚠️ **它不再是主判据**（理由见模块 docstring）：
#: 真实回调里 `action=30` 会带 `imgUrl` / `name`，而鸡蛋铺**把它们算进了签名** ⇒ 只算这张表会**误拒真报文**。
#: 现在它**只用于诊断对照**（`sign_ok_declared_fields_only`）。
SIGNED_FIELDS: tuple[str, ...] = (
    "action",
    "amount",
    "note",
    "orderNo",
    "sourceOrderNo",
    "status",
    "timestamp",
)

#: 验签结论里的"为什么"（取值是稳定字符串 ⇒ 事件记录与用例都能逐字钉它）。
REASON_OK = "ok"
REASON_SIGN_MISSING = "sign_missing"
REASON_SIGN_MISMATCH = "sign_mismatch"
REASON_UNSIGNED_VALUE = "unsigned_value"
REASON_SECRET_UNAVAILABLE = "secret_unavailable"
#: ⭐ 新增（spec §5.4 第 2 条）：**新路径**（`/api/jidanpu/callback/<码>`）的码在
#: `shared/callback-keys.env` 里**取不到可用密钥**（没有这个码 / 那一行没有密钥）⇒ 回 **401**。
#: ⚠️ 与 `REASON_SECRET_UNAVAILABLE`（**503**：我们的文件读不到）**必须分得开**，
#: ⛔ 不许合成一个（接收器 spec §4.1 的既有裁决：503 说的是"我们坏了"、401 说的是"这条验不了签"）。
#: ⚠️ 它**不由** `verify_sign` 产生（验签压根没跑到），而是 `apps/callback/wsgi.py` 在
#: 取密钥那一步写进记录的 —— 放在这里是因为"记录的 `sign_reason` 有哪些取值"是这一族常量的家。
REASON_UNKNOWN_CALLBACK_CODE = "unknown_callback_code"


class UnsignableValue(ValueError):
    """某个参与签名的字段值不是标量（dict / list）⇒ 拼不出待签串。

    调用方按 **fail-closed** 处理（非 2xx + 记录），⛔ 不猜它的字符串形状。
    """


class SignCheck(NamedTuple):
    """验签结论。`ok` 决定 HTTP 状态码；其余字段只用于留痕与排查。"""

    ok: bool
    reason: str
    #: 实际参与签名的 key（升序）——⛔ 只有名字、没有值（值里可能含密钥）。
    signed_keys: tuple[str, ...]
    #: 取证用：**只按文档字段表**（`SIGNED_FIELDS`）重算一次的结果。没算 = None。
    #: 它 True 而主判 False ⇒ "对得上文档那一组、对不上实际那一组"（双方对参与字段的理解不一致）。
    ok_declared_fields_only: bool | None


def _scalar_text(value: Any) -> str:
    """把一个标量值变成拼进待签串的文本。

    ⚠️⚠️ **证据等级 = 推断（文档没写、我们自定）**：文档只写了"参数与密钥按 key 排序拼 `k=v`"，
    **没有说非字符串的值怎么拼**（`amount` 是"账单金额"，很可能以**数字**下发）。
    本实现自定的规则就两条：

    - 布尔 ⇒ 小写 `true` / `false`；
    - 数字（`int` / `float`）⇒ Python `str()` 的十进制写法（`0` → `"0"`、`12.5` → `"12.5"`）；
    - 其余非标量（dict / list）⇒ **拒绝**（抛 `UnsignableValue`，fail-closed，⛔ 不猜它的形状）。

    ⛔ **不许把上面这段读成"文档规定"** —— 它没有出处，是我们为了"能算出来"而定的口径。

    📌 **重新评估的触发条件（写下来，免得下次从零查起）**：
    若鸡蛋铺**开始发 `action=50`（账单金额变更）**而我们的验签**失败** ⇒ **先看这一条**：
    把当时收到的 `amount` **原样**（是字符串还是数字、几位小数）抄进本文件对应的用例里，
    再决定是改这条规则还是文档有别的口径；⚠️ 在此之前⛔ 不要"顺手改成 float 格式化"之类的猜测。

    ⛔ 判空**不看真值**：`0` / `"0"` / `False` 都是**有值**、必须参与签名
    （`AGENTS.md` §5-E 的 E-0021 家族：`if value:` 会把"传了 0"当成"没传"）。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    raise UnsignableValue(f"字段值是 {type(value).__name__}，不是标量 ⇒ 拼不出待签串")


def _present(value: Any) -> bool:
    """参与签名的判据 = **不是空值**（缺省 / `None` / `""`）。

    ⚠️ 用 `is None` 与 `== ""` 显式判，⛔ 不用真值 —— 见 `_scalar_text` 的注。
    """
    return value is not None and value != ""


def _presign_from(params: Mapping[str, Any], secret: str, names: tuple[str, ...] | None) -> str:
    """拼待签串。`names=None` ⇒ **用报文里出现的所有顶层字段**；否则只按给定的那几个。

    ⛔ **返回值含密钥** ⇒ 只许在内存里喂给 `compute_sign`，不许写日志 / 异常 / 记录。

    ⚠️ 两条 `skip` 的理由：`sign` 是签名结果本身；`clientSecret` 是**我们自己追加**的那一项
    （若报文里恰好也有同名字段，只留追加的那一个 —— 否则会出现两个同名项）。
    """
    if names is None:
        candidates = [(name, value) for name, value in params.items()]
    else:
        candidates = [(name, params.get(name)) for name in names]
    items: list[tuple[str, str]] = []
    for name, value in candidates:
        if name == SIGN_FIELD_NAME or name == SECRET_FIELD_NAME or not _present(value):
            continue
        items.append((name, _scalar_text(value)))
    if not secret:
        # 空密钥拼出来的串**看起来**像一条正常的待签串 ⇒ 在这里就断掉，别让它参与比较。
        raise UnsignableValue("密钥为空 ⇒ 拼不出待签串（调用方应当先判 SecretUnavailable）")
    items.append((SECRET_FIELD_NAME, secret))
    items.sort(key=lambda item: item[0])
    return "&".join(f"{key}={value}" for key, value in items)


def build_presign_string(params: Mapping[str, Any], secret: str) -> str:
    """**主判据**的待签串：报文里出现的**所有**顶层字段（除 `sign`；空值不参与）。

    ⭐ 改准：原来是"只算文档字段表里列出的那几个"，被真机 236 条回调证伪
    （`action=30` 带 `imgUrl` / `name`，鸡蛋铺把它们算进了签名）⇒ 详见模块 docstring。
    ⛔ 别改回白名单：**算得比对方少 = 误拒真报文**，挡不住伪造、只会漏掉真通知。
    """
    return _presign_from(params, secret, None)


def _declared_fields_presign_string(params: Mapping[str, Any], secret: str) -> str:
    """**诊断对照**用的待签串：只按文档字段表（`SIGNED_FIELDS`）。⛔ 不参与主判、不影响状态码。"""
    return _presign_from(params, secret, SIGNED_FIELDS)


def compute_sign(params: Mapping[str, Any], secret: str) -> str:
    """MD5（小写 hex）。⛔ 返回值同样"含密钥信息"（见模块 docstring）。"""
    presign = build_presign_string(params, secret)
    return hashlib.md5(presign.encode("utf-8")).hexdigest()


def _md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _normalise_provided_sign(provided: str) -> str:
    """归一化**对方给来的** `sign`（大小写 + 首尾空白）。⚠️ **这是有意的，不是顺手写的。**

    独立评审把它点成"单侧归一化"（`AGENTS.md` §5-G 的 E-0083 家族：只归一化一侧会让判据**恒真**），
    业务方 的裁决是 **保留、但必须显式登记**。两条理由与一条边界：

    1. **十六进制摘要的大小写是同一个值的两种写法**：`ce15D9…` 与 `ce15d9…` 是同一个 128 位摘要
       （MD5 的输出在**语义上**就是 16 个字节，hex 只是它的文本编码之一）。
       容忍它**不改变任何安全性质**：比较仍然是"逐字节比 32 个 hex 字符"，
       只是把"第三方用大写 hex 发过来"从**误拒**变成**通过**。
    2. **`.strip()` 容忍首尾空白**：防"报文里的值带了不可见空白（复制粘贴 / 某些 JSON 序列化器）
       ⇒ 明明算得对却被拒"。⚠️ 这与 E-0083 说的"单侧归一化"不是同一件事 ——
       那条讲的是**拿归一化后的一侧去比未归一化的期望值**（判据失去判别力）；
       这里**归一化的是"收到的那个字符串"这个输入本身**，两侧比的是同一个函数的输出。
    3. ⛔ **边界（承重，别扩）**：归一化**只作用于"对方给来的值"**、**只在这一处**（本函数是唯一定义）。
       ⛔ **不许**把它挪到 `compute_sign` / `build_presign_string` 那边去（那会改待签串本身、
       改的是**算法**而不是"容忍输入写法"），⛔ 也不许在别的地方再补一套 `.lower()` / `.strip()`。
       ⚠️ 它**不会**让任何"对不上"的签名变成通过：`expected` 侧一个字符都不动。

    判据（`src/tests/test_callback_receiver.py`）：大写 hex + 首尾空白必须通过；
    而**改坏一个字符**仍然必须失败（否则归一化就把判据吃掉了）。
    """
    return provided.strip().lower()


def verify_sign(params: Mapping[str, Any], provided: Any, secret: str | None) -> SignCheck:
    """验签并给出结论（**常量时间比较**）。

    `secret` 为 `None` / 空 ⇒ `reason=secret_unavailable`（fail-closed；
    调用方据此回 503，因为那是**我们的**配置问题、不是对方签错了）。

    ⚠️ **比较用 `hmac.compare_digest`**：`==` 会在第一个不同的字符上提前返回，
    给"逐字节试签名"留了一个可测量的时间侧信道（这里不是密码学意义上的高风险，
    但它是**白送的**代价——换个函数就没了）。
    ⚠️ 归一化**只对"对方给来的值"做一次**（`_normalise_provided_sign`，理由与边界见它的 docstring）。
    """
    if not secret:
        return SignCheck(False, REASON_SECRET_UNAVAILABLE, (), None)

    provided_text = provided if isinstance(provided, str) else ""
    if not provided_text:
        return SignCheck(False, REASON_SIGN_MISSING, (), None)

    try:
        expected = compute_sign(params, secret)
    except UnsignableValue:
        return SignCheck(False, REASON_UNSIGNED_VALUE, (), None)

    provided_normalised = _normalise_provided_sign(provided_text)
    ok = hmac.compare_digest(expected, provided_normalised)
    if ok:
        # ⭐ 主判据现在是"报文里出现的所有顶层字段" ⇒ 这里如实列出**实际进了待签串的那些 key**
        # （不是文档字段表 —— 那样会漏报 `imgUrl` / `name` 这类实际参与了签名的字段）。
        signed_keys = tuple(
            sorted(
                [name for name, value in params.items() if name != SIGN_FIELD_NAME and _present(value)]
                + [SECRET_FIELD_NAME]
            )
        )
        return SignCheck(True, REASON_OK, signed_keys, None)

    # 没通过 ⇒ 顺手按**文档字段表**再算一次（**只取证、不影响状态码**）：
    # 它 True 而主判 False ⇒ "对得上文档那一组、对不上实际那一组"（双方对参与字段的理解不一致）。
    ok_declared_fields_only: bool | None = None
    try:
        declared_presign = _declared_fields_presign_string(params, secret)
    except UnsignableValue:
        declared_presign = None
    if declared_presign is not None:
        ok_declared_fields_only = hmac.compare_digest(_md5_hex(declared_presign), provided_normalised)
    return SignCheck(False, REASON_SIGN_MISMATCH, (), ok_declared_fields_only)
