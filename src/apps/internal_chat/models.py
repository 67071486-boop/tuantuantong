from django.conf import settings
from django.db import models
from django.db.models import F, Q


class ContactDirectory(models.Model):
    primary_contact = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    backup_contact_ids = models.JSONField(default=list, blank=True)


class SystemNoticeSettings(models.Model):
    approval_submitted = models.BooleanField(default=True)
    approval_approved = models.BooleanField(default=True)
    wecom_aftersales = models.BooleanField(default=True)
    order_grab_qr = models.BooleanField(default=False)
    wecom_aftersales_resumed_at = models.DateTimeField(null=True, blank=True)
    order_grab_qr_resumed_at = models.DateTimeField(null=True, blank=True)


class Conversation(models.Model):
    first_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+", null=True)
    second_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+", null=True)
    system_recipient = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+", null=True, blank=True,
    )
    first_read_seq = models.PositiveBigIntegerField(default=0)
    second_read_seq = models.PositiveBigIntegerField(default=0)
    latest_seq = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["first_user", "second_user"], name="chat_direct_pair_unique"),
            models.CheckConstraint(
                condition=Q(system_recipient__isnull=True) | Q(first_user__isnull=True, second_user__isnull=True),
                name="chat_system_conversation_without_peers",
            ),
            models.CheckConstraint(
                condition=(Q(first_user_id__lt=F("second_user_id"))
                           & Q(first_user__isnull=False, second_user__isnull=False))
                | Q(first_user__isnull=True, second_user__isnull=True), name="chat_direct_pair_canonical",
            ),
        ]


class ConversationPreference(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    pinned = models.BooleanField(default=False)
    remark = models.CharField(max_length=30, blank=True, default="")
    marked_unread = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["conversation", "user"], name="chat_preference_user_unique"),
        ]


class OrderGroup(models.Model):
    conversation = models.OneToOneField(Conversation, on_delete=models.CASCADE, related_name="order_group")
    order = models.OneToOneField("business.Order", null=True, on_delete=models.SET_NULL)
    wire_id = models.UUIDField(unique=True)
    order_no = models.CharField(max_length=20)
    binding_role = models.CharField(max_length=10, blank=True)
    binding_id = models.PositiveBigIntegerField(null=True)
    roster_version = models.PositiveBigIntegerField(default=0)
    source_roster_version = models.PositiveBigIntegerField(default=0)
    pinned_message_wire_id = models.UUIDField(null=True)
    pin_revision = models.PositiveBigIntegerField(default=0)
    pin_operation_id = models.UUIDField(null=True)


class OrderMemberRole(models.TextChoices):
    WORKER = "worker"
    ACCEPTOR = "acceptor"
    PUBLISHER = "publisher"
    FOLLOWER = "follower"


