"""
Phase F — locations (multi-site businesses): the Location model and its
partial constraints, per-location hours (rules_for / hours_now_lines), the
'=== Locales ===' prompt block and MULTI_LOCATION rules, the booking and
take_message handlers, visitor notices, the dashboard CRUD with its plan
limit, and the consumers that must only see business-wide rules. The model
provider and Twilio are ALWAYS mocked.
"""
import datetime as dt
import hashlib
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import agent, analytics, tenant_export, views
from .models import (
    AuditEvent, AvailabilityRule, Booking, Client, ContactSubmission, Conversation,
    Location, Membership, StaffContact, Subscription, match_location, route_contact,
    rules_for,
)
from .tests_core import WEB_RULES_SNAPSHOT
from .turns import hours_now_line, hours_now_lines, run_turn
from .views import _booking_handler, _take_message_handler

TOKEN = 'test-widget-token-abcdefghijklmnop'
SESSION = 'abcdef0123456789abcdef0123456789'
PR = ZoneInfo('America/Puerto_Rico')
SEND = 'landing.notify._send'  # the one Twilio seam


def make_client(slug='salon', profile='receptionist', **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', 'Salón Luz'), system_prompt='Salón de belleza.',
        notify_email=kw.pop('notify_email', f'owner@{slug}.com'),
        widget_token=kw.pop('widget_token', TOKEN), agent_profile=profile, **kw)


def two_locations(client):
    bay = Location.objects.create(client=client, name='Bayamón', address='Calle 1 #2',
                                  phone='+17875550100', is_default=True, order=0)
    cag = Location.objects.create(client=client, name='Caguas', order=1)
    return bay, cag


def weekly_rules(client, location=None, open_time='09:00', close_time='17:00', days=range(5)):
    for wd in days:
        AvailabilityRule.objects.create(client=client, location=location, weekday=wd,
                                        open_time=open_time, close_time=close_time)


def next_slot(hour=10, days=7):
    future = timezone.localtime(timezone.now() + dt.timedelta(days=days))
    # Land on a weekday so Mon-Fri rules accept it.
    while future.weekday() > 4:
        future += dt.timedelta(days=1)
    return future.replace(hour=hour, minute=0, second=0, microsecond=0)


# ---- model constraints ---------------------------------------------------------

class LocationConstraintTests(TestCase):

    def setUp(self):
        self.client_obj = make_client()
        self.other = make_client(slug='other')

    def _dup(self, fn):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                fn()

    def test_name_is_unique_per_client(self):
        Location.objects.create(client=self.client_obj, name='Bayamón')
        self._dup(lambda: Location.objects.create(client=self.client_obj, name='Bayamón'))
        # Another tenant may use the same town name.
        Location.objects.create(client=self.other, name='Bayamón')

    def test_at_most_one_default_per_client(self):
        Location.objects.create(client=self.client_obj, name='A', is_default=True)
        self._dup(lambda: Location.objects.create(client=self.client_obj, name='B', is_default=True))
        Location.objects.create(client=self.client_obj, name='C', is_default=False)
        Location.objects.create(client=self.other, name='A', is_default=True)

    def test_availability_unique_for_business_and_per_location(self):
        bay, cag = two_locations(self.client_obj)
        AvailabilityRule.objects.create(client=self.client_obj, weekday=0,
                                        open_time='09:00', close_time='17:00')
        self._dup(lambda: AvailabilityRule.objects.create(
            client=self.client_obj, weekday=0, open_time='10:00', close_time='12:00'))
        # The same weekday for a location and for the other location is fine.
        AvailabilityRule.objects.create(client=self.client_obj, location=bay, weekday=0,
                                        open_time='09:00', close_time='17:00')
        AvailabilityRule.objects.create(client=self.client_obj, location=cag, weekday=0,
                                        open_time='09:00', close_time='17:00')
        self._dup(lambda: AvailabilityRule.objects.create(
            client=self.client_obj, location=bay, weekday=0, open_time='10:00', close_time='12:00'))

    def test_booking_slot_unique_for_business_and_per_location(self):
        bay, cag = two_locations(self.client_obj)
        start = next_slot()
        Booking.objects.create(client=self.client_obj, name='A', email='a@x.com', start=start)
        self._dup(lambda: Booking.objects.create(
            client=self.client_obj, name='B', email='b@x.com', start=start))
        # One active booking per location per slot; two locations don't collide,
        # and a business-wide booking is a separate pool.
        Booking.objects.create(client=self.client_obj, location=bay, name='C',
                               email='c@x.com', start=start)
        Booking.objects.create(client=self.client_obj, location=cag, name='D',
                               email='d@x.com', start=start)
        self._dup(lambda: Booking.objects.create(
            client=self.client_obj, location=bay, name='E', email='e@x.com', start=start))
        # A cancelled booking frees the slot.
        Booking.objects.filter(location=bay).update(status='cancelled')
        Booking.objects.create(client=self.client_obj, location=bay, name='F',
                               email='f@x.com', start=start)

    def test_deleting_a_location_keeps_bookings_and_drops_its_rules(self):
        bay, _cag = two_locations(self.client_obj)
        weekly_rules(self.client_obj, location=bay)
        booking = Booking.objects.create(client=self.client_obj, location=bay, name='A',
                                         email='a@x.com', start=next_slot())
        bay.delete()
        booking.refresh_from_db()
        self.assertIsNone(booking.location)
        self.assertFalse(AvailabilityRule.objects.exists())


