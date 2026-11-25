# Web K 本地预览适配

上游为 [Telegram Web K](https://github.com/TelegramOrg/Telegram-web-k)，使用 GPL-3.0-only 许可证。
`tuantuantong.patch` 包含这个预览所需的启动和聊天适配，按上游提交
`2e4bbb52b6aec5e95ee32e5c2882c1f05de32200` 生成。单独检出上游，应用补丁：

```sh
git clone https://github.com/TelegramOrg/Telegram-web-k.git /tmp/tt-webk-preview
git -C /tmp/tt-webk-preview checkout --detach 2e4bbb52b6aec5e95ee32e5c2882c1f05de32200
git -C /tmp/tt-webk-preview apply /absolute/path/to/tuantuantong/src/webk-preview/tuantuantong.patch
cd /tmp/tt-webk-preview
pnpm install --frozen-lockfile
NODE_ENV=development TWEB_PREVIEW=1 pnpm exec vite build --config vite.local-chat.config.ts
```

本地预览通过 `VITE_INTERNAL_CHAT_WEBK_ORIGIN` 指向构建服务。适配只接受已列入白名单的本机后台来源。

## 正式站构建

在团团通仓库执行 `bash src/webk-preview/build-production.sh <新输出目录>`。脚本按上述上游提交按顺序应用预览、正式启动和聊天功能补丁，使用锁定依赖构建同源的 `/internal-chat-webk/`。已有上游检出可用 `TT_WEBK_SOURCE` 指定，每次仍从固定提交开始。

正式产物只运行团团通实时聊天，检查父页面同源，保留上游许可证和提交号。分发时将固定上游源码和构建脚本中列出的补丁作为对应源码交付。站点准备见 [部署说明](../../deploy/internal-chat/README.md)。

## 重开会话回归

浏览器用例使用正式构建产物和合成消息，检查首开、切走后切回、隐藏后重显、向上翻页、指定消息定位及 H5 未读定位。覆盖私聊、订单群、订单详情中的只读聊天记录和空历史。默认使用 Playwright 安装的 Chromium。如需使用系统 Chrome，在命令前加 `TT_WEBK_BROWSER_CHANNEL=chrome`。

```sh
node src/webk-preview/tests/reopen-history.cjs /absolute/path/to/webk-dist
```
