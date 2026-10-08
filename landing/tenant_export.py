"""Phase 7 -- full export of one tenant's data (portability / offboarding).

`write_export(client, fileobj)` writes a zip with one JSON file and one CSV
file per table into any binary file object (a file on disk for the
`export_tenant` command, a temporary file for the staff download). Rows are
streamed with .iterator() straight into the zip entries, so a large tenant
never has to fit in memory on the 512MB box.

What is deliberately NOT exported: Client.widget_token (live credential) and
install_notes (CMS access the client handed over), Stripe ids, lead IP /
user agent (server metadata the tenant never sees), the widget session ids,
and every DOMINIO cost figure (UsageEvent.cost_usd): the usage summary has
message and token counts only.
"""
import csv
import io
import json
import zipfile

from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import (
    AvailabilityRule, Booking, ChatMessage, ContactSubmission, Conversation,
    KnowledgeSource, LeadNote, Membership, Survey, UsageEvent,
)
from .platform_views import _csv_safe

FORMAT_VERSION = 1

# Explicit allowlist: a new secret field on Client is never exported by accident.
CLIENT_FIELDS = (
    'slug', 'name', 'system_prompt', 'greeting', 'notify_email', 'allowed_origins',
    'primary_color', 'surface_color', 'header_color', 'enable_bookings', 'clinic_mode',
    'is_active', 'daily_message_cap', 'notify_phone', 'notify_channel',
    'primary_language', 'retention_months', 'setup_status', 'website_url', 'platform',
    'live_at', 'created_at',
)


def _cell(value):
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def _tables(client):
    """(name, columns, row iterator factory) for every exported table. Each
    factory returns a fresh iterator: JSON and CSV are written in two passes
    because a zip entry must be finished before the next one starts."""
    leads = ContactSubmission.objects.filter(client=client).order_by('pk')
    convs = Conversation.objects.filter(client=client).order_by('pk')

    def values(qs, cols):
        return lambda: qs.values_list(*cols).iterator(chunk_size=2000)

    lead_cols = ('id', 'name', 'email', 'phone', 'company', 'service', 'budget',
                 'message', 'source', 'status', 'page_url', 'email_verified_at',
                 'created_at')
    note_cols = ('id', 'lead_id', 'author__username', 'text', 'created_at')
    conv_cols = ('id', 'lead_id', 'page_url', 'state', 'escalation_reason', 'intent',
                 'topic', 'first_question', 'tokens_used', 'config_version',
                 'started_at', 'last_message_at')
    msg_cols = ('conversation_id', 'position', 'role', 'content', 'created_at')
    booking_cols = ('id', 'lead_id', 'name', 'email', 'service', 'notes', 'start',
                    'duration_minutes', 'status', 'created_at')
    ks_cols = ('id', 'kind', 'title', 'origin', 'status', 'review_by', 'position',
               'replaces_id', 'content', 'created_at', 'updated_at')
    avail_cols = ('weekday', 'open_time', 'close_time', 'slot_minutes')
    member_cols = ('user__email', 'user__username', 'role', 'created_at')
    survey_cols = ('conversation_id', 'score', 'comment', 'created_at')

    def knowledge_rows():
        qs = (KnowledgeSource.objects.filter(client=client, status__in=('active', 'draft'))
              .order_by('position', 'pk'))
        for src in qs.iterator(chunk_size=200):
            row = [getattr(src, c) for c in ks_cols]
            # The text the agent actually uses (fragments when processed).
            row[ks_cols.index('content')] = src.compiled_text()
            yield tuple(row)

    def usage_rows():
        rows = (UsageEvent.objects.filter(client=client)
                .order_by().annotate(month=TruncMonth('created_at'))
                .values('month')
                .annotate(events=Count('id'), messages=Count('id', filter=Q(kind='chat')),
                          input=Sum('input_tokens'), output=Sum('output_tokens'),
                          cache_read=Sum('cache_read_tokens'),
                          cache_write=Sum('cache_write_tokens'))
                .order_by('month'))
        for r in rows:
            month = r['month']
            if timezone.is_aware(month):
                month = timezone.localtime(month)
            total = sum(r[k] or 0 for k in ('input', 'output', 'cache_read', 'cache_write'))
            yield (month.strftime('%Y-%m'), r['messages'], r['events'], r['input'] or 0,
                   r['output'] or 0, r['cache_read'] or 0, r['cache_write'] or 0, total)

    return [
        ('leads', lead_cols, values(leads, lead_cols)),
        ('lead_notes', ('id', 'lead_id', 'author', 'text', 'created_at'),
         values(LeadNote.objects.filter(lead__client=client).order_by('pk'), note_cols)),
        ('conversations', conv_cols, values(convs, conv_cols)),
        ('messages', msg_cols,
         values(ChatMessage.objects.filter(conversation__client=client)
                .order_by('conversation_id', 'position'), msg_cols)),
        ('bookings', booking_cols, values(Booking.objects.filter(client=client).order_by('pk'),
                                          booking_cols)),
        ('knowledge_sources', ks_cols, knowledge_rows),
        ('availability_rules', avail_cols,
         values(AvailabilityRule.objects.filter(client=client), avail_cols)),
        ('members', ('email', 'username', 'role', 'created_at'),
         values(Membership.objects.filter(client=client).order_by('pk'), member_cols)),
        ('surveys', survey_cols,
         values(Survey.objects.filter(conversation__client=client).order_by('pk'),
                survey_cols)),
        ('usage_monthly', ('month', 'messages', 'ai_calls', 'input_tokens', 'output_tokens',
                           'cache_read_tokens', 'cache_write_tokens', 'total_tokens'),
         usage_rows),
    ]


