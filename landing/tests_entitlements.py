"""Phase C -- monthly entitlements, allowance tracking and overage billing.

No model provider and no Stripe endpoint is ever called: agent.answer is a
double and payments._request is mocked.
"""
import itertools
import json
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import agent, analytics, entitlements, payments, turns, views
from . import usage as usage_ledger
from .models import AuditEvent, ChatMessage, Client, Conversation, Membership, Subscription

PR = entitlements.BUSINESS_TZ
PW = 'pw12345!'
NOW = datetime(2026, 10, 10, 15, 0, tzinfo=PR)        # day 10 of a 31-day month
USAGE = {'input_tokens': 100, 'output_tokens': 50, 'cache_read_input_tokens': 0,
         'cache_creation_input_tokens': 0, 'model': 'claude-haiku-5-5',
         'api_calls': 1, 'effort': 'low'}
_ids = itertools.count(1)


def pr(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=PR)


def make_client(slug, **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', slug.title()), system_prompt='Vendemos cosas.',
        notify_email=f'owner@{slug}.com', **kw)


def make_sub(client, plan='medium', step=1, status='active', updated_at=None, **kw):
    """updated_at defaults to BEFORE the months the tests bill (a plan or
    step change inside the billed month routes the client to manual review)."""
    sub = Subscription.objects.create(client=client, plan=plan, step=step, status=status, **kw)
    Subscription.objects.filter(pk=sub.pk).update(updated_at=updated_at or pr(2026, 9, 1))
    sub.refresh_from_db()
    return sub


def fresh(client):
    return Client.objects.select_related('subscription').get(pk=client.pk)


def _reply(conversations):
    """One assistant turn per conversation: what makes it a billable unit."""
    ChatMessage.objects.bulk_create([
        ChatMessage(conversation=c, role='assistant', content='Claro.', position=1)
        for c in conversations])


def make_conv(client, when=None, channel='web', answered=True):
    i = next(_ids)
    if channel == 'web':
        conv = Conversation.objects.create(client=client, widget_session=f'{client.slug}-s-{i}')
    else:
        conv = Conversation.objects.create(client=client, channel=channel,
                                           external_id=f'{channel}-{i}')
    if when is not None:  # started_at is auto_now_add
        Conversation.objects.filter(pk=conv.pk).update(started_at=when)
    if answered:
        _reply([conv])
    return conv


def convs(client, n, when, channel='web', answered=True):
    """n conversations started at `when` (bulk), each with an assistant
    reply unless answered=False (opened but never answered: not billable)."""
    rows = []
    for _ in range(n):
        i = next(_ids)
        if channel == 'web':
            rows.append(Conversation(client=client, widget_session=f'{client.slug}-b-{i}'))
        else:
            rows.append(Conversation(client=client, channel=channel, external_id=f'{channel}-{i}'))
    created = Conversation.objects.bulk_create(rows)
    Conversation.objects.filter(pk__in=[c.pk for c in created]).update(started_at=when)
    if answered:
        _reply(created)
    return created


def fake_answer(reply='Claro.'):
    def _answer(history, business_prompt=None, handlers=None, language='es', **kwargs):
        return reply, dict(USAGE)
    return _answer


def login(username):
    http = HttpClient()
    assert http.login(username=username, password=PW)
    return http


def member(client, username, role='org_admin', staff=False):
    user = get_user_model().objects.create_user(username, password=PW, is_staff=staff)
    Membership.objects.create(user=user, client=client, role=role)
    return user


# ------------------------------------------------------------------
# Plan tables
# ------------------------------------------------------------------

class PlanTableTests(TestCase):
    def test_packages_exist_but_are_not_public(self):
        self.assertEqual([p['id'] for p in views.PUBLIC_PLANS], ['starter', 'pro', 'scale', 'custom'])
        for pid in ('small', 'medium', 'large', 'premium'):
            self.assertIn(pid, views.PLAN_BY_ID)
            self.assertFalse(views.PLAN_BY_ID[pid]['public'])
            self.assertEqual(len(views.PLAN_BY_ID[pid]['steps']), 3)
        choices = dict(Subscription.PLAN_CHOICES)
        for p in views.PLANS:
            self.assertIn(p['id'], choices)

    def test_plan_limits_resolve_the_step(self):
        s1, s2, s3 = (views.plan_limits('small', n) for n in (1, 2, 3))
        self.assertEqual((s1['monthly_conversations'], s2['monthly_conversations'],
                          s3['monthly_conversations']), (150, 300, 450))
        self.assertEqual((s1['unit'], s1['channels'], s1['seats'], s1['locations']),
                         ('conversations', {'web'}, 0, 1))
        self.assertEqual(s1['overage']['web_block'], (100, 1500))
        # 150 conv / 30 days * 3 * 6 messages = 90 messages/day abuse ceiling
        self.assertEqual(s1['daily_message_cap'], 90)
        # Step is clamped, never an IndexError.
        self.assertEqual(views.plan_limits('small', 0)['step'], 1)
        self.assertEqual(views.plan_limits('small', 9)['step'], 3)
        large = views.plan_limits('large', 3)
        self.assertEqual((large['seats'], large['locations'], large['channels']),
                         (15, 3, {'web', 'internal'}))
        premium = views.plan_limits('premium', 3)
        self.assertEqual((premium['wa_conversations'], premium['seats']), (2000, 25))
        self.assertIn('whatsapp', premium['channels'])

    def test_launch_plans_are_unchanged(self):
        # Phase F added the explicit 'locations' ceiling (1 = as before).
        self.assertEqual(views.plan_limits('pro'),
                         {'daily_message_cap': 500, 'enable_bookings': True, 'unit': 'messages',
                          'locations': 1})
        self.assertEqual(views.PLAN_LIMITS['starter']['daily_message_cap'], 200)
        self.assertEqual(views.plan_limits('nope')['unit'], 'messages')
        # Top level of a package mirrors step 1 for the provisioning code.
        self.assertEqual(views.PLAN_LIMITS['medium']['daily_message_cap'],
                         views.plan_limits('medium', 1)['daily_message_cap'])

    def test_plan_pricing_follows_the_step(self):
        self.assertEqual(views.plan_pricing('small', 2)['monthly'], 74.99)
        self.assertEqual(views.plan_pricing('small', 2)['annual'], 749.9)
        self.assertEqual(views.plan_pricing('large', 3)['monthly'], 599)
        self.assertEqual(views.plan_pricing('pro', 3)['monthly'], 299)   # no steps: ignored
        self.assertIsNone(views.plan_pricing('nope'))
        self.assertEqual(views.PLAN_BY_ID['medium']['monthly'], 149)   # step 1 at the top

    def test_revenue_uses_the_step(self):
        c = make_client('rev')
        sub = make_sub(c, plan='large', step=2)
        self.assertEqual(analytics.plan_monthly_revenue(sub), Decimal('449.00'))
        sub.step, sub.period = 3, 'annual'
        self.assertEqual(analytics.plan_monthly_revenue(sub), Decimal('499.17'))  # 5990/12

    def test_pricing_page_shows_only_public_plans_and_the_disclaimer(self):
        resp = HttpClient().get(reverse('get_started'))
        self.assertContains(resp, 'Starter')
        self.assertNotContains(resp, 'Chat Básico')
        self.assertNotContains(resp, 'data-plan="small"')
        self.assertContains(resp, 'el agente sigue atendiendo y se cobran bloques adicionales')

    def test_pricing_form_rejects_a_hidden_plan(self):
        resp = HttpClient().post(reverse('get_started'), {
            'company': 'Acme', 'name': 'Ana', 'email': 'ana@example.com', 'plan': 'small'})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('plan', resp.context['errors'])

    def test_terms_carry_the_overage_clause(self):
        resp = HttpClient().get(reverse('terms'))
        self.assertContains(resp, 'Conversaciones adicionales (cobro por exceso)')
        self.assertContains(resp, '100 conversaciones web a $15')


