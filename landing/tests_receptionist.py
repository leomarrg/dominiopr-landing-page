"""
Phase D — receptionist core: the take_message tool, the receptionist wiring
in chat_api, the authoritative HOURS NOW line, phone-only bookings and the
StaffContact routing. The model provider and Twilio are ALWAYS mocked — no
test ever calls a real API.
"""
import datetime as dt
import json
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.forms.models import model_to_dict
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import agent, notify, views
from .forms import ClientForm
from .models import (
    AuditEvent, AvailabilityRule, Booking, Client, ContactSubmission, Conversation,
    Membership, StaffContact, route_contact,
)
from .turns import hours_now_line, run_turn
from .views import _booking_handler, _take_message_handler

TOKEN = 'test-widget-token-abcdefghijklmnop'
SESSION = 'abcdef0123456789abcdef0123456789'
PR = ZoneInfo('America/Puerto_Rico')
SEND = 'landing.notify._send'  # the one Twilio seam; patched so nothing leaves the box


def make_client(slug='salon', profile='receptionist', **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', 'Salón Luz'), system_prompt='Salón de belleza.',
        notify_email=kw.pop('notify_email', f'owner@{slug}.com'),
        widget_token=kw.pop('widget_token', TOKEN), agent_profile=profile, **kw)


def weekly_rules(client, saturday=True):
    for wd in range(5):
        AvailabilityRule.objects.create(client=client, weekday=wd,
                                        open_time='09:00', close_time='17:00')
    if saturday:
        AvailabilityRule.objects.create(client=client, weekday=5,
                                        open_time='10:00', close_time='14:00')


def message_input(**over):
    data = {'name': 'Ivette Cruz', 'callback_phone': '787-555-2210',
            'department': 'Facturación', 'urgency': 'urgent',
            'summary': 'Le cobraron doble en la última factura y quiere que la llamen.'}
    data.update(over)
    return data


# ---- take_message -------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='test-key', CONTACT_NOTIFY_EMAIL='ops@example.com',
                   SITE_URL='https://dominiopr.com')
