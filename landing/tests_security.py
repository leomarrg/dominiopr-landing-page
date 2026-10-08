"""Phase 1a security quick wins: invite flow, last-admin guard, HTML rate
limits, knowledge-test caps, audit of sensitive reads/actions and retention.

The real Anthropic API is never called (agent.answer is patched) and email goes
to Django's locmem outbox.
"""
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import agent
from .models import (
    AuditEvent, Booking, ChatMessage, Client, ContactSubmission, Conversation,
    Membership,
)

USAGE = {'model': 'claude-haiku-5-5', 'effort': 'low', 'api_calls': 1,
         'input_tokens': 10, 'output_tokens': 5,
         'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}


def make_client(slug, **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', slug.title()), system_prompt='Vendemos cosas.',
        notify_email=f'owner@{slug}.com', **kw)


def make_member(email, client, role='org_admin'):
    user = get_user_model().objects.create_user(email, email=email, password='pw12345!')
    Membership.objects.create(user=user, client=client, role=role)
    return user


def logged_in(email):
    http = HttpClient()
    http.login(username=email, password='pw12345!')
    return http


def lead(client, days_old=0, **kw):
    obj = ContactSubmission.objects.create(
        client=client, name=kw.pop('name', 'Ana'), email=kw.pop('email', 'ana@example.com'),
        service='consulting', message='Hola', **kw)
    if days_old:
        ContactSubmission.objects.filter(pk=obj.pk).update(
            created_at=timezone.now() - timezone.timedelta(days=days_old))
    return obj


# ============================================================
# 1. Invite flow + last org_admin guard
# ============================================================

class InviteTests(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = make_client('acme')
        make_member('admin@acme.com', self.acme)
        self.http = logged_in('admin@acme.com')

    def _invite(self, email, role='agent'):
        return self.http.post(reverse('user_invite'),
                              {'client': 'acme', 'email': email, 'role': role}, follow=True)

    def test_new_user_email_has_setup_link_not_a_password(self):
        self._invite('nueva@acme.com')
        msg = mail.outbox[-1]
        self.assertEqual(msg.to, ['nueva@acme.com'])
        self.assertIn('/dashboard/activar/', msg.body)
        self.assertIn('vence en 7 días', msg.body)
        self.assertNotIn('Contraseña temporal', msg.body)
        self.assertNotIn('te pedirá crear', msg.body)
        m = Membership.objects.get(user__username='nueva@acme.com', client=self.acme)
        self.assertEqual(m.role, 'agent')

    def test_user_of_another_agent_gets_no_link(self):
        other = make_client('beta')
        make_member('dual@beta.com', other)
        self._invite('dual@beta.com', role='org_admin')
        body = mail.outbox[-1].body
        self.assertIn('Usa tu contraseña existente', body)
        self.assertNotIn('/dashboard/activar/', body)
        self.assertEqual(
            Membership.objects.get(user__username='dual@beta.com', client=self.acme).role,
            'org_admin')

    def test_existing_member_role_is_not_changed(self):
        make_member('jefa@acme.com', self.acme, role='org_admin')
        resp = self._invite('jefa@acme.com', role='agent')
        self.assertContains(resp, 'Esa persona ya es miembro de este agente')
        self.assertEqual(
            Membership.objects.get(user__username='jefa@acme.com', client=self.acme).role,
            'org_admin')
        self.assertEqual(mail.outbox, [])
        self.assertFalse(AuditEvent.objects.filter(action='user.invite').exists())


class LastAdminTests(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = make_client('acme')
        self.admin = make_member('admin@acme.com', self.acme)

    def test_staff_cannot_remove_the_last_org_admin(self):
        get_user_model().objects.create_user('staff@dominiopr.com', password='pw12345!',
                                             is_staff=True)
        http = logged_in('staff@dominiopr.com')
        m = Membership.objects.get(user=self.admin)
        resp = http.post(reverse('user_remove', args=[m.pk]) + '?client=acme', follow=True)
        self.assertContains(resp, 'No puedes quitar al último administrador')
        self.assertTrue(Membership.objects.filter(pk=m.pk).exists())

    def test_admin_can_remove_another_admin_when_one_remains(self):
        other = make_member('otra@acme.com', self.acme)
        http = logged_in('admin@acme.com')
        m = Membership.objects.get(user=other)
        http.post(reverse('user_remove', args=[m.pk]) + '?client=acme')
        self.assertFalse(Membership.objects.filter(pk=m.pk).exists())
        self.assertEqual(self.acme.members.filter(role='org_admin').count(), 1)

    def test_removing_an_agent_is_unaffected(self):
        agent_user = make_member('agente@acme.com', self.acme, role='agent')
        http = logged_in('admin@acme.com')
        m = Membership.objects.get(user=agent_user)
        http.post(reverse('user_remove', args=[m.pk]) + '?client=acme')
        self.assertFalse(Membership.objects.filter(pk=m.pk).exists())


# ============================================================
# 2. Contact form throttle (HTML, not JSON)
# ============================================================

class ContactThrottleTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_sixth_post_is_throttled_with_html_message(self):
        for _ in range(5):
            resp = self.client.post(reverse('index'), {})  # invalid: no email sent
            self.assertEqual(resp.status_code, 200)
        resp = self.client.post(reverse('index'), {})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].endswith('#contact'))
        self.assertNotEqual(resp.headers.get('Content-Type', ''), 'application/json')
        page = self.client.get(resp['Location'])
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Demasiados envíos seguidos')
        # GET is never counted.
        for _ in range(3):
            self.assertEqual(self.client.get(reverse('index')).status_code, 200)

    def test_foreign_referer_is_not_followed(self):
        for _ in range(5):
            self.client.post(reverse('index'), {})
        resp = self.client.post(reverse('index'), {}, HTTP_REFERER='https://evil.example/x')
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('evil.example', resp['Location'])

    def test_json_default_is_kept_for_apis(self):
        from .views import _rate_limited
        from django.http import HttpResponse
        from django.test import RequestFactory
        view = _rate_limited('t-json', limit=1, window=60)(lambda r: HttpResponse('ok'))
        rf = RequestFactory()
        self.assertEqual(view(rf.post('/x')).status_code, 200)
        resp = view(rf.post('/x'))
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp['Content-Type'], 'application/json')


