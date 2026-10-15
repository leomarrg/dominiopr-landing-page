"""
Phase E -- the internal assistant for employees: KnowledgeSource.audience
(and the guard that an internal source never leaks into the public widget),
the 'employee' role, seat limits on invites, the /dashboard/asistente/ page
and API, the privacy of internal conversations and the reports line.

The model provider is ALWAYS mocked: no test here calls a real API.
"""
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import agent, platform_views, publishing
from .models import (
    AuditEvent, ChatMessage, Client, ContactSubmission, Conversation, KnowledgeSource,
    Membership, Subscription, UsageEvent,
)
from .turns import run_turn
from .views import _member_client_ids, leads_for

PW = 'pw12345!'
PR = ZoneInfo('America/Puerto_Rico')
USAGE = {'input_tokens': 100, 'output_tokens': 50, 'cache_read_input_tokens': 0,
         'cache_creation_input_tokens': 0, 'model': 'claude-haiku-5-5',
         'api_calls': 1, 'effort': 'low'}
PUBLIC_TEXT = 'Precio del corte: 25 dolares.'
INTERNAL_TEXT = 'Vacaciones: 15 dias tras un ano de servicio. Pedirlas a RRHH.'
BOTH_TEXT = 'Horario: lunes a viernes de 9 a 5.'


def fake_answer(reply='Claro. Fuente: Manual del Empleado', calls=None):
    """agent.answer double that records every call."""
    def _answer(history, business_prompt=None, handlers=None, language='es', **kwargs):
        if calls is not None:
            calls.append({'history': list(history), 'business_prompt': business_prompt,
                          'handlers': dict(handlers or {}), 'language': language,
                          'kwargs': kwargs})
        return reply, dict(USAGE)
    return _answer


def _resp(text='Claro.'):
    usage = SimpleNamespace(input_tokens=10, output_tokens=5,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(type='text', text=text)],
                           usage=usage, stop_details=None)


def make_client(slug='acme', **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', slug.title()), system_prompt='Salon de belleza Acme.',
        notify_email=kw.pop('notify_email', f'owner@{slug}.com'), **kw)


def member(client, username, role='org_admin', staff=False, active=True):
    user = get_user_model().objects.create_user(username, password=PW, is_staff=staff)
    if not active:
        user.is_active = False
        user.save(update_fields=['is_active'])
    if client is not None:
        Membership.objects.create(user=user, client=client, role=role)
    return user


def source(client, title, content, audience='public', status='active', **kw):
    src = KnowledgeSource.objects.create(client=client, kind='text', title=title,
                                         content=content, audience=audience, status=status, **kw)
    return src


def seed_knowledge(client):
    source(client, 'Precios', PUBLIC_TEXT, 'public')
    source(client, 'Manual del Empleado', INTERNAL_TEXT, 'internal')
    source(client, 'Horario', BOTH_TEXT, 'both')


class InternalBase(TestCase):
    def setUp(self):
        cache.clear()
        self.acme = make_client()
        Subscription.objects.create(client=self.acme, plan='large', status='active')
        self.admin = member(self.acme, 'admin@acme.com', 'org_admin')
        self.agent_user = member(self.acme, 'agent@acme.com', 'agent')
        self.employee = member(self.acme, 'emp@acme.com', 'employee')
        self.staff = member(None, 'ops@dominiopr.com', staff=True)
        self.http = HttpClient()

    def login(self, user):
        self.http.force_login(user)

    def ask(self, text='Como pido vacaciones?', client='acme', **extra):
        payload = {'message': text, 'client': client}
        payload.update(extra)
        return self.http.post(reverse('asistente_api'), data=json.dumps(payload),
                              content_type='application/json')


# ---- 1. Audience + leak guard -------------------------------------------------

class AudienceLeakGuardTests(TestCase):
    def setUp(self):
        self.acme = make_client()
        seed_knowledge(self.acme)

    def test_public_prompt_never_contains_an_internal_source(self):
        prompt = self.acme.compiled_prompt()
        self.assertIn(PUBLIC_TEXT, prompt)
        self.assertIn(BOTH_TEXT, prompt)
        self.assertNotIn(INTERNAL_TEXT, prompt)
        self.assertNotIn('Manual del Empleado', prompt)

    def test_internal_prompt_has_internal_and_both_but_not_public(self):
        prompt = self.acme.compiled_internal_prompt()
        self.assertIn('Salon de belleza Acme.', prompt)     # the business header
        self.assertIn(INTERNAL_TEXT, prompt)
        self.assertIn(BOTH_TEXT, prompt)
        self.assertNotIn(PUBLIC_TEXT, prompt)

    def test_internal_draft_preview_never_reaches_the_public_prompt(self):
        draft = source(self.acme, 'Politica interna', 'Bonos: 5% en diciembre.',
                       'internal', status='draft')
        self.assertNotIn('Bonos', self.acme.compiled_prompt(include_draft_pk=draft.pk))
        self.assertIn('Bonos', self.acme.compiled_internal_prompt(include_draft_pk=draft.pk))

    def test_public_draft_preview_does_not_reach_the_internal_prompt(self):
        draft = source(self.acme, 'Promo', 'Promo de verano: 10% off.', 'public', status='draft')
        self.assertIn('Promo de verano', self.acme.compiled_prompt(include_draft_pk=draft.pk))
        self.assertNotIn('Promo de verano',
                         self.acme.compiled_internal_prompt(include_draft_pk=draft.pk))

    def test_default_audience_is_public(self):
        src = KnowledgeSource.objects.create(client=self.acme, kind='text', title='X',
                                             content='y', status='active')
        self.assertEqual(src.audience, 'public')
        self.assertIn('=== X ===', self.acme.compiled_prompt())
        self.assertNotIn('=== X ===', self.acme.compiled_internal_prompt())

    @override_settings(ANTHROPIC_API_KEY='test-key', DOMINIO_AGENT_MODEL='claude-haiku-5-5',
                       DOMINIO_AGENT_EFFORT='low')
    def test_system_blocks_sent_to_the_provider(self):
        """The real assembly (run_turn -> agent.answer -> provider): the web
        widget's system prompt carries no internal document; the internal
        route carries it with the internal rules and no lead-capture rules."""
        conv = Conversation.objects.create(client=self.acme, widget_session='s' * 20)
        provider = MagicMock()
        provider.messages.create.return_value = _resp()
        with patch.object(agent, '_provider_client', return_value=provider):
            run_turn(self.acme, conv, 'hola', channel='web', handlers={})
            web_system = provider.messages.create.call_args.kwargs['system'][0]['text']
            internal_conv = Conversation.objects.create(
                client=self.acme, channel='internal', external_id='user:1:2026-10-14')
            run_turn(self.acme, internal_conv, 'vacaciones?', channel='internal',
                     handlers={}, profile='internal',
                     business_prompt=self.acme.compiled_internal_prompt())
            internal_system = provider.messages.create.call_args.kwargs['system'][0]['text']
        self.assertIn(PUBLIC_TEXT, web_system)
        self.assertNotIn(INTERNAL_TEXT, web_system)
        self.assertNotIn(agent.INTERNAL_RULES, web_system)
        self.assertIn(INTERNAL_TEXT, internal_system)
        self.assertIn(BOTH_TEXT, internal_system)
        self.assertNotIn(PUBLIC_TEXT, internal_system)
        self.assertIn(agent.INTERNAL_RULES, internal_system)
        self.assertNotIn('CAPTURING LEADS', internal_system)
        self.assertNotIn('tools', provider.messages.create.call_args.kwargs)

    def test_run_turn_default_prompt_is_still_the_public_one(self):
        conv = Conversation.objects.create(client=self.acme, widget_session='s' * 20)
        calls = []
        with override_settings(ANTHROPIC_API_KEY='test-key'), \
                patch.object(agent, 'answer', side_effect=fake_answer(calls=calls)):
            run_turn(self.acme, conv, 'hola', channel='web', handlers={})
        self.assertEqual(calls[0]['business_prompt'], self.acme.compiled_prompt())


