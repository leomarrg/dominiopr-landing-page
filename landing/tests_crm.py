"""CRM dashboard (fase A1): modelos y set_status, backfill de la migracion
0044, acciones nuevas sobre leads (asignar, seguimiento, etiquetas, motivo
de perdida, estado en JSON) con aislamiento multi-tenant, filtros del
tablero, analitica (zona PR, geometria SVG determinista, modo clinica, sin
cost), home por rol, digest de seguimientos, contact_label en WhatsApp.

Nunca se llama al proveedor del modelo ni a un servidor de correo real.
"""
import importlib
import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.test import Client as HttpClient
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import agent, analytics, views, whatsapp
from . import leads as lead_service
from .models import (
    AuditEvent, Booking, ChatMessage, Client, ContactSubmission, Conversation, LeadTag,
    Membership, Subscription, Survey, UsageEvent,
)
from .tests_tenant_dash import SECRET_COST, walk_for_cost

PW = 'pw12345!'
PR = analytics.BUSINESS_TZ


def make_client(slug, **kw):
    return Client.objects.create(
        slug=slug, name=kw.pop('name', slug.title()), system_prompt='Vendemos cosas.',
        notify_email=f'owner@{slug}.example', **kw)


def make_user(username, client=None, role='org_admin', **kw):
    user = get_user_model().objects.create_user(
        username=username, email=kw.pop('email', f'{username}@example.com'), password=PW, **kw)
    if client is not None:
        Membership.objects.create(user=user, client=client, role=role)
    return user


def login(username):
    http = HttpClient()
    assert http.login(username=username, password=PW)
    return http


def make_lead(client, name='Ana', source='chat', **kw):
    return ContactSubmission.objects.create(
        client=client, name=name, email=kw.pop('email', f'{name.lower()}@x.example'),
        service='consulting', message='hola', source=source, **kw)


def make_conv(client, i, when=None, **kw):
    conv = Conversation.objects.create(client=client, widget_session=f'{client.slug}-s-{i}', **kw)
    if when is not None:
        Conversation.objects.filter(pk=conv.pk).update(started_at=when)
        conv.refresh_from_db()
    return conv


def pr(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=PR)


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        Client.objects.filter(slug='dominio').delete()
        self.a = make_client('alfa', name='Alfa Café')
        self.b = make_client('bravo', name='Bravo Taller')
        self.admin_a = make_user('admin_a', self.a, 'org_admin')
        self.agent_a = make_user('agent_a', self.a, 'agent')
        self.employee_a = make_user('emp_a', self.a, 'employee')
        self.admin_b = make_user('admin_b', self.b, 'org_admin')
        self.staff = make_user('staff', is_staff=True)
        self.lead = make_lead(self.a, 'Maria')
        self.lead_b = make_lead(self.b, 'Pedro')


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------

class ModelTests(_Base):
    def test_set_status_stamps_changed_and_first_contact(self):
        # Push the stamp into the past: on Windows timezone.now() ticks every
        # ~15 ms, so two calls in a row can return the same instant.
        before = timezone.now() - timedelta(minutes=5)
        ContactSubmission.objects.filter(pk=self.lead.pk).update(status_changed_at=before)
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.set_status('contacted'))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'contacted')
        self.assertGreater(self.lead.status_changed_at, before)
        self.assertIsNotNone(self.lead.contacted_at)
        first_contact = self.lead.contacted_at
        self.lead.set_status('qualified')
        self.lead.set_status('contacted')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.contacted_at, first_contact)  # first time only

    def test_set_status_same_status_is_a_noop(self):
        stamp = self.lead.status_changed_at
        self.assertFalse(self.lead.set_status('new'))
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status_changed_at, stamp)
        with self.assertRaises(ValueError):
            self.lead.set_status('bogus')

    def test_clean_rejects_assignee_outside_the_client(self):
        self.lead.assigned_to = self.admin_b
        with self.assertRaises(ValidationError):
            self.lead.full_clean()
        self.lead.assigned_to = self.employee_a  # employee is not a dashboard role
        with self.assertRaises(ValidationError):
            self.lead.full_clean()
        self.lead.assigned_to = self.agent_a
        self.lead.full_clean()

    def test_channel_and_follow_up_helpers(self):
        self.assertEqual(self.lead.channel, 'web')
        wa = make_lead(self.a, 'Luis', source='whatsapp')
        self.assertEqual(wa.channel, 'whatsapp')
        self.assertFalse(wa.follow_up_open)
        wa.follow_up_at = timezone.now()
        self.assertTrue(wa.follow_up_open)
        wa.follow_up_done_at = timezone.now()
        self.assertFalse(wa.follow_up_open)

    def test_lead_tag_unique_per_client_and_digest_default(self):
        LeadTag.objects.create(client=self.a, name='VIP')
        LeadTag.objects.create(client=self.b, name='VIP')  # other tenant: fine
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            LeadTag.objects.create(client=self.a, name='VIP')
        self.assertTrue(Membership.objects.get(user=self.admin_a).daily_digest)

    def test_migration_backfill_sets_status_changed_at_to_created_at(self):
        old = timezone.now() - timedelta(days=40)
        ContactSubmission.objects.filter(pk=self.lead.pk).update(
            created_at=old, status_changed_at=timezone.now())
        mig = importlib.import_module('landing.migrations.0044_crm')
        mig.backfill_status_changed_at(apps, connection.schema_editor)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status_changed_at, old)


# ------------------------------------------------------------------
# Leads: list / board / filters
# ------------------------------------------------------------------

