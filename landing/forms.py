from django import forms
from django.utils.text import slugify

from .models import Client, ContactSubmission


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
            'enable_bookings', 'is_active',
        ]
        widgets = {
            'system_prompt': forms.Textarea(attrs={'rows': 10}),
            'greeting': forms.Textarea(attrs={'rows': 2}),
            'allowed_origins': forms.Textarea(attrs={'rows': 2}),
            # All three colors use the native RGB color picker.
            'primary_color': forms.TextInput(attrs={'type': 'color'}),
            'surface_color': forms.TextInput(attrs={'type': 'color'}),
            'header_color': forms.TextInput(attrs={'type': 'color'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The slug is derived from the name; the user never has to invent one.
        self.fields['slug'].required = False
        self.fields['slug'].help_text = (
            'The public ?key= in your embed snippet. Leave blank to auto-generate '
            'from the name — it will be made unique automatically.')

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


class ContactForm(forms.ModelForm):
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
            raise forms.ValidationError('Spam detected.')
        return ''

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get('email') and not cleaned.get('phone'):
            raise forms.ValidationError('Please leave an email or a phone number so we can reach you.')
        return cleaned
