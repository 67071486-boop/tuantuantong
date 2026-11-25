#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd "$(dirname "$0")" && pwd)"
target_dir="${1:-$source_dir/../frontend/dist/internal-chat-webk}"
[[ "$target_dir" == /* ]] || target_dir="$PWD/$target_dir"
revision=2e4bbb52b6aec5e95ee32e5c2882c1f05de32200
upstream=https://github.com/TelegramOrg/Telegram-web-k.git

if [[ -e "$target_dir" || -L "$target_dir" ]]; then
  echo 'Web K 输出目录已存在，请使用新构建目录。' >&2
  exit 2
fi

build_dir="$(mktemp -d "${TMPDIR:-/tmp}/tuantuantong-webk.XXXXXX")"
trap 'rm -rf -- "$build_dir"' EXIT
if [[ -n "${TT_WEBK_SOURCE:-}" ]]; then
  git clone --no-checkout --shared -- "$TT_WEBK_SOURCE" "$build_dir/source"
else
  git init --quiet "$build_dir/source"
  git -C "$build_dir/source" fetch --depth=1 "$upstream" "$revision"
fi

cd "$build_dir/source"
git checkout --quiet --detach "$revision"
for patch in tuantuantong.patch production.patch h5-unread-fix.patch mobile-keyboard-fix.patch presence.patch \
  chat-directory-search.patch chat-mobile-composer-fix.patch chat-retained-close.patch chat-retained-navigation.patch \
  chat-reopen-history.patch chat-order-authority.patch chat-system-notifications.patch \
  chat-system-notification-block.patch chat-visitor-and-window.patch \
  chat-visitor-images.patch chat-order-temporary-recovery.patch chat-image-format.patch; do
  git apply --index "$source_dir/$patch"
done
pnpm install --frozen-lockfile
TWEB_PREVIEW=1 TT_WEBK_HOSTED=1 NODE_ENV=production ./node_modules/.bin/vite build --config vite.ttt-chat.config.ts
test -s dist/index.html
# The upstream public directory also contains old build output. Publish just the runtime assets.
cp -R public/assets dist/assets
cp public/browserconfig.xml dist/browserconfig.xml
cp LICENSE dist/LICENSE
printf '%s\n' "$revision" > dist/upstream-revision.txt
mkdir -- "$target_dir"
cp -R dist/. "$target_dir/"
