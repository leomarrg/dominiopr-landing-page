"""
M-06 — outbound WhatsApp / SMS notifications via Twilio, dependency-free.

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


def notify_client(client, body):
    """Send `body` to the client on their preferred extra channel (M-06).
    Email always goes out separately; this only adds WhatsApp/SMS on top."""
    if client is None or not client.notify_phone:
        return False
    if client.notify_channel == 'whatsapp':
        from_ = getattr(settings, 'TWILIO_FROM_WHATSAPP', '')
        sent = _send(f'whatsapp:{client.notify_phone}', body, from_)
        if sent:
            return True
        # WhatsApp failed (template not approved, number not on WA) → try SMS.
        return _send(client.notify_phone, body,
                     getattr(settings, 'TWILIO_FROM_SMS', ''))
    if client.notify_channel == 'sms':
        return _send(client.notify_phone, body,
                     getattr(settings, 'TWILIO_FROM_SMS', ''))
    return False
