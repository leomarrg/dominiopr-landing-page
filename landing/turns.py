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

from . import agent, entitlements
from . import usage as usage_ledger
from .models import ChatMessage, Conversation

logger = logging.getLogger(__name__)

# English day names for the HOURS NOW line (the rules are written in English;
# the model says the day in the visitor's language).
_DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
_DAY_ABBR = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def _hours_summary(by_day):
    """'Mon-Fri 09:00-17:00; Sat 10:00-14:00; Sun closed' from
    {weekday: (open_time, close_time)}. Consecutive days with the same window
    (or both closed) collapse into a range."""
    groups = []  # [first_day, last_day, window]
    for day in range(7):
        window = by_day.get(day)
        if groups and groups[-1][2] == window and groups[-1][1] == day - 1:
            groups[-1][1] = day
        else:
            groups.append([day, day, window])
    parts = []
    for first, last, window in groups:
        label = _DAY_ABBR[first] if first == last else f'{_DAY_ABBR[first]}-{_DAY_ABBR[last]}'
        text = f'{window[0]:%H:%M}-{window[1]:%H:%M}' if window else 'closed'
        parts.append(f'{label} {text}')
    return '; '.join(parts)


def hours_now_line(client, now=None):
    """The receptionist's authoritative open/closed line (reglas-por-perfil B8):

        HOURS NOW (authoritative): OPEN NOW, closes today 17:00 | hours: Mon-Fri
        09:00-17:00; Sat 10:00-14:00; Sun closed (America/Puerto_Rico)
        HOURS NOW (authoritative): CLOSED NOW, opens Monday 09:00 | hours: ...

    Computed on the server in local (Puerto Rico) time from the client's
    AvailabilityRules; "opens today|tomorrow|<Weekday>" is never left to the
    model. '' when the client has no rules (the line is omitted). Half-open
    windows: open at open_time, closed at close_time."""
    if client is None:
        return ''
    rules = list(client.availability_rules.all())
    if not rules:
        return ''
    by_day = {r.weekday: (r.open_time, r.close_time) for r in rules if r.open_time < r.close_time}
    if not by_day:
        return ''
    local = timezone.localtime(now or timezone.now())
    today, clock = local.weekday(), local.time()

    window = by_day.get(today)
    if window and window[0] <= clock < window[1]:
        state = f'OPEN NOW, closes today {window[1]:%H:%M}'
    elif window and clock < window[0]:
        state = f'CLOSED NOW, opens today {window[0]:%H:%M}'
    else:
        state = 'CLOSED NOW'
        for offset in range(1, 8):
            nxt = by_day.get((today + offset) % 7)
            if nxt:
                when = 'tomorrow' if offset == 1 else _DAY_NAMES[(today + offset) % 7]
                state = f'CLOSED NOW, opens {when} {nxt[0]:%H:%M}'
                break
    return (f'HOURS NOW (authoritative): {state} | hours: {_hours_summary(by_day)} '
            f'({settings.TIME_ZONE})')

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


# Phase E: stands in for the internal knowledge when the business has not
# loaded any yet (see run_turn); the callers normally short-circuit before.
NO_INTERNAL_DOCS_PROMPT = '(Este negocio no ha cargado documentos internos todavía.)'


def run_turn(client, conversation, user_text, *, channel, handlers,
             language=None, page_url='', clinic=None, profile=None,
             fallback_history=None, business_prompt=None):
    """Run one turn for `client` on `channel` and return a TurnResult.

    `conversation` may be None (nothing is persisted, the ledger still records
    the cost: e.g. an old widget without a session id). `handlers` are the
    tool callbacks the transport built (they carry the request-specific
    context). `clinic` None = Client.clinic_mode; `profile` None = 'web'.
    `page_url` fills Conversation.page_url when the row has none yet.
    `business_prompt` None = Client.compiled_prompt() (the public knowledge);
    the internal assistant passes Client.compiled_internal_prompt().

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
    if business_prompt is None:
        business_prompt = client.compiled_prompt() if client is not None else None
    if profile == 'internal' and not (business_prompt or '').strip():
        # agent.answer falls back to DOMINIO_BUSINESS for an empty prompt. The
        # employees' assistant must never answer with DOMINIO's sales copy, so
        # a business without internal documents gets an explicit marker.
        business_prompt = NO_INTERNAL_DOCS_PROMPT

    # Only non-default kwargs are passed: existing test doubles for
    # agent.answer have the old fixed signature.
    extra = agent.clinic_kwargs(client) if clinic is None else ({'clinic': True} if clinic else {})
    if profile and profile != 'web':
        extra['profile'] = profile
    if channel in _AGENT_CHANNEL:
        extra['channel'] = _AGENT_CHANNEL[channel]
    if profile == 'receptionist':
        # The front desk never works out open/closed itself: the server states
        # it (uncached block, next to the date). Omitted without hours rules.
        hours = hours_now_line(client)
        if hours:
            extra['context_lines'] = [hours]

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
        first_turn = _persist(conversation, client, user_text, reply, usage, page_url)
        # Phase C: 80% / 100% monthly-allowance warnings. Best-effort and
        # only on a conversation's first turn; never blocks the reply.
        entitlements.after_turn(client, conversation, first_turn)

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
        return next_pos == 0
    except Exception:
        logger.exception('Failed to persist conversation %s', conversation.pk)
        return False