class GroupMember(models.Model):
    group = models.ForeignKey(OrderGroup, on_delete=models.CASCADE, related_name="members")
    wire_key = models.CharField(max_length=24)
    local_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=150)
    studio_name = models.CharField(max_length=100, blank=True, default="")
    remote_order_role = models.CharField(max_length=9, choices=OrderMemberRole, blank=True, default="")
    publisher = models.BooleanField(default=False)
    acceptor = models.BooleanField(default=False)
    worker = models.BooleanField(default=False)
    follower = models.BooleanField(default=False)
    designated_follower = models.BooleanField(default=False)
    manual_joined = models.BooleanField(default=False)
    left_at = models.DateTimeField(null=True)
    read_seq = models.PositiveBigIntegerField(default=0)

    @property
    def order_role(self):
        if self.worker:
            return OrderMemberRole.WORKER
        if self.acceptor:
            return OrderMemberRole.ACCEPTOR
        if self.publisher:
            return OrderMemberRole.PUBLISHER
        if self.follower:
            return OrderMemberRole.FOLLOWER
        return self.remote_order_role

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["group", "wire_key"], name="chat_group_wire_member_unique"),
            models.UniqueConstraint(fields=["group", "local_user"], name="chat_group_local_member_unique"),
        ]


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+", null=True)
    group_sender = models.ForeignKey(GroupMember, null=True, on_delete=models.PROTECT, related_name="+")
    wire_id = models.UUIDField(null=True, unique=True)
    seq = models.PositiveBigIntegerField()
    client_id = models.CharField(max_length=100)
    album_id = models.UUIDField(null=True, db_index=True)
    text = models.TextField(max_length=4000)
    system_order_no = models.CharField(max_length=20, blank=True, default="", db_default="")
    mention_member_ids = models.JSONField(default=list, blank=True)
    image_metadata = models.JSONField(null=True, blank=True)
    image_data = models.BinaryField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked = models.BooleanField(default=False)
    reply_to = models.ForeignKey("self", null=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["conversation", "seq"], name="chat_message_sequence_unique"),
            models.UniqueConstraint(
                fields=["conversation", "client_id"], condition=~Q(system_order_no=""), name="chat_system_notice_once",
            ),
            models.CheckConstraint(
                condition=Q(system_order_no="") | Q(sender__isnull=True, group_sender__isnull=True),
                name="chat_system_message_without_sender",
            ),
            models.UniqueConstraint(
                fields=["conversation", "sender", "client_id"], name="chat_message_send_unique",
            ),
            models.UniqueConstraint(
                fields=["conversation", "group_sender", "client_id"], name="chat_group_message_send_unique",
            ),
        ]

    def revoke_content(self):
        self.revoked = True
        self.text = ""
        self.mention_member_ids = []
        self.image_metadata = self.image_data = None
        self.save(update_fields=["revoked", "text", "mention_member_ids", "image_metadata", "image_data"])


class ChangeCursor(models.Model):
    latest = models.PositiveBigIntegerField(default=0)


class Change(models.Model):
    class Kind(models.TextChoices):
        CONVERSATION_CREATED = "conversation.created"
        MESSAGE_CREATED = "message.created"
        MESSAGE_REVOKED = "message.revoked"
        CONVERSATION_READ = "conversation.read"
        MEMBERS_CHANGED = "conversation.members_changed"
        CONVERSATION_REMOVED = "conversation.removed"
        PREFERENCES_CHANGED = "conversation.preferences_changed"
        PIN_CHANGED = "conversation.pin_changed"

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="changes")
    kind = models.CharField(max_length=32, choices=Kind)
    viewer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE)
    message = models.ForeignKey(Message, null=True, on_delete=models.CASCADE, related_name="+")


class OrderChatIntent(models.Model):
    order_no = models.CharField(max_length=20, unique=True)
    revision = models.PositiveBigIntegerField(default=1)
    acked_revision = models.PositiveBigIntegerField(default=0)
    ensure_group = models.BooleanField(default=False)
    last_deleted_at = models.DateTimeField(null=True,blank=True)
    last_completed_at = models.DateTimeField(null=True,blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class OrderChatPeerLink(models.Model):
    group_id = models.UUIDField(primary_key=True, editable=False)
    order = models.ForeignKey("business.Order", on_delete=models.CASCADE)
    generation = models.PositiveBigIntegerField(default=1)
    upstream_binding = models.ForeignKey("business.PeerUpstreamBinding", null=True, on_delete=models.PROTECT)
    downstream_binding = models.ForeignKey("business.PeerDownstreamBinding", null=True, on_delete=models.PROTECT)
    binding_token_hash = models.CharField(max_length=64)
    order_no = models.CharField(max_length=20)
    publisher_name = models.CharField(max_length=150)
    publisher_studio_name = models.CharField(max_length=100, blank=True, default="")
    publisher = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    acceptor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    detached_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(upstream_binding__isnull=False, downstream_binding__isnull=True)
                | Q(upstream_binding__isnull=True, downstream_binding__isnull=False),
                name="chat_peer_link_one_binding",
            ),
            models.UniqueConstraint(fields=["order", "generation"], name="chat_peer_order_generation"),
            models.UniqueConstraint(
                fields=["order"], condition=Q(detached_at__isnull=True), name="chat_peer_order_active_link",
            ),
        ]


class OrderChatPeerBinding(models.Model):
    role = models.CharField(max_length=10)
    binding_id = models.PositiveBigIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["role", "binding_id"], name="chat_peer_core_binding"),
        ]


