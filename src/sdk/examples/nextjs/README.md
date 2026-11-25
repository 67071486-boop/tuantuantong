# Next.js 接入示例

将 `studio.ts` 与 `handlers.ts` 放进 Next.js 项目的服务端模块目录。配置 `TT_STUDIO_ORIGIN`、`TT_APP_KEY` 和 `TT_APP_SECRET`，并为 App Router 的 API 和回调 Route Handler 选择 `nodejs` 运行时。示例使用应用自己的会话和数据库接入点。

| 接入点 | 用现有项目的什么能力 |
|---|---|
| `session` | 验证当前用户和工作室归属，返回稳定的登录用户编号及运营或打手身份。 |
| `operationKey` | 用工作室、登录人、动作、资源、客户端提交编号查找或原子创建操作键。同一个提交编号须使用同一份请求内容。 |
| `enqueueChangeOnce` | 按工作室和 `event_id` 唯一记录，并在数据库提交后再回应回调。后台任务按订单号或打手编号重新查数据。 |

运营 Route Handler 解析路径中的团团通订单号和请求里的打手编号，再调用 `assign`。打手 Route Handler 只解析订单号，调用 `claim` 时由登录会话确定打手身份。每个工作室的回调路由用服务端配置选定对应 SDK 客户端，再调用 `callback`。如果接多个工作室，按每个用户实际所属的工作室选择客户端。

客户端提交按钮每次产生一个新的 `X-Submission-Id`。因断网或超时重传同一次提交时，继续沿用原本的提交编号。收到结果待确认时，用同一编号查询或重试。
