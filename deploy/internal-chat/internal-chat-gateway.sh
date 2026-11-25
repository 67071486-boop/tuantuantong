#!/usr/bin/env bash

independent_internal_chat_gateway() {
    [[ -e "$1/shared/chat-gateway-release.json" || -L "$1/shared/chat-gateway-release.json" ]]
}

lock_independent_internal_chat_gateway() {
    independent_internal_chat_gateway "$1" || return 0
    exec {TT_CHAT_GATEWAY_LOCK_FD}>> "$1/shared/.chat-gateway-release.lock"
    if ! flock --nonblock "$TT_CHAT_GATEWAY_LOCK_FD"; then
        echo '该站正在进行网页或网关发布。' >&2
        return 1
    fi
}

require_internal_chat_assets() {
    if [[ ! -s "$1/src/frontend/dist/internal-chat-webk/index.html" ]]; then
        echo '本机已配置内部沟通，发布包缺少聊天界面。请设置 TT_BUILD_INTERNAL_CHAT=1 重新暂存。' >&2
        return 1
    fi
}

check_independent_internal_chat_web() {
    local root="$1" release="$2" tenant_host="$3"
    shift 3
    independent_internal_chat_gateway "$root" || return 0
    require_internal_chat_assets "$release" || return 1
    (cd "$release/src" && PYTHONPATH="$PWD" .venv/bin/python "$root/shared/chat-gateway-release.py" \
        --root "$root" web-check "$tenant_host" "$@")
}

prepare_internal_chat_gateway() {
    local root="$1" release="$2"
    if independent_internal_chat_gateway "$root"; then
        require_internal_chat_assets "$release"
        return
    fi
    [[ -f "$root/shared/internal-chat.env" ]] || return 0
    require_internal_chat_assets "$release" || return 1
    if [[ ! -x /usr/bin/node || ! -x /usr/bin/npm ]]; then
        echo '本机已配置内部沟通，需要 /usr/bin/node 和 /usr/bin/npm。' >&2
        return 1
    fi
    (cd "$release/src/chat-realtime" && /usr/bin/npm ci --omit=dev --ignore-scripts)
    sudo install -m 644 "$root/shared/tuantuantong-chat-realtime.service" /etc/systemd/system/tuantuantong-chat-realtime.service
}

configure_internal_chat_gateway() {
    local root="$1" release="$2" tenant_host="$3" unit=tuantuantong-chat-realtime.service pid
    if independent_internal_chat_gateway "$root"; then
        check_independent_internal_chat_web "$root" "$release" "$tenant_host" --live
        return
    fi
    if [[ -f "$root/shared/internal-chat.env" &&
          -s "$release/src/frontend/dist/internal-chat-webk/index.html" &&
          -d "$release/src/chat-realtime/node_modules/socket.io" ]]; then
        sudo systemctl enable "$unit"
        sudo systemctl restart "$unit"
        curl --fail --silent --show-error --max-time 3 --retry 4 --retry-connrefused \
            --header "Origin: https://$tenant_host" --output /dev/null \
            'http://127.0.0.1:4191/socket.io/?EIO=4&transport=polling'
        (cd "$release/src" && .venv/bin/python - <<'PYPROBE'
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import settings

if not settings.TT_CHAT_ORDER_GROUPS_ENABLED or not settings.TT_CHAT_REALTIME_SECRET:
    raise SystemExit("Django 内部沟通设置未启用或缺少密钥。")
if settings.TT_CHAT_REALTIME_NOTIFY_URL != "http://127.0.0.1:4191/notify":
    raise SystemExit("Django 内部沟通通知地址与本机网关不符。")
request = Request(settings.TT_CHAT_REALTIME_NOTIFY_URL, data=b"{}", headers={
    "Authorization": "Bearer " + settings.TT_CHAT_REALTIME_SECRET,
    "Content-Type": "application/json",
})
try:
    with urlopen(request, timeout=3):
        pass
except HTTPError as error:
    # The gateway authenticates before validating the deliberately empty payload.
    if error.code == 400:
        raise SystemExit(0)
except (URLError, TimeoutError):
    pass
raise SystemExit("Django 与本机实时网关的通知校验失败，请检查两边密钥。")
PYPROBE
        )
        systemctl is-active --quiet "$unit"
        pid="$(systemctl show --property=MainPID --value "$unit")"
        [[ "$pid" =~ ^[1-9][0-9]*$ ]] &&
            [[ "$(readlink -f "/proc/$pid/cwd")" == "$(readlink -f "$release/src/chat-realtime")" ]]
    elif systemctl is-active --quiet "$unit" || systemctl is-enabled --quiet "$unit" 2>/dev/null; then
        sudo systemctl disable --now "$unit"
        echo '内部沟通实时服务已关闭。'
    fi
}
