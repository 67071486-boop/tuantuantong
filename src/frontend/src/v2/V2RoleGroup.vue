<script setup lang="ts">
/**
 * 「角色职位分组」v2（`/v2/role-group`；系统维护批次 A 波）。旧页 = island 复刻页
 * `user-usergroup.ts`（对标 `/User/usergroup`，单表 3 列）：后端无「角色分组」模型与端点
 * ⇒ 诚实空态（真列名表头 + 恒空表 + 说明句，零假控件零 API；计划 §3 页 2）。
 */

</script>

<template>
  <div class="cv2-page" data-v2-page="role-group">
    <p class="cv2-note" data-v2="explain">
      对标功能的空壳复刻页：后端无「角色分组」模型与端点，页面恒为空表；旧版点击均为「尚未开放」。
      后端补上分组数据出口后，这里会按下面 3 列如实显示。
    </p>

    <!-- 表格：3 列 = island COLS 逐字（结构记录；空态一句话，不编数） -->
    <section class="cv2-panel cv2-table-panel" aria-label="角色职位分组列表">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>角色分组名称</th>
            <th>包含具体角色职位</th>
            <th class="cv2-th-center">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colspan="3" class="cv2-empty" data-v2="empty">暂无数据</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
