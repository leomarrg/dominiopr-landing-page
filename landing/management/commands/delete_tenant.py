"""Offboarding: export and then permanently delete one tenant.

    python manage.py delete_tenant --client <slug>                       # dry-run
    python manage.py delete_tenant --client <slug> --yes --export <dir>
    python manage.py delete_tenant --client <slug> --yes --skip-export   # no copy

Dry-run by default: prints what would go and touches nothing. With --yes it
1. pauses the widget (is_active=False) so no new rows arrive mid-way,
2. writes the same zip as `export_tenant` into --export (required unless
   --skip-export is given explicitly),
3. deletes, in ONE transaction, everything the tenant owns: conversations
   (messages and surveys cascade), bookings, leads (notes and tag links
   cascade), the canceled/incomplete subscription row, and the Client itself
   (locations, availability, staff contacts, channels, opt-outs, tags,
   knowledge and memberships cascade).

The PROTECT foreign keys (lead, booking, conversation, subscription -> client)
are honoured, not bypassed: their rows are deleted explicitly, first, and a
subscription that is still billing (active / past_due) stops the command --
cancel it in Stripe first, or the card keeps being charged for a tenant that
no longer exists.

Kept on purpose: AuditEvent rows (client becomes NULL; a new `tenant.delete`
event records the counts), ProcessedWebhookEvent (stops a replayed Stripe
event from re-creating the tenant) and UsageEvent (no PII, client_slug keeps
the cost history whole). Logins left without any membership are deactivated,
never deleted. The `dominio` client (DOMINIO's own site and CRM) is refused.
"""
import os

import re

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from landing.models import (
    AuditEvent, AvailabilityRule, Booking, ChannelAccount, ChannelOptOut, ChatMessage,
    Client, ContactSubmission, Conversation, KnowledgeFragment, KnowledgeSource, LeadNote,
    LeadTag, Location, Membership, StaffContact, Subscription, Survey, UsageEvent,
)

PROTECTED_SLUGS = frozenset({'dominio'})
# A subscription in one of these states is still charging the card.
LIVE_SUBSCRIPTION = ('active', 'past_due')


def tenant_counts(client):
    """Row counts per table for the dry-run and the audit trail."""
    convs = Conversation.objects.filter(client=client)
    leads = ContactSubmission.objects.filter(client=client)
    return {
        'conversaciones': convs.count(),
        'mensajes': ChatMessage.objects.filter(conversation__client=client).count(),
        'encuestas': Survey.objects.filter(conversation__client=client).count(),
        'leads': leads.count(),
        'notas': LeadNote.objects.filter(lead__client=client).count(),
        'citas': Booking.objects.filter(client=client).count(),
        'conocimiento': KnowledgeSource.objects.filter(client=client).count(),
        'fragmentos': KnowledgeFragment.objects.filter(source__client=client).count(),
        'locales': Location.objects.filter(client=client).count(),
        'horarios': AvailabilityRule.objects.filter(client=client).count(),
        'contactos': StaffContact.objects.filter(client=client).count(),
        'canales': ChannelAccount.objects.filter(client=client).count(),
        'bajas_canal': ChannelOptOut.objects.filter(client=client).count(),
        'etiquetas': LeadTag.objects.filter(client=client).count(),
        'membresias': Membership.objects.filter(client=client).count(),
        'suscripcion': Subscription.objects.filter(client=client).count(),
    }


def users_to_deactivate(client):
    """Non-staff logins whose ONLY memberships are in this client."""
    User = get_user_model()
    member_ids = Membership.objects.filter(client=client).values_list('user_id', flat=True)
    elsewhere = (Membership.objects.exclude(client=client)
                 .values_list('user_id', flat=True))
    return (User.objects.filter(pk__in=member_ids, is_staff=False, is_superuser=False)
            .exclude(pk__in=elsewhere))


