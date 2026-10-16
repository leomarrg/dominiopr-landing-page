"""
Phase 6 — knowledge versioning + publishing (staff only since Phase I).

Canonical rules (views and the ingest cron call these; nothing else decides):

* A source and the sources linked to it through `replaces` form a LINEAGE.
  At most one source per lineage is active: publishing one version archives
  every other active version of the same lineage, in one transaction.
* Editing an active source never touches it: the edit lands in a draft copy
  (replaces=<active>) that goes live only when it is activated.
* Sizes: RECOMMENDED_SOURCE_CHARS / RECOMMENDED_TOTAL_CHARS are advice (the
  page warns and shows what the live knowledge costs per question). The only
  thing that blocks a publish is the hard ceiling DOMINIO_KNOWLEDGE_HARD_MAX_CHARS
  for all active sources after the swap: beyond it every chat request would
  carry a prompt the model cannot take, so nobody may override it.
* Who may publish: DOMINIO staff. Tenants do not manage their own knowledge;
  DOMINIO loads and maintains it (the knowledge views are staff-only).
"""
import logging
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import usage as usage_ledger
from .models import AuditEvent, Client, KnowledgeFragment, KnowledgeSource

logger = logging.getLogger(__name__)

# Advice thresholds: answers get sharper from short, focused sources. They do
# NOT block anything; see hard_max_chars() for what does.
RECOMMENDED_SOURCE_CHARS = 8_000
RECOMMENDED_TOTAL_CHARS = 40_000
DEFAULT_HARD_MAX_CHARS = 160_000  # ~40-50k tokens of business knowledge per prompt
MAX_HARD_MAX_CHARS = 400_000      # a .env cannot push past ~100k tokens: the chat would 400
CHARS_PER_TOKEN = 4               # rough estimate used for the per-question cost
NOTIFY_TTL = 24 * 3600
CLIENT_DAILY_REVIEW_EMAILS = 5


class PublishError(Exception):
    """A publish that must not happen; the message is shown to the user."""


# ------------------------------------------------------------------
# Who publishes
# ------------------------------------------------------------------

def can_publish(user, client):
    """Only DOMINIO staff. Kept as a function because views, templates and
    tests of other modules ask this question."""
    if not getattr(user, 'is_authenticated', False) or client is None:
        return False
    return bool(user.is_staff)


# ------------------------------------------------------------------
# Sizes
# ------------------------------------------------------------------

def hard_max_chars():
    """Physical ceiling for the active knowledge of one tenant (all sources,
    public and internal). Configurable between 1,000 and MAX_HARD_MAX_CHARS:
    above that the business block alone would crowd the model's context and
    multiply the cost of every question of that tenant."""
    try:
        value = int(getattr(settings, 'DOMINIO_KNOWLEDGE_HARD_MAX_CHARS', DEFAULT_HARD_MAX_CHARS))
    except (TypeError, ValueError):
        value = DEFAULT_HARD_MAX_CHARS
    return min(MAX_HARD_MAX_CHARS, max(1_000, value))


def compiled_texts(sources):
    """{pk: compiled_text()} for many sources with ONE fragments query
    (same result as KnowledgeSource.compiled_text)."""
    sources = list(sources)
    frags = {}
    for source_id, content in (KnowledgeFragment.objects
                               .filter(source_id__in=[s.pk for s in sources])
                               .order_by('source_id', 'position', 'pk')
                               .values_list('source_id', 'content')):
        frags.setdefault(source_id, []).append(content)
    return {s.pk: ('\n'.join(frags[s.pk]).strip() if s.pk in frags else s.content.strip())
            for s in sources}


def _money(value):
    """'$0.0012' style; amounts under a hundredth of a cent show as '<$0.0001'."""
    if value <= 0:
        return '$0.0000'
    if value < Decimal('0.0001'):
        return '<$0.0001'
    return f'${value:.4f}'


def cost_per_question(chars, model=None):
    """What `chars` of knowledge add to ONE chat question, in USD, at the
    input price of the chat model: (without cache, with the prefix read from
    cache). Tokens are estimated as chars / CHARS_PER_TOKEN. The rest of the
    prompt (rules, tools, history) is not included."""
    model = model or getattr(settings, 'DOMINIO_AGENT_MODEL', '') or ''
    tokens = chars // CHARS_PER_TOKEN
    prices = usage_ledger.prices_for(model) or {}
    blended = Decimal(str(getattr(settings, 'DOMINIO_COST_PER_MTOK', 0.10)))
    rate_in = Decimal(str(prices.get('input', blended)))
    rate_cached = Decimal(str(prices.get('cache_read', prices.get('input', blended))))
    million = Decimal(1_000_000)
    plain = (Decimal(tokens) * rate_in / million).quantize(Decimal('0.000001'))
    cached = (Decimal(tokens) * rate_cached / million).quantize(Decimal('0.000001'))
    return {'model': model, 'tokens': tokens, 'usd': plain, 'usd_cached': cached,
            'usd_display': _money(plain), 'usd_cached_display': _money(cached)}


