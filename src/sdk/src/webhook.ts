import { createHash, createHmac, timingSafeEqual } from "node:crypto";
import type { ChangeWebhook } from "./contracts.js";
import { object } from "./transport.js";

export interface WebhookInput {
  body: string;
  headers: Pick<Headers, "get">;
  maxAgeSeconds?: number;
}

export class InvalidWebhookError extends Error {
  constructor() {
    super("团团通回调报文或签名无效");
    this.name = "InvalidWebhookError";
  }
}

function compareUnicode(a: string, b: string): number {
  const left = Array.from(a, (character) => character.codePointAt(0) ?? 0);
  const right = Array.from(b, (character) => character.codePointAt(0) ?? 0);
  for (let index = 0; index < Math.min(left.length, right.length); index++) {
    const difference = left[index]! - right[index]!;
    if (difference) return difference;
  }
  return left.length - right.length;
}

function canonical(value: unknown): string {
  if (object(value)) {
    return "{" + Object.keys(value).sort(compareUnicode).map(
      (key) => JSON.stringify(key) + ":" + canonical(value[key]),
    ).join(",") + "}";
  }
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (typeof value === "number" && !Number.isSafeInteger(value)) throw new InvalidWebhookError();
  return JSON.stringify(value);
}

function equalHex(a: string, b: string): boolean {
  return timingSafeEqual(Buffer.from(a, "hex"), Buffer.from(b, "hex"));
}

function readWebhook(value: unknown): ChangeWebhook {
  if (!object(value) || value.event !== "integration.change"
      || typeof value.delivery_id !== "string" || !/^[a-f0-9]{32}$/u.test(value.delivery_id)
      || typeof value.sign !== "string" || !/^[a-f0-9]{64}$/u.test(value.sign)
      || typeof value.timestamp !== "number" || !Number.isSafeInteger(value.timestamp)
      || typeof value.event_id !== "string" || !value.event_id
      || typeof value.resource_id !== "string" || !value.resource_id
      || typeof value.occurred_at !== "string"
      || (value.resource !== "order" && value.resource !== "taker")
      || typeof value.action !== "string" || !["created", "updated", "deleted", "restored"].includes(value.action)) {
    throw new InvalidWebhookError();
  }
  return value as unknown as ChangeWebhook;
}

export function verifyChangeWebhook(input: WebhookInput, secret: string): ChangeWebhook {
  let payload: unknown;
  try {
    payload = JSON.parse(input.body);
  } catch {
    throw new InvalidWebhookError();
  }
  const event = readWebhook(payload);
  const { sign, ...body } = event;
  const signature = input.headers.get("x-tt-signature");
  if (!signature || !/^[a-f0-9]{64}$/u.test(signature) || !equalHex(signature, sign)
      || input.headers.get("x-tt-timestamp") !== String(event.timestamp)
      || input.headers.get("x-tt-delivery") !== event.delivery_id) {
    throw new InvalidWebhookError();
  }

  const digest = createHash("sha256").update(canonical(body), "utf8").digest("hex");
  const message = [event.timestamp, event.delivery_id, digest].join("\n");
  const expected = createHmac("sha256", secret).update(message, "utf8").digest("hex");
  if (!equalHex(sign, expected)) throw new InvalidWebhookError();

  const maxAge = input.maxAgeSeconds;
  if (maxAge !== undefined && (!Number.isSafeInteger(maxAge) || maxAge < 1
      || Math.abs(Date.now() / 1000 - event.timestamp) > maxAge)) {
    throw new InvalidWebhookError();
  }
  return event;
}
