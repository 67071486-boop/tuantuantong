import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .config import ServiceError, integer
from .core_utils import ID_START, fields, text

ROSTER = 'chat.order_roster'
MESSAGE = 'chat.order_message'
ALBUM = 'chat.order_album'
PIN = 'chat.order_pin'
DETACHED = 'chat.order_detached'
EVENTS = {ROSTER, MESSAGE, ALBUM, PIN, DETACHED}
ROLE = {'publisher', 'acceptor', 'worker', 'follower', ''}


def wire_id(value):
    if not isinstance(value, str):
        raise ServiceError('Invalid peer chat identifier')
    try:
        normalized = str(uuid.UUID(value))
    except ValueError:
        raise ServiceError('Invalid peer chat identifier') from None
    if value != normalized:
        raise ServiceError('Invalid peer chat identifier')
    return value


def _member_key(value, prefix=None):
    if not isinstance(value, str) or not re.fullmatch(r'(?:a|[ab]:[1-9][0-9]{0,18})', value):
        raise ServiceError('Invalid peer chat member')
    if prefix is not None and value[0] != prefix:
        raise ServiceError('Peer chat sender belongs to the other studio')
    return value


def _peer_timestamp(value):
    try:
        parsed = datetime.fromisoformat(text(value, 100, 'peer time', blank=False))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ServiceError('Invalid peer chat timestamp') from None


