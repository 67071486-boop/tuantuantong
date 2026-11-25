# frontend — 租户后台 SPA

Vue 3 + Vite + Element Plus 的租户后台前端（`src/frontend/`）。
开发期不配 CORS：前端与 Django 同源，请求先到 Vite，再由 Vite 代理转给 Django，
并在转发时把**租户域名**写进 `Host` 头——`django-tenants` 靠这个头解析租户。

后端（Django）怎么装、怎么跑见仓库根 [`README.md`](../../README.md)。

## 前置

| 依赖 | 版本 | 来源 |
|---|---|---|
| Node | v24.21.0（本机 `node -v` 实测） | `package.json` 的 `engines` 要求 `^22.18.0 \|\| >=24.12.0` |
| pnpm | 12.4.2 | `package.json` 的 `packageManager` 已锁，用 corepack 即可 |
| 后端 | Django 监听 `127.0.0.1:8000` | 仓库根 `README.md` 的「快速开始」 |

```sh
pnpm install
```

## 本地开发租户（一次性，幂等）

Vite 代理会把**转发给 Django 的 `Host`** 改成 `VITE_TENANT_HOST`（默认 `dev.test.local`）。
**必须存在对应的 `Domain` 行**，否则请求虽然过了代理，仍会因为没有租户而 404。

```sh
cd ..   # 到 src/
uv run python manage.py shell -c \
  "from apps.platform.provisioning import provision_tenant; provision_tenant(schema_name='dev', name='本地开发租户', domain='dev.test.local')"
```

> **本机个人差异不进本文档。** 若你的 `uv` 不在 `PATH`（或沙箱下必须设 `UV_CACHE_DIR`），
> 绝对路径与标准写法见仓库根的 `AGENTS.local.md`——那份文件被 `.gitignore` 忽略，只影响本机。

重复执行安全：首次调用建 `dev` schema、跑迁移并灌基线数据；之后只在 `Domain` 缺失时补建。

## 起开发服务

两个终端：

```sh
# 终端 1（在 src/ 下）：后端
uv run python manage.py runserver

# 终端 2（在 src/frontend/ 下）：前端
pnpm dev
```

打开 Vite 打印出来的地址（默认 <http://localhost:5173/>）。
**以打印为准**：端口被占用时 Vite 会自动换端口。
另外 Vite 默认只监听 `localhost`，本机实测解析到 IPv6 `::1`，
所以 `http://127.0.0.1:5173` 连不上——用打印出来的那个地址。

### 让同一 WiFi 下的另一台设备访问

```sh
pnpm dev:lan          # = vite --host 0.0.0.0，只影响这一次运行
```

Vite 会多打印一行 `Network: http://<本机局域网 IP>:5173/`，用那个地址在另一台设备上打开。
本机 IP 用 `ipconfig getifaddr en0` 查（路由器的 DHCP 可能换 IP，打不开时先核对）。

两点须知：

- **不需要改后端**。Django 只监听 `127.0.0.1:8000`、**不对外暴露**；租户 Host 是 Vite 在服务端
  注入的，所以另一台设备用什么地址访问都能正确解析到租户，`DJANGO_ALLOWED_HOSTS` 不用动。
- **Vite 自己的 host 检查不会拦**：读 Vite 8.3.0 源码，判定里对 IP（IPv4/IPv6）直接放行
  （`vite/dist/node/chunks/node.js:16472`），只有**域名**才需要进 `server.allowedHosts`。

> ⚠️ **安全边界**：此项目此刻**还没有登录认证**（P5 才做），所以能用这个地址访问的人**都能看到后台**。
> 只在**可信网络**（家里 / 自己办公室的 WiFi）上开；不要在公共 WiFi 上开。
> 默认的 `pnpm dev` **不**暴露网络，是刻意保持的——要暴露必须显式敲 `dev:lan`。

## 代理与租户 `Host`