class LeadsBoardTests(_Base):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.wa = make_lead(self.a, 'Luis', source='whatsapp', phone='+17875550001', email='')
        self.old = make_lead(self.a, 'Viejo')
        ContactSubmission.objects.filter(pk=self.old.pk).update(created_at=now - timedelta(hours=30))
        self.overdue = make_lead(self.a, 'Vencido', follow_up_at=now - timedelta(days=2),
                                 assigned_to=self.agent_a)
        self.today = make_lead(self.a, 'Hoy', follow_up_at=now)
        self.done = make_lead(self.a, 'Hecho', follow_up_at=now - timedelta(days=1),
                              follow_up_done_at=now)
        self.tag = LeadTag.objects.create(client=self.a, name='VIP')
        self.wa.tags.add(self.tag)
        self.unconfirmed = make_lead(self.a, 'Sin', email='sin@x.example')
        self.lead.email_verified_at = now
        self.lead.save(update_fields=['email_verified_at'])
        self.http = login('agent_a')

    def names(self, **params):
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        resp = self.http.get(reverse('dashboard') + '?view=list' + (f'&{query}' if query else ''))
        self.assertEqual(resp.status_code, 200)
        return sorted(lead.name for lead in resp.context['leads'])

    def test_leads_list_lives_at_dashboard_leads_and_home_at_dashboard(self):
        self.assertEqual(reverse('dashboard'), '/dashboard/leads/')
        self.assertEqual(reverse('home'), '/dashboard/')

    def test_board_groups_by_status_with_totals_and_limit(self):
        for i in range(25):
            make_lead(self.a, f'Extra{i}', status='contacted')
        resp = self.http.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['view'], 'board')
        board = resp.context['board']
        self.assertEqual(list(board), list(ContactSubmission.STATUS_ORDER))
        self.assertEqual(board['contacted']['total'], 25)
        self.assertEqual(len(board['contacted']['leads']), views.LEAD_BOARD_LIMIT)
        self.assertEqual(board['contacted']['more'], 5)
        self.assertEqual(board['contacted']['label'], 'Contactado')
        # Maria + Luis, Viejo, Vencido, Hoy, Hecho, Sin (all 'new'); Pedro is client B.
        self.assertEqual(resp.context['counts']['new'], 7)
        self.assertNotIn('Pedro', [lead.name for lead in resp.context['leads']])

    def test_filters(self):
        self.assertEqual(self.names(channel='whatsapp'), ['Luis'])
        self.assertNotIn('Luis', self.names(channel='web'))
        self.assertEqual(self.names(follow_up='overdue'), ['Vencido'])
        self.assertEqual(self.names(follow_up='today'), ['Hoy'])
        self.assertEqual(self.names(follow_up='week'), ['Hoy'])
        self.assertNotIn('Vencido', self.names(follow_up='none'))
        self.assertIn('Hecho', self.names(follow_up='none'))
        self.assertEqual(self.names(assigned='me'), ['Vencido'])
        self.assertEqual(self.names(assigned=self.agent_a.pk), ['Vencido'])
        self.assertNotIn('Vencido', self.names(assigned='none'))
        self.assertEqual(self.names(tag=self.tag.pk), ['Luis'])
        self.assertEqual(self.names(stale='1'), ['Viejo'])
        unconfirmed = self.names(unconfirmed='1')
        self.assertIn('Sin', unconfirmed)
        self.assertNotIn('Maria', unconfirmed)
        self.assertNotIn('Luis', unconfirmed)  # no email: nothing to confirm
        self.assertEqual(self.names(status='new', q='mar'), ['Maria'])
        self.assertEqual(self.names(client='bravo'), [])  # out of scope: nothing

    def test_members_and_tags_for_a_single_client_scope(self):
        resp = self.http.get(reverse('dashboard'))
        self.assertEqual([u.username for u in resp.context['members']], ['admin_a', 'agent_a'])
        self.assertTrue(resp.context['show_assign'])
        self.assertEqual([t.name for t in resp.context['tags_all']], ['VIP'])
        self.assertEqual(resp.context['stats']['from_whatsapp'], 1)


# ------------------------------------------------------------------
# Lead actions (JSON, tenant isolation, audit)
# ------------------------------------------------------------------