# ------------------------------------------------------------------
# entitlements()
# ------------------------------------------------------------------

class EntitlementTests(TestCase):
    def test_client_without_subscription_is_a_messages_plan(self):
        ent = entitlements.entitlements(make_client('solo'))
        self.assertEqual((ent['unit'], ent['monthly_conversations'], ent['plan']),
                         ('messages', None, ''))

    def test_package_step_merged_with_overrides(self):
        c = make_client('deal', entitlement_overrides={
            'monthly_conversations': 1200, 'seats': 3, 'bogus': 1, 'locations': 'x',
            'channels': ['web', 'whatsapp', 'fax'], 'wa_conversations': 100})
        make_sub(c, plan='medium', step=2)
        ent = entitlements.entitlements(fresh(c))
        self.assertEqual(ent['plan'], 'medium')
        self.assertEqual(ent['step'], 2)
        self.assertEqual(ent['monthly_conversations'], 1200)   # override over 1000
        self.assertEqual(ent['seats'], 3)
        self.assertEqual(ent['locations'], 1)                  # bad value ignored
        self.assertEqual(ent['channels'], {'web', 'whatsapp'})  # unknown channel dropped
        self.assertNotIn('bogus', ent)

    def test_override_gives_an_old_plan_a_monthly_capacity(self):
        c = make_client('oldie', entitlement_overrides={'monthly_conversations': 800})
        make_sub(c, plan='pro')
        ent = entitlements.entitlements(fresh(c))
        self.assertEqual((ent['unit'], ent['monthly_conversations']), ('conversations', 800))

    def test_malformed_overrides_never_zero_the_plan(self):
        c = make_client('typo', entitlement_overrides={'monthly_conversations': None})
        make_sub(c, plan='small', step=3)
        self.assertEqual(entitlements.entitlements(fresh(c))['monthly_conversations'], 450)


# ------------------------------------------------------------------
# month_usage / allowance_status
# ------------------------------------------------------------------

class MonthUsageTests(TestCase):
    def setUp(self):
        self.c = make_client('uso')
        make_sub(self.c, plan='medium')
        self.other = make_client('otro')

    def test_counts_conversations_started_in_the_pr_month_only(self):
        make_conv(self.c, when=pr(2026, 9, 30, 23, 59))   # previous month (PR)
        make_conv(self.c, when=pr(2026, 10, 1, 0, 0))     # first second of the month
        make_conv(self.c, when=pr(2026, 10, 31, 23, 59))  # last minute
        make_conv(self.c, when=pr(2026, 11, 1, 0, 0))     # next month
        make_conv(self.other, when=pr(2026, 10, 5))       # another tenant
        with patch('django.utils.timezone.now', return_value=NOW):
            got = entitlements.month_usage(self.c)
        self.assertEqual(got['conversations'], 2)
        self.assertEqual(got['start'], pr(2026, 10, 1, 0, 0))
        self.assertEqual(got['end'], pr(2026, 11, 1, 0, 0))
        # Explicit month: a date or an aware datetime inside it.
        self.assertEqual(entitlements.month_usage(self.c, date(2026, 9, 3))['conversations'], 1)
        self.assertEqual(entitlements.month_usage(self.c, pr(2026, 11, 20))['conversations'], 1)

    def test_all_channels_counted_and_split(self):
        when = pr(2026, 10, 4)
        convs(self.c, 2, when)
        convs(self.c, 1, when, channel='whatsapp')
        convs(self.c, 1, when, channel='internal')
        member(self.c, 'admin@uso.com', role='org_admin')
        member(self.c, 'agent@uso.com', role='agent')
        member(self.c, 'emp@uso.com', role='employee')
        got = entitlements.month_usage(self.c, when)
        self.assertEqual((got['conversations'], got['web'], got['whatsapp'], got['internal']),
                         (4, 2, 1, 1))
        self.assertEqual(got['seats_used'], 1)

    def test_month_usage_many_has_an_entry_per_client(self):
        got = entitlements.month_usage_many([self.c.pk, self.other.pk], date(2026, 10, 1))
        self.assertEqual(set(got), {self.c.pk, self.other.pk})
        self.assertEqual(got[self.other.pk]['conversations'], 0)


