"""`OperationLog` 的形状与索引（spec §4.2）、动作登记表与写入出口（spec §5/§7.1）。"""
import inspect
import string
from datetime import datetime, timezone as dt_timezone
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import SimpleTestCase
from django_tenants.test.cases import TenantTestCase

from apps.audit import log
from apps.audit.actions import (
    ACTION_LOG_TYPES,
    ACTION_TEMPLATES,
    ORDER_ACTION_EXCLUDED,
    ORDER_ACTION_LABELS,
)
from apps.audit.log import RENDER_INPUTS, record, render_summary
from apps.audit.models import LogType, OperationLog


class OperationLogShapeTests(TenantTestCase):
    def test_log_type_has_exactly_the_eight_benchmark_codes(self):
        # 判据是**字面值**（E-0024），不是 len(LogType.choices)
        self.assertEqual(
            sorted(LogType.values),
            [5, 10, 20, 30, 40, 50, 60, 70],
        )

    def test_created_at_can_be_set_explicitly(self):
        """迁移要「保留原值」，就不能用 auto_now_add（spec §4.2）。"""
        past = datetime(2025, 1, 2, 3, 4, 5, tzinfo=dt_timezone.utc)
        row = OperationLog.objects.create(
            log_type=LogType.ORDER, action="order.transition", actor="甲",
            summary="将订单 X 的状态从A改为B", created_at=past,
        )
        row.refresh_from_db()
        self.assertEqual(row.created_at, past)

    def test_ordering_is_newest_first(self):
        for i in range(3):
            OperationLog.objects.create(
                log_type=LogType.ORDER, action=f"a{i}", actor="甲", summary=f"s{i}"
            )
        self.assertEqual(
            [r.action for r in OperationLog.objects.all()], ["a2", "a1", "a0"]
        )

    def test_expected_indexes_exist(self):
        """按**索引定义里的列名**断言，不按索引名。

        ⚠️ 判据不能写成「索引名里含 `log_type`」：Django 的自动索引名是
        `<table>_<field>_<hash>` 且**截断到 30 字符**（`audit_opera_log_ty_ab12cd`），
        子串断言会**假红**。这里查 `indexdef`（PostgreSQL 存的是完整列清单）。
        """
        with connection.cursor() as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = %s",
                [OperationLog._meta.db_table],
            )
            defs = [r[0] for r in cur.fetchall()]
        joined = " | ".join(defs)
        # 逐条断言「这一组列存在某个索引里」，字面列名
        self.assertIn("log_type", joined)
        for expected in (
            "(log_type, created_at)", "(actor, created_at)", "(target_kind, target_id)",
        ):
            self.assertTrue(
                any(expected in d for d in defs), f"缺复合索引：{expected}"
            )
        for single in ("order_no", "game_name", "action", "created_at"):
            self.assertTrue(
                any(f"({single})" in d for d in defs), f"缺单列索引：{single}"
            )


