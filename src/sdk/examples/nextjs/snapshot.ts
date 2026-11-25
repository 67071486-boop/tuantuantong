import "server-only";
import type { Change, Order, Taker, TuantuantongClient } from "@tuantuantong/sdk";

interface RebuildStore {
  // 写入新一代快照的数据区，旧一代继续服务现有请求。
  writeOrders(orders: Order[]): Promise<void>;
  writeTakers(takers: Taker[]): Promise<void>;
  // 同个数据库事务中处理整页事件及其进度，按 event_id 去重。
  // 更新和恢复按编号再读取最新状态，删除移出旧记录。
  applyChanges(changes: Change[], nextCursor: string): Promise<void>;
  publish(): Promise<void>;
}

export async function rebuildSnapshot(client: TuantuantongClient, store: RebuildStore): Promise<void> {
  const watermark = await client.sync.changes({ start: "now" });

  let orderNo = "";
  while (true) {
    const page = await client.orders.list({ after: orderNo, page_size: 100 });
    const last = page.results.at(-1);
    if (!last) break;
    await store.writeOrders(page.results);
    orderNo = last.order_no;
  }

  let takerId = 0;
  while (true) {
    const page = await client.takers.list({ after: takerId, page_size: 100 });
    const last = page.results.at(-1);
    if (!last) break;
    await store.writeTakers(page.results);
    takerId = last.id;
  }

  let cursor = watermark.next_cursor;
  while (true) {
    const page = await client.sync.changes({ cursor, page_size: 100 });
    await store.applyChanges(page.results, page.next_cursor);
    if (!page.has_more) break;
    cursor = page.next_cursor;
  }
  await store.publish();
}
