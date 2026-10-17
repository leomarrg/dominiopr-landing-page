"""Right to erasure for ONE visitor (by email or phone), across tenants.

    python manage.py erase_visitor --email ana@example.com                 # dry-run
    python manage.py erase_visitor --phone "(787) 555-1234" --client acme --yes

Dry-run by default. What it finds, optionally scoped to one --client:
- leads with that email (case-insensitive) or that phone (same line: the
  last 10 digits match, whatever the formatting);
- bookings with that email / phone, or booked over WhatsApp from that number
  (Booking.wa_id);
- WhatsApp conversations of that number, located by the same salted
  `landing.whatsapp.contact_hash` the webhook uses (the number itself is
  never stored on a conversation);
- with --email, web conversations whose transcript contains the address
  (the visitor typed it but no lead was captured).

What it does with --yes, in one transaction:
- every matched lead -- plus the lead of any matched WhatsApp conversation --
  is anonymized with `leads.anonymize_lead` (the row survives for statistics;
  name, contact data, notes, transcripts and its bookings' contact data go);
- matched conversations and bookings that belong to none of the matched leads
  (per `leads.lead_bookings`) are deleted;
- a transcript that mentions the email but belongs to ANOTHER lead is only
  reported (it is that other person's record; review it by hand).
Kept on purpose: ChannelOptOut rows (a salted hash, and the proof that the
person said STOP) and UsageEvent rows (no PII). One `visitor.erase`
AuditEvent per affected client records the counts and never the email or
phone.
"""
import re
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from landing import leads as lead_service
from landing.models import AuditEvent, Booking, ChatMessage, Client, ContactSubmission, Conversation
from landing.phone import normalize_phone
from landing.whatsapp import contact_hash


def _tail(value):
    """The last 10 digits of a phone in any format ('' when fewer)."""
    digits = re.sub(r'\D', '', str(value or ''))
    return digits[-10:] if len(digits) >= 10 else ''


def wa_ids_for(phone):
    """The WhatsApp ids (E.164 digits, no '+') this phone can arrive as: the
    digits as typed and, for a NANP number, '1' + 10 digits."""
    digits = re.sub(r'\D', '', phone)
    ids = {digits} if digits else set()
    normalized = normalize_phone(phone)
    if normalized:
        ids.add(normalized.lstrip('+'))
    return ids


def _same_phone(qs, tail):
    """Rows of `qs` whose `phone` is the same line as `tail`. Pre-filtered in
    SQL on the last 4 digits, compared exactly in Python (stored phones mix
    E.164 and whatever the visitor typed)."""
    return [obj.pk for obj in qs.filter(phone__contains=tail[-4:]).only('pk', 'phone')
            if _tail(obj.phone) == tail]


def find(email='', phone='', client=None):
    """{'leads', 'conversations', 'bookings', 'mentions_other_lead'}: sets of
    pks of what belongs to this visitor (see module docstring)."""
    scope = {'client': client} if client is not None else {}
    leads_qs = ContactSubmission.objects.filter(**scope)
    bookings_qs = Booking.objects.filter(**scope)
    convs_qs = Conversation.objects.filter(**scope)

    lead_ids, booking_ids, conv_ids, mention_ids = set(), set(), set(), set()
    if email:
        lead_ids |= set(leads_qs.filter(email__iexact=email).values_list('pk', flat=True))
        booking_ids |= set(bookings_qs.filter(email__iexact=email).values_list('pk', flat=True))
        mention_ids |= set(ChatMessage.objects
                           .filter(content__icontains=email, conversation__in=convs_qs)
                           .values_list('conversation_id', flat=True))
    if phone:
        tail = _tail(phone)
        if tail:
            lead_ids |= set(_same_phone(leads_qs, tail))
            booking_ids |= set(_same_phone(bookings_qs, tail))
        ids = wa_ids_for(phone)
        if ids:
            booking_ids |= set(bookings_qs.filter(wa_id__in=ids).values_list('pk', flat=True))
            wa = Q()
            for wa_id in ids:
                wa |= Q(external_id__startswith=f'wa:{contact_hash(wa_id)}:')
            wa_rows = convs_qs.filter(channel='whatsapp').filter(wa)
            conv_ids |= set(wa_rows.values_list('pk', flat=True))
            # A lead captured in the visitor's own WhatsApp conversation is theirs.
            lead_ids |= set(wa_rows.exclude(lead__isnull=True).values_list('lead_id', flat=True))

    # A transcript that mentions the email: the visitor's own when it has no
    # lead or one of theirs; otherwise it is another lead's record.
    other = set()
    for pk, lead_id in Conversation.objects.filter(pk__in=mention_ids).values_list('pk', 'lead_id'):
        if lead_id is None or lead_id in lead_ids:
            conv_ids.add(pk)
        else:
            other.add(pk)
    return {'leads': lead_ids, 'conversations': conv_ids, 'bookings': booking_ids,
            'mentions_other_lead': other}


