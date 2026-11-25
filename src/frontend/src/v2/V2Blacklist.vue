<script setup lang="ts">
/**
 * 「人员黑名单」v2（`/v2/blacklist`；系统人员批次 2026-11-05）。
 *
 * 旧页 = island 复刻页 `layui-island/pages/user-backuserlist.ts`（路由
 * `system-blacklist`，对标 `/User/BackUserList`）：对标功能的**空壳复刻页**——
 * 本系统没有黑名单的数据出口（无「加入黑名单」的落库面），旧页恒为空表。
 * 本页落成**诚实空态**：保留旧页真实的列清单作形状记录（11 个命名列，逐字取
 * island COLS；勾选列不搬——批量勾选对零行空表无意义），不渲染假筛选 / 假按钮
 * （旧版按钮点击提示「尚未开放」，v2 干脆不画），零 API。后端补上数据出口后
 * 按下列列如实展示即可恢复旧页功能。
 */

</script>

<template>
  <div class="cv2-page" data-v2-page="blacklist">
    <p class="cv2-note" data-v2="explain">
      人员黑名单是对标功能的空壳复刻页：本系统未实现该功能的后端，页面恒为空表；旧版页内按钮点击均提示『尚未开放』。
      后端补上数据出口后按下列列如实展示。
    </p>

    <!-- 表格：11 列 = island COLS 的命名列逐字（结构记录；空态一句话，不编数） -->
    <section class="cv2-panel cv2-table-panel" aria-label="人员黑名单列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>手机号</th>
            <th>微信号</th>
            <th>QQ</th>
            <th>名称</th>
            <th>身份证号</th>
            <th>YY号</th>
            <th>收款账号</th>
            <th>原因或备注</th>
            <th>加入时间</th>
            <th>添加人</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colspan="11" class="cv2-empty" data-v2="empty">暂无数据</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
