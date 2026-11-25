import base64
import copy
import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb

from .matrix import Matrix, PilotError, quoted
from .validation import image_data, integer


class Pilot:
    def __init__(self, config):
        self.config = config
        self.users = {str(u['id']): u for u in config['users']}
        self.matrix = Matrix(config['matrix_url'])

    @contextmanager
    def state(self):
        with psycopg.connect(self.config['postgres_dsn']) as connection:
            row = connection.execute('SELECT value FROM chat_pilot.pilot_state WHERE id=1 FOR UPDATE').fetchone()
            if row is None:
                raise PilotError('请先初始化测试样板', 503)
            state = row[0]
            yield state
            connection.execute('UPDATE chat_pilot.pilot_state SET value=%s WHERE id=1', (Jsonb(state),))

    def bootstrap(self):
        with psycopg.connect(self.config['postgres_dsn']) as connection:
            permission = connection.execute(
                "SELECT has_schema_privilege(current_user, oid, 'USAGE'), has_schema_privilege(current_user, oid, 'CREATE') "
                "FROM pg_namespace WHERE nspname='chat_pilot'").fetchone()
            if permission is None or not all(permission):
                raise PilotError('请先供给 chat_pilot schema 及适配角色权限', 503)
            connection.execute('CREATE TABLE IF NOT EXISTS chat_pilot.pilot_state (id integer PRIMARY KEY CHECK (id=1), value jsonb NOT NULL)')
            connection.execute('INSERT INTO chat_pilot.pilot_state VALUES (1,%s) ON CONFLICT DO NOTHING',
                               (Jsonb({'accounts': {}, 'rooms': {}, 'messages': {}, 'changes': [], 'cursor': 0, 'next_room': 1, 'next_message': 1}),))
        with self.state() as state:
            passwords = state.setdefault('matrix_passwords', {})
            for uid in self.users:
                passwords.setdefault(uid, secrets.token_urlsafe(48))
        with self.state() as state:
            for uid, user in self.users.items():
                if uid not in state['accounts']:
                    internal_user = {**user, 'password': state['matrix_passwords'][uid]}
                    mxid, token = self.matrix.provision(internal_user, self.config['registration_shared_secret'])
                    state['accounts'][uid] = {'mxid': mxid, 'token': token}
            for group in self.config.get('order_groups', []):
                members = list(map(str, group['member_ids']))
                if not members or any(self.users[u]['studio'] != group['studio'] for u in members):
                    raise PilotError('测试群成员配置错误')
                if not any(r.get('key') == group['key'] for r in state['rooms'].values()):
                    self.create_room(state, members[0], members, group)

    def token(self, state, uid):
        return state['accounts'][str(uid)]['token']

    def change(self, state, kind, rid, message=None):
        state['cursor'] += 1
        state['changes'].append({'cursor': state['cursor'], 'kind': kind,
                                 'conversation_id': int(rid), 'message': message})

    def create_room(self, state, uid, members, group=None):
        account = state['accounts']
        members = sorted(members, key=int)
        owner = members[0]
        identities = sorted(account[m]['mxid'] for m in members)
        binding_input = json.dumps([self.config['server_name'], self.users[uid]['studio'],
                                    group['key'] if group else 'direct', identities], ensure_ascii=True)
        binding = hmac.new(self.config['registration_shared_secret'].encode(),
                           binding_input.encode(), hashlib.sha256).hexdigest()
        alias = 'tt_pilot_' + binding
        marker = {'binding': binding, 'members': identities, 'owner': account[owner]['mxid']}
        body = {'preset': 'private_chat', 'visibility': 'private', 'room_alias_name': alias,
                'invite': [account[m]['mxid'] for m in members if m != owner],
                'initial_state': [
                    {'type': 'm.room.history_visibility', 'state_key': '', 'content': {'history_visibility': 'joined'}},
                    {'type': 'org.tuantuantong.pilot', 'state_key': '', 'content': marker}]}
        if group:
            body['name'] = group['title']
        token = self.token(state, owner)
        try:
            mxroom = self.matrix.room(token, 'createRoom', 'POST', body)['room_id']
        except PilotError as error:
            if error.matrix_code != 'M_ROOM_IN_USE':
                raise
            full_alias = '#' + alias + ':' + self.config['server_name']
            mxroom = self.matrix.room(token, 'directory/room/' + quoted(full_alias))['room_id']
            prefix = 'rooms/' + quoted(mxroom)
            actual = self.matrix.room(token, prefix + '/state/org.tuantuantong.pilot/')
            rules = self.matrix.room(token, prefix + '/state/m.room.join_rules/')
            joined = self.matrix.room(token, prefix + '/joined_members').get('joined', {})
            if actual != marker or rules.get('join_rule') != 'invite' or account[owner]['mxid'] not in joined or set(joined) - set(identities):
                raise PilotError('既有房间归属校验失败', 502)
        for member in members:
            if member != owner:
                self.matrix.room(self.token(state, member), 'join/' + quoted(mxroom), 'POST', {})
        rid = str(state['next_room'])
        state['next_room'] += 1
        state['rooms'][rid] = {'matrix_id': mxroom, 'studio': self.users[uid]['studio'],
            'members': members, 'kind': 'order' if group else 'direct', 'owner': owner,
            'read': {}, 'preferences': {}, 'pin': None, **(group or {})}
        self.change(state, 'conversation.created', rid)
        return rid

    def room_for(self, state, uid, rid):
        room = state['rooms'].get(str(rid))
        if room is None or uid not in room['members'] or room['studio'] != self.users[uid]['studio']:
            raise PilotError('会话不存在或无权访问', 404)
        return room

    def contact(self, uid):
        return {'id': int(uid), 'name': self.users[uid]['name'], 'avatar_url': None}

    def refresh(self, state, uid):
        for rid in list(state['rooms']):
            room = state['rooms'][rid]
            if uid not in room['members'] or room['studio'] != self.users[uid]['studio']:
                continue
            events, token, seen = [], None, set()
            while True:
                params = {'dir': 'b', 'limit': 100}
                if token:
                    params['from'] = token
                page = self.matrix.room(self.token(state, uid), 'rooms/' + quoted(room['matrix_id']) + '/messages', params=params)
                events.extend(page.get('chunk', []))
                next_token = page.get('end')
                if not next_token or not page.get('chunk') or next_token in seen:
                    break
                seen.add(next_token)
                token = next_token
            mxusers = {v['mxid']: key for key, v in state['accounts'].items()}
            known = {m['event_id'] for m in state['messages'].values()}
            for event in reversed(events):
                if event.get('type') != 'm.room.message' or event['event_id'] in known:
                    continue
                content = event.get('content', {})
                if content.get('msgtype') not in ('m.text', 'm.image') and not event.get('unsigned', {}).get('redacted_because'):
                    continue
                sender = mxusers.get(event['sender'])
                if sender not in room['members']:
                    continue
                mid = state['next_message']
                state['next_message'] += 1
                reply_event = content.get('m.relates_to', {}).get('m.in_reply_to', {}).get('event_id')
                reply = next((int(k) for k, m in state['messages'].items() if m['event_id'] == reply_event), None)
                message = {'id': mid, 'seq': mid, 'conversation_id': int(rid), 'sender_id': int(sender),
                    'text': content.get('body', '') if content.get('msgtype') == 'm.text' else content.get('tt_text', ''),
                    'created_at': datetime.fromtimestamp(event.get('origin_server_ts', 0) / 1000, timezone.utc).isoformat(),
                    'revoked': False, 'reply_to_id': reply, 'event_id': event['event_id'],
                    'album_id': content.get('tt_album_id'), 'mention_member_ids': content.get('tt_mentions', [])}
                if content.get('msgtype') == 'm.image':
                    info = content.get('info', {})
                    message['image'] = {'mime': info.get('mimetype', 'image/png'), 'width': info.get('w', 1), 'height': info.get('h', 1)}
                    message['mxc'] = content.get('url')
                state['messages'][str(mid)] = message
                known.add(event['event_id'])
                self.change(state, 'message.created', rid, self.public_message(message))
            redacted = {e.get('redacts', e.get('content', {}).get('redacts')) for e in events if e.get('type') == 'm.room.redaction'}
            redacted.update(e['event_id'] for e in events if e.get('unsigned', {}).get('redacted_because'))
            for message in self.messages(state, rid):
                if message['event_id'] in redacted and not message['revoked']:
                    self.redact_projection(state, rid, message)

    @staticmethod
    def public_message(message):
        return {k: copy.deepcopy(v) for k, v in message.items() if k not in ('event_id', 'mxc')}

    @staticmethod
    def messages(state, rid):
        return sorted((m for m in state['messages'].values() if m['conversation_id'] == int(rid)), key=lambda m: m['seq'])

    def summary(self, state, uid, rid):
        room = self.room_for(state, uid, rid)
        messages = self.messages(state, rid)
        read = room['read'].get(uid, 0)
        unread = [m for m in messages if m['seq'] > read and m['sender_id'] != int(uid) and not m['revoked']]
        result = {'id': int(rid), 'kind': room['kind'], 'last_message': self.public_message(messages[-1]) if messages else None,
            'unread_count': len(unread), 'read_seq': read,
            'peer_read_seq': max((v for u, v in room['read'].items() if u != uid), default=0),
            'first_unread_message_id': unread[0]['id'] if unread else None,
            'unread_mentions_count': 0, **room['preferences'].get(uid, {})}
        if room['kind'] == 'direct':
            result['peer'] = self.contact(next(u for u in room['members'] if u != uid))
        else:
            result.update(title=room['title'], order_no=room['order_no'], local_order_no=room['order_no'],
                members=[{**self.contact(u), 'left_at': None, 'studio_name': room['studio'], 'order_role': 'worker'} for u in room['members']],
                self_member_id=int(uid), is_member=True, can_join=False,
                pinned_message_id=room['pin'], can_pin_messages=room['owner'] == uid)
        return result

    def dispatch(self, uid, method, path, query, body):
        uid = str(uid)
        if path == '/api/auth/me' and method == 'GET':
            return {'user': {'id': int(uid), 'display_name': self.users[uid]['name']}}
        if path == '/api/internal-chat/contacts/' and method == 'GET':
            q = query.get('q', '').casefold()
            return {'contacts': [self.contact(k) for k, u in self.users.items() if k != uid and u['studio'] == self.users[uid]['studio'] and q in u['name'].casefold()]}
        with self.state() as state:
            self.refresh(state, uid)
            if path == '/api/internal-chat/conversations/':
                if method == 'GET':
                    return {'conversations': [self.summary(state, uid, rid) for rid, r in state['rooms'].items() if uid in r['members'] and r['studio'] == self.users[uid]['studio']], 'cursor': state['cursor']}
                peer = str(integer(body.get('peer_id')))
                if peer == uid or peer not in self.users or self.users[peer]['studio'] != self.users[uid]['studio']:
                    raise PilotError('联系人不存在', 404)
                rid = next((rid for rid, r in state['rooms'].items() if r['kind'] == 'direct' and set(r['members']) == {uid, peer}), None)
                rid = rid or self.create_room(state, uid, [uid, peer])
                return self.summary(state, uid, rid)
            if path == '/api/internal-chat/sync/' and method == 'GET':
                after = integer(query.get('after', 0), 0)
                changes = [{k: v for k, v in c.items() if k != 'cursor'} for c in state['changes'] if c['cursor'] > after and uid in state['rooms'][str(c['conversation_id'])]['members'] and state['rooms'][str(c['conversation_id'])]['studio'] == self.users[uid]['studio']]
                return {'cursor': state['cursor'], 'has_more': False, 'changes': changes}
            parts = path.strip('/').split('/')
            if len(parts) < 4 or parts[:3] != ['api', 'internal-chat', 'conversations']:
                raise PilotError('接口不存在', 404)
            rid = str(integer(parts[3]))
            room = self.room_for(state, uid, rid)
            action = '/'.join(parts[4:])
            if action == 'messages' and method == 'GET':
                messages = self.messages(state, rid)
                limit = integer(query.get('limit', 50), 1, 100)
                before = integer(query['before']) if 'before' in query else None
                after = integer(query['after'], 0) if 'after' in query else None
                around = integer(query['around']) if 'around' in query else None
                filtered = [m for m in messages if (before is None or m['seq'] < before) and (after is None or m['seq'] > after)]
                if around is not None:
                    index = next((i for i, m in enumerate(filtered) if m['id'] == around), None)
                    if index is None:
                        raise PilotError('消息不存在', 404)
                    start = max(0, index - limit // 2)
                    page = filtered[start:start + limit]
                else:
                    page = filtered[:limit] if after is not None else filtered[-limit:]
                return {'messages': [self.public_message(m) for m in page],
                    'has_more': bool(page and any(m['seq'] < page[0]['seq'] for m in messages)),
                    'has_newer': bool(page and any(m['seq'] > page[-1]['seq'] for m in messages))}
            if action == 'messages' and method == 'POST':
                return self.send(state, uid, rid, room, body)
            if action == 'messages/search' and method == 'GET':
                text = query.get('q', '').casefold()
                before = integer(query['before']) if 'before' in query else None
                sender = integer(query['sender_id']) if 'sender_id' in query else None
                date_from = integer(query['date_from'], 0) if 'date_from' in query else None
                date_to = integer(query['date_to'], 0) if 'date_to' in query else None
                found = []
                for m in reversed(self.messages(state, rid)):
                    timestamp = int(datetime.fromisoformat(m['created_at']).timestamp())
                    if m['revoked'] or text not in m['text'].casefold() or (before and m['id'] >= before) or (sender and m['sender_id'] != sender):
                        continue
                    if (date_from is not None and timestamp < date_from) or (date_to is not None and timestamp > date_to) or (query.get('image_only') == '1' and 'image' not in m):
                        continue
                    found.append(self.public_message(m))
                return {'messages': found[:50], 'count': len(found), 'has_more': len(found) > 50}
            if action == 'read' and method == 'POST':
                seq = integer(body.get('seq'), 0)
                seq = max((m['seq'] for m in self.messages(state, rid) if m['seq'] <= seq), default=0)
                room['read'][uid] = max(room['read'].get(uid, 0), seq)
                room['preferences'].setdefault(uid, {})['marked_unread'] = False
                if seq:
                    message = next(m for m in self.messages(state, rid) if m['seq'] == seq)
                    self.matrix.room(self.token(state, uid), 'rooms/' + quoted(room['matrix_id']) + '/receipt/m.read/' + quoted(message['event_id']), 'POST', {})
                self.change(state, 'conversation.read', rid)
                return {'read_seq': room['read'][uid]}
            if action == 'preferences' and method == 'POST':
                if set(body) - {'pinned', 'remark', 'marked_unread'} or any(not isinstance(v, bool) for k, v in body.items() if k != 'remark') or ('remark' in body and (not isinstance(body['remark'], str) or len(body['remark']) > 100)):
                    raise PilotError('会话设置格式错误')
                prefs = room['preferences'].setdefault(uid, {})
                prefs.update(body)
                self.change(state, 'conversation.preferences_changed', rid)
                return prefs
            if action == 'pin' and method == 'POST':
                if room['kind'] != 'order' or room['owner'] != uid:
                    raise PilotError('无权置顶消息', 403)
                mid = body.get('message_id')
                message = self.message_for(state, rid, integer(mid)) if mid is not None else None
                self.matrix.room(self.token(state, uid), 'rooms/' + quoted(room['matrix_id']) + '/state/m.room.pinned_events/', 'PUT', {'pinned': [message['event_id']] if message else []})
                room['pin'] = int(mid) if message else None
                self.change(state, 'conversation.pin_changed', rid)
                return {'pinned_message_id': room['pin']}
            if len(parts) == 7 and parts[4] == 'messages':
                mid = integer(parts[5])
                message = self.message_for(state, rid, mid)
                if parts[6] == 'image' and method == 'GET':
                    if message['revoked'] or not message.get('mxc'):
                        raise PilotError('图片不存在', 404)
                    server, media = message['mxc'].removeprefix('mxc://').split('/', 1)
                    raw, _ = self.matrix.call('GET', '/_matrix/client/v1/media/download/' + quoted(server) + '/' + quoted(media), self.token(state, uid), mime='download')
                    return {'mime': message['image']['mime'], 'data': base64.b64encode(raw).decode()}
                if parts[6] == 'revoke' and method == 'POST':
                    if message['sender_id'] != int(uid):
                        raise PilotError('只能撤回自己的消息', 403)
                    self.matrix.room(self.token(state, uid), 'rooms/' + quoted(room['matrix_id']) + '/redact/' + quoted(message['event_id']) + '/' + secrets.token_hex(16), 'PUT', {})
                    self.redact_projection(state, rid, message)
                    return self.public_message(message)
            raise PilotError('接口不存在', 404)

    def redact_projection(self, state, rid, message):
        message.update(revoked=True, text='')
        message.pop('image', None)
        message.pop('mxc', None)
        for change in state['changes']:
            if change.get('message', {}) and change['message']['id'] == message['id']:
                change['message'] = self.public_message(message)
        self.change(state, 'message.revoked', rid, self.public_message(message))

    def message_for(self, state, rid, mid):
        message = state['messages'].get(str(mid))
        if not message or message['conversation_id'] != int(rid):
            raise PilotError('消息不存在', 404)
        return message

    def send(self, state, uid, rid, room, body):
        text, client_id = body.get('text', ''), body.get('client_id')
        if not isinstance(text, str) or len(text) > 10000 or not isinstance(client_id, str) or not 1 <= len(client_id) <= 100:
            raise PilotError('消息内容或发送标识格式错误')
        images = body.get('images')
        if images is not None and (not isinstance(images, list) or not 1 <= len(images) <= 10 or any(not isinstance(image, dict) for image in images)):
            raise PilotError('每组图片需要1至10张')
        images = images if images is not None else [body.get('image')]
        if not text.strip() and images == [None]:
            raise PilotError('消息不能为空')
        reply = self.message_for(state, rid, integer(body['reply_to_id'])) if body.get('reply_to_id') is not None else None
        mentions = body.get('mention_member_ids', [])
        if not isinstance(mentions, list) or len(mentions) > 100:
            raise PilotError('提及成员格式错误')
        mentions = [integer(u) for u in mentions]
        if any(str(u) not in room['members'] for u in mentions):
            raise PilotError('提及成员不在会话中')
        prepared = [image_data(image) if image is not None else None for image in images]
        sent = []
        for index, image in enumerate(prepared):
            content = {'msgtype': 'm.text', 'body': text, 'tt_mentions': mentions}
            if reply:
                content['m.relates_to'] = {'m.in_reply_to': {'event_id': reply['event_id']}}
            if image:
                raw, mime, width, height = image
                media = self.matrix.call('POST', '/_matrix/media/v3/upload', self.token(state, uid), raw=raw, mime=mime)['content_uri']
                content.update(msgtype='m.image', body='图片', tt_text=text,
                    url=media, info={'mimetype': mime, 'w': width, 'h': height, 'size': len(raw)})
            if len(prepared) > 1:
                content['tt_album_id'] = hashlib.sha256(f'{uid}:{rid}:{client_id}'.encode()).hexdigest()
            txn = hashlib.sha256(f'{uid}:{rid}:{client_id}:{index}'.encode()).hexdigest()
            event = self.matrix.room(self.token(state, uid), 'rooms/' + quoted(room['matrix_id']) + '/send/m.room.message/' + txn, 'PUT', content)['event_id']
            sent.append(event)
        self.refresh(state, uid)
        messages = [self.public_message(next(m for m in state['messages'].values() if m['event_id'] == event)) for event in sent]
        return {'messages': messages} if body.get('images') is not None else messages[0]
