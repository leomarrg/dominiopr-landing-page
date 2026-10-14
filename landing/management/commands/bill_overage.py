"""Phase C: bill last month's overage as Stripe invoice items. Run from cron
on the 1st of each month (DEPLOYMENT.md):

    python manage.py bill_overage                 # previous month, PR time
    python manage.py bill_overage --month 2026-10
    python manage.py bill_overage --dry-run       # print, post nothing

For every client on a conversations plan (small..premium, or an override)
whose usage went past what the plan includes: one InvoiceItem per kind
(web_block / wa_block / seat) on the customer's Stripe subscription, so it
rides on the next renewal invoice; annual subscribers get a standalone
invoice right away (their renewal is months off). Clients paying by ATH
Movil / manual (or with no live Stripe subscription) are LISTED for the
owner to bill by hand and nothing is posted for them.

Idempotent: every post carries an Idempotency-Key per (client, month, kind)
and is recorded as AuditEvent 'billing.overage' (target 'YYYY-MM:kind'); a
second run for the same month finds the record and posts nothing. The
service itself is never touched here: overage never cuts the agent.
"""
import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from landing import entitlements, payments
from landing.models import AuditEvent, Client

ACTION = 'billing.overage'
INVOICE_ACTION = 'billing.overage_invoice'
BILLABLE_STATUSES = ('active', 'past_due')


class Command(BaseCommand):
    help = 'Factura el exceso de conversaciones del mes anterior como InvoiceItems de Stripe (Fase C).'

    def add_arguments(self, parser):
        parser.add_argument('--month', help='Mes a facturar, YYYY-MM (por defecto: el mes pasado).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Muestra lo que se cobraría sin llamar a Stripe ni registrar nada.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        start, _end = self._bounds(options.get('month'))
        label = timezone.localtime(start, entitlements.BUSINESS_TZ).strftime('%Y-%m')
        prefix = '[dry-run] ' if dry else ''

        clients = list(Client.objects.select_related('subscription')
                       .filter(subscription__isnull=False).order_by('slug'))
        usage = entitlements.month_usage_many([c.pk for c in clients], start)
        done = set(AuditEvent.objects.filter(action=ACTION, target__startswith=f'{label}:')
                   .values_list('client_id', 'target'))
        invoiced = set(AuditEvent.objects.filter(action=INVOICE_ACTION, target=label)
                       .values_list('client_id', flat=True))

        posted = skipped = errors = 0
        posted_cents = 0
        manual = []
        for client in clients:
            sub = client.subscription
            ent = entitlements.entitlements(client)
            if ent['unit'] != 'conversations' or not ent.get('monthly_conversations'):
                continue
            status = entitlements.allowance_status(client, start, ent=ent, usage=usage[client.pk])
            items = entitlements.overage_items(status)
            if not items:
                continue
            stripe_ok = (sub.method == 'stripe' and sub.status in BILLABLE_STATUSES
                         and bool(sub.stripe_customer_id))
            posted_here = False
            for kind, units, cents, description in items:
                target = f'{label}:{kind}'
                price = entitlements.price_label(cents)
                if (client.pk, target) in done:
                    skipped += 1
                    self.stdout.write(f'{client.slug}: {kind} de {label} ya facturado; omitido.')
                    continue
                if not stripe_ok:
                    manual.append(f'{client.slug} ({sub.get_method_display()}, {sub.status}): '
                                  f'{description} = {price}')
                    continue
                self.stdout.write(f'{prefix}{client.slug}: {description} = {price} '
                                  f'({units} x {kind}).')
                if dry:
                    continue
                try:
                    item = payments.create_invoice_item(
                        sub.stripe_customer_id, cents, description,
                        idempotency_key=f'overage:{client.pk}:{label}:{kind}',
                        # Monthly: rides on the next renewal invoice. Annual:
                        # pending on the customer, collected below.
                        subscription_id=(sub.stripe_subscription_id
                                         if sub.period == 'monthly' else ''),
                        metadata={'client': client.slug, 'month': label, 'kind': kind,
                                  'units': units, 'order_number': sub.order_number})
                except payments.StripeError as exc:
                    errors += 1
                    self.stderr.write(f'{client.slug}: Stripe rechazó {kind} de {label}: {exc}')
                    continue
                AuditEvent.objects.create(
                    client=client, action=ACTION, target=target,
                    result=f'{units} x {kind} = {cents} cents · {item.get("id", "")}'[:200])
                done.add((client.pk, target))
                posted += 1
                posted_cents += cents
                posted_here = True

            # Annual subscribers: collect now instead of at a renewal months away.
            # Also covers a previous run that posted the items but died before
            # the invoice (items audited, invoice not).
            billed_before = any(c == client.pk and t.startswith(f'{label}:') for c, t in done)
            if (stripe_ok and sub.period == 'annual' and not dry
                    and (posted_here or billed_before) and client.pk not in invoiced):
                try:
                    inv = payments.create_invoice(
                        sub.stripe_customer_id,
                        idempotency_key=f'overage-invoice:{client.pk}:{label}',
                        description=f'DOMINIO — conversaciones adicionales (mes {label})')
                except payments.StripeError as exc:
                    errors += 1
                    self.stderr.write(f'{client.slug}: no se pudo crear la factura anual: {exc}')
                else:
                    AuditEvent.objects.create(client=client, action=INVOICE_ACTION, target=label,
                                              result=str(inv.get('id', ''))[:200])
                    invoiced.add(client.pk)
                    self.stdout.write(f'{client.slug}: factura {inv.get("id", "")} creada (anual).')

        if manual:
            self.stdout.write(self.style.WARNING(
                f'COBRAR A MANO ({len(manual)}; sin Stripe activo):'))
            for line in manual:
                self.stdout.write(f'  - {line}')
            ops = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
            if ops and not dry:
                from landing.views import _send_plain_email
                _send_plain_email(
                    f'[Exceso] {len(manual)} cliente(s) sin Stripe para cobrar a mano ({label})',
                    [ops],
                    'Estos clientes pasaron lo incluido en su plan y no tienen suscripción de '
                    'Stripe activa; bill_overage no les cobró nada:\n\n'
                    + '\n'.join(f'- {line}' for line in manual))

        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Exceso de {label}: {posted} cargo(s) creados '
            f'({entitlements.price_label(posted_cents)}), {skipped} ya facturados, '
            f'{len(manual)} a mano, {errors} error(es).'))
        if errors:
            raise CommandError(f'{errors} cargo(s) fallaron; revisa el log y vuelve a correr.')

    @staticmethod
    def _bounds(month):
        if month:
            try:
                year, mon = (int(x) for x in month.split('-', 1))
                first = dt.date(year, mon, 1)
            except (TypeError, ValueError):
                raise CommandError('--month debe ser YYYY-MM.')
            return entitlements.month_bounds(first)
        this_start, _ = entitlements.month_bounds()
        return entitlements.month_bounds(this_start - dt.timedelta(days=1))
