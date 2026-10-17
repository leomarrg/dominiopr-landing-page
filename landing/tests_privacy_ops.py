"""Checklist de producción, frente C: derechos de datos y analítica.

- delete_tenant: ensayo por defecto, exporta, borra todo lo del cliente en una
  transacción, conserva auditoría / webhooks / uso, desactiva logins huérfanos.
- erase_visitor: por email o teléfono (WhatsApp por contact_hash), anonimiza
  leads y borra conversaciones y citas sin lead; auditoría sin PII.
- user_remove: un login sin membresías queda desactivado (no borrado) y una
  invitación nueva lo reactiva.
- Política de privacidad y eventos GA4 solo con consentimiento.
"""
import os
import shutil
import tempfile
import zipfile
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    AuditEvent, AvailabilityRule, Booking, ChannelAccount, ChannelOptOut, ChatMessage, Client,
    ContactSubmission, Conversation, KnowledgeSource, LeadNote, LeadTag, Location, Membership,
    ProcessedWebhookEvent, StaffContact, Subscription, Survey, UsageEvent,
)
from .whatsapp import contact_hash

User = get_user_model()


def make_client(slug, **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', slug.title()), system_prompt='Vendemos cosas.',
        notify_email=kw.pop('notify_email', f'owner@{slug}.com'), **kw)


def make_member(email, client, role='org_admin'):
    user = User.objects.filter(username=email).first() or User.objects.create_user(
        email, email=email, password='pw12345!')
    Membership.objects.create(user=user, client=client, role=role)
    return user


def run(*args, **kw):
    out = StringIO()
    call_command(*args, stdout=out, stderr=StringIO(), **kw)
    return out.getvalue()


def populate(client, email='ana@example.com'):
    """One row of every table the tenant owns."""
    lead = ContactSubmission.objects.create(
        client=client, name='Ana', email=email, phone='+17875551234',
        service='consulting', message='Hola')
    tag = LeadTag.objects.create(client=client, name='VIP')
    lead.tags.add(tag)
    LeadNote.objects.create(lead=lead, text='Llamar el lunes')
    conv = Conversation.objects.create(client=client, widget_session=f's-{client.slug}', lead=lead)
    ChatMessage.objects.create(conversation=conv, role='user', content='Hola', position=0)
    Survey.objects.create(conversation=conv, score=5)
    loc = Location.objects.create(client=client, name='Bayamón')
    AvailabilityRule.objects.create(client=client, weekday=0, open_time='09:00',
                                    close_time='17:00', location=loc)
    Booking.objects.create(client=client, name='Ana', email=email,
                           start=timezone.now() + timedelta(days=2))
    KnowledgeSource.objects.create(client=client, title='FAQ', content='Abrimos a las 9.',
                                   status='active')
    StaffContact.objects.create(client=client, name='Ventas', email=f'ventas@{client.slug}.com')
    ChannelAccount.objects.create(client=client, phone_number_id=f'pn-{client.slug}')
    ChannelOptOut.objects.create(client=client, contact_hash='abc123')
    UsageEvent.objects.create(client=client, client_slug=client.slug, kind='chat',
                              conversation=conv, cost_usd='0.01')
    return lead


