from django.db import migrations


FUNCTIONS = {
    "integration_sync_record": (
        "resource_kind text, identity text, operation text",
        "void",
        """
        BEGIN
            IF EXISTS (
                SELECT 1 FROM openapi_apiapp
                WHERE kind = 'business_integration'
            ) THEN
                INSERT INTO integration_sync_businesschange
                    (resource, resource_id, action, occurred_at)
                VALUES (resource_kind, identity, operation, clock_timestamp());
            END IF;
        END
        """,
    ),
    "integration_sync_order_update": (
        "order_pk bigint",
        "void",
        """
        DECLARE number text;
        BEGIN
            SELECT order_no INTO number FROM business_order
            WHERE id = order_pk AND deleted_at IS NULL
                AND hall_card_id IS NULL AND upstream_hall_card_id IS NULL;
            IF number IS NOT NULL THEN
                PERFORM integration_sync_record('order', number, 'updated');
            END IF;
        END
        """,
    ),
    "integration_sync_is_worker": (
        "account_pk bigint",
        "boolean",
        """
        SELECT EXISTS (
            SELECT 1 FROM auth_user_groups AS membership
            JOIN auth_group AS account_group ON account_group.id = membership.group_id
            WHERE membership.user_id = account_pk AND account_group.name = '代练员'
        ) AND NOT EXISTS (
            SELECT 1 FROM auth_user_groups AS membership
            JOIN auth_group AS account_group ON account_group.id = membership.group_id
            WHERE membership.user_id = account_pk AND account_group.name <> '代练员'
        )
        """,
    ),
    "integration_sync_taker_update": (
        "account_pk bigint",
        "void",
        """
        BEGIN
            IF integration_sync_is_worker(account_pk) AND EXISTS (
                SELECT 1 FROM auth_user WHERE id = account_pk AND NOT is_superuser AND NOT is_staff
            ) THEN
                PERFORM integration_sync_record('taker', account_pk::text, 'updated');
            END IF;
        END
        """,
    ),
    "integration_sync_order": (
        "",
        "trigger",
        """
        DECLARE before_exists boolean := false;
                after_exists boolean := false;
                operation text;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                before_exists := OLD.hall_card_id IS NULL
                    AND OLD.upstream_hall_card_id IS NULL AND OLD.deleted_at IS NULL;
            END IF;
            IF TG_OP <> 'DELETE' THEN
                after_exists := NEW.hall_card_id IS NULL
                    AND NEW.upstream_hall_card_id IS NULL AND NEW.deleted_at IS NULL;
            END IF;

            IF before_exists AND NOT after_exists THEN
                PERFORM integration_sync_record('order', OLD.order_no, 'deleted');
                RETURN NULL;
            ELSIF NOT after_exists THEN
                RETURN NULL;
            ELSIF TG_OP = 'INSERT' THEN
                operation := 'created';
            ELSIF NOT before_exists THEN
                operation := 'restored';
            ELSIF ROW(
                OLD.status, OLD.order_type, OLD.split_id, OLD.split_kind, OLD.game_id,
                OLD.server_id, OLD.category_id, OLD.project_id, OLD.project_text,
                OLD.content, OLD.demand, OLD.time_requirement, OLD.game_account,
                OLD.game_sub_account, OLD.game_role_name, OLD.current_info_1,
                OLD.current_info_2, OLD.customer_phone,
                OLD.customer_qq, OLD.customer_ww, OLD.customer_id, OLD.total_price,
                OLD.received_price, OLD.platform_price, OLD.hall_price, OLD.fee,
                OLD.source, OLD.order_source_id, OLD.source_order_no, OLD.sales_agent,
                OLD.sales_user_id, OLD.is_urgent, OLD.remark, OLD.tag, OLD.order_label,
                OLD.completed_at, OLD.published_at, OLD.submitted_at, OLD.hall_publisher_id
            ) IS NOT DISTINCT FROM ROW(
                NEW.status, NEW.order_type, NEW.split_id, NEW.split_kind, NEW.game_id,
                NEW.server_id, NEW.category_id, NEW.project_id, NEW.project_text,
                NEW.content, NEW.demand, NEW.time_requirement, NEW.game_account,
                NEW.game_sub_account, NEW.game_role_name, NEW.current_info_1,
                NEW.current_info_2, NEW.customer_phone,
                NEW.customer_qq, NEW.customer_ww, NEW.customer_id, NEW.total_price,
                NEW.received_price, NEW.platform_price, NEW.hall_price, NEW.fee,
                NEW.source, NEW.order_source_id, NEW.source_order_no, NEW.sales_agent,
                NEW.sales_user_id, NEW.is_urgent, NEW.remark, NEW.tag, NEW.order_label,
                NEW.completed_at, NEW.published_at, NEW.submitted_at, NEW.hall_publisher_id
            ) THEN
                RETURN NULL;
            ELSE
                operation := 'updated';
            END IF;
            PERFORM integration_sync_record('order', NEW.order_no, operation);
            RETURN NULL;
        END
        """,
    ),
    "integration_sync_performance": (
        "",
        "trigger",
        """
        BEGIN
            IF TG_OP = 'UPDATE' AND ROW(OLD.user_id, OLD.claimed_at, OLD.taker_price, OLD.deleted_at)
                IS NOT DISTINCT FROM ROW(NEW.user_id, NEW.claimed_at, NEW.taker_price, NEW.deleted_at)
            THEN
                RETURN NULL;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF OLD.emp_type = '打单工资' AND OLD.user_id IS NOT NULL THEN
                    PERFORM integration_sync_order_update(OLD.order_id);
                END IF;
            ELSIF NEW.emp_type = '打单工资'
                AND (TG_OP = 'UPDATE' OR NEW.user_id IS NOT NULL)
            THEN
                PERFORM integration_sync_order_update(NEW.order_id);
            END IF;
            RETURN NULL;
        END
        """,
    ),
    "integration_sync_screenshot": (
        "",
        "trigger",
        """
        BEGIN
            IF TG_OP <> 'INSERT' AND OLD.status = 'ready' THEN
                PERFORM integration_sync_order_update(OLD.order_id);
            ELSIF TG_OP <> 'DELETE' AND NEW.status = 'ready' THEN
                PERFORM integration_sync_order_update(NEW.order_id);
            END IF;
            RETURN NULL;
        END
        """,
    ),
    "integration_sync_audit": (
        "",
        "trigger",
        """
        BEGIN
            IF NEW.action = 'order.note' AND EXISTS (
                SELECT 1 FROM business_order WHERE order_no = NEW.order_no
                    AND deleted_at IS NULL AND hall_card_id IS NULL AND upstream_hall_card_id IS NULL
            ) THEN
                PERFORM integration_sync_record('order', NEW.order_no, 'updated');
            END IF;
            RETURN NULL;
        END
        """,
    ),
    "integration_sync_user": (
        "",
        "trigger",
        """
        DECLARE operation text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF NOT OLD.is_superuser AND NOT OLD.is_staff AND integration_sync_is_worker(OLD.id) THEN
                    PERFORM integration_sync_record('taker', OLD.id::text, 'deleted');
                END IF;
                RETURN OLD;
            ELSIF NOT integration_sync_is_worker(OLD.id) OR EXISTS (
                SELECT 1 FROM accounts_staffprofile
                WHERE user_id = OLD.id AND deleted_at IS NOT NULL
            ) THEN
                RETURN NULL;
            ELSIF (OLD.is_superuser OR OLD.is_staff) IS DISTINCT FROM
                (NEW.is_superuser OR NEW.is_staff) THEN
                operation := CASE WHEN NEW.is_superuser OR NEW.is_staff
                    THEN 'deleted' ELSE 'restored' END;
            ELSIF NEW.is_superuser OR NEW.is_staff OR
                ROW(OLD.username, OLD.first_name, OLD.is_active) IS NOT DISTINCT FROM
                ROW(NEW.username, NEW.first_name, NEW.is_active)
            THEN
                RETURN NULL;
            ELSE
                operation := 'updated';
            END IF;
            PERFORM integration_sync_record('taker', NEW.id::text, operation);
            RETURN NULL;
        END
        """,
    ),
    "integration_sync_membership": (
        "",
        "trigger",
        """
        DECLARE account record;
        BEGIN
            FOR account IN
                WITH affected AS (SELECT DISTINCT user_id FROM membership_delta),
                groups AS (
                    SELECT membership.user_id, account_group.name,
                        delta.id IS NULL OR TG_OP = 'DELETE' AS was_present,
                        true AS is_present
                    FROM auth_user_groups AS membership
                    JOIN affected ON affected.user_id = membership.user_id
                    JOIN auth_group AS account_group ON account_group.id = membership.group_id
                    LEFT JOIN membership_delta AS delta ON delta.id = membership.id
                    UNION ALL
                    SELECT delta.user_id, account_group.name, true, false
                    FROM membership_delta AS delta
                    JOIN auth_group AS account_group ON account_group.id = delta.group_id
                    WHERE TG_OP = 'DELETE'
                )
                SELECT groups.user_id,
                    bool_or(groups.was_present AND groups.name = '代练员')
                        AND NOT bool_or(groups.was_present AND groups.name <> '代练员') AS was_worker,
                    bool_or(groups.is_present AND groups.name = '代练员')
                        AND NOT bool_or(groups.is_present AND groups.name <> '代练员') AS is_worker
                FROM groups
                JOIN auth_user AS account_user ON account_user.id = groups.user_id
                LEFT JOIN accounts_staffprofile AS profile ON profile.user_id = groups.user_id
                WHERE NOT account_user.is_superuser AND NOT account_user.is_staff
                    AND profile.deleted_at IS NULL
                GROUP BY groups.user_id
            LOOP
                IF account.was_worker AND NOT account.is_worker THEN
                    PERFORM integration_sync_record('taker', account.user_id::text, 'deleted');
                ELSIF NOT account.was_worker AND account.is_worker THEN
                    PERFORM integration_sync_record('taker', account.user_id::text, 'created');
                END IF;
            END LOOP;
            RETURN NULL;
        END
        """,
    ),
    "integration_sync_role_rename": (
        "",
        "trigger",
        """
        DECLARE account_pk bigint;
        BEGIN
            IF OLD.name IS NOT DISTINCT FROM NEW.name OR
                (OLD.name <> '代练员' AND NEW.name <> '代练员') THEN
                RETURN NULL;
            END IF;
            FOR account_pk IN
                SELECT membership.user_id FROM auth_user_groups AS membership
                JOIN auth_user AS account_user ON account_user.id = membership.user_id
                LEFT JOIN accounts_staffprofile AS profile ON profile.user_id = membership.user_id
                WHERE membership.group_id = NEW.id
                    AND NOT account_user.is_superuser AND NOT account_user.is_staff
                    AND profile.deleted_at IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM auth_user_groups AS other
                        WHERE other.user_id = membership.user_id AND other.group_id <> NEW.id
                    )
            LOOP
                PERFORM integration_sync_record(
                    'taker', account_pk::text,
                    CASE WHEN NEW.name = '代练员' THEN 'created' ELSE 'deleted' END
                );
            END LOOP;
            RETURN NULL;
        END
        """,
    ),
    "integration_sync_profile": (
        "",
        "trigger",
        """
        DECLARE operation text := 'updated';
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.deleted_at IS NULL AND NEW.deleted_at IS NOT NULL THEN
                    operation := 'deleted';
                ELSIF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS NULL THEN
                    operation := 'restored';
                ELSIF ROW(
                    OLD.business_type, OLD.member_type, OLD.is_studio, OLD.grab_limit_dl,
                    OLD.grab_limit_pw, OLD.grab_price_min, OLD.grab_price_max,
                    OLD.grab_forbid_self_service, OLD.order_by, OLD.live_url,
                    OLD.qq, OLD.mobile, OLD.hired_on, OLD.tag, OLD.remark, OLD.avatar_key
                ) IS NOT DISTINCT FROM ROW(
                    NEW.business_type, NEW.member_type, NEW.is_studio, NEW.grab_limit_dl,
                    NEW.grab_limit_pw, NEW.grab_price_min, NEW.grab_price_max,
                    NEW.grab_forbid_self_service, NEW.order_by, NEW.live_url,
                    NEW.qq, NEW.mobile, NEW.hired_on, NEW.tag, NEW.remark, NEW.avatar_key
                ) THEN
                    RETURN NULL;
                END IF;
            END IF;
            IF integration_sync_is_worker(NEW.user_id) AND EXISTS (
                SELECT 1 FROM auth_user WHERE id = NEW.user_id AND NOT is_superuser AND NOT is_staff
            ) THEN
                PERFORM integration_sync_record('taker', NEW.user_id::text, operation);
            END IF;
            RETURN NULL;
        END
        """,
    ),
    "integration_sync_game_membership": (
        "",
        "trigger",
        """
        DECLARE profile_pk bigint;
                account_pk bigint;
        BEGIN
            profile_pk := CASE WHEN TG_OP = 'DELETE' THEN OLD.staffprofile_id ELSE NEW.staffprofile_id END;
            SELECT user_id INTO account_pk FROM accounts_staffprofile WHERE id = profile_pk;
            IF account_pk IS NOT NULL THEN
                PERFORM integration_sync_taker_update(account_pk);
            END IF;
            RETURN NULL;
        END
        """,
    ),
    "integration_sync_realname": (
        "",
        "trigger",
        """
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM integration_sync_taker_update(OLD.user_id);
            ELSIF TG_OP = 'INSERT' OR OLD.result IS DISTINCT FROM NEW.result THEN
                PERFORM integration_sync_taker_update(NEW.user_id);
            END IF;
            RETURN NULL;
        END
        """,
    ),
}

