# 内部通讯（可选组件）

内部沟通**不是网页前后端自带的**：它由三块额外的东西拼起来。没部署/没配置时，页面会显示
「当前账号暂未开启内部沟通」，订单、结算等其它功能不受影响。

| 组成 | 在仓库里的位置 | 说明 |
|---|---|---|
| 聊天界面 | `src/webk-preview/`（补丁 + 构建脚本） | 上游是 Telegram Web K（GPL-3.0），构建后输出到前端产物的 `/internal-chat-webk/` |
| 实时网关 | `src/chat-realtime/` + 本目录 | Node + Socket.IO，Django 通过 `TT_CHAT_REALTIME_NOTIFY_URL` 推事件 |
| 聊天后端适配（可选） | `src/chat_service/` | 接私有 Matrix 时用；只用内置会话可不起 |

## 1. 构建聊天界面

```sh
# 按 src/webk-preview/README.md 检出固定上游提交、应用补丁、构建；
# 正式站构建脚本把产物打到前端 dist 的 internal-chat-webk/：
bash src/webk-preview/build-production.sh <输出目录>
```

## 2. 起实时网关

```sh
# 网关依赖
cd src/chat-realtime && npm ci

# 环境变量（0600，只给部署用户与 systemd 读）
cat > shared/internal-chat.env <<'EOF'
TT_CHAT_REALTIME_SECRET=<32–128 位 Base64URL 随机串>
TT_CHAT_REALTIME_ORIGINS=https://你的域名
EOF

# systemd：按需改 User / WorkingDirectory 后安装
cp deploy/internal-chat/tuantuantong-chat-realtime.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tuantuantong-chat-realtime
```

多工作室共用一个网关时，改用 `TT_CHAT_REALTIME_TENANTS_FILE` 指向一份 0600 的 JSON：
顶层 `tenants` 按 schema 登记，每条一个独立密钥和一个非空 `origins`，密钥与来源都不许跨工作室复用。
此时从网关环境里移除单租户那两个变量。

## 3. 接进 Django

在该站点的 `.env` 里：

```sh
TT_CHAT_ORDER_GROUPS_ENABLED=1
TT_CHAT_REALTIME_SECRET=<与网关同一份>
TT_CHAT_REALTIME_NOTIFY_URL=http://127.0.0.1:4191/notify   # 网关实际监听地址
```

## 4. nginx

把 [nginx-location.conf](nginx-location.conf) 的内容放进该站点的 HTTPS `server` 块（按需改端口），
`nginx -t` 通过后再 reload。它做两件事：静态托管 `/internal-chat-webk/`，把 `/socket.io/` 反代到本机网关。

## 5. 发布与自检

- 启用后每次发布都要带上聊天产物（构建脚本见上），否则发版检查会以「发布包缺少聊天界面」失败。
- [chat-gateway-release.py](chat-gateway-release.py) 是发版时的网关自检工具（校验运行版本、HTTPS 来源握手、Django→网关密钥）。
- [internal-chat-gateway.sh](internal-chat-gateway.sh) 是发布脚本 source 的辅助函数库（加锁、校验产物、切换网关）。

## 回退

把 `TT_CHAT_ORDER_GROUPS_ENABLED` 设回 `0`、撤掉 nginx 片段与网关环境文件，再按站点流程加载配置。
早期版本没有网关时，回退会关闭聊天入口；订单与结算不受影响。

## 许可证

聊天界面来自 Telegram Web K（**GPL-3.0-only**）。分发构建产物时要保留上游许可证与对应源码，
构建脚本和补丁清单见 `src/webk-preview/README.md`。
