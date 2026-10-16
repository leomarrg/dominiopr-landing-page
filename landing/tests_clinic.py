"""
Phase 1b — clinic mode (Client.clinic_mode): the agent minimizes health data
and the handlers/notifications drop the model-written free text. This is data
minimization, NOT HIPAA compliance. The model provider is ALWAYS mocked.
"""
import json
import os
import tempfile
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.forms.models import model_to_dict
from django.test import Client as HttpClient
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import agent, notify
from .forms import ClientForm
from .models import Booking, ChatMessage, Client, ContactSubmission, Conversation
from .views import (
    CLINIC_DASHBOARD_NOTE, CLINIC_ESCALATION_REASON,
    _booking_handler, _chat_lead_handler, _human_handler,
)

FAKE_USAGE = {'input_tokens': 100, 'output_tokens': 50}
SESSION = 'abcdef0123456789abcdef0123456789'
TOKEN = 'test-widget-token-abcdefghijklmnop'
HEALTH_TEXT = 'Tengo dolor en una muela y tomo ibuprofeno'


def _all_mail_text():
    """Subject, plain body and every HTML alternative of the outbox."""
    parts = []
    for m in mail.outbox:
        parts += [m.subject, m.body] + [alt[0] for alt in getattr(m, 'alternatives', [])]
    return '\n'.join(parts)


def _spy():
    """Stand-in for agent.answer that records the extra kwargs it got."""
    calls = []

    def _answer(history, business_prompt=None, handlers=None, language='es', **kwargs):
        calls.append(kwargs)
        return 'Hola', dict(FAKE_USAGE)
    return _answer, calls


class ClinicRulesTests(SimpleTestCase):
    def test_rules_only_when_clinic(self):
        self.assertNotIn(agent.CLINIC_RULES, agent.build_rules())
        self.assertNotIn(agent.CLINIC_RULES, agent.build_rules(bookings=True, human=True))
        self.assertIn(agent.CLINIC_RULES, agent.build_rules(clinic=True))
        self.assertIn(agent.CLINIC_RULES,
                      agent.build_rules(language='en', bookings=True, human=True, clinic=True))

    def test_rules_are_byte_stable(self):
        for kw in ({}, {'clinic': True}, {'bookings': True, 'human': True, 'clinic': True}):
            with self.subTest(**kw):
                self.assertEqual(agent.build_rules(**kw), agent.build_rules(**kw))
        # The non-clinic configuration is unchanged by the new parameter.
        self.assertEqual(agent.build_rules(), agent.AGENT_RULES)

    def test_rules_cover_the_minimization_contract(self):
        rules = agent.CLINIC_RULES
        for word in ('symptoms', 'diagnoses', 'medications', 'insurance', 'medical advice',
                     'capture_lead', 'request_human', 'create_booking'):
            with self.subTest(word=word):
                self.assertIn(word, rules)

    def test_clinic_kwargs(self):
        self.assertEqual(agent.clinic_kwargs(None), {})
        self.assertEqual(agent.clinic_kwargs(SimpleNamespace(clinic_mode=False)), {})
        self.assertEqual(agent.clinic_kwargs(SimpleNamespace(clinic_mode=True)), {'clinic': True})


