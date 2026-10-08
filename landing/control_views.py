"""Phase 3a -- staff control center: every tenant chatbot in one place.

Staff only (login_required + staff_required). Every page runs a fixed number
of queries no matter how many clients exist: grouped aggregates from
landing.analytics merged in Python, then sorted and paginated server-side.
"""
import csv
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import FileResponse, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import analytics, tenant_export
from . import usage as usage_ledger
from .models import AuditEvent, Client, UsageEvent
from .platform_views import _csv_safe
from .views import _audit, staff_required

PAGE_SIZE = 50
_CENT = Decimal('0.01')
_FOUR = Decimal('0.0001')

# sort key -> row field. Numeric columns sort descending on first click.
SORT_FIELDS = {
    'name': 'sort_name', 'created': 'created_at', 'revenue': 'revenue',
    'messages': 'messages', 'conversations': 'conversations', 'leads': 'leads',
    'escalated': 'escalated', 'bookings': 'bookings', 'tokens': 'tokens',
    'cost': 'cost', 'margin': 'margin',
}
TEXT_SORTS = {'name'}
DEFAULT_SORT = '-cost'


def _parse_sort(raw):
    raw = (raw or '').strip()
    key = raw.lstrip('-')
    if key not in SORT_FIELDS:
        return DEFAULT_SORT, DEFAULT_SORT.lstrip('-'), True
    return raw if raw.startswith('-') else key, key, raw.startswith('-')


def _sort_rows(rows, key, desc):
    """Sort in Python; rows whose value is None (unknown revenue/margin) always
    go last, alphabetically, whatever the direction."""
    field = SORT_FIELDS[key]
    known = [r for r in rows if r[field] is not None]
    unknown = [r for r in rows if r[field] is None]
    known.sort(key=lambda r: r['sort_name'])            # stable tie-break by name
    known.sort(key=lambda r: r[field], reverse=desc)
    unknown.sort(key=lambda r: r['sort_name'])
    return known + unknown


def _sort_links(period, current_key, desc):
    links = {}
    for key in SORT_FIELDS:
        if key == current_key:
            nxt = key if desc else f'-{key}'
        else:
            nxt = key if key in TEXT_SORTS else f'-{key}'
        links[key] = f'?period={period}&sort={nxt}'
    return links


def _heartbeat(client, now):
    seen = client.widget_last_seen_at
    if seen is None:
        return 'never'
    return 'ok' if now - seen < timedelta(hours=24) else 'stale'


def _subscription(client):
    # select_related caches a missing reverse one-to-one as "no row".
    return getattr(client, 'subscription', None)


def _margin(revenue, month_cost):
    if revenue is None:
        return None
    return (revenue - month_cost).quantize(_CENT)


