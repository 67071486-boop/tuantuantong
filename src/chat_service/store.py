from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .config import DatabasePoolBusy, ServiceError


def _reset_session(connection):
    connection.autocommit = True
    try:
        connection.execute('DISCARD ALL')
    finally:
        connection.autocommit = False


class Store:
    def __init__(self, dsn, schema='chat_service', *, pool_size=0):
        self.dsn, self.schema = dsn, schema
        self._search_path = sql.SQL('SET search_path TO {}').format(sql.Identifier(schema))
        self._pool = None
        if pool_size:
            from psycopg_pool import ConnectionPool

            # DISCARD ALL drops server-side prepared statements on every return.
            self._pool = ConnectionPool(
                dsn, kwargs={'row_factory': dict_row, 'prepare_threshold': None},
                min_size=1, max_size=pool_size, max_waiting=16, timeout=2, num_workers=2,
                reset=_reset_session, open=True, name='chat-store',
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self._pool is not None:
            self._pool.close()

    @contextmanager
    def dedicated_connection(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as connection:
            connection.execute(self._search_path)
            yield connection

    @contextmanager
    def connect(self):
        if self._pool is None:
            with self.dedicated_connection() as connection:
                yield connection
            return
        from psycopg_pool import PoolTimeout, TooManyRequests

        try:
            with self._pool.connection() as connection:
                connection.execute(self._search_path)
                yield connection
        except (PoolTimeout, TooManyRequests):
            raise DatabasePoolBusy() from None

    def initialize(self):
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (self.schema,)).fetchone():
                raise ServiceError('Chat schema must be provisioned before initialization', 503)
            db.execute('''CREATE TABLE IF NOT EXISTS sync_state (
                tenant_id text PRIMARY KEY, phase text NOT NULL DEFAULT 'conversation',
                cursor bigint NOT NULL DEFAULT 0, snapshot_cursor bigint NOT NULL DEFAULT 0,
                snapshot_latest bigint, updated_at timestamptz NOT NULL DEFAULT now())''')
            db.execute('''CREATE TABLE IF NOT EXISTS accounts (
                tenant_id text NOT NULL, source_identity text NOT NULL, username text NOT NULL,
                password text NOT NULL, matrix_id text, access_token text,
                PRIMARY KEY (tenant_id, source_identity))''')
            db.execute('''CREATE TABLE IF NOT EXISTS rooms (
                tenant_id text NOT NULL, conversation_id bigint NOT NULL, room_id text NOT NULL,
                kind text NOT NULL, metadata jsonb NOT NULL, PRIMARY KEY (tenant_id, conversation_id),
                UNIQUE (tenant_id, room_id))''')
            db.execute('''CREATE TABLE IF NOT EXISTS members (
                tenant_id text NOT NULL, conversation_id bigint NOT NULL, member_id bigint NOT NULL,
                local_user_id bigint, active boolean NOT NULL, metadata jsonb NOT NULL,
                PRIMARY KEY (tenant_id, conversation_id, member_id))''')
            db.execute('''CREATE INDEX IF NOT EXISTS member_access_idx ON members
                (tenant_id, conversation_id, local_user_id) WHERE active''')
            db.execute('''CREATE TABLE IF NOT EXISTS messages (
                tenant_id text NOT NULL, conversation_id bigint NOT NULL, message_id bigint NOT NULL,
                seq bigint NOT NULL, sender_id bigint, sender_identity text NOT NULL,
                event_id text, revoked boolean NOT NULL, text_body text NOT NULL,
                created_at double precision NOT NULL DEFAULT 0, media_uri text, media_mime text,
                payload jsonb NOT NULL, PRIMARY KEY (tenant_id, message_id),
                UNIQUE (tenant_id, conversation_id, seq))''')
            db.execute('''CREATE INDEX IF NOT EXISTS message_page_idx ON messages
                (tenant_id, conversation_id, seq DESC)''')
            db.execute('''CREATE TABLE IF NOT EXISTS imported_changes (
                tenant_id text NOT NULL, change_id bigint NOT NULL,
                PRIMARY KEY (tenant_id, change_id))''')
            from .core_schema import initialize
            initialize(db)

    @contextmanager
    def tenant(self, tenant):
        with self.connect() as db:
            locked = db.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked',
                                ('tt_chat:' + self.schema + ':' + tenant,)).fetchone()['locked']
            if not locked:
                raise ServiceError('Tenant synchronization is busy', 503)
            db.execute('INSERT INTO sync_state (tenant_id) VALUES (%s) ON CONFLICT DO NOTHING', (tenant,))
            yield db

    def status(self, tenant):
        with self.connect() as db:
            row = db.execute('SELECT * FROM sync_state WHERE tenant_id=%s', (tenant,)).fetchone()
            if row:
                row['updated_at'] = row['updated_at'].isoformat()
                return row
            return {'tenant_id': tenant, 'phase': 'conversation', 'cursor': 0,
                    'snapshot_cursor': 0, 'snapshot_latest': None}
