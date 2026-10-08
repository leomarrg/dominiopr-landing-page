"""Phase 2 usage ledger: every Anthropic call site writes one UsageEvent, the
ledger never breaks the request it measures, and the dashboards read it in a
bounded number of queries without leaking cost to tenants.

The real Anthropic API is never called: agent.answer / generate_greeting are
patched everywhere.
"""
import json
import os
import tempfile
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import agent, usage
from .models import Client, Conversation, Membership, UsageEvent
from .tests_phase2 import SESSION, TEST_TOKEN, _signed_body

MODEL = 'claude-haiku-5-5'
FULL_USAGE = {
    'model': MODEL, 'effort': 'low', 'api_calls': 1,
    'input_tokens': 1000, 'output_tokens': 100,
    'cache_read_input_tokens': 2000, 'cache_creation_input_tokens': 0,
}
# 1000*0.10 + 100*0.50 + 2000*0.01 per MTok on Haiku 5.5.
FULL_COST = Decimal('0.000170')


def fake_answer(reply='Hola, ¿en qué te ayudo?', used=None):
    def _answer(history, business_prompt=None, handlers=None, language='es'):
        return reply, dict(used or FULL_USAGE)
    return _answer


def fake_greeting(text='Hola, soy tu asistente.'):
    def _greeting(business_prompt, usage=None):
        if usage is not None:
            usage.update(model=MODEL, input_tokens=300, output_tokens=30, api_calls=1)
        return text
    return _greeting


def make_client(slug, **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', slug.title()), system_prompt='Vendemos cosas.',
        notify_email=f'owner@{slug}.com', widget_token=kw.pop('widget_token', ''), **kw)


@override_settings(ANTHROPIC_API_KEY='test-key')
class CallSiteRecordingTests(TestCase):
    """Each call site writes exactly one event, with the right kind and tenant."""

    def setUp(self):
        cache.clear()
        self.acme = make_client('acme', widget_token=TEST_TOKEN)

    def _chat(self, session=SESSION):
        body = {'client': 'acme', 'token': TEST_TOKEN,
                'messages': [{'role': 'user', 'content': 'hola'}]}
        if session:
            body['session'] = session
        return self.client.post(reverse('chat_api'), data=json.dumps(body),
                                content_type='application/json')

    def test_chat_records_one_event_with_conversation(self):
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            resp = self._chat()
        self.assertEqual(resp.status_code, 200)
        ev = UsageEvent.objects.get()
        conv = Conversation.objects.get(client=self.acme)
        self.assertEqual((ev.kind, ev.client, ev.client_slug, ev.conversation),
                         ('chat', self.acme, 'acme', conv))
        self.assertEqual((ev.model, ev.effort, ev.api_calls), (MODEL, 'low', 1))
        self.assertEqual((ev.input_tokens, ev.output_tokens, ev.cache_read_tokens,
                          ev.cache_write_tokens), (1000, 100, 2000, 0))
        self.assertEqual(ev.cost_usd, FULL_COST)

    def test_chat_without_session_still_records(self):
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            resp = self._chat(session=None)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Conversation.objects.exists())
        ev = UsageEvent.objects.get()
        self.assertEqual((ev.kind, ev.client, ev.conversation), ('chat', self.acme, None))

    def test_legacy_usage_shape_falls_back_to_configured_model(self):
        legacy = {'input_tokens': 100, 'output_tokens': 50}
        with patch.object(agent, 'answer', side_effect=fake_answer(used=legacy)):
            self._chat()
        ev = UsageEvent.objects.get()
        self.assertEqual(ev.model, agent._model())
        self.assertEqual((ev.input_tokens, ev.output_tokens, ev.api_calls), (100, 50, 1))

    def test_demo_records_without_client(self):
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            resp = self.client.post(
                reverse('demo_api'),
                data=json.dumps({'context': 'Somos una panadería.',
                                 'messages': [{'role': 'user', 'content': 'hola'}]}),
                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        ev = UsageEvent.objects.get()
        self.assertEqual((ev.kind, ev.client, ev.client_slug), ('demo', None, ''))

    def test_knowledge_test_records_for_its_client(self):
        user = get_user_model().objects.create_user('admin@acme.com', password='pw12345!')
        Membership.objects.create(user=user, client=self.acme, role='org_admin')
        http = HttpClient()
        http.login(username='admin@acme.com', password='pw12345!')
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            resp = http.post(reverse('knowledge_test'),
                             {'client': 'acme', 'question': '¿Cuánto cuesta?'})
        self.assertEqual(resp.status_code, 200)
        ev = UsageEvent.objects.get()
        self.assertEqual((ev.kind, ev.client), ('knowledge_test', self.acme))

    def test_factory_greeting_records_for_new_client(self):
        get_user_model().objects.create_user('staff@dominiopr.com', password='pw12345!',
                                             is_staff=True)
        http = HttpClient()
        http.login(username='staff@dominiopr.com', password='pw12345!')
        with patch.object(agent, 'generate_greeting', side_effect=fake_greeting()):
            resp = http.post(reverse('client_create'), {
                'name': 'Panadería Sol', 'slug': '', 'system_prompt': 'Pan fresco.',
                'greeting': '', 'notify_email': 'sol@example.com', 'allowed_origins': '',
                'primary_color': '#34d6c8', 'surface_color': '#12304a',
                'header_color': '#0c2132', 'setup_status': 'live', 'website_url': '',
                'platform': '', 'primary_language': 'es', 'daily_message_cap': '300',
                'retention_months': '12', 'notify_phone': '', 'notify_channel': 'email'})
        self.assertEqual(resp.status_code, 302)
        new = Client.objects.get(name='Panadería Sol')
        self.assertEqual(new.greeting, 'Hola, soy tu asistente.')
        ev = UsageEvent.objects.get()
        self.assertEqual((ev.kind, ev.client, ev.client_slug), ('greeting', new, new.slug))
        self.assertEqual((ev.input_tokens, ev.output_tokens), (300, 30))

    def test_eval_command_records_each_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'eval.txt')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('hola >> ¿precio?\notra pregunta\n')
            with patch.object(agent, 'answer', side_effect=fake_answer()):
                call_command('agent_eval', client='acme', file=path, stdout=StringIO())
        events = UsageEvent.objects.all()
        self.assertEqual(events.count(), 3)  # one per answered turn
        self.assertEqual(set(events.values_list('kind', 'client_id')), {('eval', self.acme.pk)})