@login_required
@staff_required
def control_overview(request):
    period, start, end = analytics.period_bounds(request.GET.get('period'))
    sort, sort_key, desc = _parse_sort(request.GET.get('sort'))
    now = timezone.now()

    clients = list(Client.objects.select_related('subscription'))
    vol = analytics.volume(None, start, end)
    if period == 'month':
        month_usage = {cid: {'cost': v['cost']} for cid, v in vol.items()}
    else:
        month_usage = usage_ledger.rollup_by_client(*usage_ledger.month_bounds())

    rows = []
    for c in clients:
        v = vol.get(c.pk) or analytics.empty_volume()
        sub = _subscription(c)
        revenue = analytics.plan_monthly_revenue(sub)
        month_cost = month_usage.get(c.pk, {}).get('cost', Decimal(0))
        rows.append({
            'client': c, 'sub': sub, 'sort_name': c.name.lower(), 'created_at': c.created_at,
            'revenue': revenue, 'month_cost': month_cost.quantize(_FOUR),
            'margin': _margin(revenue, month_cost),
            'messages': v['messages'], 'conversations': v['conversations'],
            'leads': v['leads'], 'escalated': v['escalated'], 'bookings': v['bookings'],
            'input': v['input'], 'output': v['output'],
            'cache': v['cache_read'] + v['cache_write'], 'tokens': v['tokens'],
            'cost': v['cost'].quantize(_FOUR), 'csat_avg': v['csat_avg'],
            'heartbeat': _heartbeat(c, now), 'origins': len(c.origin_list()),
        })

    # Totals cover every client plus the client-less bucket (public demo).
    demo = vol.get(None) or analytics.empty_volume()
    known_revenue = [r['revenue'] for r in rows if r['revenue'] is not None]
    mrr = sum(known_revenue, Decimal('0.00'))
    month_cost_total = sum((u.get('cost', Decimal(0)) for u in month_usage.values()), Decimal(0))
    totals = {
        'mrr': mrr.quantize(_CENT),
        'unpriced': sum(1 for r in rows if r['revenue'] is None),
        'cost': sum((v['cost'] for v in vol.values()), Decimal(0)).quantize(_FOUR),
        'month_cost': month_cost_total.quantize(_FOUR),
        'margin': (mrr - month_cost_total).quantize(_CENT),
        'conversations': sum(v['conversations'] for v in vol.values()),
        'messages': sum(v['messages'] for v in vol.values()),
        'leads': sum(v['leads'] for v in vol.values()),
        'escalated': sum(v['escalated'] for v in vol.values()),
        'bookings': sum(v['bookings'] for v in vol.values()),
        'clients': len(rows),
        'active_clients': sum(1 for r in rows if r['client'].is_active),
        # Platform-wide spend ceiling (chat requests/day across every tenant);
        # revisit as clients are added. Same cache key chat_api increments.
        'global_cap': getattr(settings, 'DOMINIO_AGENT_DAILY_CAP', 2000),
        'global_today': cache.get('chat:global:' + now.strftime('%Y%m%d')) or 0,
    }

    rows = _sort_rows(rows, sort_key, desc)
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get('page'))

    # Config columns only for the clients on this page: still one query each.
    page_clients = [r['client'] for r in page.object_list]
    page_ids = [c.pk for c in page_clients]
    knowledge = analytics.knowledge_stats(page_ids)
    roles = analytics.member_roles(page_ids)
    caps = analytics.today_cap_usage(page_clients)
    for r in page.object_list:
        pk = r['client'].pk
        r['knowledge'] = knowledge.get(pk, {'active': 0, 'draft': 0, 'chars': 0})
        r['roles'] = roles.get(pk, {'org_admin': 0, 'agent': 0})
        r['cap'] = caps.get(pk)

    return render(request, 'landing/dashboard_control.html', {
        'active_tab': 'control',
        'page': page, 'totals': totals,
        'demo': {'events': demo['events'], 'cost': demo['cost'].quantize(_FOUR),
                 'leads': demo['leads'], 'bookings': demo['bookings']},
        'period': period, 'periods': analytics.PERIODS,
        'period_label': analytics.PERIOD_LABELS[period],
        'start': start, 'end': end,
        'sort': sort, 'sort_key': sort_key, 'sort_desc': desc,
        'sort_links': _sort_links(period, sort_key, desc),
    })


# Shared with the tenant reports page (platform_views.reports).
_svg_bars = analytics.svg_bars
_hbars = analytics.hbars


@login_required
@staff_required
def control_client(request, slug):
    client = get_object_or_404(Client.objects.select_related('subscription'), slug=slug)
    period, start, end = analytics.period_bounds(request.GET.get('period'))
    now = timezone.now()

    # Opening questions are visitor text: audit the read, once per hour.
    try:
        first_view = cache.add(f'auditview:control:{request.user.pk}:{client.pk}', 1, 3600)
    except Exception:
        first_view = True
    if first_view:
        _audit(request, 'control.view', client=client, target=client.slug)

    sub = _subscription(client)
    revenue = analytics.plan_monthly_revenue(sub)
    vol = analytics.volume([client.pk], start, end).get(client.pk) or analytics.empty_volume()
    months = analytics.monthly_table(client.pk, months=12)
    month_cost = months[-1]['cost'] if months else Decimal(0)
    daily = analytics.daily_series(client.pk, days=30)
    peak_month = max((m['cost'] for m in months), default=0) or 1
    for m in months:
        m['pct'] = round(m['cost'] * 100 / peak_month) if m['cost'] else 0
        m['cost4'] = m['cost'].quantize(_FOUR)
        m['kind_list'] = [m['kinds'].get(k) for k, _label in UsageEvent.KIND_CHOICES]

    knowledge = analytics.knowledge_stats([client.pk]).get(
        client.pk, {'active': 0, 'draft': 0, 'chars': 0})
    members = list(client.members.select_related('user').order_by('role', 'user__username'))
    audits = list(AuditEvent.objects.filter(client=client).select_related('user')[:20])

    return render(request, 'landing/dashboard_control_client.html', {
        'active_tab': 'control',
        'c': client, 'sub': sub, 'revenue': revenue,
        'month_cost': month_cost.quantize(_FOUR),
        'margin': _margin(revenue, month_cost),
        'vol': vol, 'vol_cost': vol['cost'].quantize(_FOUR),
        'period': period, 'periods': analytics.PERIODS,
        'period_label': analytics.PERIOD_LABELS[period],
        'months': months, 'kinds': UsageEvent.KIND_CHOICES,
        'daily_bars': _svg_bars(daily, 'messages'),
        'daily_total': sum(d['messages'] for d in daily),
        'intents': _hbars(analytics.top_intents([client.pk], start, end)),
        # Clinic clients: never send stored topics to the template, even ones
        # tagged before clinic mode was switched on.
        'topics': ([] if client.clinic_mode
                   else _hbars(analytics.top_topics([client.pk], start, end, n=10))),
        'questions': ([] if client.clinic_mode
                      else analytics.recent_first_questions(client.pk, n=20)),
        'knowledge': knowledge, 'members': members, 'audits': audits,
        'cap': analytics.today_cap_usage([client]).get(client.pk),
        'origins': client.origin_list(),
        'heartbeat': _heartbeat(client, now),
    })