class LeadActionTests(_Base):
    def setUp(self):
        super().setUp()
        self.http = login('agent_a')

    def post_json(self, name, lead, data, **kw):
        return self.http.post(reverse(name, args=[lead.pk]), data,
                              HTTP_ACCEPT='application/json', **kw)

    def test_lead_status_json(self):
        resp = self.post_json('lead_status', self.lead, {'status': 'contacted'})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body['ok'])
        self.assertEqual(body['status'], 'contacted')
        self.assertEqual(body['counts'], {'new': 0, 'contacted': 1, 'qualified': 0, 'won': 0, 'lost': 0})
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.contacted_at)
        self.assertEqual(AuditEvent.objects.filter(action='lead.status').count(), 1)
        bad = self.post_json('lead_status', self.lead, {'status': 'nope'})
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(bad.json()['ok'])
        # ?format=json works without the header; forms still redirect.
        resp = self.http.post(reverse('lead_status', args=[self.lead.pk]) + '?format=json',
                              {'status': 'lost', 'lost_reason': 'Precio'})
        self.assertEqual(resp.json()['status'], 'lost')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.lost_reason, 'Precio')
        resp = self.http.post(reverse('lead_status', args=[self.lead.pk]), {'status': 'won'})
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)

    def test_cross_tenant_lead_is_404_on_every_action(self):
        for name, data in (('lead_status', {'status': 'contacted'}),
                           ('lead_assign', {'assigned_to': self.admin_b.pk}),
                           ('lead_follow_up', {'date': '2030-01-02', 'time': '10:00'}),
                           ('lead_tags', {'name': 'X'}),
                           ('lead_lost_reason', {'lost_reason': 'x'})):
            resp = self.post_json(name, self.lead_b, data)
            self.assertEqual(resp.status_code, 404, name)
        self.lead_b.refresh_from_db()
        self.assertEqual((self.lead_b.status, self.lead_b.assigned_to_id, self.lead_b.follow_up_at,
                          self.lead_b.lost_reason, self.lead_b.tags.count()),
                         ('new', None, None, '', 0))
        self.assertFalse(AuditEvent.objects.filter(client=self.b).exists())

    def test_assign_member_and_reject_foreigners(self):
        resp = self.post_json('lead_assign', self.lead, {'assigned_to': self.admin_a.pk})
        self.assertEqual(resp.status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_to, self.admin_a)
        ev = AuditEvent.objects.get(action='lead.assign')
        self.assertEqual((ev.client, ev.target, ev.result),
                         (self.a, f'lead:{self.lead.pk}', f'user:{self.admin_a.pk}'))
        for foreign in (self.admin_b.pk, self.employee_a.pk, self.staff.pk, 'abc', 99999):
            resp = self.post_json('lead_assign', self.lead, {'assigned_to': foreign})
            self.assertEqual(resp.status_code, 400, foreign)
            self.assertIn('organización', resp.json()['error'])
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.assigned_to, self.admin_a)
        # Form POST without a referer: plain 400, nothing saved.
        resp = self.http.post(reverse('lead_assign', args=[self.lead.pk]),
                              {'assigned_to': self.admin_b.pk})
        self.assertEqual(resp.status_code, 400)
        resp = self.post_json('lead_assign', self.lead, {'assigned_to': ''})
        self.assertEqual(resp.status_code, 200)
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.assigned_to)
        self.assertEqual(AuditEvent.objects.filter(action='lead.assign').latest('pk').result, 'none')

    def test_follow_up_schedule_in_pr_time_done_and_clear(self):
        resp = self.post_json('lead_follow_up', self.lead, {'date': '2030-03-05', 'time': '14:30'})
        self.assertEqual(resp.status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.follow_up_at, pr(2030, 3, 5, 14, 30))
        self.assertIsNone(self.lead.follow_up_done_at)
        resp = self.post_json('lead_follow_up', self.lead, {'done': '1'})
        self.assertEqual(resp.status_code, 200)
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.follow_up_done_at)
        # done twice: nothing pending -> 400
        self.assertEqual(self.post_json('lead_follow_up', self.lead, {'done': '1'}).status_code, 400)
        # scheduling again reopens
        self.post_json('lead_follow_up', self.lead, {'date': '2030-03-06'})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.follow_up_at, pr(2030, 3, 6, 9, 0))
        self.assertIsNone(self.lead.follow_up_done_at)
        self.assertEqual(self.post_json('lead_follow_up', self.lead,
                                        {'date': '06/03/2030'}).status_code, 400)
        resp = self.post_json('lead_follow_up', self.lead, {'clear': '1'})
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.follow_up_at)
        results = list(AuditEvent.objects.filter(action='lead.follow_up')
                       .order_by('pk').values_list('result', flat=True))
        self.assertEqual(results[1], 'done')
        self.assertEqual(results[-1], 'cleared')
        self.assertTrue(results[0].startswith('2030-03-05T14:30'))

    def test_tags_create_reuse_remove_limit_and_isolation(self):
        resp = self.post_json('lead_tags', self.lead, {'name': '  Cotización   enviada ', 'color': '#ff0000'})
        self.assertEqual(resp.status_code, 200)
        tag = LeadTag.objects.get(client=self.a)
        self.assertEqual((tag.name, tag.color), ('Cotización enviada', '#ff0000'))
        self.assertEqual(list(self.lead.tags.all()), [tag])
        # Same name, other case: reused, not duplicated.
        other = make_lead(self.a, 'Otra')
        self.post_json('lead_tags', other, {'name': 'cotización ENVIADA'})
        self.assertEqual(LeadTag.objects.filter(client=self.a).count(), 1)
        self.assertEqual(list(other.tags.all()), [tag])
        # Remove by pk; a tag of another client is a 404.
        foreign = LeadTag.objects.create(client=self.b, name='Ajena')
        self.assertEqual(self.post_json('lead_tags', self.lead,
                                        {'action': 'remove', 'tag': foreign.pk}).status_code, 404)
        self.assertEqual(self.post_json('lead_tags', self.lead,
                                        {'action': 'add', 'tag': foreign.pk}).status_code, 404)
        resp = self.post_json('lead_tags', self.lead, {'action': 'remove', 'tag': tag.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.lead.tags.count(), 0)
        self.assertEqual(self.post_json('lead_tags', self.lead, {'name': '   '}).status_code, 400)
        for i in range(LeadTag.MAX_PER_CLIENT - 1):
            LeadTag.objects.create(client=self.a, name=f'T{i}')
        resp = self.post_json('lead_tags', self.lead, {'name': 'Una más'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('Máximo 20', resp.json()['error'])
        self.assertEqual(LeadTag.objects.filter(client=self.a).count(), LeadTag.MAX_PER_CLIENT)
        results = list(AuditEvent.objects.filter(action='lead.tags').values_list('result', flat=True))
        self.assertIn('+Cotización enviada', results)
        self.assertIn('-Cotización enviada', results)

    def test_lost_reason_moves_to_lost(self):
        resp = self.post_json('lead_lost_reason', self.lead, {'lost_reason': 'Escogió otro proveedor'})
        self.assertEqual(resp.status_code, 200)
        self.lead.refresh_from_db()
        self.assertEqual((self.lead.status, self.lead.lost_reason),
                         ('lost', 'Escogió otro proveedor'))
        # The trail records that a reason exists, never its text.
        self.assertEqual(AuditEvent.objects.get(action='lead.lost').result, 'con motivo')
        self.assertTrue(AuditEvent.objects.filter(action='lead.status', result='lost').exists())

    def test_lead_email_nudge_goes_through_set_status(self):
        with patch('landing.views._send_html_email', return_value=True):
            self.http.post(reverse('lead_email', args=[self.lead.pk]),
                           {'subject': 'Hola', 'body': 'Te escribo.'})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'contacted')
        self.assertIsNotNone(self.lead.contacted_at)

    def test_lead_detail_context(self):
        LeadTag.objects.create(client=self.a, name='VIP')
        self.lead.set_status('qualified')
        self.lead.follow_up_at = pr(2030, 1, 2, 15, 0)
        self.lead.save(update_fields=['follow_up_at'])
        resp = self.http.get(reverse('lead_detail', args=[self.lead.pk]))
        self.assertEqual(resp.status_code, 200)
        ctx = resp.context
        self.assertEqual([u.username for u in ctx['members']], ['admin_a', 'agent_a'])
        self.assertTrue(ctx['show_assign'])
        self.assertEqual([t.name for t in ctx['tags_all']], ['VIP'])
        self.assertEqual(ctx['follow_up']['date'], '2030-01-02')
        self.assertEqual(ctx['follow_up']['time'], '15:00')
        self.assertTrue(ctx['follow_up']['open'])
        self.assertFalse(ctx['follow_up']['overdue'])
        stepper = ctx['stepper']
        self.assertEqual([s['key'] for s in stepper], list(ContactSubmission.STATUS_ORDER))
        self.assertEqual([s['done'] for s in stepper], [True, True, False, False, False])
        self.assertEqual([s['current'] for s in stepper], [False, False, True, False, False])
        self.assertEqual(ctx['status_labels']['qualified'], 'Calificado')
        # Lost: won is not "done".
        self.lead.set_status('lost')
        stepper = self.http.get(reverse('lead_detail', args=[self.lead.pk])).context['stepper']
        self.assertEqual([s['done'] for s in stepper], [True, True, True, False, False])
        self.assertTrue(stepper[-1]['current'])


# ------------------------------------------------------------------
# Analytics
# ------------------------------------------------------------------

class AnalyticsTests(_Base):
    def test_daily_by_channel_uses_puerto_rico_days(self):
        today = timezone.localtime(timezone.now(), PR).date()
        # 23:30 PR yesterday = 03:30 UTC today: must land on YESTERDAY.
        yesterday = today - timedelta(days=1)
        make_conv(self.a, 1, when=pr(yesterday.year, yesterday.month, yesterday.day, 23, 30))
        Conversation.objects.create(client=self.a, channel='whatsapp', external_id='wa:x:1')
        Conversation.objects.create(client=self.a, channel='internal', external_id='int:1')
        make_conv(self.b, 1)
        series = analytics.daily_by_channel(self.a, days=30)
        self.assertEqual(len(series), 30)
        self.assertEqual(series[-1]['day'], today)
        self.assertEqual((series[-2]['web'], series[-2]['whatsapp']), (1, 0))
        self.assertEqual((series[-1]['web'], series[-1]['whatsapp'], series[-1]['total']), (0, 1, 1))
        self.assertEqual(sum(d['total'] for d in series), 2)  # internal and bravo excluded
        leads = analytics.daily_counts(self.a, 30, 'leads')
        self.assertEqual(leads[-1]['value'], 1)
        self.assertEqual(set(leads[0]), {'day', 'value'})

    def test_delta(self):
        self.assertEqual(analytics.delta(12, 10), {'value': 12, 'prev': 10, 'pct': 20, 'direction': 'up'})
        self.assertEqual(analytics.delta(5, 0)['pct'], None)
        self.assertEqual(analytics.delta(0, 0), {'value': 0, 'prev': 0, 'pct': 0, 'direction': 'flat'})
        self.assertEqual(analytics.delta(None, 4.5)['direction'], 'down')

    def test_kpis_and_funnel(self):
        start, end = timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=1)
        lead = self.lead
        c1 = make_conv(self.a, 1, lead=lead)
        make_conv(self.a, 2, state='escalated')
        make_conv(self.a, 3)
        Booking.objects.create(client=self.a, name='Maria', email=lead.email.upper(),
                               start=timezone.now() + timedelta(days=2))
        # Bravo's booking with the same email must not count for alfa.
        Booking.objects.create(client=self.b, name='Maria', email=lead.email,
                               start=timezone.now() + timedelta(days=3))
        Survey.objects.create(conversation=c1, score=4)
        with self.assertNumQueries(1):
            fun = analytics.funnel(self.a, start, end)
        self.assertEqual([s['n'] for s in fun['stages']], [3, 1, 1])
        self.assertEqual([s['pct'] for s in fun['stages']], [100, 33, 33])
        self.assertEqual(fun['lead_rate'], 33)
        with self.assertNumQueries(8):
            kp = analytics.kpis(self.a, start, end)
        by_key = {k['key']: k for k in kp}
        self.assertEqual(by_key['conversations']['value'], 3)
        self.assertEqual(by_key['leads']['value'], 1)
        self.assertEqual(by_key['bookings']['value'], 1)
        self.assertEqual(by_key['escalated']['value'], 1)
        self.assertEqual(by_key['conversion']['value'], 33)
        self.assertEqual(by_key['csat']['value'], 4.0)
        self.assertEqual([k['key'] for k in kp], [k for k, _l, _s in analytics.KPI_DEFS])
        self.assertEqual(analytics.resolution_rate(self.a, start, end)['pct'], None)
        Conversation.objects.filter(pk=c1.pk).update(state='closed_resolved')
        make_conv(self.a, 4, state='closed_unresolved')
        self.assertEqual(analytics.resolution_rate(self.a, start, end),
                         {'resolved': 1, 'unresolved': 1, 'closed': 2, 'pct': 50})

    def test_peak_hours_buckets_and_pr_zone(self):
        start, end = pr(2026, 3, 1), pr(2026, 4, 1)
        # Tuesday 2026-03-10 at 10:15 PR, twice; Saturday 14 at 22:00.
        make_conv(self.a, 1, when=pr(2026, 3, 10, 10, 15))
        make_conv(self.a, 2, when=pr(2026, 3, 10, 10, 45))
        make_conv(self.a, 3, when=pr(2026, 3, 14, 22, 0))
        peak = analytics.peak_hours(self.a, start, end)
        self.assertEqual(peak['bucket_hours'], 3)
        self.assertEqual(len(peak['hours']), 8)
        self.assertEqual(peak['matrix'][1][3], 2)   # Tue, 9-12 block
        self.assertEqual(peak['matrix'][5][7], 1)   # Sat, 21-24 block
        self.assertEqual(peak['top'][0], {'day': 'Mar', 'hour': '9–12', 'n': 2})
        for i in range(50):
            make_conv(self.a, 100 + i, when=pr(2026, 3, 2, 8, 0))
        peak = analytics.peak_hours(self.a, start, end)
        self.assertEqual(peak['bucket_hours'], 1)
        self.assertEqual(len(peak['hours']), 24)
        self.assertEqual(peak['matrix'][0][8], 50)
        heat = analytics.svg_heatmap(peak)
        self.assertEqual(len(heat['cells']), 7 * 24)
        busiest = [c for c in heat['cells'] if c['n'] == 50][0]
        self.assertEqual((busiest['level'], busiest['day'], busiest['hour']), (4, 'Lun', '8:00'))
        self.assertEqual(len(heat['col_labels']), 8)
        self.assertEqual(len(heat['rows']), 7)

    def test_csat_distribution_hides_comments_in_clinic_mode(self):
        start, end = timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=1)
        for i, (score, comment) in enumerate([(5, 'excelente'), (5, ''), (3, 'regular')]):
            Survey.objects.create(conversation=make_conv(self.a, i), score=score, comment=comment)
        dist = analytics.csat_distribution(self.a, start, end)
        self.assertEqual((dist['n'], dist['avg'], dist['counts'][5]), (3, 4.3, 2))
        self.assertEqual(dist['rows'][0], {'score': 5, 'n': 2, 'pct': 67})
        self.assertEqual([c['comment'] for c in dist['comments']], ['regular', 'excelente'])
        Client.objects.filter(pk=self.a.pk).update(clinic_mode=True)
        self.a.refresh_from_db()
        self.assertEqual(analytics.csat_distribution(self.a, start, end)['comments'], [])

    def test_attention_counts_and_urls(self):
        now = timezone.now()
        stale = make_lead(self.a, 'Viejo')
        ContactSubmission.objects.filter(pk=stale.pk).update(created_at=now - timedelta(hours=25))
        make_lead(self.a, 'Vencido', follow_up_at=now - timedelta(days=1), assigned_to=self.agent_a)
        make_lead(self.a, 'Hoy', follow_up_at=now)
        make_conv(self.a, 1, state='escalated')
        Booking.objects.create(client=self.a, name='x', email='x@x.example',
                               start=now + timedelta(days=1))
        make_lead(self.b, 'Ajeno', follow_up_at=now - timedelta(days=1))
        with self.assertNumQueries(3):
            att = analytics.attention(self.a, self.agent_a)
        items = {i['key']: i for i in att['items']}
        self.assertEqual(items['stale_new']['count'], 1)
        self.assertEqual(items['escalated']['count'], 1)
        self.assertEqual((items['follow_up_overdue']['count'], items['follow_up_overdue']['mine']), (1, 1))
        self.assertEqual(items['follow_up_today']['count'], 1)
        self.assertEqual(items['bookings_pending']['count'], 1)
        self.assertEqual(att['total'], 5)
        self.assertEqual(items['follow_up_overdue']['url'], '/dashboard/leads/?client=alfa&follow_up=overdue')
        self.assertEqual(items['escalated']['url'], '/dashboard/conversations/?client=alfa&state=escalated')
        lists = analytics.follow_up_lists(self.a)
        self.assertEqual([l.name for l in lists['overdue']], ['Vencido'])
        self.assertEqual([l.name for l in lists['today']], ['Hoy'])

    def test_svg_geometry_is_deterministic(self):
        self.assertEqual(analytics._nice_scale(0), (1, 4))
        self.assertEqual(analytics._nice_scale(5), (2, 8))
        self.assertEqual(analytics._nice_scale(37), (10, 40))
        spark = analytics.sparkline([0, 2, 4])
        self.assertEqual(spark['path'], 'M0.0,30.0 L60.0,16.0 L120.0,2.0')
        self.assertTrue(spark['area'].endswith('L0.0,32 Z'))
        self.assertEqual(analytics.sparkline([])['path'], '')
        from datetime import date
        series = [{'day': date(2026, 3, d), 'web': w, 'whatsapp': a}
                  for d, (w, a) in enumerate([(1, 0), (2, 2), (0, 1)], start=1)]
        area = analytics.svg_area(series, ['web', 'whatsapp'])
        self.assertEqual(area['viewBox'], '0 0 600 160')
        self.assertEqual([l['key'] for l in area['layers']], ['web', 'whatsapp'])
        self.assertEqual(len(area['y_ticks']), 5)
        self.assertEqual([t['text'] for t in area['y_ticks']], [0, 1, 2, 3, 4])
        self.assertEqual(area['y_max'], 4)
        self.assertEqual(len(area['x_labels']), 3)
        self.assertEqual(area['x_labels'][0]['text'], '01/03')
        self.assertEqual(area['layers'][0]['line'], 'M34.0,107.0 L313.0,74.0 L592.0,140.0')
        self.assertEqual(area['rows'][1], {'day': date(2026, 3, 2), 'cells': [2, 2], 'total': 4})
        self.assertEqual(area['columns'], ['Web', 'WhatsApp'])
        self.assertFalse(area['empty'])
        fun = analytics.svg_funnel([
            {'key': 'conversations', 'label': 'Conversaciones', 'n': 10, 'pct': 100},
            {'key': 'leads', 'label': 'Con lead', 'n': 5, 'pct': 50},
            {'key': 'bookings', 'label': 'Con cita', 'n': 0, 'pct': 0}])
        self.assertEqual([b['w'] for b in fun['bars']], ['600.0', '300.0', '0.0'])
        self.assertEqual([b['drop'] for b in fun['bars']], [None, 50, 100])
        self.assertEqual(fun['rows'][1]['n'], 5)
        bars = analytics.svg_bars([{'day': 1, 'messages': 2}], 'messages')
        self.assertEqual(set(bars[0]), {'x', 'w', 'y', 'h', 'day', 'value'})


