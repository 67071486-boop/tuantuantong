"""实名认证的请求体序列化器（`AGENTS.md` §5-E / E-0010：请求来源的值进类型化的列/查询之前一律过序列化器）。

⚠️ 校验失败在本仓是 **HTTP 200 + `code=1000`**（`apps/core/exceptions.py::envelope_exception_handler`
把 DRF 的 `ValidationError` 翻成 `ErrorCode.VALIDATION`），⛔ 不是 400。
"""
from rest_framework import serializers

from apps.realname.models import RealNameCertRecord

#: 姓名 / 身份证号的列宽 —— ⚠️ **从落库那张表上取**（单一出处）：这两个值最终进的就是
#: `RealNameCertRecord.name` / `.id_card_no`，在这里再抄一份字面量迟早漂移（列改宽了、校验还卡着旧宽）。
NAME_MAX = RealNameCertRecord._meta.get_field("name").max_length
ID_CARD_MAX = RealNameCertRecord._meta.get_field("id_card_no").max_length


class RealNameVerifySerializer(serializers.Serializer):
    """`POST /api/auth/realname/verify/` 的请求体：`{name, id_card_no}`。

    ⚠️ **两个字段都是「先 `strip()`、再判空、再判长度」**（照任务书口径）。

    ⭐ **为什么把 DRF 的两个默认行为关掉**（`AGENTS.md` §5-E："防御性写法不要写成承重逻辑"）：

    - `allow_blank=True`：空值判定交给我们自己的 `validate_*`。留 `False` 的话
      DRF 会在**我们的校验之前**用 `blank` 报错 ⇒ 那两段**永远跑不到**（去掉它们用例照样绿），
      即"装饰性代码"；
    - `trim_whitespace=False` + **不写 `max_length`**：DRF 的长度校验发生在
      `to_internal_value`（那里才会 strip）**之后**、且用**未 strip 的原串**去比
      —— `" 110101…011 "`（前后各一个空格）会被长度校验拦掉，
      而"前后带空白应当被 strip 后接受"正是我们想要的语义（实测踩到过：
      第一版留着 `max_length`，`13b` 用例红在 `data is None`）。
      ⇒ 长度也在 `validate_*` 里、在 strip **之后**判。

    今天这两段是**承重**的：`name="   "`（全空白）与 `" 号 "`（前后带空白）
    两种输入的形状各有一条用例（`tests/test_realname_verify_api.py` 的 `13a` / `13b`），
    删掉 strip / 判空 / 判长度任何一句，都会有用例红。

    ⛔ **不做格式校验**：身份证号的合法性由 `apps/realname/idcard.py`（本地校验位算法）判，
    领域码是 `1000`。在这里再写一份「18 位数字 + 校验位」的正则 = 同一个事实两套判据，
    迟早漂移（而且正则拦不住校验位）。
    """

    name = serializers.CharField(allow_blank=True, trim_whitespace=False)
    id_card_no = serializers.CharField(allow_blank=True, trim_whitespace=False)

    def validate_name(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("姓名不能为空")
        if len(value) > NAME_MAX:
            raise serializers.ValidationError(f"姓名长度不能超过 {NAME_MAX} 个字符")
        return value

    def validate_id_card_no(self, value: str) -> str:
        value = value.strip()
        if not value:
            raise serializers.ValidationError("身份证号不能为空")
        if len(value) > ID_CARD_MAX:
            raise serializers.ValidationError(f"身份证号长度不能超过 {ID_CARD_MAX} 位")
        return value


class RealNameSettingSerializer(serializers.Serializer):
    """对标「实名认证相关设置」弹窗那 4 项（spec §4.2 的表 + §9.1 的 PATCH 端点）。

    ⚠️ **五个字段全部 `required=False`**：消费方是 `PATCH`（对标 `SaveCertSetting` 也是整份提交，
    但我们的口径是"只改传来的那几项"）⇒ 缺项 = **不改**，⛔ 不是"置 0 / 置 False"。
    ⛔ 因此**不许**给它们写 `default=`（那会让"没传"变成"改成默认值"，
    与"PATCH 只改传了的"直接冲突，而且是**静默**的）。

    ⚠️ 消费方（`PATCH /api/accounts/realname-setting/`）属于**下一批**任务；
    本类先于端点存在，是 T6 的交货项之一（任务书给定的文件清单）。
    """

    enabled = serializers.BooleanField(required=False)
    block_claim_without_cert = serializers.BooleanField(required=False)
    require_cert_for_settlement = serializers.BooleanField(required=False)
    min_age = serializers.IntegerField(required=False, min_value=0, max_value=150)
    max_age = serializers.IntegerField(required=False, min_value=0, max_value=150)

    def validate(self, attrs: dict) -> dict:
        """年龄门槛的两条判据（⛔ **不许用真值判断**，`AGENTS.md` §5-E / E-0021）。

        对标原文：「只允许指定范围区间的年龄用户通过实名…（为空或填 0 代表不限制）」
        ⇒ `0` 是**合法且有意义的边界**（= 不限制），不是"没填"。
        所以这里比的是 `!= 0`，而不是 `if min_age:` —— 后者在"值是 `None` 或负数"时
        会**静默跳过校验**（今天 `min_value=0` 挡住了负数，但那是另一条判据，
        两条判据各守各的、别互相顶替）。

        ⚠️ **PATCH 语义**：缺项要拿**实例现值**补齐（不是我拍一个默认值），
        否则「只改 `enabled`」的那次请求会因为"另一个字段没传"而绕过交叉校验。
        """
        instance = self.instance
        current_min = getattr(instance, "min_age", 0) if instance is not None else 0
        current_max = getattr(instance, "max_age", 0) if instance is not None else 0
        min_age = attrs["min_age"] if "min_age" in attrs else current_min
        max_age = attrs["max_age"] if "max_age" in attrs else current_max

        # 两个都是 0 ⇒ 不限制，跳过（⛔ 不是"0 视作没填"）。
        if min_age != 0 and max_age != 0 and max_age < min_age:
            raise serializers.ValidationError(
                f"最大年龄（{max_age}）不能小于最小年龄（{min_age}）；填 0 表示不限制"
            )
        return attrs
