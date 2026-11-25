from django.apps import AppConfig


class InternalChatConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.internal_chat'

    def ready(self):
        from . import authority_signals, order_intents  # noqa: F401
