<script setup lang="ts">
/**
 * 「黑名单管理」（客户黑名单）v2 占位（2026-11-11 客户线第七批 §1.4）：点击不再跳 v1
 * 旧壳（映射在 `v2Menu.ts`；`config/menu.ts` 条目与旧路由零改动）。真功能（数据模型 /
 * 拉黑操作）另行拍板挂账；零 API、无假数据，定稿后整页重写。
 */
</script>

<template>
  <div class="cv2-page" data-v2-page="customer-blacklist">
    <p class="cv2-note" data-v2="explain">
      黑名单管理用于记录需要拦截的买家（客户）；功能建设中，暂未开放拉黑与查询。
    </p>
    <section class="cv2-panel cv2-table-panel" aria-label="黑名单管理">
      <table class="cv2-table">
        <thead>
          <tr>
            <th>黑名单管理</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td class="cv2-empty" data-v2="empty">功能建设中</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
