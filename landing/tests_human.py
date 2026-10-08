"""Phase 5: hCaptcha + signed human pass.

Everything is OFF while HCAPTCHA_SITEKEY / HCAPTCHA_SECRET are empty (and
settings forces them empty under `manage.py test`), so these tests enable it
with hCaptcha's public test keys via override_settings. The network is never
touched: siteverify is a mocked urlopen.
"""
import io
import json
import time
import urllib.error
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from landing import agent, human
from landing.models import Client, ContactSubmission

SITEKEY = '10000000-ffff-ffff-ffff-000000000001'
SECRET = '0x0000000000000000000000000000000000000000'
ON = dict(HCAPTCHA_SITEKEY=SITEKEY, HCAPTCHA_SECRET=SECRET)
URLOPEN = 'landing.human.urllib.request.urlopen'
TOKEN = 'acmetoken' * 4
VISITOR = 'visitor1234abcd'


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def siteverify(success=True, codes=None):
    """A urlopen stand-in; records the requests it receives."""
    calls = []

    def urlopen(req, timeout=None, context=None):
        calls.append({'url': req.full_url, 'body': req.data.decode(), 'timeout': timeout})
        body = {'success': success}
        if codes:
            body['error-codes'] = codes
        return _Resp(json.dumps(body).encode())
    return urlopen, calls


def fake_answer(*args, **kwargs):
    return '¡Hola!', {'input_tokens': 1, 'output_tokens': 1}


def today():
    return timezone.now().strftime('%Y%m%d')


# ---- the module -----------------------------------------------------------

class HumanModuleTests(TestCase):

    def setUp(self):
        cache.clear()

    def test_disabled_without_both_keys(self):
        self.assertFalse(human.enabled())
        with override_settings(HCAPTCHA_SITEKEY=SITEKEY, HCAPTCHA_SECRET=''):
            self.assertFalse(human.enabled())
        with override_settings(**ON):
            self.assertTrue(human.enabled())

    @override_settings(**ON)
    def test_verify_posts_form_encoded_with_timeout(self):
        fake, calls = siteverify(True)
        with patch(URLOPEN, side_effect=fake):
            self.assertTrue(human.verify('tok', '203.0.113.5'))
        self.assertEqual(calls[0]['url'], human.VERIFY_URL)
        self.assertEqual(calls[0]['timeout'], 5)
        self.assertIn('response=tok', calls[0]['body'])
        self.assertIn('remoteip=203.0.113.5', calls[0]['body'])
        self.assertIn(f'sitekey={SITEKEY}', calls[0]['body'])

    @override_settings(**ON)
    def test_verify_false_on_success_false(self):
        fake, _ = siteverify(False, ['invalid-input-response'])
        with patch(URLOPEN, side_effect=fake):
            self.assertFalse(human.verify('tok'))

    @override_settings(**ON)
    def test_verify_fails_open_on_network_error(self):
        with patch(URLOPEN, side_effect=urllib.error.URLError('down')), \
                self.assertLogs('landing.human', 'WARNING'):
            self.assertTrue(human.verify('tok'))

    @override_settings(**ON)
    def test_verify_fails_open_on_garbage_body(self):
        with patch(URLOPEN, return_value=_Resp(b'<html>')):
            self.assertTrue(human.verify('tok'))

    @override_settings(**ON)
    def test_empty_token_is_rejected_without_network(self):
        with patch(URLOPEN) as m:
            self.assertFalse(human.verify(''))
            self.assertFalse(human.verify(None))
        m.assert_not_called()

    def test_pass_round_trip_and_binding(self):
        p = human.issue_pass('acme', VISITOR)
        self.assertTrue(human.check_pass(p, 'acme', VISITOR))
        self.assertFalse(human.check_pass(p, 'other', VISITOR))
        self.assertFalse(human.check_pass(p, 'acme', 'someoneelse99'))
        self.assertFalse(human.check_pass(p + 'x', 'acme', VISITOR))
        self.assertFalse(human.check_pass('', 'acme', VISITOR))
        self.assertFalse(human.check_pass(None, 'acme', VISITOR))

    def test_expired_pass_is_rejected(self):
        with patch('django.core.signing.time.time', return_value=time.time() - 7300):
            p = human.issue_pass('acme', VISITOR)
        self.assertFalse(human.check_pass(p, 'acme', VISITOR))

    @override_settings(HUMAN_PASS_MAX_MESSAGES=2)
    def test_per_pass_message_cap(self):
        p = human.issue_pass('acme', VISITOR)
        self.assertTrue(human.check_pass(p, 'acme', VISITOR))
        self.assertTrue(human.check_pass(p, 'acme', VISITOR))
        self.assertFalse(human.check_pass(p, 'acme', VISITOR))
        # A fresh pass (new challenge) gets a fresh budget.
        with patch('django.core.signing.time.time', return_value=time.time() + 5):
            p2 = human.issue_pass('acme', VISITOR)
        self.assertTrue(human.check_pass(p2, 'acme', VISITOR))

    def test_normalize_origin(self):
        self.assertEqual(human.normalize_origin('https://Acme.com'), 'https://acme.com')
        self.assertEqual(human.normalize_origin('https://acme.com/'), 'https://acme.com')
        self.assertEqual(human.normalize_origin('http://localhost:8765'), 'http://localhost:8765')
        for bad in ('', 'acme.com', 'javascript:alert(1)', 'https://acme.com/path',
                    'https://u:p@acme.com', 'https://acme.com?x=1', 'ftp://acme.com',
                    "https://acme.com;script-src *", None):
            self.assertEqual(human.normalize_origin(bad), '', bad)


