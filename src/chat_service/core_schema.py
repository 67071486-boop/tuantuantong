
def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS core_control (
        tenant_id text PRIMARY KEY, active boolean NOT NULL DEFAULT false,
        next_id bigint NOT NULL DEFAULT 1000000000, cursor bigint NOT NULL DEFAULT 0,
        required_protocol integer NOT NULL DEFAULT 1)''')
    db.execute('ALTER TABLE core_control ADD COLUMN IF NOT EXISTS required_protocol integer NOT NULL DEFAULT 1')
    db.execute('''CREATE TABLE IF NOT EXISTS core_policy_clock (
        tenant_id text PRIMARY KEY, version bigint NOT NULL DEFAULT 0)''')
    db.execute('''CREATE TABLE IF NOT EXISTS core_identities (
        tenant_id text NOT NULL, user_id bigint NOT NULL, payload jsonb NOT NULL,
        PRIMARY KEY (tenant_id,user_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS core_reads (
        tenant_id text NOT NULL, conversation_id bigint NOT NULL, user_id bigint NOT NULL,
        seq bigint NOT NULL DEFAULT 0, PRIMARY KEY (tenant_id,conversation_id,user_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS core_preferences (
        tenant_id text NOT NULL, conversation_id bigint NOT NULL, user_id bigint NOT NULL,
        payload jsonb NOT NULL, PRIMARY KEY (tenant_id,conversation_id,user_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS core_events (
        tenant_id text NOT NULL, event_id bigint NOT NULL, conversation_id bigint NOT NULL,
        kind text NOT NULL, audience jsonb NOT NULL, message_id bigint,
        PRIMARY KEY (tenant_id,event_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS core_commands (
        tenant_id text NOT NULL, conversation_id bigint NOT NULL, user_id bigint NOT NULL,
        client_id text NOT NULL, fingerprint text NOT NULL, response jsonb NOT NULL,
        PRIMARY KEY (tenant_id,conversation_id,user_id,client_id))''')
    db.execute("CREATE INDEX IF NOT EXISTS core_direct_pair_idx ON rooms (tenant_id,(metadata->>'first_user_id'),(metadata->>'second_user_id')) WHERE kind='direct'")
    db.execute('''CREATE TABLE IF NOT EXISTS core_outbox (
        job_id bigserial PRIMARY KEY, tenant_id text NOT NULL, message_id bigint NOT NULL,
        action text NOT NULL, payload jsonb NOT NULL, attempts integer NOT NULL DEFAULT 0,
        available_at timestamptz NOT NULL DEFAULT now(), last_error text NOT NULL DEFAULT '',
        UNIQUE (tenant_id,message_id,action))''')
    db.execute('''CREATE INDEX IF NOT EXISTS core_outbox_tenant_pending
        ON core_outbox (tenant_id,available_at,job_id)''')
    db.execute('''CREATE TABLE IF NOT EXISTS core_notifications (
        tenant_id text NOT NULL, event_id bigint NOT NULL, payload jsonb NOT NULL,
        attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (tenant_id,event_id))''')

    db.execute('''CREATE TABLE IF NOT EXISTS core_archives (
        tenant_id text NOT NULL, conversation_id bigint NOT NULL,
        archived boolean NOT NULL DEFAULT false, completed_seen boolean NOT NULL DEFAULT false,
        activity_at timestamptz, completion_revision text, PRIMARY KEY (tenant_id,conversation_id))''')

    db.execute('ALTER TABLE core_archives ADD COLUMN IF NOT EXISTS completion_revision text')

    db.execute('''CREATE TABLE IF NOT EXISTS core_order_rooms (
        tenant_id text NOT NULL, order_no text NOT NULL, conversation_id bigint NOT NULL,
        PRIMARY KEY(tenant_id,order_no), UNIQUE(tenant_id,conversation_id))''')

    db.execute('ALTER TABLE core_control ADD COLUMN IF NOT EXISTS writes_paused boolean NOT NULL DEFAULT false')
    db.execute('ALTER TABLE core_archives ADD COLUMN IF NOT EXISTS deleted_seen boolean NOT NULL DEFAULT false')

    db.execute('ALTER TABLE core_archives ADD COLUMN IF NOT EXISTS deletion_revision text')

    db.execute('''CREATE TABLE IF NOT EXISTS core_peer_groups (
        tenant_id text NOT NULL, conversation_id bigint NOT NULL, group_id text NOT NULL,
        side text NOT NULL, active boolean NOT NULL, detached boolean NOT NULL DEFAULT false, publisher_id bigint,
        generation bigint NOT NULL DEFAULT 0, is_current boolean NOT NULL DEFAULT true,
        history_queued boolean NOT NULL DEFAULT false, local_roster jsonb,
        roster_version bigint NOT NULL DEFAULT 0, remote_version bigint NOT NULL DEFAULT 0,
        pin_revision bigint NOT NULL DEFAULT 0, pin_operation text, pinned_wire_id text,
        PRIMARY KEY(tenant_id,group_id))''')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS core_peer_groups_current
        ON core_peer_groups(tenant_id,conversation_id) WHERE is_current''')
    db.execute('''CREATE TABLE IF NOT EXISTS core_peer_messages (
        tenant_id text NOT NULL, conversation_id bigint NOT NULL, group_id text NOT NULL,
        message_id bigint NOT NULL, wire_id text NOT NULL,
        PRIMARY KEY(tenant_id,group_id,message_id), UNIQUE(tenant_id,group_id,wire_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS core_peer_outbox (
        event_id bigserial PRIMARY KEY, tenant_id text NOT NULL, conversation_id bigint NOT NULL, group_id text NOT NULL,
        event_key text NOT NULL, kind text NOT NULL, payload jsonb NOT NULL, wire_payload jsonb,
        attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now())''')
    db.execute('''CREATE INDEX IF NOT EXISTS core_peer_outbox_head_idx
        ON core_peer_outbox(tenant_id,conversation_id,event_id)''')
    db.execute('''CREATE TABLE IF NOT EXISTS core_peer_imports (
        tenant_id text NOT NULL, event_id bigint NOT NULL, receipt text NOT NULL, PRIMARY KEY(tenant_id,event_id))''')
