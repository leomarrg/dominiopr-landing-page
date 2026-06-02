from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import ContactSubmission


VALID_PAYLOAD = {
    'name': 'Ana Rivera',
    'email': 'ana@example.com',
    'company': 'Acme PR',
    'service': 'custom-platform',
    'budget': '3000-7500',
    'message': 'We need a booking platform.',
    'website': '',  # honeypot left empty
}


@override_settings(CONTACT_NOTIFY_EMAIL='leads@example.com')
class ContactFormTests(TestCase):
    def test_valid_submission_saves_and_emails(self):
        resp = self.client.post(reverse('index'), VALID_PAYLOAD)
        # Redirects back to #contact on success
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ContactSubmission.objects.count(), 1)
        sub = ContactSubmission.objects.get()
        self.assertEqual(sub.email, 'ana@example.com')
        self.assertEqual(sub.service, 'custom-platform')
        # Notification email was sent
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('Ana Rivera', mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ['leads@example.com'])

    def test_honeypot_blocks_spam(self):
        spam = dict(VALID_PAYLOAD, website='http://spam.example')
        resp = self.client.post(reverse('index'), spam)
        self.assertEqual(resp.status_code, 200)  # re-renders with error
        self.assertEqual(ContactSubmission.objects.count(), 0)

    def test_missing_required_field_does_not_save(self):
        bad = dict(VALID_PAYLOAD, email='')
        resp = self.client.post(reverse('index'), bad)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ContactSubmission.objects.count(), 0)

    def test_get_renders_form(self):
        resp = self.client.get(reverse('index'))
        self.assertEqual(resp.status_code, 200)
