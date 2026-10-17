"""Production checklist, front B: model timeouts, /healthz/, the alert brake,
branded error pages, llms.txt / favicon / sitemap lastmod, the widget's error
copy, Server-Timing + slow-request log, model latency in the ledger and the
control center, the optional Sentry init and the cost_alert command.

The model provider is ALWAYS mocked: no test here calls a real API.
"""
import datetime as dt
import json
import logging
import os
import subprocess
import sys
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.core.management import call_command
from django.http import HttpResponse
from django.test import Client as HttpClient
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import path, reverse
from django.utils import timezone

from . import agent, health, ingest, whatsapp
from . import usage as usage_ledger
from . import views
from .models import Client, UsageEvent

TOKEN = 'ops-widget-token-0123456789abcdef'
ADMINS = [('Ops', 'ops@example.com')]
MODEL = 'claude-haiku-5-5'
test_log = logging.getLogger('landing.ops_test')


def make_client(slug='acme', **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', slug.title()), system_prompt='Vendemos cosas.',
        notify_email=f'owner@{slug}.com', widget_token=kw.pop('widget_token', TOKEN),
        daily_message_cap=kw.pop('daily_message_cap', 300), **kw)


def timeout_error():
    return anthropic.APITimeoutError(
        request=httpx.Request('POST', 'https://api.anthropic.com/v1/messages'))


def fake_response(text='ok'):
    return SimpleNamespace(
        stop_reason='end_turn', content=[SimpleNamespace(type='text', text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0))


# ---- test-only URLconf for the error pages ---------------------------------

def _boom(request):
    raise RuntimeError('boom')


def _forbidden(request):
    raise PermissionDenied


def _bad(request):
    raise SuspiciousOperation('bad')


def _form(request):
    return HttpResponse('ok')


def _urlpatterns():
    # The error pages reverse real names (webmanifest...): keep the site's.
    from dominio_website.urls import urlpatterns as site
    return [path('boom/', _boom), path('forbidden/', _forbidden), path('bad/', _bad),
            path('form/', _form)] + list(site)


urlpatterns = _urlpatterns()


# ---- 1. model timeouts -------------------------------------------------------

class AgentTimeoutTests(SimpleTestCase):

    def test_provider_client_has_the_default_timeout_and_one_retry(self):
        client = agent._provider_client('test-key')
        self.assertEqual(client.timeout, 25.0)
        self.assertEqual(client.max_retries, 1)

    @override_settings(DOMINIO_AGENT_TIMEOUT=7)
    def test_timeout_is_configurable(self):
        self.assertEqual(agent._provider_client('test-key').timeout, 7.0)

    def test_invalid_or_non_positive_timeout_falls_back_to_the_default(self):
        for value in (0, -3, 'abc', None):
            with self.subTest(value=value), override_settings(DOMINIO_AGENT_TIMEOUT=value):
                self.assertEqual(agent.agent_timeout(), 25.0)

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_ingest_keeps_its_own_longer_budget(self):
        client = ingest._llm()
        self.assertEqual(client.timeout, ingest.LLM_TIMEOUT)
        self.assertEqual(client.max_retries, 2)

    def test_whatsapp_turn_tightens_it(self):
        client = agent._provider_client('test-key').with_options(
            timeout=whatsapp.TURN_TIMEOUT, max_retries=1)
        self.assertEqual(client.timeout, whatsapp.TURN_TIMEOUT)

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_answer_measures_model_latency(self):
        provider = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: fake_response()))
        with patch.object(agent, '_provider_client', return_value=provider):
            reply, usage = agent.answer([{'role': 'user', 'content': 'hola'}],
                                        business_prompt='X', handlers={})
        self.assertEqual(reply, 'ok')
        self.assertIsInstance(usage['latency_ms'], int)
        self.assertGreaterEqual(usage['latency_ms'], 0)

    @override_settings(ANTHROPIC_API_KEY='test-key')
    def test_a_timeout_surfaces_as_api_error_with_the_usage(self):
        def create(**kw):
            raise timeout_error()
        provider = SimpleNamespace(messages=SimpleNamespace(create=create))
        with patch.object(agent, '_provider_client', return_value=provider):
            with self.assertRaises(anthropic.APIError) as ctx:
                agent.answer([{'role': 'user', 'content': 'hola'}], business_prompt='X',
                             handlers={})
        self.assertIn('latency_ms', ctx.exception.usage)


