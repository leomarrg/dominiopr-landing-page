"""Phase G: push a WhatsApp event through the webhook WITHOUT Meta.

    python manage.py wa_simulate --client acme --from 17875551234 --text "hola"
    python manage.py wa_simulate --client acme --from 17875551234 --image
    python manage.py wa_simulate --client acme --from 17875551234 --text "hola" --reply "Hola, dime."
    python manage.py wa_simulate --client acme --from 17875551234 --status

Builds the JSON exactly as Meta posts it, signs it with the local
META_APP_SECRET (a throwaway one when it is empty, so the flow can be tried
before the app exists), calls the view through RequestFactory with the
Graph API send mocked, and prints what WOULD have been sent (mark-as-read,
the reply, the fixed notices). Nothing reaches graph.facebook.com.

--reply skips the model with a canned answer (no ANTHROPIC_API_KEY, no
cost, no UsageEvent); without it the real agent runs when the key is set.
The event is persisted like a real one (Conversation, ChatMessage, caps,
and the usage ledger when the model ran): it lands in the tenant's history
and month count, so run it against a test tenant. Outside DEBUG it refuses
to run without --force. Each run uses a fresh wamid, so the idempotency
key never swallows it.
"""
import json
import secrets
import time
from unittest.mock import patch

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory, override_settings

from landing import agent, whatsapp
from landing.models import ChannelAccount, Client

WEBHOOK_PATH = '/api/whatsapp/webhook/'


def build_payload(phone_number_id, wa_id, text=None, image=False, status=False, wamid=None,
                  display_phone='17875550100', waba_id='100000000000000', caption=None,
                  timestamp=None):
    """The body of a Meta webhook POST for one inbound text message, one
    image (optionally captioned) or one delivery status. Shape per the Cloud
    API reference: entry[].changes[].value with metadata, contacts and
    messages[] (or statuses[])."""
    wamid = wamid or f'wamid.SIM{secrets.token_hex(12)}'
    ts = str(int(timestamp if timestamp is not None else time.time()))
    if status:
        value = {
            'messaging_product': 'whatsapp',
            'metadata': {'display_phone_number': display_phone,
                         'phone_number_id': phone_number_id},
            'statuses': [{'id': wamid, 'status': 'delivered', 'timestamp': ts,
                          'recipient_id': wa_id,
                          'conversation': {'id': 'conv.SIM', 'origin': {'type': 'service'}},
                          'pricing': {'billable': False, 'pricing_model': 'CBP',
                                      'category': 'service'}}],
        }
    else:
        message = {'from': wa_id, 'id': wamid, 'timestamp': ts}
        if image:
            message['type'] = 'image'
            message['image'] = {'mime_type': 'image/jpeg', 'sha256': 'x' * 64, 'id': '1'}
            if caption:
                message['image']['caption'] = caption
        else:
            message['type'] = 'text'
            message['text'] = {'body': text or ''}
        value = {
            'messaging_product': 'whatsapp',
            'metadata': {'display_phone_number': display_phone,
                         'phone_number_id': phone_number_id},
            'contacts': [{'profile': {'name': 'Visitante'}, 'wa_id': wa_id}],
            'messages': [message],
        }
    return {'object': 'whatsapp_business_account',
            'entry': [{'id': waba_id, 'changes': [{'value': value, 'field': 'messages'}]}]}


def signed_request(payload, secret=None, raw=None):
    """A RequestFactory POST of `payload` carrying the X-Hub-Signature-256
    Meta would compute. `raw` overrides the body bytes (to test escaping)."""
    raw = raw if raw is not None else json.dumps(payload).encode()
    return RequestFactory().post(
        WEBHOOK_PATH, data=raw, content_type='application/json',
        HTTP_X_HUB_SIGNATURE_256=whatsapp.sign(raw, secret))


