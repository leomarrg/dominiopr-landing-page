"""Phase C: bill last month's overage as Stripe invoice items. Run from cron
on the 1st of each month (DEPLOYMENT.md):

    python manage.py bill_overage                 # previous month, PR time
    python manage.py bill_overage --month 2026-10
    python manage.py bill_overage --dry-run       # print, post nothing
    python manage.py bill_overage --month 2026-11 --force   # a month not closed yet

For every client on a conversations plan (small..premium, or an override)
whose usage went past what the plan includes: one InvoiceItem per kind
(web_block / wa_block / seat).

* Monthly subscribers: the item rides on the subscription, so it lands on
  the next renewal invoice.
* Annual subscribers (renewal months away): an empty draft invoice is
  created FIRST, each item is created straight onto it, and the invoice is
  finalized last -- so a crash halfway never leaves loose pending items
  that a later invoice could sweep up twice.

MANUAL REVIEW (listed and emailed to CONTACT_NOTIFY_EMAIL, nothing posted):
clients paying by ATH Movil / manual or with no live Stripe subscription;
clients whose plan or step changed during (or after) the billed month
(the included capacity is ambiguous); and amounts that look wrong --
overage above 3x the monthly price of the step, or usage above 3x what the
plan includes -- which is what anonymous traffic or a widget bug would
produce, not a real customer.

Idempotent: every post carries an Idempotency-Key per (client, month, kind)
and is bracketed by two AuditEvents -- 'billing.overage_pending' BEFORE
the POST and 'billing.overage' after it (target 'YYYY-MM:kind'). A rerun
finds 'billing.overage' and posts nothing; a rerun that finds a pending
record with no completion (the process died between the POST and the
record) asks Stripe for the customer's items first and only posts when the
item is really missing. The service itself is never touched here: overage
never cuts the agent.
"""
import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from landing import entitlements, payments
from landing.models import AuditEvent, Client

ACTION = 'billing.overage'
PENDING_ACTION = 'billing.overage_pending'
INVOICE_ACTION = 'billing.overage_invoice'
FINALIZE_ACTION = 'billing.overage_finalized'
BILLABLE_STATUSES = ('active', 'past_due')
# Above this multiple of the step's monthly price, or of the included
# capacity, the amount is not charged automatically.
SUSPICIOUS_FACTOR = 3


def monthly_price_cents(sub):
    """What the client pays per month at their step, in cents. None when
    the price is unknown (custom plan with no override)."""
    if sub.monthly_price_cents is not None:
        return int(sub.monthly_price_cents)
    from landing.views import plan_pricing
    pricing = plan_pricing(sub.plan, sub.step) or {}
    monthly = pricing.get('monthly')
    return int(round(float(monthly) * 100)) if monthly is not None else None


def review_reasons(sub, ent, status, month_start):
    """Why this client's overage must be reviewed by hand instead of posted
    to Stripe. Empty list = safe to bill automatically."""
    reasons = []
    if not (sub.method == 'stripe' and sub.status in BILLABLE_STATUSES
            and bool(sub.stripe_customer_id)):
        reasons.append(f'sin Stripe activo ({sub.get_method_display()}, {sub.status})')
    if sub.updated_at is not None and sub.updated_at >= month_start:
        when = timezone.localtime(sub.updated_at, entitlements.BUSINESS_TZ).strftime('%Y-%m-%d')
        reasons.append(f'plan o escalón modificado el {when}; lo incluido del mes es ambiguo')
    price = monthly_price_cents(sub)
    total = status['total_overage_cents']
    if price is not None and total > SUSPICIOUS_FACTOR * price:
        reasons.append(f'exceso {entitlements.price_label(total)} supera {SUSPICIOUS_FACTOR}x '
                       f'la mensualidad ({entitlements.price_label(price)})')
    for name, bucket in (('web', status), ('WhatsApp', status['wa'])):
        included = bucket['included']
        if included and bucket['used'] > SUSPICIOUS_FACTOR * included:
            reasons.append(f'uso {name} {bucket["used"]} supera {SUSPICIOUS_FACTOR}x lo incluido '
                           f'({included}); parece tráfico anómalo')
    return reasons