# ------------------------------------------------------------------
# Home
# ------------------------------------------------------------------

class HomeTests(_Base):
    def setUp(self):
        super().setUp()
        Subscription.objects.create(client=self.a, plan='pro', status='active')
        make_conv(self.a, 1, lead=self.lead, topic='tema-alfa', intent='precios',
                  tagged_at=timezone.now())
        make_conv(self.b, 1, topic='tema-secreto-bravo', intent='cita', tagged_at=timezone.now())
        UsageEvent.objects.create(client=self.a, client_slug='alfa', kind='chat',
                                  input_tokens=100, output_tokens=20, api_calls=1,
                                  cost_usd=SECRET_COST)

    def test_employee_is_sent_to_the_assistant(self):
        resp = login('emp_a').get(reverse('home'))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse('asistente'))

    def test_agent_and_org_admin_get_their_client(self):
        for username in ('agent_a', 'admin_a'):
            resp = login(username).get(reverse('home') + '?client=bravo')
            self.assertEqual(resp.status_code, 200, username)
            self.assertEqual(resp.context['client_obj'], self.a)
            self.assertEqual(resp.context['client_options'], [('alfa', 'Alfa Café')])
            self.assertNotContains(resp, 'tema-secreto-bravo')
            self.assertNotContains(resp, 'Bravo Taller')
            ctx = resp.context
            for key in ('kpis', 'channel_series', 'channel_chart', 'funnel', 'funnel_chart',
                        'peak', 'peak_chart', 'attention', 'follow_ups', 'intents', 'topics',
                        'agent', 'allowance', 'csat', 'resolution', 'period', 'periods'):
                self.assertIn(key, ctx, key)
            self.assertEqual(ctx['kpis'][0]['value'], 1)
            self.assertIn('spark', ctx['kpis'][0])
            self.assertEqual(ctx['agent']['setup_status'], 'live')
            self.assertNotIn('cost', ctx['allowance'])
            flat = {}
            for c in resp.context:
                flat.update(c.flatten())
            walk_for_cost(self, flat)
            for text in ('7.65', 'Costo', 'Margen'):
                self.assertNotContains(resp, text)

    def test_staff_picks_a_client_and_period(self):
        resp = login('staff').get(reverse('home') + '?client=bravo&period=30d')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['client_obj'], self.b)
        self.assertEqual(resp.context['period'], '30d')
        self.assertContains(resp, 'tema-secreto-bravo')

    def test_clinic_mode_hides_topics_and_comments(self):
        Client.objects.filter(pk=self.a.pk).update(clinic_mode=True)
        Survey.objects.create(conversation=Conversation.objects.get(client=self.a), score=5,
                              comment='dolor de espalda')
        resp = login('agent_a').get(reverse('home'))
        self.assertEqual(resp.context['topics'], [])
        self.assertEqual(resp.context['csat']['comments'], [])
        self.assertNotContains(resp, 'tema-alfa')
        self.assertNotContains(resp, 'dolor de espalda')

    def test_login_lands_on_home(self):
        http = HttpClient()
        resp = http.post(reverse('login'), {'username': 'agent_a', 'password': PW})
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)

    def test_query_count_does_not_grow_with_other_tenants(self):
        def noise(n):
            for i in range(n):
                c = make_client(f'otro{i}-{n}')
                make_conv(c, 1, state='escalated')
                make_lead(c, f'Lead{i}', follow_up_at=timezone.now())
                Booking.objects.create(client=c, name='x', email='x@x.example',
                                       start=timezone.now() + timedelta(days=1))
        http = login('agent_a')
        noise(1)
        with CaptureQueriesContext(connection) as one:
            self.assertEqual(http.get(reverse('home')).status_code, 200)
        noise(10)
        with CaptureQueriesContext(connection) as many:
            self.assertEqual(http.get(reverse('home')).status_code, 200)
        self.assertEqual(len(one), len(many))
        self.assertLessEqual(len(many), 60)