@override_settings(STRIPE_WEBHOOK_SECRET='whsec_test', CONTACT_NOTIFY_EMAIL='ops@example.com',
                   SITE_URL='https://dominiopr.com')
class ProvisioningGreetingTests(TestCase):
    """The Stripe alta writes the greeting cost to the new tenant."""

    def setUp(self):
        cache.clear()

    def _post_checkout(self):
        payload = {'id': 'evt_ledger', 'type': 'checkout.session.completed',
                   'data': {'object': {
                       'id': 'cs_ledger', 'payment_status': 'paid',
                       'customer_details': {'email': 'ana@acme.com'},
                       'metadata': {'plan': 'starter', 'period': 'monthly',
                                    'company': 'Acme PR', 'name': 'Ana'},
                       'subscription': 'sub_l', 'customer': 'cus_l'}}}
        body, sig = _signed_body(payload)
        return self.client.post(reverse('stripe_webhook'), data=body,
                                content_type='application/json', HTTP_STRIPE_SIGNATURE=sig)

    def test_provisioning_records_greeting(self):
        with patch.object(agent, 'generate_greeting', side_effect=fake_greeting()):
            resp = self._post_checkout()
        self.assertEqual(resp.status_code, 200)
        new = Client.objects.get(notify_email='ana@acme.com')
        ev = UsageEvent.objects.get()
        self.assertEqual((ev.kind, ev.client), ('greeting', new))

    def test_mock_that_ignores_usage_writes_nothing(self):
        with patch.object(agent, 'generate_greeting', return_value='Hola.'):
            resp = self._post_checkout()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Client.objects.filter(notify_email='ana@acme.com').exists())
        self.assertFalse(UsageEvent.objects.exists())


