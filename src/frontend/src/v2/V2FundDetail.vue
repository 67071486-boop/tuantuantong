<script setup lang="ts">
/**
 * 「团团通资金明细与提现」v2（`/v2/fund-detail`；财务族第二批 2026-11-06，计划
 * `docs/plans/2026-11-06-finance-batch2.md` §5）。旧页 = island 复刻页
 * `layui-island/pages/emp-funddetailslist.ts`（路由 `agents-fund-detail`，对标
 * `/Emp/funddetailslist`）：**恒空复刻页**——数据源是「团团通」官方账户的资金流水
 * （充值 / 提现），本系统没有这个数据出口（旧页余额与统计三处值全按「不取、不发请求」
 * 处置）。照 V2Settlement 口径重建：真列名 7 列（对标列清单逐字；「Banlance」拼写只活在
 * 对标 API 层，本页列名照中文「可用余额」）+ 空表体 + 如实说明句；
 * 「去提现」按钮 / 余额引用块等恒空假控件不搬。零 API。页面标题照菜单名
 * 「团团通资金明细与提现」（显示名口径 D-0339）。
 */

</script>

<template>
  <div class="cv2-page" data-v2-page="fund-detail">
    <p class="cv2-note" data-v2="explain">
      资金明细与提现的数据源是「团团通」官方账户的资金流水（充值 / 提现），本系统暂无该数据出口，
      旧版页面同样是空表。后端补上数据出口后，这里会按下面 7 列如实显示。
    </p>

    <!-- 表格：7 列 = 对标列清单逐字（结构记录；空态一句话，不编数） -->
    <section class="cv2-panel cv2-table-panel" aria-label="团团通资金明细列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>支付流水号</th>
            <th>相关订单号</th>
            <th class="cv2-th-center">资金方向</th>
            <th class="cv2-th-num">金额</th>
            <th class="cv2-th-num">可用余额</th>
            <th class="cv2-th-center">时间</th>
            <th>备注说明</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colspan="7" class="cv2-empty" data-v2="empty">暂无资金明细记录</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
