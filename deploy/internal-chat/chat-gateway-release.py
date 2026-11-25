#!/usr/bin/env python3
"""Stage and switch the tenant's independently deployed Node gateway."""

import argparse
import base64
from contextlib import contextmanager
import fcntl
import gzip
import hashlib
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
from tempfile import NamedTemporaryFile, TemporaryDirectory
import time


APP_ROOT = Path("/home/ubuntu/projects/tuantuantong")
PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")
LEGACY = "tuantuantong-chat-realtime.service"
INDEPENDENT = "tuantuantong-chat-gateway.service"
NOTIFY_URL = "http://127.0.0.1:4193/notify"
FILES = ("gateway.mjs", "package.json", "package-lock.json")
VERSION = re.compile(r"[a-f0-9]{40}")
POLLING = "/socket.io/?EIO=4&transport=polling"
WEBSOCKET = "/socket.io/?EIO=4&transport=websocket"
SIGNALS = {signal.SIGHUP, signal.SIGINT, signal.SIGTERM}


class ReleaseError(Exception):
    pass


@contextmanager
def finish_before_interrupt():
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)


def run(*args, capture=False, **kwargs):
    # An interrupted systemctl or nginx command must finish before compensation runs.
    with finish_before_interrupt():
        return subprocess.run(args, check=True, stdout=subprocess.PIPE if capture else None, **kwargs).stdout


def systemctl(command, unit):
    run("sudo", "-n", "systemctl", command, unit)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(ref, output):
    repo = Path(__file__).resolve().parents[2]
    commit = run("git", "-C", repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}",
                 capture=True).decode("ascii").strip()
    with TemporaryDirectory(prefix=".gateway-build-", dir=output.absolute().parent) as temporary:
        archive = Path(temporary) / "artifact.tar.gz"
        with gzip.GzipFile(archive, "wb", mtime=0) as compressed, tarfile.open(fileobj=compressed, mode="w") as package:
            contents = {name: run("git", "-C", repo, "show", f"{commit}:src/chat-realtime/{name}",
                                  capture=True) for name in FILES}
            contents["VERSION"] = f"{commit}\n".encode("ascii")
            for name, data in contents.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o644
                package.addfile(member, BytesIO(data))
        os.link(archive, output)
    print(f"{commit} {digest(output)} {output}")


