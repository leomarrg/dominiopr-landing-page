"""Phase C -- monthly entitlements, allowance tracking and overage billing.

No model provider and no Stripe endpoint is ever called: agent.answer is a
double and payments._request is mocked.
"""
import itertools
from datetime import date, datetime
from decimal import Decimal
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


def make_sub(client, plan='medium', step=1, status='active', **kw):
    return Subscription.objects.create(client=client, plan=plan, step=step, status=status, **kw)


def fresh(client):
    return Client.objects.select_related('subscription').get(pk=client.pk)


def make_conv(client, when=None, channel='web'):
    i = next(_ids)
    if channel == 'web':
        conv = Conversation.objects.create(client=client, widget_session=f'{client.slug}-s-{i}')
    else:
        conv = Conversation.objects.create(client=client, channel=channel,
                                           external_id=f'{channel}-{i}')
    if when is not None:  # started_at is auto_now_add
        Conversation.objects.filter(pk=conv.pk).update(started_at=when)
    return conv


def convs(client, n, when, channel='web'):
    """n conversations started at `when` (bulk)."""
    rows = []
    for _ in range(n):
        i = next(_ids)
        if channel == 'web':
            rows.append(Conversation(client=client, widget_session=f'{client.slug}-b-{i}'))
        else:
            rows.append(Conversation(client=client, channel=channel, external_id=f'{channel}-{i}'))
    created = Conversation.objects.bulk_create(rows)
    Conversation.objects.filter(pk__in=[c.pk for c in created]).update(started_at=when)


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
        self.assertEqual(views.plan_limits('pro'),
                         {'daily_message_cap': 500, 'enable_bookings': True, 'unit': 'messages'})
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
            'channels': ['web', 'whatsapp', 'fax']})
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
        conv = make_conv(self.c)
        with patch.object(agent, 'answer', side_effect=fake_answer()), \
                patch.object(entitlements, 'check_thresholds', return_value=[]) as check:
            turns.run_turn(self.c, conv, 'hola', channel='web', handlers={})
            turns.run_turn(self.c, conv, 'otra', channel='web', handlers={})
        self.assertEqual(check.call_count, 1)
        self.assertEqual(ChatMessage.objects.filter(conversation=conv).count(), 4)

    def test_a_failing_check_never_blocks_the_reply(self):
        conv = make_conv(self.c)
        with patch.object(agent, 'answer', side_effect=fake_answer('Dime.')), \
                patch.object(entitlements, 'check_thresholds', side_effect=RuntimeError('boom')):
            result = turns.run_turn(self.c, conv, 'hola', channel='web', handlers={})
        self.assertEqual(result.reply, 'Dime.')
        self.assertEqual(ChatMessage.objects.filter(conversation=conv).count(), 2)

    def test_crossing_through_a_real_turn_sends_the_warning(self):
        convs(self.c, 119, pr(2026, 10, 3))
        conv = make_conv(self.c, when=pr(2026, 10, 10, 14))   # the 120th: 80%
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

    def test_posts_invoice_items_with_idempotency_and_records_them(self):
        with patch.object(payments, '_request', return_value={'id': 'ii_1'}) as req:
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
        self.assertEqual(monthly.args[2]['description'],
                         'DOMINIO — 300 conversaciones adicionales (mes 2026-10)')
        self.assertEqual(monthly.args[2]['metadata[client]'], 'mensual')
        self.assertEqual(monthly.args[2]['metadata[month]'], '2026-10')
        self.assertEqual(monthly.args[2]['metadata[kind]'], 'web_block')
        self.assertEqual(monthly.kwargs['headers'],
                         {'Idempotency-Key': f'overage:{self.stripe_m.pk}:2026-10:web_block'})
        annual = next(c for c in items if c.args[2]['customer'] == 'cus_a')
        self.assertEqual(annual.args[2]['amount'], '1500')
        self.assertNotIn('subscription', annual.args[2])     # collected by its own invoice
        invoices = by_path['/v1/invoices']
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices[0].args[2]['customer'], 'cus_a')
        self.assertEqual(invoices[0].args[2]['pending_invoice_items_behavior'], 'include')
        self.assertEqual(invoices[0].kwargs['headers']['Idempotency-Key'],
                         f'overage-invoice:{self.annual.pk}:2026-10')
        self.assertEqual(len(posts), 3)

        self.assertEqual(AuditEvent.objects.filter(
            client=self.stripe_m, action='billing.overage', target='2026-10:web_block').count(), 1)
        self.assertIn('3 x web_block = 4500 cents', AuditEvent.objects.get(
            client=self.stripe_m, action='billing.overage').result)
        self.assertTrue(AuditEvent.objects.filter(
            client=self.annual, action='billing.overage_invoice', target='2026-10').exists())
        self.assertFalse(AuditEvent.objects.filter(
            client__in=[self.manual, self.under, self.old, self.canceled]).exists())

        self.assertIn('COBRAR A MANO (2', out)
        self.assertIn('manual (Manual, active): DOMINIO — 100 conversaciones adicionales (mes 2026-10) = $15', out)
        self.assertIn('ido (Stripe, canceled)', out)
        self.assertIn('2 cargo(s) creados ($60)', out)
        self.assertEqual(err, '')
        ops = [m for m in mail.outbox if m.to == ['ops@dominiopr.com']]
        self.assertEqual(len(ops), 1)
        self.assertIn('manual', ops[0].body)

        # Second run for the same month: nothing is posted again.
        with patch.object(payments, '_request', return_value={'id': 'ii_2'}) as req2:
            out2, _ = self.run_cmd('--month', '2026-10')
        self.assertEqual(self.posts(req2), [])
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
        with patch.object(payments, '_request', return_value={'id': 'ii_1'}) as req:
            self.run_cmd('--month', '2026-10')
        self.assertEqual(len(self.posts(req)), 3)

    def test_bad_month_argument(self):
        with self.assertRaises(CommandError):
            call_command('bill_overage', '--month', 'octubre')


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
