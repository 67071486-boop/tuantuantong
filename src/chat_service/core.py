import base64
import hashlib
import io
import json
import uuid
from datetime import datetime, timezone

from PIL import Image, UnidentifiedImageError
from psycopg.errors import UndefinedTable
from psycopg.types.json import Jsonb

from .config import ServiceError, integer, internal_url

from .core_authority import CoreAuthority, REVOKED, _grants
from .core_orders import CoreOrders
from .core_peer import CorePeer
from .core_state import CoreState
from .core_delivery import CoreDelivery
from .core_utils import DEFAULT_PREFERENCE, ID_END, ID_START, fields, text
from .image_limits import MAX_CHAT_ALBUM_BYTES, MAX_CHAT_IMAGE_BYTES, MAX_CHAT_IMAGE_PIXELS

CORE_PROTOCOL = 5
ORDER_AUTHORITY_PROTOCOL = 4
USER_WRITES = {'start', 'send', 'order_send', 'join', 'read', 'preferences', 'revoke', 'pin', 'reopen'}


class Core(CoreState, CoreDelivery, CoreAuthority, CoreOrders, CorePeer):
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self._peer_enabled = set()

    def _normalize_order_participation(self, db, tenant):
        db.execute('''UPDATE members m SET active=true,metadata=jsonb_set(m.metadata,'{left_at}','null'::jsonb)
            FROM rooms r WHERE m.tenant_id=%s AND r.tenant_id=m.tenant_id AND r.conversation_id=m.conversation_id
            AND r.kind='order' AND (NOT m.active OR m.metadata->'left_at'<>'null'::jsonb)''', (tenant,))

    def _require_protocol(self, db, tenant, *, peer=False):
        current = db.execute('SELECT required_protocol FROM core_control WHERE tenant_id=%s FOR UPDATE', (tenant,)).fetchone()
        if current and current['required_protocol'] < 2:
            self._normalize_order_participation(db, tenant)
        protocol = CORE_PROTOCOL if peer else ORDER_AUTHORITY_PROTOCOL
        db.execute('UPDATE core_control SET required_protocol=%s WHERE tenant_id=%s AND required_protocol<%s',
                   (protocol, tenant, protocol))

    def is_active(self, tenant):
        try:
            with self.store.connect() as db:
                row = db.execute('SELECT active FROM core_control WHERE tenant_id=%s', (tenant,)).fetchone()
                return bool(row and row['active'])
        except UndefinedTable:
            return False

    def active_tenants(self):
        try:
            with self.store.connect() as db:
                return [row['tenant_id'] for row in db.execute('SELECT tenant_id FROM core_control WHERE active ORDER BY tenant_id').fetchall()]
        except UndefinedTable:
            return []

    def _state(self, db, tenant, *, active=True):
        row = db.execute('SELECT * FROM core_control WHERE tenant_id=%s', (tenant,)).fetchone()
        if not row or (active and not row['active']):
            raise ServiceError('Chat core is not activated', 503)
        return row

    def _status(self, db, tenant):
        row = db.execute('SELECT * FROM core_control WHERE tenant_id=%s', (tenant,)).fetchone()
        result = dict(row) if row else {'active': False, 'cursor': 0, 'next_id': ID_START}
        result.pop('tenant_id', None)
        result.setdefault('writes_paused', False)
        result['pending_messages'] = db.execute('SELECT count(*) AS n FROM core_outbox WHERE tenant_id=%s', (tenant,)).fetchone()['n']
        result['pending_peer_events'] = db.execute('SELECT count(*) AS n FROM core_peer_outbox WHERE tenant_id=%s', (tenant,)).fetchone()['n']
        result['pending_messages'] += result['pending_peer_events']
        result['pending_notifications'] = db.execute('SELECT count(*) AS n FROM core_notifications WHERE tenant_id=%s', (tenant,)).fetchone()['n']
        return result

    def _allocate(self, db, tenant):
        row = db.execute('''UPDATE core_control SET next_id=next_id+1
            WHERE tenant_id=%s AND next_id<=%s RETURNING next_id-1 AS id''', (tenant, ID_END)).fetchone()
        if not row:
            raise ServiceError('Chat identifier range exhausted', 503)
        return row['id']

    def _identity(self, db, tenant, identity):
        fields(identity, {'id', 'name', 'avatar_url'}, {'id', 'name'})
        user = integer(identity['id'], 'identity.id', 1, ID_START - 1)
        text(identity['name'], 150, 'identity.name')
        avatar = identity.get('avatar_url')
        if avatar is not None:
            text(avatar, 2048, 'identity.avatar_url')
        value = {'id': user, 'name': identity['name'], 'avatar_url': avatar}
        db.execute('''INSERT INTO core_identities (tenant_id,user_id,payload) VALUES (%s,%s,%s)
            ON CONFLICT (tenant_id,user_id) DO UPDATE SET payload=EXCLUDED.payload''', (tenant, user, Jsonb(value)))
        return value

    def _room(self, db, tenant, cid, viewer, readonly=False):
        room = db.execute('SELECT * FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
        if not room:
            raise ServiceError('Conversation not found', 404)
        if room['kind'] == 'order' and not self._can_read_order(tenant, cid, viewer):
            raise ServiceError(REVOKED, 403)
        if readonly:
            if room['kind'] != 'order':
                raise ServiceError('Readonly grant only applies to order conversations', 403)
        elif not db.execute('''SELECT 1 FROM members WHERE tenant_id=%s AND conversation_id=%s
                AND local_user_id=%s AND active''', (tenant, cid, viewer)).fetchone():
            raise ServiceError('Conversation membership required', 403)
        return room

    def _audience(self, db, tenant, cid):
        return [r['local_user_id'] for r in db.execute('''SELECT DISTINCT local_user_id FROM members
            WHERE tenant_id=%s AND conversation_id=%s AND active AND local_user_id IS NOT NULL''', (tenant, cid)).fetchall()]

    def _event(self, db, tenant, cid, kind, message_id=None, audience=None, *, notify_message=True):
        users = self._audience(db, tenant, cid) if audience is None else audience
        cursor = db.execute('UPDATE core_control SET cursor=cursor+1 WHERE tenant_id=%s RETURNING cursor', (tenant,)).fetchone()['cursor']
        db.execute('''INSERT INTO core_events
            (tenant_id,event_id,conversation_id,kind,audience,message_id) VALUES (%s,%s,%s,%s,%s,%s)''',
            (tenant, cursor, cid, kind, Jsonb(users), message_id))
        payload = {'users': users}
        if kind == 'message.created' and message_id is not None and notify_message:
            msg = db.execute('SELECT sender_identity FROM messages WHERE tenant_id=%s AND message_id=%s', (tenant, message_id)).fetchone()
            identity = msg['sender_identity']
            payload['incoming'] = {'id': message_id, 'conversation_id': cid,
                                   'author': int(identity[6:]) if identity.startswith('local:') else None}
        if users:
            db.execute('INSERT INTO core_notifications (tenant_id,event_id,payload) VALUES (%s,%s,%s)', (tenant, cursor, Jsonb(payload)))
        return cursor

    def _activate(self, db, tenant, payload):
        fields(payload, {'minimum_cursor'}, {'minimum_cursor'})
        minimum = integer(payload['minimum_cursor'], 'minimum_cursor', 0)
        state = db.execute('SELECT * FROM sync_state WHERE tenant_id=%s', (tenant,)).fetchone()
        if not state or state['phase'] != 'changes' or state['cursor'] < minimum:
            raise ServiceError('Mirror must catch up before activation', 409)
        realtime = self.engine.tenants[tenant].get('realtime', {})
        fields(realtime, {'url', 'secret', 'tenant'}, {'url', 'secret', 'tenant'})
        internal_url(realtime['url'])
        text(realtime['secret'], 4096, 'realtime.secret', blank=False)
        if not isinstance(realtime['tenant'], str) or not realtime['tenant'].replace('_', '').isalnum():
            raise ServiceError('Invalid realtime tenant')
        existing = db.execute('SELECT active FROM core_control WHERE tenant_id=%s', (tenant,)).fetchone()
        if existing and existing['active']:
            self._require_protocol(db, tenant)
            return self._status(db, tenant)
        for table, column in [('rooms', 'conversation_id'), ('messages', 'message_id'), ('members', 'member_id'), ('members', 'local_user_id')]:
            high = db.execute(f'SELECT max({column}) AS high FROM {table} WHERE tenant_id=%s', (tenant,)).fetchone()['high']
            if high is not None and high >= ID_START:
                raise ServiceError('Legacy identifiers overlap reserved core range', 409)
        db.execute('''INSERT INTO core_control (tenant_id,active,cursor,required_protocol) VALUES (%s,true,%s,%s)
            ON CONFLICT (tenant_id) DO UPDATE SET active=true,cursor=EXCLUDED.cursor,required_protocol=EXCLUDED.required_protocol''', (tenant, state['cursor'], ORDER_AUTHORITY_PROTOCOL))
        for room in db.execute('SELECT * FROM rooms WHERE tenant_id=%s', (tenant,)).fetchall():
            metadata, cid = room['metadata'], room['conversation_id']
            for member in db.execute('SELECT * FROM members WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchall():
                uid = member['local_user_id']
                if uid is None:
                    continue
                data = member['metadata']
                self._identity(db, tenant, {'id': uid, 'name': data.get('name', str(uid)), 'avatar_url': data.get('avatar_url')})
                seq = data.get('read_seq', 0)
                if room['kind'] == 'direct':
                    seq = metadata.get('first_read_seq' if metadata.get('first_user_id') == uid else 'second_read_seq', seq)
                db.execute('INSERT INTO core_reads (tenant_id,conversation_id,user_id,seq) VALUES (%s,%s,%s,%s)', (tenant, cid, uid, integer(seq, 'read_seq', 0)))
            for pref in metadata.get('preferences', []):
                uid = integer(pref['user_id'], 'preference.user_id')
                value = {k: pref.get(k, default) for k, default in DEFAULT_PREFERENCE.items()}
                db.execute('''INSERT INTO core_preferences (tenant_id,conversation_id,user_id,payload)
                    VALUES (%s,%s,%s,%s)''', (tenant, cid, uid, Jsonb(value)))
        self._normalize_order_participation(db, tenant)
        return self._status(db, tenant)

    @staticmethod
    def _message_payload(row):
        payload = dict(row['payload'])
        if row['revoked']:
            payload['text'], payload['revoked'] = '', True
            for key in ('image', 'album_id', 'mention_member_ids'):
                payload.pop(key, None)
        return payload

    def _history(self, db, tenant, room, query, operation):
        fields(query, {'conversation_id', 'before', 'around', 'limit'} if operation == 'history' else
               {'conversation_id', 'q', 'before', 'sender_id', 'date_from', 'date_to', 'image_only', 'limit'})
        cid = room['conversation_id']
        limit = integer(query.get('limit', 60 if operation == 'history' else 30), 'limit', 1, 100)
        args, where = [tenant, cid], 'tenant_id=%s AND conversation_id=%s'
        before = integer(query['before'], 'before') if query.get('before') is not None else None
        if operation == 'search':
            keyword = text(query.get('q', ''), 100, 'q')
            image_only = query.get('image_only', False)
            if not isinstance(image_only, bool):
                raise ServiceError('Invalid image_only')
            if not keyword and not image_only and not {'sender_id', 'date_from', 'date_to'} & query.keys():
                raise ServiceError('Search requires a filter')
            where += ' AND NOT revoked'
            if keyword:
                where += ' AND position(lower(%s) in lower(text_body))>0'
                args.append(keyword)
            if image_only:
                where += " AND payload->'image' IS NOT NULL AND payload->'image' <> 'null'::jsonb"
            if 'sender_id' in query:
                where += ' AND sender_id=%s'
                args.append(integer(query['sender_id'], 'sender_id'))
            for name, compare in [('date_from', '>='), ('date_to', '<=')]:
                if name in query:
                    where += ' AND created_at' + compare + '%s'
                    args.append(integer(query[name], name, 0, 253402300799) + (0.999999 if name == 'date_to' else 0))
            if 'date_from' in query and 'date_to' in query and int(query['date_from']) > int(query['date_to']):
                raise ServiceError('Invalid date range')
            count = db.execute('SELECT count(*) AS n FROM messages WHERE ' + where, args).fetchone()['n']
            if before is not None:
                where += ' AND seq<%s'
                args.append(before)
            rows = db.execute('SELECT * FROM messages WHERE ' + where + ' ORDER BY seq DESC LIMIT %s', (*args, limit + 1)).fetchall()
            return {'messages': [self._message_payload(r) for r in rows[:limit]], 'count': count, 'has_more': len(rows) > limit}
        if query.get('around') is not None:
            if before is not None:
                raise ServiceError('before and around cannot be combined')
            target = db.execute('SELECT * FROM messages WHERE ' + where + ' AND message_id=%s', (*args, integer(query['around'], 'around'))).fetchone()
            if not target:
                raise ServiceError('Message not found', 404)
            half = limit // 2
            old = db.execute('SELECT * FROM messages WHERE ' + where + ' AND seq<%s ORDER BY seq DESC LIMIT %s', (*args, target['seq'], half + 10)).fetchall()
            new = db.execute('SELECT * FROM messages WHERE ' + where + ' AND seq>%s ORDER BY seq LIMIT %s', (*args, target['seq'], half + 10)).fetchall()
            old, more = self.engine._edge(old, half)
            new, newer = self.engine._edge(new, half)
            rows = [*reversed(old), target, *new]
        else:
            if before is not None:
                where += ' AND seq<%s'
                args.append(before)
            rows = db.execute('SELECT * FROM messages WHERE ' + where + ' ORDER BY seq DESC LIMIT %s', (*args, limit + 10)).fetchall()
            rows, more = self.engine._edge(rows, limit)
            newer = bool(rows and db.execute('SELECT 1 FROM messages WHERE tenant_id=%s AND conversation_id=%s AND seq>%s LIMIT 1', (tenant, cid, rows[0]['seq'])).fetchone())
            rows.reverse()
        return {'messages': [self._message_payload(r) for r in rows], 'has_more': more, 'has_newer': newer}

    def _send(self, db, tenant, room, viewer, payload):
        fields(payload, {'text', 'client_id', 'reply_to_id', 'image', 'images', 'mention_member_ids'}, {'client_id'})
        cid = room['conversation_id']
        content = text(payload.get('text', ''), 4000, 'message.text')
        client_id = text(payload['client_id'], 100, 'client_id', blank=False)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        prior = db.execute('''SELECT fingerprint,response FROM core_commands
            WHERE tenant_id=%s AND conversation_id=%s AND user_id=%s AND client_id=%s''', (tenant, cid, viewer, client_id)).fetchone()
        if prior:
            if prior['fingerprint'] != fingerprint:
                raise ServiceError('Client identifier reused for another message', 409)
            cached = prior['response']
            ids = [m['id'] for m in cached['messages']] if 'messages' in cached else [cached['id']]
            rows = [db.execute('SELECT * FROM messages WHERE tenant_id=%s AND message_id=%s', (tenant, mid)).fetchone() for mid in ids]
            if any(row is None for row in rows):
                raise ServiceError('Accepted message projection is missing', 503)
            messages = [self._message_payload(row) for row in rows]
            return {'messages': messages} if 'messages' in cached else messages[0]
        member = db.execute('''SELECT * FROM members WHERE tenant_id=%s AND conversation_id=%s
            AND local_user_id=%s AND active''', (tenant, cid, viewer)).fetchone()
        if room['kind'] == 'order' and not self._order_writable(db, tenant, room):
            raise ServiceError('Order conversation is read only', 403)
        sender = viewer if room['kind'] == 'direct' else member['member_id']
        if payload.get('reply_to_id') is not None:
            reply = integer(payload['reply_to_id'], 'reply_to_id')
            if not db.execute('SELECT 1 FROM messages WHERE tenant_id=%s AND conversation_id=%s AND message_id=%s AND NOT revoked', (tenant, cid, reply)).fetchone():
                raise ServiceError('Reply message not found', 404)
        else:
            reply = None
        mentions = payload.get('mention_member_ids', [])
        if not isinstance(mentions, list) or len(mentions) > 50:
            raise ServiceError('Invalid mentions')
        mentions = list(dict.fromkeys(integer(value, 'mention') for value in mentions))
        for mention in mentions:
            if room['kind'] != 'order' or mention == sender or not db.execute('''SELECT 1 FROM members
                    WHERE tenant_id=%s AND conversation_id=%s AND member_id=%s AND active''', (tenant, cid, mention)).fetchone():
                raise ServiceError('Mention member not found')
        if 'image' in payload and 'images' in payload:
            raise ServiceError('Only one image form is allowed')
        images = payload.get('images', [payload['image']] if 'image' in payload else [])
        if not isinstance(images, list) or ('images' in payload and not 2 <= len(images) <= 10):
            raise ServiceError('Invalid album')
        if not content and not images:
            raise ServiceError('Message content required')
        image_values, total = [], 0
        for image in images:
            value = self._image_input(image)
            total += len(base64.b64decode(value['data']))
            image_values.append(value)
        if 'images' in payload and total > MAX_CHAT_ALBUM_BYTES:
            raise ServiceError('Album is too large')
        latest = db.execute('SELECT coalesce(max(seq),0) AS seq FROM messages WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()['seq']
        album = str(uuid.uuid4()) if len(images) > 1 else None
        result = []
        for index, image in enumerate(image_values or [None]):
            mid = self._allocate(db, tenant)
            now = datetime.now(timezone.utc)
            message = {'id': mid, 'seq': latest + index + 1, 'conversation_id': cid, 'sender_id': sender,
                'text': content if index == 0 else '', 'created_at': now.isoformat(), 'revoked': False,
                'reply_to_id': reply if index == 0 else None, 'delivery_state': 'queued'}
            if image:
                message['image'] = {key: image[key] for key in ('mime', 'width', 'height')}
                if album is not None:
                    message['album_id'] = album
            if mentions and index == 0:
                message['mention_member_ids'] = mentions
            db.execute('''INSERT INTO messages
                (tenant_id,conversation_id,message_id,seq,sender_id,sender_identity,event_id,revoked,text_body,created_at,payload)
                VALUES (%s,%s,%s,%s,%s,%s,NULL,false,%s,%s,%s)''',
                (tenant, cid, mid, message['seq'], sender, 'local:' + str(viewer), message['text'], now.timestamp(), Jsonb(message)))
            db.execute('INSERT INTO core_outbox (tenant_id,message_id,action,payload) VALUES (%s,%s,%s,%s)',
                (tenant, mid, 'send', Jsonb({'message': message, 'image': image})))
            self._event(db, tenant, cid, 'message.created', mid, notify_message=index == 0)
            result.append(message)
        if room['kind'] == 'order':
            db.execute('UPDATE core_archives SET activity_at=clock_timestamp() WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid))
            self._queue_peer_messages(db, tenant, cid, result)
        response = {'messages': result} if album is not None else result[0]
        db.execute('''INSERT INTO core_commands (tenant_id,conversation_id,user_id,client_id,fingerprint,response)
            VALUES (%s,%s,%s,%s,%s,%s)''', (tenant, cid, viewer, client_id, fingerprint, Jsonb({'messages': [{'id': m['id']} for m in result]} if album is not None else {'id': result[0]['id']})))
        return response

    @staticmethod
    def _image_input(image):
        fields(image, {'mime', 'data', 'width', 'height'}, {'mime', 'data'})
        formats = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}
        if not isinstance(image['mime'], str) or image['mime'] not in formats or not isinstance(image['data'], str):
            raise ServiceError('Invalid image format')
        try:
            raw = base64.b64decode(image['data'], validate=True)
            if not 0 < len(raw) <= MAX_CHAT_IMAGE_BYTES or base64.b64encode(raw).decode() != image['data']:
                raise ValueError('Invalid image size')
            with Image.open(io.BytesIO(raw), formats=[formats[image['mime']]]) as decoded:
                width, height = decoded.size
                if width * height > MAX_CHAT_IMAGE_PIXELS or getattr(decoded, 'is_animated', False):
                    raise ValueError('Invalid image dimensions')
                decoded.load()
        except (ValueError, TypeError, OSError, UnidentifiedImageError, Image.DecompressionBombError):
            raise ServiceError('Invalid image bytes') from None
        return {'mime': image['mime'], 'data': image['data'], 'width': width, 'height': height}

    def _handle(self, tenant, body):
        if tenant not in self.engine.tenants:
            raise ServiceError('Unknown tenant', 403)
        fields(body, {'operation', 'viewer_id', 'query', 'payload', 'readonly'}, {'operation'})
        operation = text(body['operation'], 32, 'operation', blank=False)
        query, payload = body.get('query', {}), body.get('payload', {})
        readonly = body.get('readonly', False)
        if not isinstance(readonly, bool) or not isinstance(query, dict) or not isinstance(payload, dict):
            raise ServiceError('Invalid core request')
        if readonly and operation not in ('history', 'search', 'image', 'order', 'order_preview'):
            raise ServiceError('Readonly grant cannot write', 403)
        read_operations = {'status', 'conversations', 'unread', 'history', 'search', 'image', 'sync', 'peer', 'notification', 'order', 'order_preview'}
        context = self.store.connect() if operation in read_operations else self.store.tenant(tenant)
        with context as db:
            if operation == 'status':
                fields(query, {}); fields(payload, {})
                return self._status(db, tenant)
            if operation == 'activate':
                fields(query, {})
                return self._activate(db, tenant, payload)
            if operation == 'pause':
                fields(query, {}); fields(payload, {})
                previous = db.execute('SELECT active FROM core_control WHERE tenant_id=%s', (tenant,)).fetchone()
                if previous and previous['active']: self._require_protocol(db, tenant)
                db.execute('''INSERT INTO core_control(tenant_id,writes_paused,required_protocol) VALUES (%s,true,%s)
                    ON CONFLICT(tenant_id) DO UPDATE SET writes_paused=true,
                    required_protocol=greatest(core_control.required_protocol,EXCLUDED.required_protocol)''', (tenant, ORDER_AUTHORITY_PROTOCOL))
                return self._status(db, tenant)
            if operation == 'resume':
                fields(query, {}); fields(payload, {})
                status = self._status(db, tenant)
                if (not status['active'] or not status['writes_paused'] or status['pending_messages']
                        or status['pending_notifications']):
                    raise ServiceError('聊天尚未停写排空，不能恢复', 409)
                self._require_protocol(db, tenant)
                db.execute('UPDATE core_control SET writes_paused=false WHERE tenant_id=%s', (tenant,))
                return self._status(db, tenant)
            state = self._state(db, tenant)
            if state['writes_paused'] and operation in USER_WRITES:
                raise ServiceError('内部沟通暂时不可用', 503)
            viewer = 0 if operation in ('room', 'policy') else integer(body.get('viewer_id'), 'viewer_id', 1, ID_START - 1)
            state_operations = {'start', 'conversations', 'unread', 'preferences', 'read', 'sync', 'peer',
                                'order', 'notification', 'revoke', 'pin', 'room', 'join', 'policy', 'reopen', 'order_send', 'order_preview'}
            if operation in state_operations:
                return getattr(self, '_op_' + operation)(db, tenant, viewer, query, payload, readonly)
            if operation in ('history', 'search', 'image', 'send'):
                cid = integer(query.get('conversation_id'), 'conversation_id', 1, ID_END)
                room = self._room(db, tenant, cid, viewer, readonly)
                if operation in ('history', 'search'):
                    fields(payload, {})
                    return self._history(db, tenant, room, query, operation)
                if operation == 'send':
                    fields(query, {'conversation_id'}, {'conversation_id'})
                    return self._send(db, tenant, room, viewer, payload)
                if operation == 'image':
                    fields(query, {'conversation_id', 'message_id'}, {'conversation_id', 'message_id'})
                    fields(payload, {})
                    mid = integer(query['message_id'], 'message_id', 1, ID_END)
                    row = db.execute('SELECT * FROM messages WHERE tenant_id=%s AND conversation_id=%s AND message_id=%s AND NOT revoked', (tenant, cid, mid)).fetchone()
                    if not row or not row['payload'].get('image'):
                        raise ServiceError('Image not found', 404)
                    if row['media_uri']:
                        account = self.engine._account(db, tenant)
                        raw, mime = self.engine.matrix.download(account['access_token'], row['media_uri'])
                        return {'mime': row['media_mime'] or mime, 'data': base64.b64encode(raw).decode()}
                    job = db.execute("SELECT payload FROM core_outbox WHERE tenant_id=%s AND message_id=%s AND action='send'", (tenant, mid)).fetchone()
                    if job and job['payload'].get('image'):
                        image = job['payload']['image']
                        return {'mime': image['mime'], 'data': image['data']}
                    raise ServiceError('Image delivery is incomplete', 503)
            raise ServiceError('Unknown core operation')


    def handle(self, tenant, body):
        if tenant not in self.engine.tenants or not isinstance(body, dict):
            return self._handle(tenant, body)
        operation = body.get('operation')
        query = body.get('query', {})
        if not isinstance(query, dict):
            return self._handle(tenant, body)
        viewer = 0 if operation in ('activate', 'status', 'policy', 'room', 'pause', 'resume') else integer(body.get('viewer_id'), 'viewer_id', 1, ID_START - 1)
        if operation not in ('activate', 'status', 'policy', 'pause', 'resume'):
            with self.store.tenant(tenant) as db:
                state = self._state(db, tenant)
                self._require_protocol(db, tenant)
                if state['writes_paused'] and operation in USER_WRITES:
                    raise ServiceError('内部沟通暂时不可用', 503)
        fact = self._order_fact(tenant, self._order_number(query.get('order_no')), viewer) if operation in ('order_send', 'order_preview') else None
        grants, unavailable = self._prepare_authority(tenant, operation, query, viewer)
        token = _grants.set({'tenant': tenant, 'groups': grants, 'unavailable': unavailable, 'order_fact': fact})
        try:
            result = self._handle(tenant, body)
            if unavailable and operation in ('conversations', 'unread', 'sync'):
                result['order_permission_unavailable'] = True
            return result
        finally:
            _grants.reset(token)