# ---- chat_api / demo_api --------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='test-key', CONTACT_NOTIFY_EMAIL='ops@example.com')
class ChatHumanPassTests(TestCase):

    def setUp(self):
        cache.clear()
        self.c = Client.objects.create(
            slug='acme', widget_token=TOKEN, name='Acme', system_prompt='x',
            notify_email='o@acme.com', allowed_origins='acme.com', daily_message_cap=100)

    def _chat(self, **extra):
        body = {'client': 'acme', 'token': TOKEN,
                'messages': [{'role': 'user', 'content': 'hola'}], **extra}
        return self.client.post(reverse('chat_api'), data=json.dumps(body),
                                content_type='application/json',
                                HTTP_ORIGIN='https://acme.com', REMOTE_ADDR='203.0.113.5')

    def _demo(self, **extra):
        body = {'context': 'Pizzeria', 'messages': [{'role': 'user', 'content': 'hola'}], **extra}
        return self.client.post(reverse('demo_api'), data=json.dumps(body),
                                content_type='application/json', REMOTE_ADDR='203.0.113.5')

    def _counters(self):
        d = today()
        return [cache.get(k) for k in (
            f'chat:global:{d}', f'chat:client:acme:{d}',
            f'chat:visitor:acme:203.0.113.5:{d}', f'demo:global:{d}', f'demoip:203.0.113.5:{d}')]

    def test_disabled_chat_and_demo_behave_as_before(self):
        with patch.object(agent, 'answer', side_effect=fake_answer):
            self.assertEqual(self._chat().status_code, 200)
            self.assertEqual(self._demo().status_code, 200)

    @override_settings(**ON)
    def test_enabled_chat_without_pass_is_challenged_and_spends_nothing(self):
        with patch.object(agent, 'answer', side_effect=fake_answer) as ans:
            resp = self._chat(visitor=VISITOR)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {'error': 'Verifica que eres humano para continuar.',
                                       'challenge': True})
        # CORS header still there, so the cross-origin widget can read it.
        self.assertEqual(resp['Access-Control-Allow-Origin'], 'https://acme.com')
        ans.assert_not_called()
        self.assertEqual(self._counters(), [None] * 5)

    @override_settings(**ON)
    def test_token_and_origin_checks_still_come_first(self):
        resp = self.client.post(reverse('chat_api'), data=json.dumps(
            {'client': 'acme', 'token': 'wrong', 'messages': [{'role': 'user', 'content': 'x'}]}),
            content_type='application/json', HTTP_ORIGIN='https://acme.com')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('challenge', resp.json())

    @override_settings(**ON)
    def test_valid_pass_is_answered(self):
        p = human.issue_pass('acme', VISITOR)
        with patch.object(agent, 'answer', side_effect=fake_answer):
            resp = self._chat(visitor=VISITOR, human=p)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['reply'], '¡Hola!')

    @override_settings(**ON)
    def test_pass_for_other_slug_or_visitor_is_rejected(self):
        with patch.object(agent, 'answer', side_effect=fake_answer):
            self.assertEqual(self._chat(visitor=VISITOR,
                                        human=human.issue_pass('other', VISITOR)).status_code, 403)
            self.assertEqual(self._chat(visitor='anothervisitor1',
                                        human=human.issue_pass('acme', VISITOR)).status_code, 403)
        self.assertEqual(self._counters(), [None] * 5)

    @override_settings(**ON)
    def test_expired_pass_is_challenged(self):
        with patch('django.core.signing.time.time', return_value=time.time() - 7300):
            p = human.issue_pass('acme', VISITOR)
        self.assertEqual(self._chat(visitor=VISITOR, human=p).status_code, 403)

    @override_settings(HUMAN_PASS_MAX_MESSAGES=2, **ON)
    def test_per_pass_cap_reopens_the_challenge(self):
        p = human.issue_pass('acme', VISITOR)
        with patch.object(agent, 'answer', side_effect=fake_answer):
            self.assertEqual(self._chat(visitor=VISITOR, human=p).status_code, 200)
            self.assertEqual(self._chat(visitor=VISITOR, human=p).status_code, 200)
            resp = self._chat(visitor=VISITOR, human=p)
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.json()['challenge'])

    @override_settings(**ON)
    def test_first_party_chat_binds_to_dominio(self):
        p = human.issue_pass('dominio', VISITOR)
        with patch.object(agent, 'answer', side_effect=fake_answer):
            resp = self.client.post(reverse('chat_api'), data=json.dumps(
                {'messages': [{'role': 'user', 'content': 'hola'}], 'visitor': VISITOR,
                 'human': p}), content_type='application/json',
                HTTP_ORIGIN='https://dominiopr.com')  # seeded allowlist (0005)
        self.assertEqual(resp.status_code, 200)

    @override_settings(**ON)
    def test_demo_requires_its_own_pass(self):
        with patch.object(agent, 'answer', side_effect=fake_answer):
            resp = self._demo(visitor=VISITOR)
            self.assertEqual(resp.status_code, 403)
            self.assertTrue(resp.json()['challenge'])
            self.assertEqual(self._counters(), [None] * 5)
            # A tenant's pass is not a demo pass.
            self.assertEqual(self._demo(visitor=VISITOR,
                                        human=human.issue_pass('acme', VISITOR)).status_code, 403)
            ok = self._demo(visitor=VISITOR, human=human.issue_pass(human.DEMO_KEY, VISITOR))
        self.assertEqual(ok.status_code, 200)