class AllowanceStatusTests(TestCase):
    def setUp(self):
        self.c = make_client('alfa')
        make_sub(self.c, plan='medium')          # 500 conversations / month
        self.c = fresh(self.c)

    def status(self, client=None):
        with patch('django.utils.timezone.now', return_value=NOW):
            return entitlements.allowance_status(client or self.c)

    def test_math_and_projection(self):
        convs(self.c, 120, pr(2026, 10, 5))
        got = self.status()
        self.assertEqual((got['unit'], got['included'], got['used'], got['pct']),
                         ('conversations', 500, 120, 24))
        self.assertEqual((got['days'], got['elapsed']), (31, 10))
        self.assertEqual(got['projection'], 372)        # 120 / 10 * 31
        self.assertEqual(got['projection_pct'], 74)
        self.assertEqual(got['remaining'], 380)
        self.assertEqual((got['overage_units'], got['overage_blocks'], got['overage_cents']), (0, 0, 0))
        self.assertFalse(got['billable'])
        self.assertFalse(got['warn'])
        self.assertEqual(got['month_label'], '2026-10')
        self.assertEqual((got['block_size'], got['block_price']), (100, '$15'))

    def test_overage_blocks_round_up(self):
        c = make_client('beta')
        make_sub(c, plan='small')                # 150 included
        c = fresh(c)
        convs(c, 351, pr(2026, 10, 3))           # 201 over -> 3 blocks of 100
        got = self.status(c)
        self.assertEqual((got['overage_units'], got['overage_blocks'], got['overage_cents']),
                         (201, 3, 4500))
        self.assertEqual((got['overage_price'], got['total_overage_price']), ('$45', '$45'))
        self.assertTrue(got['reached'] and got['warn'] and got['billable'])
        self.assertEqual(got['bar_pct'], 100)
        self.assertEqual(entitlements.overage_items(got),
                         [('web_block', 3, 4500, 'DOMINIO — 300 conversaciones adicionales (mes 2026-10)')])

    def test_one_conversation_over_is_one_block(self):
        c = fresh(make_client('gamma'))
        make_sub(c, plan='small')
        convs(fresh(c), 151, pr(2026, 10, 3))
        got = self.status(fresh(c))
        self.assertEqual((got['overage_units'], got['overage_blocks'], got['overage_cents']), (1, 1, 1500))

    def test_messages_plan_has_no_overage(self):
        c = make_client('viejo')
        make_sub(c, plan='pro')
        convs(c, 40, pr(2026, 10, 3))
        got = self.status(fresh(c))
        self.assertEqual((got['unit'], got['included'], got['pct']), ('messages', None, None))
        self.assertEqual((got['overage_cents'], got['total_overage_cents']), (0, 0))
        self.assertFalse(got['billable'])
        self.assertEqual(got['all_conversations'], 40)
        self.assertEqual(entitlements.overage_items(got), [])

    def test_whatsapp_bucket_only_where_sold(self):
        p = make_client('prem')
        make_sub(p, plan='premium')              # 4000 web, 500 WhatsApp
        p = fresh(p)
        convs(p, 10, pr(2026, 10, 3))
        convs(p, 600, pr(2026, 10, 3), channel='whatsapp')
        got = self.status(p)
        self.assertEqual(got['used'], 10)        # WhatsApp is not web usage
        self.assertEqual((got['wa']['included'], got['wa']['used'], got['wa']['overage_blocks'],
                          got['wa']['overage_cents']), (500, 600, 1, 14900))
        self.assertEqual(got['total_overage_cents'], 14900)
        kinds = [item[0] for item in entitlements.overage_items(got)]
        self.assertEqual(kinds, ['wa_block'])
        # Medium does not sell WhatsApp: nothing is ever billed on that bucket.
        convs(self.c, 5, pr(2026, 10, 3), channel='whatsapp')
        got = self.status()
        self.assertIsNone(got['wa']['included'])
        self.assertEqual(got['wa']['overage_cents'], 0)

    def test_extra_seats(self):
        c = make_client('team')
        make_sub(c, plan='large')                # 5 seats
        for i in range(7):
            member(c, f'emp{i}@team.com', role='employee')
        member(c, 'boss@team.com', role='org_admin')
        got = self.status(fresh(c))
        self.assertEqual((got['seats']['included'], got['seats']['used'], got['seats']['extra'],
                          got['seats']['cents']), (5, 7, 2, 1800))
        self.assertEqual(got['total_overage_cents'], 1800)
        self.assertEqual(entitlements.overage_items(got)[0][:3], ('seat', 2, 1800))

    def test_analytics_allowance_switches_unit_per_plan(self):
        convs(self.c, 100, pr(2026, 10, 3))
        start, end = usage_ledger.month_bounds(date(2026, 10, 10))
        with patch('django.utils.timezone.now', return_value=NOW):
            got = analytics.allowance(self.c, start, end)
        self.assertEqual((got['unit'], got['unit_label'], got['included'], got['used'], got['pct']),
                         ('conversations', 'conversaciones', 500, 100, 20))
        self.assertEqual(got['overage']['block_price'], '$15')
        old = make_client('viejo')
        make_sub(old, plan='pro')
        Client.objects.filter(pk=old.pk).update(daily_message_cap=10)
        with patch('django.utils.timezone.now', return_value=NOW):
            got = analytics.allowance(fresh(old), start, end)
        self.assertEqual((got['unit'], got['included'], got['used']), ('messages', 310, 0))
        self.assertIsNone(got['overage'])


# ------------------------------------------------------------------
# 80% / 100% notifications
# ------------------------------------------------------------------

@override_settings(CONTACT_NOTIFY_EMAIL='ops@dominiopr.com', SITE_URL='https://dominiopr.com')
class ThresholdTests(TestCase):
    def setUp(self):
        cache.clear()
        self.c = make_client('aviso', name='Aviso Café')
        make_sub(self.c, plan='small')           # 150 included
        self.c = fresh(self.c)

    def check(self):
        with patch('django.utils.timezone.now', return_value=NOW):
            return entitlements.check_thresholds(self.c)

    def test_80_percent_fires_once_with_both_emails_and_an_audit_record(self):
        convs(self.c, 120, pr(2026, 10, 3))
        self.assertEqual(self.check(), [80])
        self.assertEqual(len(mail.outbox), 2)
        to_client = next(m for m in mail.outbox if m.to == ['owner@aviso.com'])
        to_ops = next(m for m in mail.outbox if m.to == ['ops@dominiopr.com'])
        self.assertIn('80%', to_client.subject)
        self.assertIn('120 de 150 conversaciones', to_client.body)
        self.assertIn('bloques adicionales de 100 conversaciones a $15', to_client.body)
        self.assertIn('No cortamos el servicio', to_client.body)
        self.assertIn('/dashboard/reports/', to_client.body)
        html = to_client.alternatives[0][0]
        self.assertIn('$15', html)
        self.assertIn('aviso', to_ops.subject)
        self.assertEqual(AuditEvent.objects.filter(client=self.c, action='allowance.80',
                                                   target='2026-10').count(), 1)
        self.assertFalse(AuditEvent.objects.filter(action='allowance.100').exists())
        # Same month, more conversations below 100%: nothing new.
        convs(self.c, 10, pr(2026, 10, 8))
        self.assertEqual(self.check(), [])
        self.assertEqual(len(mail.outbox), 2)

    def test_100_percent_fires_once_even_without_the_cache(self):
        convs(self.c, 150, pr(2026, 10, 3))
        self.assertEqual(self.check(), [80, 100])     # both crossed at once
        self.assertEqual(len(mail.outbox), 4)
        full = next(m for m in mail.outbox if m.to == ['owner@aviso.com'] and 'ya usó' in m.subject)
        self.assertIn('se cobran a fin de mes en bloques adicionales de 100 conversaciones a $15',
                      full.body)
        cache.clear()                                   # the AuditEvent is the durable dedupe
        convs(self.c, 30, pr(2026, 10, 9))
        self.assertEqual(self.check(), [])
        self.assertEqual(len(mail.outbox), 4)
        self.assertEqual(AuditEvent.objects.filter(action__startswith='allowance.').count(), 2)

    def test_next_month_fires_again(self):
        convs(self.c, 150, pr(2026, 10, 3))
        self.check()
        convs(self.c, 150, pr(2026, 11, 2))
        with patch('django.utils.timezone.now', return_value=pr(2026, 11, 5)):
            self.assertEqual(entitlements.check_thresholds(self.c), [80, 100])
        self.assertEqual(AuditEvent.objects.filter(action='allowance.100').count(), 2)

    def test_below_threshold_and_messages_plans_send_nothing(self):
        convs(self.c, 100, pr(2026, 10, 3))            # 66%
        self.assertEqual(self.check(), [])
        old = make_client('viejo')
        make_sub(old, plan='pro')
        convs(old, 900, pr(2026, 10, 3))
        with patch('django.utils.timezone.now', return_value=NOW):
            self.assertEqual(entitlements.check_thresholds(fresh(old)), [])
        self.assertEqual(len(mail.outbox), 0)

    def test_run_turn_hooks_the_check_on_the_first_turn_only(self):
        conv = make_conv(self.c, answered=False)
        with patch.object(agent, 'answer', side_effect=fake_answer()), \
                patch.object(entitlements, 'check_thresholds', return_value=[]) as check:
            turns.run_turn(self.c, conv, 'hola', channel='web', handlers={})
            turns.run_turn(self.c, conv, 'otra', channel='web', handlers={})
        self.assertEqual(check.call_count, 1)
        self.assertEqual(ChatMessage.objects.filter(conversation=conv).count(), 4)

    def test_a_failing_check_never_blocks_the_reply(self):
        conv = make_conv(self.c, answered=False)
        with patch.object(agent, 'answer', side_effect=fake_answer('Dime.')), \
                patch.object(entitlements, 'check_thresholds', side_effect=RuntimeError('boom')):
            result = turns.run_turn(self.c, conv, 'hola', channel='web', handlers={})
        self.assertEqual(result.reply, 'Dime.')
        self.assertEqual(ChatMessage.objects.filter(conversation=conv).count(), 2)

    def test_crossing_through_a_real_turn_sends_the_warning(self):
        convs(self.c, 119, pr(2026, 10, 3))
        conv = make_conv(self.c, when=pr(2026, 10, 10, 14), answered=False)   # the 120th: 80%
        with patch.object(agent, 'answer', side_effect=fake_answer()), \
                patch('django.utils.timezone.now', return_value=NOW):
            turns.run_turn(self.c, conv, 'hola', channel='web', handlers={})
        self.assertEqual(sorted(m.to[0] for m in mail.outbox),
                         ['ops@dominiopr.com', 'owner@aviso.com'])
        self.assertTrue(AuditEvent.objects.filter(client=self.c, action='allowance.80').exists())


