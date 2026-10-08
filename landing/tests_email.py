"""Phase 4: real-email checks (landing/emailcheck.py) and lead double opt-in.

No test here touches the network: every DNS-over-HTTPS call goes through a
fake `urlopen`. The DNS layer is off by default under `manage.py test`
(settings.TESTING), so these tests switch it on explicitly.
"""

import json
import re
import socket
import time
import urllib.error
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core import mail, signing
from django.core.cache import cache, caches
from django.test import Client as HttpClient
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import emailcheck, notify
from .emailcheck import DISPOSABLE_REASON, UNDELIVERABLE_REASON, check_email
from .forms import ContactForm
from .models import Booking, Client, ContactSubmission
from .views import _booking_handler, _chat_lead_handler, _human_handler, lead_confirm_url

URLOPEN = 'landing.emailcheck.urllib.request.urlopen'
TOKEN = 'x' * 32
MX_OK = {'Status': 0, 'Answer': [{'name': 'acme.com.', 'type': 15, 'data': '10 mx.acme.com.'}]}
NXDOMAIN = {'Status': 3}
NO_ANSWER = {'Status': 0}


def fake_dns(answers):
    """A urlopen stand-in answering DoH queries from `answers`, keyed by
    (name, type). Values are DoH JSON dicts or exceptions to raise. Unlisted
    queries get an empty NOERROR answer. Returns (urlopen, calls)."""
    calls = []

    def urlopen(req, timeout=None, context=None):
        query = parse_qs(urlparse(req.full_url).query)
        key = (query['name'][0], query['type'][0])
        calls.append(key)
        assert req.get_header('Accept') == 'application/dns-json'
        assert timeout is not None and timeout <= emailcheck.DNS_TIMEOUT
        value = answers.get(key, NO_ANSWER)
        if isinstance(value, BaseException):
            raise value
        resp = MagicMock()
        resp.read.return_value = json.dumps(value).encode('utf-8')
        resp.__enter__.return_value = resp
        return resp

    return urlopen, calls


def all_mail_text():
    parts = []
    for m in mail.outbox:
        parts.append(m.subject)
        parts.append(m.body)
        parts.extend(content for content, _ in m.alternatives)
    return '\n'.join(parts)


class DefaultsTests(TestCase):
    def test_dns_check_is_forced_off_under_tests(self):
        from django.conf import settings
        self.assertTrue(settings.TESTING)
        self.assertFalse(settings.DOMINIO_EMAIL_DNS_CHECK)
        with patch(URLOPEN) as urlopen:
            self.assertEqual(check_email('a@some-unknown-domain.com'), (True, ''))
        urlopen.assert_not_called()