# ---- /verificar/ ----------------------------------------------------------

@override_settings(SITE_URL='https://dominiopr.com', DEBUG=False, **ON)
class ChallengePageTests(TestCase):

    def setUp(self):
        cache.clear()
        self.c = Client.objects.create(
            slug='acme', widget_token=TOKEN, name='Acme', system_prompt='x',
            notify_email='o@acme.com', allowed_origins='acme.com, www.acme.com')

    def _get(self, **params):
        q = {'key': 'acme', 'visitor': VISITOR, 'origin': 'https://acme.com', **params}
        return self.client.get(reverse('human_verify'), q)

    @override_settings(HCAPTCHA_SITEKEY='', HCAPTCHA_SECRET='')
    def test_404_while_disabled(self):
        self.assertEqual(self._get().status_code, 404)

    def test_frameable_only_by_the_client_origins(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('X-Frame-Options', resp.headers)
        csp = resp.headers['Content-Security-Policy']
        # The view's own header wins over the middleware's setdefault.
        self.assertNotIn("frame-ancestors 'none'", csp)
        self.assertIn("frame-ancestors 'self' https://acme.com https://www.acme.com", csp)
        self.assertIn('https://*.hcaptcha.com', csp)
        self.assertIn(f'data-sitekey="{SITEKEY}"', resp.content.decode())
        self.assertIn('https://js.hcaptcha.com/1/api.js?hl=es', resp.content.decode())

    def test_bad_origin_is_rejected(self):
        self.assertEqual(self._get(origin='https://evil.com').status_code, 403)
        self.assertEqual(self._get(origin='https://acme.com/x').status_code, 400)
        self.assertEqual(self._get(origin='').status_code, 400)
        self.assertEqual(self._get(visitor='short').status_code, 400)
        self.assertEqual(self._get(key='nope').status_code, 404)

    def test_client_without_allowlist_accepts_any_https_site(self):
        Client.objects.filter(pk=self.c.pk).update(allowed_origins='')
        resp = self._get(origin='https://anything.example')
        self.assertEqual(resp.status_code, 200)
        self.assertIn("frame-ancestors 'self' https:;", resp.headers['Content-Security-Policy'])
        self.assertEqual(self._get(origin='http://anything.example').status_code, 403)

    def test_first_party_keys(self):
        for key in ('dominio', human.DEMO_KEY):
            self.assertEqual(self._get(key=key, origin='https://dominiopr.com').status_code, 200)
            self.assertEqual(self._get(key=key, origin='https://acme.com').status_code, 403)
            # localhost only while DEBUG is on
            self.assertEqual(self._get(key=key, origin='http://localhost:8765').status_code, 403)

    def test_allowlist_entry_cannot_inject_csp(self):
        Client.objects.filter(pk=self.c.pk).update(
            allowed_origins="acme.com, evil.com;script-src *")
        csp = self._get().headers['Content-Security-Policy']
        self.assertNotIn('script-src *', csp)

    def test_post_issues_pass_to_the_exact_origin(self):
        fake, calls = siteverify(True)
        with patch(URLOPEN, side_effect=fake):
            resp = self.client.post(reverse('human_verify'), {
                'key': 'acme', 'visitor': VISITOR, 'origin': 'https://acme.com',
                'h-captcha-response': 'tok'}, REMOTE_ADDR='203.0.113.5')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(calls), 1)
        html = resp.content.decode()
        self.assertIn('postMessage(data, target)', html)
        self.assertNotIn("'*'", html)
        self.assertIn('<script id="dmn-origin" type="application/json">"https://acme.com"</script>',
                      html)
        start = html.index('<script id="dmn-pass" type="application/json">') + len(
            '<script id="dmn-pass" type="application/json">')
        data = json.loads(html[start:html.index('</script>', start)])
        self.assertEqual(data['type'], 'dominio-human-pass')
        self.assertEqual(data['visitor'], VISITOR)
        self.assertTrue(human.check_pass(data['pass'], 'acme', VISITOR))
        self.assertIn('frame-ancestors', resp.headers['Content-Security-Policy'])
        self.assertNotIn('X-Frame-Options', resp.headers)

    def test_post_with_rejected_token_issues_nothing(self):
        fake, _ = siteverify(False)
        with patch(URLOPEN, side_effect=fake):
            resp = self.client.post(reverse('human_verify'), {
                'key': 'acme', 'visitor': VISITOR, 'origin': 'https://acme.com',
                'h-captcha-response': 'tok'})
        html = resp.content.decode()
        self.assertIn('Confirma que no eres un robot.', html)
        self.assertNotIn('dmn-pass', html)

    def test_post_with_bad_origin_issues_nothing(self):
        with patch(URLOPEN) as m:
            resp = self.client.post(reverse('human_verify'), {
                'key': 'acme', 'visitor': VISITOR, 'origin': 'https://evil.com',
                'h-captcha-response': 'tok'})
        self.assertEqual(resp.status_code, 403)
        m.assert_not_called()