# spec 里标「做」的动作，逐条字面值（挂账 / 不做的不在内）。
# 条数不写在这里：权威是下面那条 len 断言的字面量，复述就会过期。
EXPECTED_ACTIONS = [
    # §5.1 10 订单相关
    "order.view_credentials", "order.open_edit", "order.change_taker_price", "order.claim",
    "order.create", "order.update", "order.transition", "order.throw_to_hall",
    "order.withdraw", "order.clear_taker", "order.refund",
    # 换销售客服归属（新拟，对标没有这个动作）。
    "order.change_sales_agent",
    # 复制成新订单。
    "order.copy",
    # 订单日志。
    "order.note",
    "order.wecom_chat_link",
    "order.penalty_submit",
    "order.penalty_approve",
    # 闲管家发货结果写进订单日志（新拟）。
    "order.ship_result",
    # 拆单组协作两条（新拟，依据见 `actions.py`）。
    "order.split_ship_hold",
    "order.split_main_commission_error",
    # 改收款状态（`POST …/payment-status/`，新拟）。
    "order.change_payment_status",
    # 改发单价（`POST …/hall-price/`，新拟——照 `order.change_taker_price` 句式，spec 未指定文案；
    #    不是照抄对标，取证见 `apps/audit/actions.py` 该常量处）。
    "order.change_offer_price",
    # 删单 / 恢复（照抄对标 `systemlog:10` 两条模板，logType=10）。
    "order.delete", "order.delete_screenshot", "order.restore",
    # 镜像单接单（新拟，端点在 `OrderViewSet.remote_claim`）。
    "order.jidanpu_remote_claim",
    # 方向二回拉的两条支线（新拟，服务在 `jidanpu_hall` 的 worker 里）：
    #    鸡蛋铺打手接了我方抛出的单 ⇒ 只按远端标识记账；我方打手接了 ⇒ 去鸡蛋铺撤下 / 退单。
    "order.jidanpu_remote_taken", "order.jidanpu_withdraw",
    # 对方打手接了单 ⇒ 我方自动接手（新拟，服务在 `jidanpu_member.poll_log` 里；
    #    与上面那条 `..._remote_taken` 刻意不同义——那条只记账不算钱、这条真接手并按我方绩效算钱）。
    "order.jidanpu_member_auto_claim",
    "order.jidanpu_direct_assign",
    # 下家收到上家撤单后落本地订单日志（新拟，模板逐字取自 spec §16.2；
    #    见 `apps/audit/actions.py::ORDER_PEER_WITHDRAW`）。
    "order.peer_withdraw",
    # 抢单转正的价差覆盖台账（新拟，机器动作；
    #    见 `apps/audit/actions.py::ORDER_PEER_PROMOTE_PRICE_OVERRIDE`）。
    "order.peer_promote_price_override",
    # 团团通↔团团通同步落库的 9 条新拟动作（见 `apps/audit/actions.py` 的 `PEER_INBOUND_*` 常量块）。
    #    ⚠️ 前缀刻意不是 `order.` ⇒ 不进 `ORDER_ACTION_LABELS` / `EXPECTED_ORDER_ACTION_CODES`
    #    （那张表的键集按 `order.` 前缀**恰好**钉住）。
    "peer.inbound_order", "peer.inbound_assign", "peer.inbound_grab",
    "peer.inbound_update", "peer.inbound_screenshot",
    "peer.inbound_hall_collect",
    "peer.hall_claim", "peer.upstream_status", "peer.upstream_screenshot",
    # 出站同步「已同步给谁」的机器行（新拟；写点 `peer_worker._attempt` 落 done 处）。
    #    同族前缀口径（不进 `order.` 恰好集合）。
    "peer.outbound_sync",
    # 导出订单（照抄对标 `systemlog:10`；端点 `GET api/business/orders/export`）。
    "order.export",
    # 推送自动退单与人话原因（新拟；已结账那一档用**独立动作码**，
    #    好让「系统操作日志」页单独筛出来，spec §D6 / R9）。
    "order.xgj_push_refund", "order.xgj_push_refund_settled",
    # 交易成功推送自动改「已结款」那条人话（新拟；独立动作码同上——系统操作日志可单筛）。
    "order.xgj_push_receipt",
    # 把本地单真发到代练通（新拟）。
    "order.dailiantong_push",
    # 撤回 / 改价 / 补时 / 留言（新拟）。
    "order.dailiantong_withdraw", "order.dailiantong_price",
    "order.dailiantong_time", "order.dailiantong_message",
    # 接手后自动发群二维码（新拟）。
    "order.dailiantong_qr",
    # 按规则发话术（可选带图）（新拟）。
    "order.dailiantong_auto_reply",
    # 确认完单 / 手动同步交易状态（新拟）。
    "order.dailiantong_accept", "order.dailiantong_sync",
    # 代练通「有、我们没用上」的动作（新拟）：加价 / 客服介入 /
    #    评价 / 上下架 / 锁单 / 拉黑 / 代练通侧订单要求模板。
    "order.dailiantong_up_price", "order.dailiantong_arbitration",
    "order.dailiantong_value", "order.dailiantong_off_sell",
    "order.dailiantong_lock", "order.dailiantong_blacklist",
    "order.dailiantong_template",
    # 详情页补充动作（新拟）：重发 / 补款 / 编辑平台单 / 追加截图。
    "order.dailiantong_repush", "order.dailiantong_amount",
    "order.dailiantong_edit", "order.dailiantong_progress",
    # 同意撤销（新拟；打手申请撤销后我方同意）。
    "order.dailiantong_agree_cancel",
    # 代练通无活跃订单 ⇒ 清占位接手人（机器行）。
    "order.dailiantong_clear_taker",
    # 订单群码自动拉取（马上来查码；机器行，后台线程写）。
    "order.malailai_qr_fetch",
    # 马上来查码配置保存（PATCH，改凭据与启停）。
    "system.order_qr_config_update",
    # 上家收款查询（机器行，同步线程写）。
    "order.upstream_receipt_query",
    # §5.2 20 绩效相关
    "performance.assign", "performance.reassign", "performance.adjust",
    "performance.adjust_manual", "performance.submit", "performance.review",
    "performance.settle", "performance.delete", "performance.export", "settlement.export",
    # 我的订单收入导出（新拟——对标该页导出直接落文件、没有日志原文可抄；
    #    不复用 `performance.export` 的理由见 `apps/audit/actions.py` 的 `MY_ORDERS_EXPORT` 常量处）。
    "my_orders.export",
    # 重算客服销售提成（新拟，对标没有这个动作；
    #    前端按钮的确认文案逐字取对标「生成或更新客服销售绩效账单」）。
    "performance.recompute_customer_commission",
    # §5.3 30 用户相关（R7：游戏权限拆成 add/remove 两个 code）
    "user.create", "user.update", "user.rename", "user.game_permission_add",
    "user.game_permission_remove", "user.role_change", "user.status",
    "user.status_batch", "user.reset_password",
    # 自助改资料端点 `PATCH api/auth/profile`（模板逐字照抄对标，
    #    见 `apps/audit/actions.py::USER_SELF_UPDATE`）。
    "user.self_update",
    # 「删除用户」落成软删（可恢复）——硬删仍然不做，但这条 action 有写入者；
    #    `user.restore` 为新拟。
    "user.delete", "user.restore",
    # ① `user.realname_verify` = 用户自己提交一次实名核验（落点
    #    `apps/realname/views.py::RealNameVerifyView.post`）；② `user.realname_reset` =
    #    管理员重置某个子账号的实名（落点 `apps/accounts/views.py::AccountViewSet.reset_realname`）。
    #    两条都不以 `order.` 开头 ⇒ `ORDER_ACTION_LABELS` 不动。
    "user.realname_verify", "user.realname_reset",
    # 人事作废一条自助注册链接（立即过期；落点
    #    `apps/accounts/selfreg_views.py::InviteRevokeView.post`，埋点登记见
    #    `tests/test_operation_log_ledger.py` 的 INSTRUMENTED）。
    #    不以 `order.` 开头 ⇒ `ORDER_ACTION_LABELS` 不动。
    "user.selfreg_revoke",
    # §5.4 40 系统相关
    "system.role_update", "system.role_destroy", "system.role_create", "system.role_copy",
    "system.game_create", "system.game_destroy", "system.game_update",
    "system.server_create", "system.server_update", "system.server_destroy",
    # 订单来源（照抄对标）/ 店铺对接（新拟），同属 40。
    "system.order_source_create", "system.order_source_update", "system.order_source_destroy",
    # 公司店铺分组（新拟；计划 `docs/plans/2026-10-26-order-source-groups.md` §3.2）。
    "system.order_source_group_create", "system.order_source_group_update",
    "system.order_source_group_destroy",
    "system.shop_binding_create", "system.shop_binding_update", "system.shop_binding_destroy",
    # 闲管家店铺对接（新拟），同属 40。
    "system.xgj_binding_create", "system.xgj_binding_update", "system.xgj_binding_destroy",
    "system.xgj_binding_sync_shops",
    # 编辑识别关键词（新拟，同属 40）。
    "finance.alipay_authorization",
    "finance.online_payroll",
    "system.recognition_keywords_update",
    # 改一个系统设置开关（新拟，同属 40）。
    "system.system_switch_update",
    # 账号新增 / 编辑 / 删除 + 全局设置保存（新拟，同属 40；模板与凭据纪律见
    #    `apps/audit/actions.py` 的 `SYSTEM_PLATFORM_INTEGRATION_*` / `SYSTEM_PLATFORM_SETTINGS_UPDATE` 段）。
    "system.platform_integration_create",
    "system.platform_integration_update",
    "system.platform_integration_destroy",
    "system.approval_settings_update",
    "system.platform_settings_update",
    # 规则新增 / 编辑 / 删除（新拟，同属 40；模板与「正文不进日志」的口径见
    #    `apps/audit/actions.py` 的 `SYSTEM_PLATFORM_AUTO_REPLY_*` 段）。
    "system.platform_auto_reply_create",
    "system.platform_auto_reply_update",
    "system.platform_auto_reply_destroy",
    # 规则图片上传（新拟，同属 40；⛔ 对象名 / 文件名 / 内容不进日志）。
    "system.platform_auto_reply_image_upload",
    # ⛔ `system.jidanpu_binding_create` / `_update` 已随租户级鸡蛋铺线整条拆除
    #    （`docs/plans/2026-10-08-remove-thirdparty-jidanpu-sync.md`），动作码不再可能产生
    #    ——恰好集合缺这两条是有意的，⛔ 不要补回。
    # 代练员个人绑定（新拟，同属 40；与已拆除的租户级那两条刻意不同名——两张表零关系）。
    "system.jidanpu_member_binding_create", "system.jidanpu_member_binding_update",
    # 客服个人绑定（新拟，同属 40；
    #    与上面两条 `jidanpu_*_binding_*` 刻意不同名——三张表零关系）。
    "system.jidanpu_upstream_binding_create", "system.jidanpu_upstream_binding_update",
    # 统一列表里删掉一条鸡蛋铺上家绑定（新拟，同属 40；
    #    模板照同组两条的句式，带 `：{target_name}`）。
    "system.jidanpu_upstream_binding_delete",
    # 页面绑定 / 解绑（新拟，同属 40；
    #    对标操作日志语料里没有「绑定上家系统」这类动作，见 `apps/audit/actions.py` 该常量处）。
    "system.peer_binding_create", "system.peer_binding_delete",
    # 改「同步上家抢单大厅」/「大厅价格百分比」两格（新拟，同属 40；
    #    模板见 `apps/audit/actions.py::SYSTEM_PEER_BINDING_UPDATE`）。
    "system.peer_binding_update",
    # 投递连续失败到阈值 ⇒ 自动停绑（新拟，同属 40；
    #    见 `apps/audit/actions.py::SYSTEM_PEER_BINDING_AUTO_REVOKE`）。
    "system.peer_binding_auto_revoke",
    # 创建 / 改（改名·停用启用·IP 白名单）/ 轮换密钥（新拟，同属 40；句式与凭据纪律见
    #    `apps/audit/actions.py::SYSTEM_OPEN_API_APP_*`）。⚠️ **轮换单独一条**：
    #    它是凭据事件（旧密钥当场失效），要能事后单独筛出来。
    #    ⛔ 本节恰好三条：「改权限 / 改游戏范围」两条已随对应配置 `PUT` 删除，⛔ 不要补回。
    "system.open_api_app_create", "system.open_api_app_update",
    "system.open_api_app_rotate_secret",
    # 管理页手工重投一条投递失败的推送（新拟，同属 40；与应用管理那族分家是因为它
    #    真的产生一次对外投递——见 `apps/audit/actions.py::SYSTEM_OPEN_API_DELIVERY_RETRY` 处的说明）。
    "system.open_api_delivery_retry",
    # 管理员处置一条反馈（新拟，同属 40；对标没有这个动作，句式照 `performance.review`，见
    #    `apps/audit/actions.py::FEEDBACK_HANDLE`）。⚠️ 动作码前缀是 `feedback.`，
    #    但分类按语义取 40（它是系统自身的运维动作，不落在任何业务域）。
    "feedback.handle",
    # 内部应用管理端点的三条写路径（新拟，同属 40；⛔ 与上家侧那一族**分开三条码**——
    #    两条通道的凭据是两张表、两套契约、两个页面，混成一个码会让日志页把它们读成同一件事，
    #    见 `apps/audit/actions.py::SYSTEM_TAKER_API_APP_CREATE` 处的说明）。
    "system.taker_api_app_create",
    "system.taker_api_app_update",
    "system.taker_api_app_rotate_secret",
    # 内部页手工重投一条投递失败的回调推送（新拟，同属 40；⛔ 与上家侧那条
    #    `system.open_api_delivery_retry` 分开一条码——两条通道的台账是两张表、
    #    收件人是两类人，见 `apps/audit/actions.py::SYSTEM_TAKER_API_DELIVERY_RETRY` 处的说明）。
    "system.taker_api_delivery_retry",
    # 保存机器人凭据 / 开关（新拟，同属 40；值为何不进日志，
    #    见 `apps/audit/actions.py::QQBOT_BINDING_UPDATE` 处的说明）。
    "system.qqbot_binding_update",
    # 保存企微自建应用配置（新拟，同属 40；值为何不进日志，
    #    见 `apps/audit/actions.py::SYSTEM_WECOM_CONFIG_UPDATE` 处的说明）。
    "system.wecom_config_update",
    "system.wxpusher_config_update",
    "system.internal_chat_directory_update",
    "system.internal_chat_visitor_availability",
    "system.internal_chat_visitor_transfer",
    "system.internal_chat_visitor_close",
    "system.internal_chat_notice_settings_update",
    # 绑定确认页的绑定 / 解绑（新拟，同属 40；
    #    同一 post 里运行时还会写 `_delete`，见 `apps/wechatmp/bind_views.py`）。
    "system.wechatmp_binding_create", "system.wechatmp_binding_delete",
    # §5.5 70 其它
    # 打手钱包 / 提现审核一族（新拟）；分类理由（8 个分类里没有「财务 / 钱包」⇒ 70 其它）
    #    见 `apps/audit/actions.py` 的 `WALLET_RELEASE` 段。
    "wallet.release", "wallet.withdraw", "wallet.deposit",
    # 解冻申请被「有单在跑」清空（新拟，同属 70 其它；
    #    与 `wallet.release` 分开一条码——那条记「申请」，这条记「作废」）。
    "wallet.release_request_voided_by_activity",
    "finance.withdraw_approve", "finance.withdraw_reject",
    "finance.withdraw_transfer", "finance.withdraw_refresh",
    "finance.withdraw_mark_paid",
    "finance.withdraw_interrupt",
    # 客服后台打手钱包管理 / 押金金额两条新拟动作（同属 70 其它；
    #    分类理由见 `apps/audit/actions.py::WALLET_ADJUST` 段）。
    "finance.taker_wallet_adjust", "finance.taker_deposit_rule_update",
    # 打手个人押金额度：开关 / 金额两条新拟动作（同属 70 其它；
    #    分类理由见 `apps/audit/actions.py::TAKER_DEPOSIT_QUOTA_SWITCH` 段）。
    "finance.taker_deposit_quota_switch", "finance.taker_deposit_quota_amount",
    "project.create", "project.update", "project.destroy",
    "category.create", "category.update", "category.destroy",
]

