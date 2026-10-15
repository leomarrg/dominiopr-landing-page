import hashlib
import json
import secrets
import unicodedata

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


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
    # Health businesses: minimizes the health data the agent asks for, stores and
    # forwards. Data minimization only — it does not make the service HIPAA-ready.
    clinic_mode = models.BooleanField(
        'Modo clínica', default=False,
        help_text=('Para clínicas y consultorios: el agente no pide ni repite síntomas, '
                   'diagnósticos, medicamentos ni datos de seguro médico, y los avisos no '
                   'incluyen el texto del chat. Reduce los datos de salud que se recogen; '
                   'NO hace el servicio compatible con HIPAA.'))
    # Phase D: which job the shared engine does for this business (agent.PROFILES).
    # 'web' = service + lead capture (the original widget); 'receptionist' =
    # answers, books and takes messages (take_message) instead of capturing leads.
    AGENT_PROFILE_CHOICES = [
        ('web', 'Web: servicio al cliente + leads'),
        ('receptionist', 'Recepcionista: citas y recados'),
    ]
    agent_profile = models.CharField(
        'Perfil del agente', max_length=16, choices=AGENT_PROFILE_CHOICES, default='web',
        help_text=('Recepcionista: contesta con el horario del negocio, agenda citas y toma '
                   'recados para el equipo (no captura leads ni escala a humano).'))
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
    # Phase C: per-client deviations from the plan table (custom deals). Only
    # the keys landing.entitlements knows are honoured, e.g.
    # {"monthly_conversations": 2000, "seats": 8}. Empty = the plan as sold.
    entitlement_overrides = models.JSONField(
        blank=True, default=dict,
        help_text=('Ajustes por cliente sobre lo que incluye su plan (JSON): '
                   'monthly_conversations, wa_conversations, seats, locations.'))

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
        """The PUBLIC agent's business knowledge: the manual system_prompt plus
        every ACTIVE knowledge source (M-15) whose audience is public or both,
        in position order. Deterministic so the result stays prompt-cache
        friendly. `include_draft_pk` lets the test console preview one draft
        source before activating it.

        Phase E leak guard: a source marked 'internal' NEVER reaches this
        prompt, not even as the previewed draft (the audience filter is applied
        last, after the draft substitution)."""
        return self._compiled(KnowledgeSource.PUBLIC_AUDIENCES, include_draft_pk)

    def compiled_internal_prompt(self, include_draft_pk=None):
        """The INTERNAL assistant's knowledge (Phase E): the business header
        (system_prompt) plus every active source whose audience is internal or
        both. Public-only sources are left out: the employees' assistant answers
        from the internal documents, not from the marketing copy."""
        return self._compiled(KnowledgeSource.INTERNAL_AUDIENCES, include_draft_pk)

    def _compiled(self, audiences, include_draft_pk=None):
        parts = [self.system_prompt.strip()] if self.system_prompt.strip() else []
        # Phase F: the business's locations, as a deterministic block right
        # after the header so the model knows the options (and the rules can
        # say "listed below"). Part of compiled_prompt, so of config_version.
        locations_block = self.locations_block()
        if locations_block:
            parts.append(locations_block)
        sources = self.knowledge_sources.filter(status='active')
        try:
            draft_pk = int(include_draft_pk) if include_draft_pk else None
        except (TypeError, ValueError):
            draft_pk = None
        draft = (self.knowledge_sources.filter(pk=draft_pk, status='draft').first()
                 if draft_pk else None)
        if draft is not None:
            # Phase 6: the draft is a new version of something live — preview it
            # INSTEAD of the version it would replace, never next to it.
            hidden = draft.lineage_ids() - {draft.pk}
            sources = self.knowledge_sources.filter(
                (models.Q(status='active') & ~models.Q(pk__in=hidden)) | models.Q(pk=draft.pk))
        sources = sources.filter(audience__in=audiences)
        for src in sources.order_by('position', 'pk'):
            text = src.compiled_text()
            if text:
                parts.append(f'=== {src.title} ===\n{text}')
        return '\n\n'.join(parts)

    def locations_block(self):
        """'=== Locales ===' lines for the prompt, '' when the business has
        no Location rows. One line per location in display order:
        '- Bayamón (principal): Calle 1 · Tel. (787) 555-0100'."""
        from .phone import format_phone
        lines = []
        for loc in self.locations.all():
            label = f'- {loc.name}' + (' (principal)' if loc.is_default else '')
            details = [d for d in (loc.address, f'Tel. {format_phone(loc.phone)}' if loc.phone else '')
                       if d]
            lines.append(label + (': ' + ' · '.join(details) if details else ''))
        if not lines:
            return ''
        return '=== Locales ===\n' + '\n'.join(lines)

    def config_version(self):
        """Short fingerprint of the agent's effective configuration (M-19).
        Stamped on each conversation so any reply is attributable to the exact
        knowledge + greeting that produced it."""
        basis = (self.compiled_prompt() + '\n--\n' + (self.greeting or '')
                 + '\n--\n' + self.primary_language)
        # Appended only when on, so existing fingerprints don't change.
        if self.clinic_mode:
            basis += '\n--\nclinic'
        if self.agent_profile and self.agent_profile != 'web':
            basis += f'\n--\nprofile:{self.agent_profile}'
        return hashlib.sha1(basis.encode('utf-8')).hexdigest()[:12]