# ============================================================
# 3. knowledge_test: per-IP throttle + per-tenant daily cap
# ============================================================

@override_settings(ANTHROPIC_API_KEY='test-key')
class KnowledgeTestCapsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = make_client('acme')
        make_member('admin@acme.com', self.acme)
        self.http = logged_in('admin@acme.com')

    def _ask(self, **kw):
        return self.http.post(reverse('knowledge_test'),
                              {'client': 'acme', 'question': '¿Precio?'}, **kw)

    def test_throttled_after_ten_and_model_not_called(self):
        with patch.object(agent, 'answer', return_value=('ok', dict(USAGE))) as answer:
            for _ in range(10):
                self.assertEqual(self._ask().status_code, 200)
            resp = self._ask(follow=True)
        self.assertEqual(answer.call_count, 10)
        self.assertEqual(resp.redirect_chain[0][1], 302)
        self.assertContains(resp, 'Demasiados envíos seguidos')

    @override_settings(DOMINIO_TENANT_ACTION_DAILY_CAP=2)
    def test_tenant_daily_cap_blocks_the_model_call(self):
        with patch.object(agent, 'answer', return_value=('ok', dict(USAGE))) as answer:
            self._ask()
            self._ask()
            resp = self._ask()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(answer.call_count, 2)
        self.assertContains(resp, 'límite de pruebas de hoy')


# ============================================================
# 4. Audit trail for lead actions and transcript reads
# ============================================================