# ---- helpers -----------------------------------------------------------------------

class HelperTests(TestCase):

    def setUp(self):
        self.client_obj = make_client()
        self.bay, self.cag = two_locations(self.client_obj)

    def test_rules_for_prefers_the_location_and_falls_back_to_the_business(self):
        self.assertEqual(rules_for(self.client_obj), [])
        self.assertEqual(rules_for(self.client_obj, self.cag), [])
        weekly_rules(self.client_obj)
        business = rules_for(self.client_obj)
        self.assertEqual(len(business), 5)
        self.assertTrue(all(r.location_id is None for r in business))
        # Caguas has no rules of its own: it inherits the business's.
        self.assertEqual([r.pk for r in rules_for(self.client_obj, self.cag)],
                         [r.pk for r in business])
        weekly_rules(self.client_obj, location=self.bay, open_time='08:00', close_time='12:00',
                     days=[5])
        own = rules_for(self.client_obj, self.bay)
        self.assertEqual([(r.weekday, r.location_id) for r in own], [(5, self.bay.pk)])
        # Per-location rows never leak into the business-wide list.
        self.assertEqual(len(rules_for(self.client_obj)), 5)
        self.assertEqual(rules_for(None), [])

    def test_match_location_is_case_and_accent_insensitive(self):
        self.assertEqual(match_location(self.client_obj, 'bayamon'), self.bay)
        self.assertEqual(match_location(self.client_obj, 'BAYAMÓN'), self.bay)
        self.assertEqual(match_location(self.client_obj, 'el de Caguas'), self.cag)
        self.assertEqual(match_location(self.client_obj, 'Cag'), self.cag)
        self.assertIsNone(match_location(self.client_obj, ''))
        self.assertIsNone(match_location(self.client_obj, None))
        self.assertIsNone(match_location(self.client_obj, 'Ponce'))
        self.assertIsNone(match_location(None, 'Caguas'))
        # Another tenant's locations are never matched.
        other = make_client(slug='other')
        self.assertIsNone(match_location(other, 'Caguas'))

    def test_match_location_ignores_one_or_two_letter_inputs(self):
        # 'a' and 'n' are inside 'bayamon'; a reverse containment needs 3+ chars.
        for text in ('n', 'a', 'ay'):
            self.assertIsNone(match_location(self.client_obj, text))
        self.cag.delete()
        for text in ('n', 'a'):
            self.assertIsNone(match_location(self.client_obj, text))
        self.assertEqual(match_location(self.client_obj, 'bay'), self.bay)

    def test_cross_tenant_location_is_rejected_by_clean_and_rules_for(self):
        other = make_client(slug='other')
        foreign = Location.objects.create(client=other, name='Ponce')
        weekly_rules(other, location=foreign, open_time='01:00', close_time='02:00')
        from django.core.exceptions import ValidationError
        rule = AvailabilityRule(client=self.client_obj, location=foreign, weekday=0,
                                open_time='09:00', close_time='17:00')
        with self.assertRaises(ValidationError):
            rule.full_clean()
        with self.assertRaises(ValidationError):
            Booking(client=self.client_obj, location=foreign, name='A', email='a@x.com',
                    start=next_slot()).full_clean()
        with self.assertRaises(ValidationError):
            StaffContact(client=self.client_obj, location=foreign, name='X',
                         email='x@x.com').full_clean()
        # Own location: fine.
        AvailabilityRule(client=self.client_obj, location=self.bay, weekday=0,
                         open_time='09:00', close_time='17:00').full_clean()
        # A foreign location never lends its hours to this tenant.
        self.assertEqual(rules_for(self.client_obj, foreign), [])

    def test_match_location_refuses_an_ambiguous_partial(self):
        Location.objects.create(client=self.client_obj, name='Bayamón Norte')
        # 'bayamón' matches 'Bayamón' exactly: exact wins over the partial.
        self.assertEqual(match_location(self.client_obj, 'Bayamón'), self.bay)
        # 'bay' is contained in both: nobody guesses.
        self.assertIsNone(match_location(self.client_obj, 'bay'))


# ---- HOURS NOW per location ---------------------------------------------------------

