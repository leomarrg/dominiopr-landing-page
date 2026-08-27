"""M-10: apply each tenant's data-retention policy. Run daily from cron:

    python manage.py purge_expired

Deletes conversations (and their messages, via CASCADE) older than the
client's `retention_months`, plus stale webhook-idempotency rows. Leads are
kept while the service is active (they're the client's CRM).
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from landing.models import Client, Conversation, ProcessedWebhookEvent


class Command(BaseCommand):
    help = 'Delete conversations past each client retention window (M-10).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be deleted without deleting.')

    def handle(self, *args, **options):
        now = timezone.now()
        total = 0
        for client in Client.objects.all():
            months = client.retention_months or 12
            cutoff = now - timezone.timedelta(days=months * 30)
            qs = Conversation.objects.filter(client=client, last_message_at__lt=cutoff)
            n = qs.count()
            if n:
                if not options['dry_run']:
                    qs.delete()
                total += n
                self.stdout.write(f'{client.slug}: {n} conversaciones '
                                  f'({">" if options["dry_run"] else ""}retención {months}m)')
        stale = ProcessedWebhookEvent.objects.filter(
            created_at__lt=now - timezone.timedelta(days=90))
        n_wh = stale.count()
        if n_wh and not options['dry_run']:
            stale.delete()
        self.stdout.write(self.style.SUCCESS(
            f'{"[dry-run] " if options["dry_run"] else ""}'
            f'{total} conversaciones purgadas, {n_wh} eventos webhook viejos.'))
