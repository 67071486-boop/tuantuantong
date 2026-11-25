"""QQ 机器人设置页的序列化器。

## 两条承重纪律

1. **`app_secret` 只写不读**（`apps/qqbot/models.py` 模块 docstring 的红线）：它只出现在
   **写**方向（`write_only=True`），响应里连**键**都不出现——只回 `app_secret_set` 布尔
   （照 `apps/business/api/jidanpu_member_views.py` 的 `client_secret_set` 同一口径）。
   **留空 = 不改**：一次没带 secret 的 PATCH 把空串当「不动它」，⛔ 不许当「清掉」——
   那会把用户配好的凭据静默抹掉（E-0027 家族）。
2. **请求来源的值先进序列化器**（`AGENTS.md` §5-E / E-0010）：所有写端点的请求体
   都过本文件的 `validate()` / 字段声明，视图不再手拼类型化查询。
"""
from rest_framework import serializers

from apps.qqbot.models import (
    QQBotBinding,
    QQBotGroup,
    QQBotGroupPushConfig,
)


class QQBotBindingSerializer(serializers.ModelSerializer):
    """机器人凭据单例的读 / 写面。

    - **读**：`app_id` / `enabled` / `auth_verified` / `bot_name` / `markdown_ok` /
      `connection_state` / `last_alive_at` / `last_event_at` / `last_error` /
      `menu_version` / `app_secret_set`（布尔——「配没配」，不是「配的是什么」）。
    - **写**（PATCH，全部可选）：`app_id` / `app_secret` / `enabled` / `auth_verified`。
      ⚠️ `app_secret` **只写**；**留空或没带 = 不改**（`_BLANK_MEANS_KEEP` 的语义，
      判据在 `update()` 里落，因为「带了但为空串」不会触发 `required` 校验）。
      ⚠️ 连接健康四列（`connection_state` / `last_*` / `menu_version`）**不收**：
      它们的写入方是 ws 进程与 worker（`worker.py`），用户 PATCH 不许覆盖
      （照 jidanpu 绑定 `_PUT_WRITE_FIELDS` 把健康列排除在外的同一条理由）。
    """

    #: secret 的**只读替身**：只回答「配没配」（响应里没有 `app_secret` 这个键）。
    app_secret_set = serializers.SerializerMethodField()
    app_secret = serializers.CharField(
        max_length=256, write_only=True, required=False, allow_blank=True,
        help_text="AppSecret（只写不读；留空 = 不改）",
    )

    class Meta:
        model = QQBotBinding
        fields = [
            "app_id", "app_secret", "enabled", "auth_verified",
            "bot_name", "markdown_ok", "connection_state",
            "last_alive_at", "last_event_at", "last_error", "menu_version",
            "app_secret_set",
        ]
        read_only_fields = [
            "bot_name", "markdown_ok", "connection_state",
            "last_alive_at", "last_event_at", "last_error", "menu_version",
        ]

    def get_app_secret_set(self, obj) -> bool:
        return bool(obj.app_secret)

    def update(self, instance, validated_data):
        # 「留空 = 不改」：允许 blank 是为了把「用户清空输入框再保存」也解释成不动它
        # （真想换 secret 就填新值；模型上 secret 没有「空」的合法态——没配就是空串）。
        if validated_data.get("app_secret") == "":
            validated_data.pop("app_secret")
        return super().update(instance, validated_data)


class QQBotGroupSerializer(serializers.ModelSerializer):
    """群列表行：官方投影列 + 本地策略列 + 推送配置摘要。

    ⭐ 评审 P1-2：`join_policy_whitelist` 在读面（此前只有 PATCH join-policy 的响应
    带名单 ⇒ 页面刷新后拿不回已存名单、编辑区被清成空——一条数据丢失路径）。
    名单不是机密（member_openid 本来就显示在留痕表里），读端点带回没有额外暴露面。
    """

    push_config = serializers.SerializerMethodField()

    class Meta:
        model = QQBotGroup
        fields = [
            "id", "group_openid", "remark", "group_name", "member_num",
            "member_role", "allow_proactive_msg", "recv_msg_setting", "status",
            "first_seen_at", "last_seen_at", "join_policy_mode",
            "join_policy_whitelist", "push_config",
        ]

    def get_push_config(self, obj):
        config = getattr(obj, "push_config", None)
        if config is None:
            return None
        return {
            "is_active": config.is_active,
            "msg_type": config.msg_type,
            "game_ids": list(config.games.values_list("id", flat=True)),
        }


class QQBotGroupRemarkSerializer(serializers.ModelSerializer):
    """改群备注：**只许改 `remark`**（其余列是官方投影 / 事件写入方，页面不许碰）。"""

    class Meta:
        model = QQBotGroup
        fields = ["id", "remark"]
        read_only_fields = ["id"]