# ------------------------------------------------------------------
# Conversations, reports, bookings
# ------------------------------------------------------------------

class InboxReportsBookingsTests(_Base):
    def setUp(self):
        super().setUp()
        self.http = login('admin_a')
        self.web = make_conv(self.a, 1, lead=self.lead, intent='precios', topic='precio del menú',
                             page_url='https://alfa.example/menu', escalation_reason='quiere humano')
        self.wa = Conversation.objects.create(client=self.a, channel='whatsapp',
                                              external_id='wa:h:20260301',
                                              contact_label='WhatsApp · termina en 1234')
        Survey.objects.create(conversation=self.web, score=4, comment='bien')
        Booking.objects.create(client=self.a, name='Maria', email=self.lead.email,
                               start=timezone.now() + timedelta(days=1))

    def test_conversations_channel_filter_and_label(self):
        resp = self.http.get(reverse('conversations') + '?channel=whatsapp')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([c.pk for c in resp.context['conversations']], [self.wa.pk])
        self.assertEqual(resp.context['cur_channel'], 'whatsapp')
        self.assertEqual(resp.context['conversations'][0].contact_label, 'WhatsApp · termina en 1234')
        self.assertEqual(resp.context['stats']['whatsapp'], 1)
        resp = self.http.get(reverse('conversations') + '?client=bravo')
        self.assertEqual(len(resp.context['conversations']), 0)

    def test_conversation_detail_context_panel(self):
        resp = self.http.get(reverse('conversation_detail', args=[self.web.pk]))
        self.assertEqual(resp.status_code, 200)
        ctx = resp.context['context']
        self.assertEqual(ctx['lead'], self.lead)
        self.assertEqual(ctx['booking'].name, 'Maria')
        self.assertEqual((ctx['intent'], ctx['intent_label'], ctx['topic']),
                         ('precios', 'Precios', 'precio del menú'))
        self.assertEqual(ctx['csat'].score, 4)
        self.assertEqual(ctx['escalation_reason'], 'quiere humano')
        self.assertEqual(ctx['page_url'], 'https://alfa.example/menu')
        self.assertEqual(ctx['channel'], 'web')
        resp = self.http.get(reverse('conversation_detail', args=[self.wa.pk]))
        self.assertEqual(resp.context['contact_label'], 'WhatsApp · termina en 1234')
        Client.objects.filter(pk=self.a.pk).update(clinic_mode=True)
        ctx = self.http.get(reverse('conversation_detail', args=[self.web.pk])).context['context']
        self.assertEqual((ctx['topic'], ctx['clinic']), ('', True))

    def test_reports_gets_crm_keys_and_keeps_the_old_ones(self):
        Subscription.objects.create(client=self.a, plan='pro', status='active')
        resp = self.http.get(reverse('reports'))
        self.assertEqual(resp.status_code, 200)
        ctx = resp.context
        for key in ('stats', 'allowance', 'value', 'msg_bars', 'conv_bars', 'intents', 'topics',
                    'kpis', 'channel_series', 'channel_chart', 'funnel', 'funnel_chart', 'peak',
                    'peak_chart', 'csat', 'resolution'):
            self.assertIn(key, ctx, key)
        self.assertEqual(ctx['funnel']['stages'][1]['n'], 1)
        self.assertEqual(ctx['csat']['n'], 1)
        self.assertEqual(set(ctx['channel_series'][0]), {'day', 'web', 'whatsapp', 'total'})
        flat = {}
        for c in resp.context:
            flat.update(c.flatten())
        walk_for_cost(self, flat)
        self.assertContains(resp, 'Lo que pagas')
        self.assertContains(resp, 'Detalle técnico')

    def test_bookings_week_grid(self):
        Client.objects.filter(pk=self.a.pk).update(enable_bookings=True)
        Booking.objects.create(client=self.a, name='Lunes', email='l@x.example',
                               start=pr(2026, 3, 9, 10, 30), duration_minutes=60)
        Booking.objects.create(client=self.a, name='Domingo', email='d@x.example',
                               start=pr(2026, 3, 15, 19, 0), status='confirmed')
        Booking.objects.create(client=self.b, name='Ajena', email='a@x.example',
                               start=pr(2026, 3, 10, 10, 0))
        resp = self.http.get(reverse('bookings') + '?week=2026-03-11')
        self.assertEqual(resp.status_code, 200)
        week = resp.context['week']
        self.assertEqual((week['start'].isoformat(), week['end'].isoformat()),
                         ('2026-03-09', '2026-03-15'))
        self.assertEqual((week['prev'], week['next']), ('2026-03-02', '2026-03-16'))
        self.assertEqual(len(week['days']), 7)
        self.assertEqual(week['count'], 2)
        self.assertEqual(week['days'][0]['label'], 'Lun 9')
        block = week['days'][0]['bookings'][0]
        self.assertEqual(block['booking'].name, 'Lunes')
        self.assertEqual((week['start_hour'], week['end_hour']), (8, 20))
        self.assertEqual(block['top_pct'], round(150 * 100 / (12 * 60), 2))
        self.assertEqual(block['height_pct'], round(60 * 100 / (12 * 60), 2))
        self.assertEqual(week['days'][6]['bookings'][0]['status_label'], 'Confirmada')
        self.assertEqual(sum(len(d['bookings']) for d in week['days']), 2)
        resp = self.http.get(reverse('bookings') + '?status=confirmed&week=2026-03-11')
        self.assertEqual(resp.context['week']['count'], 1)
        self.assertEqual(resp.context['cur_status'], 'confirmed')
        # No locations for this client: the agenda table has no Local column.
        self.assertContains(resp, '<th>Fecha y hora</th>')
        self.assertNotContains(resp, '<th>Local</th><th>Estado</th>')


