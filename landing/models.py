import hashlib
import secrets

from django.conf import settings
from django.db import models


class Client(models.Model):
    """A business that DOMINIO runs an AI agent for (multi-tenant).

    Everything that makes an agent specific to a business lives here as DATA,
    not code: its knowledge, who it notifies, where its widget may run. The
    agent engine is shared; only this config changes per client. DOMINIO itself
    is client #1 of its own product.
    """
    slug = models.SlugField(unique=True, help_text='Public key used by the embed widget.')
    name = models.CharField(max_length=160)

    # The business-specific knowledge injected into the agent's system prompt
    # (about, services, pricing, anything it should know). The shared behaviour
    # rules (language, lead capture, security, tone) live in code.
    system_prompt = models.TextField(
        help_text="What the agent should know about this business: services, pricing, FAQ.")
    greeting = models.CharField(
        max_length=400, blank=True,
        help_text='First message the widget shows. Leave blank for a default.')

    # Where this client's captured leads are emailed.
    notify_email = models.EmailField()

    # The credential the embedded widget presents to /api/chat/. The slug must
    # NOT be used for this: it is slugify(company name), so it is guessable, and
    # /widget.js answers differently for real vs unknown keys, which makes it
    # enumerable. Without a real secret, anyone could burn a tenant's daily cap,
    # read their compiled prompt, or inject leads into their CRM.
    widget_token = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text='Secret the embedded widget sends to authenticate this tenant.')

    # Comma-separated domains the widget is allowed to run on (anti-abuse).
    # Blank = no origin restriction (use only for your own site while testing).
    allowed_origins = models.TextField(
        blank=True,
        help_text='Comma-separated domains allowed to embed the widget, e.g. example.com, www.example.com')

    # --- Widget appearance (two color pickers; everything else auto-derived) ---
    # Accent = launcher, user bubble, send button, links.
    primary_color = models.CharField('Accent color', max_length=9, default='#34d6c8')
    # Background = the chat panel. Its luminance decides light vs dark automatically
    # (light bg → dark text, dark bg → light text).
    surface_color = models.CharField('Background color', max_length=9, default='#12304a')
    # Header / top bar color (own pick for a two-tone look). Text on it is derived
    # for contrast. Default matches the dark panel's derived header.
    header_color = models.CharField('Header color', max_length=9, default='#0c2132')

    # When on, the agent can take reservations/appointments (the create_booking tool).
    enable_bookings = models.BooleanField(
        default=False, help_text='Let the agent take reservations / appointments.')
    is_active = models.BooleanField(default=True)
    # Set once the install-instructions email has been sent, so activating an
    # already-onboarded client doesn't re-send it.
    onboarding_sent = models.BooleanField(default=False)

    # --- Per-tenant operation knobs (Phase 2) ---
    LANGUAGE_CHOICES = [('es', 'Español (PR)'), ('en', 'English')]
    NOTIFY_CHANNEL_CHOICES = [
        ('email', 'Email'), ('whatsapp', 'Email + WhatsApp'), ('sms', 'Email + SMS')]
    # M-04: per-tenant daily chat quota. One noisy/attacked tenant must not be
    # able to exhaust the shared global budget for everyone else.
    daily_message_cap = models.PositiveIntegerField(
        default=300, help_text='Max chat messages per day for this agent (per-tenant quota).')
    # M-06: where to also notify (besides email). Blank phone = email only.
    notify_phone = models.CharField(max_length=30, blank=True)
    notify_channel = models.CharField(
        max_length=12, choices=NOTIFY_CHANNEL_CHOICES, default='email')
    # M-14: primary language of the agent for this business.
    primary_language = models.CharField(
        max_length=5, choices=LANGUAGE_CHOICES, default='es')
    # M-10: how long this tenant's conversations are kept before purge.
    retention_months = models.PositiveSmallIntegerField(
        default=12, help_text='Months to keep chat conversations before automatic purge.')

    # --- Self-serve provisioning (pay on Stripe -> tenant created by webhook) ---
    # DOMINIO installs the widget for the client (done-for-you). A client created
    # by the webhook starts 'pending' and staff flips it to 'live' once the
    # snippet is on the client's site; hand-made clients default to 'live'.
    SETUP_STATUS_CHOICES = [('pending', 'Pendiente de instalación'), ('live', 'En vivo')]
    PLATFORM_CHOICES = [
        ('wordpress', 'WordPress'), ('wix', 'Wix'), ('squarespace', 'Squarespace'),
        ('shopify', 'Shopify'), ('godaddy', 'GoDaddy Website Builder'),
        ('html', 'HTML / sitio a la medida'), ('other', 'Otra plataforma'),
        ('unknown', 'No sé'),
    ]
    setup_status = models.CharField(
        max_length=12, choices=SETUP_STATUS_CHOICES, default='live')
    website_url = models.URLField(blank=True)
    platform = models.CharField(max_length=40, choices=PLATFORM_CHOICES, blank=True)
    # How DOMINIO gets into the client's site (CMS access, contact person...).
    install_notes = models.TextField(blank=True)
    # Last time the widget script was served for this client (throttled write).
    widget_last_seen_at = models.DateTimeField(null=True, blank=True)
    live_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.slug})'

    def origin_list(self):
        return [o.strip().lower() for o in self.allowed_origins.split(',') if o.strip()]

    def ensure_widget_token(self):
        """Mint the widget credential on first use. Called wherever a widget is
        served so existing tenants heal without a data migration."""
        if not self.widget_token:
            token = secrets.token_hex(24)
            # update() not save(): callers may hold a stale instance.
            Client.objects.filter(pk=self.pk).update(widget_token=token)
            self.widget_token = token
        return self.widget_token

    def compiled_prompt(self, include_draft_pk=None):
        """The agent's business knowledge: the manual system_prompt plus every
        ACTIVE knowledge source (M-15), in position order. Deterministic so the
        result stays prompt-cache friendly. `include_draft_pk` lets the test
        console preview one draft source before activating it."""
        parts = [self.system_prompt.strip()] if self.system_prompt.strip() else []
        sources = self.knowledge_sources.filter(status='active')
        if include_draft_pk:
            sources = self.knowledge_sources.filter(
                models.Q(status='active') | models.Q(pk=include_draft_pk, status='draft'))
        for src in sources.order_by('position', 'pk'):
            text = src.compiled_text()
            if text:
                parts.append(f'=== {src.title} ===\n{text}')
        return '\n\n'.join(parts)

    def config_version(self):
        """Short fingerprint of the agent's effective configuration (M-19).
        Stamped on each conversation so any reply is attributable to the exact
        knowledge + greeting that produced it."""
        basis = (self.compiled_prompt() + '\n--\n' + (self.greeting or '')
                 + '\n--\n' + self.primary_language)
        return hashlib.sha1(basis.encode('utf-8')).hexdigest()[:12]


