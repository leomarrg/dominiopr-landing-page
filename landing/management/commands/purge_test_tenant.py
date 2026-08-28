"""
Remove a tenant that only ever existed as a test purchase.

    python manage.py purge_test_tenant --email leomarrg+prueba@gmail.com
    python manage.py purge_test_tenant --email leomarrg+prueba@gmail.com --yes

Dry-run by default: it lists exactly what would go and touches nothing until
--yes. Keyed on the payer email rather than a slug because that is the one
value shared by every row a checkout leaves behind — the Client
(notify_email), the lead (ContactSubmission.email) and the login (User.email).

What is deliberately NOT deleted: AuditEvent and ProcessedWebhookEvent rows.
They are the record that the test happened, and the webhook ledger is what
stops a replayed Stripe event from re-creating the tenant we just removed.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from landing.models import Client, ContactSubmission, Membership, Subscription


class Command(BaseCommand):
    help = 'Delete a test tenant (client, subscription, login, leads) by payer email.'

    def add_arguments(self, parser):
        parser.add_argument('--email', required=True, action='append',
                            help='Payer email. Repeatable.')
        parser.add_argument('--yes', action='store_true', help='Actually delete.')

    def handle(self, *args, **opts):
        emails = [e.strip().lower() for e in opts['email'] if e.strip()]
        if not emails:
            raise CommandError('Falta --email.')
        User = get_user_model()

        clients = Client.objects.filter(notify_email__in=emails)
        subs = Subscription.objects.filter(client__in=clients)
        leads = ContactSubmission.objects.filter(email__in=emails)
        users = User.objects.filter(email__in=emails)

        # Refuse to touch anything that looks like a paying customer.
        live = subs.filter(status='active')
        if live.exists():
            raise CommandError(
                'Hay una suscripcion ACTIVA para ese email (%s). Esto es para basura '
                'de prueba, no para clientes. Cancela en Stripe primero si de verdad '
                'quieres borrarlo.' % ', '.join(s.stripe_subscription_id for s in live))

        self.stdout.write('\nSe borraria:')
        for c in clients:
            self.stdout.write('  Client        %s (%s) activo=%s' % (c.slug, c.name, c.is_active))
        for s in subs:
            self.stdout.write('  Subscription  %s %s/%s %s' % (
                s.order_number or '-', s.plan, s.period, s.status))
        for l in leads:
            self.stdout.write('  Lead          #%s %s <%s> %s' % (l.pk, l.company, l.email, l.status))
        for u in users:
            others = Membership.objects.filter(user=u).exclude(client__in=clients).count()
            note = '' if not others else '  (NO se borra: tiene %d membresia(s) en otros clientes)' % others
            self.stdout.write('  User          %s%s' % (u.email, note))
        if not any([clients.exists(), leads.exists(), users.exists()]):
            self.stdout.write('  (nada con ese email)')
            return

        if not opts['yes']:
            self.stdout.write('\nEnsayo. Repite con --yes para borrar.')
            return

        with transaction.atomic():
            n_leads = leads.delete()[0]
            # Subscription.client is PROTECT — a client can never take its billing
            # record down with it by accident. Here that is the intent, so the
            # subscription goes first, explicitly.
            subs.delete()
            n_clients = clients.delete()[0]      # cascades memberships, conversations
            n_users = 0
            for u in users:
                if not Membership.objects.filter(user=u).exists() and not u.is_staff:
                    u.delete()
                    n_users += 1
        self.stdout.write(self.style.SUCCESS(
            '\nBorrado: %d fila(s) de cliente y dependientes, %d lead(s), %d login(s).'
            % (n_clients, n_leads, n_users)))