class DeleteTenantTests(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = make_client('acme')
        self.other = make_client('otro')
        populate(self.acme)
        populate(self.other, email='luis@example.com')
        self.only_acme = make_member('solo@acme.com', self.acme)
        self.both = make_member('ambos@acme.com', self.acme, role='agent')
        make_member('ambos@acme.com', self.other)
        self.staff = User.objects.create_user('staff@dominiopr.com', password='x', is_staff=True)
        Membership.objects.create(user=self.staff, client=self.acme)
        Subscription.objects.create(client=self.acme, plan='starter', status='canceled',
                                    order_number='DOM-2026-0001')
        AuditEvent.objects.create(client=self.acme, action='lead.status', target='lead:1')
        ProcessedWebhookEvent.objects.create(event_id='evt_1')
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_dry_run_lists_and_touches_nothing(self):
        out = run('delete_tenant', client='acme')
        self.assertIn('conversaciones: 1', out)
        self.assertIn('logins a desactivar (sin otras membresías): 1', out)
        self.assertIn('Ensayo', out)
        self.assertTrue(Client.objects.filter(slug='acme', is_active=True).exists())
        self.assertEqual(ContactSubmission.objects.filter(client=self.acme).count(), 1)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_refuses_dominio(self):
        Client.objects.get_or_create(slug='dominio', defaults={
            'name': 'DOMINIO', 'system_prompt': 'x', 'notify_email': 'hola@dominiopr.com'})
        with self.assertRaises(CommandError):
            run('delete_tenant', client='dominio', yes=True, skip_export=True)
        self.assertTrue(Client.objects.filter(slug='dominio').exists())

    def test_refuses_a_subscription_that_still_bills(self):
        Subscription.objects.filter(client=self.acme).update(status='active')
        with self.assertRaises(CommandError):
            run('delete_tenant', client='acme', yes=True, export=self.tmp)
        self.assertTrue(Client.objects.filter(slug='acme', is_active=True).exists())

    def test_yes_requires_export_or_explicit_skip(self):
        with self.assertRaises(CommandError):
            run('delete_tenant', client='acme', yes=True)
        self.assertTrue(Client.objects.filter(slug='acme').exists())

    def test_kept_audit_trail_loses_the_emails(self):
        AuditEvent.objects.create(client=self.acme, action='user.invite',
                                  target='solo@acme.com', result='rol=agent ambos@acme.com')
        run('delete_tenant', client='acme', yes=True, skip_export=True)
        ev = AuditEvent.objects.get(action='user.invite')
        self.assertIsNone(ev.client_id)
        self.assertNotIn('@acme.com', ev.target + ev.result)
        self.assertIn('[correo borrado]', ev.target)

    def test_export_then_delete_everything_of_the_tenant(self):
        run('delete_tenant', client='acme', yes=True, export=self.tmp)

        # The export was written first.
        zips = [f for f in os.listdir(self.tmp) if f.endswith('.zip')]
        self.assertEqual(len(zips), 1)
        with zipfile.ZipFile(os.path.join(self.tmp, zips[0])) as zf:
            self.assertIn('acme/leads.json', zf.namelist())

        self.assertFalse(Client.objects.filter(slug='acme').exists())
        for model in (ContactSubmission, Conversation, Booking, KnowledgeSource, Location,
                      StaffContact, ChannelAccount, ChannelOptOut, LeadTag):
            self.assertEqual(model.objects.filter(client__slug='otro').count(), 1, model)
            self.assertEqual(model.objects.count(), 1, model)     # only the other tenant's
        self.assertFalse(Subscription.objects.exists())
        self.assertEqual(ChatMessage.objects.count(), 1)
        self.assertEqual(Survey.objects.count(), 1)
        self.assertEqual(LeadNote.objects.count(), 1)
        self.assertEqual(AvailabilityRule.objects.count(), 1)
        self.assertFalse(Membership.objects.filter(client__slug='acme').exists())

        # Kept: audit (now without client), webhook ledger, usage (no PII).
        self.assertTrue(AuditEvent.objects.filter(action='lead.status', client=None).exists())
        ev = AuditEvent.objects.get(action='tenant.delete')
        self.assertEqual(ev.target, 'acme')
        self.assertIn('leads=1', ev.result)
        self.assertIn('DOM-2026-0001', ev.result)
        self.assertTrue(AuditEvent.objects.filter(action='tenant.export').exists())
        self.assertTrue(ProcessedWebhookEvent.objects.filter(event_id='evt_1').exists())
        usage = UsageEvent.objects.get(client_slug='acme')
        self.assertIsNone(usage.client_id)
        self.assertIsNone(usage.conversation_id)

        # Logins: the orphan is deactivated (not deleted); the others stay.
        self.only_acme.refresh_from_db()
        self.assertFalse(self.only_acme.is_active)
        self.both.refresh_from_db()
        self.assertTrue(self.both.is_active)
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_active)

    def test_skip_export_deletes_without_zip(self):
        run('delete_tenant', client='acme', yes=True, skip_export=True)
        self.assertFalse(Client.objects.filter(slug='acme').exists())
        self.assertFalse(AuditEvent.objects.filter(action='tenant.export').exists())


