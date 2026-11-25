"""鸡蛋铺**回调接收器**（`callback.suqing.chat`，2026-10-08 业务方决定落地）。

本 app **只有三件事**：① 把收到的原始报文逐字节留档；② 验 `sign`；③ 快速回 200。

⚠️ **它不是一个业务 app**：没有模型、没有迁移、没有 DRF 端点、不进 `config/urls.py`、
不参与租户解析、**不碰数据库**。它是挂在 `127.0.0.1:8002` 上的一个独立 WSGI 入口
（`apps.callback.wsgi`，由 `deploy/systemd/tuantuantong-callback.service` 起）。

设计口径（字段、签名算法、为什么只当"提醒"不当真相、未定项与代价）见
`docs/specs/2026-10-08-jidanpu-callback-receiver-design.md`；运维口径见 `docs/RUNBOOK.md`。
"""
