from django.contrib import admin

from .models import InboundMessage, ReceiptAttachment


@admin.register(InboundMessage)
class InboundMessageAdmin(admin.ModelAdmin):
    """Read-only diagnostics. Not a processing queue: no add, edit or delete."""

    list_display = (
        "received_at",
        "provider_message_id",
        "instance",
        "message_type",
        "status",
        "intent",
        "ignore_reason",
        "error_code",
        "user",
    )
    list_filter = ("status", "intent", "message_type", "ignore_reason", "instance")
    search_fields = ("provider_message_id", "sender_phone", "user__username")
    list_select_related = ("user", "transaction")
    date_hierarchy = "received_at"
    readonly_fields = [field.name for field in InboundMessage._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReceiptAttachment)
class ReceiptAttachmentAdmin(admin.ModelAdmin):
    """Metadata only (no image, no link, no signed URL). Read-only."""

    list_display = (
        "created_at",
        "status",
        "document_type",
        "content_type",
        "byte_size",
        "transaction",
        "error_code",
    )
    list_filter = ("status", "document_type", "content_type")
    search_fields = ("id", "sha256", "inbound_message__provider_message_id")
    list_select_related = ("transaction", "inbound_message")
    readonly_fields = [field.name for field in ReceiptAttachment._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
