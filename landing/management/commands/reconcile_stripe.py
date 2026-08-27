"""M-01: daily reconciliation — the safety net under webhooks. Run from cron:

    python manage.py reconcile_stripe

* Stripe subscriptions: re-fetch the real status and fix any local divergence
  (a lost webhook can never leave a dead subscription serving traffic).
* ATH Móvil / manual subscriptions: warn the operator 7 days before the paid
  period ends, and mark past_due + pause the widget once it's over.
"""
import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from landing import payments
from landing.models import Client, Subscription
from landing.views import _send_plain_email, widget_should_run


class Command(BaseCommand):
    help = 'Reconcile local subscriptions against Stripe / manual period ends (M-01).'

    def handle(self, *args, **options):
        ops_email = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
        now = timezone.now()
        fixed = warned = 0

        if payments.stripe_enabled():
            for sub in Subscription.objects.filter(method='stripe').exclude(
                    stripe_subscription_id=''):
                try:
                    remote = payments.retrieve(f'/v1/subscriptions/{sub.stripe_subscription_id}')
                except payments.StripeError as e:
                    self.stderr.write(f'{sub.client.slug}: error consultando Stripe: {e}')
                    continue
                status = remote.get('status', '')
                new_status = ('active' if status in ('active', 'trialing')
                              else 'canceled' if status == 'canceled'
                              else 'past_due' if status in ('past_due', 'unpaid')
                              else sub.status)
                updates = {}
                if new_status != sub.status:
                    updates['status'] = new_status
                pe = remote.get('current_period_end')
                if pe:
                    updates['current_period_end'] = dt.datetime.fromtimestamp(
                        int(pe), tz=dt.timezone.utc)
                if updates:
                    Subscription.objects.filter(pk=sub.pk).update(**updates)
                    fixed += 1
                # The widget must reflect the money: no live sub, no agent. Uses
                # the SAME rule as the webhook (views.widget_should_run), because
                # when the two disagreed a past_due customer's agent went down or
                # came back depending on which one ran last.
                active_should_be = widget_should_run(new_status)
                if not active_should_be and sub.client.is_active:
                    Client.objects.filter(pk=sub.client_id).update(is_active=False)
                    self.stdout.write(f'{sub.client.slug}: widget pausado '
                                      f'(estado Stripe: {status})')
                elif active_should_be and not sub.client.is_active:
                    # Only un-pause an agent that is actually installed. Otherwise
                    # this quietly reverses a staff member's deliberate pause, and
                    # re-publishes tenants that were never finished.
                    if sub.client.setup_status == 'live':
                        Client.objects.filter(pk=sub.client_id).update(is_active=True)
                        self.stdout.write(f'{sub.client.slug}: widget reactivado '
                                          f'(estado Stripe: {status})')

        # ATH Móvil / manual: one-shot payments with an explicit period end.
        for sub in Subscription.objects.filter(method__in=['ath_movil', 'manual'],
                                               status='active'):
            if not sub.current_period_end:
                continue
            days_left = (sub.current_period_end - now).days
            if 0 <= days_left <= 7 and ops_email:
                _send_plain_email(
                    f'[Suscripción] {sub.client.slug} vence en {days_left} días',
                    [ops_email],
                    (f'La suscripción {sub.method} de {sub.client.name} vence el '
                     f'{sub.current_period_end:%Y-%m-%d}. Coordina la renovación '
                     f'(ATH Móvil no renueva automáticamente).'))
                warned += 1
            elif days_left < 0:
                Subscription.objects.filter(pk=sub.pk).update(status='past_due')
                Client.objects.filter(pk=sub.client_id).update(is_active=False)
                if ops_email:
                    _send_plain_email(
                        f'[Suscripción] {sub.client.slug} VENCIDA — widget pausado',
                        [ops_email],
                        f'El período pagado de {sub.client.name} terminó. El widget fue pausado.')
                self.stdout.write(f'{sub.client.slug}: vencida — pausada.')

        orphans = self._find_orphaned_paid_checkouts(ops_email, now)

        self.stdout.write(self.style.SUCCESS(
            f'Reconciliación completa: {fixed} sincronizadas, {warned} avisos, '
            f'{orphans} pagos sin aprovisionar.'))

    def _find_orphaned_paid_checkouts(self, ops_email, now, hours=72):
        """Money in, nothing delivered — the failure nobody would ever notice.

        The webhook is the only thing that turns a payment into a tenant. If a
        delivery is lost, or the worker dies mid-handler, Stripe eventually stops
        retrying and the customer is left having paid for an agent that does not
        exist. Nothing else in the system looks at payments that produced no
        Subscription row, so this does: it asks Stripe what was actually paid and
        compares against what we provisioned.
        """
        if not payments.stripe_enabled():
            return 0
        since = int((now - dt.timedelta(hours=hours)).timestamp())
        try:
            listing = payments.recent_checkout_sessions(since)
        except payments.StripeError as e:
            self.stderr.write(f'No se pudo listar checkouts recientes: {e}')
            return 0

        known = set(Subscription.objects.exclude(checkout_session_id='')
                    .values_list('checkout_session_id', flat=True))
        orphans = []
        for session in (listing.get('data') or []):
            if session.get('payment_status') != 'paid':
                continue
            sid = str(session.get('id') or '')
            if sid and sid not in known:
                orphans.append(session)

        for session in orphans:
            email = ((session.get('customer_details') or {}).get('email')
                     or session.get('customer_email') or '—')
            meta = session.get('metadata') or {}
            sid = session.get('id')
            self.stderr.write(f'PAGO SIN APROVISIONAR: {sid} ({email})')
            if ops_email:
                _send_plain_email(
                    f'[ACCION REQUERIDA] Pago sin agente — {email}',
                    [ops_email],
                    (f'Stripe cobró este checkout pero no existe ninguna suscripción '
                     f'local para él, así que el cliente pagó y NO tiene agente.\n\n'
                     f'Checkout: {sid}\n'
                     f'Email: {email}\n'
                     f'Plan: {meta.get("plan", "—")}/{meta.get("period", "—")}\n'
                     f'Negocio: {meta.get("company", "—")}\n\n'
                     f'Revisa el evento en Stripe (Developers > Events) y reenvíalo, '
                     f'o crea el agente a mano en el Factory con '
                     f'notify_email={email}.'))
        return len(orphans)
