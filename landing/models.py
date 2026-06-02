from django.db import models


class ContactSubmission(models.Model):
    """A lead captured from the landing page contact form."""

    SERVICE_CHOICES = [
        ('custom-platform', 'Custom Web Platform'),
        ('dashboard', 'Dashboard / Data System'),
        ('automation', 'AI / Automation'),
        ('landing', 'Landing Page / Campaign System'),
        ('cloud', 'Cloud / Deployment'),
        ('consulting', 'Consulting'),
    ]
    BUDGET_CHOICES = [
        ('under-1000', 'Under $1,000'),
        ('1000-3000', '$1,000 - $3,000'),
        ('3000-7500', '$3,000 - $7,500'),
        ('7500-plus', '$7,500+'),
        ('not-sure', 'Not sure yet'),
    ]

    name = models.CharField(max_length=120)
    email = models.EmailField()
    company = models.CharField(max_length=160, blank=True)
    service = models.CharField(max_length=40, choices=SERVICE_CHOICES)
    budget = models.CharField(max_length=40, choices=BUDGET_CHOICES, blank=True)
    message = models.TextField()

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
