import { randomUUID } from "node:crypto";

export interface ClientOptions {
  origin: string;
  appKey: string;
  appSecret: string;
  timeoutMs?: number;
  fetch?: typeof fetch;
}

export interface ReadOptions {
  signal?: AbortSignal;
}

export interface WriteOptions extends ReadOptions {
  idempotencyKey: string;
  actor?: string;
}

export type Query = Record<string, string | number | boolean | undefined>;
export type ErrorKind = "auth" | "rate_limit" | "http" | "business" | "invalid_response" | "timeout" | "network";

interface ErrorDetails {
  httpStatus?: number;
  code?: number | string;
  requestId?: string | null;
  retryAfter?: string | null;
  outcomeUnknown?: boolean;
  createdOrderNo?: string;
}

export class TuantuantongError extends Error {
  constructor(
    public readonly kind: ErrorKind,
    message: string,
    public readonly details: Readonly<ErrorDetails> = {},
  ) {
    super(message);
    this.name = "TuantuantongError";
  }
}

export interface Token {
  access_token: string;
  token_type: string;
  expires_in: number;
}

interface CachedToken {
  value: string;
  refreshAt: number;
}

export function createIdempotencyKey(): string {
  return randomUUID();
}

export function segment(value: string | number): string {
  const text = String(value);
  if (!text || text === "." || text === ".." || /[\u0000-\u001f\u007f/\\]/u.test(text)) {
    throw new TypeError("接口标识格式不正确");
  }
  return encodeURIComponent(text);
}

export function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isToken(value: unknown): value is Token {
  return object(value) && typeof value.access_token === "string" && value.access_token.length > 0
    && typeof value.token_type === "string" && value.token_type.toLowerCase() === "bearer"
    && typeof value.expires_in === "number" && Number.isSafeInteger(value.expires_in)
    && value.expires_in > 0;
}

export function externalSubject(value: string): string {
  if (!/^[A-Za-z0-9_.:@-]{1,64}$/u.test(value)) {
    throw new TypeError("用户编号须使用 1 至 64 个英文数字、点、下划线、冒号、@ 或连字符");
  }
  return value;
}

function secureUrl(value: string): URL {
  const url = new URL(value);
  const localHttp = url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (url.username || url.password || (url.protocol !== "https:" && !localHttp)) {
    throw new TypeError("需要 HTTPS 地址，本机回环地址可使用 HTTP");
  }
  return url;
}

interface Call {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  query?: Query;
  options?: ReadOptions | WriteOptions | undefined;
  token?: string;
  nonMutation?: true;
}

export class Transport {
  readonly #origin: URL;
  readonly #fetch: typeof fetch;
  readonly #timeoutMs: number;
  readonly #appKey: string;
  readonly #appSecret: string;
  #cached: CachedToken | undefined;
  #loading: Promise<CachedToken> | undefined;