class ContactSubmission(models.Model):
    """A lead captured from the landing page contact form."""

    SERVICE_CHOICES = [
        ('operational-platform', 'Plataformas Operacionales a la Medida'),
        ('data-dashboards', 'Data, Dashboards y Sistemas de Reportes'),
        ('ai-automation', 'IA, Automatización y Optimización de Flujos'),
        ('landing-campaigns', 'Landing Pages, Formularios y Sistemas de Campaña'),
        ('consulting', 'Estrategia de Producto y Consultoría Técnica'),
        ('it-services', 'Gestión de Tecnología y Soporte de IT'),
    ]
    BUDGET_CHOICES = [
        ('under-1000', 'Menos de $1,000'),
        ('1000-3000', '$1,000 - $3,000'),
        ('3000-7500', '$3,000 - $7,500'),
        ('7500-plus', '$7,500+'),
        ('not-sure', 'Todavía no estoy seguro'),
    ]
    SOURCE_CHOICES = [
        ('contact_form', 'Contact form'),
        ('chat', 'AI chat'),
        ('signup', 'Agent signup'),
        # Phase D: a message the receptionist took for a person/department.
        ('message', 'Chat message'),
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
    # Double opt-in: set when the visitor clicks "Confirmar mi correo" in the
    # confirmation email (views.lead_confirm). Null = not confirmed (or no
    # email, or a lead from before the feature).
    email_verified_at = models.DateTimeField(null=True, blank=True)

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


class LeadNote(models.Model):
    """Phase 7: an internal note a tenant member leaves on a lead (mini-CRM
    history). Never shown to the visitor. Goes with the lead (CASCADE) and is
    removed when the lead is anonymized."""
    MAX_CHARS = 5000

    lead = models.ForeignKey(
        ContactSubmission, on_delete=models.CASCADE, related_name='notes')
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='lead_notes')
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']

    def __str__(self):
        return f'Nota lead:{self.lead_id} · {self.created_at:%Y-%m-%d %H:%M}'


