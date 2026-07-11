from django.contrib import admin

from .models import Booking, Client, ContactSubmission, Membership


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'notify_email', 'enable_bookings', 'is_active', 'created_at')
    list_filter = ('is_active', 'enable_bookings')
    search_fields = ('name', 'slug', 'notify_email')
    prepopulated_fields = {'slug': ('name',)}

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
