"""`manage.py callback_events` —— 把回调接收器落盘的事件读出来看（排查用）。

    用法（在生产机器上，于 `$R/current/src` 下）：

    ```sh
    uv run python manage.py callback_events --limit 20 # 最近 20 条，一行一条摘要
    uv run python manage.py callback_events --bad-sign # 只看验签没过的
    uv run python manage.py callback_events --limit 5 --raw # 看完整记录（含原文那句 JSON）
    ```

    ## 两条口径（⛔ 别改）

    1. **只读**、不写、不删、不碰数据库：事件文件的唯一写入方是接收器自己
    （`apps/callback/wsgi.py`）。这个命令连 Django 的库都不查 —— `apps.callback` 没有模型。
    2. **⛔ 不打印任何密钥**：接收器从来不把 `clientSecret` 写进记录，这个命令也**不读**密钥文件；
    另外对"键名里带 `secret` 的字段"统一打码 —— 万一第三方哪天在回调体里回显了密钥，
    也不至于被一条排查命令抄进工单 / 聊天记录里。
    ⚠️ 注意：**打码只按键名**（`secret` 子串，不分大小写）。`sign` **不打码** ——
    它是排查验签问题的第一手线索，且它本身不是密钥（它是"待签串 + 密钥"的一次 MD5）。

    ## 为什么 `--limit` 取的是**最新** N 条

    排查问的永远是"刚刚那一条怎么了"。文件是追加写的，最新的一定在末尾。
    ⚠️ 实现上会**把整个文件读一遍**（`iter_lines`）再取尾部 N 条 —— 第一版回调量很小
    （第三方自己限流 10 次/秒、且这个口只有它一个调用方），几十 MB 的文件用不着索引。
    代价如实登记在 spec：文件长得很大时这个命令会变慢，届时再考虑 `tail` 式读法。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand

from apps.callback import config, store

#: 键名里含它（不分大小写）的字段，打印时值一律替换成这个占位串。
_REDACTED = "***"
_REDACT_KEY_MARKER = "secret"

#: `--raw` 里 `body_b64` 的占位串（逐字节原文，没法按键名打码 ⇒ 整个省略）。
_OMITTED = "<已省略：逐字节原文请直接读事件文件>"


def _redact(value: Any) -> Any:
    """递归打码：**只按键名**（含 `secret`）替换值，⛔ 不猜值的形状。"""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _REDACT_KEY_MARKER in str(key).lower() else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _redact_record(record: dict[str, Any]) -> dict[str, Any]:
    """`--raw` 用的"可打印"版本。两处**按键名打码盖不到**的地方单独处理：

    ① `body_text` 是一段**字符串**（报文的原文）——按键名打码递归不进字符串里
    ⇒ 报文里若出现 `{"clientSecret": "…"}`（第三方回显密钥），它会原样被打出来。
    处理：能解成对象就按**打码后**重新序列化一份（这只影响 `--raw` 的显示，
    ⛔ 不碰磁盘上的记录）。
    ② `body_b64` 是**逐字节原文**，没有任何"键名"可依据 ⇒ **整个省略**，
    要看逐字节原文就直接读事件文件（这个命令本来就是为了少打一点字）。