class Location(models.Model):
    """Phase F: one branch/office of a multi-location business. Everything that
    existed before a business had Location rows (availability rules, bookings,
    staff contacts) keeps location=NULL, which means "the whole business"; a
    business with 0 or 1 locations behaves exactly as before."""
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='locations')
    name = models.CharField(max_length=80)
    address = models.CharField(max_length=200, blank=True)
    # E.164 (landing.phone.normalize_phone), like every other stored phone.
    phone = models.CharField(max_length=30, blank=True)
    # The main location: labelled "(principal)" in the prompt. At most one.
    is_default = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['client', 'name'],
                                    name='uniq_location_name_per_client'),
            models.UniqueConstraint(fields=['client'], condition=models.Q(is_default=True),
                                    name='uniq_default_location_per_client'),
        ]

    def __str__(self):
        return f'{self.client.slug} · {self.name}'


def fold_text(text):
    """Lower-case, accent-free, single-spaced copy of `text` for tolerant
    matching ('Bayamón' == 'bayamon') and for SMS bodies, which stay in the
    GSM-7 alphabet like the rest of landing.notify's texts."""
    text = unicodedata.normalize('NFKD', str(text or ''))
    text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    return ' '.join(text.lower().split())


def match_location(client, text):
    """The client's Location that `text` (a tool input, the visitor's words)
    refers to, or None. Case- and accent-insensitive; a name contained in the
    text or the text contained in the name both count, like route_contact.
    Exact matches win; a containment that fits SEVERAL locations ('Bayamón'
    vs 'Bayamón Norte' / 'Bayamón Sur') is ambiguous and returns None: with
    more than one location the handlers never guess."""
    if client is None:
        return None
    needle = fold_text(text)
    if not needle:
        return None
    locations = list(client.locations.all())
    for loc in locations:
        if fold_text(loc.name) == needle:
            return loc
    partial = []
    for loc in locations:
        name = fold_text(loc.name)
        # A name inside the text always counts ("el de Caguas"); the text
        # inside a name only from 3 characters, so "a" or "n" never pick one.
        if name and (name in needle or (len(needle) >= 3 and needle in name)):
            partial.append(loc)
    return partial[0] if len(partial) == 1 else None


def _check_location_tenant(instance):
    """ValidationError when `instance.location` belongs to another client.
    Shared by the clean() of every model with a Location FK: the admin and
    any form path must never attach a tenant's row to a foreign location."""
    if instance.location_id is None or instance.client_id is None:
        return
    owner = (Location.objects.filter(pk=instance.location_id)
             .values_list('client_id', flat=True).first())
    if owner is not None and owner != instance.client_id:
        raise ValidationError({'location': 'El local debe ser del mismo cliente.'})


def rules_for(client, location=None):
    """The AvailabilityRules in force at `location`: its own rules when it has
    any, otherwise the business-wide rules (location NULL). Without a location
    only the business-wide rules apply. [] without a client or without rules
    (= any future slot, as before)."""
    if client is None:
        return []
    if location is not None:
        # Scoped to the client too: a foreign location (bad caller, admin
        # mistake) must never lend its hours to this tenant.
        own = list(location.availability_rules.filter(client=client))
        if own:
            return own
    return list(client.availability_rules.filter(location__isnull=True))


# Where a conversation (and the bookings it produces) comes from. Shared by
# Conversation.channel and Booking.channel; the web widget keys its rows by
# widget_session, the other channels by external_id.
CHANNEL_CHOICES = [
    ('web', 'Widget web'),
    ('internal', 'Asistente interno'),
    ('whatsapp', 'WhatsApp'),
]