# ---- 2. /healthz/ ------------------------------------------------------------

class HealthzTests(TestCase):

    def test_ok(self):
        resp = self.client.get('/healthz/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'ok': True})
        self.assertIn('no-store', resp['Cache-Control'])
        self.assertNotIn('sessionid', resp.cookies)
        self.assertNotIn('csrftoken', resp.cookies)

    def test_head_works_and_post_is_refused(self):
        self.assertEqual(self.client.head('/healthz/').status_code, 200)
        self.assertEqual(self.client.post('/healthz/').status_code, 405)

    def test_database_down_is_503_without_details(self):
        with patch.object(health.connection, 'cursor', side_effect=RuntimeError('db path x')), \
                self.assertLogs('landing.health', 'WARNING'):
            resp = self.client.get('/healthz/')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {'ok': False})

    def test_cache_down_is_503(self):
        with patch.object(health.cache, 'set', side_effect=RuntimeError('no table')), \
                self.assertLogs('landing.health', 'WARNING'):
            resp = self.client.get('/healthz/')
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json(), {'ok': False})

    def test_exempt_from_the_https_redirect_in_production_settings(self):
        with override_settings(SECURE_SSL_REDIRECT=True,
                               SECURE_REDIRECT_EXEMPT=[r'^healthz/$']):
            self.assertEqual(self.client.get('/healthz/').status_code, 200)
            self.assertEqual(self.client.get('/privacy/').status_code, 301)
        # The same pattern is what settings.py sets when DEBUG is off.
        with open(os.path.join(settings.BASE_DIR, 'dominio_website', 'settings.py'),
                  encoding='utf-8') as fh:
            self.assertIn("SECURE_REDIRECT_EXEMPT = [r'^healthz/$']", fh.read())


# ---- 3. alert brake ----------------------------------------------------------

@override_settings(ADMINS=ADMINS, ANTHROPIC_API_KEY='test-key')
class AlertBrakeTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_alert_errors_once_per_hour_per_kind(self):
        with self.assertLogs('landing.ops_test', 'WARNING') as logs:
            self.assertTrue(health.alert(test_log, 'k1', 'fallo %s', 'a'))
            self.assertFalse(health.alert(test_log, 'k1', 'fallo %s', 'b'))
            self.assertTrue(health.alert(test_log, 'k2', 'fallo %s', 'c'))
        self.assertEqual([r.levelname for r in logs.records], ['ERROR', 'WARNING', 'ERROR'])

    def test_cache_down_still_alerts(self):
        with patch.object(health.cache, 'add', side_effect=RuntimeError('down')), \
                self.assertLogs('landing.ops_test', 'ERROR'):
            self.assertTrue(health.alert(test_log, 'k3', 'fallo'))

    def _chat(self, slug='acme', token=TOKEN, **extra):
        body = {'client': slug, 'token': token,
                'messages': [{'role': 'user', 'content': 'hola'}]}
        return self.client.post(reverse('chat_api'), data=json.dumps(body),
                                content_type='application/json', **extra)

    def test_model_outage_on_the_widget_mails_admins_once(self):
        make_client()
        with patch.object(agent, 'answer', side_effect=timeout_error()):
            responses = [self._chat() for _ in range(3)]
        for resp in responses:
            self.assertEqual(resp.status_code, 502)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('chat_api', mail.outbox[0].subject + mail.outbox[0].body)

    def test_widget_error_copy_is_neutral_on_a_customer_site(self):
        make_client()
        with patch.object(agent, 'answer', side_effect=timeout_error()):
            error = self._chat().json()['error']
        self.assertEqual(error, views.CHAT_ERROR_MODEL)
        self.assertNotIn('formulario', error)
        self.assertNotIn('dominiopr.com', error)

    def test_dominio_site_error_adds_the_contact(self):
        Client.objects.update_or_create(slug='dominio', defaults={'is_active': True})
        with patch.object(agent, 'answer', side_effect=timeout_error()):
            error = self._chat(slug='dominio',
                               HTTP_ORIGIN='https://dominiopr.com').json()['error']
        self.assertTrue(error.startswith(views.CHAT_ERROR_MODEL))
        self.assertIn('hola@dominiopr.com', error)

    def test_tenant_cap_copy_is_neutral(self):
        make_client(daily_message_cap=1)
        with patch.object(agent, 'answer', return_value=('Hola', {})):
            self.assertEqual(self._chat().status_code, 200)
            resp = self._chat()
        self.assertEqual(resp.status_code, 503)
        self.assertNotIn('formulario', resp.json()['error'])

    def test_demo_outage_mails_admins_once(self):
        body = json.dumps({'context': 'Vendo flores.',
                           'messages': [{'role': 'user', 'content': 'hola'}]})
        with patch.object(agent, 'answer', side_effect=timeout_error()):
            for _ in range(3):
                resp = self.client.post(reverse('demo_api'), data=body,
                                        content_type='application/json')
                self.assertEqual(resp.status_code, 502)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(META_APP_SECRET='ops-secret')
    def test_whatsapp_unhandled_error_is_logged_with_trace_and_mailed_once(self):
        raw = json.dumps({'object': 'whatsapp_business_account', 'entry': []}).encode()
        with patch.object(whatsapp, '_dispatch', side_effect=RuntimeError('boom')):
            for _ in range(3):
                resp = self.client.post(reverse('whatsapp_webhook'), data=raw,
                                        content_type='application/json',
                                        HTTP_X_HUB_SIGNATURE_256=whatsapp.sign(raw))
                self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('RuntimeError', mail.outbox[0].subject + mail.outbox[0].body)


