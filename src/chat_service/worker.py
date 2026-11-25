import time
from urllib.parse import urlencode, urlsplit

import httpx

from .config import ServiceError, integer, internal_url
from .protocol import sign_request


class Source:
    def __init__(self, key_id, config, client=None):
        self.key_id, self.config = key_id, config
        self.url = internal_url(config['source_url'])
        self.client = client or httpx.Client(timeout=30, trust_env=False)

    def page(self, operation, query):
        path = '/api/internal-chat/service/' + operation + '/?' + urlencode(query)
        request_path = urlsplit(self.url).path.rstrip('/') + path
        headers = sign_request(self.config['secret'], self.key_id, 'GET', request_path, b'')
        if self.config.get('source_host'):
            headers['Host'] = self.config['source_host']
        try:
            response = self.client.get(self.url + path, headers=headers)
        except httpx.HTTPError:
            raise ServiceError('Business journal is unavailable', 503) from None
        try:
            result = response.json()
        except ValueError:
            raise ServiceError('Business journal returned invalid JSON', 503) from None
        if not response.is_success or not isinstance(result, dict) or type(result.get('code')) is not int or result.get('code') != 0 or not isinstance(result.get('data'), dict):
            raise ServiceError('Business journal rejected export', 503)
        return result['data']


class Worker:
    def __init__(self, engine, sources=None, clock=time.monotonic):
        self.engine = engine
        self.clock = clock
        self.retry_at = {}
        self.failures = {}
        self.sources = sources if sources is not None else {
            key: Source(key, config) if config.get('source_url') else None for key, config in engine.tenants.items()
        }

    def step(self, tenant):
        remaining = self.retry_at.get(tenant, 0) - self.clock()
        if remaining > 0:
            return {'phase': None, 'has_more': True, 'completed': False, 'retry_after': remaining}
        try:
            result = self._step(tenant)
        except ServiceError as error:
            failures = min(self.failures.get(tenant, 0) + 1, 7)
            self.failures[tenant] = failures
            delay = error.retry_after if error.retry_after is not None else min(2 ** (failures - 1), 60)
            self.retry_at[tenant] = self.clock() + max(0.1, delay)
            raise
        self.failures.pop(tenant, None)
        self.retry_at.pop(tenant, None)
        return result

    def _step(self, tenant):
        core = getattr(self.engine, 'core', None)
        if core is not None and core.is_active(tenant):
            result = core.tick(tenant)
            return {'phase': 'core', 'has_more': result['has_more'], 'completed': not result['has_more'],
                    **({'retry_after': result['retry_after']} if result.get('retry_after') is not None else {})}
        state = self.engine.store.status(tenant)
        phase = state['phase']
        after = state['cursor'] if phase == 'changes' else state['snapshot_cursor']
        query = {'after': after, 'limit': 100}
        operation = 'export'
        if phase != 'changes':
            operation = 'snapshot'
            query['kind'] = 'conversations' if phase == 'conversation' else 'messages'
        source = self.sources[tenant]
        if source is None:
            raise ServiceError('Legacy chat source is not configured for this tenant', 503)
        page = source.page(operation, query)
        cursor = integer(page.get('cursor'), 'source.cursor', 0)
        latest = integer(page.get('latest'), 'source.latest', 0)
        rows = page.get('events' if phase == 'changes' else 'rows')
        if cursor < after or not isinstance(page.get('has_more'), bool) or not isinstance(rows, list):
            raise ServiceError('Invalid journal page', 503)
        if page['has_more'] and cursor <= after:
            raise ServiceError('Journal cursor failed to advance', 503)
        if phase != 'changes' and state['snapshot_latest'] is not None:
            latest = state['snapshot_latest']
        result = self.engine.import_page(tenant, {'phase': phase, 'rows': rows,
            'cursor': cursor, 'latest': latest, 'has_more': page['has_more']})
        return {'phase': result['phase'], 'has_more': page['has_more'], 'completed': phase == 'changes' and not page['has_more']}

    def once(self, tenant):
        for _ in range(100_000):
            result = self.step(tenant)
            if result['completed']:
                return self.engine.store.status(tenant)
            if result.get('retry_after') is not None:
                raise ServiceError('Tenant retry is deferred', 503, retry_after=result['retry_after'])
        raise ServiceError('Journal catch-up page limit reached', 503)
