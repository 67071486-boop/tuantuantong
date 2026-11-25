# 团团通 Node.js SDK

工作室运营使用应用凭据调用订单和打手业务。打手端使用已绑定的 App 登录账号接单和交单。包安装在 Next.js 的 Node.js 服务端，从环境配置载入工作室域名和凭据。

需要 Node.js 22.18 或更高版本。包本身无运行时依赖，内网分发可在本目录运行 `npm ci`、`npm test` 和 `npm pack`，再在 Next.js 项目安装产出的 tarball。

## 接入

- 在团团通测试工作室开通业务应用并保存应用密钥。
- 将客户端放在 Next.js 服务端模块，保存配置的工作室域名和密钥。手机端调用 App 自己的登录及业务接口。
- 工作室运营用 `orders` 和 `takers`，打手用 `forTaker(登录用户编号)` 下的 `hall` 或 `myOrders`。游戏、区服、项目和订单来源从 `catalog` 读取。SDK 在服务端自动换取令牌。
- 同一次写操作先持久保存 `createIdempotencyKey()` 返回的操作编号。网络失败和超时用同一个编号及请求体重试。若 `TuantuantongError.details.outcomeUnknown` 为真，按结果待确认处理。应用写入记录发起人时，可传入服务端登录态的 `actor`。
- `uploadImage` 的重试沿用原操作编号、图片和备注。即使上传地址已经过期，SDK 也会尝试用原认领编号取回确认结果。调用方取消请求时停止后续认领。
- 如果确认文件已经上传，可以直接用原对象名、文件名、备注和认领编号调用 `claimImage`。分步上传时先保存 `issueImageTicket` 返回的对象名及用于认领的操作编号。若前一次使用 `uploadImage`，可用 `imageUploadKeys(原操作编号)` 取回 `ticketKey` 和 `claimKey`，用原扩展名和 `ticketKey` 重取原票据，再用 `claimKey` 认领。文件确实未上传且旧票过期时，使用新操作编号取新票。
- 回调通过 `sync.verifyWebhook` 验证。投递与补拉共用 `event_id`，用工作室加事件编号做持久去重，提交成功后再返回 2xx。
- 初始化同步先调用 `sync.changes({ start: "now" })` 保存起点，再按订单号和打手编号扫描列表，最后从起点补拉。达到本页末尾时使用响应的 `next_cursor` 继续，保存业务数据和进度应在同一次提交。收到 410 时重建列表并开启新进度。

参见 [OpenAPI 接口说明](openapi.yaml) 和 [Next.js 示例](examples/nextjs/README.md)。
