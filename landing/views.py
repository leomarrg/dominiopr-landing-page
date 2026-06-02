import logging

from django.conf import settings
from django.contrib import messages
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse

from .forms import ContactForm

logger = logging.getLogger(__name__)


def _client_ip(request):
    """Real client IP, honoring the Nginx X-Forwarded-For header."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _notify_new_lead(submission):
    """Email the team when a new lead comes in. Never raises to the request."""
    recipient = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    if not recipient:
        return
    subject = f'New lead from dominiopr.com: {submission.name}'
    body = (
        f'Name:    {submission.name}\n'
        f'Email:   {submission.email}\n'
        f'Company: {submission.company or "-"}\n'
        f'Service: {submission.get_service_display()}\n'
        f'Budget:  {submission.get_budget_display() or "-"}\n'
        f'Date:    {submission.created_at:%Y-%m-%d %H:%M %Z}\n'
        f'IP:      {submission.ip_address or "-"}\n\n'
        f'Message:\n{submission.message}\n'
    )
    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [recipient],
            fail_silently=False,
        )
    except Exception:
        # The lead is already saved in the DB; a failed email must not break the UX.
        logger.exception('Failed to send contact notification email')


def index(request):
    if request.method == 'POST':
        form = ContactForm(request.POST)
        if form.is_valid():
            submission = form.save(commit=False)
            submission.ip_address = _client_ip(request)
            submission.user_agent = request.META.get('HTTP_USER_AGENT', '')[:300]
            submission.save()
            _notify_new_lead(submission)
            messages.success(
                request,
                'Thank you. We received your message and will get back to you soon.',
            )
            return redirect(reverse('index') + '#contact')
        messages.error(request, 'Please review the highlighted fields and try again.')
    else:
        form = ContactForm()
    return render(request, 'landing/index.html', {'form': form})


def terms(request):
    return render(request, 'landing/terms.html')


def privacy(request):
    return render(request, 'landing/privacy.html')