@override_settings(ANTHROPIC_API_KEY='test-key')
class RecordRobustnessTests(TestCase):

    def setUp(self):
        cache.clear()
        self.acme = make_client('acme', widget_token=TEST_TOKEN)

    def test_db_error_is_swallowed_and_chat_still_answers(self):
        body = {'client': 'acme', 'token': TEST_TOKEN, 'session': SESSION,
                'messages': [{'role': 'user', 'content': 'hola'}]}
        with patch.object(agent, 'answer', side_effect=fake_answer('Respuesta OK')), \
                patch.object(UsageEvent.objects, 'create', side_effect=DatabaseError('boom')), \
                self.assertLogs('landing.usage', level='ERROR'):
            resp = self.client.post(reverse('chat_api'), data=json.dumps(body),
                                    content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['reply'], 'Respuesta OK')
        # The conversation transcript was not rolled back by the ledger failure.
        conv = Conversation.objects.get(client=self.acme)
        self.assertEqual(conv.chat_messages.count(), 2)
        self.assertFalse(UsageEvent.objects.exists())

    def test_record_returns_none_for_empty_usage(self):
        self.assertIsNone(usage.record('chat', None))
        self.assertIsNone(usage.record('chat', {}))
        self.assertIsNone(usage.record('greeting', agent.new_usage(MODEL), client=self.acme))
        self.assertFalse(UsageEvent.objects.exists())

    def test_cost_is_frozen_per_model(self):
        tokens = {'input_tokens': 1000, 'output_tokens': 100, 'api_calls': 1}
        new = usage.record('chat', dict(tokens, model='claude-haiku-5-5'), client=self.acme)
        old = usage.record('chat', dict(tokens, model='claude-haiku-4-5'), client=self.acme)
        self.assertEqual(new.cost_usd, Decimal('0.000150'))
        self.assertEqual(old.cost_usd, Decimal('0.001500'))
        # A later price change does not rewrite what was already billed.
        with override_settings(DOMINIO_MODEL_PRICES={
                'claude-haiku-5-5': {'input': 9, 'output': 9, 'cache_write': 9, 'cache_read': 9}}):
            new.refresh_from_db()
            self.assertEqual(new.cost_usd, Decimal('0.000150'))

    def test_slug_survives_client_deletion(self):
        ev = usage.record('chat', FULL_USAGE, client=self.acme)
        self.acme.delete()
        ev.refresh_from_db()
        self.assertIsNone(ev.client_id)
        self.assertEqual(ev.client_slug, 'acme')


class AggregationTests(TestCase):

    def _event(self, client, kind='chat', when=None, inp=100, out=10, cost='0.001000'):
        return UsageEvent.objects.create(
            client=client, client_slug=client.slug if client else '', kind=kind,
            model=MODEL, input_tokens=inp, output_tokens=out, cache_read_tokens=5,
            api_calls=1, cost_usd=Decimal(cost), created_at=when or timezone.now())

    def test_month_bounds_in_local_time(self):
        start, end = usage.month_bounds(date(2026, 12, 15))
        tz = timezone.get_default_timezone()
        self.assertEqual(start, timezone.make_aware(datetime(2026, 12, 1), tz))
        self.assertEqual(end, timezone.make_aware(datetime(2027, 1, 1), tz))
        # 02:00 UTC on Nov 1 is still Oct 31 in Puerto Rico (UTC-4).
        utc_dt = datetime(2026, 11, 1, 2, 0, tzinfo=dt_timezone.utc)
        start, _ = usage.month_bounds(utc_dt)
        self.assertEqual((start.year, start.month), (2026, 10))

    def test_rollup_by_client_sums_and_counts_messages(self):
        a, b = Client.objects.create(slug='a', name='A', system_prompt='x', notify_email='a@a.com'), \
            Client.objects.create(slug='b', name='B', system_prompt='x', notify_email='b@b.com')
        self._event(a)
        self._event(a)
        self._event(a, kind='greeting', cost='0.000500')
        self._event(b, inp=1000)
        self._event(None, kind='demo')
        start, end = usage.month_bounds()
        self._event(a, when=start - timezone.timedelta(seconds=1))  # previous month
        with self.assertNumQueries(1):
            roll = usage.rollup_by_client(start, end)
        self.assertEqual(roll[a.pk]['events'], 3)
        self.assertEqual(roll[a.pk]['messages'], 2)
        self.assertEqual(roll[a.pk]['input'], 300)
        self.assertEqual(roll[a.pk]['tokens'], 300 + 30 + 15)
        self.assertEqual(roll[a.pk]['cost'], Decimal('0.002500'))
        self.assertEqual(roll[b.pk]['input'], 1000)
        self.assertEqual(roll[None]['messages'], 0)

    def test_rollup_query_count_does_not_grow_with_clients(self):
        start, end = usage.month_bounds()
        clients = [Client.objects.create(slug=f'c{i}', name=f'C{i}', system_prompt='x',
                                         notify_email=f'c{i}@x.com') for i in range(5)]
        self._event(clients[0])
        with self.assertNumQueries(1):
            self.assertEqual(len(usage.rollup_by_client(start, end)), 1)
        for c in clients[1:]:
            self._event(c)
            self._event(c, kind='greeting')
        with self.assertNumQueries(1):
            self.assertEqual(len(usage.rollup_by_client(start, end)), 5)

    def test_monthly_groups_by_month_and_client(self):
        a = Client.objects.create(slug='a', name='A', system_prompt='x', notify_email='a@a.com')
        start, _ = usage.month_bounds()
        self._event(a)
        self._event(a, kind='greeting')
        self._event(a, when=start - timezone.timedelta(days=3))
        with self.assertNumQueries(1):
            rows = usage.monthly([a.pk], months=2)
        self.assertEqual(len(rows), 2)
        self.assertLess(rows[0]['month'], rows[1]['month'])
        self.assertEqual((rows[1]['messages'], rows[1]['tokens']), (1, 2 * 115))
        self.assertEqual(rows[1]['cost'], Decimal('0.002000'))
        self.assertEqual(usage.monthly([a.pk], months=1)[0]['month'], rows[1]['month'])