class Booking(models.Model):
    """A reservation/appointment the agent can take. Double-booking is prevented
    at the database level by a partial unique constraint per (client, start),
    or per (location, start) when the booking is for one location (Phase F)."""
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
    # Phase F: which location the visitor booked at. NULL = the business has
    # no locations (or the booking predates them). SET_NULL: removing a
    # location must never delete its past bookings.
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name='bookings')

    name = models.CharField(max_length=120)
    # A booking needs one way to reach the visitor: email OR phone (Phase D,
    # phone-only bookings; enforced by the booking handler). Either may be blank.
    email = models.EmailField(blank=True, db_index=True)
    phone = models.CharField(max_length=30, blank=True)
    service = models.CharField(max_length=120, blank=True)
    notes = models.TextField(blank=True)

    # Absolute instant (aware, America/Puerto_Rico). Never store loose date+time strings.
    start = models.DateTimeField(db_index=True)
    duration_minutes = models.PositiveSmallIntegerField(default=30)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)
    # M-05: set by the reminder cron so a booking is reminded at most once.
    reminder_sent = models.BooleanField(default=False)
    # Phase G: the channel the visitor booked from. Decides how they are told
    # about reminders and status changes (views.booking_notify_visitor).
    channel = models.CharField(max_length=16, choices=CHANNEL_CHOICES, default='web')
    # Phase G: the WhatsApp id (E.164 digits, no '+') the visitor wrote FROM,
    # for the outbound templates. Kept apart from `phone` (what they typed,
    # maybe someone else's number) so a business-initiated message only ever
    # goes to the number that opened the conversation. '' off WhatsApp.
    wa_id = models.CharField(max_length=20, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['start']
        constraints = [
            # One active booking per slot per client — DB-level anti double-booking.
            # Phase F: business-wide bookings and per-location bookings are
            # two separate pools (SQL UNIQUE treats NULLs as distinct, so the
            # location column cannot simply join the first constraint).
            models.UniqueConstraint(
                fields=['client', 'start'],
                condition=models.Q(status__in=['pending', 'confirmed'], location__isnull=True),
                name='uniq_active_booking_per_slot',
            ),
            models.UniqueConstraint(
                fields=['location', 'start'],
                condition=models.Q(status__in=['pending', 'confirmed'], location__isnull=False),
                name='uniq_active_booking_per_location_slot',
            ),
        ]

    def __str__(self):
        return f'{self.name} @ {self.start:%Y-%m-%d %H:%M} ({self.status})'

    def clean(self):
        super().clean()
        _check_location_tenant(self)


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
    # Phase E: employee only talks to the internal assistant (and changes their
    # own password); never leads, conversations, bookings, reports or settings.
    ROLE_CHOICES = [('org_admin', 'Administrador'), ('agent', 'Agente'),
                    ('employee', 'Empleado')]
    # Roles that may open the internal assistant page (Phase E).
    INTERNAL_ROLES = ('employee', 'agent', 'org_admin')
    # Roles that see the dashboard (leads, conversations, reports...).
    DASHBOARD_ROLES = ('org_admin', 'agent')
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
    # The first four are the launch plans (daily message cap). The packages
    # (small..premium) are sold direct and measured in conversations per month,
    # in three price steps each (see views.PLANS / PLAN_LIMITS and `step`).
    PLAN_CHOICES = [('starter', 'Starter'), ('pro', 'Pro'),
                    ('scale', 'Scale'), ('custom', 'Custom'),
                    ('small', 'Chat Básico'), ('medium', 'Recepcionista'),
                    ('large', 'Recepcionista + Equipo'), ('premium', 'Premium WhatsApp')]
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
    # Recurring revenue override in cents per month. NULL = use the PLANS table
    # (landing.analytics.plan_monthly_revenue). Needed for custom plans.
    monthly_price_cents = models.PositiveIntegerField(
        null=True, blank=True,
        help_text='Override del precio mensual (planes custom o descuentos)')
    # Phase C: price step inside a package (1 = Entrada, 2 = Medio, 3 = Tope).
    # Selects the price and the included capacity in views.PLANS/PLAN_LIMITS.
    # Ignored by the launch plans, which have a single step.
    step = models.PositiveSmallIntegerField(
        default=1, help_text='Escalón del paquete: 1 Entrada, 2 Medio, 3 Tope.')
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
    # Where the turns come from. The web widget keys the row by widget_session;
    # the other channels by external_id (see the partial unique constraints).
    CHANNEL_CHOICES = CHANNEL_CHOICES
    client = models.ForeignKey(Client, on_delete=models.PROTECT, related_name='conversations')
    # Random id generated by the widget per visitor session — the upsert key
    # for channel='web'. Blank for the other channels.
    widget_session = models.CharField(max_length=64, blank=True, db_index=True)
    channel = models.CharField(max_length=16, choices=CHANNEL_CHOICES,
                               default='web', db_index=True)
    # Channel-side identity of the counterpart, never raw PII: e.g. the sha256
    # of a WhatsApp phone number. '' for web conversations.
    external_id = models.CharField(max_length=128, blank=True, default='', db_index=True)
    # The employee talking to the internal assistant (channel='internal').
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='internal_conversations')
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

    # Topic tagging (nightly tag_conversations). Written with .update() so the
    # auto_now last_message_at below is never bumped by the tagger.
    INTENT_CHOICES = [
        ('informacion', 'Información'),
        ('precios', 'Precios'),
        ('cita', 'Cita'),
        ('soporte', 'Soporte'),
        ('queja', 'Queja'),
        ('empleo', 'Empleo'),
        ('spam', 'Spam'),
        ('otro', 'Otro'),
    ]
    intent = models.CharField(max_length=16, choices=INTENT_CHOICES, blank=True, db_index=True)
    # Short model-written label; always blank for clinic_mode clients.
    topic = models.CharField(max_length=60, blank=True)
    tagged_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # The visitor's first message, for the operator's "what do they ask" view.
    # Blank for clinic_mode clients (no free health text outside the transcript).
    first_question = models.CharField(max_length=300, blank=True)

    started_at = models.DateTimeField(auto_now_add=True)
    last_message_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_message_at']
        indexes = [
            # Phase C: month usage / allowance / bill_overage filter by tenant
            # and the month the conversation started.
            models.Index(fields=['client', 'started_at'], name='conv_client_started_idx'),
        ]
        constraints = [
            # Web: one row per widget session. Other channels leave the session
            # blank, so the uniqueness only applies to the web channel.
            models.UniqueConstraint(fields=['client', 'widget_session'],
                                    condition=models.Q(channel='web'),
                                    name='uniq_conversation_per_session'),
            # Non-web channels: one row per (channel, counterpart). Web rows
            # keep external_id='' and are excluded, so they never collide.
            models.UniqueConstraint(fields=['client', 'channel', 'external_id'],
                                    condition=~models.Q(external_id=''),
                                    name='uniq_conversation_per_external_id'),
        ]

    def __str__(self):
        key = self.widget_session or self.external_id or self.channel
        return f'{self.client.slug} · {key[:8]} ({self.state})'


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
        ('site', 'Sitio web completo (escaneo con IA)'),
        ('file', 'Archivo subido (PDF, Word, texto)'),
    ]
    # Typed by hand in the "Añadir fuente" form. 'site'/'file' only come from
    # the ingest pipeline (landing.ingest + process_knowledge).
    MANUAL_KINDS = ('text', 'faq', 'url')
    INGEST_KINDS = ('site', 'file')
    # Phase E: which assistant a source feeds. 'internal' NEVER reaches the
    # public widget (Client.compiled_prompt); 'public' never reaches the
    # employees' assistant (Client.compiled_internal_prompt).
    AUDIENCE_CHOICES = [
        ('public', 'Público (widget del sitio)'),
        ('internal', 'Solo equipo interno'),
        ('both', 'Ambos'),
    ]
    PUBLIC_AUDIENCES = ('public', 'both')
    INTERNAL_AUDIENCES = ('internal', 'both')
    STATUS_CHOICES = [
        ('pending', 'En cola'), ('processing', 'Procesando'),
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
    audience = models.CharField(
        'Audiencia', max_length=8, choices=AUDIENCE_CHOICES, default='public',
        help_text=('Público: solo el agente del sitio web. Solo equipo interno: solo el '
                   'asistente interno de los empleados (nunca llega al widget). '
                   'Ambos: los dos.'))
    error = models.CharField(max_length=300, blank=True)
    review_by = models.DateField(
        null=True, blank=True, help_text='Fecha en que este contenido debe revisarse.')
    position = models.PositiveIntegerField(default=0)
    # Background ingest (kind site/file): human-readable step shown to the
    # tenant while pending/processing, pages read, and how many times a worker
    # claimed the row (a crash mid-run is retried once, then lands in error).
    progress = models.CharField(max_length=120, blank=True)
    pages_scanned = models.PositiveSmallIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    # Ingest drafts: JSON written by ingest.review_contacts (phones, emails,
    # URLs found, off-domain links removed) for the tenant to confirm before
    # activating. Empty = nothing to review. Read it through .review.
    review_notes = models.TextField(blank=True, default='')
    # Phase 6 versioning: the source this one is a new version of. Editing an
    # active source creates a draft with replaces=<active>; activating it
    # archives the old one. Sources linked through `replaces` (either way) form
    # a lineage, and at most one source per lineage is active.
    replaces = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.SET_NULL, related_name='revisions')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', 'pk']
        constraints = [
            # One website crawl queued/running per tenant: a crawl is the
            # expensive job (30 pages + model calls), so this is enforced by
            # the database, not just by the view's check.
            models.UniqueConstraint(
                fields=['client'],
                condition=models.Q(kind='site', status__in=['pending', 'processing']),
                name='uniq_running_site_scan_per_client',
            ),
        ]

    def __str__(self):
        return f'{self.client.slug} · {self.title} ({self.status})'

    def clean(self):
        super().clean()
        # A lineage never crosses tenants: publishing archives every active
        # version of the lineage, so a cross-client link (e.g. set in the
        # admin) would let one tenant's publish retire another's knowledge.
        if self.replaces_id is not None:
            if self.replaces_id == self.pk:
                raise ValidationError({'replaces': 'Una fuente no puede reemplazarse a sí misma.'})
            other_client = (KnowledgeSource.objects.filter(pk=self.replaces_id)
                            .values_list('client_id', flat=True).first())
            if other_client is not None and other_client != self.client_id:
                raise ValidationError(
                    {'replaces': 'Solo puede reemplazar una fuente del mismo cliente.'})

    @property
    def in_progress(self):
        return self.status in ('pending', 'processing')

    @property
    def review(self):
        """review_notes as a dict of lists ({} when empty or unreadable)."""
        if not self.review_notes:
            return {}
        try:
            data = json.loads(self.review_notes)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: [str(v) for v in vals][:50] for k, vals in data.items()
                if isinstance(vals, list) and vals}

    def compiled_text(self):
        """Fragments if the processor produced them, else the raw content."""
        frags = list(self.fragments.order_by('position').values_list('content', flat=True))
        return '\n'.join(frags).strip() if frags else self.content.strip()

    def lineage_ids(self):
        """pks of every source of this client connected to this one through
        `replaces`, in either direction (this one included). One query: a
        tenant has tens of sources, so the walk happens in memory."""
        edges = KnowledgeSource.objects.filter(
            client_id=self.client_id, replaces__isnull=False,
        ).values_list('pk', 'replaces_id')
        neighbours = {}
        for pk, parent in edges:
            neighbours.setdefault(pk, set()).add(parent)
            neighbours.setdefault(parent, set()).add(pk)
        seen, stack = {self.pk}, [self.pk]
        while stack:
            for other in neighbours.get(stack.pop(), ()):
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        return seen


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
        constraints = [
            # Phase C: the 80% / 100% allowance warnings ('allowance.80',
            # 'allowance.wa.100', ...) are recorded once per client and month
            # (target 'YYYY-MM'); two first turns racing past the cache can
            # not both email the client. Other actions are unaffected.
            models.UniqueConstraint(fields=['client', 'action', 'target'],
                                    condition=models.Q(action__startswith='allowance.'),
                                    name='uniq_allowance_audit_per_month'),
        ]

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
    # Phase F: NULL = the whole business; set = this location's own window,
    # which replaces the business-wide one for that location (rules_for).
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.CASCADE,
        related_name='availability_rules')
    weekday = models.PositiveSmallIntegerField()  # 0=Monday … 6=Sunday
    open_time = models.TimeField()
    close_time = models.TimeField()
    slot_minutes = models.PositiveSmallIntegerField(default=30)

    class Meta:
        ordering = ['weekday', 'open_time']
        constraints = [
            # One window per weekday for the business, and one per weekday
            # per location (partial: NULLs are distinct in SQL UNIQUE).
            models.UniqueConstraint(fields=['client', 'weekday'],
                                    condition=models.Q(location__isnull=True),
                                    name='uniq_availability_per_weekday'),
            models.UniqueConstraint(fields=['location', 'weekday'],
                                    condition=models.Q(location__isnull=False),
                                    name='uniq_availability_per_location_weekday'),
        ]

    def __str__(self):
        days = ['lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom']
        where = f' [{self.location.name}]' if self.location_id else ''
        return f'{self.client.slug}{where} · {days[self.weekday]} {self.open_time}-{self.close_time}'

    def clean(self):
        super().clean()
        _check_location_tenant(self)


