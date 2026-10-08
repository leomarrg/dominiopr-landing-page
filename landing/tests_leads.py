"""Phase 7 -- lead detail (timeline + notes), delete / anonymize, and the full
tenant export. No model provider or email server is called anywhere here."""
import csv
import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client as HttpClient
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import leads as lead_service
from .models import (
    AuditEvent, AvailabilityRule, Booking, ChatMessage, Client, ContactSubmission,
    Conversation, KnowledgeSource, LeadNote, Membership, Survey, UsageEvent,
)

PW = 'pw12345!'
# Every piece of personal / free text the fixture plants for the lead.
PII = ('Maria Rivera', 'maria@example.com', '7875550101', 'Panaderia Rivera',
       'Quiero una cotizacion', 'https://rivera.example/contacto', '203.0.113.9',
       'Mozilla/5.0 RiveraBrowser', 'Mi numero privado es 939', 'nota secreta del lead',
       'Hola, me llamo Maria', 'pregunta de Maria', 'tema de Maria', 'motivo de Maria',
       'comentario de Maria', 'cita notas de Maria')
SECRET_COST = Decimal('3.918273')


def make_client(slug, **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', slug.title()), system_prompt='Vendemos cosas.',
        notify_email=f'owner@{slug}.example', **kw)


def make_user(username, client=None, role='org_admin', **kw):
    user = get_user_model().objects.create_user(
        username=username, email=f'{username}@example.com', password=PW, **kw)
    if client is not None:
        Membership.objects.create(user=user, client=client, role=role)
    return user


def login(username):
    http = HttpClient()
    assert http.login(username=username, password=PW)
    return http


def make_full_lead(client, email='maria@example.com'):
    """A lead with a linked conversation (+messages, survey, usage), a booking
    matched by email, and a note -- every PII string from PII planted."""
    lead = ContactSubmission.objects.create(
        client=client, name='Maria Rivera', email=email, phone='7875550101',
        company='Panaderia Rivera', service='consulting',
        message='Quiero una cotizacion', source='chat',
        page_url='https://rivera.example/contacto', ip_address='203.0.113.9',
        user_agent='Mozilla/5.0 RiveraBrowser', email_verified_at=timezone.now())
    conv = Conversation.objects.create(
        client=client, widget_session=f'{client.slug}-sess-1', lead=lead,
        page_url='https://rivera.example/contacto', first_question='pregunta de Maria',
        topic='tema de Maria', escalation_reason='motivo de Maria', tokens_used=120)
    ChatMessage.objects.create(conversation=conv, role='user', position=0,
                               content='Hola, me llamo Maria')
    ChatMessage.objects.create(conversation=conv, role='assistant', position=1,
                               content='Mi numero privado es 939')
    Survey.objects.create(conversation=conv, score=5, comment='comentario de Maria')
    booking = Booking.objects.create(
        client=client, name='Maria Rivera', email=email.upper(), service='Consulta',
        notes='cita notas de Maria', start=timezone.now() + timedelta(days=3))
    LeadNote.objects.create(lead=lead, text='nota secreta del lead')
    usage = UsageEvent.objects.create(
        client=client, client_slug=client.slug, conversation=conv, kind='chat',
        model='claude-haiku-5-5', input_tokens=100, output_tokens=20, api_calls=1,
        cost_usd=SECRET_COST)
    return lead, conv, booking, usage


def all_text():
    """Every stored value that could carry a lead's PII, as one string."""
    chunks = []
    for model in (ContactSubmission, LeadNote, Conversation, ChatMessage, Booking,
                  Survey, AuditEvent):
        for row in model.objects.values():
            chunks.extend(str(v) for v in row.values())
    return '\n'.join(chunks)


class LeadBase(TestCase):
    def setUp(self):
        cache.clear()
        self.a = make_client('acme')
        self.b = make_client('beta')
        self.admin_a = make_user('admin_a', self.a)
        self.agent_a = make_user('agent_a', self.a, role='agent')
        self.admin_b = make_user('admin_b', self.b)
        self.staff = make_user('staffer', is_staff=True)
        self.lead, self.conv, self.booking, self.usage = make_full_lead(self.a)

    def post(self, http, name, data=None, pk=None):
        return http.post(reverse(name, args=[pk or self.lead.pk]), data or {})


