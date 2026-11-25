from django.utils import timezone

from .models import SystemNoticeSettings

NOTICE_RULES = {
    "approval_submitted": "提交审批",
    "approval_approved": "审批通过",
    "wecom_aftersales": "企微售后纠纷",
    "order_grab_qr": "接单群二维码",
}


def _settings_for_tenant():
    return SystemNoticeSettings.objects.filter(pk=1).first() or SystemNoticeSettings(pk=1)


def _payload(config):
    return {rule: getattr(config, rule) for rule in NOTICE_RULES}


def system_notice_settings():
    return _payload(_settings_for_tenant())


def system_notice_enabled(rule):
    if rule not in NOTICE_RULES:
        raise KeyError(rule)
    value = SystemNoticeSettings.objects.filter(pk=1).values_list(rule, flat=True).first()
    return getattr(SystemNoticeSettings(), rule) if value is None else value


def wecom_notice_window():
    config = _settings_for_tenant()
    return config.wecom_aftersales, config.wecom_aftersales_resumed_at


def order_grab_qr_window():
    config = _settings_for_tenant()
    return config.order_grab_qr, config.order_grab_qr_resumed_at


def update_system_notice_settings(values):
    config = _settings_for_tenant()
    changed = [rule for rule, value in values.items() if getattr(config, rule) != value]
    if changed:
        for rule in changed:
            setattr(config, rule, values[rule])
        if "wecom_aftersales" in changed and config.wecom_aftersales:
            config.wecom_aftersales_resumed_at = timezone.now()
        if "order_grab_qr" in changed and config.order_grab_qr:
            config.order_grab_qr_resumed_at = timezone.now()
        config.save()
    return _payload(config), [NOTICE_RULES[rule] for rule in changed]
