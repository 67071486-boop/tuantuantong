# AGENTS.md — `src/frontend/src/docs/` 的目录规则

> 这个目录里是**两份对外契约文档的逐字节副本**（页面 `?raw` import 它们）。
> 每一份都写明：**出处**、**怎么改**、**谁在守"两份一样"**。

## 一、`open-api.md`（**逐字节副本，⛔ 不要手改**）

- **出处**（唯一权威）：仓库根的 **`docs/OPEN-API.md`**（**上家侧**对外 API 接入契约）。
- **这里这份**：`src/docs/open-api.md`，是它的**逐字节副本**。
- ⛔ **不要直接编辑这个副本**：改了它，出处没改 ⇒ 下一个同步的人会把你的改动**整个覆盖掉**，
  而且期间页面显示的是"另一份文档"（静默漂移）。
- ✅ **正确的改法**：改 `docs/OPEN-API.md`，然后在 `src/frontend/` 下重跑一次
  ```bash
  cp ../../docs/OPEN-API.md src/docs/open-api.md
  ```
- **读到它的页面**：`src/layui-island/pages/open-apps.ts`（【开放平台】→「应用管理」的「API 文档」弹层）。

## 二、`taker-api.md`（**逐字节副本，⛔ 不要手改**）

- **出处**（唯一权威）：仓库根的 **`docs/TAKER-API.md`**（**打手侧**对外 API 接入契约）。
- **这里这份**：`src/docs/taker-api.md`，是它的**逐字节副本**。
- ⛔ **不要直接编辑这个副本**（理由与上面**逐字相同**：出处没改 = 下一个同步的人会覆盖你的改动
  ＋ 期间页面显示的是"另一份文档"）。
- ✅ **正确的改法**：改 `docs/TAKER-API.md`，然后在 `src/frontend/` 下重跑一次
  ```bash
  cp ../../docs/TAKER-API.md src/docs/taker-api.md
  ```
- **读到它的页面**：`src/layui-island/pages/taker-apps.ts`（【开放平台】→「打手对接」的
  「接入文档」弹层）。
- ⚠️ 两份文档是**两件事**：`OPEN-API.md` 是上家侧（`/api/open/…`）、`TAKER-API.md` 是打手侧
  （`/api/taker/v1/…`），凭据**互相不认**。⛔ 别把一份的内容同步到另一份上。

## 为什么要有这两个副本（不是"图方便"）

Vite 对 root（`src/frontend`）**之外**的文件有 fs 限制：`?raw` 直接 import 仓库根那份时，
**`vite build` 能解析、但 `vitest run` 的 jsdom 用例会报 `Error: Denied ID …/docs/OPEN-API.md?raw`**
（2026-10-09 当场实测；机制见 `vite/dist/node/chunks/node.js` 的 `isServerAccessDeniedForTransform`，
它按 `server.fs.allow` 判，默认 allow = 工作区根 = `src/frontend`）。而加载这些页的用例必然是 jsdom
⇒ 只能把内容放到 root 之内。

⚠️ 想让"直接 import 仓库根"这条路成立，得改 `vite.config.ts` 的 `server.fs.allow`
（等于把仓库里的路径加进 **dev server 的可服务面**）——那是**另一件事**，这两页都没有做。
细节与实测读数见 `src/layui-island/pages/open-apps.ts` 文件头那一节。

## 谁在守"两份一样"

每个副本各有**一条**用例用 `fs` **同时读两份、逐字节对拍**（`toEqual`）：

| 副本 | 出处 | 守着它的用例 |
|---|---|---|
| `src/docs/open-api.md` | `docs/OPEN-API.md` | `src/layui-island/pages/__tests__/open-apps.spec.ts` |
| `src/docs/taker-api.md` | `docs/TAKER-API.md` | `src/layui-island/pages/__tests__/taker-apps.spec.ts` |

它红了就说明那两份漂了：**照上面那条 `cp` 重跑一次**，失败信息里也会打这行命令。

⛔ 不许把这类网放宽成"包含即可"或"长度相等"：它的全部价值就是"两份一模一样"。