class StaffContact(models.Model):
    """Phase D: who receives the receptionist's messages (take_message) for a
    department or by name, and over which channel. No contacts = everything
    goes to Client.notify_email / notify_phone, as before."""
    CHANNEL_CHOICES = [('email', 'Email'), ('sms', 'Email + SMS'), ('whatsapp', 'Email + WhatsApp')]

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='staff_contacts')
    name = models.CharField(max_length=120)
    department = models.CharField(
        max_length=80, blank=True,
        help_text='Departamento que atiende (Ventas, Facturación...). En blanco = solo por nombre.')
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    channel = models.CharField(max_length=12, choices=CHANNEL_CHOICES, default='email')
    # Phase F: the location this contact serves. NULL = every location.
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='staff_contacts')
    # The contact that gets a message no department or person matched.
    is_default = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'pk']

    def __str__(self):
        dept = f' ({self.department})' if self.department else ''
        return f'{self.client.slug} · {self.name}{dept}'

    def clean(self):
        super().clean()
        _check_location_tenant(self)


def route_contact(client, department=None, recipient=None, location=None):
    """The StaffContact a message for `department`/`recipient` goes to, or
    None when the client's own notify_email/notify_phone should be used.
    Precedence (Phase F): the person named > the department at `location` >
    the department anywhere > the location's own contact (no department) >
    the default contact. Without `location` this is the Phase D order:
    person > department > default. Matching is case-insensitive and tolerant
    of 'Dra. Santos' vs 'Santos'."""
    if client is None:
        return None
    contacts = list(client.staff_contacts.all())
    if not contacts:
        return None
    recipient = (recipient or '').strip().lower()
    department = (department or '').strip().lower()
    loc_id = getattr(location, 'pk', None)
    if recipient:
        for c in contacts:
            name = c.name.strip().lower()
            if name and (name in recipient or recipient in name):
                return c
    if department:
        in_dept = [c for c in contacts
                   if c.department and c.department.strip().lower() == department]
        if loc_id is not None:
            for c in in_dept:
                if c.location_id == loc_id:
                    return c
        # A contact of another location is still the department's contact
        # when nothing closer exists (never a silent drop of the message).
        in_dept.sort(key=lambda c: c.location_id is not None)
        if in_dept:
            return in_dept[0]
    if loc_id is not None:
        for c in contacts:
            if c.location_id == loc_id and not c.department:
                return c
    for c in contacts:
        if c.is_default:
            return c
    return None