class CorePeer:
    def _validate_peer_link(self, link):
        required = {'group_id', 'side', 'order_no', 'active', 'publisher_name', 'publisher_studio_name', 'acceptor_user_id'}
        fields(link, required | {'publisher_user_id', 'generation'}, required)
        wire_id(link['group_id'])
        if link['side'] not in ('upstream', 'downstream') or type(link['active']) is not bool:
            raise ServiceError('Invalid peer chat association')
        self._order_number(link['order_no'])
        text(link['publisher_name'], 150, 'publisher name', blank=False)
        text(link['publisher_studio_name'], 100, 'publisher studio')
        integer(link.get('generation', 0), 'peer binding generation', 0)
        for key in ('acceptor_user_id', 'publisher_user_id'):
            if link.get(key) is not None:
                integer(link[key], key, 1, ID_START - 1)

    def _peer_group(self, db, tenant, cid):
        return db.execute('SELECT * FROM core_peer_groups WHERE tenant_id=%s AND conversation_id=%s AND is_current',
                          (tenant, cid)).fetchone()

    @staticmethod
    def _wire_key(group, member):
        if member['local_user_id'] is None:
            data = member['metadata']
            return data.get('peer_key') if data.get('peer_group') == group['group_id'] else None
        if group['side'] == 'downstream':
            if member['local_user_id'] == group['publisher_id']:
                return 'a'
            prefix = 'a'
        else:
            prefix = 'b'
        return f'{prefix}:{member["local_user_id"]}'

    def _peer_members(self, db, tenant, cid):
        return db.execute('SELECT * FROM members WHERE tenant_id=%s AND conversation_id=%s ORDER BY member_id',
                          (tenant, cid)).fetchall()

    def _register_peer(self, db, tenant, grant):
        link = grant.get('peer_link')
        if link is None:
            return
        self._validate_peer_link(link)
        self._require_protocol(db, tenant, peer=True)
        cid = grant['conversation_id']
        prior = self._peer_group(db, tenant, cid)
        generation = link.get('generation', 0)
        if prior and prior['generation'] > generation:
            return
        if prior and prior['group_id'] != link['group_id']:
            if prior['side'] != 'downstream' or link['side'] != 'downstream' or generation <= prior['generation']:
                raise ServiceError('Invalid peer group successor', 409)
            db.execute('''UPDATE core_peer_groups SET active=false,is_current=false
                WHERE tenant_id=%s AND group_id=%s''', (tenant, prior['group_id']))
            self._retire_peer_members(db, tenant, prior)
            prior = None
        if prior and (prior['side'], prior['generation']) != (link['side'], generation):
            raise ServiceError('Peer group association has changed', 409)
        publisher = link.get('publisher_user_id', grant.get('publisher_id')) if link['side'] == 'downstream' else None
        if publisher is not None:
            publisher = integer(publisher, 'publisher', 1, ID_START - 1)
        current = db.execute('''INSERT INTO core_peer_groups(tenant_id,conversation_id,group_id,side,active,publisher_id,generation)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,group_id) DO UPDATE SET
            active=EXCLUDED.active AND NOT core_peer_groups.detached,
            publisher_id=coalesce(core_peer_groups.publisher_id,EXCLUDED.publisher_id)
            WHERE core_peer_groups.conversation_id=EXCLUDED.conversation_id
              AND core_peer_groups.side=EXCLUDED.side AND core_peer_groups.generation=EXCLUDED.generation
            RETURNING *''', (tenant, cid, link['group_id'], link['side'], link['active'], publisher, generation)).fetchone()
        if current is None:
            raise ServiceError('Peer group belongs to another order', 409)
        detached = current['side'] == 'upstream' and not current['active']
        required = {'order_no': link['order_no'], 'title': '订单-' + link['order_no'], 'peer_readonly': detached}
        if detached:
            required['can_send'] = False
        db.execute('UPDATE rooms SET metadata=metadata || %s WHERE tenant_id=%s AND conversation_id=%s',
                   (Jsonb(required), tenant, cid))
        if prior and prior['active'] and not current['active']:
            self._retire_peer_members(db, tenant, current)
        if not current['active']:
            return
        if current['side'] == 'upstream':
            self._remote_member(db, tenant, current, 'a', link['publisher_name'], link['publisher_studio_name'], 'publisher', None)
        if 'worker_id' in grant:
            self._retire_former_worker(db, tenant, cid, grant['worker_id'])
        self._queue_peer_roster(db, tenant, current)
        if not current['history_queued']:
            self._queue_peer_history(db, tenant, current)
            db.execute('UPDATE core_peer_groups SET history_queued=true WHERE tenant_id=%s AND group_id=%s',
                       (tenant, current['group_id']))

    def _retire_former_worker(self, db, tenant, cid, worker):
        if worker is not None:
            worker = integer(worker, 'current worker', 1, ID_START - 1)
        retired = db.execute('''UPDATE members SET active=false,metadata=metadata ||
            jsonb_build_object('left_at', %s::text, 'worker',false)
            WHERE tenant_id=%s AND conversation_id=%s AND active AND local_user_id IS NOT NULL
            AND local_user_id IS DISTINCT FROM %s AND metadata->>'order_role'='worker'
            AND NOT (metadata @> '{"manual_joined":true}' OR metadata @> '{"follower":true}'
                     OR metadata @> '{"acceptor":true}' OR metadata @> '{"publisher":true}')''',
                             (datetime.now(timezone.utc).isoformat(), tenant, cid, worker)).rowcount
        if retired:
            self._event(db, tenant, cid, 'conversation.members_changed')

    def _queue_peer(self, db, tenant, group, kind, payload):
        db.execute('''INSERT INTO core_peer_outbox(tenant_id,conversation_id,group_id,event_key,kind,payload)
            VALUES (%s,%s,%s,%s,%s,%s)''', (tenant, group['conversation_id'], group['group_id'],
                                         uuid.uuid4().hex, kind, Jsonb(payload)))

    def _queue_peer_roster(self, db, tenant, group):
        if not group or not group['active']:
            return
        cid = group['conversation_id']
        members = []
        for member in self._peer_members(db, tenant, cid):
            if member['local_user_id'] is None:
                continue
            key = self._wire_key(group, member)
            if key == 'a':
                continue
            data = member['metadata']
            members.append({'key': key, 'name': data['name'], 'studio_name': data.get('studio_name', ''),
                            'order_role': data.get('order_role', ''), 'left_at': data.get('left_at')})
        members.sort(key=lambda m: m['key'])
        if members == group['local_roster']:
            return
        group['roster_version'] += 1
        group['local_roster'] = members
        db.execute('''UPDATE core_peer_groups SET local_roster=%s,roster_version=%s
            WHERE tenant_id=%s AND group_id=%s''', (Jsonb(members), group['roster_version'], tenant, group['group_id']))
        self._queue_peer(db, tenant, group, ROSTER, {'version': group['roster_version'], 'members': members})

    def _queue_peer_history(self, db, tenant, group):
        cid = group['conversation_id']
        rows = db.execute('''SELECT message_id,payload->>'album_id' AS album_id FROM messages
            WHERE tenant_id=%s AND conversation_id=%s AND sender_identity LIKE 'local:%%' ORDER BY seq''',
                          (tenant, cid)).fetchall()
        messages = {}
        for row in rows:
            album = row['album_id']
            messages.setdefault(album or row['message_id'], []).append(row['message_id'])
        for identity, ids in messages.items():
            album = identity if isinstance(identity, str) else None
            self._queue_peer(db, tenant, group, ALBUM if album else MESSAGE,
                             {'ids': ids, 'album_id': album, 'history': True})
        room = db.execute('SELECT metadata FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
        if room['metadata'].get('pinned_message_id'):
            self._queue_peer_pin(db, tenant, cid, room['metadata']['pinned_message_id'])

    def _queue_peer_messages(self, db, tenant, cid, messages):
        group = self._peer_group(db, tenant, cid)
        if not group or not group['active']:
            return
        album = messages[0].get('album_id')
        self._queue_peer(db, tenant, group, ALBUM if album else MESSAGE,
                         {'ids': [m['id'] for m in messages], 'album_id': album, 'history': False})

    def _queue_peer_revoke(self, db, tenant, cid, row):
        group = self._peer_group(db, tenant, cid)
        if not group or not group['active']:
            return
        album = row['payload'].get('album_id')
        if album:
            ids = [item['message_id'] for item in db.execute('''SELECT message_id FROM messages WHERE tenant_id=%s
                AND conversation_id=%s AND payload->>'album_id'=%s ORDER BY seq''', (tenant, cid, album)).fetchall()]
        else:
            ids = [row['message_id']]
        self._queue_peer(db, tenant, group, ALBUM if album else MESSAGE, {'ids': ids, 'album_id': album, 'history': True})

    def _message_wire_id(self, db, tenant, group, mid):
        cid = group['conversation_id']
        mapped = db.execute('''SELECT wire_id FROM core_peer_messages WHERE tenant_id=%s AND group_id=%s AND message_id=%s''',
                            (tenant, group['group_id'], mid)).fetchone()
        if mapped:
            return mapped['wire_id']
        message = db.execute('''SELECT sender_identity FROM messages WHERE tenant_id=%s AND conversation_id=%s AND message_id=%s''',
                             (tenant, cid, mid)).fetchone()
        if not message or not message['sender_identity'].startswith('local:'):
            return None
        mapped = str(uuid.uuid5(uuid.UUID(group['group_id']), group['side'] + ':' + str(mid)))
        db.execute('''INSERT INTO core_peer_messages(tenant_id,conversation_id,group_id,message_id,wire_id)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id,group_id,message_id) DO NOTHING''',
                   (tenant, cid, group['group_id'], mid, mapped))
        return mapped

    def _row_by_wire(self, db, tenant, group, message_id):
        return db.execute('''SELECT m.* FROM core_peer_messages p JOIN messages m ON m.tenant_id=p.tenant_id
            AND m.conversation_id=p.conversation_id AND m.message_id=p.message_id
            WHERE p.tenant_id=%s AND p.group_id=%s AND p.wire_id=%s''', (tenant, group['group_id'], message_id)).fetchone()

    def _wire_image(self, db, tenant, row):
        if not row['payload'].get('image') or row['revoked']:
            return None
        queued = db.execute('''SELECT payload->'image' AS image FROM core_outbox WHERE tenant_id=%s AND message_id=%s
            AND action='send' ''', (tenant, row['message_id'])).fetchone()
        if queued and queued['image']:
            return {key: queued['image'][key] for key in ('mime', 'data')}
        if not row['media_uri']:
            raise ServiceError('Peer chat image is not ready', 503)
        account = self.engine._account(db, tenant)
        raw, mime = self.engine.matrix.download(account['access_token'], row['media_uri'])
        return {'mime': row['media_mime'] or mime, 'data': base64.b64encode(raw).decode()}

    def _wire_message(self, db, tenant, group, mid, members, history):
        cid = group['conversation_id']
        row = db.execute('SELECT * FROM messages WHERE tenant_id=%s AND conversation_id=%s AND message_id=%s',
                         (tenant, cid, mid)).fetchone()
        if row is None or not row['sender_identity'].startswith('local:'):
            raise ServiceError('Peer chat sender is not local', 503)
        member = members.get(row['sender_id'])
        if member is None or member['local_user_id'] is None:
            raise ServiceError('Peer chat sender is missing', 503)
        message, data = row['payload'], member['metadata']
        reply = message.get('reply_to_id')
        created = datetime.fromtimestamp(row['created_at'], tz=timezone.utc).isoformat()
        result = {'message_id': self._message_wire_id(db, tenant, group, mid),
                  'sender': self._wire_key(group, member), 'sender_name': data['name'],
                  'sender_studio_name': data.get('studio_name', ''), 'sender_order_role': data.get('order_role', ''),
                  'sender_left_at': data.get('left_at'), 'text': '' if row['revoked'] else row['text_body'],
                  'created_at': created, 'revoked': row['revoked'], 'history': history,
                  'reply_to': self._message_wire_id(db, tenant, group, reply) if reply is not None else None}
        image = self._wire_image(db, tenant, row)
        if image is not None:
            result['image'] = image
        if not row['revoked']:
            mentions = [self._wire_key(group, members[mid]) for mid in message.get('mention_member_ids', []) if mid in members]
            if any(mentions):
                result['mention_keys'] = [key for key in mentions if key]
        return result

    def _peer_payload(self, tenant, event):
        with self.store.connect() as db:
            row = db.execute('SELECT wire_payload FROM core_peer_outbox WHERE tenant_id=%s AND event_id=%s',
                             (tenant, event['event_id'])).fetchone()
            group = self._peer_group(db, tenant, event['conversation_id'])
            if not group or not group['active'] or group['group_id'] != event['group_id']:
                return None
            if row['wire_payload'] is not None:
                return row['wire_payload']
            data = event['payload']
            if event['kind'] in (MESSAGE, ALBUM):
                members = {m['member_id']: m for m in self._peer_members(db, tenant, event['conversation_id'])}
                parts = [self._wire_message(db, tenant, group, mid, members, data['history']) for mid in data['ids']]
                if event['kind'] == MESSAGE:
                    result = parts[0]
                else:
                    result = {'album_id': data['album_id'], 'messages': parts, 'history': data['history']}
            else:
                result = data
            # A response can be lost after the business inbox has accepted this key.
            db.execute('UPDATE core_peer_outbox SET wire_payload=%s WHERE tenant_id=%s AND event_id=%s',
                       (Jsonb(result), tenant, event['event_id']))
            return result

    def _remote_member(self, db, tenant, group, key, name, studio, role, left):
        cid, wire_group = group['conversation_id'], group['group_id']
        prior = self._peer_member(db, tenant, group, key)
        mid = prior['member_id'] if prior else self._allocate(db, tenant)
        data = {'id': mid, 'name': name, 'studio_name': studio, 'order_role': role, 'left_at': left,
                'peer_key': key, 'peer_group': wire_group}
        if prior and prior['metadata'] == data:
            return mid
        db.execute('''INSERT INTO members(tenant_id,conversation_id,member_id,local_user_id,active,metadata)
            VALUES (%s,%s,%s,NULL,%s,%s) ON CONFLICT (tenant_id,conversation_id,member_id) DO UPDATE SET
            active=EXCLUDED.active,metadata=EXCLUDED.metadata''', (tenant, cid, mid, left is None, Jsonb(data)))
        self._event(db, tenant, cid, 'conversation.members_changed')
        return mid

    def _peer_member(self, db, tenant, group, key):
        return db.execute('''SELECT * FROM members WHERE tenant_id=%s AND conversation_id=%s AND local_user_id IS NULL
            AND metadata->>'peer_group'=%s AND metadata->>'peer_key'=%s''',
                          (tenant, group['conversation_id'], group['group_id'], key)).fetchone()

    def _retire_peer_members(self, db, tenant, group):
        cid = group['conversation_id']
        if db.execute('''UPDATE members SET active=false,metadata=jsonb_set(metadata,'{left_at}',to_jsonb(%s::text))
            WHERE tenant_id=%s AND conversation_id=%s AND local_user_id IS NULL AND active AND metadata->>'peer_group'=%s''',
                      (datetime.now(timezone.utc).isoformat(), tenant, cid, group['group_id'])).rowcount:
            self._event(db, tenant, cid, 'conversation.members_changed')

    def _incoming_roster(self, db, tenant, group, data):
        fields(data, {'version', 'members'}, {'version', 'members'})
        version = integer(data['version'], 'peer roster version')
        members = data['members']
        if not isinstance(members, list) or len(members) > 1000:
            raise ServiceError('Invalid peer chat roster')
        if version <= group['remote_version']:
            return
        prefix = 'a' if group['side'] == 'upstream' else 'b'
        keys = set()
        for member in members:
            fields(member, {'key', 'name', 'studio_name', 'order_role', 'left_at'}, {'key', 'name', 'left_at'})
            key = _member_key(member['key'], prefix)
            if key in keys or key == 'a':
                raise ServiceError('Invalid peer chat roster key')
            keys.add(key)
            role = member.get('order_role', '')
            if role not in ROLE:
                raise ServiceError('Invalid peer chat member role')
            name = text(member['name'], 150, 'peer name', blank=False)
            studio = text(member.get('studio_name', ''), 100, 'peer studio')
            left = _peer_timestamp(member['left_at']).isoformat() if member['left_at'] is not None else None
            self._remote_member(db, tenant, group, key, name, studio, role, left)
        db.execute('UPDATE core_peer_groups SET remote_version=%s WHERE tenant_id=%s AND group_id=%s',
                   (version, tenant, group['group_id']))

    def _incoming_message(self, db, tenant, group, data, album, notify):
        fields(data, {'message_id', 'sender', 'sender_name', 'sender_studio_name', 'sender_order_role',
                      'sender_left_at', 'text', 'created_at', 'revoked', 'reply_to', 'history', 'roster',
                      'image', 'mention_keys'},
               {'message_id', 'sender', 'sender_name', 'sender_left_at', 'text', 'created_at', 'revoked', 'reply_to'})
        cid = group['conversation_id']
        prefix = 'a' if group['side'] == 'upstream' else 'b'
        author = _member_key(data['sender'], prefix)
        name = text(data['sender_name'], 150, 'peer sender', blank=False)
        studio = text(data.get('sender_studio_name', ''), 100, 'peer studio')
        role = data.get('sender_order_role', '')
        if role not in ROLE or type(data['revoked']) is not bool:
            raise ServiceError('Invalid peer chat message')
        left = _peer_timestamp(data['sender_left_at']).isoformat() if data['sender_left_at'] is not None else None
        remote = self._peer_member(db, tenant, group, author)
        sender = remote['member_id'] if remote else self._remote_member(db, tenant, group, author, name, studio, role, left)
        message_id = wire_id(data['message_id'])
        existing = self._row_by_wire(db, tenant, group, message_id)
        identity = 'peer:' + group['group_id'] + ':' + author
        if existing:
            if existing['sender_identity'] != identity or existing['payload'].get('album_id') != album:
                raise ServiceError('Peer chat message identity has changed')
            if data['revoked'] and not existing['revoked']:
                self._revoke_message(db, tenant, cid, existing)
            return
        reply = None
        if data['reply_to'] is not None:
            reply = self._row_by_wire(db, tenant, group, wire_id(data['reply_to']))
            if reply is None:
                raise ServiceError('Peer chat reply is waiting for its parent', 503)
        created = _peer_timestamp(data['created_at'])
        content = text(data['text'], 4000, 'peer message')
        if data['revoked']:
            content, image = '', None
        else:
            image = self._image_input(data['image']) if 'image' in data else None
        seq = db.execute('''SELECT coalesce(max(seq),0)+1 AS seq FROM messages
            WHERE tenant_id=%s AND conversation_id=%s''', (tenant, cid)).fetchone()['seq']
        mid = self._allocate(db, tenant)
        payload = {'id': mid, 'seq': seq, 'conversation_id': cid, 'sender_id': sender,
                   'text': content, 'created_at': created.isoformat(), 'revoked': data['revoked'],
                   'reply_to_id': reply['message_id'] if reply else None, 'delivery_state': 'queued'}
        if album is not None:
            payload['album_id'] = album
        if image is not None:
            payload['image'] = {key: image[key] for key in ('mime', 'width', 'height')}
        mentions = data.get('mention_keys', [])
        if not isinstance(mentions, list) or len(mentions) > 50:
            raise ServiceError('Invalid peer chat mentions')
        keys = {_member_key(key) for key in mentions}
        if keys and not data['revoked']:
            payload['mention_member_ids'] = [m['member_id'] for m in self._peer_members(db, tenant, cid)
                                            if m['member_id'] != sender and self._wire_key(group, m) in keys]
        db.execute('''INSERT INTO messages(tenant_id,conversation_id,message_id,seq,sender_id,sender_identity,
            revoked,text_body,created_at,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                   (tenant, cid, mid, seq, sender, identity, data['revoked'], content, created.timestamp(), Jsonb(payload)))
        db.execute('''INSERT INTO core_peer_messages(tenant_id,conversation_id,group_id,message_id,wire_id)
            VALUES (%s,%s,%s,%s,%s)''', (tenant, cid, group['group_id'], mid, message_id))
        db.execute('INSERT INTO core_outbox(tenant_id,message_id,action,payload) VALUES (%s,%s,%s,%s)',
                   (tenant, mid, 'send', Jsonb({'message': payload, 'image': image})))
        self._event(db, tenant, cid, 'message.created', mid, notify_message=notify and not data['revoked'])
        if notify and not data['revoked']:
            db.execute('UPDATE core_archives SET activity_at=clock_timestamp() WHERE tenant_id=%s AND conversation_id=%s',
                       (tenant, cid))
        if group['is_current'] and group['pinned_wire_id'] == message_id and not data['revoked']:
            self._update_peer_pin(db, tenant, cid, mid)

    def _update_peer_pin(self, db, tenant, cid, mid):
        db.execute('''UPDATE rooms SET metadata=metadata || %s WHERE tenant_id=%s AND conversation_id=%s''',
                   (Jsonb({'pinned_message_id': mid}), tenant, cid))
        self._event(db, tenant, cid, 'conversation.pin_changed')

    def _queue_peer_pin(self, db, tenant, cid, mid):
        group = self._peer_group(db, tenant, cid)
        if not group or not group['active']:
            return
        revision = integer(group['pin_revision'] + 1, 'pin revision', 1, 2**63 - 2)
        operation = str(uuid.uuid4())
        message_id = self._message_wire_id(db, tenant, group, mid) if mid is not None else None
        if mid is not None and message_id is None:
            return
        db.execute('''UPDATE core_peer_groups SET pin_revision=%s,pin_operation=%s,pinned_wire_id=%s
            WHERE tenant_id=%s AND group_id=%s''', (revision, operation, message_id, tenant, group['group_id']))
        self._queue_peer(db, tenant, group, PIN, {'revision': revision, 'operation_id': operation, 'message_id': message_id})

    def _incoming_pin(self, db, tenant, group, data):
        fields(data, {'revision', 'operation_id', 'message_id'}, {'revision', 'operation_id', 'message_id'})
        revision = integer(data['revision'], 'pin revision', 1, 2**63 - 2)
        operation = wire_id(data['operation_id'])
        if (revision, operation) <= (group['pin_revision'], group['pin_operation'] or ''):
            return
        message_id = wire_id(data['message_id']) if data['message_id'] is not None else None
        cid = group['conversation_id']
        target = self._row_by_wire(db, tenant, group, message_id) if message_id else None
        db.execute('''UPDATE core_peer_groups SET pin_revision=%s,pin_operation=%s,pinned_wire_id=%s
            WHERE tenant_id=%s AND group_id=%s''', (revision, operation, message_id, tenant, group['group_id']))
        self._update_peer_pin(db, tenant, cid, target['message_id'] if target and not target['revoked'] else None)

    def _apply_peer_event(self, tenant, event):
        fields(event, {'id', 'order_no', 'group_id', 'kind', 'payload'}, {'id', 'order_no', 'group_id', 'kind', 'payload'})
        event_id = integer(event['id'], 'peer event')
        wire = wire_id(event['group_id'])
        number = self._order_number(event['order_no'])
        kind, data = event['kind'], event['payload']
        if not isinstance(kind, str) or kind not in EVENTS or not isinstance(data, dict):
            raise ServiceError('Invalid peer chat event')
        with self.store.tenant(tenant) as db:
            self._require_protocol(db, tenant, peer=True)
            group = db.execute('''SELECT p.* FROM core_peer_groups p JOIN core_order_rooms o ON o.tenant_id=p.tenant_id
                AND o.conversation_id=p.conversation_id WHERE p.tenant_id=%s AND p.group_id=%s AND o.order_no=%s''',
                               (tenant, wire, number)).fetchone()
            if group is None:
                raise ServiceError('Peer chat is waiting for the linked order', 503)
            digest = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            claimed = db.execute('''INSERT INTO core_peer_imports(tenant_id,event_id,receipt) VALUES (%s,%s,%s)
                ON CONFLICT DO NOTHING''', (tenant, event_id, digest)).rowcount
            if not claimed:
                observed = db.execute('SELECT receipt FROM core_peer_imports WHERE tenant_id=%s AND event_id=%s',
                                      (tenant, event_id)).fetchone()
                if observed['receipt'] != digest:
                    raise ServiceError('Peer chat receipt belongs to a different group')
                return
            cid = group['conversation_id']
            if kind == DETACHED:
                if group['side'] != 'upstream' or data:
                    raise ServiceError('Peer chat detachment came from an invalid side')
                db.execute('''UPDATE core_peer_groups SET detached=true,active=false
                    WHERE tenant_id=%s AND group_id=%s''', (tenant, group['group_id']))
                db.execute('''UPDATE rooms SET metadata=metadata || '{"can_send":false,"peer_readonly":true}'::jsonb
                    WHERE tenant_id=%s AND conversation_id=%s''', (tenant, cid))
                self._retire_peer_members(db, tenant, group)
                return
            roster = data if kind == ROSTER else data.get('roster')
            if roster is not None:
                self._incoming_roster(db, tenant, group, roster)
            if kind == PIN and group['is_current']:
                self._incoming_pin(db, tenant, group, data)
            if kind in (MESSAGE, ALBUM):
                history = data.get('history', False)
                if type(history) is not bool:
                    raise ServiceError('Invalid peer chat history flag')
                album, messages = None, [data]
                if kind == ALBUM:
                    fields(data, {'album_id', 'messages', 'history', 'roster'}, {'album_id', 'messages'})
                    album, messages = wire_id(data['album_id']), data['messages']
                    if not isinstance(messages, list) or not 2 <= len(messages) <= 10:
                        raise ServiceError('Invalid peer chat album')
                for index, message in enumerate(messages):
                    self._incoming_message(db, tenant, group, message, album, notify=not history and index == 0)
            if not group['active']:
                self._retire_peer_members(db, tenant, group)

    def _relay_peer(self, tenant):
        if tenant not in self._peer_enabled:
            return 0, False
        with self.store.connect() as db:
            events = db.execute('''SELECT q.* FROM core_peer_outbox q
                WHERE q.tenant_id=%s AND q.available_at<=now() AND NOT EXISTS
                (SELECT 1 FROM core_peer_outbox prior WHERE prior.tenant_id=q.tenant_id
                    AND prior.conversation_id=q.conversation_id AND prior.event_id<q.event_id AND prior.available_at>now())
                ORDER BY q.event_id LIMIT 20''', (tenant,)).fetchall()
        sent, deferred = 0, set()
        for event in events:
            if event['conversation_id'] in deferred:
                continue
            try:
                data = self._peer_payload(tenant, event)
                if data is not None:
                    response = self._source_request(tenant, 'peer-chat', {'operation': 'push', 'group_id': event['group_id'],
                        'kind': event['kind'], 'event_key': event['event_key'], 'payload': data})
                    if type(response.get('queued')) is not bool:
                        raise ServiceError('Peer chat request was not confirmed', 503)
            except ServiceError as error:
                self._retry(tenant, 'core_peer_outbox', 'event_id', event, error)
                deferred.add(event['conversation_id'])
                continue
            with self.store.connect() as db:
                db.execute('DELETE FROM core_peer_outbox WHERE tenant_id=%s AND event_id=%s', (tenant, event['event_id']))
            sent += 1
        pulled = self._source_request(tenant, 'peer-chat', {'operation': 'pull', 'limit': 20}).get('events')
        if not isinstance(pulled, list) or len(pulled) > 20:
            raise ServiceError('Invalid peer chat inbox', 503)
        ack, defer, blocked, last = [], [], set(), 0
        for event in pulled:
            if not isinstance(event, dict):
                raise ServiceError('Invalid peer chat inbox event', 503)
            event_id = integer(event.get('id'), 'peer event')
            if event_id <= last:
                raise ServiceError('Peer chat inbox is not in order', 503)
            last = event_id
            group_id = wire_id(event.get('group_id'))
            if group_id in blocked:
                continue
            try:
                self._apply_peer_event(tenant, event)
            except ServiceError:
                blocked.add(group_id)
                defer.append(event_id)
                continue
            ack.append(event_id)
        if defer:
            result = self._source_request(tenant, 'peer-chat', {'operation': 'defer', 'events': defer})
            integer(result.get('deferred'), 'peer chat deferral', 0, len(defer))
        acknowledged = 0
        if ack:
            result = self._source_request(tenant, 'peer-chat', {'operation': 'ack', 'events': ack})
            acknowledged = integer(result.get('acked'), 'peer chat acknowledgment', 0, len(ack))
        return sent + acknowledged, len(pulled) == 20 or bool(blocked) or acknowledged < len(ack)
