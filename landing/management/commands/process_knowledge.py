"""Background knowledge ingest (Phase 8). Run from cron every minute:

    python manage.py process_knowledge [--limit 3]

Picks pending KnowledgeSource rows (kind 'site' = website scan, 'file' =
uploaded document, 'url' = one page, fetched without the model), oldest first, claims each one atomically (two overlapping
runs never process the same row; a run lock also keeps a second run from
starting at all), crawls/reads it, distills it with the model and leaves the
result as a DRAFT for the tenant to test and activate. Never activates.

    python manage.py process_knowledge --scan-site dominio https://dominiopr.com

enqueues a website scan for a client from the CLI (any public host allowed,
it is the operator) and processes it in the same run.

Rows stuck in 'processing' for 30+ minutes are retried once, then marked error
(only by the run holding the lock). Run it under `timeout 900` in cron (see
DEPLOYMENT.md): every fetch and model call is bounded, this is the backstop.
"""
import os

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError

from landing import ingest
from landing.models import Client

LOCK_KEY = 'lock:process_knowledge'
LOCK_TTL = 30 * 60  # outlives one run (3 sources x ~3 min); expires if a run is killed


class Command(BaseCommand):
    help = 'Process queued website scans and uploaded files into draft knowledge.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=3,
                            help='Max sources per run (default 3).')
        parser.add_argument('--scan-site', nargs=2, metavar=('SLUG', 'URL'),
                            help='Enqueue a website scan for this client, then process.')

    def handle(self, *args, **options):
        if options.get('scan_site'):
            self._enqueue(*options['scan_site'])

        # Read-only checks before the lock: resetting stale rows here, outside
        # it, could pull a row out from under a run that is still alive.
        if not ingest.pending_ids(1) and not ingest.has_stale():
            # Cron runs this every minute so a scan starts within ~60 s of being
            # queued; an idle pass stays silent so the log only shows real work.
            if options.get('verbosity', 1) >= 2:
                self.stdout.write('Conocimiento: no hay fuentes en cola.')
            return
        kinds = ingest.BACKGROUND_KINDS
        if not getattr(settings, 'ANTHROPIC_API_KEY', ''):
            # Single-page URLs need no model: they still get processed.
            if not ingest.pending_ids(1, ingest.NO_MODEL_KINDS):
                raise CommandError('Falta ANTHROPIC_API_KEY: las fuentes siguen en cola.')
            self.stderr.write('Falta ANTHROPIC_API_KEY: solo se procesan las páginas (URL).')
            kinds = ingest.NO_MODEL_KINDS
        if not cache.add(LOCK_KEY, os.getpid(), LOCK_TTL):
            self.stdout.write('Conocimiento: otra corrida está en curso; nada que hacer.')
            return
        try:
            # process_pending() runs reset_stale() first, under the lock.
            results = ingest.process_pending(limit=max(0, options['limit']), kinds=kinds)
        finally:
            cache.delete(LOCK_KEY)
        for source, status in results:
            self.stdout.write(f'  #{source.pk} {source.client.slug} {source.kind} -> {status}')
        self.stdout.write(f'Conocimiento: {len(results)} fuentes procesadas.')

    def _enqueue(self, slug, url):
        client = Client.objects.filter(slug=slug).first()
        if client is None:
            raise CommandError(f'No existe un cliente con slug "{slug}".')
        try:
            clean = ingest.validate_scan(client, url, any_host=True)
            source = ingest.enqueue_site_scan(client, clean)
        except ValueError as exc:
            raise CommandError(str(exc))
        self.stdout.write(f'Escaneo en cola: #{source.pk} {clean} ({client.slug}).')