class Command(BaseCommand):
    help = 'Simulate an inbound WhatsApp event through the webhook (Graph API mocked).'

    def add_arguments(self, parser):
        parser.add_argument('--client', required=True, help='Tenant slug.')
        parser.add_argument('--from', dest='wa_id', required=True,
                            help='Sender wa_id: E.164 digits without +, e.g. 17875551234.')
        parser.add_argument('--text', default='', help='Message body.')
        parser.add_argument('--image', action='store_true', help='Send an image instead of text '
                            '(--text becomes its caption).')
        parser.add_argument('--status', action='store_true',
                            help='Send a delivery status update instead of a message.')
        parser.add_argument('--reply', default='',
                            help='Canned agent reply (skips the model and the API key).')
        parser.add_argument('--phone-number-id', default='',
                            help="Override the tenant's ChannelAccount phone_number_id.")
        parser.add_argument('--force', action='store_true',
                            help='Required when DEBUG is False: the simulation writes a real '
                                 'conversation (and usage, without --reply) to the tenant.')

    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts['force']:
            raise CommandError(
                'This is not a dry run: the event creates a real Conversation in the '
                "tenant's history and month count (and a UsageEvent when the model runs). "
                'Use a test tenant and pass --force to run it outside DEBUG.')
        client = Client.objects.filter(slug=opts['client']).first()
        if client is None:
            raise CommandError(f'No client with slug {opts["client"]!r}.')
        wa_id = opts['wa_id'].strip().lstrip('+')
        if not wa_id.isdigit():
            raise CommandError('--from must be digits only (E.164 without +).')
        phone_number_id = opts['phone_number_id']
        if not phone_number_id:
            account = whatsapp.account_for(client)
            if account is None:
                raise CommandError(
                    f'{client.slug} has no active ChannelAccount; create one in the admin '
                    '(or pass --phone-number-id to simulate an unknown number).')
            phone_number_id = account.phone_number_id
        if not opts['status'] and not opts['image'] and not opts['text'].strip():
            raise CommandError('Give --text, --image or --status.')

        payload = build_payload(phone_number_id, wa_id, text=opts['text'], image=opts['image'],
                                status=opts['status'],
                                caption=opts['text'] if opts['image'] else None)
        secret = getattr(settings, 'META_APP_SECRET', '')
        overrides = {}
        if not secret:
            secret = 'wa-simulate-local-secret'
            overrides['META_APP_SECRET'] = secret
            self.stdout.write(self.style.WARNING(
                'META_APP_SECRET is empty: signing with a throwaway secret for this run.'))
        request = signed_request(payload, secret)

        sent = []

        def fake_post(path, body, retry=True):
            sent.append((path, body))
            return {'messaging_product': 'whatsapp',
                    'messages': [{'id': f'wamid.OUT{len(sent)}'}]}

        patches = [patch.object(whatsapp, '_graph_post', side_effect=fake_post)]
        if opts['reply']:
            reply = opts['reply']

            def canned(history, business_prompt=None, handlers=None, language='es', **kwargs):
                return reply, agent.new_usage('simulated')
            patches.append(patch.object(agent, 'answer', side_effect=canned))

        self.stdout.write(f'POST {WEBHOOK_PATH} as {phone_number_id} from {wa_id}:')
        self.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False))
        with override_settings(**overrides):
            for p in patches:
                p.start()
            try:
                response = whatsapp.whatsapp_webhook(request)
            finally:
                for p in reversed(patches):
                    p.stop()
        self.stdout.write('')
        self.stdout.write(f'HTTP {response.status_code} {response.content[:200].decode(errors="replace")}')
        self.stdout.write(self.style.WARNING(
            f'Note: a handled message stays in {client.slug}\'s conversation history and '
            'month count' + (' (no UsageEvent: --reply skipped the model).' if opts['reply']
                             else ' and, if the model ran, in the usage ledger.')))
        if not sent:
            self.stdout.write(self.style.WARNING(
                'Nothing would have been sent (status event, duplicate, unknown number, '
                'plan without WhatsApp, opt-out, cap or ignored type). See the log.'))
            return
        self.stdout.write(self.style.SUCCESS(f'{len(sent)} Graph API call(s) that would go out:'))
        for path, body in sent:
            self.stdout.write(f'  POST /{settings.META_GRAPH_VERSION}/{path}')
            self.stdout.write('  ' + json.dumps(body, ensure_ascii=False, indent=2)
                              .replace('\n', '\n  '))