# ------------------------------------------------------------------
# bill_overage
# ------------------------------------------------------------------

@override_settings(CONTACT_NOTIFY_EMAIL='ops@dominiopr.com', STRIPE_SECRET_KEY='sk_test_x',
                   STRIPE_TAX_RATE_ID='')
class BillOverageTests(TestCase):
    RUN_AT = pr(2026, 11, 1, 3, 10)

    def setUp(self):
        cache.clear()
        oct3 = pr(2026, 10, 3)
        self.stripe_m = make_client('mensual')
        make_sub(self.stripe_m, plan='small', method='stripe', stripe_customer_id='cus_m',
                 stripe_subscription_id='sub_m', order_number='DOM-2026-0001')
        convs(self.stripe_m, 351, oct3)                       # 150 incl -> 3 blocks = $45
        self.manual = make_client('manual')
        make_sub(self.manual, plan='medium', method='manual')
        convs(self.manual, 520, oct3)                         # 500 incl -> 1 block, by hand
        self.under = make_client('bajo')
        make_sub(self.under, plan='medium', method='stripe', stripe_customer_id='cus_u',
                 stripe_subscription_id='sub_u')
        convs(self.under, 10, oct3)
        self.old = make_client('viejo')
        make_sub(self.old, plan='pro', method='stripe', stripe_customer_id='cus_o',
                 stripe_subscription_id='sub_o')
        convs(self.old, 60, oct3)                             # messages plan: never billed
        self.annual = make_client('anual')
        make_sub(self.annual, plan='small', step=2, period='annual', method='stripe',
                 stripe_customer_id='cus_a', stripe_subscription_id='sub_a')
        convs(self.annual, 301, oct3)                         # 300 incl -> 1 block
        self.canceled = make_client('ido')
        make_sub(self.canceled, plan='small', method='stripe', status='canceled',
                 stripe_customer_id='cus_c', stripe_subscription_id='sub_c')
        convs(self.canceled, 200, oct3)                       # no live sub: by hand

    def run_cmd(self, *args):
        out, err = StringIO(), StringIO()
        with patch('django.utils.timezone.now', return_value=self.RUN_AT):
            call_command('bill_overage', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    @staticmethod
    def posts(req):
        return [c for c in req.call_args_list if c.args[0] == 'POST']

    @staticmethod
    def stripe(method, path, data=None, headers=None):
        """A Stripe double: ids by object type, empty listings."""
        if method == 'GET':
            return {'object': 'list', 'data': []}
        if path == '/v1/invoices':
            return {'id': 'in_x', 'status': 'draft'}
        if path.endswith('/finalize'):
            return {'id': path.split('/')[3], 'status': 'open'}
        return {'id': 'ii_1'}

    def test_posts_invoice_items_with_idempotency_and_records_them(self):
        with patch.object(payments, '_request', side_effect=self.stripe) as req:
            out, err = self.run_cmd('--month', '2026-10')
        posts = self.posts(req)
        by_path = {}
        for c in posts:
            by_path.setdefault(c.args[1], []).append(c)
        items = by_path['/v1/invoiceitems']
        self.assertEqual(len(items), 2)          # mensual + anual; nobody else
        monthly = next(c for c in items if c.args[2]['customer'] == 'cus_m')
        self.assertEqual(monthly.args[2]['amount'], '4500')
        self.assertEqual(monthly.args[2]['currency'], 'usd')
        self.assertEqual(monthly.args[2]['subscription'], 'sub_m')
        self.assertNotIn('invoice', monthly.args[2])
        self.assertEqual(monthly.args[2]['description'],
                         'DOMINIO — 300 conversaciones adicionales (mes 2026-10)')
        self.assertEqual(monthly.args[2]['metadata[client]'], 'mensual')
        self.assertEqual(monthly.args[2]['metadata[month]'], '2026-10')
        self.assertEqual(monthly.args[2]['metadata[kind]'], 'web_block')
        self.assertEqual(monthly.kwargs['headers'],
                         {'Idempotency-Key': f'overage:{self.stripe_m.pk}:2026-10:web_block'})
        # Annual: the draft invoice exists BEFORE its item, the item is
        # created straight onto it, and the invoice is finalized last.
        annual = next(c for c in items if c.args[2]['customer'] == 'cus_a')
        self.assertEqual(annual.args[2]['amount'], '1500')
        self.assertNotIn('subscription', annual.args[2])
        self.assertEqual(annual.args[2]['invoice'], 'in_x')
        invoices = by_path['/v1/invoices']
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices[0].args[2]['customer'], 'cus_a')
        self.assertEqual(invoices[0].args[2]['pending_invoice_items_behavior'], 'exclude')
        self.assertEqual(invoices[0].args[2]['auto_advance'], 'true')
        self.assertEqual(invoices[0].args[2]['collection_method'], 'charge_automatically')
        self.assertEqual(invoices[0].kwargs['headers']['Idempotency-Key'],
                         f'overage-invoice:{self.annual.pk}:2026-10')
        finalize = by_path['/v1/invoices/in_x/finalize']
        self.assertEqual(len(finalize), 1)
        self.assertEqual(finalize[0].kwargs['headers']['Idempotency-Key'],
                         f'overage-finalize:{self.annual.pk}:2026-10')
        self.assertLess(posts.index(invoices[0]), posts.index(annual))
        self.assertLess(posts.index(annual), posts.index(finalize[0]))
        self.assertEqual(len(posts), 4)
        self.assertEqual([c for c in req.call_args_list if c.args[0] == 'GET'], [])

        self.assertEqual(AuditEvent.objects.filter(
            client=self.stripe_m, action='billing.overage', target='2026-10:web_block').count(), 1)
        self.assertIn('3 x web_block = 4500 cents', AuditEvent.objects.get(
            client=self.stripe_m, action='billing.overage').result)
        self.assertTrue(AuditEvent.objects.filter(
            client=self.stripe_m, action='billing.overage_pending', target='2026-10:web_block').exists())
        self.assertEqual(AuditEvent.objects.get(
            client=self.annual, action='billing.overage_invoice', target='2026-10').result, 'in_x')
        self.assertEqual(AuditEvent.objects.get(
            client=self.annual, action='billing.overage_finalized', target='2026-10').result, 'in_x')
        self.assertFalse(AuditEvent.objects.filter(
            client__in=[self.manual, self.under, self.old, self.canceled]).exists())

        self.assertIn('COBRAR A MANO (2', out)
        self.assertIn('manual (Manual, active): DOMINIO — 100 conversaciones adicionales (mes 2026-10) = $15', out)
        self.assertIn('ido (Stripe, canceled)', out)
        self.assertIn('sin Stripe activo', out)
        self.assertIn('2 cargo(s) creados ($60)', out)
        self.assertEqual(err, '')
        ops = [m for m in mail.outbox if m.to == ['ops@dominiopr.com']]
        self.assertEqual(len(ops), 1)
        self.assertIn('manual', ops[0].body)

        # Second run for the same month: nothing is posted (or even asked) again.
        with patch.object(payments, '_request', side_effect=self.stripe) as req2:
            out2, _ = self.run_cmd('--month', '2026-10')
        req2.assert_not_called()
        self.assertIn('ya facturado', out2)
        self.assertIn('0 cargo(s) creados', out2)
        self.assertEqual(AuditEvent.objects.filter(action='billing.overage').count(), 2)

    def test_dry_run_posts_and_records_nothing(self):
        with patch.object(payments, '_request') as req:
            out, _ = self.run_cmd('--month', '2026-10', '--dry-run')
        req.assert_not_called()
        self.assertFalse(AuditEvent.objects.filter(action__startswith='billing.').exists())
        self.assertIn('[dry-run] mensual: DOMINIO — 300 conversaciones adicionales (mes 2026-10) = $45', out)
        self.assertIn('COBRAR A MANO', out)
        self.assertEqual(mail.outbox, [])

    def test_default_month_is_the_previous_one(self):
        with patch.object(payments, '_request', return_value={'id': 'ii_1'}):
            out, _ = self.run_cmd()
        self.assertIn('Exceso de 2026-10', out)
        self.assertTrue(AuditEvent.objects.filter(action='billing.overage',
                                                  target='2026-10:web_block').exists())

    def test_stripe_error_is_reported_and_retried_next_run(self):
        with patch.object(payments, '_request', side_effect=payments.StripeError('card gone')):
            with self.assertRaises(CommandError):
                self.run_cmd('--month', '2026-10')
        self.assertFalse(AuditEvent.objects.filter(action='billing.overage').exists())
        # The monthly item got its pending marker before the failed POST, so
        # the retry first asks Stripe whether the item exists (it does not).
        self.assertTrue(AuditEvent.objects.filter(
            client=self.stripe_m, action='billing.overage_pending').exists())
        with patch.object(payments, '_request', side_effect=self.stripe) as req:
            self.run_cmd('--month', '2026-10')
        gets = [c for c in req.call_args_list if c.args[0] == 'GET']
        self.assertEqual(len(gets), 1)
        self.assertIn('customer=cus_m', gets[0].args[1])
        self.assertIn('pending=true', gets[0].args[1])
        self.assertEqual(len(self.posts(req)), 4)     # item + (invoice, item, finalize)
        self.assertEqual(AuditEvent.objects.filter(action='billing.overage').count(), 2)

    def test_bad_month_argument(self):
        with self.assertRaises(CommandError):
            call_command('bill_overage', '--month', 'octubre')

    def test_an_open_month_is_refused_unless_dry_run_or_force(self):
        with patch.object(payments, '_request', side_effect=self.stripe) as req:
            with self.assertRaisesMessage(CommandError, 'Ese mes no ha cerrado'):
                self.run_cmd('--month', '2026-11')          # RUN_AT is Nov 1: still open
            with self.assertRaisesMessage(CommandError, 'Ese mes no ha cerrado'):
                self.run_cmd('--month', '2027-03')          # the future
            req.assert_not_called()
            out, _ = self.run_cmd('--month', '2026-11', '--dry-run')
            self.assertIn('[dry-run] Exceso de 2026-11', out)
            req.assert_not_called()
            convs(self.stripe_m, 151, pr(2026, 11, 1, 1))  # 150 included: 1 block, open month
            out, _ = self.run_cmd('--month', '2026-11', '--force')
        self.assertIn('1 cargo(s) creados ($15)', out)
        self.assertEqual(len(self.posts(req)), 1)