class ScopingAndRoleTests(LeadBase):
    def test_other_tenant_gets_404_everywhere(self):
        http = login('admin_b')
        self.assertEqual(http.get(reverse('lead_detail', args=[self.lead.pk])).status_code, 404)
        self.assertEqual(self.post(http, 'lead_note', {'text': 'x'}).status_code, 404)
        self.assertEqual(self.post(http, 'lead_delete', {'confirm': '1'}).status_code, 404)
        self.assertEqual(self.post(http, 'lead_anonymize', {'confirm': '1'}).status_code, 404)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.name, 'Maria Rivera')
        self.assertEqual(LeadNote.objects.count(), 1)

    def test_user_without_membership_gets_404(self):
        make_user('nobody')
        http = login('nobody')
        self.assertEqual(http.get(reverse('lead_detail', args=[self.lead.pk])).status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        resp = HttpClient().get(reverse('lead_detail', args=[self.lead.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('login', resp['Location'])

    def test_agent_can_read_and_add_note(self):
        http = login('agent_a')
        self.assertEqual(http.get(reverse('lead_detail', args=[self.lead.pk])).status_code, 200)
        resp = self.post(http, 'lead_note', {'text': 'Llamar el martes'})
        self.assertRedirects(resp, reverse('lead_detail', args=[self.lead.pk]))
        note = LeadNote.objects.get(text='Llamar el martes')
        self.assertEqual(note.author, self.agent_a)
        ev = AuditEvent.objects.get(action='lead.note')
        self.assertEqual(ev.target, f'lead:{self.lead.pk}')
        self.assertNotIn('martes', ev.target + ev.result)

    def test_agent_cannot_delete_or_anonymize(self):
        http = login('agent_a')
        for name in ('lead_delete', 'lead_anonymize'):
            self.assertEqual(self.post(http, name, {'confirm': '1'}).status_code, 403)
            self.assertEqual(self.post(http, name).status_code, 403)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.email, 'maria@example.com')
        self.assertTrue(Conversation.objects.filter(pk=self.conv.pk).exists())
        self.assertFalse(AuditEvent.objects.filter(action__in=('lead.delete', 'lead.anonymize')).exists())

    def test_agent_does_not_see_privacy_buttons(self):
        resp = login('agent_a').get(reverse('lead_detail', args=[self.lead.pk]))
        self.assertNotContains(resp, reverse('lead_delete', args=[self.lead.pk]))
        resp = login('admin_a').get(reverse('lead_detail', args=[self.lead.pk]))
        self.assertContains(resp, reverse('lead_delete', args=[self.lead.pk]))

    def test_get_on_actions_is_405(self):
        http = login('admin_a')
        for name in ('lead_note', 'lead_delete', 'lead_anonymize'):
            self.assertEqual(http.get(reverse(name, args=[self.lead.pk])).status_code, 405)

    def test_lead_without_client_is_staff_only(self):
        own = ContactSubmission.objects.create(name='Own', email='own@x.com',
                                               service='consulting', message='m')
        self.assertEqual(self.post(login('admin_a'), 'lead_delete', {'confirm': '1'},
                                   pk=own.pk).status_code, 404)
        resp = self.post(login('staffer'), 'lead_delete', {'confirm': '1'}, pk=own.pk)
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
        self.assertFalse(ContactSubmission.objects.filter(pk=own.pk).exists())


class NoteValidationTests(LeadBase):
    def test_note_over_limit_is_rejected(self):
        http = login('admin_a')
        self.post(http, 'lead_note', {'text': 'x' * (LeadNote.MAX_CHARS + 1)})
        self.assertEqual(LeadNote.objects.count(), 1)
        self.post(http, 'lead_note', {'text': 'x' * LeadNote.MAX_CHARS})
        self.assertEqual(LeadNote.objects.count(), 2)

    def test_blank_note_is_rejected(self):
        self.post(login('admin_a'), 'lead_note', {'text': '   '})
        self.assertEqual(LeadNote.objects.count(), 1)
        self.assertFalse(AuditEvent.objects.filter(action='lead.note').exists())


class DeleteTests(LeadBase):
    def test_first_post_only_asks_for_confirmation(self):
        resp = self.post(login('admin_a'), 'lead_delete')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="confirm" value="1"')
        self.assertEqual(resp.context['counts'],
                         {'conversations': 1, 'messages': 2, 'bookings': 1, 'notes': 1})
        self.assertTrue(ContactSubmission.objects.filter(pk=self.lead.pk).exists())

    def test_org_admin_delete_removes_everything_but_usage(self):
        other_conv = Conversation.objects.create(client=self.a, widget_session='other')
        other_booking = Booking.objects.create(
            client=self.b, name='X', email='maria@example.com',
            start=timezone.now() + timedelta(days=5))
        resp = self.post(login('admin_a'), 'lead_delete', {'confirm': '1'})
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
        pk = self.lead.pk
        self.assertFalse(ContactSubmission.objects.filter(pk=pk).exists())
        self.assertFalse(Conversation.objects.filter(pk=self.conv.pk).exists())
        self.assertFalse(ChatMessage.objects.filter(conversation_id=self.conv.pk).exists())
        self.assertFalse(Survey.objects.exists())
        self.assertFalse(Booking.objects.filter(pk=self.booking.pk).exists())
        self.assertFalse(LeadNote.objects.exists())
        # Unrelated rows survive -- including another tenant's booking with the same email.
        self.assertTrue(Conversation.objects.filter(pk=other_conv.pk).exists())
        self.assertTrue(Booking.objects.filter(pk=other_booking.pk).exists())
        usage = UsageEvent.objects.get(pk=self.usage.pk)
        self.assertIsNone(usage.conversation_id)
        self.assertEqual(usage.cost_usd, SECRET_COST)
        ev = AuditEvent.objects.get(action='lead.delete')
        self.assertEqual(ev.target, f'lead:{pk}')
        self.assertEqual(ev.client, self.a)
        self.assertEqual(ev.user, self.admin_a)
        for s in PII:
            self.assertNotIn(s, ev.target + ev.result)
        self.assertNotIn('Maria', all_text())

    def test_staff_can_delete_any_tenant_lead(self):
        self.post(login('staffer'), 'lead_delete', {'confirm': '1'})
        self.assertFalse(ContactSubmission.objects.filter(pk=self.lead.pk).exists())


class AnonymizeTests(LeadBase):
    def test_anonymize_keeps_row_and_leaves_no_pii_anywhere(self):
        old_session = self.conv.widget_session
        login('admin_a').get(reverse('lead_detail', args=[self.lead.pk]))  # audit trail too
        resp = self.post(login('admin_a'), 'lead_anonymize', {'confirm': '1'})
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)

        lead = ContactSubmission.objects.get(pk=self.lead.pk)
        self.assertEqual(lead.name, lead_service.ANON_NAME)
        for field in ('email', 'phone', 'company', 'message', 'page_url', 'user_agent'):
            self.assertEqual(getattr(lead, field), '', field)
        self.assertIsNone(lead.ip_address)
        self.assertIsNone(lead.email_verified_at)
        # Statistics survive.
        self.assertEqual((lead.source, lead.service, lead.client_id), ('chat', 'consulting', self.a.pk))

        self.assertFalse(LeadNote.objects.filter(lead=lead).exists())
        conv = Conversation.objects.get(pk=self.conv.pk)
        self.assertEqual(conv.lead_id, lead.pk)
        self.assertEqual((conv.first_question, conv.topic, conv.escalation_reason, conv.page_url),
                         ('', '', '', ''))
        self.assertNotEqual(conv.widget_session, old_session)
        self.assertEqual(conv.tokens_used, 120)
        self.assertEqual(set(conv.chat_messages.values_list('content', flat=True)),
                         {lead_service.ANON_TEXT})
        self.assertEqual(Survey.objects.get().comment, '')
        self.assertEqual(Survey.objects.get().score, 5)
        booking = Booking.objects.get(pk=self.booking.pk)
        self.assertEqual((booking.name, booking.email, booking.notes),
                         (lead_service.ANON_NAME, '', ''))
        self.assertEqual(booking.lead_id, lead.pk)
        self.assertTrue(booking.reminder_sent)

        text = all_text()
        for s in PII:
            self.assertNotIn(s, text)
        ev = AuditEvent.objects.get(action='lead.anonymize')
        self.assertEqual(ev.target, f'lead:{lead.pk}')

        # The detail page still renders, without any of it, and keeps the booking.
        page = login('admin_a').get(reverse('lead_detail', args=[lead.pk]))
        self.assertEqual(page.status_code, 200)
        for s in PII:
            self.assertNotContains(page, s)
        self.assertIn('booking', [it['kind'] for it in page.context['timeline']])

    def test_anonymize_is_idempotent(self):
        http = login('admin_a')
        self.post(http, 'lead_anonymize', {'confirm': '1'})
        self.post(http, 'lead_anonymize', {'confirm': '1'})
        self.assertEqual(ContactSubmission.objects.get(pk=self.lead.pk).name, lead_service.ANON_NAME)


class TimelineTests(LeadBase):
    def test_timeline_is_chronological_across_kinds(self):
        base = self.lead.created_at
        LeadNote.objects.filter(lead=self.lead).update(created_at=base + timedelta(hours=3))
        Conversation.objects.filter(pk=self.conv.pk).update(started_at=base + timedelta(hours=1))
        Booking.objects.filter(pk=self.booking.pk).update(created_at=base + timedelta(hours=2))
        ev = AuditEvent.objects.create(client=self.a, action='lead.status',
                                       target=f'lead:{self.lead.pk}', result='won')
        AuditEvent.objects.filter(pk=ev.pk).update(created_at=base + timedelta(hours=4))
        # Another lead's audit row never leaks in (lead:1 vs lead:11 style).
        AuditEvent.objects.create(client=self.a, action='lead.status',
                                  target=f'lead:{self.lead.pk}1', result='lost')

        resp = login('admin_a').get(reverse('lead_detail', args=[self.lead.pk]))
        kinds = [it['kind'] for it in resp.context['timeline']]
        # lead.view (written by this very request) is excluded from the timeline.
        self.assertEqual(kinds, ['created', 'conversation', 'booking', 'note', 'audit'])
        stamps = [it['at'] for it in resp.context['timeline']]
        self.assertEqual(stamps, sorted(stamps))
        self.assertContains(resp, 'Hola, me llamo Maria')  # transcript excerpt
        self.assertContains(resp, reverse('conversation_detail', args=[self.conv.pk]))
        self.assertTrue(AuditEvent.objects.filter(action='lead.view').exists())

    def test_clinic_client_hides_transcript_text(self):
        clinic = make_client('clinica', clinic_mode=True)
        make_user('admin_c', clinic)
        lead, conv, _booking, _usage = make_full_lead(clinic, email='p@example.com')
        resp = login('admin_c').get(reverse('lead_detail', args=[lead.pk]))
        self.assertContains(resp, 'Conversación (modo clínica)')
        for s in ('Hola, me llamo Maria', 'Mi numero privado', 'pregunta de Maria',
                  'tema de Maria', 'cita notas de Maria'):
            self.assertNotContains(resp, s)
        conv_items = [it for it in resp.context['timeline'] if it['kind'] == 'conversation']
        self.assertEqual(conv_items[0]['excerpt'], [])

    def test_list_links_to_detail(self):
        resp = login('admin_a').get(reverse('dashboard'))
        self.assertContains(resp, reverse('lead_detail', args=[self.lead.pk]))

    def test_status_selector_and_reply_form_present(self):
        resp = login('admin_a').get(reverse('lead_detail', args=[self.lead.pk]))
        self.assertContains(resp, reverse('lead_status', args=[self.lead.pk]))
        self.assertContains(resp, reverse('lead_email', args=[self.lead.pk]))
        self.assertContains(resp, 'Correo confirmado')


class ExportTests(TestCase):
    def setUp(self):
        cache.clear()
        self.a = make_client('acme')
        self.b = make_client('beta')
        self.token = self.a.ensure_widget_token()
        self.b.ensure_widget_token()
        make_user('admin_a', self.a)
        make_user('agent_a', self.a, role='agent')
        make_user('staffer', is_staff=True)
        self.lead, self.conv, self.booking, _ = make_full_lead(self.a)
        ContactSubmission.objects.create(
            client=self.a, name='=HYPERLINK("http://evil.example")', email='evil@example.com',
            service='consulting', message='+cmd', source='contact_form')
        make_full_lead(self.b, email='beta-only@example.com')
        ContactSubmission.objects.filter(client=self.b).update(name='Solo Beta')
        KnowledgeSource.objects.create(client=self.a, title='Horario', content='Abrimos 9am',
                                       status='active')
        KnowledgeSource.objects.create(client=self.a, title='Viejo', content='archivado x',
                                       status='archived')
        AvailabilityRule.objects.create(client=self.a, weekday=0, open_time='09:00',
                                        close_time='17:00')
        self.a.install_notes = 'wp-admin clave supersecreta'
        self.a.save(update_fields=['install_notes'])
        self.out = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.out, ignore_errors=True)

    def run_export(self):
        stdout = io.StringIO()
        call_command('export_tenant', client='acme', out=self.out, stdout=stdout)
        files = os.listdir(self.out)
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r'^acme-export-\d{8}\.zip$')
        self.assertIn(files[0], stdout.getvalue())
        return zipfile.ZipFile(os.path.join(self.out, files[0])), stdout.getvalue()

    def read_all(self, zf):
        return {n: zf.read(n).decode('utf-8-sig') for n in zf.namelist()}

    def test_zip_has_every_table_for_this_tenant_only(self):
        zf, stdout = self.run_export()
        contents = self.read_all(zf)
        tables = ('leads', 'lead_notes', 'conversations', 'messages', 'bookings',
                  'knowledge_sources', 'availability_rules', 'members', 'surveys',
                  'usage_monthly')
        expected = {f'acme/{t}.{ext}' for t in tables for ext in ('json', 'csv')}
        expected |= {'acme/client.json', 'acme/client.csv', 'acme/manifest.json'}
        self.assertEqual(set(contents), expected)
        self.assertIn('leads: 2', stdout)

        data = {t: json.loads(contents[f'acme/{t}.json']) for t in tables}
        self.assertEqual(len(data['leads']), 2)
        self.assertEqual(len(data['lead_notes']), 1)
        self.assertEqual(len(data['conversations']), 1)
        self.assertEqual(len(data['messages']), 2)
        self.assertEqual(len(data['bookings']), 1)
        self.assertEqual([k['title'] for k in data['knowledge_sources']], ['Horario'])
        self.assertEqual(len(data['availability_rules']), 1)
        self.assertEqual({(m['email'], m['role']) for m in data['members']},
                         {('admin_a@example.com', 'org_admin'), ('agent_a@example.com', 'agent')})
        self.assertEqual(len(data['surveys']), 1)
        self.assertEqual(data['usage_monthly'][0]['messages'], 1)
        self.assertEqual(data['usage_monthly'][0]['total_tokens'], 120)
        config = json.loads(contents['acme/client.json'])
        self.assertEqual(config['slug'], 'acme')

        blob = '\n'.join(contents.values())
        for leaked in ('Solo Beta', 'beta-only@example.com', 'beta-sess', 'owner@beta.example',
                       self.token, self.b.widget_token, 'supersecreta', 'archivado x',
                       '203.0.113.9', 'RiveraBrowser', 'acme-sess-1'):
            self.assertNotIn(leaked, blob)
        self.assertNotIn('widget_token', blob)
        self.assertNotIn('cost', blob.lower())
        self.assertNotIn(str(SECRET_COST), blob)
        # One JSON per file must stay valid even with accents and quotes.
        self.assertTrue(AuditEvent.objects.filter(action='tenant.export', target='acme').exists())

    def test_csv_cells_are_formula_safe(self):
        zf, _ = self.run_export()
        rows = list(csv.reader(io.StringIO(zf.read('acme/leads.csv').decode('utf-8-sig'))))
        header = rows[0]
        names = [r[header.index('name')] for r in rows[1:]]
        messages = [r[header.index('message')] for r in rows[1:]]
        self.assertIn('\'=HYPERLINK("http://evil.example")', names)
        self.assertIn("'+cmd", messages)
        # JSON keeps the raw value (it is data, not a spreadsheet).
        raw = json.loads(zf.read('acme/leads.json').decode('utf-8'))
        self.assertIn('=HYPERLINK("http://evil.example")', [r['name'] for r in raw])

    def test_unknown_slug_and_missing_dir_fail(self):
        with self.assertRaises(CommandError):
            call_command('export_tenant', client='nope', out=self.out, stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('export_tenant', client='acme', out=os.path.join(self.out, 'x'),
                         stdout=io.StringIO())

    def test_staff_download_from_control_center(self):
        url = reverse('control_export_tenant', args=['acme'])
        http = login('staffer')
        self.assertEqual(http.get(url).status_code, 405)
        resp = http.post(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/zip')
        self.assertIn('acme-export-', resp['Content-Disposition'])
        zf = zipfile.ZipFile(io.BytesIO(b''.join(resp.streaming_content)))
        self.assertIn('acme/leads.json', zf.namelist())
        ev = AuditEvent.objects.get(action='tenant.export')
        self.assertEqual(ev.user.username, 'staffer')
        self.assertNotIn('maria', (ev.target + ev.result).lower())
        page = http.get(reverse('control_client', args=['acme']))
        self.assertContains(page, 'Exportar datos del cliente')

    def test_tenant_admin_cannot_download(self):
        resp = login('admin_a').post(reverse('control_export_tenant', args=['acme']))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(AuditEvent.objects.filter(action='tenant.export').exists())
