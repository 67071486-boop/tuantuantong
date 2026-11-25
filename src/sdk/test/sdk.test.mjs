import { setTimeout as pause } from "node:timers/promises";
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  createTuantuantongClient,
  createIdempotencyKey,
  imageUploadKeys,
  InvalidWebhookError,
  TuantuantongError,
} from "../dist/index.js";

const appKey = "sdk-test";
const appSecret = "sdk-fixture";
const origin = "https://studio.test";
const orderNo = "20261116000000000001";
const tokenPath = "/api/open/oauth/token";
const basePath = "/api/integration/v1/";
const orderPath = basePath + "orders/" + orderNo + "/";
const write = { idempotencyKey: "order-assign-fixture", actor: "operator-1" };
const bearer = (value) => ({ access_token: value, expires_in: 240, token_type: "Bearer" });
const success = (data) => Response.json({ code: 0, msg: "OK", data });
const makeSdk = (fetch, more = {}) => createTuantuantongClient({ origin, appKey, appSecret, fetch, ...more });

test("并发首查只取一次令牌，工作室请求全程拒绝重定向和缓存", async () => {
  let exchanged = 0;
  const sdk = makeSdk(async (url, init) => {
    assert.equal(url.origin, origin);
    assert.equal(init.redirect, "error");
    assert.equal(init.cache, "no-store");
    if (url.pathname === tokenPath) {
      exchanged++;
      assert.equal(init.headers["Content-Type"], "application/x-www-form-urlencoded");
      assert.equal(init.body.get("grant_type"), "client_credentials");
      assert.equal(init.body.get("client_id"), appKey);
      assert.equal(init.body.get("client_secret"), appSecret);
      await pause(10);
      return Response.json(bearer("app-token"));
    }
    assert.equal(url.pathname, orderPath);
    assert.equal(init.headers.get("Authorization"), "Bearer app-token");
    return success({ order_no: orderNo });
  });

  const result = await Promise.all([sdk.orders.get(orderNo), sdk.orders.get(orderNo), sdk.orders.get(orderNo)]);
  assert.deepEqual(result.map((entry) => entry.order_no), [orderNo, orderNo, orderNo]);
  assert.equal(exchanged, 1);
});

test("服务端拒绝旧令牌后重新取令牌，派单重发保留原请求体、操作键和发起人", async () => {
  let exchanged = 0;
  const observed = [];
  const sdk = makeSdk(async (url, init) => {
    if (url.pathname === tokenPath) return Response.json(bearer("app-" + ++exchanged));
    observed.push({
      path: url.pathname,
      method: init.method,
      bearer: init.headers.get("Authorization"),
      key: init.headers.get("Idempotency-Key"),
      actor: init.headers.get("X-TT-Actor"),
      body: JSON.parse(init.body),
    });
    if (observed.length === 1) {
      return Response.json({ code: 401, msg: "令牌已过期", data: null }, { status: 401 });
    }
    return success({ order_no: orderNo, status: "IN_PROGRESS" });
  });

  const response = await sdk.orders.assign(orderNo, { taker_id: 17 }, write);
  assert.equal(response.status, "IN_PROGRESS");
  assert.equal(exchanged, 2);
  const expected = {
    path: orderPath + "assign/",
    method: "POST",
    key: write.idempotencyKey,
    actor: write.actor,
    body: { taker_id: 17 },
  };
  assert.deepEqual(observed, [
    { ...expected, bearer: "Bearer app-1" },
    { ...expected, bearer: "Bearer app-2" },
  ]);
});

test("并列拆单提交分项价格，删单仅发送操作键", async () => {
  const split = {
    remaining_project_text: "保留项目",
    new_orders: [{ project_text: "拆出项目", total_price: "10.20" }],
    existing_orders: [{ order_no: "20261116000000000002", total_price: "0.00" }],
  };
  const result = {
    order_no: orderNo, split_id: orderNo, split_kind: "PEER", source_total_price: "89.80",
    updated: ["20261116000000000002"], created: [], members: [],
  };
  const sdk = makeSdk(async (url, init) => {
    if (url.pathname === tokenPath) return Response.json(bearer("app-token"));
    assert.equal(init.method, "POST");
    assert.equal(init.headers.get("Idempotency-Key"), write.idempotencyKey);
    if (url.pathname === orderPath + "split-peer/") {
      assert.deepEqual(JSON.parse(init.body), split);
      return success(result);
    }
    assert.equal(url.pathname, orderPath + "delete/");
    assert.equal(init.body, undefined);
    return success({ order_no: orderNo });
  });
  assert.deepEqual(await sdk.orders.splitPeer(orderNo, split, write), result);
  assert.equal((await sdk.orders.delete(orderNo, write)).order_no, orderNo);
});