# 每个 action 的 log_type **逐条字面值**（spec §5 各表的分类列）。
# ⚠️ `performance.assign` → 20 与 `order.claim` → 10 是对标自己的不自洽分类，刻意照抄。
# 归类口径：`order.*` ⇒ 10、`performance.*` ⇒ 20、`user.*` ⇒ 30、`system.*` ⇒ 40、
# 钱包 / 其它 ⇒ 70；`order.*` ⇔ 10 的等价另有
# `test_order_prefixed_actions_are_exactly_the_log_type_ten_actions` 钉着。例外见各自注释。
EXPECTED_LOG_TYPES = {
    "order.view_credentials": 10, "order.open_edit": 10, "order.change_taker_price": 10,
    "order.claim": 10, "order.create": 10, "order.update": 10, "order.transition": 10,
    "order.throw_to_hall": 10, "order.withdraw": 10, "order.clear_taker": 10,
    "order.change_sales_agent": 10,
    "order.refund": 10,
    "order.copy": 10,
    # 订单日志：对标给不出分类依据，理由见 `apps/audit/actions.py` 的那一行注释。
    "order.note": 10,
    "order.wecom_chat_link": 10,
    "order.penalty_submit": 10,
    "order.penalty_approve": 10,
    # 闲管家发货结果：对标那条流的 `LogType` 实测恒 `null`，理由见 `apps/audit/actions.py` 的对应行。
    "order.ship_result": 10,
    "order.split_ship_hold": 10,
    "order.split_main_commission_error": 10,
    "order.change_payment_status": 10,
    "order.change_offer_price": 10,
    "order.delete": 10, "order.delete_screenshot": 10, "order.restore": 10,
    "order.jidanpu_remote_claim": 10,
    "order.jidanpu_remote_taken": 10, "order.jidanpu_withdraw": 10,
    "order.jidanpu_member_auto_claim": 10,
    "order.jidanpu_direct_assign": 10,
    "order.peer_withdraw": 10,
    "order.peer_promote_price_override": 10,
    # ⚠️ `peer.*` 9 条取 40、不是 10：`test_order_prefixed_actions_are_exactly_the_log_type_ten_actions`
    #    规定「非 `order.` 前缀的动作不得登记成 10」（它逼人显式决定「订单操作记录页收不收」）。
    #    本组裁定不收进那个页（那是「页面上的订单操作」），订单详情页按 `order_no` 照常可见。
    "peer.inbound_order": 40, "peer.inbound_assign": 40, "peer.inbound_grab": 40,
    "peer.inbound_update": 40, "peer.inbound_screenshot": 40,
    "peer.inbound_hall_collect": 40,
    "peer.hall_claim": 40, "peer.upstream_status": 40, "peer.upstream_screenshot": 40,
    # 出站同步机器行 ⇒ 40（与上面 8 条同族同裁定）。
    "peer.outbound_sync": 40,
    "order.export": 10,
    "order.xgj_push_refund": 10, "order.xgj_push_refund_settled": 10,
    "order.xgj_push_receipt": 10,
    "order.dailiantong_push": 10,
    "order.dailiantong_withdraw": 10, "order.dailiantong_price": 10,
    "order.dailiantong_time": 10, "order.dailiantong_message": 10,
    "order.dailiantong_qr": 10,
    "order.dailiantong_auto_reply": 10,
    "order.dailiantong_accept": 10, "order.dailiantong_sync": 10,
    "order.dailiantong_up_price": 10, "order.dailiantong_arbitration": 10,
    "order.dailiantong_value": 10, "order.dailiantong_off_sell": 10,
    "order.dailiantong_lock": 10, "order.dailiantong_blacklist": 10,
    "order.dailiantong_template": 10,
    "order.dailiantong_repush": 10, "order.dailiantong_amount": 10,
    "order.dailiantong_edit": 10, "order.dailiantong_progress": 10,
    "order.dailiantong_agree_cancel": 10,
    "order.dailiantong_clear_taker": 10,
    "order.malailai_qr_fetch": 10,
    "system.order_qr_config_update": 40,
    "order.upstream_receipt_query": 10,
    "performance.assign": 20, "performance.reassign": 20, "performance.adjust": 20,
    "performance.adjust_manual": 20, "performance.submit": 20, "performance.review": 20,
    "performance.settle": 20,
    "performance.delete": 20, "performance.export": 20, "settlement.export": 20,
    # 导出的是绩效行（打手自己的收入账单）⇒ 20；⛔ 不复用 `performance.export` 的码，
    #    理由见 `apps/audit/actions.py` 常量处。
    "my_orders.export": 20,
    "performance.recompute_customer_commission": 20,
    "user.create": 30, "user.update": 30, "user.rename": 30,
    "user.game_permission_add": 30, "user.game_permission_remove": 30,
    "user.role_change": 30, "user.status": 30, "user.status_batch": 30,
    "user.reset_password": 30,
    "user.self_update": 30,
    "user.delete": 30, "user.restore": 30,
    "user.realname_verify": 30, "user.realname_reset": 30,
    "user.selfreg_revoke": 30,
    "system.role_update": 40, "system.role_destroy": 40, "system.role_create": 40,
    "system.role_copy": 40, "system.game_create": 40, "system.game_destroy": 40,
    "system.game_update": 40, "system.server_create": 40, "system.server_update": 40,
    "system.server_destroy": 40,
    "system.order_source_create": 40, "system.order_source_update": 40,
    "system.order_source_destroy": 40,
    "system.order_source_group_create": 40, "system.order_source_group_update": 40,
    "system.order_source_group_destroy": 40,
    "system.shop_binding_create": 40, "system.shop_binding_update": 40,
    "system.shop_binding_destroy": 40,
    "system.xgj_binding_create": 40, "system.xgj_binding_update": 40,
    "system.xgj_binding_destroy": 40,
    "system.xgj_binding_sync_shops": 40,
    "finance.alipay_authorization": 40,
    "finance.online_payroll": 20,
    "system.recognition_keywords_update": 40,
    "system.system_switch_update": 40,
    "system.platform_integration_create": 40,
    "system.platform_integration_update": 40,
    "system.platform_integration_destroy": 40,
    "system.approval_settings_update": 40,
    "system.platform_settings_update": 40,
    "system.platform_auto_reply_create": 40,
    "system.platform_auto_reply_update": 40,
    "system.platform_auto_reply_destroy": 40,
    "system.platform_auto_reply_image_upload": 40,
    # ⛔ `system.jidanpu_binding_create` / `_update` 已随租户级鸡蛋铺线整条拆除
    #    （动作码不再可能产生；历史台账行照旧可读）。
    "system.jidanpu_member_binding_create": 40,
    "system.jidanpu_member_binding_update": 40,
    "system.jidanpu_upstream_binding_create": 40,
    "system.jidanpu_upstream_binding_update": 40,
    "system.jidanpu_upstream_binding_delete": 40,
    "system.peer_binding_create": 40, "system.peer_binding_delete": 40,
    "system.peer_binding_update": 40,
    "system.peer_binding_auto_revoke": 40,
    # ⛔ 应用管理恰好三条：「改权限 / 改游戏范围」两条已随对应配置删除，⛔ 不要补回。
    "system.open_api_app_create": 40, "system.open_api_app_update": 40,
    "system.open_api_app_rotate_secret": 40,
    "system.open_api_delivery_retry": 40,
    # 前缀是 `feedback.` 但按语义取 40（系统自身的运维动作，不落任何业务域）；
    #    分类依据与模板见 `apps/audit/actions.py::FEEDBACK_HANDLE`。
    "feedback.handle": 40,
    # 与上家侧那一族分开三条码（两条通道两张表两套契约），
    #    见 `apps/audit/actions.py::SYSTEM_TAKER_API_APP_CREATE` 那一族。
    "system.taker_api_app_create": 40, "system.taker_api_app_update": 40,
    "system.taker_api_app_rotate_secret": 40,
    # 与上家侧 `system.open_api_delivery_retry` 同形码刻意分家（两张台账、两类收件人）。
    "system.taker_api_delivery_retry": 40,
    "system.qqbot_binding_update": 40,
    "system.wecom_config_update": 40,
    "system.wxpusher_config_update": 40,
    "system.internal_chat_directory_update": 40,
    "system.internal_chat_visitor_availability": 40,
    "system.internal_chat_visitor_transfer": 40,
    "system.internal_chat_visitor_close": 40,
    "system.internal_chat_notice_settings_update": 40,
    "system.wechatmp_binding_create": 40, "system.wechatmp_binding_delete": 40,
    # 钱包 / 提现一族归 70 其它：8 个分类里没有「财务 / 钱包」，
    #    分类理由见 `apps/audit/actions.py` 的 `WALLET_RELEASE` 段。
    "wallet.release": 70, "wallet.withdraw": 70, "wallet.deposit": 70,
    "wallet.release_request_voided_by_activity": 70,
    "finance.withdraw_approve": 70, "finance.withdraw_reject": 70,
    "finance.withdraw_transfer": 70, "finance.withdraw_refresh": 70,
    "finance.withdraw_mark_paid": 70,
    "finance.withdraw_interrupt": 70,
    "finance.taker_wallet_adjust": 70, "finance.taker_deposit_rule_update": 70,
    "finance.taker_deposit_quota_switch": 70, "finance.taker_deposit_quota_amount": 70,
    "project.create": 70, "project.update": 70, "project.destroy": 70,
    "category.create": 70, "category.update": 70, "category.destroy": 70,
}