def _resp(text='Hola', stop='end_turn', tool=None, tool_input=None):
    content = []
    if tool:
        content.append(SimpleNamespace(type='tool_use', id='tu_1', name=tool,
                                       input=tool_input or {}))
    if text:
        content.append(SimpleNamespace(type='text', text=text))
    usage = SimpleNamespace(input_tokens=10, output_tokens=5,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(stop_reason=stop, content=content, usage=usage, stop_details=None)


@override_settings(ANTHROPIC_API_KEY='test-key', DOMINIO_AGENT_MODEL='claude-haiku-5-5',
                   DOMINIO_AGENT_EFFORT='low')
class AnswerEngineClinicTests(SimpleTestCase):
    def _run(self, *responses, **kwargs):
        client = MagicMock()
        client.messages.create.side_effect = list(responses)
        with patch.object(agent, '_provider_client', return_value=client):
            reply = agent.answer([{'role': 'user', 'content': 'hola'}], **kwargs)
        return reply, client

    def test_clinic_rules_reach_the_system_prompt(self):
        _, client = self._run(_resp(), clinic=True)
        self.assertIn(agent.CLINIC_RULES,
                      client.messages.create.call_args.kwargs['system'][0]['text'])
        _, client = self._run(_resp())
        self.assertNotIn(agent.CLINIC_RULES,
                         client.messages.create.call_args.kwargs['system'][0]['text'])

    def test_tool_handlers_actually_run(self):
        """Regression: the tool loop must execute the handler and feed its
        result back (it was unreachable code after the usage-ledger change)."""
        seen = []

        def handler(data):
            seen.append(data)
            return 'Lead guardado'
        (reply, _), client = self._run(
            _resp(text='', stop='tool_use', tool='capture_lead', tool_input={'name': 'Ana'}),
            _resp(text='Listo'),
            handlers={'capture_lead': handler})
        self.assertEqual(reply, 'Listo')
        self.assertEqual(seen, [{'name': 'Ana'}])
        last_messages = client.messages.create.call_args.kwargs['messages']
        self.assertEqual(last_messages[-1]['content'][0]['type'], 'tool_result')
        self.assertEqual(last_messages[-1]['content'][0]['content'], 'Lead guardado')


@override_settings(ANTHROPIC_API_KEY='test-key', CONTACT_NOTIFY_EMAIL='ops@example.com')
class ChatApiClinicTests(TestCase):
    def setUp(self):
        cache.clear()
        self.clinic = Client.objects.create(
            slug='dental', widget_token=TOKEN, name='Dental Sol', system_prompt='Clínica dental.',
            notify_email='citas@dental.com', clinic_mode=True)
        self.shop = Client.objects.create(
            slug='acme', widget_token=TOKEN, name='Acme PR', system_prompt='Widgets.',
            notify_email='owner@acme.com')

    def _post(self, slug):
        return self.client.post(
            reverse('chat_api'),
            data=json.dumps({'client': slug, 'token': TOKEN, 'session': SESSION,
                             'messages': [{'role': 'user', 'content': 'hola'}]}),
            content_type='application/json')

    def test_clinic_client_passes_clinic_true(self):
        spy, calls = _spy()
        with patch.object(agent, 'answer', side_effect=spy):
            self.assertEqual(self._post('dental').status_code, 200)
        self.assertEqual(calls, [{'clinic': True}])

    def test_regular_client_passes_no_clinic_kwarg(self):
        spy, calls = _spy()
        with patch.object(agent, 'answer', side_effect=spy):
            self.assertEqual(self._post('acme').status_code, 200)
        self.assertEqual(calls, [{}])

    def test_config_version_changes_with_clinic_mode(self):
        before = self.shop.config_version()
        self.shop.clinic_mode = True
        self.assertNotEqual(before, self.shop.config_version())

    def test_knowledge_test_passes_clinic(self):
        # Phase I: the knowledge test console is staff-only.
        get_user_model().objects.create_user('ops@dominiopr.com', password='pw12345!', is_staff=True)
        http = HttpClient()
        http.login(username='ops@dominiopr.com', password='pw12345!')
        spy, calls = _spy()
        with patch.object(agent, 'answer', side_effect=spy):
            resp = http.post(reverse('knowledge_test'),
                             {'client': 'dental', 'question': '¿Hacen limpiezas?'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(calls, [{'clinic': True}])

    def test_agent_eval_passes_clinic_only_for_clinics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'eval.txt')
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write('hola\n')
            for slug, expected in (('dental', [{'clinic': True}]), ('acme', [{}])):
                spy, calls = _spy()
                with patch.object(agent, 'answer', side_effect=spy):
                    call_command('agent_eval', client=slug, file=path, stdout=StringIO())
                self.assertEqual(calls, expected, slug)


@override_settings(CONTACT_NOTIFY_EMAIL='ops@example.com', SITE_URL='https://dominiopr.com')
class ClinicHandlerTests(TestCase):
    def setUp(self):
        cache.clear()
        self.rf = RequestFactory()
        self.clinic = Client.objects.create(
            slug='dental', widget_token=TOKEN, name='Dental Sol', system_prompt='x',
            notify_email='citas@dental.com', clinic_mode=True, enable_bookings=True,
            notify_phone='+17875550000', notify_channel='sms')
        self.shop = Client.objects.create(
            slug='acme', widget_token=TOKEN, name='Acme PR', system_prompt='x',
            notify_email='owner@acme.com', enable_bookings=True)

    def _request(self):
        req = self.rf.post('/api/chat/')
        req.META['REMOTE_ADDR'] = '10.1.2.3'
        return req

    def _conversation(self, client):
        conv = Conversation.objects.create(client=client, widget_session=SESSION)
        ChatMessage.objects.create(conversation=conv, position=0, role='user', content=HEALTH_TEXT)
        return conv

    # --- capture_lead ---
    def test_clinic_lead_stores_generic_message_and_mails_no_summary(self):
        handle = _chat_lead_handler(self._request(), self.clinic)
        handle({'name': 'Ana', 'email': 'ana@example.com', 'summary': HEALTH_TEXT,
                'service': 'cita por dolor de muela'})
        lead = ContactSubmission.objects.get()
        self.assertTrue(lead.message.startswith('[Modo clínica]'))
        self.assertNotIn('dolor', lead.message)
        sent = _all_mail_text()
        self.assertEqual(len(mail.outbox), 2)  # team + visitor confirmation
        self.assertNotIn('ibuprofeno', sent)
        self.assertNotIn('[Modo clínica]', mail.outbox[1].body)  # visitor copy
        self.assertIn(CLINIC_DASHBOARD_NOTE, mail.outbox[0].body)

    def test_clinic_lead_never_stores_model_text(self):
        handle = _chat_lead_handler(self._request(), self.clinic)
        handle({'name': 'Ana', 'email': 'ana@example.com', 'summary': HEALTH_TEXT,
                'service': 'diabetes tipo 2'})
        message = ContactSubmission.objects.get().message
        self.assertNotIn('diabetes', message)
        self.assertNotIn('ibuprofeno', message)

    def test_regular_lead_keeps_summary_in_record_and_mail(self):
        handle = _chat_lead_handler(self._request(), self.shop)
        handle({'name': 'Ana', 'email': 'ana@example.com', 'summary': 'Quiere 40 widgets azules'})
        self.assertIn('40 widgets azules', ContactSubmission.objects.get().message)
        self.assertIn('40 widgets azules', mail.outbox[0].body)
        self.assertNotIn(CLINIC_DASHBOARD_NOTE, mail.outbox[0].body)

    # --- request_human ---
    def test_clinic_escalation_is_generic_everywhere(self):
        conv = self._conversation(self.clinic)
        with patch.object(notify, 'notify_client') as ping:
            handle = _human_handler(self._request(), self.clinic, conversation=conv)
            handle({'reason': 'Dolor fuerte de muela desde ayer', 'name': 'Luis',
                    'email': 'luis@example.com'})
        conv.refresh_from_db()
        self.assertEqual(conv.state, 'escalated')
        self.assertEqual(conv.escalation_reason, CLINIC_ESCALATION_REASON)
        self.assertNotIn('muela', ContactSubmission.objects.get().message)
        sent = _all_mail_text()
        self.assertNotIn('muela', sent)          # neither the reason...
        self.assertNotIn('ibuprofeno', sent)     # ...nor the transcript
        self.assertIn(CLINIC_DASHBOARD_NOTE, sent)
        body = ping.call_args.args[1]
        self.assertNotIn('muela', body)
        self.assertIn(CLINIC_DASHBOARD_NOTE, body)

    def test_regular_escalation_keeps_reason_and_transcript(self):
        conv = self._conversation(self.shop)
        with patch.object(notify, 'notify_client') as ping:
            handle = _human_handler(self._request(), self.shop, conversation=conv)
            handle({'reason': 'Quiere una cotización a la medida'})
        conv.refresh_from_db()
        self.assertIn('cotización', conv.escalation_reason)
        sent = _all_mail_text()
        self.assertIn('cotización', sent)
        self.assertIn('ibuprofeno', sent)  # the transcript goes out for non-clinics
        self.assertIn('cotización', ping.call_args.args[1])

    # --- create_booking ---
    def _slot(self):
        future = timezone.localtime(timezone.now() + timezone.timedelta(days=7))
        return future.replace(hour=10, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M')

    def test_clinic_booking_drops_notes(self):
        handle = _booking_handler(self._request(), self.clinic)
        self.assertIn('Reservado', handle({'name': 'Ana', 'email': 'ana@example.com',
                                           'start': self._slot(),
                                           'service': 'cita por dolor de pecho',
                                           'notes': HEALTH_TEXT}))
        booking = Booking.objects.get()
        self.assertEqual(booking.notes, '')
        self.assertEqual(booking.service, '')
        sent = _all_mail_text()
        self.assertNotIn('ibuprofeno', sent)
        self.assertNotIn('dolor de pecho', sent)
        self.assertIn(CLINIC_DASHBOARD_NOTE, mail.outbox[0].body)

    def test_regular_booking_keeps_notes(self):
        handle = _booking_handler(self._request(), self.shop)
        handle({'name': 'Ana', 'email': 'ana@example.com', 'start': self._slot(),
                'notes': 'Trae el modelo 3'})
        self.assertEqual(Booking.objects.get().notes, 'Trae el modelo 3')
        self.assertIn('Trae el modelo 3', mail.outbox[0].body)
        self.assertNotIn(CLINIC_DASHBOARD_NOTE, mail.outbox[0].body)


class ClinicWidgetTests(TestCase):
    def setUp(self):
        cache.clear()
        Client.objects.create(slug='dental', widget_token=TOKEN, name='Dental Sol',
                              system_prompt='x', clinic_mode=True)
        Client.objects.create(slug='acme', widget_token=TOKEN, name='Acme PR', system_prompt='x')

    def _js(self, key):
        return self.client.get(reverse('widget_js'), {'key': key}).content.decode()

    def test_notice_only_for_clinic_clients(self):
        js = self._js('dental')
        self.assertIn('dmn-notice', js)
        self.assertIn('Por tu privacidad, no compartas informaci', js)
        self.assertNotIn('dmn-notice', self._js('acme'))


class ClinicFormTests(TestCase):
    def test_client_form_saves_clinic_mode(self):
        obj = Client.objects.create(slug='dental', name='Dental Sol', system_prompt='x',
                                    notify_email='citas@dental.com')
        data = {k: v for k, v in model_to_dict(obj, fields=ClientForm._meta.fields).items()
                if v is not None}
        data['clinic_mode'] = 'on'
        form = ClientForm(data, instance=obj)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        obj.refresh_from_db()
        self.assertTrue(obj.clinic_mode)
        self.assertIn('HIPAA', str(form.fields['clinic_mode'].help_text))