# ------------------------------------------------------------------
# Digest
# ------------------------------------------------------------------

@override_settings(SITE_URL='https://dominiopr.com')
class DigestTests(_Base):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.overdue = make_lead(self.a, 'Vencida', email='vencida@x.example',
                                 follow_up_at=now - timedelta(days=1), assigned_to=self.agent_a)
        self.today = make_lead(self.a, 'DeHoy', follow_up_at=now)
        self.stale = make_lead(self.a, 'Viejo')
        ContactSubmission.objects.filter(pk=self.stale.pk).update(created_at=now - timedelta(hours=26))

    def run_cmd(self, *args):
        from io import StringIO
        out = StringIO()
        call_command('send_followup_digest', *args, stdout=out)
        return out.getvalue()

    def test_sends_once_per_day_to_members_with_pending_items(self):
        out = self.run_cmd()
        # admin_a and agent_a of alfa; nobody of bravo (nothing pending); never the employee.
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ['admin_a@example.com', 'agent_a@example.com'])
        msg = mail.outbox[0]
        self.assertIn('Alfa Café', msg.subject)
        self.assertIn('1 vencidos, 1 para hoy, 1 leads nuevos', msg.subject)
        body = msg.body
        for name in ('Vencida', 'DeHoy', 'Viejo'):
            self.assertIn(name, body)
        self.assertIn(f'https://dominiopr.com/dashboard/lead/{self.overdue.pk}/', body)
        self.assertIn('agent_a', body)
        self.assertNotIn('vencida@x.example', body)
        html = msg.alternatives[0][0]
        self.assertIn('Seguimientos vencidos', html)
        self.assertIn('2 resúmenes enviados', out)
        self.assertEqual(AuditEvent.objects.filter(action='digest.followup', client=self.a).count(), 2)
        # Second run the same day: nothing.
        out = self.run_cmd()
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn('2 ya enviados hoy', out)
        self.run_cmd('--force')
        self.assertEqual(len(mail.outbox), 4)

    def test_opt_out_and_nothing_pending(self):
        Membership.objects.filter(user=self.agent_a).update(daily_digest=False)
        self.run_cmd()
        self.assertEqual([m.to[0] for m in mail.outbox], ['admin_a@example.com'])
        mail.outbox.clear()
        ContactSubmission.objects.filter(client=self.a).delete()
        out = self.run_cmd('--force')
        self.assertEqual(mail.outbox, [])
        self.assertIn('0 resúmenes enviados', out)

    def test_dry_run_sends_and_records_nothing(self):
        out = self.run_cmd('--dry-run')
        self.assertEqual(mail.outbox, [])
        self.assertIn('[dry-run] alfa', out)
        self.assertFalse(AuditEvent.objects.filter(action='digest.followup').exists())