class EraseVisitorTests(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = make_client('acme')
        self.other = make_client('otro')

    def test_dry_run_changes_nothing(self):
        lead = ContactSubmission.objects.create(client=self.acme, name='Ana',
                                                email='ana@example.com', message='x')
        out = run('erase_visitor', email='ana@example.com')
        self.assertIn('leads a anonimizar: 1', out)
        lead.refresh_from_db()
        self.assertEqual(lead.name, 'Ana')
        self.assertFalse(AuditEvent.objects.filter(action='visitor.erase').exists())

    def test_by_email_anonymizes_leads_and_deletes_orphans(self):
        lead = ContactSubmission.objects.create(client=self.acme, name='Ana',
                                                email='Ana@Example.com', message='Soy Ana')
        linked = Conversation.objects.create(client=self.acme, widget_session='a1', lead=lead)
        ChatMessage.objects.create(conversation=linked, role='user', content='hola', position=0)
        orphan_booking = Booking.objects.create(client=self.acme, name='Ana', email='ana@example.com',
                                                start=timezone.now() + timedelta(days=1))
        # A web chat where she typed her email but no lead was captured.
        mention = Conversation.objects.create(client=self.acme, widget_session='a2')
        ChatMessage.objects.create(conversation=mention, role='user',
                                   content='mi correo es ana@example.com', position=0)
        # Another person's transcript that mentions her: reported, not touched.
        luis = ContactSubmission.objects.create(client=self.acme, name='Luis',
                                                email='luis@example.com', message='x')
        foreign = Conversation.objects.create(client=self.acme, widget_session='a3', lead=luis)
        ChatMessage.objects.create(conversation=foreign, role='user',
                                   content='escríbele a ana@example.com', position=0)
        untouched = Conversation.objects.create(client=self.acme, widget_session='a4')

        out = run('erase_visitor', email='ana@example.com', yes=True)

        lead.refresh_from_db()
        self.assertEqual(lead.name, 'Anonimizado')
        self.assertEqual(lead.email, '')
        self.assertEqual(ChatMessage.objects.get(conversation=linked).content, '[anonimizado]')
        # The booking matched by email is pinned to the lead and anonymized.
        orphan_booking.refresh_from_db()
        self.assertEqual(orphan_booking.email, '')
        self.assertFalse(Conversation.objects.filter(pk=mention.pk).exists())
        self.assertTrue(Conversation.objects.filter(pk__in=[foreign.pk, untouched.pk]).count() == 2)
        self.assertIn(str(foreign.pk), out)
        luis.refresh_from_db()
        self.assertEqual(luis.name, 'Luis')

        ev = AuditEvent.objects.get(action='visitor.erase')
        self.assertEqual(ev.client, self.acme)
        self.assertNotIn('ana', (ev.target + ev.result).lower())

    def test_booking_without_lead_is_deleted(self):
        booking = Booking.objects.create(client=self.acme, name='Ana', email='ana@example.com',
                                         start=timezone.now() + timedelta(days=1))
        run('erase_visitor', email='ana@example.com', yes=True)
        self.assertFalse(Booking.objects.filter(pk=booking.pk).exists())

    def test_by_phone_finds_whatsapp_by_contact_hash(self):
        chash = contact_hash('17875551234')
        orphan = Conversation.objects.create(client=self.acme, channel='whatsapp',
                                             external_id=f'wa:{chash}:20261001',
                                             contact_label='WhatsApp · termina en 1234')
        ChatMessage.objects.create(conversation=orphan, role='user', content='hola', position=0)
        wa_lead = ContactSubmission.objects.create(client=self.acme, name='Pedro',
                                                   email='pedro@example.com', message='x')
        with_lead = Conversation.objects.create(client=self.acme, channel='whatsapp',
                                                external_id=f'wa:{chash}:20261002', lead=wa_lead)
        typed = ContactSubmission.objects.create(client=self.acme, name='Ana',
                                                 phone='+17875551234', message='x')
        wa_booking = Booking.objects.create(client=self.acme, name='Ana', wa_id='17875551234',
                                            channel='whatsapp',
                                            start=timezone.now() + timedelta(days=1))
        typed_booking = Booking.objects.create(client=self.acme, name='Ana', phone='787-555-1234',
                                               start=timezone.now() + timedelta(days=3))
        other_number = Conversation.objects.create(
            client=self.acme, channel='whatsapp',
            external_id=f'wa:{contact_hash("17875559999")}:20261001')
        opt_out = ChannelOptOut.objects.create(client=self.acme, contact_hash=chash)

        run('erase_visitor', phone='(787) 555-1234', yes=True)

        self.assertFalse(Conversation.objects.filter(pk=orphan.pk).exists())
        self.assertTrue(Conversation.objects.filter(pk=with_lead.pk).exists())
        with_lead.refresh_from_db()
        self.assertEqual(with_lead.contact_label, '')
        for obj in (wa_lead, typed):
            obj.refresh_from_db()
            self.assertEqual(obj.name, 'Anonimizado')
        self.assertFalse(Booking.objects.filter(pk__in=[wa_booking.pk, typed_booking.pk]).exists())
        self.assertTrue(Conversation.objects.filter(pk=other_number.pk).exists())
        # The STOP record is kept (salted hash, proof of the opt-out).
        self.assertTrue(ChannelOptOut.objects.filter(pk=opt_out.pk).exists())

    def test_client_scope(self):
        mine = ContactSubmission.objects.create(client=self.acme, name='Ana',
                                                email='ana@example.com', message='x')
        theirs = ContactSubmission.objects.create(client=self.other, name='Ana',
                                                  email='ana@example.com', message='x')
        run('erase_visitor', email='ana@example.com', client='acme', yes=True)
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.name, 'Anonimizado')
        self.assertEqual(theirs.name, 'Ana')

    def test_rejects_bad_input(self):
        with self.assertRaises(CommandError):
            run('erase_visitor', phone='123')
        with self.assertRaises(CommandError):
            run('erase_visitor', email='no-es-email')


class UserRemoveDeactivationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = make_client('acme')
        self.other = make_client('otro')
        self.admin = make_member('admin@acme.com', self.acme)
        self.http = HttpClient()
        self.http.login(username='admin@acme.com', password='pw12345!')

    def _remove(self, user):
        m = Membership.objects.get(user=user, client=self.acme)
        return self.http.post(reverse('user_remove', args=[m.pk]) + '?client=acme', follow=True)

    def test_last_membership_deactivates_the_login(self):
        agent_user = make_member('agente@acme.com', self.acme, role='agent')
        resp = self._remove(agent_user)
        self.assertContains(resp, 'quedó desactivada')
        agent_user.refresh_from_db()
        self.assertFalse(agent_user.is_active)            # not deleted
        self.assertTrue(AuditEvent.objects.filter(action='user.deactivate',
                                                  target='agente@acme.com').exists())
        self.assertFalse(HttpClient().login(username='agente@acme.com', password='pw12345!'))

    def test_login_with_other_memberships_stays_active(self):
        shared = make_member('compartido@acme.com', self.acme, role='agent')
        make_member('compartido@acme.com', self.other)
        resp = self._remove(shared)
        self.assertContains(resp, 'la cuenta sigue existiendo')
        shared.refresh_from_db()
        self.assertTrue(shared.is_active)
        self.assertFalse(AuditEvent.objects.filter(action='user.deactivate').exists())

    def test_reinvite_reactivates(self):
        agent_user = make_member('agente@acme.com', self.acme, role='agent')
        self._remove(agent_user)
        self.http.post(reverse('user_invite') + '?client=acme',
                       {'email': 'agente@acme.com', 'role': 'agent'})
        agent_user.refresh_from_db()
        self.assertTrue(agent_user.is_active)
        self.assertTrue(Membership.objects.filter(user=agent_user, client=self.acme).exists())