class Command(BaseCommand):
    help = "Erase one visitor's personal data by email or phone (dry-run unless --yes)."

    def add_arguments(self, parser):
        who = parser.add_mutually_exclusive_group(required=True)
        who.add_argument('--email', help="The visitor's email.")
        who.add_argument('--phone', help="The visitor's phone, any format.")
        parser.add_argument('--client', help='Only within this client slug.')
        parser.add_argument('--yes', action='store_true', help='Actually erase.')

    def handle(self, *args, **opts):
        email = (opts['email'] or '').strip().lower()
        phone = (opts['phone'] or '').strip()
        if email:
            try:
                validate_email(email)
            except ValidationError:
                raise CommandError('El email no es válido.')
        if opts['phone'] is not None and not _tail(phone):
            raise CommandError('El teléfono necesita al menos 10 dígitos.')
        client = None
        if opts['client']:
            client = Client.objects.filter(slug=opts['client'].strip()).first()
            if client is None:
                raise CommandError(f'No existe un cliente con slug "{opts["client"]}".')

        found = find(email=email, phone=phone, client=client)
        lead_ids = found['leads']
        # Conversations / bookings of a matched lead are handled by
        # anonymize_lead; only the orphans are deleted outright.
        conv_delete = set(Conversation.objects.filter(pk__in=found['conversations'])
                          .filter(lead__isnull=True).values_list('pk', flat=True))
        # Same definition of "the lead's bookings" as the lead privacy actions
        # (linked, or same tenant + same email): those are anonymized with it.
        covered = set()
        for lead in ContactSubmission.objects.filter(pk__in=lead_ids):
            covered |= set(lead_service.lead_bookings(lead).values_list('pk', flat=True))
        booking_delete = set(found['bookings']) - covered

        self.stdout.write('\nEncontrado:')
        self.stdout.write(f'  leads a anonimizar: {len(lead_ids)}')
        self.stdout.write(f'  conversaciones sin lead a borrar: {len(conv_delete)}')
        self.stdout.write(f'  citas sin lead a borrar: {len(booking_delete)}')
        if found['mentions_other_lead']:
            self.stdout.write(
                f'  conversaciones de OTRO lead que mencionan el email (revisar a mano, '
                f'no se tocan): {sorted(found["mentions_other_lead"])}')
        if not (lead_ids or conv_delete or booking_delete):
            self.stdout.write('  (nada que borrar)')
            return
        if not opts['yes']:
            self.stdout.write('\nEnsayo. Repite con --yes para borrar.')
            return

        per_client = defaultdict(lambda: {'leads': 0, 'conv': 0, 'citas': 0})
        with transaction.atomic():
            for client_id in (Conversation.objects.filter(pk__in=conv_delete)
                              .values_list('client_id', flat=True)):
                per_client[client_id]['conv'] += 1
            for client_id in (Booking.objects.filter(pk__in=booking_delete)
                              .values_list('client_id', flat=True)):
                per_client[client_id]['citas'] += 1
            Conversation.objects.filter(pk__in=conv_delete).delete()
            Booking.objects.filter(pk__in=booking_delete).delete()
            for lead in ContactSubmission.objects.filter(pk__in=lead_ids).order_by('pk'):
                lead_service.anonymize_lead(lead)
                per_client[lead.client_id]['leads'] += 1
            for client_id, n in per_client.items():
                # Counts only: never the email or the phone that was erased.
                AuditEvent.objects.create(
                    client_id=client_id, action='visitor.erase', target='cli',
                    result=f"leads={n['leads']} conversaciones={n['conv']} citas={n['citas']}")

        self.stdout.write(self.style.SUCCESS(
            f'\nListo: {len(lead_ids)} lead(s) anonimizado(s), {len(conv_delete)} '
            f'conversación(es) y {len(booking_delete)} cita(s) borradas.'))