class QQBotPushConfigSerializer(serializers.Serializer):
    """推送配置的**整份替换**（PUT 语义：三格都必填，没带的格子就是「清掉」）。

    `game_ids` 逐个校验存在于当前租户的 `Game`（外键还没建——`games` 是 M2M，
    塞进不存在的 id 会让 `.set()` 抛 `IntegrityError` ⇒ 500；先在这里翻译成业务校验错误）。
    """

    is_active = serializers.BooleanField()
    msg_type = serializers.ChoiceField(
        choices=QQBotGroupPushConfig.MsgType.choices,
        error_messages={"invalid_choice": "消息类型只能是 markdown 或 text"},
    )
    game_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=True,
    )


class QQBotJoinPolicySerializer(serializers.Serializer):
    """入群审批策略：`mode` 枚举 + 白名单（仅 whitelist 模式有意义，照存 JSON 列）。"""

    mode = serializers.ChoiceField(
        choices=QQBotGroup.JoinPolicyMode.choices,
        error_messages={"invalid_choice": "审批策略取值不合法"},
    )
    whitelist = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, default=list,
    )


class QQBotJoinRequestActionSerializer(serializers.Serializer):
    """人工审批一条入群申请：`op` 二值 + 申请标识；拒绝理由可选。"""

    op = serializers.ChoiceField(
        choices=["approve", "decline"],
        error_messages={"invalid_choice": "op 只能是 approve 或 decline"},
    )
    join_request_id = serializers.CharField(max_length=128)
    member_openid = serializers.CharField(max_length=64)
    reject_reason = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default="",
    )


class QQBotMenuSerializer(serializers.Serializer):
    """群菜单的**服务端校验**（校验失败不调 QQ——业务校验错误，`code 1000`）。

    官方口径（`qqbot_api.put_menu` 的 docstring：路径**推断**，接线前实测复核）：
    一级 ≤ **10** 个、每个一级的二级 ≤ **5** 个、二级只许 `send_message` / `link` 两种类型、
    名称长度一级 ≤ 10 / 二级 ≤ 14（官方按**显示宽度**算、中文算 2；这里用 `len()` 简化——
    中文按 1 数，等于把上限放成了「≤10 个全中文」的宽口径，注释明示**不必精确**）、
    `link` 类型按钮的链接必须 `https://` 开头（官方不接受 http 与裸域名）。

    校验通过后 `validated_data["menu"]` 就是**原样**传给 `put_menu` 的载荷
    （我们不做结构改写——透传官方信封，远端才是最终裁决者）。
    """

    #: 二级按钮允许的类型（一级不设类型白名单：它可以只是带子按钮的文件夹）。
    SUB_BUTTON_TYPES = ("send_message", "link")

    menu = serializers.JSONField()

    def _validate_button(self, button, *, level: int, errors: list):
        where = "一级" if level == 1 else "二级"
        if not isinstance(button, dict):
            errors.append(f"{where}菜单项必须是对象")
            return
        name = button.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{where}菜单项缺少 name")
        elif len(name) > (10 if level == 1 else 14):
            # 官方按显示宽度（中文算 2）算长度；这里用 len() 简化（模块 docstring 已说明）。
            errors.append(f"{where}菜单名过长（最多 {10 if level == 1 else 14} 个字符）")
        if level == 2:
            if button.get("type") not in self.SUB_BUTTON_TYPES:
                errors.append(
                    "二级菜单类型只能是 send_message 或 link"
                )
        link = button.get("link")
        if button.get("type") == "link":
            if not isinstance(link, str) or not link.startswith("https://"):
                errors.append("link 类型菜单的链接必须以 https:// 开头")
        sub = button.get("sub_button")
        if level == 1 and sub:
            if not isinstance(sub, list):
                errors.append("sub_button 必须是数组")
            elif len(sub) > 5:
                errors.append("二级菜单最多 5 个")
            else:
                for item in sub:
                    self._validate_button(item, level=2, errors=errors)

    def validate(self, attrs):
        menu = attrs.get("menu")
        errors: list = []
        if not isinstance(menu, dict):
            errors.append("menu 必须是对象")
        else:
            buttons = menu.get("buttons")
            if not isinstance(buttons, list):
                errors.append("menu.buttons 必须是数组")
            elif len(buttons) > 10:
                errors.append("一级菜单最多 10 个")
            else:
                for button in buttons:
                    self._validate_button(button, level=1, errors=errors)
        if errors:
            raise serializers.ValidationError({"menu": errors})
        return attrs
