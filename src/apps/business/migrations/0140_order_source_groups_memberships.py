from django.db import migrations, models


def copy_existing_memberships(apps, schema_editor):
    Source = apps.get_model("business", "OrderSource")
    Membership = Source.groups.through
    quote = schema_editor.quote_name
    source_table = quote(Source._meta.db_table)
    membership_table = quote(Membership._meta.db_table)
    schema_editor.execute(
        f"INSERT INTO {membership_table} (ordersource_id, ordersourcegroup_id) "
        f"SELECT id, group_id FROM {source_table} WHERE group_id IS NOT NULL"
    )


class Migration(migrations.Migration):
    dependencies = [("business", "0139_backfill_split_ship_main_order_no")]

    operations = [
        migrations.AddField(
            model_name="ordersource",
            name="groups",
            field=models.ManyToManyField(
                blank=True, related_name="member_sources",
                to="business.ordersourcegroup", verbose_name="公司店铺分组",
            ),
        ),
        migrations.RunPython(copy_existing_memberships, migrations.RunPython.noop),
    ]