def filename_for(client, now=None):
    now = timezone.localtime(now or timezone.now())
    return f'{client.slug}-export-{now:%Y%m%d}.zip'


def write_export(client, fileobj):
    """Write the zip into `fileobj`; return {table: row count}."""
    counts = {}
    prefix = f'{client.slug}/'
    with zipfile.ZipFile(fileobj, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        config = {f: _cell(getattr(client, f)) for f in CLIENT_FIELDS}
        zf.writestr(prefix + 'client.json',
                    json.dumps(config, cls=DjangoJSONEncoder, ensure_ascii=False, indent=2))
        with zf.open(prefix + 'client.csv', 'w') as raw:
            out = io.TextIOWrapper(raw, encoding='utf-8-sig', newline='')
            writer = csv.writer(out)
            writer.writerow(['campo', 'valor'])
            for field, value in config.items():
                writer.writerow([_csv_safe(field), _csv_safe(value)])
            out.flush()
            out.detach()

        for name, cols, rows in _tables(client):
            n = 0
            with zf.open(f'{prefix}{name}.json', 'w') as raw:
                out = io.TextIOWrapper(raw, encoding='utf-8', newline='')
                out.write('[')
                for row in rows():
                    out.write(',\n' if n else '\n')
                    out.write(json.dumps(dict(zip(cols, (_cell(v) for v in row))),
                                         cls=DjangoJSONEncoder, ensure_ascii=False))
                    n += 1
                out.write('\n]\n')
                out.flush()
                out.detach()
            with zf.open(f'{prefix}{name}.csv', 'w') as raw:
                # BOM so Excel opens accents correctly (same as the leads CSV).
                out = io.TextIOWrapper(raw, encoding='utf-8-sig', newline='')
                writer = csv.writer(out)
                writer.writerow(cols)
                for row in rows():
                    writer.writerow([_csv_safe(_cell(v)) for v in row])
                out.flush()
                out.detach()
            counts[name] = n

        manifest = {
            'format_version': FORMAT_VERSION,
            'client': client.slug,
            'generated_at': timezone.now().isoformat(),
            'counts': counts,
            'excluido': ['credencial del widget', 'notas de instalación (accesos al CMS)',
                         'IP y navegador del visitante', 'id de sesión del widget',
                         'gastos de IA de DOMINIO'],
        }
        zf.writestr(prefix + 'manifest.json',
                    json.dumps(manifest, ensure_ascii=False, indent=2))
    return counts
