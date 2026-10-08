"""Phase 6 — knowledge versioning + plan-gated publishing."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.urls import reverse

from . import ingest, knowledge, publishing
from .models import AuditEvent, Client, KnowledgeSource, Membership, Subscription

PW = 'pw12345!'


@override_settings(CONTACT_NOTIFY_EMAIL='ops@example.com', SITE_URL='https://dominiopr.com')
class VersioningBase(TestCase):
    def setUp(self):
        cache.clear()
        User = get_user_model()
        self.acme = Client.objects.create(slug='acme', name='Acme', system_prompt='Base.',
                                          notify_email='o@acme.com')
        self.other = Client.objects.create(slug='otro', name='Otro', notify_email='o@otro.com')
        self.admin = User.objects.create_user('admin@acme.com', password=PW)
        Membership.objects.create(user=self.admin, client=self.acme, role='org_admin')
        self.agent_user = User.objects.create_user('agent@acme.com', password=PW)
        Membership.objects.create(user=self.agent_user, client=self.acme, role='agent')
        self.outsider = User.objects.create_user('admin@otro.com', password=PW)
        Membership.objects.create(user=self.outsider, client=self.other, role='org_admin')
        self.staff = User.objects.create_user('ops@dominiopr.com', password=PW, is_staff=True)
        self.http = HttpClient()

    # helpers
    def plan(self, plan='pro', status='active', client=None):
        return Subscription.objects.create(client=client or self.acme, plan=plan, status=status)

    def login(self, user=None):
        self.http.force_login(user or self.admin)

    def src(self, content='Abrimos de 9 a 5.', status='active', **kw):
        defaults = dict(client=self.acme, kind='text', title='Horario', content=content,
                        status=status)
        defaults.update(kw)
        return KnowledgeSource.objects.create(**defaults)

    def save(self, pk=None, title='Horario', content='Abrimos de 8 a 6.', kind='text', **extra):
        data = {'client': 'acme', 'kind': kind, 'title': title, 'content': content}
        if pk:
            data['pk'] = pk
        data.update(extra)
        return self.http.post(reverse('knowledge_save'), data)

    def act(self, source, action, client='acme', **extra):
        return self.http.post(reverse('knowledge_action', args=[source.pk]),
                              {'client': client, 'action': action, **extra})

    def live(self):
        return self.acme.compiled_prompt()


class EditActiveSourceTests(VersioningBase):
    def setUp(self):
        super().setUp()
        self.plan('pro')
        self.login()

    def test_edit_of_active_creates_draft_and_keeps_live_prompt(self):
        active = self.src()
        before = self.live()
        self.save(pk=active.pk)
        active.refresh_from_db()
        self.assertEqual(active.status, 'active')
        self.assertEqual(active.content, 'Abrimos de 9 a 5.')
        draft = KnowledgeSource.objects.get(replaces=active)
        self.assertEqual((draft.status, draft.kind, draft.title, draft.position),
                         ('draft', 'text', 'Horario', active.position))
        self.assertTrue(draft.fragments.exists())
        self.assertEqual(self.live(), before)
        self.assertNotIn('8 a 6', self.live())

    def test_second_edit_reuses_the_open_draft(self):
        active = self.src()
        self.save(pk=active.pk, content='Primera edición.')
        self.save(pk=active.pk, content='Segunda edición.')
        drafts = KnowledgeSource.objects.filter(replaces=active)
        self.assertEqual(drafts.count(), 1)
        self.assertEqual(drafts.get().content, 'Segunda edición.')

    def test_edit_of_draft_updates_in_place(self):
        draft = self.src(status='draft')
        self.save(pk=draft.pk, content='Corregido.')
        self.assertEqual(KnowledgeSource.objects.count(), 1)
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.content), ('draft', 'Corregido.'))

    def test_activating_the_draft_swaps_and_archives(self):
        active = self.src()
        self.save(pk=active.pk)
        draft = KnowledgeSource.objects.get(replaces=active)
        self.act(draft, 'activate')
        active.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual((draft.status, active.status), ('active', 'archived'))
        self.assertIn('8 a 6', self.live())
        self.assertNotIn('9 a 5', self.live())

    def test_preview_excludes_the_replaced_source(self):
        active = self.src()
        self.save(pk=active.pk)
        draft = KnowledgeSource.objects.get(replaces=active)
        preview = self.acme.compiled_prompt(include_draft_pk=draft.pk)
        self.assertIn('8 a 6', preview)
        self.assertNotIn('9 a 5', preview)
        self.assertEqual(preview.count('=== Horario ==='), 1)
        self.assertIn('Base.', preview)
        # A bad pk from the form never breaks the preview.
        self.assertEqual(self.acme.compiled_prompt(include_draft_pk='abc'), self.live())

    def test_reprocess_of_active_url_writes_into_a_draft(self):
        active = self.src(kind='url', origin='https://acme.com/precios', content='')
        with patch.object(knowledge, 'fetch_url_text', return_value='Precio viejo $25.'):
            knowledge.process_source(active)
        self.assertIn('$25', self.live())
        with patch.object(knowledge, 'fetch_url_text', return_value='Precio nuevo $30.'):
            self.act(active, 'reprocess')
        active.refresh_from_db()
        self.assertEqual(active.status, 'active')
        draft = KnowledgeSource.objects.get(replaces=active)
        self.assertEqual(draft.status, 'draft')
        self.assertIn('$30', draft.compiled_text())
        self.assertNotIn('$30', self.live())


class RestoreAndHistoryTests(VersioningBase):
    def setUp(self):
        super().setUp()
        self.plan('scale')
        self.login()

    def chain(self):
        """v1 <- v2 <- v3 (v3 live)."""
        v1 = self.src(content='Versión uno.', status='archived')
        v2 = self.src(content='Versión dos.', status='archived', replaces=v1)
        v3 = self.src(content='Versión tres.', status='active', replaces=v2)
        return v1, v2, v3

    def test_restore_keeps_exactly_one_live_version(self):
        v1, v2, v3 = self.chain()
        self.act(v1, 'restore')
        statuses = dict(KnowledgeSource.objects.values_list('pk', 'status'))
        self.assertEqual(statuses, {v1.pk: 'active', v2.pk: 'archived', v3.pk: 'archived'})
        self.assertIn('Versión uno.', self.live())
        self.assertNotIn('Versión tres.', self.live())
        # And forward again.
        self.act(v3, 'restore')
        self.assertEqual(KnowledgeSource.objects.filter(status='active').get().pk, v3.pk)

    def test_restore_does_not_touch_other_lineages(self):
        v1, _, _ = self.chain()
        other = self.src(title='Precios', content='Corte $25.')
        self.act(v1, 'restore')
        other.refresh_from_db()
        self.assertEqual(other.status, 'active')

    def test_history_shown_with_restore_buttons(self):
        v1, v2, v3 = self.chain()
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, 'Versiones anteriores (2)')
        self.assertContains(resp, 'value="restore"')
        self.assertEqual(resp.context['sources'][0].history, [v2, v1])

    def test_deleting_a_middle_version_keeps_the_lineage(self):
        v1, v2, v3 = self.chain()
        self.act(v2, 'delete')
        v3.refresh_from_db()
        self.assertEqual(v3.replaces_id, v1.pk)
        self.act(v1, 'restore')
        v3.refresh_from_db()
        self.assertEqual(v3.status, 'archived')

    def test_restore_only_from_archived(self):
        draft = self.src(status='draft')
        self.act(draft, 'restore')
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')

    def test_rescan_draft_replaces_the_same_site_on_activation(self):
        old = self.src(kind='site', origin='https://acme.com/', title='Sitio web: acme.com',
                       content='Sitio viejo.')
        new = self.src(kind='site', origin='https://acme.com/', title='Sitio web: acme.com',
                       content='Sitio nuevo.', status='draft')
        self.act(new, 'activate')
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual((new.status, new.replaces_id, old.status), ('active', old.pk, 'archived'))

    def test_ambiguous_ingest_match_is_left_alone(self):
        a = self.src(kind='file', origin='archivo: menu.pdf', title='menu.pdf', content='A.')
        b = self.src(kind='file', origin='archivo: menu.pdf', title='menu.pdf', content='B.')
        new = self.src(kind='file', origin='archivo: menu.pdf', title='menu.pdf',
                       content='C.', status='draft')
        self.act(new, 'activate')
        new.refresh_from_db()
        self.assertEqual((new.status, new.replaces_id), ('active', None))
        self.assertEqual(KnowledgeSource.objects.filter(pk__in=[a.pk, b.pk],
                                                        status='active').count(), 2)


class SizeCapTests(VersioningBase):
    def setUp(self):
        super().setUp()
        self.plan('pro')

    def test_per_source_cap(self):
        self.login()
        big = self.src(content='x' * (publishing.MAX_SOURCE_CHARS + 1), status='draft')
        self.act(big, 'activate')
        big.refresh_from_db()
        self.assertEqual(big.status, 'draft')
        ok = self.src(content='x' * publishing.MAX_SOURCE_CHARS, status='draft', title='Ok')
        self.act(ok, 'activate')
        ok.refresh_from_db()
        self.assertEqual(ok.status, 'active')

    def test_total_cap_counts_the_swap(self):
        self.login()
        chunk = 'y' * publishing.MAX_SOURCE_CHARS
        live = [self.src(content=chunk, title=f'S{i}') for i in range(5)]  # exactly 40,000
        extra = self.src(content='z', status='draft', title='Extra')
        self.act(extra, 'activate')
        extra.refresh_from_db()
        self.assertEqual(extra.status, 'draft')
        # Replacing one of them with something the same size fits: the old one retires.
        swap = self.src(content='w' * publishing.MAX_SOURCE_CHARS, status='draft',
                        title='S0', replaces=live[0])
        self.act(swap, 'activate')
        swap.refresh_from_db()
        self.assertEqual(swap.status, 'active')

    def test_staff_override_and_org_admin_cannot_override(self):
        big = self.src(content='x' * (publishing.MAX_SOURCE_CHARS + 50), status='draft')
        self.login()
        self.act(big, 'activate', override_caps='1')
        big.refresh_from_db()
        self.assertEqual(big.status, 'draft')
        self.login(self.staff)
        self.act(big, 'activate', override_caps='1')
        big.refresh_from_db()
        self.assertEqual(big.status, 'active')
        self.assertTrue(AuditEvent.objects.filter(
            action='knowledge.activate', result__contains='límites ignorados').exists())

    def test_existing_over_cap_source_keeps_working_with_warning(self):
        self.src(content='q' * (publishing.MAX_SOURCE_CHARS + 10), title='Enorme')
        self.assertIn('qqqq', self.live())
        self.login()
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, 'por encima del límite recomendado')
        self.assertContains(resp, '"Enorme" supera')


class PlanGateTests(VersioningBase):
    def test_can_publish_matrix(self):
        self.assertFalse(publishing.can_publish(self.admin, self.acme))  # no subscription
        self.assertTrue(publishing.can_publish(self.staff, self.acme))
        sub = self.plan('starter')
        self.acme.refresh_from_db()
        self.assertFalse(publishing.can_publish(self.admin, self.acme))
        for plan, status, expected in (('pro', 'active', True), ('scale', 'past_due', True),
                                       ('custom', 'active', True), ('pro', 'canceled', False),
                                       ('pro', 'incomplete', False)):
            Subscription.objects.filter(pk=sub.pk).update(plan=plan, status=status)
            self.acme.refresh_from_db()
            self.assertEqual(publishing.can_publish(self.admin, self.acme), expected,
                             (plan, status))
        Subscription.objects.filter(pk=sub.pk).update(plan='pro', status='active')
        self.acme.refresh_from_db()
        self.assertFalse(publishing.can_publish(self.agent_user, self.acme))
        self.assertFalse(publishing.can_publish(self.outsider, self.acme))

    def test_starter_gets_403_on_activate_and_owner_notified_once(self):
        self.plan('starter')
        self.login()
        resp = self.http.post(reverse('knowledge_save'), {
            'client': 'acme', 'kind': 'text', 'title': 'Precios', 'content': 'Corte $25.'},
            follow=True)
        self.assertContains(resp, publishing.REVIEW_MESSAGE)
        draft = KnowledgeSource.objects.get()
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ['ops@example.com'])
        self.assertIn('Acme', msg.subject)
        self.assertIn('Precios', msg.body)
        self.assertIn('https://dominiopr.com/', msg.body)
        self.assertIn('?client=acme', msg.body)
        resp = self.act(draft, 'activate')
        self.assertEqual(resp.status_code, 403)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')
        self.save(pk=draft.pk, title='Precios', content='Corte $30.')  # same day: no new email
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(AuditEvent.objects.filter(action='knowledge.publish_request').count(), 1)
        self.assertNotIn('$30', self.live())

    def test_starter_cannot_archive_or_restore_live_sources_and_buttons_hidden(self):
        self.plan('starter')
        active = self.src()
        old = self.src(content='Viejo.', status='archived')
        active.replaces = old
        active.save()
        self.src(title='Borrador', content='B.', status='draft')
        self.login()
        self.assertEqual(self.act(active, 'archive').status_code, 403)
        self.assertEqual(self.act(old, 'restore').status_code, 403)
        active.refresh_from_db()
        self.assertEqual(active.status, 'active')
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, publishing.REVIEW_MESSAGE)
        for hidden in ('value="activate"', 'value="restore"', 'value="archive"'):
            self.assertNotContains(resp, hidden)

    def test_starter_can_still_delete_its_own_draft(self):
        draft = self.src(status='draft')
        self.login()
        self.act(draft, 'delete')
        self.assertFalse(KnowledgeSource.objects.exists())

    def test_no_plan_editing_active_source_still_keeps_it_live(self):
        active = self.src()
        self.login()
        self.save(pk=active.pk)
        active.refresh_from_db()
        self.assertEqual(active.status, 'active')
        self.assertEqual(len(mail.outbox), 1)

    def test_pro_org_admin_can_publish_and_no_email(self):
        self.plan('pro')
        self.login()
        self.save()
        draft = KnowledgeSource.objects.get()
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, 'value="activate"')
        self.assertNotContains(resp, publishing.REVIEW_MESSAGE)
        self.act(draft, 'activate')
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'active')
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_publishes_for_a_starter_tenant(self):
        self.plan('starter')
        draft = self.src(status='draft')
        self.login(self.staff)
        self.act(draft, 'activate')
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'active')

    def test_agent_role_cannot_publish(self):
        self.plan('pro')
        draft = self.src(status='draft')
        self.login(self.agent_user)
        resp = self.act(draft, 'activate')
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')

    def test_other_tenant_cannot_touch_the_source(self):
        self.plan('pro')
        self.plan('pro', client=self.other)
        active = self.src()
        self.login(self.outsider)
        self.assertEqual(self.act(active, 'archive', client='otro').status_code, 404)
        self.assertEqual(self.act(active, 'archive', client='acme').status_code, 302)
        resp = self.http.post(reverse('knowledge_save'), {
            'client': 'otro', 'pk': active.pk, 'kind': 'text', 'title': 'X', 'content': 'y'})
        self.assertEqual(resp.status_code, 404)
        active.refresh_from_db()
        self.assertEqual((active.status, active.content), ('active', 'Abrimos de 9 a 5.'))
        self.assertEqual(KnowledgeSource.objects.count(), 1)


class IngestNotificationTests(VersioningBase):
    def run_cron(self, source):
        with patch.object(ingest, 'run_source', return_value='draft'), \
                patch.object(ingest, 'claim', return_value=True), \
                patch.object(ingest, 'pending_ids', return_value=[source.pk]):
            ingest.process_pending(limit=1)

    def test_finished_scan_for_starter_notifies_owner_once(self):
        self.plan('starter')
        src = self.src(kind='site', status='draft', origin='https://acme.com/',
                       title='Sitio web: acme.com')
        self.run_cron(src)
        self.run_cron(src)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Sitio web: acme.com', mail.outbox[0].body)
        event = AuditEvent.objects.get(action='knowledge.publish_request')
        self.assertEqual((event.client, event.user), (self.acme, None))

    def test_finished_scan_for_pro_sends_nothing(self):
        self.plan('pro')
        src = self.src(kind='file', status='draft', origin='archivo: a.pdf', title='a.pdf')
        self.run_cron(src)
        self.assertEqual(len(mail.outbox), 0)
