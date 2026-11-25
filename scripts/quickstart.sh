#!/usr/bin/env bash
# 一键初始化：装后端依赖 → 建平台库 → 建平台表 → 开通第一个租户
#
# 用法：
#   1) 先准备 .env：cp .env.example .env，改掉数据库账号/密码/密钥
#   2) bash scripts/quickstart.sh
#
# 环境变量（可选）：TENANT_SCHEMA / TENANT_NAME / TENANT_DOMAIN
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SCHEMA="${TENANT_SCHEMA:-demo}"
NAME="${TENANT_NAME:-演示工作室}"
DOMAIN="${TENANT_DOMAIN:-demo.test.local}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少 $1，请先安装"; exit 1; }; }
need uv
need psql

if [ ! -f .env ]; then
  cp .env.example .env
  cat <<'EOF'
已生成 .env（模板）。
请先改掉里面的数据库账号、密码、DJANGO_SECRET_KEY 与 POSTGRES_ADMIN_USER，再重新运行：

  bash scripts/quickstart.sh
EOF
  exit 1
fi

# 读 .env（只用于本脚本建库；应用自己也会读它）
set -a
# shellcheck disable=SC1091
source ./.env
set +a

: "${POSTGRES_DB:?}" "${POSTGRES_USER:?}"
PGHOST="${POSTGRES_HOST:-127.0.0.1}"
PGPORT="${POSTGRES_PORT:-5432}"
ADMIN_USER="${POSTGRES_ADMIN_USER:-postgres}"
ADMIN_DB="${POSTGRES_ADMIN_DB:-postgres}"

echo "==> 后端依赖"
(cd src && uv sync -q)

echo "==> 平台库 ${POSTGRES_DB}"
if psql -h "$PGHOST" -p "$PGPORT" -U "$ADMIN_USER" -d "$ADMIN_DB" -tAc \
      "select 1 from pg_database where datname = '${POSTGRES_DB}'" | grep -q 1; then
  echo "    已存在，跳过"
else
  psql -h "$PGHOST" -p "$PGPORT" -U "$ADMIN_USER" -d "$ADMIN_DB" \
    -c "CREATE DATABASE \"${POSTGRES_DB}\" OWNER \"${POSTGRES_USER}\" ENCODING 'UTF8'" >/dev/null
  echo "    已创建"
fi

echo "==> 平台表（public schema）"
(cd src && uv run python manage.py migrate_schemas --shared --noinput | tail -2)

echo "==> 开通租户 ${SCHEMA}"
(cd src && uv run python manage.py provision_tenant_db --schema "$SCHEMA" --name "$NAME" --domain "$DOMAIN" | tail -2) \
  || echo "    （租户已存在或供给失败，可按上面的报错处理）"

cat <<EOF

初始化完成。接下来：

  1) 设管理员密码：
       cd src && uv run python manage.py set_tenant_password --schema ${SCHEMA}

  2) 起后端：
       cd src && uv run python manage.py runserver

  3) 另开一个终端起前端：
       cd src/frontend && pnpm install && pnpm dev

  浏览器打开 http://${DOMAIN}:5173 —— 需要把 ${DOMAIN} 指向 127.0.0.1。
EOF