class DashboardTests(TestCase):
    """Cost is operator-only; the factory list stays O(1) in queries."""

    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.acme = make_client('acme')
        UsageEvent.objects.create(
            client=self.acme, client_slug='acme', kind='chat', model=MODEL,
            input_tokens=1_000_000, output_tokens=100_000, api_calls=3,
            cost_usd=Decimal('0.150000'))
        User.objects.create_user('staff@dominiopr.com', password='pw12345!', is_staff=True)
        tenant = User.objects.create_user('admin@acme.com', password='pw12345!')
        Membership.objects.create(user=tenant, client=self.acme, role='org_admin')

    def _login(self, username):
        http = HttpClient()
        http.login(username=username, password='pw12345!')
        return http

    def test_staff_reports_show_ledger_cost(self):
        resp = self._login('staff@dominiopr.com').get(reverse('reports'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, '$0.15')
        self.assertEqual(resp.context['stats']['est_cost'], Decimal('0.15'))
        row = next(r for r in resp.context['per_client'] if r['client__name'] == 'Acme')
        self.assertEqual((row['messages'], row['cost']), (1, Decimal('0.15')))

    def test_tenant_reports_never_show_cost(self):
        resp = self._login('admin@acme.com').get(reverse('reports'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, '$0.15')
        self.assertNotContains(resp, 'Costo')
        self.assertIsNone(resp.context['stats']['est_cost'])
        self.assertIsNone(resp.context['per_client'])

    def test_clients_list_shows_month_usage(self):
        resp = self._login('staff@dominiopr.com').get(reverse('clients_list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Mensajes (mes): <b>1</b>', html=False)
        self.assertContains(resp, 'Costo IA (mes): <b>$0.1500</b>', html=False)

    def _clients_list_queries(self, http):
        with CaptureQueriesContext(connection) as ctx:
            resp = http.get(reverse('clients_list'))
        self.assertEqual(resp.status_code, 200)
        return len(ctx.captured_queries)

    def test_clients_list_query_count_is_constant(self):
        http = self._login('staff@dominiopr.com')
        self._clients_list_queries(http)  # warm session/auth caches
        baseline = self._clients_list_queries(http)
        for i in range(5):
            c = make_client(f'extra{i}')
            UsageEvent.objects.create(client=c, client_slug=c.slug, kind='chat',
                                      model=MODEL, input_tokens=10, cost_usd=Decimal('0.01'))
        self.assertEqual(self._clients_list_queries(http), baseline)


class PartialUsageOnErrorTests(TestCase):
    """A failure mid tool-loop still bills the rounds that ran: record them."""

    @override_settings(ANTHROPIC_API_KEY='test-key', DOMINIO_AGENT_MODEL='claude-haiku-5-5')
    def test_answer_attaches_usage_to_api_error(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        import anthropic

        first = SimpleNamespace(
            stop_reason='tool_use',
            content=[SimpleNamespace(type='tool_use', id='t1', name='capture_lead', input={})],
            usage=SimpleNamespace(input_tokens=50, output_tokens=20,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0))
        err = anthropic.APIConnectionError(request=MagicMock())
        provider = MagicMock()
        provider.messages.create.side_effect = [first, err]
        with patch.object(agent, '_provider_client', return_value=provider):
            with self.assertRaises(anthropic.APIError) as ctx:
                agent.answer([{'role': 'user', 'content': 'x'}],
                             handlers={'capture_lead': lambda d: 'ok'})
        self.assertEqual(ctx.exception.usage['input_tokens'], 50)
        self.assertEqual(ctx.exception.usage['api_calls'], 1)

    def test_admin_cannot_delete_ledger_rows(self):
        from django.contrib.admin.sites import site
        model_admin = site._registry[UsageEvent]
        self.assertFalse(model_admin.has_delete_permission(None))
