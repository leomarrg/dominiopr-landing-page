"""
Phase G -- WhatsApp through Meta's Cloud API: the webhook (verification,
signature, parsing, idempotency), tenant and contact identity, the 24 h /
monthly session rule, plan and clinic gates, keywords, media, caps, the turn
itself (channel, CHANNEL line, timeout, bookings), outbound text/templates
and the notice selector, the usage bucket and bill_overage, wa_simulate.
The model provider, the Graph API and Twilio are ALWAYS mocked.
"""
import datetime as dt
import json
import time
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import anthropic
import httpx
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import agent, entitlements, payments, views, whatsapp
from .management.commands.wa_simulate import build_payload
from .models import (
    AuditEvent, Booking, ChannelAccount, ChannelOptOut, ChatMessage, Client, Conversation,
    Location, Subscription, UsageEvent,
)
from .tests_core import fake_answer

PR = ZoneInfo('America/Puerto_Rico')
SECRET = 'meta-app-secret-for-tests'
VERIFY = 'verify-me-please'
PNID = '111222333444'
WA_ID = '17875551234'
SEND = 'landing.notify._send'   # the one Twilio seam
USAGE = {'input_tokens': 100, 'output_tokens': 50, 'cache_read_input_tokens': 0,
         'cache_creation_input_tokens': 0, 'model': 'claude-haiku-5-5',
         'api_calls': 1, 'effort': 'low'}

WA_SETTINGS = dict(META_APP_SECRET=SECRET, META_WA_VERIFY_TOKEN=VERIFY,
                   META_WA_ACCESS_TOKEN='EAAG-token', ANTHROPIC_API_KEY='test-key',
                   DOMINIO_PHONE_HASH_KEY='phone-hash-key', CONTACT_NOTIFY_EMAIL='',
                   META_WA_TEMPLATE_REMINDER='', META_WA_TEMPLATE_STATUS='')


def make_client(slug='acme', plan='premium', profile='receptionist', **kw):
    """A tenant with a WhatsApp number. Premium sells the channel (500 WA
    conversations at step 1)."""
    client = Client.objects.create(
        slug=slug, name=kw.pop('name', 'Acme PR'), system_prompt='Vendemos widgets.',
        notify_email=kw.pop('notify_email', f'owner@{slug}.com'), agent_profile=profile,
        daily_message_cap=kw.pop('daily_message_cap', 300),
        enable_bookings=kw.pop('enable_bookings', True), **kw)
    if plan:
        Subscription.objects.create(client=client, plan=plan, status='active')
    ChannelAccount.objects.create(client=client, phone_number_id=kw.get('pnid', PNID),
                                  waba_id='9000', display_phone='+1 787-555-0100')
    return client


def event(text='hola', wa_id=WA_ID, pnid=PNID, **kw):
    return build_payload(pnid, wa_id, text=text, **kw)