规则写在 [`vite.config.ts`](vite.config.ts)（含逐行理由与源码行号），这里只说结论：

- `/healthz`（存活探针，首页连通性自检打的就是它）与 `/api`（P2 预留）代理到 `http://127.0.0.1:8000`；
- 租户 Host 通过代理的 `headers: { Host: tenantHost }` 注入（**无条件**合并进出站请求头），
  `changeOrigin` 保持 `false`——它是把 Host 改成**目标**地址，正好是这里不想要的；
- 此刻后端**还没有** `/api` 路由，所以经代理访问 `/api` 返回 **404 是预期**，不是代理没生效。

排障注意：**不要**用 `curl -H 'Host: dev.test.local' http://localhost:5173/...` 去测。
那个 `Host` 是发给 **Vite** 的，会被 Vite 自己的 host 检查拦成 403，既测不到 Django 又会误导排查。
要测代理，就别带 `Host`：

```sh
PORT=5173   # 换成 Vite 打印出来的端口
curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:$PORT/healthz"   # 200
```

## 环境变量

模板见 [`.env.example`](.env.example)（复制为 `.env.local`，`.env.local` 已被 `.gitignore` 忽略）。

| 变量 | 说明 |
|---|---|
| `VITE_TENANT_HOST` | 开发期代理注入的租户 Host；必须与本地租户的 `Domain` 行一致。**只有 `vite.config.ts`（Node 侧）读它**，前端代码不得读 |
| `VITE_API_BASE` | API 前缀；留空表示同源根路径 |
| `TENANT_WEB_BASE` | 前端挂载前缀（Vite 的 `base`）；默认 `/`，挂到子路径时设 `/console/` 之类。刻意**不带 `VITE_` 前缀**——只有 Node 侧配置读它，浏览器侧读 Vite 注入的 `import.meta.env.BASE_URL`（见 `src/router/index.ts`）。⚠️ 名字刻意项目专有：`loadEnv` 用空前缀，**shell/CI 里的同名变量会静默覆盖**它（这也是它不叫 `APP_BASE` 的原因） |

注意 `vite.config.ts` 跑在 Node 里，读环境变量用的是 `loadEnv(mode, process.cwd(), '')`，
**不是** `import.meta.env`（那个在配置里不可用）。`VITE_API_BASE` / `VITE_TENANT_HOST` 的类型声明在
[`env.d.ts`](env.d.ts)（该文件用 `ViteTypeOptions.strictImportMetaEnv` 关掉了 Vite 的宽松索引签名，
所以**新增任何 `VITE_` 变量都必须在那里声明**，否则使用处直接报错）。

`.env.local` 的新增 / 修改 / **删除**都会让 Vite 自动重启并重新应用（本机 Vite 8.3.0 实测：
三种情形都打印 `.env.local changed, restarting server...`，删除后代理回到缺省 `dev.test.local`；
源码依据 `node_modules/vite/dist/node/chunks/node.js`——watcher 把 env 文件加进 chokidar（L24291）、
`unlink` 事件接到 `handleFileAddUnlink(…, true)`（L24502–24504）、`handleHMRUpdate` 的 `isEnv` 命中即重启（L24897–24898））。
若改了值却像是没生效，**先直接重启 `pnpm dev`**；再检查 shell 里是否 `export` 过同名的
`VITE_TENANT_HOST`——`loadEnv` 让**已存在的 `process.env` 覆盖 `.env` 文件的值**，那样是文件被忽略，不是没重启。

## 门禁

```sh
pnpm typecheck && pnpm test && pnpm build && pnpm lint
```

- `build` = `vue-tsc --build --noEmit` **然后** `vite build`（顺序见下）；
- `eslint . --fix` 会**就地改文件**，跑完记得看 `git status`。

### 坑 1：自动引入的声明文件要提交