# ------------------------------------------------------------------
# WhatsApp: contact_label, source and anonymization
# ------------------------------------------------------------------

class WhatsAppCrmTests(_Base):
    def test_contact_label_only_keeps_the_last_four_digits(self):
        self.assertEqual(whatsapp.contact_label('17875551234'), 'WhatsApp · termina en 1234')
        self.assertEqual(whatsapp.contact_label('12'), '')
        chash = whatsapp.contact_hash('17875551234')
        conv = whatsapp.resolve_conversation(self.a, chash, wa_id='17875551234')
        self.assertEqual(conv.contact_label, 'WhatsApp · termina en 1234')
        self.assertNotIn('17875551234', conv.contact_label)
        self.assertEqual(whatsapp.resolve_conversation(self.a, chash), conv)  # reused

    @override_settings(DOMINIO_PHONE_HASH_KEY='k')
    def test_lead_captured_over_whatsapp_has_source_whatsapp(self):
        conv = Conversation.objects.create(client=self.a, channel='whatsapp', external_id='wa:h:1')
        request = RequestFactory().post('/api/whatsapp/webhook/')
        handlers, _profile = views.build_handlers(
            request, self.a, conv, actor_key='wa:h', channel='whatsapp', contact='17875551234')
        with patch('landing.views._send_lead_emails'):
            handlers['capture_lead']({'name': 'Luis', 'phone': '787-555-1234', 'summary': 'quiere info'})
        lead = ContactSubmission.objects.get(name='Luis')
        self.assertEqual((lead.source, lead.channel), ('whatsapp', 'whatsapp'))
        conv.refresh_from_db()
        self.assertEqual(conv.lead, lead)
        # The web widget keeps 'chat'.
        web = make_conv(self.a, 9)
        handlers, _profile = views.build_handlers(request, self.a, web, actor_key='ip')
        with patch('landing.views._send_lead_emails'):
            handlers['capture_lead']({'name': 'Web', 'phone': '787-555-0000'})
        self.assertEqual(ContactSubmission.objects.get(name='Web').source, 'chat')

    def test_anonymize_clears_contact_label(self):
        conv = Conversation.objects.create(client=self.a, channel='whatsapp', external_id='wa:h:2',
                                           contact_label='WhatsApp · termina en 1234', lead=self.lead)
        ChatMessage.objects.create(conversation=conv, role='user', position=0, content='hola')
        lead_service.anonymize_lead(self.lead)
        conv.refresh_from_db()
        self.assertEqual(conv.contact_label, '')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.name, lead_service.ANON_NAME)