"""
    redacted = _redact(record)
    text = record.get("body_text")
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            redacted["body_text"] = json.dumps(_redact(parsed), ensure_ascii=False)
    if record.get("body_b64") is not None:
        redacted["body_b64"] = _OMITTED
    return redacted


def _body_params(record: dict[str, Any]) -> dict[str, Any] | None:
    """从**落盘的原文**里解出字段（只为显示；解不出来就回 None）。"""
    text = record.get("body_text")
    if not isinstance(text, str):
        return None
    try:
        params = json.loads(text)
    except json.JSONDecodeError:
        return None
    return params if isinstance(params, dict) else None


def _summary_line(record: dict[str, Any], raw_line: str) -> str:
    """一行摘要。⚠️ 缺字段时打 `-`（⛔ 不静默留空 —— 空与"没这个字段"要能分辨）。"""
    params = _body_params(record) or {}
    sign_state = "ok" if record.get("sign_ok") else f"FAIL({record.get('sign_reason') or '?'})"
    fields = [
        str(record.get("received_at") or "-"),
        str(record.get("source_ip") or "-"),
        f"action={params.get('action', '-')}",
        f"sign={sign_state}",
        f"len={record.get('body_len', '-')}",
        f"orderNo={params.get('orderNo', '-')}",
        f"sourceOrderNo={params.get('sourceOrderNo', '-')}",
        f"status={params.get('status', '-')}",
    ]
    if params.get("amount") is not None:
        fields.append(f"amount={params.get('amount')}")
    note = params.get("note")
    if note not in (None, ""):
        fields.append(f"note={note}")
    if not record.get("json_ok"):
        fields.append("[报文不是 JSON 对象]")
    if record.get("sign_ok_declared_fields_only"):
        # 主判（全字段）没过、但"只算文档字段表"能过 ⇒ **报文里有字段没被对方签进签名**：
        # 可能是注入（攻击者加了一个字段）、也可能双方对"哪些字段参与"的口径不同。
        # ⚠️ 这个字段的含义**反过来了**（原来主判只算文档表，那时它表示"对方加了字段"）。
        fields.append("[只算文档字段表能通过 ⇒ 报文里有字段没被对方签进签名]")
    if not record.get("body_stored"):
        fields.append("[原文未落盘：超限]")
    return "  ".join(fields)


class Command(BaseCommand):
    help = "查看鸡蛋铺回调接收器落盘的事件（共享文件 shared/callback/events.jsonl；只读、不打印密钥）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="看最新的多少条（默认 20；<=0 表示不限）",
        )
        parser.add_argument(
            "--path",
            default=None,
            help=f"事件文件路径（默认 {config.events_path()}；也可用环境变量 CALLBACK_EVENTS_PATH 覆盖）",
        )
        parser.add_argument(
            "--bad-sign",
            action="store_true",
            help="只看验签没通过的（sign_ok=false）",
        )
        parser.add_argument(
            "--raw",
            action="store_true",
            help="打印完整记录（JSON，含 base64 原文；键名带 secret 的字段已打码）",
        )

    def handle(self, *args, **options):
        path = Path(options["path"]) if options["path"] else config.events_path()
        limit = options["limit"]
        only_bad = options["bad_sign"]
        raw = options["raw"]

        listed: list[tuple[dict[str, Any] | None, str]] = []
        bad_lines = 0
        total = 0
        passed = 0
        failed = 0
        for line in store.iter_lines(path):
            if not line.strip():
                continue
            total += 1
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                parsed = None
            if not isinstance(parsed, dict):
                # ⚠️ 坏行**照样列出来**：并发写坏一行正是这个命令最该暴露的东西。
                bad_lines += 1
                listed.append((None, line))
                continue
            if parsed.get("sign_ok"):
                passed += 1
            else:
                failed += 1
            if only_bad and parsed.get("sign_ok"):
                continue
            listed.append((parsed, line))

        shown = listed if limit is None or limit <= 0 else listed[-limit:]
        for record, line in shown:
            if record is None:
                self.stdout.write(f"[坏行 / 不是 JSON 对象] {line[:200]!r}")
            elif raw:
                self.stdout.write(json.dumps(_redact_record(record), ensure_ascii=False, indent=2))
            else:
                self.stdout.write(_summary_line(record, line))

        self.stdout.write("")
        self.stdout.write(f"文件：{path}")
        if total == 0:
            # ⚠️ "文件不存在"与"文件存在但为空"要能分辨（本仓口径：静默空比报错更贵）。
            self.stdout.write(
                self.style.WARNING(
                    f"一条事件都没有（{path} 不存在、或是空文件）⇒ 接收器还没收到过回调？"
                )
            )
            return
        self.stdout.write(
            f"共 {total} 行（坏行 {bad_lines}）；验签通过 {passed} / 未通过 {failed}；"
            f"本次显示 {len(shown)} 条"
            + ("（已按 --bad-sign 过滤）" if only_bad else "")
        )
        if bad_lines:
            self.stdout.write(
                self.style.ERROR(
                    f"⚠️ 有 {bad_lines} 行不是合法的 JSON 对象 —— 那通常是**并发写坏了行**"
                    "（或文件被外部程序改过）。请把这几行原文保留下来排查。"
                )
            )