Element Plus 是**按需引入**的（`unplugin-auto-import` / `unplugin-vue-components`，见 `vite.config.ts`）。
代码里**首次**使用某个自动引入的符号（组件如 `el-menu`，API 如 `ElMessage`）之后：

1. 跑一次 `pnpm build`，让插件重新生成根目录的 `auto-imports.d.ts` / `components.d.ts`；
2. 把这两个重新生成的文件**一起提交**——它们进版本库，干净检出才能通过类型检查。

> ⚠️ **2026-09-20 更正（原写「跑一次 `pnpm dev`（或 `vite build`）」，并称声明文件没生成时
> `pnpm typecheck` / `pnpm build` 会报 `TS2304`）**：实测 **`pnpm build` 与 `pnpm dev` 都会重写这两个文件**
> （插件在 `transform` 阶段写盘，内容没变则不写），其中 **`pnpm build` 是可靠的那一个**；
> 而 `pnpm typecheck` **既不重生成、也不报缺**——本工程没开 `strictTemplates`，模板里未知的 kebab-case
> 标签不是类型错误（本仓 `auto-imports.d.ts` 当前还是空表：没有符号靠自动引入）。
> 反向的坑：`pnpm dev` 在文件**不存在**时，仅启动就会写一个**残缺**文件（实测 475 B，12 个 EP 组件全丢）。
> 完整机制（读码行号与对照实测）见原设计记录（本仓库只开源网页前后端源码，未收录内部计划文档）
> 「执行后更正」第（1）条。判据只有一条：**生成的文件确实被更新**（`git status` 看得到它们）；
> 更稳的做法是把它变成机检（如 CI 跑 `pnpm build && git diff --exit-code`），别依赖记忆。

### 坑 2：请求超时值是拍的

`src/api/http.ts` 里的 `TIMEOUT_MS = 15_000` 是**拍的**，不是算出来的。
该文件是这一事实的唯一出处，调整时改那一处即可。

### 坑 3：收起态侧栏宽度依赖 Element Plus 自己的算式（最容易被升级搞坏的一行）

`--shell-sidebar-collapsed-w: 60px` 与菜单**实际渲染出来的宽度**不是同一个东西：
`.el-menu--collapse` 的宽度由 Element Plus 自己算——
`node_modules/element-plus/theme-chalk/el-menu.css`（压缩成单行）里是

```css
.el-menu--collapse{width:calc(var(--el-menu-icon-width) + var(--el-menu-base-level-padding) * 2)}
```

默认 `24px + 20px×2 = 64px`，比冻结的 60px **宽 4px**，会被硬塞进 60px 的盒子并溢出成横向滚动条。
`src/frontend/src/styles/shell.css` 因此在收起态把 EP 的图标宽变量反解成
`calc(60px − var(--el-menu-base-level-padding) * 2)`，让 EP 的算式**自己**算出 60px
（`icon + 2p` 与 `p` 的默认值无关，不必写死 20px）。该文件同时注明：
EP 2.14.6 没有「收起宽度」的单一变量（全包 grep `collapse-width` / `collapseWidth` 无匹配），
所以只能借 EP 的 `:root` 变量——**全项目最依赖 EP 内部变量的地方**。

升级 Element Plus 后若收起态先坏（多出 4px 横向滚动条、或图标被压得比 16px 字号还小），
按实测改 `shell.css` 那一处；**不要**去改 `--shell-sidebar-collapsed-w`（那是冻结尺寸）。
断言依据必须能在 EP 源码里指到行/规则，别凭印象调。

## 类型检查用的是 `vue-tsc` 而不是 `tsc`

TypeScript 默认拿不到 `.vue` 的类型信息，所以命令行用 `vue-tsc`。
编辑器里需要 [Volar](https://marketplace.visualstudio.com/items?itemName=Vue.volar)（并关掉 Vetur）
才能让 TS 语言服务认识 `.vue` 类型。

Vite 配置本身见 [Vite Configuration Reference](https://vite.dev/config/)。