def size_report(client):
    """Data for the page: how big the live knowledge is, what it costs per
    question, which sources are above the recommended size, and how close
    the tenant is to the hard ceiling."""
    active = list(client.knowledge_sources.filter(status='active'))
    texts = compiled_texts(active)
    total = sum(len(t) for t in texts.values())
    over = [s for s in active if len(texts[s.pk]) > RECOMMENDED_SOURCE_CHARS]
    hard = hard_max_chars()
    report = {
        'total_chars': total,
        'over_total': total > RECOMMENDED_TOTAL_CHARS,
        'over_sources': over,
        'recommended_source': RECOMMENDED_SOURCE_CHARS,
        'recommended_total': RECOMMENDED_TOTAL_CHARS,
        'hard_max': hard,
        'hard_pct': min(100, round(total * 100 / hard)) if hard else 0,
        'over_hard': total > hard,
    }
    report.update(cost_per_question(total))
    return report


def _check_hard_cap(client, source, text, retiring_pks):
    others = (client.knowledge_sources.filter(status='active')
              .exclude(pk__in=set(retiring_pks) | {source.pk}))
    total = len(text) + sum(len(t) for t in compiled_texts(others).values())
    hard = hard_max_chars()
    if total > hard:
        raise PublishError(
            f'Con esta fuente el conocimiento activo sumaría {total:,} caracteres '
            f'(≈ {total // CHARS_PER_TOKEN:,} tokens por pregunta); el techo técnico es '
            f'{hard:,}. Archiva o recorta otras fuentes primero.')


# ------------------------------------------------------------------
# Versioning
# ------------------------------------------------------------------

def open_draft_for(source):
    """The draft revision an edit of ACTIVE `source` writes into: the existing
    open one (draft or error) if any, else a new unsaved copy."""
    existing = (source.revisions.filter(status__in=('draft', 'error'))
                .order_by('-pk').first())
    if existing is not None:
        return existing
    return KnowledgeSource(
        client=source.client, kind=source.kind, title=source.title, origin=source.origin,
        content=source.content, position=source.position, review_by=source.review_by,
        audience=source.audience, replaces=source)


def _guess_replaced(source):
    """Ingest drafts (re-scan, re-upload) are new rows without a link: if
    exactly ONE active source has the same kind and origin, it is obviously
    the previous version. Ambiguous or no match -> None."""
    if source.kind not in KnowledgeSource.INGEST_KINDS or not source.origin:
        return None
    matches = list(source.client.knowledge_sources.filter(
        status='active', kind=source.kind, origin=source.origin,
    ).exclude(pk=source.pk).values_list('pk', flat=True)[:2])
    return matches[0] if len(matches) == 1 else None


def publish(source, *, restoring=False):
    """Make `source` the live version of its lineage. Draft -> active (or
    archived -> active when restoring); every other active version of the
    lineage is archived in the same transaction. Returns the titles retired.
    Raises PublishError with a user-facing message (bad state, empty text,
    or the hard size ceiling)."""
    allowed_from = 'archived' if restoring else 'draft'
    with transaction.atomic():
        # Serializes publishing per tenant (PostgreSQL): two concurrent
        # activations in one lineage can't both end up live.
        Client.objects.select_for_update().filter(pk=source.client_id).first()
        src = (KnowledgeSource.objects.select_for_update()
               .filter(pk=source.pk, client_id=source.client_id).first())
        if src is None or src.status != allowed_from:
            raise PublishError('Acción no permitida para el estado de esta fuente.')
        text = src.compiled_text()
        if not text:
            raise PublishError(f'"{src.title}" no tiene contenido para publicar.')
        if not restoring and src.replaces_id is None:
            guessed = _guess_replaced(src)
            if guessed:
                # Persisted before the lineage walk (it reads edges from the
                # DB); rolled back with everything else if the cap rejects it.
                src.replaces_id = guessed
                KnowledgeSource.objects.filter(pk=src.pk).update(replaces=guessed)
        lineage = src.lineage_ids()
        retiring = list(src.client.knowledge_sources.filter(
            status='active', pk__in=lineage - {src.pk}))
        _check_hard_cap(src.client, src, text, [r.pk for r in retiring])
        now = timezone.now()
        if retiring:
            # Kept intact (content included, even for files): it is a version
            # that can be restored, unlike an explicit "Archivar".
            KnowledgeSource.objects.filter(pk__in=[r.pk for r in retiring]).update(
                status='archived', updated_at=now)
        KnowledgeSource.objects.filter(pk=src.pk).update(
            status='active', error='', replaces=src.replaces_id, updated_at=now)
    return [r.title for r in retiring]


