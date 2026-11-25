"""代练通推送行补**远端多字段** + 并发抢单收口标记（计划 Rev.9 §2 / §4）。

## 为什么加这些列

- 业务方口径（Rev.9 §2）：「远端状态变化 ⇒ **回写本地订单状态**；推送行保存远端多字段
  （接单人 UID / 昵称、接单时间、撤销状态、撤销原因、最近同步时间）」。
  `remote_status` / `remote_cancel_status` / `last_synced_at` 早已存在 ⇒ 本次只补缺的四列：
  `remote_taker_uid` / `remote_taker_name` / `remote_accepted_at` / `remote_cancel_reason`。
- `remote_note`：**最近一次同步的业务结论**（中文一句话）。为什么不复用 `last_error` ——
  回读成功会把它清空（30 秒一轮），而「本地打手已被代练通接管」这类结论要留在页面上。
- `claim_withdraw_at` / `claim_withdraw_attempts`：Rev.9 §4 的「内部打手先接 ⇒ 及时把
  代练通订单撤掉」。删除成功走 `state=closed`；**协商撤销**成功后远端还活着（要等打手 /
  客服处理），只能靠 `claim_withdraw_at` 挡住重复申请；失败用 attempts 做有界重试。

## 纪律

- **只增不删**（`AGENTS.md` §4）：全部是 `AddField`，没有改列 / 删列；
- 全部可空或带默认值 ⇒ 对存量行零回填、零停机窗口；
- 只动 `DailiantongPush` 一张表；**一个叶子**（依赖链上只有一个父迁移 `0112`）。
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('business', '0112_platformpushsettings_default_limit_accept_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='dailiantongpush',
            name='claim_withdraw_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='因本地先接而撤回代练通的时间'),
        ),
        migrations.AddField(
            model_name='dailiantongpush',
            name='claim_withdraw_attempts',
            field=models.PositiveSmallIntegerField(default=0, verbose_name='撤回代练通连续失败次数'),
        ),
        migrations.AddField(
            model_name='dailiantongpush',
            name='remote_accepted_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='上游接单时间'),
        ),
        migrations.AddField(
            model_name='dailiantongpush',
            name='remote_cancel_reason',
            field=models.CharField(blank=True, default='', max_length=255, verbose_name='上游撤销原因'),
        ),
        migrations.AddField(
            model_name='dailiantongpush',
            name='remote_note',
            field=models.CharField(blank=True, default='', max_length=200, verbose_name='最近一次同步结论'),
        ),
        migrations.AddField(
            model_name='dailiantongpush',
            name='remote_taker_name',
            field=models.CharField(blank=True, default='', max_length=128, verbose_name='上游接单人昵称'),
        ),
        migrations.AddField(
            model_name='dailiantongpush',
            name='remote_taker_uid',
            field=models.CharField(blank=True, default='', max_length=64, verbose_name='上游接单人 UID'),
        ),
    ]