# ------------------------------------------------------------------
# Export and booking actions keep the user's view
# ------------------------------------------------------------------

class ExportAndBookingReturnTests(_Base):
    def test_csv_export_applies_the_board_filters(self):
        wa = make_lead(self.a, 'Wilma', source='whatsapp', phone='+17875550002', email='')
        vip = LeadTag.objects.create(client=self.a, name='VIP')
        wa.tags.add(vip)
        wa.assigned_to = self.agent_a
        wa.save(update_fields=['assigned_to'])
        http = login('admin_a')
        body = http.get(reverse('export_leads') + '?channel=whatsapp').content.decode('utf-8-sig')
        self.assertIn('Wilma', body)
        self.assertNotIn('Maria', body)
        self.assertIn('Asignado a,Etiquetas,Próximo seguimiento', body)
        self.assertIn('agent_a', body)
        self.assertIn('VIP', body)
        body = http.get(reverse('export_leads') + f'?tag={vip.pk}').content.decode('utf-8-sig')
        self.assertIn('Wilma', body)
        self.assertNotIn('Maria', body)
        # A foreign client's slug narrows to nothing, never widens the scope.
        body = http.get(reverse('export_leads') + '?client=bravo').content.decode('utf-8-sig')
        self.assertNotIn('Pedro', body)

    def test_booking_status_returns_to_the_same_week_and_filters(self):
        Client.objects.filter(pk=self.a.pk).update(enable_bookings=True)
        b = Booking.objects.create(client=self.a, name='Lunes', email='l@x.example',
                                   start=pr(2026, 3, 9, 10, 30))
        http = login('admin_a')
        back = 'http://testserver' + reverse('bookings') + '?week=2026-03-09&status=pending'
        resp = http.post(reverse('booking_status', args=[b.pk]), {'status': 'confirmed'},
                         HTTP_REFERER=back)
        self.assertRedirects(resp, back, fetch_redirect_response=False)
        msgs = [m.message for m in resp.wsgi_request._messages]
        self.assertIn('Cita marcada como confirmada.', msgs)
        # A foreign referer never becomes an open redirect.
        b2 = Booking.objects.create(client=self.a, name='Martes', email='m@x.example',
                                    start=pr(2026, 3, 10, 10, 30))
        resp = http.post(reverse('booking_status', args=[b2.pk]), {'status': 'confirmed'},
                         HTTP_REFERER='https://evil.example/x')
        self.assertRedirects(resp, reverse('bookings'), fetch_redirect_response=False)


class CrmSecurityFixTests(_Base):
    """Security audit of the CRM redesign (2026-10-16): M1, M2, L2, L4."""

    def test_anonymize_clears_loss_reason_and_tags(self):
        tag = LeadTag.objects.create(client=self.a, name='Diabético')
        self.lead.tags.add(tag)
        http = login('agent_a')
        http.post(reverse('lead_status', args=[self.lead.pk]), {'status': 'lost'})
        http.post(reverse('lead_lost_reason', args=[self.lead.pk]),
                  {'lost_reason': 'Tiene diabetes tipo 2'})
        lead_service.anonymize_lead(self.lead)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.lost_reason, '')
        self.assertEqual(self.lead.tags.count(), 0)
        self.assertFalse(AuditEvent.objects.filter(result__icontains='diabetes').exists())

    def test_follow_up_dates_out_of_range_never_500(self):
        http = login('agent_a')
        for date in ('9999-12-31', '0001-01-01'):
            resp = http.post(reverse('lead_follow_up', args=[self.lead.pk]),
                             {'date': date, 'time': '23:59'})
            self.assertIn(resp.status_code, (302, 400), date)
            self.lead.refresh_from_db()
            self.assertIsNone(self.lead.follow_up_at, date)

    def test_same_tag_name_twice_reuses_it(self):
        http = login('agent_a')
        for _ in range(2):
            http.post(reverse('lead_tags', args=[self.lead.pk]), {'action': 'add', 'name': 'VIP'})
        self.assertEqual(LeadTag.objects.filter(client=self.a, name__iexact='vip').count(), 1)

    def test_audit_list_only_shows_clients_where_user_is_org_admin(self):
        Membership.objects.create(user=self.admin_a, client=self.b, role='agent')
        AuditEvent.objects.create(client=self.b, action='lead.tags', target='lead:1', result='+secreto-b')
        AuditEvent.objects.create(client=self.a, action='lead.tags', target='lead:2', result='+visible-a')
        resp = login('admin_a').get(reverse('audit'))
        self.assertContains(resp, 'visible-a')
        self.assertNotContains(resp, 'secreto-b')
