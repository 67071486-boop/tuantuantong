import { createHash } from "node:crypto";
import type {
  ChangePage, ChangeQuery, ClaimedOrder, DictionaryEntry, Game, GameServer, HallOrder, HallQuery,
  ImageClaim, ImageExt, ImageTicket, ManagedOrderImage, Order, OrderAudit, OrderCreate, OrderImage, OrderPatch, OrderQuery,
  OrderSource, OrderState, OrderTiming, Page, PageQuery, PeerSplitOrder, PeerSplitResult, Project, SplitOrderResult,
  StatusChange, Taker, TakerCreate, TakerPatch,
  TakerQuery, TakerStatus,
} from "./contracts.js";
import {
  Transport, TuantuantongError, externalSubject, isToken, segment,
} from "./transport.js";
import type { ClientOptions, ReadOptions, WriteOptions } from "./transport.js";
import { verifyChangeWebhook } from "./webhook.js";
import type { WebhookInput } from "./webhook.js";

const BASE = "/api/integration/v1/";
const orderPath = (id: string) => "orders/" + segment(id) + "/";
const takerPath = (id: number) => "takers/" + segment(id) + "/";

type WriteMethod = "POST" | "PATCH" | "PUT" | "DELETE";
type QueryParams = Record<string, string | number | boolean | undefined>;
type Read = <T>(path: string, query: QueryParams, options?: ReadOptions) => Promise<T>;
type Write = <T>(path: string, method: WriteMethod, body: unknown, options: WriteOptions) => Promise<T>;

export interface ImageUpload {
  bytes: Uint8Array;
  ext: ImageExt;
  original_name?: string;
  remark?: string;
}

export function imageUploadKeys(operationKey: string): { ticketKey: string; claimKey: string } {
  const derive = (step: "ticket" | "claim") => createHash("sha256").update(operationKey + ":" + step).digest("hex");
  return { ticketKey: derive("ticket"), claimKey: derive("claim") };
}

function isImageValidationError(error: unknown): boolean {
  return error instanceof TuantuantongError && error.kind === "business" && error.details.code === 1000;
}

function orderImages<T extends OrderImage>(read: Read, write: Write, transport: Transport) {
  const ticket = (orderNo: string, ext: ImageExt, options: WriteOptions) =>
    write<ImageTicket>(orderPath(orderNo) + "images/ticket/", "POST", { ext }, options);
  const claim = (orderNo: string, input: ImageClaim, options: WriteOptions) =>
    write<OrderImage>(orderPath(orderNo) + "images/", "POST", input, options);
  return {
    images: (orderNo: string, query: PageQuery = {}, options?: ReadOptions) =>
      read<Page<T>>(orderPath(orderNo) + "images/", { ...query }, options),
    issueImageTicket: ticket,
    claimImage: claim,
    uploadImage: async (orderNo: string, input: ImageUpload, options: WriteOptions) => {
      const { bytes, ext, ...metadata } = input;
      const { ticketKey, claimKey } = imageUploadKeys(options.idempotencyKey);
      const issued = await ticket(orderNo, ext, { ...options, idempotencyKey: ticketKey });
      const confirm = () => claim(orderNo, { key: issued.key, ...metadata }, { ...options, idempotencyKey: claimKey });
      try {
        await transport.upload(issued.upload_url, bytes, ext === "png" ? "image/png" : "image/jpeg", options);
      } catch (uploadError) {
        if (options.signal?.aborted) throw uploadError;
        try {
          return await confirm();
        } catch (claimError) {
          // 认领未通过参数或上传校验时，原始上传错误更能说明问题。
          if (options.signal?.aborted || isImageValidationError(claimError)) throw uploadError;
          throw claimError;
        }
      }
      return confirm();
    },
  };
}

export interface AssignTaker {
  taker_id: number;
  price?: string;
}

export interface MyOrdersQuery extends PageQuery {
  source_order_no?: string;
  updated_since?: string;
}