# ---- 4. error pages ----------------------------------------------------------

@override_settings(ROOT_URLCONF='landing.tests_ops', DEBUG=False)
class ErrorPageTests(TestCase):

    def test_404(self):
        with self.assertLogs('django.request', 'WARNING'):
            resp = self.client.get('/no-existe/')
        self.assertContains(resp, 'Esta página no existe', status_code=404)

    def test_500(self):
        http = HttpClient(raise_request_exception=False)
        with self.assertLogs('django.request', 'ERROR'):
            resp = http.get('/boom/')
        self.assertContains(resp, 'Algo salió mal de nuestro lado', status_code=500)

    def test_403(self):
        with self.assertLogs('django.request', 'WARNING'):
            resp = self.client.get('/forbidden/')
        self.assertContains(resp, 'No tienes acceso a esta página', status_code=403)
        self.assertContains(resp, 'lang="es"', status_code=403)

    def test_400(self):
        with self.assertLogs('django.security', 'ERROR'):
            resp = self.client.get('/bad/')
        self.assertContains(resp, 'No pudimos procesar esa solicitud', status_code=400)

    def test_csrf_failure_is_the_branded_page(self):
        http = HttpClient(enforce_csrf_checks=True)
        with self.assertLogs('landing.health', 'WARNING'):
            resp = http.post('/form/', {'x': '1'})
        self.assertContains(resp, 'El formulario caducó', status_code=403)
        self.assertNotContains(resp, 'CSRF verification failed', status_code=403)
        self.assertEqual(settings.CSRF_FAILURE_VIEW, 'landing.health.csrf_failure')


# ---- 5. SEO / AEO ------------------------------------------------------------

class SeoTests(TestCase):

    def test_llms_txt(self):
        resp = self.client.get('/llms.txt')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp['Content-Type'].startswith('text/plain'))
        body = resp.content.decode()
        self.assertTrue(body.startswith('# DOMINIO'))
        self.assertIn('DOMINIO Chat', body)
        self.assertIn('24/7', body)
        self.assertIn('hola@dominiopr.com', body)
        self.assertIn(settings.SITE_URL + '/', body)
        for plan in views.PUBLIC_PLANS:
            self.assertIn(f"- {plan['name']} (", body)
        for name in ('get_started', 'privacy', 'terms'):
            self.assertIn(settings.SITE_URL + reverse(name), body)
        for plan in views.PLANS:
            if not plan.get('public', True):
                self.assertNotIn(f"- {plan['name']} (", body)

    def test_favicon_redirects_permanently_to_the_png(self):
        resp = self.client.get('/favicon.ico')
        self.assertEqual(resp.status_code, 301)
        self.assertIn('favicon-32', resp['Location'])

    def test_sitemap_has_lastmod(self):
        resp = self.client.get('/sitemap.xml')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode().count('<lastmod>'), 4)


# ---- 6. widget copy ----------------------------------------------------------

