import "server-only";
import {
  InvalidWebhookError,
  TuantuantongError,
} from "@tuantuantong/sdk";
import type { ChangeWebhook, TuantuantongClient } from "@tuantuantong/sdk";

interface Session {
  subject: string;
  role: "operator" | "taker";
}

interface Dependencies {
  client: TuantuantongClient;
  // 校验是本工作室的登录用户。
  session(request: Request): Promise<Session | null>;
  // 业务提交编号按工作室、登录人、动作和资源关联，持久保存为团团通操作键。
  operationKey(input: {
    subject: string;
    submissionId: string;
    action: "assign" | "claim";
    orderNo: string;
  }): Promise<string>;
  // 在持久存储按工作室和 event_id 去重，持久化后才返回。
  enqueueChangeOnce(change: ChangeWebhook): Promise<void>;
}

function submissionId(request: Request): string | null {
  const value = request.headers.get("X-Submission-Id");
  return value && /^[A-Za-z0-9_-]{8,128}$/u.test(value) ? value : null;
}

function reportError(error: unknown): Response {
  if (!(error instanceof TuantuantongError)) throw error;
  if (error.details.outcomeUnknown) {
    return Response.json({ error: "operation_unknown" }, { status: 503 });
  }
  if (error.kind === "rate_limit") {
    const retryAfter = error.details.retryAfter;
    return Response.json({ error: "rate_limited" }, {
      status: 429,
      headers: retryAfter ? { "Retry-After": retryAfter } : {},
    });
  }
  if (error.kind === "business") {
    return Response.json({ error: "order_action_rejected", code: error.details.code }, { status: 409 });
  }
  return Response.json({ error: "integration_unavailable" }, { status: 502 });
}

export function createHandlers(deps: Dependencies) {
  return {
    async assign(request: Request, orderNo: string, takerId: number): Promise<Response> {
      const session = await deps.session(request);
      if (session?.role !== "operator") return new Response(null, { status: 403 });
      const id = submissionId(request);
      if (!id || !Number.isSafeInteger(takerId) || takerId < 1) {
        return new Response(null, { status: 400 });
      }

      const idempotencyKey = await deps.operationKey({
        subject: session.subject,
        action: "assign",
        submissionId: id,
        orderNo,
      });
      try {
        const order = await deps.client.orders.assign(orderNo, { taker_id: takerId }, {
          actor: session.subject,
          idempotencyKey,
        });
        return Response.json({ order_no: order.order_no, status: order.status });
      } catch (error) {
        return reportError(error);
      }
    },

    async claim(request: Request, orderNo: string): Promise<Response> {
      const session = await deps.session(request);
      if (session?.role !== "taker") return new Response(null, { status: 403 });
      const id = submissionId(request);
      if (!id) return new Response(null, { status: 400 });

      const idempotencyKey = await deps.operationKey({
        subject: session.subject,
        action: "claim",
        submissionId: id,
        orderNo,
      });
      try {
        const order = await deps.client.forTaker(session.subject).hall.claim(orderNo, { idempotencyKey });
        return Response.json({ order_no: order.order_no, status: order.status });
      } catch (error) {
        return reportError(error);
      }
    },

    async callback(request: Request): Promise<Response> {
      let change: ChangeWebhook;
      try {
        change = deps.client.sync.verifyWebhook({ body: await request.text(), headers: request.headers });
      } catch (error) {
        if (error instanceof InvalidWebhookError) return new Response(null, { status: 400 });
        throw error;
      }

      await deps.enqueueChangeOnce(change);
      return new Response(null, { status: 204 });
    },
  };
}