function personals(transport: Transport, externalUserId: string) {
  let cache: { token: string; refreshAt: number } | undefined;
  let pending: Promise<{ token: string; refreshAt: number }> | undefined;

  function currentToken() {
    if (cache && cache.refreshAt > Date.now()) return Promise.resolve(cache);
    pending ??= (async () => {
      const start = Date.now();
      const value = await transport.call<unknown>(BASE + "taker-token/", {
        method: "POST",
        body: { external_user_id: externalUserId },
        nonMutation: true,
      });
      if (!isToken(value)) {
        throw new TuantuantongError("invalid_response", "打手令牌响应无效");
      }
      const lifetime = value.expires_in * 1000;
      cache = { token: value.access_token, refreshAt: start + lifetime - Math.min(30_000, lifetime / 10) };
      return cache;
    })().finally(() => { pending = undefined; });
    return pending;
  }

  async function withToken<T>(operation: (token: string) => Promise<T>, options?: ReadOptions): Promise<T> {
    options?.signal?.throwIfAborted();
    const snapshot = await currentToken();
    try {
      return await operation(snapshot.token);
    } catch (error) {
      if (!(error instanceof TuantuantongError) || error.details.httpStatus !== 401) throw error;
      if (cache === snapshot) cache = undefined;
      options?.signal?.throwIfAborted();
      return operation((await currentToken()).token);
    }
  }

  const read: Read = (path, query, options) =>
    withToken((token) => transport.call(BASE + "taker/" + path, { query, options, token }), options);
  const write: Write = (path, method, body, options) =>
    withToken((token) => transport.call(BASE + "taker/" + path, { method, body, options, token }), options);
  return {
    hall: {
      list: (query: HallQuery = {}, options?: ReadOptions) =>
        read<Page<HallOrder>>("hall/", { ...query }, options),
      claim: (orderNo: string, options: WriteOptions) =>
        write<OrderState>("hall/" + segment(orderNo) + "/claim/", "POST", {}, options),
    },
    myOrders: {
      list: (query: MyOrdersQuery = {}, options?: ReadOptions) =>
        read<Page<ClaimedOrder>>("my/orders/", { ...query }, options),
      get: (orderNo: string, options?: ReadOptions) =>
        read<ClaimedOrder>(orderPath(orderNo), {}, options),
      submit: (orderNo: string, options: WriteOptions) =>
        write<OrderState>(orderPath(orderNo) + "status/", "POST", { status: "PENDING_REVIEW" }, options),
      changeStatus: (orderNo: string, input: StatusChange<TakerStatus>, options: WriteOptions) =>
        write<OrderState>(orderPath(orderNo) + "status/", "POST", input, options),
      ...orderImages<OrderImage>(read, write, transport),
    },
  };
}

