from django.db import migrations, models


def snapshot_active_groups(apps, schema_editor):
    Order = apps.get_model("business", "Order")
    orders = Order.objects.using(schema_editor.connection.alias)
    active_members = orders.filter(deleted_at__isnull=True).exclude(split_id="")
    main_orders = active_members.filter(split_id=models.OuterRef("split_id"))
    active_members.filter(split_ship_main_order_no__isnull=True).update(
        split_ship_main_order_no=models.Subquery(
            main_orders.order_by("id").values("order_no")[:1]
        )
    )


class Migration(migrations.Migration):
    atomic = False
    dependencies = [("business", "0138_order_split_ship_main_order_no")]

    operations = [
        migrations.RunPython(snapshot_active_groups, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["split_ship_main_order_no"],
                name="idx_order_split_ship_main",
                condition=models.Q(split_ship_main_order_no__isnull=False),
            ),
        ),
    ]