class ContactSubmission(models.Model):
    """A lead captured from the landing page contact form."""

    SERVICE_CHOICES = [
        ('operational-platform', 'Custom Operational Platforms'),
        ('data-dashboards', 'Data, Dashboards & Reporting Systems'),
        ('ai-automation', 'AI, Automation & Workflow Optimization'),
        ('landing-campaigns', 'Landing Pages, Forms & Campaign Systems'),
        ('consulting', 'Product Strategy & Technical Consulting'),
        ('it-services', 'Technology Management & IT Support'),
    ]
    BUDGET_CHOICES = [
        ('under-1000', 'Under $1,000'),
        ('1000-3000', '$1,000 - $3,000'),
        ('3000-7500', '$3,000 - $7,500'),
        ('7500-plus', '$7,500+'),
        ('not-sure', 'Not sure yet'),
    ]
    SOURCE_CHOICES = [
        ('contact_form', 'Contact form'),
        ('chat', 'AI chat'),
        ('signup', 'Agent signup'),
    ]
    STATUS_CHOICES = [
        ('new', 'New'),
        ('contacted', 'Contacted'),
        ('qualified', 'Qualified'),
        ('won', 'Won'),
        ('lost', 'Lost'),
    ]

    name = models.CharField(max_length=120)
    # A lead needs at least one way to reach them — email OR phone (enforced in
    # the form / capture handlers), so either may be blank on its own.
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    company = models.CharField(max_length=160, blank=True)
    service = models.CharField(max_length=40, choices=SERVICE_CHOICES)
    budget = models.CharField(max_length=40, choices=BUDGET_CHOICES, blank=True)
    message = models.TextField()

    # Which client this lead belongs to (null = DOMINIO's own site, pre-migration).
    # PROTECT, not SET_NULL: deleting a Client must never silently orphan a
    # tenant's lead PII into the unscoped null bucket. Reassign or archive first.
    client = models.ForeignKey(
        'Client', null=True, blank=True, on_delete=models.PROTECT, related_name='leads')

    # Where the lead came from, and how far along it is (mini-CRM).
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default='contact_form', db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new', db_index=True)
    # Exact page the visitor was on when the lead was captured (widget sends
    # location.href; forms fall back to the Referer header). Blank on old leads.
    page_url = models.CharField(max_length=500, blank=True)

    # Metadata (captured server-side, not from the user)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Contact submission'
        verbose_name_plural = 'Contact submissions'

    def __str__(self):
        return f'{self.name} <{self.email}> - {self.created_at:%Y-%m-%d %H:%M}'


