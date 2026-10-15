from django.contrib import admin

from .models import (
    AuditEvent, AvailabilityRule, Booking, Client, ContactSubmission,
    Conversation, KnowledgeSource, LeadNote, Location, Membership, StaffContact,
    Subscription, Survey, UsageEvent,
)


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'notify_email', 'setup_status', 'enable_bookings',
                    'is_active', 'widget_last_seen_at', 'created_at')
    list_filter = ('is_active', 'setup_status', 'enable_bookings', 'platform')
    search_fields = ('name', 'slug', 'notify_email')
    prepopulated_fields = {'slug': ('name',)}
    # Client-supplied CMS access notes are handled only from the Factory and
    # purged when the agent goes live; keep them out of the admin entirely.
    exclude = ('install_notes',)

    # Policy: clients are never deleted, only deactivated (is_active=False), so a
    # paused client can be brought back later with all its data intact.
    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'client', 'created_at')
    list_filter = ('client',)
    search_fields = ('user__username', 'user__email', 'client__name', 'client__slug')
    list_select_related = ('user', 'client')
    autocomplete_fields = ('client',)


class LocationScopedAdmin(admin.ModelAdmin):
    """Phase F: on the change form, the `location` choices are the client's
    own (the add form has no client yet; Model.clean() rejects a foreign
    location there)."""

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == 'location':
            object_id = (request.resolver_match.kwargs.get('object_id')
                         if request.resolver_match else None)
            instance = self.model.objects.filter(pk=object_id).first() if object_id else None
            if instance is not None:
                kwargs['queryset'] = Location.objects.filter(client_id=instance.client_id)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(Booking)
class BookingAdmin(LocationScopedAdmin):
    list_display = ('start', 'name', 'email', 'phone', 'service', 'location', 'status', 'client')
    list_filter = ('status', 'start', 'client')
    list_editable = ('status',)
    search_fields = ('name', 'email', 'phone')
    date_hierarchy = 'start'
    list_select_related = ('client', 'lead', 'location')


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    """Phase F: the branches of a multi-location business."""
    list_display = ('client', 'name', 'address', 'phone', 'is_default', 'order')
    list_filter = ('client',)
    search_fields = ('name', 'address', 'client__name', 'client__slug')
    list_select_related = ('client',)
    autocomplete_fields = ('client',)


@admin.register(StaffContact)
class StaffContactAdmin(LocationScopedAdmin):
    """Phase D: who receives the receptionist's messages per department."""
    list_display = ('client', 'name', 'department', 'channel', 'is_default', 'order')
    list_filter = ('client', 'channel', 'is_default')
    search_fields = ('name', 'department', 'client__name', 'client__slug')
    list_select_related = ('client',)
    autocomplete_fields = ('client',)


@admin.register(ContactSubmission)
class ContactSubmissionAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'company', 'service', 'budget', 'created_at')
    list_filter = ('service', 'budget', 'created_at')
    search_fields = ('name', 'email', 'company', 'message')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = (
        'name', 'email', 'company', 'service', 'budget', 'message',
        'page_url', 'ip_address', 'user_agent', 'created_at',
    )

    # Leads are records: don't allow creating/editing them by hand in the admin.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('client', 'plan', 'step', 'period', 'method', 'status', 'current_period_end')
    list_filter = ('status', 'plan', 'method')
    search_fields = ('client__name', 'client__slug', 'stripe_subscription_id',
                     'stripe_customer_id', 'checkout_session_id')


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ('client', 'state', 'lead', 'tokens_used', 'last_message_at')
    list_filter = ('state', 'client')
    search_fields = ('widget_session', 'lead__name', 'lead__email')
    date_hierarchy = 'started_at'

    def has_add_permission(self, request):
        return False


@admin.register(KnowledgeSource)
class KnowledgeSourceAdmin(admin.ModelAdmin):
    list_display = ('client', 'title', 'kind', 'audience', 'status', 'review_by', 'updated_at')
    list_filter = ('status', 'kind', 'audience', 'client')
    search_fields = ('title', 'origin', 'content')
    raw_id_fields = ('replaces',)  # a select would list every tenant's sources


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'user', 'client', 'action', 'target', 'result')
    list_filter = ('action',)
    search_fields = ('target', 'result', 'user__username')
    date_hierarchy = 'created_at'

    # The audit trail is append-only evidence.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Survey)
class SurveyAdmin(admin.ModelAdmin):
    list_display = ('conversation', 'score', 'created_at')
    list_filter = ('score',)


@admin.register(UsageEvent)
class UsageEventAdmin(admin.ModelAdmin):
    list_display = ('kind', 'client_slug', 'model', 'total_tokens', 'cost_usd', 'created_at')
    list_filter = ('kind', 'model')
    search_fields = ('client_slug',)
    date_hierarchy = 'created_at'

    # The cost ledger is written only by the code that made the API call, and is
    # never purged: history must survive for the monthly cost reports.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AvailabilityRule)
class AvailabilityRuleAdmin(LocationScopedAdmin):
    list_display = ('client', 'location', 'weekday', 'open_time', 'close_time')
    list_filter = ('client',)
    list_select_related = ('client', 'location')


@admin.register(LeadNote)
class LeadNoteAdmin(admin.ModelAdmin):
    """Internal lead notes: read-only here; they are written from the lead
    detail page (audited) and removed by delete/anonymize."""
    list_display = ('lead', 'author', 'created_at')
    list_select_related = ('lead', 'author')
    readonly_fields = ('lead', 'author', 'text', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
