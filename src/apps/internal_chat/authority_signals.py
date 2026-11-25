from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from rest_framework.exceptions import APIException

from apps.accounts.models import StaffProfile
from apps.core.plane import on_public_plane
from apps.rbac.models import Role

from . import core_bridge
from .realtime import notify_viewers_on_commit as invalidate_users

User = get_user_model()


def _enabled():
    if getattr(settings, 'TT_CHAT_CORE_BACKEND', 'legacy') != 'service' or on_public_plane():
        return False
    try:
        return core_bridge.enabled()
    except APIException:
        return False


@receiver(pre_save, sender=User, dispatch_uid='chat_authority_user_state_before')
def remember_user_state(sender, instance, update_fields=None, raw=False, **kwargs):
    if raw or not instance.pk or update_fields is not None and 'is_active' not in update_fields or not _enabled():
        return
    instance._chat_previous_active = sender.objects.filter(pk=instance.pk).values_list('is_active', flat=True).first()


@receiver(post_save, sender=User, dispatch_uid='chat_authority_user_state_after')
def user_state_changed(sender, instance, **kwargs):
    previous = instance.__dict__.pop('_chat_previous_active', None)
    if previous is not None and previous != instance.is_active:
        invalidate_users([instance.pk])


def _affected(sender, instance, reverse, pk_set):
    if sender is User.groups.through:
        return (list(pk_set) if pk_set is not None else list(instance.user_set.values_list('pk',flat=True))) if reverse else [instance.pk]
    if sender is Role.permissions.through:
        roles = Role.objects.filter(pk__in=pk_set) if reverse and pk_set is not None else (
            instance.roles.all() if reverse else Role.objects.filter(pk=instance.pk))
        return list(User.objects.filter(groups__role__in=roles).values_list('pk', flat=True).distinct())
    profiles = StaffProfile.objects.filter(pk__in=pk_set) if reverse and pk_set is not None else (
        StaffProfile.objects.filter(games=instance) if reverse else StaffProfile.objects.filter(pk=instance.pk))
    return list(profiles.values_list('user_id', flat=True))


@receiver(m2m_changed, sender=User.groups.through, dispatch_uid='chat_authority_user_groups')
@receiver(m2m_changed, sender=Role.permissions.through, dispatch_uid='chat_authority_role_nodes')
@receiver(m2m_changed, sender=StaffProfile.games.through, dispatch_uid='chat_authority_user_games')
def access_changed(sender, instance, action, reverse, pk_set=None, **kwargs):
    key = '_chat_authority_changed_' + sender._meta.db_table
    if action.startswith('pre_') and _enabled():
        setattr(instance, key, _affected(sender, instance, reverse, pk_set))
    elif action.startswith('post_'):
        users = instance.__dict__.pop(key, None)
        if users is not None:
            invalidate_users(users)


@receiver(pre_delete, sender=Role, dispatch_uid='chat_authority_role_delete_before')
@receiver(pre_delete, sender=Group, dispatch_uid='chat_authority_group_delete_before')
def remember_deleted_role_users(sender, instance, **kwargs):
    if _enabled():
        group = instance.group if sender is Role else instance
        instance._chat_deleted_role_users = list(group.user_set.values_list('pk',flat=True))


@receiver(post_delete, sender=Role, dispatch_uid='chat_authority_role_delete_after')
@receiver(post_delete, sender=Group, dispatch_uid='chat_authority_group_delete_after')
def deleted_role_users(sender, instance, **kwargs):
    users = instance.__dict__.pop('_chat_deleted_role_users', None)
    if users:
        invalidate_users(users)