# 「交易订单操作记录」页「操作说明」列（`ORDER_ACTION_LABELS`）的键集——**逐条字面值**
# （交易订单操作记录线 spec D6 / §7-11）。
# ⚠️ ⛔ **不从 `ACTION_LOG_TYPES` / `ACTION_TEMPLATES` 派生**：从登记表派生是**自指恒真**，
#    把 `ORDER_ACTION_LABELS` 改坏（少一条 / 多一条 / 拼错一条）时它照样绿（E-0024 / E-0065）。
# ⚠️ 本集合刻意**只列 code、不列文案**：文案是给业务看的短名，改文案不该红；
#    但**加/删一个 `order.*` 动作码必须同批决定它的短名**（否则本集合不等）。
# ⚠️ 条数不在这里推算（⛔ 不推算）：当场实测，判据是 `test_registry_has_exactly_…`
#    与下面两条等号断言一起报出来的。
EXPECTED_ORDER_ACTION_CODES = frozenset({
    "order.view_credentials", "order.open_edit", "order.change_taker_price",
    "order.change_payment_status", "order.change_offer_price",
    "order.claim", "order.create", "order.update", "order.transition",
    "order.throw_to_hall", "order.withdraw", "order.clear_taker", "order.refund",
    "order.change_sales_agent",
    "order.copy", "order.note", "order.wecom_chat_link", "order.penalty_submit", "order.penalty_approve", "order.ship_result",
    # 拆单组协作两条（新拟）。
    "order.split_ship_hold", "order.split_main_commission_error",
    "order.delete", "order.delete_screenshot", "order.restore",
    "order.jidanpu_remote_claim", "order.jidanpu_remote_taken",
    "order.jidanpu_withdraw", "order.jidanpu_member_auto_claim", "order.jidanpu_direct_assign",
    "order.peer_withdraw", "order.xgj_push_refund",
    "order.xgj_push_refund_settled", "order.export",
    # 交易成功自动收款（新拟）。
    "order.xgj_push_receipt",
    # 转正价差覆盖（新拟）。
    "order.peer_promote_price_override",
    # 抛到代练通（新拟）。
    "order.dailiantong_push",
    # 撤回 / 改价 / 补时 / 留言（新拟）。
    "order.dailiantong_withdraw",
    "order.dailiantong_price",
    "order.dailiantong_time",
    "order.dailiantong_message",
    # 接手后自动发群二维码（新拟）。
    "order.dailiantong_qr",
    # 按规则发话术（可选带图）（新拟）。
    "order.dailiantong_auto_reply",
    # 确认完单 / 手动同步交易状态（新拟）。
    "order.dailiantong_accept",
    "order.dailiantong_sync",
    # 代练通「有、我们没用上」的动作（新拟）。
    "order.dailiantong_up_price",
    "order.dailiantong_arbitration",
    "order.dailiantong_value",
    "order.dailiantong_off_sell",
    "order.dailiantong_lock",
    "order.dailiantong_blacklist",
    "order.dailiantong_template",
    # 详情页补充动作（新拟）：重发 / 补款 / 编辑平台单 / 追加截图。
    "order.dailiantong_repush",
    "order.dailiantong_amount",
    "order.dailiantong_edit",
    "order.dailiantong_progress",
    # 同意撤销（新拟）。
    "order.dailiantong_agree_cancel",
    "order.dailiantong_clear_taker",
    # 订单群码自动拉取（新拟）。
    "order.malailai_qr_fetch",
    # 上家收款查询（新拟）。
    "order.upstream_receipt_query",
})

# 渲染器能提供的**全部**输入键（spec §5.9 + R8/R9/R11 的列清单 + 审计日志线 §3.2）。
# 模板引用了这个集合之外的键 ⇒ 红；集合本身还要与 `render_summary` 的形参对得上。
# ⚠️ `field_label` 不是形参：它由 `field_name` 查静态标签表派生（R8/R9）。
# ⚠️ `actor_name` 是模板里渲染的**显示名**；`actor_id` 只作「这行是不是人为」的判定、
#    不参与文本 ⇒ 任何模板都**不该**引用它。
RENDERER_COLUMNS = {
    "actor", "actor_id", "actor_name", "target_id", "target_name", "order_no",
    "source_order_no", "game_name", "game_role_name", "field_name", "field_label",
    "from_value", "to_value", "reason",
}

# 「会被模板渲染的列 → (用它的一条 action, 送入的值, 该值在 summary 里的形态)」。
# `field_name` 是特例：列里存**真实列名**、summary 里显示**中文标签**（R8/R9）。
# 「`record()` 漏转发 `game_name` ⇒ 日志行里游戏名消失」这类缺陷由这张表抓。
# 表格主动覆盖 `actor_name`（`record()` 里它由 `actor_id` 派生 ⇒ 只有**真实用户**喂得进去，
#    见用例里的那一支特例）；`actor_id` **不在**本表——它不被任何模板渲染（只作判定输入），
#    由用例的豁免集合显式登记。
RENDERED_COLUMN_CASES = {
    "actor": ("performance.assign", "甲", "甲将订单指派给"),
    "actor_name": ("performance.assign", "测试昵称甲", "测试昵称甲将订单指派给"),
    "target_id": ("user.create", "7", "ID：7"),
    "target_name": ("performance.assign", "张三", "指派给张三"),
    "order_no": ("order.transition", "20260921000000000001", "订单 20260921000000000001 的状态"),
    "source_order_no": ("order.create", "TB-1", "来源单号：TB-1"),
    "game_name": ("user.game_permission_add", "王者荣耀", "新增用户游戏权限：王者荣耀"),
    "field_name": ("order.update", "total_price", "订单总价"),
    "from_value": ("order.transition", "待派单", "从待派单改为"),
    "to_value": ("order.transition", "服务中", "改为服务中"),
    "reason": ("performance.review", "备注文本", "备注：备注文本"),
}

#: `RENDERER_COLUMNS` 里**不参与**「由模板渲染出来」的键（显式枚举，⛔ 不用理由文本当后门）：
#: - `field_label` 由 `field_name` 派生（R8/R9），不是形参；
#: - `game_role_name` 今天没有任何模板渲染它（它的「转发」由 spy 用例覆盖）；
#: - `actor_id` 是「人为 / 机器」的判定输入，不参与 `format_map` 的文本（spec §3.2）。
NON_RENDERED_RENDERER_COLUMNS = {"field_label", "game_role_name", "actor_id"}



