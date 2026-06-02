from django.contrib import admin

from .models import ContactSubmission


@admin.register(ContactSubmission)
class ContactSubmissionAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'company', 'service', 'budget', 'created_at')
    list_filter = ('service', 'budget', 'created_at')
    search_fields = ('name', 'email', 'company', 'message')
    date_hierarchy = 'created_at'
    ordering = ('-created_at',)
    readonly_fields = (
        'name', 'email', 'company', 'service', 'budget', 'message',
        'ip_address', 'user_agent', 'created_at',
    )

    # Leads are records: don't allow creating/editing them by hand in the admin.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