class Booking(models.Model):
    """A reservation/appointment the agent can take. Double-booking is prevented
    at the database level by a partial unique constraint per (client, start)."""
    STATUS_CHOICES = [
        ('pending', 'Pending confirmation'),
        ('confirmed', 'Confirmed'),
        ('cancelled', 'Cancelled'),
        ('completed', 'Completed'),
        ('no_show', 'No show'),
    ]
    client = models.ForeignKey(
        'Client', null=True, blank=True, on_delete=models.PROTECT, related_name='bookings')
    lead = models.ForeignKey(
        ContactSubmission, null=True, blank=True, on_delete=models.SET_NULL, related_name='bookings')

    name = models.CharField(max_length=120)
    email = models.EmailField(db_index=True)
    service = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)

    # Absolute instant (aware, America/Puerto_Rico). Never store loose date+time strings.
    start = models.DateTimeField(db_index=True)
    duration_minutes = models.PositiveSmallIntegerField(default=30)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    # M-05: set by the reminder cron so a booking is reminded at most once.
    reminder_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start']
        constraints = [
            # One active booking per slot per client — DB-level anti double-booking.
            models.UniqueConstraint(
                fields=['client', 'start'],
                condition=models.Q(status__in=['pending', 'confirmed']),
                name='uniq_active_booking_per_slot',
            ),
        ]

    def __str__(self):
        return f'{self.name} @ {self.start:%Y-%m-%d %H:%M} ({self.status})'