@override_settings(DOMINIO_EMAIL_DNS_CHECK=True)
class CheckEmailTests(TestCase):
    def setUp(self):
        cache.clear()
        caches['emaildns'].clear()

    def test_invalid_syntax_rejected(self):
        self.assertEqual(check_email('no-at-sign'), (False, emailcheck.INVALID_REASON))
        self.assertEqual(check_email(''), (False, emailcheck.INVALID_REASON))

    def test_disposable_exact_domain_rejected_without_dns(self):
        dns, calls = fake_dns({})
        with patch(URLOPEN, side_effect=dns):
            self.assertEqual(check_email('x@mailinator.com'), (False, DISPOSABLE_REASON))
            self.assertEqual(check_email('x@YOPMAIL.com'), (False, DISPOSABLE_REASON))
        self.assertEqual(calls, [])

    def test_disposable_subdomain_rejected(self):
        self.assertEqual(check_email('x@inbox.mailinator.com'), (False, DISPOSABLE_REASON))
        self.assertEqual(check_email('x@a.b.guerrillamail.net'), (False, DISPOSABLE_REASON))
        # A legit domain that merely ends with a listed name is not caught.
        self.assertTrue(emailcheck.is_disposable('mailinator.com'))
        self.assertFalse(emailcheck.is_disposable('notreallymailinator.com.pr'))
        self.assertFalse(emailcheck.is_disposable('gmail.com'))

    def test_nxdomain_rejected(self):
        dns, _ = fake_dns({('nope-xyz.com', 'MX'): NXDOMAIN})
        with patch(URLOPEN, side_effect=dns):
            self.assertEqual(check_email('a@nope-xyz.com'), (False, UNDELIVERABLE_REASON))

    def test_null_mx_rejected(self):
        dns, _ = fake_dns({('nomail.com', 'MX'): {
            'Status': 0, 'Answer': [{'name': 'nomail.com.', 'type': 15, 'data': '0 .'}]}})
        with patch(URLOPEN, side_effect=dns):
            self.assertEqual(check_email('a@nomail.com'), (False, UNDELIVERABLE_REASON))

    def test_mx_ok_accepted(self):
        dns, calls = fake_dns({('acme.com', 'MX'): MX_OK})
        with patch(URLOPEN, side_effect=dns):
            self.assertEqual(check_email('ana@acme.com'), (True, ''))
        self.assertEqual(calls, [('acme.com', 'MX')])

    def test_no_mx_but_a_record_accepted(self):
        dns, calls = fake_dns({('solo-a.com', 'A'): {
            'Status': 0, 'Answer': [{'name': 'solo-a.com.', 'type': 1, 'data': '1.2.3.4'}]}})
        with patch(URLOPEN, side_effect=dns):
            self.assertEqual(check_email('a@solo-a.com'), (True, ''))
        self.assertEqual(calls, [('solo-a.com', 'MX'), ('solo-a.com', 'A')])

    def test_no_mx_no_a_no_aaaa_rejected(self):
        # MX query answered with only a CNAME: not an MX record.
        dns, calls = fake_dns({('empty.com', 'MX'): {
            'Status': 0, 'Answer': [{'name': 'empty.com.', 'type': 5, 'data': 'x.com.'}]}})
        with patch(URLOPEN, side_effect=dns):
            self.assertEqual(check_email('a@empty.com'), (False, UNDELIVERABLE_REASON))
        self.assertEqual([c[1] for c in calls], ['MX', 'A', 'AAAA'])

    def test_timeout_fails_open_and_logs(self):
        dns, _ = fake_dns({('slow.com', 'MX'): socket.timeout('timed out')})
        with patch(URLOPEN, side_effect=dns), \
                self.assertLogs('landing.emailcheck', 'WARNING') as logs:
            self.assertEqual(check_email('a@slow.com'), (True, ''))
        self.assertIn('failed open', logs.output[0])

    def test_network_error_and_bad_payload_fail_open(self):
        for err in (urllib.error.URLError('no route'),
                    urllib.error.HTTPError('u', 503, 'down', {}, None)):
            cache.clear()
            caches['emaildns'].clear()
            dns, _ = fake_dns({('down.com', 'MX'): err})
            with patch(URLOPEN, side_effect=dns), self.assertLogs('landing.emailcheck', 'WARNING'):
                self.assertEqual(check_email('a@down.com'), (True, ''))
        cache.clear()
        caches['emaildns'].clear()
        with patch(URLOPEN, side_effect=fake_dns({('odd.com', 'MX'): ['not', 'a', 'dict']})[0]), \
                self.assertLogs('landing.emailcheck', 'WARNING'):
            self.assertEqual(check_email('a@odd.com'), (True, ''))

    def test_servfail_fails_open(self):
        dns, _ = fake_dns({('dnssec-broken.com', 'MX'): {'Status': 2}})
        with patch(URLOPEN, side_effect=dns), self.assertLogs('landing.emailcheck', 'WARNING'):
            self.assertEqual(check_email('a@dnssec-broken.com'), (True, ''))

    def test_per_domain_cache_reused(self):
        dns, calls = fake_dns({('acme.com', 'MX'): MX_OK, ('gone.com', 'MX'): NXDOMAIN})
        with patch(URLOPEN, side_effect=dns):
            self.assertTrue(check_email('ana@acme.com')[0])
            self.assertTrue(check_email('luis@ACME.com')[0])
            self.assertFalse(check_email('a@gone.com')[0])
            self.assertFalse(check_email('b@gone.com')[0])
        self.assertEqual(calls, [('acme.com', 'MX'), ('gone.com', 'MX')])
        self.assertEqual(caches['emaildns'].get('emaildns:acme.com'), 'ok')
        self.assertEqual(caches['emaildns'].get('emaildns:gone.com'), 'bad')

    def test_failure_is_cached_briefly_as_unknown(self):
        dns, calls = fake_dns({('flaky.com', 'MX'): socket.timeout()})
        with patch(URLOPEN, side_effect=dns), self.assertLogs('landing.emailcheck', 'WARNING'):
            check_email('a@flaky.com')
            check_email('b@flaky.com')
        self.assertEqual(len(calls), 1)  # the '' marker stops a retry storm
        self.assertEqual(caches['emaildns'].get('emaildns:flaky.com'), '')

    def test_idn_domain_is_punycoded_for_the_query(self):
        dns, calls = fake_dns({('xn--espaol-zwa.com', 'MX'): MX_OK})
        with patch(URLOPEN, side_effect=dns):
            self.assertEqual(check_email('ana@español.com'), (True, ''))
        self.assertEqual(calls, [('xn--espaol-zwa.com', 'MX')])

    @override_settings(DOMINIO_EMAIL_DNS_CHECK=False)
    def test_dns_disabled_never_calls_network(self):
        with patch(URLOPEN) as urlopen:
            self.assertEqual(check_email('a@whatever-domain.com'), (True, ''))
        urlopen.assert_not_called()