class ActionRegistryTests(SimpleTestCase):
    """登记表本身（spec §5 + R7）。纯字典，不碰数据库。"""

    def test_registry_has_exactly_the_instrumented_actions(self):
        # 条数和清单都是写死字面量，不从登记表派生（派生会恒真）。
        # 加或删一个动作，下面几条一起红；按本网自己报出的差改字面量，不要拿历史数字相加。
        # 新增动作必须同时改 EXPECTED_ACTIONS 与 EXPECTED_LOG_TYPES。
        self.assertEqual(sorted(ACTION_TEMPLATES), sorted(EXPECTED_ACTIONS))
        self.assertEqual(len(ACTION_TEMPLATES), 182)

    def test_log_type_map_agrees_key_for_key_with_the_template_registry(self):
        # 键少一个/多一个/拼错一个都必须红
        self.assertEqual(sorted(ACTION_LOG_TYPES), sorted(ACTION_TEMPLATES))
        self.assertEqual(ACTION_LOG_TYPES, EXPECTED_LOG_TYPES)

    def test_registry_keeps_the_two_counter_intuitive_benchmark_boundaries(self):
        self.assertEqual(ACTION_LOG_TYPES["performance.assign"], 20)
        self.assertEqual(ACTION_LOG_TYPES["order.claim"], 10)

    def test_order_action_labels_cover_exactly_the_order_prefixed_actions(self):
        """短名表键集是手写字面量，不从登记表派生。

        `EXPECTED_ORDER_ACTION_CODES` 是登记表口径，`ORDER_ACTION_LABELS` 是本页口径。
        差集由 `ORDER_ACTION_EXCLUDED` 显式给出（`order.view_credentials` 不进本页）。
        少一条、多一条、拼错或排除集漂移都会红。条数断言防「两边都空、等号仍成立」。
        """
        self.assertEqual(len(EXPECTED_ORDER_ACTION_CODES), 60)
        self.assertEqual(
            set(ORDER_ACTION_LABELS),
            set(EXPECTED_ORDER_ACTION_CODES) - ORDER_ACTION_EXCLUDED,
        )
        self.assertEqual(len(ORDER_ACTION_LABELS), 59)

    def test_order_action_labels_agree_key_for_key_with_the_order_prefixed_registry(self):
        """标签表 ⇔ **登记表 − 排除集**：**两侧都派生**地相等（spec §10.3；排除集见 §11）。

        ⚠️ 与上面那条是两种钉子、都要有：上面守「= 那份手写字面量清单」，本条守
        「生产侧有登记（且没被排除）就得有短名」——曾实测：登记面加了码而标签表不动时，
        端点为那一行回的是**原始动作码**、下拉里也没有它，而全套照绿。
        两侧都是派生 ⇒ 不是自指恒真；并列长度断言是自证非空；
        ⚠️ `ORDER_ACTION_EXCLUDED` 必须真的落在前缀集合里（本方法最后那条）：
        否则「排除一个不存在的码」会让两边同时少一条也照样相等——恒真判据。
        """
        prefixed = {code for code in ACTION_TEMPLATES if code.startswith("order.")}
        self.assertEqual(len(prefixed), 60)
        self.assertTrue(
            ORDER_ACTION_EXCLUDED <= prefixed,
            f"排除集里有不在 order.* 前缀集合里的码：{sorted(ORDER_ACTION_EXCLUDED - prefixed)}",
        )
        self.assertEqual(set(ORDER_ACTION_LABELS), prefixed - ORDER_ACTION_EXCLUDED)

    def test_order_action_labels_are_all_non_empty(self):
        """值非空（spec §7-11 / D6）——空短名等于那一列没内容，用户分不清动作。

        ⚠️ 判据用 `.strip()`：只有空白的串同样不可读。
        """
        blanks = sorted(
            code for code, label in ORDER_ACTION_LABELS.items() if not label.strip()
        )
        self.assertEqual(blanks, [], f"这些动作码的短名是空的：{blanks}")

    def test_order_prefixed_actions_are_exactly_the_log_type_ten_actions(self):
        """**D1 的等价性钉**（交易订单操作记录线 spec D1 / §8-5）。

        钉住「`order.*` 恰好都登记成 `log_type=10`」：将来加一个**非 `order.*` 的 10 类动作**
        （或把某条 `order.*` 改成非 10）时本网会红，逼人决定那个动作要不要收进订单操作记录页。
        ⚠️ 两侧都是派生的 ⇒ 不是自指；并列长度断言是自证非空（条数当场实测，⛔ 不推算）。
        """
        ten = {code for code, log_type in ACTION_LOG_TYPES.items() if log_type == 10}
        prefixed = {code for code in ACTION_TEMPLATES if code.startswith("order.")}
        self.assertEqual(ten, prefixed)
        self.assertEqual(len(ten), 60)

    def test_suspended_and_excluded_actions_are_not_registered(self):
        # ⚠️ 挂账 / 「不做」的动作**不能**出现在 `ACTION_TEMPLATES` 里（没埋点的动作不许有文案，
        #    否则渲染器会替它背书）。
        # ⚠️ 动作上线埋点后从本名单移除、登记进 `EXPECTED_ACTIONS` 正向名单；
        #    挂账表一侧同步删（去向见 `tests/test_operation_log_ledger.py` 的 `DEFERRED_ACTIONS`）。
        for code in (
            "order.change_price_no_assign",
            "user.switches", "user.rate_stat",
        ):
            self.assertNotIn(code, ACTION_TEMPLATES)

    def test_one_code_means_exactly_one_template(self):
        for action, template in ACTION_TEMPLATES.items():
            self.assertIsInstance(template, str, action)
            self.assertTrue(template.strip(), action)

    def test_every_template_placeholder_is_a_renderer_input(self):
        """§5.9：占位符必须落在渲染器**能提供**的那组键里。

        「模板引用了不存在的键」与「渲染器永远填不上的键」都在这一条上红。
        """
        seen = set()
        for action, template in ACTION_TEMPLATES.items():
            for _, field, _, _ in string.Formatter().parse(template):
                if field is None:
                    continue
                seen.add(field)
                self.assertIn(field, RENDERER_COLUMNS, f"{action} 用了行外变量 {{{field}}}")
        self.assertTrue(seen, "解析不到任何占位符 ⇒ 这条断言退化成恒真")

    def test_renderer_supplies_exactly_the_documented_inputs(self):
        """渲染器的形参集必须**恰好**是上面那组键（少一个/多一个都红）。

        `field_label` 是唯一由渲染器**派生**的键（`field_name` → 标签表），故不在形参里；
        已经不收 `**_` ⇒ 调用方多传一个键（例如自己拼的 `field_label`）会 `TypeError`。
        """
        parameters = list(inspect.signature(render_summary).parameters.values())
        self.assertFalse(
            any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters),
            "render_summary 不该再有 **_：它让调用方能覆盖派生值",
        )
        params = {
            p.name for p in parameters if p.kind is inspect.Parameter.KEYWORD_ONLY
        }
        self.assertEqual(params | {"field_label"}, RENDERER_COLUMNS)

    def test_caller_cannot_supply_the_derived_field_label(self):
        """R8/R9 的边界：`field_label` 只能由 `field_name` 派生，调用方传不进来。"""
        with self.assertRaises(TypeError):
            render_summary(
                "order.update", actor="甲", order_no="X", field_name="total_price",
                from_value="1", to_value="2", field_label="我自己拼的",
            )

    def test_adjust_template_registers_the_intentional_deviation(self):
        """R9：`performance.adjust` **有意**不用对标那句「一句四个值」。

        对标原句含「原生效绩效/现生效绩效/是否已结账」，本设计一行一个字段放不下；
        硬塞会把「绩效金额」渲染成「生效绩效」的标签。这条测试把偏离钉住，
        也防「改回去」时静默通过。
        """
        template = ACTION_TEMPLATES["performance.adjust"]
        self.assertEqual(
            template, "修改了{target_name}的{field_label}（原{from_value}，新{to_value}）"
        )
        self.assertNotIn("生效绩效", template)
        self.assertNotIn("是否已结账", template)
        self.assertEqual(template.count("{from_value}"), 1)
        self.assertEqual(template.count("{to_value}"), 1)

    def test_templates_keep_the_spec_wording_verbatim(self):
        """抽查几处的**全角标点与括号**（【】/：/，/（）/→），防「顺手改进」。"""
        self.assertEqual(
            ACTION_TEMPLATES["order.transition"],
            # 尾段「（原因：{reason}）」是**模板的一部分**（reason 为空时该段由渲染器剔除，
            #    见 RenderSummaryTests 的空值用例）——这里钉的是**带原因段的完整字面值**，防「顺手改进」。
            "{actor_name}将订单 {order_no} 的状态从{from_value}改为{to_value}（原因：{reason}）",
        )
        self.assertEqual(
            ACTION_TEMPLATES["order.claim"],
            # 名字占位符必须是 `{actor_name}`（用 `{target_name}` 会渲染出双人名，见 `actions.py`）。
            # 逐字值仍然钉住。
            "{actor_name}从{reason}接手了订单 {order_no}，接手价 {to_value} 元",
        )
        self.assertEqual(
            ACTION_TEMPLATES["performance.submit"],
            "{actor_name}提交了订单 {order_no} 的完单，绩效金额：{to_value}",
        )
        self.assertEqual(
            ACTION_TEMPLATES["order.clear_taker"],
            "清除了订单 {order_no} 的接单人（原接单人：{target_name}）",
        )
        self.assertEqual(
            ACTION_TEMPLATES["user.rename"],
            "已将登录账号由 {from_value} 改为 {to_value}",
        )
        self.assertEqual(
            ACTION_TEMPLATES["performance.reassign"],
            "{actor_name}将订单重新指派给{target_name}，发单价有变化，原来发单价：{from_value}，"
            "新发单价：{to_value}",
        )
        self.assertEqual(
            ACTION_TEMPLATES["system.role_copy"],
            "复制角色职位：{from_value} → {to_value}",
        )
        self.assertEqual(
            ACTION_TEMPLATES["user.game_permission_add"], "新增用户游戏权限：{game_name}"
        )
        self.assertEqual(
            ACTION_TEMPLATES["user.game_permission_remove"], "删除用户游戏权限：{game_name}"
        )
        self.assertEqual(ACTION_TEMPLATES["user.reset_password"], "已将{target_name}的登录密码重置为初始密码")
        self.assertEqual(ACTION_TEMPLATES["system.game_destroy"], "删除游戏：{target_name}")
        self.assertEqual(
            ACTION_TEMPLATES["performance.settle"],
            "已将{target_name}的绩效结账状态标记为{to_value}（仅标记状态）",
        )


