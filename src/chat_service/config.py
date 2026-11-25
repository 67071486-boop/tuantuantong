import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

from .browser_ticket import validate_browser


class ServiceError(Exception):
    def __init__(self, message, status=400, matrix_code=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.matrix_code = matrix_code
        self.retry_after = retry_after


class DatabasePoolBusy(ServiceError):
    def __init__(self):
        super().__init__('内部沟通数据库暂时繁忙', 503, retry_after=1)


def internal_address(address):
    ranges = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7')
    return address.is_loopback or any(address in ipaddress.ip_network(network) for network in ranges)


def internal_url(value):
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
        raise ValueError('Invalid service URL')
    if parsed.scheme == 'https':
        return value.rstrip('/')
    if parsed.scheme != 'http':
        raise ValueError('Service URL must use HTTPS or private HTTP')
    try:
        address = ipaddress.ip_address(parsed.hostname)
        private = internal_address(address)
    except ValueError:
        private = parsed.hostname == 'localhost'
    if not private:
        raise ValueError('Public HTTP service URL is not allowed')
    return value.rstrip('/')


def browser_tenant_origins(tenant):
    browser = validate_browser(tenant['browser'])
    source = urlsplit(internal_url(tenant.get('source_url', '')))
    try:
        local = source.hostname == 'localhost' or internal_address(ipaddress.ip_address(source.hostname))
    except ValueError:
        local = False
    if (not isinstance(tenant.get('secret'), str) or len(tenant['secret']) < 32
            or not local or not tenant.get('source_host') or source.path
            or source.port is not None and not 1 <= source.port <= 65535):
        raise ValueError('Browser chat requires a trusted internal source and tenant signing secret')
    return {(browser['audience'], origin) for origin in browser['origins']}


def load_config(path):
    if os.stat(path).st_mode & 0o077:
        raise ValueError('Service configuration must have mode 0600')
    with open(path) as handle:
        result = json.load(handle)
    for key in ('matrix_url', 'registration_shared_secret', 'postgres_dsn', 'tenants'):
        if not result.get(key):
            raise ValueError(f'Missing configuration: {key}')
    result['matrix_url'] = internal_url(result['matrix_url'])
    result.setdefault('schema', 'chat_service')
    pool_size = result.setdefault('postgres_pool_size', 0)
    if type(pool_size) is not int or not 0 <= pool_size <= 8:
        raise ValueError('postgres_pool_size must be an integer between 0 and 8')
    if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', result['schema']):
        raise ValueError('Invalid database schema')
    seen_origins = set()
    for key, tenant in result['tenants'].items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', key) or not tenant.get('secret'):
            raise ValueError('Invalid tenant signing configuration')
        if tenant.get('source_url'):
            tenant['source_url'] = internal_url(tenant['source_url'])
        if tenant.get('source_host') and any(c in tenant['source_host'] for c in '\r\n/'):
            raise ValueError('Invalid source Host')
        if 'browser' in tenant:
            origins = browser_tenant_origins(tenant)
            if origins & seen_origins:
                raise ValueError('Browser chat origins must map to one tenant')
            seen_origins.update(origins)
        if 'realtime' in tenant:
            realtime = tenant['realtime']
            if (not isinstance(realtime, dict) or set(realtime) != {'url', 'secret', 'tenant'}
                    or not isinstance(realtime['secret'], str) or len(realtime['secret']) < 32
                    or not isinstance(realtime['tenant'], str)
                    or not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}', realtime['tenant'])):
                raise ValueError('Invalid realtime configuration')
            realtime['url'] = internal_url(realtime['url'])
            address = urlsplit(realtime['url'])
            if address.path or (address.port is not None and not 1 <= address.port <= 65535):
                raise ValueError('Realtime URL must be an origin with a valid port')
    return result


def integer(value, name='id', minimum=1, maximum=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ServiceError(f'{name} must be an integer')
    try:
        result = int(value)
    except ValueError:
        raise ServiceError(f'{name} must be an integer') from None
    if str(result) != str(value) or not minimum <= result <= maximum:
        raise ServiceError(f'{name} is outside the allowed range')
    return result