@override_settings(DOMINIO_EMAIL_DNS_CHECK=True, CONTACT_NOTIFY_EMAIL='leads@example.com')
class FormWiringTests(TestCase):
    def setUp(self):
        cache.clear()
        caches['emaildns'].clear()

    def _contact_data(self, email):
        return {'name': 'Ana', 'email': email, 'phone': '', 'company': '',
                'service': 'consulting', 'budget': '', 'message': 'Hola', 'website': ''}

    def test_contact_form_rejects_disposable_in_spanish(self):
        form = ContactForm(self._contact_data('ana@mailinator.com'))
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['email'], [DISPOSABLE_REASON])

    def test_contact_form_rejects_nxdomain_in_spanish(self):
        with patch(URLOPEN, side_effect=fake_dns({('gmial.co', 'MX'): NXDOMAIN})[0]):
            form = ContactForm(self._contact_data('ana@gmial.co'))
            self.assertFalse(form.is_valid())
        self.assertEqual(form.errors['email'], [UNDELIVERABLE_REASON])

    def test_contact_form_email_optional_skips_check(self):
        data = self._contact_data('')
        data['phone'] = '7871234567'
        with patch(URLOPEN) as urlopen:
            self.assertTrue(ContactForm(data).is_valid())
        urlopen.assert_not_called()

    def test_index_post_shows_error_and_saves_nothing(self):
        resp = self.client.post(reverse('index'), self._contact_data('ana@yopmail.com'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ese correo parece temporal')
        self.assertFalse(ContactSubmission.objects.exists())

    def test_get_started_shows_spanish_error(self):
        with patch(URLOPEN, side_effect=fake_dns({('nope-xyz.com', 'MX'): NXDOMAIN})[0]):
            resp = self.client.post(reverse('get_started'), {
                'company': 'Acme PR', 'name': 'Ana', 'email': 'ana@nope-xyz.com',
                'plan': 'pro', 'period': 'monthly'})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'No encontramos ese dominio de correo')
        self.assertFalse(ContactSubmission.objects.exists())

    def test_get_started_disposable(self):
        resp = self.client.post(reverse('get_started'), {
            'company': 'Acme PR', 'name': 'Ana', 'email': 'ana@10minutemail.com',
            'plan': 'pro', 'period': 'monthly'})
        self.assertContains(resp, 'Ese correo parece temporal')


@override_settings(DOMINIO_EMAIL_DNS_CHECK=True, CONTACT_NOTIFY_EMAIL='ops@example.com',
                   SITE_URL='https://dominiopr.com')
class ChatHandlerTests(TestCase):
    def setUp(self):
        cache.clear()
        caches['emaildns'].clear()
        self.rf = RequestFactory()
        self.shop = Client.objects.create(
            slug='acme', widget_token=TOKEN, name='Acme PR', system_prompt='x',
            notify_email='owner@acme.com', enable_bookings=True)

    def _request(self):
        req = self.rf.post('/api/chat/')
        req.META['REMOTE_ADDR'] = '10.9.8.7'
        return req

    def _slot(self):
        future = timezone.localtime(timezone.now() + timezone.timedelta(days=7))
        return future.replace(hour=10, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M')

    def test_lead_disposable_returns_reason_and_saves_nothing(self):
        handle = _chat_lead_handler(self._request(), self.shop)
        result = handle({'name': 'Ana', 'email': 'ana@mailinator.com', 'phone': '7871234567'})
        self.assertIn('No se guardó el lead', result)
        self.assertIn(DISPOSABLE_REASON, result)
        self.assertFalse(ContactSubmission.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_lead_nxdomain_returns_reason(self):
        with patch(URLOPEN, side_effect=fake_dns({('nope-xyz.com', 'MX'): NXDOMAIN})[0]):
            result = _chat_lead_handler(self._request(), self.shop)(
                {'name': 'Ana', 'email': 'ana@nope-xyz.com'})
        self.assertIn(UNDELIVERABLE_REASON, result)
        self.assertFalse(ContactSubmission.objects.exists())

    def test_lead_good_email_saved(self):
        with patch(URLOPEN, side_effect=fake_dns({('acme.com', 'MX'): MX_OK})[0]), \
                patch.object(notify, 'notify_client'):
            result = _chat_lead_handler(self._request(), self.shop)(
                {'name': 'Ana', 'email': 'ana@acme.com'})
        self.assertIn('Lead guardado', result)
        self.assertEqual(ContactSubmission.objects.get().email, 'ana@acme.com')

    def test_booking_disposable_returns_reason(self):
        result = _booking_handler(self._request(), self.shop)(
            {'name': 'Ana', 'email': 'ana@yopmail.com', 'start': self._slot()})
        self.assertIn('No se reservó la cita', result)
        self.assertIn(DISPOSABLE_REASON, result)
        self.assertFalse(Booking.objects.exists())

    def test_booking_nxdomain_returns_reason(self):
        with patch(URLOPEN, side_effect=fake_dns({('nope-xyz.com', 'MX'): NXDOMAIN})[0]):
            result = _booking_handler(self._request(), self.shop)(
                {'name': 'Ana', 'email': 'ana@nope-xyz.com', 'start': self._slot()})
        self.assertIn(UNDELIVERABLE_REASON, result)
        self.assertFalse(Booking.objects.exists())

    def test_booking_emails_have_no_confirm_link(self):
        with patch(URLOPEN, side_effect=fake_dns({('acme.com', 'MX'): MX_OK})[0]), \
                patch.object(notify, 'notify_client'):
            result = _booking_handler(self._request(), self.shop)(
                {'name': 'Ana', 'email': 'ana@acme.com', 'start': self._slot()})
        self.assertIn('Reservado', result)
        self.assertEqual(len(mail.outbox), 2)
        text = all_mail_text()
        self.assertNotIn('/confirmar/', text)
        self.assertNotIn('Confirmar mi correo', text)
        self.assertNotIn('sin confirmar', text)

    def test_human_handler_drops_bad_email_but_escalates(self):
        with patch.object(notify, 'notify_client'):
            result = _human_handler(self._request(), self.shop)(
                {'name': 'Luis', 'email': 'luis@mailinator.com', 'phone': '7871234567',
                 'reason': 'Quiere hablar con alguien'})
        self.assertIn('Escalado', result)
        lead = ContactSubmission.objects.get()
        self.assertEqual(lead.email, '')
        self.assertEqual(lead.phone, '+17871234567')


@override_settings(CONTACT_NOTIFY_EMAIL='leads@example.com', SITE_URL='https://dominiopr.com')
class LeadDoubleOptInTests(TestCase):
    PAYLOAD = {'name': 'Ana Rivera', 'email': 'ana@example.com', 'company': 'Acme PR',
               'service': 'consulting', 'budget': '', 'message': 'Hola', 'website': ''}

    def setUp(self):
        cache.clear()
        caches['emaildns'].clear()

    def _submit(self):
        self.client.post(reverse('index'), self.PAYLOAD)
        return ContactSubmission.objects.get()

    def _confirm_path(self):
        visitor = next(m for m in mail.outbox if m.to == ['ana@example.com'])
        match = re.search(r'https://dominiopr\.com(/confirmar/[^/\s"]+/)', visitor.body)
        self.assertIsNotNone(match, visitor.body)
        return match.group(1)

    def test_visitor_email_asks_to_confirm_and_team_sees_marker(self):
        lead = self._submit()
        self.assertIsNone(lead.email_verified_at)
        visitor = next(m for m in mail.outbox if m.to == ['ana@example.com'])
        team = next(m for m in mail.outbox if m.to == ['leads@example.com'])
        self.assertIn('Confirma tu correo', visitor.subject)
        self.assertIn('Gracias', visitor.body)  # the thank-you content stays
        html = visitor.alternatives[0][0]
        self.assertIn('Confirmar mi correo', html)
        self.assertIn(self._confirm_path(), html)
        self.assertIn('(correo sin confirmar)', team.subject)
        self.assertIn('(correo sin confirmar)', team.body)
        self.assertIn('(correo sin confirmar)', team.alternatives[0][0])
        # The address is never put in the link (only a keyed tag of it).
        self.assertNotIn('ana', signing.b64_decode(
            self._confirm_path().split('/')[2].split(':')[0].encode()).decode('utf-8', 'ignore'))

    def test_phone_only_lead_gets_no_marker(self):
        payload = dict(self.PAYLOAD, email='', phone='7871234567')
        self.client.post(reverse('index'), payload)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn('sin confirmar', all_mail_text())

    def test_get_does_not_confirm(self):
        lead = self._submit()
        resp = self.client.get(self._confirm_path())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Confirmar mi correo')
        self.assertContains(resp, '<form method="post"')
        self.assertEqual(resp['Cache-Control'], 'no-store')
        lead.refresh_from_db()
        self.assertIsNone(lead.email_verified_at)

    def test_post_confirms_and_is_idempotent(self):
        lead = self._submit()
        path = self._confirm_path()
        resp = self.client.post(path)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Correo confirmado')
        lead.refresh_from_db()
        first = lead.email_verified_at
        self.assertIsNotNone(first)
        resp = self.client.post(path)
        self.assertEqual(resp.status_code, 200)
        lead.refresh_from_db()
        self.assertEqual(lead.email_verified_at, first)
        # And a GET afterwards says it's done, without a button.
        resp = self.client.get(path)
        self.assertContains(resp, 'Correo confirmado')
        self.assertNotContains(resp, '<form method="post"')

    def test_post_requires_csrf(self):
        self._submit()
        strict = HttpClient(enforce_csrf_checks=True)
        self.assertEqual(strict.post(self._confirm_path()).status_code, 403)
        self.assertIsNone(ContactSubmission.objects.get().email_verified_at)

    def test_expired_token_rejected(self):
        lead = ContactSubmission.objects.create(
            name='Ana', email='ana@example.com', service='consulting', message='x')
        eight_days_ago = time.time() - 8 * 86400
        with patch('django.core.signing.time.time', return_value=eight_days_ago):
            url = lead_confirm_url(lead)
        path = urlparse(url).path
        for method in (self.client.get, self.client.post):
            resp = method(path)
            self.assertEqual(resp.status_code, 400)
            self.assertContains(resp, 'Este enlace expiró', status_code=400)
        lead.refresh_from_db()
        self.assertIsNone(lead.email_verified_at)

    def test_tampered_token_rejected(self):
        lead = self._submit()
        path = self._confirm_path()
        token = path.split('/')[2]
        bad = path.replace(token, token[:-2] + ('AA' if not token.endswith('AA') else 'BB'))
        resp = self.client.post(bad)
        self.assertEqual(resp.status_code, 400)
        self.assertContains(resp, 'Enlace no válido', status_code=400)
        # A token minted for another purpose (different salt) is refused too.
        other = signing.dumps({'l': lead.pk, 'e': 'x'}, salt='something-else')
        self.assertEqual(self.client.post(f'/confirmar/{other}/').status_code, 400)
        lead.refresh_from_db()
        self.assertIsNone(lead.email_verified_at)

    def test_email_mismatch_rejected(self):
        lead = self._submit()
        path = self._confirm_path()
        ContactSubmission.objects.filter(pk=lead.pk).update(email='otra@example.com')
        resp = self.client.post(path)
        self.assertEqual(resp.status_code, 400)
        lead.refresh_from_db()
        self.assertIsNone(lead.email_verified_at)

    def test_deleted_lead_rejected(self):
        lead = self._submit()
        path = self._confirm_path()
        lead.delete()
        self.assertEqual(self.client.post(path).status_code, 400)

    def test_clinic_visitor_email_has_link_but_no_chat_text(self):
        clinic = Client.objects.create(
            slug='dental', widget_token=TOKEN, name='Dental Sol', system_prompt='x',
            notify_email='citas@dental.com', clinic_mode=True)
        req = RequestFactory().post('/api/chat/')
        req.META['REMOTE_ADDR'] = '10.1.1.1'
        with patch.object(notify, 'notify_client'):
            _chat_lead_handler(req, clinic)({'name': 'Ana', 'email': 'ana@example.com',
                                             'summary': 'me duele la muela'})
        visitor = next(m for m in mail.outbox if m.to == ['ana@example.com'])
        self.assertIn('/confirmar/', visitor.body)
        self.assertNotIn('muela', visitor.body + visitor.alternatives[0][0])
        self.assertNotIn('[Modo clínica]', visitor.body)

    def test_dashboard_badges(self):
        ContactSubmission.objects.create(
            name='Confirmada', email='a@example.com', service='consulting', message='x',
            email_verified_at=timezone.now())
        ContactSubmission.objects.create(
            name='Pendiente', email='b@example.com', service='consulting', message='x')
        ContactSubmission.objects.create(
            name='SoloTel', phone='+17871234567', service='consulting', message='x')
        staff = get_user_model().objects.create_user('staff', password='pw-123456', is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Correo confirmado', count=1)
        self.assertContains(resp, 'Sin confirmar', count=1)  # phone-only lead: no badge


class DnsCacheIsolationTests(TestCase):
    """Attacker-chosen domains must never grow the default cache, whose cull
    would evict the daily spend counters."""

    def test_dns_verdicts_live_outside_the_default_cache(self):
        from django.test import override_settings as _ovr
        from unittest.mock import patch as _patch
        from . import emailcheck as ec
        cache.set('chat:global:20261008', 7, 3600)
        with _ovr(DOMINIO_EMAIL_DNS_CHECK=True),                 _patch.object(ec, '_lookup', return_value='ok', create=True):
            for i in range(20):
                ec.check_email(f'a@random-{i}.example')
        self.assertEqual(cache.get('chat:global:20261008'), 7)
        self.assertIsNone(cache.get('emaildns:random-0.example'))