class TakeMessageHandlerTests(TestCase):

    def setUp(self):
        cache.clear()
        self.rf = RequestFactory()
        self.client_obj = make_client(notify_phone='+17875550000', notify_channel='sms')
        self.conv = Conversation.objects.create(client=self.client_obj, widget_session=SESSION)

    def _request(self, ip='10.1.2.3'):
        req = self.rf.post('/api/chat/')
        req.META['REMOTE_ADDR'] = ip
        return req

    def _handle(self, **over):
        handle = _take_message_handler(self._request(), self.client_obj,
                                       conversation=self.conv, page_url='https://salon.com/')
        return handle(message_input(**over))

    def test_stores_links_notifies_and_audits(self):
        with patch(SEND, return_value=True) as send:
            result = self._handle()
        self.assertIn('Recado guardado para Facturación', result)
        lead = ContactSubmission.objects.get()
        self.assertEqual(lead.source, 'message')
        self.assertEqual(lead.phone, '+17875552210')
        self.assertEqual(lead.name, 'Ivette Cruz')
        self.assertEqual(lead.client, self.client_obj)
        self.assertEqual(lead.page_url, 'https://salon.com/')
        self.assertTrue(lead.message.startswith(
            '[Recado] Para: Facturación · Urgencia: urgent · Le cobraron doble'))
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.lead, lead)
        # Email to the client's default inbox (no StaffContact configured).
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ['owner@salon.com'])
        self.assertIn('[Recado URGENTE] Facturación', msg.subject)
        self.assertIn('+17875552210', msg.body)
        self.assertIn('Le cobraron doble', msg.body)
        # SMS to the client's default phone.
        send.assert_called_once()
        to, body, _from = send.call_args[0]
        self.assertEqual(to, '+17875550000')
        self.assertIn('recado urgente para Facturación', body)
        ev = AuditEvent.objects.get(action='lead.message')
        self.assertEqual((ev.client, ev.target, ev.result),
                         (self.client_obj, f'lead:{lead.pk}', 'Facturación|urgent'))

    def test_recipient_is_the_target_and_bad_email_is_dropped(self):
        with patch(SEND, return_value=False):
            result = self._handle(recipient='Dra. Santos', email='no-es-email',
                                  urgency='normal', preferred_time='en la tarde')
        self.assertIn('Dra. Santos', result)
        lead = ContactSubmission.objects.get()
        self.assertEqual(lead.email, '')
        self.assertIn('[Recado] Para: Dra. Santos · Urgencia: normal ·', lead.message)
        self.assertIn('Prefiere llamada: en la tarde', lead.message)

    def test_incomplete_phone_reasks_and_stores_nothing(self):
        with patch(SEND) as send:
            result = self._handle(callback_phone='787-55')
        self.assertIn('No se guardó el recado', result)
        self.assertIn('10 dígitos', result)
        self.assertEqual(ContactSubmission.objects.count(), 0)
        self.assertEqual(mail.outbox, [])
        send.assert_not_called()
        self.assertFalse(AuditEvent.objects.filter(action='lead.message').exists())
        self.conv.refresh_from_db()
        self.assertIsNone(self.conv.lead)

    def test_missing_phone_reasks_too(self):
        result = self._handle(callback_phone='')
        self.assertIn('No se guardó el recado', result)
        self.assertEqual(ContactSubmission.objects.count(), 0)

    def test_unknown_urgency_and_blank_department_fall_back(self):
        with patch(SEND, return_value=False):
            self._handle(department='', urgency='asap')
        lead = ContactSubmission.objects.get()
        self.assertIn('[Recado] Para: General · Urgencia: normal ·', lead.message)

    def test_routes_to_the_department_contact(self):
        StaffContact.objects.create(
            client=self.client_obj, name='Carla', department='Facturación',
            email='cobros@salon.com', phone='+17875559999', channel='whatsapp')
        StaffContact.objects.create(
            client=self.client_obj, name='Pepe', department='Ventas',
            email='ventas@salon.com', is_default=True)
        with patch(SEND, return_value=True) as send:
            self._handle()
        self.assertEqual(mail.outbox[0].to, ['cobros@salon.com'])
        # Routed contacts marked 'whatsapp' get an SMS too (Twilio is SMS only).
        self.assertEqual(send.call_args[0][0], '+17875559999')

    def test_routed_contact_without_phone_sends_no_text(self):
        """A routed contact with email only means no SMS: the owner's phone
        is never used as a silent fallback."""
        StaffContact.objects.create(
            client=self.client_obj, name='Carla', department='Facturación',
            email='cobros@salon.com', channel='email')
        with patch(SEND, return_value=True) as send:
            self._handle()
        self.assertEqual(mail.outbox[0].to, ['cobros@salon.com'])
        send.assert_not_called()

    def test_contact_without_email_falls_back_to_client_inbox(self):
        StaffContact.objects.create(
            client=self.client_obj, name='Carla', department='Facturación',
            phone='+17875559999', channel='sms')
        with patch(SEND, return_value=True) as send:
            self._handle()
        self.assertEqual(mail.outbox[0].to, ['owner@salon.com'])
        self.assertEqual(send.call_args[0][0], '+17875559999')

    def test_per_ip_cap(self):
        with patch(SEND, return_value=False):
            for i in range(5):
                self.assertIn('Recado guardado', self._handle(callback_phone=f'787555{i:04d}'))
            result = self._handle(callback_phone='7875559999')
        self.assertIn('Anotado', result)
        self.assertEqual(ContactSubmission.objects.count(), 5)

    @override_settings(DOMINIO_TENANT_ACTION_DAILY_CAP=1)
    def test_tenant_cap(self):
        with patch(SEND, return_value=False):
            self.assertIn('Recado guardado', self._handle())
            handle = _take_message_handler(self._request(ip='10.9.9.9'), self.client_obj)
            result = handle(message_input())
        self.assertIn('Anotado', result)
        self.assertEqual(ContactSubmission.objects.count(), 1)

    def test_clinic_mode_stores_and_forwards_no_free_text(self):
        self.client_obj.clinic_mode = True
        self.client_obj.save(update_fields=['clinic_mode'])
        with patch(SEND, return_value=True) as send:
            result = self._handle(summary='tiene dolor de pecho desde ayer',
                                  preferred_time='mañana temprano', department='Citas')
        self.assertIn('Recado guardado', result)
        lead = ContactSubmission.objects.get()
        self.assertTrue(lead.message.startswith('[Recado] Para: Citas · servicio:'))
        self.assertNotIn('dolor', lead.message)
        self.assertNotIn('mañana temprano', lead.message)
        self.assertNotIn('dolor', mail.outbox[0].body)
        self.assertIn(views.CLINIC_DASHBOARD_NOTE, mail.outbox[0].body)
        self.assertNotIn('dolor', send.call_args[0][1])

    def test_summary_is_single_line(self):
        with patch(SEND, return_value=False):
            self._handle(summary='línea uno\r\nBcc: x@y.com\nlínea dos')
        lead = ContactSubmission.objects.get()
        self.assertNotIn('\n', lead.message)
        self.assertIn('línea uno Bcc: x@y.com línea dos', lead.message)


