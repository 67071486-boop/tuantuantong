from django.db.models import Count, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce

from apps.business.models import OrderSource


def sources_in_group(group_id):
    return OrderSource.objects.filter(Q(group_id=group_id) | Q(groups__pk=group_id))


def source_ids_in_group(group_id):
    # IN 去重由数据库处理，分组关系不会放大订单、绩效或店铺行。
    return sources_in_group(group_id).order_by().values("pk")


def group_source_count():
    counts = (
        sources_in_group(OuterRef("pk"))
        .order_by()
        .values(bucket=Value(1))
        .annotate(count=Count("pk", distinct=True))
        .values("count")
    )
    return Coalesce(Subquery(counts), 0)


def groups_for_source(source):
    groups = {group.pk: group for group in source.groups.all()}
    if source.group_id is not None:
        groups[source.group_id] = source.group
    return sorted(groups.values(), key=lambda group: (-group.order_by, group.pk))


def group_rows_for_source(source):
    return [{"id": group.pk, "name": group.name} for group in groups_for_source(source)]


def legacy_group_for_source(source):
    if source.group_id is not None:
        return source.group
    return next(iter(source.groups.all()), None)
