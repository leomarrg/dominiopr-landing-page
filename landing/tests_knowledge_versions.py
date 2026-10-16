"""Phase 6 — knowledge versioning; Phase I — knowledge is staff-only and the
size caps are advice (only the hard ceiling blocks)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client as HttpClient
from django.test import TestCase, override_settings
from django.urls import reverse

from . import ingest, knowledge, platform_views, publishing
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
        """Staff by default: since Phase I nobody else reaches these views."""
        self.http.force_login(user or self.staff)

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
        # H1: the request only queues the draft copy; the cron fetches.
        with patch.object(knowledge, 'fetch_url_text',
                          side_effect=AssertionError('fetched inside the request')):
            self.act(active, 'reprocess')
        active.refresh_from_db()
        self.assertEqual(active.status, 'active')
        draft = KnowledgeSource.objects.get(replaces=active)
        self.assertEqual((draft.status, draft.progress), ('pending', 'En cola…'))
        with patch.object(knowledge, 'fetch_url_text', return_value='Precio nuevo $30.'):
            ingest.process_pending()
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')
        self.assertIn('$30', draft.compiled_text())
        self.assertNotIn('$30', self.live())

    def test_url_save_is_queued_and_the_cron_makes_the_draft(self):
        with patch.object(knowledge, 'fetch_url_text',
                          side_effect=AssertionError('fetched inside the request')):
            resp = self.save(kind='url', title='Precios', content='',
                             origin='https://acme.com/precios')
        self.assertEqual(resp.status_code, 302)
        src = KnowledgeSource.objects.get()
        self.assertEqual((src.kind, src.status, src.progress), ('url', 'pending', 'En cola…'))
        page = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(page, 'En cola… Se procesa en segundo plano')
        self.assertNotContains(page, 'value="reprocess"')  # nothing to re-queue yet
        with patch.object(knowledge, 'fetch_url_text', return_value='Corte $25.') as fetch:
            ingest.process_pending()
        fetch.assert_called_once_with('https://acme.com/precios')
        src.refresh_from_db()
        self.assertEqual((src.status, src.progress, src.attempts), ('draft', '', 1))
        self.assertIn('$25', src.compiled_text())
        self.assertNotIn('$25', self.live())  # draft, never active

    def test_queued_url_fetch_error_lands_in_error(self):
        self.save(kind='url', title='Precios', content='', origin='https://acme.com/precios')
        with patch.object(knowledge, 'fetch_url_text',
                          side_effect=ValueError('La página tardó demasiado.')):
            ingest.process_pending()
        src = KnowledgeSource.objects.get()
        self.assertEqual((src.status, src.error), ('error', 'La página tardó demasiado.'))

    def test_edit_while_the_url_draft_is_queued_is_refused(self):
        active = self.src(kind='url', origin='https://acme.com/precios', content='')
        self.act(active, 'reprocess')
        self.assertEqual(KnowledgeSource.objects.filter(replaces=active).count(), 1)
        self.save(pk=active.pk, kind='url', origin='https://acme.com/otra')
        self.act(active, 'reprocess')
        self.assertEqual(KnowledgeSource.objects.filter(replaces=active).count(), 1)
        self.assertEqual(KnowledgeSource.objects.get(replaces=active).origin,
                         'https://acme.com/precios')

    def test_url_queue_is_capped_per_client(self):
        for i in range(ingest.MAX_QUEUED_PER_CLIENT):
            self.src(kind='file', status='pending', title=f'{i}.txt', content='abc')
        self.save(kind='url', title='Precios', content='', origin='https://acme.com/precios')
        self.assertFalse(KnowledgeSource.objects.filter(kind='url').exists())

    def test_cron_without_api_key_still_processes_urls_only(self):
        from io import StringIO
        from django.core.management import call_command
        self.save(kind='url', title='Precios', content='', origin='https://acme.com/precios')
        f = self.src(kind='file', status='pending', title='a.txt', content='abc')
        with override_settings(ANTHROPIC_API_KEY=''), \
                patch.object(knowledge, 'fetch_url_text', return_value='Corte $25.'):
            call_command('process_knowledge', stdout=StringIO(), stderr=StringIO())
        self.assertEqual(KnowledgeSource.objects.get(kind='url').status, 'draft')
        f.refresh_from_db()
        self.assertEqual((f.status, f.attempts), ('pending', 0))  # untouched


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


class SizeAdviceTests(VersioningBase):
    """Phase I: 8k per source / 40k total are advice shown with the cost per
    question; only DOMINIO_KNOWLEDGE_HARD_MAX_CHARS blocks a publish."""

    def setUp(self):
        super().setUp()
        self.login()

    def test_source_over_the_recommended_size_still_publishes(self):
        big = self.src(content='x' * (publishing.RECOMMENDED_SOURCE_CHARS + 1), status='draft')
        self.act(big, 'activate')
        big.refresh_from_db()
        self.assertEqual(big.status, 'active')
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, 'supera los 8,000 caracteres recomendados')

    def test_total_over_the_recommended_size_still_publishes_with_a_warning(self):
        chunk = 'y' * publishing.RECOMMENDED_SOURCE_CHARS
        for i in range(5):
            self.src(content=chunk, title=f'S{i}')  # exactly 40,000 live
        extra = self.src(content='z' * 10, status='draft', title='Extra')
        self.act(extra, 'activate')
        extra.refresh_from_db()
        self.assertEqual(extra.status, 'active')
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, 'Por encima de lo recomendado en total')
        self.assertContains(resp, 'Se puede seguir publicando')

    @override_settings(DOMINIO_KNOWLEDGE_HARD_MAX_CHARS=20_000)
    def test_hard_ceiling_blocks_even_staff_and_counts_the_swap(self):
        self.src(content='a' * 15_000, title='Grande')
        over = self.src(content='b' * 6_000, status='draft', title='Demasiado')
        resp = self.http.post(reverse('knowledge_action', args=[over.pk]),
                              {'client': 'acme', 'action': 'activate'}, follow=True)
        over.refresh_from_db()
        self.assertEqual(over.status, 'draft')
        self.assertContains(resp, 'techo técnico es 20,000')
        self.assertContains(resp, 'sumaría 21,000 caracteres')
        # The same text REPLACING the big one fits: the old version retires.
        swap = self.src(content='c' * 15_500, status='draft', title='Grande',
                        replaces=KnowledgeSource.objects.get(title='Grande', status='active'))
        self.act(swap, 'activate')
        swap.refresh_from_db()
        self.assertEqual(swap.status, 'active')
        self.assertEqual(KnowledgeSource.objects.filter(status='active').count(), 1)
        # No override exists anymore: the old checkbox is gone from the page.
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertNotContains(resp, 'override_caps')

    @override_settings(DOMINIO_KNOWLEDGE_HARD_MAX_CHARS=10_000)
    def test_existing_over_ceiling_knowledge_keeps_working_and_is_flagged(self):
        self.src(content='q' * 12_000, title='Enorme')
        self.assertIn('qqqq', self.live())
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, 'Por encima del techo técnico')
        self.assertContains(resp, '100% usado')

    @override_settings(DOMINIO_AGENT_MODEL='claude-haiku-5-5',
                       DOMINIO_MODEL_PRICES={'claude-haiku-5-5': {
                           'input': 0.10, 'output': 0.50, 'cache_write': 0.125, 'cache_read': 0.01}})
    def test_size_report_shows_tokens_and_cost_per_question(self):
        self.src(content='k' * 49_200, title='Catálogo')
        report = publishing.size_report(self.acme)
        self.assertEqual((report['total_chars'], report['tokens']), (49_200, 12_300))
        self.assertEqual(str(report['usd']), '0.001230')
        self.assertEqual(str(report['usd_cached']), '0.000123')
        self.assertEqual((report['usd_display'], report['usd_cached_display']),
                         ('$0.0012', '$0.0001'))
        self.assertEqual((report['hard_max'], report['hard_pct'], report['over_hard']),
                         (160_000, 31, False))
        self.assertTrue(report['over_total'])
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, '49,200 caracteres ≈ 12,300 tokens → ~$0.0012 por pregunta sin caché '
                                  '($0.0001 con caché leída), con claude-haiku-5-5')

    def test_hard_max_setting_defaults_and_floor(self):
        with self.settings():
            from django.conf import settings
            del settings.DOMINIO_KNOWLEDGE_HARD_MAX_CHARS
            self.assertEqual(publishing.hard_max_chars(), 160_000)
        self.assertEqual(settings_value('DOMINIO_KNOWLEDGE_HARD_MAX_CHARS'), 160_000)
        with override_settings(DOMINIO_KNOWLEDGE_HARD_MAX_CHARS=5):
            self.assertEqual(publishing.hard_max_chars(), 1_000)
        # A .env cannot push the ceiling past the model-safe maximum.
        with override_settings(DOMINIO_KNOWLEDGE_HARD_MAX_CHARS=10_000_000):
            self.assertEqual(publishing.hard_max_chars(), 400_000)


def settings_value(name):
    from django.conf import settings
    return getattr(settings, name)


class StaffOnlyTests(VersioningBase):
    """Phase I: tenants do not manage their knowledge; DOMINIO does."""

    KNOWLEDGE_URLS = ('knowledge', 'knowledge_save', 'knowledge_scan', 'knowledge_upload',
                      'knowledge_test')

    def test_can_publish_is_staff_only_whatever_the_plan(self):
        self.assertTrue(publishing.can_publish(self.staff, self.acme))
        self.assertFalse(publishing.can_publish(self.admin, self.acme))
        self.plan('pro')
        self.acme.refresh_from_db()
        self.assertFalse(publishing.can_publish(self.admin, self.acme))
        self.assertFalse(publishing.can_publish(self.agent_user, self.acme))
        self.assertFalse(publishing.can_publish(self.outsider, self.acme))
        self.assertFalse(publishing.can_publish(self.staff, None))

    def test_org_admin_is_redirected_with_the_message_on_every_knowledge_url(self):
        self.plan('pro')
        draft = self.src(status='draft')
        self.login(self.admin)
        resp = self.http.get(reverse('knowledge'), follow=True)
        self.assertRedirects(resp, reverse('dashboard'))
        self.assertContains(resp, platform_views.KNOWLEDGE_CLIENT_MESSAGE)
        self.assertContains(resp, 'hola@dominiopr.com')
        self.assertEqual(self.save().status_code, 302)
        self.assertEqual(self.http.post(reverse('knowledge_scan'),
                                        {'client': 'acme', 'url': 'https://acme.com/'}).status_code, 302)
        self.assertEqual(self.http.post(reverse('knowledge_test'),
                                        {'client': 'acme', 'question': 'hola'}).status_code, 302)
        resp = self.act(draft, 'activate')
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')
        self.assertEqual(KnowledgeSource.objects.count(), 1)  # the save created nothing
        self.assertFalse(AuditEvent.objects.filter(action__startswith='knowledge.').exists())

    def test_org_admin_upload_is_refused_before_the_body_is_read(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.login(self.admin)
        with patch.object(ingest, 'BoundedMemoryUploadHandler',
                          side_effect=AssertionError('handler installed for a tenant')):
            resp = self.http.post(reverse('knowledge_upload'), {
                'client': 'acme', 'file': SimpleUploadedFile('a.txt', b'abc')}, follow=True)
        self.assertRedirects(resp, reverse('dashboard'))
        self.assertContains(resp, platform_views.KNOWLEDGE_CLIENT_MESSAGE)
        self.assertFalse(KnowledgeSource.objects.exists())

    def test_nav_shows_knowledge_to_staff_only(self):
        self.login(self.admin)
        resp = self.http.get(reverse('dashboard'))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, reverse('knowledge'))
        self.assertNotContains(resp, '>Conocimiento<')
        self.assertContains(resp, '>Usuarios<')  # the rest of the admin nav stays
        # Users, contacts and locations keep their own org_admin gate.
        self.assertEqual(self.http.get(reverse('dash_users') + '?client=acme').status_code, 200)
        self.login(self.staff)
        resp = self.http.get(reverse('dashboard'))
        self.assertContains(resp, '>Conocimiento<')

    def test_staff_sees_the_page_and_switches_tenants(self):
        self.src(title='De Acme', content='Acme abre a las 9.')
        self.src(title='De Otro', content='Otro abre a las 10.', client=self.other)
        self.login(self.staff)
        resp = self.http.get(reverse('knowledge'))  # no ?client: first tenant
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['client_obj'], self.acme)
        self.assertContains(resp, 'De Acme')
        self.assertNotContains(resp, 'De Otro')
        self.assertContains(resp, 'id="kn-client"')  # the tenant selector
        resp = self.http.get(reverse('knowledge') + '?client=otro')
        self.assertEqual(resp.context['client_obj'], self.other)
        self.assertContains(resp, 'De Otro')
        self.assertNotContains(resp, 'De Acme')
        # Unknown slug never falls back to another tenant (a stale form must
        # not land staff's action on the wrong agent): back to the dashboard.
        resp = self.http.get(reverse('knowledge') + '?client=nope', follow=True)
        self.assertRedirects(resp, reverse('dashboard'))
        self.assertContains(resp, 'No hay ningún agente con ese identificador')
        # POST picks the tenant from the form.
        self.http.post(reverse('knowledge_save'), {
            'client': 'otro', 'kind': 'text', 'title': 'Nuevo', 'content': 'x'})
        self.assertEqual(KnowledgeSource.objects.get(title='Nuevo').client, self.other)

    def test_staff_publishes_for_any_tenant_without_plan(self):
        draft = self.src(status='draft')
        self.login(self.staff)
        self.act(draft, 'activate')
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'active')
        self.assertEqual(len(mail.outbox), 0)  # nothing to ask DOMINIO: DOMINIO did it

    def test_agent_role_cannot_publish(self):
        self.plan('pro')
        draft = self.src(status='draft')
        self.login(self.agent_user)
        resp = self.act(draft, 'activate')
        self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
        draft.refresh_from_db()
        self.assertEqual(draft.status, 'draft')

    def test_other_tenant_admin_cannot_touch_the_source(self):
        self.plan('pro')
        self.plan('pro', client=self.other)
        active = self.src()
        self.login(self.outsider)
        for client in ('otro', 'acme'):
            resp = self.act(active, 'archive', client=client)
            self.assertRedirects(resp, reverse('dashboard'), fetch_redirect_response=False)
        resp = self.http.post(reverse('knowledge_save'), {
            'client': 'otro', 'pk': active.pk, 'kind': 'text', 'title': 'X', 'content': 'y'})
        self.assertEqual(resp.status_code, 302)
        active.refresh_from_db()
        self.assertEqual((active.status, active.content), ('active', 'Abrimos de 9 a 5.'))
        self.assertEqual(KnowledgeSource.objects.count(), 1)

    def test_page_copy_is_for_staff_not_the_tenant(self):
        self.login(self.staff)
        resp = self.http.get(reverse('knowledge') + '?client=acme')
        self.assertContains(resp, 'Solo staff de DOMINIO ve esta página')
        self.assertContains(resp, 'Escanear el sitio')
        self.assertNotContains(resp, 'Escanear mi sitio')
        self.assertNotContains(resp, 'DOMINIO lo publica')
        self.assertNotContains(resp, 'revisa y publica tus cambios')


class IngestNotificationTests(VersioningBase):
    """A finished scan/upload is a draft only staff can publish: DOMINIO gets
    one email per source per day, whatever the tenant's plan."""

    def run_cron(self, source):
        with patch.object(ingest, 'run_source', return_value='draft'), \
                patch.object(ingest, 'claim', return_value=True), \
                patch.object(ingest, 'pending_ids', return_value=[source.pk]):
            ingest.process_pending(limit=1)

    def test_finished_scan_notifies_dominio_once(self):
        self.plan('starter')
        src = self.src(kind='site', status='draft', origin='https://acme.com/',
                       title='Sitio web: acme.com')
        self.run_cron(src)
        self.run_cron(src)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Sitio web: acme.com', mail.outbox[0].body)
        self.assertIn('?client=acme', mail.outbox[0].body)
        event = AuditEvent.objects.get(action='knowledge.publish_request')
        self.assertEqual((event.client, event.user), (self.acme, None))

    def test_finished_upload_notifies_whatever_the_plan(self):
        self.plan('pro')
        src = self.src(kind='file', status='draft', origin='archivo: a.pdf', title='a.pdf')
        self.run_cron(src)
        self.assertEqual(len(mail.outbox), 1)

    def test_url_saved_by_staff_notifies_when_the_cron_makes_the_draft(self):
        self.login()
        resp = self.save(kind='url', title='Precios', content='',
                         origin='https://acme.com/precios')
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)  # nothing to review yet
        with patch.object(knowledge, 'fetch_url_text', return_value='Corte $25.'):
            ingest.process_pending()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Precios', mail.outbox[0].body)


