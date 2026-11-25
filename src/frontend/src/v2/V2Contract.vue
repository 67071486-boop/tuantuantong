<script setup lang="ts">
/**
 * 「员工电子合同管理」v2（`/v2/contract`；系统人员批次 2026-11-05）。
 *
 * 旧页 = island 复刻页 `layui-island/pages/user-usercontractsigning.ts`（路由
 * `system-contract`，对标 `/User/UserContractSigning`）：对标功能的**空壳复刻页**——
 * 本系统没有电子签约的数据出口（`/api/SignContract/*` 不在我们后端），旧页恒为空表。
 * 本页落成**诚实空态**：保留旧页真实的列清单作形状记录（9 个命名列，逐字取
 * island COLS；「邀请时间 / 签约时间 / 操作」末两列 field 同名是对标原文，照抄不改；
 * 勾选列不搬——批量勾选对零行空表无意义），不渲染假筛选 / 假按钮（旧版按钮点击
 * 提示「尚未开放」，v2 干脆不画），零 API。后端补上数据出口后按下列列如实展示。
 */

</script>

<template>
  <div class="cv2-page" data-v2-page="contract">
    <p class="cv2-note" data-v2="explain">
      员工电子合同管理是对标功能的空壳复刻页：本系统未实现该功能的后端，页面恒为空表；旧版页内按钮点击均提示『尚未开放』。
      后端补上数据出口后按下列列如实展示。
    </p>

    <!-- 表格：9 列 = island COLS 的命名列逐字（结构记录；空态一句话，不编数） -->
    <section class="cv2-panel cv2-table-panel" aria-label="员工电子合同管理列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>真实姓名</th>
            <th>身份证号</th>
            <th>手机号</th>
            <th>系统用户名称</th>
            <th>年龄</th>
            <th>签约状态</th>
            <th>邀请时间</th>
            <th>签约时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colspan="9" class="cv2-empty" data-v2="empty">暂无数据</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