class HoursNowLinesTests(TestCase):
    SUMMARY = 'hours: Mon-Fri 09:00-17:00; Sat-Sun closed (America/Puerto_Rico)'

    def setUp(self):
        self.client_obj = make_client()
        weekly_rules(self.client_obj)
        self.wed_10 = dt.datetime(2026, 10, 14, 10, tzinfo=PR)  # Wednesday 10:00

    def test_single_location_keeps_the_old_line(self):
        before = hours_now_line(self.client_obj, now=self.wed_10)
        Location.objects.create(client=self.client_obj, name='Bayamón')
        self.assertEqual(hours_now_line(self.client_obj, now=self.wed_10), before)
        self.assertEqual(hours_now_lines(self.client_obj, now=self.wed_10), [before])
        self.assertEqual(before, f'HOURS NOW (authoritative): OPEN NOW, closes today 17:00 | {self.SUMMARY}')

    def test_one_line_per_location_with_header(self):
        bay, cag = two_locations(self.client_obj)
        weekly_rules(self.client_obj, location=cag, open_time='11:00', close_time='19:00')
        lines = hours_now_lines(self.client_obj, now=self.wed_10)
        self.assertEqual(lines, [
            'HOURS NOW (authoritative, per location):',
            f'[Bayamón] OPEN NOW, closes today 17:00 | {self.SUMMARY}',
            '[Caguas] CLOSED NOW, opens today 11:00 | hours: Mon-Fri 11:00-19:00; Sat-Sun closed '
            '(America/Puerto_Rico)',
        ])
        # hours_now_line stays the business-wide line (its callers predate F).
        self.assertTrue(hours_now_line(self.client_obj, now=self.wed_10)
                        .startswith('HOURS NOW (authoritative): OPEN NOW'))

    def test_location_without_rules_inherits_the_business_hours(self):
        two_locations(self.client_obj)
        lines = hours_now_lines(self.client_obj, now=self.wed_10)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith('[Bayamón] OPEN NOW'))
        self.assertTrue(lines[2].startswith('[Caguas] OPEN NOW'))

    def test_location_without_any_hours_is_omitted(self):
        AvailabilityRule.objects.all().delete()
        bay, _cag = two_locations(self.client_obj)
        weekly_rules(self.client_obj, location=bay)
        lines = hours_now_lines(self.client_obj, now=self.wed_10)
        self.assertEqual([l.split(']')[0] for l in lines[1:]], ['[Bayamón'])
        # Nothing anywhere: no block at all.
        AvailabilityRule.objects.all().delete()
        self.assertEqual(hours_now_lines(self.client_obj, now=self.wed_10), [])
        self.assertEqual(hours_now_lines(None), [])


# ---- prompt and rules ---------------------------------------------------------------

class PromptTests(TestCase):

    def test_prompt_lists_locations_and_changes_config_version(self):
        c = make_client()
        before = c.config_version()
        self.assertNotIn('=== Locales ===', c.compiled_prompt())
        two_locations(c)
        prompt = c.compiled_prompt()
        self.assertIn('=== Locales ===\n- Bayamón (principal): Calle 1 #2 · Tel. (787) 555-0100\n'
                      '- Caguas', prompt)
        # Right after the business header, before the knowledge sources.
        self.assertTrue(prompt.startswith('Salón de belleza.\n\n=== Locales ==='))
        self.assertNotEqual(c.config_version(), before)
        # The internal assistant sees the same block.
        self.assertIn('=== Locales ===', c.compiled_internal_prompt())

    def test_web_snapshot_is_intact_and_the_block_goes_last(self):
        for key, digest in WEB_RULES_SNAPSHOT.items():
            lang, flags = key.split('|')
            b, h, cl = (f == '1' for f in flags)
            rules = agent.build_rules(language=lang, bookings=b, human=h, clinic=cl)
            self.assertEqual(hashlib.sha256(rules.encode('utf-8')).hexdigest(), digest)
            self.assertEqual(agent.build_rules(language=lang, bookings=b, human=h, clinic=cl,
                                               multi_location=True),
                             rules + agent.MULTI_LOCATION_RULES)
        self.assertNotIn('LOCATIONS:', agent.build_rules(bookings=True, human=True, clinic=True))

    def test_block_sits_between_booking_and_clinic_before_security(self):
        rec = agent.build_rules(bookings=True, clinic=True, profile='receptionist',
                                multi_location=True)
        block = agent.MULTI_LOCATION_RULES.strip()
        self.assertIn(block, rec)
        self.assertLess(rec.index(agent.BOOKING_RULES_GENERAL.strip()), rec.index(block))
        self.assertLess(rec.index(block), rec.index(agent.CLINIC_RULES.strip()))
        self.assertLess(rec.index(block), rec.index('SECURITY:'))
        self.assertTrue(rec.endswith(agent._RULES_SECURITY_GENERAL.strip()))
        self.assertNotIn(block, agent.build_rules(bookings=True, profile='receptionist'))
        # The employees' assistant never gets it.
        self.assertNotIn(block, agent.build_rules(profile='internal', multi_location=True))

    def test_block_text_is_the_approved_b9_wording(self):
        text = agent.MULTI_LOCATION_RULES
        self.assertTrue(text.startswith('\n\nLOCATIONS: This business has several locations (listed below).'))
        self.assertIn('ask once which location, naming the options', text)
        self.assertIn('put the location in tool inputs', text)


