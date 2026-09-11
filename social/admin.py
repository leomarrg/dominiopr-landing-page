from django.contrib import admin, messages
from django.utils.html import format_html

from landing.models import AuditEvent

from .models import Post


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ('thumbnail', 'scheduled_at', 'platform', 'kind', 'status',
                    'excerpt', 'client', 'published_at')
    list_display_links = ('scheduled_at', 'excerpt')
    list_filter = ('status', 'platform', 'kind', 'client')
    search_fields = ('caption', 'external_id', 'media_url', 'client__name')
    date_hierarchy = 'scheduled_at'
    autocomplete_fields = ('client',)
    actions = ['aprobar']

    # Everything publish_due owns. Editing any of it by hand would either
    # re-publish a live post or hide a real failure.
    readonly_fields = ('preview', 'external_id', 'published_at', 'attempts',
                       'next_attempt_at', 'claimed_at', 'remote_ref', 'error',
                       'created_at', 'updated_at')
    fieldsets = (
        (None, {'fields': ('client', 'platform', 'kind', 'status')}),
        ('Contenido', {'fields': ('caption', 'media_url', 'preview', 'scheduled_at')}),
        ('Publicación (automático)', {
            'classes': ('collapse',),
            'fields': ('external_id', 'published_at', 'error', 'attempts',
                       'next_attempt_at', 'claimed_at', 'remote_ref',
                       'created_at', 'updated_at'),
        }),
    )

    # ---- columns --------------------------------------------------------

    @admin.display(description='')
    def thumbnail(self, obj):
        if not obj.media_url:
            return '—'
        return format_html(
            '<img src="{}" alt="" loading="lazy" '
            'style="height:56px;width:56px;object-fit:cover;border-radius:6px;'
            'background:#eee">', obj.media_url)

    @admin.display(description='Caption')
    def excerpt(self, obj):
        text = (obj.caption or '').strip().replace('\n', ' ')
        return (text[:70] + '…') if len(text) > 70 else (text or '(sin texto)')

    @admin.display(description='Vista previa')
    def preview(self, obj):
        if not obj.media_url:
            return '—'
        return format_html(
            '<a href="{0}" target="_blank" rel="noopener">'
            '<img src="{0}" alt="" style="max-height:320px;max-width:100%;'
            'border-radius:8px"></a>', obj.media_url)

    # ---- actions --------------------------------------------------------

    @admin.action(description='Aprobar los seleccionados (publish_due los publicará)')
    def aprobar(self, request, queryset):
        """Approval is the moment a human takes responsibility for something
        going out publicly, so it is deliberately narrow:

        * an already-published post is never touched — that would re-publish it;
        * a retry of a failed post starts from a clean slate (attempts, backoff
          and error reset), otherwise it inherits an exhausted counter and
          publish_due marks it failed again without ever calling the API;
        * `remote_ref` is cleared too: a stale Instagram container expires
          after 24h, and resuming a dead one fails forever.
        """
        skipped = queryset.filter(status='published').count()
        unsupported = [p for p in queryset if (p.platform, p.kind) in Post.UNSUPPORTED]
        # Materialise before the update: afterwards the queryset would be
        # re-evaluated against the new status and the audit trail would drift.
        approvable = list(queryset.exclude(status='published').exclude(
            pk__in=[p.pk for p in unsupported]))

        updated = Post.objects.filter(pk__in=[p.pk for p in approvable]).update(
            status='approved', error='', attempts=0, next_attempt_at=None,
            claimed_at=None, remote_ref='')
        for post in approvable:
            AuditEvent.objects.create(
                client=post.client, user=request.user, action='social.approve',
                target=f'post:{post.pk} {post.platform}/{post.kind}',
                result=(post.scheduled_at.isoformat() if post.scheduled_at
                        else 'sin fecha — sale en la próxima corrida'))

        self.message_user(request, f'{updated} post(s) aprobados.', messages.SUCCESS)
        if skipped:
            self.message_user(
                request, f'{skipped} ya estaban publicados y no se tocaron.',
                messages.WARNING)
        if unsupported:
            self.message_user(
                request,
                f'{len(unsupported)} no se aprobaron: TikTok no soporta stories.',
                messages.ERROR)

    # Published posts are the record of what actually went out; deleting the row
    # does not delete the post on the platform, it only loses the receipt.
    def has_delete_permission(self, request, obj=None):
        return not (obj and obj.status == 'published')