TRIGGERS = (
    ("order", "business_order", "AFTER INSERT OR UPDATE OR DELETE", "order"),
    ("performance", "business_performance", "AFTER INSERT OR UPDATE OF user_id, claimed_at, taker_price, deleted_at OR DELETE", "performance"),
    ("screenshot", "business_orderscreenshot", "AFTER INSERT OR UPDATE OF status OR DELETE", "screenshot"),
    ("audit", "audit_operationlog", "AFTER INSERT", "audit"),
    ("user_update", "auth_user", "AFTER UPDATE OF username, first_name, is_active, is_superuser, is_staff", "user"),
    ("user_delete", "auth_user", "BEFORE DELETE", "user"),
    ("role_rename", "auth_group", "AFTER UPDATE OF name", "role_rename"),
    ("profile", "accounts_staffprofile", "AFTER INSERT OR UPDATE", "profile"),
    ("game_membership", "accounts_staffprofile_games", "AFTER INSERT OR DELETE", "game_membership"),
    ("realname", "realname_realnamecertrecord", "AFTER INSERT OR UPDATE OF result OR DELETE", "realname"),
)


def install(apps, schema_editor):
    # Bind queries to the trigger's tenant schema even when SQL sets a different search_path.
    schema = schema_editor.quote_name(schema_editor.connection.schema_name)
    for name, (args, returns, body) in FUNCTIONS.items():
        language = "sql" if name == "integration_sync_is_worker" else "plpgsql"
        schema_editor.execute(
            f"CREATE FUNCTION {schema}.{name}({args}) RETURNS {returns} "
            f"LANGUAGE {language} SET search_path = pg_catalog, {schema}, pg_temp AS $$ {body} $$"
        )
    for operation, transition in (("INSERT", "NEW"), ("DELETE", "OLD")):
        schema_editor.execute(
            f"CREATE TRIGGER integration_sync_membership_{operation.lower()} AFTER {operation} "
            f"ON {schema}.auth_user_groups REFERENCING {transition} TABLE AS membership_delta "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {schema}.integration_sync_membership()"
        )
    for name, table, actions, function in TRIGGERS:
        schema_editor.execute(
            f"CREATE TRIGGER integration_sync_{name} {actions} ON {schema}.{table} "
            f"FOR EACH ROW EXECUTE FUNCTION {schema}.integration_sync_{function}()"
        )


def uninstall(apps, schema_editor):
    schema = schema_editor.quote_name(schema_editor.connection.schema_name)
    for name, table, _actions, _function in reversed(TRIGGERS):
        schema_editor.execute(f"DROP TRIGGER integration_sync_{name} ON {schema}.{table}")
    for operation in ("insert", "delete"):
        schema_editor.execute(
            f"DROP TRIGGER integration_sync_membership_{operation} ON {schema}.auth_user_groups"
        )
    for name in reversed(FUNCTIONS):
        schema_editor.execute(f"DROP FUNCTION {schema}.{name}")


class Migration(migrations.Migration):
    dependencies = [
        ("integration_sync", "0001_initial"),
        ("accounts", "0034_wxpusher_preferences_and_notices"),
        ("audit", "0008_merge_0007_operationlog_actor_name_0007_requestmetric"),
        ("business", "0150_backfill_penalty_approval_logs"),
        ("openapi", "0008_apiapp_kind"),
        ("realname", "0001_initial"),
    ]

    operations = [migrations.RunPython(install, uninstall)]
