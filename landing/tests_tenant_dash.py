"""Phase 3b -- tenant reports dashboard: allowance in messages, value for the
price paid, analytics, and NEVER a DOMINIO cost figure for a tenant.

No model provider is called anywhere in this file.
"""
import math
from datetime import date, datetime, time
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import Client as HttpClient
from django.test import TestCase
from django.test.utils import CaptureQueriesContext, ContextList
from django.urls import reverse
from django.utils import timezone

from . import analytics, views
from . import usage as usage_ledger
from .models import (
    AvailabilityRule, Client, ContactSubmission, Conversation, Membership,
    Subscription, UsageEvent,
)

PW = 'pw12345!'
MODEL = 'claude-haiku-5-5'
PR = analytics.BUSINESS_TZ
# A DOMINIO cost no tenant may ever see, in any rendering.
SECRET_COST = Decimal('7.654321')
SECRET_COST_STRINGS = ('7.65', '7.654', '7,65')
# Context keys that carry DOMINIO's AI spend in the staff pages.
COST_KEYS = {'cost', 'est_cost', 'month_cost', 'vol_cost', 'cost_usd', 'cost4', 'margin',
             'per_client'}


def make_client(slug, **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', slug.title()), system_prompt='Vendemos cosas.',
        notify_email=f'owner@{slug}.com', **kw)


def make_sub(client, plan='pro', status='active', **kw):
    return Subscription.objects.create(client=client, plan=plan, status=status, **kw)


def chat_event(client, when=None, kind='chat', cost=SECRET_COST):
    return UsageEvent.objects.create(
        client=client, client_slug=client.slug, kind=kind, model=MODEL,
        input_tokens=100, output_tokens=20, api_calls=1, cost_usd=cost,
        created_at=when or timezone.now())


def make_conv(client, i, when=None, **kw):
    conv = Conversation.objects.create(client=client, widget_session=f'{client.slug}-sess-{i}',
                                       **kw)
    if when is not None:  # started_at is auto_now_add
        Conversation.objects.filter(pk=conv.pk).update(started_at=when)
    return conv


def make_lead(client, source='chat', page_url=''):
    return ContactSubmission.objects.create(
        client=client, name='Ana', email='ana@x.com', service='consulting',
        message='hola', source=source, page_url=page_url)


def pr(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=PR)


def login(username):
    http = HttpClient()
    assert http.login(username=username, password=PW)
    return http