class PrivacyPageTests(TestCase):
    def test_deletion_request_section(self):
        resp = self.client.get('/privacy/')
        for text in ('Cómo Pedir que Borremos sus Datos', 'hola@dominiopr.com', '30 días',
                     'Anthropic', 'Meta (WhatsApp Business)', 'Stripe', 'Twilio',
                     'Google Analytics'):
            self.assertContains(resp, text)


@override_settings(GA_MEASUREMENT_ID='G-TEST123', CONTACT_NOTIFY_EMAIL='leads@example.com')
class ConversionTrackingTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_get_started_has_banner_and_begin_checkout(self):
        resp = self.client.get(reverse('get_started'))
        self.assertContains(resp, 'id="cookieBanner"')
        self.assertContains(resp, 'landing/js/conversion.js')
        self.assertContains(resp, 'data-ga-submit="begin_checkout"')
        self.assertContains(resp, 'data-ga-monthly="149"')

    def test_contact_success_marks_generate_lead(self):
        payload = {'name': 'Ana Rivera', 'email': 'ana@example.com', 'company': 'Acme',
                   'service': 'operational-platform', 'budget': '3000-7500',
                   'message': 'Necesitamos una plataforma.', 'website': ''}
        resp = self.client.post(reverse('index'), payload, follow=True)
        self.assertContains(resp, 'data-ga-event="generate_lead"')
        # The event carries no personal data.
        self.assertNotIn('ana@example.com', resp.content.decode().split('data-ga-event')[1][:200])

    def test_failed_contact_does_not_mark_generate_lead(self):
        resp = self.client.post(reverse('index'), {'name': 'Ana', 'website': ''})
        self.assertNotContains(resp, 'data-ga-event="generate_lead"')

    def test_bienvenida_purchase_event_without_pii(self):
        client = make_client('acme', notify_email='dueno@acme.com')
        Subscription.objects.create(client=client, plan='starter', status='active',
                                    checkout_session_id='cs_test_abc123',
                                    order_number='DOM-2026-0042', initial_total_cents=14900,
                                    initial_subtotal_cents=14900, initial_tax_cents=0,
                                    initial_discount_cents=0)
        resp = self.client.get(reverse('bienvenida') + '?session_id=cs_test_abc123')
        self.assertContains(resp, 'data-ga-event="purchase"')
        self.assertContains(resp, 'data-ga-transaction="DOM-2026-0042"')
        self.assertContains(resp, 'data-ga-value="149.00"')
        self.assertContains(resp, 'id="cookieBanner"')
        marker = resp.content.decode().split('data-ga-event="purchase"')[1].split('>')[0]
        self.assertNotIn('dueno@acme.com', marker)

    def test_reported_url_drops_the_query_string(self):
        resp = self.client.get(reverse('get_started'))
        self.assertContains(resp, 'page_location: dominioCleanLocation()')

    @override_settings(GA_MEASUREMENT_ID='')
    def test_no_ga_no_conversion_script(self):
        resp = self.client.get(reverse('get_started'))
        self.assertNotContains(resp, 'conversion.js')

    def test_conversion_script_checks_consent(self):
        from django.contrib.staticfiles import finders
        path = finders.find('landing/js/conversion.js')
        with open(path, encoding='utf-8') as fh:
            js = fh.read()
        self.assertIn("localStorage.getItem(CONSENT_KEY) === 'accepted'", js)
        self.assertIn('window.__dominioGA', js)
