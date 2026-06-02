import logging

from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMultiAlternatives
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse

from .forms import ContactForm

logger = logging.getLogger(__name__)


def _client_ip(request):
    """Real client IP, honoring the Nginx X-Forwarded-For header."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _send_html_email(subject, to, html_template, txt_template, context, reply_to=None):
    """Send a multipart (text + HTML) email. Logs and swallows errors."""
    try:
        html_body = render_to_string(html_template, context)
        text_body = render_to_string(txt_template, context)
        msg = EmailMultiAlternatives(
            subject,
            text_body,
            settings.DEFAULT_FROM_EMAIL,
            to,
            reply_to=reply_to,
        )
        msg.attach_alternative(html_body, 'text/html')
        msg.send(fail_silently=False)
    except Exception:
        # The lead is already saved in the DB; a failed email must not break the UX.
        logger.exception('Failed to send email "%s" to %s', subject, to)


def _send_lead_emails(submission):
    """Notify the DOMINIO team and send the visitor a styled confirmation."""
    notify_to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    context = {'submission': submission}

    # 1) Internal notification to the DOMINIO inbox.
    if notify_to:
        _send_html_email(
            subject=f'New lead from dominiopr.com: {submission.name}',
            to=[notify_to],
            html_template='landing/emails/lead_notification.html',
            txt_template='landing/emails/lead_notification.txt',
            context=context,
            reply_to=[submission.email],
        )

    # 2) Confirmation to the person who submitted the form.
    _send_html_email(
        subject='We received your request — DOMINIO',
        to=[submission.email],
        html_template='landing/emails/lead_confirmation.html',
        txt_template='landing/emails/lead_confirmation.txt',
        context=context,
        reply_to=[notify_to] if notify_to else None,
    )


def index(request):
    if request.method == 'POST':
        form = ContactForm(request.POST)
        if form.is_valid():
            submission = form.save(commit=False)
            submission.ip_address = _client_ip(request)
            submission.user_agent = request.META.get('HTTP_USER_AGENT', '')[:300]
            submission.save()
            _send_lead_emails(submission)
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
