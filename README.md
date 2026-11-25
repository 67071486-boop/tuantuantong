# 团团通（tuantuantong）· 网页版

面向**游戏代练 / 陪玩工作室**的多租户 SaaS 网页端：后端一套 Django 服务 + 前端一套 Vue 3 单页应用，
每个工作室（租户）**独享自己的域名和自己的 PostgreSQL 数据库**。

> 本仓库是开源出来的**网页前后端源码**。不含运营与需求文档、手机客户端（Android / 小程序）
> 以及第三方平台对接的内部资料。

## 技术栈

| 层 | 选型 |
|---|---|
| 语言 / 运行时 | Python 3.13 |
| Web 框架 | Django 5.2 LTS · Django REST Framework（统一响应信封） |
| 多租户 | `django-tenants`：**一个租户一个 PostgreSQL database**，角色只对自己那个库有 `CONNECT` |
| 数据库 | PostgreSQL 16 |
| 前端 | Vue 3 · TypeScript · Vite · Pinia · Vue Router · Element Plus / Framework7 |
| 打包 / 部署 | uv（后端依赖）· pnpm（前端）· gunicorn + nginx |

## 目录结构

```
.
├── .env.example          # 后端环境变量模板（复制成仓库根的 .env）
└── src/
    ├── manage.py
    ├── pyproject.toml    # 后端依赖（uv.lock 锁定版本）
    ├── config/           # Django 设置、根路由、WSGI
    ├── apps/             # 业务应用（账号、订单、财务、平台供给……）
    ├── jidanpu/          # vendor 进来的第三方（鸡蛋铺）SDK
    ├── sdk/              # 对内/对外的小型 SDK
    ├── tests/            # 后端测试
    └── frontend/         # 租户后台 SPA（Vue 3 + Vite）
```

## 快速开始

前置：Python 3.13、[uv](https://docs.astral.sh/uv/)、PostgreSQL 16、Node（`^22.18` 或 `>=24.12`）、pnpm。

### 1. 数据库

```sh
psql -U postgres -c "CREATE ROLE tuantuantong LOGIN PASSWORD 'change-me' CREATEDB;"
psql -U postgres -c "CREATE DATABASE tuantuantong OWNER tuantuantong ENCODING 'UTF8';"
```

### 2. 后端

```sh
# 依赖（按 src/uv.lock 建 src/.venv）
cd src && uv sync

# 环境变量：必须放在仓库根的 .env（src/config/settings.py 按仓库根定位它）
cd .. && cp .env.example .env    # 打开改掉里面的假值
cd src

# 建表：public（平台）schema
uv run python manage.py migrate_schemas --shared

# 自检 / 跑测试 / 启动
uv run python manage.py check
uv run python manage.py test
uv run python manage.py runserver 127.0.0.1:8000
```

### 3. 前端

```sh
cd src/frontend
pnpm install
pnpm dev          # 只监听本机；要局域网访问用 pnpm dev:lan
```

开发期前端与后端同源：请求先到 Vite，再由 Vite 代理转给 Django，并**在转发时把租户域名写进 `Host` 头**——
`django-tenants` 靠这个头解析租户。所以本地要先建一个开发租户（一次性、幂等）：

```sh
cd src
uv run python manage.py shell -c \
  "from apps.platform.provisioning import provision_tenant; provision_tenant(schema_name='dev', name='本地开发租户', domain='dev.test.local')"
```

并把 `src/frontend/.env.example` 复制为 `.env.local`（`VITE_TENANT_HOST` 与上面那个域名一致）。

## 环境变量

全部键与说明见 [`.env.example`](.env.example)（后端）和 [`src/frontend/.env.example`](src/frontend/.env.example)（前端）。
真实密钥只写进 `.env` / `.env.local`，两个文件都已被 `.gitignore` 忽略。

## 多租户是怎么隔离的

- **一租户一库一角色**：建库后 `REVOKE ALL ON DATABASE … FROM PUBLIC`，角色只对自己那个库有 `CONNECT`，
  租户之间在连接层就连不上。
- **租户上下文只来自认证态与域名**，绝不接受请求参数传入；任何新增查询都携带租户上下文。
- 供给一个新租户是一次显式操作：`manage.py provision_tenant_db --schema <名> --name <名称> --domain <域名>`。
- ⚠️ 运维身份（`POSTGRES_ADMIN_USER`，通常是 `postgres`）按设计能连每一个租户库，**不要**把它配到租户机上。

## 许可证

[MIT](LICENSE)