class RenderSummaryTests(SimpleTestCase):
    """渲染器是纯函数：只吃行内列，且不查库（spec §5.9/§10.4）。

    ⚠️ 这个类刻意用 `SimpleTestCase`：它**禁止**任何数据库访问，
    所以下列断言一旦发生查询就会以 `DatabaseOperationForbidden` 报错——
    「渲染不回查业务表」这条约束因此是**被结构性验证**的，不只是一句注释。
    """

    def test_renders_from_row_columns_without_touching_the_database(self):
        self.assertEqual(
            render_summary(
                "order.transition", actor="甲", order_no="20260921000000000001",
                from_value="待派单", to_value="服务中",
            ),
            "将订单 20260921000000000001 的状态从待派单改为服务中",
        )

    def test_unknown_action_falls_back_to_a_readable_summary(self):
        self.assertEqual(render_summary("whatever.new", actor="甲", actor_name="甲"), "【甲】操作记录")

    def test_rename_and_claim_describe_the_actual_operation(self):
        self.assertEqual(
            render_summary("user.rename", actor="甲", from_value="老名", to_value="新名"),
            "已将登录账号由 老名 改为 新名",
        )
        self.assertEqual(
            render_summary(
                "order.claim", actor="甲", actor_id=1, actor_name="王五",
                target_name="王五", reason="网页端大厅",
                order_no="20260921000000000001", source_order_no="TB-1", to_value="88.00",
            ),
            "王五从网页端大厅接手了订单 TB-1，接手价 88.00 元",
        )

    def test_downstream_claim_names_the_local_account_and_verified_destination(self):
        fields = {
            "actor": "下家鸡蛋铺同步",
            "actor_name": "下家鸡蛋铺同步",
            "target_name": "测试接单账号",
            "order_no": "20260921000000000001",
            "source_order_no": "TB-1",
            "field_name": "downstream_claim",
            "to_value": "80.00",
        }
        self.assertEqual(
            render_summary("order.claim", **fields, reason="测试下家"),
            "【下家鸡蛋铺同步】测试接单账号接手了订单 TB-1，接手价 80.00 元，"
            "已同步到下家 测试下家 的系统",
        )
        self.assertEqual(
            render_summary("order.claim", **fields),
            "【下家鸡蛋铺同步】测试接单账号接手了订单 TB-1，接手价 80.00 元",
        )

    def test_qr_result_describes_success_and_failure_without_raw_codes(self):
        fields = {
            "actor": "马上来查码",
            "actor_name": "马上来查码",
            "order_no": "20260921000000000001",
            "source_order_no": "TB-1",
        }
        self.assertEqual(
            render_summary(
                "order.malailai_qr_fetch", **fields, reason="自动拉取了本单群二维码"
            ),
            "【马上来查码】自动拉取订单 TB-1 的群码成功",
        )
        self.assertEqual(
            render_summary(
                "order.malailai_qr_fetch", **fields,
                reason="自动拉取群码失败：查码接口返回 503",
            ),
            "【马上来查码】自动拉取订单 TB-1 的群码失败（原因：查码服务暂时异常）",
        )

    def test_name_variables_render_the_name_column_not_the_id_column(self):
        """R11：`target_id` 是对标「相关ID」列（数字 id），句子里的名字走 `target_name`。

        ⚠️ 审计日志线（spec §3.4）之后 `performance.assign` 的占位符是 `{actor_name}`
        ⇒ 这一条要喂 `actor_name` 才渲染得出人名（`actor` 已不再被任何模板引用）。
        """
        self.assertEqual(
            render_summary(
                "performance.assign", actor="甲", actor_name="甲", target_id="7",
                target_name="张三", to_value="88.00",
            ),
            "甲将订单指派给张三，发单价：88.00",
        )
        self.assertEqual(
            render_summary("user.update", actor="甲", target_id="7", target_name="张三"),
            "编辑用户(ID：7，用户名：张三)的资料",
        )
        self.assertEqual(
            render_summary("system.role_update", actor="甲", target_name="客服", from_value="客服A"),
            "编辑角色权限：客服，原职位名称：客服A",
        )

    def test_field_label_comes_from_the_static_map_while_field_name_stays_the_column(self):
        """R8/R9：`field_name` 存真实列名，summary 里显示中文标签。"""
        self.assertEqual(
            render_summary(
                "order.update", actor="甲", order_no="20260921000000000001",
                field_name="total_price", from_value="88.00", to_value="99.00",
            ),
            "编辑了订单 20260921000000000001，订单总价由88.00改为99.00",
        )
        # 标签表之外的字段使用通用中文说明。
        self.assertEqual(
            render_summary(
                "order.update", actor="甲", order_no="X",
                field_name="weird_column", from_value="1", to_value="2",
            ),
            "编辑了订单 X，订单字段由1改为2",
        )

    def test_adjust_renders_one_field_pair_exactly_once(self):
        """R9 的判据：一行一个字段 ⇒ `to_value` 在渲染结果里**只出现一次**。

        旧的「一句四个值」模板让 `{from_value}`/`{to_value}` 各出现两次，
        会把绩效金额渲染成生效绩效的标签；这条断言就是那个 bug 类的兜底。
        """
        text = render_summary(
            "performance.adjust", actor="甲", target_name="张三",
            field_name="temp_amount", from_value="88.00", to_value="200.00",
        )
        self.assertEqual(text, "修改了张三的预计绩效（原88.00，新200.00）")
        self.assertEqual(text.count("200.00"), 1)
        self.assertEqual(text.count("88.00"), 1)
        bonus = render_summary(
            "performance.adjust", actor="甲", target_name="张三",
            field_name="bonus_amount", from_value="0.00", to_value="1.00",
        )
        self.assertEqual(bonus, "修改了张三的生效绩效（原0.00，新1.00）")

    def test_all_row_columns_are_explicit_parameters_now(self):
        # `**_` 已删除 ⇒ 行内列都是具名形参，`source_order_no` 这类列不需要"漏网"通道
        self.assertEqual(
            render_summary(
                "order.create", actor="甲", order_no="20260921000000000001",
                source_order_no="TB-1", to_value="88.00",
            ),
            "录入了订单 20260921000000000001，来源单号：TB-1，发单价 88.00 元",
        )

    # ── 人为 / 机器行的显示名前缀（spec §3.3 / §4 判据 1、2）──────────────
    #: 人为输入的合成夹具（E-0077：不用真值；`actor_id=1` 是**假主键**——
    #: `render_summary` 是纯函数、不查库，这里只判「非空即人为」）。
    HUMAN_ID = 1
    HUMAN_NAME = "测试昵称甲"

    def test_human_action_without_a_placeholder_gets_the_name_as_a_prefix(self):
        """判据 1①：人为 + 模板里没有 `{actor_name}` ⇒ 正文**以** `【昵称】` 开头。"""
        self.assertEqual(
            render_summary(
                "order.change_payment_status", actor="cs1", actor_id=self.HUMAN_ID,
                actor_name=self.HUMAN_NAME, order_no="20260921000000000001",
                from_value="未收款", to_value="已收款",
            ),
            "【测试昵称甲】修改了订单收款状态（未收款 → 已收款）（订单 20260921000000000001）",
        )

    def test_human_action_with_the_placeholder_does_not_repeat_the_name(self):
        """判据 1②：人为 + 模板**有** `{actor_name}` ⇒ **不以** `【` 开头、名字**只出现一次**。"""
        text = render_summary(
            "performance.assign", actor="cs1", actor_id=self.HUMAN_ID,
            actor_name=self.HUMAN_NAME, target_name="张三", to_value="88.00",
        )
        self.assertEqual(text, "测试昵称甲将订单指派给张三，发单价：88.00")
        self.assertFalse(text.startswith("【"), text)
        self.assertEqual(text.count(self.HUMAN_NAME), 1)

    def test_the_two_former_double_name_actions_render_the_name_exactly_once(self):
        """判据 C：两条曾经「双人名」的模板，人为渲染**逐字**——名字恰好出现一次。

        成因是模板用 `{target_name}` 自带一次名字、前缀规则又加一次；正解是**改模板**
        （`{target_name}` → `{actor_name}`），不是「渲染值相等就不加前缀」。
        """
        self.assertEqual(
            render_summary(
                "order.claim", actor="taker1", actor_id=self.HUMAN_ID,
                actor_name=self.HUMAN_NAME, target_name="taker1",
                reason="系统网页", order_no="20260921000000000001", to_value="88.00",
            ),
            "测试昵称甲从网页端大厅接手了订单 20260921000000000001，接手价 88.00 元",
        )
        self.assertEqual(
            render_summary(
                "performance.submit", actor="taker1", actor_id=self.HUMAN_ID,
                actor_name=self.HUMAN_NAME, target_name="taker1",
                order_no="20260921000000000001", to_value="0.00",
            ),
            "测试昵称甲提交了订单 20260921000000000001 的完单，绩效金额：0.00",
        )

    def test_another_action_still_shows_the_operator_when_the_two_names_differ(self):
        """判据 C③：`target_name != actor_name` 的同类动作**仍然显示操作人**。

        给「渲染值相等就不加前缀」那类抑制规则留的反例位：那条规则会把 `张三` 多写一次、
        有昵称的人又修不到（`target_name` 存账号、`actor_name` 存昵称）——正解是模板占位符。
        """
        text = render_summary(
            "performance.assign", actor="kefu-a", actor_id=self.HUMAN_ID,
            actor_name=self.HUMAN_NAME, target_name="张三", to_value="88.00",
        )
        self.assertEqual(text, "测试昵称甲将订单指派给张三，发单价：88.00")
        self.assertEqual(text.count(self.HUMAN_NAME), 1)
        # 被指派的人（`target_name`）必须仍在句子里——抑制规则最坏会把这两个人搞混。
        self.assertIn("张三", text)

    def test_machine_action_gets_its_source_as_a_prefix(self):
        """判据 1（spec §3-A1）：机器行（`actor_id` 空）**也加** `【来源】`。

        判据只有「`actor_name` 非空 ∧ 模板里没有 `{actor_name}`」；机器行的
        `actor_name == actor` ⇒ 前缀就是**来源**（沿用既有 `actor` 名称，⛔ 不新造）。
        判别力：把条件里去掉 `actor_id` 那一半又加回去（即回退「人为才加」）⇒ 第一条立刻红。
        """
        self.assertEqual(
            render_summary(
                "order.change_payment_status", actor="下家鸡蛋铺同步", actor_name="下家鸡蛋铺同步",
                order_no="20260921000000000001", from_value="未收款", to_value="已收款",
            ),
            "【下家鸡蛋铺同步】修改了订单收款状态（未收款 → 已收款）（订单 20260921000000000001）",
        )
        # 来源**恰好一次**：多一层前缀 / 模板里又写一遍都在这条上红。
        text = render_summary(
            "order.change_payment_status", actor="系统", actor_name="系统",
            order_no="20260921000000000001", from_value="未收款", to_value="已收款",
        )
        self.assertEqual(text.count("【"), 1, text)
        self.assertEqual(text.count("系统"), 1, text)

    def test_a_human_row_still_shows_the_nickname_exactly_once(self):
        """状态流转的正文以账号名称开头，昵称恰好一次。

        ⚠️ 这是机器行前缀改动的**回归位**：夹具让 `actor`（登录账号）与 `actor_name`（昵称）
        **不同形**，否则「正文里的那个名字到底是谁」不可辨、这条判据什么也证明不了。
        """
        text = render_summary(
            "order.transition", actor="cs1", actor_id=self.HUMAN_ID,
            actor_name=self.HUMAN_NAME, order_no="20260921000000000001",
            from_value="待验收", to_value="已完成",
        )
        self.assertEqual(
            text, f"{self.HUMAN_NAME}将订单 20260921000000000001 的状态从待验收改为已完成"
        )
        self.assertEqual(text.count(self.HUMAN_NAME), 1, text)
        self.assertNotIn("【", text)
        # 登录账号**不进正文**（`actor` 列的语义不变，但它是身份列、不是显示值）
        self.assertNotIn("cs1", text)

    def test_a_machine_row_without_a_source_renders_no_empty_brackets(self):
        """判据 2：机器行 `actor` 为空 ⇒ `actor_name` 也为空 ⇒ ⛔ **不许出现 `【】`**。

        真实可达：`order.refund` 那条写点的 `actor` 来自 `order.updated_by or ""`
        ⇒ 机器行的来源**结构上可以为空**。此时正文必须与「没有前缀规则」时**逐字相同**
        （而不是退化成 `【】将订单 …`）。
        """
        text = render_summary(
            "order.transition", actor="", actor_name="",
            order_no="20260921000000000001", from_value="待验收", to_value="已完成",
        )
        self.assertEqual(text, "将订单 20260921000000000001 的状态从待验收改为已完成")
        self.assertNotIn("【", text)

    def test_a_machine_row_whose_template_owns_actor_name_is_not_prefixed_twice(self):
        """自带来源的拆单发货提醒只显示一次操作人。"""
        text = render_summary(
            "order.split_ship_hold", actor="系统", actor_name="系统",
            order_no="20260921000000000001", reason="1/2",
        )
        self.assertEqual(text, "【系统】拆单组未全部完成（1/2），暂不发货（订单 20260921000000000001）")
        self.assertEqual(text.count("【"), 1, text)
        self.assertEqual(text.count("系统"), 1, text)

    def test_human_action_with_an_empty_display_name_gets_no_empty_brackets(self):
        """空显示名不增加空括号。"""
        text = render_summary(
            "order.transition", actor="", actor_id=self.HUMAN_ID, actor_name="",
            order_no="20260921000000000001", from_value="A", to_value="B",
        )
        self.assertNotIn("【", text)
        self.assertEqual(text, "将订单 20260921000000000001 的状态从A改为B")

    def test_the_placeholder_check_is_not_a_substring_check(self):
        """⭐ 承重判据的**边界**：`{actor_name!r}` / `{actor_name:>6}` 也算「模板里有这个占位符」。

        子串判据 `"{actor_name}" in template` 会把这两种写法误判成「没有占位符」⇒ 多加一层前缀。
        这条用例用**替身模板**直接钉住 `_placeholder_names`（不改生产常量）。
        """
        from apps.audit import log as log_module

        for template in ("{actor_name!r}做了事", "{actor_name:>6}做了事"):
            with self.subTest(template=template):
                with mock.patch.dict(
                    log_module.ACTION_TEMPLATES, {"order.transition": template}, clear=False
                ):
                    text = render_summary(
                        "order.transition", actor="cs1", actor_id=self.HUMAN_ID,
                        actor_name=self.HUMAN_NAME,
                    )
                self.assertFalse(text.startswith("【"), text)
                self.assertNotIn("【", text)

    def test_every_registered_action_shows_the_actor_when_the_row_is_human(self):
        """判据 2：**全部登记动作**的人为渲染都必须含显示名、且恰好出现一次。

        把业务方「所有人为操作必须带上操作人」变成**可失败的机器判据**：新增动作若既没有
        `{actor_name}` 占位符、又走不到前缀规则，这条当场红。判定打在**渲染结果串**上
        （不是「模板里有占位符」这类可能自指的判据）；「恰好一次」而非「含」——
        双人名缺陷在「含」的判据下是绿的。权威条数是本方法末尾那条字面量断言（网自报），
        这里不写条数。
        """
        seen = 0
        for action in ACTION_TEMPLATES:
            with self.subTest(action=action):
                text = render_summary(
                    action, actor="cs1", actor_id=self.HUMAN_ID,
                    actor_name=self.HUMAN_NAME, target_id="7", target_name="张三",
                    order_no="20260921000000000001", source_order_no="TB-1",
                    game_name="王者荣耀", game_role_name="角色甲",
                    field_name="total_price", from_value="1", to_value="2",
                    reason="备注",
                )
                self.assertIn(self.HUMAN_NAME, text, f"{action} 的人为正文里没有操作人")
                # ⚠️ 字面值 1（E-0024）：多了就是双人名，少了就是没写进去。
                self.assertEqual(
                    text.count(self.HUMAN_NAME), 1,
                    f"{action} 的人为正文里操作人出现了 {text.count(self.HUMAN_NAME)} 次：{text}",
                )
                seen += 1
        # 前置非空断言：循环真的走完了整张登记表（防「一次都没进」的恒真）。
        self.assertEqual(seen, len(ACTION_TEMPLATES))
        # 条数是**字面值**（⛔ 不推算）：登记表变了，这条与登记表那条一起红，逼人当场数。
        self.assertEqual(seen, 182)


