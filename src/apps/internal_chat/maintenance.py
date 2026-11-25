from django.conf import settings
from rest_framework.exceptions import APIException


class ChatMaintenanceUnavailable(APIException):
    status_code = 503
    default_detail = '内部沟通暂时不可用'


class ChatMaintenanceMixin:
    def initial(self, request, *args, **kwargs):
        if getattr(settings,'TT_CHAT_MAINTENANCE',False):
            raise ChatMaintenanceUnavailable()
        return super().initial(request,*args,**kwargs)