class Membership(models.Model):
    """Links a login (User) to the Client whose data it may see.

    This is the identity layer on top of the multi-tenant data model: a user
    sees the leads of every Client they're a member of. DOMINIO staff
    (is_staff=True) need NO Membership — they see everything. A user with neither
    staff nor a membership sees nothing (safe default, never another tenant's data).

    A user can belong to SEVERAL clients (one login managing multiple agents), and
    a client can have several users — so this is a plain many-to-many bridge,
    unique per (user, client).
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='memberships')
    client = models.ForeignKey(
        Client, on_delete=models.CASCADE, related_name='members')
    # True right after auto-provisioning a brand-new login: the user still has the
    # emailed temp password. The dashboard forces a change before showing anything.
    must_change_password = models.BooleanField(default=False)
    # M-17: what this member may do inside their organization. org_admin manages
    # knowledge, users and settings; agent only works leads and conversations.
    ROLE_CHOICES = [('org_admin', 'Administrador'), ('agent', 'Agente')]
    role = models.CharField(max_length=12, choices=ROLE_CHOICES, default='org_admin')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'client'],
                                    name='uniq_membership_user_client'),
        ]

    def __str__(self):
        return f'{self.user} → {self.client.slug}'


# ============================================================
# PHASE 2 — platform entities (subscriptions, conversations,
# knowledge, audit, surveys, availability)
# ============================================================

class Subscription(models.Model):
    """M-01: a client's paid subscription. Stripe drives the automatic path;
    ATH Móvil / manual payments are one-shot with an explicit period end that
    the reconcile job watches."""
    PLAN_CHOICES = [('starter', 'Starter'), ('pro', 'Pro'),
                    ('scale', 'Scale'), ('custom', 'Custom')]
    PERIOD_CHOICES = [('monthly', 'Mensual'), ('annual', 'Anual')]
    METHOD_CHOICES = [('stripe', 'Stripe'), ('ath_movil', 'ATH Móvil'), ('manual', 'Manual')]
    STATUS_CHOICES = [
        ('incomplete', 'Incompleta'),      # checkout started, not paid yet
        ('active', 'Activa'),
        ('past_due', 'Pago vencido'),
        ('canceled', 'Cancelada'),
    ]

    client = models.OneToOneField(
        Client, on_delete=models.PROTECT, related_name='subscription')
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES)
    period = models.CharField(max_length=10, choices=PERIOD_CHOICES, default='monthly')
    method = models.CharField(max_length=12, choices=METHOD_CHOICES, default='stripe')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES,
                              default='incomplete', db_index=True)
    stripe_customer_id = models.CharField(max_length=80, blank=True)
    stripe_subscription_id = models.CharField(max_length=80, blank=True, db_index=True)
    # Checkout Session that created/linked this subscription (welcome-page lookup).
    # Indexed because this is the dedupe key for provisioning (the same checkout
    # re-delivered under a new event id) and the fast path on /bienvenida/.
    checkout_session_id = models.CharField(max_length=120, blank=True, db_index=True)
    # For Stripe: mirrored from the subscription. For ATH/manual: set by hand —
    # the reconcile job warns before it expires and marks past_due after.
    current_period_end = models.DateTimeField(null=True, blank=True)
    # What the card was actually charged on the FIRST invoice, in cents, copied
    # from the Checkout Session at provisioning time. The printed receipt used
    # to be derived from the price table, which silently ignored any promotion
    # code — a customer paying $1.34 with a coupon was shown $667.89. Stored
    # rather than fetched so the welcome page never waits on the Stripe API.
    # NULL = provisioned before this existed (or ATH/manual): fall back to the
    # price table.
    initial_subtotal_cents = models.IntegerField(null=True, blank=True)
    initial_discount_cents = models.IntegerField(null=True, blank=True)
    initial_tax_cents = models.IntegerField(null=True, blank=True)
    initial_total_cents = models.IntegerField(null=True, blank=True)
    # Did this checkout include the one-time install fee? The welcome email
    # must not offer "install it yourself" to someone who just paid us to do it.
    setup_fee_charged = models.BooleanField(default=False)
    # Our own order id (DOM-2026-0007), independent of Stripe's. Everything
    # identifying a sale used to be a Stripe id, so changing processor — or just
    # reconciling with an accountant — would leave nothing to point at.
    order_number = models.CharField(max_length=24, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.client.slug}: {self.plan}/{self.period} ({self.status})'


class ProcessedWebhookEvent(models.Model):
    """M-01: idempotency ledger — a Stripe event id is acted on at most once,
    no matter how many times Stripe retries the delivery.

    `handled_at` is what makes the slot *conditional on success*. Claiming the
    row up front and trusting a compensating delete on failure only works for
    Python exceptions: if the worker is OOM-killed mid-handler (a real risk on a
    512MB box), the row survives, Stripe's retry sees a duplicate, and a paid
    checkout is swallowed forever with nobody notified. An unfinished row is
    therefore treated as never-processed, and re-running is safe because
    provisioning dedupes on `Subscription.checkout_session_id`."""
    event_id = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    handled_at = models.DateTimeField(null=True, blank=True)


class Conversation(models.Model):
    """M-02/M-16: a persisted chat session for a tenant. The widget keeps the
    turn state; the server upserts this row per session id so the tenant can
    audit and follow up. The public demo NEVER creates one of these."""
    STATE_CHOICES = [
        ('active', 'Activa'),
        ('escalated', 'Escalada'),
        ('followup', 'En seguimiento'),
        ('closed_resolved', 'Cerrada — resuelta'),
        ('closed_unresolved', 'Cerrada — no resuelta'),
        ('abandoned', 'Abandonada'),
        ('blocked', 'Bloqueada'),
    ]
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name='conversations')
    # Random id generated by the widget per visitor session — the upsert key.
    widget_session = models.CharField(max_length=64, db_index=True)
    lead = models.ForeignKey(
        ContactSubmission, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='conversations')
    page_url = models.CharField(max_length=500, blank=True)
    state = models.CharField(max_length=20, choices=STATE_CHOICES,
                             default='active', db_index=True)
    # Why it was escalated, in the visitor's words (M-16 request_human).
    escalation_reason = models.CharField(max_length=300, blank=True)
    # Fingerprint of the agent config that produced the replies (M-19).
    config_version = models.CharField(max_length=16, blank=True)
    # Total API tokens consumed by this conversation (M-03 cost analytics).
    tokens_used = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_message_at']
        constraints = [
            models.UniqueConstraint(fields=['client', 'widget_session'],
                                    name='uniq_conversation_per_session'),
        ]

    def __str__(self):
        return f'{self.client.slug} · {self.widget_session[:8]} ({self.state})'


class ChatMessage(models.Model):
    """M-02: one turn of a persisted conversation."""
    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='chat_messages')
    role = models.CharField(max_length=12)  # 'user' | 'assistant'
    content = models.TextField()
    position = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position']
        constraints = [
            models.UniqueConstraint(fields=['conversation', 'position'],
                                    name='uniq_message_position'),
        ]


class KnowledgeSource(models.Model):
    """M-15: one approved piece of a tenant's knowledge base. The compiler
    concatenates ACTIVE sources (plus the legacy system_prompt) into the
    agent's context — no vector index until a tenant outgrows the prompt."""
    KIND_CHOICES = [
        ('text', 'Texto'), ('faq', 'Preguntas frecuentes (P: / R:)'),
        ('url', 'Página web (URL)'),
    ]
    STATUS_CHOICES = [
        ('draft', 'Borrador'), ('active', 'Activa'),
        ('error', 'Error'), ('archived', 'Archivada'),
    ]
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='knowledge_sources')
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default='text')
    title = models.CharField(max_length=160)
    # URL for kind='url'; free text/FAQ body otherwise.
    origin = models.CharField(max_length=500, blank=True)
    content = models.TextField(
        blank=True, help_text='Texto o FAQ. Para URL se llena automáticamente al procesar.')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES,
                              default='draft', db_index=True)
    error = models.CharField(max_length=300, blank=True)
    review_by = models.DateField(
        null=True, blank=True, help_text='Fecha en que este contenido debe revisarse.')
    position = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', 'pk']

    def __str__(self):
        return f'{self.client.slug} · {self.title} ({self.status})'

    def compiled_text(self):
        """Fragments if the processor produced them, else the raw content."""
        frags = list(self.fragments.order_by('position').values_list('content', flat=True))
        return '\n'.join(frags).strip() if frags else self.content.strip()