@override_settings(ANTHROPIC_API_KEY='test-key')
class RunTurnFlagTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client_obj = make_client()
        self.conv = Conversation.objects.create(client=self.client_obj, widget_session=SESSION)

    def _calls(self, **kw):
        calls = []

        def _answer(history, business_prompt=None, handlers=None, language='es', **kwargs):
            calls.append(kwargs)
            return 'ok', {'input_tokens': 1, 'output_tokens': 1}
        with patch.object(agent, 'answer', side_effect=_answer):
            run_turn(self.client_obj, self.conv, 'hola', channel='web', handlers={}, **kw)
        return calls[0]

    def test_flag_only_with_two_or_more_locations(self):
        self.assertNotIn('multi_location', self._calls())
        Location.objects.create(client=self.client_obj, name='Bayamón')
        self.assertNotIn('multi_location', self._calls(profile='receptionist'))
        Location.objects.create(client=self.client_obj, name='Caguas')
        self.assertTrue(self._calls()['multi_location'])
        self.assertTrue(self._calls(profile='receptionist')['multi_location'])
        self.assertNotIn('multi_location', self._calls(profile='internal'))

    def test_receptionist_gets_the_per_location_block_as_one_context_line(self):
        weekly_rules(self.client_obj)
        two_locations(self.client_obj)
        kwargs = self._calls(profile='receptionist')
        self.assertEqual(len(kwargs['context_lines']), 1)
        block = kwargs['context_lines'][0]
        self.assertTrue(block.startswith('HOURS NOW (authoritative, per location):\n[Bayamón] '))
        self.assertIn('\n[Caguas] ', block)


# ---- handlers --------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='test-key', CONTACT_NOTIFY_EMAIL='ops@example.com',
                   SITE_URL='https://dominiopr.com')
class BookingHandlerLocationTests(TestCase):

    def setUp(self):
        cache.clear()
        self.rf = RequestFactory()
        self.client_obj = make_client(enable_bookings=True)
        self.slot = next_slot()

    def _handle(self):
        req = self.rf.post('/api/chat/')
        req.META['REMOTE_ADDR'] = '10.1.2.3'
        return _booking_handler(req, self.client_obj)

    def _input(self, **over):
        data = {'name': 'Ana Pérez', 'email': 'ana@example.com',
                'start': self.slot.strftime('%Y-%m-%dT%H:%M'), 'service': 'corte'}
        data.update(over)
        return data

    def test_no_locations_books_business_wide(self):
        self.assertIn('Reservado', self._handle()(self._input(location='Ponce')))
        self.assertIsNone(Booking.objects.get().location)

    def test_single_location_is_assigned_silently(self):
        bay = Location.objects.create(client=self.client_obj, name='Bayamón')
        result = self._handle()(self._input())  # no `location` in the tool input
        self.assertIn('Reservado', result)
        self.assertIn(' en Bayamón', result)
        self.assertEqual(Booking.objects.get().location, bay)

    def test_asks_for_the_location_when_ambiguous(self):
        two_locations(self.client_obj)
        handle = self._handle()
        for given in ({}, {'location': ''}, {'location': 'Ponce'}):
            result = handle(self._input(**given))
            self.assertEqual(result, 'No se reservó: falta el local. Pregunta en cuál local '
                                     'quiere la cita: Bayamón, Caguas.')
        self.assertEqual(Booking.objects.count(), 0)
        self.assertEqual(mail.outbox, [])
        # A soft rejection does not consume the per-IP booking cap.
        self.assertEqual(cache.get('bookcap:10.1.2.3', 0), 0)

    def test_matches_the_location_case_insensitively(self):
        _bay, cag = two_locations(self.client_obj)
        with patch(SEND) as send:
            result = self._handle()(self._input(location='caguas'))
        self.assertIn('Reservado', result)
        self.assertIn(' en Caguas', result)
        self.assertEqual(Booking.objects.get().location, cag)
        send.assert_not_called()  # the owner has no notify_phone

    def test_validates_the_hours_of_the_chosen_location(self):
        bay, cag = two_locations(self.client_obj)
        weekly_rules(self.client_obj)                                   # business 09-17
        weekly_rules(self.client_obj, location=cag, open_time='13:00', close_time='20:00')
        handle = self._handle()
        # 10:00 is inside the business hours (Bayamón inherits them)...
        self.assertIn('Reservado', handle(self._input(location='Bayamón')))
        # ...but outside Caguas's own window.
        with self.assertRaises(ValueError):
            handle(self._input(location='Caguas'))
        self.assertEqual(Booking.objects.count(), 1)
        late = self.slot.replace(hour=18)
        self.assertIn('Reservado', handle(self._input(
            location='Caguas', start=late.strftime('%Y-%m-%dT%H:%M'))))
        with self.assertRaises(ValueError):
            handle(self._input(location='Bayamón', name='Luis', email='l@example.com',
                               start=late.strftime('%Y-%m-%dT%H:%M')))

    def test_business_wide_booking_blocks_the_slot_for_every_location(self):
        # A pending booking taken BEFORE the business had locations...
        handle = self._handle()
        self.assertIn('Reservado', handle(self._input()))
        self.assertIsNone(Booking.objects.get().location)
        # ...keeps the slot busy once a location exists (the constraints alone
        # would allow a second row in the per-location pool).
        Location.objects.create(client=self.client_obj, name='Bayamón')
        self.assertIn('ocupada', self._handle()(self._input(name='Luis', email='l@example.com')))
        self.assertEqual(Booking.objects.count(), 1)

    def test_location_booking_blocks_the_slot_for_a_business_wide_one(self):
        bay = Location.objects.create(client=self.client_obj, name='Bayamón')
        Booking.objects.create(client=self.client_obj, location=bay, name='Ana',
                               email='a@x.com', start=self.slot)
        # The location changes hands (admin mistake): the handler now books
        # business-wide for this client, and the slot is still busy.
        Location.objects.filter(pk=bay.pk).update(client=make_client(slug='other'))
        self.assertIn('ocupada', self._handle()(self._input(name='Luis', email='l@example.com')))
        self.assertEqual(Booking.objects.count(), 1)

    def test_two_locations_share_a_slot_but_not_with_themselves(self):
        two_locations(self.client_obj)
        handle = self._handle()
        self.assertIn('Reservado', handle(self._input(location='Bayamón')))
        self.assertIn('Reservado', handle(self._input(location='Caguas')))
        self.assertIn('ocupada', handle(self._input(location='Caguas')))
        self.assertEqual(Booking.objects.count(), 2)

    def test_emails_and_sms_mention_the_location(self):
        bay, _cag = two_locations(self.client_obj)
        with patch(SEND, return_value=True) as send:
            self._handle()(self._input(location='Bayamón', email='', phone='787-555-7788'))
        booking = Booking.objects.get()
        self.assertEqual(booking.location, bay)
        # Owner notification (HTML + text) names the location and its address.
        owner_mail = next(m for m in mail.outbox if m.subject.startswith('Nueva cita'))
        self.assertIn('Local:   Bayamón — Calle 1 #2', owner_mail.body)
        self.assertIn('Bayamón &mdash; Calle 1 #2', owner_mail.alternatives[0][0])
        # Phone-only visitor: the SMS says where (accent-free, GSM-7 safe).
        to, body, _from = send.call_args[0]
        self.assertEqual(to, '+17875557788')
        self.assertIn(' en Bayamon.', body)
        self.assertNotIn('Bayamón', body)
        # Visitor email confirmation too.
        mail.outbox.clear()
        with patch(SEND):
            self._handle()(self._input(location='Caguas', name='Luis', email='luis@example.com'))
        visitor_mail = next(m for m in mail.outbox if m.subject.startswith('Recibimos tu cita'))
        self.assertIn('Local:  Caguas', visitor_mail.body)
        self.assertEqual(views.booking_sms_text(Booking.objects.get(location__name='Caguas'),
                                                'Salón Luz', reminder=True).count(' en Caguas'), 1)


