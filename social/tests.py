"""M-20 — regression suite for the social publishing queue.

The publishers are ALWAYS mocked: no test ever calls Meta or TikTok. What is
actually under test is the queue's contract — which rows are due, that a post
goes out at most once, that transient failures back off and permanent ones stop
— because those are the parts a bad night in production would expose.
"""
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from landing.models import AuditEvent, Client

from .admin import PostAdmin
from .models import Post
from .publishers import PublishError

MEDIA = 'https://cdn.example.com/post.jpg'

SOCIAL_CREDS = dict(
    SOCIAL_META_ACCESS_TOKEN='tok', SOCIAL_IG_USER_ID='17841400000000000',
    SOCIAL_FB_PAGE_ID='1000000000', SOCIAL_TIKTOK_ACCESS_TOKEN='tt-tok',
    SOCIAL_MAX_ATTEMPTS=3, SOCIAL_BACKOFF_CAP_SECONDS=1800,
)


def make_post(**kwargs):
    data = dict(platform='instagram', kind='feed', status='approved',
                caption='Hola PR', media_url=MEDIA,
                scheduled_at=timezone.now() - timezone.timedelta(minutes=1))
    data.update(kwargs)
    return Post.objects.create(**data)


@override_settings(**SOCIAL_CREDS)
class DueQueueTests(TestCase):
    """Which rows publish_due is allowed to touch."""

    def test_only_approved_and_overdue_are_due(self):
        due = make_post()
        make_post(status='draft')
        make_post(status='failed')
        make_post(scheduled_at=timezone.now() + timezone.timedelta(hours=2))
        self.assertEqual([p.pk for p in Post.due()], [due.pk])

    def test_post_without_schedule_goes_out_on_the_next_run(self):
        post = make_post(scheduled_at=None)
        self.assertIn(post, list(Post.due()))

    def test_published_post_is_never_due_again(self):
        make_post(status='published', external_id='123', published_at=timezone.now())
        self.assertEqual(list(Post.due()), [])

    def test_backoff_keeps_a_post_out_until_its_time(self):
        post = make_post(attempts=1,
                         next_attempt_at=timezone.now() + timezone.timedelta(minutes=5))
        self.assertNotIn(post, list(Post.due()))
        Post.objects.filter(pk=post.pk).update(
            next_attempt_at=timezone.now() - timezone.timedelta(seconds=1))
        self.assertIn(post, list(Post.due()))


@override_settings(**SOCIAL_CREDS)
class PublishDueTests(TestCase):

    def test_publishes_and_stores_external_id(self):
        post = make_post()
        with patch('social.publishers.publish', return_value=('IG_1', 'cont_1')) as pub:
            call_command('publish_due')
        pub.assert_called_once()
        post.refresh_from_db()
        self.assertEqual(post.status, 'published')
        self.assertEqual(post.external_id, 'IG_1')
        self.assertIsNotNone(post.published_at)
        self.assertEqual(post.error, '')
        self.assertIsNone(post.claimed_at)

    def test_second_run_does_not_publish_again(self):
        make_post()
        with patch('social.publishers.publish', return_value=('IG_1', '')) as pub:
            call_command('publish_due')
            call_command('publish_due')
        self.assertEqual(pub.call_count, 1)

    def test_a_row_claimed_by_a_live_runner_is_skipped(self):
        make_post(claimed_at=timezone.now())
        with patch('social.publishers.publish') as pub:
            call_command('publish_due')
        pub.assert_not_called()

    def test_a_stale_claim_is_taken_back(self):
        # The lease exists so an OOM-killed worker cannot park a post forever.
        post = make_post(claimed_at=timezone.now() - timezone.timedelta(hours=1))
        with patch('social.publishers.publish', return_value=('IG_1', '')):
            call_command('publish_due')
        post.refresh_from_db()
        self.assertEqual(post.status, 'published')

    def test_transient_failure_backs_off_and_stays_approved(self):
        post = make_post()
        with patch('social.publishers.publish',
                   side_effect=PublishError('502 Bad Gateway', retryable=True)):
            call_command('publish_due')
        post.refresh_from_db()
        self.assertEqual(post.status, 'approved')
        self.assertEqual(post.attempts, 1)
        self.assertIn('502', post.error)
        self.assertGreater(post.next_attempt_at, timezone.now())
        self.assertIsNone(post.claimed_at)

    def test_backoff_grows_between_attempts(self):
        post = make_post()
        delays = []
        with patch('social.publishers.publish',
                   side_effect=PublishError('boom', retryable=True)):
            for _ in range(2):
                Post.objects.filter(pk=post.pk).update(
                    next_attempt_at=timezone.now() - timezone.timedelta(seconds=1))
                before = timezone.now()
                call_command('publish_due')
                post.refresh_from_db()
                delays.append((post.next_attempt_at - before).total_seconds())
        self.assertGreater(delays[1], delays[0])

    def test_permanent_failure_fails_immediately_without_retrying(self):
        post = make_post()
        with patch('social.publishers.publish',
                   side_effect=PublishError('caption invalida', retryable=False)) as pub:
            call_command('publish_due')
        self.assertEqual(pub.call_count, 1)
        post.refresh_from_db()
        self.assertEqual(post.status, 'failed')
        self.assertIn('caption invalida', post.error)
        self.assertIsNone(post.next_attempt_at)

    def test_post_fails_after_the_attempt_budget_is_spent(self):
        post = make_post()
        with patch('social.publishers.publish',
                   side_effect=PublishError('timeout', retryable=True)):
            for _ in range(SOCIAL_CREDS['SOCIAL_MAX_ATTEMPTS']):
                Post.objects.filter(pk=post.pk).update(
                    next_attempt_at=timezone.now() - timezone.timedelta(seconds=1))
                call_command('publish_due')
        post.refresh_from_db()
        self.assertEqual(post.status, 'failed')
        self.assertEqual(post.attempts, SOCIAL_CREDS['SOCIAL_MAX_ATTEMPTS'])

    def test_still_processing_keeps_the_upload_token_and_resumes(self):
        """The whole point of remote_ref: the retry must not re-upload."""
        post = make_post()
        with patch('social.publishers.publish', return_value=('', 'cont_1')):
            call_command('publish_due')
        post.refresh_from_db()
        self.assertEqual(post.status, 'approved')
        self.assertEqual(post.remote_ref, 'cont_1')

        Post.objects.filter(pk=post.pk).update(
            next_attempt_at=timezone.now() - timezone.timedelta(seconds=1))
        with patch('social.publishers.publish', return_value=('IG_9', 'cont_1')) as pub:
            call_command('publish_due')
        self.assertEqual(pub.call_args[0][0].remote_ref, 'cont_1')
        post.refresh_from_db()
        self.assertEqual(post.external_id, 'IG_9')

    def test_dry_run_touches_nothing(self):
        post = make_post()
        with patch('social.publishers.publish') as pub:
            call_command('publish_due', '--dry-run')
        pub.assert_not_called()
        post.refresh_from_db()
        self.assertEqual(post.status, 'approved')

    def test_platform_filter(self):
        make_post(platform='facebook')
        with patch('social.publishers.publish') as pub:
            call_command('publish_due', '--platform', 'instagram')
        pub.assert_not_called()