def delete_source(source):
    """Delete without breaking a lineage: revisions of the deleted version are
    re-linked to what it replaced (A <- B <- C, delete B => A <- C)."""
    with transaction.atomic():
        source.revisions.update(replaces=source.replaces_id)
        source.delete()


def history_for(sources, archived):
    """{pk of a displayed source: [archived versions of its lineage, newest
    first]} so each live/draft row can show its past versions. Pure Python
    over rows already loaded for the page."""
    rows = list(sources) + list(archived)
    neighbours = {}
    for s in rows:
        if s.replaces_id:
            neighbours.setdefault(s.pk, set()).add(s.replaces_id)
            neighbours.setdefault(s.replaces_id, set()).add(s.pk)
    archived_by_pk = {s.pk: s for s in archived}
    result = {}
    for s in sources:
        seen, stack = {s.pk}, [s.pk]
        while stack:
            for other in neighbours.get(stack.pop(), ()):
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        past = [archived_by_pk[pk] for pk in seen if pk in archived_by_pk]
        result[s.pk] = sorted(past, key=lambda a: (a.updated_at, a.pk), reverse=True)
    return result


# ------------------------------------------------------------------
# "Draft ready" notices to DOMINIO
# ------------------------------------------------------------------

def _incr(key, ttl):
    """Atomic-enough daily counter: add-then-incr, re-adding if the key was
    culled/expired between the two calls. Returns the new value."""
    cache.add(key, 0, ttl)
    try:
        return cache.incr(key)
    except ValueError:
        cache.add(key, 1, ttl)
        return cache.get(key, 1)


def request_review(source, request=None):
    """Email DOMINIO that `source` waits for review/publication (a finished
    scan or upload, a draft left by staff). At most one email per source per
    day, and at most CLIENT_DAILY_REVIEW_EMAILS per client per day, so a
    burst of drafts never turns this into a mail cannon. Best-effort: never
    raises. Returns True if sent."""
    try:
        to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
        if not to:
            return False
        day = timezone.localdate().isoformat()
        client_key = f'kpub:client:{source.client_id}:{day}'
        # Cheap pre-check first: a capped client writes no per-source keys.
        if (cache.get(client_key) or 0) >= CLIENT_DAILY_REVIEW_EMAILS:
            return False
        key = f'kpub:{source.pk}:{day}'
        if not cache.add(key, 1, NOTIFY_TTL):
            return False
        # Counted only for an email we are about to send, so re-asking for
        # the same draft does not use up the budget of the next one.
        if _incr(client_key, NOTIFY_TTL) > CLIENT_DAILY_REVIEW_EMAILS:
            return False
        client = source.client
        from .views import _audit, _send_plain_email  # lazy: views imports this app's modules
        link = settings.SITE_URL + reverse('knowledge') + f'?client={client.slug}'
        body = (f'{client.name} tiene conocimiento esperando revisión y publicación.\n\n'
                f'Fuente: {source.title}\n'
                f'Tipo: {source.get_kind_display()}\n'
                f'Revisar y publicar: {link}\n')
        sent = _send_plain_email(f'Publicar conocimiento — {client.name}', [to], body)
        result = 'email enviado' if sent else 'email falló'
        if request is not None:
            _audit(request, 'knowledge.publish_request', client=client,
                   target=source.title, result=result)
        else:
            AuditEvent.objects.create(client=client, action='knowledge.publish_request',
                                      target=source.title[:200], result=result)
        return sent
    except Exception:
        logger.exception('Knowledge publish request failed for source %s', source.pk)
        return False


def notify_ingest_draft(source):
    """Called by the ingest cron when a scan/upload lands as a draft: tell
    DOMINIO it is ready to review and publish. Never raises."""
    try:
        request_review(source)
    except Exception:
        logger.exception('Knowledge publish check failed for source %s', source.pk)
