from django import forms

from .models import ContactSubmission


class ContactForm(forms.ModelForm):
    """Validates the landing contact form and includes a honeypot anti-spam field."""

    # Honeypot: real users never see/fill this. Bots usually do.
    website = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = ContactSubmission
        fields = ['name', 'email', 'company', 'service', 'budget', 'message']

    def clean_website(self):
        if self.cleaned_data.get('website'):
            raise forms.ValidationError('Spam detected.')
        return ''