class Command(BaseCommand):
    help = 'Export and permanently delete one tenant (dry-run unless --yes).'

    def add_arguments(self, parser):
        parser.add_argument('--client', required=True, help='Client slug.')
        parser.add_argument('--yes', action='store_true', help='Actually delete.')
        parser.add_argument('--export', metavar='DIR',
                            help='Write the export zip here before deleting.')
        parser.add_argument('--skip-export', action='store_true',
                            help='Delete without an export (explicit opt-out).')

    def handle(self, *args, **opts):
        slug = (opts['client'] or '').strip()
        if slug in PROTECTED_SLUGS:
            raise CommandError(f'"{slug}" es el sitio y el CRM de DOMINIO: no se borra.')
        client = Client.objects.filter(slug=slug).first()
        if client is None:
            raise CommandError(f'No existe un cliente con slug "{slug}".')

        sub = Subscription.objects.filter(client=client).first()
        if sub is not None and sub.status in LIVE_SUBSCRIPTION:
            raise CommandError(
                f'La suscripción de "{slug}" sigue cobrando (estado {sub.status}, '
                f'orden {sub.order_number or "-"}). Cancélala en Stripe primero.')

        export_dir = opts['export']
        if export_dir:
            export_dir = os.path.abspath(export_dir)
            if not os.path.isdir(export_dir):
                raise CommandError(f'El directorio {export_dir} no existe.')

        counts = tenant_counts(client)
        deactivate = list(users_to_deactivate(client).values_list('pk', flat=True))
        kept_usage = UsageEvent.objects.filter(client=client).count()
        kept_audit = AuditEvent.objects.filter(client=client).count()

        self.stdout.write(f'\nCliente {client.slug} ({client.name}) activo={client.is_active}')
        self.stdout.write('Se borraría:')
        for name, n in counts.items():
            self.stdout.write(f'  {name}: {n}')
        self.stdout.write(f'  logins a desactivar (sin otras membresías): {len(deactivate)}')
        self.stdout.write('Se conserva:')
        self.stdout.write(f'  eventos de uso (sin PII, quedan sin cliente): {kept_usage}')
        self.stdout.write(f'  eventos de auditoría (quedan sin cliente): {kept_audit}')

        if not opts['yes']:
            self.stdout.write('\nEnsayo. Repite con --yes y --export <dir> para borrar.')
            return
        if not export_dir and not opts['skip_export']:
            raise CommandError('Falta --export <dir> (o --skip-export para borrar sin copia).')

        # 1. Stop new traffic first: rows created between the export and the
        # delete would otherwise be removed without ever being exported.
        Client.objects.filter(pk=client.pk).update(is_active=False)
        # 2. Export (same zip and audit trail as `export_tenant`).
        if export_dir:
            try:
                call_command('export_tenant', client=client.slug, out=export_dir,
                             stdout=self.stdout)
            except Exception:
                self.stderr.write('La exportación falló: no se borró nada, pero el widget '
                                  'quedó pausado (is_active=False).')
                raise
        # 3. Delete.
        with transaction.atomic():
            counts = tenant_counts(client)       # final numbers, inside the transaction
            sub_note = ''
            if sub is not None:
                sub = Subscription.objects.select_for_update().get(pk=sub.pk)
                if sub.status in LIVE_SUBSCRIPTION:
                    raise CommandError('La suscripción se reactivó mientras tanto. Nada se borró.')
                sub_note = f' sub={sub.order_number or "-"}:{sub.plan}/{sub.status}'
            # The PROTECT children go first, explicitly. Conversations before
            # leads so no transcript is left behind with lead=NULL.
            Conversation.objects.filter(client=client).delete()
            Booking.objects.filter(client=client).delete()
            ContactSubmission.objects.filter(client=client).delete()
            Subscription.objects.filter(client=client).delete()
            deactivated = (get_user_model().objects.filter(pk__in=deactivate)
                           .update(is_active=False))
            # The kept audit trail must not keep people's emails: redact any
            # address in this client's events before the client goes.
            email_re = re.compile(r'[^\s@:]+@[^\s@:]+\.[A-Za-z]{2,}')
            for ev in AuditEvent.objects.filter(client=client).only('pk', 'target', 'result'):
                target = email_re.sub('[correo borrado]', ev.target or '')
                result = email_re.sub('[correo borrado]', ev.result or '')
                if (target, result) != (ev.target, ev.result):
                    AuditEvent.objects.filter(pk=ev.pk).update(target=target, result=result)
            Client.objects.filter(pk=client.pk).delete()

            summary = ' '.join(f'{k}={v}' for k, v in counts.items() if v)
            AuditEvent.objects.create(
                client=None, action='tenant.delete', target=client.slug,
                # Subscription and logins first: the field is cut at 200 chars.
                result=(f'cli{sub_note} logins_desactivados={deactivated} {summary}')[:200])

        self.stdout.write(self.style.SUCCESS(
            f'\nBorrado "{client.slug}". Logins desactivados: {deactivated}.'))