class ReviewEmailCapTests(VersioningBase):
    """M3: a burst of drafts gets at most CLIENT_DAILY_REVIEW_EMAILS review
    emails a day."""

    def test_six_drafts_same_day_send_five_emails(self):
        drafts = [self.src(status='draft', title=f'Borrador {i}') for i in range(6)]
        sent = [publishing.request_review(d) for d in drafts]
        self.assertEqual(sent, [True] * 5 + [False])
        self.assertEqual(len(mail.outbox), 5)
        # Re-asking for an already-notified draft does not use up the budget...
        cache.clear()
        publishing.request_review(drafts[0])
        publishing.request_review(drafts[0])
        publishing.request_review(drafts[1])
        self.assertEqual(len(mail.outbox), 7)

    def test_cap_is_per_client(self):
        for i in range(5):
            publishing.request_review(self.src(status='draft', title=f'A{i}'))
        other = self.src(status='draft', title='Otro', client=self.other)
        self.assertTrue(publishing.request_review(other))
        self.assertEqual(len(mail.outbox), 6)


class KnowledgeActionThrottleTests(VersioningBase):
    """M1: knowledge_action is rate-limited like knowledge_save."""

    def test_21st_action_is_throttled(self):
        self.login()
        draft = self.src(status='draft')
        for _ in range(20):
            self.assertEqual(self.act(draft, 'noop').status_code, 302)
        resp = self.http.post(reverse('knowledge_action', args=[draft.pk]),
                              {'client': 'acme', 'action': 'noop'}, follow=True)
        self.assertContains(resp, 'Demasiados envíos seguidos')
        self.assertEqual(AuditEvent.objects.filter(action='knowledge.noop').count(), 20)


class LineageTenantTests(VersioningBase):
    """L4: a version can only replace a source of the same client."""

    def test_clean_rejects_cross_client_replaces(self):
        from django.core.exceptions import ValidationError
        theirs = self.src(client=self.other, title='De otro')
        mine = KnowledgeSource(client=self.acme, kind='text', title='Mía', content='x',
                               replaces=theirs)
        with self.assertRaises(ValidationError) as ctx:
            mine.full_clean()
        self.assertIn('replaces', ctx.exception.message_dict)
        ok = KnowledgeSource(client=self.acme, kind='text', title='Mía', content='x',
                             replaces=self.src())
        ok.full_clean()  # same client: fine
