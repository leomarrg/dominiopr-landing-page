"""CRM: the morning follow-up digest. Run once a day from cron, at 8:00 a.m.
Puerto Rico (the cron line picks the hour; see DEPLOYMENT.md):

    python manage.py send_followup_digest

One email per (member, client) to every org_admin/agent whose
Membership.daily_digest is on, with the client's overdue follow-ups, the
ones due today and the leads still 'new' after 24 h. Nothing is sent when
there is nothing pending. The body carries lead names and links to the lead
card -- never emails, phones or messages.

Idempotent per day: each send is recorded as an AuditEvent
('digest.followup', client, target=YYYY-MM-DD, result=user:<pk>), so a
second run the same day (a retried cron, a manual run) sends nothing.
--force re-sends, --dry-run only prints, --date YYYY-MM-DD picks the day.
"""
from datetime import date, datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils import timezone

from landing import analytics
from landing.models import AuditEvent, ContactSubmission, Membership
from landing.views import _send_html_email

DIGEST_ACTION = 'digest.followup'
# Per section, per email: a digest is a nudge, not a report.
SECTION_LIMIT = 50


class Command(BaseCommand):
    help = 'Email each org_admin/agent the overdue/today follow-ups and stale new leads (CRM).'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='List what would be sent without sending or recording.')
        parser.add_argument('--force', action='store_true',
                            help='Send even if a digest was already recorded for the day.')
        parser.add_argument('--date', default='',
                            help='Day to report as YYYY-MM-DD (default: today in Puerto Rico).')

    def handle(self, *args, **options):
        tz = analytics.BUSINESS_TZ
        if options['date']:
            try:
                day = date.fromisoformat(options['date'])
            except ValueError:
                raise CommandError('--date debe ser YYYY-MM-DD')
        else:
            day = timezone.localtime(timezone.now(), tz).date()
        day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        stale_before = timezone.now() - timedelta(hours=analytics.STALE_NEW_HOURS)
        dry, force = options['dry_run'], options['force']

        memberships = (Membership.objects
                       .filter(role__in=Membership.DASHBOARD_ROLES, daily_digest=True,
                               user__is_active=True, client__is_active=True)
                       .exclude(user__email='')
                       .select_related('user', 'client')
                       .order_by('client_id', 'user_id'))
        sent = skipped = failed = empty = 0
        pending_by_client = {}
        for m in memberships:
            client = m.client
            if client.pk not in pending_by_client:
                pending_by_client[client.pk] = self._pending(client, day_start, day_end,
                                                             stale_before)
            pending = pending_by_client[client.pk]
            if pending is None:
                empty += 1
                continue
            user = m.user
            marker = f'user:{user.pk}'
            already = AuditEvent.objects.filter(
                action=DIGEST_ACTION, client=client, target=day.isoformat(), result=marker).exists()
            if already and not force:
                skipped += 1
                continue
            subject = (f'Seguimientos de hoy · {client.name}: '
                       f'{len(pending["overdue"])} vencidos, {len(pending["today"])} para hoy, '
                       f'{len(pending["stale"])} leads nuevos sin atender')
            if dry:
                self.stdout.write(f'[dry-run] {client.slug} -> user:{user.pk}: {subject}')
                continue
            context = dict(pending, business=client.name, day=day,
                           stale_hours=analytics.STALE_NEW_HOURS,
                           leads_url=settings.SITE_URL + reverse('dashboard')
                           + f'?client={client.slug}&follow_up=overdue')
            ok = _send_html_email(
                subject=subject, to=[user.email],
                html_template='landing/emails/followup_digest.html',
                txt_template='landing/emails/followup_digest.txt',
                context=context,
            )
            if ok:
                AuditEvent.objects.create(client=client, user=None, action=DIGEST_ACTION,
                                          target=day.isoformat(), result=marker)
                sent += 1
            else:
                failed += 1
        self.stdout.write(self.style.SUCCESS(
            f'{sent} resúmenes enviados, {skipped} ya enviados hoy, {failed} fallidos, '
            f'{empty} sin pendientes.'))
        if failed:
            raise CommandError(f'{failed} resúmenes no se pudieron enviar.')

    @staticmethod
    def _pending(client, day_start, day_end, stale_before):
        """{'overdue', 'today', 'stale'} item lists for one client, or None
        when nothing is pending. Three queries."""
        base = ContactSubmission.objects.filter(client=client)
        open_fu = dict(follow_up_at__isnull=False, follow_up_done_at__isnull=True)
        overdue = list(base.filter(follow_up_at__lt=day_start, **open_fu)
                       .select_related('assigned_to').order_by('follow_up_at')[:SECTION_LIMIT])
        today = list(base.filter(follow_up_at__gte=day_start, follow_up_at__lt=day_end, **open_fu)
                     .select_related('assigned_to').order_by('follow_up_at')[:SECTION_LIMIT])
        stale = list(base.filter(status='new', created_at__lt=stale_before)
                     .order_by('created_at')[:SECTION_LIMIT])
        if not (overdue or today or stale):
            return None

        def item(lead, when):
            return {
                'name': lead.name,
                'when': timezone.localtime(when, analytics.BUSINESS_TZ),
                'assigned': lead.assigned_to.username if lead.assigned_to_id else '',
                'url': settings.SITE_URL + reverse('lead_detail', args=[lead.pk]),
            }

        return {
            'overdue': [item(lead, lead.follow_up_at) for lead in overdue],
            'today': [item(lead, lead.follow_up_at) for lead in today],
            'stale': [item(lead, lead.created_at) for lead in stale],
        }
