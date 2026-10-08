"""M-10: apply each tenant's data-retention policy. Run daily from cron:

    python manage.py purge_expired [--dry-run]

Per client, with cutoff = now - retention_months (30-day months, 12 by
default), deletes:
  - conversations whose last message is older than the cutoff (messages and
    surveys go with them via CASCADE);
  - leads (ContactSubmission) created before the cutoff, EXCEPT source='signup'
    (that row is DOMINIO's own billing relationship with the client), plus any
    conversation linked to one of those leads — Conversation.lead is SET_NULL,
    so a recent transcript would otherwise outlive the lead it belongs to;
  - bookings whose `start` is before the cutoff.
Also drops webhook-idempotency rows older than 90 days, and clears the raw
extracted text of uploaded-file knowledge sources that ended in error or were
archived (ingest already does it at that moment; this catches older rows).
--dry-run only counts.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from landing.models import (
    Booking, Client, ContactSubmission, Conversation, KnowledgeSource, ProcessedWebhookEvent,
)


class Command(BaseCommand):
    help = ('Delete conversations, leads (except signup) and bookings past each '
            'client retention window (M-10).')

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be deleted without deleting.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        now = timezone.now()
        totals = {'conv': 0, 'leads': 0, 'bookings': 0}
        # None = legacy rows with no client: they get the default 12 months, so
        # the privacy policy's promise holds for them too.
        for client in [*Client.objects.all(), None]:
            months = (client.retention_months if client else 0) or 12
            cutoff = now - timezone.timedelta(days=months * 30)
            owner = {'client': client} if client else {'client__isnull': True}
            leads = (ContactSubmission.objects
                     .filter(created_at__lt=cutoff, **owner)
                     .exclude(source='signup'))
            stale_convs = (Q(client=client, last_message_at__lt=cutoff) if client
                           else Q(pk__in=[]))
            convs = Conversation.objects.filter(stale_convs | Q(lead__in=leads))
            bookings = Booking.objects.filter(start__lt=cutoff, **owner)
            n_conv, n_leads, n_book = convs.count(), leads.count(), bookings.count()
            if not (n_conv or n_leads or n_book):
                continue
            if not dry_run:
                # Conversations first: deleting the leads first would SET_NULL
                # the link and the linked transcripts would no longer match.
                with transaction.atomic():
                    convs.delete()
                    bookings.delete()
                    leads.delete()
            totals['conv'] += n_conv
            totals['leads'] += n_leads
            totals['bookings'] += n_book
            self.stdout.write(
                f'{client.slug if client else "(sin cliente)"}: {n_conv} conversaciones, {n_leads} leads, {n_book} citas '
                f'({">" if dry_run else ""}retención {months}m)')
        stale = ProcessedWebhookEvent.objects.filter(
            created_at__lt=now - timezone.timedelta(days=90))
        n_wh = stale.count()
        if n_wh and not dry_run:
            stale.delete()
        raw_files = (KnowledgeSource.objects
                     .filter(kind='file', status__in=('error', 'archived'))
                     .exclude(content=''))
        n_raw = raw_files.count()
        if n_raw and not dry_run:
            raw_files.update(content='')
        if n_raw:
            self.stdout.write(f'{"[dry-run] " if dry_run else ""}{n_raw} archivos de conocimiento '
                              f'con error/archivados: texto crudo borrado.')
        self.stdout.write(self.style.SUCCESS(
            f'{"[dry-run] " if dry_run else ""}'
            f'{totals["conv"]} conversaciones, {totals["leads"]} leads y '
            f'{totals["bookings"]} citas purgadas, {n_wh} eventos webhook viejos.'))