# ---- chat_api wiring ------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='test-key', CONTACT_NOTIFY_EMAIL='ops@example.com')
class ReceptionistWiringTests(TestCase):

    def setUp(self):
        cache.clear()

    def _post(self, slug):
        body = {'client': slug, 'token': TOKEN, 'session': SESSION,
                'messages': [{'role': 'user', 'content': 'hola'}],
                'page': 'https://salon.com/'}
        return self.client.post(reverse('chat_api'), data=json.dumps(body),
                                content_type='application/json')

    def _spy_post(self, slug):
        calls = []

        def fake_run_turn(client, conversation, text, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(reply='ok', usage={}, conversation=conversation)

        with patch('landing.views.run_turn', side_effect=fake_run_turn):
            resp = self._post(slug)
        self.assertEqual(resp.status_code, 200, resp.content)
        return calls[0]

    def test_receptionist_gets_take_message_and_booking_only(self):
        make_client(enable_bookings=True)
        kwargs = self._spy_post('salon')
        self.assertEqual(set(kwargs['handlers']), {'take_message', 'create_booking'})
        self.assertEqual(kwargs['profile'], 'receptionist')
        self.assertEqual(kwargs['channel'], 'web')

    def test_receptionist_without_bookings(self):
        make_client(enable_bookings=False)
        kwargs = self._spy_post('salon')
        self.assertEqual(set(kwargs['handlers']), {'take_message'})
        self.assertEqual(kwargs['profile'], 'receptionist')

    def test_web_client_is_unchanged(self):
        make_client(slug='acme', profile='web', enable_bookings=True)
        kwargs = self._spy_post('acme')
        self.assertEqual(set(kwargs['handlers']),
                         {'capture_lead', 'request_human', 'create_booking'})
        self.assertNotIn('profile', kwargs)

    def test_take_message_tool_is_offered_only_with_its_handler(self):
        captured = []

        def create(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                stop_reason='end_turn',
                content=[SimpleNamespace(type='text', text='ok')],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                      cache_read_input_tokens=0,
                                      cache_creation_input_tokens=0))
        fake = SimpleNamespace(messages=SimpleNamespace(create=create))
        history = [{'role': 'user', 'content': 'hola'}]
        with patch.object(agent, '_provider_client', return_value=fake):
            agent.answer(history, business_prompt='x', handlers={'take_message': lambda d: 'ok'},
                         profile='receptionist')
            agent.answer(history, business_prompt='x', handlers={})
        self.assertEqual([t['name'] for t in captured[0]['tools']], ['take_message'])
        self.assertEqual(captured[0]['tools'][0], agent.TAKE_MESSAGE_TOOL)
        self.assertNotIn('tools', captured[1])

    def test_tool_schemas_follow_the_spec(self):
        self.assertEqual(agent.TAKE_MESSAGE_TOOL['input_schema']['required'],
                         ['name', 'callback_phone', 'department', 'urgency', 'summary'])
        self.assertEqual(agent.TAKE_MESSAGE_TOOL['input_schema']['properties']['urgency']['enum'],
                         ['normal', 'urgent'])
        self.assertEqual(agent.BOOKING_TOOL['input_schema']['required'], ['name', 'start'])
        for prop in ('phone', 'location', 'email'):
            self.assertIn(prop, agent.BOOKING_TOOL['input_schema']['properties'])
        self.assertIn('a valid email OR a 10-digit phone', agent.BOOKING_TOOL['description'])

    def test_config_version_changes_with_the_profile(self):
        c = make_client(profile='web')
        web = c.config_version()
        c.agent_profile = 'receptionist'
        self.assertNotEqual(c.config_version(), web)


