import base64
import binascii
import copy
import hashlib
import io
from datetime import datetime

from PIL import Image, UnidentifiedImageError
from psycopg.types.json import Jsonb

from .config import ServiceError, integer
from .image_limits import MAX_CHAT_IMAGE_BYTES
from .matrix import quoted


class Engine:
    def __init__(self, store, matrix, tenants):
        self.store, self.matrix, self.tenants = store, matrix, tenants
        from .core import Core
        self.core = Core(self)

    def _account(self, db, tenant):
        account = db.execute('SELECT * FROM accounts WHERE tenant_id=%s AND source_identity=%s', (tenant, 'service:bot')).fetchone()
        if not account:
            account = self.matrix.account(tenant)
            db.execute('''INSERT INTO accounts
                (tenant_id,source_identity,username,password,matrix_id,access_token)
                VALUES (%s,%s,%s,%s,%s,%s)''',
                (tenant, 'service:bot', account['username'], account['password'], account['matrix_id'], account['access_token']))
        return account

    def _conversation(self, db, tenant, value, account):
        if not isinstance(value, dict) or value.get('kind') not in ('direct', 'order'):
            raise ServiceError('Invalid conversation snapshot')
        cid = integer(value.get('id'), 'conversation.id')
        current = db.execute('SELECT room_id FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
        room = current['room_id'] if current else self.matrix.room(tenant, cid, account, str(value.get('title') or ''))
        metadata = copy.deepcopy(value)
        members = metadata.pop('members', [])
        if not isinstance(members, list) or not isinstance(metadata.get('preferences', []), list):
            raise ServiceError('Invalid conversation members/preferences')
        db.execute('''INSERT INTO rooms (tenant_id,conversation_id,room_id,kind,metadata)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,conversation_id)
            DO UPDATE SET kind=EXCLUDED.kind,metadata=EXCLUDED.metadata''',
            (tenant, cid, room, value['kind'], Jsonb(metadata)))
        db.execute('DELETE FROM members WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid))
        for member in members:
            if not isinstance(member, dict):
                raise ServiceError('Invalid member')
            mid = integer(member.get('id'), 'member.id')
            local_id = member.get('local_user_id')
            if local_id is not None:
                local_id = integer(local_id, 'member.local_user_id')
            db.execute('''INSERT INTO members (tenant_id,conversation_id,member_id,local_user_id,active,metadata)
                VALUES (%s,%s,%s,%s,%s,%s)''', (tenant, cid, mid, local_id, member.get('left_at') is None, Jsonb(member)))
        return cid, room

    def _message(self, db, tenant, conversation, value, account):
        if not isinstance(value, dict):
            raise ServiceError('Invalid message snapshot')
        cid, room = conversation
        mid = integer(value.get('id'), 'message.id')
        seq = integer(value.get('seq'), 'message.seq')
        if value.get('conversation_id') is not None and integer(value['conversation_id']) != cid:
            raise ServiceError('Message conversation mismatch')
        revoked = value.get('revoked', False)
        if not isinstance(revoked, bool):
            raise ServiceError('Invalid revoked flag')
        previous = db.execute('SELECT * FROM messages WHERE tenant_id=%s AND message_id=%s', (tenant, mid)).fetchone()
        if previous and (previous['conversation_id'] != cid or previous['seq'] != seq):
            raise ServiceError('Message identity changed')
        revoked = revoked or bool(previous and previous['revoked'])
        payload = copy.deepcopy(value)
        image = payload.pop('image_data', None)
        local_id = payload.pop('sender_local_user_id', None)
        sender_id = payload.get('sender_id')
        if sender_id is not None:
            sender_id = integer(sender_id, 'message.sender_id')
        kind = db.execute('SELECT kind FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()['kind']
        if kind == 'direct':
            local_id = sender_id
        identity = 'local:' + str(integer(local_id)) if local_id is not None else f'external:{cid}:{sender_id}'
        if not isinstance(payload.get('text', ''), str) or len(payload.get('text', '')) > 4000:
            raise ServiceError('Invalid message text')
        text = '' if revoked else payload.get('text', '')
        payload.update({'conversation_id': cid, 'text': text, 'revoked': revoked})
        if revoked:
            for field in ('image', 'album_id', 'mention_member_ids'):
                payload.pop(field, None)
        media_uri = previous['media_uri'] if previous else None
        media_mime = previous['media_mime'] if previous else None
        event = previous['event_id'] if previous else None
        transaction = hashlib.sha256(f'{tenant}:message:{mid}'.encode()).hexdigest()
        if event is None:
            content = {'msgtype': 'm.text', 'body': text, 'tt_payload': payload,
                       'tt_sender_identity': identity}
            if image is not None and not revoked:
                if not isinstance(image, dict) or image.get('mime') not in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'):
                    raise ServiceError('Invalid image metadata')
                try:
                    raw = base64.b64decode(image['data'], validate=True)
                    if not 0 < len(raw) <= MAX_CHAT_IMAGE_BYTES:
                        raise ValueError('Image too large')
                    with Image.open(io.BytesIO(raw)) as decoded:
                        if decoded.width * decoded.height > 25_000_000:
                            raise ValueError('Image dimensions too large')
                        decoded.verify()
                except (KeyError, TypeError, ValueError, binascii.Error, UnidentifiedImageError, OSError, Image.DecompressionBombError):
                    raise ServiceError('Invalid image bytes') from None
                media_mime = image['mime']
                media_uri = self.matrix.upload(account['access_token'], raw, media_mime)
                content.update({'msgtype': 'm.image', 'url': media_uri,
                                'body': text or 'Image', 'info': {'mimetype': media_mime, 'size': len(raw)}})
            elif payload.get('image') and not revoked:
                raise ServiceError('Image snapshot is missing bytes', 503)
            event = self.matrix.send(room, account['access_token'], transaction, content)
        if revoked and not (previous and previous['revoked']):
            self.matrix.redact(room, event, account['access_token'], 'revoke_' + transaction)
        if revoked:
            media_uri, media_mime = None, None
        created = value.get('created_at', 0)
        try:
            created = datetime.fromisoformat(created.replace('Z', '+00:00')).timestamp() if isinstance(created, str) else float(created)
        except (ValueError, TypeError, OverflowError):
            raise ServiceError('Invalid message timestamp') from None
        db.execute('''INSERT INTO messages
            (tenant_id,conversation_id,message_id,seq,sender_id,sender_identity,event_id,revoked,text_body,created_at,media_uri,media_mime,payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (tenant_id,message_id) DO UPDATE SET
            revoked=EXCLUDED.revoked,text_body=EXCLUDED.text_body,media_uri=EXCLUDED.media_uri,
            media_mime=EXCLUDED.media_mime,payload=EXCLUDED.payload''',
            (tenant, cid, mid, seq, sender_id, identity, event, revoked, text, created, media_uri, media_mime, Jsonb(payload)))

    def import_page(self, tenant, body):
        if tenant not in self.tenants:
            raise ServiceError('Unknown tenant', 403)
        if not isinstance(body, dict) or set(body) - {'phase', 'rows', 'cursor', 'latest', 'has_more'}:
            raise ServiceError('Invalid import request')
        phase = body.get('phase')
        if phase not in ('conversation', 'message', 'changes'):
            raise ServiceError('Invalid import phase')
        rows = body.get('rows')
        cursor = integer(body.get('cursor'), 'cursor', 0)
        latest = integer(body.get('latest'), 'latest', 0)
        more = body.get('has_more')
        if not isinstance(rows, list) or len(rows) > 500 or not isinstance(more, bool):
            raise ServiceError('Invalid import page')
        if phase == 'changes' and cursor > latest:
            raise ServiceError('Invalid change cursor')
        positions = []
        for row in rows:
            if not isinstance(row, dict):
                raise ServiceError('Invalid import row')
            item = row.get('message') if phase == 'message' else row
            if not isinstance(item, dict):
                raise ServiceError('Invalid message snapshot')
            positions.append(integer(item.get('id'), 'row.id'))
        if any(position > cursor for position in positions) or any(
                right <= left for left, right in zip(positions, positions[1:])):
            raise ServiceError('Import rows do not match the ordered page cursor')
        for row, position in zip(rows, positions):
            self._import_atomic_page(tenant, phase, [row], position, latest, True)
        return self._import_atomic_page(tenant, phase, [], cursor, latest, more)

    def _import_atomic_page(self, tenant, phase, rows, cursor, latest, more):
        with self.store.tenant(tenant) as db:
            if db.execute('SELECT 1 FROM core_control WHERE tenant_id=%s AND active', (tenant,)).fetchone():
                raise ServiceError('Chat core owns this tenant', 409)
            state = db.execute('SELECT * FROM sync_state WHERE tenant_id=%s', (tenant,)).fetchone()
            ranks = {'conversation': 0, 'message': 1, 'changes': 2}
            if ranks[phase] < ranks[state['phase']]:
                return {'cursor': state['cursor'], 'phase': state['phase']}
            if phase != state['phase']:
                raise ServiceError('Import phase is out of order', 409)
            previous_cursor = state['cursor'] if phase == 'changes' else state['snapshot_cursor']
            if cursor < previous_cursor or (rows and cursor == previous_cursor):
                return {'cursor': state['cursor'], 'phase': state['phase']}
            if phase == 'changes' and (cursor > latest or latest < state['cursor']):
                raise ServiceError('Invalid change cursor')
            if phase != 'changes' and state['snapshot_latest'] is not None and latest != state['snapshot_latest']:
                raise ServiceError('Snapshot high watermark changed', 409)
            account = self._account(db, tenant) if rows else None
            last_change = previous_cursor
            for row in rows:
                if not isinstance(row, dict):
                    raise ServiceError('Invalid import row')
                if phase == 'conversation':
                    self._conversation(db, tenant, row, account)
                else:
                    if phase == 'changes':
                        change_id = integer(row.get('id'), 'change.id')
                        if change_id <= previous_cursor:
                            continue
                        if change_id <= last_change or change_id > cursor:
                            raise ServiceError('Changes are not ordered')
                        last_change = change_id
                        if db.execute('SELECT 1 FROM imported_changes WHERE tenant_id=%s AND change_id=%s', (tenant, change_id)).fetchone():
                            continue
                    conversation = self._conversation(db, tenant, row.get('conversation'), account)
                    if row.get('message') is not None:
                        self._message(db, tenant, conversation, row['message'], account)
                    if phase == 'changes':
                        db.execute('INSERT INTO imported_changes (tenant_id,change_id) VALUES (%s,%s)', (tenant, change_id))
            next_phase = phase if more or phase == 'changes' else ('message' if phase == 'conversation' else 'changes')
            if phase == 'changes':
                db.execute('UPDATE sync_state SET cursor=%s,updated_at=now() WHERE tenant_id=%s', (cursor, tenant))
            else:
                db.execute('''UPDATE sync_state SET phase=%s,snapshot_latest=%s,snapshot_cursor=%s,
                    cursor=%s,updated_at=now() WHERE tenant_id=%s''',
                    (next_phase, latest, cursor if more else 0, latest if next_phase == 'changes' else 0, tenant))
        return self.store.status(tenant)

    def _render(self, db, tenant, room, rows):
        result = []
        account = None
        for row in rows:
            payload = dict(row['payload'])
            if not row['revoked']:
                if account is None:
                    account = self._account(db, tenant)
                event = self.matrix.call('GET', '/_matrix/client/v3/rooms/' + quoted(room) + '/event/' + quoted(row['event_id']), account['access_token'])
                content = event.get('content', {})
                mirror = content.get('tt_payload', {})
                if mirror.get('id') != row['message_id'] or mirror.get('conversation_id') != row['conversation_id']:
                    raise ServiceError('Matrix event mapping mismatch', 503)
                if content.get('msgtype') != 'm.image':
                    payload['text'] = content.get('body', '')
            result.append(payload)
        return result

    @staticmethod
    def _edge(rows, limit):
        end = limit
        if len(rows) > limit and limit and rows[limit - 1]['payload'].get('album_id') is not None:
            album = rows[limit - 1]['payload']['album_id']
            while end < min(len(rows), limit + 9) and rows[end]['payload'].get('album_id') == album:
                end += 1
        return rows[:end], len(rows) > end

    def read(self, tenant, body):
        if tenant not in self.tenants:
            raise ServiceError('Unknown tenant', 403)
        if not isinstance(body, dict) or set(body) - {'operation', 'conversation_id', 'viewer_id', 'readonly', 'minimum_cursor', 'query'}:
            raise ServiceError('Invalid read request')
        cid = integer(body.get('conversation_id'), 'conversation_id')
        viewer = integer(body.get('viewer_id'), 'viewer_id')
        minimum = integer(body.get('minimum_cursor', 0), 'minimum_cursor', 0)
        readonly = body.get('readonly', False)
        if not isinstance(readonly, bool):
            raise ServiceError('readonly must be boolean')
        operation = body.get('operation')
        query = body.get('query', {})
        if not isinstance(query, dict):
            raise ServiceError('Invalid query')
        with self.store.connect() as db:
            state = db.execute('SELECT phase,cursor FROM sync_state WHERE tenant_id=%s', (tenant,)).fetchone()
            if not state or state['phase'] != 'changes' or state['cursor'] < minimum:
                raise ServiceError('Matrix mirror has not caught up', 503)
            room = db.execute('SELECT * FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
            if not room:
                raise ServiceError('Conversation not found', 404)
            if readonly and room['kind'] != 'order':
                raise ServiceError('Readonly grant is only valid for order chats', 403)
            if not readonly and not db.execute('''SELECT 1 FROM members WHERE tenant_id=%s
                AND conversation_id=%s AND local_user_id=%s AND active''', (tenant, cid, viewer)).fetchone():
                raise ServiceError('Conversation membership required', 403)
            args = [tenant, cid]
            where = 'tenant_id=%s AND conversation_id=%s'
            if operation == 'image':
                if set(query) - {'message_id'}:
                    raise ServiceError('Invalid image query')
                mid = integer(query.get('message_id'), 'message_id')
                row = db.execute('SELECT media_uri,media_mime FROM messages WHERE ' + where + ' AND message_id=%s AND NOT revoked', (*args, mid)).fetchone()
                if not row or not row['media_uri']:
                    raise ServiceError('Image not found', 404)
                account = self._account(db, tenant)
                raw, mime = self.matrix.download(account['access_token'], row['media_uri'])
                return {'mime': row['media_mime'] or mime, 'data': base64.b64encode(raw).decode()}
            if operation not in ('history', 'search'):
                raise ServiceError('Invalid operation')
            allowed = {'before', 'around', 'limit'} if operation == 'history' else {'q', 'before', 'sender_id', 'date_from', 'date_to', 'image_only', 'limit'}
            if set(query) - allowed:
                raise ServiceError('Invalid message query')
            limit = integer(query.get('limit', 60 if operation == 'history' else 30), 'limit', 1, 100)
            before = integer(query['before'], 'before') if 'before' in query else None
            if operation == 'search':
                text = query.get('q', '')
                if not isinstance(text, str) or len(text) > 100:
                    raise ServiceError('Invalid search text')
                image_only = query.get('image_only', False)
                if not isinstance(image_only, bool):
                    raise ServiceError('Invalid image_only flag')
                if not text and not image_only and not {'sender_id', 'date_from', 'date_to'} & query.keys():
                    raise ServiceError('Search requires a filter')
                where += ' AND NOT revoked'
                if text:
                    where += " AND position(lower(%s) in lower(text_body))>0"
                    args.append(text)
                if 'sender_id' in query:
                    where += ' AND sender_id=%s'
                    args.append(integer(query['sender_id'], 'sender_id'))
                for field, comparison in (('date_from', '>='), ('date_to', '<=')):
                    if field in query:
                        value = integer(query[field], field, 0, 253402300799)
                        where += ' AND created_at' + comparison + '%s'
                        args.append(value + (0.999999 if field == 'date_to' else 0))
                if 'date_from' in query and 'date_to' in query and int(query['date_from']) > int(query['date_to']):
                    raise ServiceError('Invalid date range')
                if image_only:
                    where += ' AND media_uri IS NOT NULL'
                count = db.execute('SELECT count(*) AS total FROM messages WHERE ' + where, args).fetchone()['total']
                if before is not None:
                    where += ' AND seq<%s'
                    args.append(before)
                rows = db.execute('SELECT * FROM messages WHERE ' + where + ' ORDER BY seq DESC LIMIT %s', (*args, limit + 1)).fetchall()
                return {'messages': self._render(db, tenant, room['room_id'], rows[:limit]), 'count': count, 'has_more': len(rows) > limit}
            if 'around' in query:
                if before is not None:
                    raise ServiceError('before and around cannot be combined')
                around = integer(query['around'], 'around')
                target = db.execute('SELECT * FROM messages WHERE ' + where + ' AND message_id=%s', (*args, around)).fetchone()
                if not target:
                    raise ServiceError('Message not found', 404)
                half = limit // 2
                old = db.execute('SELECT * FROM messages WHERE ' + where + ' AND seq<%s ORDER BY seq DESC LIMIT %s', (*args, target['seq'], half + 10)).fetchall()
                new = db.execute('SELECT * FROM messages WHERE ' + where + ' AND seq>%s ORDER BY seq LIMIT %s', (*args, target['seq'], half + 10)).fetchall()
                old, has_more = self._edge(old, half)
                new, has_newer = self._edge(new, half)
                rows = [*reversed(old), target, *new]
            else:
                if before is not None:
                    where += ' AND seq<%s'
                    args.append(before)
                rows = db.execute('SELECT * FROM messages WHERE ' + where + ' ORDER BY seq DESC LIMIT %s', (*args, limit + 10)).fetchall()
                rows, has_more = self._edge(rows, limit)
                has_newer = bool(rows and db.execute('''SELECT 1 FROM messages WHERE tenant_id=%s
                    AND conversation_id=%s AND seq>%s LIMIT 1''', (tenant, cid, rows[0]['seq'])).fetchone())
                rows.reverse()
            return {'messages': self._render(db, tenant, room['room_id'], rows), 'has_more': has_more, 'has_newer': has_newer}