test("HTTP 200 中的业务失败携带已创建订单号，便于调用方继续处理原单", async () => {
  const sdk = makeSdk(async (url) => {
    if (url.pathname === tokenPath) return Response.json(bearer("app-token"));
    return Response.json({
      code: 1022, msg: "已录单，指派失败", data: { order_no: orderNo, reason: "打手不可用" },
    }, { headers: { "X-Request-Id": "fixture-request" } });
  });
  await assert.rejects(
    sdk.orders.create({
      game: 1, project_text: "测试项目", source_order_no: "fixture-source", total_price: "0.00",
    }, write),
    (error) => error instanceof TuantuantongError && error.kind === "business"
      && error.details.code === 1022 && error.details.createdOrderNo === orderNo
      && error.details.requestId === "fixture-request",
  );
});

test("后端限流保留 Retry-After，SDK 不自行重发写动作", async () => {
  let calls = 0;
  const sdk = makeSdk(async (url) => {
    if (url.pathname === tokenPath) return Response.json(bearer("app-token"));
    calls++;
    return Response.json({ code: 429, msg: "限流", data: null }, {
      status: 429, headers: { "retry-after": "3" },
    });
  });
  await assert.rejects(sdk.orders.publish(orderNo, write), (error) =>
    error.kind === "rate_limit" && error.details.retryAfter === "3");
  assert.equal(calls, 1);
});

test("写请求收到响应头后超时，结果仍需按同一个操作键核对", async () => {
  const sdk = makeSdk(async (url, init) => {
    if (url.pathname === tokenPath) return Response.json(bearer("app-token"));
    const stream = new ReadableStream({
      start(controller) {
        init.signal.addEventListener("abort", () => controller.error(init.signal.reason), { once: true });
      },
    });
    return new Response(stream);
  }, { timeoutMs: 30 });
  const keepAlive = setInterval(() => {}, 100);
  try {
    await assert.rejects(sdk.orders.publish(orderNo, write), (error) =>
      error.kind === "timeout" && error.details.outcomeUnknown === true);
  } finally {
    clearInterval(keepAlive);
  }
});

test("两个打手获得并使用各自的短期令牌，工作室令牌只用于换取", async () => {
  const issuedFor = [];
  const observed = [];
  const sdk = makeSdk(async (url, init) => {
    if (url.pathname === tokenPath) return Response.json(bearer("app-token"));
    if (url.pathname === basePath + "taker-token/") {
      assert.equal(init.headers.get("Authorization"), "Bearer app-token");
      const { external_user_id: id } = JSON.parse(init.body);
      issuedFor.push(id);
      return success({ ...bearer("bound-" + id), token_type: "bearer" });
    }
    assert.equal(url.pathname, basePath + "taker/my/orders/");
    observed.push(init.headers.get("Authorization"));
    return success({ results: [] });
  });
  const left = sdk.forTaker("left");
  await Promise.all([left.myOrders.list(), left.myOrders.list(), sdk.forTaker("right").myOrders.list()]);
  assert.deepEqual(issuedFor.sort(), ["left", "right"]);
  assert.deepEqual(observed.sort(), ["Bearer bound-left", "Bearer bound-left", "Bearer bound-right"]);
});

test("进度图走取票、直传、认领，直传对象存储不带应用凭据", async () => {
  const upload = "https://storage.test/example?signature=fixture";
  const writes = [];
  let direct = 0;
  const sdk = makeSdk(async (url, init) => {
    if (url.pathname === tokenPath) return Response.json(bearer("app-token"));
    if (url.href === upload) {
      direct++;
      assert.equal(init.method, "PUT");
      assert.equal(init.headers.Authorization, undefined);
      assert.equal(init.headers["Content-Type"], "image/png");
      assert.deepEqual(init.body, new Uint8Array([17, 42]));
      return new Response(null, { status: 204 });
    }
    const key = init.headers.get("Idempotency-Key");
    writes.push({ path: url.pathname, key });
    if (url.pathname === orderPath + "images/ticket/") {
      assert.deepEqual(JSON.parse(init.body), { ext: "png" });
      return success({ key: "fixture-key", expires_in: 600, upload_url: upload });
    }
    assert.equal(url.pathname, orderPath + "images/");
    assert.deepEqual(JSON.parse(init.body), { key: "fixture-key", original_name: "fixture.png" });
    return success({ original_name: "fixture.png" });
  });
  const image = { bytes: new Uint8Array([17, 42]), ext: "png", original_name: "fixture.png" };
  const options = { idempotencyKey: createIdempotencyKey() };
  assert.equal((await sdk.orders.uploadImage(orderNo, image, options)).original_name, "fixture.png");
  await sdk.orders.uploadImage(orderNo, image, options);
  assert.equal(direct, 2);
  assert.notEqual(writes[0].key, writes[1].key);
  assert.deepEqual(writes.slice(0, 2), writes.slice(2));
});

