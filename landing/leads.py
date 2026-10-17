"""Phase 7 -- lead detail domain logic: what belongs to a lead, its timeline,
and the two privacy actions (delete / anonymize).

Views stay thin and call these. One definition of "the rows that belong to
this lead" (related_rows) is shared by the timeline, the confirmation page
counts, delete and anonymize, so what the operator is shown is exactly what
gets removed.
"""
import secrets

from django.db import transaction
from django.db.models import Q

from .models import AuditEvent, Booking, ChatMessage, ContactSubmission, Conversation, Survey

ANON_NAME = 'Anonimizado'
ANON_TEXT = '[anonimizado]'
# Transcript lines shown per conversation in the lead timeline.
EXCERPT_MESSAGES = 4
EXCERPT_CHARS = 220
# Timeline audit rows: bounded so a lead with a long history can't blow up the page.
TIMELINE_AUDIT_LIMIT = 100
# Audit actions that are noise on the lead's own timeline.
TIMELINE_AUDIT_EXCLUDE = ('lead.view',)

AUDIT_LABELS = {
    'lead.status': 'Estado cambiado',
    'lead.email': 'Correo enviado al lead',
    'lead.note': 'Nota añadida',
    'lead.anonymize': 'Lead anonimizado',
    'lead.assign': 'Asignación cambiada',
    'lead.follow_up': 'Seguimiento',
    'lead.tags': 'Etiquetas',
    'lead.lost': 'Motivo de pérdida',
}


def audit_target(lead):
    return f'lead:{lead.pk}'


def lead_conversations(lead):
    return Conversation.objects.filter(lead=lead)


def lead_bookings(lead):
    """Bookings that belong to this lead: explicitly linked, or made in the
    same tenant with the same email (the booking tool does not link the lead
    itself, so without the email match the timeline would never show one).
    Scoped to the lead's own client, so another tenant's booking is never
    matched."""
    cond = Q(lead=lead)
    if lead.email:
        owner = Q(client_id=lead.client_id) if lead.client_id else Q(client__isnull=True)
        cond |= owner & Q(email__iexact=lead.email)
    return Booking.objects.filter(cond)


def related_counts(lead):
    """What delete/anonymize will touch, for the confirmation page."""
    convs = lead_conversations(lead)
    return {
        'conversations': convs.count(),
        'messages': ChatMessage.objects.filter(conversation__in=convs).count(),
        'bookings': lead_bookings(lead).count(),
        'notes': lead.notes.count(),
    }


def _excerpt(text):
    text = ' '.join((text or '').split())
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS - 1] + '…'


def build_timeline(lead, clinic=False):
    """One chronological list (oldest first) of everything that happened to
    this lead: capture, notes, linked conversations, bookings and the audit
    trail of actions on it. A fixed number of queries per lead.

    `clinic` (Client.clinic_mode): conversations are listed without any
    transcript text, first question or topic, and bookings without notes or
    service -- the same data minimization the notification emails apply."""
    items = [{'kind': 'created', 'at': lead.created_at, 'order': 0}]

    for note in lead.notes.select_related('author'):
        items.append({'kind': 'note', 'at': note.created_at, 'order': 1, 'note': note,
                      'author': note.author.username if note.author else 'Usuario eliminado'})

    convs = list(lead_conversations(lead).order_by('started_at', 'pk'))
    excerpts = {}
    if convs and not clinic:
        rows = (ChatMessage.objects
                .filter(conversation__in=convs, position__lt=EXCERPT_MESSAGES)
                .order_by('conversation_id', 'position')
                .values_list('conversation_id', 'role', 'content'))
        for conv_id, role, content in rows:
            excerpts.setdefault(conv_id, []).append({'role': role, 'text': _excerpt(content)})
    for conv in convs:
        items.append({'kind': 'conversation', 'at': conv.started_at, 'order': 2,
                      'conv': conv, 'clinic': clinic,
                      'excerpt': [] if clinic else excerpts.get(conv.pk, [])})

    for booking in lead_bookings(lead).order_by('created_at', 'pk'):
        items.append({'kind': 'booking', 'at': booking.created_at, 'order': 3,
                      'booking': booking, 'clinic': clinic})

    # Every lead.* audit row is written with client=lead.client; filtering on
    # it first rides the client FK index (target itself is not indexed).
    owner = {'client_id': lead.client_id} if lead.client_id else {'client__isnull': True}
    audits = (AuditEvent.objects
              .filter(target=audit_target(lead), **owner)
              .exclude(action__in=TIMELINE_AUDIT_EXCLUDE)
              .select_related('user')
              .order_by('-created_at')[:TIMELINE_AUDIT_LIMIT])
    for ev in audits:
        items.append({'kind': 'audit', 'at': ev.created_at, 'order': 4, 'event': ev,
                      'label': AUDIT_LABELS.get(ev.action, ev.action),
                      'author': ev.user.username if ev.user else 'Sistema'})

    items.sort(key=lambda it: (it['at'], it['order']))
    return items