@override_settings(ANTHROPIC_API_KEY='test-key', CONTACT_NOTIFY_EMAIL='ops@example.com',
                   SITE_URL='https://dominiopr.com')
class TakeMessageLocationTests(TestCase):

    def setUp(self):
        cache.clear()
        self.rf = RequestFactory()
        self.client_obj = make_client()
        self.bay, self.cag = two_locations(self.client_obj)

    def _handle(self):
        req = self.rf.post('/api/chat/')
        req.META['REMOTE_ADDR'] = '10.1.2.3'
        return _take_message_handler(req, self.client_obj)

    def _input(self, **over):
        data = {'name': 'Ivette Cruz', 'callback_phone': '787-555-2210',
                'department': 'Ventas', 'urgency': 'normal',
                'summary': 'Quiere saber si tienen keratina.'}
        data.update(over)
        return data

    def test_message_stores_the_matched_location_name(self):
        with patch(SEND):
            self._handle()(self._input(location='el de caguas'))
        sub = ContactSubmission.objects.get()
        self.assertIn('Local: Caguas', sub.message)
        self.assertNotIn('el de caguas', sub.message)
        self.assertTrue(any('Local: Caguas' in m.body for m in mail.outbox))
        # Unknown text stays as the model wrote it (not clinic mode).
        with patch(SEND):
            self._handle()(self._input(location='Ponce', name='Luis'))
        self.assertIn('Local: Ponce', ContactSubmission.objects.latest('pk').message)

    def test_routes_to_the_department_contact_of_that_location(self):
        anywhere = StaffContact.objects.create(client=self.client_obj, name='Ventas general',
                                               department='Ventas', email='ventas@salon.com')
        caguas = StaffContact.objects.create(client=self.client_obj, name='Ventas Caguas',
                                             department='Ventas', email='caguas@salon.com',
                                             location=self.cag)
        with patch(SEND):
            self._handle()(self._input(location='Caguas'))
            self._handle()(self._input(location='Bayamón', name='Luis'))
        self.assertEqual([m.to for m in mail.outbox],
                         [[caguas.email], [anywhere.email]])


class RouteContactLocationTests(TestCase):

    def setUp(self):
        self.client_obj = make_client()
        self.bay, self.cag = two_locations(self.client_obj)
        self.santos = StaffContact.objects.create(client=self.client_obj, name='Dra. Santos',
                                                  email='s@x.com')
        self.ventas = StaffContact.objects.create(client=self.client_obj, name='Ventas',
                                                  department='Ventas', email='v@x.com')
        self.ventas_cag = StaffContact.objects.create(client=self.client_obj, name='Ventas Caguas',
                                                      department='Ventas', email='vc@x.com',
                                                      location=self.cag)
        self.cag_desk = StaffContact.objects.create(client=self.client_obj, name='Recepción Caguas',
                                                    email='rc@x.com', location=self.cag)
        self.default = StaffContact.objects.create(client=self.client_obj, name='Dueño',
                                                   email='d@x.com', is_default=True)

    def test_precedence_person_department_location_default(self):
        # person > department+location > department > location > default
        self.assertEqual(route_contact(self.client_obj, 'Ventas', 'la dra. santos', self.cag), self.santos)
        self.assertEqual(route_contact(self.client_obj, 'Ventas', location=self.cag), self.ventas_cag)
        self.assertEqual(route_contact(self.client_obj, 'Ventas', location=self.bay), self.ventas)
        self.assertEqual(route_contact(self.client_obj, 'Ventas'), self.ventas)
        self.assertEqual(route_contact(self.client_obj, 'Facturación', location=self.cag), self.cag_desk)
        self.assertEqual(route_contact(self.client_obj, 'Facturación', location=self.bay), self.default)
        self.assertEqual(route_contact(self.client_obj, 'Facturación'), self.default)

    def test_department_contact_of_another_location_beats_nothing(self):
        self.ventas.delete()
        # Only Caguas has a sales contact: a Bayamón sales message still reaches it.
        self.assertEqual(route_contact(self.client_obj, 'Ventas', location=self.bay), self.ventas_cag)