class AuditTests(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = make_client('acme')
        make_member('admin@acme.com', self.acme)
        self.http = logged_in('admin@acme.com')
        self.lead = lead(self.acme)

    def test_lead_status_is_audited(self):
        self.http.post(reverse('lead_status', args=[self.lead.pk]), {'status': 'qualified'})
        ev = AuditEvent.objects.get(action='lead.status')
        self.assertEqual((ev.client, ev.target, ev.result),
                         (self.acme, f'lead:{self.lead.pk}', 'qualified'))

    def test_lead_email_is_audited_without_the_address(self):
        self.http.post(reverse('lead_email', args=[self.lead.pk]),
                       {'subject': 'Hola', 'body': 'Te escribo.'})
        ev = AuditEvent.objects.get(action='lead.email')
        self.assertEqual((ev.client, ev.target, ev.result),
                         (self.acme, f'lead:{self.lead.pk}', 'sent'))
        self.assertNotIn('ana@example.com', ev.target + ev.result)

    def test_failed_lead_email_is_audited_as_failed(self):
        with patch('landing.views._send_html_email', return_value=False):
            self.http.post(reverse('lead_email', args=[self.lead.pk]),
                           {'subject': 'Hola', 'body': 'Te escribo.'})
        self.assertEqual(AuditEvent.objects.get(action='lead.email').result, 'failed')

    def test_conversation_view_is_audited_once_per_hour(self):
        conv = Conversation.objects.create(client=self.acme, widget_session='a' * 16)
        url = reverse('conversation_detail', args=[conv.pk])
        self.assertEqual(self.http.get(url).status_code, 200)
        self.assertEqual(self.http.get(url).status_code, 200)
        events = AuditEvent.objects.filter(action='conversation.view')
        self.assertEqual(events.count(), 1)
        self.assertEqual((events[0].client, events[0].target),
                         (self.acme, f'conv:{conv.pk}'))


# ============================================================
# 5. Retention: leads, bookings and linked conversations
# ============================================================

class PurgeRetentionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = make_client('acme', retention_months=1)  # cutoff = 30 days
        self.old_lead = lead(self.acme, days_old=45, source='chat')
        self.old_form = lead(self.acme, days_old=45, source='contact_form')
        self.old_signup = lead(self.acme, days_old=400, source='signup')
        self.new_lead = lead(self.acme, source='chat')
        # Recent conversation linked to an expired lead: must go with it.
        self.linked = Conversation.objects.create(
            client=self.acme, widget_session='l' * 16, lead=self.old_lead)
        ChatMessage.objects.create(conversation=self.linked, role='user',
                                   content='hola', position=0)
        self.recent = Conversation.objects.create(
            client=self.acme, widget_session='r' * 16, lead=self.new_lead)
        now = timezone.now()
        self.old_booking = Booking.objects.create(
            client=self.acme, name='Ana', email='ana@example.com',
            start=now - timezone.timedelta(days=40), status='completed')
        self.new_booking = Booking.objects.create(
            client=self.acme, name='Ana', email='ana@example.com',
            start=now + timezone.timedelta(days=2))

    def test_purge_deletes_expired_and_keeps_the_rest(self):
        out = StringIO()
        call_command('purge_expired', stdout=out)
        self.assertEqual(
            set(ContactSubmission.objects.values_list('pk', flat=True)),
            {self.old_signup.pk, self.new_lead.pk})
        self.assertFalse(Conversation.objects.filter(pk=self.linked.pk).exists())
        self.assertTrue(Conversation.objects.filter(pk=self.recent.pk).exists())
        self.assertFalse(ChatMessage.objects.exists())
        self.assertEqual(list(Booking.objects.values_list('pk', flat=True)),
                         [self.new_booking.pk])
        self.assertIn('acme: 1 conversaciones, 2 leads, 1 citas', out.getvalue())

    def test_dry_run_deletes_nothing(self):
        out = StringIO()
        call_command('purge_expired', '--dry-run', stdout=out)
        self.assertEqual(ContactSubmission.objects.count(), 4)
        self.assertEqual(Conversation.objects.count(), 2)
        self.assertEqual(Booking.objects.count(), 2)
        self.assertIn('acme: 1 conversaciones, 2 leads, 1 citas', out.getvalue())
        self.assertIn('[dry-run]', out.getvalue())

    def test_other_tenant_with_default_window_is_untouched(self):
        beta = make_client('beta')  # 12 months
        kept = lead(beta, days_old=45)
        call_command('purge_expired', stdout=StringIO())
        self.assertTrue(ContactSubmission.objects.filter(pk=kept.pk).exists())

    def test_legacy_leads_without_client_use_default_retention(self):
        stale = lead(None, days_old=400, source='chat')
        fresh = lead(None, days_old=100, source='chat')
        call_command('purge_expired', stdout=StringIO())
        self.assertFalse(ContactSubmission.objects.filter(pk=stale.pk).exists())
        self.assertTrue(ContactSubmission.objects.filter(pk=fresh.pk).exists())