# ------------------------------------------------------------------
# Billing audit follow-up (H1, M1, M3, M5, M6, L1, L4, L5, L7)
# ------------------------------------------------------------------

@override_settings(CONTACT_NOTIFY_EMAIL='ops@dominiopr.com', STRIPE_SECRET_KEY='sk_test_x',
                   STRIPE_TAX_RATE_ID='')
class BillingHardeningTests(TestCase):
    RUN_AT = pr(2026, 11, 1, 3, 10)

    def setUp(self):
        cache.clear()
        self.c = make_client('duro')
        make_sub(self.c, plan='small', method='stripe', stripe_customer_id='cus_d',
                 stripe_subscription_id='sub_d')              # 150 included, $49.99
        self.c = fresh(self.c)

    def run_cmd(self, *args):
        out, err = StringIO(), StringIO()
        with patch('django.utils.timezone.now', return_value=self.RUN_AT):
            call_command('bill_overage', *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    @staticmethod
    def posts(req):
        return [c for c in req.call_args_list if c.args[0] == 'POST']

    # H1 (1): only answered conversations are units ------------------

    def test_unanswered_conversations_are_not_units(self):
        convs(self.c, 200, pr(2026, 10, 3), answered=False)
        convs(self.c, 3, pr(2026, 10, 3), channel='whatsapp', answered=False)
        usage = entitlements.month_usage(self.c, date(2026, 10, 1))
        self.assertEqual((usage['conversations'], usage['web'], usage['whatsapp']), (0, 0, 0))
        with patch.object(payments, '_request') as req:
            out, _ = self.run_cmd('--month', '2026-10', '--dry-run')
        req.assert_not_called()
        self.assertNotIn('duro', out)
        self.assertIn('0 cargo(s) creados', out)
        # One reply turns the row into a unit; a user-only turn does not.
        rows = convs(self.c, 2, pr(2026, 10, 4), answered=False)
        ChatMessage.objects.create(conversation=rows[0], role='user', content='hola', position=0)
        ChatMessage.objects.create(conversation=rows[1], role='user', content='hola', position=0)
        ChatMessage.objects.create(conversation=rows[1], role='assistant', content='Hola.', position=1)
        self.assertEqual(entitlements.month_usage(self.c, date(2026, 10, 1))['web'], 1)

    # H1 (2): absurd amounts go to manual review ---------------------

    def test_usage_far_above_the_plan_is_reviewed_not_charged(self):
        convs(self.c, 1500, pr(2026, 10, 3))          # 10x included: 14 blocks = $210 > 3x $49.99
        with patch.object(payments, '_request', return_value={'id': 'ii_1'}) as req:
            out, _ = self.run_cmd('--month', '2026-10')
        req.assert_not_called()
        self.assertIn('COBRAR A MANO (1', out)
        self.assertIn('duro (Stripe, active): DOMINIO — 1400 conversaciones adicionales (mes 2026-10) = $210', out)
        self.assertIn('supera 3x la mensualidad', out)
        self.assertIn('uso web 1500 supera 3x lo incluido (150)', out)
        self.assertFalse(AuditEvent.objects.filter(action__startswith='billing.').exists())
        ops = [m for m in mail.outbox if m.to == ['ops@dominiopr.com']]
        self.assertEqual(len(ops), 1)
        self.assertIn('tráfico sospechoso', ops[0].body)
        self.assertIn('duro', ops[0].body)

    def test_usage_just_over_three_times_included_is_reviewed_even_if_cheap(self):
        self.c.entitlement_overrides = {'overage': {'web_block': [1000, 100]}}
        self.c.save()
        convs(self.c, 451, pr(2026, 10, 3))           # 3x150 + 1; overage only $1
        with patch.object(payments, '_request', return_value={'id': 'ii_1'}) as req:
            out, _ = self.run_cmd('--month', '2026-10')
        req.assert_not_called()
        self.assertIn('uso web 451 supera 3x lo incluido (150)', out)
        # Three times exactly, with a normal price, is charged.
        Conversation.objects.filter(client=self.c).delete()
        self.c.entitlement_overrides = {}
        self.c.save()
        convs(self.c, 450, pr(2026, 10, 3))           # 3 blocks = $45 < 3x $49.99
        with patch.object(payments, '_request', return_value={'id': 'ii_1'}) as req:
            out, _ = self.run_cmd('--month', '2026-10')
        self.assertEqual(len(self.posts(req)), 1)
        self.assertIn('1 cargo(s) creados ($45)', out)

    # M1: crash between the POST and the record ----------------------

    def test_crash_after_post_is_recovered_from_stripe_without_posting_again(self):
        convs(self.c, 251, pr(2026, 10, 3))           # 2 blocks = $30
        real_create = AuditEvent.objects.create

        def create_or_die(**kw):
            if kw.get('action') == 'billing.overage':
                raise RuntimeError('db gone')
            return real_create(**kw)

        with patch.object(payments, '_request', return_value={'id': 'ii_lost'}) as req, \
                patch.object(AuditEvent.objects, 'create', side_effect=create_or_die):
            with self.assertRaises(RuntimeError):
                self.run_cmd('--month', '2026-10')
        self.assertEqual(len(self.posts(req)), 1)     # Stripe has the item...
        self.assertTrue(AuditEvent.objects.filter(action='billing.overage_pending',
                                                  target='2026-10:web_block').exists())
        self.assertFalse(AuditEvent.objects.filter(action='billing.overage').exists())

        def stripe(method, path, data=None, headers=None):
            if method == 'GET':
                self.assertIn('/v1/invoiceitems?', path)
                self.assertIn('customer=cus_d', path)
                self.assertIn('pending=true', path)
                return {'object': 'list', 'data': [
                    {'id': 'ii_other', 'metadata': {'month': '2026-09', 'kind': 'web_block'}},
                    {'id': 'ii_lost', 'metadata': {'month': '2026-10', 'kind': 'web_block'}},
                ]}
            raise AssertionError(f'unexpected {method} {path}')

        with patch.object(payments, '_request', side_effect=stripe) as req2:
            out, err = self.run_cmd('--month', '2026-10')
        self.assertEqual(self.posts(req2), [])
        self.assertEqual(len(req2.call_args_list), 1)
        self.assertIn('ya estaba en Stripe (ii_lost)', out)
        self.assertEqual(err, '')
        rec = AuditEvent.objects.get(client=self.c, action='billing.overage',
                                     target='2026-10:web_block')
        self.assertIn('ii_lost', rec.result)
        # Third run: nothing at all.
        with patch.object(payments, '_request', side_effect=stripe) as req3:
            self.run_cmd('--month', '2026-10')
        req3.assert_not_called()

    def test_pending_without_the_item_in_stripe_posts_it(self):
        convs(self.c, 151, pr(2026, 10, 3))
        AuditEvent.objects.create(client=self.c, action='billing.overage_pending',
                                  target='2026-10:web_block', result='1 x web_block')

        def stripe(method, path, data=None, headers=None):
            if method == 'GET':
                return {'object': 'list', 'data': [
                    {'id': 'ii_wa', 'metadata': {'month': '2026-10', 'kind': 'wa_block'}}]}
            return {'id': 'ii_new'}

        with patch.object(payments, '_request', side_effect=stripe) as req:
            out, _ = self.run_cmd('--month', '2026-10')
        self.assertEqual(len(self.posts(req)), 1)
        self.assertIn('1 cargo(s) creados ($15)', out)
        self.assertEqual(AuditEvent.objects.filter(action='billing.overage_pending').count(), 1)

    # M3: plan or step changed in the billed month -------------------

    def test_plan_changed_in_the_month_is_reviewed_not_charged(self):
        convs(self.c, 151, pr(2026, 10, 3))
        Subscription.objects.filter(client=self.c).update(updated_at=pr(2026, 10, 20))
        with patch.object(payments, '_request', return_value={'id': 'ii_1'}) as req:
            out, _ = self.run_cmd('--month', '2026-10')
        req.assert_not_called()
        self.assertIn('plan o escalón modificado el 2026-10-20', out)
        self.assertFalse(AuditEvent.objects.filter(action__startswith='billing.').exists())
        # A change the month BEFORE is fine.
        Subscription.objects.filter(client=self.c).update(updated_at=pr(2026, 9, 29))
        with patch.object(payments, '_request', return_value={'id': 'ii_1'}) as req:
            self.run_cmd('--month', '2026-10')
        self.assertEqual(len(self.posts(req)), 1)

    # M5: WhatsApp bucket thresholds ---------------------------------

    def test_whatsapp_bucket_fires_its_own_thresholds(self):
        wa = make_client('wasap', name='Wasap Clínica')
        make_sub(wa, plan='premium')             # 4000 web + 500 WhatsApp
        wa = fresh(wa)
        convs(wa, 400, pr(2026, 10, 3), channel='whatsapp')   # 80% WA, 0% web
        with patch('django.utils.timezone.now', return_value=NOW):
            self.assertEqual(entitlements.check_thresholds(wa), [('wa', 80)])
        self.assertTrue(AuditEvent.objects.filter(client=wa, action='allowance.wa.80',
                                                  target='2026-10').exists())
        self.assertFalse(AuditEvent.objects.filter(action='allowance.80').exists())
        to_client = next(m for m in mail.outbox if m.to == ['owner@wasap.com'])
        self.assertIn('conversaciones de WhatsApp', to_client.subject)
        self.assertIn('400 de 500 conversaciones de WhatsApp', to_client.body)
        self.assertIn('bloques adicionales de 500 conversaciones de WhatsApp a $149 + IVU', to_client.body)
        self.assertIn('una factura aparte', to_client.body)
        to_ops = next(m for m in mail.outbox if m.to == ['ops@dominiopr.com'])
        self.assertIn('conversaciones de WhatsApp', to_ops.subject)
        self.assertEqual(len(mail.outbox), 2)
        # Same month: nothing more; cache cleared: the AuditEvent still dedupes.
        cache.clear()
        with patch('django.utils.timezone.now', return_value=NOW):
            self.assertEqual(entitlements.check_thresholds(wa), [])
        self.assertEqual(len(mail.outbox), 2)
        # Both buckets at 100% in one go: four emails, both actions recorded.
        convs(wa, 100, pr(2026, 10, 5), channel='whatsapp')
        convs(wa, 4000, pr(2026, 10, 5))
        with patch('django.utils.timezone.now', return_value=NOW):
            self.assertEqual(entitlements.check_thresholds(wa), [80, 100, ('wa', 100)])
        self.assertEqual(sorted(AuditEvent.objects.filter(client=wa).values_list('action', flat=True)),
                         ['allowance.100', 'allowance.80', 'allowance.wa.100', 'allowance.wa.80'])
        self.assertEqual(len(mail.outbox), 8)

    def test_whatsapp_thresholds_never_fire_where_the_channel_is_not_sold(self):
        convs(self.c, 400, pr(2026, 10, 3), channel='whatsapp')   # small: no WhatsApp
        with patch('django.utils.timezone.now', return_value=NOW):
            self.assertEqual(entitlements.check_thresholds(self.c), [])
        self.assertEqual(mail.outbox, [])

    def test_allowance_audit_record_is_unique_per_client_month(self):
        AuditEvent.objects.create(client=self.c, action='allowance.80', target='2026-10')
        self.assertFalse(entitlements._record_threshold(self.c, 'allowance.80', '2026-10', 'x'))
        self.assertEqual(AuditEvent.objects.filter(action='allowance.80').count(), 1)
        self.assertTrue(entitlements._record_threshold(self.c, 'allowance.80', '2026-11', 'x'))
        # Non-allowance actions keep repeating freely.
        AuditEvent.objects.create(client=self.c, action='billing.overage', target='2026-10:web_block')
        AuditEvent.objects.create(client=self.c, action='billing.overage', target='2026-10:web_block')

    # M6: internal conversations are never in the web bucket ---------

    def test_internal_conversations_are_reported_but_not_billed(self):
        big = make_client('equipo')
        make_sub(big, plan='large', method='stripe', stripe_customer_id='cus_e',
                 stripe_subscription_id='sub_e')                # 1500 web, internal sold
        big = fresh(big)
        convs(big, 1500, pr(2026, 10, 3))
        convs(big, 900, pr(2026, 10, 3), channel='internal')
        with patch('django.utils.timezone.now', return_value=NOW):
            status = entitlements.allowance_status(big)
        self.assertEqual((status['used'], status['internal'], status['all_conversations']),
                         (1500, 900, 2400))
        self.assertEqual(status['overage_blocks'], 0)
        self.assertFalse(status['billable'])
        with patch.object(payments, '_request', return_value={'id': 'ii_1'}) as req:
            out, _ = self.run_cmd('--month', '2026-10')
        req.assert_not_called()
        self.assertIn('0 cargo(s) creados', out)

    # L1: the plain-text email is not HTML-escaped --------------------

    def test_text_email_keeps_apostrophes(self):
        self.c.name = "D'Angelo & Hijos"
        self.c.save()
        convs(self.c, 120, pr(2026, 10, 3))
        with patch('django.utils.timezone.now', return_value=NOW):
            entitlements.check_thresholds(fresh(self.c))
        to_client = next(m for m in mail.outbox if m.to == ['owner@duro.com'])
        self.assertIn("D'Angelo & Hijos, tu agente", to_client.body)
        self.assertNotIn('&#x27;', to_client.body)
        self.assertIn('$15 + IVU', to_client.body)
        self.assertIn('en tu próxima factura (los planes anuales reciben una factura aparte)',
                      to_client.body)
        self.assertIn('D&#x27;Angelo', to_client.alternatives[0][0])   # HTML stays escaped

    # L4: override clamps --------------------------------------------

    def test_override_clamps(self):
        self.c.entitlement_overrides = {
            'monthly_conversations': 10 ** 12,
            'overage': {'web_block': [100, 1]},            # a $0.01 block: ignored
            'channels': ['web', 'whatsapp'],                # WhatsApp without capacity: ignored
        }
        self.c.save()
        ent = entitlements.entitlements(self.c)
        self.assertEqual(ent['monthly_conversations'], 10 ** 7)
        self.assertEqual(ent['overage']['web_block'], (100, 1500))
        self.assertEqual(ent['channels'], {'web'})
        self.c.entitlement_overrides = {'channels': ['web', 'whatsapp'], 'wa_conversations': 200,
                                        'overage': {'web_block': [0, 1500]}}
        self.c.save()
        ent = entitlements.entitlements(self.c)
        self.assertEqual(ent['channels'], {'web', 'whatsapp'})
        self.assertEqual(ent['wa_conversations'], 200)
        self.assertEqual(ent['overage']['web_block'], (100, 1500))   # 0-size block: ignored

    # L5: disabled logins are not seats -------------------------------

    def test_inactive_users_do_not_take_a_seat(self):
        member(self.c, 'a@duro.com', role='employee')
        gone = member(self.c, 'b@duro.com', role='employee')
        gone.is_active = False
        gone.save()
        self.assertEqual(entitlements.month_usage(self.c, date(2026, 10, 1))['seats_used'], 1)

    # L7: month bounds are Puerto Rico regardless of settings ----------

    @override_settings(TIME_ZONE='UTC')
    def test_month_bounds_are_puerto_rico_even_when_the_default_tz_is_not(self):
        start, end = entitlements.month_bounds(date(2026, 10, 5))
        self.assertEqual(start, pr(2026, 10, 1, 0, 0))
        self.assertEqual(end, pr(2026, 11, 1, 0, 0))
        self.assertNotEqual(start, usage_ledger.month_bounds(date(2026, 10, 5))[0])
        # 03:30 UTC on Nov 1 is still Oct 31 in Puerto Rico.
        start, _ = entitlements.month_bounds(datetime(2026, 11, 1, 3, 30, tzinfo=ZoneInfo('UTC')))
        self.assertEqual(start, pr(2026, 10, 1, 0, 0))
        with patch('django.utils.timezone.now',
                   return_value=datetime(2026, 11, 1, 3, 30, tzinfo=ZoneInfo('UTC'))):
            self.assertEqual(entitlements.month_bounds()[0], pr(2026, 10, 1, 0, 0))


# ------------------------------------------------------------------
# H1 (3): new conversations per IP and day on the widget endpoint
# ------------------------------------------------------------------

@override_settings(ANTHROPIC_API_KEY='test-key', DOMINIO_NEW_CONVERSATIONS_PER_IP_DAY=5)
class NewConversationCapTests(TestCase):
    TOKEN = 'cap-widget-token-abcdefghijklmnop'

    def setUp(self):
        cache.clear()
        self.c = make_client('tope', widget_token=self.TOKEN, daily_message_cap=300)
        make_sub(self.c, plan='small')

    def post(self, session, text='hola'):
        return HttpClient().post(
            reverse('chat_api'),
            data=json.dumps({'client': 'tope', 'token': self.TOKEN, 'session': session,
                             'messages': [{'role': 'user', 'content': text}]}),
            content_type='application/json')

    def test_sixth_new_session_from_one_ip_is_refused_but_old_sessions_go_on(self):
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            for i in range(5):
                self.assertEqual(self.post(f'session-{i}-xxxxxxxx').status_code, 200)
            resp = self.post('session-6-xxxxxxxx')
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.json(), {'error': 'Demasiadas solicitudes. Baja un poco la velocidad.'})
            self.assertIn('Retry-After', resp)
            self.assertFalse(Conversation.objects.filter(widget_session='session-6-xxxxxxxx').exists())
            # An existing session keeps talking.
            self.assertEqual(self.post('session-0-xxxxxxxx', 'otra').status_code, 200)
            self.assertEqual(self.post('session-4-xxxxxxxx', 'otra').status_code, 200)
        self.assertEqual(Conversation.objects.filter(client=self.c).count(), 5)
        self.assertEqual(ChatMessage.objects.filter(
            conversation__widget_session='session-0-xxxxxxxx').count(), 4)

    def test_cap_is_per_ip(self):
        with patch.object(agent, 'answer', side_effect=fake_answer()):
            for i in range(5):
                self.post(f'session-{i}-xxxxxxxx')
            self.assertEqual(self.post('session-6-xxxxxxxx').status_code, 429)
            resp = HttpClient().post(
                reverse('chat_api'),
                data=json.dumps({'client': 'tope', 'token': self.TOKEN,
                                 'session': 'session-7-xxxxxxxx',
                                 'messages': [{'role': 'user', 'content': 'hola'}]}),
                content_type='application/json', REMOTE_ADDR='203.0.113.9')
            self.assertEqual(resp.status_code, 200)


