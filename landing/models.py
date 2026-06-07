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

    # Comma-separated domains the widget is allowed to run on (anti-abuse).
    # Blank = no origin restriction (use only for your own site while testing).
    allowed_origins = models.TextField(
        blank=True,
        help_text='Comma-separated domains allowed to embed the widget, e.g. example.com, www.example.com')

    primary_color = models.CharField(max_length=9, default='#34d6c8')
    # When on, the agent can take reservations/appointments (the create_booking tool).
    enable_bookings = models.BooleanField(
        default=False, help_text='Let the agent take reservations / appointments.')
    is_active = models.BooleanField(default=True)
    # Set once the install-instructions email has been sent, so activating an
    # already-onboarded client doesn't re-send it.
    onboarding_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.slug})'

    def origin_list(self):
        return [o.strip().lower() for o in self.allowed_origins.split(',') if o.strip()]


class ContactSubmission(models.Model):
    """A lead captured from the landing page contact form."""

    SERVICE_CHOICES = [
        ('operational-platform', 'Custom Operational Platforms'),
        ('data-dashboards', 'Data, Dashboards & Reporting Systems'),
        ('ai-automation', 'AI, Automation & Workflow Optimization'),
        ('landing-campaigns', 'Landing Pages, Forms & Campaign Systems'),
        ('consulting', 'Product Strategy & Technical Consulting'),
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
    email = models.EmailField()
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'client'],
                                    name='uniq_membership_user_client'),
        ]

    def __str__(self):
        return f'{self.user} → {self.client.slug}'
