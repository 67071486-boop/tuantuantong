import hashlib
import json

from psycopg.types.json import Jsonb

from .config import ServiceError, integer
from .core_authority import REVOKED, _grants
from .core_utils import DEFAULT_PREFERENCE, ID_END, ID_START, fields, text


class CoreState:
    def _preference(self, db, tenant, cid, viewer):
        row = db.execute('''SELECT payload FROM core_preferences
            WHERE tenant_id=%s AND conversation_id=%s AND user_id=%s''', (tenant, cid, viewer)).fetchone()
        return {**DEFAULT_PREFERENCE, **(row['payload'] if row else {})}

    def _read_seq(self, db, tenant, cid, viewer):
        row = db.execute('SELECT seq FROM core_reads WHERE tenant_id=%s AND conversation_id=%s AND user_id=%s', (tenant, cid, viewer)).fetchone()
        return row['seq'] if row else 0

    def _person(self, db, tenant, user_id):
        row = db.execute('SELECT payload FROM core_identities WHERE tenant_id=%s AND user_id=%s', (tenant, user_id)).fetchone()
        return row['payload'] if row else {'id': user_id, 'name': str(user_id), 'avatar_url': None}

    def _dto(self, db, tenant, room, viewer):
        cid, metadata = room['conversation_id'], room['metadata']
        members = db.execute('SELECT * FROM members WHERE tenant_id=%s AND conversation_id=%s ORDER BY NOT active,member_id', (tenant, cid)).fetchall()
        own = next((m for m in members if m['local_user_id'] == viewer and m['active']), None)
        read_seq = self._read_seq(db, tenant, cid, viewer)
        latest = db.execute('SELECT * FROM messages WHERE tenant_id=%s AND conversation_id=%s ORDER BY seq DESC LIMIT 1', (tenant, cid)).fetchone()
        where = 'tenant_id=%s AND conversation_id=%s AND seq>%s AND NOT revoked AND sender_identity<>%s'
        args = (tenant, cid, read_seq, 'local:' + str(viewer))
        mention = Jsonb([own['member_id']]) if own else Jsonb([None])
        stats = db.execute("""SELECT count(*) FILTER (WHERE payload->>'album_id' IS NULL)
            + count(DISTINCT payload->>'album_id') AS unread,
            count(*) FILTER (WHERE payload->'mention_member_ids' @> %s) AS mentions
            FROM messages WHERE """ + where, (mention, *args)).fetchone()
        first = db.execute('SELECT message_id FROM messages WHERE ' + where + ' ORDER BY seq LIMIT 1', args).fetchone()
        first_mention = db.execute("SELECT message_id FROM messages WHERE " + where +
            " AND payload->'mention_member_ids' @> %s ORDER BY seq LIMIT 1", (*args, mention)).fetchone() if own else None
        pref = self._preference(db, tenant, cid, viewer)
        result = {'id': cid, 'last_message': self._message_payload(latest) if latest else None,
            'unread_count': stats['unread'], 'read_seq': read_seq, 'peer_read_seq': 0,
            'first_unread_message_id': first['message_id'] if first else None,
            'first_unread_mention_id': first_mention['message_id'] if first_mention else None,
            'unread_mentions_count': stats['mentions'], **pref}
        if room['kind'] == 'direct':
            peer = next((m['local_user_id'] for m in members if m['local_user_id'] != viewer), None)
            if peer is None:
                raise ServiceError('Direct conversation identity mapping missing', 503)
            result.update(peer=self._person(db, tenant, peer), peer_read_seq=self._read_seq(db, tenant, cid, peer))
        else:
            grant = self._grant(tenant, cid)
            archive = self._archive_state(db, tenant, cid, grant)
            permitted = set(grant['read_user_ids'])
            peer_readonly = metadata.get('peer_readonly', False)
            result.update(archived=archive['archived'], can_reopen=not peer_readonly and viewer in grant['reopen_user_ids'],
                can_send=bool(own and viewer in permitted and self._order_writable(db, tenant, room)),
                can_join=bool(not peer_readonly and own is None and viewer in permitted))
            result.update(kind='order', title=metadata.get('title') or '订单-' + metadata.get('order_no', ''),
                order_no=metadata.get('order_no', ''), local_order_no=metadata.get('local_order_no') or metadata.get('order_no'),
                members=[{'id': m['member_id'], 'name': m['metadata'].get('name', ''), 'avatar_url': None,
                    'studio_name': m['metadata'].get('studio_name', ''), 'order_role': m['metadata'].get('order_role', ''),
                    'left_at': m['metadata'].get('left_at'),
                    'has_permission': m['local_user_id'] in permitted or bool(m['metadata'].get('peer_key') and m['active']),
                    'permission_state': 'allowed' if m['local_user_id'] in permitted or (m['metadata'].get('peer_key') and m['active'])
                    else 'denied'} for m in members],
                self_member_id=own['member_id'] if own else None, is_member=own is not None,
                pinned_message_id=metadata.get('pinned_message_id'),
                can_pin_messages=bool(own and not archive['archived'] and self._can_pin(metadata, own)))
        return result

    @staticmethod
    def _can_pin(metadata, member):
        return metadata.get('can_send', False) and member['local_user_id'] in metadata.get('can_pin_user_ids', [])

    def _op_start(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {})
        fields(payload, {'viewer', 'peer'}, {'viewer', 'peer'})
        own, peer = self._identity(db, tenant, payload['viewer']), self._identity(db, tenant, payload['peer'])
        if own['id'] != viewer or viewer == peer['id']:
            raise ServiceError('Invalid direct participants')
        first, second = sorted((viewer, peer['id']))
        room = db.execute('''SELECT * FROM rooms WHERE tenant_id=%s AND kind='direct'
            AND metadata->>'first_user_id'=%s AND metadata->>'second_user_id'=%s''', (tenant, str(first), str(second))).fetchone()
        if room is None:
            cid = self._allocate(db, tenant)
            metadata = {'id': cid, 'kind': 'direct', 'first_user_id': first, 'second_user_id': second}
            db.execute('INSERT INTO rooms (tenant_id,conversation_id,room_id,kind,metadata) VALUES (%s,%s,%s,%s,%s)',
                (tenant, cid, 'pending:' + str(cid), 'direct', Jsonb(metadata)))
            for person in (own, peer):
                data = {**person, 'local_user_id': person['id'], 'left_at': None}
                db.execute('''INSERT INTO members (tenant_id,conversation_id,member_id,local_user_id,active,metadata)
                    VALUES (%s,%s,%s,%s,true,%s)''', (tenant, cid, person['id'], person['id'], Jsonb(data)))
                db.execute('INSERT INTO core_reads (tenant_id,conversation_id,user_id) VALUES (%s,%s,%s)', (tenant, cid, person['id']))
            self._event(db, tenant, cid, 'conversation.created')
            room = self._room(db, tenant, cid, viewer)
        return self._dto(db, tenant, room, viewer)

    def _visible_rooms(self, db, tenant, viewer):
        rows = db.execute('''SELECT r.* FROM rooms r
            LEFT JOIN core_preferences p ON p.tenant_id=r.tenant_id AND p.conversation_id=r.conversation_id AND p.user_id=%s
            LEFT JOIN LATERAL (SELECT max(message_id) AS mid FROM messages x
                WHERE x.tenant_id=r.tenant_id AND x.conversation_id=r.conversation_id) latest ON true
            WHERE r.tenant_id=%s AND EXISTS (SELECT 1 FROM members m WHERE m.tenant_id=r.tenant_id
                AND m.conversation_id=r.conversation_id AND m.local_user_id=%s AND m.active)
            ORDER BY coalesce((p.payload->>'pinned')::boolean,false) DESC,coalesce(latest.mid,0) DESC,r.conversation_id DESC''',
            (viewer, tenant, viewer)).fetchall()
        context = _grants.get()
        return [row for row in rows if row['kind'] != 'order' or
            not context['unavailable'] and self._can_read_order(tenant, row['conversation_id'], viewer)]

    def _op_conversations(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'limit', 'offset'}); fields(payload, {})
        cursor = self._state(db, tenant)['cursor']
        rooms = self._visible_rooms(db, tenant, viewer)
        offset = integer(query.get('offset', 0), 'offset', 0)
        limit = integer(query['limit'], 'limit', 1, 100) if 'limit' in query else len(rooms)
        selected = rooms[offset:offset + limit]
        fresh = [self._grant(tenant, r['conversation_id']) for r in selected if r['kind'] == 'order']
        if fresh:
            self._merge_authority_rooms(db, tenant, fresh)
            selected = [db.execute('SELECT * FROM rooms WHERE tenant_id=%s AND conversation_id=%s',
                (tenant, room['conversation_id'])).fetchone() if room['kind'] == 'order' else room for room in selected]
        return {'cursor': cursor, 'conversations': [self._dto(db, tenant, room, viewer) for room in selected],
                'has_more': offset + limit < len(rooms), 'next_offset': offset + len(selected)}

    def _op_unread(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {}); fields(payload, {})
        cids = [r['conversation_id'] for r in self._visible_rooms(db, tenant, viewer)]
        if not cids:
            return {'unread_count': 0}
        rows = db.execute('''SELECT r.conversation_id,
            count(*) FILTER (WHERE x.message_id IS NOT NULL AND x.payload->>'album_id' IS NULL)
             + count(DISTINCT x.payload->>'album_id') AS unread,
            coalesce((p.payload->>'marked_unread')::boolean,false) AS marked
            FROM rooms r LEFT JOIN core_reads rd ON rd.tenant_id=r.tenant_id AND rd.conversation_id=r.conversation_id AND rd.user_id=%s
            LEFT JOIN core_preferences p ON p.tenant_id=r.tenant_id AND p.conversation_id=r.conversation_id AND p.user_id=%s
            LEFT JOIN messages x ON x.tenant_id=r.tenant_id AND x.conversation_id=r.conversation_id
                AND x.seq>coalesce(rd.seq,0) AND NOT x.revoked AND x.sender_identity<>%s
            WHERE r.tenant_id=%s AND r.conversation_id=ANY(%s)
            GROUP BY r.conversation_id,p.payload''', (viewer, viewer, 'local:' + str(viewer), tenant, cids)).fetchall()
        return {'unread_count': sum(max(row['unread'], int(row['marked'])) for row in rows)}

    def _op_preferences(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'conversation_id'}, {'conversation_id'})
        cid = integer(query['conversation_id'], 'conversation_id', 1, ID_END)
        self._room(db, tenant, cid, viewer)
        fields(payload, set(DEFAULT_PREFERENCE))
        if not payload:
            raise ServiceError('Preference change required')
        for key, value in payload.items():
            if key == 'remark':
                text(value, 30, 'remark')
            elif not isinstance(value, bool):
                raise ServiceError('Invalid preference flag')
        pref = self._preference(db, tenant, cid, viewer)
        changed = any(pref[k] != v for k, v in payload.items())
        pref.update(payload)
        db.execute('''INSERT INTO core_preferences (tenant_id,conversation_id,user_id,payload) VALUES (%s,%s,%s,%s)
            ON CONFLICT (tenant_id,conversation_id,user_id) DO UPDATE SET payload=EXCLUDED.payload''', (tenant, cid, viewer, Jsonb(pref)))
        if changed:
            self._event(db, tenant, cid, 'conversation.preferences_changed', audience=[viewer])
        return pref

    def _op_read(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'conversation_id'}, {'conversation_id'}); fields(payload, {'seq'}, {'seq'})
        cid = integer(query['conversation_id'], 'conversation_id', 1, ID_END)
        room = self._room(db, tenant, cid, viewer)
        seq = integer(payload['seq'], 'seq', 0)
        last = db.execute('SELECT coalesce(max(seq),0) AS seq FROM messages WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()['seq']
        if seq > last:
            raise ServiceError('Read position exceeds conversation')
        current = self._read_seq(db, tenant, cid, viewer)
        pref = self._preference(db, tenant, cid, viewer)
        if pref['marked_unread']:
            self._op_preferences(db, tenant, viewer, {'conversation_id': cid}, {'marked_unread': False}, False)
        if seq > current:
            db.execute('''INSERT INTO core_reads (tenant_id,conversation_id,user_id,seq) VALUES (%s,%s,%s,%s)
                ON CONFLICT (tenant_id,conversation_id,user_id) DO UPDATE SET seq=EXCLUDED.seq''', (tenant, cid, viewer, seq))
            self._event(db, tenant, cid, 'conversation.read', audience=[viewer] if room['kind'] == 'order' else None)
        return {'read_seq': max(seq, current)}

    def _op_sync(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'after'}, {'after'}); fields(payload, {})
        after = integer(query['after'], 'after', 0)
        rows = db.execute('''SELECT e.*,r.kind AS room_kind FROM core_events e
            JOIN rooms r ON r.tenant_id=e.tenant_id AND r.conversation_id=e.conversation_id
            WHERE e.tenant_id=%s AND e.event_id>%s AND e.audience @> %s
            ORDER BY e.event_id LIMIT 101''', (tenant, after, Jsonb([viewer]))).fetchall()
        page, changes = rows[:100], []
        context = _grants.get()
        denied = {r['conversation_id'] for r in db.execute('''SELECT r.conversation_id FROM rooms r JOIN members m
            ON m.tenant_id=r.tenant_id AND m.conversation_id=r.conversation_id
            WHERE r.tenant_id=%s AND r.kind='order' AND m.local_user_id=%s''', (tenant, viewer)).fetchall()
            if not context['unavailable'] and not self._can_read_order(tenant, r['conversation_id'], viewer)}
        for event in page:
            if event['room_kind'] == 'order' and (context['unavailable'] or event['conversation_id'] in denied):
                continue
            row = db.execute('SELECT * FROM messages WHERE tenant_id=%s AND message_id=%s', (tenant, event['message_id'])).fetchone() if event['message_id'] is not None else None
            changes.append({'kind': event['kind'], 'conversation_id': event['conversation_id'],
                            'message': self._message_payload(row) if row else None})
        changes.extend({'kind': 'conversation.removed', 'conversation_id': cid, 'message': None} for cid in sorted(denied))
        joined = {row['conversation_id'] for row in db.execute('''SELECT r.conversation_id FROM rooms r JOIN members m
            ON m.tenant_id=r.tenant_id AND m.conversation_id=r.conversation_id
            WHERE r.tenant_id=%s AND r.kind='order' AND m.local_user_id=%s AND m.active''', (tenant, viewer)).fetchall()}
        visible = sorted(cid for cid in joined if cid not in denied)
        visibility_token = None if context['unavailable'] else hashlib.sha256(json.dumps(visible, separators=(',', ':')).encode()).hexdigest()
        return {'cursor': page[-1]['event_id'] if page else after, 'has_more': len(rows) > 100,
                'changes': changes, 'order_visibility_token': visibility_token}

    def _op_peer(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'conversation_id'}, {'conversation_id'}); fields(payload, {})
        room = self._room(db, tenant, integer(query['conversation_id'], 'conversation_id', 1, ID_END), viewer)
        if room['kind'] != 'direct':
            raise ServiceError('Presence is only available for direct conversations', 403)
        return {'peer_id': self._dto(db, tenant, room, viewer)['peer']['id']}

    def _op_order(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'conversation_id'}, {'conversation_id'}); fields(payload, {})
        room = self._room(db, tenant, integer(query['conversation_id'], 'conversation_id', 1, ID_END), viewer, readonly)
        return self._dto(db, tenant, room, viewer)

    def _op_notification(self, db, tenant, viewer, query, payload, readonly):
        fields(payload, {})
        room = self._room(db, tenant, integer(query.get('conversation_id'), 'conversation_id', 1, ID_END), viewer)
        page = self._history(db, tenant, room, query, 'history')
        dto = self._dto(db, tenant, room, viewer)
        sender_ids = {m['sender_id'] for m in page['messages']}
        if room['kind'] == 'direct':
            senders = [self._person(db, tenant, uid) for uid in (viewer, dto['peer']['id']) if uid in sender_ids]
            details = {'kind': 'direct', 'peer_name': dto['peer']['name'], 'order_no': '',
                       'self_member_id': viewer, 'can_reply': True, 'senders': [{**s, 'studio_name': ''} for s in senders]}
        else:
            details = {'kind': 'order', 'order_no': dto['order_no'], 'self_member_id': dto['self_member_id'],
                       'can_reply': dto['can_send'],
                       'senders': [{k: m[k] for k in ('id', 'name', 'studio_name')} for m in dto['members'] if m['id'] in sender_ids]}
        return {**page, **details, 'remark': dto['remark']}

    def _op_revoke(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'conversation_id', 'message_id'}, {'conversation_id', 'message_id'}); fields(payload, {})
        cid = integer(query['conversation_id'], 'conversation_id', 1, ID_END)
        room = self._room(db, tenant, cid, viewer)
        if room['kind'] == 'order' and not self._order_writable(db, tenant, room):
            raise ServiceError('订单群已归档，仅可查看历史消息', 403)
        row = db.execute('SELECT * FROM messages WHERE tenant_id=%s AND conversation_id=%s AND message_id=%s',
                         (tenant, cid, integer(query['message_id'], 'message_id', 1, ID_END))).fetchone()
        if not row:
            raise ServiceError('Message not found', 404)
        if row['sender_identity'] != 'local:' + str(viewer):
            raise ServiceError('Only sender can revoke message', 403)
        if not row['revoked']:
            if room['kind'] == 'order':
                self._queue_peer_revoke(db, tenant, cid, row)
            self._revoke_message(db, tenant, cid, row)
        return self._message_payload({**row, 'revoked': True})

    def _revoke_message(self, db, tenant, cid, row):
        value = self._message_payload({**row, 'revoked': True})
        if row['payload'].get('album_id') and self._peer_group(db, tenant, cid):
            value['album_id'] = row['payload']['album_id']
        db.execute('''UPDATE messages SET revoked=true,text_body='',media_uri=NULL,media_mime=NULL,payload=%s
            WHERE tenant_id=%s AND message_id=%s''', (Jsonb(value), tenant, row['message_id']))
        db.execute('''INSERT INTO core_outbox (tenant_id,message_id,action,payload)
            VALUES (%s,%s,'revoke','{}') ON CONFLICT (tenant_id,message_id,action) DO NOTHING''', (tenant, row['message_id']))
        db.execute("UPDATE core_outbox SET payload=jsonb_build_object('message',%s::jsonb,'image',NULL) WHERE tenant_id=%s AND message_id=%s AND action='send'", (Jsonb(value), tenant, row['message_id']))
        self._event(db, tenant, cid, 'message.revoked', row['message_id'])
        room = db.execute('SELECT metadata FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
        if room['metadata'].get('pinned_message_id') == row['message_id']:
            self._update_peer_pin(db, tenant, cid, None)

    def _op_pin(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'conversation_id'}, {'conversation_id'}); fields(payload, {'message_id'}, {'message_id'})
        cid = integer(query['conversation_id'], 'conversation_id', 1, ID_END)
        room = self._room(db, tenant, cid, viewer)
        member = db.execute('SELECT * FROM members WHERE tenant_id=%s AND conversation_id=%s AND local_user_id=%s AND active', (tenant, cid, viewer)).fetchone()
        if room['kind'] != 'order' or self._archive_state(db, tenant, cid, self._grant(tenant, cid))['archived'] or not self._can_pin(room['metadata'], member):
            raise ServiceError('Pin permission required', 403)
        mid = payload['message_id']
        if mid is not None:
            mid = integer(mid, 'message_id', 1, ID_END)
            if not db.execute('SELECT 1 FROM messages WHERE tenant_id=%s AND conversation_id=%s AND message_id=%s AND NOT revoked', (tenant, cid, mid)).fetchone():
                raise ServiceError('Pinned message not found', 404)
        if room['metadata'].get('pinned_message_id') != mid:
            metadata = {**room['metadata'], 'pinned_message_id': mid}
            db.execute('UPDATE rooms SET metadata=%s WHERE tenant_id=%s AND conversation_id=%s', (Jsonb(metadata), tenant, cid))
            self._event(db, tenant, cid, 'conversation.pin_changed')
            self._queue_peer_pin(db, tenant, cid, mid)
        return {'pinned_message_id': mid}

    def _op_policy(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {}); fields(payload, {})
        row = db.execute('''INSERT INTO core_policy_clock (tenant_id,version) VALUES (%s,1)
            ON CONFLICT (tenant_id) DO UPDATE SET version=core_policy_clock.version+1
            WHERE core_policy_clock.version<%s RETURNING version''', (tenant, 2**63 - 1)).fetchone()
        if not row:
            raise ServiceError('Business policy version range exhausted', 503)
        return {'policy_version': row['version']}

    def _op_room(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {})
        fields(payload, {'id', 'kind', 'members', 'order_no', 'local_order_no', 'title', 'first_user_id', 'second_user_id',
            'can_send', 'can_pin_user_ids', 'denied_user_ids', 'restricted_user_ids', 'policy_version', 'preferences', 'first_read_seq', 'second_read_seq', 'pinned_message_id'}, {'id', 'kind', 'members'})
        self._validate_authority_room(payload)
        cid = integer(payload['id'], 'conversation.id', 1, ID_END)
        if payload['kind'] != 'order' or not isinstance(payload['members'], list) or len(payload['members']) > 1000:
            raise ServiceError('Business room must be an order group')
        old_room = db.execute('SELECT * FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
        if old_room and old_room['kind'] != 'order':
            raise ServiceError('Cannot replace direct conversation', 409)
        current = old_room['metadata'].get('policy_version', 0) if old_room else 0
        if 'policy_version' not in payload and current:
            raise ServiceError('Versioned room requires a business policy version', 409)
        version = integer(payload['policy_version'], 'policy_version') if 'policy_version' in payload else 0
        policy = {key: value for key, value in payload.items() if key != 'policy_version'}
        fingerprint = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if version:
            clock = db.execute('SELECT version FROM core_policy_clock WHERE tenant_id=%s', (tenant,)).fetchone()
            if clock is None or version > clock['version']:
                raise ServiceError('Business policy version was not issued for this tenant', 409)
            if version < current:
                return {'conversation_id': cid, 'applied': False}
            if version == current:
                if old_room['metadata'].get('policy_fingerprint') != fingerprint:
                    raise ServiceError('Business policy version reused for another payload', 409)
                return {'conversation_id': cid, 'applied': False}
        if 'can_send' in payload and not isinstance(payload['can_send'], bool):
            raise ServiceError('Invalid group send permission')
        if 'can_pin_user_ids' in payload and (not isinstance(payload['can_pin_user_ids'], list) or
                any(type(v) is not int or not 0 < v < ID_START for v in payload['can_pin_user_ids'])):
            raise ServiceError('Invalid pin permission list')
        self._merge_authority_room(db, tenant, payload)
        if version:
            db.execute('''UPDATE rooms SET metadata=metadata || %s WHERE tenant_id=%s AND conversation_id=%s''',
                (Jsonb({'policy_version': version, 'policy_fingerprint': fingerprint}), tenant, cid))
        return {'conversation_id': cid, 'applied': True}

    def _op_join(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'conversation_id'}, {'conversation_id'}); fields(payload, {'grant', 'viewer'}, {'grant', 'viewer'})
        if not isinstance(payload['viewer'], dict) or payload['grant'] is not True or payload['viewer'].get('id') != viewer:
            raise ServiceError('Business join grant required', 403)
        identity = self._identity(db, tenant, payload['viewer'])
        cid = integer(query['conversation_id'], 'conversation_id', 1, ID_END)
        room = self._room(db, tenant, cid, viewer, readonly=True)
        member = db.execute('SELECT * FROM members WHERE tenant_id=%s AND conversation_id=%s AND local_user_id=%s', (tenant, cid, viewer)).fetchone()
        if room['metadata'].get('peer_readonly', False):
            raise ServiceError('群聊已结束，仅可查看历史消息', 403)
        if member and member['active']:
            return self._dto(db, tenant, room, viewer)
        mid = member['member_id'] if member else self._allocate(db, tenant)
        data = {**(member['metadata'] if member and member['active'] else {}), **identity,
                'id': mid, 'local_user_id': viewer, 'left_at': None, 'manual_joined': True, 'follower': True}
        data.setdefault('studio_name', '')
        data.setdefault('order_role', 'follower')
        db.execute('''INSERT INTO members (tenant_id,conversation_id,member_id,local_user_id,active,metadata)
            VALUES (%s,%s,%s,%s,true,%s) ON CONFLICT (tenant_id,conversation_id,member_id)
            DO UPDATE SET active=true,metadata=EXCLUDED.metadata''', (tenant, cid, mid, viewer, Jsonb(data)))
        db.execute('INSERT INTO core_reads (tenant_id,conversation_id,user_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING', (tenant, cid, viewer))
        if not member or not member['active'] or not member['metadata'].get('manual_joined'):
            self._event(db, tenant, cid, 'conversation.members_changed')
            self._queue_peer_roster(db, tenant, self._peer_group(db, tenant, cid))
        return self._dto(db, tenant, room, viewer)

    def _op_reopen(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'conversation_id'}, {'conversation_id'}); fields(payload, {})
        cid = integer(query['conversation_id'], 'conversation_id', 1, ID_END)
        room = self._room(db, tenant, cid, viewer, readonly=True)
        if room['metadata'].get('peer_readonly', False) or viewer not in self._grant(tenant, cid)['reopen_user_ids']:
            raise ServiceError('当前账号无权重开订单群', 403)
        self._archive_state(db, tenant, cid, self._grant(tenant, cid))
        changed = db.execute('''UPDATE core_archives SET archived=false,activity_at=clock_timestamp()
            WHERE tenant_id=%s AND conversation_id=%s AND archived RETURNING conversation_id''', (tenant, cid)).fetchone()
        if changed:
            self._event(db, tenant, cid, 'conversation.archive_changed')
        return self._dto(db, tenant, room, viewer)