# ---- HOURS NOW ----------------------------------------------------------------

class HoursNowLineTests(TestCase):
    SUMMARY = 'hours: Mon-Fri 09:00-17:00; Sat 10:00-14:00; Sun closed (America/Puerto_Rico)'

    def setUp(self):
        self.client_obj = make_client()
        weekly_rules(self.client_obj)

    def _at(self, day, hour, minute=0, tz=PR):
        # 2026-10-14 is a Wednesday; the other fixed days follow from it.
        return dt.datetime(2026, 10, day, hour, minute, tzinfo=tz)

    def test_open_now(self):
        line = hours_now_line(self.client_obj, now=self._at(14, 10))  # Wed 10:00
        self.assertEqual(line, f'HOURS NOW (authoritative): OPEN NOW, closes today 17:00 | {self.SUMMARY}')

    def test_boundaries_are_half_open(self):
        self.assertIn('OPEN NOW', hours_now_line(self.client_obj, now=self._at(14, 9)))
        self.assertIn('CLOSED NOW, opens tomorrow 09:00',
                      hours_now_line(self.client_obj, now=self._at(14, 17)))

    def test_closed_opens_later_today(self):
        line = hours_now_line(self.client_obj, now=self._at(14, 8, 30))
        self.assertIn('CLOSED NOW, opens today 09:00 |', line)

    def test_closed_opens_tomorrow(self):
        line = hours_now_line(self.client_obj, now=self._at(16, 18))  # Fri 18:00 -> Sat
        self.assertIn('CLOSED NOW, opens tomorrow 10:00 |', line)
        line = hours_now_line(self.client_obj, now=self._at(18, 12))  # Sun -> Mon
        self.assertIn('CLOSED NOW, opens tomorrow 09:00 |', line)

    def test_closed_opens_on_a_weekday(self):
        line = hours_now_line(self.client_obj, now=self._at(17, 15))  # Sat 15:00 -> Mon
        self.assertEqual(line, f'HOURS NOW (authoritative): CLOSED NOW, opens Monday 09:00 | {self.SUMMARY}')

    def test_computed_in_puerto_rico_time(self):
        # 14:00Z on a Wednesday is 10:00 in Puerto Rico (UTC-4): open.
        line = hours_now_line(self.client_obj, now=self._at(14, 14, tz=dt.timezone.utc))
        self.assertIn('OPEN NOW', line)
        # 23:30Z Sunday is 19:30 Sunday in PR: closed, opens tomorrow.
        line = hours_now_line(self.client_obj, now=self._at(18, 23, 30, tz=dt.timezone.utc))
        self.assertIn('opens tomorrow 09:00', line)

    def test_single_rule_day_wraps_the_week(self):
        other = make_client(slug='solo')
        AvailabilityRule.objects.create(client=other, weekday=2, open_time='09:00', close_time='12:00')
        line = hours_now_line(other, now=self._at(14, 13))  # Wed after close -> next Wed
        self.assertIn('CLOSED NOW, opens Wednesday 09:00 | hours: Mon-Tue closed; Wed 09:00-12:00; Thu-Sun closed', line)

    def test_absent_without_rules(self):
        self.assertEqual(hours_now_line(make_client(slug='norules')), '')
        self.assertEqual(hours_now_line(None), '')

    def test_defaults_to_now(self):
        line = hours_now_line(self.client_obj)
        self.assertTrue(line.startswith('HOURS NOW (authoritative): '))
        self.assertIn(self.SUMMARY, line)