# ------------------------------------------------------------------
# Dashboards
# ------------------------------------------------------------------

class DashboardTests(TestCase):
    def setUp(self):
        cache.clear()
        Client.objects.filter(slug='dominio').delete()
        self.new = make_client('nuevo', name='Nuevo Salón')
        make_sub(self.new, plan='small')                    # 150 conversations
        member(self.new, 'ana@nuevo.com')
        self.old = make_client('viejo', name='Viejo Taller')
        make_sub(self.old, plan='pro')
        member(self.old, 'beto@viejo.com')
        get_user_model().objects.create_user('staff@dominiopr.com', password=PW, is_staff=True)

    def test_reports_show_conversations_and_the_disclaimer_for_a_package(self):
        convs(self.new, 120, timezone.now())
        resp = login('ana@nuevo.com').get(reverse('reports'))
        self.assertEqual(resp.status_code, 200)
        allowance = resp.context['allowance']
        self.assertEqual((allowance['unit'], allowance['included'], allowance['used']),
                         ('conversations', 150, 120))
        self.assertContains(resp, 'de 150 conversaciones este mes')
        self.assertContains(resp, 'bloques adicionales de 100 conversaciones a $15')
        self.assertContains(resp, 'Te quedan 30 conversaciones')
        self.assertContains(resp, 'Llevas <b>')
        convs(self.new, 40, timezone.now())                # 160: over
        resp = login('ana@nuevo.com').get(reverse('reports'))
        self.assertContains(resp, 'Ya usaste lo incluido')
        self.assertContains(resp, '<b>1</b> bloque adicional de 100 ($15)')

    def test_reports_keep_messages_for_a_launch_plan(self):
        convs(self.old, 5, timezone.now())
        resp = login('beto@viejo.com').get(reverse('reports'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['allowance']['unit'], 'messages')
        self.assertContains(resp, 'mensajes este mes')
        self.assertNotContains(resp, 'bloques adicionales')

    def test_billing_page_shows_included_used_and_the_disclaimer(self):
        convs(self.new, 20, timezone.now())
        resp = login('ana@nuevo.com').get(reverse('billing'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'de 150 conversaciones')
        self.assertContains(resp, 'bloques adicionales de 100 conversaciones a $15')
        resp = login('beto@viejo.com').get(reverse('billing'))
        self.assertNotContains(resp, 'bloques adicionales')

    def test_control_center_shows_overage(self):
        convs(self.new, 160, timezone.now())
        http = login('staff@dominiopr.com')
        resp = http.get(reverse('control'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Mes: 160/150 conv.')
        self.assertContains(resp, 'Exceso $15')
        row = next(r for r in resp.context['page'].object_list if r['client'] == self.new)
        self.assertEqual(row['allowance']['overage_blocks'], 1)
        old_row = next(r for r in resp.context['page'].object_list if r['client'] == self.old)
        self.assertIsNone(old_row['allowance'])
        resp = http.get(reverse('control_client', args=['nuevo']))
        self.assertContains(resp, 'Incluido / usado')
        self.assertContains(resp, '160 de 150 conversaciones')
        self.assertContains(resp, 'bill_overage')
        resp = http.get(reverse('control_client', args=['viejo']))
        self.assertContains(resp, 'plan por mensajes')