class WidgetCopyTests(TestCase):

    def test_widget_fallbacks_are_neutral(self):
        make_client()
        js = self.client.get('/widget.js', {'key': 'acme'}).content.decode()
        self.assertIn('No pude responder en este momento. Intenta de nuevo en un minuto.', js)
        self.assertNotIn('formulario', js)
        # The contact tail only applies to DOMINIO's own widget.
        self.assertIn("CFG.slug === 'dominio'", js)


# ---- 7. performance ----------------------------------------------------------

class ServerTimingTests(TestCase):

    def test_header_on_every_response(self):
        resp = self.client.get('/healthz/')
        self.assertRegex(resp['Server-Timing'], r'^app;dur=\d+$')

    def test_slow_request_logs_the_route_not_the_raw_path(self):
        clock = iter([0.0, 5.0])
        fake_time = SimpleNamespace(monotonic=lambda: next(clock))
        with patch('landing.middleware.time', fake_time), \
                self.assertLogs('landing.timing', 'WARNING') as logs:
            self.client.get('/confirmar/secret-token-123/?email=x@y.com')
        line = logs.records[0].getMessage()
        self.assertIn('GET /confirmar/<str:token>/', line)
        self.assertIn('ms=5000', line)
        self.assertNotIn('secret-token-123', line)
        self.assertNotIn('x@y.com', line)

    def test_fast_request_logs_nothing(self):
        with self.assertNoLogs('landing.timing', 'WARNING'):
            self.client.get('/healthz/')


