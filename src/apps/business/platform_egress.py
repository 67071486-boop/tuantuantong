"""Select a tenant's fixed HTTPS vendor tunnels at the socket boundary."""

import json
import os
import re
import stat
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection

from apps.platform.identifiers import validate_schema_name

_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_VENDOR_HOST = re.compile(rf"(?:{_DNS_LABEL}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?")


@dataclass(frozen=True)
class _TenantRoute:
    hosts: dict[str, str]
    dailiantong_base_url: str | None


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate route")
    return result


@lru_cache(maxsize=8)
def _load_routes(filename):
    path = Path(filename)
    if not path.is_absolute():
        raise ImproperlyConfigured("TT_PLATFORM_EGRESS_ROUTES_FILE must be absolute")
    try:
        with path.open(encoding="utf-8") as route_file:
            mode = os.fstat(route_file.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError("invalid route file permissions")
            rows = json.load(route_file, object_pairs_hook=_unique_object)
        if not isinstance(rows, dict) or not rows:
            raise ValueError("expected tenant routes")
        routes = {}
        for schema, row in rows.items():
            validate_schema_name(schema)
            if not isinstance(row, dict) or row.keys() - {"hosts", "dailiantong_base_url"}:
                raise ValueError("unexpected tenant route fields")
            destinations = row.get("hosts")
            if not isinstance(destinations, dict) or not destinations:
                raise ValueError("expected vendor routes")
            for host, socket_path in destinations.items():
                if not _VENDOR_HOST.fullmatch(host) or len(host) > 253:
                    raise ValueError("invalid vendor domain")
                if (
                    not isinstance(socket_path, str)
                    or not os.path.isabs(socket_path)
                    or os.path.normpath(socket_path) != socket_path
                    or "\x00" in socket_path
                ):
                    raise ValueError("invalid socket path")
            if len(set(destinations.values())) != len(destinations):
                raise ValueError("a socket must forward to exactly one vendor")
            base_url = row.get("dailiantong_base_url")
            if base_url is not None:
                if not isinstance(base_url, str):
                    raise ValueError("invalid Dailiantong URL")
                url = httpx.URL(base_url)
                if (
                    url.scheme != "https" or url.port not in (None, 443)
                    or url.raw_host.decode("ascii") not in destinations
                    or url.userinfo or url.query or url.fragment
                ):
                    raise ValueError("Dailiantong URL must target a registered HTTPS vendor")
            routes[schema] = _TenantRoute(destinations, base_url)
    except (OSError, ValueError, httpx.InvalidURL):
        raise ImproperlyConfigured("Invalid platform egress route file or permissions") from None
    return routes


def _tenant_route():
    filename = getattr(settings, "TT_PLATFORM_EGRESS_ROUTES_FILE", "") if settings.configured else ""
    if not filename:
        return None
    return _load_routes(filename).get(connection.schema_name)


def uses_platform_egress() -> bool:
    return _tenant_route() is not None


def dailiantong_base_url(default: str) -> str:
    route = _tenant_route()
    return (route.dailiantong_base_url or default) if route is not None else default


class _VendorTunnel(httpx.BaseTransport):
    def __init__(self, host, socket_path):
        self.host = host
        hostname = host.decode("ascii")
        self.authorities = (hostname, f"{hostname}:443")
        self.transport = httpx.HTTPTransport(uds=socket_path) if socket_path is not None else None

    def handle_request(self, request):
        if (
            self.transport is None
            or request.url.scheme != "https"
            or request.url.port not in (None, 443)
            or request.url.raw_host != self.host
            or request.url.userinfo
            or request.headers.get("host", "").lower() not in self.authorities
        ):
            raise httpx.ConnectError("vendor HTTPS destination is not configured for this tunnel", request=request)
        return self.transport.handle_request(request)

    def close(self):
        if self.transport is not None:
            self.transport.close()


def vendor_transport(base_url: str) -> httpx.BaseTransport | None:
    route = _tenant_route()
    if route is None:
        return None
    host = httpx.URL(base_url).raw_host
    return _VendorTunnel(host, route.hosts.get(host.decode("ascii")))
