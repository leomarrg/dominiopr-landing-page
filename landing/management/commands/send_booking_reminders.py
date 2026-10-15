"""M-05: remind the visitor ~24h before each active booking. Run hourly from cron:

    python manage.py send_booking_reminders

Email when the booking has one; otherwise by the channel it came from: a
WhatsApp utility template (Phase G, Meta Cloud API) or an SMS (Twilio,
landing.notify) for phone-only bookings (Phase D) -- one selector,
views.booking_notify_visitor. Idempotent via Booking.reminder_sent — a
booking is reminded at most once; a failed send is retried on the next run
while the booking is still inside the window.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from landing.models import Booking
from landing.views import booking_notify_visitor


class Command(BaseCommand):
    help = 'Send 24h reminders (email or SMS) for upcoming bookings (M-05).'

    def handle(self, *args, **options):
        now = timezone.now()
        window_start = now + timezone.timedelta(hours=20)
        window_end = now + timezone.timedelta(hours=28)
        qs = Booking.objects.filter(
            status__in=['pending', 'confirmed'],
            reminder_sent=False,
            start__gte=window_start, start__lt=window_end,
        ).select_related('client', 'location')
        sent = 0
        for b in qs:
            # Email, WhatsApp template or SMS by the booking's channel and
            # contact; True as well when there is nothing to remind by
            # (anonymized lead), which closes it out.
            ok = booking_notify_visitor(b, 'reminder')
            if ok:
                Booking.objects.filter(pk=b.pk).update(reminder_sent=True)
                sent += 1
        self.stdout.write(self.style.SUCCESS(f'{sent} recordatorios enviados.'))
