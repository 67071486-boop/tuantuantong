from django.db import migrations


SETUP = """
CREATE TABLE business_finance_read_revision (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    epoch uuid NOT NULL DEFAULT gen_random_uuid(),
    catalog_version bigint NOT NULL DEFAULT 0,
    settlement_version bigint NOT NULL DEFAULT 0,
    catalog_xid bigint,
    settlement_xid bigint
);
INSERT INTO business_finance_read_revision DEFAULT VALUES;

CREATE FUNCTION business_bump_finance_read_revision() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    catalog boolean := TG_ARGV[0] IN ('catalog', 'both');
    settlement boolean := TG_ARGV[0] IN ('settlement', 'both');
    tx bigint := txid_current();
BEGIN
    EXECUTE format(
        'UPDATE %I.business_finance_read_revision
         SET catalog_version = catalog_version + ($1 AND catalog_xid IS DISTINCT FROM $3)::int,
             settlement_version = settlement_version + ($2 AND settlement_xid IS DISTINCT FROM $3)::int,
             catalog_xid = CASE WHEN $1 THEN $3 ELSE catalog_xid END,
             settlement_xid = CASE WHEN $2 THEN $3 ELSE settlement_xid END
         WHERE singleton
           AND (($1 AND catalog_xid IS DISTINCT FROM $3) OR ($2 AND settlement_xid IS DISTINCT FROM $3))',
        TG_TABLE_SCHEMA
    ) USING catalog, settlement, tx;
    RETURN NULL;
END;
$$;
"""

# A relevant change locks the revision row at commit, once per transaction per domain.
TRIGGERS = []


def add_trigger(table, suffix, clause, domain, condition=None, deferred=True):
    name = f"finance_revision_{suffix}"
    kind = "CONSTRAINT TRIGGER" if deferred else "TRIGGER"
    level = "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW" if deferred else "FOR EACH STATEMENT"
    when = f"WHEN ({condition})" if condition else ""
    setup = f"""
        CREATE {kind} {name} AFTER {clause} ON {table}
        {level} {when} EXECUTE FUNCTION business_bump_finance_read_revision('{domain}');
    """
    TRIGGERS.append((setup, f"DROP TRIGGER {name} ON {table};"))


def report_row(alias):
    return f"{alias}.settle_status = '已结账' AND {alias}.settled_at IS NOT NULL AND {alias}.deleted_at IS NULL"


def changed(columns):
    before = ", ".join(f"OLD.{column}" for column in columns)
    after = ", ".join(f"NEW.{column}" for column in columns)
    return f"ROW({before}) IS DISTINCT FROM ROW({after})"


add_trigger("business_performance", "insert", "INSERT", "settlement", report_row("NEW"))
add_trigger("business_performance", "delete", "DELETE", "settlement", report_row("OLD"))
ledger_columns = ("order_id", "emp_type", "settle_status", "settled_at", "settled_amount", "deleted_at")
add_trigger(
    "business_performance", "update", "UPDATE OF " + ", ".join(ledger_columns), "settlement",
    f"(({report_row('OLD')}) OR ({report_row('NEW')})) AND {changed(ledger_columns)}",
)
add_trigger(
    "business_order", "update", "UPDATE OF game_id, order_source_id, deleted_at", "settlement",
    "(OLD.deleted_at IS NULL OR NEW.deleted_at IS NULL) AND "
    "(OLD.game_id IS DISTINCT FROM NEW.game_id OR OLD.order_source_id IS DISTINCT FROM NEW.order_source_id "
    "OR (OLD.deleted_at IS NULL) IS DISTINCT FROM (NEW.deleted_at IS NULL))",
)
for table, columns in (
    ("business_game", ("id", "name", "sort_order", "deleted_at")),
    ("business_gameserver", ("id", "name", "sort_order", "game_id")),
    ("business_ordersource", ("id", "name", "order_by")),
):
    add_trigger(table, "catalog_membership", "INSERT OR DELETE", "catalog")
    add_trigger(table, "catalog_update", "UPDATE OF " + ", ".join(columns), "catalog", changed(columns))
    add_trigger(table, "truncate", "TRUNCATE", "catalog", deferred=False)

add_trigger(
    "business_ordersource", "settlement_rename", "UPDATE OF name", "settlement",
    "OLD.name IS DISTINCT FROM NEW.name",
)
add_trigger("business_performance", "truncate", "TRUNCATE", "settlement", deferred=False)


class Migration(migrations.Migration):
    dependencies = [("business", "0157_jidanpu_upstream_partial_probe_at")]
    operations = [
        migrations.RunSQL(
            SETUP + "\n".join(create for create, _drop in TRIGGERS),
            "\n".join(drop for _create, drop in reversed(TRIGGERS)) + """
                DROP FUNCTION business_bump_finance_read_revision();
                DROP TABLE business_finance_read_revision;
            """,
        ),
    ]