# ---- booking_status -----------------------------------------------------------------

class BookingStatusTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client_obj = make_client(enable_bookings=True)
        _bay, self.cag = two_locations(self.client_obj)
        User = get_user_model()
        self.admin = User.objects.create_user('admin@salon.com', password='x')
        Membership.objects.create(user=self.admin, client=self.client_obj, role='org_admin')
        self.client.force_login(self.admin)

    def test_phone_only_booking_gets_an_sms_and_no_empty_email(self):
        booking = Booking.objects.create(client=self.client_obj, location=self.cag, name='Ana',
                                         email='', phone='+17875557788', start=next_slot())
        with patch(SEND, return_value=True) as send:
            resp = self.client.post(reverse('booking_status', args=[booking.pk]),
                                    {'status': 'confirmed'})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(mail.outbox, [])
        to, body, _from = send.call_args[0]
        self.assertEqual(to, '+17875557788')
        self.assertIn('confirmada', body)
        self.assertIn(' en Caguas', body)
        with patch(SEND, return_value=True) as send:
            self.client.post(reverse('booking_status', args=[booking.pk]), {'status': 'cancelled'})
        self.assertIn('cancelada', send.call_args[0][1])
        self.assertEqual(mail.outbox, [])

    def test_email_booking_mentions_the_location(self):
        booking = Booking.objects.create(client=self.client_obj, location=self.cag, name='Ana',
                                         email='ana@example.com', start=next_slot())
        with patch(SEND) as send:
            self.client.post(reverse('booking_status', args=[booking.pk]), {'status': 'confirmed'})
        send.assert_not_called()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(' en Caguas.', mail.outbox[0].body)


# ---- dashboard CRUD --------------------------------------------------------------