@contextmanager
def release_lock(root):
    with (root / "shared/.chat-gateway-release.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseError("该站正在进行网页或网关发布") from error
        yield


def http_status(host, port, path, *, method="GET", origin=None, token=None, tls=False, websocket=False):
    connection = (http.client.HTTPSConnection if tls else http.client.HTTPConnection)(host, port, timeout=3)
    headers = {"Origin": origin} if origin else {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        body = b"{}"
    if websocket:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        headers.update({"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                        "Sec-WebSocket-Key": key})
    try:
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        if response.status == 101 and websocket:
            accept = base64.b64encode(hashlib.sha1(f"{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode()).digest())
            if response.getheader("Sec-WebSocket-Accept") != accept.decode("ascii"):
                raise ReleaseError("WebSocket 握手响应无效")
        if path == POLLING and response.status == 200 and not response.read(5).startswith(b'0{"si'):
            raise ReleaseError("Socket.IO 握手响应无效")
        return response.status
    finally:
        connection.close()


def expect_status(label, status, *args, **kwargs):
    actual = http_status(*args, **kwargs)
    if actual != status:
        raise ReleaseError(f"{label}返回 HTTP {actual}，预期 {status}")


def wait_for(check):
    for attempt in range(8):
        try:
            check()
            return
        except (OSError, http.client.HTTPException, ReleaseError, subprocess.CalledProcessError):
            if attempt == 7:
                raise
            time.sleep(0.35)


def gateway_secret(root):
    pattern = re.compile(r"^[ \t]*TT_CHAT_REALTIME_SECRET[ \t]*=[ \t]*(.*?)\s*$", re.MULTILINE)
    secrets = pattern.findall((root / "shared/internal-chat.env").read_text(encoding="utf-8"))
    if len(secrets) == 1:
        value = secrets[0]
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1]
        if re.fullmatch(r"[!-~]+", value) and not any(char in value for char in "'\"\\"):
            return value
    raise ReleaseError("internal-chat.env 需要一份可直接读取的网关密钥")


def service_pid(unit):
    result = run("systemctl", "show", "--property=MainPID", "--property=ControlGroup", unit, capture=True)
    properties = dict(line.split("=", 1) for line in result.decode("ascii").splitlines() if "=" in line)
    pid = properties.get("MainPID", "")
    group = Path(properties.get("ControlGroup", ""))
    if not re.fullmatch(r"[1-9][0-9]*", pid) or not group.is_absolute() or group == Path("/") or ".." in group.parts:
        raise ReleaseError(f"找不到 {unit} 的主进程")
    members = (CGROUP_ROOT / group.relative_to("/") / "cgroup.procs").read_text(encoding="ascii").split()
    if pid not in members:
        raise ReleaseError(f"{unit} 的主进程不在服务控制组")
    return PROC_ROOT / pid


def owns_listener(process, port):
    expected = f"0100007F:{port:04X}"
    with (PROC_ROOT / "net/tcp").open(encoding="ascii") as table:
        next(table)
        listeners = {f"socket:[{fields[9]}]" for line in table
                     if (fields := line.split())[1] == expected and fields[3] == "0A"}
    return any(descriptor.is_symlink() and descriptor.readlink().name in listeners
               for descriptor in (process / "fd").iterdir())


class Gateway:
    def __init__(self, root, host, route):
        self.root = root.resolve()
        self.host = host
        self.route = route
        self.base = self.root / "chat-gateway"
        self.releases = self.base / "releases"
        self.current = self.base / "current"
        self.legacy = self.base / "legacy"

    @classmethod
    def read_config(cls, root):
        config = json.loads((root / "shared/chat-gateway-release.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.keys() != {"host", "nginx_target"}:
            raise ReleaseError("独立网关配置需要 host 和 nginx_target")
        host, route = config["host"], Path(config["nginx_target"])
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) or not route.is_absolute():
            raise ReleaseError("独立网关配置的域名或 Nginx 目标路径无效")
        return cls(root, host, route)

    def stage(self, archive, checksum):
        self.releases.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".stage-", dir=self.releases) as temporary:
            package = Path(temporary) / "package"
            package.mkdir()
            with archive.open("rb") as source:
                contents = source.read()
            if hashlib.sha256(contents).hexdigest() != checksum:
                raise ReleaseError("网关发布包 SHA256 不匹配")
            with tarfile.open(fileobj=BytesIO(contents), mode="r:gz") as bundle:
                entries = bundle.getmembers()
                if ({item.name for item in entries} != {*FILES, "VERSION"} or len(entries) != len(FILES) + 1
                        or not all(item.isfile() and item.size > 0 for item in entries)):
                    raise ReleaseError("网关发布包包含无效文件")
                for item in entries:
                    (package / item.name).write_bytes(bundle.extractfile(item).read())
            version = (package / "VERSION").read_text(encoding="ascii").strip()
            if not VERSION.fullmatch(version):
                raise ReleaseError("网关发布包版本无效")
            target = self.releases / version
            if target.is_symlink():
                raise ReleaseError("网关发布目录不能是符号链接")
            if target.exists():
                if (target / "PACKAGE-SHA256").read_text(encoding="ascii").strip() != checksum:
                    raise ReleaseError("该网关版本已暂存另一个发布包")
                self.check_directory(target)
            else:
                subprocess.run(["/usr/bin/npm", "ci", "--omit=dev", "--ignore-scripts"],
                               check=True, cwd=package, timeout=120)
                self.check_directory(package)
                (package / "PACKAGE-SHA256").write_text(f"{checksum}\n", encoding="ascii")
                package.rename(target)
        print(f"已暂存聊天网关 {version}")

    def check_directory(self, directory):
        if directory.is_symlink() or not all((directory / name).is_file() for name in
                                             (*FILES, "node_modules/socket.io/package.json")):
            raise ReleaseError(f"网关发布目录缺少入口或锁定依赖：{directory}")

    def selected(self):
        match = re.fullmatch(r"server 127\.0\.0\.1:(419[12]);\s*", self.route.read_text(encoding="ascii"))
        if not match:
            raise ReleaseError("Nginx 网关上游需指向本机 4191 或 4192")
        return int(match[1])

    def current_release(self):
        if not self.current.is_symlink():
            if self.current.exists():
                raise ReleaseError("网关 current 需为版本软链")
            return None
        destination = self.current.resolve()
        if destination.parent != self.releases or not VERSION.fullmatch(destination.name):
            raise ReleaseError("网关 current 指向无效版本目录")
        return destination

    def set_current(self, directory):
        if directory is None:
            self.current.unlink(missing_ok=True)
        else:
            with TemporaryDirectory(prefix=".current-", dir=self.base) as temporary:
                link = Path(temporary) / "link"
                link.symlink_to(directory)
                link.replace(self.current)

    @staticmethod
    def unit(port):
        return LEGACY if port == 4191 else INDEPENDENT

    def check_handshakes(self, host, port, *, tls=False):
        origin = f"https://{self.host}"
        scope = "公网" if tls else "本机"
        expect_status(f"{scope} WebSocket 握手", 101, host, port, WEBSOCKET, origin=origin, tls=tls, websocket=True)
        expect_status(f"{scope} Socket.IO 握手", 200, host, port, POLLING, origin=origin, tls=tls)

    def wait_direct(self, port, directory):
        def check():
            process = service_pid(self.unit(port))
            if (process / "cwd").resolve() != directory or not owns_listener(process, port):
                raise ReleaseError(f"{self.unit(port)} 尚未运行指定版本")
            self.check_handshakes("127.0.0.1", port)
            expect_status("网关本机通知", 400, "127.0.0.1", port, "/notify", method="POST",
                          token=gateway_secret(self.root))
        wait_for(check)

    def check_routes(self, token=None, *, assets=False):
        secret = gateway_secret(self.root) if token is None else token

        def check():
            self.check_handshakes(self.host, 443, tls=True)
            expect_status("公网通知路径", 404, self.host, 443, "/notify", method="POST", tls=True)
            expect_status("本机未授权通知", 401, "127.0.0.1", 4193, "/notify", method="POST")
            # The gateway authenticates before rejecting the empty payload, which cannot send a message.
            expect_status("本机已授权通知", 400, "127.0.0.1", 4193, "/notify", method="POST", token=secret)
            if assets:
                expect_status("公网 Web K 入口", 200, self.host, 443, "/internal-chat-webk/index.html", tls=True)
        wait_for(check)

    def route_to(self, port):
        previous = self.route.read_bytes()

        def reload_route(content):
            with finish_before_interrupt(), NamedTemporaryFile() as local:
                local.write(content)
                local.flush()
                remote = run("sudo", "-n", "mktemp", f"{self.route}.XXXXXX", capture=True).decode().strip()
                try:
                    run("sudo", "-n", "install", "-m", "644", local.name, remote)
                    run("sudo", "-n", "mv", "--", remote, self.route)
                finally:
                    run("sudo", "-n", "rm", "-f", "--", remote)
                run("sudo", "-n", "nginx", "-t")
                systemctl("reload", "nginx")

        try:
            reload_route(f"server 127.0.0.1:{port};\n".encode("ascii"))
        except (Exception, KeyboardInterrupt):
            reload_route(previous)
            raise

    def select_legacy(self):
        systemctl("start", LEGACY)
        self.wait_direct(4191, self.legacy)
        systemctl("enable", LEGACY)
        self.route_to(4191)
        systemctl("stop", INDEPENDENT)

    def select_independent(self, directory):
        if self.current_release() != directory:
            systemctl("stop", INDEPENDENT)
            self.set_current(directory)
        systemctl("start", INDEPENDENT)
        self.wait_direct(4192, directory)
        systemctl("enable", INDEPENDENT)
        self.route_to(4192)
        systemctl("stop", LEGACY)
        self.check_routes()

    def transition(self, version, *, rollback=False):
        previous_port, previous_release = self.selected(), self.current_release()
        self.check_directory(self.legacy)
        if previous_port == 4192 and previous_release is None:
            raise ReleaseError("请先将试点网关切回 4191，再接入独立发布")
        target = None
        if version == "legacy" and rollback:
            pass
        elif VERSION.fullmatch(version):
            target = self.releases / version
            self.check_directory(target)
            if rollback and not (target / "READY").is_file():
                raise ReleaseError("只能回滚到此前通过探活的网关版本")
        else:
            raise ReleaseError("请输入网关提交号，回滚到原网关时使用 legacy")
        try:
            if target is None:
                self.select_legacy()
                self.check_routes()
            else:
                if previous_port == 4192 and target != previous_release:
                    self.select_legacy()
                    self.check_routes()
                self.select_independent(target)
                (target / "READY").touch()
        except (Exception, KeyboardInterrupt):
            try:
                with finish_before_interrupt():
                    if previous_port == 4191:
                        self.select_legacy()
                        self.set_current(previous_release)
                        self.check_routes()
                    else:
                        self.select_independent(previous_release)
            except (Exception, KeyboardInterrupt) as error:
                raise ReleaseError("网关切换失败，恢复未通过；请检查网关服务、上游，并执行 rollback legacy") from error
            print("网关切换失败，已恢复原上游并通过探活。", file=sys.stderr)
            raise
        print(f"聊天网关已切换到 {version}")

    def web_check(self, host, *, live=False):
        from config import settings

        secret = settings.TT_CHAT_REALTIME_SECRET
        if host != self.host or not settings.TT_CHAT_ORDER_GROUPS_ENABLED or secret != gateway_secret(self.root):
            raise ReleaseError("网页发布的聊天开关、密钥或域名未配齐")
        notify_url = settings.TT_CHAT_REALTIME_NOTIFY_URL
        if live:
            process = service_pid("tuantuantong.service")
            for entry in (process / "environ").read_bytes().split(b"\0"):
                name, _, value = entry.partition(b"=")
                if name == b"TT_CHAT_REALTIME_NOTIFY_URL":
                    notify_url = value.decode("utf-8")
                if name == b"TT_CHAT_REALTIME_SECRET" and value != secret.encode("utf-8"):
                    raise ReleaseError("运行中的 Django 通知密钥与发版配置不一致")
                if name == b"TT_CHAT_ORDER_GROUPS_ENABLED" and value.lower() not in {b"1", b"true", b"yes", b"y", b"on", b"ok"}:
                    raise ReleaseError("运行中的 Django 聊天开关未开启")
        if notify_url != NOTIFY_URL:
            raise ReleaseError("Django 内部沟通通知地址需指向本机 4193 代理")
        port = self.selected()
        destination = self.legacy if port == 4191 else self.current_release()
        if destination is None:
            raise ReleaseError("独立网关尚未接入发布")
        self.wait_direct(port, destination)
        self.check_routes(secret, assets=live)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP_ROOT)
    actions = parser.add_subparsers(dest="action", required=True)
    builder = actions.add_parser("build")
    builder.add_argument("ref")
    builder.add_argument("output", type=Path)
    stage = actions.add_parser("stage")
    stage.add_argument("archive", type=Path)
    stage.add_argument("sha256")
    for name in ("activate", "rollback"):
        action = actions.add_parser(name)
        action.add_argument("version")
    web = actions.add_parser("web-check")
    web.add_argument("tenant_host")
    web.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if args.action == "build":
        build(args.ref, args.output)
        return
    root = args.root.resolve()
    gateway = Gateway.read_config(root)
    if args.action == "web-check":
        gateway.web_check(args.tenant_host, live=args.live)
        return
    with release_lock(root):
        if args.action == "stage":
            gateway.stage(args.archive, args.sha256)
        else:
            gateway.transition(args.version, rollback=args.action == "rollback")


def interrupted(_signal, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except (OSError, ValueError, TypeError, http.client.HTTPException, ReleaseError,
            tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"聊天网关发布失败：{error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("聊天网关发布被中断。", file=sys.stderr)
        sys.exit(130)
