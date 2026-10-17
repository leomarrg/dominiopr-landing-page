"""Hourly spend alarm. Run from cron (through deploy/scripts/cron_run.sh):

    python manage.py cost_alert [--dry-run]

Two thresholds, each mailed to ADMINS at most ONCE per day:

* AI spend today (sum of UsageEvent.cost_usd since local midnight, every kind:
  chat, WhatsApp, demo, ingest, tagging...) above DOMINIO_DAILY_COST_ALERT_USD
  (default 5 USD).
* Chat messages today at 80% or more of DOMINIO_AGENT_DAILY_CAP (the same
  'chat:global:<UTC day>' counter the widget and the WhatsApp webhook bump).

"Once per day" is a marker in the shared DatabaseCache, written only AFTER
the email went out: a failed send is retried on the next hourly run. The
email carries totals and tenant slugs only, never conversation text or
visitor data.
"""
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.mail import mail_admins
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum
from django.utils import timezone

from landing import usage as usage_ledger
from landing.models import UsageEvent

CAP_SHARE = 0.8
MARKER_TTL = 60 * 60 * 26
TOP_TENANTS = 5


def _marker(kind, day):
    return f'costalert:{kind}:{day:%Y%m%d}'


class Command(BaseCommand):
    help = 'Email ADMINS (once a day per threshold) when AI spend or chat volume runs high.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Show the figures and what would be sent; send nothing.')

    def handle(self, *args, dry_run=False, **options):
        start, end = usage_ledger.day_bounds()
        day = timezone.localdate(start, timezone.get_default_timezone())
        events = UsageEvent.objects.filter(created_at__gte=start, created_at__lt=end)
        totals = events.aggregate(cost=Sum('cost_usd'), n=Count('id'))
        cost = (totals['cost'] or Decimal(0)).quantize(Decimal('0.0001'))
        limit = Decimal(str(getattr(settings, 'DOMINIO_DAILY_COST_ALERT_USD', 5) or 5))

        cap = int(getattr(settings, 'DOMINIO_AGENT_DAILY_CAP', 2000) or 0)
        used = int(cache.get('chat:global:' + timezone.now().strftime('%Y%m%d')) or 0)

        self.stdout.write(f'cost_alert {day:%Y-%m-%d}: gasto IA ${cost} de ${limit} '
                          f'({totals["n"]} eventos); mensajes {used} de {cap}.')

        if cost >= limit:
            top = (events.values('client_slug').annotate(c=Sum('cost_usd'), n=Count('id'))
                   .order_by('-c')[:TOP_TENANTS])
            lines = [f'Gasto de IA de hoy ({day:%Y-%m-%d}, hora de Puerto Rico): ${cost} USD.',
                     f'Umbral: ${limit} USD (DOMINIO_DAILY_COST_ALERT_USD).',
                     f'Eventos facturados: {totals["n"]}.', '', 'Mayor gasto por cliente:']
            lines += [f'- {row["client_slug"] or "(sin cliente: demo/interno)"}: '
                      f'${(row["c"] or Decimal(0)).quantize(Decimal("0.0001"))} '
                      f'({row["n"]} eventos)' for row in top]
            lines += ['', 'Revisa el centro de control (/dashboard/control/). Este aviso '
                          'sale una vez al día.']
            self._send('cost', day, f'Gasto de IA alto hoy: ${cost}', lines, dry_run)

        if cap and used >= int(cap * CAP_SHARE):
            lines = [f'Mensajes de chat hoy: {used} de {cap} (DOMINIO_AGENT_DAILY_CAP).',
                     f'Al llegar al tope, el chat de TODOS los clientes contesta "no disponible" '
                     'hasta medianoche UTC.', '',
                     'Si el volumen es legítimo, sube DOMINIO_AGENT_DAILY_CAP; si no, revisa '
                     'el abuso en el centro de control. Este aviso sale una vez al día.']
            self._send('cap', day, f'Chat al {int(used * 100 / cap)}% del tope diario', lines,
                       dry_run)

    def _send(self, kind, day, subject, lines, dry_run):
        key = _marker(kind, day)
        if cache.get(key):
            self.stdout.write(f'  {kind}: ya se avisó hoy.')
            return
        if dry_run:
            self.stdout.write(f'  [dry-run] se enviaría: {subject}')
            return
        if not settings.ADMINS:
            self.stderr.write(f'  {kind}: ADMINS está vacío; nadie recibe el aviso.')
        try:
            mail_admins(subject, '\n'.join(lines), fail_silently=False)
        except Exception as exc:
            raise CommandError(f'No se pudo enviar el aviso {kind}: {exc.__class__.__name__}')
        cache.set(key, 1, MARKER_TTL)
        self.stdout.write(f'  {kind}: aviso enviado ({subject}).')
