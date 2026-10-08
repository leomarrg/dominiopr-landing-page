"""
Phase 6 — knowledge versioning + plan-gated publishing.

Canonical rules (views and the ingest cron call these; nothing else decides):

* A source and the sources linked to it through `replaces` form a LINEAGE.
  At most one source per lineage is active: publishing one version archives
  every other active version of the same lineage, in one transaction.
* Editing an active source never touches it: the edit lands in a draft copy
  (replaces=<active>) that goes live only when it is activated.
* Size caps on publish: MAX_SOURCE_CHARS per source and MAX_TOTAL_CHARS for
  all active sources after the swap. Sources already over the cap keep
  working (the page warns); staff can override.
* Who may publish (activate / restore / archive something live): staff, or an
  org_admin whose subscription is Pro/Scale/Custom and active or past_due.
  Everyone else saves drafts and DOMINIO is notified to review and publish.
"""
import logging

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from .models import AuditEvent, Client, KnowledgeFragment, KnowledgeSource, Membership

logger = logging.getLogger(__name__)

SELF_PUBLISH_PLANS = frozenset({'pro', 'scale', 'custom'})
SELF_PUBLISH_STATUSES = frozenset({'active', 'past_due'})
MAX_SOURCE_CHARS = 8_000
MAX_TOTAL_CHARS = 40_000
REVIEW_MESSAGE = ('Tu plan Starter: DOMINIO revisa y publica tus cambios '
                  '(normalmente en 1 día laborable).')
NOTIFY_TTL = 24 * 3600


class PublishError(Exception):
    """A publish that must not happen; the message is shown to the user."""


# ------------------------------------------------------------------
# Plan gate
# ------------------------------------------------------------------

def plan_allows_self_publish(client):
    try:
        sub = client.subscription
    except ObjectDoesNotExist:
        return False
    return sub.plan in SELF_PUBLISH_PLANS and sub.status in SELF_PUBLISH_STATUSES


def can_publish(user, client):
    """Staff always; an org_admin of `client` only on a self-publish plan.
    Agents and members of other tenants never."""
    if not getattr(user, 'is_authenticated', False) or client is None:
        return False
    if user.is_staff:
        return True
    if not Membership.objects.filter(user=user, client=client, role='org_admin').exists():
        return False
    return plan_allows_self_publish(client)


# ------------------------------------------------------------------
# Sizes
# ------------------------------------------------------------------

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


def size_report(client):
    """Warning data for the page: active sources over the per-source cap and
    the total size of what is live."""
    active = list(client.knowledge_sources.filter(status='active'))
    texts = compiled_texts(active)
    total = sum(len(t) for t in texts.values())
    over = [s for s in active if len(texts[s.pk]) > MAX_SOURCE_CHARS]
    return {'total_chars': total, 'over_total': total > MAX_TOTAL_CHARS,
            'over_sources': over, 'max_source': MAX_SOURCE_CHARS, 'max_total': MAX_TOTAL_CHARS}


def _check_caps(client, source, text, retiring_pks):
    if len(text) > MAX_SOURCE_CHARS:
        raise PublishError(
            f'"{source.title}" tiene {len(text):,} caracteres; el máximo por fuente es '
            f'{MAX_SOURCE_CHARS:,}. Divídela en fuentes más pequeñas.')
    others = (client.knowledge_sources.filter(status='active')
              .exclude(pk__in=set(retiring_pks) | {source.pk}))
    total = len(text) + sum(len(t) for t in compiled_texts(others).values())
    if total > MAX_TOTAL_CHARS:
        raise PublishError(
            f'Con esta fuente el conocimiento activo sumaría {total:,} caracteres; el máximo '
            f'es {MAX_TOTAL_CHARS:,}. Archiva o recorta otras fuentes primero.')


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
        replaces=source)


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


def publish(source, *, restoring=False, override_caps=False):
    """Make `source` the live version of its lineage. Draft -> active (or
    archived -> active when restoring); every other active version of the
    lineage is archived in the same transaction. Returns the titles retired.
    Raises PublishError with a user-facing message."""
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
                # DB); rolled back with everything else if a cap rejects it.
                src.replaces_id = guessed
                KnowledgeSource.objects.filter(pk=src.pk).update(replaces=guessed)
        lineage = src.lineage_ids()
        retiring = list(src.client.knowledge_sources.filter(
            status='active', pk__in=lineage - {src.pk}))
        if not override_caps:
            _check_caps(src.client, src, text, [r.pk for r in retiring])
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
# Review requests (Starter / no plan)
# ------------------------------------------------------------------

def request_review(source, request=None):
    """Email DOMINIO that `source` waits for review/publication. At most one
    email per source per day. Best-effort: never raises. Returns True if sent."""
    try:
        to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
        if not to:
            return False
        key = f'kpub:{source.pk}:{timezone.localdate().isoformat()}'
        if not cache.add(key, 1, NOTIFY_TTL):
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
    """Called by the ingest cron when a scan/upload lands as a draft: tenants
    who can't publish themselves get DOMINIO notified. Never raises."""
    try:
        if not plan_allows_self_publish(source.client):
            request_review(source)
    except Exception:
        logger.exception('Knowledge publish check failed for source %s', source.pk)
