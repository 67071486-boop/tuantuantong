<script setup lang="ts">
/**
 * 「登录限制设置」v2（`/v2/login-restrict`；系统人员批次 2026-11-05）。
 *
 * 旧页 = island 复刻页 `layui-island/pages/user-userloginrestrict.ts`（路由
 * `system-login-restrict`，对标 `/User/userloginrestrict`）：对标功能的**空壳复刻页**——
 * 本系统没有「按周期段 / 时间段 / IP 白名单限制登录」的数据出口，旧页走声明式渲染器、
 * 恒为空表。本页照旧页的实际行为落成**诚实空态**：保留旧页真实的列清单作形状记录
 * （10 个命名列，逐字取 island COLS；勾选列不搬——批量勾选对零行空表无意义），
 * 不渲染假筛选 / 假按钮（旧版按钮点击提示「尚未开放」，v2 干脆不画），零 API。
 * 后端补上数据出口后按下列列如实展示即可恢复旧页功能。
 */

</script>

<template>
  <div class="cv2-page" data-v2-page="login-restrict">
    <p class="cv2-note" data-v2="explain">
      登录限制设置是对标功能的空壳复刻页：本系统未实现该功能的后端，页面恒为空表；旧版页内按钮点击均提示『尚未开放』。
      后端补上数据出口后按下列列如实展示。
    </p>

    <!-- 表格：10 列 = island COLS 的命名列逐字（结构记录；空态一句话，不编数） -->
    <section class="cv2-panel cv2-table-panel" aria-label="登录限制设置列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>系统用户</th>
            <th>限制周期段</th>
            <th>限制时间段</th>
            <th>限制方式</th>
            <th>IP范围</th>
            <th>规则状态</th>
            <th>创建时间</th>
            <th>更新时间</th>
            <th>修改人名称</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colspan="10" class="cv2-empty" data-v2="empty">暂无数据</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