@override_settings(**SOCIAL_CREDS)
class PublisherGuardTests(TestCase):
    """The refusals that must happen before any HTTP call is made."""

    def test_tiktok_stories_are_refused(self):
        post = Post(platform='tiktok', kind='story', media_url=MEDIA)
        with self.assertRaises(PublishError) as ctx:
            from . import publishers
            publishers.publish(post)
        self.assertFalse(ctx.exception.retryable)

    def test_non_https_media_is_refused(self):
        from . import publishers
        post = Post(platform='instagram', kind='feed', media_url='http://x.tld/a.jpg')
        with self.assertRaises(PublishError) as ctx:
            publishers.publish(post)
        self.assertFalse(ctx.exception.retryable)

    @override_settings(SOCIAL_META_ACCESS_TOKEN='', SOCIAL_IG_USER_ID='')
    def test_missing_credentials_fail_loudly_instead_of_silently(self):
        from . import publishers
        post = make_post()
        with self.assertRaises(PublishError):
            publishers.publish(post)


class ApproveActionTests(TestCase):

    def setUp(self):
        self.admin = PostAdmin(Post, AdminSite())
        self.user = get_user_model().objects.create_superuser(
            'staff', 'staff@example.com', 'x')
        self.request = RequestFactory().post('/admin/social/post/')
        self.request.user = self.user
        # message_user needs the messages framework; collect them instead.
        self.request._messages = type('M', (), {'add': lambda *a, **k: None})()

    def test_approve_resets_a_failed_post_to_a_clean_slate(self):
        post = make_post(status='failed', attempts=8, error='timeout',
                         remote_ref='cont_old',
                         next_attempt_at=timezone.now())
        self.admin.aprobar(self.request, Post.objects.filter(pk=post.pk))
        post.refresh_from_db()
        self.assertEqual(post.status, 'approved')
        self.assertEqual(post.attempts, 0)
        self.assertEqual(post.error, '')
        self.assertEqual(post.remote_ref, '')
        self.assertIsNone(post.next_attempt_at)

    def test_approve_never_touches_a_published_post(self):
        post = make_post(status='published', external_id='IG_1',
                         published_at=timezone.now())
        self.admin.aprobar(self.request, Post.objects.filter(pk=post.pk))
        post.refresh_from_db()
        self.assertEqual(post.status, 'published')
        self.assertEqual(post.external_id, 'IG_1')

    def test_approve_refuses_tiktok_stories(self):
        post = make_post(platform='tiktok', kind='story', status='draft')
        self.admin.aprobar(self.request, Post.objects.filter(pk=post.pk))
        post.refresh_from_db()
        self.assertEqual(post.status, 'draft')

    def test_approve_writes_an_audit_event(self):
        client = Client.objects.create(
            slug='acme', name='Acme', system_prompt='x', notify_email='a@b.com')
        post = make_post(status='draft', client=client)
        self.admin.aprobar(self.request, Post.objects.filter(pk=post.pk))
        event = AuditEvent.objects.get(action='social.approve')
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.client, client)
        self.assertIn(str(post.pk), event.target)