class RecordTests(TenantTestCase):
    def test_record_fills_summary_from_structured_columns(self):
        row = record(
            log_type=LogType.ORDER, action="order.transition", actor="甲",
            # `record()` 对「没有操作人」fail-closed（见 `apps/audit/log.py`）
            # ⇒ 直调它的用例要么给 `actor_id`、要么显式声明 `system=True`。
            system=True,
            order_no="20260921000000000001", field_name="status",
            from_value="待派单", to_value="服务中",
        )
        # 机器行也加前缀，前缀就是它的来源（`actor`；spec §3-A1）。
        self.assertEqual(
            row.summary, "甲将订单 20260921000000000001 的状态从待派单改为服务中"
        )

    def test_record_never_accepts_a_caller_supplied_summary(self):
        with self.assertRaises(TypeError):
            record(log_type=LogType.ORDER, action="order.transition", actor="甲",
                   system=True, summary="我自己拼的")

    def test_record_rejects_a_row_without_an_actor(self):
        """（fail-closed）不传 `actor_id` **且**不声明 `system`
        ⇒ 抛 `ValueError`，⛔ 不静默落成一条机器行。

        判别力：把 `log.py` 里那段 `raise ValueError` 拿掉 ⇒ 本用例红。
        """
        with self.assertRaises(ValueError) as ctx:
            record(log_type=LogType.ORDER, action="order.transition", actor="甲")
        self.assertIn("system=True", str(ctx.exception))
        self.assertFalse(
            OperationLog.objects.filter(action="order.transition").exists(),
            "响亮失败必须是「一行都没写」，不是「写完再抛」",
        )

    def test_reason_longer_than_column_is_truncated_not_raised(self):
        row = record(log_type=LogType.USER, action="user.reset_password",
                     actor="甲", system=True, reason="x" * 400)
        self.assertEqual(len(row.reason), 255)

    def test_overlong_values_are_truncated_to_their_column_width_not_raised(self):
        """E-0010：请求/业务来源的值超长不能把业务打 500（PostgreSQL 会 `DataError`）。

        逐列取模型自己的 `max_length`，所以这条断言不抄列宽数字。
        """
        supplied = {
            "actor": "甲" * 100, "target_kind": "k" * 100, "target_id": "i" * 100,
            "target_name": "名" * 100, "order_no": "9" * 100,
            "source_order_no": "s" * 100, "game_role_name": "g" * 100,
            "game_name": "游" * 100, "field_name": "f" * 100,
            "from_value": "1" * 100, "to_value": "2" * 100, "reason": "r" * 400,
        }
        # `actor_name` 也是**一条文本列**（`_columns()` 按列宽截断）⇒ 必须进这份**列清单**，
        # 否则它的 150 宽截断没有任何用例钉（E-0175 同族：列清单是独立字面量、故意不派生
        # ⇒ 加列必须人肉同步）。⚠️ 它**不是 `record()` 的形参**（由 `actor_id` 派生，spec §3.2）
        # ⇒ 不能塞进 `supplied` 一起展开；它的截断单独钉（见下面第二条 `record()`）。
        # 「会被落库的文本列」的**逐条**清单（两边各写一遍 ⇒ 不是自指断言）。
        self.assertEqual(
            set(supplied) | {"actor_name"},
            {
                "actor", "actor_name", "target_kind", "target_id", "target_name",
                "order_no", "source_order_no", "game_role_name", "game_name",
                "field_name", "from_value", "to_value", "reason",
            },
        )
        user = User.objects.create_user(username="width-actor", first_name="宽" * 100)
        row = record(
            log_type=LogType.ORDER, action="order.claim",
            actor_id=user.pk, **supplied,
        )
        row.refresh_from_db()
        for name, value in supplied.items():
            width = OperationLog._meta.get_field(name).max_length
            self.assertEqual(
                len(getattr(row, name)), min(len(value), width), f"{name} 未按列宽截断"
            )
            self.assertEqual(getattr(row, name), value[:width], name)
        self.assertEqual(len(row.summary), 255)

        # ⭐ `actor_name` 的 150 宽截断：它的两个**正常**来路（`first_name` / `username`）
        # 各自就是 150 ⇒ 唯一能越过 150 的是「主键查不到用户 ⇒ 回落**原始** `actor`」
        # 那一档（`_actor_name` 读的是**裁剪之前**的入参）。判据与其余列同形。
        overlong_actor_name = "昵" * 300
        missing_pk = 987654321      # 合成值：本租户里查不到的主键
        self.assertFalse(
            User.objects.filter(pk=missing_pk).exists(),
            "前置：这个主键必须真的查不到，否则走不到「回落原始 actor」那一档",
        )
        fallback = record(
            log_type=LogType.ORDER, action="order.claim",
            actor=overlong_actor_name, actor_id=missing_pk,
        )
        fallback.refresh_from_db()
        name_width = OperationLog._meta.get_field("actor_name").max_length
        self.assertEqual(len(fallback.actor_name), name_width)
        self.assertEqual(fallback.actor_name, overlong_actor_name[:name_width])
        # 同一行上 `actor` 列也按自己的列宽收口（两列同宽，但判据各自取自己的元数据）。
        actor_width = OperationLog._meta.get_field("actor").max_length
        self.assertEqual(fallback.actor, overlong_actor_name[:actor_width])

    def test_unknown_action_renders_a_stable_fallback(self):
        """未登记的动作不能抛异常（否则埋点漏登记会 500 掉业务）。"""
        row = record(log_type=LogType.OTHER, action="whatever.new", actor="甲",
                     system=True, target_id="7")
        self.assertEqual(row.action, "whatever.new")
        self.assertEqual(row.summary, "【甲】操作记录")

    def test_record_persists_every_structured_column(self):
        user = User.objects.create_user(username="persist-actor", first_name="测试昵称丙")
        row = record(
            log_type=LogType.ORDER, action="order.create", actor="甲", actor_id=user.pk,
            target_kind="order", target_id="20260921000000000001",
            target_name="张三", order_no="20260921000000000001", source_order_no="TB-1",
            game_role_name="角色甲", game_name="王者荣耀", field_name="total_price",
            from_value="", to_value="88.00", reason="备注",
        )
        row.refresh_from_db()
        self.assertEqual(row.log_type, 10)
        self.assertEqual(row.actor, "甲")
        self.assertEqual(row.actor_id, user.pk)
        # ⭐ 审计日志线（spec §3.1/§3.2）：`actor_name` 是一列，落的是**派生出来的显示名**。
        self.assertEqual(row.actor_name, "测试昵称丙")
        self.assertEqual(row.target_kind, "order")
        self.assertEqual(row.target_id, "20260921000000000001")
        self.assertEqual(row.target_name, "张三")
        self.assertEqual(row.source_order_no, "TB-1")
        self.assertEqual(row.game_role_name, "角色甲")
        self.assertEqual(row.game_name, "王者荣耀")
        self.assertEqual(row.field_name, "total_price")
        self.assertEqual(row.to_value, "88.00")
        self.assertEqual(row.reason, "备注")
        self.assertEqual(
            row.summary,
            "测试昵称丙录入了订单 20260921000000000001，来源单号：TB-1，发单价 88.00 元"
        )

    def test_record_surfaces_every_rendered_column_in_the_summary(self):
        """表驱动：**每个**模板会渲染的列，`record()` 传进去后都必须出现在 summary 里
        （「漏转发某一列」这类缺陷就是被这一类断言抓的；不渲染列的豁免显式枚举在
        `NON_RENDERED_RENDERER_COLUMNS`）。
        ⚠️ `actor_name` **不是** `record()` 的形参（它由 `actor_id` 派生，spec §3.2）
        ⇒ 用**真实用户**喂进去；判据不变：落库的值 + 出现在 summary 里。
        """
        self.assertEqual(
            set(RENDERED_COLUMN_CASES) | NON_RENDERED_RENDERER_COLUMNS,
            RENDERER_COLUMNS,
        )
        for column, (action, value, expected) in RENDERED_COLUMN_CASES.items():
            with self.subTest(column=column):
                if column == "actor_name":
                    user = User.objects.create_user(username="列夹具甲", first_name=value)
                    row = record(
                        log_type=LogType.OTHER, action=action, actor="测试人",
                        actor_id=user.pk,
                    )
                else:
                    kwargs = {"actor": "测试人", column: value}
                    # 这一支没有认证态用户 ⇒ 显式声明机器来源
                    #（`record()` 对「没有操作人」fail-closed）。
                    row = record(log_type=LogType.OTHER, action=action, system=True, **kwargs)
                self.assertEqual(getattr(row, column), value)
                self.assertIn(expected, row.summary)

    def test_record_forwards_every_renderer_input(self):
        """`record()` 转发给 `render_summary` 的键必须**恰好**是 `RENDER_INPUTS`。

        用 spy 直接截住转发出去的关键字参数——「漏转发某一列」在结构上被这条钉死。
        （桩打在被查找的模块 `apps.audit.log` 上，见 E-0029。）
        ⚠️ `record()` 的形参集 = `RENDER_INPUTS` **减去** `actor_name` / `actor_id`：
        前者由 `actor_id` 派生、后者是整数直传（spec §3.2）⇒ 这条断言两边都钉住：
        转发出去的 `actor_name` 必须**是派生值**（不是调用方原样传的 `actor`），
        `actor_id` 必须是**原样的整数**（不是被 `_text()` 变成字符串）。
        """
        user = User.objects.create_user(username="spy-actor", first_name="测试昵称乙")
        supplied = {
            "actor": "甲", "target_id": "7", "target_name": "张三",
            "order_no": "20260921000000000001", "source_order_no": "TB-1",
            "game_name": "王者荣耀", "game_role_name": "角色甲",
            "field_name": "total_price", "from_value": "1", "to_value": "2",
            "reason": "备注",
        }
        # 测试自带的字面集合与生产常量必须一致（不是自指：两边各写一遍）
        self.assertEqual(set(RENDER_INPUTS) | {"field_label"}, RENDERER_COLUMNS)
        self.assertEqual(
            set(supplied), set(RENDER_INPUTS) - {"actor_name", "actor_id"}
        )
        captured = {}
        real = log.render_summary

        def spy(action, **kwargs):
            captured["action"] = action
            captured.update(kwargs)
            return real(action, **kwargs)

        with mock.patch.object(log, "render_summary", spy):
            log.record(
                log_type=LogType.OTHER, action="whatever.new",
                actor_id=user.pk, **supplied,
            )

        self.assertEqual(captured.pop("action"), "whatever.new")
        self.assertEqual(
            captured, {**supplied, "actor_name": "测试昵称乙", "actor_id": user.pk}
        )

    def test_record_uses_target_name_in_the_rendered_sentence(self):
        row = record(
            log_type=LogType.PERFORMANCE, action="performance.assign", actor="甲",
            # 没有认证态用户 ⇒ 显式声明机器来源（fail-closed）。
            system=True,
            target_kind="user", target_id="7", target_name="张三", to_value="88.00",
        )
        self.assertEqual(row.target_id, "7")
        # ⚠️ `performance.assign` 的模板**自带** `{actor_name}`（机器行该值 = `actor`）
        # ⇒ 正文里那个人名来**模板**，不是 §3.3 的前缀规则；`【】` 一个字都不加。
        self.assertEqual(row.summary, "甲将订单指派给张三，发单价：88.00")
        self.assertFalse(row.summary.startswith("【"))

    def test_overlong_summary_is_truncated_to_the_column_width(self):
        # performance.reassign 有 4 个 64 字符的取值位，必然越过 255
        row = record(
            log_type=LogType.PERFORMANCE, action="performance.reassign", actor="甲" * 64,
            system=True,
            target_name="用" * 64, from_value="1" * 64, to_value="2" * 64,
        )
        self.assertEqual(len(row.summary), 255)
        self.assertTrue(row.summary.startswith("甲甲甲"))


