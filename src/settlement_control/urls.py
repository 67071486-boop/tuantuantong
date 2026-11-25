from django.urls import path
from apps.settlement_control import views
from apps.settlement_control.health import health

urlpatterns = [
    path('healthz', health),
    path('authorizations', views.authorizations),
    path('authorizations/<uuid:request_id>/authorize', views.authorize),
    path('authorizations/<uuid:request_id>/refresh', views.refresh),
    path('authorizations/<uuid:request_id>/identity', views.identity),
    path('authorizations/<uuid:request_id>/identity-refresh', views.identity_refresh),
    path('authorizations/<uuid:request_id>/revoke', views.revoke),
]
