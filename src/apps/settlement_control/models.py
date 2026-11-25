from django.db import models


class Nonce(models.Model):
    client = models.CharField(max_length=128)
    nonce = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('client', 'nonce'), name='sc_nonce_unique')]


class UpstreamSession(models.Model):
    key = models.CharField(primary_key=True, max_length=16, default='agent')
    token = models.BinaryField(null=True)
    attempted_at = models.DateTimeField(null=True)
    token_created_at = models.DateTimeField(null=True)
    login_attempt = models.UUIDField(null=True)


class RateWindow(models.Model):
    client = models.CharField(max_length=128)
    minute = models.BigIntegerField()
    count = models.PositiveIntegerField(default=0)
    rejected_logged = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('client', 'minute'), name='sc_rate_unique')]


class SecurityEvent(models.Model):
    client = models.CharField(max_length=128)
    action = models.CharField(max_length=32)
    result = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class ManualReview(models.Model):
    client = models.CharField(max_length=128)
    request_id = models.UUIDField()
    operator = models.CharField(max_length=128)
    process_user = models.CharField(max_length=128)
    decision = models.CharField(max_length=32)
    evidence_ciphertext = models.BinaryField()
    evidence_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class Authorization(models.Model):
    client = models.CharField(max_length=128)
    request_id = models.UUIDField()
    payload_hash = models.CharField(max_length=64)
    ciphertext = models.BinaryField()
    app_id = models.CharField(max_length=128, blank=True)
    app_name = models.CharField(max_length=128)
    phase = models.CharField(max_length=32, default='new')
    status = models.CharField(max_length=16, default='pending')
    next_action = models.CharField(max_length=16, default='agreement')
    unknown_action = models.CharField(max_length=32, blank=True)
    review_code = models.CharField(max_length=32, blank=True)
    configured = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=('client', 'request_id'), name='sc_auth_unique')]
