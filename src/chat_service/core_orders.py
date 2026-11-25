from .config import ServiceError, integer
from .core_authority import REVOKED, UNAVAILABLE, _grants
from .core_utils import DEFAULT_PREFERENCE, ID_END, ID_START, fields, text


class CoreOrders:
    @staticmethod
    def _order_number(value):
        text(value, 20, 'order_no', blank=False)
        if len(value) != 20 or not value.isascii() or not value.isdigit():
            raise ServiceError('Invalid order number')
        return value

    def _order_fact(self, tenant, order_no, viewer):
        result = self._authority_request(tenant, [{'order_no': order_no, 'user_ids': [viewer]}], viewer=viewer)
        facts = result.get('orders')
        if not isinstance(facts, list): raise ServiceError(UNAVAILABLE, 503)
        if not facts: raise ServiceError('Order not found', 404)
        if len(facts) != 1: raise ServiceError(UNAVAILABLE, 503)
        return self._validate_order_fact(facts[0], order_no)

    def _validate_order_fact(self, fact, order_no=None):
        try:
            self._order_number(fact['order_no'])
            if order_no is not None and fact['order_no'] != order_no: raise ValueError
            for key in ('read_user_ids', 'reopen_user_ids', 'default_join_user_ids'):
                if not isinstance(fact[key], list): raise ValueError
                for uid in fact[key]: integer(uid, key, 1, ID_START - 1)
            if 'can_send' in fact and type(fact['can_send']) is not bool: raise ValueError
            if 'can_pin_user_ids' in fact:
                if not isinstance(fact['can_pin_user_ids'], list): raise ValueError
                for uid in fact['can_pin_user_ids']: integer(uid, 'can_pin_user', 1, ID_START - 1)
            if type(fact['completed']) is not bool or type(fact['deleted']) is not bool: raise ValueError
            self._observed_revision(fact, 'completion')
            self._observed_revision(fact, 'deletion')
            if not isinstance(fact['participants'], list): raise ValueError
            for participant in fact['participants']: self._validate_participant(participant)
            if fact.get('conversation_id') is not None: integer(fact['conversation_id'], 'conversation_id', 1, ID_END)
            if 'peer_link' in fact: self._validate_peer_link(fact['peer_link'])
            self._fact_room(fact, 1)
        except (KeyError, ValueError, TypeError, ServiceError):
            raise ServiceError(UNAVAILABLE, 503) from None
        return fact

    def _validate_participant(self, participant):
        fields(participant, {'local_user_id', 'name', 'avatar_url', 'studio_name', 'order_role'}, {'local_user_id', 'name'})
        uid = integer(participant['local_user_id'], 'participant.user', 1, ID_START - 1)
        self._validate_authority_room({'id': 1, 'kind': 'order', 'members': [{'id': uid, **participant}]})
        return uid

    def _fact_room(self, fact, cid, db=None, tenant=None):
        if fact.get('room') is not None:
            room = {**fact['room'], 'id': cid, 'members': [dict(member) for member in fact['room']['members']]}
            for key in ('can_send', 'can_pin_user_ids'):
                if key in fact: room[key] = fact[key]
            self._validate_authority_room(room)
            return room
        members = []
        if not isinstance(fact['participants'], list): raise ServiceError('Invalid order participants')
        for participant in fact['participants']:
            uid = self._validate_participant(participant)
            mid = uid
            if db is not None:
                prior = db.execute('SELECT member_id FROM members WHERE tenant_id=%s AND conversation_id=%s AND local_user_id=%s', (tenant, cid, uid)).fetchone()
                mid = prior['member_id'] if prior else self._allocate(db, tenant)
            members.append({**participant, 'id': mid, 'left_at': None})
        room = {'id': cid, 'kind': 'order', 'order_no': fact['order_no'], 'local_order_no': fact['order_no'],
            'title': '订单-' + fact['order_no'], 'can_send': fact.get('can_send', True),
            'can_pin_user_ids': fact.get('can_pin_user_ids', []), 'members': members}
        self._validate_authority_room(room)
        return room

    def _ensure_order_room(self, db, tenant, fact):
        number = fact['order_no']
        mapping = db.execute('SELECT conversation_id FROM core_order_rooms WHERE tenant_id=%s AND order_no=%s', (tenant, number)).fetchone()
        source_cid = fact.get('conversation_id')
        if mapping:
            cid = mapping['conversation_id']
            if source_cid is not None and source_cid != cid: raise ServiceError('Order conversation mapping conflicts', 409)
        elif source_cid is not None:
            cid = source_cid
            known = db.execute('SELECT 1 FROM rooms WHERE tenant_id=%s AND conversation_id=%s AND kind=\'order\'', (tenant, cid)).fetchone()
            if not known: raise ServiceError('Existing order history is not ready', 503)
        else:
            existing = db.execute('''SELECT conversation_id FROM rooms WHERE tenant_id=%s AND kind='order'
                AND coalesce(nullif(metadata->>'local_order_no',''),metadata->>'order_no')=%s
                ORDER BY conversation_id LIMIT 2''', (tenant, number)).fetchall()
            if len(existing) > 1: raise ServiceError('Order conversation mapping is ambiguous', 409)
            cid = existing[0]['conversation_id'] if existing else self._allocate(db, tenant)
        db.execute('''INSERT INTO core_order_rooms(tenant_id,order_no,conversation_id) VALUES (%s,%s,%s)
            ON CONFLICT (tenant_id,order_no) DO NOTHING''', (tenant, number, cid))
        prior = db.execute('SELECT metadata FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
        created = prior is None
        if created and (source_cid is not None or mapping): raise ServiceError('Existing order history is not ready', 503)
        room = self._fact_room(fact, cid, db, tenant)
        if prior:
            for key in ('order_no', 'title'):
                if prior['metadata'].get(key): room[key] = prior['metadata'][key]
        grant = {**fact, 'conversation_id': cid, 'room': room}
        self._merge_authority_rooms(db, tenant, [grant], allow_create=created)
        context = _grants.get()
        if context is not None: context['groups'][cid] = grant
        return cid, created

    def _op_order_send(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'order_no'}, {'order_no'}); fields(payload, {'viewer', 'message'}, {'viewer', 'message'})
        self._order_number(query['order_no'])
        fact = _grants.get()['order_fact']
        if viewer not in fact['read_user_ids']: raise ServiceError(REVOKED, 403)
        if not isinstance(payload['viewer'], dict) or payload['viewer'].get('id') != viewer:
            raise ServiceError('Sender identity mismatch', 403)
        identity = self._identity(db, tenant, payload['viewer'])
        cid, created = self._ensure_order_room(db, tenant, fact)
        room = self._room(db, tenant, cid, viewer, readonly=True)
        if created:
            self._op_join(db, tenant, viewer, {'conversation_id': cid}, {'grant': True, 'viewer': identity}, False)
        elif not db.execute('SELECT 1 FROM members WHERE tenant_id=%s AND conversation_id=%s AND local_user_id=%s AND active', (tenant, cid, viewer)).fetchone():
            if not self._order_writable(db, tenant, room): raise ServiceError('Order conversation is read only', 403)
            raise ServiceError('请先加入会话', 409)
        room = self._room(db, tenant, cid, viewer)
        sent = self._send(db, tenant, room, viewer, payload['message'])
        return {'conversation': {**self._dto(db, tenant, room, viewer), 'has_conversation': True},
            'messages': sent['messages'] if 'messages' in sent else [sent]}

    def _op_order_preview(self, db, tenant, viewer, query, payload, readonly):
        fields(query, {'order_no'}, {'order_no'}); fields(payload, {})
        fact = _grants.get()['order_fact']
        if viewer not in fact['read_user_ids']: raise ServiceError(REVOKED, 403)
        row = db.execute('SELECT conversation_id FROM core_order_rooms WHERE tenant_id=%s AND order_no=%s', (tenant, fact['order_no'])).fetchone()
        cid = row['conversation_id'] if row else fact.get('conversation_id')
        if cid is not None:
            room = db.execute('SELECT * FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
            if room:
                grant = {**fact, 'conversation_id': cid, 'room': self._fact_room(fact, cid, db, tenant)}
                self._merge_authority_rooms(db, tenant, [grant])
                room = db.execute('SELECT * FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, cid)).fetchone()
                _grants.get()['groups'][cid] = {**fact, 'conversation_id': cid}
                return {**self._dto(db, tenant, room, viewer), 'has_conversation': True}
            raise ServiceError('Existing order history is not ready', 503)
        return {**DEFAULT_PREFERENCE, 'has_conversation': False, 'id': None, 'kind': 'order', 'order_no': fact['order_no'], 'local_order_no': fact['order_no'],
            'title': '订单-' + fact['order_no'], 'is_member': False, 'self_member_id': None, 'members': [],
            'last_message': None, 'read_seq': 0, 'unread_count': 0, 'archived': fact['completed'] or fact['deleted'],
            'can_send': not fact['completed'] and not fact['deleted'] and fact.get('can_send', True), 'can_reopen': False, 'can_join': False,
            'can_start': not fact['completed'] and not fact['deleted'], 'messages': [], 'readonly': True,
            'first_unread_message_id': None, 'first_unread_mention_id': None, 'unread_mentions_count': 0,
            'pinned_message_id': None, 'can_pin_messages': False, 'peer_read_seq': 0}

    def _with_default_participants(self, db, tenant, grants):
        relevant = [g for g in grants if g.get('participants')]
        if not relevant: return grants
        cids = [g['conversation_id'] for g in relevant]
        existing = {(m['conversation_id'], m['local_user_id']): m['member_id'] for m in db.execute(
            'SELECT conversation_id,local_user_id,member_id FROM members WHERE tenant_id=%s AND conversation_id=ANY(%s)', (tenant, cids)).fetchall() if m['local_user_id'] is not None}
        result = []
        for grant in sorted(grants, key=lambda item: item['conversation_id']):
            room = {**grant['room'], 'members': [dict(member) for member in grant['room']['members']]}
            by_user = {member.get('local_user_id'): member for member in room['members'] if member.get('local_user_id') is not None}
            for profile in grant.get('participants', []):
                uid = integer(profile['local_user_id'], 'participant.user', 1, ID_START - 1)
                if uid not in grant.get('default_join_user_ids', []): continue
                member = by_user.get(uid)
                if member:
                    member.update(profile)
                else:
                    mid = existing.get((grant['conversation_id'], uid))
                    if mid is None: mid = self._allocate(db, tenant)
                    room['members'].append({**profile, 'id': mid, 'left_at': None})
                    by_user[uid] = room['members'][-1]
            self._validate_authority_room(room)
            result.append({**grant, 'room': room})
        return result

    def _reconcile_order_intents(self, tenant):
        data = self._source_request(tenant, 'order-intents', {'operation': 'pull', 'limit': 100})
        peer_enabled = data.get('peer_chat', False)
        if type(peer_enabled) is not bool:
            raise ServiceError(UNAVAILABLE, 503)
        if peer_enabled:
            self._peer_enabled.add(tenant)
        intents = data.get('intents')
        if not isinstance(intents, list) or len(intents) > 100: raise ServiceError(UNAVAILABLE, 503)
        seen_ids, seen_orders = set(), set()
        for intent in intents:
            fields(intent, {'id', 'revision', 'ensure_group', 'order'}, {'id', 'revision', 'ensure_group', 'order'})
            integer(intent['id'], 'intent.id'); integer(intent['revision'], 'intent.revision')
            if type(intent['ensure_group']) is not bool: raise ServiceError(UNAVAILABLE, 503)
            self._validate_order_fact(intent['order'])
            if intent['id'] in seen_ids or intent['order']['order_no'] in seen_orders: raise ServiceError(UNAVAILABLE, 503)
            seen_ids.add(intent['id']); seen_orders.add(intent['order']['order_no'])
        if not intents: return 0
        with self.store.tenant(tenant) as db:
            for intent in sorted(intents, key=lambda item: item['order']['order_no']):
                fact = intent['order']
                known = db.execute('SELECT 1 FROM core_order_rooms WHERE tenant_id=%s AND order_no=%s', (tenant, fact['order_no'])).fetchone()
                if not known and fact.get('conversation_id') is not None:
                    known = db.execute('SELECT 1 FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, fact['conversation_id'])).fetchone()
                    if not known: raise ServiceError('Existing order history is not ready', 503)
                if intent['ensure_group'] or known: self._ensure_order_room(db, tenant, fact)
        ack = [{'id': row['id'], 'revision': row['revision']} for row in intents]
        result = self._source_request(tenant, 'order-intents', {'operation': 'ack', 'intents': ack})
        return integer(result.get('acked'), 'acked', 0, len(ack))