@override_settings(ANTHROPIC_API_KEY='test-key')
class HoursLineInPromptTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client_obj = make_client()
        weekly_rules(self.client_obj)
        self.conv = Conversation.objects.create(client=self.client_obj, widget_session=SESSION)

    def _spy_answer(self, calls):
        def _answer(history, business_prompt=None, handlers=None, language='es', **kwargs):
            calls.append(kwargs)
            return 'ok', {'input_tokens': 1, 'output_tokens': 1}
        return _answer

    def test_run_turn_passes_the_line_only_for_the_receptionist(self):
        calls = []
        with patch.object(agent, 'answer', side_effect=self._spy_answer(calls)):
            run_turn(self.client_obj, self.conv, 'hola', channel='web', handlers={},
                     profile='receptionist')
            run_turn(self.client_obj, self.conv, 'hola', channel='web', handlers={})
            run_turn(self.client_obj, self.conv, 'hola', channel='web', handlers={},
                     profile='internal')
        self.assertEqual(calls[0]['profile'], 'receptionist')
        self.assertEqual(len(calls[0]['context_lines']), 1)
        self.assertTrue(calls[0]['context_lines'][0].startswith('HOURS NOW (authoritative): '))
        self.assertNotIn('context_lines', calls[1])
        self.assertNotIn('context_lines', calls[2])

    def test_run_turn_omits_the_line_without_rules(self):
        bare = make_client(slug='norules')
        conv = Conversation.objects.create(client=bare, widget_session=SESSION)
        calls = []
        with patch.object(agent, 'answer', side_effect=self._spy_answer(calls)):
            run_turn(bare, conv, 'hola', channel='web', handlers={}, profile='receptionist')
        self.assertEqual(calls[0], {'profile': 'receptionist'})

    def test_line_lives_in_the_uncached_block_only(self):
        captured = []

        def create(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                stop_reason='end_turn',
                content=[SimpleNamespace(type='text', text='ok')],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                      cache_read_input_tokens=0,
                                      cache_creation_input_tokens=0))
        fake = SimpleNamespace(messages=SimpleNamespace(create=create))
        hours = hours_now_line(self.client_obj)
        with patch.object(agent, '_provider_client', return_value=fake):
            agent.answer([{'role': 'user', 'content': 'hola'}], business_prompt='Negocio X.',
                         handlers={}, profile='receptionist', context_lines=[hours])
            agent.answer([{'role': 'user', 'content': 'hola'}], business_prompt='Negocio X.',
                         handlers={}, profile='receptionist')
        system = captured[0]['system']
        self.assertEqual(len(system), 2)
        cached, uncached = system
        self.assertEqual(cached['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(cached['text'],
                         agent.build_rules(profile='receptionist') + agent.BUSINESS_HEADER + 'Negocio X.')
        # The rules mention the line by name; the line itself is never cached.
        self.assertNotIn('HOURS NOW (authoritative)', cached['text'])
        self.assertNotIn('cache_control', uncached)
        self.assertIn('CURRENT DATE: today is', uncached['text'])
        self.assertTrue(uncached['text'].endswith('\n\n' + hours))
        # Without context lines (and without bookings) there is no second block.
        self.assertEqual(len(captured[1]['system']), 1)
        self.assertEqual(captured[1]['system'][0]['text'], cached['text'])


# ---- phone-only bookings --------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='test-key', CONTACT_NOTIFY_EMAIL='ops@example.com')
class PhoneOnlyBookingTests(TestCase):

    def setUp(self):
        cache.clear()
        self.rf = RequestFactory()
        self.client_obj = make_client(enable_bookings=True)
        future = timezone.localtime(timezone.now() + dt.timedelta(days=7))
        self.slot = future.replace(hour=10, minute=0, second=0, microsecond=0)

    def _handle(self):
        req = self.rf.post('/api/chat/')
        req.META['REMOTE_ADDR'] = '10.1.2.3'
        return _booking_handler(req, self.client_obj)

    def test_phone_only_booking_is_stored_and_confirmed_by_sms(self):
        with patch(SEND, return_value=True) as send:
            result = self._handle()({'name': 'Ana Pérez', 'phone': '787-555-7788',
                                     'start': self.slot.strftime('%Y-%m-%dT%H:%M'),
                                     'service': 'corte'})
        self.assertIn('Reservado', result)
        self.assertIn('por SMS', result)
        booking = Booking.objects.get()
        self.assertEqual((booking.email, booking.phone), ('', '+17875557788'))
        # The owner is notified by email; the visitor gets no email, only the SMS.
        subjects = [m.subject for m in mail.outbox]
        self.assertTrue(any(s.startswith('Nueva cita para') for s in subjects))
        self.assertFalse(any(s.startswith('Recibimos tu cita') for s in subjects))
        self.assertEqual(mail.outbox[0].reply_to, [])
        to, body, _from = send.call_args[0]
        self.assertEqual(to, '+17875557788')
        self.assertIn('recibimos tu cita', body)
        self.assertIn('Salón Luz', body)

    def test_email_booking_still_confirms_by_email(self):
        with patch(SEND) as send:
            result = self._handle()({'name': 'Ana', 'email': 'ana@example.com',
                                     'start': self.slot.strftime('%Y-%m-%dT%H:%M')})
        self.assertIn('por email', result)
        self.assertTrue(any(m.subject.startswith('Recibimos tu cita') for m in mail.outbox))
        send.assert_not_called()  # the owner has no notify_phone

    def test_invalid_phone_without_email_is_refused(self):
        with self.assertRaises(ValueError):
            self._handle()({'name': 'Ana', 'phone': '787-55',
                            'start': self.slot.strftime('%Y-%m-%dT%H:%M')})
        with self.assertRaises(ValueError):
            self._handle()({'name': 'Ana', 'start': self.slot.strftime('%Y-%m-%dT%H:%M')})
        self.assertEqual(Booking.objects.count(), 0)

    def test_bad_phone_next_to_a_valid_email_is_dropped(self):
        with patch(SEND):
            self._handle()({'name': 'Ana', 'email': 'ana@example.com', 'phone': '12',
                            'start': self.slot.strftime('%Y-%m-%dT%H:%M')})
        self.assertEqual(Booking.objects.get().phone, '')

    def test_reminder_goes_by_sms_for_phone_only_bookings(self):
        start = timezone.now() + dt.timedelta(hours=24)
        by_phone = Booking.objects.create(client=self.client_obj, name='Ana', email='',
                                          phone='+17875557788', start=start)
        by_email = Booking.objects.create(client=self.client_obj, name='Luis',
                                          email='luis@example.com',
                                          start=start + dt.timedelta(minutes=30))
        with patch(SEND, return_value=True) as send:
            call_command('send_booking_reminders')
        by_phone.refresh_from_db()
        by_email.refresh_from_db()
        self.assertTrue(by_phone.reminder_sent)
        self.assertTrue(by_email.reminder_sent)
        self.assertEqual(send.call_args[0][0], '+17875557788')
        self.assertIn('te recordamos tu cita', send.call_args[0][1])
        self.assertEqual([m.to for m in mail.outbox], [['luis@example.com']])

    def test_failed_sms_reminder_is_retried_next_run(self):
        booking = Booking.objects.create(client=self.client_obj, name='Ana', email='',
                                         phone='+17875557788',
                                         start=timezone.now() + dt.timedelta(hours=24))
        with patch(SEND, return_value=False):
            call_command('send_booking_reminders')
        booking.refresh_from_db()
        self.assertFalse(booking.reminder_sent)


# ---- StaffContact routing + UI --------------------------------------------------

class RouteContactTests(TestCase):

    def setUp(self):
        self.client_obj = make_client()
        self.santos = StaffContact.objects.create(
            client=self.client_obj, name='Dra. Santos', email='santos@salon.com')
        self.ventas = StaffContact.objects.create(
            client=self.client_obj, name='Pepe', department='Ventas', email='ventas@salon.com')
        self.default = StaffContact.objects.create(
            client=self.client_obj, name='Recepción', email='front@salon.com', is_default=True)

    def test_precedence_person_department_default(self):
        self.assertEqual(route_contact(self.client_obj, 'Ventas', 'la dra. santos'), self.santos)
        self.assertEqual(route_contact(self.client_obj, 'ventas'), self.ventas)
        self.assertEqual(route_contact(self.client_obj, 'Facturación'), self.default)
        self.assertEqual(route_contact(self.client_obj, 'Ventas', 'alguien que no existe'), self.ventas)
        self.assertEqual(route_contact(self.client_obj), self.default)

    def test_none_without_match_or_contacts(self):
        self.default.delete()
        self.assertIsNone(route_contact(self.client_obj, 'Facturación'))
        self.assertIsNone(route_contact(make_client(slug='empty'), 'Ventas'))
        self.assertIsNone(route_contact(None, 'Ventas'))

    def test_never_routes_across_tenants(self):
        other = make_client(slug='other')
        self.assertIsNone(route_contact(other, 'Ventas', 'Dra. Santos'))


class StaffContactPageTests(TestCase):

    def setUp(self):
        self.client_obj = make_client()
        User = get_user_model()
        self.admin = User.objects.create_user('admin@salon.com', password='x')
        Membership.objects.create(user=self.admin, client=self.client_obj, role='org_admin')
        self.agent_user = User.objects.create_user('agent@salon.com', password='x')
        Membership.objects.create(user=self.agent_user, client=self.client_obj, role='agent')

    def test_org_admin_adds_and_removes_a_contact(self):
        self.client.force_login(self.admin)
        resp = self.client.post(reverse('staff_contact_add'), {
            'client': 'salon', 'name': 'Carla', 'department': 'Facturación',
            'email': 'cobros@salon.com', 'phone': '(787) 555-9999', 'channel': 'sms',
            'is_default': 'on'})
        self.assertEqual(resp.status_code, 302)
        contact = StaffContact.objects.get()
        self.assertEqual((contact.client, contact.phone, contact.channel, contact.is_default),
                         (self.client_obj, '+17875559999', 'sms', True))
        self.assertTrue(AuditEvent.objects.filter(action='contact.add').exists())
        page = self.client.get(reverse('dash_users'))
        self.assertContains(page, 'Contactos para recados')
        self.assertContains(page, 'cobros@salon.com')
        resp = self.client.post(reverse('staff_contact_delete', args=[contact.pk]), {'client': 'salon'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(StaffContact.objects.count(), 0)

    def test_validation(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('staff_contact_add'), {
            'client': 'salon', 'name': 'Carla', 'phone': '787-55', 'channel': 'sms'})
        self.client.post(reverse('staff_contact_add'), {
            'client': 'salon', 'name': 'Carla', 'email': 'c@salon.com', 'channel': 'sms'})
        self.client.post(reverse('staff_contact_add'), {'client': 'salon', 'name': 'Carla'})
        self.assertEqual(StaffContact.objects.count(), 0)

    def test_agent_role_cannot_manage_contacts(self):
        self.client.force_login(self.agent_user)
        resp = self.client.post(reverse('staff_contact_add'), {
            'client': 'salon', 'name': 'Carla', 'email': 'c@salon.com'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(StaffContact.objects.count(), 0)

    def test_cannot_delete_another_tenants_contact(self):
        other = make_client(slug='other')
        foreign = StaffContact.objects.create(client=other, name='X', email='x@other.com')
        self.client.force_login(self.admin)
        resp = self.client.post(reverse('staff_contact_delete', args=[foreign.pk]), {'client': 'salon'})
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(StaffContact.objects.filter(pk=foreign.pk).exists())


class ClientFormProfileTests(TestCase):

    def test_client_form_saves_agent_profile(self):
        obj = make_client(profile='web')
        data = {k: v for k, v in model_to_dict(obj, fields=ClientForm._meta.fields).items()
                if v is not None}
        data['agent_profile'] = 'receptionist'
        form = ClientForm(data, instance=obj)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        obj.refresh_from_db()
        self.assertEqual(obj.agent_profile, 'receptionist')
        data['agent_profile'] = 'sales'
        self.assertFalse(ClientForm(data, instance=obj).is_valid())


class NotifyOverrideTests(TestCase):

    def test_send_to_and_overrides(self):
        c = make_client(notify_phone='+17875550000', notify_channel='whatsapp')
        with patch(SEND, return_value=True) as send:
            self.assertTrue(notify.notify_client(c, 'hola'))
        # Twilio is SMS only (WhatsApp goes through Meta): the legacy
        # 'whatsapp' preference sends one plain SMS, never a whatsapp: sender.
        self.assertEqual([call[0][0] for call in send.call_args_list], ['+17875550000'])
        with patch(SEND, return_value=True) as send:
            self.assertTrue(notify.notify_client(c, 'hola', phone='+17875551111', channel='sms'))
            self.assertFalse(notify.notify_client(c, 'hola', phone='', channel='sms'))
            self.assertFalse(notify.notify_client(c, 'hola', phone='+17875551111', channel='email'))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args[0][0], '+17875551111')
