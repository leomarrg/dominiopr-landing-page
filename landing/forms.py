from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm
from django.utils.text import slugify

from . import human
from .emailcheck import check_email
from .models import Client, ContactSubmission, LeadNote
from .phone import normalize_phone


class HumanCheckMixin:
    """Phase 5: require a solved hCaptcha checkbox ('h-captcha-response')
    when human.enabled(); a no-op while the keys are empty.

    Checked in clean() BEFORE the parent's clean(), so on the login forms a
    bot never reaches authenticate() (no password probing without a human).
    The visitor IP comes from `self.human_ip` (set by the view) or, on the
    auth forms, from `self.request`. It is optional for hCaptcha.
    """

    human_failed = False
    human_ip = None

    def _human_remote_ip(self):
        if self.human_ip:
            return self.human_ip
        request = getattr(self, 'request', None)
        if request is None:
            return None
        from .views import _client_ip  # views imports this module
        return _client_ip(request)

    def clean(self):
        if human.enabled() and not human.verify(
                self.data.get('h-captcha-response', ''), self._human_remote_ip()):
            self.human_failed = True
            raise forms.ValidationError(human.FORM_ERROR, code='human')
        return super().clean()


class HumanAuthenticationForm(HumanCheckMixin, AuthenticationForm):
    """Dashboard login (/dashboard/login/)."""


class HumanPasswordResetForm(HumanCheckMixin, PasswordResetForm):
    """'Forgot password' request: it sends email, so bots must not drive it."""


class HumanAdminAuthenticationForm(HumanCheckMixin, AdminAuthenticationForm):
    """/admin/login/: a superuser there sees every tenant."""


class ClientForm(forms.ModelForm):
    """Create/edit a client agent in the factory (the per-business config)."""

    # Plain CharField (not the model's SlugField) so messy input — spaces,
    # accents, punctuation — is normalized in clean_slug instead of rejected.
    slug = forms.CharField(required=False)

    class Meta:
        model = Client
        fields = [
            'name', 'slug', 'system_prompt', 'greeting', 'notify_email',
            'allowed_origins', 'primary_color', 'surface_color', 'header_color',
            'enable_bookings', 'clinic_mode', 'is_active',
            # Self-serve provisioning state (webhook sets pending; staff flips live)
            'setup_status', 'website_url', 'platform',
            # Phase 2 operation knobs (M-04/M-06/M-10/M-14)
            'primary_language', 'daily_message_cap', 'retention_months',
            'notify_phone', 'notify_channel',
        ]
        widgets = {
            'system_prompt': forms.Textarea(attrs={'rows': 10}),
            'greeting': forms.Textarea(attrs={'rows': 2}),
            'allowed_origins': forms.Textarea(attrs={'rows': 2}),
            # All three colors use the native RGB color picker.
            'primary_color': forms.TextInput(attrs={'type': 'color'}),
            'surface_color': forms.TextInput(attrs={'type': 'color'}),
            'header_color': forms.TextInput(attrs={'type': 'color'}),
            'notify_phone': forms.TextInput(attrs={'placeholder': '+17871234567'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The slug is derived from the name; the user never has to invent one.
        self.fields['slug'].required = False
        self.fields['slug'].help_text = (
            'El ?key= público de tu snippet de instalación. Déjalo en blanco para '
            'generarlo automáticamente del nombre — se hace único por sí solo.')

    def clean_slug(self):
        # Normalize whatever was typed (spaces, accents, casing). Blank is OK:
        # clean() derives one from the name and guarantees uniqueness.
        return slugify(self.cleaned_data.get('slug', ''))

    def clean(self):
        cleaned = super().clean()
        base = cleaned.get('slug') or slugify(cleaned.get('name', '')) or 'agent'
        # Append -2, -3, … until the slug is free (ignoring this same row on edit).
        siblings = Client.objects.all()
        if self.instance.pk:
            siblings = siblings.exclude(pk=self.instance.pk)
        slug, n = base, 2
        while siblings.filter(slug=slug).exists():
            slug = f'{base}-{n}'
            n += 1
        cleaned['slug'] = slug
        return cleaned


class ContactForm(HumanCheckMixin, forms.ModelForm):
    """Validates the landing contact form and includes a honeypot anti-spam field."""

    # Honeypot: real users never see/fill this. Bots usually do.
    website = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = ContactSubmission
        fields = ['name', 'email', 'phone', 'company', 'service', 'budget', 'message']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Email is no longer required on its own — a phone is enough (see clean()).
        self.fields['email'].required = False

    def clean_website(self):
        if self.cleaned_data.get('website'):
            raise forms.ValidationError('Se detectó spam.')
        return ''

    def clean_email(self):
        # Optional here (a phone is enough), but a given email must be one that
        # can actually receive our reply: no throwaway inboxes, no dead domains.
        email = (self.cleaned_data.get('email') or '').strip()
        if email:
            ok, reason = check_email(email)
            if not ok:
                raise forms.ValidationError(reason)
        return email

    def clean_phone(self):
        # Normalize to E.164, or reject garbage. Blank stays blank (optional).
        normalized = normalize_phone(self.cleaned_data.get('phone', ''))
        if normalized is None:
            raise forms.ValidationError('Escribe un número de teléfono válido, ej. (787) 123-4567.')
        return normalized

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('email') and not cleaned.get('phone'):
            raise forms.ValidationError('Déjanos un email o un número de teléfono para poder contactarte.')
        return cleaned


class LeadNoteForm(forms.Form):
    """Phase 7: an internal note on a lead. The length cap is enforced here,
    server-side (the textarea maxlength is only a hint)."""
    text = forms.CharField(
        max_length=LeadNote.MAX_CHARS, strip=True,
        widget=forms.Textarea(attrs={'rows': 3, 'maxlength': LeadNote.MAX_CHARS}),
        error_messages={
            'required': 'La nota no puede estar vacía.',
            'max_length': f'La nota no puede pasar de {LeadNote.MAX_CHARS} caracteres.',
        })