# ---- widget.js ------------------------------------------------------------

@override_settings(SITE_URL='https://dominiopr.com')
class WidgetChallengeTests(TestCase):

    def test_widget_carries_challenge_url_and_origin_check(self):
        Client.objects.create(slug='acme', name='Acme', system_prompt='x',
                              notify_email='o@acme.com', is_active=True)
        js = self.client.get(reverse('widget_js'), {'key': 'acme'}).content.decode()
        self.assertIn("challenge: 'https://dominiopr.com/verificar/'", js)
        self.assertIn('e.source !== hvFrame.contentWindow', js)
        self.assertIn('e.origin !== CHALLENGE_ORIGIN', js)
        self.assertIn("'dominio-human-pass'", js)
        self.assertIn('visitor: VISITOR, human: getPass()', js)
        self.assertNotIn("postMessage", js)


# ---- classic forms --------------------------------------------------------

CONTACT = {'name': 'Ana', 'email': 'ana@example.com', 'service': 'consulting',
           'message': 'Hola, necesito ayuda.'}


class FormHumanCheckTests(TestCase):

    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_superuser(
            'boss', 'boss@example.com', 'S3gura-pass-123')

    def test_disabled_forms_render_no_captcha_and_work(self):
        page = self.client.get(reverse('index')).content.decode()
        self.assertNotIn('h-captcha', page)
        self.assertNotIn('js.hcaptcha.com', page)
        self.client.post(reverse('index'), CONTACT)
        self.assertEqual(ContactSubmission.objects.count(), 1)
        resp = self.client.post(reverse('login'), {'username': 'boss',
                                                   'password': 'S3gura-pass-123'})
        self.assertEqual(resp.status_code, 302)

    @override_settings(**ON)
    def test_enabled_pages_render_the_checkbox(self):
        for name in ('index', 'get_started', 'login', 'password_reset'):
            page = self.client.get(reverse(name)).content.decode()
            self.assertIn(f'class="h-captcha" data-sitekey="{SITEKEY}"', page, name)
            self.assertIn('https://js.hcaptcha.com/1/api.js?hl=es', page, name)
        page = self.client.get('/admin/login/').content.decode()
        self.assertIn('class="h-captcha"', page)

    @override_settings(**ON)
    def test_contact_form_requires_token(self):
        self.client.post(reverse('index'), CONTACT)
        self.assertEqual(ContactSubmission.objects.count(), 0)
        fake, _ = siteverify(True)
        cache.clear()
        with patch(URLOPEN, side_effect=fake):
            self.client.post(reverse('index'), {**CONTACT, 'h-captcha-response': 'tok'})
        self.assertEqual(ContactSubmission.objects.count(), 1)

    @override_settings(**ON)
    def test_get_started_requires_token(self):
        resp = self.client.post(reverse('get_started'), {
            'company': 'Acme', 'name': 'Ana', 'email': 'ana@example.com', 'plan': 'custom'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Confirma que no eres un robot.')
        self.assertEqual(ContactSubmission.objects.count(), 0)

    @override_settings(**ON)
    def test_dashboard_login_requires_token(self):
        resp = self.client.post(reverse('login'), {'username': 'boss',
                                                   'password': 'S3gura-pass-123'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Confirma que no eres un robot.')
        self.assertNotIn('_auth_user_id', self.client.session)
        fake, _ = siteverify(True)
        with patch(URLOPEN, side_effect=fake):
            resp = self.client.post(reverse('login'), {
                'username': 'boss', 'password': 'S3gura-pass-123', 'h-captcha-response': 'tok'})
        self.assertEqual(resp.status_code, 302)

    @override_settings(**ON)
    def test_password_reset_requires_token(self):
        resp = self.client.post(reverse('password_reset'), {'email': 'boss@example.com'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Confirma que no eres un robot.')
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(**ON)
    def test_admin_login_requires_token(self):
        resp = self.client.post('/admin/login/', {'username': 'boss',
                                                  'password': 'S3gura-pass-123'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Confirma que no eres un robot.')
        self.assertNotIn('_auth_user_id', self.client.session)
        fake, _ = siteverify(True)
        with patch(URLOPEN, side_effect=fake):
            resp = self.client.post('/admin/login/', {
                'username': 'boss', 'password': 'S3gura-pass-123',
                'h-captcha-response': 'tok', 'next': '/admin/'})
        self.assertEqual(resp.status_code, 302)


class CspHcaptchaTests(TestCase):

    def test_site_policy_allows_hcaptcha(self):
        csp = self.client.get(reverse('index')).headers['Content-Security-Policy']
        self.assertIn("frame-src 'self' https://hcaptcha.com https://*.hcaptcha.com", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        for directive in ('script-src', 'style-src', 'connect-src'):
            part = next(p for p in csp.split(';') if p.strip().startswith(directive))
            self.assertIn('https://*.hcaptcha.com', part, directive)


class PreflightHcaptchaTests(TestCase):

    def _run(self):
        from django.core.management import call_command
        out = io.StringIO()
        call_command('preflight', django=True, stdout=out, stderr=out)
        return out.getvalue()

    @override_settings(DEBUG=False)
    def test_missing_keys_warn_in_production(self):
        self.assertRegex(self._run(), r'\[warn\] HCAPTCHA .*human check is OFF')

    @override_settings(DEBUG=False, HCAPTCHA_SITEKEY='real-key', HCAPTCHA_SECRET='0xreal')
    def test_keys_present_ok(self):
        self.assertRegex(self._run(), r'\[ ok \] HCAPTCHA')