class ChannelAccount(models.Model):
    """Phase G: a messaging number of a tenant on an external channel. Today
    only WhatsApp through Meta's Cloud API: the number lives in DOMINIO's
    WhatsApp Business Account, so per tenant there is no secret, only the
    `phone_number_id` Meta sends in every webhook (the tenant lookup key).
    One active number per tenant and channel."""
    PROVIDER_CHOICES = [('meta', 'Meta Cloud API')]
    CHANNEL_CHOICES = [('whatsapp', 'WhatsApp')]
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='channel_accounts')
    provider = models.CharField(max_length=16, choices=PROVIDER_CHOICES, default='meta')
    channel = models.CharField(max_length=16, choices=CHANNEL_CHOICES, default='whatsapp')
    # Meta's id of the phone number (NOT the phone number itself).
    phone_number_id = models.CharField(max_length=40)
    waba_id = models.CharField(max_length=40, blank=True)
    # '+1 787-555-0100': display only (dashboard, admin), never a lookup key.
    display_phone = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['provider', 'channel', 'phone_number_id'],
                                    name='uniq_channel_address'),
            models.UniqueConstraint(fields=['client', 'channel'],
                                    condition=models.Q(is_active=True),
                                    name='uniq_active_channel_per_client'),
        ]

    def __str__(self):
        return f'{self.client.slug} · {self.channel} {self.display_phone or self.phone_number_id}'

    def clean(self):
        super().clean()
        # Health data over a consumer messaging app is a line the service does
        # not cross: the webhook also refuses such tenants (defense in depth).
        if self.client_id and self.client.clinic_mode:
            raise ValidationError(
                'Un negocio en modo clínica no puede atender por WhatsApp.')