class ActorNameTests(TenantTestCase):
    """`record()` 派生 `actor_name`（spec §3.1/§3.2 / §4 判据 3）。

    三档逐条钉住：① 有 `actor_id` ⇒ 昵称；② 用户已删（主键查不到）⇒ 回落 `actor`；
    ③ `actor_id is None` ⇒ `actor_name == actor`。每档都**同时**钉住正文的人为前缀形态。
    """

    ORDER_NO = "20260921000000000001"

    def _transition(self, *, actor, actor_id):
        return record(
            log_type=LogType.ORDER, action="order.transition", actor=actor,
            actor_id=actor_id, order_no=self.ORDER_NO,
            # `actor_id is None` 这一档就是**机器行** ⇒ 显式声明
            #（`record()` 对「没有操作人」fail-closed，见 `apps/audit/log.py`）。
            system=actor_id is None,
            from_value="待验收", to_value="已完成",
        )

    def test_a_human_row_gets_the_nickname_of_the_user(self):
        user = User.objects.create_user(username="kefu-a", first_name="测试昵称甲")
        row = self._transition(actor="kefu-a", actor_id=user.pk)
        row.refresh_from_db()
        # `actor` 列的语义**没变**（仍是登录账号，供 `?actor=` 过滤与身份追溯，spec §5.4）
        self.assertEqual(row.actor, "kefu-a")
        self.assertEqual(row.actor_id, user.pk)
        self.assertEqual(row.actor_name, "测试昵称甲")
        self.assertEqual(
            row.summary,
            "测试昵称甲将订单 20260921000000000001 的状态从待验收改为已完成",
        )

    def test_no_first_name_falls_back_to_the_username(self):
        """口径唯一出处 `apps/accounts/display.py::user_display_name`（`first_name or username`）。"""
        user = User.objects.create_user(username="kefu-b")
        row = self._transition(actor="kefu-b", actor_id=user.pk)
        self.assertEqual(row.actor_name, "kefu-b")
        self.assertEqual(
            row.summary,
            "kefu-b将订单 20260921000000000001 的状态从待验收改为已完成",
        )

    def test_a_missing_user_falls_back_to_the_actor_column(self):
        """用户已删 / 非本租户（主键查不到）⇒ 回落 `actor`，⛔ 不许抛、⛔ 不许留空。"""
        missing_pk = 987654321   # 合成值：本租户里不存在的主键
        self.assertFalse(
            User.objects.filter(pk=missing_pk).exists(),
            "前置：这个主键必须真的查不到，否则本用例测的不是回落那一档",
        )
        row = self._transition(actor="kefu-gone", actor_id=missing_pk)
        self.assertEqual(row.actor_name, "kefu-gone")
        self.assertEqual(row.actor_id, missing_pk)   # 主键原样落库，不被回落改掉
        self.assertEqual(
            row.summary,
            "kefu-gone将订单 20260921000000000001 的状态从待验收改为已完成",
        )

    def test_a_row_without_a_user_id_falls_back_to_the_actor_column(self):
        """没有用户主键时，正文保留机器来源名称且只显示一次。"""
        row = self._transition(actor="系统", actor_id=None)
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.actor_name, "系统")
        self.assertEqual(
            row.summary,
            "系统将订单 20260921000000000001 的状态从待验收改为已完成",
        )
        self.assertEqual(row.summary.count("系统"), 1, row.summary)

    def test_an_empty_actor_machine_row_uses_system_as_the_display_name(self):
        row = self._transition(actor="", actor_id=None)
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.actor, "")
        self.assertEqual(row.actor_name, "系统")
        self.assertEqual(
            row.summary,
            "系统将订单 20260921000000000001 的状态从待验收改为已完成",
        )

