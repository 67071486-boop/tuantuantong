import base64
import hashlib
from datetime import timedelta

import httpx
import psycopg
from psycopg.types.json import Jsonb

from .config import ServiceError, internal_url


class CoreDelivery:
    def _delivery_context(self, tenant, mid):
        with self.store.connect() as db:
            message = db.execute('SELECT * FROM messages WHERE tenant_id=%s AND message_id=%s', (tenant, mid)).fetchone()
            room = db.execute('SELECT * FROM rooms WHERE tenant_id=%s AND conversation_id=%s', (tenant, message['conversation_id'])).fetchone()
            account = self.engine._account(db, tenant)
        if room['room_id'].startswith('pending:'):
            room_id = self.engine.matrix.room(tenant, room['conversation_id'], account, room['metadata'].get('title', ''))
            with self.store.connect() as db:
                db.execute('UPDATE rooms SET room_id=%s WHERE tenant_id=%s AND conversation_id=%s', (room_id, tenant, room['conversation_id']))
            room['room_id'] = room_id
        return message, room, account

    def _deliver(self, tenant, job):
        mid, payload = job['message_id'], dict(job['payload'])
        message, room, account = self._delivery_context(tenant, mid)
        token = account['access_token']
        transaction = hashlib.sha256(f'{tenant}:message:{mid}'.encode()).hexdigest()
        if job['action'] == 'revoke':
            if message['event_id'] is None:
                raise ServiceError('Waiting for original event before redaction', 503, retry_after=1)
            self.engine.matrix.redact(room['room_id'], message['event_id'], token, 'revoke_' + transaction)
            self._complete(tenant, job, message['event_id'], None, None)
            return
        event = message['event_id']
        value = self._message_payload(message)
        content = {'msgtype': 'm.text', 'body': value['text'], 'tt_payload': value,
                   'tt_sender_identity': message['sender_identity']}
        media_uri, media_mime = payload.get('media_uri'), payload.get('media_mime')
        image = payload.get('image')
        if image and not message['revoked']:
            raw = base64.b64decode(image['data'])
            if media_uri is None:
                media_uri = self.engine.matrix.upload(token, raw, image['mime'])
                media_mime = image['mime']
                payload.update(media_uri=media_uri, media_mime=media_mime)
                with self.store.connect() as db:
                    db.execute('UPDATE core_outbox SET payload=%s WHERE tenant_id=%s AND job_id=%s', (Jsonb(payload), tenant, job['job_id']))
            content.update(msgtype='m.image', url=media_uri, body=value['text'] or 'Image',
                           info={'mimetype': media_mime, 'size': len(raw)})
        if event is None:
            event = self.engine.matrix.send(room['room_id'], token, transaction, content)
        self._complete(tenant, job, event, media_uri, media_mime)

    def _complete(self, tenant, job, event, uri, mime):
        with self.store.connect() as db:
            db.execute('''UPDATE messages SET event_id=%s,
                media_uri=CASE WHEN revoked THEN NULL ELSE %s END,
                media_mime=CASE WHEN revoked THEN NULL ELSE %s END,
                payload=jsonb_set(payload,'{delivery_state}','"delivered"'::jsonb)
                WHERE tenant_id=%s AND message_id=%s''', (event, uri, mime, tenant, job['message_id']))
            db.execute('DELETE FROM core_outbox WHERE tenant_id=%s AND job_id=%s', (tenant, job['job_id']))

    def _retry(self, tenant, table, id_field, row, error):
        attempts = min(row['attempts'] + 1, 10)
        delay = error.retry_after if isinstance(error, ServiceError) and error.retry_after is not None else min(2 ** (attempts - 1), 60)
        with self.store.connect() as db:
            db.execute(f'''UPDATE {table} SET attempts=attempts+1,available_at=now()+%s
                WHERE tenant_id=%s AND {id_field}=%s''', (timedelta(seconds=max(.1, delay)), tenant, row[id_field]))

    def _notify_batch(self, tenant, jobs):
        checks = {}
        for job in jobs:
            if job['room_kind'] == 'order':
                checks.setdefault(job['conversation_id'], set()).update(job['payload']['users'])
        eligible, authority_unavailable = jobs, False
        try:
            grants = self._authority(tenant, [{'conversation_id': cid, 'user_ids': sorted(users)} for cid, users in sorted(checks.items())]) if checks else {}
        except ServiceError as error:
            authority_unavailable = True
            eligible = [job for job in jobs if job['room_kind'] != 'order']
            for job in jobs:
                if job['room_kind'] == 'order': self._retry(tenant, 'core_notifications', 'event_id', job, error)
            grants = {}
        notifications = []
        for job in eligible:
            payload = dict(job['payload'])
            if job['room_kind'] == 'order':
                permitted = set(grants.get(job['conversation_id'], {}).get('read_user_ids', []))
                payload['users'] = [uid for uid in payload['users'] if uid in permitted]
            if payload['users']: notifications.append(payload)
        config = self.engine.tenants[tenant]['realtime']
        try:
            if notifications:
                url = internal_url(config['url'])
                if not url.endswith('/notify'): url += '/notify'
                response = httpx.post(url, json={'tenant': config['tenant'], 'notifications': notifications},
                    headers={'Authorization': 'Bearer ' + config['secret']}, timeout=5, trust_env=False)
                if response.status_code != 204: raise ServiceError('Realtime gateway rejected notification batch', 503)
            if eligible:
                with self.store.connect() as db:
                    db.execute('DELETE FROM core_notifications WHERE tenant_id=%s AND event_id=ANY(%s)',
                        (tenant, [job['event_id'] for job in eligible]))
            return len(eligible), authority_unavailable
        except (ServiceError, httpx.HTTPError, psycopg.OperationalError) as error:
            for job in eligible: self._retry(tenant, 'core_notifications', 'event_id', job, error)
            return 0, authority_unavailable

    def tick(self, tenant):
        if tenant not in self.engine.tenants or not self.is_active(tenant):
            raise ServiceError('Chat core is not activated', 503)
        processed = 0
        with self.store.tenant(tenant) as db:
            self._require_protocol(db, tenant)
            db.execute("""UPDATE core_archives SET archived=true WHERE tenant_id=%s AND NOT archived
                AND activity_at<=now()-interval '72 hours'""", (tenant,))
        # A session lock spans deliveries that individually borrow from the pool.
        with self.store.dedicated_connection() as lock_db:
            key = 'tt_chat_core_tick:' + self.store.schema + ':' + tenant
            locked = lock_db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked', (key,)).fetchone()['locked']
            lock_db.commit()
            if not locked:
                return {'processed': 0, 'has_more': True, 'retry_after': .2}
            try:
                try:
                    reconciled = self._reconcile_order_intents(tenant)
                    intent_unavailable = False
                except (ServiceError, httpx.HTTPError, psycopg.OperationalError):
                    reconciled, intent_unavailable = 0, True
                peer_more = False
                try:
                    count, peer_more = self._relay_peer(tenant)
                    processed += count
                except (ServiceError, httpx.HTTPError, psycopg.OperationalError):
                    peer_more = True
                with self.store.connect() as db:
                    jobs = db.execute('''SELECT * FROM core_outbox WHERE tenant_id=%s
                        AND available_at<=now() ORDER BY job_id LIMIT 20''', (tenant,)).fetchall()
                for job in jobs:
                    try:
                        self._deliver(tenant, job)
                        processed += 1
                    except (ServiceError, httpx.HTTPError, psycopg.OperationalError) as error:
                        self._retry(tenant, 'core_outbox', 'job_id', job, error)
                        if isinstance(error, ServiceError) and error.retry_after is not None: break
                with self.store.connect() as db:
                    notifications = db.execute('''SELECT n.*,e.conversation_id,r.kind AS room_kind FROM core_notifications n
                        JOIN core_events e ON e.tenant_id=n.tenant_id AND e.event_id=n.event_id
                        JOIN rooms r ON r.tenant_id=e.tenant_id AND r.conversation_id=e.conversation_id
                        WHERE n.tenant_id=%s AND n.available_at<=now() ORDER BY n.event_id LIMIT 20''', (tenant,)).fetchall()
                if notifications:
                    completed, unavailable = self._notify_batch(tenant, notifications)
                    processed += completed
                    if unavailable and all(job['room_kind'] == 'order' for job in notifications):
                        with self.store.connect() as db:
                            direct = db.execute('''SELECT n.*,e.conversation_id,r.kind AS room_kind FROM core_notifications n
                                JOIN core_events e ON e.tenant_id=n.tenant_id AND e.event_id=n.event_id
                                JOIN rooms r ON r.tenant_id=e.tenant_id AND r.conversation_id=e.conversation_id
                                WHERE n.tenant_id=%s AND n.available_at<=now() AND r.kind='direct'
                                ORDER BY n.event_id LIMIT 20''', (tenant,)).fetchall()
                        if direct: processed += self._notify_batch(tenant, direct)[0]
                with self.store.connect() as db:
                    pending = db.execute('''SELECT count(*) AS n,min(available_at) AS available FROM
                        (SELECT available_at FROM core_outbox WHERE tenant_id=%s UNION ALL
                         SELECT available_at FROM core_notifications WHERE tenant_id=%s UNION ALL
                         SELECT q.available_at FROM core_peer_outbox q WHERE q.tenant_id=%s AND NOT EXISTS
                            (SELECT 1 FROM core_peer_outbox prior WHERE prior.tenant_id=q.tenant_id
                                AND prior.conversation_id=q.conversation_id AND prior.event_id<q.event_id)
                        ) tasks''', (tenant, tenant, tenant)).fetchone()
                    remaining = db.execute('SELECT greatest(0,extract(epoch FROM (%s-now()))) AS delay', (pending['available'],)).fetchone()['delay'] if pending['available'] else 0
                result = {'processed': processed, 'has_more': bool(pending['n']) or reconciled == 100 or peer_more, 'reconciled_orders': reconciled,
                    'order_intents_unavailable': intent_unavailable}
                if pending['n'] and remaining and not processed:
                    result['retry_after'] = float(remaining)
                elif peer_more and not processed:
                    result['retry_after'] = 1.0
                return result
            finally:
                lock_db.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', (key,))
                lock_db.commit()
