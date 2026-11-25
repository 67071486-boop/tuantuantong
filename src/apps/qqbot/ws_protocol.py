"""QQ 机器人 WS 网关的**纯协议层**（不碰 DB、不碰网络，全部可单测）。

帧形状与握手序（官方 v2 网关，2026-09 版任务书给定）：

- op10 hello `{heartbeat_interval: 45000}` → op2 identify
  `{token: "QQBot <access_token>", intents, shard: [0, 1]}`；
- READY（op0, `t="READY"`，`d.session_id` / `d.user`）；
- op1 心跳 `{op: 1, d: <最新 s>}`（首次 `d=null`）→ op11 ack；
- op6 resume `{token, session_id, seq}`；op7 服务端要求重连；op9 invalid session。

断线策略（`worker.py` 落地）：先 resume，失败退避 5/10/30/60 秒（封顶）重新 identify。
"""
from __future__ import annotations

import json

# ── op 码 ──────────────────────────────────────────────────────────────────
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# ── intents（本进程订阅的两个群事件位）────────────────────────────────────
#: 群消息相关：GROUP_AT_MESSAGE_CREATE / GROUP_ADD_ROBOT / GROUP_DEL_ROBOT /
#: GROUP_MSG_REJECT / GROUP_MSG_RECEIVE。
INTENT_GROUP_AND_C2C_EVENT = 1 << 25
#: 群成员相关：GROUP_JOIN_REQUEST。
INTENT_GROUP_MEMBER_EVENT = 1 << 24
#: 本进程订阅的集合（两者都要）。
INTENTS = INTENT_GROUP_AND_C2C_EVENT | INTENT_GROUP_MEMBER_EVENT

# ── 事件名（op0 dispatch 的 `t`）──────────────────────────────────────────
EVENT_READY = "READY"
EVENT_GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"
EVENT_GROUP_ADD_ROBOT = "GROUP_ADD_ROBOT"
EVENT_GROUP_DEL_ROBOT = "GROUP_DEL_ROBOT"
EVENT_GROUP_MSG_REJECT = "GROUP_MSG_REJECT"
EVENT_GROUP_MSG_RECEIVE = "GROUP_MSG_RECEIVE"
EVENT_GROUP_JOIN_REQUEST = "GROUP_JOIN_REQUEST"

#: token 在 identify / resume 帧里的前缀（官方要求，⛔ 不是可省的装饰）。
TOKEN_SCHEME = "QQBot"


def build_identify(token: str, intents: int = INTENTS) -> dict:
    """op2 identify 帧（`shard` 固定单分片 `[0, 1]`——本进程独占一个机器人）。"""
    return {
        "op": OP_IDENTIFY,
        "d": {"token": f"{TOKEN_SCHEME} {token}", "intents": intents, "shard": [0, 1]},
    }


def build_heartbeat(seq: int | None) -> dict:
    """op1 心跳帧：`d` = 收到的最新 `s`；一条都没收到过时是 `None`（官方：首跳 `d=null`）。"""
    return {"op": OP_HEARTBEAT, "d": seq}


def build_resume(token: str, session_id: str, seq: int | None) -> dict:
    """op6 resume 帧（断线重连第一跳；`seq` 同心跳口径，可为 None）。"""
    return {
        "op": OP_RESUME,
        "d": {"token": f"{TOKEN_SCHEME} {token}", "session_id": session_id, "seq": seq},
    }


def parse_frame(raw) -> tuple[int | None, int | None, str | None, dict | None]:
    """把一帧原始文本拆成 `(op, s, t, d)`；畸形帧按 `None` 兜底，⛔ 不抛给调用方。"""
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError):
        return None, None, None, None
    if not isinstance(frame, dict):
        return None, None, None, None
    return frame.get("op"), frame.get("s"), frame.get("t"), frame.get("d")