class KnowledgeFragment(models.Model):
    """M-15: clean extracted chunk of a source (e.g. fetched URL text)."""
    source = models.ForeignKey(
        KnowledgeSource, on_delete=models.CASCADE, related_name='fragments')
    content = models.TextField()
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['position']


class AuditEvent(models.Model):
    """M-17: who did what, where, with what result. Written from every critical
    admin action (knowledge, users, agents, exports, deletions)."""
    client = models.ForeignKey(
        Client, null=True, blank=True, on_delete=models.SET_NULL, related_name='audit_events')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='audit_events')
    action = models.CharField(max_length=60, db_index=True)
    target = models.CharField(max_length=200, blank=True)
    result = models.CharField(max_length=200, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.action} · {self.target} · {self.created_at:%Y-%m-%d %H:%M}'


class Survey(models.Model):
    """M-18: one optional CSAT rating per conversation."""
    conversation = models.OneToOneField(
        Conversation, on_delete=models.CASCADE, related_name='survey')
    score = models.PositiveSmallIntegerField()  # 1-5, validated at the endpoint
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class AvailabilityRule(models.Model):
    """M-05: a tenant's working window for one weekday (0=Monday … 6=Sunday).
    No rules configured = bookings behave exactly as before (any future slot)."""
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='availability_rules')
    weekday = models.PositiveSmallIntegerField()  # 0=Monday … 6=Sunday
    open_time = models.TimeField()
    close_time = models.TimeField()
    slot_minutes = models.PositiveSmallIntegerField(default=30)

    class Meta:
        ordering = ['weekday', 'open_time']
        constraints = [
            models.UniqueConstraint(fields=['client', 'weekday'],
                                    name='uniq_availability_per_weekday'),
        ]

    def __str__(self):
        days = ['lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom']
        return f'{self.client.slug} · {days[self.weekday]} {self.open_time}-{self.close_time}'
