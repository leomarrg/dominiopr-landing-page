"""M-05: remind the visitor ~24h before each active booking. Run hourly from cron:

    python manage.py send_booking_reminders

Email when the booking has one; SMS (Twilio, landing.notify) for phone-only
bookings (Phase D). Idempotent via Booking.reminder_sent — a booking is
reminded at most once; a failed send is retried on the next run while the
booking is still inside the window.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from landing import notify
from landing.models import Booking
from landing.views import _send_plain_email, booking_sms_text


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
            business = b.client.name if b.client else 'DOMINIO'
            local = timezone.localtime(b.start)
            # Phase F: which location, for multi-site businesses.
            where = f' en {b.location.name}' if b.location_id else ''
            if b.email:
                ok = _send_plain_email(
                    f'Recordatorio de tu cita — {business}',
                    [b.email],
                    (f'¡Hola {b.name}! Te recordamos tu cita con {business} '
                     f'mañana: {local:%A %d de %B a las %I:%M %p}{where}.\n\n'
                     f'Si necesitas cambiarla, respóndenos este email.\n\n— {business}'))
            elif b.phone:
                ok = notify.send_to(b.phone, booking_sms_text(b, business, reminder=True), 'sms')
            else:
                # Nothing to remind by (anonymized lead): close it out.
                ok = True
            if ok:
                Booking.objects.filter(pk=b.pk).update(reminder_sent=True)
                sent += 1
        self.stdout.write(self.style.SUCCESS(f'{sent} recordatorios enviados.'))