  constructor(options: ClientOptions) {
    this.#origin = secureUrl(options.origin);
    if (this.#origin.pathname !== "/" || this.#origin.search || this.#origin.hash) {
      throw new TypeError("工作室地址只能包含协议和域名");
    }
    if (!options.appKey || !options.appSecret) {
      throw new TypeError("工作室应用凭据缺失");
    }
    this.#appKey = options.appKey;
    this.#appSecret = options.appSecret;
    this.#fetch = options.fetch ?? globalThis.fetch;
    this.#timeoutMs = options.timeoutMs ?? 10_000;
    if (!Number.isSafeInteger(this.#timeoutMs) || this.#timeoutMs < 1 || this.#timeoutMs > 2_147_483_647) {
      throw new TypeError("请求超时须为合法的正整数毫秒数");
    }
  }

  async call<T>(path: string, call: Call = {}): Promise<T> {
    const url = new URL(path, this.#origin);
    if (url.origin !== this.#origin.origin) {
      throw new TypeError("接口路径必须属于工作室域名");
    }
    for (const [key, value] of Object.entries(call.query ?? {})) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }

    const method = call.method ?? "GET";
    const write = method !== "GET" && !call.nonMutation;
    const headers = new Headers({ Accept: "application/json" });
    if (write) {
      const key = call.options && "idempotencyKey" in call.options ? call.options.idempotencyKey : "";
      if (typeof key !== "string" || !/^[!-~]{8,128}$/u.test(key)) {
        throw new TypeError("写请求需要 8 至 128 字符的 Idempotency-Key");
      }
      headers.set("Idempotency-Key", key);
      if (call.options && "actor" in call.options && call.options.actor !== undefined) {
        headers.set("X-TT-Actor", externalSubject(call.options.actor));
      }
    }
    const init: RequestInit = { method, headers };
    if (call.body !== undefined) {
      headers.set("Content-Type", "application/json");
      init.body = JSON.stringify(call.body);
    }
    const signal = call.options?.signal;
    signal?.throwIfAborted();
    const token = call.token === undefined ? await this.#appToken() : undefined;
    headers.set("Authorization", "Bearer " + (call.token ?? token?.value ?? ""));
    let reply = await this.#json(url, init, write, signal);
    if (reply.response.status === 401 && token) {
      if (this.#cached === token) this.#cached = undefined;
      signal?.throwIfAborted();
      headers.set("Authorization", "Bearer " + (await this.#appToken()).value);
      reply = await this.#json(url, init, write, signal);
    }
    const { response, payload } = reply;
    if (!response.ok) {
      const kind = response.status === 401 ? "auth" : response.status === 429 ? "rate_limit" : "http";
      throw this.#responseError(kind, response, payload, write && response.status >= 500);
    }
    if (!object(payload) || typeof payload.code !== "number" || !("data" in payload)) {
      throw this.#responseError("invalid_response", response, payload, write);
    }
    if (payload.code !== 0) throw this.#responseError("business", response, payload);
    return payload.data as T;
  }

  async upload(url: string, body: Uint8Array, contentType: string, options?: ReadOptions): Promise<void> {
    const bytes = body.buffer instanceof ArrayBuffer
      ? new Uint8Array(body.buffer, body.byteOffset, body.byteLength)
      : Uint8Array.from(body);
    await this.#request(secureUrl(url), {
      method: "PUT",
      headers: { "Content-Type": contentType },
      body: bytes,
    }, true, options?.signal, async (response) => {
      await response.body?.cancel();
      if (!response.ok) throw this.#responseError("http", response, null, response.status >= 500);
    });
  }

  #appToken(): Promise<CachedToken> {
    if (this.#cached && this.#cached.refreshAt > Date.now()) return Promise.resolve(this.#cached);
    this.#loading ??= this.#exchange().finally(() => { this.#loading = undefined; });
    return this.#loading;
  }

  async #exchange(): Promise<CachedToken> {
    const url = new URL("/api/open/oauth/token", this.#origin);
    const start = Date.now();
    const { response, payload } = await this.#json(url, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "client_credentials",
        client_id: this.#appKey,
        client_secret: this.#appSecret,
      }),
    }, false);
    if (!response.ok) {
      const kind = response.status === 429 ? "rate_limit" : response.status === 401 ? "auth" : "http";
      throw this.#responseError(kind, response, payload);
    }
    if (!isToken(payload)) {
      throw this.#responseError("invalid_response", response, payload);
    }
    const lifetime = payload.expires_in * 1000;
    const token = { value: payload.access_token, refreshAt: start + lifetime - Math.min(30_000, lifetime / 10) };
    this.#cached = token;
    return token;
  }

  #json(url: URL, init: RequestInit, write: boolean, signal?: AbortSignal) {
    return this.#request(url, init, write, signal, async (response) => {
      try {
        return { response, payload: await response.json() as unknown };
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
        return { response, payload: null };
      }
    });
  }

  async #request<T>(
    url: URL,
    init: RequestInit,
    write: boolean,
    signal: AbortSignal | undefined,
    read: (response: Response) => Promise<T>,
  ): Promise<T> {
    const deadline = AbortSignal.timeout(this.#timeoutMs);
    const abort = signal ? AbortSignal.any([signal, deadline]) : deadline;
    abort.throwIfAborted();
    try {
      const response = await this.#fetch(url, { ...init, cache: "no-store", redirect: "error", signal: abort });
      return await read(response);
    } catch (error) {
      if (error instanceof TuantuantongError) throw error;
      const kind = deadline.aborted ? "timeout" : "network";
      const message = signal?.aborted ? "调用已取消" : kind === "timeout" ? "团团通请求超时" : "团团通连接失败";
      throw new TuantuantongError(kind, message, { outcomeUnknown: write });
    }
  }

  #responseError(kind: ErrorKind, response: Response, payload: unknown, outcomeUnknown = false) {
    const data = object(payload) ? payload : {};
    const message = data.msg ?? data.error_description;
    const code = data.code ?? data.error;
    return new TuantuantongError(
      kind,
      typeof message === "string" ? message : "团团通请求失败（HTTP " + response.status + "）",
      {
        httpStatus: response.status,
        ...(typeof code === "number" || typeof code === "string" ? { code } : {}),
        requestId: response.headers.get("x-request-id"),
        retryAfter: response.headers.get("retry-after"),
        outcomeUnknown,
        ...((code === 1022 || code === 1023) && object(data.data) && typeof data.data.order_no === "string"
          ? { createdOrderNo: data.data.order_no }
          : {}),
      },
    );
  }
}