@login_required
@staff_required
def control_export(request):
    """Client x month usage (messages, tokens split, cost) as CSV. Audited."""
    try:
        months = max(1, min(36, int(request.GET.get('months', 12))))
    except (TypeError, ValueError):
        months = 12
    since = analytics.local_months(months)[0]
    rows = (
        UsageEvent.objects.filter(created_at__gte=since)
        .order_by().annotate(month=TruncMonth('created_at'))
        .values('month', 'client_id', 'client_slug', 'client__name')
        .annotate(events=Count('id'), messages=Count('id', filter=Q(kind='chat')),
                  input=Sum('input_tokens'), output=Sum('output_tokens'),
                  cache_read=Sum('cache_read_tokens'), cache_write=Sum('cache_write_tokens'),
                  cost=Sum('cost_usd'))
        .order_by('month', 'client_slug')
    )

    resp = HttpResponse(content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = 'attachment; filename="control-uso-mensual.csv"'
    resp.write('﻿')  # BOM so Excel opens accents correctly
    writer = csv.writer(resp)
    writer.writerow(['Mes', 'Cliente', 'Slug', 'Mensajes', 'Llamadas IA', 'Tokens entrada',
                     'Tokens salida', 'Cache lectura', 'Cache escritura', 'Costo USD'])
    n = 0
    for r in rows:
        month = timezone.localtime(r['month']) if timezone.is_aware(r['month']) else r['month']
        name = r['client__name'] or ('Demo público / sin cliente' if not r['client_slug']
                                     else f"{r['client_slug']} (eliminado)")
        writer.writerow([_csv_safe(v) for v in (
            month.strftime('%Y-%m'), name, r['client_slug'],
            r['messages'], r['events'],
            r['input'] or 0, r['output'] or 0, r['cache_read'] or 0, r['cache_write'] or 0,
            Decimal(r['cost'] or 0).quantize(Decimal('0.000001')))])
        n += 1
    _audit(request, 'control.export', target=f'{n} filas', result=f'meses={months}')
    return resp


@login_required
@staff_required
@require_POST
def control_export_tenant(request, slug):
    """Phase 7: download the full tenant export (same zip as the
    export_tenant command). POST so a link or prefetch can't trigger it.
    Built in a temporary file -- spooled to disk, never fully in memory --
    and streamed back."""
    client = get_object_or_404(Client, slug=slug)
    tmp = tempfile.TemporaryFile()
    try:
        counts = tenant_export.write_export(client, tmp)
    except Exception:
        tmp.close()
        _audit(request, 'tenant.export', client=client, target=client.slug, result='failed')
        raise
    tmp.seek(0)
    _audit(request, 'tenant.export', client=client, target=client.slug,
           result=' '.join(f'{k}={v}' for k, v in counts.items())[:200])
    return FileResponse(tmp, as_attachment=True, filename=tenant_export.filename_for(client),
                        content_type='application/zip')
