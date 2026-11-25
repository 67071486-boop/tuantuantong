import json
from contextvars import ContextVar
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import urlsplit

import httpx
from psycopg.types.json import Jsonb

from .config import DatabasePoolBusy, ServiceError, integer, internal_url
from .core_utils import ID_END, ID_START, fields, text
from .protocol import sign_request


UNAVAILABLE = '暂时无法确认订单权限，请稍后重试'
REVOKED = '订单权限已撤销'
_grants = ContextVar('chat_order_authority', default=None)


class CoreAuthority:
    def _source_request(self, tenant, operation, body, *, timeout=5):
        config = self.engine.tenants[tenant]
        try:
            url = internal_url(config['source_url'])
            path = '/api/internal-chat/service/' + operation + '/'
            raw = json.dumps(body, separators=(',', ':')).encode()
            headers = sign_request(config['secret'], tenant, 'POST', urlsplit(url).path.rstrip('/') + path, raw)
            headers['Content-Type'] = 'application/json'
            if config.get('source_host'): headers['Host'] = config['source_host']
            response = httpx.post(url + path, content=raw, headers=headers, timeout=timeout, trust_env=False)
            envelope = response.json()
            if not response.is_success or type(envelope.get('code')) is not int or envelope.get('code') != 0 or not isinstance(envelope.get('data'), dict): raise ValueError
            return envelope['data']
        except (KeyError, OSError, ValueError, TypeError, AttributeError, httpx.HTTPError):
            raise ServiceError(UNAVAILABLE, 503) from None

    def _authority_request(self, tenant, checks, *, viewer=None, discover=False, timeout=5):
        body = {'checks': checks, 'discover': discover}
        if viewer is not None: body['viewer_id'] = viewer
        data = self._source_request(tenant, 'order-authorizations', body, timeout=timeout)
        if type(data.get('has_order_access')) is not bool or not isinstance(data.get('groups'), list): raise ServiceError(UNAVAILABLE, 503)
        return data

    def _authority(self, tenant, checks, *, viewer=None, discover=False):
        checks = [dict(check) for check in checks]
        cids = [c['conversation_id'] for c in checks if 'conversation_id' in c]
        if cids:
            with self.store.connect() as db:
                rows = db.execute("SELECT conversation_id,metadata FROM rooms WHERE tenant_id=%s AND conversation_id=ANY(%s) AND kind='order'", (tenant, cids)).fetchall()
            numbers = {r['conversation_id']: r['metadata'].get('local_order_no') or r['metadata'].get('order_no') for r in rows}
            for check in checks:
                if numbers.get(check.get('conversation_id')): check['order_no'] = numbers[check['conversation_id']]
        allowed = {}
        deadline = monotonic() + 5
        for offset in range(0, max(1, len(checks)), 500):
            batch = checks[offset:offset + 500]
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ServiceError(UNAVAILABLE, 503)
            response = self._authority_request(tenant, batch, viewer=viewer, discover=discover and offset == 0, timeout=remaining)
            if monotonic() >= deadline:
                raise ServiceError(UNAVAILABLE, 503)
            requested = {row['conversation_id']: set(row['user_ids']) for row in batch}
            seen = set()
            for group in response['groups']:
                try:
                    cid = integer(group['conversation_id'], 'conversation_id', 1, ID_END)
                    default = group['default_join_user_ids']
                    room = group['room']
                    if not isinstance(default, list) or not isinstance(room, dict) or room.get('id') != cid or room.get('kind') != 'order':
                        raise ValueError
                    self._validate_authority_room(room)
                    self._observed_revision(group, 'completion')
                    self._observed_revision(group, 'deletion')
                    if 'peer_link' in group: self._validate_peer_link(group['peer_link'])
                    member_users = {m['local_user_id'] for m in room['members'] if m.get('local_user_id') is not None}
                    for profile in group.get('participants', []):
                        member_users.add(self._validate_participant(profile))
                    member_users.update(integer(uid, 'default_join_user', 1, ID_START - 1) for uid in default)
                    if type(group.get('deleted', False)) is not bool: raise ValueError
                    if cid not in requested:
                        if not discover or offset or viewer not in default:
                            raise ValueError
                        requested[cid] = {viewer, *member_users}
                    for key in ('read_user_ids', 'reopen_user_ids'):
                        if not isinstance(group[key], list) or any(type(uid) is not int or uid not in requested[cid] | member_users for uid in group[key]):
                            raise ValueError
                    if type(group['completed']) is not bool or cid in seen:
                        raise ValueError
                    seen.add(cid)
                    allowed[cid] = group
                except (KeyError, TypeError, ValueError, ServiceError):
                    raise ServiceError(UNAVAILABLE, 503) from None
        return allowed

    @staticmethod
    def _validate_authority_room(room):
        fields(room, {'id', 'kind', 'members', 'order_no', 'local_order_no', 'title', 'first_user_id', 'second_user_id',
            'can_send', 'can_pin_user_ids', 'denied_user_ids', 'restricted_user_ids', 'policy_version', 'preferences',
            'first_read_seq', 'second_read_seq', 'pinned_message_id'}, {'id', 'kind', 'members'})
        integer(room['id'], 'conversation.id', 1, ID_END)
        if room['kind'] != 'order' or not isinstance(room['members'], list) or len(room['members']) > 1000:
            raise ServiceError('Invalid business members')
        if type(room.get('can_send', False)) is not bool:
            raise ServiceError('Invalid business send flag')
        members, users = set(), set()
        for member in room['members']:
            fields(member, {'id', 'local_user_id', 'name', 'avatar_url', 'left_at', 'studio_name', 'order_role',
                'read_seq', 'publisher', 'acceptor', 'worker', 'follower', 'designated_follower', 'manual_joined'}, {'id', 'name'})
            mid = integer(member['id'], 'member.id', 1, ID_END)
            uid = member.get('local_user_id')
            if uid is not None: uid = integer(uid, 'member.local_user_id', 1, ID_START - 1)
            if mid in members or uid is not None and uid in users:
                raise ServiceError('Duplicate business member')
            members.add(mid)
            if uid is not None:
                users.add(integer(uid, 'member.local_user_id', 1, ID_START - 1))
            text(member['name'], 150, 'member.name')
            text(member.get('studio_name', ''), 100, 'member.studio_name')
            for key, maximum in (('avatar_url', 2048), ('left_at', 100)):
                if member.get(key) is not None: text(member[key], maximum, 'member.' + key)
            if member.get('order_role', '') not in ('', 'worker', 'publisher', 'acceptor', 'follower'):
                raise ServiceError('Invalid member role')
            for key in ('publisher', 'acceptor', 'worker', 'follower', 'designated_follower', 'manual_joined'):
                if key in member and type(member[key]) is not bool: raise ServiceError('Invalid member flag')
            if 'read_seq' in member: integer(member['read_seq'], 'read_seq', 0)
        for key in ('order_no', 'local_order_no', 'title'):
            if room.get(key) is not None: text(room[key], 255, key)
        for key in ('can_pin_user_ids', 'denied_user_ids', 'restricted_user_ids'):
            if key in room:
                if not isinstance(room[key], list) or len(room[key]) > 1000: raise ServiceError('Invalid member permission list')
                for uid in room[key]: integer(uid, key, 1, ID_START - 1)
        for key in ('first_user_id', 'second_user_id'):
            if room.get(key) is not None: integer(room[key], key, 1, ID_START - 1)
        for key in ('first_read_seq', 'second_read_seq'):
            if key in room: integer(room[key], key, 0)
        if room.get('pinned_message_id') is not None: integer(room['pinned_message_id'], 'pinned_message_id', 1, ID_END)
        if 'preferences' in room:
            if not isinstance(room['preferences'], list): raise ServiceError('Invalid preferences')
            for preference in room['preferences']:
                fields(preference, {'user_id', 'pinned', 'remark', 'marked_unread'}, {'user_id'})
                integer(preference['user_id'], 'preference.user_id', 1, ID_START - 1)
                for key in ('pinned', 'marked_unread'):
                    if key in preference and type(preference[key]) is not bool: raise ServiceError('Invalid preference flag')
                if 'remark' in preference: text(preference['remark'], 30, 'remark')

    @staticmethod
    def _timestamp_revision(grant, key):
        value = grant.get(key)
        if value is None: return None
        text(value, 100, key, blank=False)
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.utcoffset() is None: raise ValueError
            return parsed.astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            raise ServiceError('Invalid timestamp revision') from None

    def _completion_revision(self, grant):
        return self._timestamp_revision(grant, 'completion_revision')

    def _deletion_revision(self, grant):
        return self._timestamp_revision(grant, 'deletion_revision')

    def _observed_revision(self, grant, kind):
        values = [self._timestamp_revision(grant, key) for key in (kind + '_revision', 'last_' + kind + '_revision')]
        present = [value for value in values if value is not None]
        return max(present, key=datetime.fromisoformat) if present else None

    def _prepare_authority(self, tenant, operation, query, viewer):
        if operation in ('activate', 'status', 'policy', 'room', 'start', 'peer', 'order_send', 'order_preview', 'pause', 'resume'):
            return {}, False
        collection = operation in ('conversations', 'unread', 'sync')
        with self.store.connect() as db:
            if collection:
                rows = db.execute('''SELECT r.conversation_id,m.local_user_id FROM rooms r JOIN members m
                    ON m.tenant_id=r.tenant_id AND m.conversation_id=r.conversation_id
                    WHERE r.tenant_id=%s AND r.kind='order' AND m.local_user_id IS NOT NULL
                    AND EXISTS (SELECT 1 FROM members own WHERE own.tenant_id=r.tenant_id
                     AND own.conversation_id=r.conversation_id AND own.local_user_id=%s)''', (tenant, viewer)).fetchall()
            else:
                cid = integer(query.get('conversation_id'), 'conversation_id', 1, ID_END)
                room = db.execute('SELECT kind FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
                if room and room['kind'] != 'order':
                    return {}, False
                rows = db.execute('SELECT conversation_id,local_user_id FROM members WHERE tenant_id=%s AND conversation_id=%s AND local_user_id IS NOT NULL', (tenant, cid)).fetchall()
                rows.append({'conversation_id': cid, 'local_user_id': viewer})
        groups = {}
        for row in rows:
            groups.setdefault(row['conversation_id'], {viewer}).add(row['local_user_id'])
        checks = [{'conversation_id': cid, 'user_ids': sorted(users)} for cid, users in groups.items()]
        try:
            grants = self._authority(tenant, checks, viewer=viewer, discover=collection)
        except DatabasePoolBusy:
            raise
        except ServiceError:
            if collection:
                return {}, True
            raise
        additions = [grant for cid, grant in grants.items() if not collection or cid not in groups]
        if additions:
            with self.store.tenant(tenant) as db:
                self._merge_authority_rooms(db, tenant, additions)
        return grants, False

    def _grant(self, tenant, cid):
        context = _grants.get()
        if context is None or context['tenant'] != tenant or context.get('unavailable'):
            raise ServiceError(UNAVAILABLE, 503)
        return context['groups'].get(cid, {'read_user_ids': [], 'reopen_user_ids': [], 'completed': False})

    def _can_read_order(self, tenant, cid, viewer):
        return viewer in self._grant(tenant, cid)['read_user_ids']

    def _merge_authority_room(self, db, tenant, payload):
        self._merge_authority_rooms(db, tenant, [{'conversation_id': payload['id'], 'room': payload, 'completed': False}], observe_completion=False)

    def _merge_authority_rooms(self, db, tenant, grants, *, observe_completion=True, allow_create=False):
        cids = [integer(grant['conversation_id'], 'conversation_id', 1, ID_END) for grant in grants]
        if not allow_create and cids:
            present = {row['conversation_id'] for row in db.execute("SELECT conversation_id FROM rooms WHERE tenant_id=%s AND conversation_id=ANY(%s) AND kind='order'", (tenant, cids)).fetchall()}
            if set(cids) - present: raise ServiceError('Existing order history is not ready', 503)
        grants = self._with_default_participants(db, tenant, grants)
        rooms, members = [], []
        for grant in sorted(grants, key=lambda item: item['conversation_id']):
            payload = grant['room']
            self._validate_authority_room(payload)
            cid = integer(payload['id'], 'conversation_id', 1, ID_END)
            metadata = {k: payload[k] for k in ('id', 'kind', 'order_no', 'local_order_no', 'title', 'can_pin_user_ids', 'can_send') if k in payload}
            rooms.append({'cid': cid, 'metadata': metadata, 'completed': grant['completed'], 'revision': self._observed_revision(grant, 'completion'), 'deleted': grant.get('deleted', False), 'deletion_revision': self._observed_revision(grant, 'deletion')})
            for item in payload['members']:
                uid = item.get('local_user_id')
                if uid is not None:
                    uid = integer(uid, 'member.local_user_id', 1, ID_START - 1)
                mid = integer(item['id'], 'member.id', 1, ID_END)
                members.append({'cid': cid, 'mid': mid, 'uid': uid, 'metadata': {**item, 'id': mid, 'left_at': None}})
        members.sort(key=lambda item: (item['cid'], item['mid']))
        packed_rooms, packed_members = Jsonb(rooms), Jsonb(members)
        if db.execute('''SELECT 1 FROM rooms WHERE tenant_id=%s AND kind<>'order'
                AND conversation_id IN (SELECT cid FROM jsonb_to_recordset(%s) AS x(cid bigint)) LIMIT 1''', (tenant, packed_rooms)).fetchone():
            raise ServiceError(UNAVAILABLE, 503)
        db.execute('''INSERT INTO rooms (tenant_id,conversation_id,room_id,kind,metadata)
            SELECT %s,cid,'pending:' || cid::text,'order',metadata FROM jsonb_to_recordset(%s) AS x(cid bigint,metadata jsonb) ORDER BY cid
            ON CONFLICT (tenant_id,conversation_id) DO UPDATE SET metadata=rooms.metadata || EXCLUDED.metadata''', (tenant, packed_rooms))
        db.execute('''INSERT INTO members (tenant_id,conversation_id,member_id,local_user_id,active,metadata)
            SELECT %s,x.cid,coalesce(old.member_id,x.mid),x.uid,true,
                coalesce(old.metadata,'{}'::jsonb) || x.metadata || jsonb_build_object('id',coalesce(old.member_id,x.mid))
                || CASE WHEN old.metadata->>'manual_joined'='true' THEN '{"manual_joined":true}'::jsonb ELSE '{}'::jsonb END
            FROM jsonb_to_recordset(%s) AS x(cid bigint,mid bigint,uid bigint,metadata jsonb)
            LEFT JOIN members old ON old.tenant_id=%s AND old.conversation_id=x.cid
                AND (old.local_user_id=x.uid OR x.uid IS NULL AND old.member_id=x.mid)
            ORDER BY x.cid,coalesce(old.member_id,x.mid)
            ON CONFLICT (tenant_id,conversation_id,member_id) DO UPDATE SET active=true,metadata=EXCLUDED.metadata''',
            (tenant, packed_members, tenant))
        db.execute('''INSERT INTO core_reads (tenant_id,conversation_id,user_id)
            SELECT DISTINCT %s,cid,uid FROM jsonb_to_recordset(%s) AS x(cid bigint,uid bigint) WHERE uid IS NOT NULL ORDER BY cid,uid
            ON CONFLICT DO NOTHING''', (tenant, packed_members))
        db.execute('''INSERT INTO core_identities (tenant_id,user_id,payload)
            SELECT DISTINCT ON (uid) %s,uid,jsonb_build_object('id',uid,'name',metadata->>'name','avatar_url',metadata->'avatar_url')
            FROM jsonb_to_recordset(%s) AS x(uid bigint,metadata jsonb) WHERE uid IS NOT NULL ORDER BY uid
            ON CONFLICT (tenant_id,user_id) DO UPDATE SET payload=EXCLUDED.payload''', (tenant, packed_members))
        if not observe_completion:
            return
        self._observe_archives(db, tenant, rooms)
        for grant in grants:
            self._register_peer(db, tenant, grant)

    def _observe_archives(self, db, tenant, observations):
        db.execute('''INSERT INTO core_archives (tenant_id,conversation_id,completed_seen,archived,completion_revision,deleted_seen,deletion_revision)
            SELECT %s,cid,completed,completed OR deleted OR revision IS NOT NULL OR deletion_revision IS NOT NULL,revision,deleted,deletion_revision FROM jsonb_to_recordset(%s)
                AS x(cid bigint,completed boolean,revision text,deleted boolean,deletion_revision text) ORDER BY cid
            ON CONFLICT (tenant_id,conversation_id) DO UPDATE SET
                archived=CASE WHEN (
                    EXCLUDED.deletion_revision IS NOT NULL AND (core_archives.deletion_revision IS NULL
                        OR EXCLUDED.deletion_revision::timestamptz>core_archives.deletion_revision::timestamptz)
                    OR EXCLUDED.deleted_seen AND EXCLUDED.deletion_revision IS NULL AND NOT core_archives.deleted_seen) THEN true
                    WHEN (
                    EXCLUDED.completion_revision IS NOT NULL AND (core_archives.completion_revision IS NULL
                        OR EXCLUDED.completion_revision::timestamptz>core_archives.completion_revision::timestamptz)
                    OR EXCLUDED.completed_seen AND EXCLUDED.completion_revision IS NULL AND NOT core_archives.completed_seen) THEN true
                    WHEN core_archives.activity_at<=now()-interval '72 hours' THEN true ELSE core_archives.archived END,
                completed_seen=EXCLUDED.completed_seen,deleted_seen=EXCLUDED.deleted_seen,
                deletion_revision=CASE WHEN EXCLUDED.deletion_revision IS NOT NULL
                    AND (core_archives.deletion_revision IS NULL OR EXCLUDED.deletion_revision::timestamptz>core_archives.deletion_revision::timestamptz)
                    THEN EXCLUDED.deletion_revision ELSE core_archives.deletion_revision END,
                completion_revision=CASE WHEN EXCLUDED.completion_revision IS NOT NULL
                    AND (core_archives.completion_revision IS NULL OR EXCLUDED.completion_revision::timestamptz>core_archives.completion_revision::timestamptz)
                    THEN EXCLUDED.completion_revision ELSE core_archives.completion_revision END''',
            (tenant, Jsonb(observations)))

    def _archive_state(self, db, tenant, cid, grant):
        self._observe_archives(db, tenant, [{'cid': cid, 'completed': grant.get('completed', False),
            'revision': self._observed_revision(grant, 'completion'), 'deleted': grant.get('deleted', False), 'deletion_revision': self._observed_revision(grant, 'deletion')}])
        return db.execute('SELECT * FROM core_archives WHERE tenant_id=%s AND conversation_id=%s FOR UPDATE', (tenant, cid)).fetchone()

    def _order_writable(self, db, tenant, room):
        grant = self._grant(tenant, room['conversation_id'])
        archive = self._archive_state(db, tenant, room['conversation_id'], grant)
        return not archive['archived'] and room['metadata'].get('can_send', False)
