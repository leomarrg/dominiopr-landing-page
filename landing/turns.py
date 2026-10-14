"""Channel-agnostic chat turn.

One visitor message in, one agent reply out, with the server-owned transcript,
cost and ledger handled the same way for every channel. Transports (the web
widget endpoint, the internal assistant page, the WhatsApp webhook) own what
is specific to them: parsing, authentication, abuse controls, quotas, how a
Conversation is resolved, and how an error becomes a response. They then call
run_turn() and never persist or bill a turn themselves.

Errors from the agent (AgentNotConfigured, anthropic.APIError and subclasses)
are raised to the caller unchanged; a partial usage attached to an APIError is
recorded in the ledger before re-raising.
"""
import logging
from dataclasses import dataclass

import anthropic
from django.conf import settings
from django.db import transaction
from django.db.models import F, Max
from django.utils import timezone

from . import agent
from . import usage as usage_ledger
from .models import ChatMessage, Conversation

logger = logging.getLogger(__name__)

# Bound of a single stored/sent message. Defined here (the core never imports
# a transport); views re-exports it for the web endpoint's body checks.
MAX_MESSAGE_CHARS = 2000

# Conversation.channel -> UsageEvent.kind
CHANNEL_KINDS = {
    'web': 'chat',
    'internal': 'internal_chat',
    'whatsapp': 'whatsapp',
}

# Conversation.channel -> agent.answer(channel=) delivery style. The internal
# assistant is a web page; only WhatsApp changes how replies are written.
_AGENT_CHANNEL = {'whatsapp': 'whatsapp'}


@dataclass
class TurnResult:
    reply: str
    usage: dict
    conversation: 'Conversation | None'


def channel_kind(channel):
    """UsageEvent.kind for a Conversation.channel. Unknown channels raise."""
    try:
        return CHANNEL_KINDS[channel]
    except KeyError:
        raise ValueError(f'Unknown chat channel: {channel!r}') from None


def build_history(conversation, user_text, max_turns=None, fallback_history=None):
    """The messages the model sees: the server-owned transcript plus the one
    new user turn, trimmed to the most recent `max_turns` and starting with a
    user turn (the Messages API requires it). When there IS a conversation the
    caller's view of the history is never trusted: the stored transcript is
    the only source of prior turns, so a visitor cannot feed the model a
    fabricated earlier reply. Without one (old widget with no session id, an
    unknown slug) `fallback_history` — the transport's already-sanitized
    prior turns, excluding the new message — keeps multi-turn context, as
    chat_api did before the turn moved here."""
    if max_turns is None:
        max_turns = getattr(settings, 'DOMINIO_AGENT_MAX_TURNS', 12)
    user_text = (user_text or '').strip()[:MAX_MESSAGE_CHARS]
    if conversation is not None:
        stored = list(conversation.chat_messages.order_by('position')
                      .values('role', 'content'))
    else:
        stored = [{'role': m['role'], 'content': m['content']} for m in (fallback_history or [])]
    history = (stored + [{'role': 'user', 'content': user_text}])[-max_turns:]
    while history and history[0]['role'] != 'user':
        history.pop(0)
    return history


def run_turn(client, conversation, user_text, *, channel, handlers,
             language=None, page_url='', clinic=None, profile=None,
             fallback_history=None):
    """Run one turn for `client` on `channel` and return a TurnResult.

    `conversation` may be None (nothing is persisted, the ledger still records
    the cost: e.g. an old widget without a session id). `handlers` are the
    tool callbacks the transport built (they carry the request-specific
    context). `clinic` None = Client.clinic_mode; `profile` None = 'web'.
    `page_url` fills Conversation.page_url when the row has none yet.

    Steps: history from the stored transcript -> agent.answer -> one
    transaction appending the two ChatMessage rows and bumping the
    Conversation counters -> usage ledger OUTSIDE that transaction.
    Persistence is best-effort (logged, never raised): the reply was already
    produced and paid for, and losing it would make the visitor repeat it.
    """
    kind = channel_kind(channel)
    user_text = (user_text or '').strip()[:MAX_MESSAGE_CHARS]
    if not user_text:
        raise ValueError('run_turn needs a non-empty user_text')
    if language is None:
        language = client.primary_language if client is not None else 'es'
    business_prompt = client.compiled_prompt() if client is not None else None

    # Only non-default kwargs are passed: existing test doubles for
    # agent.answer have the old fixed signature.
    extra = agent.clinic_kwargs(client) if clinic is None else ({'clinic': True} if clinic else {})
    if profile and profile != 'web':
        extra['profile'] = profile
    if channel in _AGENT_CHANNEL:
        extra['channel'] = _AGENT_CHANNEL[channel]

    history = build_history(conversation, user_text, fallback_history=fallback_history)
    try:
        reply, usage = agent.answer(
            history, business_prompt=business_prompt, handlers=handlers or {},
            language=language, **extra)
    except anthropic.APIError as exc:
        # Rounds that already ran were billed; keep the ledger honest.
        usage_ledger.record(kind, getattr(exc, 'usage', None), client=client,
                            conversation=conversation)
        raise

    if conversation is not None:
        _persist(conversation, client, user_text, reply, usage, page_url)

    usage_ledger.record(kind, usage, client=client, conversation=conversation)
    return TurnResult(reply=reply, usage=usage, conversation=conversation)


def _persist(conversation, client, user_text, reply, usage, page_url):
    """Append the two turns and bump the conversation counters, atomically.
    Append only: the stored transcript is never rewritten from a request.
    Handler side effects (state, lead) were written with .update(), so the
    .update() here cannot clobber them."""
    try:
        with transaction.atomic():
            next_pos = (conversation.chat_messages.aggregate(
                m=Max('position'))['m'] or -1) + 1
            ChatMessage.objects.bulk_create([
                ChatMessage(conversation=conversation, role='user',
                            content=user_text, position=next_pos),
                ChatMessage(conversation=conversation, role='assistant',
                            content=(reply or '')[:MAX_MESSAGE_CHARS],
                            position=next_pos + 1),
            ])
            updates = {'tokens_used': F('tokens_used') + agent.total_tokens(usage),
                       'last_message_at': timezone.now()}
            # The opening question feeds the operator control center. Clinic
            # tenants keep no free text outside the transcript.
            clinic = bool(client is not None and getattr(client, 'clinic_mode', False))
            if next_pos == 0 and not clinic:
                updates['first_question'] = user_text[:300]
            if page_url and not conversation.page_url:
                updates['page_url'] = page_url[:500]
            Conversation.objects.filter(pk=conversation.pk).update(**updates)
    except Exception:
        logger.exception('Failed to persist conversation %s', conversation.pk)