test("第一次认领已提交但响应丢失，原上传票过期后仍能回放认领", async () => {
  const confirmed = { original_name: "fixture.png", img_type: 0 };
  let uploads = 0;
  let persistedClaim;
  const sdk = makeSdk(async (url, init) => {
    if (url.pathname === tokenPath) return Response.json(bearer("app-token"));
    if (url.hostname === "storage.test") {
      uploads++;
      return new Response(null, { status: uploads === 1 ? 204 : 403 });
    }
    if (url.pathname === orderPath + "images/ticket/") {
      return success({ key: "fixture-key", expires_in: 1, upload_url: "https://storage.test/expired-ticket" });
    }
    assert.equal(url.pathname, orderPath + "images/");
    const receipt = { key: init.headers.get("Idempotency-Key"), body: JSON.parse(init.body) };
    if (persistedClaim) {
      assert.deepEqual(receipt, persistedClaim);
      return success(confirmed);
    }
    persistedClaim = receipt;
    throw new TypeError("模拟已经提交认领但连接断开");
  });

  const image = { bytes: new Uint8Array([17, 42]), ext: "png", original_name: "fixture.png" };
  const options = { idempotencyKey: "photo-operation-1" };
  await assert.rejects(sdk.orders.uploadImage(orderNo, image, options), (error) =>
    error.kind === "network" && error.details.outcomeUnknown === true);
  assert.deepEqual(await sdk.orders.uploadImage(orderNo, image, options), confirmed);
  const { ticketKey, claimKey } = imageUploadKeys(options.idempotencyKey);
  const ticket = await sdk.orders.issueImageTicket(orderNo, "png", { idempotencyKey: ticketKey });
  assert.deepEqual(await sdk.orders.claimImage(orderNo, {
    key: ticket.key, original_name: image.original_name,
  }, { idempotencyKey: claimKey }), confirmed);
});

test("调用方取消上传时保留未知状态，不再发认领请求", async () => {
  const abort = new AbortController();
  let claims = 0;
  const sdk = makeSdk(async (url, init) => {
    if (url.pathname === tokenPath) return Response.json(bearer("app-token"));
    if (url.pathname === orderPath + "images/ticket/") {
      return success({ key: "fixture-key", expires_in: 60, upload_url: "https://storage.test/fixture" });
    }
    if (url.hostname === "storage.test") {
      abort.abort();
      init.signal.throwIfAborted();
    }
    claims++;
    return success({ original_name: "fixture.png" });
  });

  const image = { bytes: new Uint8Array([17, 42]), ext: "png", original_name: "fixture.png" };
  await assert.rejects(sdk.orders.uploadImage(orderNo, image, {
    idempotencyKey: "photo-cancelled-1", signal: abort.signal,
  }), (error) => error.kind === "network" && error.details.outcomeUnknown === true);
  assert.equal(claims, 0);
});

test("回调匹配 Python 规范化签名，内容改动或请求头不匹配都会被拒绝", () => {
  const sdk = makeSdk(async () => { throw new Error("回调验签无需网络"); });
  const change = {
    event: "integration.change", event_id: "ev-123", delivery_id: "0123456789abcdef0123456789abcdef",
    resource: "order", resource_id: "测试-单", action: "updated",
    occurred_at: "2026-11-16T09:00:00+08:00", timestamp: 1794790800,
    sign: "d1d92cac68ac4fa985d1c8127dbbc3e3fe70b0df64e2c8c99014d9f9b999db75",
  };
  const headers = new Headers({
    "X-TT-Timestamp": String(change.timestamp),
    "X-TT-Delivery": change.delivery_id,
    "X-TT-Signature": change.sign,
  });
  assert.deepEqual(sdk.sync.verifyWebhook({ body: JSON.stringify(change, null, 2), headers }), change);
  assert.throws(() => sdk.sync.verifyWebhook({
    body: JSON.stringify({ ...change, action: "deleted" }), headers,
  }), InvalidWebhookError);
  headers.set("X-TT-Timestamp", "1794790801");
  assert.throws(() => sdk.sync.verifyWebhook({ body: JSON.stringify(change), headers }), InvalidWebhookError);
});
