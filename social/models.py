"""M-20 — the social publishing queue.

One row per thing to publish, on one platform. A Post is written as a `draft`
(by hand today, by the generator later), deliberately flipped to `approved` by
a human, and from that moment `publish_due` owns it: nothing else ever writes
to a row that has an `external_id`.

Media is referenced by URL and never uploaded from this server: all three
platforms PULL the file themselves, so `media_url` has to be reachable by
Meta's and TikTok's fetchers — a public/CDN/signed link, not a path behind our
login. That is also why publishing is a queue and not a request/response: the
platforms take seconds to minutes to ingest a file, and a gunicorn worker on a
512MB box must never be the thing waiting.
"""
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from landing.models import Client


class Post(models.Model):
    PLATFORM_CHOICES = [
        ('instagram', 'Instagram'),
        ('facebook', 'Facebook'),
        ('tiktok', 'TikTok'),
    ]
    KIND_CHOICES = [
        ('feed', 'Post de feed'),
        ('story', 'Story'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Borrador'),
        ('approved', 'Aprobado'),
        ('published', 'Publicado'),
        ('failed', 'Fallido'),
    ]
    # TikTok has no stories API: only photo posts to the feed. Kept as data so
    # both the admin form and the publisher reject the same combination.
    UNSUPPORTED = {('tiktok', 'story')}

    # Nullable on purpose: DOMINIO publishes its own marketing from here first,
    # and a tenant's posts get the FK later. Present from day one so going
    # multi-tenant is a settings change, not a data migration.
    client = models.ForeignKey(
        Client, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='social_posts',
        help_text='Negocio dueño del post. Vacío = cuentas propias de DOMINIO.')
    platform = models.CharField(max_length=12, choices=PLATFORM_CHOICES, db_index=True)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default='feed')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES,
                              default='draft', db_index=True)
    caption = models.TextField(
        blank=True,
        help_text='Texto del post. Instagram ignora el caption en stories (su API no lo acepta).')
    media_url = models.URLField(
        max_length=500,
        help_text='URL pública HTTPS de la imagen. La plataforma la descarga ella misma, '
                  'así que no puede estar detrás de login.')
    scheduled_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text='Cuándo publicar. Vacío = en la próxima corrida de publish_due tras aprobarlo.')

    # ---- Written only by publish_due; read-only everywhere else -------------
    # The presence of an external_id is what makes publishing idempotent: it is
    # the receipt, and the due-queue excludes any row that has one.
    external_id = models.CharField(
        max_length=120, blank=True, db_index=True,
        help_text='Id del post en la plataforma. Su presencia impide republicarlo.')
    error = models.CharField(max_length=300, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(
        null=True, blank=True, help_text='No reintentar antes de esta hora (backoff).')
    # Lease. A runner stamps this before it calls the API, so an overlapping
    # cron run cannot publish the same row twice. An OLD stamp counts as free:
    # a worker OOM-killed mid-publish would otherwise park the post forever,
    # which is the same failure the Stripe webhook ledger was bitten by.
    claimed_at = models.DateTimeField(null=True, blank=True)
    # Meta media container id / TikTok publish_id. Both APIs are two-phase
    # (ingest, then publish), so a retry resumes the existing upload instead of
    # sending the platform a second copy of the same image.
    remote_ref = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-scheduled_at', '-pk']
        indexes = [
            models.Index(fields=['status', 'scheduled_at'], name='social_post_due_idx'),
        ]

    def __str__(self):
        when = (timezone.localtime(self.scheduled_at).strftime('%Y-%m-%d %H:%M')
                if self.scheduled_at else 'sin fecha')
        return f'{self.get_platform_display()} · {self.get_kind_display()} · {when} ({self.status})'

    def clean(self):
        if (self.platform, self.kind) in self.UNSUPPORTED:
            raise ValidationError(
                {'kind': 'TikTok no tiene API de stories: solo posts de foto al feed.'})

    @classmethod
    def due(cls, now=None):
        """The rows `publish_due` may act on right now — the single definition
        of "vencido", shared by the command, the admin and the tests:
        approved, no receipt yet, past its scheduled time, past its backoff."""
        now = now or timezone.now()
        return cls.objects.filter(status='approved', external_id='').filter(
            models.Q(scheduled_at__isnull=True) | models.Q(scheduled_at__lte=now)
        ).filter(
            models.Q(next_attempt_at__isnull=True) | models.Q(next_attempt_at__lte=now)
        )