class VisitorReceptionSettings(models.Model):
    contact_ids = models.JSONField(default=list, blank=True)
    greeting_sender = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    greeting_text = models.CharField(max_length=4000, blank=True, default="")
    # NULL preserves the single-sender configuration until it is edited.
    greetings = models.JSONField(null=True, blank=True)
    seats = models.JSONField(null=True, blank=True)
    seat_rotation = models.JSONField(default=dict, blank=True)


class VisitorDeskPresence(models.Model):
    staff = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True)
    enabled = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(null=True)


class VisitorIdentity(models.Model):
    token_digest = models.CharField(max_length=64, unique=True)
    phone = models.CharField(max_length=11)
    registration_fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["registration_fingerprint", "created_at"], name="visitor_registration_window")]


class VisitorConversation(models.Model):
    class Seat(models.TextChoices):
        HR = "hr", "人事客服"
        ORDERS = "orders", "订单客服"

    class ServiceStatus(models.TextChoices):
        NEW = "new", "未发起"
        WAITING = "waiting", "待接待"
        ASSIGNED = "assigned", "接待中"
        CLOSED = "closed", "已结束"

    visitor = models.ForeignKey(VisitorIdentity, on_delete=models.CASCADE, related_name="conversations")
    staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE, related_name="+")
    seat_kind = models.CharField(max_length=6, choices=Seat.choices, blank=True, default="")
    service_status = models.CharField(max_length=8, choices=ServiceStatus.choices, default=ServiceStatus.NEW)
    assigned_staff = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    queued_at = models.DateTimeField(null=True)
    latest_seq = models.PositiveBigIntegerField(default=0)
    visitor_read_seq = models.PositiveBigIntegerField(default=0)
    staff_read_seq = models.PositiveBigIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["visitor", "staff"], name="visitor_staff_conversation"),
            models.UniqueConstraint(
                fields=["visitor", "seat_kind"], condition=~Q(seat_kind=""), name="visitor_seat_conversation",
            ),
        ]
        indexes = [
            models.Index(fields=["seat_kind", "service_status", "queued_at"], name="visitor_seat_queue"),
            models.Index(fields=["seat_kind", "service_status", "assigned_staff"], name="visitor_seat_caseload"),
        ]


class VisitorMessage(models.Model):
    conversation = models.ForeignKey(VisitorConversation, on_delete=models.CASCADE, related_name="messages")
    seq = models.PositiveBigIntegerField()
    sender_is_guest = models.BooleanField()
    client_id = models.CharField(max_length=100)
    text = models.CharField(max_length=4000)
    album_id = models.UUIDField(null=True, db_index=True)
    image_metadata = models.JSONField(null=True, blank=True)
    image_data = models.BinaryField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reply_to = models.ForeignKey("self", null=True, on_delete=models.SET_NULL, related_name="+")
    staff_sender_id = models.PositiveBigIntegerField(null=True)
    staff_sender_name = models.CharField(max_length=150, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["conversation", "seq"], name="visitor_message_sequence"),
            models.UniqueConstraint(
                fields=["conversation", "sender_is_guest", "client_id"], name="visitor_message_idempotency",
            ),
        ]
        indexes = [models.Index(fields=["conversation", "sender_is_guest", "created_at"], name="visitor_message_send_window")]


class VisitorChange(models.Model):
    conversation = models.ForeignKey(VisitorConversation, on_delete=models.CASCADE)
    message = models.OneToOneField(VisitorMessage, null=True, on_delete=models.CASCADE)
    former_staff_id = models.PositiveBigIntegerField(null=True, db_index=True)
    transferred_staff_id = models.PositiveBigIntegerField(null=True)
    transferred_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [models.Index(fields=["conversation", "id"], name="visitor_change_conversation")]


class OrderGrabQrNotice(models.Model):
    order = models.ForeignKey("business.Order", on_delete=models.CASCADE)
    worker = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    claimed_at = models.DateTimeField()
    next_attempt_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["order", "worker", "claimed_at"], name="chat_grab_qr_notice_once",
        )]
        indexes = [models.Index(
            fields=["next_attempt_at", "id"], condition=Q(finished_at__isnull=True), name="chat_grab_qr_notice_due",
        )]