@transaction.atomic
def delete_lead(lead):
    """Hard delete: the lead's conversations (messages and surveys cascade),
    its bookings, then the lead itself (notes cascade). UsageEvent rows hold
    no PII and survive with conversation=NULL (SET_NULL) so cost history stays
    whole. Conversations go first: deleting the lead first would SET_NULL the
    link and the transcripts would be left behind, unattached."""
    counts = related_counts(lead)
    lead_conversations(lead).delete()
    lead_bookings(lead).delete()
    ContactSubmission.objects.filter(pk=lead.pk).delete()
    return counts


@transaction.atomic
def anonymize_lead(lead):
    """Keep the row (and the linked conversations/bookings) for statistics --
    source, status, service, dates, states and token counts survive -- but
    remove every piece of personal or free text. Idempotent."""
    locked = ContactSubmission.objects.select_for_update().get(pk=lead.pk)
    counts = related_counts(locked)
    convs = lead_conversations(locked)
    conv_ids = list(convs.values_list('pk', flat=True))
    # Bookings are matched partly by email; resolve them BEFORE the email is
    # blanked, then pin them to the lead so they stay on its timeline.
    booking_ids = list(lead_bookings(locked).values_list('pk', flat=True))

    ChatMessage.objects.filter(conversation_id__in=conv_ids).update(content=ANON_TEXT)
    Survey.objects.filter(conversation_id__in=conv_ids).update(comment='')
    for conv_id in conv_ids:
        # A fresh widget_session per row: the widget upserts on (client,
        # session), so a returning visitor would otherwise keep appending new,
        # identifiable messages to the anonymized conversation.
        Conversation.objects.filter(pk=conv_id).update(
            first_question='', topic='', escalation_reason='', page_url='',
            # The "termina en 1234" label is partial phone data: gone too.
            contact_label='',
            widget_session=f'anon-{secrets.token_hex(12)}')
    Booking.objects.filter(pk__in=booking_ids).update(
        lead=locked, name=ANON_NAME, email='', phone='', notes='',
        # No address left to remind: keep the reminder cron from mailing ''.
        reminder_sent=True)
    locked.notes.all().delete()

    locked.name = ANON_NAME
    locked.email = ''
    locked.phone = ''
    locked.company = ''
    locked.message = ''
    locked.page_url = ''
    locked.ip_address = None
    locked.user_agent = ''
    locked.email_verified_at = None
    # CRM fields the team typed: a loss reason or a tag can carry personal
    # (even health) data, e.g. "diabetes tipo 2" or "Diabético".
    locked.lost_reason = ''
    locked.tags.clear()
    locked.save(update_fields=[
        'name', 'email', 'phone', 'company', 'message', 'page_url',
        'ip_address', 'user_agent', 'email_verified_at', 'lost_reason'])
    return counts