class Command(BaseCommand):
    help = 'Factura el exceso de conversaciones del mes anterior como InvoiceItems de Stripe (Fase C).'

    def add_arguments(self, parser):
        parser.add_argument('--month', help='Mes a facturar, YYYY-MM (por defecto: el mes pasado).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Muestra lo que se cobraría sin llamar a Stripe ni registrar nada.')
        parser.add_argument('--force', action='store_true',
                            help='Permite facturar un mes que todavía no ha cerrado.')

    def handle(self, *args, **options):
        dry = options['dry_run']
        start, _end = self._bounds(options.get('month'))
        this_start, _ = entitlements.month_bounds()
        if start >= this_start and not dry and not options.get('force'):
            raise CommandError('Ese mes no ha cerrado; usa --dry-run (o --force para cobrarlo igual).')
        label = timezone.localtime(start, entitlements.BUSINESS_TZ).strftime('%Y-%m')
        prefix = '[dry-run] ' if dry else ''
        self.errors = 0

        clients = list(Client.objects.select_related('subscription')
                       .filter(subscription__isnull=False).order_by('slug'))
        usage = entitlements.month_usage_many([c.pk for c in clients], start)
        self.done = set(AuditEvent.objects.filter(action=ACTION, target__startswith=f'{label}:')
                        .values_list('client_id', 'target'))
        self.pending = set(AuditEvent.objects
                           .filter(action=PENDING_ACTION, target__startswith=f'{label}:')
                           .values_list('client_id', 'target'))
        invoices = dict(AuditEvent.objects.filter(action=INVOICE_ACTION, target=label)
                        .values_list('client_id', 'result'))
        finalized = set(AuditEvent.objects.filter(action=FINALIZE_ACTION, target=label)
                        .values_list('client_id', flat=True))

        posted = skipped = 0
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
            todo = [(kind, units, cents, description) for kind, units, cents, description in items
                    if (client.pk, f'{label}:{kind}') not in self.done]
            for kind, _units, _cents, _description in items:
                if (client.pk, f'{label}:{kind}') in self.done:
                    skipped += 1
                    self.stdout.write(f'{client.slug}: {kind} de {label} ya facturado; omitido.')
            if not todo:
                continue
            reasons = review_reasons(sub, ent, status, start)
            if reasons:
                why = '; '.join(reasons)
                for kind, units, cents, description in todo:
                    manual.append(f'{client.slug} ({sub.get_method_display()}, {sub.status}): '
                                  f'{description} = {entitlements.price_label(cents)} -- {why}')
                continue

            annual = sub.period == 'annual'
            invoice_id = invoices.get(client.pk, '') if annual else ''
            if annual and not invoice_id and not dry:
                invoice_id = self._create_invoice(client, sub, label)
                if not invoice_id:
                    continue
                invoices[client.pk] = invoice_id
            listed = None     # Stripe's view of the customer's items, fetched once if needed
            client_errors = 0
            for kind, units, cents, description in todo:
                target = f'{label}:{kind}'
                self.stdout.write(f'{prefix}{client.slug}: {description} = '
                                  f'{entitlements.price_label(cents)} ({units} x {kind}).')
                if dry:
                    continue
                if (client.pk, target) in self.pending:
                    # A previous run posted (maybe) and died before recording.
                    if listed is None:
                        listed = self._list_items(client, sub, invoice_id)
                        if listed is None:
                            client_errors += 1
                            break
                    found = next((it for it in listed
                                  if (it.get('metadata') or {}).get('month') == label
                                  and (it.get('metadata') or {}).get('kind') == kind), None)
                    if found is not None:
                        self._record(client, target, units, kind, cents, found.get('id', ''))
                        skipped += 1
                        self.stdout.write(f'{client.slug}: {kind} de {label} ya estaba en Stripe '
                                          f'({found.get("id", "")}); registrado sin repetir.')
                        continue
                else:
                    AuditEvent.objects.create(client=client, action=PENDING_ACTION, target=target,
                                              result=f'{units} x {kind} = {cents} cents')
                    self.pending.add((client.pk, target))
                try:
                    item = payments.create_invoice_item(
                        sub.stripe_customer_id, cents, description,
                        idempotency_key=f'overage:{client.pk}:{label}:{kind}',
                        # Monthly: rides on the next renewal invoice. Annual:
                        # straight onto the draft invoice created above.
                        subscription_id=('' if annual else sub.stripe_subscription_id),
                        invoice_id=invoice_id,
                        metadata={'client': client.slug, 'month': label, 'kind': kind,
                                  'units': units, 'order_number': sub.order_number})
                except payments.StripeError as exc:
                    self.errors += 1
                    client_errors += 1
                    self.stderr.write(f'{client.slug}: Stripe rechazó {kind} de {label}: {exc}')
                    continue
                self._record(client, target, units, kind, cents, item.get('id', ''))
                posted += 1
                posted_cents += cents

            # Annual: every line is on the draft now; send it. A client with
            # a failed line keeps its draft open for the next run.
            if (annual and not dry and invoice_id and not client_errors
                    and client.pk not in finalized):
                try:
                    payments.finalize_invoice(
                        invoice_id, idempotency_key=f'overage-finalize:{client.pk}:{label}')
                except payments.StripeError as exc:
                    self.errors += 1
                    self.stderr.write(f'{client.slug}: no se pudo finalizar la factura '
                                      f'{invoice_id}: {exc}')
                else:
                    AuditEvent.objects.create(client=client, action=FINALIZE_ACTION, target=label,
                                              result=invoice_id[:200])
                    finalized.add(client.pk)
                    self.stdout.write(f'{client.slug}: factura {invoice_id} finalizada (anual).')

        if manual:
            self.stdout.write(self.style.WARNING(
                f'COBRAR A MANO ({len(manual)}; revisar antes de cobrar):'))
            for line in manual:
                self.stdout.write(f'  - {line}')
            ops = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
            if ops and not dry:
                from landing.views import _send_plain_email
                _send_plain_email(
                    f'[Exceso] {len(manual)} cargo(s) para revisar a mano ({label})',
                    [ops],
                    'Estos clientes pasaron lo incluido en su plan y bill_overage NO les cobró '
                    'nada: no tienen Stripe activo, cambiaron de plan en el mes, o el monto '
                    'parece anómalo (tráfico sospechoso). Revisa cada caso y cobra a mano si '
                    'procede:\n\n'
                    + '\n'.join(f'- {line}' for line in manual))

        self.stdout.write(self.style.SUCCESS(
            f'{prefix}Exceso de {label}: {posted} cargo(s) creados '
            f'({entitlements.price_label(posted_cents)}), {skipped} ya facturados, '
            f'{len(manual)} a mano, {self.errors} error(es).'))
        if self.errors:
            raise CommandError(f'{self.errors} cargo(s) fallaron; revisa el log y vuelve a correr.')

    # ------------------------------------------------------------------

    def _record(self, client, target, units, kind, cents, item_id):
        AuditEvent.objects.create(
            client=client, action=ACTION, target=target,
            result=f'{units} x {kind} = {cents} cents · {item_id}'[:200])
        self.done.add((client.pk, target))

    def _create_invoice(self, client, sub, label):
        """Empty draft for an annual subscriber; '' on failure (counted)."""
        try:
            inv = payments.create_invoice(
                sub.stripe_customer_id,
                idempotency_key=f'overage-invoice:{client.pk}:{label}',
                description=f'DOMINIO — conversaciones adicionales (mes {label})')
        except payments.StripeError as exc:
            self.errors += 1
            self.stderr.write(f'{client.slug}: no se pudo crear la factura anual: {exc}')
            return ''
        invoice_id = str(inv.get('id', ''))
        AuditEvent.objects.create(client=client, action=INVOICE_ACTION, target=label,
                                  result=invoice_id[:200])
        self.stdout.write(f'{client.slug}: factura {invoice_id} creada (anual, borrador).')
        return invoice_id

    def _list_items(self, client, sub, invoice_id):
        """The customer's items as Stripe has them (the lines of the draft
        invoice for annual, the pending items for monthly). None on failure."""
        try:
            if invoice_id:
                resp = payments.list_invoice_items(sub.stripe_customer_id, invoice_id=invoice_id)
            else:
                resp = payments.list_invoice_items(sub.stripe_customer_id, pending=True)
        except payments.StripeError as exc:
            self.errors += 1
            self.stderr.write(f'{client.slug}: no se pudo consultar los cargos previos: {exc}')
            return None
        return list(resp.get('data') or [])

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