export function createTuantuantongClient(options: ClientOptions) {
  const transport = new Transport(options);
  const webhookSecret = options.appSecret;
  const read: Read = (path, query, options) => transport.call(BASE + path, { query, options });
  const write: Write = (path, method, body, options) =>
    transport.call(BASE + path, { method, body, options });
  const dictionary = <T extends DictionaryEntry>(
    kind: string, query: PageQuery & { game_id?: number }, options?: ReadOptions,
  ) =>
    read<Page<T>>("catalog/" + kind + "/", { ...query }, options);
  const orderAction = (orderNo: string, kind: string, options: WriteOptions) =>
    write<Order>(orderPath(orderNo) + kind + "/", "POST", {}, options);
  const takerAction = (takerId: number, kind: string, options: WriteOptions) =>
    write<Taker>(takerPath(takerId) + kind + "/", "POST", {}, options);

  return {
    orders: {
      list: (query: OrderQuery = {}, options?: ReadOptions) => read<Page<Order>>("orders/", { ...query }, options),
      get: (orderNo: string, options?: ReadOptions) => read<Order>(orderPath(orderNo), {}, options),
      create: (input: OrderCreate, options: WriteOptions) => write<Order>("orders/", "POST", input, options),
      update: (orderNo: string, input: OrderPatch, options: WriteOptions) =>
        write<Order>(orderPath(orderNo), "PATCH", input, options),
      timing: (orderNo: string, options?: ReadOptions) =>
        read<OrderTiming>(orderPath(orderNo) + "status/", {}, options),
      changeStatus: (orderNo: string, input: StatusChange, options: WriteOptions) =>
        write<Order>(orderPath(orderNo) + "status/", "POST", input, options),
      setHallPrice: (orderNo: string, price: string, options: WriteOptions, reason?: string) =>
        write<Order>(
          orderPath(orderNo) + "assign-price/", "PUT", { price, reason }, options,
        ),
      listAssignees: (query: PageQuery & { game_id?: number } = {}, options?: ReadOptions) =>
        read<Page<Taker>>("orders/assignees/", { ...query }, options),
      assign: (orderNo: string, input: AssignTaker, options: WriteOptions) =>
        write<Order>(orderPath(orderNo) + "assign/", "POST", input, options),
      reassign: (orderNo: string, input: AssignTaker, options: WriteOptions) =>
        write<Order>(orderPath(orderNo) + "reassign/", "POST", input, options),
      publish: (orderNo: string, options: WriteOptions) => orderAction(orderNo, "publish", options),
      withdraw: (orderNo: string, options: WriteOptions) => orderAction(orderNo, "withdraw", options),
      clearTaker: (orderNo: string, options: WriteOptions) => orderAction(orderNo, "clear-taker", options),
      delete: (orderNo: string, options: WriteOptions) =>
        write<Order>(orderPath(orderNo) + "delete/", "POST", undefined, options),
      restore: (orderNo: string, options: WriteOptions) => orderAction(orderNo, "restore", options),
      recycleBin: (query: PageQuery = {}, options?: ReadOptions) =>
        read<Page<Order>>("orders/recycle-bin/", { ...query }, options),
      audit: (orderNo: string, query: PageQuery = {}, options?: ReadOptions) =>
        read<Page<OrderAudit>>(orderPath(orderNo) + "audit/", { ...query }, options),
      addNote: (orderNo: string, content: string, options: WriteOptions) =>
        write<{ order_no: string }>(orderPath(orderNo) + "notes/", "POST", { content }, options),
      split: (orderNo: string, projects: string[], options: WriteOptions) =>
        write<SplitOrderResult>(orderPath(orderNo) + "split/", "POST", { projects }, options),
      splitPeer: (orderNo: string, input: PeerSplitOrder, options: WriteOptions) =>
        write<PeerSplitResult>(orderPath(orderNo) + "split-peer/", "POST", input, options),
      deleteImage: (orderNo: string, imageId: number, options: WriteOptions) =>
        write<null>(orderPath(orderNo) + "images/" + segment(imageId) + "/", "DELETE", undefined, options),
      ...orderImages<ManagedOrderImage>(read, write, transport),
    },
    takers: {
      list: (query: TakerQuery = {}, options?: ReadOptions) => read<Page<Taker>>("takers/", { ...query }, options),
      get: (takerId: number, options?: ReadOptions) => read<Taker>(takerPath(takerId), {}, options),
      create: (input: TakerCreate, options: WriteOptions) => write<Taker>("takers/", "POST", input, options),
      update: (takerId: number, input: TakerPatch, options: WriteOptions) =>
        write<Taker>(takerPath(takerId), "PATCH", input, options),
      enable: (takerId: number, options: WriteOptions) => takerAction(takerId, "enable", options),
      disable: (takerId: number, options: WriteOptions) => takerAction(takerId, "disable", options),
      delete: (takerId: number, options: WriteOptions) => takerAction(takerId, "delete", options),
      restore: (takerId: number, options: WriteOptions) => takerAction(takerId, "restore", options),
      bind: (externalUserId: string, takerId: number, options: WriteOptions) =>
        write<{ external_user_id: string; taker_id: number }>(
          "taker-bindings/" + segment(externalSubject(externalUserId)) + "/", "PUT", { taker_id: takerId }, options,
        ),
      unbind: (externalUserId: string, options: WriteOptions) =>
        write<{ external_user_id: string }>(
          "taker-bindings/" + segment(externalSubject(externalUserId)) + "/", "DELETE", undefined, options,
        ),
    },
    forTaker: (externalUserId: string) => personals(transport, externalSubject(externalUserId)),
    catalog: {
      games: (query: PageQuery = {}, options?: ReadOptions) => dictionary<Game>("games", query, options),
      servers: (query: PageQuery & { game_id?: number } = {}, options?: ReadOptions) =>
        dictionary<GameServer>("servers", query, options),
      projects: (query: PageQuery & { game_id: number }, options?: ReadOptions) =>
        dictionary<Project>("projects", query, options),
      orderSources: (query: PageQuery = {}, options?: ReadOptions) => dictionary<OrderSource>("order-sources", query, options),
    },
    sync: {
      changes: (query: ChangeQuery = {}, options?: ReadOptions) => read<ChangePage>("changes/", { ...query }, options),
      verifyWebhook: (input: WebhookInput) => verifyChangeWebhook(input, webhookSecret),
    },
  };
}

export type TuantuantongClient = ReturnType<typeof createTuantuantongClient>;
export type TakerClient = ReturnType<typeof personals>;