def walk_for_cost(test, value, path='context'):
    """Fail if a DOMINIO cost key carries a value, or the secret cost appears."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in COST_KEYS:
                test.assertIsNone(v, f'{path}.{k} carries a cost for a tenant')
            walk_for_cost(test, v, f'{path}.{k}')
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            walk_for_cost(test, v, f'{path}[{i}]')
    elif isinstance(value, Decimal):
        test.assertNotEqual(value, SECRET_COST, f'{path} is the DOMINIO cost')


def flat_context(resp):
    contexts = resp.context if isinstance(resp.context, ContextList) else [resp.context]
    out = {}
    for ctx in contexts:
        out.update(ctx.flatten())
    return out


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        Client.objects.filter(slug='dominio').delete()
        User = get_user_model()
        self.staff = User.objects.create_user('staff@dominiopr.com', password=PW, is_staff=True)
        self.a = make_client('alfa', name='Alfa Café')
        self.b = make_client('bravo', name='Bravo Taller')
        self.user_a = User.objects.create_user('ana@alfa.com', password=PW)
        Membership.objects.create(user=self.user_a, client=self.a, role='agent')

    def url(self, **params):
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        return reverse('reports') + (f'?{query}' if query else '')


class ScopingTests(_Base):
    def setUp(self):
        super().setUp()
        make_conv(self.b, 1, topic='tema-secreto-bravo', intent='precios',
                  tagged_at=timezone.now(), page_url='https://bravo.com/pagina-bravo',
                  state='escalated', escalation_reason='motivo-secreto-bravo')
        make_lead(self.b, page_url='https://bravo.com/lead-bravo')
        chat_event(self.b)
        make_conv(self.a, 1, topic='tema-alfa', tagged_at=timezone.now())

    def test_tenant_never_sees_other_tenant_even_with_client_param(self):
        resp = login('ana@alfa.com').get(self.url(client='bravo'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['client_obj'], self.a)
        self.assertEqual(resp.context['client_options'], [('alfa', 'Alfa Café')])
        for secret in ('Bravo Taller', 'tema-secreto-bravo', 'pagina-bravo', 'lead-bravo',
                       'motivo-secreto-bravo'):
            self.assertNotContains(resp, secret)
        self.assertContains(resp, 'tema-alfa')
        self.assertEqual(resp.context['stats']['conversations'], 1)
        self.assertEqual(resp.context['allowance']['used'], 0)

    def test_user_with_several_agents_can_switch(self):
        Membership.objects.create(user=self.user_a, client=self.b, role='agent')
        http = login('ana@alfa.com')
        resp = http.get(self.url(client='bravo'))
        self.assertEqual(resp.context['client_obj'], self.b)
        self.assertContains(resp, 'tema-secreto-bravo')
        self.assertContains(resp, 'id="rep-client"')  # selector shown with 2+ agents
        self.assertEqual(http.get(self.url()).context['client_obj'], self.a)  # alphabetical

    def test_staff_can_view_any_client(self):
        resp = login('staff@dominiopr.com').get(self.url(client='bravo'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['client_obj'], self.b)
        self.assertContains(resp, 'tema-secreto-bravo')
        self.assertContains(resp, 'Operador')
        self.assertContains(resp, '$7.6543')  # staff keeps the real AI spend

    def test_user_without_membership_sees_empty_state(self):
        get_user_model().objects.create_user('nadie@x.com', password=PW)
        resp = login('nadie@x.com').get(self.url(client='alfa'))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.context['client_obj'])
        self.assertContains(resp, 'Todavía no tienes un agente asignado')
        self.assertNotContains(resp, 'Alfa Café')


class NoCostTests(_Base):
    def setUp(self):
        super().setUp()
        make_sub(self.a, plan='pro')
        make_conv(self.a, 1)
        make_lead(self.a)
        chat_event(self.a)
        chat_event(self.a, kind='greeting')

    def test_tenant_page_and_context_carry_no_dominio_cost(self):
        for period in ('month', 'last_month', '30d', '90d'):
            resp = login('ana@alfa.com').get(self.url(period=period))
            self.assertEqual(resp.status_code, 200)
            for text in SECRET_COST_STRINGS + ('Costo', 'costo IA', 'Margen'):
                self.assertNotContains(resp, text)
            ctx = flat_context(resp)
            self.assertIsNone(ctx['operator'])
            self.assertNotIn('cost', ctx['stats'])
            self.assertNotIn('cost', ctx['allowance'])
            self.assertNotIn('daily', ctx)  # the raw series has a per-day cost
            for bar in ctx['msg_bars'] + ctx['conv_bars']:
                self.assertEqual(set(bar), {'x', 'w', 'y', 'h', 'day', 'value'})
            walk_for_cost(self, ctx)

    def test_value_card_shows_price_paid_not_cost(self):
        resp = login('ana@alfa.com').get(self.url())
        self.assertContains(resp, 'Lo que pagas')
        self.assertContains(resp, '$299.00')
        self.assertContains(resp, 'Detalle técnico')
        self.assertEqual(resp.context['allowance']['tokens'], 240)


class AllowanceTests(_Base):
    def setUp(self):
        super().setUp()
        self.now = pr(2026, 10, 10, 15)
        self.start, self.end = usage_ledger.month_bounds(date(2026, 10, 10))

    def _events(self, client, n, when=None):
        for i in range(n):
            chat_event(client, when=when or pr(2026, 10, 2 + i % 7))

    def test_included_from_cap_times_days_and_projection(self):
        Client.objects.filter(pk=self.a.pk).update(daily_message_cap=10)
        self.a.refresh_from_db()
        self._events(self.a, 8)
        chat_event(self.a, kind='greeting', when=pr(2026, 10, 3))      # not a message
        chat_event(self.a, when=pr(2026, 9, 30, 23))                     # previous month
        with patch('django.utils.timezone.now', return_value=self.now):
            cache.set(analytics.today_cap_key('alfa'), 9)
            got = analytics.allowance(self.a, self.start, self.end)
        self.assertEqual((got['used'], got['days'], got['elapsed']), (8, 31, 10))
        self.assertEqual(got['included'], 310)
        self.assertEqual(got['pct'], 3)
        self.assertEqual(got['projection'], 25)          # 8 / 10 days * 31
        self.assertFalse(got['warn'])
        self.assertEqual((got['today'], got['today_cap'], got['today_pct']), (9, 10, 90))
        self.assertTrue(got['today_warn'])

    def test_warn_at_80_percent(self):
        Client.objects.filter(pk=self.a.pk).update(daily_message_cap=1)
        self.a.refresh_from_db()
        self._events(self.a, 24)
        with patch('django.utils.timezone.now', return_value=self.now):
            got = analytics.allowance(self.a, self.start, self.end)
            self.assertEqual((got['included'], got['pct'], got['warn']), (31, 77, False))
            chat_event(self.a, when=pr(2026, 10, 9))
            got = analytics.allowance(self.a, self.start, self.end)
        self.assertEqual((got['pct'], got['warn'], got['bar_pct']), (81, True, 81))
        self.assertTrue(got['projection_warn'])

    def test_plan_monthly_messages_wins_when_defined(self):
        make_sub(self.a, plan='pro')
        self.a = Client.objects.select_related('subscription').get(pk=self.a.pk)
        self._events(self.a, 5)
        with patch.dict(views.PLAN_LIMITS['pro'], {'monthly_messages': 1000}), \
                patch('django.utils.timezone.now', return_value=self.now):
            got = analytics.allowance(self.a, self.start, self.end)
        self.assertEqual((got['included'], got['pct']), (1000, 0))

    def test_past_month_is_fully_elapsed(self):
        self._events(self.a, 6, when=pr(2026, 9, 15))
        start, end = usage_ledger.month_bounds(date(2026, 9, 15))
        with patch('django.utils.timezone.now', return_value=self.now):
            got = analytics.allowance(self.a, start, end)
        self.assertEqual((got['used'], got['elapsed'], got['projection']), (6, 30, 6))

    def test_zero_cap_has_no_pct(self):
        Client.objects.filter(pk=self.a.pk).update(daily_message_cap=0)
        self.a.refresh_from_db()
        with patch('django.utils.timezone.now', return_value=self.now):
            got = analytics.allowance(self.a, self.start, self.end)
        self.assertEqual((got['included'], got['pct'], got['warn']), (0, None, False))

    def test_page_uses_warn_style_at_80_percent(self):
        Client.objects.filter(pk=self.a.pk).update(daily_message_cap=1)
        start, end = usage_ledger.month_bounds()
        days = analytics._local_days(start, end)
        for _ in range(math.ceil(days * 0.8)):
            chat_event(self.a)
        resp = login('ana@alfa.com').get(self.url())
        self.assertTrue(resp.context['allowance']['warn'])
        self.assertContains(resp, 'rep-fill--warn')
        self.assertContains(resp, 'Llevas <b>')

    def test_page_without_warn_below_80(self):
        chat_event(self.a)
        resp = login('ana@alfa.com').get(self.url())
        self.assertFalse(resp.context['allowance']['warn'])
        self.assertNotContains(resp, 'rep-fill--warn')


class ValueTests(_Base):
    def setUp(self):
        super().setUp()
        self.start, self.end = usage_ledger.month_bounds()

    def _value(self):
        client = Client.objects.select_related('subscription').get(pk=self.a.pk)
        return analytics.value_metrics(client, self.start, self.end)

    def test_price_per_lead_and_conversation(self):
        make_sub(self.a, plan='pro')
        make_conv(self.a, 1)
        make_conv(self.a, 2)
        make_lead(self.a)
        make_lead(self.a, source='contact_form')  # not captured by the agent
        v = self._value()
        self.assertEqual(v['price'], Decimal('299.00'))
        self.assertEqual((v['conversations'], v['leads']), (2, 1))
        self.assertEqual(v['cost_per_lead'], Decimal('299.00'))
        self.assertEqual(v['cost_per_conversation'], Decimal('149.50'))

    def test_zero_leads_shows_dash(self):
        make_sub(self.a, plan='starter')
        make_conv(self.a, 1)
        v = self._value()
        self.assertIsNone(v['cost_per_lead'])
        self.assertEqual(v['cost_per_conversation'], Decimal('99.00'))
        resp = login('ana@alfa.com').get(self.url())
        self.assertContains(resp, '<b>—</b> por lead', html=False)

    def test_unknown_price_hides_card(self):
        make_sub(self.a, plan='custom')  # custom with no override = unknown price
        self.assertIsNone(self._value()['price'])
        resp = login('ana@alfa.com').get(self.url())
        self.assertNotContains(resp, 'Lo que pagas')

    def test_no_paid_subscription_hides_card(self):
        self.assertIsNone(self._value()['price'])
        make_sub(self.a, plan='pro', status='canceled')
        self.assertIsNone(self._value()['price'])
        self.assertNotContains(login('ana@alfa.com').get(self.url()), 'Lo que pagas')

    def test_rolling_period_prorates_price(self):
        make_sub(self.a, plan='pro')
        _key, start, end = analytics.period_bounds('90d')
        client = Client.objects.select_related('subscription').get(pk=self.a.pk)
        v = analytics.value_metrics(client, start, end)
        self.assertEqual((v['months'], v['paid']), (Decimal('3.00'), Decimal('897.00')))
        _key, start, end = analytics.period_bounds('last_month')
        self.assertEqual(analytics.value_metrics(client, start, end)['months'], 1)

    def test_override_price_used(self):
        make_sub(self.a, plan='custom', monthly_price_cents=12_000)
        for i in range(4):
            make_conv(self.a, i)
        self.assertEqual(self._value()['cost_per_conversation'], Decimal('30.00'))


class AfterHoursTests(_Base):
    # 2026-10-05 is a Monday, 2026-10-10 a Saturday.
    def setUp(self):
        super().setUp()
        self.start, self.end = pr(2026, 10, 1, 0), pr(2026, 11, 1, 0)
        for client in (self.a, self.b):
            make_conv(client, 1, when=pr(2026, 10, 5, 10))      # Mon 10:00
            make_conv(client, 2, when=pr(2026, 10, 5, 20))      # Mon 20:00
            make_conv(client, 3, when=pr(2026, 10, 10, 11))     # Sat 11:00
            make_conv(client, 4, when=pr(2026, 10, 6, 17, 0))   # Tue 17:00 (closing)

    def test_default_hours_without_rules(self):
        with self.assertNumQueries(2):
            got = analytics.after_hours([self.a.pk], self.start, self.end)
        self.assertEqual(got, {self.a.pk: 3})

    def test_rules_override_default_per_client(self):
        AvailabilityRule.objects.create(client=self.b, weekday=0, open_time=time(9),
                                        close_time=time(12))
        AvailabilityRule.objects.create(client=self.b, weekday=5, open_time=time(9),
                                        close_time=time(13))
        with self.assertNumQueries(2):
            got = analytics.after_hours([self.a.pk, self.b.pk], self.start, self.end)
        # Bravo: Mon 10 in, Mon 20 out, Sat 11 in, Tue closed all day (no rule).
        self.assertEqual(got, {self.a.pk: 3, self.b.pk: 2})

    def test_utc_instant_is_judged_in_puerto_rico_time(self):
        # 13:30 UTC on a Monday is 9:30 in Puerto Rico: inside default hours.
        moment = datetime(2026, 10, 5, 13, 30, tzinfo=dt_timezone.utc)
        self.assertFalse(analytics.is_after_hours(moment, analytics.DEFAULT_HOURS))
        moment = datetime(2026, 10, 5, 11, 30, tzinfo=dt_timezone.utc)  # 7:30 PR
        self.assertTrue(analytics.is_after_hours(moment, analytics.DEFAULT_HOURS))

    def test_page_shows_after_hours_and_hours_used(self):
        resp = login('ana@alfa.com').get(self.url(period='90d'))
        # The fixed dates may fall outside "now - 90 days" depending on when the
        # suite runs; the count itself is covered by the function tests above.
        self.assertIn('after_hours', resp.context['stats'])
        self.assertContains(resp, 'Fuera de horario')
        self.assertContains(resp, 'Horario por defecto')
        AvailabilityRule.objects.create(client=self.a, weekday=2, open_time=time(9),
                                        close_time=time(18))
        resp = login('ana@alfa.com').get(self.url())
        self.assertTrue(resp.context['custom_hours'])
        self.assertContains(resp, 'Tu horario configurado')


class ClinicTests(_Base):
    def test_clinic_hides_topics(self):
        Client.objects.filter(pk=self.a.pk).update(clinic_mode=True)
        # Tagged before clinic mode was switched on.
        make_conv(self.a, 1, topic='tema-guardado-antes', intent='cita',
                  tagged_at=timezone.now())
        resp = login('ana@alfa.com').get(self.url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['topics'], [])
        self.assertNotContains(resp, 'tema-guardado-antes')
        self.assertContains(resp, 'En modo clínica no se guardan temas')

    def test_non_clinic_shows_topics(self):
        make_conv(self.a, 1, topic='horario de verano', tagged_at=timezone.now())
        resp = login('ana@alfa.com').get(self.url())
        self.assertContains(resp, 'horario de verano')


class QueryCountTests(_Base):
    def _count(self, http, url):
        http.get(url)  # warm session/auth caches
        with CaptureQueriesContext(connection) as ctx:
            resp = http.get(url)
        self.assertEqual(resp.status_code, 200)
        return len(ctx.captured_queries)

    def _activity(self, client, i):
        make_conv(client, i, topic=f'tema {i}', intent='precios', tagged_at=timezone.now(),
                  page_url=f'https://x.com/p{i}')
        make_lead(client, page_url=f'https://x.com/p{i}')
        chat_event(client)

    def test_tenant_page_constant_for_1_vs_10_conversations(self):
        make_sub(self.a)
        self._activity(self.a, 0)
        http = login('ana@alfa.com')
        one = self._count(http, self.url())
        for i in range(1, 10):
            self._activity(self.a, i)
        self.assertEqual(self._count(http, self.url()), one)

    def test_staff_page_constant_for_1_vs_10_clients(self):
        self._activity(self.a, 0)
        http = login('staff@dominiopr.com')
        one = self._count(http, self.url(client='alfa'))
        for i in range(10):
            c = make_client(f'extra{i}')
            make_sub(c)
            self._activity(c, i)
        self.assertEqual(self._count(http, self.url(client='alfa')), one)
