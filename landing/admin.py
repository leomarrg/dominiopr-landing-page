from django.contrib import admin

from .models import (
    AuditEvent, AvailabilityRule, Booking, Client, ContactSubmission,
    Conversation, KnowledgeSource, Membership, Subscription, Survey,
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


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ('start', 'name', 'email', 'service', 'status', 'client')
    list_filter = ('status', 'start', 'client')
    list_editable = ('status',)
    search_fields = ('name', 'email')
    date_hierarchy = 'start'
    list_select_related = ('client', 'lead')


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
    list_display = ('client', 'plan', 'period', 'method', 'status', 'current_period_end')
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
    list_display = ('client', 'title', 'kind', 'status', 'review_by', 'updated_at')
    list_filter = ('status', 'kind', 'client')
    search_fields = ('title', 'origin', 'content')


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


admin.site.register(AvailabilityRule)