class LatencyLedgerTests(TestCase):

    def test_record_stores_latency(self):
        usage = dict(agent.new_usage(MODEL), input_tokens=10, output_tokens=5,
                     api_calls=1, latency_ms=1234)
        self.assertEqual(usage_ledger.record('chat', usage).latency_ms, 1234)
        usage.pop('latency_ms')
        self.assertIsNone(usage_ledger.record('chat', usage).latency_ms)

    def test_percentiles_of_today(self):
        for ms in range(100, 1100, 100):          # 100..1000
            UsageEvent.objects.create(kind='chat', model=MODEL, latency_ms=ms)
        UsageEvent.objects.create(kind='tagging', model=MODEL, latency_ms=99999)
        UsageEvent.objects.create(kind='chat', model=MODEL, latency_ms=None)
        stats = usage_ledger.latency_percentiles(*usage_ledger.day_bounds())
        self.assertEqual(stats, {'count': 10, 'p50': 500, 'p95': 1000})

    def test_no_samples(self):
        self.assertEqual(usage_ledger.latency_percentiles(*usage_ledger.day_bounds()),
                         {'count': 0, 'p50': None, 'p95': None})

    def test_control_center_shows_it_to_staff(self):
        make_client()
        UsageEvent.objects.create(kind='chat', model=MODEL, latency_ms=800)
        staff = get_user_model().objects.create_user('staff', password='x', is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(reverse('control'))
        self.assertContains(resp, 'Latencia del modelo hoy')
        self.assertContains(resp, 'p50 <b>800 ms</b>')

    def test_control_center_is_not_for_tenants(self):
        user = get_user_model().objects.create_user('owner', password='x')
        self.client.force_login(user)
        resp = self.client.get(reverse('control'))
        self.assertNotEqual(resp.status_code, 200)


# ---- 8. optional Sentry ------------------------------------------------------

class SentryInitTests(SimpleTestCase):
    """settings.py runs once per process, so each case imports it in a child
    interpreter with a controlled environment."""

    NO_DSN_PROBE = ('import sys; import dominio_website.settings; '
                    'print("loaded" if "sentry_sdk" in sys.modules else "absent")')
    DSN_PROBE = ('import sys; import dominio_website.settings; '
                 'print("loaded" if "sentry_sdk" in sys.modules else "absent"); '
                 'import sentry_sdk; o = sentry_sdk.get_client().options; '
                 'print(o.get("send_default_pii"), o.get("traces_sample_rate"))')

    def _run(self, dsn):
        env = dict(os.environ, SENTRY_DSN=dsn, DJANGO_DEBUG='True',
                   DJANGO_SECRET_KEY='ops-test-key')
        out = subprocess.run([sys.executable, '-c', self.DSN_PROBE if dsn else self.NO_DSN_PROBE],
                             cwd=settings.BASE_DIR, env=env, capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.split()

    def test_without_dsn_sentry_is_never_imported(self):
        self.assertEqual(self._run(''), ['absent'])

    def test_with_dsn_it_starts_without_pii_or_tracing(self):
        self.assertEqual(self._run('https://public@o0.ingest.sentry.io/0'),
                         ['loaded', 'False', '0.0'])


# ---- 9. cost_alert -----------------------------------------------------------

@override_settings(ADMINS=ADMINS, DOMINIO_DAILY_COST_ALERT_USD=5, DOMINIO_AGENT_DAILY_CAP=100)
class CostAlertTests(TestCase):

    def setUp(self):
        cache.clear()

    def _run(self, *args):
        out = StringIO()
        call_command('cost_alert', *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def _spend(self, usd, slug='acme'):
        UsageEvent.objects.create(kind='chat', model=MODEL, client_slug=slug,
                                  cost_usd=Decimal(str(usd)))

    def _messages_today(self, n):
        cache.set('chat:global:' + timezone.now().strftime('%Y%m%d'), n, 3600)

    def test_below_both_thresholds_sends_nothing(self):
        self._spend('1.25')
        self._messages_today(79)
        self._run()
        self.assertEqual(mail.outbox, [])

    def test_spend_over_threshold_mails_once_per_day(self):
        self._spend('3.00')
        self._spend('2.50', slug='beta')
        self._run()
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn('Gasto de IA alto', msg.subject)
        self.assertIn('$5.5000', msg.body)
        self.assertIn('acme', msg.body)

    def test_older_spend_does_not_count(self):
        UsageEvent.objects.create(kind='chat', model=MODEL, cost_usd=Decimal('50'),
                                  created_at=timezone.now() - dt.timedelta(days=2))
        self._run()
        self.assertEqual(mail.outbox, [])

    def test_eighty_percent_of_the_message_cap_mails_once(self):
        self._messages_today(80)
        self._run()
        self._run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('80%', mail.outbox[0].subject)

    def test_both_thresholds_are_independent(self):
        self._spend('9')
        self._messages_today(95)
        self._run()
        self.assertEqual(len(mail.outbox), 2)

    def test_dry_run_sends_and_marks_nothing(self):
        self._spend('9')
        self.assertIn('[dry-run]', self._run('--dry-run'))
        self.assertEqual(mail.outbox, [])
        self._run()
        self.assertEqual(len(mail.outbox), 1)

    def test_failed_send_is_retried_next_run(self):
        self._spend('9')
        with patch('landing.management.commands.cost_alert.mail_admins',
                   side_effect=OSError('smtp down')):
            with self.assertRaises(Exception):
                self._run()
        self._run()
        self.assertEqual(len(mail.outbox), 1)


class AgentTurnBudgetTests(TestCase):
    """The whole turn (tool rounds + final call + retries) ends before
    gunicorn's 60 s timeout: late calls get a shorter timeout, and an exhausted
    budget is a timeout error the views already turn into a friendly reply."""

    def _tool_response(self):
        block = SimpleNamespace(type='tool_use', name='capture_lead', input={}, id='t1')
        return SimpleNamespace(stop_reason='tool_use', content=[block],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                                     cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0))

    def test_late_calls_shrink_timeout_and_budget_runs_out(self):
        from unittest import mock
        clock = {'t': 1000.0}
        provider = mock.MagicMock()
        provider.messages.create.side_effect = lambda **kw: self._tool_response()
        shrunk = mock.MagicMock()
        shrunk.messages.create.side_effect = lambda **kw: self._tool_response()
        provider.with_options.return_value = shrunk

        def handler(_):
            clock['t'] += 20  # each tool round "costs" 20 s of wall clock
            return 'ok'

        with self.settings(ANTHROPIC_API_KEY='k', DOMINIO_AGENT_TURN_BUDGET=50,
                           DOMINIO_AGENT_TIMEOUT=25), \
                mock.patch.object(agent, '_provider_client', return_value=provider), \
                mock.patch.object(agent.time, 'monotonic', side_effect=lambda: clock['t']):
            with self.assertRaises(anthropic.APITimeoutError):
                agent.answer([{'role': 'user', 'content': 'hola'}],
                             handlers={'capture_lead': handler})
        # First call used the client as configured; the late ones were shrunk.
        self.assertEqual(provider.messages.create.call_count, 1)
        kwargs = [c.kwargs for c in provider.with_options.call_args_list]
        self.assertTrue(kwargs)
        self.assertTrue(all(k['timeout'] <= 30 for k in kwargs), kwargs)
        self.assertEqual(kwargs[-1]['max_retries'], 0)
