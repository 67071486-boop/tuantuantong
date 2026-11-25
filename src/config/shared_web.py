"""Select one tenant database for each request in the shared WSGI process."""
import json
import logging
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import local

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, connections
from django.db.utils import OperationalError
from django.http import HttpResponse, HttpResponseNotFound
from django.http.request import split_domain_port, validate_host
from django.views.decorators.debug import sensitive_variables
from django_tenants.middleware.main import TenantMainMiddleware
from django_tenants.postgresql_backend.base import DatabaseWrapper
from psycopg.pq import TransactionStatus

from apps.platform.db_provisioning import tenant_database_name

logger = logging.getLogger(__name__)
_REUSE_MAX_AGE = 60


@dataclass(frozen=True)
class TenantRoute:
    schema: str
    database: str
    password: str = field(repr=False)
    host: str
    port: int

    @sensitive_variables()
    def connection(self):
        db_settings = {
            **connections.settings[DEFAULT_DB_ALIAS],
            "ENGINE": "django_tenants.postgresql_backend",
            "NAME": self.database,
            "USER": self.database,
            "PASSWORD": self.password,
            "HOST": self.host,
            "PORT": str(self.port),
            "CONN_MAX_AGE": 0,
            "OPTIONS": {"connect_timeout": 3},
        }
        return DatabaseWrapper(db_settings, alias=DEFAULT_DB_ALIAS)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


@sensitive_variables()
def _load_routes(filename):
    path = Path(filename)
    if not path.is_absolute():
        raise ImproperlyConfigured("TT_SHARED_WEB_ROUTES_FILE must be an absolute path")
    try:
        with path.open(encoding="utf-8") as route_file:
            mode = os.fstat(route_file.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise ValueError("invalid route file permissions")
            rows = json.load(route_file, object_pairs_hook=_unique_object)
        if not isinstance(rows, dict) or not rows:
            raise ValueError("expected a nonempty host mapping")
        routes = {}
        tenants = {}
        for hostname, config in rows.items():
            if split_domain_port(hostname) != (hostname, "") or not validate_host(hostname, settings.ALLOWED_HOSTS):
                raise ValueError("invalid route domain")
            if not isinstance(config, dict) or config.keys() != {"schema", "password", "host", "port"}:
                raise ValueError("invalid route fields")
            database = tenant_database_name(config["schema"])
            if (
                not isinstance(config["password"], str)
                or not config["password"]
                or not isinstance(config["host"], str)
                or not config["host"]
                or type(config["port"]) is not int
                or not 1 <= config["port"] <= 65535
            ):
                raise ValueError("invalid route values")
            route = TenantRoute(database=database, **config)
            if tenants.setdefault(route.schema, route) != route:
                raise ValueError("inconsistent database settings for tenant aliases")
            routes[hostname] = route
    except (OSError, ValueError):
        raise ImproperlyConfigured("Invalid shared web route file or permissions") from None
    return routes


@contextmanager
def _bind_database(database):
    # Every ORM path, including explicit using('default'), sees this thread's request-owned connection.
    previous = connections[DEFAULT_DB_ALIAS]
    connections[DEFAULT_DB_ALIAS] = database
    try:
        yield
    finally:
        connections[DEFAULT_DB_ALIAS] = previous


class SharedWebApplication:
    def __init__(self, application, routes_file):
        self.application = application
        self.routes = _load_routes(routes_file)
        self._idle_connections = local()

    def __call__(self, environ, start_response, *, reuse_schema=None):
        hostname, _ = split_domain_port(environ.get("HTTP_HOST", ""))
        route = self.routes.get(hostname)
        if route is None:
            start_response("404 Not Found", [("Content-Type", "text/plain"), ("Cache-Control", "no-store")])
            return [b"Not found"]

        idle = None
        database = None
        if route.schema == reuse_schema and route.database == tenant_database_name(route.schema):
            idle = getattr(self._idle_connections, "by_route", None)
            if idle is None:
                idle = self._idle_connections.by_route = {}
            # An open response exclusively owns its connection, including during nested requests.
            database = idle.pop(route, None)
        if database is None:
            database = route.connection()
            if idle is not None:
                database.settings_dict.update(CONN_MAX_AGE=_REUSE_MAX_AGE, CONN_HEALTH_CHECKS=True)
        database.shared_web_schema = route.schema
        database.shared_web_hostname = hostname
        # The application owns file streaming and closes it together with the database.
        environ = environ.copy()
        environ.pop("wsgi.file_wrapper", None)
        try:
            with _bind_database(database):
                if idle is not None:
                    database.close_if_unusable_or_obsolete()
                response = self.application(environ, start_response)
        except BaseException:
            database.close()
            raise
        return _SharedWebResponse(response, database, idle, route)


def _clean_connection(database):
    driver = database.connection
    return (
        driver is not None
        and driver.info.transaction_status == TransactionStatus.IDLE
        and driver.autocommit and database.autocommit
        and not database.in_atomic_block and not database.needs_rollback
        and not database.run_on_commit and not database.errors_occurred
    )


class _SharedWebResponse:
    def __init__(self, response, database, idle, route):
        self.response = response
        self.database = database
        self.idle = idle
        self.route = route
        self.closed = False
        self.complete = False

    def __iter__(self):
        try:
            with _bind_database(self.database):
                chunks = iter(self.response)
            while True:
                with _bind_database(self.database):
                    try:
                        chunk = next(chunks)
                    except StopIteration:
                        self.complete = True
                        break
                yield chunk
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        with _bind_database(self.database):
            retained = False
            try:
                try:
                    reusable = (
                        self.idle is not None and self.complete and not self.response.streaming
                        and 200 <= self.response.status_code < 300 and not getattr(self.response, "exception", False)
                        and _clean_connection(self.database)
                    )
                finally:
                    self.response.close()
                if reusable and _clean_connection(self.database):
                    self.database.close_if_unusable_or_obsolete()
                    if self.database.connection is not None:
                        # The next request must resolve its domain again from the public schema.
                        self.database.set_schema_to_public()
                        retained = self.idle.setdefault(self.route, self.database) is self.database
            finally:
                if not retained:
                    self.database.close()


class SharedTenantMiddleware(TenantMainMiddleware):
    @staticmethod
    def hostname_from_request(request):
        # The pilot uses explicit host registrations, including any www aliases.
        return split_domain_port(request.get_host())[0]

    def get_tenant(self, domain_model, hostname):
        database = connections[DEFAULT_DB_ALIAS]
        if hostname != database.shared_web_hostname:
            raise domain_model.DoesNotExist
        tenant = super().get_tenant(domain_model, hostname)
        if tenant.schema_name != database.shared_web_schema or not tenant.is_active:
            raise domain_model.DoesNotExist
        return tenant

    def no_tenant_found(self, request, hostname):
        return HttpResponseNotFound("Not found")

    def process_request(self, request):
        database = connections[DEFAULT_DB_ALIAS]
        if not getattr(database, "shared_web_schema", None):
            return self.no_tenant_found(request, "")
        try:
            return super().process_request(request)
        except OperationalError:
            logger.warning("Shared web tenant database unavailable for %s", database.shared_web_hostname)
            return HttpResponse("Service unavailable", status=503)


def shared_cache_key(key, key_prefix, version):
    database = connections[DEFAULT_DB_ALIAS]
    schema = getattr(database, "shared_web_schema", "")
    return f"{key_prefix}:{version}:{schema}:{key}"