class Graph:
    """Records every Graph API call the code would make; `fail` raises a
    GraphError for matching payloads."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def __call__(self, path, payload, retry=True):
        self.calls.append((path, payload))
        if self.fail and self.fail(payload):
            raise whatsapp.GraphError(401, 190, 'Error validating access token')
        return {'messages': [{'id': f'wamid.OUT{len(self.calls)}'}]}

    @property
    def texts(self):
        return [p['text']['body'] for _, p in self.calls if p.get('type') == 'text']

    @property
    def reads(self):
        return [p for _, p in self.calls if p.get('status') == 'read']

    @property
    def templates(self):
        return [p for _, p in self.calls if p.get('type') == 'template']


@override_settings(**WA_SETTINGS)
class WebhookBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client_obj = make_client()
        self.graph = Graph()
        self.calls = []

    def post(self, payload=None, raw=None, secret=SECRET, reply='Claro, dime.', **headers):
        raw = raw if raw is not None else json.dumps(payload).encode()
        if secret is not None:
            headers.setdefault('HTTP_X_HUB_SIGNATURE_256', whatsapp.sign(raw, secret))
        with patch.object(whatsapp, '_graph_post', side_effect=self.graph), \
                patch.object(agent, 'answer', side_effect=fake_answer(reply, calls=self.calls)):
            return self.client.post(reverse('whatsapp_webhook'), data=raw,
                                    content_type='application/json', **headers)

    def conversations(self):
        return Conversation.objects.filter(client=self.client_obj, channel='whatsapp')


# ------------------------------------------------------------------
# Verification + signature
# ------------------------------------------------------------------

class VerificationTests(WebhookBase):
    def test_get_handshake_echoes_the_challenge(self):
        resp = self.client.get(reverse('whatsapp_webhook'), {
            'hub.mode': 'subscribe', 'hub.verify_token': VERIFY, 'hub.challenge': '1158201444'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b'1158201444')
        self.assertEqual(resp['Content-Type'], 'text/plain')

    def test_get_with_wrong_or_missing_token_is_403(self):
        resp = self.client.get(reverse('whatsapp_webhook'), {
            'hub.mode': 'subscribe', 'hub.verify_token': 'nope', 'hub.challenge': '1'})
        self.assertEqual(resp.status_code, 403)
        with override_settings(META_WA_VERIFY_TOKEN=''):
            resp = self.client.get(reverse('whatsapp_webhook'), {
                'hub.mode': 'subscribe', 'hub.verify_token': '', 'hub.challenge': '1'})
        self.assertEqual(resp.status_code, 403)

    def test_valid_signature_is_200_and_answers(self):
        resp = self.post(event('hola'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.graph.texts, ['Claro, dime.'])
        self.assertEqual(len(self.calls), 1)

    def test_invalid_signature_is_403_and_spends_nothing(self):
        resp = self.post(event('hola'), secret='another-secret')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.graph.calls, [])
        self.assertFalse(Conversation.objects.exists())
        resp = self.post(event('hola'), secret=None)     # no header at all
        self.assertEqual(resp.status_code, 403)

    def test_without_app_secret_is_503_and_warns_once(self):
        with override_settings(META_APP_SECRET=''), \
                self.assertLogs('landing.whatsapp', 'WARNING') as logs:
            resp = self.post(event('hola'), secret='whatever')
            self.assertEqual(self.post(event('dos'), secret='whatever').status_code, 503)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(self.calls, [])
        self.assertEqual(len(logs.records), 1)
        self.assertIn('META_APP_SECRET', logs.records[0].getMessage())

    def test_non_ascii_or_malformed_signatures_are_403_not_500(self):
        for header in ('sha256=é', 'sha256=' + 'g' * 64, 'sha256=' + 'a' * 63, 'md5=abc', 'é'):
            with self.subTest(header=header):
                resp = self.post(event('hola'), secret=None, HTTP_X_HUB_SIGNATURE_256=header)
                self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(mail.outbox, [])

    def test_uppercase_hex_signature_is_accepted(self):
        raw = json.dumps(event('hola')).encode()
        header = whatsapp.sign(raw).upper().replace('SHA256=', 'sha256=')
        resp = self.post(raw=raw, secret=None, HTTP_X_HUB_SIGNATURE_256=header)
        self.assertEqual(resp.status_code, 200)

    def test_non_ascii_verify_token_is_403_without_email(self):
        resp = self.client.get(reverse('whatsapp_webhook'), {
            'hub.mode': 'subscribe', 'hub.verify_token': 'é', 'hub.challenge': '1'})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(mail.outbox, [])

    def test_internal_failure_after_the_signature_is_still_200(self):
        raw = json.dumps(event('hola')).encode()
        with patch.object(whatsapp, '_dispatch', side_effect=RuntimeError('boom')), \
                self.assertLogs('landing.whatsapp', 'WARNING') as logs:
            resp = self.client.post(reverse('whatsapp_webhook'), data=raw,
                                    content_type='application/json',
                                    HTTP_X_HUB_SIGNATURE_256=whatsapp.sign(raw))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mail.outbox, [])
        self.assertIn('RuntimeError', logs.records[0].getMessage())
        self.assertNotIn('boom', logs.records[0].getMessage())

    def test_signature_covers_the_raw_body_with_escaped_accents(self):
        # Meta escapes non-ASCII; re-serializing would not match, the raw bytes do.
        raw = json.dumps(event('¿Cuánto cuesta el niño?'), ensure_ascii=True).encode()
        self.assertIn(b'\\u00bf', raw)
        resp = self.post(raw=raw)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.calls[0]['history'][-1]['content'], '¿Cuánto cuesta el niño?')

    def test_oversized_body_is_413(self):
        payload = event('x' * (260 * 1024))
        resp = self.post(payload)
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(self.calls, [])

    def test_statuses_are_acknowledged_without_effects(self):
        resp = self.post(event(status=True))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.graph.calls, [])
        self.assertEqual(self.calls, [])
        self.assertFalse(Conversation.objects.exists())

    def test_other_objects_and_bad_json_do_not_blow_up(self):
        resp = self.post({'object': 'page', 'entry': []})
        self.assertEqual(resp.status_code, 200)
        resp = self.post(raw=b'{not json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.calls, [])


# ------------------------------------------------------------------
# Tenant, idempotency, identity, session
# ------------------------------------------------------------------

class TenantTests(WebhookBase):
    def test_unknown_phone_number_id_is_200_without_sending(self):
        resp = self.post(event('hola', pnid='999'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.graph.calls, [])
        self.assertEqual(self.calls, [])
        self.assertFalse(Conversation.objects.exists())

    def test_inactive_client_or_account_is_ignored(self):
        Client.objects.filter(pk=self.client_obj.pk).update(is_active=False)
        self.assertEqual(self.post(event('hola')).status_code, 200)
        Client.objects.filter(pk=self.client_obj.pk).update(is_active=True)
        ChannelAccount.objects.update(is_active=False)
        self.assertEqual(self.post(event('hola')).status_code, 200)
        self.assertEqual(self.graph.calls, [])
        self.assertEqual(self.calls, [])

    def test_duplicate_wamid_is_ignored(self):
        payload = event('hola', wamid='wamid.FIXED1')
        self.post(payload)
        self.post(payload)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.graph.texts, ['Claro, dime.'])
        self.assertEqual(self.conversations().get().chat_messages.count(), 2)

    def test_stale_redelivery_is_logged_not_answered(self):
        self.post(event('hola', timestamp=time.time() - 90000))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.graph.calls, [])

    def test_contact_hash_is_stable_keyed_and_not_the_number(self):
        h1, h2 = whatsapp.contact_hash(WA_ID), whatsapp.contact_hash(WA_ID)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 32)
        self.assertNotIn(WA_ID[-7:], h1)
        self.assertNotEqual(h1, whatsapp.contact_hash('17875559999'))
        with override_settings(DOMINIO_PHONE_HASH_KEY='other-key'):
            self.assertNotEqual(h1, whatsapp.contact_hash(WA_ID))

    def test_conversation_keyed_by_hash_and_day_counts_in_the_wa_bucket(self):
        self.post(event('hola'))
        conv = self.conversations().get()
        today = timezone.localtime(timezone.now(), PR).strftime('%Y%m%d')
        self.assertEqual(conv.external_id, f'wa:{whatsapp.contact_hash(WA_ID)}:{today}')
        self.assertEqual((conv.channel, conv.widget_session), ('whatsapp', ''))
        self.assertNotIn(WA_ID, conv.external_id)
        self.assertTrue(conv.config_version)
        self.assertEqual(conv.chat_messages.count(), 2)
        usage = entitlements.month_usage(self.client_obj)
        self.assertEqual((usage['whatsapp'], usage['web']), (1, 0))
        self.assertEqual(UsageEvent.objects.get().kind, 'whatsapp')

    def test_profile_name_never_reaches_the_prompt(self):
        payload = event('hola')
        payload['entry'][0]['changes'][0]['value']['contacts'][0]['profile']['name'] = 'Zuleika'
        self.post(payload)
        sent = json.dumps(self.calls[0], default=str)
        self.assertNotIn('Zuleika', sent)


class SessionTests(WebhookBase):
    def setUp(self):
        super().setUp()
        self.hash = whatsapp.contact_hash(WA_ID)
        self.now = dt.datetime(2026, 10, 15, 15, 0, tzinfo=PR)

    def _conv(self, started, last):
        conv = Conversation.objects.create(
            client=self.client_obj, channel='whatsapp',
            external_id=f'wa:{self.hash}:{started.astimezone(PR):%Y%m%d}')
        Conversation.objects.filter(pk=conv.pk).update(started_at=started, last_message_at=last)
        return conv

    def test_reused_inside_24h(self):
        conv = self._conv(self.now - dt.timedelta(hours=30), self.now - dt.timedelta(hours=23))
        self.assertEqual(whatsapp.resolve_conversation(self.client_obj, self.hash, self.now), conv)
        self.assertEqual(self.conversations().count(), 1)

    def test_new_after_24h_of_silence(self):
        old = self._conv(self.now - dt.timedelta(hours=30), self.now - dt.timedelta(hours=25))
        got = whatsapp.resolve_conversation(self.client_obj, self.hash, self.now)
        self.assertNotEqual(got, old)
        self.assertEqual(got.external_id, f'wa:{self.hash}:20261015')
        self.assertEqual(self.conversations().count(), 2)

    def test_new_in_a_new_month_even_if_recent(self):
        now = dt.datetime(2026, 11, 1, 0, 10, tzinfo=PR)
        old = self._conv(dt.datetime(2026, 10, 31, 23, 0, tzinfo=PR),
                         dt.datetime(2026, 10, 31, 23, 50, tzinfo=PR))
        got = whatsapp.resolve_conversation(self.client_obj, self.hash, now)
        self.assertNotEqual(got, old)
        self.assertEqual(got.external_id, f'wa:{self.hash}:20261101')

    def test_two_messages_same_day_share_one_conversation(self):
        self.post(event('hola'))
        self.post(event('y los precios?'))
        self.assertEqual(self.conversations().count(), 1)
        self.assertEqual(self.conversations().get().chat_messages.count(), 4)
        self.assertEqual(self.calls[1]['history'][0]['content'], 'hola')


# ------------------------------------------------------------------
# Gates: plan, clinic, model
# ------------------------------------------------------------------

class GateTests(WebhookBase):
    def test_plan_without_whatsapp_gets_no_reply_and_no_model(self):
        Subscription.objects.filter(client=self.client_obj).update(plan='medium')
        resp = self.post(event('hola'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.graph.calls, [])
        self.assertEqual(self.calls, [])
        self.assertFalse(Conversation.objects.exists())

    def test_override_can_sell_the_channel(self):
        Subscription.objects.filter(client=self.client_obj).update(plan='medium')
        Client.objects.filter(pk=self.client_obj.pk).update(
            entitlement_overrides={'channels': ['web', 'whatsapp'], 'wa_conversations': 100})
        self.post(event('hola'))
        self.assertEqual(self.graph.texts, ['Claro, dime.'])

    def test_clinic_mode_gets_a_fixed_notice_once_and_no_conversation(self):
        Client.objects.filter(pk=self.client_obj.pk).update(clinic_mode=True)
        self.post(event('me duele el pecho'))
        self.post(event('hola?'))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.graph.texts, [whatsapp.TEXTS['es']['clinic']])
        self.assertFalse(Conversation.objects.exists())

    def test_channel_account_rejects_clinic_and_enforces_uniqueness(self):
        clinic = Client.objects.create(slug='clinica', name='Clínica', clinic_mode=True)
        account = ChannelAccount(client=clinic, phone_number_id='777')
        with self.assertRaises(ValidationError):
            account.full_clean()
        shop = Client.objects.create(slug='shop', name='Shop')
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChannelAccount.objects.create(client=shop, phone_number_id=PNID)   # taken by acme
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChannelAccount.objects.create(client=self.client_obj, phone_number_id='888')
        # An inactive second number is fine (history of a swapped number).
        ChannelAccount.objects.create(client=self.client_obj, phone_number_id='888',
                                      is_active=False)

    def test_model_error_sends_a_fixed_text_and_still_200(self):
        exc = anthropic.APIError('boom', httpx.Request('POST', 'https://x'), body=None)
        raw = json.dumps(event('hola')).encode()
        with patch.object(whatsapp, '_graph_post', side_effect=self.graph), \
                patch.object(agent, 'answer', side_effect=exc):
            resp = self.client.post(reverse('whatsapp_webhook'), data=raw,
                                    content_type='application/json',
                                    HTTP_X_HUB_SIGNATURE_256=whatsapp.sign(raw))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.graph.texts, [whatsapp.TEXTS['es']['error']])

    def test_timeout_and_not_configured_also_fall_back(self):
        timeout = anthropic.APITimeoutError(httpx.Request('POST', 'https://x'))
        for exc in (timeout, agent.AgentNotConfigured()):
            self.graph = Graph()
            raw = json.dumps(event('hola')).encode()
            with patch.object(whatsapp, '_graph_post', side_effect=self.graph), \
                    patch.object(agent, 'answer', side_effect=exc):
                resp = self.client.post(reverse('whatsapp_webhook'), data=raw,
                                        content_type='application/json',
                                        HTTP_X_HUB_SIGNATURE_256=whatsapp.sign(raw))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(self.graph.texts, [whatsapp.TEXTS['es']['error']])

    def test_english_tenant_gets_english_fixed_texts(self):
        Client.objects.filter(pk=self.client_obj.pk).update(primary_language='en')
        self.post(event(image=True))
        self.assertEqual(self.graph.texts, [whatsapp.TEXTS['en']['media']])


# ------------------------------------------------------------------
# Message types and keywords
# ------------------------------------------------------------------

class MessageTypeTests(WebhookBase):
    def test_image_alone_gets_the_fixed_notice_without_the_model(self):
        self.post(event(image=True))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.graph.texts, [whatsapp.TEXTS['es']['media']])
        self.assertFalse(Conversation.objects.exists())

    def test_image_caption_is_answered_as_text(self):
        self.post(event(image=True, caption='¿Tienen este modelo?'))
        self.assertEqual(self.calls[0]['history'][-1]['content'], '¿Tienen este modelo?')
        self.assertEqual(self.graph.texts, ['Claro, dime.'])

    def test_button_and_interactive_replies_use_their_title(self):
        payload = event('')
        msg = payload['entry'][0]['changes'][0]['value']['messages'][0]
        msg.pop('text')
        msg.update(type='interactive',
                   interactive={'type': 'button_reply',
                                'button_reply': {'id': 'b1', 'title': 'Ver precios'}})
        self.post(payload)
        self.assertEqual(self.calls[0]['history'][-1]['content'], 'Ver precios')
        payload = event('', wamid='wamid.BTN')
        msg = payload['entry'][0]['changes'][0]['value']['messages'][0]
        msg.pop('text')
        msg.update(type='button', button={'payload': 'x', 'text': 'Sí, confirmar'})
        self.post(payload)
        self.assertEqual(self.calls[1]['history'][-1]['content'], 'Sí, confirmar')

    def test_reactions_and_unknown_types_are_ignored(self):
        for mtype in ('reaction', 'unsupported', 'order'):
            payload = event('', wamid=f'wamid.{mtype}')
            msg = payload['entry'][0]['changes'][0]['value']['messages'][0]
            msg.pop('text')
            msg['type'] = mtype
            self.post(payload)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.graph.calls, [])

    def test_several_messages_in_one_batch_are_each_handled(self):
        payload = event('hola')
        value = payload['entry'][0]['changes'][0]['value']
        second = dict(value['messages'][0], id='wamid.SECOND', text={'body': 'precios?'})
        value['messages'].append(second)
        self.post(payload)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.graph.texts, ['Claro, dime.', 'Claro, dime.'])


class OptOutTests(WebhookBase):
    def test_stop_records_the_opt_out_and_silences_the_agent(self):
        self.post(event('STOP'))
        self.assertTrue(ChannelOptOut.objects.filter(
            client=self.client_obj, channel='whatsapp',
            contact_hash=whatsapp.contact_hash(WA_ID)).exists())
        self.assertEqual(self.graph.texts, [whatsapp.TEXTS['es']['stop']])
        self.post(event('hola de nuevo'))
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.graph.texts), 1)
        self.assertFalse(Conversation.objects.exists())

    def test_start_lifts_it(self):
        self.post(event('baja'))
        self.post(event('START'))
        self.assertFalse(ChannelOptOut.objects.exists())
        self.assertEqual(self.graph.texts[-1], whatsapp.TEXTS['es']['start'])
        self.post(event('hola'))
        self.assertEqual(len(self.calls), 1)

    def test_keywords_are_whole_message_only_and_cancelar_is_not_one(self):
        self.post(event('quiero cancelar mi cita'))
        self.post(event('cancelar'))      # a reply to a reminder: a request for the agent
        self.assertFalse(ChannelOptOut.objects.exists())
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('cancelar', whatsapp.STOP_WORDS)

    def _opted_out_booking(self, phone):
        ChannelOptOut.objects.get_or_create(client=self.client_obj, channel='whatsapp',
                                            contact_hash=whatsapp.contact_hash(WA_ID))
        return Booking.objects.create(
            client=self.client_obj, name='Ana', phone=phone, channel='whatsapp',
            wa_id=WA_ID, start=timezone.now() + dt.timedelta(days=1))

    def _remind(self, booking):
        with override_settings(META_WA_TEMPLATE_REMINDER='cita_recordatorio'), \
                patch.object(whatsapp, '_graph_post', side_effect=self.graph), \
                patch(SEND, return_value=True) as sms:
            ok = views.booking_notify_visitor(booking, 'reminder')
        return ok, sms

    def test_opt_out_blocks_template_and_sms_to_the_same_number(self):
        for phone in ('+17875551234', '787-555-1234', ''):
            with self.subTest(phone=phone):
                ok, sms = self._remind(self._opted_out_booking(phone))
                self.assertTrue(ok)
                self.assertEqual(self.graph.templates, [])
                sms.assert_not_called()
        self.assertTrue(whatsapp.same_number('(787) 555-1234', WA_ID))
        self.assertFalse(whatsapp.same_number('+17875559999', WA_ID))
        self.assertFalse(whatsapp.same_number('', WA_ID))

    def test_opt_out_still_allows_sms_to_a_different_phone(self):
        ok, sms = self._remind(self._opted_out_booking('+17875559999'))
        self.assertTrue(ok)
        self.assertEqual(self.graph.templates, [])
        self.assertEqual(sms.call_args[0][0], '+17875559999')


# ------------------------------------------------------------------
# Caps
# ------------------------------------------------------------------

class CapTests(WebhookBase):
    def _day(self):
        return timezone.localtime(timezone.now(), PR).strftime('%Y%m%d')

    def test_per_contact_cap_answers_once_then_silence(self):
        key = f'wa:msgs:acme:{whatsapp.contact_hash(WA_ID)}:{self._day()}'
        with override_settings(DOMINIO_WA_CONTACT_DAILY_CAP=3):
            cache.set(key, 3, 3600)
            self.post(event('uno'))
            self.post(event('dos'))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.graph.texts, [whatsapp.TEXTS['es']['cap']])
        # Another contact is unaffected.
        self.post(event('hola', wa_id='17875550000'))
        self.assertEqual(len(self.calls), 1)

    def test_stop_and_start_count_against_the_contact_cap(self):
        key = f'wa:msgs:acme:{whatsapp.contact_hash(WA_ID)}:{self._day()}'
        with override_settings(DOMINIO_WA_CONTACT_DAILY_CAP=3):
            cache.set(key, 3, 3600)
            self.post(event('STOP'))
            self.assertTrue(ChannelOptOut.objects.exists())
            self.post(event('START'))
            self.assertFalse(ChannelOptOut.objects.exists())
            self.post(event('stop'))
            self.assertTrue(ChannelOptOut.objects.exists())
            self.post(event('alta'))
            self.assertFalse(ChannelOptOut.objects.exists())
        self.assertEqual(self.calls, [])
        # Only the one cap notice; never the STOP/START confirmations.
        self.assertEqual(self.graph.texts, [whatsapp.TEXTS['es']['cap']])
        self.assertEqual(cache.get(key), 3)

    def test_tenant_cap_is_separate_from_the_widget(self):
        # The widget's counter at its ceiling does not touch WhatsApp...
        cache.set(f'chat:client:acme:{timezone.now():%Y%m%d}', 300, 3600)
        self.post(event('hola'))
        self.assertEqual(len(self.calls), 1)
        # ...and the WhatsApp counter at the ceiling answers 'busy' once.
        cache.set(f'chat:wa:acme:{self._day()}', 300, 3600)
        self.post(event('dos'))
        self.post(event('tres'))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.graph.texts, ['Claro, dime.', whatsapp.TEXTS['es']['busy']])

    def test_global_cap_is_shared_with_the_widget(self):
        key = f'chat:global:{timezone.now():%Y%m%d}'
        self.post(event('hola'))
        self.assertEqual(cache.get(key), 1)    # a WhatsApp turn spends the global budget
        with override_settings(DOMINIO_AGENT_DAILY_CAP=1):
            self.post(event('dos'))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.graph.texts[-1], whatsapp.TEXTS['es']['busy'])


# ------------------------------------------------------------------
# The turn: channel, CHANNEL line, timeout, tools, sending
# ------------------------------------------------------------------

class TurnTests(WebhookBase):
    def test_run_turn_gets_channel_line_and_timeout(self):
        self.post(event('hola'))
        kwargs = self.calls[0]['kwargs']
        self.assertEqual(kwargs, {'profile': 'receptionist', 'channel': 'whatsapp',
                                  'context_lines': ['CHANNEL: whatsapp'], 'timeout': 10})
        self.assertEqual(set(self.calls[0]['handlers']), {'take_message', 'create_booking'})

    def test_web_profile_tenant_gets_the_web_tools_on_whatsapp(self):
        Client.objects.filter(pk=self.client_obj.pk).update(agent_profile='web')
        self.post(event('hola'))
        self.assertEqual(set(self.calls[0]['handlers']),
                         {'capture_lead', 'request_human', 'create_booking'})
        self.assertEqual(self.calls[0]['kwargs']['channel'], 'whatsapp')
        self.assertNotIn('profile', self.calls[0]['kwargs'])

    def test_hours_line_comes_before_the_channel_line(self):
        from .models import AvailabilityRule
        for wd in range(7):
            AvailabilityRule.objects.create(client=self.client_obj, weekday=wd,
                                            open_time='09:00', close_time='17:00')
        self.post(event('hola'))
        lines = self.calls[0]['kwargs']['context_lines']
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('HOURS NOW'))
        self.assertEqual(lines[1], 'CHANNEL: whatsapp')

    def test_booking_from_whatsapp_is_stamped_and_capped_by_contact(self):
        slot = (timezone.localtime(timezone.now() + dt.timedelta(days=7))
                .replace(hour=10, minute=0, second=0, microsecond=0))

        def booking_answer(history, business_prompt=None, handlers=None, language='es', **kw):
            result = handlers['create_booking']({
                'name': 'Ana Pérez', 'phone': '787-555-7788',
                'start': slot.strftime('%Y-%m-%dT%H:%M'), 'service': 'corte'})
            return f'Listo. {result}', dict(USAGE)

        raw = json.dumps(event('quiero cita')).encode()
        with patch.object(whatsapp, '_graph_post', side_effect=self.graph), \
                patch.object(agent, 'answer', side_effect=booking_answer), \
                patch(SEND, return_value=True) as sms:
            resp = self.client.post(reverse('whatsapp_webhook'), data=raw,
                                    content_type='application/json',
                                    HTTP_X_HUB_SIGNATURE_256=whatsapp.sign(raw))
        self.assertEqual(resp.status_code, 200)
        booking = Booking.objects.get()
        self.assertEqual((booking.channel, booking.wa_id, booking.phone),
                         ('whatsapp', WA_ID, '+17875557788'))
        # No SMS: the reply in the chat is the confirmation. The owner's
        # email notification still goes out.
        sms.assert_not_called()
        self.assertEqual([m.to for m in mail.outbox], [['owner@acme.com']])
        self.assertIn('Reservado', self.graph.texts[0])
        self.assertIn('no se envía otro mensaje', self.graph.texts[0])
        self.assertEqual(cache.get(f'bookcap:wa:{whatsapp.contact_hash(WA_ID)}'), 1)
        self.assertIsNone(cache.get('bookcap:127.0.0.1'))

    def test_long_reply_is_split_in_pieces_under_the_limit(self):
        self.post(event('hola'), reply=('palabra ' * 700).strip())   # ~5,600 chars
        self.assertEqual(len(self.graph.texts), 2)
        self.assertTrue(all(len(t) <= 4096 for t in self.graph.texts))
        self.assertEqual(' '.join(self.graph.texts).split(), ['palabra'] * 700)
        self.assertEqual(whatsapp.split_text(''), [])
        self.assertEqual(whatsapp.split_text('a' * 10, limit=4), ['aaaa', 'aaaa', 'aa'])

    def test_mark_as_read_goes_first_and_is_best_effort(self):
        self.post(event('hola', wamid='wamid.READ1'))
        self.assertEqual(self.graph.calls[0][1],
                         {'messaging_product': 'whatsapp', 'status': 'read',
                          'message_id': 'wamid.READ1'})
        self.assertEqual(self.graph.calls[0][0], f'{PNID}/messages')
        self.graph = Graph(fail=lambda p: p.get('status') == 'read')
        self.post(event('otra'))
        self.assertEqual(self.graph.texts, ['Claro, dime.'])

    def test_graph_errors_keep_the_200_and_audit_once_per_hour(self):
        self.graph = Graph(fail=lambda p: True)
        self.assertEqual(self.post(event('hola')).status_code, 200)
        self.assertEqual(self.post(event('dos')).status_code, 200)
        self.assertEqual(len(self.calls), 2)
        events = AuditEvent.objects.filter(client=self.client_obj, action='whatsapp.send_failed')
        self.assertEqual(events.count(), 1)
        self.assertEqual((events.get().target, events.get().result[:3]), ('http:401', '190'))
        self.assertNotIn(WA_ID, events.get().result)

    def test_graph_post_retries_once_on_5xx_only(self):
        import urllib.error
        attempts = []

        def urlopen(req, timeout=None):
            attempts.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 503, 'down', {}, None)
        with patch.object(whatsapp, '_urlopen', side_effect=urlopen):
            with self.assertRaises(whatsapp.GraphError) as ctx:
                whatsapp._graph_post(f'{PNID}/messages', {'x': 1})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(attempts[0], f'https://graph.facebook.com/v23.0/{PNID}/messages')
        attempts.clear()

        def forbidden(req, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError(req.full_url, 400, 'bad', {}, None)
        with patch.object(whatsapp, '_urlopen', side_effect=forbidden):
            with self.assertRaises(whatsapp.GraphError):
                whatsapp._graph_post(f'{PNID}/messages', {'x': 1})
        self.assertEqual(len(attempts), 1)

    def test_graph_redirects_are_errors_never_followed(self):
        import urllib.error
        attempts = []

        def redirect(req, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError(req.full_url, 302, 'moved',
                                         {'Location': 'https://evil.example/x'}, None)
        with patch.object(whatsapp, '_urlopen', side_effect=redirect):
            with self.assertRaises(whatsapp.GraphError) as ctx:
                whatsapp._graph_post(f'{PNID}/messages', {'x': 1})
            with self.assertRaises(whatsapp.GraphError):
                whatsapp.graph_get(PNID)
        self.assertEqual(ctx.exception.status, 302)
        self.assertEqual(len(attempts), 2)     # one each, no retry
        # The real opener refuses to build a follow-up request at all.
        handler = whatsapp._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, 'moved', {},
                                                   'https://evil.example/x'))
        self.assertTrue(any(isinstance(h, whatsapp._NoRedirect)
                            for h in whatsapp._opener.handlers))

    def test_answer_passes_the_timeout_to_the_provider_client(self):
        captured = {}

        class FakeClient:
            def with_options(self, **kw):
                captured.update(kw)
                return self

            def __init__(self):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                return SimpleNamespace(
                    stop_reason='end_turn', content=[SimpleNamespace(type='text', text='ok')],
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                          cache_read_input_tokens=0,
                                          cache_creation_input_tokens=0))
        with patch.object(agent, '_provider_client', return_value=FakeClient()):
            agent.answer([{'role': 'user', 'content': 'hola'}], business_prompt='X',
                         handlers={}, timeout=10)
            self.assertEqual(captured, {'timeout': 10, 'max_retries': 1})
            captured.clear()
            agent.answer([{'role': 'user', 'content': 'hola'}], business_prompt='X', handlers={})
            self.assertEqual(captured, {})


# ------------------------------------------------------------------
# Outbound notices: templates, selector, reminders, dashboard
# ------------------------------------------------------------------

@override_settings(META_WA_TEMPLATE_REMINDER='cita_recordatorio',
                   META_WA_TEMPLATE_STATUS='cita_estado')
class OutboundTests(WebhookBase):
    def setUp(self):
        super().setUp()
        self.start = (timezone.now() + dt.timedelta(hours=24)).replace(microsecond=0)
        self.booking = Booking.objects.create(
            client=self.client_obj, name='Ana', phone='+17875551234', channel='whatsapp',
            wa_id=WA_ID, start=self.start)

    def _notify(self, kind, booking=None):
        with patch.object(whatsapp, '_graph_post', side_effect=self.graph), \
                patch(SEND, return_value=True) as sms:
            ok = views.booking_notify_visitor(booking or self.booking, kind)
        return ok, sms

    def test_send_template_payload(self):
        account = whatsapp.account_for(self.client_obj)
        with patch.object(whatsapp, '_graph_post', side_effect=self.graph):
            self.assertTrue(whatsapp.send_template(account, WA_ID, 'cita_estado',
                                                   ['Acme PR', '21/10', 'Acme PR', 'confirmada']))
        path, payload = self.graph.calls[0]
        self.assertEqual(path, f'{PNID}/messages')
        self.assertEqual(payload['to'], WA_ID)
        self.assertEqual(payload['type'], 'template')
        self.assertEqual(payload['template']['name'], 'cita_estado')
        self.assertEqual(payload['template']['language'], {'code': 'es'})
        self.assertEqual([p['text'] for p in payload['template']['components'][0]['parameters']],
                         ['Acme PR', '21/10', 'Acme PR', 'confirmada'])

    def test_reminder_uses_the_template_with_business_time_and_place(self):
        ok, sms = self._notify('reminder')
        self.assertTrue(ok)
        sms.assert_not_called()
        self.assertEqual(len(self.graph.templates), 1)
        tpl = self.graph.templates[0]['template']
        local = timezone.localtime(self.start)
        when = f'{local:%d/%m} a las {local:%I:%M %p}'.replace(' 0', ' ')
        self.assertEqual(tpl['name'], 'cita_recordatorio')
        self.assertEqual([p['text'] for p in tpl['components'][0]['parameters']],
                         ['Acme PR', when, 'Acme PR'])
        self.assertEqual(mail.outbox, [])

    def test_template_parameters_are_single_line(self):
        Client.objects.filter(pk=self.client_obj.pk).update(name='Acme\nPR\tInc')
        booking = Booking.objects.select_related('client').get(pk=self.booking.pk)
        self._notify('reminder', booking=booking)
        params = [p['text'] for p in self.graph.templates[0]['template']['components'][0]['parameters']]
        self.assertEqual(params[0], 'Acme PR Inc')
        self.assertEqual(params[2], 'Acme PR Inc')

    def test_location_fills_the_place_slot(self):
        loc = Location.objects.create(client=self.client_obj, name='Bayamón')
        Booking.objects.filter(pk=self.booking.pk).update(location=loc)
        self.booking.refresh_from_db()
        self._notify('confirmed')
        params = [p['text'] for p in self.graph.templates[0]['template']['components'][0]['parameters']]
        self.assertEqual(params[2:], ['Bayamón', 'confirmada'])

    def test_without_template_falls_back_to_sms(self):
        with override_settings(META_WA_TEMPLATE_REMINDER=''):
            ok, sms = self._notify('reminder')
        self.assertTrue(ok)
        self.assertEqual(self.graph.templates, [])
        self.assertEqual(sms.call_args[0][0], '+17875551234')
        self.assertIn('te recordamos tu cita', sms.call_args[0][1])

    def test_without_template_and_without_twilio_nothing_goes_and_it_is_false(self):
        with override_settings(META_WA_TEMPLATE_REMINDER=''), \
                patch.object(whatsapp, '_graph_post', side_effect=self.graph):
            ok = views.booking_notify_visitor(self.booking, 'reminder')
        self.assertFalse(ok)       # retried by the cron while in the window

    def test_email_wins_when_the_booking_has_one(self):
        Booking.objects.filter(pk=self.booking.pk).update(email='ana@example.com')
        self.booking.refresh_from_db()
        ok, sms = self._notify('cancelled')
        self.assertTrue(ok)
        self.assertEqual(self.graph.calls, [])
        sms.assert_not_called()
        self.assertEqual([m.to for m in mail.outbox], [['ana@example.com']])
        self.assertIn('cancelada', mail.outbox[0].subject)

    def test_confirmation_of_a_whatsapp_booking_sends_nothing_extra(self):
        ok, sms = self._notify('confirmation')
        self.assertTrue(ok)
        self.assertEqual(self.graph.calls, [])
        sms.assert_not_called()

    def test_web_booking_keeps_the_sms_path(self):
        web = Booking.objects.create(client=self.client_obj, name='Luis', phone='+17875550001',
                                     start=self.start + dt.timedelta(hours=1))
        ok, sms = self._notify('reminder', booking=web)
        self.assertTrue(ok)
        self.assertEqual(self.graph.calls, [])
        self.assertEqual(sms.call_args[0][0], '+17875550001')

    def test_failed_template_is_false_and_audited(self):
        self.graph = Graph(fail=lambda p: True)
        ok, sms = self._notify('reminder')
        self.assertFalse(ok)
        sms.assert_not_called()    # a failed template never double-sends by SMS
        self.assertEqual(AuditEvent.objects.filter(action='whatsapp.send_failed').count(), 1)

    def test_reminder_cron_uses_the_template_and_marks_the_booking(self):
        with patch.object(whatsapp, '_graph_post', side_effect=self.graph), \
                patch(SEND, return_value=True) as sms:
            call_command('send_booking_reminders', stdout=StringIO())
        self.booking.refresh_from_db()
        self.assertTrue(self.booking.reminder_sent)
        self.assertEqual(self.graph.templates[0]['template']['name'], 'cita_recordatorio')
        sms.assert_not_called()

    def test_dashboard_status_change_sends_the_status_template(self):
        from django.contrib.auth import get_user_model
        from .models import Membership
        user = get_user_model().objects.create_user('admin1', password='pw-12345678')
        Membership.objects.create(user=user, client=self.client_obj, role='org_admin')
        self.client.login(username='admin1', password='pw-12345678')
        with patch.object(whatsapp, '_graph_post', side_effect=self.graph), \
                patch(SEND, return_value=True) as sms:
            resp = self.client.post(reverse('booking_status', args=[self.booking.pk]),
                                    {'status': 'confirmed'})
        self.assertEqual(resp.status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'confirmed')
        tpl = self.graph.templates[0]['template']
        self.assertEqual(tpl['name'], 'cita_estado')
        self.assertEqual(tpl['components'][0]['parameters'][-1]['text'], 'confirmada')
        sms.assert_not_called()
        self.assertEqual(mail.outbox, [])


# ------------------------------------------------------------------
# Usage bucket, thresholds and bill_overage
# ------------------------------------------------------------------

class BillingTests(WebhookBase):
    def _wa_convs(self, n, when):
        h = whatsapp.contact_hash
        rows = [Conversation(client=self.client_obj, channel='whatsapp',
                             external_id=f'wa:{h(str(17870000000 + i))}:{when:%Y%m%d}')
                for i in range(n)]
        created = Conversation.objects.bulk_create(rows)
        Conversation.objects.filter(pk__in=[c.pk for c in created]).update(started_at=when)
        ChatMessage.objects.bulk_create([
            ChatMessage(conversation=c, role='assistant', content='Claro.', position=1)
            for c in created])

    def test_80_percent_of_the_wa_bucket_fires_on_a_real_turn(self):
        self._wa_convs(399, timezone.now())          # premium step 1: 500 included
        self.post(event('hola'))
        self.assertTrue(AuditEvent.objects.filter(client=self.client_obj,
                                                  action='allowance.wa.80').exists())
        self.assertFalse(AuditEvent.objects.filter(action='allowance.80').exists())
        self.assertTrue(any('WhatsApp' in m.subject for m in mail.outbox))

    @override_settings(CONTACT_NOTIFY_EMAIL='ops@dominiopr.com', STRIPE_SECRET_KEY='sk_test_x',
                       STRIPE_TAX_RATE_ID='')
    def test_bill_overage_posts_a_wa_block(self):
        Subscription.objects.filter(client=self.client_obj).update(
            method='stripe', stripe_customer_id='cus_wa', stripe_subscription_id='sub_wa',
            updated_at=dt.datetime(2026, 8, 1, tzinfo=PR))
        self._wa_convs(501, dt.datetime(2026, 9, 3, 12, 0, tzinfo=PR))

        def stripe(method, path, data=None, headers=None):
            return {'id': 'ii_wa'} if method == 'POST' else {'object': 'list', 'data': []}
        with patch.object(payments, '_request', side_effect=stripe) as req, \
                patch('django.utils.timezone.now',
                      return_value=dt.datetime(2026, 10, 1, 3, 10, tzinfo=PR)):
            call_command('bill_overage', '--month', '2026-09', stdout=StringIO())
        posts = [c for c in req.call_args_list if c.args[0] == 'POST'
                 and c.args[1] == '/v1/invoiceitems']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].args[2]['metadata[kind]'], 'wa_block')
        self.assertEqual(posts[0].args[2]['amount'], '14900')
        self.assertEqual(posts[0].args[2]['customer'], 'cus_wa')
        self.assertTrue(AuditEvent.objects.filter(action='billing.overage',
                                                  target='2026-09:wa_block').exists())


# ------------------------------------------------------------------
# wa_simulate
# ------------------------------------------------------------------

class SimulateTests(WebhookBase):
    def test_round_trip_prints_what_would_be_sent(self):
        out = StringIO()
        call_command('wa_simulate', '--client', 'acme', '--from', WA_ID, '--text', 'hola',
                     '--reply', 'Hola desde la simulación.', '--force', stdout=out)
        text = out.getvalue()
        self.assertIn('HTTP 200', text)
        self.assertIn('Graph API call(s) that would go out', text)
        self.assertIn('Hola desde la simulación.', text)
        self.assertIn('"status": "read"', text)
        self.assertIn('conversation history', text)
        self.assertEqual(self.conversations().count(), 1)
        self.assertEqual(self.conversations().get().chat_messages.count(), 2)
        self.assertFalse(UsageEvent.objects.exists())     # --reply: the model never ran

    def test_refuses_without_force_outside_debug(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError) as ctx:     # the test runner sets DEBUG=False
            call_command('wa_simulate', '--client', 'acme', '--from', WA_ID, '--text', 'hola',
                         '--reply', 'x', stdout=StringIO())
        self.assertIn('--force', str(ctx.exception))
        self.assertFalse(Conversation.objects.exists())
        with override_settings(DEBUG=True):
            call_command('wa_simulate', '--client', 'acme', '--from', WA_ID, '--text', 'hola',
                         '--reply', 'x', stdout=StringIO())
        self.assertEqual(self.conversations().count(), 1)

    def test_status_and_unknown_number_send_nothing(self):
        out = StringIO()
        call_command('wa_simulate', '--client', 'acme', '--from', WA_ID, '--status', '--force',
                     stdout=out)
        self.assertIn('Nothing would have been sent', out.getvalue())
        out = StringIO()
        call_command('wa_simulate', '--client', 'acme', '--from', WA_ID, '--text', 'hola',
                     '--phone-number-id', '000', '--reply', 'x', '--force', stdout=out)
        self.assertIn('Nothing would have been sent', out.getvalue())
        self.assertFalse(Conversation.objects.exists())

    def test_works_without_an_app_secret(self):
        out = StringIO()
        with override_settings(META_APP_SECRET=''):
            call_command('wa_simulate', '--client', 'acme', '--from', WA_ID, '--image',
                         '--force', stdout=out)
        self.assertIn('throwaway secret', out.getvalue())
        self.assertIn(whatsapp.TEXTS['es']['media'][:20], out.getvalue())
