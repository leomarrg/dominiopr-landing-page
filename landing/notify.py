"""
M-06 — outbound SMS notifications via Twilio, dependency-free.

Twilio is SMS only (decision 2026-10-14: WhatsApp goes direct through the
Cloud API of Meta, see landing/whatsapp.py). A tenant whose notify_channel is
still 'whatsapp' gets the same alert as SMS; nothing is sent through a Twilio
WhatsApp sender any more.

Only NOTIFICATIONS to the tenant ("you have a new lead") — the visitor's chat
stays in the widget. Fire-and-forget: any failure logs and falls back to the
email that was already sent, so an alert is never lost because of this channel.
Disabled entirely (silent no-op) until the TWILIO_* env vars are configured.
"""
import base64
import logging
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = 10


def _twilio_config():
    sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    return (sid, token) if sid and token else None


def _send(to, body, from_):
    cfg = _twilio_config()
    if not cfg or not to or not from_:
        return False
    sid, token = cfg
    url = f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json'
    data = urllib.parse.urlencode({'To': to, 'From': from_, 'Body': body[:1500]}).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    auth = base64.b64encode(f'{sid}:{token}'.encode()).decode()
    req.add_header('Authorization', f'Basic {auth}')
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT):
            return True
    except Exception:
        logger.exception('Twilio send to %s failed', to)
        return False


def send_to(phone, body, channel='sms'):
    """Send `body` to one E.164 number as an SMS. `channel` 'sms' and the
    legacy 'whatsapp' preference both go out as SMS (Twilio is SMS only);
    any other channel (e.g. 'email') is a no-op. Phase D uses it for routed
    messages (StaffContact) and for phone-only booking texts."""
    if not phone or channel not in ('sms', 'whatsapp'):
        return False
    return _send(phone, body, getattr(settings, 'TWILIO_FROM_SMS', ''))


def notify_client(client, body, phone=None, channel=None):
    """Send `body` to the client on their preferred extra channel (M-06).
    Email always goes out separately; this only adds an SMS on top.
    `phone`/`channel` override the client's defaults when the alert is
    routed to a specific person (StaffContact); a routed contact without a
    phone means no text, never a silent fallback to the owner's number."""
    if phone is not None or channel is not None:
        return send_to(phone or '', body, channel or 'sms')
    if client is None or not client.notify_phone:
        return False
    return send_to(client.notify_phone, body, client.notify_channel)