class LocationDashboardTests(TestCase):

    def setUp(self):
        cache.clear()
        self.client_obj = make_client(enable_bookings=True)
        Subscription.objects.create(client=self.client_obj, plan='scale', status='active')
        User = get_user_model()
        self.admin = User.objects.create_user('admin@salon.com', password='x')
        Membership.objects.create(user=self.admin, client=self.client_obj, role='org_admin')
        self.agent_user = User.objects.create_user('agent@salon.com', password='x')
        Membership.objects.create(user=self.agent_user, client=self.client_obj, role='agent')

    def _save(self, **data):
        payload = {'client': 'salon', 'name': 'Bayamón'}
        payload.update(data)
        return self.client.post(reverse('location_save'), payload)

    def test_org_admin_creates_updates_and_deletes(self):
        self.client.force_login(self.admin)
        resp = self._save(address='Calle 1', phone='(787) 555-0100', is_default='on')
        self.assertEqual(resp.status_code, 302)
        loc = Location.objects.get()
        self.assertEqual((loc.client, loc.address, loc.phone, loc.is_default),
                         (self.client_obj, 'Calle 1', '+17875550100', True))
        self.assertTrue(AuditEvent.objects.filter(action='location.add', client=self.client_obj).exists())
        # Update by pk; the default moves to the new one atomically.
        self._save(name='Caguas', is_default='on')
        self._save(pk=loc.pk, name='Bayamón Centro', phone='')
        loc.refresh_from_db()
        self.assertEqual((loc.name, loc.phone, loc.is_default), ('Bayamón Centro', '', False))
        self.assertEqual(Location.objects.filter(is_default=True).count(), 1)
        page = self.client.get(reverse('bookings'))
        self.assertContains(page, 'Bayamón Centro')
        self.assertContains(page, 'Todo el negocio')
        resp = self.client.post(reverse('location_delete', args=[loc.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Location.objects.filter(pk=loc.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(action='location.remove').exists())

    def test_validation(self):
        self.client.force_login(self.admin)
        self._save(name='')
        self._save(phone='787-55')
        self.assertEqual(Location.objects.count(), 0)
        self._save()
        self._save(name='bayamón')  # case-insensitive duplicate
        self.assertEqual(Location.objects.count(), 1)

    def test_agent_role_cannot_manage_locations(self):
        self.client.force_login(self.agent_user)
        self.assertEqual(self._save().status_code, 302)
        self.assertEqual(Location.objects.count(), 0)
        loc = Location.objects.create(client=self.client_obj, name='Caguas')
        self.client.post(reverse('location_delete', args=[loc.pk]))
        self.assertTrue(Location.objects.filter(pk=loc.pk).exists())

    def test_plan_limit_is_enforced_and_audited(self):
        self.client_obj.subscription.plan = 'pro'   # 1 location
        self.client_obj.subscription.save(update_fields=['plan'])
        self.client.force_login(self.admin)
        self._save()
        self._save(name='Caguas')
        self.assertEqual(list(Location.objects.values_list('name', flat=True)), ['Bayamón'])
        denied = AuditEvent.objects.get(action='location.denied')
        self.assertEqual((denied.client, denied.target, denied.result),
                         (self.client_obj, 'Caguas', 'limit:1'))
        # Editing an existing one is not a new location.
        loc = Location.objects.get()
        self._save(pk=loc.pk, name='Bayamón Centro')
        self.assertEqual(Location.objects.get().name, 'Bayamón Centro')
        # Overrides raise the ceiling; a downgrade never deletes the extras.
        Client.objects.filter(pk=self.client_obj.pk).update(entitlement_overrides={'locations': 3})
        self._save(name='Caguas')
        self.assertEqual(Location.objects.count(), 2)
        Client.objects.filter(pk=self.client_obj.pk).update(entitlement_overrides={})
        self._save(name='Ponce')
        self.assertEqual(Location.objects.count(), 2)

    def test_first_location_adopts_the_future_business_wide_bookings(self):
        self.client.force_login(self.admin)
        future = Booking.objects.create(client=self.client_obj, name='Ana', email='a@x.com',
                                        start=next_slot())
        cancelled = Booking.objects.create(client=self.client_obj, name='Bea', email='b@x.com',
                                           start=next_slot(hour=11), status='cancelled')
        past = Booking.objects.create(client=self.client_obj, name='Cid', email='c@x.com',
                                      start=timezone.now() - dt.timedelta(days=3))
        other = make_client(slug='other', enable_bookings=True)
        foreign = Booking.objects.create(client=other, name='Zed', email='z@x.com',
                                         start=next_slot(hour=12))
        self._save()
        bay = Location.objects.get()
        for b in (future, cancelled, past, foreign):
            b.refresh_from_db()
        self.assertEqual(future.location, bay)
        self.assertIsNone(cancelled.location)
        self.assertIsNone(past.location)
        self.assertIsNone(foreign.location)
        added = AuditEvent.objects.get(action='location.add')
        self.assertEqual(added.result, 'Bayamón|citas_reasignadas:1')
        # The second location adopts nothing.
        later = Booking.objects.create(client=self.client_obj, name='Dan', email='d@x.com',
                                       start=next_slot(hour=13))
        self._save(name='Caguas')
        later.refresh_from_db()
        self.assertIsNone(later.location)
        self.assertEqual(AuditEvent.objects.filter(action='location.add').latest('pk').result,
                         'Caguas')

    def test_checks_run_inside_the_locked_transaction(self):
        from django.db import connection
        from . import platform_views
        self.client_obj.subscription.plan = 'pro'   # 1 location
        self.client_obj.subscription.save(update_fields=['plan'])
        self.client.force_login(self.admin)
        seen = []
        real_ent = platform_views.entitlements.entitlements

        def spy_ent(client):
            seen.append(connection.in_atomic_block)
            return real_ent(client)
        with patch.object(platform_views.entitlements, 'entitlements', side_effect=spy_ent):
            self._save()
        self.assertEqual(seen, [True])
        self.assertEqual(Location.objects.count(), 1)

    def test_concurrent_duplicate_name_is_caught_by_the_constraint(self):
        self.client.force_login(self.admin)
        real_create = Location.objects.create

        def racing_create(**kwargs):
            # Another request inserts the same name between our check and
            # our insert (SQLite has no row lock to stop it).
            Location(client=self.client_obj, name='Bayamón').save()
            return real_create(**kwargs)
        with patch.object(Location.objects, 'create', side_effect=racing_create):
            resp = self.client.post(reverse('location_save'),
                                    {'client': 'salon', 'name': 'Bayamón'}, follow=True)
        self.assertContains(resp, 'No se pudo guardar el local')
        self.assertFalse(AuditEvent.objects.filter(action='location.add').exists())
        # The simulated rival insert shares our transaction, so the constraint
        # violation rolls both back here; in production the rival is already
        # committed and only ours is refused. Either way: never two rows.
        self.assertLessEqual(Location.objects.filter(name='Bayamón').count(), 1)

    def test_downgrade_notice_when_over_the_plan(self):
        two_locations(self.client_obj)
        self.client_obj.subscription.plan = 'pro'   # 1 location, 2 exist
        self.client_obj.subscription.save(update_fields=['plan'])
        self.client.force_login(self.admin)
        page = self.client.get(reverse('bookings'))
        self.assertContains(page, 'Tu plan incluye 1 local(es) y tienes 2')
        self.assertEqual(Location.objects.count(), 2)
        self.client_obj.subscription.plan = 'scale'
        self.client_obj.subscription.save(update_fields=['plan'])
        self.assertNotContains(self.client.get(reverse('bookings')), 'y tienes 2')

    def test_delete_is_blocked_while_future_bookings_exist(self):
        self.client.force_login(self.admin)
        loc = Location.objects.create(client=self.client_obj, name='Caguas')
        booking = Booking.objects.create(client=self.client_obj, location=loc, name='Ana',
                                         email='a@x.com', start=next_slot())
        self.client.post(reverse('location_delete', args=[loc.pk]))
        self.assertTrue(Location.objects.filter(pk=loc.pk).exists())
        Booking.objects.filter(pk=booking.pk).update(status='cancelled')
        self.client.post(reverse('location_delete', args=[loc.pk]))
        self.assertFalse(Location.objects.filter(pk=loc.pk).exists())

    def test_availability_add_with_a_location(self):
        self.client.force_login(self.admin)
        loc = Location.objects.create(client=self.client_obj, name='Caguas')
        foreign = Location.objects.create(client=make_client(slug='other'), name='Ponce')
        base = {'client': self.client_obj.pk, 'weekday': 0, 'open_time': '09:00',
                'close_time': '17:00'}
        self.client.post(reverse('availability_add'), base)
        self.client.post(reverse('availability_add'), {**base, 'location': loc.pk,
                                                       'open_time': '11:00'})
        # Same weekday again for the location: update, not a second row.
        self.client.post(reverse('availability_add'), {**base, 'location': loc.pk,
                                                       'open_time': '12:00'})
        self.client.post(reverse('availability_add'), {**base, 'location': foreign.pk})
        rules = {(r.location_id, str(r.open_time)) for r in AvailabilityRule.objects.all()}
        self.assertEqual(rules, {(None, '09:00:00'), (loc.pk, '12:00:00')})
        page = self.client.get(reverse('bookings'))
        self.assertContains(page, 'Caguas')

    def test_isolation_between_tenants(self):
        other = make_client(slug='other', enable_bookings=True)
        foreign = Location.objects.create(client=other, name='Ponce')
        Booking.objects.create(client=other, location=foreign, name='Zed', email='z@x.com',
                               start=next_slot())
        self.client.force_login(self.admin)
        # Updating or deleting another tenant's location 404s.
        self.assertEqual(self._save(pk=foreign.pk, name='Hacked').status_code, 404)
        self.assertEqual(self.client.post(reverse('location_delete', args=[foreign.pk])).status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, 'Ponce')
        # Filtering by a foreign location shows nothing of theirs.
        page = self.client.get(reverse('bookings') + f'?location={foreign.pk}')
        self.assertNotContains(page, 'Zed')
        self.assertNotContains(page, 'Ponce')

    def test_bookings_list_filters_by_location_and_shows_the_column(self):
        self.client.force_login(self.admin)
        bay, cag = two_locations(self.client_obj)
        Booking.objects.create(client=self.client_obj, location=bay, name='AnaBay',
                               email='a@x.com', start=next_slot())
        Booking.objects.create(client=self.client_obj, location=cag, name='LuisCag',
                               email='l@x.com', start=next_slot(hour=11))
        page = self.client.get(reverse('bookings'))
        self.assertContains(page, '<th>Local</th>')
        self.assertContains(page, 'AnaBay')
        self.assertContains(page, 'LuisCag')
        page = self.client.get(reverse('bookings') + f'?location={cag.pk}')
        self.assertContains(page, 'LuisCag')
        self.assertNotContains(page, 'AnaBay')


# ---- consumers that only see business-wide rules -------------------------------------

class ConsumerTests(TestCase):

    def setUp(self):
        self.client_obj = make_client()
        self.bay, self.cag = two_locations(self.client_obj)

    def test_analytics_business_hours_ignores_location_rules(self):
        weekly_rules(self.client_obj)
        weekly_rules(self.client_obj, location=self.cag, open_time='00:00', close_time='23:59')
        hours = analytics.business_hours([self.client_obj.pk])
        self.assertEqual(hours[self.client_obj.pk][0], (dt.time(9), dt.time(17)))

    def test_export_includes_the_location(self):
        weekly_rules(self.client_obj, location=self.cag, days=[0])
        Booking.objects.create(client=self.client_obj, location=self.cag, name='Ana',
                               email='a@x.com', start=next_slot())
        Booking.objects.create(client=self.client_obj, name='Luis', email='l@x.com',
                               start=next_slot(hour=11))
        tables = {name: (cols, list(rows())) for name, cols, rows in tenant_export._tables(self.client_obj)}
        cols, rows = tables['bookings']
        self.assertIn('location', cols)
        by_name = {r[cols.index('name')]: r[cols.index('location')] for r in rows}
        self.assertEqual(by_name, {'Ana': 'Caguas', 'Luis': None})
        cols, rows = tables['availability_rules']
        self.assertEqual([r[cols.index('location')] for r in rows], ['Caguas'])

    def test_plan_limits_declare_locations(self):
        self.assertEqual({p: views.PLAN_LIMITS[p]['locations'] for p in
                          ('starter', 'pro', 'scale', 'custom', 'small', 'medium', 'large', 'premium')},
                         {'starter': 1, 'pro': 1, 'scale': 3, 'custom': 50,
                          'small': 1, 'medium': 1, 'large': 3, 'premium': 3})

    def test_agent_eval_rules_for_skips_location_rules(self):
        from .management.commands.agent_eval import _rules_for
        weekly_rules(self.client_obj, location=self.cag)
        self.assertEqual(_rules_for(self.client_obj), [])
        weekly_rules(self.client_obj, days=[0])
        self.assertEqual([r.location_id for r in _rules_for(self.client_obj)], [None])