# ---- Audience in the knowledge UI --------------------------------------------

@override_settings(CONTACT_NOTIFY_EMAIL='ops@example.com', SITE_URL='https://dominiopr.com')
class AudienceUiTests(InternalBase):
    def setUp(self):
        super().setUp()
        # A self-publish plan so the org_admin can activate/restore.
        Subscription.objects.filter(client=self.acme).update(plan='pro')
        self.login(self.admin)

    def save(self, pk=None, title='Manual', content='Vacaciones: 15 dias.', **extra):
        data = {'client': 'acme', 'kind': 'text', 'title': title, 'content': content}
        if pk:
            data['pk'] = pk
        data.update(extra)
        return self.http.post(reverse('knowledge_save'), data)

    def act(self, src, action):
        return self.http.post(reverse('knowledge_action', args=[src.pk]),
                              {'client': 'acme', 'action': action})

    def test_create_with_internal_audience(self):
        self.save(audience='internal')
        src = KnowledgeSource.objects.get(client=self.acme, title='Manual')
        self.assertEqual((src.audience, src.status), ('internal', 'draft'))

    def test_unknown_audience_keeps_default(self):
        self.save(audience='everyone')
        self.assertEqual(KnowledgeSource.objects.get(title='Manual').audience, 'public')

    def test_list_shows_audience_badge_and_selector(self):
        source(self.acme, 'Manual', INTERNAL_TEXT, 'internal')
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, 'kn-audience--internal')
        self.assertContains(resp, 'Solo equipo interno')
        self.assertContains(resp, 'name="audience"')

    def test_edit_of_active_keeps_audience_in_the_draft_and_after_activation(self):
        active = source(self.acme, 'Manual', INTERNAL_TEXT, 'internal')
        self.save(pk=active.pk, content='Vacaciones: 20 dias.')
        draft = KnowledgeSource.objects.get(replaces=active)
        self.assertEqual(draft.audience, 'internal')
        self.assertEqual(self.act(draft, 'activate').status_code, 302)
        draft.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual((draft.status, draft.audience), ('active', 'internal'))
        self.assertEqual(active.status, 'archived')
        self.assertNotIn('20 dias', self.acme.compiled_prompt())
        self.assertIn('20 dias', self.acme.compiled_internal_prompt())

    def test_edit_can_change_audience_of_the_draft_only(self):
        active = source(self.acme, 'Manual', INTERNAL_TEXT, 'internal')
        self.save(pk=active.pk, audience='both')
        active.refresh_from_db()
        self.assertEqual(active.audience, 'internal')          # live row untouched
        self.assertEqual(KnowledgeSource.objects.get(replaces=active).audience, 'both')

    def test_restore_preserves_audience(self):
        old = source(self.acme, 'Manual', INTERNAL_TEXT, 'internal', status='archived')
        self.assertEqual(self.act(old, 'restore').status_code, 302)
        old.refresh_from_db()
        self.assertEqual((old.status, old.audience), ('active', 'internal'))
        self.assertNotIn(INTERNAL_TEXT, self.acme.compiled_prompt())
        self.assertIn(INTERNAL_TEXT, self.acme.compiled_internal_prompt())

    def test_open_draft_copies_audience(self):
        active = source(self.acme, 'Manual', INTERNAL_TEXT, 'both')
        self.assertEqual(publishing.open_draft_for(active).audience, 'both')

    def test_upload_lands_with_the_chosen_audience(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile('manual.txt', INTERNAL_TEXT.encode('utf-8'))
        resp = self.http.post(reverse('knowledge_upload'),
                              {'client': 'acme', 'file': f, 'audience': 'internal'})
        self.assertEqual(resp.status_code, 302)
        src = KnowledgeSource.objects.get(client=self.acme, kind='file')
        self.assertEqual((src.audience, src.status), ('internal', 'pending'))
        f = SimpleUploadedFile('menu.txt', PUBLIC_TEXT.encode('utf-8'))
        self.http.post(reverse('knowledge_upload'), {'client': 'acme', 'file': f, 'audience': 'x'})
        self.assertEqual(KnowledgeSource.objects.get(title='menu.txt').audience, 'public')

    def test_audience_action_changes_any_kind(self):
        """Scans and uploads have no edit form: the audience action is how an
        uploaded manual becomes internal. On a live source it is publishing."""
        upload = KnowledgeSource.objects.create(
            client=self.acme, kind='file', title='manual.pdf', content=INTERNAL_TEXT,
            status='active')
        self.assertIn(INTERNAL_TEXT, self.acme.compiled_prompt())
        resp = self.http.post(reverse('knowledge_action', args=[upload.pk]),
                              {'client': 'acme', 'action': 'audience', 'audience': 'internal'})
        self.assertEqual(resp.status_code, 302)
        upload.refresh_from_db()
        self.assertEqual(upload.audience, 'internal')
        self.assertNotIn(INTERNAL_TEXT, self.acme.compiled_prompt())
        self.assertIn(INTERNAL_TEXT, self.acme.compiled_internal_prompt())
        self.assertTrue(AuditEvent.objects.filter(action='knowledge.audience',
                                                  result='internal').exists())
        # Invalid value: nothing changes.
        self.http.post(reverse('knowledge_action', args=[upload.pk]),
                       {'client': 'acme', 'action': 'audience', 'audience': 'todos'})
        upload.refresh_from_db()
        self.assertEqual(upload.audience, 'internal')

    def test_audience_change_on_a_live_source_needs_publish_rights(self):
        Subscription.objects.filter(client=self.acme).update(plan='starter')
        live = source(self.acme, 'Manual', INTERNAL_TEXT, 'internal')
        resp = self.http.post(reverse('knowledge_action', args=[live.pk]),
                              {'client': 'acme', 'action': 'audience', 'audience': 'public'})
        self.assertEqual(resp.status_code, 403)
        live.refresh_from_db()
        self.assertEqual(live.audience, 'internal')
        self.assertNotIn(INTERNAL_TEXT, self.acme.compiled_prompt())
        # A draft may be re-targeted freely (nothing is live yet).
        draft = source(self.acme, 'Borrador', 'x', 'internal', status='draft')
        self.http.post(reverse('knowledge_action', args=[draft.pk]),
                       {'client': 'acme', 'action': 'audience', 'audience': 'both'})
        draft.refresh_from_db()
        self.assertEqual(draft.audience, 'both')

    def test_internal_sources_count_toward_the_same_caps(self):
        source(self.acme, 'Grande', 'x' * (publishing.MAX_TOTAL_CHARS - 10), 'internal')
        draft = source(self.acme, 'Otra', 'y' * 100, 'public', status='draft')
        with self.assertRaises(publishing.PublishError):
            publishing.publish(draft)

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_test_console_as_employee_uses_internal_prompt_and_profile(self):
        seed_knowledge(self.acme)
        calls = []
        with patch.object(agent, 'answer', side_effect=fake_answer(calls=calls)):
            self.http.post(reverse('knowledge_test'),
                           {'client': 'acme', 'question': 'vacaciones?', 'as': 'internal'})
            self.http.post(reverse('knowledge_test'),
                           {'client': 'acme', 'question': 'precio?'})
        self.assertEqual(calls[0]['business_prompt'], self.acme.compiled_internal_prompt())
        self.assertEqual(calls[0]['kwargs'], {'profile': 'internal'})
        self.assertEqual(calls[1]['business_prompt'], self.acme.compiled_prompt())
        self.assertNotIn('profile', calls[1]['kwargs'])

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_test_console_as_employee_uses_the_same_kwargs_as_run_turn(self):
        """QA B1: a clinic client's console test carries clinic=True exactly
        like assistant_api -> run_turn does."""
        seed_knowledge(self.acme)
        Client.objects.filter(pk=self.acme.pk).update(clinic_mode=True)
        Subscription.objects.filter(client=self.acme).update(plan='large')  # has the assistant
        calls = []
        with patch.object(agent, 'answer', side_effect=fake_answer(calls=calls)):
            self.http.post(reverse('knowledge_test'),
                           {'client': 'acme', 'question': 'vacaciones?', 'as': 'internal'})
            self.login(self.employee)
            resp = self.ask()
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(calls[0]['kwargs'], {'clinic': True, 'profile': 'internal'})
        self.assertEqual(calls[1]['kwargs'], calls[0]['kwargs'])


# ---- 2. Employee role --------------------------------------------------------

class EmployeeRoleTests(InternalBase):
    GATED = ('dashboard', 'conversations', 'bookings', 'reports', 'knowledge', 'dash_users',
             'audit', 'export_leads', 'install', 'billing')

    def test_employee_is_sent_to_the_assistant_from_every_other_page(self):
        self.login(self.employee)
        for name in self.GATED:
            resp = self.http.get(reverse(name))
            self.assertEqual(resp.status_code, 302, name)
            self.assertEqual(resp.url, reverse('asistente'), name)

    def test_employee_may_open_the_assistant_and_change_password(self):
        self.login(self.employee)
        self.assertEqual(self.http.get(reverse('asistente')).status_code, 200)
        self.assertEqual(self.http.get(reverse('password_change')).status_code, 200)

    def test_employee_cannot_read_any_conversation_even_their_own(self):
        conv = Conversation.objects.create(
            client=self.acme, channel='internal', user=self.employee,
            external_id=platform_views._assistant_external_id(self.employee))
        self.login(self.employee)
        resp = self.http.get(reverse('conversation_detail', args=[conv.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('asistente'))

    def test_employee_membership_is_not_a_dashboard_scope(self):
        ContactSubmission.objects.create(client=self.acme, name='L', email='l@x.com',
                                         service='consulting', message='hola')
        self.assertEqual(_member_client_ids(self.employee), [])
        self.assertEqual(leads_for(self.employee).count(), 0)
        self.assertEqual(platform_views.conversations_for(self.employee).count(), 0)

    def test_mixed_account_sees_only_the_other_client(self):
        """Employee of Acme + agent of Beta: Beta's dashboard, none of Acme's data."""
        beta = make_client('beta')
        Membership.objects.create(user=self.employee, client=beta, role='agent')
        ContactSubmission.objects.create(client=self.acme, name='Acme lead', email='a@x.com',
                                         service='consulting', message='hola')
        ContactSubmission.objects.create(client=beta, name='Beta lead', email='b@x.com',
                                         service='consulting', message='hola')
        Conversation.objects.create(client=self.acme, widget_session='w' * 20)
        self.login(self.employee)
        resp = self.http.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Beta lead')
        self.assertNotContains(resp, 'Acme lead')
        self.assertEqual(list(_member_client_ids(self.employee)), [beta.pk])
        self.assertEqual(platform_views.conversations_for(self.employee).count(), 0)

    def test_nav_for_employee_only_shows_the_assistant(self):
        self.login(self.employee)
        resp = self.http.get(reverse('asistente'))
        self.assertContains(resp, '>Asistente</a>')
        self.assertNotContains(resp, '>Leads</a>')
        self.assertNotContains(resp, '>Conversaciones</a>')
        self.assertNotContains(resp, '>Reportes</a>')

    def test_nav_for_admin_on_internal_plan_has_the_assistant(self):
        self.login(self.admin)
        resp = self.http.get(reverse('dashboard'))
        self.assertContains(resp, '>Asistente</a>')
        self.assertContains(resp, '>Leads</a>')

    def test_nav_without_internal_plan_hides_the_assistant(self):
        Subscription.objects.filter(client=self.acme).update(plan='medium')
        self.login(self.admin)
        self.assertNotContains(self.http.get(reverse('dashboard')), '>Asistente</a>')


# ---- Seat limit on invite ----------------------------------------------------

@override_settings(SITE_URL='https://dominiopr.com')
class SeatLimitTests(InternalBase):
    def invite(self, email, role='employee'):
        return self.http.post(reverse('user_invite'),
                              {'client': 'acme', 'email': email, 'role': role}, follow=True)

    def test_plan_without_internal_refuses_employees(self):
        Subscription.objects.filter(client=self.acme).update(plan='medium')
        self.login(self.admin)
        resp = self.invite('nuevo@acme.com')
        self.assertContains(resp, 'Tu plan no incluye el asistente interno')
        self.assertFalse(Membership.objects.filter(user__username='nuevo@acme.com').exists())
        self.assertTrue(AuditEvent.objects.filter(
            action='user.invite', target='nuevo@acme.com',
            result__startswith='denegado').exists())

    def test_seat_limit_is_enforced(self):
        # large step 1 = 5 seats; self.employee already uses one.
        for i in range(4):
            member(self.acme, f'e{i}@acme.com', 'employee')
        self.login(self.admin)
        resp = self.invite('otro@acme.com')
        self.assertContains(resp, 'Llegaste al límite de 5 asientos')
        self.assertFalse(Membership.objects.filter(user__username='otro@acme.com').exists())

    def test_inactive_employees_do_not_use_a_seat(self):
        for i in range(4):
            member(self.acme, f'e{i}@acme.com', 'employee', active=(i != 0))
        self.login(self.admin)
        resp = self.invite('otro@acme.com')
        self.assertContains(resp, 'Invitación enviada a otro@acme.com (employee)')
        self.assertEqual(Membership.objects.get(user__username='otro@acme.com').role, 'employee')

    def test_invite_under_the_limit_creates_an_employee(self):
        self.login(self.admin)
        resp = self.invite('nuevo@acme.com')
        self.assertContains(resp, 'Invitación enviada')
        m = Membership.objects.get(user__username='nuevo@acme.com')
        self.assertEqual((m.role, m.client_id), ('employee', self.acme.pk))

    def test_agents_and_admins_never_count_as_seats(self):
        for i in range(10):
            member(self.acme, f'a{i}@acme.com', 'agent')
        self.assertEqual(platform_views._seats_used(self.acme), 1)
        self.login(self.admin)
        self.assertContains(self.invite('nuevo@acme.com'), 'Invitación enviada')

    def test_users_page_shows_seats(self):
        self.login(self.admin)
        resp = self.http.get(reverse('dash_users') + '?client=acme')
        self.assertContains(resp, 'Asientos del asistente interno: <b>1</b> de 5')
        self.assertContains(resp, 'value="employee"')
        Subscription.objects.filter(client=self.acme).update(plan='small')
        resp = self.http.get(reverse('dash_users') + '?client=acme')
        self.assertContains(resp, 'Tu plan no incluye el asistente interno')
        self.assertNotContains(resp, 'value="employee"')


# ---- 3. Assistant page + API -------------------------------------------------

class AssistantPageTests(InternalBase):
    def test_every_member_role_opens_the_page(self):
        for user in (self.employee, self.agent_user, self.admin):
            self.login(user)
            resp = self.http.get(reverse('asistente'))
            self.assertEqual(resp.status_code, 200, user.username)
            self.assertContains(resp, 'Este asistente responde solo con los documentos internos de Acme')

    def test_plan_without_internal_is_403_with_message(self):
        Subscription.objects.filter(client=self.acme).update(plan='medium')
        self.login(self.employee)
        resp = self.http.get(reverse('asistente'))
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, 'Tu plan no incluye el asistente interno', status_code=403)

    def test_outsider_is_404_and_anonymous_redirects_to_login(self):
        outsider = member(None, 'nobody@x.com')
        self.login(outsider)
        self.assertEqual(self.http.get(reverse('asistente')).status_code, 404)
        self.http.logout()
        resp = self.http.get(reverse('asistente'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('login'), resp.url)

    def test_staff_picks_a_client_by_slug(self):
        self.login(self.staff)
        self.assertEqual(self.http.get(reverse('asistente')).status_code, 302)
        resp = self.http.get(reverse('asistente') + '?client=acme')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Asistente interno — Acme')

    def test_foreign_slug_never_resolves_for_a_member(self):
        beta = make_client('beta')
        Subscription.objects.create(client=beta, plan='large', status='active')
        self.login(self.employee)
        self.assertEqual(self.http.get(reverse('asistente') + '?client=beta').status_code, 404)

    def test_page_renders_todays_transcript(self):
        conv = Conversation.objects.create(
            client=self.acme, channel='internal', user=self.employee,
            external_id=platform_views._assistant_external_id(self.employee))
        ChatMessage.objects.create(conversation=conv, role='user', content='vacaciones?', position=0)
        ChatMessage.objects.create(conversation=conv, role='assistant',
                                   content='15 dias. Fuente: Manual del Empleado', position=1)
        self.login(self.employee)
        resp = self.http.get(reverse('asistente'))
        self.assertContains(resp, 'Fuente: Manual del Empleado')


@override_settings(ANTHROPIC_API_KEY='test-key')
class AssistantApiTests(InternalBase):
    def setUp(self):
        super().setUp()
        seed_knowledge(self.acme)

    def test_happy_path(self):
        self.login(self.employee)
        calls = []
        with patch.object(agent, 'answer', side_effect=fake_answer(calls=calls)):
            resp = self.ask()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['reply'], 'Claro. Fuente: Manual del Empleado')
        call = calls[0]
        self.assertEqual(call['kwargs'].get('profile'), 'internal')
        self.assertNotIn('channel', call['kwargs'])          # a web page, not WhatsApp
        self.assertEqual(call['handlers'], {})                # no tools, ever
        self.assertEqual(call['business_prompt'], self.acme.compiled_internal_prompt())
        self.assertIn(INTERNAL_TEXT, call['business_prompt'])
        self.assertNotIn(PUBLIC_TEXT, call['business_prompt'])
        self.assertEqual(call['history'], [{'role': 'user', 'content': 'Como pido vacaciones?'}])

        conv = Conversation.objects.get(client=self.acme, channel='internal')
        today = timezone.localtime(timezone.now()).date()
        self.assertEqual(conv.external_id, f'user:{self.employee.pk}:{today:%Y-%m-%d}')
        self.assertEqual(conv.user, self.employee)
        self.assertEqual(conv.pk, resp.json()['conversation'])
        self.assertEqual(list(conv.chat_messages.values_list('role', flat=True)),
                         ['user', 'assistant'])
        event = UsageEvent.objects.get()
        self.assertEqual((event.kind, event.client_id, event.conversation_id),
                         ('internal_chat', self.acme.pk, conv.pk))

    def test_same_day_reuses_the_conversation(self):
        self.login(self.employee)
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            self.ask('uno')
            self.ask('dos')
        conv = Conversation.objects.get(client=self.acme, channel='internal')
        self.assertEqual(conv.chat_messages.count(), 4)
        self.assertEqual(UsageEvent.objects.filter(kind='internal_chat').count(), 2)

    def test_conversation_key_is_per_local_day(self):
        night = dt.datetime(2026, 10, 14, 23, 30, tzinfo=PR)
        next_day = night + dt.timedelta(hours=1)
        self.assertEqual(platform_views._assistant_external_id(self.employee, night),
                         f'user:{self.employee.pk}:2026-10-14')
        self.assertEqual(platform_views._assistant_external_id(self.employee, next_day),
                         f'user:{self.employee.pk}:2026-10-15')
        self.assertNotIn(self.employee.username,
                         platform_views._assistant_external_id(self.employee))

    def test_each_employee_has_their_own_conversation(self):
        other = member(self.acme, 'e2@acme.com', 'employee')
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            self.login(self.employee)
            self.ask('uno')
            self.login(other)
            self.ask('dos')
        self.assertEqual(Conversation.objects.filter(channel='internal').count(), 2)
        self.assertEqual(set(Conversation.objects.filter(channel='internal')
                             .values_list('user_id', flat=True)), {self.employee.pk, other.pk})

    def test_plan_without_internal_is_403(self):
        Subscription.objects.filter(client=self.acme).update(plan='medium')
        self.login(self.employee)
        with patch.object(agent, 'answer', side_effect=fake_answer()) as answer:
            resp = self.ask()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['error'], 'Tu plan no incluye el asistente interno.')
        answer.assert_not_called()
        self.assertFalse(Conversation.objects.filter(channel='internal').exists())

    def test_outsider_403_and_anonymous_401(self):
        outsider = member(None, 'nobody@x.com')
        self.login(outsider)
        self.assertEqual(self.ask().status_code, 403)
        self.http.logout()
        self.assertEqual(self.ask().status_code, 401)

    def test_csrf_is_enforced(self):
        strict = HttpClient(enforce_csrf_checks=True)
        strict.force_login(self.employee)
        resp = strict.post(reverse('asistente_api'),
                           data=json.dumps({'message': 'hola', 'client': 'acme'}),
                           content_type='application/json')
        self.assertEqual(resp.status_code, 403)

    def test_bad_input(self):
        self.login(self.employee)
        self.assertEqual(self.ask('   ').status_code, 400)
        self.assertEqual(self.ask('x' * 2001).status_code, 400)
        resp = self.http.post(reverse('asistente_api'), data='{not json',
                              content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        resp = self.http.post(reverse('asistente_api'), data=json.dumps(['nope']),
                              content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        resp = self.http.post(reverse('asistente_api'), data='x' * (64 * 1024 + 1),
                              content_type='application/json')
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(self.http.get(reverse('asistente_api')).status_code, 405)

    def test_per_user_rate_limit(self):
        self.login(self.employee)
        cache.set(f'rl:asistente:user:{self.employee.pk}',
                  platform_views.ASSISTANT_USER_HOURLY_LIMIT, 3600)
        with patch.object(agent, 'answer', side_effect=fake_answer()) as answer:
            resp = self.ask()
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp['Retry-After'], '3600')
        answer.assert_not_called()
        # Another employee is not affected.
        other = member(self.acme, 'e2@acme.com', 'employee')
        self.login(other)
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            self.assertEqual(self.ask().status_code, 200)

    def test_tenant_daily_cap_is_its_own_counter(self):
        Client.objects.filter(pk=self.acme.pk).update(daily_message_cap=2)
        self.login(self.employee)
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            self.assertEqual(self.ask('1').status_code, 200)
            self.assertEqual(self.ask('2').status_code, 200)
            resp = self.ask('3')
        self.assertEqual(resp.status_code, 503)
        # The public widget's counter was never touched.
        self.assertIsNone(cache.get(f'chat:client:acme:{timezone.now():%Y%m%d}'))

    def test_agent_errors_become_json_statuses(self):
        import anthropic
        import httpx
        self.login(self.employee)
        with override_settings(ANTHROPIC_API_KEY=''):
            self.assertEqual(self.ask().status_code, 503)
        err = anthropic.APIError('boom', httpx.Request('POST', 'https://x'), body=None)
        with patch.object(agent, 'answer', side_effect=err):
            self.assertEqual(self.ask().status_code, 502)

    def test_lead_handlers_never_apply(self):
        """Even a reply that looks like a tool call changes nothing: no tools
        are offered, no lead, booking or message is ever created."""
        self.login(self.employee)
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            self.ask('Quiero una cita manana y mi email es x@y.com')
        self.assertFalse(ContactSubmission.objects.exists())
        self.assertIsNone(Conversation.objects.get(channel='internal').lead)


# ---- 4. Privacy of internal conversations -----------------------------------

class InternalConversationPrivacyTests(InternalBase):
    def setUp(self):
        super().setUp()
        self.conv = Conversation.objects.create(
            client=self.acme, channel='internal', user=self.employee,
            external_id=platform_views._assistant_external_id(self.employee))
        ChatMessage.objects.create(conversation=self.conv, role='user',
                                   content='cuantos dias de vacaciones?', position=0)
        ChatMessage.objects.create(conversation=self.conv, role='assistant',
                                   content='15 dias. Fuente: Manual del Empleado', position=1)
        self.web = Conversation.objects.create(client=self.acme, widget_session='w' * 20)

    def test_org_admin_sees_it_with_badge_and_name_and_the_read_is_audited(self):
        self.login(self.admin)
        resp = self.http.get(reverse('conversations'))
        self.assertContains(resp, '>Interno</span>')
        self.assertContains(resp, 'emp@acme.com')
        resp = self.http.get(reverse('conversation_detail', args=[self.conv.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Empleado: emp@acme.com')
        self.assertContains(resp, 'cuantos dias de vacaciones?')
        self.assertTrue(AuditEvent.objects.filter(
            action='conversation.view', user=self.admin, client=self.acme,
            target=f'conv:{self.conv.pk}').exists())

    def test_agent_never_sees_internal_conversations(self):
        self.login(self.agent_user)
        resp = self.http.get(reverse('conversations'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, '>Interno</span>')
        self.assertNotContains(resp, 'emp@acme.com')
        self.assertContains(resp, reverse('conversation_detail', args=[self.web.pk]))
        self.assertEqual(
            self.http.get(reverse('conversation_detail', args=[self.conv.pk])).status_code, 404)
        self.assertEqual(self.http.post(reverse('conversation_state', args=[self.conv.pk]),
                                        {'state': 'closed_resolved'}).status_code, 404)
        # Not even through the transcript search.
        resp = self.http.get(reverse('conversations') + '?q=vacaciones')
        self.assertNotContains(resp, 'emp@acme.com')

    def test_admin_of_another_client_never_sees_it(self):
        beta = make_client('beta')
        beta_admin = member(beta, 'admin@beta.com', 'org_admin')
        self.login(beta_admin)
        self.assertEqual(
            self.http.get(reverse('conversation_detail', args=[self.conv.pk])).status_code, 404)

    def test_mixed_admin_elsewhere_agent_here_does_not_see_it(self):
        beta = make_client('beta')
        Membership.objects.create(user=self.agent_user, client=beta, role='org_admin')
        self.login(self.agent_user)
        self.assertEqual(
            self.http.get(reverse('conversation_detail', args=[self.conv.pk])).status_code, 404)

    def test_staff_sees_it(self):
        self.login(self.staff)
        resp = self.http.get(reverse('conversation_detail', args=[self.conv.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '>Interno</span>')


# ---- 5. Reports --------------------------------------------------------------

class ReportsInternalLineTests(InternalBase):
    def test_internal_usage_is_a_separate_line(self):
        for i in range(3):
            Conversation.objects.create(client=self.acme, channel='internal',
                                        user=self.employee, external_id=f'user:{i}:2026-10-14')
        Conversation.objects.create(client=self.acme, widget_session='w' * 20)
        self.login(self.admin)
        resp = self.http.get(reverse('reports') + '?client=acme')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Asistente interno: <b>3</b> conversaciones este mes')
        self.assertEqual(resp.context['internal_conversations'], 3)
        self.assertEqual(resp.context['internal_month'], 3)

    def test_line_is_hidden_without_internal_usage(self):
        Conversation.objects.create(client=self.acme, widget_session='w' * 20)
        self.login(self.admin)
        resp = self.http.get(reverse('reports') + '?client=acme')
        self.assertNotContains(resp, 'Asistente interno:')

    def test_visitor_figures_never_count_internal_conversations(self):
        """QA A1: volume, daily bars and after-hours are visitor figures; the
        internal line keeps its own count."""
        for i in range(3):
            Conversation.objects.create(client=self.acme, channel='internal',
                                        user=self.employee, external_id=f'user:{i}:2026-10-14')
        Conversation.objects.create(client=self.acme, widget_session='w' * 20)
        self.login(self.admin)
        resp = self.http.get(reverse('reports') + '?client=acme')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['stats']['conversations'], 1)
        self.assertEqual(resp.context['daily_conversations'], 1)
        self.assertEqual(sum(b['value'] for b in resp.context['conv_bars']), 1)
        self.assertLessEqual(resp.context['stats']['after_hours'], 1)
        self.assertEqual(resp.context['value']['conversations'], 1)
        self.assertEqual(resp.context['internal_conversations'], 3)
        self.assertEqual(resp.context['internal_month'], 3)

    def test_staff_control_center_counts_visitors_only(self):
        from . import analytics
        for i in range(2):
            Conversation.objects.create(client=self.acme, channel='internal',
                                        user=self.employee, external_id=f'user:{i}:2026-10-14')
        Conversation.objects.create(client=self.acme, widget_session='w' * 20)
        start, end = timezone.now() - dt.timedelta(days=1), timezone.now() + dt.timedelta(days=1)
        self.assertEqual(analytics.volume(None, start, end)[self.acme.pk]['conversations'], 1)
        self.assertEqual(analytics.after_hours(None, start, end).get(self.acme.pk, 0) <= 1, True)


class InternalTopicsNeverReachReportsTests(InternalBase):
    """QA A2: an internal conversation's topic/intent/first question never
    reaches the reports (the agent role reads them) and the nightly tagger
    never spends a model call on one."""

    def _internal(self, **kw):
        conv = Conversation.objects.create(
            client=self.acme, channel='internal', user=self.employee,
            external_id=f'user:{Conversation.objects.count()}:2026-10-14', **kw)
        ChatMessage.objects.create(conversation=conv, role='user', content='despidos?', position=0)
        ChatMessage.objects.create(conversation=conv, role='assistant', content='ok', position=1)
        Conversation.objects.filter(pk=conv.pk).update(
            last_message_at=timezone.now() - dt.timedelta(hours=2))
        return conv

    def test_agent_report_has_no_internal_topic_intent_or_question(self):
        self._internal(topic='Despidos', intent='otro', tagged_at=timezone.now(),
                       first_question='como despido a alguien?')
        Conversation.objects.create(client=self.acme, widget_session='w' * 20, topic='precios',
                                    intent='precios', tagged_at=timezone.now(),
                                    first_question='cuanto cuesta?')
        self.login(self.agent_user)
        resp = self.http.get(reverse('reports') + '?client=acme')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([t['topic'] for t in resp.context['topics']], ['precios'])
        self.assertEqual([i['intent'] for i in resp.context['intents']], ['precios'])
        self.assertEqual(sum(i['n'] for i in resp.context['intents']), 1)
        self.assertNotContains(resp, 'Despidos')
        from . import analytics
        self.assertEqual([q['first_question'] for q in
                          analytics.recent_first_questions(self.acme.pk)], ['cuanto cuesta?'])

    def test_tagger_skips_internal_conversations(self):
        from io import StringIO
        from django.core.management import call_command
        internal = self._internal()
        web = Conversation.objects.create(client=self.acme, widget_session='w' * 20)
        ChatMessage.objects.create(conversation=web, role='user', content='precio?', position=0)
        Conversation.objects.filter(pk=web.pk).update(
            last_message_at=timezone.now() - dt.timedelta(hours=2))
        out = StringIO()
        call_command('tag_conversations', '--dry-run', stdout=out)
        text = out.getvalue()
        self.assertIn(f'conv {web.pk} ', text)
        self.assertNotIn(f'conv {internal.pk} ', text)
        self.assertIn('Se etiquetarían 1 conversaciones', text)


# ---- 6. QA follow-ups: no internal documents, seat lock, client pick, console kwargs ----

@override_settings(SITE_URL='https://dominiopr.com')
class NoInternalDocumentsTests(InternalBase):
    """QA M1: a business with no internal knowledge must never get an answer
    made from DOMINIO's own sales copy (agent.answer's default prompt)."""

    def setUp(self):
        super().setUp()
        Client.objects.filter(pk=self.acme.pk).update(system_prompt='')
        self.acme.refresh_from_db()
        source(self.acme, 'Precios', PUBLIC_TEXT, 'public')
        self.assertEqual(self.acme.compiled_internal_prompt(), '')

    def test_assistant_api_returns_the_notice_without_calling_the_model(self):
        self.login(self.employee)
        with patch.object(agent, 'answer') as answer:
            resp = self.ask()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['reply'], platform_views.NO_INTERNAL_DOCS_MESSAGE)
        answer.assert_not_called()
        self.assertFalse(Conversation.objects.filter(channel='internal').exists())
        self.assertFalse(UsageEvent.objects.exists())

    def test_assistant_api_answers_once_an_internal_source_exists(self):
        source(self.acme, 'Manual', INTERNAL_TEXT, 'internal')
        self.login(self.employee)
        calls = []
        with patch.object(agent, 'answer', side_effect=fake_answer(calls=calls)):
            resp = self.ask()
        self.assertEqual(resp.status_code, 200)
        self.assertIn(INTERNAL_TEXT, calls[0]['business_prompt'])

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_console_as_employee_returns_the_notice_without_calling_the_model(self):
        self.login(self.admin)
        with patch.object(agent, 'answer') as answer:
            resp = self.http.post(reverse('knowledge_test'),
                                  {'client': 'acme', 'question': 'vacaciones?', 'as': 'internal'})
        self.assertContains(resp, 'Todavía no hay documentos internos cargados')
        answer.assert_not_called()
        # As a visitor the public knowledge is there: the model is consulted.
        with patch.object(agent, 'answer', side_effect=fake_answer()) as answer:
            self.http.post(reverse('knowledge_test'), {'client': 'acme', 'question': 'precio?'})
        answer.assert_called_once()

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_console_previewing_an_internal_draft_does_consult_the_model(self):
        draft = source(self.acme, 'Manual', INTERNAL_TEXT, 'internal', status='draft')
        self.login(self.admin)
        calls = []
        with patch.object(agent, 'answer', side_effect=fake_answer(calls=calls)):
            self.http.post(reverse('knowledge_test'),
                           {'client': 'acme', 'question': 'vacaciones?', 'as': 'internal',
                            'draft': draft.pk})
        self.assertIn(INTERNAL_TEXT, calls[0]['business_prompt'])

    @override_settings(ANTHROPIC_API_KEY='test-key', DOMINIO_AGENT_MODEL='claude-haiku-5-5',
                       DOMINIO_AGENT_EFFORT='low')
    def test_run_turn_internal_never_falls_back_to_dominio_business(self):
        from .turns import NO_INTERNAL_DOCS_PROMPT
        conv = Conversation.objects.create(client=self.acme, channel='internal',
                                           external_id='user:1:2026-10-14')
        provider = MagicMock()
        provider.messages.create.return_value = _resp()
        with patch.object(agent, '_provider_client', return_value=provider):
            run_turn(self.acme, conv, 'vacaciones?', channel='internal', handlers={},
                     profile='internal', business_prompt=self.acme.compiled_internal_prompt())
        system = provider.messages.create.call_args.kwargs['system'][0]['text']
        self.assertNotIn('dominiopr.com', system)
        self.assertIn(NO_INTERNAL_DOCS_PROMPT, system)
        self.assertIn(agent.INTERNAL_RULES, system)

    @override_settings(ANTHROPIC_API_KEY='test-key', DOMINIO_AGENT_MODEL='claude-haiku-5-5',
                       DOMINIO_AGENT_EFFORT='low')
    def test_run_turn_web_keeps_the_dominio_default(self):
        """The marker is internal-only: client #1 (DOMINIO) still relies on
        the default public prompt."""
        conv = Conversation.objects.create(client=self.acme, widget_session='s' * 20)
        provider = MagicMock()
        provider.messages.create.return_value = _resp()
        with patch.object(agent, '_provider_client', return_value=provider):
            run_turn(self.acme, conv, 'hola', channel='web', handlers={}, business_prompt='')
        system = provider.messages.create.call_args.kwargs['system'][0]['text']
        self.assertIn('dominiopr.com', system)


@override_settings(SITE_URL='https://dominiopr.com')
class SeatLockTests(InternalBase):
    """QA M2: the seat check and the membership are decided under the client
    row lock (a no-op on SQLite); the flow and the limit are unchanged."""

    def invite(self, email, role='employee'):
        return self.http.post(reverse('user_invite'),
                              {'client': 'acme', 'email': email, 'role': role}, follow=True)

    def test_invite_still_creates_the_employee_and_sends_the_email(self):
        from django.core import mail
        self.login(self.admin)
        resp = self.invite('nuevo@acme.com')
        self.assertContains(resp, 'Invitación enviada a nuevo@acme.com (employee)')
        self.assertEqual(Membership.objects.get(user__username='nuevo@acme.com').role, 'employee')
        self.assertEqual([m.to for m in mail.outbox], [['nuevo@acme.com']])

    def test_denied_invite_writes_nothing(self):
        for i in range(4):
            member(self.acme, f'e{i}@acme.com', 'employee')
        before = (Membership.objects.count(), get_user_model().objects.count())
        self.login(self.admin)
        resp = self.invite('otro@acme.com')
        self.assertContains(resp, 'Llegaste al límite de 5 asientos')
        self.assertEqual((Membership.objects.count(), get_user_model().objects.count()), before)
        self.assertEqual(platform_views._seats_used(self.acme), 5)

    def test_invite_locks_the_client_row(self):
        self.login(self.admin)
        with patch.object(platform_views.Client.objects, 'select_for_update',
                          wraps=platform_views.Client.objects.select_for_update) as sfu:
            self.invite('nuevo@acme.com')
        sfu.assert_called_once()
        with patch.object(platform_views.Client.objects, 'select_for_update',
                          wraps=platform_views.Client.objects.select_for_update) as sfu:
            self.invite('agente@acme.com', role='agent')
        sfu.assert_not_called()  # dashboard roles take no seat: nothing to lock


class AssistantClientPickTests(InternalBase):
    """QA M3: without ?client=, open the first client whose plan includes the
    assistant, never a lower-plan one that would 403."""

    def test_prefers_the_client_with_the_internal_channel(self):
        alpha = make_client('alpha')
        Subscription.objects.create(client=alpha, plan='small', status='active')
        beta = make_client('beta')
        Subscription.objects.create(client=beta, plan='large', status='active')
        self.assertLess(alpha.pk, beta.pk)
        user = member(alpha, 'dual@x.com', 'employee')
        Membership.objects.create(user=user, client=beta, role='employee')
        self.http.force_login(user)
        resp = self.http.get(reverse('asistente'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['client_obj'], beta)
        # An explicit slug still wins (and shows the plan message).
        resp = self.http.get(reverse('asistente') + '?client=alpha')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.context['client_obj'], alpha)

    def test_api_picks_the_same_client(self):
        alpha = make_client('alpha')
        Subscription.objects.create(client=alpha, plan='small', status='active')
        beta = make_client('beta')
        Subscription.objects.create(client=beta, plan='large', status='active')
        source(beta, 'Manual', INTERNAL_TEXT, 'internal')
        user = member(alpha, 'dual@x.com', 'employee')
        Membership.objects.create(user=user, client=beta, role='employee')
        self.http.force_login(user)
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            resp = self.http.post(reverse('asistente_api'),
                                  data=json.dumps({'message': 'hola'}),
                                  content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Conversation.objects.get(channel='internal').client, beta)

    def test_without_any_internal_plan_the_first_client_still_resolves(self):
        Subscription.objects.filter(client=self.acme).update(plan='small')
        self.login(self.employee)
        resp = self.http.get(reverse('asistente'))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.context['client_obj'], self.acme)


class AssistantPageJsTests(InternalBase):
    def test_non_json_reply_is_reported_as_a_session_change(self):
        """QA B4: a redirect to HTML (expired session, must_change_password)
        is not 'Sin conexión'."""
        self.login(self.employee)
        resp = self.http.get(reverse('asistente'))
        self.assertContains(resp, 'Tu sesión cambió. Recarga la página.')
        self.assertContains(resp, 'Sin conexión. Intenta de nuevo.')