class ChannelOptOut(models.Model):
    """Phase G: a contact that said STOP on a channel. The durable record
    (the cache is not evidence): no business-initiated message (reminder,
    status template) goes to them until they say START. Keyed by the same
    salted hash as the conversations, never the number."""
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='channel_opt_outs')
    channel = models.CharField(max_length=16, default='whatsapp')
    contact_hash = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['client', 'channel', 'contact_hash'],
                                    name='uniq_opt_out'),
        ]

    def __str__(self):
        return f'{self.client.slug} · {self.channel} · {self.contact_hash[:8]}'


class UsageEvent(models.Model):
    """One billed Anthropic call (or tool loop) and what it cost.

    The ledger behind cost and usage dashboards. It holds no conversation text
    or visitor data, so it is never purged: client_slug is copied at write time
    so totals survive a deleted tenant, and cost_usd is frozen with the price
    in force when the call happened.
    """
    KIND_CHOICES = [
        ('chat', 'Chat'),
        ('demo', 'Demo'),
        ('knowledge_test', 'Prueba de conocimiento'),
        ('greeting', 'Saludo'),
        ('eval', 'Evaluación'),
        ('tagging', 'Etiquetado'),
        ('knowledge_ingest', 'Conocimiento (escaneo/archivo)'),
        ('internal_chat', 'Chat interno'),
        ('whatsapp', 'WhatsApp'),
    ]
    client = models.ForeignKey(
        Client, null=True, blank=True, on_delete=models.SET_NULL, related_name='usage_events')
    client_slug = models.CharField(max_length=60, blank=True)
    conversation = models.ForeignKey(
        Conversation, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='usage_events')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    model = models.CharField(max_length=60, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cache_write_tokens = models.PositiveIntegerField(default=0)
    api_calls = models.PositiveSmallIntegerField(default=0)
    effort = models.CharField(max_length=10, blank=True)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['client', 'created_at'], name='usage_client_created_idx'),
            models.Index(fields=['kind', 'created_at'], name='usage_kind_created_idx'),
        ]

    @property
    def total_tokens(self):
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens)

    def __str__(self):
        return f'{self.kind} · {self.client_slug or "-"} · {self.created_at:%Y-%m-%d %H:%M}'
