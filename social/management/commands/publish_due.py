"""M-20: publish the approved social posts whose time has come. Run from cron
every five minutes:

    python manage.py publish_due
    python manage.py publish_due --dry-run
    python manage.py publish_due --platform instagram --limit 5

Idempotency is layered, because each layer covers a failure the others do not:

1. `external_id` is the receipt. `Post.due()` excludes any row that has one, so
   a published post can never be picked up again no matter what re-runs.
2. A lease (`claimed_at`) is taken with a CONDITIONAL update before the API is
   touched, so two overlapping runs — cron firing again while a slow platform
   is still answering — cannot both publish the same row. The claim is a lease
   and not a permanent flag because a worker OOM-killed mid-publish (a real
   risk on a 512MB box) would otherwise park the post forever; an old stamp
   counts as free.
3. `remote_ref` holds the platform's half-finished upload (Instagram media
   container, Facebook unpublished photo, TikTok publish_id). A retry resumes
   that upload instead of sending a second copy of the same image, which is
   what "retry" would otherwise mean on a two-phase API.

Retries use exponential backoff in `next_attempt_at`, persisted rather than
slept, because the gap between attempts has to survive the process exiting.
Only transient failures are retried (see publishers._classify); a rejected
caption or an unreachable media_url goes straight to `failed` with the reason
in `error`, since retrying it 8 times only delays the human finding out.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from social import publishers
from social.models import Post

# How long a claim is honoured before another run may take the row back.
# Comfortably longer than the slowest publish (Instagram container polling)
# and far shorter than the cron gap being useless.
LEASE_MINUTES = 15


class Command(BaseCommand):
    help = 'Publish approved social posts whose scheduled_at has passed (M-20).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be published without calling any API.')
        parser.add_argument('--platform', choices=[p for p, _ in Post.PLATFORM_CHOICES],
                            help='Only this platform.')
        parser.add_argument('--limit', type=int, default=25,
                            help='Max posts per run (default 25).')

    def handle(self, *args, **options):
        now = timezone.now()
        max_attempts = getattr(settings, 'SOCIAL_MAX_ATTEMPTS', 8)

        qs = Post.due(now).select_related('client').order_by('scheduled_at', 'pk')
        if options['platform']:
            qs = qs.filter(platform=options['platform'])
        pending = list(qs[:options['limit']])

        if not pending:
            self.stdout.write('Nada vencido por publicar.')
            return

        published = pendiente = fallidos = 0
        for post in pending:
            label = f'#{post.pk} {post.platform}/{post.kind}'

            if options['dry_run']:
                enabled = 'ok' if publishers.enabled_for(post.platform) else 'SIN CREDENCIALES'
                self.stdout.write(f'[dry-run] {label} — {enabled} — {post.media_url}')
                continue

            if not self._claim(post, now):
                # Another runner has it. Not an error: say so and move on.
                self.stdout.write(f'{label}: en proceso por otra corrida, se salta.')
                continue

            try:
                external_id, remote_ref = publishers.publish(post)
            except publishers.PublishError as e:
                if e.retryable and post.attempts + 1 < max_attempts:
                    delay = self._backoff(post.attempts)
                    Post.objects.filter(pk=post.pk).update(
                        attempts=post.attempts + 1,
                        next_attempt_at=timezone.now() + timezone.timedelta(seconds=delay),
                        error=str(e)[:300], claimed_at=None)
                    self.stderr.write(f'{label}: fallo temporal ({e}); '
                                      f'reintento en {delay // 60}m '
                                      f'(intento {post.attempts + 1}/{max_attempts}).')
                else:
                    Post.objects.filter(pk=post.pk).update(
                        status='failed', attempts=post.attempts + 1,
                        error=str(e)[:300], claimed_at=None, next_attempt_at=None)
                    fallidos += 1
                    # Goes to stderr so cron mails it: a post that silently
                    # never went out is the failure nobody would notice.
                    self.stderr.write(self.style.ERROR(f'{label}: FALLIDO — {e}'))
                continue

            if external_id:
                Post.objects.filter(pk=post.pk).update(
                    status='published', external_id=external_id,
                    published_at=timezone.now(), remote_ref=remote_ref,
                    error='', claimed_at=None, next_attempt_at=None)
                published += 1
                self.stdout.write(self.style.SUCCESS(f'{label}: publicado ({external_id}).'))
            else:
                # Accepted but still ingesting. Keep the token, come back later.
                delay = self._backoff(post.attempts)
                Post.objects.filter(pk=post.pk).update(
                    remote_ref=remote_ref, attempts=post.attempts + 1,
                    next_attempt_at=timezone.now() + timezone.timedelta(seconds=delay),
                    error='', claimed_at=None)
                pendiente += 1
                self.stdout.write(f'{label}: la plataforma sigue procesando; '
                                  f'se revisa en {delay // 60}m.')

        if options['dry_run']:
            self.stdout.write(self.style.SUCCESS(
                f'[dry-run] {len(pending)} posts vencidos.'))
            return
        self.stdout.write(self.style.SUCCESS(
            f'{published} publicados, {pendiente} en proceso, {fallidos} fallidos.'))

    # ---- helpers --------------------------------------------------------

    def _claim(self, post, now):
        """Take the lease with a conditional UPDATE. Returns True only if THIS
        run won the row: the WHERE clause is what makes it a lock, so the
        rowcount — not a prior read — is the answer."""
        cutoff = now - timezone.timedelta(minutes=LEASE_MINUTES)
        claimed = Post.objects.filter(
            pk=post.pk, status='approved', external_id='',
        ).filter(
            # Free = never claimed, or claimed long enough ago that the
            # previous runner is presumed dead.
            Q(claimed_at__isnull=True) | Q(claimed_at__lt=cutoff)
        ).update(claimed_at=now)
        return bool(claimed)

    def _backoff(self, attempts):
        """1, 2, 4, 8 ... minutes, capped. Persisted in next_attempt_at, so the
        wait holds across process restarts instead of blocking a worker."""
        cap = getattr(settings, 'SOCIAL_BACKOFF_CAP_SECONDS', 1800)
        return min(60 * (2 ** attempts), cap)
